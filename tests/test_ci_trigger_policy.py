"""Static GitHub workflow contract: development CI belongs on staging.

These checks validate declared triggers/guards, not GitHub account capacity or
the actual hosted scheduler. In particular, a manual dispatch can select any
ref, so the Hub jobs must repeat the exact main-ref/event restriction.
"""
import re
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
    assert config['on']['push'] == {'branches': ['main']}
    assert config['permissions'] == {'contents': 'read'}


def test_jobs_repeat_main_ref_and_known_event_guards():
    legacy = workflow('container.yml')['jobs']
    current = workflow('hub-control.yml')['jobs']
    assert legacy['verify']['if'] == PUSH_MAIN
    assert current['verify']['if'] == PUSH_OR_MANUAL_MAIN
    assert current['publish']['if'] == PUSH_OR_MANUAL_MAIN
    assert current['publish']['needs'] == 'verify'
    assert legacy['publish']['if'] == '${{ false }}'
    assert legacy['publish']['needs'] == 'verify'


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
    assert workflow('container.yml')['jobs']['publish']['if'] == '${{ false }}'


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


def _publish_runs():
    return [step.get('run', '') for step in workflow('hub-control.yml')['jobs']['publish']['steps']]


def test_main_build_publishes_only_immutable_tags_and_never_moves_latest():
    text = (ROOT / '.github/workflows/hub-control.yml').read_text()
    assert ':latest' not in text and '--tag "$PACKAGE:latest"' not in text
    runs = _publish_runs()
    creates = [run for run in runs if 'imagetools create' in run]
    assert len(creates) == 1
    sha = creates[0]
    assert 'SHA_TAG="$PACKAGE:sha-$GITHUB_SHA"' in sha
    assert 'immutable sha tags are never moved"; exit 1' in sha
    assert 'docker buildx imagetools create --prefer-index=false --tag "$SHA_TAG" "$PACKAGE@$IMAGE_DIGEST"' in sha
    assert 'cmp hub-registry-manifest.json hub-sha-manifest.json' in sha
    assert "denied|unauthori[sz]ed|forbidden" in sha and '{ cat sha-inspect-error.txt >&2; exit 1; }' in sha
    steps = workflow('hub-control.yml')['jobs']['publish']['steps']
    upload = next(i for i, step in enumerate(steps) if 'docker push "$BUILD_IMAGE"' in step.get('run', ''))
    private = next(i for i, step in enumerate(steps) if 'Anonymous pull token unexpectedly available' in step.get('run', ''))
    tag = next(i for i, step in enumerate(steps) if 'imagetools create' in step.get('run', ''))
    evidence = next(i for i, step in enumerate(steps) if 'scripts/hub_candidate_evidence.py write' in step.get('run', ''))
    artifact = next(i for i, step in enumerate(steps) if step.get('with', {}).get('name') == 'hub-control-candidate-${{ github.sha }}')
    assert upload < private < tag < evidence < artifact
    assert steps[artifact]['with']['if-no-files-found'] == 'error'
    assert 'hub-candidate.sha256' in steps[artifact]['with']['path']


PROMOTE_ORDER = [
    'Validate requested candidate', 'Check out promotion verifier', 'Require the commit to be on main',
    'Find the successful main build run for the commit', "Verify the candidate's evidence", None, None,
    'Require the private package', 'Verify the registry still holds the exact candidate',
    'Read current latest and resolve the rollback target', 'Persist the rollback receipt before moving latest',
    'Upload the rollback receipt', 'Move latest to the tested digest', 'Upload the promotion result']


def test_only_the_manual_main_promotion_moves_latest():
    config = workflow('hub-promote.yml')
    text = (ROOT / '.github/workflows/hub-promote.yml').read_text()
    assert set(config['on']) == {'workflow_dispatch'}
    inputs = config['on']['workflow_dispatch']['inputs']
    assert set(inputs) == {'commit', 'digest', 'initialize_latest', 'approved_by', 'approval_reference'}
    assert inputs['approved_by']['required'] == 'true' and inputs['approval_reference']['required'] == 'true'
    assert inputs['initialize_latest']['default'] == 'false' and inputs['initialize_latest']['type'] == 'boolean'
    assert config['permissions'] == {'contents': 'read'}
    assert config['concurrency'] == {'group': 'hub-promote-latest', 'cancel-in-progress': 'false'}
    job = config['jobs']['promote']
    assert job['if'] == "${{ github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' }}"
    assert job['permissions'] == {'contents': 'read', 'actions': 'read', 'packages': 'write'}
    assert [step.get('name') for step in job['steps']] == PROMOTE_ORDER
    steps = {step.get('name'): step for step in job['steps']}
    # Inputs reach shell only through env; no build, no other secrets.
    assert all('${{ inputs.' not in step.get('run', '') for step in job['steps'])
    assert 'build-push-action' not in text and 'secrets.' not in text.replace('secrets.GITHUB_TOKEN', '')
    run = steps['Find the successful main build run for the commit']['run']
    for guard in ['-f branch=main', '-f status=success', '.head_branch == \\"main\\"', '.conclusion == \\"success\\"',
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
    assert r"grep -qE '\bMANIFEST_UNKNOWN\b|\bmanifest unknown\b'" in body and 'not found|' not in body
    assert 'for attempt in 1 2 3' in body
    assert 'if manifest_unknown_only "$RUNNER_TEMP/inspect-error.txt" "${ref##*[:@]}"; then' in body
    assert '[[ "$(grep -c . "$file")" == 1 ]] || return 1' in body and 'NAME_UNKNOWN' in body
    assert 'latest is missing although production latest is expected to exist' in body
    assert 'initialize_latest was requested but latest already exists' in body
    assert 'inspect_digest "$PACKAGE@$PREVIOUS_DIGEST"' in body
    receipt = steps['Persist the rollback receipt before moving latest']['run']
    for field in ['"mode"', 'previousDigest', 'newDigest', '"commit"', 'candidateRunId', 'promotionRunId', 'recordedAt',
                  'dispatchedBy', 'approvedBy', 'approvalReference', '"approvalEnforced": false']:
        assert field in receipt
    assert steps['Upload the rollback receipt']['with']['if-no-files-found'] == 'error'
    assert steps['Upload the promotion result']['if'] == "${{ always() && steps.receipt.outcome == 'success' }}"
    assert text.count('always()') == 1
    assert text.count('imagetools create --prefer-index=false --tag "$PACKAGE:latest"') == 1
    move = steps['Move latest to the tested digest']['run']
    assert re.search(r'record verify-failed[\s\S]*?rollback_help[\s\S]*?exit 1', move)
    # Fail-closed re-read of latest immediately before the single write; documented as not a CAS.
    reread = move.index('CURRENT="$(docker buildx imagetools inspect "$PACKAGE:latest"')
    abort = move.index('record aborted-latest-changed')
    write = move.index('imagetools create --prefer-index=false --tag "$PACKAGE:latest"')
    assert reread < abort < write
    assert 'if [[ "$CURRENT" != "$PREVIOUS_DIGEST" ]]; then' in move
    assert 'if [[ "$PREVIOUS_DIGEST" == absent ]] && manifest_unknown_only "$RUNNER_TEMP/reread-error.txt" latest; then' in move
    assert 'NOT an atomic compare-and-swap' in move
    assert 'NOT an atomic compare-and-swap' in text.split(chr(10) + 'on:' + chr(10))[0]
    assert 'latest was NOT restored automatically.' in move
    assert 'It was NOT restored automatically' in move
    validate = steps['Validate requested candidate']['run']
    assert '$APPROVED_BY' in validate and '$APPROVAL_REFERENCE' in validate
    assert job['env']['APPROVED_BY'] == '${{ inputs.approved_by }}'
    assert 'GitHub Environment with required' in text and 'reviewers' in text


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
