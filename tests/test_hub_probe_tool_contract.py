"""Probe discovery sets must match the actual Hub tool registration contract."""
import ast
from pathlib import Path

import pytest

from tests.test_hub_unified_mcp import BUS_TOOLS, HUB_TOOLS


ROOT = Path(__file__).parents[1]


def literal_assignment(source, name):
    tree = ast.parse(source)
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)]
    assert len(values) == 1
    return ast.literal_eval(values[0])


@pytest.mark.parametrize('script,field', [
    ('hub_dashboard_container_smoke.py', 'expected_tools'),
    ('hub_split_container_smoke.py', 'expected_tools'),
    ('hub_unified_container_smoke.py', 'HUB'),
    ('hub_settings_container_smoke.py', 'HUB'),
])
def test_embedded_probe_hub_names_match_native_discovery_contract(script, field):
    source = (ROOT / 'scripts' / script).read_text()
    probe = literal_assignment(source, 'PROBE')
    assert literal_assignment(probe, field) == HUB_TOOLS
    if field == 'HUB':
        # The legacy bus remains24; only the composed Hub gained planning tools.
        assert literal_assignment(probe, 'BUS') == BUS_TOOLS
