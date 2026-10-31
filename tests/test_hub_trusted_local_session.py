"""Local session binding tests with synthetic operator evidence and providers."""
import pytest
import hashlib
import os
pytest.importorskip('fcntl')
from hub_container_executor import trusted_local_session as local
from hub_container_executor.installed_coordinator import parse_configuration
from engineering_hub.trusted_queued_session import parse_enrollment, TrustedSessionError
from tests.test_hub_container_executor_installed_coordinator import configuration, canonical
from tests.test_hub_trusted_queued_session import document, v2_document, encode
from tests.test_hub_unified_pilot_startup import profile_document


def test_local_session_requires_disjoint_operator_enrollment_and_closes(monkeypatch):
    data = configuration()
    data['manifest'] = profile_document()['manifest']
    data['secret_bindings']['executor']['digest'] = 'sha256:' + hashlib.sha256(b'synthetic-token').hexdigest()
    config = parse_configuration(canonical(data))
    events = []
    class Transport:
        def __init__(self, path, *, credential):
            assert path == config.executor_socket and credential == b'synthetic-token'
            events.append('transport')
        def close(self): events.append('transport-close')
    class Observer:
        def __init__(self, **kwargs): events.append('observer')
        def close(self): events.append('observer-close')
    monkeypatch.setattr(local, 'CurrentExecutorUnixTransport', Transport)
    monkeypatch.setattr(local, 'CurrentExecutorObserver', Observer)
    with pytest.raises(TrustedSessionError):
        local.TrustedLocalQueuedSession(config=config, enrollment=parse_enrollment(encode(document())),
                                        credential=b'synthetic-token')
    assert events == []
    session = local.TrustedLocalQueuedSession(config=config, enrollment=parse_enrollment(encode(v2_document())),
                                              credential=b'synthetic-token')
    session.close()
    assert events == ['transport', 'observer', 'observer-close', 'transport-close']


@pytest.mark.asyncio
async def test_explicit_local_coordinator_composition_drains_before_session_close(monkeypatch):
    config = parse_configuration(canonical(configuration()))
    enrollment = parse_enrollment(encode(v2_document()))
    events = []
    class Secrets:
        def __init__(self, root, *, bindings): pass
        def __enter__(self): return self
        def __exit__(self, *args): events.append('secret-close')
        def resolve_test_secret(self, *slot): return 'synthetic-token'
    class Session:
        def __init__(self, *, config, enrollment, credential):
            assert credential == b'synthetic-token'
            events.append('session')
        def reconciliation_factory(self, *args): pass
        def session_acceptance_factory(self, *args): pass
        def close(self): events.append('session-close')
    async def serve(config, *, reconciliation_factory, session_acceptance_factory):
        assert type(reconciliation_factory.__self__) is Session
        assert session_acceptance_factory.__self__ is reconciliation_factory.__self__
        events.append('coordinator-drained')
    monkeypatch.setattr(local, 'ImmutableTestSecretResolver', Secrets)
    monkeypatch.setattr(local, 'TrustedLocalQueuedSession', Session)
    monkeypatch.setattr(local, 'run_installed_coordinator', serve)
    await local.serve_trusted_local_coordinator(config, enrollment=enrollment)
    assert events == ['secret-close', 'session', 'coordinator-drained', 'session-close']


def test_local_enrollment_reads_only_protected_existing_disjoint_attestation(tmp_path):
    config = parse_configuration(canonical(configuration()))
    os.chmod(tmp_path, 0o700)
    path = tmp_path / 'operator.json'
    path.write_bytes(encode(v2_document())); os.chmod(path, 0o600)
    enrollment = local.load_local_enrollment(str(path), config=config)
    assert enrollment.schema == local.SCHEMA_V2
    os.chmod(path, 0o644)
    with pytest.raises(TrustedSessionError): local.load_local_enrollment(str(path), config=config)
    os.chmod(path, 0o600)
    alias = tmp_path / 'alias.json'
    alias.symlink_to(path)
    with pytest.raises(TrustedSessionError): local.load_local_enrollment(str(alias), config=config)
    path.write_bytes(encode(document()))
    with pytest.raises(TrustedSessionError): local.load_local_enrollment(str(path), config=config)
    with pytest.raises(TrustedSessionError):
        local.load_local_enrollment('/pilot/controller/operator.json', config=config)
    with pytest.raises(TrustedSessionError):
        local.load_local_enrollment('/pilot/secrets/operator.json', config=config)


def test_cli_uses_explicit_protected_inputs_and_sanitizes_failures(monkeypatch, capsys):
    config = parse_configuration(canonical(configuration()))
    enrollment = parse_enrollment(encode(v2_document()))
    events = []
    monkeypatch.setattr(local, 'load_configuration', lambda path: events.append(('config', path)) or config)
    monkeypatch.setattr(local, 'load_local_enrollment', lambda path, *, config: events.append(('enrollment', path)) or enrollment)
    async def serve(actual, *, enrollment):
        assert actual is config
        events.append(('serve', enrollment))
    monkeypatch.setattr(local, 'serve_trusted_local_coordinator', serve)
    assert local.main(['--config', '/config', '--enrollment', '/operator']) == 0
    assert events == [('config', '/config'), ('enrollment', '/operator'), ('serve', enrollment)]
    assert local.main(['--config', '/secret-value', '--config', '/other', '--enrollment', '/operator']) == 2
    assert '/secret-value' not in capsys.readouterr().err
