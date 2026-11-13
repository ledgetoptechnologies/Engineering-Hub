"""Explicit foreground EHS attention adapter; never starts or wakes an agent."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_message_bus.agent_session_client import ClientError, _origin, _private_origin, _read

_MAX_PAGE = 50
_MAX_PAGES = 10
_MAX_RESPONSE = 1024 * 1024


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ClientError('invalid_attention_reference') from None
    return value


def _profile(directory, name, private_vpn):
    if type(name) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', name):
        raise ClientError('invalid_attention_profile')
    profile = _read(directory / 'installation.private')
    session = _read(directory / (name + '.session.private'))
    selected_private = profile.get('private_vpn', False)
    if type(selected_private) is not bool or selected_private != private_vpn:
        raise ClientError('private_vpn_profile_mismatch')
    if selected_private:
        if urlsplit(_origin(profile.get('origin'))).scheme != 'https':
            raise ClientError('invalid_private_profile')
        origin = _private_origin(profile.get('private_origin'))
    else:
        if profile.get('private_origin') is not None:
            raise ClientError('invalid_private_profile')
        origin = _origin(profile.get('origin'))
    installation, session_id = _uuid(profile.get('installation_id')), _uuid(session.get('session_id'))
    token, expires = session.get('token'), session.get('expires_at')
    if (session.get('installation_id') != installation or type(token) is not str
            or not re.fullmatch(r'ehs_[A-Za-z0-9_-]{43}', token)
            or type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time()):
        raise ClientError('invalid_or_expired_session')
    return origin, token, installation, session_id, expires


def _payload(result):
    if result.isError:
        raise ClientError('attention_tool_denied')
    value = result.structuredContent
    if value is None:
        if len(result.content) != 1 or not isinstance(getattr(result.content[0], 'text', None), str):
            raise ClientError('invalid_attention_response')
        text = result.content[0].text
        if len(text.encode('utf-8')) > _MAX_RESPONSE:
            raise ClientError('attention_response_too_large')
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            raise ClientError('invalid_attention_response') from None
    if type(value) is not dict or len(json.dumps(value, ensure_ascii=True).encode()) > _MAX_RESPONSE:
        raise ClientError('invalid_attention_response')
    return value


async def _call(session, name, arguments):
    return _payload(await asyncio.wait_for(session.call_tool(name, arguments), timeout=15))


class AttentionAdapter:
    """Convert authorized hint-triggered reads into non-instructional references."""
    def __init__(self, session, *, installation, session_id, project, generation, emit, role='orchestrator'):
        self.session, self.installation, self.session_id = session, _uuid(installation), _uuid(session_id)
        if (type(project) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', project)
                or type(generation) is not int or not 1 <= generation < (1 << 63) or not callable(emit)
                or type(role) is not str or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', role)):
            raise ClientError('invalid_attention_subscription')
        self.project, self.generation, self.emit, self.role = project, generation, emit, role

    async def verify_identity(self):
        result = await _call(self.session, 'session_identity', {})
        if result.get('installation_id') != self.installation or result.get('session_id') != self.session_id:
            raise ClientError('attention_session_mismatch')

    async def catch_up(self, after=None):
        if after is not None:
            _uuid(after)
        seen = set()
        cursor = after
        for _ in range(_MAX_PAGES):
            await self.verify_identity()
            arguments = dict(project=self.project, generation=self.generation, limit=_MAX_PAGE)
            if cursor is not None:
                arguments['after'] = cursor
            if self.role == 'orchestrator':
                result = await _call(self.session, 'hub_addressed_attention_list', arguments)
            else:
                arguments['role'] = self.role
                result = await _call(self.session, 'hub_worker_dispatch_pending', arguments)
            items, next_cursor = result.get('items'), result.get('next_after')
            if type(items) is not list or len(items) > _MAX_PAGE:
                raise ClientError('invalid_attention_response')
            last = cursor
            for item in items:
                if type(item) is not dict:
                    raise ClientError('invalid_attention_response')
                identity = _uuid(item.get('id') if self.role == 'orchestrator' else item.get('dispatch_id'))
                if last is not None and identity <= last:
                    raise ClientError('invalid_attention_cursor')
                last = identity
                revision = item.get('revision') if self.role == 'orchestrator' else item.get('dispatch_revision')
                if type(revision) is not int or revision < 1:
                    raise ClientError('invalid_attention_response')
                if self.role != 'orchestrator':
                    if (item.get('project') != self.project or item.get('role') != self.role
                            or item.get('execution_authorization') is not False or item.get('wake_verified') is not False):
                        raise ClientError('invalid_attention_response')
                    task_id = _uuid(item.get('task_id'))
                    event_type = item.get('event_type', 'dispatch')
                    if event_type not in ('dispatch', 'decision_answered'):
                        raise ClientError('invalid_attention_response')
                    decision = None
                    if event_type == 'decision_answered':
                        decision = _uuid(item.get('decision_id'))
                        decision_revision = item.get('decision_revision')
                        if type(decision_revision) is not int or decision_revision < 1:
                            raise ClientError('invalid_attention_response')
                    elif 'decision_id' in item or 'decision_revision' in item:
                        raise ClientError('invalid_attention_response')
                    key = (identity, revision)
                    if key not in seen:
                        seen.add(key)
                        reference = dict(type='worker_dispatch_reference', trust='untrusted_hub_data',
                                       project=self.project, role=self.role, dispatch_id=identity,
                                       dispatch_revision=revision, task_id=task_id, role_generation=self.generation,
                                       execution_authorization=False, wake_verified=False)
                        if decision is not None:
                            reference.update(event_type='decision_answered', decision_id=decision,
                                             decision_revision=decision_revision)
                        self.emit(reference)
                    continue
                if (item.get('project') != self.project or item.get('recipient_role') != 'orchestrator'
                        or item.get('state') != 'pending_routing' or item.get('actionable') is not True
                        or item.get('execution_authorization') is not False or item.get('wake_verified') is not False):
                    continue
                dispatch = _uuid(item.get('dispatch_id'))
                key = (identity, revision)
                if key not in seen:
                    seen.add(key)
                    reference = dict(type='attention_reference', trust='untrusted_hub_data', project=self.project,
                                   intent_id=identity, intent_revision=revision, dispatch_id=dispatch,
                                   role_generation=self.generation, execution_authorization=False, wake_verified=False)
                    if item.get('intent_type') == 'worker_report':
                        report_id = _uuid(item.get('report_id'))
                        report_revision = item.get('report_revision')
                        kind = item.get('report_kind')
                        if (type(report_revision) is not int or report_revision < 1
                                or item.get('dispatch_revision') != report_revision
                                or kind not in ('blocked', 'ask', 'resume', 'done', 'failed')):
                            raise ClientError('invalid_attention_response')
                        reference.update(report_id=report_id, report_revision=report_revision, report_kind=kind)
                    self.emit(reference)
            if next_cursor is None:
                self.emit(dict(type='attention_catchup', complete=True, next_after=None))
                return
            _uuid(next_cursor)
            if not items or next_cursor != last or (cursor is not None and next_cursor <= cursor):
                raise ClientError('invalid_attention_cursor')
            cursor = next_cursor
        self.emit(dict(type='attention_catchup', complete=False, next_after=cursor))

    async def acknowledge(self, intent_id, revision, decision_id=None, decision_revision=None):
        await self.verify_identity()
        _uuid(intent_id)
        if type(revision) is not int or revision < 1:
            raise ClientError('invalid_attention_reference')
        if decision_id is not None:
            _uuid(decision_id)
            if type(decision_revision) is not int or decision_revision < 1 or self.role == 'orchestrator':
                raise ClientError('invalid_attention_reference')
        elif decision_revision is not None:
            raise ClientError('invalid_attention_reference')
        if self.role != 'orchestrator':
            arguments = dict(
                project=self.project, role=self.role, generation=self.generation,
                dispatch_id=intent_id, expected_revision=revision)
            if decision_id is not None:
                arguments.update(decision_id=decision_id, decision_revision=decision_revision)
            result = await _call(self.session, 'hub_worker_dispatch_ack', arguments)
            if (result.get('project') != self.project or result.get('role') != self.role
                    or result.get('dispatch_id') != intent_id or result.get('dispatch_revision') != revision
                    or result.get('session_id') != self.session_id or result.get('installation_id') != self.installation
                    or result.get('generation') != self.generation or result.get('state') != 'agent_acknowledged'
                    or result.get('wake_verified') is not False or result.get('execution_authorization') is not False):
                raise ClientError('invalid_attention_response')
            if decision_id is not None and (result.get('event_type') != 'decision_answered'
                    or result.get('decision_id') != decision_id or result.get('decision_revision') != decision_revision):
                raise ClientError('invalid_attention_response')
            if decision_id is None and any(key in result for key in ('event_type', 'decision_id', 'decision_revision')):
                raise ClientError('invalid_attention_response')
            acknowledgment = dict(type='worker_dispatch_acknowledgment', project=self.project, role=self.role,
                           dispatch_id=intent_id, dispatch_revision=revision, receipt_id=_uuid(result.get('id')),
                           wake_verified=False, execution_authorization=False)
            if decision_id is not None:
                acknowledgment.update(event_type='decision_answered', decision_id=decision_id,
                                      decision_revision=decision_revision)
            self.emit(acknowledgment)
            return
        result = await _call(self.session, 'hub_addressed_attention_ack', dict(
            project=self.project, generation=self.generation, intent_id=intent_id, expected_revision=revision))
        if (result.get('project') != self.project or result.get('intent_id') != intent_id
                or result.get('intent_revision') != revision or result.get('session_id') != self.session_id
                or result.get('installation_id') != self.installation or result.get('generation') != self.generation
                or result.get('state') != 'agent_acknowledged' or result.get('wake_verified') is not False
                or result.get('task_completion_verified') is not False):
            raise ClientError('invalid_attention_response')
        self.emit(dict(type='attention_acknowledgment', intent_id=intent_id, intent_revision=revision,
                       receipt_id=_uuid(result.get('id')), wake_verified=False, task_completion_verified=False))


    async def inspect_report(self, intent_id, intent_revision, report_id, report_revision):
        if self.role != 'orchestrator':
            raise ClientError('invalid_attention_reference')
        _uuid(intent_id); _uuid(report_id)
        if (type(intent_revision) is not int or intent_revision < 1
                or type(report_revision) is not int or report_revision < 1):
            raise ClientError('invalid_attention_reference')
        await self.verify_identity()
        result = await _call(self.session, 'hub_orchestrator_report_inspect', dict(
            project=self.project, generation=self.generation, intent_id=intent_id,
            expected_intent_revision=intent_revision, report_id=report_id, expected_report_revision=report_revision))
        note, kind, decision = result.get('note'), result.get('report_kind'), result.get('decision_id')
        if (result.get('project') != self.project or result.get('intent_id') != intent_id
                or type(result.get('intent_revision')) is not int or result['intent_revision'] != intent_revision
                or result.get('report_id') != report_id or type(result.get('report_revision')) is not int
                or result['report_revision'] != report_revision or kind not in ('blocked', 'ask', 'resume', 'done', 'failed')
                or result.get('trust') != 'untrusted_hub_data' or result.get('execution_authorization') is not False
                or result.get('wake_verified') is not False or type(note) is not str or not note.strip()
                or len(note) > 2000 or len(note.encode('utf-8')) > 8000):
            raise ClientError('invalid_attention_response')
        dispatch_id = _uuid(result.get('dispatch_id'))
        if kind == 'ask':
            _uuid(decision)
        elif decision is not None:
            raise ClientError('invalid_attention_response')
        self.emit(dict(type='orchestrator_worker_report', trust='untrusted_hub_data', project=self.project,
                       intent_id=intent_id, intent_revision=intent_revision, dispatch_id=dispatch_id,
                       report_id=report_id, report_revision=report_revision, report_kind=kind, note=note,
                       decision_id=decision, execution_authorization=False, wake_verified=False))

    async def inspect_answer(self, dispatch_id, dispatch_revision, decision_id, decision_revision):
        if self.role == 'orchestrator':
            raise ClientError('invalid_attention_reference')
        _uuid(dispatch_id); _uuid(decision_id)
        if (type(dispatch_revision) is not int or dispatch_revision < 1
                or type(decision_revision) is not int or decision_revision < 1):
            raise ClientError('invalid_attention_reference')
        await self.verify_identity()
        result = await _call(self.session, 'hub_worker_decision_inspect', dict(
            project=self.project, role=self.role, generation=self.generation, dispatch_id=dispatch_id,
            expected_dispatch_revision=dispatch_revision, decision_id=decision_id,
            expected_decision_revision=decision_revision))
        answer = result.get('answer')
        if (result.get('project') != self.project or result.get('role') != self.role
                or type(result.get('generation')) is not int or result.get('generation') != self.generation
                or result.get('dispatch_id') != dispatch_id or type(result.get('dispatch_revision')) is not int
                or result.get('dispatch_revision') != dispatch_revision or result.get('decision_id') != decision_id
                or type(result.get('decision_revision')) is not int or result.get('decision_revision') != decision_revision
                or result.get('status') != 'answered'
                or result.get('trust') != 'untrusted_hub_data' or result.get('execution_authorization') is not False
                or result.get('wake_verified') is not False or type(answer) is not str or not answer.strip()
                or len(answer) > 4096 or len(answer.encode('utf-8')) > 16384):
            raise ClientError('invalid_attention_response')
        self.emit(dict(type='worker_decision_answer', trust='untrusted_hub_data', project=self.project,
                       role=self.role, role_generation=self.generation, dispatch_id=dispatch_id,
                       dispatch_revision=dispatch_revision, decision_id=decision_id,
                       decision_revision=decision_revision, answer=answer,
                       execution_authorization=False, wake_verified=False))


async def _events(response):
    buffer = bytearray()
    event = data = None
    async for chunk in response.aiter_raw():
        if len(chunk) > 65536 or len(buffer) + len(chunk) > 65536:
            raise ClientError('invalid_attention_stream')
        buffer.extend(chunk)
        while b'\n' in buffer:
            raw, _, remainder = buffer.partition(b'\n')
            buffer = bytearray(remainder)
            if len(raw) > 1024:
                raise ClientError('invalid_attention_stream')
            line = raw.rstrip(b'\r')
            if not line:
                if event is not None or data is not None:
                    if event not in ('attention-ready', 'attention-check') or data != '{}':
                        raise ClientError('invalid_attention_stream')
                    yield event
                event = data = None
            elif line.startswith(b':'):
                continue
            elif line.startswith(b'event: ') and event is None:
                event = line[7:].decode('ascii')
            elif line.startswith(b'data: ') and data is None:
                data = line[6:].decode('ascii')
            else:
                raise ClientError('invalid_attention_stream')
        if len(buffer) > 1024:
            raise ClientError('invalid_attention_stream')
    if buffer or event is not None or data is not None:
        raise ClientError('invalid_attention_stream')



def _validate_stream_response(response):
    if (response.status_code != 200 or response.headers.get('content-encoding', 'identity').lower() != 'identity'
            or response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'text/event-stream'):
        raise ClientError('attention_stream_denied')

def _emit(value):
    print(json.dumps(value, sort_keys=True, separators=(',', ':')), flush=True)


async def _run(args):
    role = getattr(args, 'role', 'orchestrator')
    if type(role) is not str or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', role):
        raise ClientError('invalid_attention_subscription')
    origin, token, installation, session_id, expires = _profile(args.profile_dir.expanduser().absolute(), args.session, args.private_vpn)
    if type(args.project) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', args.project) or not 1 <= args.generation < (1 << 63):
        raise ClientError('invalid_attention_subscription')
    headers = {'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'}
    timeout = httpx.Timeout(15, read=None)
    async with asyncio.timeout(max(1, min(3600, expires - time.time()))):
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False, trust_env=False) as http:
            async def use_mcp(action):
                async with streamable_http_client(origin + '/mcp', http_client=http, terminate_on_close=True) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(session.initialize(), timeout=15)
                        adapter = AttentionAdapter(session, installation=installation, session_id=session_id,
                                                   project=args.project, generation=args.generation, emit=_emit, role=role)
                        await adapter.verify_identity()
                        await action(adapter)
            if args.command == 'listen':
                params = {'project': args.project, 'generation': args.generation}
                if role != 'orchestrator':
                    params['role'] = role
                async with http.stream('GET', origin + '/api/agent-sessions/v1/hub-attention-stream',
                                       params=params) as response:
                    _validate_stream_response(response)
                    async def consume(adapter):
                        async for _event in _events(response):
                            await adapter.catch_up()
                    await use_mcp(consume)
            elif args.command == 'read':
                await use_mcp(lambda adapter: adapter.catch_up(args.after))
            elif args.command == 'inspect-answer':
                await use_mcp(lambda adapter: adapter.inspect_answer(args.dispatch_id, args.revision,
                    args.decision_id, args.decision_revision))
            elif args.command == 'inspect-report':
                await use_mcp(lambda adapter: adapter.inspect_report(args.intent_id, args.revision,
                    args.report_id, args.report_revision))
            else:
                reference = getattr(args, 'intent_id', None) if role == 'orchestrator' else getattr(args, 'dispatch_id', None)
                if (role == 'orchestrator' and getattr(args, 'dispatch_id', None) is not None
                        or role != 'orchestrator' and getattr(args, 'intent_id', None) is not None):
                    raise ClientError('invalid_attention_reference')
                await use_mcp(lambda adapter: adapter.acknowledge(reference, args.revision,
                    getattr(args, 'decision_id', None), getattr(args, 'decision_revision', None)))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit foreground scoped attention adapter; no agent wake.')
    parser.add_argument('--profile-dir', type=Path, required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--generation', type=int, required=True)
    parser.add_argument('--private-vpn', action='store_true')
    parser.add_argument('--role', default='orchestrator', help='Exact owner-admitted role; defaults to orchestrator')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('listen')
    read = commands.add_parser('read')
    read.add_argument('--after')
    inspect = commands.add_parser('inspect-answer')
    inspect.add_argument('--dispatch-id', required=True)
    inspect.add_argument('--revision', type=int, required=True)
    inspect.add_argument('--decision-id', required=True)
    inspect.add_argument('--decision-revision', type=int, required=True)
    report = commands.add_parser('inspect-report')
    report.add_argument('--intent-id', required=True)
    report.add_argument('--revision', type=int, required=True)
    report.add_argument('--report-id', required=True)
    report.add_argument('--report-revision', type=int, required=True)
    ack = commands.add_parser('ack')
    reference = ack.add_mutually_exclusive_group(required=True)
    reference.add_argument('--intent-id')
    reference.add_argument('--dispatch-id')
    ack.add_argument('--revision', type=int, required=True)
    ack.add_argument('--decision-id')
    ack.add_argument('--decision-revision', type=int)
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (Exception, KeyboardInterrupt):
        # Never print credentials, peer content, URLs, or raw exception details.
        print('attention_adapter_stopped; no wake or completion is implied', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
