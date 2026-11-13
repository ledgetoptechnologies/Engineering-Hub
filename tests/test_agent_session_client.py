"""Synthetic local profiles; never use a real installation credential."""
import hashlib
import json
import os
import time
from urllib.error import HTTPError

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


def test_private_vpn_enrollment_persists_mode_and_uses_only_private_origin(tmp_path, monkeypatch, capsys):
    owner_origin = 'https://hub.example.test'
    private_origin = 'http://192.168.50.80:8770'
    response = {'id': '00000000-0000-0000-0000-000000000001', 'family': 'codex',
                'state': 'pending', 'pairing_code': '123456',
                'key_fingerprint': '0' * 16, 'expires_at': '2030-01-01T00:00:00Z'}
    requests = []
    monkeypatch.setattr(client, '_request', lambda origin, path, payload, token=None:
        (requests.append((origin, path, payload, token)) or response))
    assert client.main(['--profile-dir', str(tmp_path), 'enroll', '--origin', owner_origin,
        '--private-vpn', '--private-origin', private_origin, '--family', 'codex',
        '--name', 'Synthetic laptop', '--host-label', 'vpn-laptop']) == 0
    profile = client._read(tmp_path / 'installation.private')
    assert profile['origin'] == owner_origin
    assert profile['private_vpn'] is True
    assert profile['private_origin'] == private_origin
    assert requests[0][0:2] == (private_origin, '/enrollments')
    assert profile['secret'] not in capsys.readouterr().out


def test_private_vpn_connect_requires_flag_and_emits_private_mcp_url(tmp_path, monkeypatch, capsys):
    owner_origin = 'https://hub.example.test'
    private_origin = 'http://192.168.50.80:8770'
    install = 'ehi_' + 'a' * 43
    session = 'ehs_' + 'b' * 43
    client._write(tmp_path / 'installation.private', {
        'origin': owner_origin, 'private_vpn': True, 'private_origin': private_origin,
        'family': 'codex', 'secret': install,
        'installation_id': '00000000-0000-0000-0000-000000000001'})
    calls = []
    monkeypatch.setattr(client, '_request', lambda origin, path, payload, token=None:
        (calls.append((origin, path, token)) or {
            'token': session, 'session': {'id': 'session-id', 'expires_at': '2030-01-01T00:00:00Z'}}))
    with pytest.raises(client.ClientError, match='private_vpn_flag_required'):
        client.main(['--profile-dir', str(tmp_path), 'connect', '--session', 'vpn-session'])
    assert calls == []
    assert client.main(['--profile-dir', str(tmp_path), 'connect', '--session', 'vpn-session',
                        '--private-vpn']) == 0
    assert calls == [(private_origin, '/exchange', install)]
    output = json.loads(capsys.readouterr().out)
    assert output['mcp_url'] == private_origin + '/mcp'
    assert install not in json.dumps(output) and session not in json.dumps(output)


@pytest.mark.parametrize('origin', [
    'https://192.168.50.80:8770', 'http://8.8.8.8:8770', 'http://hub.example.test:8770',
    'http://[fd00::80]:8770', 'http://127.0.0.1:8770', 'http://169.254.1.10:8770',
    'http://100.64.0.1:8770', 'http://192.0.2.1:8770', 'http://198.18.0.1:8770',
    'http://224.0.0.1:8770', 'http://192.168.50.80', 'http://192.168.50.80:0',
    'http://192.168.50.80:65536', 'http://192.168.50.80:08770',
    'http://192.168.050.80:8770', 'http://user@192.168.50.80:8770',
    'http://192.168.50.80:8770/path', 'http://192.168.50.80:8770?x=1',
    'http://192.168.50.80:8770#frag', 'http://192.168.50.80:8770%2Fignored',
])
def test_private_vpn_origin_requires_canonical_rfc1918_http_literal(origin):
    with pytest.raises(client.ClientError, match='invalid_private_vpn_origin'):
        client._private_origin(origin)


def test_private_origin_requires_explicit_enroll_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(client, '_request', lambda *args, **kwargs: pytest.fail('must not send request'))
    with pytest.raises(client.ClientError, match='private_origin_requires_private_vpn_flag'):
        client.main(['--profile-dir', str(tmp_path), 'enroll', '--origin', 'https://hub.example.test',
            '--private-origin', 'http://192.168.50.80:8770', '--family', 'codex',
            '--name', 'fixture', '--host-label', 'fixture'])


def test_private_vpn_enrollment_requires_https_owner_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(client, '_request', lambda *args, **kwargs: pytest.fail('must not send request'))
    with pytest.raises(client.ClientError, match='private_vpn_requires_https_owner_origin'):
        client.main(['--profile-dir', str(tmp_path), 'enroll', '--origin', 'http://127.0.0.1:9911',
            '--private-vpn', '--private-origin', 'http://192.168.50.80:8770', '--family', 'codex',
            '--name', 'fixture', '--host-label', 'fixture'])


def test_private_vpn_connect_revalidates_profile_origin_without_fallback(tmp_path, monkeypatch):
    client._write(tmp_path / 'installation.private', {
        'origin': 'https://hub.example.test', 'private_vpn': True,
        'private_origin': 'http://8.8.8.8:8770', 'family': 'codex',
        'secret': 'synthetic', 'installation_id': '00000000-0000-0000-0000-000000000001'})
    monkeypatch.setattr(client, '_request', lambda *args, **kwargs: pytest.fail('must not send request'))
    with pytest.raises(client.ClientError, match='invalid_private_vpn_origin'):
        client.main(['--profile-dir', str(tmp_path), 'connect', '--session', 'vpn-session', '--private-vpn'])


def test_private_vpn_flag_cannot_select_private_transport_for_legacy_profile(tmp_path, monkeypatch):
    client._write(tmp_path / 'installation.private', {
        'origin': 'https://hub.example.test', 'family': 'codex', 'secret': 'synthetic',
        'installation_id': '00000000-0000-0000-0000-000000000001'})
    monkeypatch.setattr(client, '_request', lambda *args, **kwargs: pytest.fail('must not send request'))
    with pytest.raises(client.ClientError, match='private_vpn_profile_required'):
        client.main(['--profile-dir', str(tmp_path), 'connect', '--session', 'vpn-session', '--private-vpn'])


def test_private_vpn_requests_do_not_use_proxy_or_follow_public_redirect(monkeypatch):
    seen = {'calls': 0, 'url': None, 'userAgent': None, 'handlers': None}

    class RedirectingOpener:
        def open(self, request, timeout):
            seen['calls'] += 1
            seen['url'] = request.full_url
            seen['userAgent'] = request.get_header('User-agent')
            raise HTTPError(request.full_url, 302, 'redirect', {}, None)

    def build_opener(*handlers):
        seen['handlers'] = handlers
        return RedirectingOpener()

    monkeypatch.setattr(client, 'build_opener', build_opener)
    with pytest.raises(client.ClientError, match='server_request_denied_302'):
        client._request('http://192.168.50.80:8770', '/exchange', {'label': ''}, 'synthetic')
    assert seen['calls'] == 1
    assert seen['url'] == 'http://192.168.50.80:8770/api/agent-sessions/v1/exchange'
    assert seen['userAgent'] == 'Engineering-Hub-Agent-Client/0.7'
    proxy_handlers = [handler for handler in seen['handlers'] if isinstance(handler, client.ProxyHandler)]
    assert len(proxy_handlers) == 1 and proxy_handlers[0].proxies == {}
    assert any(isinstance(handler, client.NoRedirect) for handler in seen['handlers'])


def test_profile_lock_does_not_overwrite_an_existing_operation(tmp_path):
    with client._profile_lock(tmp_path):
        with pytest.raises(client.ClientError, match='in_progress'):
            with client._profile_lock(tmp_path):
                pytest.fail('must not acquire a second pairing lock')


def test_explicit_attention_listener_emits_only_fixed_hints(tmp_path, monkeypatch, capsys):
    import io
    from email.message import Message
    installation = '00000000-0000-4000-8000-000000000001'
    secret = 'ehs_' + 'z' * 43
    client._write(tmp_path / 'installation.private', {
        'origin': 'https://hub.example', 'family': 'codex', 'installation_id': installation})
    client._write(tmp_path / 'first.session.private', {
        'token': secret, 'expires_at': time.time() + 120, 'installation_id': installation})
    requests = []
    class Response(io.BytesIO):
        status = 200
        headers = Message()
        headers['Content-Type'] = 'text/event-stream'
    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return Response(b'event: attention-ready\ndata: {}\n\nevent: attention-check\ndata: {}\n\n')
    monkeypatch.setattr(client, 'build_opener', lambda *args: Opener())
    assert client.main(['--profile-dir', str(tmp_path), 'listen-attention', '--session', 'first',
                        '--project', 'engineering-hub', '--generation', '1']) == 0
    output = capsys.readouterr().out
    assert [json.loads(line)['event'] for line in output.splitlines()] == ['attention-ready', 'attention-check']
    assert secret not in output and secret not in requests[0][0].full_url
    assert requests[0][0].get_header('Authorization') == 'Bearer ' + secret
    assert 0 < requests[0][1] <= 120


def test_attention_listener_rejects_untrusted_frame_without_echo(tmp_path, monkeypatch, capsys):
    import io
    from email.message import Message
    installation = '00000000-0000-4000-8000-000000000001'
    client._write(tmp_path / 'installation.private', {
        'origin': 'https://hub.example', 'family': 'codex', 'installation_id': installation})
    client._write(tmp_path / 'first.session.private', {
        'token': 'ehs_' + 'z' * 43, 'expires_at': time.time() + 120, 'installation_id': installation})
    class Response(io.BytesIO):
        status = 200
        headers = Message()
        headers['Content-Type'] = 'text/event-stream'
    class Opener:
        def open(self, request, timeout):
            return Response(b'event: attention-check\ndata: SECRET-UNTRUSTED\n\n')
    monkeypatch.setattr(client, 'build_opener', lambda *args: Opener())
    with pytest.raises(client.ClientError, match='invalid_server_response'):
        client.main(['--profile-dir', str(tmp_path), 'listen-attention', '--session', 'first',
                     '--project', 'engineering-hub', '--generation', '1'])
    assert 'SECRET-UNTRUSTED' not in capsys.readouterr().out
