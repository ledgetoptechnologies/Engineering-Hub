"""Explicit, immutable local tracking-write envelopes; no background delivery."""
from __future__ import annotations

import json
import heapq
import hashlib
import os
import stat
import tempfile
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


def stage(directory, origin, installation, session_id, action, arguments):
    """Persist one immutable write before any network operation; return opaque ID."""
    directory = _directory(directory)
    record_id = str(uuid.uuid4())
    record = dict(version=1, id=record_id, origin=origin, installation_id=installation,
                  staged_session_id=session_id, action=action, arguments=arguments)
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
    fd = os.open(directory / (record_id + '.tracking'), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
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
        if (type(record) is not dict or set(record) != {'version', 'id', 'origin', 'installation_id',
                    'staged_session_id', 'action', 'arguments'} or record['version'] != 1 or record['id'] != record_id):
            raise ClientError('invalid_tracking_outbox')
        return record
    finally:
        if fd >= 0:
            os.close(fd)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def is_draft(record):
    """Only a proposal with neither routing identity is a profile-free draft."""
    return (record.get('origin') is None and record.get('installation_id') is None
            and record.get('staged_session_id') is None and record.get('action') == 'propose')


def _draft_binding_supported():
    return os.name != 'nt'


def _require_durable_draft_binding():
    # The current Windows writer does not flush directory metadata, so it
    # cannot prove that a first binding survives power loss. Fail closed until
    # a supported, independently qualified durability boundary exists.
    if not _draft_binding_supported():
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
        fd = os.open(directory / (identity + '.binding'), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
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
    _require_durable_draft_binding()
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
    _require_durable_draft_binding()
    value = binding(directory, record)
    if value is None:
        raise ClientError('tracking_outbox_unbound')
    return value


def _receipt_value(record, task_id, bound=None):
    """Bind acceptance to the exact saved request, never to task completion."""
    _id(record['id']); _id(task_id)
    action, arguments = record.get('action'), record.get('arguments')
    if action not in ('propose', 'update', 'review', 'withdraw-claim') or type(arguments) is not dict:
        raise ClientError('invalid_tracking_receipt')
    if action != 'propose' and (arguments.get('task_id') != task_id or _id(arguments['task_id']) != task_id):
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
        fd = os.open(directory / (identity + '.accepted'), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
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
    bound = bound_for_send(directory, record) if is_draft(record) else None
    value = _receipt_value(record, result.get('id'), bound)
    if (record['action'] == 'propose' and 'project' in result
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
