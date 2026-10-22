"""Reject accidental image-layer ledgers in the fixed Linux Hub namespace.

Exact mountpoint presence is a startup metadata prerequisite, not proof of NAS
ACLs, storage durability, ledger ownership or future mount stability. Nested
settings/bus mounts do not make their parent a persistent ledger mount. No
configuration flag, writes, path creation, chmod or fallback is supplied here.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import sys


ROOT=PurePosixPath('/var/lib/hub')
_LIMIT=2*1024*1024
_ESCAPE=re.compile(r'\\(040|011|012|134)')


class ImageStateBoundaryError(ValueError):
    def __init__(self):
        super().__init__('hub_image_state_mount_required')


def _read_mountinfo():
    with open('/proc/self/mountinfo','rb') as source:
        raw=source.read(_LIMIT+1)
    if len(raw)>_LIMIT:
        raise ImageStateBoundaryError()
    return raw.decode('utf-8')


def _mount_path(raw):
    if '\\' in _ESCAPE.sub('',raw):
        raise ImageStateBoundaryError()
    value=_ESCAPE.sub(lambda match:chr(int(match.group(1),8)),raw)
    path=PurePosixPath(value)
    if (not value.startswith('/') or value.startswith('//') or '\x00' in value
        or str(path)!=value or any(part in {'.','..'} for part in value.split('/'))):
        raise ImageStateBoundaryError()
    return path


def _require_exact_mount(text):
    if type(text) is not str or not text or len(text.encode('utf-8'))>_LIMIT:
        raise ImageStateBoundaryError()
    matches=0
    identifiers=set()
    for line in text.splitlines():
        fields=line.split()
        if len(fields)<10 or '-' not in fields:
            raise ImageStateBoundaryError()
        separator=fields.index('-')
        if separator<6 or len(fields)!=separator+4:
            raise ImageStateBoundaryError()
        numbers=(*fields[:2],*fields[2].split(':'))
        if (len(fields[2].split(':'))!=2 or fields[0] in identifiers
            or any(not re.fullmatch('[0-9]+',number) or str(int(number))!=number for number in numbers)):
            raise ImageStateBoundaryError()
        identifiers.add(fields[0])
        _mount_path(fields[3])
        if _mount_path(fields[4])==ROOT:
            matches+=1
    if matches!=1:
        raise ImageStateBoundaryError()


def require_image_state_mount(database_path):
    """Check before repository mkdir/open, only for the image's fixed root.

    Resolve symlink aliases as metadata, without creating missing paths. Both
    a lexical image path and an outside alias into that namespace are covered.
    Non-Linux source development and all other database roots are untouched.
    """
    if sys.platform!='linux':
        return
    lexical=PurePosixPath(os.path.abspath(os.fspath(database_path)))
    in_image=lexical==ROOT or ROOT in lexical.parents
    try:
        canonical=PurePosixPath(str(Path(database_path).resolve(strict=False)))
        in_image=in_image or canonical==ROOT or ROOT in canonical.parents
        if in_image:
            _require_exact_mount(_read_mountinfo())
    except (OSError,ValueError,RuntimeError,UnicodeError):
        if in_image:
            raise ImageStateBoundaryError() from None
