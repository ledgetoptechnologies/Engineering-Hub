"""Request-local trusted Hub admission, inert for legacy operations.

The bridge owns the context. Repository commit hooks run inside its session
database writer guard; neither actor strings nor request parameters grant it.
"""
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class HubSessionPrincipal:
    installation_id: str
    session_id: str
    family: str
    expires_at: float
    authorization_revision: int

    @property
    def actor(self):
        return 'installation:' + self.installation_id


current = ContextVar('hub_session_operation', default=None)


def actor_allowed(actor):
    operation = current.get()
    return operation is not None and actor == operation.principal.actor


def check_project(project):
    operation = current.get()
    if operation is not None:
        operation.project(project)


def before_commit(storage_path, db, **metadata):
    operation = current.get()
    if operation is not None:
        operation.before_commit(storage_path, db, **metadata)


def provenance(db, sequence):
    operation = current.get()
    if operation is not None:
        p = operation.principal
        db.execute('INSERT INTO hub_session_provenance VALUES(?,?,?,?,?,?)',
                   (sequence, p.installation_id, p.session_id, p.family,
                    p.authorization_revision, operation.capability))


def claim_fields(actor):
    operation = current.get()
    if operation is None or actor != operation.principal.actor:
        return {}
    p = operation.principal
    return dict(owner_family=p.family, owner_session_id=p.session_id)


def continuation_allowed(item, actor):
    if item.get('owner_session_id') is None:
        return not str(item.get('owner', '')).startswith('installation:')
    operation = current.get()
    return (operation is not None and actor == operation.principal.actor
            and item['owner_session_id'] == operation.principal.session_id)


def family(actor):
    operation = current.get()
    return operation.principal.family if actor_allowed(actor) else actor
