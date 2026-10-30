"""Synthetic preflight checks; no live credentials or host changes."""
from types import SimpleNamespace
from pathlib import PurePosixPath
import stat

from hub_staging_service import observer_preflight as preflight


def setup(monkeypatch):
    monkeypatch.setattr(preflight.os, 'name', 'posix')
    monkeypatch.setattr(preflight, 'Path', PurePosixPath)
    monkeypatch.setattr(preflight.os, 'geteuid', lambda: 123, raising=False)
    monkeypatch.setattr(preflight.os, 'getegid', lambda: 456, raising=False)
    monkeypatch.setattr(preflight, '_read_host', lambda path: {})
    monkeypatch.setattr(preflight, '_validate_host', lambda data, now: ({'state': 'observed'}, [], []))
    monkeypatch.setattr(preflight, '_secure_directory', lambda path: 7)
    monkeypatch.setattr(preflight.os, 'O_NOFOLLOW', 0, raising=False)
    monkeypatch.setattr(preflight.os, 'O_NONBLOCK', 0, raising=False)
    monkeypatch.setattr(preflight.os, 'open', lambda *args, **kwargs: 8)
    monkeypatch.setattr(preflight.os, 'close', lambda fd: None)
    monkeypatch.setattr(preflight.os, 'fstat', lambda fd: SimpleNamespace(st_mode=stat.S_IFREG | 0o600,
                        st_nlink=1, st_uid=123, st_size=20))
    monkeypatch.setattr(preflight.os, 'read', lambda fd, size: b'synthetic-token-only')


def test_current_identity_and_protected_inputs(monkeypatch):
    setup(monkeypatch)
    result = preflight.check('/protected/snapshot', '/protected/token', 123, 456)
    assert result == {'identity': 'observed', 'snapshot': 'observed', 'credential': 'observed',
                      'firewall_admission_tls_acceptance': 'not_assessed'}
    assert 'synthetic-token-only' not in str(result)


def test_wrong_identity_stops_all_reads(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(preflight, '_read_host', lambda path: (_ for _ in ()).throw(AssertionError()))
    assert preflight.check('/protected/snapshot', '/protected/token', 124, 456)['identity'] == 'unavailable'


def test_stale_and_unprotected_are_unavailable(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(preflight, '_validate_host', lambda data, now: ({'state': 'stale'}, [], []))
    monkeypatch.setattr(preflight.os, 'fstat', lambda fd: SimpleNamespace(st_mode=stat.S_IFREG | 0o644,
                        st_nlink=1, st_uid=123, st_size=20))
    result = preflight.check('/protected/snapshot', '/protected/token', 123, 456)
    assert result['snapshot'] == result['credential'] == 'unavailable'


def test_symlink_or_unsafe_ancestor_is_unavailable(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(preflight, '_secure_directory', lambda path: (_ for _ in ()).throw(ValueError()))
    assert preflight.check('/protected/snapshot', '/protected/token', 123, 456)['credential'] == 'unavailable'



def test_cli_exit_status_and_json(monkeypatch, capsys):
    import json
    import sys
    monkeypatch.setattr(sys, 'argv', ['preflight', '--snapshot', '/snapshot', '--token-file', '/token',
                                    '--expected-uid', '123', '--expected-gid', '456'])
    result = {'identity': 'observed', 'snapshot': 'observed', 'credential': 'observed',
              'firewall_admission_tls_acceptance': 'not_assessed'}
    monkeypatch.setattr(preflight, 'check', lambda *args: result.copy())
    assert preflight.main() == 0
    assert json.loads(capsys.readouterr().out) == result
    for key in ('identity', 'snapshot', 'credential'):
        failed = result.copy()
        failed[key] = 'unavailable'
        monkeypatch.setattr(preflight, 'check', lambda *args: failed.copy())
        assert preflight.main() == 1
        assert json.loads(capsys.readouterr().out) == failed
