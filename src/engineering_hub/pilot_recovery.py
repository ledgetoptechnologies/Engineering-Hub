"""Bounded original-command GET recovery; never an executor POST or a service.

The operator must already hold a live local PilotOwner. Recovery cannot establish
host liveness, source provenance, current application health or user approval.
"""
import json
import uuid
from dataclasses import dataclass

from .container_pilot import ContainerPilotWorkflow
from .repository import AGENTS, HubError


ACTIONS = ('deploy_container', 'smoke_container', 'rollback_container', 'smoke_container_rollback')
RECOVERY_TRANSITIONS = {
    'pending': frozenset({'running', 'held'}),
    'running': frozenset({'running', 'succeeded', 'failed', 'held'}),
    'failed': frozenset({'failed', 'rolled_back', 'held'}),
    'held': frozenset({'running', 'succeeded', 'failed', 'rolled_back', 'held'}),
    'succeeded': frozenset({'succeeded'}),
    'rolled_back': frozenset({'rolled_back'}),
}


@dataclass(frozen=True)
class _Reservation:
    active: tuple | None
    attempt: tuple | None


class PilotRecovery:
    def __init__(self, workflow):
        if type(workflow) is not ContainerPilotWorkflow or workflow.pilot_owner is None:
            raise HubError('pilot_recovery_owner_required', 409)
        self.workflow, self.repo, self.owner = workflow, workflow.repo, workflow.pilot_owner
        self._guard()
        with self.repo._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS hub_container_pilot_recovery_claims (
                    run_id TEXT PRIMARY KEY, occurrence TEXT NOT NULL,
                    epoch TEXT NOT NULL, state TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS hub_container_pilot_recoveries (
                    occurrence TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    state TEXT NOT NULL, evidence TEXT)''')

    def _guard(self):
        self.workflow._assert_pilot_owner()
        if self.workflow.pilot_owner is not self.owner:
            raise HubError('pilot_recovery_owner_required', 409)

    @staticmethod
    def _rows(db, run_id):
        return [dict(row) for row in db.execute(
            'SELECT action,payload,state,response_json FROM hub_container_pilot_commands WHERE run_id=? ORDER BY action', (run_id,))]

    def _reservation(self, db, run_id):
        advance = db.execute('SELECT owner FROM hub_container_pilot_advances WHERE run_id=?', (run_id,)).fetchone()
        epoch = db.execute('SELECT advance_owner,epoch FROM hub_container_pilot_advance_epochs WHERE run_id=?', (run_id,)).fetchone()
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_container_pilot_attempts'").fetchone()
        attempt = (db.execute('SELECT advance_owner,epoch,state FROM hub_container_pilot_attempts WHERE run_id=?',
                              (run_id,)).fetchone() if table else None)
        try:
            if advance is not None or epoch is not None:
                if advance is None or epoch is None or advance['owner'] != epoch['advance_owner']:
                    raise ValueError
                if str(uuid.UUID(advance['owner'])) != advance['owner']:
                    raise ValueError
                # Only the kernel-excluded predecessor can leave an active
                # reservation. A current-epoch coroutine may still be running.
                self.owner.assert_prior_epoch(epoch['epoch'])
                active = (advance['owner'], epoch['advance_owner'], epoch['epoch'])
                if attempt is not None and tuple(attempt) != (advance['owner'], epoch['epoch'], 'active'):
                    raise ValueError
                return _Reservation(active, tuple(attempt) if attempt is not None else None)
            if attempt is None or attempt['state'] != 'quiescent':
                raise ValueError
            if str(uuid.UUID(attempt['advance_owner'])) != attempt['advance_owner']:
                raise ValueError
            if attempt['epoch'] != self.owner.epoch:
                self.owner.assert_prior_epoch(attempt['epoch'])
            return _Reservation(None, tuple(attempt))
        except Exception:
            raise HubError('pilot_recovery_reservation_unverified', 409) from None

    def _authorized(self, db, run):
        if run['status'] not in RECOVERY_TRANSITIONS:
            raise HubError('pilot_recovery_state_unverified', 409)
        if run['command_id'] not in {None, run['id']}:
            raise HubError('command_conflict', 409)
        task = self.repo._get(db, run['task_id'], 'task')
        review = task.get('review')
        if (task['owner'] != run['created_by'] or task['owner'] not in AGENTS
                or task['status'] != 'verified' or task['candidate_commit'] != run['commit']
                or not isinstance(review, dict) or review != run['review']
                or review.get('actor') not in AGENTS or review['actor'] == task['owner']
                or review.get('commit') != run['commit'] or review.get('verdict') != 'approve'):
            raise HubError('pilot_recovery_review_unverified', 409)
        fence = db.execute('SELECT generation FROM hub_target_fences WHERE target=?', (run['target'],)).fetchone()
        if fence is None or fence[0] != run['fencing_generation']:
            raise HubError('stale_fence', 409)

    @staticmethod
    def _derive(evidence):
        """Only contiguous, phase-consistent historical terminal evidence."""
        phases = {item['action']: item for item in evidence}
        if any(item['state'] not in {'succeeded', 'failed'} for item in evidence):
            return 'held'
        deploy, smoke, rollback, rollback_smoke = (phases.get(action) for action in ACTIONS)
        if deploy is None:
            return 'running' if not phases else 'held'
        if deploy['state'] == 'failed':
            if smoke:
                return 'held'
            failed = True
        else:
            if smoke and smoke['state'] == 'succeeded' and smoke['container_id'] != deploy['container_id']:
                return 'held'
            failed = bool(smoke and smoke['state'] == 'failed')
            if not failed:
                if rollback or rollback_smoke:
                    return 'held'
                return 'succeeded' if smoke else 'running'
        if not rollback:
            return 'held' if rollback_smoke else 'failed'
        if rollback['state'] != 'succeeded':
            return 'held'
        if rollback['container_id'] and rollback['container_id'] == deploy['container_id']:
            return 'held'
        if not rollback_smoke:
            return 'failed'
        if rollback_smoke['state'] != 'succeeded' or rollback_smoke['container_id'] != rollback['container_id']:
            return 'held'
        return 'rolled_back'

    async def recover(self, run_id):
        self._guard()
        self.repo._assert_queued_pilot('recover', run_id=run_id)
        occurrence = str(uuid.uuid4())
        try:
            return await self._recover(run_id, occurrence)
        except Exception:
            # An ordinary local validation/CAS failure has unwound this call;
            # it is not an active inspection. Permit a corrected explicit retry
            # without pretending unknown commands were never accepted. Owner
            # loss/cancellation still preserves the in-flight claim for restart.
            try:
                self._guard()
                with self.repo._transaction() as db:
                    changed = db.execute("UPDATE hub_container_pilot_recovery_claims SET state='held' "
                        "WHERE run_id=? AND occurrence=? AND epoch=? AND state='inspecting'",
                        (run_id, occurrence, self.owner.epoch)).rowcount
                    if changed:
                        db.execute("UPDATE hub_container_pilot_recoveries SET state='held',evidence=COALESCE(evidence,'[]') WHERE occurrence=?",
                                   (occurrence,))
            except Exception:
                pass
            raise

    async def _recover(self, run_id, occurrence):
        with self.repo._transaction() as db:
            reservation = self._reservation(db, run_id)
            run = self.repo._get(db, run_id, 'run')
            self._authorized(db, run)
            original_rows = self._rows(db, run_id)
            claim = db.execute('SELECT epoch,state FROM hub_container_pilot_recovery_claims WHERE run_id=?', (run_id,)).fetchone()
            if claim:
                if claim['epoch'] == self.owner.epoch and claim['state'] == 'inspecting':
                    raise HubError('pilot_recovery_in_progress', 409)
                if claim['epoch'] != self.owner.epoch:
                    try:
                        self.owner.assert_prior_epoch(claim['epoch'])
                    except Exception:
                        raise HubError('pilot_recovery_reservation_unverified', 409) from None
            db.execute('INSERT INTO hub_container_pilot_recovery_claims VALUES(?,?,?,?) '
                       'ON CONFLICT(run_id) DO UPDATE SET occurrence=excluded.occurrence,epoch=excluded.epoch,state=excluded.state',
                       (run_id, occurrence, self.owner.epoch, 'inspecting'))
            db.execute('INSERT INTO hub_container_pilot_recoveries VALUES(?,?,?,NULL)', (occurrence, run_id, 'inspecting'))
        target = self.workflow._target(run, pin=False)
        evidence = []
        by_action = {row['action']: row for row in original_rows}
        if any(action not in ACTIONS for action in by_action):
            evidence.append(dict(action='invalid', state='held', response=None, container_id=None))
        for action in ACTIONS:
            if action not in by_action:
                continue
            saved = by_action[action]
            item = dict(action=action, state='held', response=None, container_id=None)
            try:
                # inspect verifies exact deterministic request and bounded echo.
                response = await self.workflow.inspect(run_id, action)
                command = self.workflow._request(run, target, action)
                state = self.workflow._verified_state(response, command, target)
                if saved['state'] in {'succeeded', 'failed'}:
                    prior = self.workflow._validated_response(type(response).model_validate_json(saved['response_json'], strict=True))
                    if (self.workflow._verified_state(prior, command, target) != saved['state']
                            or response.state != prior.state
                            or response.evidence.container_id != prior.evidence.container_id
                            or response.evidence.image_config_id != prior.evidence.image_config_id):
                        state = 'held'
                item.update(state=state, response=response.model_dump_json(), container_id=response.evidence.container_id)
            except Exception:
                # Unknown/404/invalid evidence is never proof of nonacceptance.
                pass
            self._guard()
            evidence.append(item)
        derived = self._derive(evidence)
        if not original_rows and run['status'] not in {'pending', 'running'}:
            derived = 'held'
        # Missing durable phases must not erase already recorded progress or
        # candidate failure. Recovery is not a reset of inconsistent ledgers.
        failed_history = any(event['event'] == 'failed' for event in run['events'])
        candidate_failed = any(item['action'] in ACTIONS[:2] and item['state'] == 'failed' for item in evidence)
        if ((run['status'] == 'pending' and original_rows)
                or (run['status'] == 'failed' and not candidate_failed)
                or (failed_history and not candidate_failed)
                or (any(item['action'] in ACTIONS[2:] for item in evidence) and not failed_history)):
            derived = 'held'
        for event in run['events']:
            required = {'rollback_failed': ACTIONS[2], 'rollback_outcome_uncertain': ACTIONS[2],
                        'rollback_smoke_failed': ACTIONS[3], 'rollback_smoke_outcome_uncertain': ACTIONS[3]}.get(event.get('reason'))
            if required is not None and required not in by_action:
                derived = 'held'
        if derived not in RECOVERY_TRANSITIONS[run['status']]:
            derived = 'held'
        self._guard()
        with self.repo._transaction() as db:
            if self._reservation(db, run_id) != reservation or self._rows(db, run_id) != original_rows:
                raise HubError('pilot_recovery_conflict', 409)
            current = self.repo._get(db, run_id, 'run')
            if current != run:
                raise HubError('pilot_recovery_conflict', 409)
            self._authorized(db, current)
            claim = db.execute('SELECT occurrence,epoch,state FROM hub_container_pilot_recovery_claims WHERE run_id=?', (run_id,)).fetchone()
            if tuple(claim or ()) != (occurrence, self.owner.epoch, 'inspecting'):
                raise HubError('pilot_recovery_conflict', 409)
            paused = self.repo._paused(db)
            terminal_conflict = current['status'] in {'succeeded', 'rolled_back'} and derived != current['status']
            if terminal_conflict:
                derived = 'held'
            # A pause may record GET evidence but cannot reactivate any run or
            # release its reservation, even if the historical outcome is known.
            state = 'paused' if paused else ('held' if derived == 'held' else 'recovered')
            db.execute('UPDATE hub_container_pilot_recoveries SET state=?,evidence=? WHERE occurrence=?',
                       (state, json.dumps(evidence, sort_keys=True, separators=(',', ':')), occurrence))
            db.execute('UPDATE hub_container_pilot_recovery_claims SET state=? WHERE run_id=? AND occurrence=?', (state, run_id, occurrence))
            if paused:
                return dict(run=current, recovery=state, occurrence=occurrence)
            # Conflicting terminal run state is never silently downgraded.
            if terminal_conflict:
                self.repo._save(db, self.repo.execution_principal, 'run.recovery_held', 'run', current)
                return dict(run=current, recovery='held', occurrence=occurrence)
            if derived != 'held':
                for item in evidence:
                    db.execute('UPDATE hub_container_pilot_commands SET state=?,response_json=? WHERE run_id=? AND action=?',
                               (item['state'], item['response'], run_id, item['action']))
                if reservation.active is not None:
                    advance_owner, epoch_owner, prior_epoch = reservation.active
                    if (db.execute('DELETE FROM hub_container_pilot_advances WHERE run_id=? AND owner=?',
                                   (run_id, advance_owner)).rowcount != 1
                            or db.execute('DELETE FROM hub_container_pilot_advance_epochs WHERE run_id=? AND advance_owner=? AND epoch=?',
                                          (run_id, epoch_owner, prior_epoch)).rowcount != 1):
                        raise HubError('pilot_recovery_conflict', 409)
                if reservation.attempt is not None:
                    advance_owner, prior_epoch, prior_state = reservation.attempt
                    changed = db.execute("UPDATE hub_container_pilot_attempts SET state='quiescent' "
                        'WHERE run_id=? AND advance_owner=? AND epoch=? AND state=?',
                        (run_id, advance_owner, prior_epoch, prior_state)).rowcount
                    if changed != 1:
                        raise HubError('pilot_recovery_conflict', 409)
                candidate_failed = any(item['action'] in ACTIONS[:2] and item['state'] == 'failed' for item in evidence)
                if candidate_failed and not any(event['event'] == 'failed' for event in current['events']):
                    task = self.repo._get(db, current['task_id'], 'task')
                    task['repair_count'] = min(3, task['repair_count'] + 1)
                    self.repo._save(db, self.repo.execution_principal, 'task.run_failed', 'task', task)
            event_id = str(uuid.uuid5(uuid.UUID(run_id), 'container_pilot_v1:recovery:' + occurrence + ':' + derived))
            current['status'] = derived
            current['command_id'] = current['command_id'] or run_id
            entry = dict(event=derived, actor=self.repo.execution_principal,
                command_id=run_id, fencing_generation=current['fencing_generation'], event_id=event_id)
            reason = None
            if derived == 'held':
                last = next((item for item in reversed(evidence) if item['action'] in ACTIONS), None)
                phase = 'rollback_smoke' if last and last['action'] == ACTIONS[3] else 'rollback'
                reason = (phase + ('_failed' if last['state'] == 'failed' else '_outcome_uncertain')
                          if last and last['action'] in ACTIONS[2:] else 'candidate_outcome_uncertain')
                entry['reason'] = reason
            current['events'].append(entry)
            fingerprint = json.dumps([self.repo.execution_principal, run_id, derived,
                current['fencing_generation'], reason], sort_keys=True, separators=(',', ':'))
            db.execute('INSERT INTO hub_run_event_receipts VALUES(?,?,?)', (run_id, event_id, fingerprint))
            result = self.repo._save(db, self.repo.execution_principal, 'run.recovery_' + state, 'run', current)
            return dict(run=result, recovery=state, occurrence=occurrence)
