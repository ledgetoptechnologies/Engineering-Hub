"""Explicit container-pilot controller; never a Docker client or default worker.

The operator supplies a binding to a complete host-owned canonical manifest.
The manifest digest is not an agent approval or proof of image provenance. Host
acceptance must independently validate that manifest and actual Docker effects.
Fixture actions, manifests and ledgers are deliberately not reused here.
"""
from dataclasses import asdict, dataclass, field
import json
import re
import sqlite3
import uuid
from pathlib import Path

from .container_pilot_client import ContainerPilotClient, ContainerPilotRequest, ContainerPilotResponse
from .container_pilot_manifest import PilotManifest
from .repository import HubError
from .pilot_ownership import PilotOwner


@dataclass(frozen=True)
class ContainerPilotTarget:
    """Immutable controller view of an operator-selected complete host manifest.

    Image IDs are Docker config IDs, not registry manifest digests. This object
    does not construct the full host manifest or authorize an arbitrary daemon.
    """
    artifact_digest: str
    candidate_commit: str
    manifest_digest: str
    rollback_image_config_id: str
    rollback_commit: str
    target_id: str = 'engineering-hub:staging'
    policy_revision: int = 1
    driver: str = 'container_pilot_v1'
    canonical_manifest: str | None = field(default=None, repr=False)

    @classmethod
    def from_manifest(cls, manifest):
        if type(manifest) is not PilotManifest:
            raise ValueError('validated_pilot_manifest_required')
        canonical = manifest.canonical_bytes()
        verified = PilotManifest.from_bytes(canonical)
        return cls(verified.candidate.image_config_id, verified.candidate.source_commit,
                   verified.digest(), verified.rollback.image_config_id,
                   verified.rollback.source_commit, target_id=verified.target_id,
                   policy_revision=verified.policy_revision,
                   canonical_manifest=canonical.decode('utf-8'))

    def validated_manifest(self):
        if not isinstance(self.canonical_manifest, str):
            raise HubError('container_pilot_manifest_unverified', 403)
        try:
            manifest = PilotManifest.from_bytes(self.canonical_manifest.encode('utf-8'))
            if manifest.canonical_bytes().decode('utf-8') != self.canonical_manifest:
                raise ValueError('canonical_pilot_manifest_required')
            matches = (manifest.digest() == self.manifest_digest
                       and manifest.candidate.image_config_id == self.artifact_digest
                       and manifest.candidate.source_commit == self.candidate_commit
                       and manifest.rollback.image_config_id == self.rollback_image_config_id
                       and manifest.rollback.source_commit == self.rollback_commit
                       and manifest.target_id == self.target_id
                       and manifest.policy_revision == self.policy_revision
                       and manifest.driver.id == self.driver)
        except Exception:
            raise HubError('container_pilot_manifest_unverified', 403) from None
        if not matches:
            raise HubError('container_pilot_manifest_changed', 409)
        return manifest

    def __post_init__(self):
        if self.driver != 'container_pilot_v1' or self.policy_revision != 1:
            raise ValueError('container_pilot_binding_required')
        if type(self.policy_revision) is not int:
            raise ValueError('container_pilot_binding_required')
        for digest in (self.artifact_digest, self.manifest_digest, self.rollback_image_config_id):
            if not isinstance(digest, str) or re.fullmatch(r'sha256:[0-9a-f]{64}', digest) is None:
                raise ValueError('invalid_pilot_digest')
        if self.artifact_digest == self.rollback_image_config_id:
            raise ValueError('distinct_pilot_images_required')
        for commit in (self.candidate_commit, self.rollback_commit):
            if not isinstance(commit, str) or re.fullmatch(r'[0-9a-f]{40}', commit) is None:
                raise ValueError('invalid_pilot_commit')
        ContainerPilotRequest(schema_version='container_pilot_v1', command_id=str(uuid.uuid4()),
                              run_id=str(uuid.uuid4()), target_id=self.target_id,
                              action='deploy_container', manifest_digest=self.manifest_digest,
                              candidate_commit=self.candidate_commit, policy_revision=1,
                              fencing_generation=1)


class ContainerPilotWorkflow:
    @staticmethod
    def _validate_client_deadline(client, manifest):
        # A synchronous host response must fit within the client's complete
        # deadline. Reject before reserving a run, rather than predictably
        # timing out after a possible effect. Injected adapters must declare
        # this capability too; no optimistic default is inferred.
        deadline = getattr(client, 'timeout_seconds', None)
        required = manifest.timeouts.total_command_ms / 1000 + ContainerPilotClient.TRANSPORT_MARGIN_SECONDS
        if (type(deadline) not in {int, float}
                or not required <= deadline <= ContainerPilotClient.MAX_TIMEOUT_SECONDS):
            raise HubError('container_pilot_client_deadline_incompatible', 403)

    @staticmethod
    def _manifest_digest(target):
        # Recovery fingerprints the original request using the full operator
        # manifest digest, not a hash of this narrower controller view.
        return target.manifest_digest

    def _assert_pilot_owner(self):
        """Check live authority, including late enrollment of legacy instances.

        A saved epoch, Boolean or duck-typed object is never a capability. This
        guard does not adopt stale workflow rows or authorize host effects.
        """
        try:
            if self.pilot_owner is not None:
                if type(self.pilot_owner) is not PilotOwner:
                    raise ValueError()
                self.pilot_owner.assert_active(self.repo.path)
                return
            # Read-only mode cannot recreate a deleted/replaced database.
            db = sqlite3.connect(Path(self.repo.path).absolute().as_uri() + '?mode=ro', uri=True)
            try:
                enrolled = db.execute("SELECT 1 FROM sqlite_master WHERE name='hub_pilot_owner'").fetchone()
            finally:
                db.close()
            if enrolled:
                raise ValueError()
        except Exception:
            raise HubError('container_pilot_ownership_unavailable', 409) from None

    def __init__(self, repo, client, targets, *, pilot_owner=None):
        if repo.execution_principal is None:
            raise ValueError('execution_identity_required')
        if not isinstance(targets, (list, tuple)) or any(type(t) is not ContainerPilotTarget for t in targets):
            raise ValueError('invalid_pilot_targets')
        for target in targets:
            self._validate_client_deadline(client, target.validated_manifest())
        self.repo, self.client = repo, client
        self.pilot_owner = pilot_owner
        self._assert_pilot_owner()
        self.targets = {target.target_id: target for target in targets}
        if len(self.targets) != len(targets) or any(not isinstance(t, ContainerPilotTarget) for t in targets):
            raise ValueError('invalid_pilot_targets')
        with repo._connection() as db:
            self._assert_pilot_owner()
            db.executescript('''
                CREATE TABLE IF NOT EXISTS hub_container_pilot_commands (
                    run_id TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, response_json TEXT, PRIMARY KEY(run_id,action));
                CREATE TABLE IF NOT EXISTS hub_container_pilot_bindings (
                    run_id TEXT PRIMARY KEY, binding TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_container_pilot_advances (
                    run_id TEXT PRIMARY KEY, owner TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_container_pilot_manifest_views (
                    manifest_digest TEXT PRIMARY KEY, binding TEXT NOT NULL);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(hub_container_pilot_commands)')}
            if 'response_json' not in columns:
                # Preserve legacy unknown/held reservations; never reconstruct
                # missing evidence or resend because this column was absent.
                self._assert_pilot_owner()
                db.execute('ALTER TABLE hub_container_pilot_commands ADD COLUMN response_json TEXT')
        # Pin the operator's manifest interpretation before run creation, not
        # only before the first effect. A restart must not reinterpret the same
        # manifest digest as a different rollback image or source provenance.
        with repo._transaction() as db:
            for target in targets:
                self._assert_pilot_owner()
                binding = json.dumps(asdict(target), sort_keys=True, separators=(',', ':'))
                db.execute('INSERT OR IGNORE INTO hub_container_pilot_manifest_views VALUES(?,?)', (target.manifest_digest, binding))
                prior = db.execute('SELECT binding FROM hub_container_pilot_manifest_views WHERE manifest_digest=?', (target.manifest_digest,)).fetchone()[0]
                if prior != binding:
                    raise HubError('container_pilot_manifest_changed', 409)

    def create_run(self, actor, task_id, expected_revision, artifact_digest, target_id, *, client_request_id=None):
        self._assert_pilot_owner()
        task = self.repo.get_task(task_id)
        target = self.targets.get(target_id)
        if target is not None:
            self._validate_client_deadline(self.client, target.validated_manifest())
        if (target is None or task['project'] != 'engineering-hub'
                or artifact_digest != target.artifact_digest
                or task['candidate_commit'] != target.candidate_commit):
            raise HubError('container_pilot_manifest_mismatch', 403)
        self._assert_pilot_owner()
        return self.repo.create_run(actor, task_id, expected_revision, artifact_digest, target_id,
                                    execution_manifest_digest=target.manifest_digest,
                                    client_request_id=client_request_id)

    def _target(self, run, *, pin=True):
        target = self.targets.get(run['target'])
        if target is not None:
            self._validate_client_deadline(self.client, target.validated_manifest())
        if (target is None or run['artifact_digest'] != target.artifact_digest
                or run['commit'] != target.candidate_commit
                or run['policy_revision'] != target.policy_revision
                or run['execution_actor'] != self.repo.execution_principal
                or run.get('execution_manifest_digest') != target.manifest_digest):
            raise HubError('container_pilot_manifest_mismatch', 403)
        canonical = json.dumps(asdict(target), sort_keys=True, separators=(',', ':'))
        if not pin:
            with self.repo._connection() as db:
                prior = db.execute('SELECT binding FROM hub_container_pilot_bindings WHERE run_id=?', (run['id'],)).fetchone()
            if prior is not None and prior[0] != canonical:
                raise HubError('container_pilot_manifest_changed', 409)
            return target
        with self.repo._transaction() as db:
            self._assert_pilot_owner()
            db.execute('INSERT OR IGNORE INTO hub_container_pilot_bindings VALUES(?,?)', (run['id'], canonical))
            prior = db.execute('SELECT binding FROM hub_container_pilot_bindings WHERE run_id=?', (run['id'],)).fetchone()[0]
            if prior != canonical:
                raise HubError('container_pilot_manifest_changed', 409)
        return target

    def _event(self, run, event, *, phase, reason=None):
        self._assert_pilot_owner()
        if phase not in {'deploy_container', 'smoke_container', 'rollback_container', 'smoke_container_rollback'}:
            raise ValueError('invalid_pilot_phase')
        event_id = str(uuid.uuid5(uuid.UUID(run['id']), 'container_pilot_v1:event:' + phase + ':' + event))
        return self.repo.append_run_event(self.repo.execution_principal, run['id'], event,
                                         run['id'], run['fencing_generation'], run['revision'],
                                         reason=reason, event_id=event_id)

    def _hold(self, run, reason, *, phase):
        # A failed candidate is not a terminal recovery result. Failed or
        # uncertain rollback must visibly hold the same target reservation,
        # rather than leaving an ordinary candidate-failure label behind.
        return self._event(run, 'held', phase=phase, reason=reason) if run['status'] in {'pending', 'running', 'failed'} else run

    @staticmethod
    def _validated_response(response):
        # Frozen Pydantic objects can still be created via model_construct or
        # copied with unvalidated updates. Revalidate at this injection boundary
        # before examining booleans/echo fields or storing any asserted receipt.
        if type(response) is not ContainerPilotResponse:
            raise ValueError('invalid_pilot_response')
        # The integer JSON serializer can normalize True to 1 even under
        # warnings='error', so strict-check the original Python values first.
        verified = ContainerPilotResponse.model_validate(response.model_dump(warnings='error'), strict=True)
        payload = verified.model_dump_json(warnings='error')
        if len(payload.encode('utf-8')) > ContainerPilotClient.RESPONSE_BYTE_LIMIT:
            raise ValueError('invalid_pilot_response')
        return ContainerPilotResponse.model_validate_json(payload, strict=True)

    @staticmethod
    def _verified_state(response, command, target):
        # Typed client verifies echo; retain that boundary even for an injected
        # adapter and do not equate a transport success with actual app health.
        if type(response) is not ContainerPilotResponse or any(
                getattr(response, key, None) != value for key, value in command.model_dump().items()):
            return 'held'
        state = response.state
        if state not in {'succeeded', 'failed'}:
            return 'held'
        if state == 'failed':
            return 'failed'
        evidence = response.evidence
        rollback = command.action in {'rollback_container', 'smoke_container_rollback'}
        expected = target.rollback_image_config_id if rollback else target.artifact_digest
        if (not evidence.evidence_verified or evidence.image_config_id != expected
                or not evidence.container_id):
            return 'held'
        if command.action in {'smoke_container', 'smoke_container_rollback'} and evidence.health != 'healthy':
            return 'held'
        return 'succeeded'

    @staticmethod
    def _request(run, target, action):
        return ContainerPilotRequest(
            schema_version='container_pilot_v1', command_id=str(uuid.uuid5(uuid.UUID(run['id']), 'container_pilot_v1:' + action)),
            run_id=run['id'], target_id=run['target'], action=action,
            manifest_digest=target.manifest_digest, candidate_commit=target.candidate_commit,
            policy_revision=target.policy_revision, fencing_generation=run['fencing_generation'])

    async def _command(self, run, target, action):
        self._assert_pilot_owner()
        command = self._request(run, target, action)
        canonical = json.dumps(command.model_dump(), sort_keys=True, separators=(',', ':'))
        with self.repo._transaction() as db:
            self._assert_pilot_owner()
            row = db.execute('SELECT payload,state FROM hub_container_pilot_commands WHERE run_id=? AND action=?', (run['id'], action)).fetchone()
            if row:
                if row['payload'] != canonical:
                    raise HubError('container_pilot_manifest_changed', 409)
                return row['state'] if row['state'] in {'succeeded', 'failed'} else 'held'
            db.execute('INSERT INTO hub_container_pilot_commands(run_id,action,payload,state) VALUES(?,?,?,?)',
                       (run['id'], action, canonical, 'unknown'))
        response_json = None
        self._assert_pilot_owner()
        try:
            response = await self.client.submit_command(command)
            response = self._validated_response(response)
            state = self._verified_state(response, command, target)
            if type(response) is ContainerPilotResponse:
                # Typed, bounded adapter assertion, not independent Docker proof.
                # Retain exact evidence for audit even when it causes a hold.
                response_json = response.model_dump_json()
        except Exception:
            state = 'held'  # No endpoint, credential or exception output retained.
        self._assert_pilot_owner()
        with self.repo._transaction() as db:
            self._assert_pilot_owner()
            db.execute('UPDATE hub_container_pilot_commands SET state=?,response_json=? WHERE run_id=? AND action=?',
                       (state, response_json, run['id'], action))
        return state

    async def advance(self, run_id):
        self._assert_pilot_owner()
        owner = str(uuid.uuid4())
        with self.repo._transaction() as db:
            self._assert_pilot_owner()
            if db.execute('SELECT 1 FROM hub_container_pilot_advances WHERE run_id=?', (run_id,)).fetchone():
                raise HubError('advance_requires_reconciliation', 409)
            db.execute('INSERT INTO hub_container_pilot_advances VALUES(?,?)', (run_id, owner))
        try:
            run = self.repo.get_run(run_id)
            target = self._target(run)
            if run['status'] in {'succeeded', 'rolled_back', 'held'}:
                return run
            if self.repo.get_paused():
                raise HubError('paused', 423)
            if run['status'] == 'pending':
                run = self._event(run, 'running', phase='deploy_container')
            if run['status'] == 'running':
                for action in ('deploy_container', 'smoke_container'):
                    if self.repo.get_paused():
                        # Pause before the next effect is resumable. It is not
                        # evidence of an unknown effect requiring a hold.
                        raise HubError('paused', 423)
                    state = await self._command(run, target, action)
                    if state == 'held':
                        return self._hold(run, 'candidate_outcome_uncertain', phase=action)
                    if state == 'failed':
                        run = self._event(run, 'failed', phase=action)
                        break
                else:
                    return self._event(run, 'succeeded', phase='smoke_container')
            if run['status'] == 'failed':
                for action in ('rollback_container', 'smoke_container_rollback'):
                    if self.repo.get_paused():
                        raise HubError('paused', 423)
                    state = await self._command(run, target, action)
                    if state != 'succeeded':
                        phase = 'rollback' if action == 'rollback_container' else 'rollback_smoke'
                        reason = phase + ('_failed' if state == 'failed' else '_outcome_uncertain')
                        return self._hold(run, reason, phase=action)
                return self._event(run, 'rolled_back', phase='smoke_container_rollback')
            return run
        finally:
            # Lost ownership preserves the reservation for evidence-only
            # recovery. Never clear it using a stale process capability.
            self._assert_pilot_owner()
            with self.repo._transaction() as db:
                self._assert_pilot_owner()
                db.execute('DELETE FROM hub_container_pilot_advances WHERE run_id=? AND owner=?', (run_id, owner))

    async def inspect(self, run_id, action):
        self._assert_pilot_owner()
        run = self.repo.get_run(run_id)
        target = self._target(run, pin=False)
        with self.repo._connection() as db:
            row = db.execute('SELECT payload FROM hub_container_pilot_commands WHERE run_id=? AND action=?', (run_id, action)).fetchone()
        if row is None:
            raise HubError('command_not_found', 404)
        command = self._request(run, target, action)
        canonical = json.dumps(command.model_dump(), sort_keys=True, separators=(',', ':'))
        if row['payload'] != canonical:
            raise HubError('container_pilot_manifest_changed', 409)
        # No automatic reconcile, effect replay, release or takeover.
        self._assert_pilot_owner()
        response = await self.client.get_command(command)
        self._assert_pilot_owner()
        try:
            response = self._validated_response(response)
            if any(getattr(response, key) != value for key, value in command.model_dump().items()):
                raise ValueError()
            return response
        except Exception:
            raise HubError('container_pilot_inspection_unverified', 409) from None
