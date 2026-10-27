"""Synthetic local profiles; never use a real installation credential."""
import hashlib
import json
import os
import time

import pytest

from agent_message_bus import agent_session_client as client


def test_pairing_sends_only_hash_and_retries_same_local_identity(tmp_path, monkeypatch, capsys):
    requests = []
    response = {'id': '00000000-0000-0000-0000-000000000001', 'family': 'codex',
                'state': 'pending', 'pairing_code': '123456',
                'key_fingerprint': '0' * 16, 'expires_at': '2030-01-01T00:00:00Z'}

    def request(origin, path, payload, token=None):
        requests.append((origin, path, payload, token))
        if len(requests) == 1:
            raise client.ClientError('synthetic_lost_response')
        return response

    monkeypatch.setattr(client, '_request', request)
    args = ['--profile-dir', str(tmp_path), 'enroll', '--origin', 'http://127.0.0.1:9911',
            '--family', 'codex', '--name', 'Synthetic PC', '--host-label', 'fixture']
    with pytest.raises(client.ClientError):
        client.main(args)
    profile = client._read(tmp_path / 'installation.private')
    assert client.main(args) == 0
    assert requests[0][2]['credential_sha256'] == requests[1][2]['credential_sha256']
    assert requests[1][2]['credential_sha256'] == hashlib.sha256(profile['secret'].encode()).hexdigest()
    assert requests[1][3] is None
    assert set(requests[1][2]) == {'family', 'name', 'host_label', 'credential_sha256'}
    assert profile['secret'] not in capsys.readouterr().out


def test_connect_private_header_pipe_and_no_automatic_replacement(tmp_path, monkeypatch, capsys):
    install = 'ehi_' + 'a' * 43
    session = 'ehs_' + 'b' * 43
    client._write(tmp_path / 'installation.private', {
        'origin': 'http://127.0.0.1:9911', 'family': 'codex', 'secret': install,
        'installation_id': '00000000-0000-0000-0000-000000000001'})
    calls = []

    def request(origin, path, payload, token=None):
        calls.append((path, token))
        return {'token': session, 'session': {'id': 'session-id', 'expires_at': '2030-01-01T00:00:00Z'}}

    monkeypatch.setattr(client, '_request', request)
    assert client.main(['--profile-dir', str(tmp_path), 'connect', '--session', 'first']) == 0
    output = capsys.readouterr().out
    assert install not in output and session not in output
    assert calls == [('/exchange', install)]
    assert client.main(['--profile-dir', str(tmp_path), 'headers', '--session', 'first']) == 0
    assert json.loads(capsys.readouterr().out) == {'Authorization': 'Bearer ' + session}
    # Header retrieval is local and never silently renews a revoked connection.
    assert len(calls) == 1
    with pytest.raises(client.ClientError, match='session_profile_exists'):
        client.main(['--profile-dir', str(tmp_path), 'connect', '--session', 'first'])


def test_expired_header_is_not_exchanged_or_printed(tmp_path, monkeypatch, capsys):
    client._write(tmp_path / 'expired.session.private', {'token': 'ehs_' + 'c' * 43,
                                                       'expires_at': time.time() - 1})
    monkeypatch.setattr(client, '_request', lambda *a, **k: pytest.fail('must not exchange'))
    with pytest.raises(client.ClientError, match='session_expired'):
        client.main(['--profile-dir', str(tmp_path), 'headers', '--session', 'expired'])
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('origin', ['http://example.test', 'https://example.test/path',
                                  'https://user:pass@example.test', 'https://example.test?token=value'])
def test_origin_binding_refuses_insecure_or_credential_urls(origin):
    with pytest.raises(client.ClientError):
        client._origin(origin)


def test_private_profile_file_permissions_and_no_symlink_follow(tmp_path):
    path = tmp_path / 'test.private'
    client._write(path, {'synthetic': True})
    assert client._read(path) == {'synthetic': True}
    if os.name != 'nt':
        path.chmod(0o644)
        with pytest.raises(client.ClientError, match='permissions'):
            client._read(path)
        path.chmod(0o600)
        other = tmp_path / 'link'
        other.symlink_to(path)
        with pytest.raises(OSError):
            client._read(other)


def test_live_profile_binding_is_not_reused_for_another_origin(tmp_path):
    client._write(tmp_path / 'installation.private', {
        'origin': 'https://first.example.test', 'family': 'codex', 'secret': 'synthetic'})
    with pytest.raises(client.ClientError, match='binding_mismatch'):
        client.main(['--profile-dir', str(tmp_path), 'enroll', '--origin', 'https://other.example.test',
                     '--family', 'codex', '--name', 'fixture', '--host-label', 'fixture'])


def test_profile_lock_does_not_overwrite_an_existing_operation(tmp_path):
    with client._profile_lock(tmp_path):
        with pytest.raises(client.ClientError, match='in_progress'):
            with client._profile_lock(tmp_path):
                pytest.fail('must not acquire a second pairing lock')
