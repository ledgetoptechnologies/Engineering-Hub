"""Read-only recognition of an original modular reservation during pilot enrollment.

Recognition preserves a veto/hold; it supplies no admission, recovery, release or
execution authority. Call only inside the caller's existing enrollment transaction.
Held submissions without their original claim remain deliberately unrecognized.
Generated command/source UUIDs have no independent local attestation: consistency
with this durable journal is not proof of a remote receipt or permission to act.
"""
from collections import Counter
import hashlib
import json
import math
import os
import re
import sqlite3
from uuid import UUID

from .execution_policy import SharedExecutionPolicies
from .repository import AGENTS, HubRepository
from .staging_controller import StagingController
from .staging_resources import KINDS, StagingResourceStore
from .work_queue import STAGING_SERVICE_EXECUTOR, WorkAction, WorkQueue, WorkWorker


# Exhaustion preserves the enrollment veto; history is never truncated/pruned.
# One SQLite row is necessarily materialized by sqlite3, but it is rejected by
# character length before encoding and never accumulated into an unbounded list.
MAX_RECORD_BYTES = 128 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_HISTORY_ROWS = 10_000


class _Budget:
    def __init__(self):
        self.rows = self.bytes = 0

    def consume(self, raw):
        _require(self.rows < MAX_HISTORY_ROWS)
        _require(type(raw) is str and len(raw) <= MAX_RECORD_BYTES)
        encoded = raw.encode('utf-8')
        _require(len(encoded) <= MAX_RECORD_BYTES
                 and self.bytes + len(encoded) <= MAX_METADATA_BYTES)
        self.rows += 1
        self.bytes += len(encoded)


def _require(value):
    if not value:
        raise ValueError('unrecognized_modular_hold')


def _pairs(entries):
    result = {}
    for key, value in entries:
        _require(key not in result)
        result[key] = value
    return result


def _json(raw, budget):
    budget.consume(raw)
    value = json.loads(raw, object_pairs_hook=_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    _require(type(value) is dict)
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _uuid(value):
    _require(type(value) is str and str(UUID(value)) == value)
    return value


def _worker(value):
    _require(type(value) is str and value.startswith(STAGING_SERVICE_EXECUTOR + ':'))
    _uuid(value[len(STAGING_SERVICE_EXECUTOR) + 1:])


def _protections(value, job_id, *, resource=False):
    _require(type(value) is list and len(value) <= 2)
    seen = set()
    for item in value:
        _require(type(item) is dict and set(item) == {'reason', 'ref'}
                 and item['reason'] in ({'active_job', 'held_campaign'} if resource else {'held_campaign'})
                 and item['ref'] == job_id and item['reason'] not in seen)
        seen.add(item['reason'])
    if resource:
        _require('active_job' in seen)


def modular_hold_matches(db, *, repo, queue, policies, modular_controller, reservation):
    """Return exact bool; unknown/malformed history leaves enrollment's veto intact.

    Uses only SELECT on the passed SQLite transaction. No clocks/lease expiry,
    secondary connections, worker API, writes or cleanup are performed. An old
    worker UUID is historical identity, never adopted by the current controller.
    All JSON metadata shares 10,000-row/8MiB UTF-8 scan ceilings, including the
    target and resource-job binding. A record is at most128KiB. Excess denies
    recognition without skipping, deleting or adopting any history.
    """
    try:
        _require(type(db) is sqlite3.Connection and db.in_transaction
                 and type(repo) is HubRepository and type(queue) is WorkQueue
                 and queue.repo is repo and type(policies) is SharedExecutionPolicies
                 and policies.repo is repo and policies.queue is queue
                 and type(modular_controller) is StagingController)
        budget = _Budget()
        databases = db.execute("SELECT file FROM pragma_database_list WHERE name='main' LIMIT 2").fetchall()
        _require(len(databases) == 1 and os.path.samefile(databases[0]['file'], repo.path))
        controller = modular_controller
        _require(controller.repo is repo and controller.queue is queue
                 and controller.execution_policies is policies
                 and controller._execution_policies is policies
                 and type(controller.resources) is StagingResourceStore
                 and controller.resources.repo is repo
                 and controller.resources._control is controller.principal
                 and type(controller._policy_context) is tuple and len(controller._policy_context) == 6
                 and controller.actions == controller._policy_actions
                 and all(a is b for a, b in zip(
                     (repo, queue, controller.client, controller.resources, controller.principal, controller.worker),
                     controller._policy_context)))
        binding = policies.assert_pending_binding('modular', controller._policy_binding)
        _require(binding is controller._policy_binding and binding.worker is controller.worker
                 and type(controller.worker) is WorkWorker
                 and controller.worker.agent == STAGING_SERVICE_EXECUTOR)
        supplied = dict(reservation)
        _require(set(supplied) == {'machine_id', 'job_id', 'generation'})
        _uuid(supplied['job_id'])
        _require(type(supplied['generation']) is int and supplied['generation'] > 0)
        machines = db.execute('SELECT machine_id,job_id,generation FROM hub_work_machines WHERE job_id=? LIMIT 2',
                              (supplied['job_id'],)).fetchall()
        _require(len(machines) == 1 and dict(machines[0]) == supplied)
        jobs = db.execute('SELECT * FROM hub_work_jobs WHERE id=? LIMIT 2', (supplied['job_id'],)).fetchall()
        _require(len(jobs) == 1)
        job = dict(jobs[0]); action = queue.actions.get(job['action_key'])
        _require(type(action) is WorkAction and binding.actions.get(job['action_key']) is action
                 and action.machine_id == job['machine_id'] == supplied['machine_id']
                 and action.project == job['project'] and action.environments == frozenset({'staging'})
                 and action.requires_execution_proof is True and job['requires_execution_proof'] == 1
                 and job['environment'] == 'staging' and job['approval_id'] is None
                 and job['state'] in {'claimed', 'held'} and job['fence'] == supplied['generation'])
        _worker(job['worker'])
        _require(type(job['lease_until']) in (int, float) and math.isfinite(job['lease_until'])
                 and job['lease_until'] > 0)
        target = _json(job['target'], budget)
        _require(set(target) == {'task_id', 'source_commit', 'artifact_digest'})
        match = None
        identity_counts = {key: Counter() for key in ('id', 'job_id', 'source_id', 'command_id')}
        cross_role_counts = Counter()
        for row in db.execute('SELECT * FROM hub_staging_submissions'):
            candidate = _json(row['data'], budget)
            for key, counts in identity_counts.items():
                value = candidate.get(key)
                _require(value is None or type(value) is str and len(value) <= 128)
                counts[value] += 1
                if key != 'job_id':
                    cross_role_counts[value] += 1
            if candidate.get('job_id') == job['id']:
                _require(match is None)
                match = (dict(row), candidate)
        _require(match is not None)
        row, item = match
        for key in ('id', 'request_id', 'source_id', 'command_id'):
            _uuid(item[key])
        _require(len({item[k] for k in ('id', 'source_id', 'command_id')}) == 3)
        _require(all(counts[item[key]] == 1 for key, counts in identity_counts.items()))
        _require(all(cross_role_counts[item[key]] == 1 for key in ('id', 'source_id', 'command_id')))
        _require(row['id'] == item['id'] and row['actor'] == item['actor'] == job['actor']
                 and item['actor'] in AGENTS and row['request_id'] == item['request_id']
                 and job['request_id'] == 'staging:' + item['request_id']
                 and item['project'] == job['project']
                 and controller.actions.get((item['project'], item['recipe'])) == job['action_key']
                 and item['phase'] in {'queued', 'dispatching', 'accepted', 'held'}
                 and re.fullmatch('[a-f0-9]{40}', item['source_commit'])
                 and re.fullmatch('[a-f0-9]{64}', item['source_digest'])
                 and target == {'task_id': item['id'], 'source_commit': item['source_commit'],
                                'artifact_digest': 'sha256:' + item['source_digest']})
        _require(item.get('topic') is None or type(item['topic']) is str
                 and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', item['topic']))
        public = {key: item[key] for key in
                  ('request_id', 'project', 'recipe', 'source_commit', 'source_digest', 'topic')}
        _require(row['fingerprint'] == hashlib.sha256(_canonical(public).encode()).hexdigest())
        fingerprint = [job['action_key'], job['environment'], job['machine_id'], job['project'],
                       bool(job['requires_execution_proof']), job['approval_id'], job['target']]
        _require(job['fingerprint'] == _canonical(fingerprint))
        claim = item.get('claim')
        if claim is not None:
            _require(type(claim) is dict and all(claim.get(key) == job[key] for key in
                ('id', 'actor', 'request_id', 'action_key', 'environment', 'machine_id', 'project',
                 'worker', 'fence', 'requires_execution_proof'))
                and claim.get('target') == target and claim.get('state') == 'claimed')
        else:
            _require(item['phase'] == 'queued')
        ids = item['resource_ids']
        _require(type(ids) is dict and set(ids) == KINDS and len(set(ids.values())) == 4)
        for identity in ids.values():
            _uuid(identity)
        resource_jobs = db.execute('SELECT data FROM hub_staging_resource_jobs WHERE id=? LIMIT 2', (job['id'],)).fetchall()
        _require(len(resource_jobs) == 1)
        resource_job = _json(resource_jobs[0]['data'], budget)
        _require(resource_job['id'] == job['id'] and resource_job['project'] == item['project']
                 and resource_job['created_by'] == item['actor']
                 and resource_job['source_commit'] == item['source_commit']
                 and resource_job['artifact_digest'] is None and resource_job['terminal'] is False)
        _protections(resource_job['protected'], job['id'])
        ownership = db.execute('SELECT * FROM hub_staging_resource_ownership WHERE job_id=? LIMIT 5', (job['id'],)).fetchall()
        _require(len(ownership) == 4 and {o['id'] for o in ownership} == set(ids.values()))
        entity_items = {}
        for entity in db.execute("SELECT id,data FROM hub_entities WHERE kind='staging_resource'"):
            candidate = _json(entity['data'], budget)
            if candidate.get('job_id') == job['id']:
                _require(len(entity_items) < 4 and entity['id'] not in entity_items
                         and entity['id'] == candidate['id'])
                entity_items[entity['id']] = candidate
        _require(set(entity_items) == set(ids.values()))
        for owner in ownership:
            resource = entity_items[owner['id']]
            _require(all(owner[key] == expected for key, expected in (
                ('project', item['project']), ('created_by', item['actor']), ('source_commit', item['source_commit']))))
            _require(resource['id'] == owner['id'] and resource['job_id'] == job['id']
                     and resource['kind'] in ids and ids[resource['kind']] == resource['id']
                     and resource['project'] == item['project'] and resource['created_by'] == item['actor']
                     and resource['source_commit'] == item['source_commit']
                     and resource['artifact_digest'] is None and resource['state'] in {'provisioning', 'failed'}
                     and resource['preview_path'] is None and resource['keep'] is None)
            _protections(resource['protected'], job['id'], resource=True)
        for cleanup in db.execute('SELECT data FROM hub_staging_dispatch_cleanup'):
            _require(_json(cleanup['data'], budget).get('resource_id') not in ids.values())
        # Recheck private context after parsing all durable data, without opening
        # a second database view or invoking admission/reconciliation callbacks.
        _require(policies.assert_pending_binding('modular', controller._policy_binding) is binding)
        return True
    except Exception:
        return False
