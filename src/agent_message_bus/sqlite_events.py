"""Native Linux inotify events for committed SQLite WAL message changes.

Inotify readiness is only an invalidation hint. Each relevant filesystem batch
is reconciled behind one ``BEGIN IMMEDIATE`` arbitration so uncommitted WAL
frames cannot be mistaken for durable message commits. No timer or poll query is
used; the worker blocks in epoll until an inotify or eventfd readiness event.
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import select
import sqlite3
import stat
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

try:
    import fcntl
    import termios
except ImportError:  # Windows is an unsupported but importable deployment target.
    fcntl = None
    termios = None

from .database import AGENTS


class WatcherFailed(RuntimeError):
    """The native event source or SQLite consistency barrier failed closed."""


_SQLITE_ERROR_BASES = (
    'SQLITE_ERROR', 'SQLITE_INTERNAL', 'SQLITE_PERM', 'SQLITE_ABORT',
    'SQLITE_BUSY', 'SQLITE_LOCKED', 'SQLITE_NOMEM', 'SQLITE_READONLY',
    'SQLITE_INTERRUPT', 'SQLITE_IOERR', 'SQLITE_CORRUPT', 'SQLITE_NOTFOUND',
    'SQLITE_FULL', 'SQLITE_CANTOPEN', 'SQLITE_PROTOCOL', 'SQLITE_EMPTY',
    'SQLITE_SCHEMA', 'SQLITE_TOOBIG', 'SQLITE_CONSTRAINT', 'SQLITE_MISMATCH',
    'SQLITE_MISUSE', 'SQLITE_NOLFS', 'SQLITE_AUTH', 'SQLITE_FORMAT',
    'SQLITE_RANGE', 'SQLITE_NOTADB', 'SQLITE_NOTICE', 'SQLITE_WARNING',
)
_SQLITE_ERROR_NAMES = frozenset(
    name for name in dir(sqlite3)
    if any(name == base or name.startswith(base + '_') for base in _SQLITE_ERROR_BASES)
)


def _sqlite_error_details(exc: BaseException) -> dict[str, int | str]:
    """Return only SQLite's bounded code and matching known constant name.

    Exception text, SQL, file paths and arbitrary error attributes are never
    included. OperationalError alone cannot distinguish a busy writer from
    read-only storage, I/O errors or other failures.
    """
    if not isinstance(exc, sqlite3.Error):
        return {}
    code = getattr(exc, 'sqlite_errorcode', None)
    if type(code) is not int or not 0 <= code <= 2**31 - 1:
        return {}
    result: dict[str, int | str] = {'sqlite_errorcode': code}
    name = getattr(exc, 'sqlite_errorname', None)
    if (type(name) is str and name in _SQLITE_ERROR_NAMES
            and getattr(sqlite3, name) == code):
        result['sqlite_errorname'] = name
    return result


class MessageCursorReader:
    """Page cursor rows while retaining only distinct recipient names.

    ``None`` is the startup sentinel: return the current durable head without
    reading any historical message rows. Integer cursors scan every retained
    delivery-sequence row through the head in bounded pages. Message IDs and
    bodies are never selected or retained.
    """

    def __init__(self, *, page_size: int = 512):
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 4096:
            raise ValueError("page_size must be between 1 and 4096")
        self.page_size = page_size
        self.page_reads = 0
        self.max_page_rows = 0

    @staticmethod
    def _head(conn: sqlite3.Connection) -> int:
        row = conn.execute("""SELECT
            COALESCE((SELECT seq FROM sqlite_sequence WHERE name='message_delivery_sequence'), 0),
            COALESCE(MAX(seq), 0)
            FROM message_delivery_sequence""").fetchone()
        head, max_mapping = int(row[0]), int(row[1])
        if head < max_mapping or head < 0:
            raise WatcherFailed("message delivery sequence head is inconsistent")
        return head

    def __call__(self, conn: sqlite3.Connection, cursor: int | None) -> tuple[Sequence[str], int]:
        head = self._head(conn)
        if cursor is None:
            return (), head
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0 or cursor > head:
            raise WatcherFailed("message event cursor is outside the durable sequence head")

        recipients: set[str] = set()
        current = cursor
        while current < head:
            rows = conn.execute("""SELECT m.recipient, d.seq
                FROM message_delivery_sequence AS d
                INNER JOIN messages AS m ON m.id=d.message_id
                WHERE d.seq>? AND d.seq<=? ORDER BY d.seq ASC LIMIT ?""",
                (current, head, self.page_size)).fetchall()
            self.page_reads += 1
            self.max_page_rows = max(self.max_page_rows, len(rows))
            if not rows:
                # Rows can be removed by retention, leaving legal sequence gaps.
                current = head
                break
            for row in rows:
                recipient = str(row[0])
                if recipient not in AGENTS:
                    raise WatcherFailed("message delivery sequence has an invalid recipient")
                recipients.add(recipient)
            current = int(rows[-1][1])
        return tuple(sorted(recipients)), head


_LOCAL_FILESYSTEM_TYPES = frozenset({
    "bcachefs", "btrfs", "erofs", "ext2", "ext3", "ext4", "f2fs",
    "overlay", "tmpfs", "xfs", "zfs",
})


def _unescape_mount_field(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def filesystem_type_for(path: str | os.PathLike[str]) -> str | None:
    """Return the most-specific Linux mount type for ``path`` if known."""
    if not sys_platform_linux():
        return None
    try:
        target = Path(path).expanduser().resolve(strict=True)
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except (OSError, RuntimeError):
        return None
    candidates: list[tuple[int, str]] = []
    for line in mountinfo.splitlines():
        try:
            left, right = line.split(" - ", 1)
            mountpoint = Path(_unescape_mount_field(left.split()[4]))
            fs_type = right.split()[0]
        except (IndexError, ValueError):
            continue
        if target == mountpoint or mountpoint in target.parents:
            candidates.append((len(mountpoint.parts), fs_type))
    return max(candidates, default=(0, None))[1]


def native_event_support(path: str | os.PathLike[str]) -> tuple[bool, str]:
    """Check Linux facilities and a known local filesystem before activation."""
    if not sys_platform_linux():
        return False, "native SQLite inotify requires Linux epoll/eventfd"
    try:
        target = Path(path).expanduser().absolute()
        resolved = target.resolve(strict=True)
        if target != resolved:
            return False, "database path contains symlink components"
        if not stat.S_ISREG(resolved.stat().st_mode):
            return False, "database path is not a regular file"
    except (OSError, RuntimeError) as exc:
        return False, f"database path is unavailable: {type(exc).__name__}"
    fs_type = filesystem_type_for(resolved)
    if fs_type not in _LOCAL_FILESYSTEM_TYPES:
        return False, f"unsupported or non-local filesystem type: {fs_type or 'unknown'}"
    return True, f"Linux local {fs_type} filesystem"


# Linux inotify ABI masks.
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000

_DIR_MASK = IN_ATTRIB | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF
_FILE_MASK = IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_DELETE_SELF | IN_MOVE_SELF
_REPLACEMENT_MASK = IN_ATTRIB | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
_EVENT_HEADER = struct.Struct("iIII")
_FIONREAD = getattr(termios, "FIONREAD", None) if termios is not None else None


class SQLiteEventSource:
    """Persistent native eventsource for message delivery-sequence changes.

Lifecycle methods are blocking and must be called via ``asyncio.to_thread``
from an ASGI lifespan. Startup installs every watch before taking one
barrier-protected head snapshot. Normal operation has no timer or SQLite
query when idle; a bounded retry timer exists only while an invalidation is
dirty and a verified SQLite BUSY prevents reconciliation.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        read_after: Callable[[sqlite3.Connection, int | None], tuple[Sequence[str], int]],
        deliver: Callable[[Sequence[str], int], None],
        *,
        busy_timeout_ms: int = 10_000,
        busy_retry_deadline_ms: int = 60_000,
        busy_retry_interval_ms: int = 250,
        read_buffer_bytes: int = 64 * 1024,
        max_flush_bytes: int = 8 * 1024 * 1024,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        if not sys_platform_linux():
            raise OSError("SQLiteEventSource requires Linux inotify, epoll, and eventfd")
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        if busy_retry_deadline_ms <= 0 or busy_retry_interval_ms <= 0:
            raise ValueError("SQLite busy retry bounds must be positive")
        if read_buffer_bytes < _EVENT_HEADER.size:
            raise ValueError("read_buffer_bytes is too small for an inotify event")
        if max_flush_bytes < read_buffer_bytes:
            raise ValueError("max_flush_bytes must be at least read_buffer_bytes")
        requested_path = Path(os.path.abspath(os.fspath(Path(db_path).expanduser())))
        resolved_path = requested_path.resolve(strict=True)
        if requested_path != resolved_path:
            raise ValueError("db_path must not contain symlink components")
        if not stat.S_ISREG(resolved_path.stat().st_mode):
            raise ValueError("db_path must be a regular file")

        self.db_path = resolved_path
        self.read_after = read_after
        self.deliver = deliver
        self.busy_timeout_ms = busy_timeout_ms
        self.busy_retry_deadline_ms = busy_retry_deadline_ms
        self.busy_retry_interval_ms = busy_retry_interval_ms
        self.read_buffer_bytes = read_buffer_bytes
        self.max_flush_bytes = max_flush_bytes
        self.on_error = on_error
        self._ready = threading.Event()
        self._thread_done = threading.Event()
        self._thread: threading.Thread | None = None
        # Only lifecycle/test control commands are queued; this queue is bounded.
        self._commands: queue.Queue[tuple[str, threading.Event | None]] = queue.Queue(maxsize=2)
        self._condition = threading.Condition()
        self._stats: dict[str, int | bool | str | None] = {
            "healthy": False,
            "failure": None,
            "cursor": None,
            "snapshot_reads": 0,
            "pages_read": 0,
            "max_page_rows": 0,
            "inotify_batches": 0,
            "relevant_events": 0,
            "deliveries": 0,
            "startup_catchups": 0,
            "watcher_stopped": False,
            "failure_stage": None,
            "sqlite_errorcode": None,
            "sqlite_errorname": None,
            "degraded": False,
            "busy_retry_count": 0,
        }
        self._stage = "created"
        self._failure: BaseException | None = None
        self._inotify_fd: int | None = None
        self._event_fd: int | None = None
        self._epoll: select.epoll | None = None
        self._watch_kinds: dict[int, str] = {}
        self._watch_identities: dict[str, tuple[int, int]] = {}
        self._directory_wd: int | None = None
        self._db_wd: int | None = None
        self._wal_wd: int | None = None
        self._reader: sqlite3.Connection | None = None
        self._barrier: sqlite3.Connection | None = None
        self._cursor: int | None = None
        self._db_name = self.db_path.name
        self._wal_name = self.db_path.name + "-wal"
        self._dirty = False
        self._dirty_since: float | None = None
        self._retry_at: float | None = None
        self._last_busy_error: sqlite3.OperationalError | None = None

    @property
    def stats(self) -> dict[str, int | bool | str | None]:
        with self._condition:
            return dict(self._stats)

    @property
    def healthy(self) -> bool:
        with self._condition:
            return bool(self._stats["healthy"])

    @property
    def failure(self) -> BaseException | None:
        with self._condition:
            return self._failure

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._thread_done.is_set()

    def start(self) -> dict[str, int | bool | str | None]:
        if self._thread is not None:
            raise RuntimeError("SQLite event source can only be started once")
        self._thread = threading.Thread(target=self._run, name=f"sqlite-inotify:{self.db_path.name}", daemon=False)
        self._thread.start()
        startup_wait = (self.busy_timeout_ms + self.busy_retry_deadline_ms) / 1000 + 10
        if not self._ready.wait(startup_wait):
            raise TimeoutError("timed out starting native SQLite event source")
        self.raise_if_failed()
        return self.stats

    def raise_if_failed(self) -> None:
        failure = self.failure
        if failure is not None:
            raise WatcherFailed(f"native SQLite event source failed closed: {failure}") from failure

    def wait_for_cursor(self, cursor: int, timeout: float = 5.0) -> bool:
        """Synchronize tests/operations without querying SQLite or polling it."""
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        with self._condition:
            reached = self._condition.wait_for(
                lambda: int(self._stats["cursor"] or 0) >= cursor or self._failure is not None,
                timeout=timeout,
            )
            return reached and self._failure is None and int(self._stats["cursor"] or 0) >= cursor

    def stop(self, timeout: float = 10.0) -> dict[str, int | bool | str | None]:
        if self._thread is None:
            return self.stats
        if not self._thread_done.is_set():
            done = threading.Event()
            try:
                self._commands.put(("stop", done), timeout=timeout)
            except queue.Full as exc:
                raise TimeoutError("native SQLite lifecycle command queue is full") from exc
            self._signal_control()
            if not done.wait(timeout):
                self.raise_if_failed()
                raise TimeoutError("timed out stopping native SQLite event source")
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            self.raise_if_failed()
            raise TimeoutError("native SQLite event thread did not stop")
        return self.stats

    def flush_pending(self, timeout: float = 10.0) -> dict[str, int | bool | str | None]:
        """Drain currently queued kernel events once; used only for synchronization."""
        if not self.running:
            self.raise_if_failed()
            raise RuntimeError("native SQLite event source is not running")
        done = threading.Event()
        try:
            self._commands.put(("flush", done), timeout=timeout)
        except queue.Full as exc:
            raise TimeoutError("native SQLite lifecycle command queue is full") from exc
        self._signal_control()
        if not done.wait(timeout):
            self.raise_if_failed()
            raise TimeoutError("timed out waiting for native SQLite event flush")
        self.raise_if_failed()
        return self.stats

    def _signal_control(self) -> None:
        fd = self._event_fd
        if fd is None:
            raise RuntimeError("native SQLite eventfd is not ready")
        while True:
            try:
                os.eventfd_write(fd, 1)
                return
            except InterruptedError:
                continue

    def _set_stat(self, key: str, value: int | bool | str | None) -> None:
        with self._condition:
            self._stats[key] = value
            self._condition.notify_all()

    def _add_stat(self, key: str, amount: int = 1) -> None:
        with self._condition:
            self._stats[key] = int(self._stats[key] or 0) + amount
            self._condition.notify_all()

    def _inotify_init(self) -> int:
        libc = ctypes.CDLL(None, use_errno=True)
        init1 = libc.inotify_init1
        init1.argtypes = [ctypes.c_int]
        init1.restype = ctypes.c_int
        fd = init1(os.O_CLOEXEC | os.O_NONBLOCK)
        if fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return fd

    def _add_watch(self, path: Path, mask: int, kind: str) -> int:
        assert self._inotify_fd is not None
        before = path.stat(follow_symlinks=False)
        libc = ctypes.CDLL(None, use_errno=True)
        add_watch = libc.inotify_add_watch
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        wd = add_watch(self._inotify_fd, os.fsencode(path), mask)
        if wd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(path))
        after = path.stat(follow_symlinks=False)
        identity = (before.st_dev, before.st_ino)
        if identity != (after.st_dev, after.st_ino):
            raise WatcherFailed(f"{kind} path changed while installing its inotify watch")
        self._watch_kinds[wd] = kind
        self._watch_identities[kind] = identity
        return wd

    def _setup(self) -> None:
        self._stage = "setup_native_watches"
        self._inotify_fd = self._inotify_init()
        self._directory_wd = self._add_watch(self.db_path.parent, _DIR_MASK, "directory")
        self._db_wd = self._add_watch(self.db_path, _FILE_MASK, "database")
        wal_path = Path(str(self.db_path) + "-wal")
        if wal_path.exists():
            self._wal_wd = self._add_watch(wal_path, _FILE_MASK, "wal")
        self._event_fd = os.eventfd(0, os.EFD_CLOEXEC | os.EFD_NONBLOCK)
        self._epoll = select.epoll()
        self._epoll.register(self._inotify_fd, select.EPOLLIN)
        self._epoll.register(self._event_fd, select.EPOLLIN)

        self._stage = "setup_sqlite_connections"
        timeout = self.busy_timeout_ms / 1000
        self._reader = sqlite3.connect(str(self.db_path), timeout=timeout, isolation_level=None)
        self._reader.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        journal_mode = str(self._reader.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if journal_mode != "wal":
            raise RuntimeError(f"database must already be in WAL mode, got {journal_mode!r}")
        self._reader.execute("PRAGMA query_only=ON")
        self._barrier = sqlite3.connect(str(self.db_path), timeout=timeout, isolation_level=None)
        self._barrier.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")

    def _run(self) -> None:
        ready_signaled = False

        def signal_startup_ready() -> None:
            nonlocal ready_signaled
            self._set_stat("startup_catchups", 1)
            self._set_stat("healthy", True)
            self._ready.set()
            ready_signaled = True

        try:
            self._setup()
            # The reader sentinel takes one head snapshot only after all watches
            # exist. A busy writer may delay this initial snapshot, but the
            # source must not become ready or healthy until it succeeds.
            self._dirty = True
            self._dirty_since = time.monotonic()
            self._attempt_reconcile(reason="startup")
            if self._retry_at is None and not self._dirty:
                signal_startup_ready()
            assert self._epoll is not None and self._inotify_fd is not None and self._event_fd is not None
            while True:
                self._stage = "wait_native_event"
                timeout = -1 if self._retry_at is None else max(0, self._retry_at - time.monotonic())
                ready = self._epoll.poll(timeout)
                if not ready and self._retry_at is not None:
                    # The timer exists only while a filesystem invalidation is
                    # dirty. Drain queued events first so overflow, inode
                    # replacement, and WAL recreation remain fail-closed.
                    self._drain_pending_events_before_publish()
                    if self._dirty and self._retry_at is not None and time.monotonic() >= self._retry_at:
                        self._attempt_reconcile(reason="busy_retry")
                        if not ready_signaled and self._retry_at is None and not self._dirty:
                            signal_startup_ready()
                    continue
                should_stop = False
                for fd, _mask in ready:
                    if fd == self._inotify_fd:
                        self._stage = "read_native_event"
                        data = self._read_once()
                        if data:
                            self._process_data(data)
                    elif fd == self._event_fd:
                        try:
                            os.eventfd_read(self._event_fd)
                        except BlockingIOError:
                            pass
                        should_stop = self._run_commands()
                if (not should_stop and self._dirty and self._retry_at is not None
                        and time.monotonic() >= self._retry_at):
                    self._drain_pending_events_before_publish()
                    self._attempt_reconcile(reason="busy_retry")
                    if not ready_signaled and self._retry_at is None and not self._dirty:
                        signal_startup_ready()
                if should_stop and not ready_signaled:
                    raise WatcherFailed("native SQLite event source stopped before its startup cursor snapshot")
                if should_stop or self.failure is not None:
                    break
        except BaseException as exc:
            self._fail(exc)
        finally:
            if not ready_signaled:
                self._ready.set()
            self._cleanup()
            self._set_stat("watcher_stopped", True)
            self._thread_done.set()
            while True:
                try:
                    _kind, done = self._commands.get_nowait()
                except queue.Empty:
                    break
                if done is not None:
                    done.set()

    def _run_commands(self) -> bool:
        self._stage = "control_command"
        stop = False
        while True:
            try:
                command, done = self._commands.get_nowait()
            except queue.Empty:
                break
            try:
                if command == "flush":
                    self._flush_inotify_queue()
                elif command == "stop":
                    stop = True
                else:
                    raise RuntimeError(f"unknown native SQLite command {command!r}")
            except BaseException as exc:
                self._fail(exc)
                stop = True
            finally:
                if done is not None:
                    done.set()
        return stop

    def _read_once(self) -> bytes:
        assert self._inotify_fd is not None
        while True:
            try:
                return os.read(self._inotify_fd, self.read_buffer_bytes)
            except InterruptedError:
                continue
            except BlockingIOError:
                return b""

    def _flush_inotify_queue(self) -> None:
        self._stage = "flush_native_events"
        assert self._inotify_fd is not None
        if fcntl is None or _FIONREAD is None:
            raise RuntimeError("inotify flush requires Linux fcntl support")
        available_raw = fcntl.ioctl(self._inotify_fd, _FIONREAD, struct.pack("I", 0))
        available = struct.unpack("I", available_raw)[0]
        if available > self.max_flush_bytes:
            raise WatcherFailed(f"queued inotify batch {available} exceeds flush limit {self.max_flush_bytes}")
        if available:
            chunks: list[bytes] = []
            remaining = available
            while remaining:
                try:
                    chunk = os.read(self._inotify_fd, min(self.read_buffer_bytes, remaining))
                except InterruptedError:
                    continue
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if data:
                self._process_data(data)

    def _process_data(self, data: bytes, *, reconcile: bool = True) -> None:
        self._stage = "process_native_events"
        dirty = False
        relevant = 0
        offset = 0
        while offset < len(data):
            if len(data) - offset < _EVENT_HEADER.size:
                raise WatcherFailed("truncated inotify event header")
            wd, mask, _cookie, name_len = _EVENT_HEADER.unpack_from(data, offset)
            offset += _EVENT_HEADER.size
            end = offset + name_len
            if end > len(data):
                raise WatcherFailed("truncated inotify event name")
            name = os.fsdecode(data[offset:end].split(b"\0", 1)[0]) if name_len else ""
            offset = end
            if mask & IN_Q_OVERFLOW:
                raise WatcherFailed("IN_Q_OVERFLOW: SQLite commit invalidations may have been lost")

            kind = self._watch_kinds.get(wd)
            if kind is None:
                if mask & IN_IGNORED:
                    raise WatcherFailed(f"IN_IGNORED for unknown watch descriptor {wd}")
                continue
            if kind == "directory":
                if mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED):
                    raise WatcherFailed("watched database directory was removed or moved")
                if name == self._db_name and mask & _REPLACEMENT_MASK:
                    raise WatcherFailed("database directory entry changed; native subscriptions failed closed")
                if name == self._wal_name:
                    if mask & (IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO):
                        raise WatcherFailed("WAL directory entry was removed or replaced")
                    if mask & IN_CREATE:
                        if self._wal_wd is not None:
                            raise WatcherFailed("WAL file was recreated while an old inode watch remained")
                        self._wal_wd = self._add_watch(Path(str(self.db_path) + "-wal"), _FILE_MASK, "wal")
                        # Commits can precede handling this queued CREATE event.
                        # Reconcile after installing the inode watch so that those
                        # writes cannot be stranded without another notification.
                        dirty = True
                        relevant += 1
                    if mask & IN_ATTRIB:
                        dirty = True
                        relevant += 1
                continue
            if kind in {"database", "wal"}:
                if mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED):
                    noun = "database" if kind == "database" else "WAL"
                    raise WatcherFailed(f"{noun} inode was removed or replaced")
                if mask & (IN_MODIFY | IN_CLOSE_WRITE | IN_ATTRIB):
                    dirty = True
                    relevant += 1
                continue
            raise WatcherFailed(f"unknown inotify watch kind {kind!r}")

        if relevant:
            self._add_stat("inotify_batches")
            self._add_stat("relevant_events", relevant)
        if dirty:
            self._dirty = True
            if self._dirty_since is None:
                self._dirty_since = time.monotonic()
            if reconcile and self._retry_at is None:
                self._attempt_reconcile(reason="inotify")

    def _drain_pending_events_before_publish(self) -> None:
        """Drain a bounded native-event snapshot before releasing the barrier."""
        assert self._inotify_fd is not None
        if fcntl is None or _FIONREAD is None:
            raise RuntimeError("pre-publish inotify validation requires Linux fcntl support")
        self._stage = "validate_pending_native_events"
        started = time.monotonic()
        total = 0
        while True:
            available = struct.unpack(
                "I", fcntl.ioctl(self._inotify_fd, _FIONREAD, struct.pack("I", 0))
            )[0]
            if available == 0:
                return
            if total + available > self.max_flush_bytes:
                raise WatcherFailed("queued inotify events exceed the pre-publish bound")
            try:
                chunk = os.read(self._inotify_fd, min(self.read_buffer_bytes, available))
            except InterruptedError:
                continue
            if not chunk:
                continue
            total += len(chunk)
            self._process_data(chunk, reconcile=False)
            if time.monotonic() - started > 1.0:
                raise WatcherFailed("pre-publish inotify validation exceeded its time bound")

    def _verify_watched_paths(self) -> None:
        paths = {"directory": self.db_path.parent, "database": self.db_path}
        if self._wal_wd is not None:
            paths["wal"] = Path(str(self.db_path) + "-wal")
        for kind, path in paths.items():
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise WatcherFailed(f"watched {kind} path is unavailable") from exc
            if self._watch_identities.get(kind) != (info.st_dev, info.st_ino):
                raise WatcherFailed(f"watched {kind} path was replaced")

    def _attempt_reconcile(self, *, reason: str) -> None:
        if (self._retry_at is not None and self._dirty_since is not None
                and time.monotonic() >= self._dirty_since + self.busy_retry_deadline_ms / 1000):
            assert self._last_busy_error is not None
            self._stage = "acquire_write_barrier"
            raise self._last_busy_error
        try:
            self._reconcile(reason=reason)
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            if (self._stage != "acquire_write_barrier" or not isinstance(code, int)
                    or code & 0xFF != sqlite3.SQLITE_BUSY):
                raise
            now = time.monotonic()
            assert self._dirty_since is not None
            if now - self._dirty_since >= self.busy_retry_deadline_ms / 1000:
                raise
            self._last_busy_error = exc
            self._retry_at = min(
                now + self.busy_retry_interval_ms / 1000,
                self._dirty_since + self.busy_retry_deadline_ms / 1000,
            )
            self._set_stat("degraded", True)
            self._add_stat("busy_retry_count")
            return
        self._dirty = False
        self._dirty_since = None
        self._retry_at = None
        self._last_busy_error = None
        self._set_stat("degraded", False)

    def _reconcile(self, *, reason: str) -> None:
        assert self._barrier is not None and self._reader is not None
        began = False
        try:
            assert self._dirty_since is not None
            retry_deadline = self._dirty_since + self.busy_retry_deadline_ms / 1000
            remaining_ms = max(0, int((retry_deadline - time.monotonic()) * 1000))
            attempt_timeout_ms = min(
                self.busy_timeout_ms, self.busy_retry_interval_ms, remaining_ms)
            self._barrier.execute(f"PRAGMA busy_timeout={attempt_timeout_ms}")
            self._stage = "acquire_write_barrier"
            self._barrier.execute("BEGIN IMMEDIATE")
            began = True
            self._assert_retry_deadline(include_initial=reason == "startup")
            self._stage = "read_committed_cursor"
            recipients, next_cursor = self.read_after(self._reader, self._cursor)
            self._stage = "validate_cursor"
            selected = tuple(recipients)
            if isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor < 0:
                raise WatcherFailed("message cursor adapter returned an invalid high-water mark")
            if self._cursor is None:
                if selected:
                    raise WatcherFailed("startup cursor snapshot must not replay historical recipients")
            elif next_cursor < self._cursor:
                raise WatcherFailed("message cursor adapter returned a backwards cursor")
            elif selected and next_cursor <= self._cursor:
                raise WatcherFailed("recipient rows did not advance the message cursor")
            self._add_stat("snapshot_reads")
            self._drain_pending_events_before_publish()
            self._verify_watched_paths()
            self._assert_retry_deadline()
            self._stage = "release_write_barrier"
            self._barrier.execute("ROLLBACK")
            began = False
            self._dirty = False
            self._dirty_since = None
            self._cursor = next_cursor
            self._set_stat("cursor", next_cursor)
            if selected:
                self._stage = "publish_invalidation"
                self.deliver(selected, next_cursor)
                self._add_stat("deliveries")
        except BaseException:
            if began:
                try:
                    self._barrier.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise

    def _assert_retry_deadline(self, *, include_initial: bool = False) -> None:
        if (self._dirty_since is not None
                and (self._retry_at is not None or include_initial)
                and time.monotonic() >= self._dirty_since + self.busy_retry_deadline_ms / 1000):
            self._stage = "acquire_write_barrier"
            if self._last_busy_error is not None:
                raise self._last_busy_error
            raise WatcherFailed("SQLite reconciliation exceeded its busy retry deadline")

    def _fail(self, exc: BaseException) -> None:
        with self._condition:
            first_failure = self._failure is None
            if first_failure:
                self._failure = exc
                self._stats["healthy"] = False
                self._stats["degraded"] = False
                self._stats["failure"] = f"{type(exc).__name__}: {exc}"
                self._stats["failure_stage"] = self._stage
                self._stats.update(_sqlite_error_details(exc))
                self._condition.notify_all()
        if first_failure and self.on_error is not None:
            try:
                self.on_error(exc)
            except Exception:
                pass
        if self._event_fd is not None and self._thread is not threading.current_thread():
            try:
                self._signal_control()
            except (OSError, RuntimeError):
                pass

    def _cleanup(self) -> None:
        for conn in (self._reader, self._barrier):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        self._reader = self._barrier = None
        if self._epoll is not None:
            try:
                self._epoll.close()
            except OSError:
                pass
            self._epoll = None
        for fd_name in ("_inotify_fd", "_event_fd"):
            fd = getattr(self, fd_name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_name, None)
        self._watch_kinds.clear()


def sys_platform_linux() -> bool:
    return os.name == "posix" and hasattr(os, "eventfd") and hasattr(select, "epoll")
