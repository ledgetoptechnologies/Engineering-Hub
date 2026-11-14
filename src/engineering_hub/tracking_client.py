"""Explicit, foreground project-tracking MCP client for an enrolled EHS session.

Task content comes from standard input, not process arguments. This client does
not enroll, grant rights, create agent turns, execute work or verify evidence.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_message_bus.agent_session_client import ClientError
from engineering_hub.attention_client import _call, _profile, _installation_profile
from engineering_hub import tracking_outbox

_MAX_INPUT = 65536
_TOOLS = {
    'list': ('hub_tracking_task_list', {'project'}, {'limit', 'after_task_id', 'unfinished'}),
    'propose': ('hub_tracking_task_propose', {'project', 'title', 'client_request_id'},
                {'details', 'links', 'origin_request_id'}),
    'update': ('hub_tracking_task_update', {'task_id', 'expected_revision', 'client_request_id', 'progress', 'evidence_links'},
               {'completion_claim'}),
    'withdraw-claim': ('hub_tracking_claim_withdraw', {'task_id', 'expected_revision', 'expected_claim_revision',
                                                       'client_request_id', 'reason'}, set()),
    'review': ('hub_tracking_task_review', {'task_id', 'expected_revision', 'client_request_id', 'verdict', 'note', 'evidence_links'},
               set()),
    'history': ('hub_tracking_task_history', {'task_id', 'kind'}, {'before'}),
    'origin': ('hub_tracking_task_origin', {'task_id'}, set()),
    'request-summary': ('hub_orchestration_request_origin_task_summary', {'project', 'request_id'},
                        {'limit', 'after'}),
}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unknown arguments, which could accidentally
        # contain private task content or a credential.
        raise ClientError('invalid_tracking_request')


def _arguments(action, value):
    if action not in _TOOLS or type(value) is not dict:
        raise ClientError('invalid_tracking_request')
    tool, required, optional = _TOOLS[action]
    if not required <= value.keys() or value.keys() - required - optional:
        raise ClientError('invalid_tracking_request')
    return tool, value


async def invoke(session, installation, session_id, action, arguments):
    """Invoke one explicitly selected operation after exact session verification."""
    tool, arguments = _arguments(action, arguments)
    identity = await _call(session, 'session_identity', {})
    if identity.get('installation_id') != installation or identity.get('session_id') != session_id:
        raise ClientError('tracking_session_mismatch')
    result = await _call(session, tool, arguments)
    return {'type': 'tracking_result', 'trust': 'untrusted_hub_data', 'operation': action,
            'execution_authorization': False, 'result': result}


async def _run(args, arguments, *, binding=None, action=None):
    origin, token, installation, session_id, expires = _profile(
        args.profile_dir.expanduser().absolute(), args.session, args.private_vpn)
    if binding is not None and (binding['origin'] != origin or binding['installation_id'] != installation):
        raise ClientError('tracking_outbox_binding_mismatch')
    headers = {'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'}
    async with asyncio.timeout(max(1, min(60, expires - time.time()))):
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(15),
                                     follow_redirects=False, trust_env=False) as http:
            async with streamable_http_client(origin + '/mcp', http_client=http, terminate_on_close=True) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=15)
                    return await invoke(session, installation, session_id, action or args.action, arguments)


def main(argv=None):
    parser = _Parser(description='Explicit project tracking; JSON operation arguments on stdin.')
    parser.add_argument('--profile-dir', type=Path, required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--private-vpn', action='store_true')
    parser.add_argument('--outbox-dir', type=Path)
    parser.add_argument('action', choices=sorted((*_TOOLS, 'stage', 'send-staged', 'list-staged', 'inspect-staged')))
    try:
        args = parser.parse_args(argv)
        raw = sys.stdin.buffer.read(_MAX_INPUT + 1)
        if len(raw) > _MAX_INPUT:
            raise ClientError('invalid_tracking_request')
        arguments = json.loads(raw)
        if args.action in ('stage', 'send-staged', 'list-staged', 'inspect-staged'):
            if args.outbox_dir is None or type(arguments) is not dict:
                raise ClientError('invalid_tracking_request')
            if args.action == 'list-staged':
                if arguments.keys() - {'limit', 'after'}:
                    raise ClientError('invalid_tracking_request')
                page = tracking_outbox.reference_page(args.outbox_dir, limit=arguments.get('limit', 1000),
                                                      after=arguments.get('after'))
                result = {'type': 'tracking_outbox_references', 'record_ids': page['record_ids']}
                # Preserve the original {}/small-outbox response shape.
                if arguments or page['next_after'] is not None:
                    result['next_after'] = page['next_after']
            elif args.action == 'stage':
                if set(arguments) != {'action', 'arguments'} or arguments['action'] not in ('propose', 'update', 'review', 'withdraw-claim'):
                    raise ClientError('invalid_tracking_request')
                _arguments(arguments['action'], arguments['arguments'])
                origin, installation = _installation_profile(
                    args.profile_dir.expanduser().absolute(), args.private_vpn)
                record_id = tracking_outbox.stage(args.outbox_dir, origin, installation, None,
                                                   arguments['action'], arguments['arguments'])
                result = {'type': 'tracking_outbox_staged', 'record_id': record_id,
                          'sent': False, 'execution_authorization': False}
            else:
                if set(arguments) != {'record_id'}:
                    raise ClientError('invalid_tracking_request')
                record = tracking_outbox.read(args.outbox_dir, arguments['record_id'])
                if record['action'] not in ('propose', 'update', 'review', 'withdraw-claim'):
                    raise ClientError('invalid_tracking_request')
                _arguments(record['action'], record['arguments'])
                if args.action == 'inspect-staged':
                    result = {'type': 'tracking_outbox_record', 'trust': 'private_local_data',
                              'execution_authorization': False, 'record': record}
                else:
                    result = asyncio.run(_run(args, record['arguments'], binding=record, action=record['action']))
        else:
            if args.outbox_dir is not None:
                raise ClientError('invalid_tracking_request')
            _arguments(args.action, arguments)
            result = asyncio.run(_run(args, arguments))
        print(json.dumps(result, ensure_ascii=True, separators=(',', ':')))
    except (Exception, KeyboardInterrupt):
        # A lost response may follow a commit. Retry a write only with its same
        # client_request_id and unchanged payload; never print peer/secret data.
        print('tracking_operation_unconfirmed; check or retry with the same request id', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
