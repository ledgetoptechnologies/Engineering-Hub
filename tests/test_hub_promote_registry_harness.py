"""Runs the real promotion and sha-tag shell steps against a stub `docker`.

The steps are extracted from the workflow YAML, so this exercises the exact
shell that GitHub would run, with a fake registry. Live registry wording,
GitHub APIs and token permissions are not covered here.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
PACKAGE = 'ghcr.io/ledgetoptechnologies/engineering-hub-control'
OLD = 'sha256:' + 'a' * 64
NEW = 'sha256:' + 'b' * 64
OTHER = 'sha256:' + 'e' * 64
COMMIT = 'c' * 40
MANIFEST_UNKNOWN = f'ERROR: {PACKAGE}:latest: MANIFEST_UNKNOWN: manifest unknown; map[Tag:latest]'

STUB = r'''#!/usr/bin/env bash
key() { echo "$REG/$(echo "$1" | tr '/:@' '___')"; }
if [[ "$1 $2 $3" == "buildx imagetools inspect" ]]; then
  ref="$4"
  if [[ "$ref" == *:latest && -n "${STUB_LATEST_ERROR:-}" ]]; then echo "$STUB_LATEST_ERROR" >&2; exit 1; fi
  if [[ "$ref" == *:latest && -f "$REG/latest-error.txt" ]]; then cat "$REG/latest-error.txt" >&2; exit 1; fi
  if [[ "$ref" == *:latest && -n "${STUB_VERIFY_FAIL:-}" && -f "$REG/written" ]]; then printf '"%s"\n' "$STUB_VERIFY_FAIL"; exit 0; fi
  if [[ "$ref" == *@sha256:* ]]; then
    d="${ref#*@}"; if grep -qx "$d" "$REG"/*_* 2>/dev/null; then printf '"%s"\n' "$d"; exit 0; fi
    echo "ERROR: $ref: not found" >&2; exit 1
  fi
  f="$(key "$ref")"
  [[ -f "$f" ]] || { echo "${STUB_MISSING_MSG:-ERROR: $ref: not found}" >&2; exit 1; }
  if [[ "${5:-}" == "--raw" ]]; then echo "{\"manifest\": \"$(cat "$f")\"}"; else printf '"%s"\n' "$(cat "$f")"; fi
  exit 0
fi
if [[ "$1 $2 $3" == "buildx imagetools create" ]]; then
  shift 3; [[ "$1" == "--prefer-index=false" ]] && shift
  [[ "$1" == "--tag" ]] || exit 2
  echo "${3#*@}" > "$(key "$2")"; touch "$REG/written"; exit 0
fi
exit 2
'''


def _steps(name, job):
    config = yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)
    return {step.get('name'): step['run'] for step in config['jobs'][job]['steps'] if 'run' in step}


@pytest.fixture
def registry(tmp_path):
    bash = shutil.which('bash')
    if not bash or os.name == 'nt':
        pytest.skip('needs a POSIX bash')
    (tmp_path / 'bin').mkdir()
    stub = tmp_path / 'bin' / 'docker'
    stub.write_text(STUB, encoding='utf-8')
    stub.chmod(0o755)
    (tmp_path / 'reg').mkdir()
    (tmp_path / 'tmp').mkdir()
    return tmp_path


def _tag(root, ref, digest):
    (root / 'reg' / ref.replace('/', '_').replace(':', '_').replace('@', '_')).write_text(digest + '\n')


def _read(root, ref):
    path = root / 'reg' / ref.replace('/', '_').replace(':', '_').replace('@', '_')
    return path.read_text().strip() if path.exists() else 'absent'


def _run(root, script, extra):
    out = root / 'github_output'
    out.touch()
    env = {'PATH': f"{root / 'bin'}:{os.environ['PATH']}", 'REG': str(root / 'reg'), 'RUNNER_TEMP': str(root / 'tmp'),
           'GITHUB_OUTPUT': str(out), 'GITHUB_STEP_SUMMARY': str(root / 'summary.md'), 'PACKAGE': PACKAGE,
           'PROMOTE_COMMIT': COMMIT, 'PROMOTE_DIGEST': NEW, 'CANDIDATE_RUN_ID': '11', 'GITHUB_RUN_ID': '22',
           'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_REPOSITORY': 'o/r',
           'PROMOTE_RETRY_DELAY_SECONDS': '0', 'INITIALIZE_LATEST': 'false', 'GITHUB_ACTOR': 'dispatcher',
           'APPROVED_BY': 'release owner', 'APPROVAL_REFERENCE': 'hub-decision:1234', **extra}
    return subprocess.run(['bash', '-e', '-c', script], cwd=root, env=env, capture_output=True, text=True)


def _promote(root, before_move=None, **extra):
    steps = _steps('hub-promote.yml', 'promote')
    previous = _run(root, steps['Read current latest and resolve the rollback target'], extra)
    result = {'previous': previous, 'receipt': None, 'move': None}
    if previous.returncode:
        return result
    outputs = dict(line.split('=', 1) for line in (root / 'github_output').read_text().splitlines())
    extra = {**extra, 'PREVIOUS_DIGEST': outputs['digest'], 'PROMOTION_MODE': outputs['mode']}
    result['receipt'] = _run(root, steps['Persist the rollback receipt before moving latest'], extra)
    if result['receipt'].returncode == 0:
        if before_move:
            before_move(root)
        result['move'] = _run(root, steps['Move latest to the tested digest'], extra)
    return result


def _receipt(root):
    path = root / 'tmp' / 'hub-promotion-receipt' / 'hub-promotion-receipt.json'
    return path.read_text() if path.exists() else None


def _result(root):
    path = root / 'tmp' / 'hub-promotion-result' / 'hub-promotion-result.json'
    return path.read_text() if path.exists() else None


def test_latest_present_is_replaced_after_receipt(registry):
    _tag(registry, f'{PACKAGE}:latest', OLD)
    r = _promote(registry)
    assert r['move'].returncode == 0, r['move'].stderr
    assert _read(registry, f'{PACKAGE}:latest') == NEW
    assert f'"previousDigest": "{OLD}"' in _receipt(registry) and '"mode": "replace"' in _receipt(registry)
    assert '"status": "promoted"' in _result(registry)


@pytest.mark.parametrize('error', ['ERROR: failed to authorize: 401 Unauthorized', 'ERROR: dial tcp: i/o timeout',
                                   'denied: requested resource not found or access denied'])
def test_registry_error_on_read_aborts_without_writing(registry, error):
    _tag(registry, f'{PACKAGE}:latest', OLD)
    r = _promote(registry, STUB_LATEST_ERROR=error)
    assert r['previous'].returncode != 0 and r['previous'].stderr.count('failed (attempt') == 3
    assert r['receipt'] is None and _receipt(registry) is None
    assert _read(registry, f'{PACKAGE}:latest') == OLD and not (registry / 'reg' / 'written').exists()


def test_bare_not_found_aborts_even_with_opt_in(registry):
    r = _promote(registry, INITIALIZE_LATEST='true')
    assert r['previous'].returncode != 0 and r['receipt'] is None
    assert _read(registry, f'{PACKAGE}:latest') == 'absent'


def test_absence_without_opt_in_aborts(registry):
    r = _promote(registry, STUB_MISSING_MSG=MANIFEST_UNKNOWN)
    assert r['previous'].returncode != 0 and 'expected to exist' in r['previous'].stdout
    assert _read(registry, f'{PACKAGE}:latest') == 'absent'


def test_absence_with_opt_in_initializes_and_records_intent(registry):
    r = _promote(registry, STUB_MISSING_MSG=MANIFEST_UNKNOWN, INITIALIZE_LATEST='true')
    assert r['move'].returncode == 0, r['move'].stderr
    assert '"mode": "initialize"' in _receipt(registry) and '"previousDigest": "absent"' in _receipt(registry)
    assert _read(registry, f'{PACKAGE}:latest') == NEW


def test_opt_in_refused_when_latest_exists(registry):
    _tag(registry, f'{PACKAGE}:latest', OLD)
    r = _promote(registry, INITIALIZE_LATEST='true')
    assert r['previous'].returncode != 0 and _read(registry, f'{PACKAGE}:latest') == OLD


def test_malformed_latest_digest_aborts(registry):
    _tag(registry, f'{PACKAGE}:latest', 'sha256:XYZ')
    r = _promote(registry)
    assert r['previous'].returncode != 0 and 'malformed' in r['previous'].stdout
    assert _read(registry, f'{PACKAGE}:latest') == 'sha256:XYZ'


def test_write_succeeds_but_verification_fails_keeps_receipt_and_exits_nonzero(registry):
    _tag(registry, f'{PACKAGE}:latest', OLD)
    r = _promote(registry, STUB_VERIFY_FAIL=OTHER)
    assert r['move'].returncode == 1
    assert f'"previousDigest": "{OLD}"' in _receipt(registry)
    assert '"status": "verify-failed"' in _result(registry)
    assert f'--tag {PACKAGE}:latest {PACKAGE}@{OLD}' in r['move'].stdout


def test_already_promoted_is_a_no_op(registry):
    _tag(registry, f'{PACKAGE}:latest', NEW)
    r = _promote(registry)
    assert r['move'].returncode == 0 and not (registry / 'reg' / 'written').exists()


def _sha_step(root, **extra):
    script = next(run for name, run in _steps('hub-control.yml', 'publish').items() if 'imagetools create' in run)
    (root / 'hub-registry-manifest.json').write_text(f'{{"manifest": "{NEW}"}}\n')
    return _run(root, script, {'GITHUB_SHA': COMMIT, 'IMAGE_DIGEST': NEW, **extra})


def test_sha_tag_is_created_once_and_never_moved(registry):
    assert _sha_step(registry).returncode == 0
    assert _read(registry, f'{PACKAGE}:sha-{COMMIT}') == NEW
    assert _sha_step(registry).returncode == 0
    _tag(registry, f'{PACKAGE}:sha-{COMMIT}', OLD)
    moved = _sha_step(registry)
    assert moved.returncode != 0 and 'never moved' in moved.stdout
    assert _read(registry, f'{PACKAGE}:sha-{COMMIT}') == OLD


def test_sha_tag_registry_error_aborts(registry):
    r = _sha_step(registry, STUB_MISSING_MSG='ERROR: failed to authorize: 401 Unauthorized')
    assert r.returncode != 0 and _read(registry, f'{PACKAGE}:sha-{COMMIT}') == 'absent'


def test_latest_changed_between_receipt_and_write_aborts_and_keeps_receipt(registry):
    _tag(registry, f'{PACKAGE}:latest', OLD)
    r = _promote(registry, before_move=lambda root: _tag(root, f'{PACKAGE}:latest', OTHER))
    assert r['move'].returncode == 1 and 'nothing was written' in r['move'].stdout
    assert _read(registry, f'{PACKAGE}:latest') == OTHER and not (registry / 'reg' / 'written').exists()
    assert f'"previousDigest": "{OLD}"' in _receipt(registry)
    assert '"status": "aborted-latest-changed"' in _result(registry)


def test_latest_unreadable_before_write_aborts(registry):
    _tag(registry, f'{PACKAGE}:latest', OLD)
    r = _promote(registry, before_move=lambda root: (root / 'reg' / 'latest-error.txt').write_text('ERROR: dial tcp: i/o timeout'))
    assert r['move'].returncode == 1 and not (registry / 'reg' / 'written').exists()
    assert '"status": "aborted-latest-changed"' in _result(registry)


def test_receipt_records_approver_and_reference_as_unenforced(registry):
    _tag(registry, f'{PACKAGE}:latest', OLD)
    r = _promote(registry)
    receipt = _receipt(registry)
    assert r['move'].returncode == 0
    for text in ['"approvedBy": "release owner"', '"approvalReference": "hub-decision:1234"',
                 '"dispatchedBy": "dispatcher"', '"approvalEnforced": false']:
        assert text in receipt


def test_failed_read_back_says_latest_was_not_restored(registry):
    _tag(registry, f'{PACKAGE}:latest', OLD)
    r = _promote(registry, STUB_VERIFY_FAIL=OTHER)
    assert 'latest was NOT restored automatically.' in r['move'].stdout
    assert 'NOT restored automatically' in (registry / 'summary.md').read_text()


def test_initialize_rereads_absence_before_write(registry):
    r = _promote(registry, before_move=lambda root: _tag(root, f'{PACKAGE}:latest', OTHER),
                 STUB_MISSING_MSG=MANIFEST_UNKNOWN, INITIALIZE_LATEST='true')
    assert r['move'].returncode == 1 and _read(registry, f'{PACKAGE}:latest') == OTHER


@pytest.mark.parametrize('message', [
    f'ERROR: {PACKAGE}:latest: MANIFEST_UNKNOWN: manifest unknown; 401 Unauthorized',
    f'ERROR: {PACKAGE}:latest: MANIFEST_UNKNOWN: manifest unknown; NAME_UNKNOWN: repository name not known',
    f'ERROR: {PACKAGE}:latest: MANIFEST_UNKNOWN: manifest unknown' + chr(10) + 'ERROR: dial tcp: i/o timeout',
    f'ERROR: {PACKAGE}:latest: manifest unknown (tls handshake retry)',
    f'ERROR: {PACKAGE}:other: MANIFEST_UNKNOWN: manifest unknown',
    'MANIFEST_UNKNOWN',
    '',
])
def test_mixed_or_ambiguous_absence_is_refused_even_with_opt_in(registry, message):
    r = _promote(registry, STUB_MISSING_MSG=message, INITIALIZE_LATEST='true')
    assert r['previous'].returncode != 0 and r['receipt'] is None
    assert _read(registry, f'{PACKAGE}:latest') == 'absent' and not (registry / 'reg' / 'written').exists()


def test_pre_write_reread_refuses_mixed_absence_error(registry):
    mixed = f'ERROR: {PACKAGE}:latest: MANIFEST_UNKNOWN: manifest unknown; 503 Service Unavailable'
    r = _promote(registry, before_move=lambda root: (root / 'reg' / 'latest-error.txt').write_text(mixed),
                 STUB_MISSING_MSG=MANIFEST_UNKNOWN, INITIALIZE_LATEST='true')
    assert r['move'].returncode == 1 and not (registry / 'reg' / 'written').exists()
    assert '"status": "aborted-latest-changed"' in _result(registry)


@pytest.mark.parametrize('message', [
    'ERROR: ghcr.io/ledgetoptechnologies/other-image:latest: MANIFEST_UNKNOWN: manifest unknown',
    'ERROR: ghcr.io/someone-else/engineering-hub-control:latest: MANIFEST_UNKNOWN: manifest unknown',
    'ERROR: docker.io/ledgetoptechnologies/engineering-hub-control:latest: MANIFEST_UNKNOWN: manifest unknown',
    f'ERROR: {PACKAGE}@sha256:{"a" * 64}: MANIFEST_UNKNOWN: manifest unknown',
    f'ERROR: {PACKAGE}:latest-old: MANIFEST_UNKNOWN: manifest unknown',
    f'ERROR: {PACKAGE}:latest: MANIFEST_UNKNOWN: manifest unknown; map[Tag:other]',
    f'ERROR: {PACKAGE}:latest: not found',
    f'failed: {PACKAGE}:latest: MANIFEST_UNKNOWN: manifest unknown',
])
def test_absence_must_name_the_exact_requested_reference(registry, message):
    r = _promote(registry, STUB_MISSING_MSG=message, INITIALIZE_LATEST='true')
    assert r['previous'].returncode != 0 and r['receipt'] is None
    assert not (registry / 'reg' / 'written').exists()


def test_both_recognised_absence_forms_initialize(registry):
    r = _promote(registry, STUB_MISSING_MSG=f'ERROR: {PACKAGE}:latest: MANIFEST_UNKNOWN: manifest unknown',
                 INITIALIZE_LATEST='true')
    assert r['move'].returncode == 0 and _read(registry, f'{PACKAGE}:latest') == NEW
