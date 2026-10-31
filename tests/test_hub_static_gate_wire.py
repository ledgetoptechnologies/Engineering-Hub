"""Real Linux seqpacket and Ed25519 tests; no launcher is enabled."""
import array
import ctypes
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_hub_static_gate_json import Doc


class Timespec(ctypes.Structure):
    _fields_ = [('tv_sec', ctypes.c_long), ('tv_nsec', ctypes.c_long)]


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('gcc') and shutil.which('openssl'),
                     'requires Linux, gcc and openssl')
class GateWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hub-gate-wire-')
        cls.dir = pathlib.Path(cls.tmp.name)
        root = pathlib.Path(__file__).resolve().parents[1] / 'hub_container_executor/static_gate'
        lib = cls.dir / 'wire.so'
        subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(root / 'json.c'), str(root / 'wire.c'), '-lcrypto', '-o', str(lib)],
                       check=True, timeout=30)
        cls.lib = ctypes.CDLL(str(lib))
        cls.lib.gate_wire_send.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(Timespec)]
        cls.lib.gate_wire_recv.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
                                           ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(Timespec)]
        cls.lib.gate_wire_connect.argtypes = [ctypes.c_char_p, ctypes.POINTER(Timespec)]
        cls.lib.gate_wire_connect.restype = ctypes.c_int
        cls.lib.gate_wire_verify.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(Doc),
                                             ctypes.POINTER(ctypes.c_size_t)]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def deadline(self, seconds=1):
        ns = time.monotonic_ns() + int(seconds * 10**9)
        return Timespec(ns // 10**9, ns % 10**9)

    def receive(self, sock, cap=32, seconds=1):
        buffer = ctypes.create_string_buffer(cap)
        length = ctypes.c_size_t()
        deadline = self.deadline(seconds)
        result = self.lib.gate_wire_recv(sock.fileno(), buffer, cap, ctypes.byref(length), ctypes.byref(deadline))
        return result, buffer.raw[:length.value]

    def test_seqpacket_send_receive_deadline_and_truncation(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with a, b:
            deadline = self.deadline()
            self.assertEqual(self.lib.gate_wire_send(a.fileno(), b'hello', 5, ctypes.byref(deadline)), 1)
            self.assertEqual(self.receive(b), (1, b'hello'))
            a.send(b'x' * 33)
            self.assertEqual(self.receive(b, cap=32)[0], 0)
            self.assertEqual(self.receive(b, seconds=0.01)[0], 0)
            expired = self.deadline(-1)
            self.assertEqual(self.lib.gate_wire_send(a.fileno(), b'x', 1, ctypes.byref(expired)), 0)

    def test_path_connect_requires_socket_and_cloexec(self):
        path = self.dir / 'control.sock'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            listener.bind(str(path)); listener.listen(1)
            deadline = self.deadline()
            fd = self.lib.gate_wire_connect(os.fsencode(path), ctypes.byref(deadline))
            self.assertGreaterEqual(fd, 0)
            with socket.socket(fileno=fd) as client:
                server, _ = listener.accept()
                with server:
                    self.assertFalse(os.get_inheritable(client.fileno()))
                    client.send(b'test'); self.assertEqual(server.recv(16), b'test')
            alias = self.dir / 'alias.sock'; alias.symlink_to(path)
            self.assertEqual(self.lib.gate_wire_connect(os.fsencode(alias), ctypes.byref(self.deadline())), -1)
            self.assertEqual(self.lib.gate_wire_connect(b'relative', ctypes.byref(self.deadline())), -1)
            self.assertEqual(self.lib.gate_wire_connect(os.fsencode(path), ctypes.byref(self.deadline(-1))), -1)
        finally:
            listener.close()
            path.unlink(missing_ok=True)
            (self.dir / 'alias.sock').unlink(missing_ok=True)

    def test_rights_are_closed_and_rejected(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        read_fd, write_fd = os.pipe()
        try:
            before = len(os.listdir('/proc/self/fd'))
            a.sendmsg([b'x'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [read_fd, write_fd]))])
            self.assertEqual(self.receive(b)[0], 0)
            self.assertEqual(len(os.listdir('/proc/self/fd')), before)
        finally:
            a.close(); b.close(); os.close(read_fd); os.close(write_fd)

    def test_empty_packet_rights_are_closed_and_rejected(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        read_fd, write_fd = os.pipe()
        try:
            before = len(os.listdir('/proc/self/fd'))
            self.assertEqual(a.sendmsg([b''], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                               array.array('i', [read_fd, write_fd]))]), 0)
            self.assertEqual(self.receive(b)[0], 0)
            self.assertEqual(len(os.listdir('/proc/self/fd')), before)
        finally:
            a.close(); b.close(); os.close(read_fd); os.close(write_fd)

    def test_signed_exact_envelope_and_domain(self):
        key = self.dir / 'key.pem'; public = self.dir / 'public.der'; msg = self.dir / 'message'; sig = self.dir / 'sig'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(key)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-outform', 'DER', '-out', str(public)],
                       check=True, capture_output=True, timeout=10)
        domain = b'test-domain\0'; payload = b'{"number":1}'
        msg.write_bytes(domain + payload)
        subprocess.run(['openssl', 'pkeyutl', '-sign', '-rawin', '-inkey', str(key), '-in', str(msg), '-out', str(sig)],
                       check=True, capture_output=True, timeout=10)
        signature = sig.read_bytes().hex().encode()
        wire = b'{"payload":' + payload + b',"signature":"' + signature + b'"}'
        pub = public.read_bytes()[-32:]
        def verify(value, domain_value=domain, pub_value=pub):
            doc = Doc(); index = ctypes.c_size_t()
            return self.lib.gate_wire_verify(value, len(value), pub_value, domain_value, len(domain_value),
                                             ctypes.byref(doc), ctypes.byref(index))
        self.assertEqual(verify(wire), 1)
        self.assertEqual(verify(wire, b'wrong\0'), 0)
        self.assertEqual(verify(wire, pub_value=b'\0' * 32), 0)
        self.assertEqual(verify(wire.replace(b'number":1', b'number":2')), 0)
        self.assertEqual(verify(wire.replace(b',"signature":', b',"q":1,"signature":')), 0)
        self.assertEqual(verify(wire.replace(b'{"payload":', b'{ "payload":')), 0)


if __name__ == '__main__':
    unittest.main()
