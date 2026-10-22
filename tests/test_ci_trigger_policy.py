"""Static GitHub workflow contract: development CI belongs on staging.

These checks validate declared triggers/guards, not GitHub account capacity or
the actual hosted scheduler. In particular, a manual dispatch can select any
ref, so the Hub jobs must repeat the exact main-ref/event restriction.
"""
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
    verify = [step['run'] for step in jobs['verify']['steps'] if helper in step.get('run', '')]
    assert len(verify) == 1
    assert '--image "ghcr.io/ledgetoptechnologies/engineering-hub-control:build-' in verify[0]
    assert '--commit "$GITHUB_SHA"' in verify[0]
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
