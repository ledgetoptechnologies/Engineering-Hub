"""Fixed bootstrap IPC rejects malformed requests without opening resources."""
import json
import io
import types
from pathlib import Path
import subprocess
import sys

import pytest

if sys.platform != 'linux':
    pytest.skip('local baseline child requires Linux', allow_module_level=True)


@pytest.mark.parametrize('body', [b'{}', b'{"owner":"one","owner":"two"}', b'x' * (4*1024*1024+1)],
                         ids=['empty-object', 'duplicate-field', 'oversized'])
def test_invalid_child_request_is_sanitized_and_creates_nothing(tmp_path, body):
    entry = Path(__file__).resolve().parents[1] / 'scripts/hub_local_baseline_child.py'
    child = subprocess.run([sys.executable, '-I', '-B', str(entry)], input=body,
                           capture_output=True, timeout=10, cwd=tmp_path,
                           env={'LANG':'C.UTF-8','PATH':'/usr/bin:/bin'})
    assert child.returncode == 2 and child.stderr == b''
    assert json.loads(child.stdout) == {'error': 'local_baseline_unavailable'}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(('message','expected'), [
    ('baseline_inventory_changed','baseline_inventory_changed'),
    ('private client material','unavailable'),
])
def test_child_reports_only_allowlisted_snapshot_codes(monkeypatch, capsys, message, expected):
    from scripts import hub_local_baseline_child as child
    from hub_container_executor.baseline_snapshot import BaselineSnapshotError
    monkeypatch.setattr(child.sys,'stdin',types.SimpleNamespace(buffer=io.BytesIO(b'{}')))
    def fail(_):
        raise BaselineSnapshotError(message)
    monkeypatch.setattr(child,'run',fail)
    assert child.main()==2
    out=capsys.readouterr()
    assert json.loads(out.out)=={'error':'local_baseline_unavailable','phase':'snapshot','code':expected}
    assert out.err==''


def test_child_phase_diagnostic_redacts_unknown_values(monkeypatch,capsys):
    from scripts import hub_local_baseline_child as child
    monkeypatch.setattr(child.sys,'stdin',types.SimpleNamespace(buffer=io.BytesIO(b'{}')))
    def fail(_):
        child._DIAGNOSTIC.update(phase='observe',inventory='private value')
        raise ValueError('private exception')
    monkeypatch.setattr(child,'run',fail)
    assert child.main()==2
    out=capsys.readouterr()
    assert json.loads(out.out)=={'error':'local_baseline_unavailable','phase':'observe','inventory':'unavailable'}
    assert out.err==''
