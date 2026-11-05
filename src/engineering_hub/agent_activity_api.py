"""Human view of bus routing: counts, ages and metadata, never message bodies.

Topic links are inferred from messages sent in the window. They show who has
been talking on a topic, not that a session is bound to it or reachable.
"""
from datetime import datetime, timedelta, timezone
import re
from typing import Annotated, Literal

from fastapi import Request, Response
from pydantic import Field

from .dto import ResponseDTO
from .repository import HubError

WINDOW_HOURS = 24
MAX_TOPICS = 200
_TOPIC = re.compile(r'[^\x00-\x1f\x7f]{1,128}')
_TYPE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}')
Agent = Literal['hermes', 'codex', 'claude']
Topic = Annotated[str, Field(max_length=128)]
Stamp = Annotated[str, Field(max_length=64)]


class AgentActivityItem(ResponseDTO):
    agent: Agent
    pending: int = Field(ge=0)
    claimed: int = Field(ge=0)
    oldest_pending_at: Stamp | None


class TopicActivityItem(ResponseDTO):
    # None collects messages with no usable body.topic, which no topic listener can route.
    topic: Topic | None
    agents: list[Agent] = Field(max_length=3)
    messages: int = Field(ge=0)
    pending: int = Field(ge=0)
    claimed: int = Field(ge=0)
    pending_by_agent: dict[Agent, int]
    claimed_by_agent: dict[Agent, int]
    oldest_pending_at: Stamp | None
    last_activity_at: Stamp | None


class OpenMessageItem(ResponseDTO):
    id: Annotated[str, Field(max_length=64)]
    sender: Agent
    recipient: Agent
    message_type: Annotated[str, Field(max_length=64)]
    priority: Literal['low', 'normal', 'high', 'urgent']
    status: Literal['pending', 'claimed']
    topic: Topic | None
    created_at: Stamp


class AgentActivityDTO(ResponseDTO):
    observed_at: Stamp
    window_hours: int
    agents: list[AgentActivityItem] = Field(max_length=3)
    topics: list[TopicActivityItem] = Field(max_length=MAX_TOPICS + 1)
    open: list[OpenMessageItem] = Field(max_length=100)
    open_total: int = Field(ge=0)


def _topic(value):
    return value if isinstance(value, str) and value.strip() and _TOPIC.fullmatch(value) else None


def _earliest(current, value):
    return value if current is None or value < current else current


def project_activity(raw, *, observed_at, window_hours=WINDOW_HOURS):
    """Fold repository rows into the response. Unusable topics join the unrouted row."""
    agents = {name: dict(agent=name, pending=0, claimed=0, oldest_pending_at=None)
              for name in ('hermes', 'codex', 'claude')}
    topics = {}

    def entry(name):
        return topics.setdefault(name, dict(topic=name, agents=set(), messages=0, pending=0, claimed=0, pending_by_agent={},
                                            claimed_by_agent={},
                                            oldest_pending_at=None, last_activity_at=None))

    for row in raw['open_counts']:
        item, agent = entry(_topic(row['topic'])), agents[row['recipient']]
        item['agents'].add(row['recipient'])
        for target in (item, agent):
            target[row['status']] += row['total']
            if row['status'] == 'pending':
                target['oldest_pending_at'] = _earliest(target['oldest_pending_at'], row['oldest'])
        held = item[row['status'] + '_by_agent']
        held[row['recipient']] = held.get(row['recipient'], 0) + row['total']
    for row in raw['links']:
        item = entry(_topic(row['topic']))
        item['agents'].update((row['sender'], row['recipient']))
        item['messages'] += row['total']
        item['last_activity_at'] = max(item['last_activity_at'] or '', row['latest'])
    ordered = sorted(topics.values(), key=lambda item: (-item['pending'], -item['claimed'], item['topic'] or ''))
    # Busy topics sort first, so the cap only ever drops quiet ones.
    kept = [item for item in ordered if item['topic'] is None] + [
        item for item in ordered if item['topic'] is not None][:MAX_TOPICS]
    return dict(observed_at=observed_at, window_hours=window_hours, agents=list(agents.values()),
                topics=[{**item, 'agents': sorted(item['agents'])} for item in kept],
                open=[{**row, 'topic': _topic(row['topic']),
                       'message_type': row['message_type'] if _TYPE.fullmatch(row['message_type']) else 'other'}
                      for row in raw['open']],
                open_total=sum(row['total'] for row in raw['open_counts']))


def attach_agent_activity_route(app, *, bus_service, human):
    @app.get('/api/hub/v1/agents/activity', response_model=AgentActivityDTO)
    def agent_activity(request: Request, response: Response):
        human(request)
        if bus_service is None:
            raise HubError('agent_activity_unavailable', 503)
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=WINDOW_HOURS)).isoformat().replace('+00:00', 'Z')
        response.headers['Cache-Control'] = 'no-store'
        return project_activity(bus_service.repository.routing_activity(since=since),
                                observed_at=now.isoformat().replace('+00:00', 'Z'))
