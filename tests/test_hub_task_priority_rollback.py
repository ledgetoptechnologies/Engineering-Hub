"""Exercise one private SQLite ledger across the exact rollback source and this build."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile

import pytest
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub import app as hub_app
from engineering_hub.auth import AuthConfig


ROLLBACK = '60263aafaec66d0688f6c14431874e36e4884b3a'
BASE = '/api/hub/v1'
ORIGIN = 'https://rollback-fixture.invalid'
BEARER = {'Authorization': 'Bearer rollback-codex'}
NEW = {'project': 'engineering-hub', 'title': 'Created by feature', 'client_request_id': 'feature-create',
       'priority': 'high'}
EDIT = {'priority': 'urgent', 'expected_revision': 1, 'expected_priority_revision': 0,
        'client_request_id': 'feature-edit'}


def _canonical_row(database, task_id):
    with closing(sqlite3.connect(database)) as db:
        return db.execute('SELECT data FROM hub_entities WHERE kind=? AND id=?', ('task', task_id)).fetchone()[0]


def _task_views(client, task):
    path = f"{BASE}/tasks/{task['id']}"
    detail = client.get(path, headers=BEARER)
    assert detail.status_code == 200, detail.text
    assert detail.json() == task
    for route, key in ((BASE + '/tasks', 'items'), (BASE + '/overview', 'tasks')):
        response = client.get(route, headers=BEARER)
        assert response.status_code == 200, response.text
        row = next(item for item in response.json()[key] if item['id'] == task['id'])
        assert row == {**task, 'state_since': row['state_since']}


def _run_phase(phase, directory):
    database = directory / 'hub.sqlite'
    evidence_path = directory / 'evidence.json'
    evidence = json.loads(evidence_path.read_text()) if evidence_path.exists() else {}
    settings = hub_app.Settings(database, AuthSettings('rollback-hermes', 'rollback-codex'),
                                AuthConfig(ORIGIN, beau_github_id=42))
    app = hub_app.create_app(settings)
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        if phase == 'baseline_seed':
            create = client.post(BASE + '/tasks', headers=BEARER, json={
                'project': 'engineering-hub', 'title': 'Created by rollback build',
                'client_request_id': 'baseline-create', 'assignee': 'codex'})
            assert create.status_code == 201, create.text
            evidence['legacy'] = create.json()
        else:
            legacy = evidence['legacy']
            task_id = legacy['id']
            base_path = f'{BASE}/tasks/{task_id}'
            priority_path = base_path + '/priority'
            session = app.state.sessions.create_session(42)
            client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
            csrf = {'Origin': ORIGIN, 'X-Hub-CSRF': session['csrf_token']}

            if phase == 'feature_write':
                _task_views(client, legacy)
                unchanged_bytes = _canonical_row(database, task_id)
                edit = client.post(priority_path, headers=csrf, json=EDIT)
                assert edit.status_code == 200, edit.text
                assert edit.json() == {**legacy, 'priority': 'urgent', 'priority_revision': 1}
                evidence['edit_result'] = edit.json()
                assert _canonical_row(database, task_id) == unchanged_bytes
                new = client.post(BASE + '/tasks', headers=csrf, json=NEW)
                assert new.status_code == 201, new.text
                evidence['prioritized_new'] = new.json()
                assert evidence['prioritized_new']['priority'] == 'high'
                assert 'priority_revision' not in evidence['prioritized_new']

            elif phase == 'baseline_read_write':
                _task_views(client, legacy)
                feature_new = {k: v for k, v in evidence['prioritized_new'].items() if k != 'priority'}
                _task_views(client, feature_new)
                note = client.post(base_path + '/update', headers=BEARER, json={
                    'expected_revision': legacy['revision'], 'client_request_id': 'baseline-edit',
                    'note': 'The rollback writer can still update the canonical task.'})
                assert note.status_code == 200, note.text
                evidence['baseline_edit_result'] = note.json()
                assert evidence['baseline_edit_result']['revision'] == legacy['revision'] + 1
                _task_views(client, evidence['baseline_edit_result'])

            elif phase == 'feature_reupgrade':
                noted = evidence['baseline_edit_result']
                _task_views(client, noted)
                overview = client.get(BASE + '/overview?include_task_priorities=true')
                assert overview.status_code == 200, overview.text
                by_id = {task['id']: task for task in overview.json()['tasks']}
                assert by_id[task_id] == {**noted, 'priority': 'urgent', 'priority_revision': 1,
                                         'state_since': by_id[task_id]['state_since']}
                feature_new = evidence['prioritized_new']
                assert by_id[feature_new['id']] == {**feature_new, 'state_since': by_id[feature_new['id']]['state_since']}
                ranked = client.get(BASE + '/tasks?task_order=priority', headers=BEARER)
                assert ranked.status_code == 200, ranked.text
                assert [(task['id'], task['priority']) for task in ranked.json()['items']] == [
                    (task_id, 'urgent'), (feature_new['id'], 'high')]
                replay = client.post(priority_path, headers=csrf, json=EDIT)
                assert replay.status_code == 200 and replay.json() == evidence['edit_result']
                stale = client.post(priority_path, headers=csrf, json={**EDIT, 'client_request_id': 'next'})
                assert stale.status_code == 409 and stale.json()['detail']['code'] == 'revision_conflict'
                stale = client.post(priority_path, headers=csrf, json={
                    **EDIT, 'client_request_id': 'next', 'expected_revision': noted['revision']})
                assert stale.status_code == 409 and stale.json()['detail']['code'] == 'priority_revision_conflict'
                assert client.post(BASE + '/tasks', headers=csrf, json=NEW).json() == feature_new
                with closing(sqlite3.connect(database)) as db:
                    assert db.execute("SELECT id FROM hub_entities WHERE kind='task_priority' ORDER BY id").fetchall() == [
                        ('task_priority:' + identity,) for identity in sorted(by_id)]
                    for table in ('hub_audit', 'hub_outbox'):
                        events = db.execute(f'SELECT entity_id, revision FROM {table} WHERE action=?',
                                            ('task.priority_updated',)).fetchall()
                        assert events == [(task_id, legacy['revision'])]
            else:
                raise AssertionError(phase)
    evidence_path.write_text(json.dumps(evidence))


def test_priority_writes_survive_real_rollback_and_reupgrade(tmp_path):
    root = Path(__file__).resolve().parents[1]
    feature_src = root / 'src'
    try:
        present = subprocess.run(['git', 'cat-file', '-e', ROLLBACK + '^{commit}'], cwd=root,
                                 capture_output=True, timeout=10)
    except FileNotFoundError:
        return pytest.skip('Git is required for the exact rollback-source fixture')
    if present.returncode != 0:
        pytest.skip(f'Exact rollback commit {ROLLBACK} is not in the local object store')
    archive = subprocess.run(['git', 'archive', ROLLBACK, 'src'], cwd=root, capture_output=True,
                             check=True, timeout=10).stdout
    rollback = tmp_path / 'rollback'
    rollback.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(rollback, filter='data')
    fixture = tmp_path / 'private-ledger'
    fixture.mkdir(mode=0o700)
    for phase, src in (('baseline_seed', rollback / 'src'), ('feature_write', feature_src),
                       ('baseline_read_write', rollback / 'src'), ('feature_reupgrade', feature_src)):
        proc = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), phase, str(fixture), str(src)],
                              cwd=fixture, env={**os.environ, 'PYTHONPATH': str(src)}, capture_output=True,
                              text=True, timeout=30)
        assert proc.returncode == 0, f'{phase}: {proc.stdout}\n{proc.stderr}'


if __name__ == '__main__':
    phase, directory, expected_src = sys.argv[1:]
    assert Path(hub_app.__file__).resolve() == Path(expected_src) / 'engineering_hub' / 'app.py'
    _run_phase(phase, Path(directory))
