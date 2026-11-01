"""Fail-closed argument checks for the local static gate build."""
import pathlib
import ast
import importlib.util
import subprocess
import sys
import tempfile
import unittest


class GateBuildTests(unittest.TestCase):
    def test_static_elf_check_rejects_dynamic_segment_without_needed(self):
        path = pathlib.Path(__file__).resolve().parents[1] / 'scripts/build_hub_static_gate.py'
        spec = importlib.util.spec_from_file_location('gate_build', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        header = 'Class: ELF64\nMachine: Advanced Micro Devices X86-64\n'
        module.validate_elf_reports(header, '  LOAD 0x0\n', 'There is no dynamic section')
        for program, dynamic in [('  DYNAMIC 0x0\n', 'There is no dynamic section'),
                                 ('  INTERP 0x0\n', ''), ('  LOAD 0x0\n', ' (NEEDED)')]:
            with self.subTest(program=program, dynamic=dynamic):
                with self.assertRaises(RuntimeError): module.validate_elf_reports(header, program, dynamic)

    def test_image_entrypoint_matches_enrollment_contract(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        tree = ast.parse((root / 'hub_container_executor/application_gate_enrollment.py').read_text())
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'STATIC_ENTRYPOINT' for t in node.targets))
        expected = ast.literal_eval(assignment.value)
        dockerfile = (root / 'docker/Dockerfile.hub-gated').read_text()
        self.assertEqual(expected, ('/usr/local/libexec/hub-application-gate',))
        self.assertIn('ENTRYPOINT ["' + expected[0] + '"]', dockerfile)
        self.assertIn('chmod 0755 ' + expected[0], dockerfile)

    def test_rejects_missing_or_malformed_explicit_inputs(self):
        script = pathlib.Path(__file__).resolve().parents[1] / 'scripts/build_hub_static_gate.py'
        with tempfile.TemporaryDirectory(prefix='hub-gate-build-args-') as directory:
            out = pathlib.Path(directory)
            for key, revision in [('A' * 64, 'a' * 40), ('a' * 63, 'a' * 40), ('a' * 64, 'b' * 39)]:
                result = subprocess.run([sys.executable, str(script), '--public-key-hex', key,
                                         '--source-commit', revision, '--output-dir', str(out)],
                                        capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(out.iterdir()), [])
            result = subprocess.run([sys.executable, str(script), '--public-key-hex', 'a' * 64,
                                     '--source-commit', 'b' * 40, '--output-dir', str(out),
                                     '--channel-version', '4'], capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(out.iterdir()), [])


if __name__ == '__main__': unittest.main()
