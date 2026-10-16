"""Read-only, versioned discovery help. Never executes examples or reads configuration."""
from importlib.metadata import version

TOPICS = ('overview', 'commands', 'messaging', 'push', 'presence', 'recovery', 'feedback', 'changes', 'worker')


def bus_help(topic, identity, tools, publisher):
    if topic not in TOPICS:
        raise ValueError('unknown help topic')
    result = {
        'version': version('agent-message-bus'), 'schema_version': 4, 'topic': topic,
        'topics': list(TOPICS), 'tool_count': len(tools), 'tools': sorted(t.name for t in tools),
        'native_push': {'subscriptions_enabled': bool(publisher.enabled),
                        'inbox_uri': f'agentbus://inbox/{identity}',
                        'notification': 'notifications/resources/updated'},
        'features': {'sender_scoped_send_idempotency': True, 'sender_receipts': True,
                     'server_observed_presence': True},
        'slash_shortcut': 'A client may map /info or /bus-info to bus_info; the server does not install or override client slash commands.',
        'safety': 'Examples are help only, never executed. Messages, reports and routing metadata do not grant execution approval.',
    }
    help_text = {
        'overview': 'Call bus_info with a topic for current command schemas, examples, recovery, presence or selected-conversation limits. Refresh/select new tools in each client after an upgrade; server discovery does not refresh a client allowlist.',
        'commands': 'This catalog is generated from this running server, not a separately maintained command list.',
        'messaging': 'send_message derives sender from authentication. Optional client_request_id deduplicates identical send acceptance per sender, not task execution. Keys are exact ASCII [A-Za-z0-9][A-Za-z0-9._:-]{0,127}; never trim or change them on retry. Claim addressed work before handling it, reply using in_reply_to and correlation_id, then acknowledge handled work. Acknowledgment does not prove the task goal is complete.',
        'push': 'Subscribe to your authenticated inbox resource before catch-up. Read pages after your saved cursor on notification or reconnect only. SSE comments every15seconds keep the stream alive without inbox/model polling. After404 or transport loss, initialize a fresh session, subscribe and catch up. A notification does not itself start an agent; each host needs its own supported connector.',
        'presence': 'presence_status reports active authenticated GET SSE streams and registered inbox subscriptions intersecting those streams, on this server process only. Subscribed is not ready, delivered, read, task completion or approval. No session IDs, heartbeat writes, model calls or database polling. Native source failure makes subscribed false; connection observations remain separate.',
        'recovery': 'After an UNKNOWN keyed send, query get_send_receipt with the same key. A retry must use the same key AND identical resolved payload; changed payload conflicts (REST409 / MCP send_conflict). A pruned original is never recreated (REST410 / MCP send_replay_unavailable); its metadata receipt remains. Keys have no automatic expiry. Keyless legacy sends have no new receipt or deduplication guarantee. Never blindly retry a keyless UNKNOWN send, borrow recipient credentials, or generate a new key to hide ambiguity.',
        'feedback': 'Submit bug, improvement or info reports with a stable client_request_id. List and paginate review items; get evidence history. Updates require expected_revision and the author or Hermes maintainer. Reports never trigger automatic execution.',
        'changes': 'v0.6.0 adds sender-scoped atomic send idempotency, private metadata receipts, server-observed SSE/subscription presence, and presence/recovery help (24 tools, schema4). v0.5.2 rejected future push cursors in one snapshot. v0.5.1 fixed source-archive deployment templates. Existing keyless sends, native push, knowledge, semantic search, files and feedback remain compatible. Host session input and execution authorization are separate gates.',
        'worker': 'Use only the owner-selected topic conversation and approved native host integration. The unused mcp-worker profile was deleted at Beau\'s request; do not recreate it or silently substitute another/latest session. A topic record is not a running connector or authorization. No scheduled polling, arbitrary shell commands or automatic production changes. Verify native subscription and exact host session input independently.',
    }
    result['help'] = help_text[topic]
    if topic == 'commands':
        result['commands'] = [{'name': t.name, 'description': t.description, 'input_schema': t.inputSchema} for t in tools]
    elif topic == 'feedback':
        result['examples'] = [
            {'tool': 'feedback_submit', 'arguments': {'kind': 'improvement', 'title': 'Short description', 'description': 'Reproduction or proposal; no credentials', 'client_request_id': 'stable-caller-generated-id'}},
            {'tool': 'feedback_list', 'arguments': {'status': 'open', 'limit': 50}},
            {'tool': 'feedback_get', 'arguments': {'feedback_id': '<returned UUID>'}},
        ]
    elif topic == 'messaging':
        result['examples'] = [
            {'tool': 'send_message', 'arguments': {'recipient': 'codex', 'message_type': 'INFO', 'body': {'subject': 'Harmless example'}, 'correlation_id': '<topic>', 'client_request_id': 'caller-generated-unique-id'}},
            {'tool': 'get_message', 'arguments': {'message_id': '<addressed UUID>'}},
        ]
    elif topic == 'push':
        result['examples'] = [
            {'method': 'resources/subscribe', 'uri': f'agentbus://inbox/{identity}'},
            {'method': 'resources/read', 'uri': f'agentbus://inbox/{identity}?after=<saved cursor>&limit=50'},
        ]
    elif topic == 'presence':
        result['examples'] = [{'tool': 'presence_status', 'arguments': {}}]
    elif topic == 'recovery':
        result['examples'] = [{'tool': 'get_send_receipt', 'arguments': {'client_request_id': 'caller-generated-unique-id'}}]
    return result
