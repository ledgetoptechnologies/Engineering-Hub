"""Derive local test resource names from one genuinely admitted native run.

This is a test fixture, not execution authority or staging enrollment. The
broker and volume independently validate and journal effects; this snapshot
alone cannot grant access or prove current host custody.
"""
from dataclasses import dataclass
import uuid

from engineering_hub.container_pilot_client import ContainerPilotRequest
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.queued_pilot import QueuedContainerPilot


@dataclass(frozen=True)
class NativeTestBinding:
    run_id: str
    fence: int
    baseline_name: str
    candidate_name: str
    rollback_name: str
    candidate_generation: str
    rollback_generation: str


class LocalNativeBinding:
    """One admission-scoped derivation; no daemon or filesystem access."""

    def __init__(self, *, bridge, manifest, owner):
        if type(bridge) is not QueuedContainerPilot or type(manifest) is not PilotManifest:
            raise ValueError('exact native queue and manifest required')
        if type(owner) is not str or str(uuid.UUID(owner)) != owner:
            raise ValueError('canonical test owner required')
        prefix = 'hub-pilot-' + uuid.UUID(owner).hex[:24]
        if (manifest.runtime.resource_name_prefix != prefix or
                bridge.target.manifest_digest != manifest.digest()):
            raise ValueError('test manifest binding mismatch')
        self._bridge, self._manifest, self._prefix = bridge, manifest, prefix
        self._used = False

    def bind_first_deploy(self, command):
        if self._used:
            raise ValueError('test binding already attempted')
        self._used = True
        if type(command) is not ContainerPilotRequest or command.action != 'deploy_container':
            raise ValueError('first native deploy command required')
        run_uuid = uuid.UUID(command.run_id)
        if (str(run_uuid) != command.run_id or command.manifest_digest != self._manifest.digest() or
                command.target_id != self._manifest.target_id or
                command.candidate_commit != self._manifest.candidate.source_commit or
                command.policy_revision != self._manifest.policy_revision or
                command.command_id != str(uuid.uuid5(run_uuid, 'container_pilot_v1:deploy_container'))):
            raise ValueError('native command binding mismatch')
        # The repository gate checks the actual live native execute scope,
        # current owner/claim/fence and mapped run. This read is not a durable
        # grant; later broker and adapter checks still apply.
        repo = self._bridge.repo
        with repo._connection() as db:
            db.execute('BEGIN')
            try:
                if getattr(repo, '_queued_pilot', None) is not self._bridge or not repo.queued_pilot_enrolled(db):
                    raise ValueError('native queue enrollment unavailable')
                repo._assert_queued_pilot('command', db=db, run_id=command.run_id, action=command.action)
                rows = db.execute('SELECT j.fence,j.state FROM hub_queued_pilot_jobs q '
                                  'JOIN hub_work_jobs j ON j.id=q.job_id WHERE q.run_id=? AND q.state=?',
                                  (command.run_id, 'mapped')).fetchall()
                run = repo._get(db, command.run_id, 'run')
            finally:
                db.rollback()
        if (len(rows) != 1 or rows[0]['fence'] != command.fencing_generation or rows[0]['state'] != 'claimed' or
                run.get('fencing_generation') != command.fencing_generation or
                run.get('target') != command.target_id):
            raise ValueError('native run changed')
        def generation(role, action, operation_kind):
            command_id = uuid.uuid5(run_uuid, 'container_pilot_v1:' + action)
            operation = uuid.uuid5(command_id, operation_kind)
            material = self._manifest.digest() + '/' + command.run_id + '/' + str(operation) + '/' + role
            return role + '-' + uuid.uuid5(uuid.NAMESPACE_URL, material).hex
        stem = self._prefix + '-' + command.run_id[:12]
        if command.run_id[:12] == '00000000-000':
            raise ValueError('reserved baseline name collision')
        return NativeTestBinding(command.run_id, command.fencing_generation,
            self._prefix + '-00000000-000-rollback', stem + '-candidate', stem + '-rollback',
            generation('candidate', 'deploy_container', 'state_copy'),
            generation('rollback', 'rollback_container', 'rollback_state'))
