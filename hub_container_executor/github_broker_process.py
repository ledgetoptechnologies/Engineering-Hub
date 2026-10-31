"""Fixed inherited-FD entrypoint for the separately supervised test broker."""
import asyncio
import os
import socket

from .github_broker_service import serve_enrolled_broker


async def _run(enrollment_fd, listener_fd, stop_fd, ready_fd, enrollment_digest):
    listener=socket.socket(fileno=listener_fd)
    stop=asyncio.Event(); loop=asyncio.get_running_loop(); invalid=False
    def stopped():
        nonlocal invalid
        try: data=os.read(stop_fd,2)
        except BlockingIOError: return
        if data not in (b'',b'S'): invalid=True
        stop.set()
        loop.remove_reader(stop_fd)
    def ready():
        nonlocal ready_fd
        if os.write(ready_fd,b'R')!=1: raise RuntimeError()
        os.close(ready_fd)
        ready_fd=None
    os.set_blocking(stop_fd,False)
    loop.add_reader(stop_fd,stopped)
    try:
        await serve_enrolled_broker(enrollment_fd=enrollment_fd,listener=listener,stop_event=stop,on_ready=ready,
                                    expected_enrollment_digest=enrollment_digest)
        if invalid: raise RuntimeError()
    finally:
        loop.remove_reader(stop_fd)
        listener.close()
        for fd in (enrollment_fd,stop_fd,ready_fd):
            try:
                if fd is not None: os.close(fd)
            except OSError: pass


def main(argv):
    try:
        if len(argv)!=5 or any(not value.isdecimal() for value in argv[:4]): raise ValueError()
        fds=list(map(int,argv[:4]));digest=argv[4]
        import re
        if re.fullmatch(r'sha256:[0-9a-f]{64}',digest) is None: raise ValueError()
        if len(set(fds))!=4 or any(fd<3 for fd in fds): raise ValueError()
        asyncio.run(_run(*fds,digest))
        return 0
    except BaseException:
        return 2
