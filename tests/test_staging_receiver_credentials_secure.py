import os
import json
import stat
import sys
from pathlib import Path

import pytest

from hub_staging_service.__main__ import credentials
from hub_staging_service.core import Rejected

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='descriptor traversal requires POSIX')


@pytest.fixture
def credential(tmp_path):
    root = tmp_path / 'private'; root.mkdir(mode=0o700)
    path = root / 'token'; path.write_text('synthetic-original-123456\n'); path.chmod(0o600)
    return path


def test_private_file_uses_descriptor_not_path_read_text(credential, monkeypatch):
    monkeypatch.setattr(Path, 'read_text', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('path reopen')))
    assert credentials(credential) == 'synthetic-original-123456'


def test_real_check_config_loads_protected_file_without_disclosure(credential, tmp_path, monkeypatch, capsys):
    from hub_staging_service.__main__ import main
    from tests.test_staging_service_http import StorageConfigTests
    config = StorageConfigTests().configuration(tmp_path)
    config['token_file'] = str(credential)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    monkeypatch.setattr(sys, 'argv', ['worker', '--config', str(path), '--check-config'])
    main()
    output = capsys.readouterr()
    assert json.loads(output.out)['valid'] is True
    assert 'synthetic-original-123456' not in output.out + output.err
    assert not (tmp_path / 'unopened').exists()


@pytest.mark.parametrize('replacement', ['symlink', 'regular'])
def test_replacement_between_stat_and_open_is_rejected(credential, monkeypatch, replacement):
    other = credential.parent / 'other'; other.write_text('synthetic-attacker-123456'); other.chmod(0o644)
    original_stat = os.stat
    switched = [False]
    def stat_and_swap(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == 'token' and kwargs.get('dir_fd') is not None and not switched[0]:
            switched[0] = True
            credential.rename(credential.parent / 'saved')
            if replacement == 'symlink': credential.symlink_to(other)
            else: other.rename(credential)
        return info
    monkeypatch.setattr(os, 'stat', stat_and_swap)
    with pytest.raises(Rejected) as exc:
        credentials(credential)
    assert switched[0] and 'synthetic-' not in str(exc.value)


@pytest.mark.parametrize('mutation', ['replace', 'unlink', 'same_inode', 'grow', 'truncate', 'parent'])
def test_change_after_open_is_rejected(credential, monkeypatch, mutation):
    original_read = os.read
    target = credential.stat()
    changed = [False]
    def read_and_change(fd, size):
        result = original_read(fd, size)
        info = os.fstat(fd)
        if result and (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino) and not changed[0]:
            changed[0] = True
            if mutation == 'replace':
                credential.rename(credential.parent / 'saved')
                credential.write_text('synthetic-replacement-123'); credential.chmod(0o600)
            elif mutation == 'unlink': credential.unlink()
            elif mutation == 'parent':
                parent = credential.parent; parent.rename(parent.with_name('displaced'))
                parent.mkdir(mode=0o700)
            elif mutation == 'same_inode': credential.write_bytes(b'X' * target.st_size)
            elif mutation == 'grow': credential.write_bytes(b'X' * (target.st_size + 1))
            else: credential.write_bytes(b'X')
        return result
    monkeypatch.setattr(os, 'read', read_and_change)
    with pytest.raises(Rejected) as exc:
        credentials(credential)
    assert changed[0] and 'synthetic-' not in str(exc.value)


@pytest.mark.parametrize('kind', ['mode', 'hardlink', 'fifo', 'oversize', 'nonascii', 'control',
                                  'trailing_tab', 'multiple_newlines', 'parent_mode'])
def test_unsafe_files_and_parent_are_rejected(credential, kind):
    if kind == 'mode': credential.chmod(0o644)
    elif kind == 'hardlink': os.link(credential, credential.parent / 'second-link')
    elif kind == 'fifo': credential.unlink(); os.mkfifo(credential, 0o600)
    elif kind == 'oversize': credential.write_bytes(b'X' * 4097)
    elif kind == 'nonascii': credential.write_bytes(b'A' * 20 + b'\xff')
    elif kind == 'control': credential.write_bytes(b'A' * 20 + b'\x00')
    elif kind == 'trailing_tab': credential.write_bytes(b'A' * 20 + b'\t')
    elif kind == 'multiple_newlines': credential.write_bytes(b'A' * 20 + b'\n\n')
    else: credential.parent.chmod(0o777)
    with pytest.raises(Rejected): credentials(credential)


def test_wrong_owner_rejected_when_privileged(credential):
    if os.geteuid() != 0: pytest.skip('requires synthetic ownership change')
    os.chown(credential, 65534, 65534)
    with pytest.raises(Rejected): credentials(credential)


def test_credential_fds_closed_on_failure(credential, monkeypatch):
    if not Path('/proc/self/fd').is_dir(): pytest.skip('requires procfs fd inventory')
    before = set(os.listdir('/proc/self/fd'))
    original_read = os.read
    def fail(fd, size):
        if stat.S_ISREG(os.fstat(fd).st_mode): raise OSError('synthetic read failure')
        return original_read(fd, size)
    monkeypatch.setattr(os, 'read', fail)
    with pytest.raises(Rejected): credentials(credential)
    assert set(os.listdir('/proc/self/fd')) == before


def test_non_posix_fails_closed_without_acl_proof(credential, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(os, 'name', 'nt')
        with pytest.raises(Rejected, match='credential_protection_unavailable'):
            credentials(credential)


def test_excessive_path_depth_is_rejected_before_open(monkeypatch):
    monkeypatch.setattr(os, 'open', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('must not open')))
    with pytest.raises(Rejected, match='credential_reference_required'):
        credentials('/' + '/'.join(['x'] * 128) + '/token')


def test_embedded_nul_is_sanitized(credential):
    with pytest.raises(Rejected, match='credential_file_invalid') as exc:
        credentials(str(credential) + '\x00synthetic-secret')
    assert 'synthetic-secret' not in str(exc.value)


@pytest.mark.parametrize('kind', [TypeError, NotImplementedError])
def test_unsupported_descriptor_operations_are_sanitized(credential, monkeypatch, kind):
    original_stat = os.stat
    def unsupported(path, *args, **kwargs):
        if kwargs.get('dir_fd') is not None:
            raise kind('synthetic-secret')
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', unsupported)
    with pytest.raises(Rejected, match='credential_file_invalid') as exc:
        credentials(credential)
    assert 'synthetic-secret' not in str(exc.value)
