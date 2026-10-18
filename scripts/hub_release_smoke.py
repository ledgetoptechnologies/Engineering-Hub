"""Isolated CI-only named-volume startup/recreation check; synthetic tokens only."""
import argparse
import json
import re
import subprocess
import uuid
from hub_dashboard_container_smoke import PROBE


def run(*args, timeout=60):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


def owned_exists(kind, name, owner):
    label = '.Config.Labels' if kind == 'container' else '.Labels'
    result = subprocess.run(['docker', kind, 'inspect', '--format',
                             '{{index ' + label + ' "hub.release-owner"}}', name],
                            capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        if result.stdout.strip() != owner:
            raise RuntimeError('Release fixture ownership mismatch; not removed')
        return True
    if ('no such ' + kind) in result.stderr.lower():
        return False
    raise RuntimeError('Cannot verify release fixture cleanup: Docker inspect failed')


def remove_owned(kind, name, owner):
    if owned_exists(kind, name, owner):
        args = ['docker', kind, 'rm'] + (['-f'] if kind == 'container' else []) + [name]
        run(*args)
        if owned_exists(kind, name, owner):
            raise RuntimeError('Release fixture still exists after removal')


def smoke(image, commit):
    if not re.fullmatch(r"ghcr\.io/ledgetoptechnologies/engineering-hub-control:(?:build-)[a-z0-9-]+|ghcr\.io/ledgetoptechnologies/engineering-hub-control@sha256:[a-f0-9]{64}", image):
        raise ValueError("Expected the explicit Hub release artifact")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Expected exact source commit")
    owner = uuid.uuid4().hex
    volume = "hub-release-state-" + owner
    names = []
    anchor = "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))"
    assert PROBE.count(anchor) == 1
    probe = PROBE.replace(anchor, anchor + "\nopener.addheaders = [('Host', 'hub.ledgetoptechnologies.com')]")
    try:
        run("docker", "volume", "create", "--label", "hub.release-owner=" + owner, volume)
        for iteration in range(2):
            name = "hub-release-" + owner + "-" + str(iteration)
            names.append(name)
            run("docker", "run", "-d", "--pull", "never", "--name", name,
                "--label", "hub.release-owner=" + owner, "--network", "none",
                "--user", "568:568", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--memory", "512m",
                "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "128",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
                "--mount", "type=volume,source=" + volume + ",target=/var/lib/hub",
                "-e", "HUB_BASE_URL=https://hub.ledgetoptechnologies.com",
                "-e", "HERMES_TOKEN=synthetic-release-hermes-only",
                "-e", "CODEX_TOKEN=synthetic-release-codex-only",
                "-e", "CLAUDE_TOKEN=synthetic-release-claude-only", image)
            report = json.loads(run("docker", "exec", name, "python", "-c", probe, commit))
            assert report["image_smoke"] == "passed" and report["build_commit"] == commit
            marker = "from pathlib import Path; p=Path('/var/lib/hub/.release-smoke'); "
            marker += "p.write_text('isolated-release-fixture')" if iteration == 0 else "assert p.read_text() == 'isolated-release-fixture'"
            run("docker", "exec", name, "python", "-c", marker)
            remove_owned('container', name, owner)
            names.remove(name)
        return {"source_commit": commit, "image_smoke": "passed", "canonical_host": "hub.ledgetoptechnologies.com",
                "authenticated_agents": 3, "mcp_tools": 9, "human_sign_in": "disabled",
                "named_volume_recreation": "passed", "network": "none", "truenas": "not_deployed"}
    finally:
        for name in names:
            remove_owned('container', name, owner)
        remove_owned('volume', volume, owner)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    print(json.dumps(smoke(args.image, args.commit)))
