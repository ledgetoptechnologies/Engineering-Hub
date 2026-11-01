"""Exercise the deliberately restrictive C parser without enabling a gate."""
import ctypes
import pathlib
import shutil
import subprocess
import tempfile
import unittest


class Token(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int), ('start', ctypes.c_size_t),
                ('end', ctypes.c_size_t), ('next', ctypes.c_size_t)]


class Doc(ctypes.Structure):
    _fields_ = [('bytes', ctypes.c_char_p), ('length', ctypes.c_size_t),
                ('count', ctypes.c_size_t), ('tokens', Token * 256)]


@unittest.skipUnless(shutil.which('gcc'), 'requires a local C compiler')
class GateJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hub-gate-json-')
        root = pathlib.Path(__file__).resolve().parents[1]
        lib = pathlib.Path(cls.tmp.name) / 'json.so'
        subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(root / 'hub_container_executor/static_gate/json.c'), '-o', str(lib)],
                       check=True, timeout=30)
        cls.lib = ctypes.CDLL(str(lib))
        cls.lib.gate_json_parse.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(Doc)]
        cls.lib.gate_json_get.argtypes = [ctypes.POINTER(Doc), ctypes.c_size_t, ctypes.c_char_p]
        cls.lib.gate_json_u64.argtypes = [ctypes.POINTER(Doc), ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint64)]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def parse(self, raw):
        doc = Doc()
        return self.lib.gate_json_parse(raw, len(raw), ctypes.byref(doc)), doc

    def test_nested_lookup_and_unsigned_boundaries(self):
        raw = b'{"a":{"b":1},"c":[0,18446744073709551615],"z":18446744073709551616}'
        ok, doc = self.parse(raw)
        self.assertEqual(ok, 1)
        a = self.lib.gate_json_get(ctypes.byref(doc), 0, b'a')
        b = self.lib.gate_json_get(ctypes.byref(doc), a, b'b')
        self.assertEqual(self.lib.gate_json_get(ctypes.byref(doc), 0, b'b'), -1)
        out = ctypes.c_uint64()
        self.assertEqual(self.lib.gate_json_u64(ctypes.byref(doc), b, ctypes.byref(out)), 1)
        self.assertEqual(out.value, 1)
        c = self.lib.gate_json_get(ctypes.byref(doc), 0, b'c')
        self.assertEqual(self.lib.gate_json_u64(ctypes.byref(doc), c + 2, ctypes.byref(out)), 1)
        self.assertEqual(out.value, 2**64 - 1)
        z = self.lib.gate_json_get(ctypes.byref(doc), 0, b'z')
        self.assertEqual(self.lib.gate_json_u64(ctypes.byref(doc), z, ctypes.byref(out)), 0)

    def test_reject_noncanonical_ambiguous_and_unsupported(self):
        cases = [b'{"a":1,"a":2}', b'{"b":1,"a":2}', b'{"a":true}', b'{"a":null}',
                 b'{"a":01}', b'{"a":-0}', b'{"a":1.0}', b'{ "a":1}', b'{"a":"x\\ny"}',
                 b'{"a":"\xc3\xa9"}', b'{"a":1}x', b'[1,]', b'', b'[' * 9 + b'0' + b']' * 9,
                 b'[' + b'0,' * 255 + b'0]', b' ' * 8193]
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                self.assertEqual(self.parse(raw)[0], 0)

    def test_negative_is_not_unsigned(self):
        ok, doc = self.parse(b'-1')
        self.assertEqual(ok, 1)
        out = ctypes.c_uint64()
        self.assertEqual(self.lib.gate_json_u64(ctypes.byref(doc), 0, ctypes.byref(out)), 0)


if __name__ == '__main__':
    unittest.main()
