"""Cross-language verification of signed static gate start enrollment."""
import ctypes
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.test_hub_static_gate_json import Doc


@unittest.skipUnless(shutil.which('gcc'), 'requires a local C compiler')
class StartEnrollmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hub-gate-start-')
        root = pathlib.Path(__file__).resolve().parents[1] / 'hub_container_executor/static_gate'
        lib = pathlib.Path(cls.tmp.name) / 'start.so'
        subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        *(str(root / n) for n in ('json.c', 'identity.c', 'wire.c', 'enrollment.c')),
                        '-lcrypto', '-o', str(lib)], check=True, timeout=30)
        cls.lib = ctypes.CDLL(str(lib))
        cls.lib.gate_start_verify.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64,
            ctypes.c_uint64, ctypes.c_uint64, ctypes.POINTER(Doc), ctypes.POINTER(ctypes.c_size_t)]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_signed_enrollment_and_failure_cases(self):
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        uuid = '12345678-1234-1234-1234-123456789abc'
        digest = 'sha256:' + 'a'*64
        ident = dict(nonce='b'*64, launch_id=uuid, operation_id=uuid, run_id=uuid, boot_id=uuid,
            fence=1, manifest_digest=digest, image_config_id=digest, generation_id='g1',
            expected_device=42, expected_inode=99, container_id='c'*64, host_pid=7,
            process_start_ticks=8, mount_target='/var/lib/hub', mount_flags=['rw'],
            runnable_digest=digest, platform_manifest_digest=digest, implementation_commit='f'*40)
        payload = dict(protocol='engineering-hub.application-gate-start-enrollment.v1',
            audience='engineering-hub.static-application-bootstrap', identity=ident,
            bootstrap_digest=digest, issued_at_ms=10000, expires_at_ms=12000)
        def canonical(v): return json.dumps(v, sort_keys=True, separators=(',', ':')).encode()
        def wire(p):
            sig = key.sign(b'engineering-hub-application-gate-start-enrollment-v1\0' + canonical(p)).hex()
            return canonical(dict(payload=p, signature=sig))
        def verify(p, *, public=pub, nonce=b'b'*64, executable=digest.encode(), device=42,
                   inode=99, wall=10000, elapsed=100, now=10100):
            raw = wire(p); doc = Doc(); idx = ctypes.c_size_t()
            return self.lib.gate_start_verify(raw, len(raw), public, nonce, executable, device, inode,
                                               wall, elapsed, now, ctypes.byref(doc), ctypes.byref(idx))
        self.assertEqual(verify(payload), 1)
        for kwargs in (dict(public=b'\0'*32), dict(nonce=b'c'*64), dict(device=43),
                       dict(executable=('sha256:'+'d'*64).encode()), dict(now=12000),
                       dict(elapsed=2000), dict(now=9999)):
            with self.subTest(kwargs=kwargs): self.assertEqual(verify(payload, **kwargs), 0)
        self.assertEqual(verify(dict(payload, unexpected=1)), 0)
        self.assertEqual(verify(dict(payload, identity=dict(ident, fence=0))), 0)


if __name__ == '__main__': unittest.main()
