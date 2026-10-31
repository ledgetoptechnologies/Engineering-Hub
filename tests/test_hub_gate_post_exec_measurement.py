"""Real file/descriptor checks in a synthetic proc tree; no host trust claim."""
import hashlib
import os
from pathlib import Path
import sys
import time

import pytest
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor import launch_supervisor as launch


@pytest.mark.parametrize('bootstrap', [False, True])
@pytest.mark.parametrize('fault', ['none', 'digest', 'argv', 'inode', 'flags', 'symlink'])
def test_measurement_requires_expected_executable_argv_and_mount(tmp_path, fault, bootstrap):
    root = tmp_path / 'root'; target = root / 'var/lib/hub'; target.mkdir(parents=True)
    proc = tmp_path / 'proc'; current = proc / str(os.getpid()); current.mkdir(parents=True)
    (current / 'root').symlink_to(root, target_is_directory=True)
    (current / 'exe').symlink_to(Path(sys.executable).resolve())
    argv = ([b'/usr/local/libexec/hub-application-gate'] if bootstrap else
            [b'/opt/venv/bin/python', b'-I', b'-B', b'-m', b'engineering_hub',
             b'--host', b'0.0.0.0', b'--port', b'8770'])
    (current / 'cmdline').write_bytes(b'\0'.join(argv) + b'\0')
    info = target.stat(); bits = os.statvfs(target).f_flag
    flags = sorted(name for name, bit in [('nodev', os.ST_NODEV), ('noexec', os.ST_NOEXEC),
                                           ('nosuid', os.ST_NOSUID)] if bits & bit)
    if not bits & os.ST_RDONLY: flags.append('rw')
    pins = {'expected_device': info.st_dev, 'expected_inode': info.st_ino, 'mount_flags': flags}
    with open(Path(sys.executable).resolve(), 'rb') as stream:
        digest = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
    if fault == 'digest': digest = 'sha256:' + '0' * 64
    if fault == 'argv': (current / 'cmdline').write_bytes(b'/bin/other\0')
    if fault == 'inode': pins['expected_inode'] += 1
    if fault == 'flags': pins['mount_flags'] = []
    if fault == 'symlink':
        target.rmdir(); target.symlink_to(tmp_path, target_is_directory=True)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    try:
        if fault == 'none':
            result = launch._measure_gate_process(peer, pins, digest, time.monotonic() + 3, proc_root=proc, bootstrap=bootstrap)
            assert result['executable_digest'] == digest and result['inode'] == info.st_ino
        else:
            with pytest.raises((launch.LaunchSupervisorError, OSError)):
                launch._measure_gate_process(peer, pins, digest, time.monotonic() + 3, proc_root=proc, bootstrap=bootstrap)
    finally:
        peer.close()
