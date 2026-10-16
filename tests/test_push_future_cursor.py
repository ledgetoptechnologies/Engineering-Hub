"""Push resource cursor validation must match durable global sequence semantics."""
import pytest
from contextlib import closing
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl
from tests.test_mcp_push import TOKENS, live_app, loopback, mcp_client, resource_json


@pytest.mark.asyncio
@pytest.mark.parametrize('existing_messages', [0, 1])
async def test_resource_rejects_future_cursor_without_breaking_session(tmp_path, monkeypatch, existing_messages):
    monkeypatch.setenv('AGENT_BUS_ALLOWED_HOSTS', '127.0.0.1:*')
    app = live_app(tmp_path)
    repo = app.state.repository
    for _ in range(existing_messages):
        repo.insert(sender='hermes', recipient='claude', message_type='INFO', body={})
    head = repo.delivery_head()
    # Retain the fixture's initial WAL until the event source has opened its
    # own anchors; garbage collection must not unlink it during server startup.
    # WAL replacement/fail-closed behavior is covered by the native-event tests.
    with closing(repo.connect()) as keeper:
        keeper.execute('SELECT COUNT(*) FROM messages').fetchone()
        async with loopback(app) as url, mcp_client(url, TOKENS.codex_token) as client:
            session, _, _, _ = client
            for after in [head + 1, 999999]:
                with pytest.raises(McpError, match='after exceeds current delivery sequence head'):
                    await session.read_resource(AnyUrl(f'agentbus://inbox/codex?after={after}&limit=3'))
            # Global head remains valid even if Codex has never received a message.
            page = resource_json(await session.read_resource(AnyUrl(f'agentbus://inbox/codex?after={head}&limit=3')))
            assert page == {'messages': [], 'next_cursor': head}
            msg = repo.insert(sender='hermes', recipient='codex', message_type='INFO', body={'fixture': True})
            page = resource_json(await session.read_resource(AnyUrl(f'agentbus://inbox/codex?after={head}&limit=3')))
            assert [m['id'] for m in page['messages']] == [msg.id]
            assert page['next_cursor'] == repo.sequence_for(msg.id)
