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
    query when the inotify/eventfd descriptors are idle.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        read_after: Callable[[sqlite3.Connection, int | None], tuple[Sequence[str], int]],
        deliver: Callable[[Sequence[str], int], None],
        *,
        busy_timeout_ms: int = 10_000,
        read_buffer_bytes: int = 64 * 1024,
        max_flush_bytes: int = 8 * 1024 * 1024,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        if not sys_platform_linux():
            raise OSError("SQLiteEventSource requires Linux inotify, epoll, and eventfd")
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
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
        }
        self._failure: BaseException | None = None
        self._inotify_fd: int | None = None
        self._event_fd: int | None = None
        self._epoll: select.epoll | None = None
        self._watch_kinds: dict[int, str] = {}
        self._directory_wd: int | None = None
        self._db_wd: int | None = None
        self._wal_wd: int | None = None
        self._reader: sqlite3.Connection | None = None
        self._barrier: sqlite3.Connection | None = None
        self._cursor: int | None = None
        self._db_name = self.db_path.name
        self._wal_name = self.db_path.name + "-wal"

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
        if not self._ready.wait(self.busy_timeout_ms / 1000 + 10):
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
        libc = ctypes.CDLL(None, use_errno=True)
        add_watch = libc.inotify_add_watch
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        wd = add_watch(self._inotify_fd, os.fsencode(path), mask)
        if wd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(path))
        self._watch_kinds[wd] = kind
        return wd

    def _setup(self) -> None:
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
        try:
            self._setup()
            # The reader sentinel takes one head snapshot only after all watches exist.
            self._reconcile(reason="startup")
            self._set_stat("startup_catchups", 1)
            self._set_stat("healthy", True)
            self._ready.set()
            ready_signaled = True
            assert self._epoll is not None and self._inotify_fd is not None and self._event_fd is not None
            while True:
                ready = self._epoll.poll(-1)
                should_stop = False
                for fd, _mask in ready:
                    if fd == self._inotify_fd:
                        data = self._read_once()
                        if data:
                            self._process_data(data)
                    elif fd == self._event_fd:
                        try:
                            os.eventfd_read(self._event_fd)
                        except BlockingIOError:
                            pass
                        should_stop = self._run_commands()
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

    def _process_data(self, data: bytes) -> None:
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
            self._reconcile(reason="inotify")

    def _reconcile(self, *, reason: str) -> None:
        assert self._barrier is not None and self._reader is not None
        began = False
        try:
            self._barrier.execute("BEGIN IMMEDIATE")
            began = True
            recipients, next_cursor = self.read_after(self._reader, self._cursor)
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
            self._barrier.execute("ROLLBACK")
            began = False
            self._cursor = next_cursor
            self._set_stat("cursor", next_cursor)
            if selected:
                self.deliver(selected, next_cursor)
                self._add_stat("deliveries")
        except BaseException:
            if began:
                try:
                    self._barrier.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise

    def _fail(self, exc: BaseException) -> None:
        with self._condition:
            first_failure = self._failure is None
            if first_failure:
                self._failure = exc
                self._stats["healthy"] = False
                self._stats["failure"] = f"{type(exc).__name__}: {exc}"
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
