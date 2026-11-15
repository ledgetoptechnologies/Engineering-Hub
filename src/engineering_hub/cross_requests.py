"""Opt-in, explicitly shared cross-project request records; no delivery or work."""
import hashlib
import json
import uuid

from agent_message_bus import session_hub_context as context
from .repository import HubError, _json
from .seat_authority import _slug, _uuid

_MAX_REQUESTS = 4096
_MAX_MESSAGES = 256
_MAX_TOTAL_MESSAGES = 8192


def _key(value):
    if type(value) is not str or not value.strip() or len(value) > 128:
        raise HubError('invalid_cross_request', 422)
    return value.strip()


def _fingerprint(*value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class CrossRequests:
    def __init__(self, bridge):
        self.bridge, self.repo, self.seats = bridge, bridge.repo, bridge.seat_authority
        if self.seats is None:
            raise ValueError('cross_requests_require_seat_foundation')
        with self.repo._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS hub_cross_rate_clock (singleton INTEGER PRIMARY KEY CHECK(singleton=1), last REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS hub_cross_rate_events (actor TEXT NOT NULL, action TEXT NOT NULL, at REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS hub_cross_rate_actor ON hub_cross_rate_events(actor,action,at)')

    def _owner(self, actor):
        self.bridge._owned(); self.bridge.sessions._owner(actor)

    @staticmethod
    def _active_project(db, project):
        # Deliberately do not call context.check_project for the other side.
        row = db.execute('SELECT data FROM hub_projects WHERE id=?', (project,)).fetchone()
        if row is None:
            raise HubError('cross_request_unavailable', 404)
        try:
            if json.loads(row['data']).get('archived') is not False:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise HubError('cross_request_unavailable', 404) from None

    def _route(self, db, route_id):
        try:
            route = self.repo._get(db, _uuid(route_id), 'cross_route')
            if route['active'] is not True:
                raise ValueError()
            self._active_project(db, route['source_project'])
            self._active_project(db, route['target_project'])
            return route
        except (HubError, ValueError, KeyError, TypeError):
            raise HubError('cross_request_unavailable', 404) from None

    def _seat(self, db, actor, generation):
        seat = self.seats._current(db, actor)
        if type(generation) is not int or generation != seat['generation']:
            raise HubError('cross_request_unavailable', 404)
        return seat

    @staticmethod
    def _side(seat, route, request=None):
        if (seat['project'] == route['source_project'] and
                (request is None or route['share_with_source_project'] is True or seat['id'] == request['source_seat_id'])):
            return 'source'
        if (seat['project'] == route['target_project'] and seat['topic'] == route['target_topic']
                and seat['id'] in route['participants']):
            return 'target'
        raise HubError('cross_request_unavailable', 404)

    def _request(self, db, identity, seat):
        try:
            item = self.repo._get(db, _uuid(identity), 'cross_request')
            route = self._route(db, item['route_id'])
            if (item['source_project'] != route['source_project'] or item['target_project'] != route['target_project']
                    or item['target_topic'] != route['target_topic']):
                raise ValueError()
            return item, route, self._side(seat, route, item)
        except (HubError, KeyError, TypeError, ValueError):
            raise HubError('cross_request_unavailable', 404) from None

    @staticmethod
    def _receipt(db, actor, key, fingerprint):
        row = db.execute('SELECT * FROM hub_requests WHERE actor=? AND key=?', (actor, key)).fetchone()
        if row is not None and row['fingerprint'] != fingerprint:
            raise HubError('idempotency_conflict', 409)
        return row['entity_id'] if row else None

    @staticmethod
    def _save_receipt(db, actor, key, fingerprint, identity):
        db.execute('INSERT INTO hub_requests VALUES(?,?,?,?)', (actor, key, fingerprint, identity))

    @staticmethod
    def _mutation(identity):
        return {'id': identity, 'response_scope': 'mutation_receipt', 'current_state_included': False,
                'execution_authorization': False}

    def _rate(self, db, actor, action, maximum):
        now = self.bridge.sessions._now()  # finite, nonnegative, bounded
        previous = db.execute('SELECT last FROM hub_cross_rate_clock WHERE singleton=1').fetchone()
        if previous is not None and now < previous['last']:
            raise HubError('cross_request_clock_untrusted', 503)
        count = db.execute('SELECT count(*) FROM hub_cross_rate_events WHERE actor=? AND action=? AND at>?',
                           (actor, action, now - 60)).fetchone()[0]
        if count >= maximum:
            raise HubError('cross_request_rate_limited', 429)
        db.execute('INSERT INTO hub_cross_rate_clock VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET last=excluded.last', (now,))
        db.execute('INSERT INTO hub_cross_rate_events VALUES(?,?,?)', (actor, action, now))

    @staticmethod
    def _capacity(db, kind, field, value, maximum):
        # Field names are fixed internally, never supplied by the request.
        count = db.execute("SELECT count(*) FROM hub_entities WHERE kind=? AND json_extract(data,'$." + field + "')=?",
                           (kind, value)).fetchone()[0]
        if count >= maximum:
            raise HubError('cross_request_capacity', 409)

    @staticmethod
    def _actor_capacity(db, actor, action, maximum):
        count = db.execute('SELECT count(*) FROM hub_audit WHERE actor=? AND action=?', (actor, action)).fetchone()[0]
        if count >= maximum:
            raise HubError('cross_request_capacity', 409)

    def set_route(self, actor, source_project, target_project, target_topic, participants, triager, active,
                  share_with_source_project, expected_revision):
        self._owner(actor)
        _slug(source_project); _slug(target_project); _slug(target_topic)
        if (source_project == target_project or type(active) is not bool or type(share_with_source_project) is not bool
                or type(expected_revision) is not int
                or expected_revision < 0 or type(participants) is not list or not 1 <= len(participants) <= 16
                or any(type(value) is not str for value in participants)
                or len(set(participants)) != len(participants)):
            raise HubError('invalid_cross_route', 422)
        for value in participants: _uuid(value)
        if triager not in participants:
            raise HubError('invalid_cross_route', 422)
        identity = str(uuid.uuid5(uuid.UUID('22494af0-f193-4580-b3fc-878a1d60cb24'),
                                  _json([source_project, target_project, target_topic])))
        with self.repo._transaction() as db:
            self.repo._project(source_project, db); self.repo._project(target_project, db)
            for seat_id in participants:
                seat = self.repo._get(db, seat_id, 'cross_seat')
                if seat['project'] != target_project or seat['topic'] != target_topic:
                    raise HubError('invalid_cross_route', 422)
            row = db.execute("SELECT data FROM hub_entities WHERE id=?", (identity,)).fetchone()
            if row is None:
                if expected_revision != 0:
                    raise HubError('revision_conflict', 409)
                if db.execute("SELECT count(*) FROM hub_entities WHERE kind='cross_route'").fetchone()[0] >= 4096:
                    raise HubError('cross_request_capacity', 409)
                item = dict(id=identity, source_project=source_project, target_project=target_project,
                            target_topic=target_topic, participants=sorted(participants), triager=triager, active=active,
                            share_with_source_project=share_with_source_project)
                return self.repo._save(db, actor, 'cross_route.created', 'cross_route', item, new=True, publish=False)
            item = self.repo._get(db, identity, 'cross_route'); self.repo._cas(item, expected_revision)
            item.update(participants=sorted(participants), triager=triager, active=active,
                        share_with_source_project=share_with_source_project)
            return self.repo._save(db, actor, 'cross_route.updated', 'cross_route', item, publish=False)

    def routes(self, actor, generation, limit, after):
        if type(limit) is not int or not 1 <= limit <= 50: raise HubError('invalid_limit', 422)
        if after is not None: _uuid(after)
        with self.repo._transaction() as db:
            seat = self._seat(db, actor, generation)
            if db.execute("SELECT count(*) FROM hub_entities WHERE kind='cross_route'").fetchone()[0] > 4096:
                raise HubError('cross_request_capacity', 409)
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='cross_route' "
                              "AND json_extract(data,'$.source_project')=? AND id>? ORDER BY id",
                              (seat['project'], after or ''))
            visible = []
            for row in rows:
                item = json.loads(row['data'])
                try: route = self._route(db, item['id'])
                except HubError as error:
                    if error.code != 'cross_request_unavailable': raise
                    continue
                visible.append({key: route[key] for key in ('id', 'target_project', 'target_topic', 'revision')})
                if len(visible) > limit: break
            return {'items': visible[:limit], 'next_after': visible[limit - 1]['id'] if len(visible) > limit else None}

    def create(self, actor, generation, route_id, title, details, links, client_request_id):
        title = self.repo._text(title, 200); key = _key(client_request_id)
        content = self.repo._context(details, links)
        fingerprint = _fingerprint('cross.create', route_id, generation, title, content)
        with self.repo._transaction() as db:
            seat = self._seat(db, actor, generation); route = self._route(db, route_id)
            if self._side(seat, route) != 'source':
                raise HubError('cross_request_unavailable', 404)
            existing = self._receipt(db, actor, key, fingerprint)
            if existing is not None:
                self._request(db, existing, seat)
                return self._mutation(existing)
            if db.execute("SELECT count(*) FROM hub_entities WHERE kind='cross_request'").fetchone()[0] >= _MAX_REQUESTS:
                raise HubError('cross_request_capacity', 409)
            self._capacity(db, 'cross_request', 'source_project', route['source_project'], 512)
            self._capacity(db, 'cross_request', 'target_project', route['target_project'], 512)
            self._capacity(db, 'cross_request', 'route_id', route['id'], 256)
            self._actor_capacity(db, actor, 'cross_request.created', 128)
            self._rate(db, actor, 'cross_request.created', 20)
            item = dict(id=str(uuid.uuid4()), route_id=route['id'], source_project=route['source_project'],
                        source_seat_id=seat['id'], source_generation=seat['generation'],
                        target_project=route['target_project'], target_topic=route['target_topic'],
                        title=title, status='open', conversation_id=str(uuid.uuid4()),
                        delivery='not_attempted', execution_authorization=False, **content)
            item = self.repo._save(db, actor, 'cross_request.created', 'cross_request', item, new=True, publish=False)
            self._save_receipt(db, actor, key, fingerprint, item['id'])
            return self._mutation(item['id'])

    def get(self, actor, generation, identity):
        with self.repo._transaction() as db:
            seat = self._seat(db, actor, generation)
            item, _, _ = self._request(db, identity, seat)
            return item

    def list(self, actor, generation, limit, after):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise HubError('invalid_limit', 422)
        if after is not None: _uuid(after)
        with self.repo._transaction() as db:
            seat = self._seat(db, actor, generation)
            if db.execute("SELECT count(*) FROM hub_entities WHERE kind='cross_request'").fetchone()[0] > _MAX_REQUESTS:
                raise HubError('cross_request_capacity', 409)
            # Admission caps the entire request population; filtering precedes pagination.
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='cross_request' AND id>? ORDER BY id",
                              (after or '',))
            visible = []
            for row in rows:
                item = json.loads(row['data'])
                try:
                    _, _, _ = self._request(db, item['id'], seat)
                except HubError as error:
                    if error.code != 'cross_request_unavailable': raise
                    continue
                visible.append(item)
                if len(visible) > limit: break
            return {'items': visible[:limit], 'next_after': visible[limit - 1]['id'] if len(visible) > limit else None}

    def transition(self, actor, generation, identity, expected_revision, status, client_request_id):
        if status not in ('accepted', 'declined', 'done'):
            raise HubError('invalid_cross_request', 422)
        key = _key(client_request_id); fingerprint = _fingerprint('cross.transition', identity, generation, expected_revision, status)
        with self.repo._transaction() as db:
            seat = self._seat(db, actor, generation); item, route, side = self._request(db, identity, seat)
            if side != 'target' or seat['id'] != route['triager']:
                raise HubError('cross_request_unavailable', 404)
            existing = self._receipt(db, actor, key, fingerprint)
            if existing is not None:
                if existing != item['id']: raise HubError('idempotency_conflict', 409)
                return self._mutation(existing)
            self.repo._cas(item, expected_revision)
            if status not in ({'accepted', 'declined'} if item['status'] == 'open' else {'done'} if item['status'] == 'accepted' else set()):
                raise HubError('invalid_cross_transition', 409)
            item['status'] = status
            self.repo._save(db, actor, 'cross_request.' + status, 'cross_request', item, publish=False)
            self._save_receipt(db, actor, key, fingerprint, item['id'])
            return self._mutation(item['id'])

    def append(self, actor, generation, identity, text, client_request_id):
        text = self.repo._text(text, 4096); key = _key(client_request_id)
        try:
            if len(text.encode('utf-8')) > 4096: raise ValueError()
        except (UnicodeError, ValueError):
            raise HubError('invalid_cross_request', 422) from None
        fingerprint = _fingerprint('cross.message', identity, generation, text)
        with self.repo._transaction() as db:
            seat = self._seat(db, actor, generation); item, _, side = self._request(db, identity, seat)
            existing = self._receipt(db, actor, key, fingerprint)
            if existing is not None:
                message = self.repo._get(db, existing, 'cross_message')
                if message['request_id'] != item['id']: raise HubError('idempotency_conflict', 409)
                return self._mutation(existing)
            count = db.execute("SELECT count(*) FROM hub_entities WHERE kind='cross_message' AND "
                               "json_extract(data,'$.request_id')=?", (item['id'],)).fetchone()[0]
            if count >= _MAX_MESSAGES: raise HubError('cross_request_capacity', 409)
            if db.execute("SELECT count(*) FROM hub_entities WHERE kind='cross_message'").fetchone()[0] >= _MAX_TOTAL_MESSAGES:
                raise HubError('cross_request_capacity', 409)
            self._capacity(db, 'cross_message', 'source_project', item['source_project'], 1024)
            self._capacity(db, 'cross_message', 'target_project', item['target_project'], 1024)
            self._capacity(db, 'cross_message', 'route_id', item['route_id'], 512)
            self._actor_capacity(db, actor, 'cross_message.created', 256)
            self._rate(db, actor, 'cross_message.created', 60)
            message = self.repo._save(db, actor, 'cross_message.created', 'cross_message',
                dict(id=str(uuid.uuid4()), request_id=item['id'], author_seat_id=seat['id'], side=side,
                     source_project=item['source_project'], target_project=item['target_project'], route_id=item['route_id'],
                     sequence=count + 1, text=text, execution_authorization=False), new=True, publish=False)
            self._save_receipt(db, actor, key, fingerprint, message['id'])
            return self._mutation(message['id'])

    def messages(self, actor, generation, identity, limit, after):
        if type(limit) is not int or not 1 <= limit <= 50: raise HubError('invalid_limit', 422)
        if after is not None and (type(after) is not int or not 0 <= after <= _MAX_MESSAGES):
            raise HubError('invalid_cursor', 422)
        with self.repo._transaction() as db:
            seat = self._seat(db, actor, generation); item, _, _ = self._request(db, identity, seat)
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='cross_message' AND "
                              "json_extract(data,'$.request_id')=? AND json_extract(data,'$.sequence')>? "
                              "ORDER BY CAST(json_extract(data,'$.sequence') AS INTEGER) LIMIT ?",
                              (item['id'], after or 0, limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows[:limit]]
            return {'items': items, 'next_after': items[-1]['sequence'] if len(rows) > limit else None}

    def owner_transition(self, actor, identity, expected_revision, status, client_request_id):
        self._owner(actor)
        if status not in ('accepted', 'declined', 'done', 'expired'):
            raise HubError('invalid_cross_request', 422)
        key = _key(client_request_id)
        fingerprint = _fingerprint('cross.owner_transition', identity, expected_revision, status)
        with self.repo._transaction() as db:
            item = self.repo._get(db, _uuid(identity), 'cross_request')
            self._route(db, item['route_id'])
            existing = self._receipt(db, actor, key, fingerprint)
            if existing is not None:
                if existing != item['id']: raise HubError('idempotency_conflict', 409)
                return self._mutation(existing)
            self.repo._cas(item, expected_revision)
            allowed = ({'accepted', 'declined', 'expired'} if item['status'] == 'open'
                       else {'done', 'expired'} if item['status'] == 'accepted' else set())
            if status not in allowed:
                raise HubError('invalid_cross_transition', 409)
            item['status'] = status
            self.repo._save(db, actor, 'cross_request.' + status, 'cross_request', item, publish=False)
            self._save_receipt(db, actor, key, fingerprint, item['id'])
            return self._mutation(item['id'])

    def owner_records(self, actor, project, direction, limit, after):
        """Owner's bounded history includes retained records on revoked routes."""
        self._owner(actor); _slug(project)
        if direction not in ('inbound', 'outbound') or type(limit) is not int or not 1 <= limit <= 50:
            raise HubError('invalid_cross_request', 422)
        if after is not None: _uuid(after)
        column = 'target_project' if direction == 'inbound' else 'source_project'
        with self.repo._connection() as db:
            db.execute('BEGIN'); self.repo._project(project, db, require_active=False)
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='cross_request' AND "
                "json_extract(data,'$." + column + "')=? AND id>? ORDER BY id LIMIT ?",
                (project, after or '', limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows[:limit]]
            return {'items': items, 'next_after': items[-1]['id'] if len(rows) > limit else None}

    def owner_routes(self, actor, project, limit, after):
        self._owner(actor); _slug(project)
        if type(limit) is not int or not 1 <= limit <= 50: raise HubError('invalid_limit', 422)
        if after is not None: _uuid(after)
        with self.repo._connection() as db:
            db.execute('BEGIN'); self.repo._project(project, db, require_active=False)
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='cross_route' "
                              "AND json_extract(data,'$.source_project')=? AND id>? ORDER BY id LIMIT ?",
                              (project, after or '', limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows[:limit]]
            return {'items': items, 'next_after': items[-1]['id'] if len(rows) > limit else None}

    def owner_get(self, actor, identity):
        self._owner(actor)
        with self.repo._connection() as db:
            return self.repo._get(db, _uuid(identity), 'cross_request')

    def owner_messages(self, actor, identity, limit, after):
        self._owner(actor)
        if type(limit) is not int or not 1 <= limit <= 50: raise HubError('invalid_limit', 422)
        if after is not None and (type(after) is not int or not 0 <= after <= _MAX_MESSAGES):
            raise HubError('invalid_cursor', 422)
        with self.repo._connection() as db:
            db.execute('BEGIN'); self.repo._get(db, _uuid(identity), 'cross_request')
            rows = db.execute("SELECT data FROM hub_entities WHERE kind='cross_message' AND "
                "json_extract(data,'$.request_id')=? AND json_extract(data,'$.sequence')>? "
                "ORDER BY CAST(json_extract(data,'$.sequence') AS INTEGER) LIMIT ?",
                (identity, after or 0, limit + 1)).fetchall()
            items = [json.loads(row['data']) for row in rows[:limit]]
            return {'items': items, 'next_after': items[-1]['sequence'] if len(rows) > limit else None}
