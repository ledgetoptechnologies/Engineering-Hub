"""Standalone Linux user+PID namespace credential check; no installed deps.

Manual-only; this is not part of CI. Run in an unprivileged user namespace with
PID creation allowed, for example
``unshare -Ur -- python3 -B tests/inherited_pid_namespace_unprivileged.py``.
This extracts the actual packet reader from the checkout; it does not test the
full broker, production account mappings, or the separate root-only fixture.
"""
import array
import ast
import os
from pathlib import Path
import select
import signal
import socket
import struct
import time

source = Path(__file__).resolve().parents[1] / 'hub_container_executor' / 'broker_seqpacket.py'
tree = ast.parse(source.read_text(encoding='utf-8'))
selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
            and node.name in {'BrokerTransportError', '_require', '_read_packet'}]
assert {node.name for node in selected} == {'BrokerTransportError', '_require', '_read_packet'}
namespace = dict(array=array, os=os, socket=socket, _MAX_PACKET=32768, _CREDS=struct.Struct('=iII'))
exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)

left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
left.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
read_fd, write_fd = os.pipe()
ready_read, ready_write = os.pipe()
child = os.fork()
if child == 0:
    try:
        os.setsid()
        os.close(ready_read)
        os.write(ready_write, b'R')
        os.close(ready_write)
        right.close(); os.close(read_fd)
        try:
            os.unshare(os.CLONE_NEWPID)
        except (PermissionError, AttributeError):
            os.write(write_fd, b'unsupported')
            os._exit(77)
        grandchild = os.fork()
        if grandchild == 0:
            try:
                if not select.select([left], [], [], 5)[0]:
                    os._exit(2)
                raw, credentials = namespace['_read_packet'](left)
                expected = (0, os.getuid(), os.getgid())
                os.write(write_fd, b'pass' if raw == b'{}' and credentials == [expected] else b'fail')
                os._exit(0)
            except BaseException:
                os._exit(3)
        _, status = os.waitpid(grandchild, 0)
        os._exit(os.waitstatus_to_exitcode(status))
    except BaseException:
        os._exit(4)
left.close(); os.close(write_fd); os.close(ready_write)
ready = False
reaped = False
try:
    if not select.select([ready_read], [], [], 5)[0] or os.read(ready_read, 1) != b'R':
        raise RuntimeError('namespace child failed to establish its session')
    ready = True
    try:
        right.send(b'{}')
    except BrokenPipeError:
        # The child may already have reported unsupported namespace creation.
        pass
    if not select.select([read_fd], [], [], 8)[0]:
        raise RuntimeError('namespace test timed out')
    result = os.read(read_fd, 16)
    deadline = time.monotonic() + 3
    while True:
        waited, status = os.waitpid(child, os.WNOHANG)
        if waited:
            reaped = True
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('namespace child did not exit')
        time.sleep(0.01)
    if os.waitstatus_to_exitcode(status) == 77 and result == b'unsupported':
        print('unsupported: PID namespace creation unavailable')
        raise SystemExit(77)
    assert os.waitstatus_to_exitcode(status) == 0 and result == b'pass'
    print('actual packet reader: invisible sender PID 0, translated UID/GID matched')
finally:
    if not reaped:
        try:
            if ready:
                os.killpg(child, signal.SIGKILL)
            else:
                os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 3
        while True:
            waited, _ = os.waitpid(child, os.WNOHANG)
            if waited:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('namespace child could not be reaped')
            time.sleep(0.01)
    right.close(); os.close(read_fd); os.close(ready_read)
