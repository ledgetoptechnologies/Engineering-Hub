"""Static decision checks against independent OpenSSL Ed25519 signatures."""
import ctypes
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


class Decision(ctypes.Structure):
    _fields_ = [('issued_at_ms', ctypes.c_uint64), ('expires_at_ms', ctypes.c_uint64)]


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('gcc') and shutil.which('openssl'), 'requires Linux tools')
class DecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hub-gate-decision-'); cls.dir = pathlib.Path(cls.tmp.name)
        root = pathlib.Path(__file__).resolve().parents[1] / 'hub_container_executor/static_gate'
        lib = cls.dir / 'decision.so'
        subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        *(str(root / n) for n in ('json.c', 'wire.c', 'decision.c')), '-lcrypto', '-o', str(lib)],
                       check=True, timeout=30)
        cls.lib = ctypes.CDLL(str(lib)); c = ctypes.c_char_p; z = ctypes.c_size_t; u = ctypes.c_uint64
        cls.lib.gate_decision_verify.argtypes = [c, z, c, c, z, u, u, u, u, ctypes.POINTER(Decision)]
        cls.lib.gate_ack_verify.argtypes = [c, z, c, c, z, c, z, u, u, u, u]
        cls.key = cls.dir / 'key'; pub = cls.dir / 'pub'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(cls.key)], check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(cls.key), '-pubout', '-outform', 'DER', '-out', str(pub)], check=True, capture_output=True)
        cls.pub = pub.read_bytes()[-32:]

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def raw(self, obj): return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()

    def sign(self, payload, domain):
        msg, sig = self.dir / 'msg', self.dir / 'sig'; msg.write_bytes(domain + self.raw(payload))
        subprocess.run(['openssl', 'pkeyutl', '-sign', '-rawin', '-inkey', str(self.key), '-in', str(msg), '-out', str(sig)],
                       check=True, capture_output=True, timeout=10)
        return self.raw({'payload': payload, 'signature': sig.read_bytes().hex()})

    def identity(self):
        uid = '12345678-1234-1234-1234-123456789abc'; digest = 'sha256:' + 'a' * 64
        return dict(boot_id=uid, container_id='b'*64, expected_device=4, expected_inode=5, fence=1,
                    generation_id='test', host_pid=42, image_config_id=digest, implementation_commit='c'*40,
                    launch_id=uid, manifest_digest=digest, mount_flags=['nodev', 'rw'], mount_target='/var/lib/hub',
                    nonce='d'*64, operation_id=uid, platform_manifest_digest=digest, process_start_ticks=1,
                    run_id=uid, runnable_digest=digest)

    def make(self, identity):
        value = dict(identity, protocol='engineering-hub.mount-gate-decision.v1', audience='engineering-hub.application-bootstrap',
                     decision='allow', observed_device=4, observed_inode=5, issued_at_ms=1000, expires_at_ms=5000)
        return self.sign(value, b'engineering-hub.mount-gate-decision.v1\0')

    def verify(self, wire, identity, times=(1000, 100, 2000, 110), key=None):
        i = self.raw(identity); out = Decision()
        return self.lib.gate_decision_verify(wire, len(wire), key or self.pub, i, len(i), *times, ctypes.byref(out))

    def ack(self, wire, identity, **changes):
        payload = dict(protocol='engineering-hub.application-gate-consume-ack.v1',
                       audience='engineering-hub.static-application-bootstrap', nonce=identity['nonce'],
                       launch_id=identity['launch_id'], operation_id=identity['operation_id'], fence=identity['fence'],
                       expires_at_ms=5000, decision_sha256=hashlib.sha256(wire).hexdigest())
        payload.update(changes)
        return self.sign(payload, b'engineering-hub-application-gate-consume-ack-v1\0')

    def verify_ack(self, ack, wire, identity, times=(1000, 100, 2000, 110)):
        i = self.raw(identity)
        return self.lib.gate_ack_verify(ack, len(ack), self.pub, i, len(i), wire, len(wire), *times)

    def test_decision_valid_and_denials(self):
        identity = self.identity(); wire = self.make(identity)
        self.assertEqual(self.verify(wire, identity), 1)
        self.assertEqual(self.verify(wire, dict(identity, fence=2)), 0)
        self.assertEqual(self.verify(wire, identity, key=b'\0'*32), 0)
        self.assertEqual(self.verify(wire, identity, times=(1000, 100, 5000, 110)), 0)
        self.assertEqual(self.verify(wire, identity, times=(1000, 100, 2000, 99)), 0)
        invalid = dict(identity, fence=True)
        self.assertEqual(self.verify(self.make(invalid), invalid), 0)
        self.assertEqual(self.verify(self.make(dict(identity, expected_inode=6)), dict(identity, expected_inode=6)), 0)

    def test_ack_matches_consumed_exact_decision(self):
        identity = self.identity(); wire = self.make(identity); ack = self.ack(wire, identity)
        self.assertEqual(self.verify_ack(ack, wire, identity), 1)
        self.assertEqual(self.verify_ack(self.ack(wire, identity, fence=2), wire, identity), 0)
        self.assertEqual(self.verify_ack(self.ack(wire, identity, decision_sha256='0'*64), wire, identity), 0)
        self.assertEqual(self.verify_ack(self.ack(wire, identity, extra='x'), wire, identity), 0)
        self.assertEqual(self.verify_ack(ack, wire, identity, times=(1000, 100, 5000, 110)), 0)


if __name__ == '__main__': unittest.main()
