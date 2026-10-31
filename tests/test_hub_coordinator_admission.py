"""Supervised coordinator bootstrap ordering; no installed host or daemon."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

pytest.importorskip('fcntl')
from hub_container_executor import coordinator_admission as admission
from hub_container_executor import trusted_local_session as local
from hub_container_executor import installed_coordinator, launch_transport, runtime_identity


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def document(config=b'config', session=b'session'):
    sha = lambda data: 'sha256:' + hashlib.sha256(data).hexdigest()
    return {'schema': admission.SCHEMA, 'issuer_uid': 0, 'issuer_gid': 10001,
        'config_digest': sha(config), 'session_digest': sha(session), 'lifetime_seconds': 30, 'timeout_ms': 2000,
        'release': {'issuer_id': 'synthetic', 'public_key_hex': '1'*64, 'implementation_commit': 'a'*40,
            'runnable_digest': 'sha256:'+'b'*64, 'platform_manifest_digest': 'sha256:'+'c'*64,
            'image_config_id': 'sha256:'+'d'*64, 'launch_profile_digest': 'sha256:'+'e'*64,
            'platform': {'os':'linux','architecture':'amd64','variant':None}}}


def test_normalized_commitment_preserves_exact_raw_pin_and_closed_schema():
    first = document(); raw = canonical(first)
    value = admission.parse_admission(raw)
    assert value.config_digest == first['config_digest']
    changed = document(); changed['release']['launch_profile_digest'] = 'sha256:'+'f'*64
    assert admission.admission_commitment(raw)[0] == admission.admission_commitment(canonical(changed))[0]
    assert admission.admission_commitment(raw)[1] != admission.admission_commitment(canonical(changed))[1]
    for invalid in (raw+b' ', canonical(dict(first, issuer_uid=True)), canonical(dict(first, extra='x'))):
        with pytest.raises(ValueError): admission.parse_admission(invalid)


def test_supervised_capture_requires_readonly_mount_and_rejects_symlink(tmp_path, monkeypatch):
    source = tmp_path / 'input.json'; source.write_bytes(b'valid'); os.chmod(source, 0o600)
    with monkeypatch.context() as patch:
        patch.setattr(local.os, 'fstatvfs', lambda fd: SimpleNamespace(f_flag=0))
        with pytest.raises(local.TrustedSessionError): local._read_supervised_input(str(source), 100)
    alias = tmp_path / 'alias'; alias.symlink_to(source)
    with pytest.raises(OSError): local._read_supervised_input(str(alias), 100)


@pytest.mark.parametrize('fail_receipt', [False, True])
def test_receipt_precedes_config_and_verifier_owned_until_drain(monkeypatch, fail_receipt):
    events = []
    files = {local._SUPERVISED_ADMISSION: canonical(document()), local._SUPERVISED_CONFIG: b'config',
             local._SUPERVISED_SESSION: b'session'}
    monkeypatch.setattr(local, '_read_supervised_input', lambda path, maximum: events.append(('read',path)) or files[path])
    class Transport:
        def __init__(self, **kwargs): events.append('transport')
        def close(self): events.append('transport-close')
    class Verifier:
        def __init__(self, **kwargs):
            events.append('receipt')
            if fail_receipt: raise ValueError('denied')
        def implementation_commit(self): events.append('check'); return 'a'*40
        def close(self): events.append('verifier-close')
    monkeypatch.setattr(launch_transport, 'CoordinatorUnixLaunchReceiptTransport', Transport)
    monkeypatch.setattr(runtime_identity, 'CoordinatorLaunchReceiptIdentityVerifier', Verifier)
    monkeypatch.setattr(installed_coordinator, '_process', lambda: events.append('process'))
    config = SimpleNamespace(database='/var/lib/test/hub.db', secret_root='/var/lib/secrets',
                             auth=SimpleNamespace(human_auth_mode='github', beau_github_id=123))
    session = SimpleNamespace(schema=local.SCHEMA_V3, operator_actor='human:123')
    monkeypatch.setattr(installed_coordinator, 'parse_configuration', lambda raw: events.append(('config-parse', raw)) or config)
    monkeypatch.setattr(local, 'parse_enrollment', lambda raw: events.append(('session-parse', raw)) or session)
    monkeypatch.setattr(local, 'load_configuration', lambda path: pytest.fail('must parse captured bytes'))
    monkeypatch.setattr(local, 'load_local_enrollment', lambda *args, **kwargs: pytest.fail('must parse captured bytes'))
    async def serve(config, *, enrollment, admission_verifier): events.append('drained')
    monkeypatch.setattr(local, 'serve_trusted_local_coordinator', serve)
    if fail_receipt:
        with pytest.raises(ValueError): local.run_supervised_local_coordinator()
        assert events == [('read', local._SUPERVISED_ADMISSION), 'transport', 'receipt', 'transport-close']
    else:
        local.run_supervised_local_coordinator()
        assert events.index('receipt') < events.index(('read', local._SUPERVISED_CONFIG))
        assert events.index('drained') < events.index('verifier-close') < events.index('transport-close')


@pytest.mark.asyncio
async def test_expired_admission_after_agent_bind_does_not_bind_browser(monkeypatch):
    from contextlib import contextmanager
    from engineering_hub import split_server
    events = []
    class Verifier:
        def implementation_commit(self):
            events.append('check')
            if events.count('check') == 2: raise ValueError('expired')
            return 'a'*40
    @contextmanager
    def bind(*args):
        events.append('agent-bind')
        try: yield object()
        finally: events.append('agent-close')
    @contextmanager
    def browser(*args):
        events.append('browser-bind')
        yield object()
    monkeypatch.setattr(runtime_identity, 'CoordinatorLaunchReceiptIdentityVerifier', Verifier)
    monkeypatch.setattr(split_server, '_bind', bind)
    monkeypatch.setattr(split_server, 'private_browser_socket', browser)
    with pytest.raises(split_server.SplitServerError):
        await split_server.serve_private_coordinator(lambda: events.append('pair'),
            socket_path='/unused/hub.sock', agent_host='127.0.0.1', agent_port=0,
            install_signals=False, admission_verifier=Verifier())
    assert events == ['check', 'agent-bind', 'check', 'agent-close']


def test_real_signed_coordinator_receipt_drives_bootstrap(monkeypatch):
    import base64, struct, time, uuid
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from tests.test_hub_container_executor_launch_transport import issuer, read_exact, frame
    from hub_container_executor.launch_transport import COORDINATOR_REQUEST_PROTOCOL
    key = Ed25519PrivateKey.generate()
    data = document(); data['issuer_uid'] = os.geteuid(); data['issuer_gid'] = os.getegid()
    data['release']['public_key_hex'] = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    events = []
    def handle(peer, state):
        request = json.loads(read_exact(peer, struct.unpack('!I', read_exact(peer, 4))[0]))
        assert request['protocol'] == COORDINATOR_REQUEST_PROTOCOL and peer.recv(1) == b''
        release = data['release']; now = int(time.time()*1000)
        payload = {'protocol':runtime_identity.COORDINATOR_PROTOCOL,'audience':runtime_identity.COORDINATOR_AUDIENCE,
            'nonce':request['nonce'],'receipt_id':str(uuid.uuid4()),'launch_id':str(uuid.uuid4()),
            'issuer_id':release['issuer_id'],'boot_id':str(uuid.uuid4()),'authenticated_host_pid':123,
            'process_start_ticks':456,'container_id':'b'*64,'issued_at_ms':now,'expires_at_ms':now+20000,
            **{name:release[name] for name in ('runnable_digest','platform_manifest_digest','image_config_id',
                'platform','implementation_commit','launch_profile_digest')}}
        signature = base64.urlsafe_b64encode(key.sign(runtime_identity.COORDINATOR_SIGNATURE_DOMAIN+canonical(payload))).decode().rstrip('=')
        events.append('signed'); peer.sendall(frame(canonical({'payload':payload,'signature':signature})))
    files = {local._SUPERVISED_ADMISSION:canonical(data),local._SUPERVISED_CONFIG:b'config',local._SUPERVISED_SESSION:b'session'}
    monkeypatch.setattr(local, '_read_supervised_input', lambda path, maximum: files[path])
    monkeypatch.setattr(installed_coordinator, '_process', lambda: None)
    config = SimpleNamespace(database='/var/lib/test/hub.db', secret_root='/var/lib/secrets',
                             auth=SimpleNamespace(human_auth_mode='github', beau_github_id=123))
    monkeypatch.setattr(installed_coordinator, 'parse_configuration', lambda raw: events.append('parse') or config)
    monkeypatch.setattr(local, 'parse_enrollment', lambda raw: SimpleNamespace(schema=local.SCHEMA_V3, operator_actor='human:123'))
    async def serve(config, *, enrollment, admission_verifier): events.append('drained')
    monkeypatch.setattr(local, 'serve_trusted_local_coordinator', serve)
    with issuer(handle) as (path, state):
        monkeypatch.setattr(local, '_SUPERVISED_ISSUER', path)
        local.run_supervised_local_coordinator()
    assert events == ['signed', 'parse', 'drained']
