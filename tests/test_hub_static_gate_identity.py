"""Static identity validation does not establish host authority or custody."""
import ctypes
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from tests.test_hub_static_gate_json import Doc


@unittest.skipUnless(shutil.which('gcc'), 'requires a local C compiler')
class GateIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hub-gate-identity-')
        root = pathlib.Path(__file__).resolve().parents[1] / 'hub_container_executor/static_gate'
        lib = pathlib.Path(cls.tmp.name) / 'identity.so'
        subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(root / 'json.c'), str(root / 'identity.c'), '-o', str(lib)],
                       check=True, timeout=30)
        cls.lib = ctypes.CDLL(str(lib))
        cls.lib.gate_json_parse.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(Doc)]
        cls.lib.gate_identity_validate.argtypes = [ctypes.POINTER(Doc), ctypes.c_size_t,
                                                    ctypes.c_char_p, ctypes.c_uint64, ctypes.c_uint64]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def check(self, identity):
        raw = json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()
        doc = Doc()
        self.assertEqual(self.lib.gate_json_parse(raw, len(raw), ctypes.byref(doc)), 1)
        return self.lib.gate_identity_validate(ctypes.byref(doc), 0, b'a' * 64, 42, 99)

    def test_exact_schema_and_mount_identity(self):
        ident = dict(nonce='a'*64, launch_id='1'*8+'-'+'1'*4+'-'+'1'*4+'-'+'1'*4+'-'+'1'*12,
            operation_id='2'*8+'-'+'2'*4+'-'+'2'*4+'-'+'2'*4+'-'+'2'*12,
            run_id='3'*8+'-'+'3'*4+'-'+'3'*4+'-'+'3'*4+'-'+'3'*12,
            boot_id='4'*8+'-'+'4'*4+'-'+'4'*4+'-'+'4'*4+'-'+'4'*12,
            fence=1, manifest_digest='sha256:'+'a'*64, image_config_id='sha256:'+'b'*64,
            generation_id='g-1', expected_device=42, expected_inode=99, container_id='c'*64,
            host_pid=7, process_start_ticks=8, mount_target='/var/lib/hub', mount_flags=['nodev','rw'],
            runnable_digest='sha256:'+'d'*64, platform_manifest_digest='sha256:'+'e'*64,
            implementation_commit='f'*40)
        self.assertEqual(self.check(ident), 1)
        for updates in ({'expected_inode': 100}, {'fence': 0}, {'host_pid': 2**63},
                        {'nonce': 'b'*64}, {'mount_flags': ['rw', 'nodev']},
                        {'mount_flags': ['nodev']}, {'mount_target': '/tmp'},
                        {'generation_id': '-bad'}, {'extra': 1}):
            with self.subTest(updates=updates):
                self.assertEqual(self.check(dict(ident, **updates)), 0)


if __name__ == '__main__':
    unittest.main()
