"""Source-contract checks only; these do not establish installed isolation.

Never invoke systemd, Docker, SSH, package tools or host preparation helpers here.
The deliberate small parser preserves repeated systemd directives.
"""

from pathlib import Path
import shlex

import pytest


ARTIFACTS = Path(__file__).resolve().parents[1] / "deployment" / "staging-bootstrap"


def _read(name):
    sections = {}
    section = None
    for raw in (ARTIFACTS / name).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        assert not line.endswith("\\"), "Review parser before adding continuations"
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            assert section not in sections, "Unexpected repeated section"
            sections[section] = {}
            continue
        assert section is not None and "=" in line
        key, value = line.split("=", 1)
        sections[section].setdefault(key, []).append(value)
    return sections


def _one(section, key):
    values = section[key]
    assert len(values) == 1, f"Duplicate {key} needs explicit review"
    return values[0]


def test_bootstrap_is_conditional_user_unit_without_install_target():
    unit = _read("eh-pilot-docker.service")
    assert set(unit) == {"Unit", "Service"}
    assert _one(unit["Unit"], "ConditionUser") == "10001"
    assert _one(unit["Unit"], "Requires") == "dbus.socket"
    assert _one(unit["Unit"], "After") == "dbus.socket"
    assert set(unit["Unit"]["ConditionPathExists"]) == {
        "/usr/bin/newuidmap", "/usr/bin/newgidmap"
    }
    # User=/Group= would not turn a system service into a supported rootless unit.
    assert "User" not in unit["Service"]
    assert "Group" not in unit["Service"]


def test_bootstrap_has_only_explicit_local_daemon_exec_no_application_commands():
    service = _read("eh-pilot-docker.service")["Service"]
    assert {key for key in service if key.startswith("Exec")} == {"ExecStart"}
    assert shlex.split(_one(service, "ExecStart")) == [
        "/usr/bin/env", "-i",
        "HOME=%h",
        "PATH=/usr/bin:/bin:/usr/sbin:/sbin",
        "XDG_RUNTIME_DIR=%t",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus",
        "NOTIFY_SOCKET=${NOTIFY_SOCKET}",
        "DOCKERD_ROOTLESS_ROOTLESSKIT_NET=none",
        "DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR=%t/eh-pilot-rootlesskit",
        "/usr/bin/dockerd-rootless.sh",
        "--host=unix://%t/eh-pilot-docker.sock",
        "--data-root=%h/docker-data",
        "--exec-root=%t/eh-pilot-exec",
        "--bridge=none",
        "--iptables=false",
        "--ip6tables=false",
    ]
    assert "Environment" not in service
    assert "EnvironmentFile" not in service
    assert "UnsetEnvironment" not in service
    assert _one(service, "Type") == "notify"
    assert _one(service, "NotifyAccess") == "all"
    # Do not suppress the approved setuid mapping helpers as a side effect.
    assert "NoNewPrivileges" not in service


@pytest.mark.parametrize(("key", "expected"), [
    ("Restart", "no"),
    ("RuntimeMaxSec", "600"),
    ("TimeoutStartSec", "30"),
    ("TimeoutStopSec", "1200"),
    ("KillMode", "mixed"),
    ("SendSIGKILL", "no"),
    ("Delegate", "cpu memory pids"),
    ("TasksMax", "256"),
    ("UMask", "0077"),
])
def test_bootstrap_service_bounded_lifetime_and_non_forced_stop_contract(key, expected):
    assert _one(_read("eh-pilot-docker.service")["Service"], key) == expected


def test_bootstrap_parent_slice_has_aggregate_limits_not_just_daemon_limits():
    unit = _read("50-eh-pilot-bootstrap-limits.conf")
    assert set(unit) == {"Slice"}
    assert unit["Slice"] == {
        "CPUAccounting": ["yes"],
        "MemoryAccounting": ["yes"],
        "TasksAccounting": ["yes"],
        "CPUQuota": ["100%"],
        "MemoryMax": ["1G"],
        "MemorySwapMax": ["0"],
        "TasksMax": ["256"],
    }
    source = (ARTIFACTS / "50-eh-pilot-bootstrap-limits.conf").read_text()
    assert "/etc/systemd/system/user-10001.slice.d/" in source
    assert "NOT user.slice" in source


def test_bootstrap_manager_delegation_is_documented_for_exact_account_only():
    unit = _read("50-eh-pilot-bootstrap-delegate.conf")
    assert unit == {"Service": {"Delegate": ["cpu memory pids"]}}
    source = (ARTIFACTS / "50-eh-pilot-bootstrap-delegate.conf").read_text()
    assert "/etc/systemd/system/user@10001.service.d/" in source
    assert "NOT the global user@ template" in source
