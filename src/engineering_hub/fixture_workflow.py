"""Bounded fixture-only orchestration. No application or shell deployment driver.

Operator manifests and controller identity are constructor dependencies, never
agent input. Every command is durably reserved before I/O. Unknown delivery is
held, including after a crash; inspect-only recovery never repeats an effect.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import uuid

from .executor_client import CommandRequest
from .repository import HubError


@dataclass(frozen=True)
class FixtureTarget:
    target_id: str
    artifact_digest: str
    candidate_commit: str
    rollback_digest: str
    rollback_commit: str
    policy_revision: int = 1
    driver: str = "fixture"

    def __post_init__(self):
        if self.driver != "fixture" or not self.target_id.endswith(":staging"):
            raise ValueError("fixture_target_required")
        for digest, commit in ((self.artifact_digest, self.candidate_commit),
                               (self.rollback_digest, self.rollback_commit)):
            CommandRequest(command_id=str(uuid.uuid4()), target_id=self.target_id,
                           action="deploy_fixture", artifact_digest=digest,
                           candidate_commit=commit, policy_revision=self.policy_revision,
                           fencing_generation=1)


class FixtureWorkflow:
    def __init__(self, repo, client, targets):
        if repo.execution_principal is None:
            raise ValueError("execution_identity_required")
        self.repo, self.client = repo, client
        self.targets = {target.target_id: target for target in targets}
        if len(self.targets) != len(targets):
            raise ValueError("duplicate_target")
        with repo._connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS hub_fixture_commands (
                run_id TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, PRIMARY KEY(run_id, action))''')
            db.execute('''CREATE TABLE IF NOT EXISTS hub_fixture_manifests (
                run_id TEXT PRIMARY KEY, manifest TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS hub_fixture_advances (
                run_id TEXT PRIMARY KEY, owner TEXT NOT NULL)''')

    def _target(self, run):
        target = self.targets.get(run['target'])
        if (target is None or target.artifact_digest != run['artifact_digest']
                or target.candidate_commit != run['commit']
                or target.policy_revision != run['policy_revision']
                or run['execution_actor'] != self.repo.execution_principal):
            raise HubError("fixture_manifest_mismatch", 403)
        bound = run.get('execution_manifest_digest')
        if bound is not None and bound != self._manifest_digest(target):
            raise HubError('fixture_manifest_changed', 409)
        return target

    @staticmethod
    def _manifest_digest(target):
        canonical = json.dumps(asdict(target), sort_keys=True, separators=(',', ':'))
        return 'sha256:' + hashlib.sha256(canonical.encode()).hexdigest()

    def create_run(self, actor, task_id, expected_revision, artifact_digest, target_id, *,
                   client_request_id=None):
        task = self.repo.get_task(task_id)
        target = self.targets.get(target_id)
        if (target is None or target_id != task['project'] + ':staging'
                or artifact_digest != target.artifact_digest
                or task['candidate_commit'] != target.candidate_commit
                or target.policy_revision != 1):
            raise HubError('fixture_manifest_mismatch', 403)
        return self.repo.create_run(actor, task_id, expected_revision, artifact_digest, target_id,
                                    execution_manifest_digest=self._manifest_digest(target),
                                    client_request_id=client_request_id)

    def _event(self, run, event):
        # Run execution evidence uses one stable run identity. The individual
        # adapter action IDs remain separately durable in the command ledger.
        return self.repo.append_run_event(self.repo.execution_principal, run['id'], event,
                                         run['id'], run['fencing_generation'], run['revision'])

    def _pin_manifest(self, run, target):
        manifest = json.dumps(asdict(target), sort_keys=True)
        with self.repo._transaction() as db:
            db.execute('INSERT OR IGNORE INTO hub_fixture_manifests VALUES(?,?)',
                       (run['id'], manifest))
            saved = db.execute('SELECT manifest FROM hub_fixture_manifests WHERE run_id=?',
                               (run['id'],)).fetchone()[0]
            if saved != manifest:
                raise HubError('fixture_manifest_changed', 409)

    def _hold(self, run):
        if run['status'] in {'pending', 'running'}:
            return self._event(run, 'held')
        return run  # Failed rollback stays reserved, never falsely rolled_back.

    async def _command(self, run, target, action):
        rollback = action == 'rollback_fixture'
        payload = dict(command_id=str(uuid.uuid5(uuid.UUID(run['id']), action)),
                       target_id=run['target'], action=action,
                       artifact_digest=target.rollback_digest if rollback else run['artifact_digest'],
                       candidate_commit=target.rollback_commit if rollback else run['commit'],
                       policy_revision=run['policy_revision'],
                       fencing_generation=run['fencing_generation'])
        with self.repo._transaction() as db:
            row = db.execute('SELECT * FROM hub_fixture_commands WHERE run_id=? AND action=?',
                             (run['id'], action)).fetchone()
            if row:
                if json.loads(row['payload']) != payload:
                    raise HubError('fixture_manifest_changed', 409)
                if row['state'] in {'succeeded', 'failed'}:
                    return row['state']
                # No competing call, restart or retry can issue another POST.
                return 'held'
            db.execute('INSERT INTO hub_fixture_commands VALUES(?,?,?,?)',
                       (run['id'], action, json.dumps(payload, sort_keys=True), 'unknown'))
        try:
            response = await self.client.submit_command(CommandRequest(**payload))
            state = response.state
        except Exception:
            # No exception strings, endpoint URLs or credentials in public state.
            state = 'held'
        state = state if state in {'succeeded', 'failed'} else 'held'
        with self.repo._transaction() as db:
            db.execute('UPDATE hub_fixture_commands SET state=? WHERE run_id=? AND action=?',
                       (state, run['id'], action))
        return state

    async def advance(self, run_id):
        """One bounded deploy/smoke/rollback attempt; no polling or model calls."""
        owner = str(uuid.uuid4())
        with self.repo._transaction() as db:
            prior = db.execute('SELECT 1 FROM hub_fixture_advances WHERE run_id=?',
                               (run_id,)).fetchone()
            if prior:
                # Cannot distinguish an active call from a crashed controller.
                # Neither automatic expiry nor another caller grants takeover.
                raise HubError('advance_requires_reconciliation', 409)
            db.execute('INSERT INTO hub_fixture_advances VALUES(?,?)', (run_id, owner))
        try:
            return await self._advance_owned(run_id)
        finally:
            with self.repo._transaction() as db:
                db.execute('DELETE FROM hub_fixture_advances WHERE run_id=? AND owner=?',
                           (run_id, owner))

    async def _advance_owned(self, run_id):
        run = self.repo.get_run(run_id)
        target = self._target(run)
        self._pin_manifest(run, target)
        if run['status'] in {'succeeded', 'rolled_back', 'held'}:
            return run
        if self.repo.get_paused():
            raise HubError('paused', 423)
        if run['status'] == 'pending':
            run = self._event(run, 'running')
        if run['status'] == 'running':
            for action in ('deploy_fixture', 'smoke_fixture'):
                if self.repo.get_paused():
                    return self._hold(run)
                state = await self._command(run, target, action)
                if state == 'held':
                    return self._hold(run)
                if state == 'failed':
                    run = self._event(run, 'failed')
                    break
            else:
                return self._event(run, 'succeeded')
        if run['status'] == 'failed':
            if self.repo.get_paused():
                return run
            state = await self._command(run, target, 'rollback_fixture')
            if state == 'succeeded':
                return self._event(run, 'rolled_back')
        return run

    async def inspect(self, run_id, action):
        """Read exact command evidence only. Never release a held reservation."""
        run = self.repo.get_run(run_id)
        self._target(run)
        with self.repo._connection() as db:
            row = db.execute('SELECT payload FROM hub_fixture_commands WHERE run_id=? AND action=?',
                             (run_id, action)).fetchone()
        if row is None:
            raise HubError('command_not_found', 404)
        return await self.client.get_command(CommandRequest(**json.loads(row['payload'])))
