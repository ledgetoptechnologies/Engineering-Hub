import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('staging_isolation_preflight',
    Path(__file__).resolve().parents[1] / 'scripts/staging_isolation_preflight.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_subordinate_ranges_overlap_and_malformed_are_not_silenced():
    assert module.subordinate_conflicts('a:100000:65536\nb:300000:100\n', 165535, 2) == 1
    assert module.subordinate_conflicts('a:100000:65536\n', 165536, 1) == 0
    with pytest.raises(ValueError):
        module.subordinate_conflicts('a:invalid:65536', 200000, 1)


def test_candidate_inventory_is_read_only_and_never_claims_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(module.pwd, 'getpwuid', lambda _: (_ for _ in ()).throw(KeyError()))
    monkeypatch.setattr(module.grp, 'getgrgid', lambda _: (_ for _ in ()).throw(KeyError()))
    monkeypatch.setattr(module.pwd, 'getpwall', lambda: [SimpleNamespace(pw_uid=200005)])
    monkeypatch.setattr(module.grp, 'getgrall', lambda: [])
    candidate = tmp_path / 'fresh'
    result = module.inspect_candidate(12345, 12345, 200000, 65536, str(candidate), 1)
    assert result['checks']['candidate_absent'] is True
    assert result['checks']['subid_overlaps_account_uid'] is True
    assert result['checks']['subid_overlaps_account_gid'] is False
    assert result['checks']['ancestor_free_floor_met'] is True
    assert result['checks']['future_target_mount_verified'] is False
    assert result['ready'] is False and result['incomplete']
    assert not candidate.exists()


def test_symlink_ancestor_is_unknown(tmp_path):
    (tmp_path / 'link').symlink_to(tmp_path, target_is_directory=True)
    result = module.inspect_candidate(12345, 12345, 200000, 65536, str(tmp_path / 'link' / 'new'), 1)
    assert result['checks']['candidate_absent'] is None
    assert result['checks']['ancestor_free_floor_met'] is None


def test_candidate_ids_in_subordinate_range_are_reported(tmp_path):
    result = module.inspect_candidate(200005, 200006, 200000, 65536, str(tmp_path / 'fresh'), 1)
    assert result['checks']['candidate_uid_in_subid_range'] is True
    assert result['checks']['candidate_gid_in_subid_range'] is True
    assert 'nss_enumeration_completeness_and_active_namespace_maps' in result['incomplete']


def test_namespace_map_scan_excludes_initial_and_reports_overlap_and_exited(tmp_path, monkeypatch):
    for name in ('1', 'self', '20', '30'):
        (tmp_path / name / 'ns').mkdir(parents=True)
    initial = tmp_path / '1/ns/user'
    initial.write_text('initial')
    os.link(initial, tmp_path / 'self/ns/user')
    (tmp_path / 'self/uid_map').write_text('0 0 4294967295\n')
    (tmp_path / 'self/gid_map').write_text('0 0 4294967295\n')
    (tmp_path / '20/ns/user').write_text('other')
    (tmp_path / '20/uid_map').write_text('0 200000 100\n')
    (tmp_path / '20/gid_map').write_text('0 400000 100\n')
    result = module.namespace_inventory(12345, 400005, 200000, 100, proc=tmp_path)
    assert result['proc_view_identity_map_verified'] is True
    assert result['host_proc_namespace_verified'] is False
    assert result['proc_pid1_namespace_skipped'] == 1 and result['inspected'] == 1
    assert result['uid_overlaps'] == result['gid_overlaps'] == 1
    assert result['exited'] == 1
    original_iterdir = Path.iterdir
    def no_enumeration(path):
        if path == tmp_path:
            raise PermissionError()
        return original_iterdir(path)
    with monkeypatch.context() as patcher:
        patcher.setattr(Path, 'iterdir', no_enumeration)
        unknown = module.namespace_inventory(1, 1, 2, 1, proc=tmp_path)
        assert unknown['proc_view_identity_map_verified'] is True
        assert unknown['inspected'] is None and unknown['uid_overlaps'] is None
    original_read = Path.read_text
    def denied(path, *args, **kwargs):
        if path == tmp_path / '20/uid_map':
            raise PermissionError()
        return original_read(path, *args, **kwargs)
    with monkeypatch.context() as patcher:
        patcher.setattr(Path, 'read_text', denied)
        assert module.namespace_inventory(1, 1, 2, 1, proc=tmp_path)['denied'] == 1
    original_stat = Path.stat
    calls = []
    def changed(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == tmp_path / '20/ns/user':
            calls.append(1)
            if len(calls) == 2:
                return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1)
        return info
    with monkeypatch.context() as patcher:
        patcher.setattr(Path, 'stat', changed)
        assert module.namespace_inventory(1, 1, 2, 1, proc=tmp_path)['invalid_or_changed'] == 1
    (tmp_path / '20/uid_map').write_text('broken')
    result = module.namespace_inventory(12345, 400005, 200000, 100, proc=tmp_path)
    assert result['invalid_or_changed'] == 1
    (tmp_path / 'self/uid_map').write_text('0 1000 1\n')
    unknown = module.namespace_inventory(1, 1, 2, 1, proc=tmp_path)
    assert unknown['proc_view_identity_map_verified'] is False
    assert unknown['uid_overlaps'] is None and unknown['inspected'] is None
