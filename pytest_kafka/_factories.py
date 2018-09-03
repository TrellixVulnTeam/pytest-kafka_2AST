"""Kafka fixures for Pytest."""
import os
import signal
import logging
from pathlib import Path
from time import time, sleep
from typing import List, Callable, Optional, Tuple, TYPE_CHECKING
from subprocess import Popen, TimeoutExpired
from kafka import KafkaProducer, KafkaConsumer  # type: ignore
from kafka.errors import NoBrokersAvailable  # type: ignore
import pytest  # type: ignore
import port_for  # type: ignore
if TYPE_CHECKING:
    # Don't break anything else than typechecking if pytest changes.
    from _pytest.fixtures import SubRequest  # type: ignore  # noqa


ROOT = Path(__name__).parent


KAFKA_SERVER_CONFIG_TEMPLATE = '''
reserved.broker.max.id=65535
broker.id={kafka_port}
listeners=PLAINTEXT://:{kafka_port}
log.dirs={kafka_log_dir}
num.partitions=1
# The number of threads lowered to 1 - may boost startup time:
num.recovery.threads.per.data.dir=1
num.network.threads=1
num.io.threads=1
log.retention.hours=1
log.segment.bytes=1073741824
zookeeper.connect=localhost:{zk_port}
zookeeper.connection.timeout.ms=6000
offsets.topic.replication.factor=1
default.replication.factor=1
'''

ZOOKEEPER_CONFIG_TEMPLATE = '''
dataDir={zk_data_dir}
clientPort={zk_port}
maxClientCnxns=0
'''

DEFAULT_CONSUMER_TIMEOUT_MS = 500


def wait_until(cond: Callable[[], bool], timeout: float = 15, interval: float = 0.1):
    """Poll until the condition is True."""
    start = time()
    end = start + timeout
    while time() <= end:
        if cond() is True:
            return
        sleep(interval)

    raise AssertionError("Condition not true in {} seconds".format(timeout))


def write_config(template_string: str, destination: Path, **template_vars) -> None:
    """
    Render the specified config template into the configs_dir.

    :param template_string: Python str.format template string
    :param destination: file to render the template into (create if needed)
    """
    rendered = template_string.format(**template_vars)
    destination.write_text(rendered)


def teardown(proc):
    """Kill the process with TERM and wait for it."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def get_tmpdir_fixture_name(scope: str) -> str:
    """Get appropriately-scoped tmpdir fixture."""
    if scope == 'session':
        return 'session_tmpdir_path'
    else:
        return 'tmpdir_path'


def make_zookeeper_process(
    zk_bin: str,
    zk_port: Optional[int] = None,
    zk_config_template: str = ZOOKEEPER_CONFIG_TEMPLATE,
    scope: str = 'function',
) -> Callable[..., Tuple[Popen, int]]:
    """
    Make a Zookeeper fixture.

    The fixture will spawn a Zookeeper process in a new process group and return its process handle
    and port number. Data will be stored in a Pytest-provided temporary directory.

    :param zk_bin: path to Zookeeper launch script (typically to bin/zookeeper-server-start.sh)
    :param zk_port: Zookeeper port (random free port by default)
    :param zk_config_template: Zookeeper config template, must use keys ``zk_data_dir`` and
        ``zk_port``
    :param scope: 'function' or 'session'
    """
    @pytest.fixture(scope=scope)
    def zookeeper_process(request: 'SubRequest') -> Tuple[Popen, int]:
        """Configure and start a Zookeeper service."""
        used_zk_port = port_for.select_random() if zk_port is None else zk_port
        tempdir_path = request.getfixturevalue(get_tmpdir_fixture_name(scope))

        zk_dir = tempdir_path / 'zookeeper-{}'.format(used_zk_port)
        zk_data_dir = zk_dir / 'data'
        zk_data_dir.mkdir(parents=True)
        zk_config_path = zk_dir / 'zookeeper.properties'

        write_config(
            zk_config_template, zk_config_path,
            zk_port=used_zk_port,
            zk_data_dir=zk_data_dir
        )

        zk_proc = Popen(
            [zk_bin, str(zk_config_path)],
            start_new_session=True,
        )

        request.addfinalizer(lambda: teardown(zk_proc))

        # Kafka will wait for zookeeper, not need to poll it here.
        # If you use the zookeeper fixure alone, I'm sorry.
        return zk_proc, used_zk_port

    return zookeeper_process


def make_kafka_server(
    kafka_bin: str,
    zookeeper_fixture_name: str,
    kafka_port: Optional[int] = None,
    kafka_config_template: str = KAFKA_SERVER_CONFIG_TEMPLATE,
    scope: str = 'function',
) -> Callable[..., Tuple[Popen, int]]:
    """
    Make a Kafka fixture.

    The fixture will spawn a Kafka process in a new process group and return its process handle
    and port number. Data will be stored in a Pytest-provided temporary directory.

    :param zookeeper_fixture_name: the name of the Zookeeper fixture to depend on. The scope must
        not be wider than this fixture's scope.
    :param kafka_bin: path to Kafka launch script (typically to bin/kafka-server-start.sh)
    :param kafka_port: Kafka port (random free port by default)
    :param kafka_config_template: Kafka config template, must use keys ``kafka_log_dir`` and
        ``kafka_port``
    :param scope: 'function' or 'session'
    """
    @pytest.fixture(scope=scope)
    def kafka_server(request: 'SubRequest') -> Tuple[Popen, int]:
        """Configure and start a Kafka server."""
        _, zk_port = request.getfixturevalue(zookeeper_fixture_name)
        used_kafka_port = port_for.select_random() if kafka_port is None else kafka_port
        tempdir_path = request.getfixturevalue(get_tmpdir_fixture_name(scope))

        kafka_dir = tempdir_path / 'kafka-server-{}'.format(used_kafka_port)
        kafka_log_dir = kafka_dir / 'logs'
        kafka_log_dir.mkdir(parents=True)
        kafka_config_path = kafka_dir / 'kafka-server.properties'

        write_config(
            kafka_config_template, kafka_config_path,
            zk_port=zk_port,
            kafka_port=used_kafka_port,
            kafka_log_dir=kafka_log_dir
        )

        kafka_proc = Popen(
            [kafka_bin, str(kafka_config_path)],
            start_new_session=True,
        )

        request.addfinalizer(lambda: teardown(kafka_proc))

        def kafka_started():
            assert kafka_proc.poll() is None, 'Kafka process must not terminate'
            try:
                producer = KafkaProducer(bootstrap_servers='localhost:{}'.format(used_kafka_port))
                producer.close()
            except NoBrokersAvailable:
                return False
            return True

        # Silence kafka errors when polling.
        kafka_logger = logging.getLogger('kafka.producer.kafka')
        prev_propagate = kafka_logger.propagate
        try:
            kafka_logger.propagate = False
            wait_until(kafka_started)
        finally:
            kafka_logger.propagate = prev_propagate

        return kafka_proc, used_kafka_port

    return kafka_server


def make_kafka_consumer(
    kafka_fixture_name: str,
    kafka_topics: Optional[List[str]] = None,
    seek_to_beginning: bool = False,
    **consumer_kwargs
) -> Callable[..., KafkaConsumer]:
    """
    Make a Kafka consumer fixture.

    Unlike the other fixtures, the scope is always ``"function"``.

    :param kafka_fixture_name: the name of the Kafka fixture to depend on
    :param kafka_topics: topics to subscribe to
    :param seek_to_beginning: whether the consumer should consume from the earlies offsets. Solves
        the race condition between consumer setup and Kafka server + Producer setup but requires
        to know the topics upfront.
    :param consumer_kwargs: what to pass to KafkaConsumer.

    It's recommended to pass both ``kafka_topics`` and ``seek_to_beginning``.
    """
    if kafka_topics is None:
        kafka_topics = []

    @pytest.fixture
    def kafka_consumer(request: 'SubRequest') -> KafkaConsumer:
        """
        Get a connected Kafka consumer.

        Will consume from the beginning and with a timeout, so ``list(consumer)`` can be used.
        """
        _, kafka_port = request.getfixturevalue(kafka_fixture_name)

        used_consumer_kwargs = consumer_kwargs.copy()
        used_consumer_kwargs.setdefault('consumer_timeout_ms', DEFAULT_CONSUMER_TIMEOUT_MS)
        used_consumer_kwargs.setdefault('bootstrap_servers', 'localhost:{}'.format(kafka_port))

        consumer = KafkaConsumer(
            *kafka_topics,
            **used_consumer_kwargs,
        )

        if seek_to_beginning:
            assert kafka_topics, (
                'In order to be able to seek to beginning, we must have some partitions assigned '
                'for which we need to subscribe to topics.')

            def partitions_assigned():
                consumer.poll(timeout_ms=20)
                return len(consumer.assignment()) > 0

            wait_until(partitions_assigned)

            consumer.seek_to_beginning()
        return consumer

    return kafka_consumer
