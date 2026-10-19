"""The future Docker harness is import-safe and hard-disabled in this source phase."""
import importlib.util
from pathlib import Path


def test_harness_import_is_inert_and_execution_reports_pending_gate(capsys):
    source = Path(__file__).resolve().parents[1] / "docker" / "hub-container-pilot-acceptance.py"
    spec = importlib.util.spec_from_file_location("hub_pilot_acceptance_pending", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert capsys.readouterr().out == ""
    assert module.ACCEPTANCE_STATUS == "NOT_RUN_NO_DOCKER_BACKEND"
    assert module.main([]) == 2
    result = capsys.readouterr()
    assert "pending" in result.err.lower() or "blocked" in result.err.lower()
