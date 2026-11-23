"""Inventory of every place in the repository that can write a registry tag.

`latest` of engineering-hub-control may be written by exactly two writers:
hub-control.yml's `publish` job, in its one main-push-only, forward-only
"Move latest forward to the tested digest" step, and hub-promote.yml's manual
`promote` job (rollback). The immutable `sha-<commit>` tag is written only by
hub-control.yml's `publish` job. Every other registry writer must write a
different tag. A new writer anywhere in workflows or scripts fails this test until it is
reviewed and added here.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / '.github' / 'workflows'

# Commands that write to a registry (shell and Python argument-list forms).
WRITE = re.compile(
    r"imagetools\s+create|'imagetools',\s*'create'|docker\s+push|'docker',\s*'push'|"
    r"buildx\s+build[^\n]*--push|'--push'|crane\s+(?:tag|copy|cp|push)|skopeo\s+copy|oras\s+(?:push|tag)")

# (workflow, job) -> what that job is allowed to write.
ALLOWED_JOBS = {
    ('hub-control.yml', 'publish'): 'build-<sha>-<run>-<attempt>, write-once sha-<commit>, forward-only latest',
    ('hub-promote.yml', 'promote'): 'latest (manual promotion and rollback)',
    ('hub-executor-publication.yml', 'publication'): 'staging-executor-* via scripts/publish_hub_executor.py',
}
ALLOWED_SCRIPTS = {'scripts/publish_hub_executor.py'}
LATEST_WRITE = 'imagetools create --prefer-index=false --tag "$PACKAGE:latest"'
# (workflow, job, step name, step condition) of the only two `latest` writers.
LATEST_WRITERS = {
    ('hub-control.yml', 'publish', 'Move latest forward to the tested digest', "${{ github.event_name == 'push' }}"),
    ('hub-promote.yml', 'promote', 'Move latest to the tested digest', ''),
}


def _workflows():
    for path in sorted(WORKFLOWS.glob('*.y*ml')):
        yield path.name, yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def _job_writes(job):
    writes = []
    for step in job.get('steps', []):
        run = step.get('run', '')
        if WRITE.search(run) or 'publish_hub_executor.py' in run:
            writes.append(run)
        uses = step.get('uses', '')
        if 'build-push-action' in uses and step.get('with', {}).get('push', 'false') != 'false':
            writes.append(f'{uses} push={step["with"]["push"]}')
    return writes


def test_every_workflow_registry_writer_is_known():
    found = set()
    for name, config in _workflows():
        for job_id, job in config.get('jobs', {}).items():
            if _job_writes(job):
                found.add((name, job_id))
    unknown = found - set(ALLOWED_JOBS)
    assert not unknown, f'unreviewed registry writer(s): {sorted(unknown)}'
    assert set(ALLOWED_JOBS) <= found, 'inventory lists a writer that no longer exists; update it'


def _writes_latest(run):
    return bool(re.search(r'--tag\s+"?\$PACKAGE:latest|engineering-hub-control:latest', run)) or (
        'imagetools create' in run and ':latest' in run.split('imagetools create', 1)[1].split('\n', 1)[0])


def test_exactly_two_latest_writers_and_only_publish_writes_sha_tags():
    found = set()
    for name, config in _workflows():
        for job_id, job in config.get('jobs', {}).items():
            for step in job.get('steps', []):
                run = step.get('run', '')
                if not _job_writes({'steps': [step]}):
                    continue
                if _writes_latest(run):
                    found.add((name, job_id, step.get('name'), step.get('if', '')))
                    assert run.count(LATEST_WRITE) == 1, (name, job_id)
                writes_sha = '$SHA_TAG' in run or re.search(r':sha-', run.split('imagetools create', 1)[-1]) is not None
                if writes_sha and 'imagetools create' in run:
                    assert (name, job_id) == ('hub-control.yml', 'publish'), (name, job_id)
                    assert not _writes_latest(run), 'the sha tag and latest are written by separate steps'
    assert found == LATEST_WRITERS, f'latest writers changed: {sorted(found)}'
    for name, _config in _workflows():
        text = (WORKFLOWS / name).read_text()
        expected = 1 if name in {'hub-control.yml', 'hub-promote.yml'} else 0
        assert text.count(LATEST_WRITE) == expected, name
        if not expected:
            assert not re.search(r'--tag\s+"?\$PACKAGE:latest', text), name


def test_latest_writer_and_sha_writer_are_serialized():
    promote = yaml.load((WORKFLOWS / 'hub-promote.yml').read_text(), Loader=yaml.BaseLoader)
    assert promote['concurrency'] == {'group': 'hub-promote-latest', 'cancel-in-progress': 'false'}
    control = yaml.load((WORKFLOWS / 'hub-control.yml').read_text(), Loader=yaml.BaseLoader)
    assert control['concurrency']['cancel-in-progress'] == 'false'
    assert control['concurrency']['group'] == 'hub-control-${{ github.ref }}'
    executor = yaml.load((WORKFLOWS / 'hub-executor-publication.yml').read_text(), Loader=yaml.BaseLoader)
    assert executor['concurrency']['group'] == 'hub-executor-publication'


def test_no_script_writes_registry_tags_except_the_reviewed_executor_publisher():
    writers = set()
    for path in ROOT.rglob('*'):
        if not path.is_file() or path.suffix not in {'.py', '.sh', '.mjs', '.js', '.ts', '.ps1'}:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(('tests/', 'hub-ui/node_modules/', '.git/', 'node_modules/')) or '/node_modules/' in rel:
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        if WRITE.search(text):
            writers.add(rel)
    assert writers == ALLOWED_SCRIPTS, f'unreviewed script registry writer(s): {sorted(writers - ALLOWED_SCRIPTS)}'
    executor = (ROOT / 'scripts/publish_hub_executor.py').read_text(encoding='utf-8')
    assert "PACKAGE + ':staging-executor-' + suffix" in executor
    assert ':latest' not in executor and ':sha-' not in executor
