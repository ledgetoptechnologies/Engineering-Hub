"""Standalone declared-device and maintenance ledger; never probes or dispatches.

This owns a dedicated SQLite connection, not a HubRepository connection. Future
Hub-ledger integration must use that repository's writer-fenced transactions;
this class deliberately refuses foreign tables rather than bypassing the fence.
Principals are supplied by trusted authentication adapters, never body fields.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import re
import sqlite3
import threading
from types import MappingProxyType
import uuid

from .repository import HubError


CAPABILITIES = frozenset({'read', 'device_write', 'maintenance_write', 'probe_write', 'metrics_read'})
# Owner-human-only: metrics summarize whole machines, not just declared probes.
HUMAN_ONLY = frozenset({'device_write', 'metrics_read'})
MAX_EVENTS_PER_DEVICE = 512
MAX_MAINTENANCE_PER_DEVICE = 100
MAX_PROBE_IDENTITIES = 16
# Raw probe telemetry is ephemeral: retain newest16 strictly ordered samples
# per stable probe identity (<=256/device). Maintenance/events are never pruned.
MAX_PROBE_SAMPLES = 16
MAX_PROBES_PER_DEVICE = MAX_PROBE_IDENTITIES * MAX_PROBE_SAMPLES
_TABLES = frozenset({'infra_devices', 'infra_maintenance', 'infra_requests', 'infra_events', 'infra_probes'})


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _text(value, maximum, *, empty=False, multiline=False):
    if (type(value) is not str or len(value) > maximum or (not empty and not value.strip())
            or any((ord(char) < 32 and not (multiline and char == '\n')) or ord(char) == 127 for char in value)):
        raise HubError('invalid_infrastructure_text')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise HubError('invalid_infrastructure_text') from None
    return value


def _slug(value):
    if type(value) is not str or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', value) is None:
        raise HubError('invalid_device_id')
    return value


def _timestamp(value):
    if type(value) is not str or len(value) > 40:
        raise HubError('invalid_infrastructure_time')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise HubError('invalid_infrastructure_time') from None


def _revision(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise HubError('invalid_revision')
    return value


@dataclass(frozen=True)
class InfrastructurePrincipal:
    kind: str
    identity: str


class InfrastructureRegistry:
    """Dedicated connection ownership transfers here on successful creation."""
    def __init__(self, connection, *, capabilities=None, clock=None, admission_guard=None):
        if type(connection) is not sqlite3.Connection or connection.in_transaction:
            raise TypeError('An idle dedicated SQLite connection is required')
        if admission_guard is not None and not callable(admission_guard):
            raise TypeError('admission_guard must be a trusted callable')
        self._admission_guard, self._admission_failed = admission_guard, False
        self._assert_admission()
        if (any(row[1] != 'main' for row in connection.execute('PRAGMA database_list'))
                or connection.execute('SELECT 1 FROM sqlite_temp_master LIMIT 1').fetchone()):
            raise HubError('dedicated_infrastructure_database_required', 409)
        policies = {}
        for principal, actions in (capabilities or {}).items():
            self._principal(principal)
            if type(actions) not in (set, frozenset) or not actions <= CAPABILITIES:
                raise ValueError('Invalid infrastructure capabilities')
            policies[principal] = frozenset(actions)
        objects = connection.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if any(kind != 'table' or name not in _TABLES for name, kind in objects):
            raise HubError('dedicated_infrastructure_database_required', 409)
        if objects and {name for name, _ in objects} != _TABLES:
            raise HubError('incomplete_infrastructure_schema', 409)
        self.db, self.capabilities = connection, MappingProxyType(policies)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS infra_devices(id TEXT PRIMARY KEY,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS infra_maintenance(id TEXT PRIMARY KEY,device_id TEXT NOT NULL
                REFERENCES infra_devices(id),state TEXT NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS infra_requests(principal TEXT NOT NULL,key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(principal,key));
            CREATE TABLE IF NOT EXISTS infra_events(sequence INTEGER PRIMARY KEY,device_id TEXT NOT NULL
                REFERENCES infra_devices(id),action TEXT NOT NULL,principal TEXT NOT NULL,
                at TEXT NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS infra_probes(sequence INTEGER PRIMARY KEY,device_id TEXT NOT NULL
                REFERENCES infra_devices(id),principal TEXT NOT NULL,data TEXT NOT NULL);
        ''')
        self.db.commit()
        self._assert_admission()

    def _assert_admission(self):
        # This callback is trusted composition metadata, never request input.
        # Loss is sticky: a later True cannot adopt replacement storage.
        if self._admission_failed:
            raise HubError('infrastructure_unavailable',503)
        if self._admission_guard is not None:
            try:
                if self._admission_guard() is not True:
                    raise ValueError()
            except Exception:
                self._admission_failed = True
                raise HubError('infrastructure_unavailable',503) from None

    @staticmethod
    def _principal(principal):
        if type(principal) is not InfrastructurePrincipal or principal.kind not in ('human', 'agent'):
            raise HubError('forbidden', 403)
        _text(principal.identity, 128)
        return {'kind': principal.kind, 'identity': principal.identity}

    def _authorize(self, principal, action):
        self._principal(principal)
        allowed = self.capabilities.get(principal, frozenset())
        if ('read' not in allowed or action not in allowed
                or (action in HUMAN_ONLY and principal.kind != 'human')):
            raise HubError('forbidden', 403)
        self._assert_admission()

    def _now(self):
        value = self.clock()
        if type(value) is not datetime or value.utcoffset() is None:
            raise HubError('invalid_infrastructure_clock', 503)
        return value.astimezone(timezone.utc)

    @contextmanager
    def _transaction(self):
        with self.lock:
            self._assert_admission()
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self.db
                self._assert_admission()
                self.db.commit()
                self._assert_admission()
            except Exception:
                self.db.rollback()
                self._assert_admission()
                raise

    @contextmanager
    def _reading(self):
        with self.lock:
            self._assert_admission()
            try:
                yield
            finally:
                self._assert_admission()

    @staticmethod
    def _device(db, identity):
        row = db.execute('SELECT data FROM infra_devices WHERE id=?', (_slug(identity),)).fetchone()
        if row is None:
            raise HubError('device_not_found', 404)
        return json.loads(row[0])

    def _event(self, db, principal, device_id, action, item):
        # Reserve one remaining event for each currently open maintenance item.
        # Device edits cannot consume its eventual resolution record.
        count = db.execute('SELECT count(*) FROM infra_events WHERE device_id=?',(device_id,)).fetchone()[0]
        opened = db.execute("SELECT count(*) FROM infra_maintenance WHERE device_id=? AND state='open'",
                            (device_id,)).fetchone()[0]
        if count >= MAX_EVENTS_PER_DEVICE - opened:
            raise HubError('infrastructure_event_limit',409)
        db.execute('INSERT INTO infra_events(device_id,action,principal,at,data) VALUES(?,?,?,?,?)',
                   (device_id, action, _dump(self._principal(principal)), self._now().isoformat(), _dump(item)))

    def upsert_device(self, principal, device, *, expected_revision):
        self._authorize(principal, 'device_write')
        required = {'id', 'name', 'addresses', 'role'}
        optional = {'hostname', 'network', 'vlan', 'retired'}
        if type(device) is not dict or not required <= set(device) or set(device) - required - optional:
            raise HubError('invalid_device_schema')
        identity = _slug(device['id'])
        name, role = _text(device['name'], 100), _text(device['role'], 100)
        hostname = device.get('hostname')
        if hostname is not None:
            if (type(hostname) is not str or not 1 <= len(hostname) <= 253
                    or any(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part) is None
                           for part in hostname.split('.'))):
                raise HubError('invalid_device_hostname')
            hostname = hostname.lower()
        addresses = device['addresses']
        if type(addresses) is not list or len(addresses) > 16:
            raise HubError('invalid_device_addresses')
        try:
            if any(type(value) is not str or '%' in value for value in addresses):
                raise ValueError()
            addresses = [str(ipaddress.ip_address(value)) for value in addresses]
            if len(set(addresses)) != len(addresses):
                raise ValueError()
            network = device.get('network')
            if network is not None:
                if type(network) is not str or '%' in network:
                    raise ValueError()
                network = ipaddress.ip_network(network, strict=True)
                if any(ipaddress.ip_address(value) not in network for value in addresses):
                    raise ValueError()
                network = str(network)
        except (TypeError, ValueError):
            raise HubError('invalid_device_addresses') from None
        vlan, retired = device.get('vlan'), device.get('retired', False)
        if (vlan is not None and (type(vlan) is not int or not 1 <= vlan <= 4094)) or type(retired) is not bool:
            raise HubError('invalid_device_metadata')
        expected_revision = _revision(expected_revision)
        with self._transaction() as db:
            old = db.execute('SELECT data FROM infra_devices WHERE id=?', (identity,)).fetchone()
            old = json.loads(old[0]) if old else None
            if expected_revision != (old['revision'] if old else 0):
                raise HubError('revision_conflict', 409)
            if old is None and db.execute('SELECT count(*) FROM infra_devices').fetchone()[0] >= 256:
                raise HubError('device_limit', 409)
            at = self._now().isoformat()
            item = dict(id=identity, name=name, hostname=hostname, addresses=addresses, network=network,
                        vlan=vlan, role=role, retired=retired, revision=expected_revision+1,
                        created_at=old['created_at'] if old else at, updated_at=at)
            db.execute('INSERT INTO infra_devices VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (identity, _dump(item)))
            self._event(db, principal, identity, 'device.upserted', item)
            return item

    def get_device(self, principal, device_id):
        self._authorize(principal, 'read')
        with self._reading():
            return self._device(self.db, device_id)

    def list_devices(self, principal, *, include_retired=False):
        self._authorize(principal, 'read')
        if type(include_retired) is not bool:
            raise HubError('invalid_filter')
        with self._reading():
            items = [json.loads(row[0]) for row in self.db.execute('SELECT data FROM infra_devices ORDER BY id LIMIT 257')]
            if len(items) > 256:
                raise HubError('device_limit', 409)
            return {'items': [item for item in items if include_retired or not item['retired']]}

    def append_maintenance(self, principal, device_id, reason, client_request_id, *, expected_end=None):
        self._authorize(principal, 'maintenance_write')
        device_id, reason = _slug(device_id), _text(reason, 1000, multiline=True)
        key = _text(client_request_id, 128)
        end = _timestamp(expected_end).isoformat() if expected_end is not None else None
        payload = dict(device_id=device_id, reason=reason, expected_end=end)
        fingerprint = hashlib.sha256(_dump(payload).encode()).hexdigest()
        actor = _dump(self._principal(principal))
        with self._transaction() as db:
            prior = db.execute('SELECT * FROM infra_requests WHERE principal=? AND key=?', (actor,key)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HubError('idempotency_conflict', 409)
                return json.loads(prior['result'])
            device = self._device(db,device_id)
            if device['retired']:
                raise HubError('device_retired',409)
            if db.execute("SELECT 1 FROM infra_maintenance WHERE device_id=? AND state='open'",(device_id,)).fetchone():
                raise HubError('maintenance_already_open',409)
            if db.execute('SELECT count(*) FROM infra_maintenance WHERE device_id=?',(device_id,)).fetchone()[0] >= MAX_MAINTENANCE_PER_DEVICE:
                raise HubError('maintenance_history_limit',409)
            item = dict(id=str(uuid.uuid4()), **payload, state='open', revision=1,
                        opened_at=self._now().isoformat(), opened_by=self._principal(principal),
                        resolved_at=None, resolved_by=None, resolution=None)
            db.execute('INSERT INTO infra_maintenance VALUES(?,?,?,?)', (item['id'],device_id,'open',_dump(item)))
            db.execute('INSERT INTO infra_requests VALUES(?,?,?,?)',(actor,key,fingerprint,_dump(item)))
            self._event(db,principal,device_id,'maintenance.opened',item)
            return item

    def resolve_maintenance(self, principal, maintenance_id, *, expected_revision, resolution):
        self._authorize(principal,'maintenance_write')
        try:
            valid_id = type(maintenance_id) is str and str(uuid.UUID(maintenance_id)) == maintenance_id
        except ValueError:
            valid_id = False
        if not valid_id:
            raise HubError('invalid_maintenance_id')
        resolution, expected_revision = _text(resolution,1000,multiline=True), _revision(expected_revision)
        with self._transaction() as db:
            row=db.execute('SELECT data FROM infra_maintenance WHERE id=?',(maintenance_id,)).fetchone()
            if row is None:
                raise HubError('maintenance_not_found',404)
            item=json.loads(row[0])
            if item['state']!='open' or item['revision']!=expected_revision:
                raise HubError('revision_conflict',409)
            item.update(state='resolved',revision=expected_revision+1,resolution=resolution,
                        resolved_at=self._now().isoformat(),resolved_by=self._principal(principal))
            db.execute("UPDATE infra_maintenance SET state='resolved',data=? WHERE id=?",(_dump(item),maintenance_id))
            self._event(db,principal,item['device_id'],'maintenance.resolved',item)
            return item

    def maintenance_history(self, principal, device_id, *, limit=50):
        self._authorize(principal,'read')
        if type(limit) is not int or not 1<=limit<=50:
            raise HubError('invalid_limit')
        with self._reading():
            self._device(self.db,device_id)
            rows=self.db.execute('SELECT data FROM infra_maintenance WHERE device_id=? ORDER BY rowid DESC LIMIT ?',
                                 (device_id,limit+1)).fetchall()
            return {'items':[json.loads(row[0]) for row in rows[:limit]],'truncated':len(rows)>limit}

    def record_probe(self, principal, device_id, status, *, observed_at, valid_until,
                     probe_id='default', source_label=None):
        self._authorize(principal,'probe_write')
        if type(status) is not str or status not in ('healthy','unhealthy','unknown'):
            raise HubError('invalid_probe_status')
        observed, valid = _timestamp(observed_at), _timestamp(valid_until)
        probe_id = _slug(probe_id)
        label = _text(probe_id if source_label is None else source_label,100)
        recorded=self._now()
        if valid<=observed or observed>recorded:
            raise HubError('invalid_infrastructure_time')
        item=dict(probe_id=probe_id,source_label=label,status=status,
                  observed_at=observed.isoformat(),valid_until=valid.isoformat(),recorded_at=recorded.isoformat())
        with self._transaction() as db:
            device=self._device(db,device_id)
            if device['retired']:
                raise HubError('device_retired',409)
            rows=db.execute('SELECT sequence,data FROM infra_probes WHERE device_id=? LIMIT ?',
                            (device_id,MAX_PROBES_PER_DEVICE+1)).fetchall()
            if len(rows)>MAX_PROBES_PER_DEVICE:
                raise HubError('probe_history_limit',409)
            previous=[json.loads(row['data']) for row in rows]
            if probe_id not in {entry.get('probe_id','default') for entry in previous} and len(
                    {entry.get('probe_id','default') for entry in previous})>=MAX_PROBE_IDENTITIES:
                raise HubError('probe_identity_limit',409)
            same=[entry for entry in previous if entry.get('probe_id','default')==probe_id]
            if same and observed<=max(_timestamp(entry['observed_at']) for entry in same):
                raise HubError('probe_observation_out_of_order',409)
            db.execute('INSERT INTO infra_probes(device_id,principal,data) VALUES(?,?,?)',
                       (device_id,_dump(self._principal(principal)),_dump(item)))
            # The newly accepted sample is strictly newer than every retained
            # sample for THIS probe. Delete only excess older raw telemetry;
            # keep the current sample/monotonic guard and every other probe.
            older=sorted(((_timestamp(entry['observed_at']),row['sequence'])
                          for row,entry in zip(rows,previous)
                          if entry.get('probe_id','default')==probe_id),reverse=True)
            for _,sequence in older[MAX_PROBE_SAMPLES-1:]:
                db.execute('DELETE FROM infra_probes WHERE device_id=? AND sequence=?',(device_id,sequence))
            return item

    def probe_state(self, principal, device_id, *, probe_id='default'):
        self._authorize(principal,'read')
        probe_id=_slug(probe_id)
        with self._reading():
            self._device(self.db,device_id)
            rows=self.db.execute('SELECT data FROM infra_probes WHERE device_id=? ORDER BY sequence DESC LIMIT ?',
                                 (device_id,MAX_PROBES_PER_DEVICE+1)).fetchall()
            unknown={'probe_id':probe_id,'source_label':None,'status':'unknown','observed_at':None,'valid_until':None}
            if len(rows)>MAX_PROBES_PER_DEVICE:
                return unknown
            try:
                # Observation time chooses the current value; arrival order is
                # never freshness. Malformed matching history fails unknown.
                matching=[json.loads(row[0]) for row in rows
                          if json.loads(row[0]).get('probe_id','default')==probe_id]
                if not matching:
                    return unknown
                item=max(matching,key=lambda value:_timestamp(value['observed_at']))
                observed=_timestamp(item['observed_at']); valid=_timestamp(item['valid_until'])
                recorded=_timestamp(item['recorded_at'])
                now=self._now()
                if (item['status'] not in ('healthy','unhealthy','unknown') or valid<=observed
                        or observed>now or observed>recorded or recorded>now):
                    raise ValueError()
                return {**item,'status':'stale' if valid<=now else item['status']}
            except (KeyError,TypeError,ValueError,AttributeError,HubError):
                return unknown

    def probe_states(self, principal, device_id):
        self._authorize(principal,'read')
        with self._reading():
            self._device(self.db,device_id)
            rows=self.db.execute('SELECT data FROM infra_probes WHERE device_id=? LIMIT ?',
                                 (device_id,MAX_PROBES_PER_DEVICE+1)).fetchall()
            if len(rows)>MAX_PROBES_PER_DEVICE:
                raise HubError('probe_history_limit',409)
            try:
                identities=sorted({_slug(json.loads(row[0]).get('probe_id','default')) for row in rows})
            except (ValueError,TypeError,AttributeError,HubError):
                raise HubError('invalid_probe_history',409) from None
            if len(identities)>MAX_PROBE_IDENTITIES:
                raise HubError('probe_identity_limit',409)
            return {'items':[self.probe_state(principal,device_id,probe_id=identity) for identity in identities]}

    def close(self):
        with self.lock:
            self.db.close()
