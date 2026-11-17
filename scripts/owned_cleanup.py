"""Start processes and containers a tool owns, and prove they are gone afterwards.

Used by scripts/local_premerge.py and scripts/hub_pidfd_slim_check.py. Only
things this module started (or containers carrying a label/name/image the
caller created) are ever stopped; nothing is matched by program name or port.

- Local processes: a Windows job object (every descendant joins it; the job is
  terminated and its active-process count must reach zero) or a POSIX session
  (the whole process group is signalled and must disappear).
- WSL: the step runs under `setsid`, records its session id in a file, and
  cleanup kills that session inside WSL and checks `pgrep -s` is empty. Linux
  never reuses a pid as a new session id while members of the old one live.
- Containers: removed by an owned label, an owned name, or the run's unique
  image tag (`ancestor=`), then listed again to prove they are gone.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

WINDOWS = os.name == 'nt'


class Cancelled(Exception):
    """The operator interrupted the run (Ctrl+C); owned work was cleaned up."""


class StepTimeout(Exception):
    """A step exceeded its time limit; owned work was cleaned up."""


class CleanupError(RuntimeError):
    """Something this run started could not be proven gone."""

    def __init__(self, leftovers):
        self.leftovers = list(leftovers)
        super().__init__('cleanup not proven: ' + '; '.join(self.leftovers))


if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _ntdll = ctypes.WinDLL('ntdll')
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                               wintypes.DWORD, wintypes.LPVOID)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _CREATE_SUSPENDED = 0x00000004
    _KILL_ON_JOB_CLOSE = 0x00002000

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _BasicLimit), ('IoInfo', _IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    class _Accounting(ctypes.Structure):
        _fields_ = [('TotalUserTime', ctypes.c_int64), ('TotalKernelTime', ctypes.c_int64),
                    ('ThisPeriodTotalUserTime', ctypes.c_int64), ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                    ('TotalPageFaultCount', wintypes.DWORD), ('TotalProcesses', wintypes.DWORD),
                    ('ActiveProcesses', wintypes.DWORD), ('TotalTerminatedProcesses', wintypes.DWORD)]


class ProcessTree:
    """One owned child process plus everything it starts."""

    def __init__(self, argv, *, cwd=None, env=None, stdout=None):
        self.argv = [str(a) for a in argv]
        self.job = None
        if WINDOWS:
            job = _k32.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), 'CreateJobObjectW failed')
            limits = _ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                _k32.CloseHandle(job)
                raise OSError(ctypes.get_last_error(), 'SetInformationJobObject failed')
            self.job = job
            # Start suspended so the process cannot spawn anything before it is in the job.
            self.proc = subprocess.Popen(self.argv, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL,
                                         creationflags=_CREATE_SUSPENDED | subprocess.CREATE_NEW_PROCESS_GROUP)
            if not _k32.AssignProcessToJobObject(job, int(self.proc._handle)):
                error = ctypes.get_last_error()
                self.proc.kill()
                raise OSError(error, 'AssignProcessToJobObject failed')
            _ntdll.NtResumeProcess(int(self.proc._handle))
        else:
            self.proc = subprocess.Popen(self.argv, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, start_new_session=True)
            self.pgid = self.proc.pid
        self.pid = self.proc.pid

    def wait(self, timeout):
        """Return the exit code; raise StepTimeout or Cancelled (KeyboardInterrupt)."""
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    return self.proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise StepTimeout(f'exceeded {timeout:g}s') from None
        except KeyboardInterrupt:
            raise Cancelled('interrupted') from None

    def active(self):
        if WINDOWS:
            info = _Accounting()
            if not _k32.QueryInformationJobObject(self.job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                return -1
            return info.ActiveProcesses
        try:
            os.killpg(self.pgid, 0)
            return 1
        except ProcessLookupError:
            return 0
        except PermissionError:
            return 1

    def terminate(self, grace=5.0):
        """Stop the whole tree; return leftovers (empty list = proven gone)."""
        if WINDOWS:
            _k32.TerminateJobObject(self.job, 1)
        else:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.pgid, sig)
                except ProcessLookupError:
                    break
                end = time.monotonic() + (grace if sig == signal.SIGTERM else 2)
                while time.monotonic() < end and self.active():
                    self.proc.poll()
                    time.sleep(0.05)
                if not self.active():
                    break
        end = time.monotonic() + grace
        while time.monotonic() < end and self.active():
            self.proc.poll()
            time.sleep(0.05)
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        left = self.active()
        if left == 0:
            if WINDOWS and self.job:
                _k32.CloseHandle(self.job)
                self.job = None
            return []
        what = 'job' if WINDOWS else 'process group'
        return [f'{left if left > 0 else "unknown number of"} process(es) left in the {what} of '
                f'pid {self.pid} ({" ".join(self.argv[:3])})']


def run_owned(argv, *, log_path, timeout, cwd=None, env=None, after=None):
    """Run argv as an owned tree, appending output to log_path.

    Always terminates the tree and runs `after()` (extra owned cleanup returning
    leftovers). Raises CleanupError if anything is left, else StepTimeout or
    Cancelled if that happened, else returns (exit code, new output).
    """
    start = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    with open(log_path, 'ab') as log:
        log.write(('$ ' + ' '.join(map(str, argv)) + '\n').encode())
        log.flush()
        tree = None
        problem = None
        code = None
        try:
            tree = ProcessTree(argv, cwd=cwd, env=env, stdout=log)
            code = tree.wait(timeout)
        except (StepTimeout, Cancelled) as error:
            problem = error
        except KeyboardInterrupt:
            problem = Cancelled('interrupted')
        except OSError as error:
            log.write(f'{error}\n'.encode())
            code = -1
        finally:
            leftovers = tree.terminate() if tree else []
            if after:
                leftovers += after(killed=problem is not None)
            note = f'[{type(problem).__name__}: {problem}]' if problem else f'[exit {code}]'
            log.write(f'\n{note}\n'.encode())
            if leftovers:
                log.write(('[cleanup not proven] ' + '; '.join(leftovers) + '\n').encode())
    if leftovers:
        raise CleanupError(leftovers)
    if problem:
        raise problem
    with open(log_path, 'rb') as log:
        log.seek(start)
        output = log.read().decode('utf-8', errors='replace')
    return code, output


# ---------------------------------------------------------------- containers
def _docker(docker, *args, env=None, timeout=60):
    return subprocess.run([docker, *args], capture_output=True, text=True, env=env, timeout=timeout,
                          encoding='utf-8', errors='replace')


def owned_containers(docker, filters, env=None, exact_image=None):
    """Container ids matching ALL filters; with exact_image, only containers
    created from that exact image reference (an `ancestor=` filter alone also
    matches another tag that happens to share the image id)."""
    argv = ['ps', '-a', '--no-trunc', '--format', '{{.ID}}	{{.Image}}']
    for item in filters:
        argv += ['--filter', item]
    result = _docker(docker, *argv, env=env)
    if result.returncode:
        raise RuntimeError('docker ps failed: ' + result.stderr.strip()[:200])
    ids = []
    for line in result.stdout.splitlines():
        cid, _, image = line.partition('	')
        if cid and (exact_image is None or image == exact_image):
            ids.append(cid)
    return ids


def remove_containers(docker, filters, env=None, with_volumes=False, exact_image=None):
    """Remove containers matching ALL filters (each must be owned-specific).

    Returns leftovers: containers (and, with_volumes, their named volumes)
    still present after removal.
    """
    if not filters:
        raise ValueError('refusing to remove containers without an owned filter')
    try:
        ids = owned_containers(docker, filters, env, exact_image)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        return [f'could not list containers for {filters}: {error}']
    volumes = []
    if ids and with_volumes:
        inspect = _docker(docker, 'inspect', '--format',
                          '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}', *ids, env=env)
        volumes = sorted(set(inspect.stdout.split()))
    if ids:
        _docker(docker, 'rm', '-f', *ids, env=env, timeout=120)
    leftovers = []
    try:
        still = owned_containers(docker, filters, env, exact_image)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        return [f'could not re-list containers for {filters}: {error}']
    leftovers += [f'container {cid[:12]} ({", ".join(filters)})' for cid in still]
    for volume in volumes:
        _docker(docker, 'volume', 'rm', volume, env=env)
        if _docker(docker, 'volume', 'inspect', volume, env=env).returncode == 0:
            leftovers.append(f'volume {volume}')
    return leftovers


def pid_alive(pid):
    """True while pid is a live (non-zombie) process."""
    if WINDOWS:
        handle = _k32.OpenProcess(0x1000 | 0x00100000, False, int(pid))
        if not handle:
            return False
        try:
            return _k32.WaitForSingleObject(handle, 0) != 0
        finally:
            _k32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f'/proc/{int(pid)}/stat', encoding='ascii') as stat:
            return stat.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


def interrupt_main_after(seconds):
    """Deliver a real KeyboardInterrupt to the main thread later (tests)."""
    import _thread
    timer = threading.Timer(seconds, _thread.interrupt_main)
    timer.daemon = True
    timer.start()
    return timer
