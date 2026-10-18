"""Explicit, bounded durable-outbox catchup for operator-configured fixtures.

No timer, observer, reassignment, model invocation or real-host driver lives here.
Call ``drain_batch`` on startup/invalidation. A reservation without a completed
receipt is *not* retried: a process may have died across the run-creation gap.
Such entries remain visible as requiring reconciliation, never guessed from
another run's target or task. The repository still enforces CAS, pause, fences
and the task's lifetime repair budget.
"""
from dataclasses import asdict
import json
import uuid

from .repository import AGENTS, HubError


class StagingDispatcher:
    def __init__(self, repo, workflow):
        if workflow.repo is not repo or repo.execution_principal is None:
            raise ValueError('dispatcher_repository_identity_required')
        self.repo, self.workflow = repo, workflow
        self._assert_writer()
        with repo._transaction() as db:
            schema = '''
                CREATE TABLE IF NOT EXISTS hub_dispatch_cursor (
                    id INTEGER PRIMARY KEY CHECK(id=1), sequence INTEGER NOT NULL);
                INSERT OR IGNORE INTO hub_dispatch_cursor VALUES(1,0);
                CREATE TABLE IF NOT EXISTS hub_dispatch_receipts (
                    sequence INTEGER PRIMARY KEY, entity_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, owner TEXT NOT NULL,
                    state TEXT NOT NULL, run_id TEXT, reason TEXT,
                    request TEXT NOT NULL, attempt_head INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS hub_dispatch_owners (
                    sequence INTEGER PRIMARY KEY, owner TEXT NOT NULL,
                    epoch TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_dispatch_recovery_seen (
                    sequence INTEGER PRIMARY KEY, epoch TEXT NOT NULL, head INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_dispatch_scan (
                    id INTEGER PRIMARY KEY CHECK(id=1), prefer_recovery INTEGER NOT NULL);
                INSERT OR IGNORE INTO hub_dispatch_scan VALUES(1,0);
            '''
            for statement in schema.split(';'):
                if statement.strip():
                    db.execute(statement)

    def _owner(self):
        from .container_pilot import ContainerPilotWorkflow
        return self.workflow.pilot_owner if type(self.workflow) is ContainerPilotWorkflow else None

    def _pin_owner(self, db, sequence, owner, state='active'):
        capability = self._owner()
        if capability is not None:
            db.execute('INSERT INTO hub_dispatch_owners VALUES(?,?,?,?) '
                       'ON CONFLICT(sequence) DO UPDATE SET owner=excluded.owner,epoch=excluded.epoch,state=excluded.state',
                       (sequence, owner, capability.epoch, state))

    def _verified_owner(self, db, row):
        capability = self._owner()
        paired = db.execute('SELECT owner,epoch,state FROM hub_dispatch_owners WHERE sequence=?', (row['sequence'],)).fetchone()
        if capability is None or paired is None or paired['owner'] != row['owner'] or paired['state'] not in {'active', 'quiescent'}:
            raise HubError('dispatch_owner_unverified', 409)
        try:
            if str(uuid.UUID(row['owner'])) != row['owner']:
                raise ValueError()
        except Exception:
            raise HubError('dispatch_owner_unverified', 409) from None
        if ((row['state'] in {'reserved', 'running'}) != (paired['state'] == 'active')):
            raise HubError('dispatch_owner_unverified', 409)
        if paired['epoch'] == capability.epoch:
            if paired['state'] != 'quiescent':
                raise HubError('dispatch_in_progress', 409)
        else:
            try:
                capability.assert_prior_epoch(paired['epoch'])
            except Exception:
                raise HubError('dispatch_owner_unverified', 409) from None
        return paired

    def _active_owner(self, db, reservation):
        capability = self._owner()
        if capability is not None:
            row = db.execute('SELECT owner,epoch,state FROM hub_dispatch_owners WHERE sequence=?', (reservation['sequence'],)).fetchone()
            if row is None or tuple(row) != (reservation['owner'], capability.epoch, 'active'):
                raise HubError('dispatch_owner_unverified', 409)

    def _recovery_candidates(self, limit):
        if self._owner() is None or limit == 0:
            return [], 0
        with self.repo._connection() as db:
            head = db.execute('SELECT COALESCE(MAX(sequence),0) FROM hub_outbox').fetchone()[0]
            rows = [dict(row) for row in db.execute('''SELECT r.* FROM hub_dispatch_receipts r
                JOIN hub_dispatch_owners o ON o.sequence=r.sequence AND o.owner=r.owner
                LEFT JOIN hub_dispatch_recovery_seen s ON s.sequence=r.sequence
                WHERE r.state IN ('reserved','running','needs_reconciliation')
                AND (s.sequence IS NULL OR s.epoch<>? OR s.head<?)
                AND (o.epoch<>? OR (o.state='quiescent' AND r.attempt_head<?
                    AND EXISTS (SELECT 1 FROM hub_outbox b WHERE b.sequence>r.attempt_head
                        AND (b.entity_id<>COALESCE(r.run_id,'')
                             OR b.action IN ('run.succeeded','run.rolled_back'))
                        AND b.action NOT IN ('task.run_created','task.run_failed'))))
                ORDER BY r.sequence LIMIT ?''', (self._owner().epoch, head, self._owner().epoch, head, limit))]
        return rows, head

    def _assert_writer(self):
        from .container_pilot import ContainerPilotWorkflow
        if type(self.workflow) is ContainerPilotWorkflow:
            self.workflow._assert_pilot_owner()
            return
        # Legacy fixture dispatch must not silently become a second writer to
        # an owner-enrolled pilot ledger.
        with self.repo._connection() as db:
            enrolled = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_pilot_owner'").fetchone()
        if enrolled:
            raise HubError('pilot_ownership_required', 409)

    @property
    def cursor(self):
        with self.repo._connection() as db:
            return db.execute('SELECT sequence FROM hub_dispatch_cursor WHERE id=1').fetchone()[0]

    def receipts(self, limit=50):
        self._limit(limit)
        with self.repo._connection() as db:
            rows = db.execute('SELECT * FROM hub_dispatch_receipts ORDER BY sequence DESC LIMIT ?', (limit,))
            return [{**{key: row[key] for key in row.keys() if key != 'request'},
                     'needs_reconciliation': row['state'] in {'reserved', 'running', 'needs_reconciliation'}}
                    for row in rows]

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise HubError('invalid_limit')

    @staticmethod
    def _eligible(task, target, revision):
        review = task.get('review') if task else None
        return bool(task and target and task['revision'] == revision
                    and task['status'] == 'verified' and task['owner'] in AGENTS
                    and review and review.get('actor') in AGENTS
                    and review['actor'] != task['owner'] and review.get('verdict') == 'approve'
                    and review.get('commit') == task['candidate_commit'] == target.candidate_commit
                    and target.policy_revision == 1
                    and target.driver in {'fixture', 'container_pilot_v1'})

    def _reserve_next(self):
        """Select/validate/reserve and advance one durable event atomically."""
        self._assert_writer()
        with self.repo._transaction() as db:
            cursor = db.execute('SELECT sequence FROM hub_dispatch_cursor WHERE id=1').fetchone()[0]
            row = db.execute('SELECT * FROM hub_outbox WHERE sequence>? ORDER BY sequence LIMIT 1', (cursor,)).fetchone()
            if row is None:
                return None
            task, target = None, None
            if row['action'] == 'task.reviewed':
                found = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='task'", (row['entity_id'],)).fetchone()
                task = json.loads(found[0]) if found else None
                if task:
                    target = self.workflow.targets.get(task['project'] + ':staging')
            eligible = self._eligible(task, target, row['revision'])
            if eligible and self.repo._paused(db):
                return {'paused': True}
            # Advancing past unrelated/stale events is safe; it never records a
            # deployment receipt for them. Coalesced invalidations lose no rows.
            db.execute('UPDATE hub_dispatch_cursor SET sequence=? WHERE id=1', (row['sequence'],))
            if not eligible:
                return {'skipped': row['sequence']}
            owner = str(uuid.uuid4())
            request = dict(actor=task['owner'], task_id=task['id'], expected_revision=task['revision'],
                           artifact_digest=target.artifact_digest, target_id=target.target_id,
                           client_request_id='hub-dispatch:' + str(row['sequence']), manifest=asdict(target))
            head = db.execute('SELECT COALESCE(MAX(sequence),0) FROM hub_outbox').fetchone()[0]
            db.execute('INSERT INTO hub_dispatch_receipts VALUES(?,?,?,?,?,?,?,?,?)',
                       (row['sequence'], task['id'], task['revision'], owner, 'reserved', None, None,
                        json.dumps(request, sort_keys=True), head))
            self._pin_owner(db, row['sequence'], owner)
            return dict(sequence=row['sequence'], task=task, target=target, owner=owner)

    def _reserve_deferred(self, sequence):
        """Reclaim only proven local nonacceptance, never uncertain effects."""
        self._assert_writer()
        with self.repo._transaction() as db:
            row = db.execute("SELECT * FROM hub_dispatch_receipts WHERE sequence=? AND state='deferred'",
                             (sequence,)).fetchone()
            if row is None:
                return None
            if self._owner() is not None:
                try:
                    self._verified_owner(db, row)
                except HubError:
                    return None
            head = db.execute('SELECT COALESCE(MAX(sequence),0) FROM hub_outbox').fetchone()[0]
            if row['attempt_head'] >= head:
                return None
            request = json.loads(row['request'])
            target = self.workflow.targets.get(request['target_id'])
            if target is None or asdict(target) != request['manifest']:
                db.execute("UPDATE hub_dispatch_receipts SET state='needs_reconciliation',reason='fixture_manifest_changed' WHERE sequence=?",
                           (sequence,))
                return None
            found = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='task'", (request['task_id'],)).fetchone()
            task = json.loads(found[0]) if found else None
            if not self._eligible(task, target, request['expected_revision']):
                db.execute("UPDATE hub_dispatch_receipts SET state='skipped',reason='stale_review' WHERE sequence=?", (sequence,))
                return None
            if self.repo._paused(db):
                return None
            owner = str(uuid.uuid4())
            changed = db.execute("UPDATE hub_dispatch_receipts SET owner=?,state='reserved',reason=NULL,attempt_head=? WHERE sequence=? AND state='deferred'",
                                 (owner, head, sequence)).rowcount
            if changed == 1:
                self._pin_owner(db, sequence, owner)
            return dict(sequence=sequence, owner=owner) if changed == 1 else None

    def _record(self, reservation, state, *, run_id=None, reason=None):
        self._assert_writer()
        with self.repo._transaction() as db:
            self._active_owner(db, reservation)
            changed = db.execute('UPDATE hub_dispatch_receipts SET state=?,run_id=COALESCE(?,run_id),reason=? '
                       'WHERE sequence=? AND owner=?',
                       (state, run_id, reason, reservation['sequence'], reservation['owner'])).rowcount
            if changed != 1:
                raise HubError('dispatch_reservation_conflict', 409)
            self._pin_owner(db, reservation['sequence'], reservation['owner'],
                            'active' if state in {'reserved', 'running'} else 'quiescent')

    async def drain_batch(self, limit=50):
        """Inspect at most limit historical rows, with at most one workflow per row.

        This is not a durable replay subscription. Call again when ``more`` is
        true; the caller decides scheduling. Exceptions are sanitized in receipts.
        BaseException/cancellation deliberately leaves an uncertain reservation.
        """
        self._limit(limit)
        examined = dispatched = 0
        paused = False
        recoverable, recovery_head = self._recovery_candidates(limit)
        if recoverable:
            with self.repo._transaction() as db:
                unread = db.execute('SELECT 1 FROM hub_outbox WHERE sequence>? LIMIT 1', (self.cursor,)).fetchone() is not None
                if unread:
                    if limit == 1:
                        preference = db.execute('SELECT prefer_recovery FROM hub_dispatch_scan WHERE id=1').fetchone()[0]
                        db.execute('UPDATE hub_dispatch_scan SET prefer_recovery=? WHERE id=1', (1 - preference,))
                        recoverable = recoverable[:preference]
                    else:
                        recoverable = recoverable[:max(1, limit // 2)]
            for row in recoverable:
                examined += 1
                if self.repo.get_paused():
                    paused = True
                    break
                with self.repo._transaction() as db:
                    db.execute('INSERT INTO hub_dispatch_recovery_seen VALUES(?,?,?) '
                               'ON CONFLICT(sequence) DO UPDATE SET epoch=excluded.epoch,head=excluded.head',
                               (row['sequence'], self._owner().epoch, recovery_head))
                try:
                    result = await self.resume_accepted(row['sequence'], expected_owner=row['owner'])
                    dispatched += int(result['state'] in {'succeeded', 'rolled_back'})
                except HubError:
                    # A failed proof cannot authorize takeover. Other distinct
                    # events in this bounded batch may still make progress.
                    pass
        # Snapshot the bounded retry set once. A still-busy target cannot spin
        # within one invocation or consume a new run identity on each retry.
        with self.repo._connection() as db:
            head = db.execute('SELECT COALESCE(MAX(sequence),0) FROM hub_outbox').fetchone()[0]
            deferred = [row[0] for row in db.execute("SELECT sequence FROM hub_dispatch_receipts WHERE state='deferred' AND attempt_head<? ORDER BY sequence LIMIT ?", (head, limit - examined))]
        for sequence in deferred:
            examined += 1
            reservation = self._reserve_deferred(sequence)
            if reservation is not None:
                dispatched += await self._execute(reservation)
        for _ in range(limit - examined):
            reservation = self._reserve_next()
            if reservation is None:
                break
            if reservation.get('paused'):
                paused = True
                break
            examined += 1
            if 'skipped' in reservation:
                continue
            dispatched += await self._execute(reservation)
        with self.repo._connection() as db:
            more = db.execute('SELECT 1 FROM hub_outbox WHERE sequence>? LIMIT 1', (self.cursor,)).fetchone() is not None
        more = more or bool(self._recovery_candidates(1)[0])
        return dict(examined=examined, dispatched=dispatched, cursor=self.cursor, paused=paused, more=more)

    async def _execute(self, reservation, *, recover_accepted=False):
        self._assert_writer()
        with self.repo._connection() as db:
            self._active_owner(db, reservation)
            row = db.execute('SELECT * FROM hub_dispatch_receipts WHERE sequence=? AND owner=?',
                             (reservation['sequence'], reservation['owner'])).fetchone()
        if row is None:
            raise HubError('dispatch_reservation_conflict', 409)
        try:
            request = json.loads(row['request'])
            if row['entity_id'] != request['task_id'] or row['revision'] != request['expected_revision']:
                raise HubError('dispatch_receipt_conflict', 409)
            target = self.workflow.targets.get(request['target_id'])
            if target is None or asdict(target) != request['manifest']:
                raise HubError('fixture_manifest_changed', 409)
            # Receipt lookup is exact, sender-scoped and transactional with run
            # creation. Never search by target/task/latest run to infer identity.
            prior = self.repo.get_run_request(request['actor'], request['client_request_id'])
            arguments = {key: value for key, value in request.items() if key != 'manifest'}
            # Re-submit the same *local transactional request*, not an executor
            # command: this also validates the saved request fingerprint. A
            # colliding key must not bind an unrelated run, even with similar
            # task/target fields. No external I/O happens in create_run.
            if prior is None:
                run = self.workflow.create_run(**arguments)
            else:
                # Exact receipt validation must not depend on today's task
                # candidate: the completed task may already have a new one.
                self._assert_writer()
                run = self.repo.create_run(request['actor'], request['task_id'], request['expected_revision'],
                                           request['artifact_digest'], request['target_id'],
                                           execution_manifest_digest=self.workflow._manifest_digest(target),
                                           client_request_id=request['client_request_id'])
            if prior is not None and prior['id'] != run['id']:
                raise HubError('dispatch_receipt_conflict', 409)
            if row['run_id'] is not None and row['run_id'] != run['id']:
                raise HubError('dispatch_receipt_conflict', 409)
            self._record(reservation, 'running', run_id=run['id'])
            if recover_accepted:
                if run['status'] in {'succeeded', 'rolled_back'}:
                    self._record(reservation, run['status'], run_id=run['id'])
                    return 1
                from .pilot_recovery import PilotRecovery
                recovery = PilotRecovery(self.workflow)
                with self.repo._connection() as db:
                    active = db.execute('SELECT 1 FROM hub_container_pilot_advances WHERE run_id=?', (run['id'],)).fetchone()
                    attempt = db.execute('SELECT 1 FROM hub_container_pilot_attempts WHERE run_id=?', (run['id'],)).fetchone()
                    commands = db.execute('SELECT 1 FROM hub_container_pilot_commands WHERE run_id=?', (run['id'],)).fetchone()
                    orphan = db.execute('SELECT 1 FROM hub_container_pilot_advance_epochs WHERE run_id=?', (run['id'],)).fetchone()
                if active or attempt:
                    outcome = await recovery.recover(run['id'])
                    if outcome['recovery'] != 'recovered':
                        self._record(reservation, 'needs_reconciliation', run_id=run['id'], reason=outcome['recovery'])
                        return 0
                else:
                    if run['status'] != 'pending' or commands or orphan:
                        raise HubError('dispatch_run_quiescence_unverified', 409)
                    with self.repo._transaction() as db:
                        recovery._authorized(db, self.repo._get(db, run['id'], 'run'))
            result = await self.workflow.advance(run['id'])
            state = result['status']
            self._record(reservation, state if state in {'succeeded', 'rolled_back'} else 'needs_reconciliation',
                         run_id=run['id'], reason=None if state in {'succeeded', 'rolled_back'} else state)
            return 1
        except HubError as exc:
            if exc.code in {'active_run', 'paused'}:
                try:
                    unaccepted = self.repo.get_run_request(request['actor'], request['client_request_id']) is None
                except Exception:
                    unaccepted = False
                if unaccepted:
                    # Repository rejects these before allocating run/fence or
                    # effects. Only exact absence of the atomic receipt permits
                    # ordinary event-triggered retry, with current review CAS.
                    self._record(reservation, 'deferred', reason=exc.code)
                    return 0
            self._record(reservation, 'needs_reconciliation', reason=exc.code)
        except Exception:
            self._record(reservation, 'needs_reconciliation', reason='dispatch_uncertain')
        return 0

    async def resume_accepted(self, sequence, *, expected_owner):
        """Recover the original local request under proven dispatcher ownership.

        This may continue never-reserved phases only after GET recovery. It is
        neither caller-Boolean quiescence nor an expiry-based takeover.
        """
        self._assert_writer()
        if type(sequence) is not int or sequence < 1 or self._owner() is None:
            raise HubError('dispatch_owner_unverified', 409)
        owner = str(uuid.uuid4())
        with self.repo._transaction() as db:
            row = db.execute('SELECT * FROM hub_dispatch_receipts WHERE sequence=?', (sequence,)).fetchone()
            if row is None:
                raise HubError('not_found', 404)
            if row['owner'] != expected_owner:
                raise HubError('dispatch_reservation_conflict', 409)
            self._verified_owner(db, row)
            if row['state'] in {'succeeded', 'rolled_back'}:
                return {key: row[key] for key in row.keys() if key != 'request'}
            if self.repo._paused(db):
                raise HubError('paused', 423)
            db.execute("UPDATE hub_dispatch_receipts SET owner=?,state='reserved',reason=NULL, "
                       "attempt_head=(SELECT COALESCE(MAX(sequence),0) FROM hub_outbox) WHERE sequence=? AND owner=?",
                       (owner, sequence, expected_owner))
            self._pin_owner(db, sequence, owner)
        await self._execute(dict(sequence=sequence, owner=owner), recover_accepted=True)
        with self.repo._connection() as db:
            row = db.execute('SELECT * FROM hub_dispatch_receipts WHERE sequence=?', (sequence,)).fetchone()
            return {key: row[key] for key in row.keys() if key != 'request'}

    async def reconcile(self, sequence, *, expected_owner, confirmed_quiescent=False):
        """Explicit recovery after the prior dispatcher is proven quiescent.

        Not an expiry-based lock takeover. Exact durable run receipts recover
        local crashes, but unknown executor effects remain held by the workflow.
        A caller must establish quiescence outside this module before invoking.
        """
        self._assert_writer()
        with self.repo._connection() as db:
            enrolled = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_pilot_owner'").fetchone()
        if enrolled:
            # A caller Boolean cannot adopt an enrolled owner's reservations.
            # Epoch-tagged, evidence-only adoption is a separate prerequisite.
            raise HubError('dispatch_owner_recovery_unavailable', 409)
        if type(sequence) is not int or sequence < 1 or confirmed_quiescent is not True:
            raise HubError('dispatch_quiescence_required', 409)
        owner = str(uuid.uuid4())
        with self.repo._transaction() as db:
            row = db.execute('SELECT * FROM hub_dispatch_receipts WHERE sequence=?', (sequence,)).fetchone()
            if row is None:
                raise HubError('not_found', 404)
            if row['state'] in {'succeeded', 'rolled_back'}:
                return {key: row[key] for key in row.keys() if key != 'request'}
            if row['owner'] != expected_owner:
                raise HubError('dispatch_reservation_conflict', 409)
            if self.repo._paused(db):
                raise HubError('paused', 423)
            db.execute("UPDATE hub_dispatch_receipts SET owner=?,state='reserved' WHERE sequence=? AND owner=?",
                       (owner, sequence, expected_owner))
        await self._execute(dict(sequence=sequence, owner=owner))
        with self.repo._connection() as db:
            row = db.execute('SELECT * FROM hub_dispatch_receipts WHERE sequence=?', (sequence,)).fetchone()
            return {key: row[key] for key in row.keys() if key != 'request'}
