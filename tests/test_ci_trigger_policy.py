"""Static GitHub workflow contract: development CI belongs on staging.

These checks validate declared triggers/guards, not GitHub account capacity or
the actual hosted scheduler. In particular, a manual dispatch can select any
ref, so the Hub jobs must repeat the exact main-ref/event restriction.
"""
import re
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parents[1]
MAIN = 'refs/heads/main'
PUSH_MAIN = "${{ github.ref == 'refs/heads/main' && github.event_name == 'push' }}"
PUSH_OR_MANUAL_MAIN = ("${{ github.ref == 'refs/heads/main' && "
    "(github.event_name == 'push' || github.event_name == 'workflow_dispatch') }}")


def workflow(name):
    # BaseLoader keeps the GitHub 'on' key as a string (not YAML1.1 bool True).
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


@pytest.mark.parametrize('name', ['container.yml', 'hub-control.yml'])
def test_no_pr_or_tag_trigger_and_push_is_exact_main(name):
    config = workflow(name)
    expected = {'push'} if name == 'container.yml' else {'push', 'workflow_dispatch'}
    assert set(config['on']) == expected
    assert config['on']['push']['branches'] == ['main']
    if name == 'container.yml':
        assert set(config['on']['push']['paths']) == {
            '.github/workflows/container.yml', '.dockerignore', '.env.example', 'Dockerfile',
            'compose.yaml', 'compose.truenas.yaml', 'docker/entrypoint.sh',
            'pyproject.toml', 'uv.lock', 'README.md', 'src/**',
        }
    else:
        assert config['on']['push'] == {'branches': ['main']}
    assert config['permissions'] == {'contents': 'read'}


def test_jobs_repeat_main_ref_and_known_event_guards():
    legacy = workflow('container.yml')['jobs']
    current = workflow('hub-control.yml')['jobs']
    assert legacy['verify']['if'] == PUSH_MAIN
    assert current['verify']['if'] == PUSH_OR_MANUAL_MAIN
    assert current['publish']['if'] == PUSH_OR_MANUAL_MAIN
    assert current['publish']['needs'] == 'verify'
    assert set(legacy) == {'verify'}


@pytest.mark.parametrize('event,ref,legacy_runs,hub_runs', [
    ('push', MAIN, True, True),
    ('push', 'refs/heads/codex/development', False, False),
    ('push', 'refs/heads/staging', False, False),
    ('push', 'refs/tags/v0.6.1', False, False),
    ('pull_request', 'refs/pull/43/merge', False, False),
    ('pull_request', MAIN, False, False),
    ('workflow_dispatch', MAIN, False, True),
    ('workflow_dispatch', 'refs/heads/codex/development', False, False),
    ('workflow_dispatch', 'refs/tags/v0.6.1', False, False),
])
def test_declared_policy_matrix(event, ref, legacy_runs, hub_runs):
    for name, expected in [('container.yml', legacy_runs), ('hub-control.yml', hub_runs)]:
        config = workflow(name)
        trigger = event in config['on']
        if event == 'push':
            trigger = trigger and ref == MAIN
        guard = config['jobs']['verify']['if']
        assert guard in {PUSH_MAIN, PUSH_OR_MANUAL_MAIN}
        admitted_events = {'push'} if guard == PUSH_MAIN else {'push', 'workflow_dispatch'}
        assert (trigger and ref == MAIN and event in admitted_events) is expected


def test_only_current_hub_can_publish_from_main():
    current = workflow('hub-control.yml')['jobs']['publish']
    assert current['permissions'] == {'contents': 'read', 'packages': 'write'}
    assert current['env']['PACKAGE'] == 'ghcr.io/ledgetoptechnologies/engineering-hub-control'
    assert current['needs'] == 'verify'
    assert set(workflow('container.yml')['jobs']) == {'verify'}


def test_manual_package_verification_also_requires_main():
    config = workflow('verify-published.yml')
    assert set(config['on']) == {'workflow_dispatch'}
    assert config['jobs']['verify-package']['if'] == (
        "${{ github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' }}")


def test_protected_image_profile_runs_before_upload_and_latest_promotion():
    jobs = workflow('hub-control.yml')['jobs']
    helper = 'python scripts/hub_protected_container_smoke.py'
    steps = jobs['publish']['steps']
    local = next(i for i, step in enumerate(steps)
                 if helper + ' --image "$BUILD_IMAGE"' in step.get('run', ''))
    upload = next(i for i, step in enumerate(steps)
                  if 'docker push "$BUILD_IMAGE"' in step.get('run', ''))
    registry = next(i for i, step in enumerate(steps)
                    if helper + ' --image "$PACKAGE@$DIGEST"' in step.get('run', ''))
    promote = next(i for i, step in enumerate(steps)
                   if 'imagetools create' in step.get('run', ''))
    assert local < upload == registry < promote
    assert '--commit "$GITHUB_SHA" > hub-local-protected-startup.json' in steps[local]['run']
    assert '--commit "$GITHUB_SHA" > hub-registry-protected-startup.json' in steps[registry]['run']
    assert any('hub-*-startup.json' in step.get('with', {}).get('path', '') for step in steps)


SMOKES = ('hub_release_smoke', 'hub_split_container_smoke', 'hub_unified_container_smoke',
          'hub_settings_container_smoke', 'hub_protected_container_smoke')


def test_image_is_built_once_and_every_check_gates_upload_and_promotion():
    jobs = workflow('hub-control.yml')['jobs']
    # verify is the source gate; the only image build is the publication candidate.
    verify = jobs['verify']['steps']
    assert not any('build-push-action' in step.get('uses', '') for step in verify)
    assert not any('_smoke.py' in step.get('run', '') for step in verify)
    steps = jobs['publish']['steps']
    builds = [i for i, step in enumerate(steps) if 'build-push-action' in step.get('uses', '')]
    assert len(builds) == 1 and steps[builds[0]]['with']['push'] == 'false'
    upload = next(i for i, step in enumerate(steps) if 'docker push "$BUILD_IMAGE"' in step.get('run', ''))
    promote = next(i for i, step in enumerate(steps) if 'imagetools create' in step.get('run', ''))
    for smoke in SMOKES:
        local = next(i for i, step in enumerate(steps)
                     if f'python scripts/{smoke}.py --image "$BUILD_IMAGE"' in step.get('run', ''))
        registry = next(i for i, step in enumerate(steps)
                        if f'python scripts/{smoke}.py --image "$PACKAGE@$DIGEST"' in step.get('run', ''))
        assert builds[0] < local < upload == registry < promote


@pytest.mark.parametrize('phase', ['local', 'registry'])
@pytest.mark.parametrize('fail_first', [False, True])
def test_smoke_pairs_join_before_release_continues(tmp_path, phase, fail_first):
    bash = shutil.which('bash')
    if os.name == 'nt' or not bash:
        pytest.skip('requires POSIX Bash')

    steps = workflow('hub-control.yml')['jobs']['publish']['steps']
    step = next(step['run'] for step in steps
                if f'> hub-{phase}-protected-startup.json' in step.get('run', ''))
    gate = step[step.index('wait_smokes()'):]
    mock = """
python() {
  case "$1" in
    */hub_release_smoke.py)
      if [ "$FAIL_FIRST" = yes ]; then return 7; fi ;;
    */hub_protected_container_smoke.py) sleep 0.05 ;;
  esac
  printf 'done\\n'
}
"""
    result = subprocess.run(
        [bash, '-c', 'set -eu\n' + mock + gate + '\nprintf release-continued'],
        cwd=tmp_path, capture_output=True, text=True, timeout=5,
        env={**os.environ, 'FAIL_FIRST': 'yes' if fail_first else 'no',
             'BUILD_IMAGE': 'local', 'PACKAGE': 'registry', 'DIGEST': 'digest', 'GITHUB_SHA': 'commit'},
    )
    assert (tmp_path / f'hub-{phase}-protected-startup.json').read_text() == 'done\n'
    if fail_first:
        assert result.returncode == 7
        assert not (tmp_path / f'hub-{phase}-split-startup.json').exists()
        assert result.stdout == ''
    else:
        assert result.returncode == 0, result.stderr
        assert len(list(tmp_path.glob(f'hub-{phase}-*startup.json'))) == 5
        assert result.stdout == 'release-continued'


def _publish_runs():
    return [step.get('run', '') for step in workflow('hub-control.yml')['jobs']['publish']['steps']]


LATEST_STEP = 'Move latest forward to the tested digest'
PROMOTE_MOVE = 'Move latest to the tested digest'
WRITER = './.github/workflows/hub-latest-writer.yml'


def test_main_build_publishes_only_immutable_tags_in_the_publish_job():
    publish = workflow('hub-control.yml')['jobs']['publish']
    runs = _publish_runs()
    assert not any(':latest' in run for run in runs)
    creates = [run for run in runs if 'imagetools create' in run]
    assert len(creates) == 1
    sha = creates[0]
    assert 'SHA_TAG="$PACKAGE:sha-$GITHUB_SHA"' in sha
    assert 'immutable sha tags are never moved"; exit 1' in sha
    assert 'docker buildx imagetools create --prefer-index=false --tag "$SHA_TAG" "$PACKAGE@$IMAGE_DIGEST"' in sha
    assert 'cmp hub-registry-manifest.json hub-sha-manifest.json' in sha
    assert "denied|unauthori[sz]ed|forbidden" in sha and '{ cat sha-inspect-error.txt >&2; exit 1; }' in sha
    steps = publish['steps']
    upload = next(i for i, step in enumerate(steps) if 'docker push "$BUILD_IMAGE"' in step.get('run', ''))
    private = next(i for i, step in enumerate(steps) if 'Anonymous pull token unexpectedly available' in step.get('run', ''))
    tag = next(i for i, step in enumerate(steps) if 'imagetools create' in step.get('run', ''))
    evidence = next(i for i, step in enumerate(steps) if 'scripts/hub_candidate_evidence.py write' in step.get('run', ''))
    artifact = next(i for i, step in enumerate(steps) if step.get('with', {}).get('name') == 'hub-control-candidate-${{ github.sha }}')
    assert upload < private < tag < evidence < artifact
    assert steps[artifact]['with']['if-no-files-found'] == 'error'
    assert 'hub-candidate.sha256' in steps[artifact]['with']['path']
    # The digest handed to the latest job is the one the registry step verified.
    assert publish['outputs'] == {'digest': '${{ steps.registry.outputs.digest }}'}
    registry = next(step for step in steps if step.get('id') == 'registry')
    assert 'echo "digest=$DIGEST" >> "$GITHUB_OUTPUT"' in registry['run']


def test_latest_is_a_separate_job_so_the_candidate_stays_promotable():
    jobs = workflow('hub-control.yml')['jobs']
    latest = jobs['latest']
    # Only after publish succeeded, only on a main push (a manual dispatch publishes only).
    assert latest['needs'] == 'publish'
    assert latest['if'] == "${{ github.ref == 'refs/heads/main' && github.event_name == 'push' }}"
    assert latest['uses'] == WRITER
    assert latest['with'] == {'mode': 'forward', 'commit': '${{ github.sha }}',
                              'digest': '${{ needs.publish.outputs.digest }}'}
    assert latest['permissions'] == {'contents': 'read', 'packages': 'write'}
    assert set(jobs) == {'verify', 'publish', 'latest', 'latest-confirm'}
    confirm = jobs['latest-confirm']
    assert confirm['needs'] == ['publish', 'latest']
    assert confirm['if'] == ("${{ always() && needs.publish.result == 'success' && github.ref == 'refs/heads/main' "
                             "&& github.event_name == 'push' }}")
    assert confirm['steps'][0]['env'] == {'LATEST_RESULT': '${{ needs.latest.result }}'}
    # hub-promote accepts a run whose publish job succeeded, whatever happened to `latest`.
    find = {s.get('name'): s for s in workflow('hub-promote.yml')['jobs']['promote']['steps']}[
        'Find the successful main build run for the commit']['run']
    assert '-f status=completed' in find and '-f status=success' not in find
    assert 'select(.name == "publish" and .conclusion == "success")' in find
    assert '.conclusion == \\"success\\"' not in find


def test_one_serialized_writer_for_forward_moves_and_manual_promotion():
    writer = workflow('hub-latest-writer.yml')
    assert set(writer['on']) == {'workflow_call'}
    assert set(writer['on']['workflow_call']['inputs']) == {'mode', 'commit', 'digest', 'expected_previous'}
    assert writer['permissions'] == {'contents': 'read'}
    assert set(writer['jobs']) == {'write'}
    job = writer['jobs']['write']
    # One repository-wide group; a running write is never cancelled.
    # A bounded, non-replacing queue (GitHub: up to 100 pending, FIFO; cannot be
    # combined with cancel-in-progress, whose default false is what this needs).
    assert job['concurrency'] == {'group': 'hub-latest-writer', 'queue': 'max'}
    assert job['if'] == ("${{ github.ref == 'refs/heads/main' && ((inputs.mode == 'forward' && github.event_name == 'push') "
                         "|| (inputs.mode == 'promote' && github.event_name == 'workflow_dispatch')) }}")
    assert job['permissions'] == {'contents': 'read', 'packages': 'write'}
    # Both callers use it, and nothing else does.
    callers = {(name, job_id) for name in ('hub-control.yml', 'hub-promote.yml', 'hub-latest-writer.yml',
                                           'hub-executor-publication.yml', 'hub-executor-compatibility.yml',
                                           'hub-rollback-verification.yml', 'verify-published.yml', 'container.yml')
               for job_id, body in workflow(name)['jobs'].items() if body.get('uses') == WRITER}
    assert callers == {('hub-control.yml', 'latest'), ('hub-promote.yml', 'write')}
    steps = {step.get('name'): step for step in job['steps']}
    assert steps[LATEST_STEP]['if'] == "${{ inputs.mode == 'forward' }}"
    assert steps[PROMOTE_MOVE]['if'] == "${{ inputs.mode == 'promote' }}"
    assert steps[PROMOTE_MOVE]['env'] == {'PROMOTE_COMMIT': '${{ inputs.commit }}', 'PROMOTE_DIGEST': '${{ inputs.digest }}',
                                          'PREVIOUS_DIGEST': '${{ inputs.expected_previous }}'}
    assert steps['Check out history for the forward-only check']['with']['fetch-depth'] == '0'
    assert all('${{' not in step.get('run', '') for step in job['steps'])
    text = (ROOT / '.github/workflows/hub-latest-writer.yml').read_text()
    for documented in ['queue: max', 'up to 100 jobs wait', 'queue is full', 'latest-confirm', 'NOT an atomic compare-and-swap']:
        assert documented in text


def test_forward_move_is_forward_only_and_summarised():
    steps = {s.get('name'): s for s in workflow('hub-latest-writer.yml')['jobs']['write']['steps']}
    move = steps[LATEST_STEP]['run']
    assert move.startswith('set -euo pipefail')
    candidate = move.index('read_digest "$PACKAGE:sha-$TARGET_COMMIT"')
    read = move.index('if ! PREVIOUS_DIGEST="$(read_digest "$PACKAGE:latest")"; then')
    label = move.index('org.opencontainers.image.revision')
    ancestry = move.index('git merge-base --is-ancestor "$PREVIOUS_COMMIT" "$TARGET_COMMIT"')
    behind = move.index('git merge-base --is-ancestor "$TARGET_COMMIT" "$PREVIOUS_COMMIT"')
    reread = move.index('CURRENT="$(read_digest "$PACKAGE:latest")"')
    write = move.index('imagetools create --prefer-index=false --tag "$PACKAGE:latest" "$PACKAGE@$TARGET_DIGEST"')
    verify = move.index('AFTER="$(read_digest "$PACKAGE:latest")"')
    assert candidate < read < label < ancestry < behind < reread < write < verify
    assert move.count('imagetools create') == 2  # the write and the printed rollback command
    for status in ['skipped-older-build', 'aborted-diverged', 'aborted-latest-changed', 'aborted-not-candidate',
                   'already-current', 'write-failed', 'verify-failed', 'record moved']:
        assert status in move
    assert 'latest was NOT restored automatically.' in move
    for field in ['previousDigest', 'previousCommit', 'newDigest', '"commit"', 'runId', 'latestAfter', 'recordedAt']:
        assert field in move
    assert "printf -- '- Commit: `%s`\\n- Digest: `%s`\\n' \"$TARGET_COMMIT\" \"$TARGET_DIGEST\"" in move
    assert "printf -- '- Previous latest: `%s` (commit `%s`)\\n' \"$PREVIOUS_DIGEST\" \"$PREVIOUS_COMMIT\"" in move
    upload = steps['Upload the forward result']
    assert upload['if'] == "${{ always() && steps.forward.outcome != 'skipped' }}"


PROMOTE_ORDER = [
    'Validate requested candidate', 'Check out promotion verifier', 'Require the commit to be on main',
    'Find the successful main build run for the commit', "Verify the candidate's evidence", None, None,
    'Require the private package', 'Verify the registry still holds the exact candidate',
    'Read current latest and resolve the rollback target', 'Persist the rollback receipt before moving latest',
    'Upload the rollback receipt']


def test_only_the_manual_main_promotion_moves_latest():
    config = workflow('hub-promote.yml')
    text = (ROOT / '.github/workflows/hub-promote.yml').read_text()
    assert set(config['on']) == {'workflow_dispatch'}
    inputs = config['on']['workflow_dispatch']['inputs']
    assert set(inputs) == {'commit', 'digest', 'initialize_latest', 'approved_by', 'approval_reference'}
    assert inputs['approved_by']['required'] == 'true' and inputs['approval_reference']['required'] == 'true'
    assert inputs['initialize_latest']['default'] == 'false' and inputs['initialize_latest']['type'] == 'boolean'
    assert config['permissions'] == {'contents': 'read'}
    # Manual admission queues instead of replacing a pending promotion.
    assert config['concurrency'] == {'group': 'hub-promote-latest', 'queue': 'max'}
    assert set(config['jobs']) == {'promote', 'write', 'confirm'}
    job = config['jobs']['promote']
    assert job['if'] == "${{ github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' }}"
    # The promote job only checks and records; the write is the shared writer's.
    assert job['permissions'] == {'contents': 'read', 'actions': 'read', 'packages': 'read'}
    assert job['outputs'] == {'previous': '${{ steps.previous.outputs.digest }}'}
    assert [step.get('name') for step in job['steps']] == PROMOTE_ORDER
    write = config['jobs']['write']
    assert write['needs'] == 'promote' and write['uses'] == WRITER
    assert write['with'] == {'mode': 'promote', 'commit': '${{ inputs.commit }}', 'digest': '${{ inputs.digest }}',
                             'expected_previous': '${{ needs.promote.outputs.previous }}'}
    assert write['permissions'] == {'contents': 'read', 'packages': 'write'}
    confirm = config['jobs']['confirm']
    assert confirm['needs'] == ['promote', 'write']
    assert confirm['if'] == "${{ always() && needs.promote.result == 'success' }}"
    steps = {step.get('name'): step for step in job['steps']}
    # Inputs reach shell only through env; no build, no other secrets.
    assert all('${{ inputs.' not in step.get('run', '') for step in job['steps'])
    assert 'build-push-action' not in text and 'secrets.' not in text.replace('secrets.GITHUB_TOKEN', '')
    assert 'imagetools create' not in text
    run = steps['Find the successful main build run for the commit']['run']
    for guard in ['-f branch=main', '-f status=completed', '.head_branch == \\"main\\"', '.status == \\"completed\\"',
                  '.path == \\".github/workflows/hub-control.yml\\"', '.head_repository.full_name == \\"$GITHUB_REPOSITORY\\"']:
        assert guard in run
    evidence = steps["Verify the candidate's evidence"]['run']
    assert '--name "hub-control-candidate-$PROMOTE_COMMIT"' in evidence
    assert 'sha256sum --check --strict hub-candidate.sha256' in evidence
    assert 'python3 scripts/hub_candidate_evidence.py verify' in evidence
    assert 'compare/$PROMOTE_COMMIT...$GITHUB_SHA' in steps['Require the commit to be on main']['run']
    registry = steps['Verify the registry still holds the exact candidate']['run']
    assert 'imagetools inspect "$PACKAGE:sha-$PROMOTE_COMMIT"' in registry and 'test "$SHA_DIGEST" = "$PROMOTE_DIGEST"' in registry
    previous = steps['Read current latest and resolve the rollback target']
    assert previous['env'] == {'INITIALIZE_LATEST': '${{ inputs.initialize_latest }}'}
    body = previous['run']
    assert 'imagetools create' not in body and '2>/dev/null' not in body
    classifier = body.split('manifest_unknown_only() {')[1].split('\n}')[0]
    assert 'not found' not in classifier and 'grep -q' not in classifier
    assert 'for attempt in 1 2 3' in body
    assert 'if manifest_unknown_only "$RUNNER_TEMP/inspect-error.txt" "$ref"; then' in body
    assert '[[ "$line" == "ERROR: $ref: MANIFEST_UNKNOWN: manifest unknown"' in body
    assert 'latest is missing although production latest is expected to exist' in body
    assert 'initialize_latest was requested but latest already exists' in body
    assert 'inspect_digest "$PACKAGE@$PREVIOUS_DIGEST"' in body
    receipt = steps['Persist the rollback receipt before moving latest']['run']
    for field in ['"mode"', 'previousDigest', 'newDigest', '"commit"', 'candidateRunId', 'promotionRunId', 'recordedAt',
                  'dispatchedBy', 'approvedBy', 'approvalReference', '"approvalEnforced": false']:
        assert field in receipt
    assert steps['Upload the rollback receipt']['with']['if-no-files-found'] == 'error'
    assert text.count('always()') == 1
    # The guarded move itself, moved verbatim into the shared writer.
    writer_text = (ROOT / '.github/workflows/hub-latest-writer.yml').read_text()
    writer_steps = {s.get('name'): s for s in workflow('hub-latest-writer.yml')['jobs']['write']['steps']}
    assert writer_steps['Upload the promotion result']['if'] == "${{ always() && steps.move.outcome != 'skipped' }}"
    assert writer_text.count('imagetools create --prefer-index=false --tag "$PACKAGE:latest" "$PACKAGE@$PROMOTE_DIGEST"') == 1
    move = writer_steps[PROMOTE_MOVE]['run']
    assert re.search(r'record verify-failed[\s\S]*?rollback_help[\s\S]*?exit 1', move)
    # Fail-closed re-read of latest immediately before the single write; documented as not a CAS.
    reread = move.index('CURRENT="$(docker buildx imagetools inspect "$PACKAGE:latest"')
    abort = move.index('record aborted-latest-changed')
    write_at = move.index('imagetools create --prefer-index=false --tag "$PACKAGE:latest"')
    assert reread < abort < write_at
    assert 'if [[ "$CURRENT" != "$PREVIOUS_DIGEST" ]]; then' in move
    assert 'if [[ "$PREVIOUS_DIGEST" == absent ]] && manifest_unknown_only "$RUNNER_TEMP/reread-error.txt" "$PACKAGE:latest"; then' in move
    assert 'NOT an atomic compare-and-swap' in move
    assert 'NOT an atomic compare-and-swap' in text.split(chr(10) + 'on:' + chr(10))[0]
    assert 'latest was NOT restored automatically.' in move
    assert 'It was NOT restored automatically' in move
    validate = steps['Validate requested candidate']['run']
    assert '$APPROVED_BY' in validate and '$APPROVAL_REFERENCE' in validate
    assert job['env']['APPROVED_BY'] == '${{ inputs.approved_by }}'
    assert 'GitHub Environment with required' in text and 'reviewers' in text


@pytest.mark.parametrize('result,code', [('success', 0), ('cancelled', 1), ('failure', 1), ('skipped', 1)])
def test_a_cancelled_or_failed_manual_write_fails_the_promotion_loudly(tmp_path, result, code):
    bash = shutil.which('bash')
    if os.name == 'nt' or not bash:
        pytest.skip('requires POSIX Bash')
    step = workflow('hub-promote.yml')['jobs']['confirm']['steps'][0]
    assert step['env'] == {'WRITE_RESULT': '${{ needs.write.result }}'}
    summary = tmp_path / 'summary.md'
    run = subprocess.run([bash, '-c', step['run']], capture_output=True, text=True, timeout=5,
                         env={**os.environ, 'WRITE_RESULT': result, 'GITHUB_STEP_SUMMARY': str(summary)})
    assert run.returncode == code
    if code:
        assert '::error::' in run.stdout and 'run this promotion again' in run.stdout
        assert f'This promotion did NOT complete: the write job was {result}.' in summary.read_text()
    if result == 'cancelled':
        assert 'queue was full (100 pending) or someone cancelled the run' in run.stdout


@pytest.mark.parametrize('result,code', [('success', 0), ('cancelled', 1), ('failure', 1), ('skipped', 1)])
def test_an_incomplete_forward_move_fails_the_main_run_loudly(tmp_path, result, code):
    bash = shutil.which('bash')
    if os.name == 'nt' or not bash:
        pytest.skip('requires POSIX Bash')
    step = workflow('hub-control.yml')['jobs']['latest-confirm']['steps'][0]
    summary = tmp_path / 'summary.md'
    run = subprocess.run([bash, '-c', step['run']], capture_output=True, text=True, timeout=5,
                         env={**os.environ, 'LATEST_RESULT': result, 'GITHUB_STEP_SUMMARY': str(summary)})
    assert run.returncode == code
    if code:
        assert '::error::' in run.stdout and 'promote this candidate by hand' in run.stdout
        assert f'the latest job was {result}. The candidate stays promotable' in summary.read_text()
    if result == 'cancelled':
        assert 'queue was full (100 pending) or someone cancelled the run' in run.stdout


def test_no_queue_is_combined_with_cancel_in_progress():
    # GitHub rejects `queue: max` together with cancel-in-progress.
    for path in sorted((ROOT / '.github/workflows').glob('*.yml')):
        config = workflow(path.name)
        groups = [config.get('concurrency')] + [job.get('concurrency') for job in config.get('jobs', {}).values()]
        for group in groups:
            if isinstance(group, dict) and group.get('queue') == 'max':
                assert 'cancel-in-progress' not in group, path.name
                assert set(group) == {'group', 'queue'}, path.name


def test_verify_runs_backend_e2e_and_pidfd_gates_once():
    verify = workflow('hub-control.yml')['jobs']['verify']['steps']
    runs = [step.get('run', '') for step in verify]
    combined = [run for run in runs if 'python -W default -m pytest -q' in run]
    assert len(combined) == 1
    assert 'npm run test:e2e' in combined[0]
    assert '[ "$pytest_status" -eq 0 ] && [ "$e2e_status" -eq 0 ]' in combined[0]
    assert sum('python scripts/hub_pidfd_slim_check.py' in run for run in runs) == 1
    assert sum('npm test' in run and 'npm run build' in run for run in runs) == 1
    # The legacy workflow no longer repeats the backend suite.
    legacy = workflow('container.yml')['jobs']['verify']['steps']
    assert not any('pytest' in step.get('run', '') for step in legacy)
