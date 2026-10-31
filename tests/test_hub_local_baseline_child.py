"""Fixed bootstrap IPC rejects malformed requests without opening resources."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


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
