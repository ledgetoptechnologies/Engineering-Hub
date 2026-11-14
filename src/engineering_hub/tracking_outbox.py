"""Explicit, immutable local tracking-write envelopes; no background delivery."""
from __future__ import annotations

import json
import errno
import heapq
import hashlib
import base64
import os
import re
import secrets
import stat
import tempfile
import unicodedata
import uuid
from pathlib import Path

from agent_message_bus.agent_session_client import ClientError, _origin, _private_origin, _protect

_LIMIT = 131072


def _directory(path):
    """Require an existing private directory controlled exclusively by this user."""
    path = Path(path).expanduser().absolute()
    info = path.lstat()
    reparse = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & reparse):
        raise ClientError('invalid_tracking_outbox')
    if os.name != 'nt' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ClientError('private_outbox_permissions_required')
    return path


def _id(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ClientError('invalid_tracking_outbox_reference') from None
    return value


def _open_regular(path):
    """Reject special files before open and avoid blocking on a FIFO race."""
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
        raise OSError(errno.ELOOP, 'invalid_tracking_outbox')
    if not stat.S_ISREG(info.st_mode):
        raise ClientError('invalid_tracking_outbox')
    return os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))


def stage(directory, origin, installation, session_id, action, arguments):
    """Persist one immutable write before any network operation; return opaque ID."""
    directory = _directory(directory)
    record_id = str(uuid.uuid4())
    version = 1
    if action == 'propose-draft':
        if origin is not None or installation is not None or session_id is not None or type(arguments) is not dict or 'intake_id' in arguments:
            raise ClientError('invalid_tracking_outbox')
        arguments = {**arguments, 'intake_id': secrets.token_urlsafe(32)}
        version = 2
    record = dict(version=version, id=record_id, origin=origin, installation_id=installation,
                  staged_session_id=session_id, action=action, arguments=arguments)
    return _stage_record(directory, record)


def _stage_record(directory, record):
    record_id = _id(record['id'])
    payload = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
    if len(payload) > _LIMIT:
        raise ClientError('tracking_outbox_too_large')
    payload = _protect(payload)
    if len(payload) > _LIMIT:
        raise ClientError('tracking_outbox_too_large')
    fd, temporary = tempfile.mkstemp(prefix='.tracking-stage-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as target:
            fd = -1
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        # Hard-link publication is atomic and refuses an existing destination.
        os.link(temporary, directory / (record_id + '.tracking'))
    finally:
        if fd >= 0:
            os.close(fd)
        os.unlink(temporary)
    if os.name != 'nt':
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return record_id


def read(directory, record_id):
    directory, record_id = _directory(directory), _id(record_id)
    fd = _open_regular(directory / (record_id + '.tracking'))
    try:
        info = os.fstat(fd)
        reparse = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT
                or getattr(info, 'st_file_attributes', 0) & reparse
                or (os.name != 'nt' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077))):
            raise ClientError('invalid_tracking_outbox')
        with os.fdopen(fd, 'rb') as source:
            fd = -1
            raw = source.read(_LIMIT + 1)
        record = json.loads(_protect(raw, decrypt=True))
        if type(record) is not dict:
            raise ClientError('invalid_tracking_outbox')
        fields = {'version', 'id', 'origin', 'installation_id', 'staged_session_id', 'action', 'arguments'}
        recovery_fields = {'source_id', 'source_sha256', 'source_binding_sha256', 'expected_task_id'}
        extra = recovery_fields | ({'prior_id', 'prior_sha256'} if record.get('version') == 4 else set()) if record.get('version') in (3, 4) else set()
        if (record.get('version') not in (1, 2, 3, 4)
                or set(record) != fields | extra
                or record.get('id') != record_id or (record['version'] == 2 and not is_draft(record))):
            raise ClientError('invalid_tracking_outbox')
        if record['version'] in (3, 4):
            _id(record['source_id']); _id(record['installation_id'])
            if (record['source_id'] == record_id or record['action'] != 'propose-draft'
                    or record['staged_session_id'] is not None or type(record['source_sha256']) is not str
                    or len(record['source_sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in record['source_sha256'])
                    or type(record['source_binding_sha256']) is not str or len(record['source_binding_sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in record['source_binding_sha256'])
                    or type(record['origin']) is not str or type(record['arguments']) is not dict):
                raise ClientError('invalid_tracking_outbox')
            if record['expected_task_id'] is not None:
                _id(record['expected_task_id'])
            if record['version'] == 4:
                _id(record['prior_id'])
                if (record['prior_id'] in (record_id, record['source_id']) or record['expected_task_id'] is None
                        or type(record['prior_sha256']) is not str or len(record['prior_sha256']) != 64
                        or any(c not in '0123456789abcdef' for c in record['prior_sha256'])):
                    raise ClientError('invalid_tracking_outbox')
        return record
    finally:
        if fd >= 0:
            os.close(fd)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _accepted_prior(directory, source, original, prior_id):
    """Validate one explicitly referenced historical response without walking a chain."""
    prior = read(directory, _id(prior_id))
    if (prior.get('version') not in (3, 4) or prior['source_id'] != source['id']
            or prior['source_sha256'] != _fingerprint(source)
            or prior['source_binding_sha256'] != _fingerprint(original)
            or prior['origin'] != original['origin'] or prior['installation_id'] == original['installation_id']
            or prior['arguments'] != source['arguments']):
        raise ClientError('invalid_tracking_recovery')
    accepted = receipt(directory, prior)
    if accepted is None or (prior['expected_task_id'] is not None and prior['expected_task_id'] != accepted['task_id']):
        raise ClientError('invalid_tracking_recovery')
    return prior, accepted['task_id']


def stage_recovery(directory, source, origin, installation, accepted_attempt_id=None):
    """Stage a separate explicit v2 retry while retaining the original draft."""
    directory = _directory(directory)
    if source.get('version') != 2 or not is_draft(source) or read(directory, source['id']) != source:
        raise ClientError('invalid_tracking_recovery')
    original = bound_for_send(directory, source)
    prior = receipt(directory, source)
    _id(installation)
    if origin != original['origin'] or installation == original['installation_id']:
        raise ClientError('invalid_tracking_recovery')
    expected = prior['task_id'] if prior else None
    accepted_prior = None
    if accepted_attempt_id is not None:
        accepted_prior, prior_task = _accepted_prior(directory, source, original, accepted_attempt_id)
        if expected is not None and expected != prior_task:
            raise ClientError('tracking_recovery_task_mismatch')
        expected = prior_task
    record = dict(version=4 if accepted_prior is not None else 3, id=str(uuid.uuid4()), origin=origin, installation_id=installation,
                  staged_session_id=None, action='propose-draft', arguments=source['arguments'],
                  source_id=source['id'], source_sha256=_fingerprint(source),
                  source_binding_sha256=_fingerprint(original),
                  expected_task_id=expected)
    if accepted_prior is not None:
        record.update(prior_id=accepted_prior['id'], prior_sha256=_fingerprint(accepted_prior))
    return _stage_record(directory, record)


def validate_recovery(directory, record):
    """Fail closed if the original draft, routing or known acceptance changed."""
    if record.get('version') not in (3, 4):
        return
    source = read(directory, record['source_id'])
    if source.get('version') != 2 or not is_draft(source) or _fingerprint(source) != record['source_sha256']:
        raise ClientError('invalid_tracking_recovery')
    original = bound_for_send(directory, source)
    prior = receipt(directory, source)
    if (_fingerprint(original) != record['source_binding_sha256']
            or original['origin'] != record['origin'] or original['installation_id'] == record['installation_id']
            or source['arguments'] != record['arguments']
            or (prior is not None and record['expected_task_id'] not in (None, prior['task_id']))):
        raise ClientError('invalid_tracking_recovery')
    if record['version'] == 4:
        accepted_prior, prior_task = _accepted_prior(directory, source, original, record['prior_id'])
        if (_fingerprint(accepted_prior) != record['prior_sha256'] or prior_task != record['expected_task_id']
                or (prior is not None and prior['task_id'] != prior_task)):
            raise ClientError('tracking_recovery_task_mismatch')
        return prior_task
    return prior['task_id'] if prior is not None else None


def is_draft(record):
    """Only a proposal with neither routing identity is a profile-free draft."""
    if (record.get('origin') is not None or record.get('installation_id') is not None
            or record.get('staged_session_id') is not None):
        return False
    if record.get('version') == 1:
        return record.get('action') == 'propose'
    if record.get('version') != 2 or record.get('action') != 'propose-draft' or type(record.get('arguments')) is not dict:
        return False
    intake = record['arguments'].get('intake_id')
    try:
        if type(intake) is not str or len(intake) != 43:
            return False
        raw = base64.urlsafe_b64decode(intake + '=')
        return len(raw) == 32 and base64.urlsafe_b64encode(raw).decode().rstrip('=') == intake
    except (ValueError, TypeError):
        return False


def _draft_binding_supported():
    return os.name != 'nt'


def _require_durable_draft_binding(record):
    # The current Windows writer does not flush directory metadata, so it
    # cannot prove that a first binding survives power loss. Fail closed until
    # a supported, independently qualified durability boundary exists.
    # V2 uses a separately versioned Hub-global intake ID as the duplicate
    # barrier when a local binding is lost; v1 has only actor-scoped keys.
    if record.get('version') != 2 and not _draft_binding_supported():
        raise ClientError('tracking_binding_durability_unavailable')


def _binding_value(record, origin, installation):
    _id(record['id']); _id(installation)
    if not is_draft(record) or type(origin) is not str:
        raise ClientError('invalid_tracking_binding')
    try:
        _origin(origin)
    except ClientError:
        _private_origin(origin)
    return dict(version=1, record_id=record['id'], envelope_sha256=_fingerprint(record),
                origin=origin, installation_id=installation)


def binding(directory, record):
    """Read the one-time binding for an unbound draft, without network I/O."""
    directory = _directory(directory); identity = _id(record['id'])
    if not is_draft(record):
        raise ClientError('invalid_tracking_binding')
    try:
        fd = _open_regular(directory / (identity + '.binding'))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        reparse = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT
                or getattr(info, 'st_file_attributes', 0) & reparse
                or (os.name != 'nt' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077))):
            raise ClientError('invalid_tracking_binding')
        with os.fdopen(fd, 'rb') as source:
            fd = -1
            raw = source.read(_LIMIT + 1)
        value = json.loads(_protect(raw, decrypt=True))
        if type(value) is not dict or value != _binding_value(record, value.get('origin'), value.get('installation_id')):
            raise ClientError('invalid_tracking_binding')
        _sync_directory(directory)
        return value
    finally:
        if fd >= 0:
            os.close(fd)


def bind(directory, record, origin, installation):
    """Atomically bind a draft once; never change a selected installation."""
    _require_durable_draft_binding(record)
    directory = _directory(directory); identity = _id(record['id'])
    if read(directory, identity) != record:
        raise ClientError('tracking_outbox_changed')
    value = _binding_value(record, origin, installation)
    existing = binding(directory, record)
    if existing is not None:
        if existing != value:
            raise ClientError('tracking_outbox_already_bound')
        return existing
    payload = _protect(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode())
    if len(payload) > _LIMIT:
        raise ClientError('invalid_tracking_binding')
    fd, temporary = tempfile.mkstemp(prefix='.tracking-binding-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as target:
            fd = -1
            target.write(payload); target.flush(); os.fsync(target.fileno())
        try:
            os.link(temporary, directory / (identity + '.binding'))
        except FileExistsError:
            if binding(directory, record) != value:
                raise ClientError('tracking_outbox_already_bound') from None
    finally:
        if fd >= 0:
            os.close(fd)
        os.unlink(temporary)
    _sync_directory(directory)
    return value


def bound_for_send(directory, record):
    """Require a qualified durability boundary and an existing first binding."""
    _require_durable_draft_binding(record)
    value = binding(directory, record)
    if value is None:
        raise ClientError('tracking_outbox_unbound')
    return value


def _receipt_value(record, task_id, bound=None):
    """Bind acceptance to the exact saved request, never to task completion."""
    _id(record['id']); _id(task_id)
    action, arguments = record.get('action'), record.get('arguments')
    if action not in ('propose', 'propose-draft', 'update', 'review', 'withdraw-claim') or type(arguments) is not dict:
        raise ClientError('invalid_tracking_receipt')
    if action not in ('propose', 'propose-draft') and (arguments.get('task_id') != task_id or _id(arguments['task_id']) != task_id):
        raise ClientError('invalid_tracking_receipt')
    if is_draft(record):
        if type(bound) is not dict or bound != _binding_value(record, bound.get('origin'), bound.get('installation_id')):
            raise ClientError('invalid_tracking_receipt')
        origin, installation = bound['origin'], bound['installation_id']
    else:
        origin, installation = record.get('origin'), record.get('installation_id')
        if type(origin) is not str or type(installation) is not str:
            raise ClientError('invalid_tracking_receipt')
    value = dict(version=1, record_id=record['id'], envelope_sha256=_fingerprint(record),
                origin=origin, installation_id=installation,
                action=action, task_id=task_id, state='hub_accepted',
                execution_authorization=False)
    if is_draft(record):
        value['binding_sha256'] = _fingerprint(bound)
    return value


def _sync_directory(directory):
    if os.name != 'nt':
        fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def receipt(directory, record):
    """Read a validated protected sidecar; absence is unconfirmed, not failure."""
    directory = _directory(directory)
    identity = _id(record['id'])
    try:
        fd = _open_regular(directory / (identity + '.accepted'))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        reparse = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT
                or getattr(info, 'st_file_attributes', 0) & reparse
                or (os.name != 'nt' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077))):
            raise ClientError('invalid_tracking_receipt')
        with os.fdopen(fd, 'rb') as source:
            fd = -1
            raw = source.read(_LIMIT + 1)
        value = json.loads(_protect(raw, decrypt=True))
        bound = bound_for_send(directory, record) if is_draft(record) else None
        if type(value) is not dict or value != _receipt_value(record, value.get('task_id'), bound):
            raise ClientError('invalid_tracking_receipt')
        # A previous writer may have failed after publishing but before the
        # directory flush; never report acceptance if that flush still fails.
        _sync_directory(directory)
        return value
    finally:
        if fd >= 0:
            os.close(fd)


def record_acceptance(directory, record, response):
    """Publish an immutable, deterministic sidecar after validated MCP success."""
    directory = _directory(directory)
    identity = _id(record['id'])
    if read(directory, identity) != record:
        raise ClientError('tracking_outbox_changed')
    if (type(response) is not dict or response.get('type') != 'tracking_result'
            or response.get('operation') != record.get('action')
            or response.get('execution_authorization') is not False
            or type(response.get('result')) is not dict):
        raise ClientError('invalid_tracking_receipt')
    result = response['result']
    if record.get('version') in (3, 4):
        known_task = validate_recovery(directory, record)
        if record['expected_task_id'] is not None and result.get('id') != record['expected_task_id']:
            raise ClientError('tracking_recovery_task_mismatch')
        if known_task is not None and result.get('id') != known_task:
            raise ClientError('tracking_recovery_task_mismatch')
    bound = bound_for_send(directory, record) if is_draft(record) else None
    value = _receipt_value(record, result.get('id'), bound)
    if (record['action'] in ('propose', 'propose-draft') and 'project' in result
            and result['project'] != record['arguments'].get('project')):
        raise ClientError('invalid_tracking_receipt')
    existing = receipt(directory, record)
    if existing is not None:
        if existing != value:
            raise ClientError('tracking_receipt_conflict')
        return value
    payload = _protect(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode())
    if len(payload) > _LIMIT:
        raise ClientError('invalid_tracking_receipt')
    fd, temporary = tempfile.mkstemp(prefix='.tracking-accepted-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as target:
            fd = -1
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        try:
            os.link(temporary, directory / (identity + '.accepted'))
        except FileExistsError:
            if receipt(directory, record) != value:
                raise ClientError('tracking_receipt_conflict') from None
    finally:
        if fd >= 0:
            os.close(fd)
        os.unlink(temporary)
    _sync_directory(directory)
    return value


def reference_page(directory, *, limit=1000, after=None):
    """Read a bounded UUID-sorted page of opaque names; never open a record."""
    directory = _directory(directory)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ClientError('invalid_tracking_outbox_limit')
    if after is not None:
        _id(after)
    def eligible(entries):
        for entry in entries:
            if not entry.name.endswith('.tracking'):
                continue
            try:
                identity = _id(entry.name[:-9])
            except ClientError:
                continue
            if after is None or identity > after:
                yield identity
    # scandir yields entries lazily; nsmallest retains at most limit+1 names.
    # Names are not opened or followed here; read() independently checks the
    # record type, permissions and contents before it can be inspected/sent.
    with os.scandir(directory) as entries:
        selected = heapq.nsmallest(limit + 1, eligible(entries))
    result = selected[:limit]
    return {'record_ids': result, 'next_after': result[-1] if len(selected) > limit else None}


def references(directory):
    """Legacy bounded list API; callers needing larger sets use reference_page."""
    page = reference_page(directory)
    if page['next_after'] is not None:
        raise ClientError('tracking_outbox_too_many')
    return page['record_ids']


def inventory_page(directory, *, limit=25, after=None):
    """Explicit private local summary; bounded enumeration and record reads."""
    directory = _directory(directory)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ClientError('invalid_tracking_outbox_limit')
    if after is not None:
        _id(after)
    candidates, unrecognized = [], 0
    with os.scandir(directory) as entries:
        for count, entry in enumerate(entries, 1):
            if count > 4096:
                raise ClientError('tracking_inventory_too_many_entries')
            if not entry.name.endswith('.tracking'):
                continue
            try:
                identity = _id(entry.name[:-9])
            except ClientError:
                unrecognized += 1
                continue
            if after is None or identity > after:
                candidates.append(identity)
    selected = heapq.nsmallest(limit + 1, candidates)
    ids = selected[:limit]
    items = []
    for identity in ids:
        try:
            info = (directory / (identity + '.tracking')).lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                raise ClientError('invalid_tracking_outbox')
            record = read(directory, identity)
            action, args = record['action'], record['arguments']
            if action not in ('propose', 'propose-draft', 'update', 'review', 'withdraw-claim') or type(args) is not dict:
                raise ClientError('invalid_tracking_outbox')
            project, title = args.get('project'), args.get('title')
            if action in ('propose', 'propose-draft'):
                if type(project) is not str or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', project) is None:
                    raise ClientError('invalid_tracking_outbox')
                if type(title) is not str or not 1 <= len(title) <= 200:
                    raise ClientError('invalid_tracking_outbox')
                title = ''.join(character for character in title if not unicodedata.category(character).startswith('C')).strip()
                if not title:
                    raise ClientError('invalid_tracking_outbox')
            else:
                project = title = None
            known = validate_recovery(directory, record)
            bound = binding(directory, record) if is_draft(record) else record
            accepted = receipt(directory, record)
            if accepted is not None and ((known is not None and accepted['task_id'] != known)
                    or (record.get('expected_task_id') is not None and accepted['task_id'] != record['expected_task_id'])):
                raise ClientError('tracking_recovery_task_mismatch')
            items.append(dict(record_id=identity, project=project, title=title, action=action,
                state='hub_accepted' if accepted is not None else 'unbound' if bound is None else 'unconfirmed',
                task_id=accepted['task_id'] if accepted is not None else None,
                source_record_id=record.get('source_id'), accepted_attempt_id=record.get('prior_id')))
        except Exception:
            # Names remain visible even if a file disappeared or its contents,
            # binding, receipt or recovery evidence could not be validated.
            items.append(dict(record_id=identity, state='unreadable'))
    return dict(items=items, next_after=ids[-1] if len(selected) > limit else None,
                unrecognized_tracking_entries=unrecognized)
