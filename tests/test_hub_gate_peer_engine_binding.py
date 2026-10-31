"""Daemon accounting binds the accepted pidfd to the enrolled app image."""
import copy
import os
import time
from types import SimpleNamespace

import pytest

from hub_container_executor import launch_supervisor as launch
from hub_container_executor.broker_seqpacket import PeerPin, _start


def test_gate_peer_is_checked_against_authenticated_daemon():
    if not hasattr(os, 'pidfd_open'):
        pytest.skip('Linux pidfd required')
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    container = 'a' * 64
    image = 'sha256:' + 'b' * 64
    pins = {'container_id': container, 'host_pid': peer.pid,
            'process_start_ticks': peer.start, 'image_config_id': image}
    expected = {'Id': container, 'Image': image, 'State': {'Running': True, 'Status': 'running',
                'Paused': False, 'Restarting': False, 'Dead': False, 'Pid': peer.pid}}
    class Engine:
        def __init__(self): self.value = expected
        def json(self, method, route, deadline):
            assert (method, route) == ('GET', '/containers/' + container + '/json')
            return 200, self.value
    supervisor = object.__new__(launch.LaunchSupervisor)
    supervisor._engine = Engine()
    enrollment = SimpleNamespace(image_config_id=image)
    try:
        supervisor._gate_peer_engine_check(peer, pins, enrollment, time.monotonic() + 2)
        for field, value in [('Id', 'c' * 64), ('Image', 'sha256:' + 'c' * 64)]:
            supervisor._engine.value = copy.deepcopy(expected)
            supervisor._engine.value[field] = value
            with pytest.raises(Exception):
                supervisor._gate_peer_engine_check(peer, pins, enrollment, time.monotonic() + 2)
        for field, value in [('Pid', peer.pid + 1), ('Pid', True), ('Running', False),
                             ('Paused', True), ('Restarting', True), ('Dead', True)]:
            supervisor._engine.value = copy.deepcopy(expected)
            supervisor._engine.value['State'][field] = value
            with pytest.raises(Exception):
                supervisor._gate_peer_engine_check(peer, pins, enrollment, time.monotonic() + 2)
        supervisor._engine.value = expected
        with pytest.raises(Exception):
            supervisor._gate_peer_engine_check(peer, dict(pins, host_pid=True), enrollment, time.monotonic() + 2)
        class ReusedPeer:
            pid, start = peer.pid, peer.start
            calls = 0
            def check(self):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError('pidfd exited or start identity changed')
        reused = ReusedPeer()
        with pytest.raises(RuntimeError):
            supervisor._gate_peer_engine_check(reused, pins, enrollment, time.monotonic() + 2)
        assert reused.calls == 2
    finally:
        peer.close()
