"""Adversarial fixture-only protocol/evidence checks; no live endpoints."""
import json
import threading
import urllib.error
import urllib.request
import uuid
import pytest
from contextlib import contextmanager

pytest.importorskip("fcntl", reason="Fixture executor requires Linux kernel flock; verify executor acceptance on Linux")

from hub_executor.fixtures import make_fixture_executor, FIXTURE_TARGET, FIXTURE_CANDIDATE_DIGEST, FIXTURE_CANDIDATE_COMMIT
from hub_executor.http import make_server

TOKEN = 'test-controller-fixture-only'


@contextmanager
def serving(executor, **kwargs):
    server = make_server(executor, TOKEN, **kwargs)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def command(**overrides):
    return dict(command_id=str(uuid.uuid4()), target_id=FIXTURE_TARGET,
                action='deploy_fixture', artifact_digest=FIXTURE_CANDIDATE_DIGEST,
                candidate_commit=FIXTURE_CANDIDATE_COMMIT, policy_revision=1,
                fencing_generation=1) | overrides


def request(origin, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    call = urllib.request.Request(origin + path, data=data,
        headers={'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(call, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_fence_above_sqlite_range_is_invalid_not_internal_error(tmp_path):
    executor = make_fixture_executor(tmp_path)
    payload = command(fencing_generation=2**63)
    with serving(executor) as (_, origin):
        status, result = request(origin, '/v1/commands', payload)
    assert status == 400
    assert result == {'error': {'code': 'invalid_payload'}}
    assert executor.get_command(payload['command_id']) is None


def test_partial_unauthenticated_connections_are_bounded_and_expire(tmp_path):
    import socket
    import time
    executor = make_fixture_executor(tmp_path)
    sockets = []
    with serving(executor, max_workers=2, request_timeout=0.1) as (server, origin):
        try:
            for _ in range(6):
                peer = socket.create_connection(('127.0.0.1', server.server_port), timeout=1)
                sockets.append(peer)
                try:
                    peer.sendall(b'GET /health HTTP/1.1\r\nHost:')
                except OSError:
                    pass  # Excess connections are deliberately refused.
            time.sleep(0.3)  # Bounded observation of request timeout, not polling.
            status, health = request(origin, '/health')
            assert status == 200 and health == {'ok': True}
            assert server.peak_connections <= 2
        finally:
            for peer in sockets:
                peer.close()


@pytest.mark.parametrize('malformed', [[], None, True, 'not-an-object', {}])
def test_malformed_target_evidence_stays_held_over_http(tmp_path, monkeypatch, malformed):
    executor = make_fixture_executor(tmp_path)
    def interrupted(_payload):
        raise RuntimeError('fixture interruption before effect')
    monkeypatch.setattr(executor, '_apply_fixture', interrupted)
    payload = command()
    with pytest.raises(RuntimeError):
        executor.execute_command(payload)
    target = executor.target_roots[FIXTURE_TARGET]
    target.mkdir(parents=True)
    (target / 'state.json').write_text(json.dumps(malformed))
    with serving(executor) as (_, origin):
        call = urllib.request.Request(origin + f"/v1/commands/{payload['command_id']}/reconcile",
            data=b'', headers={'Authorization': 'Bearer ' + TOKEN}, method='POST')
        with urllib.request.urlopen(call, timeout=3) as response:
            result = json.loads(response.read())
            assert response.status == 200 and result['state'] == 'held'
            assert result['observed'] == {}


def test_changed_artifact_bytes_cannot_reconcile_with_old_receipt(tmp_path, monkeypatch):
    executor = make_fixture_executor(tmp_path)
    original = executor._apply_fixture
    def interrupted(payload):
        original(payload)
        raise RuntimeError('fixture interruption after effect')
    monkeypatch.setattr(executor, '_apply_fixture', interrupted)
    payload = command()
    with pytest.raises(RuntimeError):
        executor.execute_command(payload)
    path = executor.target_roots[FIXTURE_TARGET] / 'state.json'
    state = json.loads(path.read_text())
    state['artifact_bytes'] = 'different fixture bytes'
    path.write_text(json.dumps(state))
    assert executor.reconcile_command(payload['command_id'])['state'] == 'held'
