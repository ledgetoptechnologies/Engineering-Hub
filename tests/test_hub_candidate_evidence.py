import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.hub_candidate_evidence import PACKAGE, SMOKE_EVIDENCE, EvidenceError, build_record, verify_record

ROOT = Path(__file__).parents[1]
REPO = 'ledgetoptechnologies/Engineering-Hub'
COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
RUN = '123456789'
ENV = {
    'GITHUB_SHA': COMMIT, 'IMAGE_DIGEST': DIGEST, 'GITHUB_REPOSITORY': REPO, 'PACKAGE': PACKAGE,
    'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ID': RUN, 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_NAME': 'push',
    'GITHUB_WORKFLOW_REF': f'{REPO}/.github/workflows/hub-control.yml@refs/heads/main',
}
EXPECTED = {'commit': COMMIT, 'digest': DIGEST, 'repository': REPO, 'run_id': RUN}


def smoke_dir(tmp_path):
    for name in SMOKE_EVIDENCE:
        (tmp_path / name).write_text('{"ok": true}', encoding='utf-8')
    return tmp_path


def test_written_evidence_verifies_for_the_exact_candidate(tmp_path):
    record = build_record(ENV, smoke_dir(tmp_path))
    # Schema 2: written before the run's own latest step, so it makes no latest claim.
    assert record['evidenceSchemaVersion'] == 2 and 'latest' not in record
    assert record['shaTag'] == f'{PACKAGE}:sha-{COMMIT}'
    assert verify_record(record, EXPECTED, tmp_path) == {'commit': COMMIT, 'digest': DIGEST}


def test_schema_1_candidates_stay_promotable_for_rollback(tmp_path):
    record = {**build_record(ENV, smoke_dir(tmp_path)), 'evidenceSchemaVersion': 1, 'latest': 'unchanged'}
    assert verify_record(record, EXPECTED, tmp_path) == {'commit': COMMIT, 'digest': DIGEST}
    for bad in ({'latest': 'moved'}, {'evidenceSchemaVersion': 1, 'latest': None}):
        with pytest.raises(EvidenceError, match='promotion refused'):
            verify_record({**record, **bad}, EXPECTED, tmp_path)


@pytest.mark.parametrize('override', [{'GITHUB_REF': 'refs/heads/feature'}, {'IMAGE_DIGEST': 'latest'},
                                      {'GITHUB_SHA': 'abc'}, {'PACKAGE': 'ghcr.io/example/other'}])
def test_write_refuses_non_main_or_malformed_inputs(tmp_path, override):
    with pytest.raises(EvidenceError):
        build_record({**ENV, **override}, smoke_dir(tmp_path))


def test_write_requires_every_smoke_evidence_file(tmp_path):
    smoke_dir(tmp_path)
    (tmp_path / SMOKE_EVIDENCE[-1]).unlink()
    with pytest.raises(EvidenceError, match='missing smoke evidence'):
        build_record(ENV, tmp_path)


def _mutate(path, value):
    def change(record):
        target = record
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return change


@pytest.mark.parametrize('change,overrides', [
    (None, {'commit': 'c' * 40}), (None, {'digest': 'sha256:' + 'd' * 64}), (None, {'run_id': '1'}),
    (None, {'repository': 'someone/fork'}), (None, {'commit': 'a' * 7}), (None, {'digest': 'latest'}),
    (_mutate(['run', 'ref'], 'refs/heads/feature'), {}), (_mutate(['run', 'event'], 'pull_request'), {}),
    (_mutate(['run', 'workflowRef'], f'{REPO}/.github/workflows/other.yml@refs/heads/main'), {}),
    (_mutate(['package'], 'ghcr.io/example/other'), {}), (_mutate(['shaTag'], f'{PACKAGE}:latest'), {}),
    (_mutate(['evidenceSchemaVersion'], 3), {}), (_mutate(['evidenceSchemaVersion'], True), {}),
    (_mutate(['latest'], 'unchanged'), {}), (_mutate(['latest'], 'moved'), {}),
    (_mutate(['smokeEvidence'], []), {}),
])
def test_verify_refuses_anything_but_the_exact_main_candidate(tmp_path, change, overrides):
    record = build_record(ENV, smoke_dir(tmp_path))
    record = copy.deepcopy(record)
    if change:
        change(record)
    with pytest.raises(EvidenceError, match='promotion refused'):
        verify_record(record, {**EXPECTED, **overrides}, tmp_path)


def test_verify_requires_downloaded_smoke_files(tmp_path):
    record = build_record(ENV, smoke_dir(tmp_path))
    (tmp_path / SMOKE_EVIDENCE[0]).unlink()
    with pytest.raises(EvidenceError, match='missing smoke evidence'):
        verify_record(record, EXPECTED, tmp_path)


def test_command_line_write_and_verify(tmp_path):
    smoke_dir(tmp_path)
    script = ROOT / 'scripts' / 'hub_candidate_evidence.py'
    env = {**ENV, 'PATH': '', 'SYSTEMROOT': ''}
    subprocess.run([sys.executable, str(script), 'write', 'hub-candidate.json'], cwd=tmp_path, env=env, check=True)
    verify_env = {'PROMOTE_COMMIT': COMMIT, 'PROMOTE_DIGEST': DIGEST, 'GITHUB_REPOSITORY': REPO, 'CANDIDATE_RUN_ID': RUN}
    ok = subprocess.run([sys.executable, str(script), 'verify', str(tmp_path / 'hub-candidate.json')],
                        env=verify_env, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    bad = subprocess.run([sys.executable, str(script), 'verify', str(tmp_path / 'hub-candidate.json')],
                         env={**verify_env, 'PROMOTE_DIGEST': 'sha256:' + 'f' * 64}, capture_output=True, text=True)
    assert bad.returncode == 1 and 'evidence digest mismatch' in bad.stderr
    assert json.loads((tmp_path / 'hub-candidate.json').read_text())['commit'] == COMMIT
