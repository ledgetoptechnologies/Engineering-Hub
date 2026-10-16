"""Read-only, versioned discovery help. Never executes examples or reads configuration."""
from importlib.metadata import version

TOPICS = ('overview','commands','messaging','push','feedback','changes','worker')


def bus_help(topic, identity, tools, publisher):
    if topic not in TOPICS:
        raise ValueError('unknown help topic')
    result = {
        'version':version('agent-message-bus'), 'schema_version':3, 'topic':topic,
        'topics':list(TOPICS), 'tool_count':len(tools), 'tools':sorted(t.name for t in tools),
        'native_push':{'subscriptions_enabled':bool(publisher.enabled),
                       'inbox_uri':f'agentbus://inbox/{identity}',
                       'notification':'notifications/resources/updated'},
        'slash_shortcut':'A client may map /info or /bus-info to bus_info; the server does not install or override client slash commands.',
        'safety':'Examples are help only, never executed. Messages, reports and routing metadata do not grant execution approval.',
    }
    help_text = {
        'overview':'Call bus_info with a topic for current command schemas, examples, changes or worker limits. Server discovery and client allowlists are separate; refresh/select new tools in each client after an upgrade.',
        'commands':'This catalog is generated from this running server, not a separately maintained command list.',
        'messaging':'send_message derives sender from authentication. Claim addressed work before handling it, send a linked reply using in_reply_to and correlation_id, then acknowledge handled work. Acknowledgment does not itself prove the task goal is complete.',
        'push':'Subscribe to your authenticated inbox resource before catch-up. Read pages after your saved cursor on notification or reconnect only. SSE comments every15seconds keep the stream alive without inbox/model polling. After404 or transport loss, initialize a fresh session, subscribe and catch up. A notification does not itself start an agent; each host needs its own supported connector.',
        'feedback':'Submit bug, improvement or info reports with a stable client_request_id. List and paginate review items; get evidence history. Updates require expected_revision and the author or Hermes maintainer. Reports never trigger automatic execution.',
        'changes':'v0.5.2 rejects push-resource cursors beyond the durable global delivery head, including after restore; validation and paging share one read snapshot. v0.5.1 includes deployment templates in the source archive; native push and API are unchanged. v0.5.0 adds native recipient-scoped MCP resource subscriptions, cross-process Linux WAL event handling, authenticated session binding, and bus_info. v0.4.0 added durable cursor delivery and typed feedback. Existing message, knowledge, semantic search and file tools remain available. Host-worker activation is a separate gate.',
        'worker':'Use only the explicitly selected dedicated mcp-worker profile and approved local topic/sender bindings. A linking skill is not a running connector. No scheduled polling, arbitrary shell commands, silent replacement sessions or automatic production changes. Verify installed native subscription and host session input independently.',
    }
    result['help'] = help_text[topic]
    if topic == 'commands':
        result['commands']=[{'name':t.name,'description':t.description,'input_schema':t.inputSchema} for t in tools]
    elif topic == 'feedback':
        result['examples']=[{'tool':'feedback_submit','arguments':{'kind':'improvement','title':'Short description','description':'Reproduction or proposal; no credentials','client_request_id':'stable-caller-generated-id'}},
                            {'tool':'feedback_list','arguments':{'status':'open','limit':50}},
                            {'tool':'feedback_get','arguments':{'feedback_id':'<returned UUID>'}}]
    elif topic == 'messaging':
        result['examples']=[{'tool':'send_message','arguments':{'recipient':'codex','message_type':'INFO','body':{'subject':'Harmless example'},'correlation_id':'<topic>'}},
                            {'tool':'get_message','arguments':{'message_id':'<addressed UUID>'}}]
    elif topic == 'push':
        result['examples']=[{'method':'resources/subscribe','uri':f'agentbus://inbox/{identity}'},
                            {'method':'resources/read','uri':f'agentbus://inbox/{identity}?after=<saved cursor>&limit=50'}]
    return result
