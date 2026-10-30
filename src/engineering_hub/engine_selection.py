"""Offline record selecting a separate Docker Engine for a future staging pilot.

Source only and off by default. Parsing a record contacts no Engine, reads no
host path, starts nothing and never rebinds the frozen pilot acceptance. It
refuses records naming the held TEST Engine, its storage, backup and runtime
roots, or the ordinary system Docker Engine.

The path checks here are lexical: they compare written paths, not the host.
They prove nothing about mounts, symlinks, bind sources, user namespaces or
cgroups. Those need separate host evidence, referenced by digest in the record
and checked by an independent reviewer. A namespace ID is not accepted as a
field at all: an old namespace number is history, not proof of isolation now.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re


SCHEMA = 'engineering-hub.engine-selection.v1'
MAX_RECORD_BYTES = 16384

# The held TEST Engine and everything retained for it. Never select, reuse or
# overlap any of these, even read-only.
HELD_ENGINE_ID = '43e4e27e-b16b-4b19-9771-00333915c5cf'
HELD_RUNTIME_UID = 10001
PROTECTED_ROOTS = (
    PurePosixPath('/var/lib/eh-pilot-volume-test-v1'),      # TEST mount, data root and recovery socket
    PurePosixPath('/var/lib/eh-pilot-test-v1.ext4'),        # raw TEST image
    PurePosixPath('/var/lib/eh-test-storage-recovery-20261024-_pb9sahn'),  # retained original backup
    PurePosixPath('/run/user/10001'),                       # TEST account runtime and residual exec roots
)
# The ordinary system Engine that already runs other workloads on staging.
SYSTEM_ENGINE_ROOTS = (
    PurePosixPath('/var/run/docker.sock'),
    PurePosixPath('/run/docker.sock'),
    PurePosixPath('/var/lib/docker'),
    PurePosixPath('/var/lib/containerd'),
    PurePosixPath('/run/containerd'),
)

# Host evidence an independent reviewer needs before any activation. A record
# may cite it, but citing is not verification.
EVIDENCE_KINDS = frozenset({
    'engine_identity',        # current Engine ID, version and run-as account
    'storage_disjointness',   # actual mounts and devices behind the data root
    'resource_limits',        # effective cgroup CPU, memory and pids limits
    'namespace_review',       # current user and mount namespace relationship
    'capacity',               # free disk and memory against the budget
})
LIMITS = ('lexical_paths_only', 'no_mount_or_symlink_proof', 'no_namespace_proof',
          'no_live_engine_identity', 'no_activation')

_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
_ENGINE_ID = re.compile(r'[0-9A-Za-z][0-9A-Za-z:-]{7,127}\Z')
_HUMAN = re.compile(r'human:(?:[1-9][0-9]{0,19}|cf:[0-9a-f]{64})\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_NOTE = re.compile(r'[a-z0-9][a-z0-9_./-]{0,200}\.md\Z')
_MAX_CPU_MILLIS = 64_000
_MAX_MEMORY_BYTES = 1 << 40
_MAX_DISK_BYTES = 1 << 44


class SelectionError(ValueError):
    """Closed refusal code; never carries input values."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Budget:
    cpu_millis: int
    memory_bytes: int
    disk_bytes: int


@dataclass(frozen=True)
class Evidence:
    kind: str
    note_path: str
    sha256: str


@dataclass(frozen=True)
class EngineSelection:
    machine_id: str
    engine_id: str
    mode: str
    run_as_uid: int
    socket_path: PurePosixPath
    data_root: PurePosixPath
    budget: Budget
    enabled: bool
    decision_id: str | None
    approved_by: str | None
    evidence: tuple[Evidence, ...]

    @property
    def status(self):
        """'disabled' or 'selected_unverified'; this module never reports an active Engine."""
        return 'selected_unverified' if self.enabled else 'disabled'

    @property
    def limits(self):
        return LIMITS


def _refuse(code):
    raise SelectionError(code)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _refuse('duplicate_key')
        result[key] = value
    return result


def _closed(value, keys, code):
    if type(value) is not dict or set(value) != keys:
        _refuse(code)
    return value


def _integer(value, maximum, code):
    if type(value) is not int or not 0 < value <= maximum:
        _refuse(code)
    return value


def _path(value, code):
    """An absolute, already-normal POSIX spelling. Lexical only."""
    if (type(value) is not str or not value.startswith('/') or len(value) > 512
            or '\x00' in value or '\\' in value or not value.isprintable()):
        _refuse(code)
    path = PurePosixPath(value)
    # PurePosixPath silently folds '//', '/./' and a trailing '/'; require the
    # written form to already be that normal form, and never accept '..'.
    if str(path) != value or path == PurePosixPath('/') or '..' in path.parts:
        _refuse(code)
    return path


def _within(path, root):
    return path == root or root in path.parents


def _overlaps(path, root):
    return _within(path, root) or _within(root, path)


def _check_paths(socket_path, data_root):
    for root in PROTECTED_ROOTS:
        if _within(socket_path, root):
            _refuse('socket_in_protected_root')
        if _overlaps(data_root, root):
            _refuse('data_root_overlaps_protected_root')
    for root in SYSTEM_ENGINE_ROOTS:
        if _within(socket_path, root):
            _refuse('socket_is_system_engine')
        if _overlaps(data_root, root):
            _refuse('data_root_overlaps_system_engine')
    if _within(socket_path, data_root):
        _refuse('socket_inside_data_root')


def parse_selection(text):
    """Parse one record from JSON text; refuses oversized, duplicate or unknown keys."""
    if type(text) is not str or len(text.encode('utf-8')) > MAX_RECORD_BYTES:
        _refuse('record_size')
    try:
        data = json.loads(text, object_pairs_hook=_unique)
    except SelectionError:
        raise
    except (ValueError, RecursionError):
        raise SelectionError('record_json') from None
    return validate_selection(data)


def validate_selection(data):
    record = _closed(data, {'schema', 'enabled', 'machine_id', 'engine', 'budget', 'approval', 'evidence'}, 'record_shape')
    if record['schema'] != SCHEMA:
        _refuse('schema')
    if type(record['enabled']) is not bool:
        _refuse('enabled')
    if record['machine_id'] != 'staging-pc':
        _refuse('machine_id')

    engine = _closed(record['engine'], {'engine_id', 'mode', 'run_as_uid', 'socket_path', 'data_root'}, 'engine_shape')
    if type(engine['engine_id']) is not str or _ENGINE_ID.fullmatch(engine['engine_id']) is None:
        _refuse('engine_id')
    if engine['engine_id'].lower() == HELD_ENGINE_ID:
        _refuse('held_engine')
    if engine['mode'] not in ('rootless', 'rootful'):
        _refuse('mode')
    uid = engine['run_as_uid']
    if type(uid) is not int or not 0 <= uid <= 0x7fffffff:
        _refuse('run_as_uid')
    if (engine['mode'] == 'rootless') != (uid > 0):
        _refuse('mode_uid_mismatch')
    if uid == HELD_RUNTIME_UID:
        _refuse('held_runtime_account')
    socket_path = _path(engine['socket_path'], 'socket_path')
    data_root = _path(engine['data_root'], 'data_root')
    _check_paths(socket_path, data_root)

    budget = _closed(record['budget'], {'cpu_millis', 'memory_bytes', 'disk_bytes'}, 'budget_shape')
    parsed_budget = Budget(_integer(budget['cpu_millis'], _MAX_CPU_MILLIS, 'budget_cpu'),
                           _integer(budget['memory_bytes'], _MAX_MEMORY_BYTES, 'budget_memory'),
                           _integer(budget['disk_bytes'], _MAX_DISK_BYTES, 'budget_disk'))

    if type(record['evidence']) is not list or len(record['evidence']) > 32:
        _refuse('evidence_shape')
    evidence = []
    for item in record['evidence']:
        item = _closed(item, {'kind', 'note_path', 'sha256'}, 'evidence_shape')
        if item['kind'] not in EVIDENCE_KINDS:
            _refuse('evidence_kind')
        if type(item['note_path']) is not str or _NOTE.fullmatch(item['note_path']) is None or '..' in item['note_path']:
            _refuse('evidence_note_path')
        if type(item['sha256']) is not str or _SHA256.fullmatch(item['sha256']) is None:
            _refuse('evidence_sha256')
        evidence.append(Evidence(item['kind'], item['note_path'], item['sha256']))
    if len({item.kind for item in evidence}) != len(evidence):
        _refuse('evidence_duplicate_kind')

    approval = record['approval']
    if approval is None:
        decision_id = approved_by = None
    else:
        approval = _closed(approval, {'decision_id', 'approved_by'}, 'approval_shape')
        if type(approval['decision_id']) is not str or _UUID.fullmatch(approval['decision_id']) is None:
            _refuse('approval_decision_id')
        if type(approval['approved_by']) is not str or _HUMAN.fullmatch(approval['approved_by']) is None:
            _refuse('approval_actor')
        decision_id, approved_by = approval['decision_id'], approval['approved_by']

    # Enabling needs a human decision and every kind of host evidence cited.
    # It still activates nothing: status stays 'selected_unverified'.
    if record['enabled']:
        if decision_id is None:
            _refuse('enabled_without_approval')
        if {item.kind for item in evidence} != EVIDENCE_KINDS:
            _refuse('enabled_without_evidence')

    return EngineSelection(machine_id=record['machine_id'], engine_id=engine['engine_id'], mode=engine['mode'],
                           run_as_uid=uid, socket_path=socket_path, data_root=data_root, budget=parsed_budget,
                           enabled=record['enabled'], decision_id=decision_id, approved_by=approved_by,
                           evidence=tuple(evidence))
