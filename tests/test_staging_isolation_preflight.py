import importlib.util
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
