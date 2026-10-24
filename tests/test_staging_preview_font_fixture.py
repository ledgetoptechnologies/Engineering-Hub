"""Keep the checked-in original font bytes reproducible without font tooling."""
import ast
import base64
from pathlib import Path
import runpy


def test_original_browser_font_matches_standard_library_generator():
    fixtures = Path(__file__).parent / 'fixtures'
    generator = runpy.run_path(str(fixtures / 'generate_staging_preview_font.py'))
    tree = ast.parse((fixtures / 'staging_preview_browser_server.py').read_text(encoding='utf-8'))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'FONT'
                              for target in node.targets))
    encoded = ast.literal_eval(assignment.value.args[0])
    actual = base64.b64decode(encoded, validate=True)
    assert actual == generator['build']()
    assert len(actual) < 1024
    assert generator['checksum'](actual) == 0xb1b0afba
