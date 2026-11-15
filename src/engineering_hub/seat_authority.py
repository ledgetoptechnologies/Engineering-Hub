"""Opt-in, owner-managed EHS seat/handoff substrate; no routing or effects."""
import json
import re
import sqlite3
import uuid

from agent_message_bus import session_hub_context as context
from agent_message_bus.agent_sessions import AgentSessionError
from .repository import HubError

_SLUG = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')
_NAMESPACE = uuid.UUID('20980b5e-00e5-43fa-9113-969a73516ef3')


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (TypeError, ValueError, AttributeError):
        raise HubError('seat_unavailable', 404) from None
    return value


def _slug(value):
    if type(value) is not str or not _SLUG.fullmatch(value):
        raise HubError('invalid_seat_input', 422)
    return value


class SeatAuthority:
    """Only these EHS capabilities are fenced; legacy/effects remain unchanged."""
    def __init__(self, bridge):
        self.bridge, self.repo, self.sessions = bridge, bridge.repo, bridge.sessions
        with self.repo._transaction() as db:
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS hub_cross_seat_session ON hub_entities "
                       "(json_extract(data,'$.session_id')) WHERE kind='cross_seat' "
                       "AND json_extract(data,'$.session_id') IS NOT NULL")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS hub_cross_seat_role ON hub_entities "
                       "(json_extract(data,'$.project'),json_extract(data,'$.topic'),json_extract(data,'$.role')) "
                       "WHERE kind='cross_seat'")

    def _owner(self, actor):
        self.bridge._owned()
        self.sessions._owner(actor)

    def _target(self, db, session_id, project):
        now = self.sessions._now()
        row = db.execute('SELECT * FROM agent_sessions WHERE id=?', (_uuid(session_id),)).fetchone()
        if row is None or row['revoked_at'] is not None or row['expires_at'] <= now:
            raise HubError('seat_target_unavailable', 404)
        try:
            installation = self.sessions._installation(db, row['installation_id'])
            self.sessions._active_installation(installation, now)
        except AgentSessionError:
            raise HubError('seat_target_unavailable', 404) from None
        grant = self.bridge._grant(db, row['installation_id'])
        if (grant is None or grant['revoked_at'] is not None or grant['expires_at'] <= now
                or project not in json.loads(grant['projects'])
                or not {'cross.seat.read', 'cross.seat.handoff'} <= set(json.loads(grant['capabilities']))):
            raise HubError('seat_target_unavailable', 404)
        return row['installation_id']

    def create_topic(self, actor, project, topic, name):
        self._owner(actor); _slug(project); _slug(topic)
        name = self.repo._text(name, 200)
        identity = str(uuid.uuid5(_NAMESPACE, 'topic:' + project + ':' + topic))
        with self.repo._transaction() as db:
            self.repo._project(project, db)
            if db.execute("SELECT 1 FROM hub_entities WHERE id=?", (identity,)).fetchone():
                raise HubError('seat_topic_exists', 409)
            if db.execute("SELECT count(*) FROM hub_entities WHERE kind='cross_seat_topic'").fetchone()[0] >= 4096:
                raise HubError('seat_capacity', 409)
            return self.repo._save(db, actor, 'seat_topic.created', 'cross_seat_topic',
                dict(id=identity, project=project, topic=topic, name=name), new=True, publish=False)

    def create_seat(self, actor, project, topic, role):
        self._owner(actor); _slug(project); _slug(topic); _slug(role)
        topic_id = str(uuid.uuid5(_NAMESPACE, 'topic:' + project + ':' + topic))
        with self.repo._transaction() as db:
            self.repo._project(project, db)
            self.repo._get(db, topic_id, 'cross_seat_topic')
            if db.execute("SELECT count(*) FROM hub_entities WHERE kind='cross_seat'").fetchone()[0] >= 4096:
                raise HubError('seat_capacity', 409)
            try:
                return self.repo._save(db, actor, 'seat.created', 'cross_seat', dict(id=str(uuid.uuid4()),
                    project=project, topic=topic, role=role, installation_id=None, session_id=None,
                    generation=0, handoff_revision=0, handoff='', coordination_only=True,
                    execution_authorization=False), new=True, publish=False)
            except sqlite3.IntegrityError:
                raise HubError('seat_exists', 409) from None

    def bind(self, actor, seat_id, expected_revision, session_id):
        """Explicit owner assignment/transfer; no automatic stale takeover."""
        self._owner(actor); _uuid(seat_id); _uuid(session_id)
        with self.sessions._transaction() as sessions:
            with self.repo._transaction(before_commit=lambda db: self._target(sessions, session_id, item['project'])) as db:
                item = self.repo._get(db, seat_id, 'cross_seat')
                self.repo._project(item['project'], db)
                self.repo._cas(item, expected_revision)
                if item['session_id'] is not None:
                    old = sessions.execute('SELECT installation_id FROM agent_sessions WHERE id=?',
                                           (item['session_id'],)).fetchone()
                    if old is None or old['installation_id'] != item['installation_id']:
                        raise HubError('seat_binding_unavailable', 409)
                installation = self._target(sessions, session_id, item['project'])
                if item['session_id'] == session_id:
                    raise HubError('seat_already_bound', 409)
                if type(item['generation']) is not int or not 0 <= item['generation'] < 2**63 - 1:
                    raise HubError('seat_generation_unavailable', 409)
                item.update(session_id=session_id, installation_id=installation, generation=item['generation'] + 1)
                try:
                    return self.repo._save(db, actor, 'seat.bound', 'cross_seat', item, publish=False)
                except sqlite3.IntegrityError:
                    raise HubError('seat_session_already_bound', 409) from None

    def owner_list(self, actor, project, kind, limit, after):
        self._owner(actor); _slug(project)
        if kind not in ('cross_seat', 'cross_seat_topic') or type(limit) is not int or not 1 <= limit <= 100:
            raise HubError('invalid_seat_input', 422)
        if after is not None:
            _uuid(after)
        with self.repo._connection() as db:
            db.execute('BEGIN')
            self.repo._project(project, db)
            rows = db.execute("SELECT data FROM hub_entities WHERE kind=? AND json_extract(data,'$.project')=? "
                              "AND id>? ORDER BY id LIMIT ?", (kind, project, after or '', limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows[:limit]]
            return {'items': items, 'next_after': items[-1]['id'] if len(rows) > limit else None}

    def unbind(self, actor, seat_id, expected_revision):
        """Explicit owner revocation; no automatic takeover or routing."""
        self._owner(actor); _uuid(seat_id)
        with self.sessions._transaction():
            with self.repo._transaction() as db:
                item = self.repo._get(db, seat_id, 'cross_seat')
                self.repo._project(item['project'], db)
                self.repo._cas(item, expected_revision)
                if item['session_id'] is None:
                    raise HubError('seat_already_unbound', 409)
                if type(item['generation']) is not int or not 0 <= item['generation'] < 2**63 - 1:
                    raise HubError('seat_generation_unavailable', 409)
                item.update(session_id=None, installation_id=None, generation=item['generation'] + 1)
                return self.repo._save(db, actor, 'seat.unbound', 'cross_seat', item, publish=False)

    def _current(self, db, actor):
        operation = context.current.get()
        if operation is None or not context.actor_allowed(actor):
            raise HubError('seat_unavailable', 404)
        row = db.execute("SELECT data FROM hub_entities WHERE kind='cross_seat' "
                         "AND json_extract(data,'$.session_id')=?", (operation.principal.session_id,)).fetchone()
        if row is None:
            raise HubError('seat_unavailable', 404)
        item = json.loads(row['data'])
        if item['installation_id'] != operation.principal.installation_id:
            raise HubError('seat_unavailable', 404)
        try:
            self.repo._project(item['project'], db)
        except HubError:
            raise HubError('seat_unavailable', 404) from None
        return item

    def current(self, actor):
        with self.repo._transaction() as db:
            return self._current(db, actor)

    def recheck_current(self, actor, expected):
        """Revalidate the full projected snapshot immediately before response."""
        with self.repo._transaction() as db:
            if self._current(db, actor) != expected:
                raise HubError('seat_unavailable', 404)

    def update_handoff(self, actor, generation, expected_revision, text):
        text = self.repo._text(text, 4096)
        with self.repo._transaction() as db:
            item = self._current(db, actor)
            if type(generation) is not int or generation != item['generation']:
                raise HubError('seat_unavailable', 404)
            self.repo._cas(item, expected_revision)
            item.update(handoff=text, handoff_revision=item['handoff_revision'] + 1)
            item = self.repo._save(db, actor, 'seat.handoff', 'cross_seat', item, publish=False)
            return {'id': item['id'], 'revision': item['revision'], 'generation': generation,
                    'response_scope': 'mutation_receipt', 'execution_authorization': False}
