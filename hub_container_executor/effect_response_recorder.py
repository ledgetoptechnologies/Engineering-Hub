"""Opt-in same-process durable daemon response evidence; not a recovery authority.

The journal must be explicitly migrated to v3 while quiescent. The recorder
persists a keyed request fingerprint before a POST and a sanitized keyed response
projection before returning it to backend logic. A crash before recording a
response remains unknown; an accepted HTTP status is not proof of host state.
No request body, credential, daemon error, or probe output is persisted.
"""
import hmac
import json
import os
import re
from .effect_mac_key import EffectMacKey
from .operation_journal import SQLiteOperationJournal


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


class DurableEffectResponseRecorder:
    def __init__(self, journal, key):
        if (type(journal) is not SQLiteOperationJournal or type(key) is not EffectMacKey
                or key._directory / key._journal_name != journal.path
                or key._journal_identity != journal._owner._database_identity):
            raise ValueError('exact owned journal and key required')
        with journal._transaction() as db:
            if db.execute('PRAGMA user_version').fetchone() != (3,):
                raise ValueError('explicit response journal migration required')
        self.journal, self.key, self._pid, self._closed = journal, key, os.getpid(), False

    def _active(self):
        if self._closed or self._pid != os.getpid():
            raise ValueError('response recorder unavailable')

    def begin(self, operation_id, step, *, route, params, body):
        self._active()
        if type(route) is not str or not route.startswith('/') or len(route) > 1024:
            raise ValueError('invalid response request projection')
        value = {'domain':'engineering-hub.effect-http-request.v1','operation_id':operation_id,
                 'step':step,'method':'POST','route':route,'params':params,'body':body}
        request_mac = self.key.mac(_canonical(value))
        self.journal.begin_response(operation_id, step, request_mac)
        return request_mac

    @staticmethod
    def _response_value(step, status, raw):
        if type(raw) is not bytes or len(raw) > 1024*1024 or type(status) is not int:
            raise ValueError('invalid daemon response')
        if step in ('create',) or step.startswith('probe-create:'):
            if status != 201: raise ValueError('invalid daemon response')
            def unique(pairs):
                result={}
                for key,value in pairs:
                    if key in result: raise ValueError()
                    result[key]=value
                return result
            value=json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(value) is not dict or type(value.get('Id')) is not str or not re.fullmatch(r'[a-f0-9]{64}',value['Id']):
                raise ValueError('invalid daemon response')
            if step == 'create' and 'Warnings' in value:
                warnings = value['Warnings']
                if warnings is not None and (type(warnings) is not list or any(type(item) is not str for item in warnings)):
                    raise ValueError('invalid daemon response')
            return value['Id']
        if step in ('start','stop'):
            if status != 204 or raw: raise ValueError('invalid daemon response')
            return None
        if step.startswith('probe-start:') and status == 200:
            return None  # Output stays opaque; it is not a recoverable health result.
        raise ValueError('invalid daemon response')

    def complete(self, operation_id, step, request_mac, *, status, raw):
        self._active()
        safe = self._response_value(step,status,raw)
        raw_mac = self.key.mac(b'engineering-hub.effect-http-raw-response.v1\x00' + raw)
        value = {'domain':'engineering-hub.effect-http-response.v1','operation_id':operation_id,
                 'step':step,'request_mac':request_mac,'status':status,'safe_value':safe,'raw_mac':raw_mac}
        response_mac = self.key.mac(_canonical(value))
        self.journal.record_response(operation_id,step,request_mac,status,response_mac,safe,raw_mac)

    def inspect(self, operation_id):
        """Verify stored projections; never convert them into an effect outcome."""
        self._active()
        rows = self.journal.inspect_responses(operation_id)
        for op,step,request_mac,state,status,response_mac,safe,raw_mac,_kind in rows:
            if state == 'received':
                expected = self.key.mac(_canonical({'domain':'engineering-hub.effect-http-response.v1',
                    'operation_id':op,'step':step,'request_mac':request_mac,'status':status,'safe_value':safe,'raw_mac':raw_mac}))
                if not hmac.compare_digest(expected,response_mac):
                    raise ValueError('response evidence unavailable')
        return rows

    def matches_raw_response(self, operation_id, step, raw):
        """Compare externally retained bytes, without treating them as an outcome."""
        if type(raw) is not bytes or len(raw) > 1024 * 1024:
            raise ValueError('response evidence unavailable')
        rows = [row for row in self.inspect(operation_id) if row[1] == step]
        if len(rows) != 1 or rows[0][3] != 'received':
            raise ValueError('response evidence unavailable')
        actual = self.key.mac(b'engineering-hub.effect-http-raw-response.v1\x00' + raw)
        return hmac.compare_digest(actual, rows[0][7])

    def close(self):
        self._closed = True
