"""Real UDS peer checks with synthetic retained executor authority."""
import os
import socket
import time
from types import SimpleNamespace
import pytest

pytest.importorskip('fcntl')
from hub_container_executor import launch_supervisor as launch
from tests.test_hub_container_executor_launch_supervisor import synthetic_issuer, short_root, root_journal, current_ready


@pytest.mark.parametrize('scenario', ['valid', 'wrong_uid', 'not_previously_pinned'])
def test_retained_executor_endpoint_peer_and_inode(synthetic_issuer, short_root, scenario):
    authority, _, checks = synthetic_issuer
    current_ready(authority)
    directory = short_root / 'endpoint'; directory.mkdir(mode=0o700)
    path = directory / 'executor.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path)); os.chmod(path, 0o600); server.listen(2)
        info = directory.stat()
        authority.profile = SimpleNamespace(role=None, executor_endpoint='protected-uds-v1')
        authority._mounts = {'executor_endpoint_directory': SimpleNamespace(
            source=str(directory), uid=os.geteuid(), gid=os.getegid())}
        authority._source_pins = {'executor_endpoint_directory': (info.st_dev, info.st_ino)}
        authority._executor_endpoint_name = path.name
        if scenario != 'not_previously_pinned': authority._check_executor_endpoint_directory()
        if scenario == 'wrong_uid': authority._consumer_uid += 1
        if scenario != 'valid':
            with pytest.raises(launch.LaunchSupervisorError): authority.attest_executor_endpoint(timeout_ms=1000)
            assert authority._held
        else:
            result = authority.attest_executor_endpoint(timeout_ms=1000)
            sock_info = path.lstat()
            assert result[0] == authority._launch_id and result[3] == os.getpid()
            assert result[-1] == (sock_info.st_dev, sock_info.st_ino) and len(checks) >= 2
        path.unlink()


def test_attestation_mutex_admission_is_bounded(synthetic_issuer):
    authority, _, _ = synthetic_issuer
    authority._mutex.acquire()
    try:
        start = time.monotonic()
        with pytest.raises(launch.LaunchSupervisorError): authority.attest_executor_endpoint(timeout_ms=100)
        assert time.monotonic() - start < 1 and not authority._held
    finally:
        authority._mutex.release()


@pytest.mark.parametrize('mode', ['error', 'late'])
def test_attestation_close_failure_holds_and_attempts_directory_close(synthetic_issuer, short_root, monkeypatch, mode):
    authority, _, _ = synthetic_issuer
    current_ready(authority)
    directory = short_root / 'endpoint'; directory.mkdir(mode=0o700)
    path = directory / 'executor.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path)); os.chmod(path, 0o600); server.listen(2)
        info = directory.stat()
        authority.profile = SimpleNamespace(role=None, executor_endpoint='protected-uds-v1')
        authority._mounts = {'executor_endpoint_directory': SimpleNamespace(source=str(directory),
            uid=os.geteuid(), gid=os.getegid())}
        authority._source_pins = {'executor_endpoint_directory': (info.st_dev, info.st_ino)}
        authority._executor_endpoint_name = path.name
        authority._check_executor_endpoint_directory()
        real_socket, real_close = socket.socket, os.close
        closes = []
        class FaultSocket:
            def __init__(self, *args): self.socket = real_socket(*args)
            def __getattr__(self, name): return getattr(self.socket, name)
            def close(self):
                self.socket.close()
                if mode == 'late': time.sleep(.15)
                else: raise OSError('synthetic close failure')
        def close_fd(fd): closes.append(fd); real_close(fd)
        with monkeypatch.context() as patch:
            patch.setattr(launch.socket, 'socket', FaultSocket)
            patch.setattr(launch.os, 'close', close_fd)
            with pytest.raises(launch.LaunchSupervisorError):
                authority.attest_executor_endpoint(timeout_ms=100 if mode == 'late' else 1000)
        assert authority._held and closes
        path.unlink()
