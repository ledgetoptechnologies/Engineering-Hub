"""Explicit, immutable local tracking-write envelopes; no background delivery."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from pathlib import Path

from agent_message_bus.agent_session_client import ClientError, _protect

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


def references(directory):
    directory = _directory(directory)
    result = []
    for path in directory.iterdir():
        if path.name.endswith('.tracking'):
            try:
                result.append(_id(path.name[:-9]))
            except ClientError:
                continue
            if len(result) > 1000:
                raise ClientError('tracking_outbox_too_many')
    return sorted(result)
