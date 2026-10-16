#!/usr/bin/env python3
"""Disposable real Docker + Streamable HTTP MCP collaboration acceptance test.

Uses only stdlib on the host. Builds the working tree, runs one read-only-root
container with one UID 568 bind mount and isolated random tokens, then removes
only that labeled container and its private scratch fixture. Evidence contains
checks/metadata only (never bearer tokens or shared document contents).
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SCRATCH = REPO.parent
IMAGE = "agent-message-bus:collaboration-v040-smoke"
CONTAINER_PORT = 8765
FILE_LIMIT = 64 * 1024 * 1024
EXPECTED_TOOLS = {
    "ack_message", "claim_message", "file_get", "file_history", "file_list", "file_put",
    "get_message", "knowledge_get", "knowledge_list", "knowledge_put", "knowledge_search",
    "receive_messages", "reject_message", "semantic_search", "semantic_status", "send_message",
    "wait_for_messages", "feedback_submit", "feedback_get", "feedback_list", "feedback_update",
}
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from", "how",
    "in", "is", "it", "its", "of", "on", "or", "so", "the", "then", "to", "what",
    "when", "with", "your",
}
FIXTURE_CHMOD_PROGRAM = (
    "import os,pathlib; r=pathlib.Path('/var/lib/agent-message-bus'); "
    "[os.chmod(p,0o777) for p in r.rglob('*') if p.is_dir() and (p.stat().st_mode & 0o077) != 0o077]"
)


def run(args: list[str], *, timeout: int = 60, cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def token_words(text: str) -> set[str]:
    return {word.casefold() for word in re.findall(r"[A-Za-z0-9]+", text) if word.casefold() not in STOP_WORDS}


class SmokeFailure(RuntimeError):
    def __init__(self, test: str):
        super().__init__(test)
        self.test = test


class MCPClient:
    def __init__(self, base_url: str, token: str, identity: str):
        self.base_url, self.token, self.identity = base_url, token, identity
        self.session_id: str | None = None
        self.next_id = 1

    def _http(self, payload: dict[str, Any], *, initialize: bool = False) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id and not initialize:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = "2025-03-26"
        request = urllib.request.Request(
            self.base_url + "/mcp", data=json.dumps(payload, separators=(",", ":")).encode(),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read()
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                status_code = response.status
        except urllib.error.HTTPError as exc:
            # Keep response details in memory only; never include headers/tokens in evidence.
            raw = exc.read()
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
            status_code = exc.code
        if not raw:
            return status_code, None, response_headers
        content_type = response_headers.get("content-type", "")
        if "text/event-stream" in content_type:
            events = [line[6:].strip() for line in raw.decode("utf-8", "replace").splitlines() if line.startswith("data:")]
            body = json.loads(events[-1]) if events else None
        else:
            body = json.loads(raw)
        return status_code, body, response_headers

    def initialize(self) -> list[str]:
        payload = {
            "jsonrpc": "2.0", "id": self.next_id, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": f"collaboration-smoke-{self.identity}-test", "version": "1"},
            },
        }
        self.next_id += 1
        status_code, body, headers = self._http(payload, initialize=True)
        if status_code != 200 or not body or "result" not in body:
            raise SmokeFailure(f"mcp_initialize_{self.identity}")
        self.session_id = headers.get("mcp-session-id")
        if not self.session_id:
            raise SmokeFailure(f"mcp_session_{self.identity}")
        status_code, body, _ = self._http({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if status_code not in (200, 202):
            raise SmokeFailure(f"mcp_initialized_notification_{self.identity}")
        result = self.rpc("tools/list", {})
        names = sorted(item["name"] for item in result["tools"])
        return names

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        status_code, body, _ = self._http({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if status_code != 200 or not body or body.get("id") != request_id:
            raise SmokeFailure(f"mcp_rpc_{method}")
        if "error" in body:
            raise SmokeFailure(f"mcp_rpc_{method}_error")
        return body["result"]

    def call_raw(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.rpc("tools/call", {"name": name, "arguments": arguments})

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self.call_raw(name, arguments)
        if result.get("isError"):
            raise SmokeFailure(f"mcp_tool_{name}")
        if "structuredContent" in result:
            return result["structuredContent"]
        for part in result.get("content", []):
            if part.get("type") == "text":
                try:
                    return json.loads(part["text"])
                except (json.JSONDecodeError, TypeError):
                    return part["text"]
        return result


def rest_json(base_url: str, token: str, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
            status_code = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status_code = exc.code
    return status_code, (json.loads(raw) if raw else None)


def wait_healthy(container: str, base_url: str) -> dict[str, Any]:
    deadline = time.monotonic() + 150
    last_status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        inspect = run(["docker", "inspect", "--format", "{{.State.Running}}", container])
        if inspect.returncode != 0 or inspect.stdout.strip() != "true":
            raise SmokeFailure("container_running")
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=4) as response:
                last_status = json.loads(response.read())
            if last_status is not None and last_status.get("ok") is True:
                return last_status
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(1)
    raise SmokeFailure("container_health")


def create_container(container: str, data_dir: Path, port: int, tokens: dict[str, str], label_value: str) -> None:
    host_allow = f"127.0.0.1:{port},127.0.0.1,localhost:{port},localhost"
    args = [
        "docker", "run", "--detach", "--name", container,
        "--label", f"org.hermes.live-collaboration-smoke={label_value}",
        "--user", "568:568", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        "--publish", f"127.0.0.1:{port}:{CONTAINER_PORT}",
        "--mount", f"type=bind,src={data_dir},dst=/var/lib/agent-message-bus",
        "--env", "AGENT_BUS_CONTAINER_HOST=0.0.0.0",
        "--env", f"AGENT_BUS_PORT={CONTAINER_PORT}",
        "--env", "AGENT_BUS_DB=/var/lib/agent-message-bus/messages.sqlite3",
        "--env", "AGENT_BUS_AUDIT=/var/lib/agent-message-bus/audit.jsonl",
        "--env", "AGENT_BUS_FILE_ROOT=/var/lib/agent-message-bus/files",
        "--env", "AGENT_BUS_ALLOWED_IPS=172.17.0.1",
        "--env", f"AGENT_BUS_ALLOWED_HOSTS={host_allow}",
        "--env", f"HERMES_TOKEN={tokens['hermes']}",
        "--env", f"CODEX_TOKEN={tokens['codex']}",
        "--env", f"CLAUDE_TOKEN={tokens['claude']}",
        "--env", f"ADMIN_TOKEN={tokens['admin']}",
        IMAGE,
    ]
    result = run(args, timeout=90)
    if result.returncode != 0:
        raise SmokeFailure("docker_run")


def container_metadata(container: str, label_value: str, data_dir: Path, port: int) -> dict[str, Any]:
    fmt = "{{json .Mounts}}|{{.Config.User}}|{{.HostConfig.ReadonlyRootfs}}|{{json .HostConfig.PortBindings}}|{{index .Config.Labels \"org.hermes.live-collaboration-smoke\"}}"
    result = run(["docker", "inspect", "--format", fmt, container])
    if result.returncode != 0:
        raise SmokeFailure("container_inspect")
    parts = result.stdout.strip().split("|", 4)
    mounts = json.loads(parts[0])
    bindings = json.loads(parts[3])
    mount_ok = (
        len(mounts) == 1 and mounts[0].get("Type") == "bind"
        and Path(mounts[0].get("Source", "")).resolve() == data_dir.resolve()
        and mounts[0].get("Destination") == "/var/lib/agent-message-bus"
        and mounts[0].get("RW") is True
    )
    port_config = bindings.get(f"{CONTAINER_PORT}/tcp", [])
    loopback_ok = any(item.get("HostIp") == "127.0.0.1" and int(item.get("HostPort", 0)) == port for item in port_config)
    valid = mount_ok and parts[1] == "568:568" and parts[2] == "true" and loopback_ok and parts[4] == label_value
    return {"passed": valid, "bind_mount_count": len(mounts), "uid_gid": parts[1],
            "read_only_rootfs": parts[2] == "true", "loopback_port": loopback_ok}


def upload_size_limited(base_url: str, token: str, path: str, size: int, boundary: str) -> tuple[int, Any]:
    prefix = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"path\"\r\n\r\n{path}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"payload.bin\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    length = len(prefix) + size + len(suffix)
    conn = http.client.HTTPConnection("127.0.0.1", int(base_url.rsplit(":", 1)[1]), timeout=240)
    try:
        conn.putrequest("POST", "/api/files")
        conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(length))
        conn.putheader("Connection", "close")
        conn.endheaders()
        conn.send(prefix)
        block = bytes((index % 251 for index in range(1024 * 1024)))
        remaining = size
        while remaining:
            chunk = block[:min(remaining, len(block))]
            conn.send(chunk)
            remaining -= len(chunk)
        conn.send(suffix)
        response = conn.getresponse()
        raw_response = response.read()
        response_status = response.status
        body = json.loads(raw_response) if raw_response else None
        return response_status, body
    finally:
        conn.close()


def sqlite_backup(source: Path, destination: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SmokeFailure("backup_sqlite_integrity")
    finally:
        dst.close()
        src.close()


def validate_object_snapshot(db: Path, file_root: Path) -> tuple[int, int]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        keys: set[str] = set()
        for table in ("files", "file_revisions"):
            try:
                keys.update(str(row[0]) for row in conn.execute(f"SELECT storage_key FROM {table} WHERE storage_key IS NOT NULL"))
            except sqlite3.OperationalError:
                pass
    object_dir = file_root / ".agent-message-bus" / "objects"
    checked = 0
    for key in keys:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise SmokeFailure("backup_object_key")
        blob = object_dir / key
        if not blob.is_file() or sha256(blob.read_bytes()) != key:
            raise SmokeFailure("backup_object_integrity")
        checked += 1
    return len(keys), checked


def chmod_fixture_tree(root: Path) -> None:
    """Keep only this private scratch fixture host-readable and restorable."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise SmokeFailure("scratch_fixture_has_no_symlinks")
        os.chmod(path, 0o777 if path.is_dir() else 0o666, follow_symlinks=False)


def main() -> int:
    run_id = uuid.uuid4().hex
    label_value = run_id
    container = f"agent-bus-collab-{run_id[:12]}"
    root = SCRATCH / f".live-collaboration-{run_id}"
    data_dir = root / "data"
    backup_dir = root / "backup"
    evidence_path = SCRATCH / f"live-collaboration-evidence-{run_id[:12]}.json"
    tests: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "schema": "agent-message-bus-live-collaboration-smoke/v1",
        "run_id": run_id,
        "repository": str(REPO),
        "script": str(Path(__file__).resolve()),
        "evidence_path": str(evidence_path),
        "git_head": None,
        "image": IMAGE,
        "container": container,
        "scratch_data_path": str(data_dir),
        "tests": tests,
        "tokens": {"generated": True, "persisted": False, "contents_in_evidence": False},
        "client_scope": "isolated random test tokens only; no production identities or endpoints",
        "windows_clients_tested": False,
        "production_deployed": False,
        "cleanup": {"container_removed": False, "scratch_fixture_removed": False},
    }
    tokens = {name: secrets.token_urlsafe(32) for name in ("hermes", "codex", "claude", "admin")}
    base_url: str | None = None
    created = False
    failure: str | None = None

    def check(name: str, condition: bool, details: dict[str, Any] | None = None) -> None:
        tests.append({"name": name, "passed": bool(condition), **(details or {})})
        if not condition:
            raise SmokeFailure(name)

    def existing_status() -> list[str]:
        result = run(["git", "status", "--porcelain=v1"])
        if result.returncode != 0:
            raise SmokeFailure("git_status")
        return result.stdout.splitlines()

    initial_status: list[str] = []
    try:
        head = run(["git", "rev-parse", "HEAD"])
        check("repo_head_available", head.returncode == 0 and bool(head.stdout.strip()))
        evidence["git_head"] = head.stdout.strip()
        initial_status = existing_status()
        evidence["initial_worktree_status_count"] = len(initial_status)

        head_compose = run(["git", "show", "HEAD:compose.truenas.yaml"])
        working_compose = (REPO / "compose.truenas.yaml").read_text()
        def volume_block(text: str) -> str:
            lines = text.splitlines()
            start = next((idx for idx, line in enumerate(lines) if line == "    volumes:"), None)
            end = next((idx for idx, line in enumerate(lines[start + 1:], start + 1)
                        if line == "    healthcheck:"), None) if start is not None else None
            if start is None or end is None:
                raise SmokeFailure("compose_volume_block_present")
            return "\n".join(lines[start:end]) + "\n"
        expected_volumes = volume_block(head_compose.stdout)
        current_volumes = volume_block(working_compose)
        equal_volumes = head_compose.returncode == 0 and current_volumes == expected_volumes
        check("compose_truenas_volume_text_matches_git_head", equal_volumes,
              {"volume_text_sha256": sha256(current_volumes.encode()), "git_head_volume_sha256": sha256(expected_volumes.encode())})

        existing_image = run(["docker", "image", "inspect", "--format", "{{.Id}}|{{json .Config.Labels}}", IMAGE])
        previous_id = None
        safe_existing_candidate = existing_image.returncode != 0
        if existing_image.returncode == 0:
            previous_id, labels_text = existing_image.stdout.strip().split("|", 1)
            labels = json.loads(labels_text)
            safe_existing_candidate = (
                labels.get("org.opencontainers.image.revision") == evidence["git_head"]
                and labels.get("org.opencontainers.image.version") == "collaboration-v040-smoke"
            )
        check("candidate_image_tag_safe_to_build", safe_existing_candidate,
              {"tag_was_preexisting": previous_id is not None})
        evidence["candidate_image_before"] = previous_id
        build = run(["docker", "build", "--tag", IMAGE, "--build-arg", f"VCS_REF={evidence['git_head']}",
                     "--build-arg", "VERSION=collaboration-v040-smoke", "."], timeout=900)
        check("docker_build_candidate", build.returncode == 0)
        image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE])
        check("candidate_image_tag_present", image_id.returncode == 0 and image_id.stdout.startswith("sha256:"))
        evidence["image_id"] = image_id.stdout.strip()

        root.mkdir(mode=0o700)
        data_dir.mkdir(mode=0o777)
        backup_dir.mkdir(mode=0o700)
        os.chmod(data_dir, 0o777)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        evidence["loopback_port"] = port
        evidence["bind_host"] = "127.0.0.1"
        base_url = f"http://127.0.0.1:{port}"
        create_container(container, data_dir, port, tokens, label_value)
        created = True
        metadata = container_metadata(container, label_value, data_dir, port)
        check("container_uid568_readonly_single_bind_loopback", metadata["passed"], metadata)
        health = wait_healthy(container, base_url)
        check("health_database_ok", health.get("ok") is True and health.get("database") == "ok")
        semantic_health = health.get("semantic", {})
        check("health_semantic_enabled", semantic_health.get("state") == "enabled",
              {"state": semantic_health.get("state")})

        clients: dict[str, MCPClient] = {}
        tool_counts: dict[str, int] = {}
        for identity in ("hermes", "codex", "claude"):
            client = MCPClient(base_url, tokens[identity], identity)
            names = client.initialize()
            check(f"mcp_initialize_tools_{identity}", set(names) == EXPECTED_TOOLS and len(names) == len(EXPECTED_TOOLS),
                  {"tool_count": len(names), "tool_names_sha256": sha256("\n".join(names).encode())})
            clients[identity] = client
            tool_counts[identity] = len(names)
        evidence["mcp_tool_counts"] = tool_counts

        marker = f"SMOKE_PRIVATE_CONTENT_{run_id}"
        project = f"live-{run_id[:10]}"
        other_project = f"other-{run_id[:10]}"
        knowledge_path = "archive/atmosphere-regeneration.md"
        knowledge_title = "Maintenance cycle field note"
        knowledge_v1 = (
            f"{marker} An amine sorbent captures carbon dioxide exhaled by occupants from a recirculating "
            "ventilation stream; brief heating liberates the trapped compound to a disposal line and "
            "restores the bed for another service cycle."
        )
        knowledge_v2 = (
            f"{marker} An amine sorbent captures carbon dioxide exhaled by occupants from a recirculating "
            "ventilation stream. Thermal treatment releases the trapped compound to a disposal line and "
            "restores the bed for another service cycle."
        )
        kargs = {"project": project, "path": knowledge_path, "title": knowledge_title,
                 "content": knowledge_v1, "expected_version": 0}
        knowledge_created = clients["hermes"].call("knowledge_put", kargs)
        check("hermes_knowledge_put", knowledge_created.get("version") == 1 and knowledge_created.get("created_by") == "hermes")
        knowledge_read = clients["codex"].call("knowledge_get", {"project": project, "path": knowledge_path})
        check("codex_knowledge_get_digest_and_provenance",
              knowledge_read.get("content") == knowledge_v1 and knowledge_read.get("sha256") == sha256(knowledge_v1.encode())
              and knowledge_read.get("created_by") == "hermes")
        knowledge_updated = clients["codex"].call(
            "knowledge_put", {**kargs, "content": knowledge_v2, "expected_version": 1})
        check("codex_knowledge_cas_update", knowledge_updated.get("version") == 2 and knowledge_updated.get("updated_by") == "codex")
        stale_knowledge = clients["claude"].call_raw(
            "knowledge_put", {**kargs, "content": f"{marker} stale knowledge write", "expected_version": 1})
        stale_knowledge_text = json.dumps(stale_knowledge, sort_keys=True)
        check("knowledge_stale_version_conflict", stale_knowledge.get("isError") is True and "conflict" in stale_knowledge_text.casefold())
        knowledge_after_conflict = clients["hermes"].call("knowledge_get", {"project": project, "path": knowledge_path})
        check("knowledge_conflict_preserved_newer_version",
              knowledge_after_conflict.get("version") == 2 and knowledge_after_conflict.get("content") == knowledge_v2)

        other_doc = clients["hermes"].call("knowledge_put", {
            "project": other_project, "path": "reference/service-cycle.md", "title": "Separate project reference",
            "content": f"{marker} {knowledge_v2}", "expected_version": 0,
        })
        check("knowledge_other_project_fixture", other_doc.get("version") == 1)
        distractor = clients["hermes"].call("knowledge_put", {
            "project": project, "path": "archive/textile-maintenance.md", "title": "Cleaning materials for clothing",
            "content": f"{marker} A hydroponic plant cycles nutrient solution after contaminants are removed from irrigation water; "
                       "the grower restores its filter for the next crop.", "expected_version": 0,
        })
        check("knowledge_semantic_distractor_fixture", distractor.get("version") == 1)

        file_path = "memos/atmosphere-cycle.txt"
        file_v1 = f"{marker} An amine sorbent captures carbon dioxide exhaled by occupants from the cabin's recirculating stream."
        file_v2 = (f"{marker} An amine sorbent captures carbon dioxide exhaled by occupants from a recirculating ventilation stream; "
                   "brief heating liberates the trapped compound to a disposal line and restores the bed for another service cycle.")
        file_created = clients["hermes"].call("file_put", {
            "project": project, "path": file_path, "content": file_v1, "expected_version": 0,
            "encoding": "utf-8", "content_type": "text/plain",
        })
        check("hermes_file_put", file_created.get("version") == 1 and file_created.get("created_by") == "hermes")
        other_project_file = clients["hermes"].call("file_put", {
            "project": other_project, "path": file_path, "content": file_v2, "expected_version": 0,
            "encoding": "utf-8", "content_type": "text/plain",
        })
        check("other_project_semantic_file_fixture", other_project_file.get("version") == 1)
        project_relative_path = "plan.md"
        project_relative_content = (
            f"{marker} A pressure controller closes the airlock during a cabin leak and opens the reserve oxygen supply."
        )
        project_relative_file = clients["hermes"].call("file_put", {
            "project": project, "path": project_relative_path, "content": project_relative_content,
            "expected_version": 0, "encoding": "utf-8", "content_type": "text/plain",
        })
        other_project_relative_file = clients["hermes"].call("file_put", {
            "project": other_project, "path": project_relative_path, "content": project_relative_content,
            "expected_version": 0, "encoding": "utf-8", "content_type": "text/plain",
        })
        check("project_relative_file_fixtures", project_relative_file.get("version") == 1
              and other_project_relative_file.get("version") == 1)
        file_read_v1 = clients["codex"].call("file_get", {"project": project, "path": file_path})
        check("codex_file_get_sha256_v1",
              file_read_v1.get("content") == file_v1 and file_read_v1.get("sha256") == sha256(file_v1.encode())
              and file_read_v1.get("version") == 1)
        file_updated = clients["claude"].call("file_put", {
            "project": project, "path": file_path, "content": file_v2, "expected_version": 1,
            "encoding": "utf-8", "content_type": "text/plain",
        })
        check("claude_file_cas_update", file_updated.get("version") == 2 and file_updated.get("updated_by") == "claude")
        history = clients["claude"].call("file_history", {"project": project, "path": file_path})
        versions = history.get("versions", [])
        check("claude_file_history_authors_and_versions",
              [item.get("version") for item in versions[:2]] == [2, 1]
              and [item.get("updated_by") for item in versions[:2]] == ["claude", "hermes"])
        stale_file = clients["hermes"].call_raw("file_put", {
            "project": project, "path": file_path, "content": f"{marker} stale file write", "expected_version": 1,
            "encoding": "utf-8", "content_type": "text/plain",
        })
        check("file_stale_version_conflict", stale_file.get("isError") is True and "conflict" in json.dumps(stale_file).casefold())
        file_after_conflict = clients["codex"].call("file_get", {"project": project, "path": file_path})
        check("file_conflict_preserved_newer_version",
              file_after_conflict.get("version") == 2 and file_after_conflict.get("content") == file_v2)

        aba_path = "aba.md"
        aba_first_content = f"{marker} Original file before delete and recreation."
        aba_recreated_content = f"{marker} Replacement file after delete and recreation."
        aba_first = clients["hermes"].call("file_put", {
            "project": project, "path": aba_path, "content": aba_first_content, "expected_version": 0,
            "encoding": "utf-8", "content_type": "text/plain",
        })
        check("file_aba_initial_version", aba_first.get("version") == 1)
        delete_status, _ = rest_json(
            base_url, tokens["admin"], "DELETE",
            f"/api/files/{project}/{aba_path}?expected_version=1")
        check("file_aba_delete", delete_status == 204, {"http_status": delete_status})
        aba_recreated = clients["codex"].call("file_put", {
            "project": project, "path": aba_path, "content": aba_recreated_content, "expected_version": 0,
            "encoding": "utf-8", "content_type": "text/plain",
        })
        check("file_aba_recreate_monotonic_version", aba_recreated.get("version") == 2)
        stale_aba = clients["claude"].call_raw("file_put", {
            "project": project, "path": aba_path, "content": f"{marker} stale ABA write",
            "expected_version": 1, "encoding": "utf-8", "content_type": "text/plain",
        })
        check("file_aba_stale_cas_conflict", stale_aba.get("isError") is True
              and "conflict" in json.dumps(stale_aba).casefold())
        aba_after_conflict = clients["hermes"].call("file_get", {"project": project, "path": aba_path})
        check("file_aba_conflict_preserved_recreated_version",
              aba_after_conflict.get("version") == 2 and aba_after_conflict.get("content") == aba_recreated_content)

        binary_path = "assets/nonutf8-fixture.bin"
        binary_bytes = bytes((0, 255, 1, 128, 10, 13, 0, 42, 200, 254, 17)) + bytes(range(256))
        binary_put = clients["hermes"].call("file_put", {
            "project": project, "path": binary_path, "content": base64.b64encode(binary_bytes).decode("ascii"),
            "expected_version": 0, "encoding": "base64", "content_type": "application/octet-stream",
        })
        binary_get = clients["codex"].call("file_get", {
            "project": project, "path": binary_path, "encoding": "base64",
        })
        check("binary_roundtrip_sha256",
              base64.b64decode(binary_get.get("content", ""), validate=True) == binary_bytes
              and binary_get.get("sha256") == sha256(binary_bytes) and binary_put.get("sha256") == sha256(binary_bytes))
        metadata_query = "nonutf8 fixture binary asset"
        metadata_first_results = clients["hermes"].call("semantic_search", {
            "query": metadata_query, "project": project, "source": "file", "limit": 10,
        }).get("results", [])
        metadata_first = next((item for item in metadata_first_results if item.get("path") == binary_path), None)
        metadata_second_results = clients["codex"].call("semantic_search", {
            "query": metadata_query, "project": project, "source": "file", "limit": 10,
        }).get("results", [])
        metadata_second = next((item for item in metadata_second_results if item.get("path") == binary_path), None)
        metadata_flags = (False, False, True)
        first_flags = tuple(metadata_first.get(key) for key in ("truncated", "content_indexed", "metadata_only")) \
            if metadata_first is not None else None
        second_flags = tuple(metadata_second.get(key) for key in ("truncated", "content_indexed", "metadata_only")) \
            if metadata_second is not None else None
        check("semantic_binary_metadata_flags", first_flags == metadata_flags)
        check("semantic_repeated_metadata_flags_preserved",
              second_flags == first_flags == metadata_flags)

        for suffix, text in (
            ("b-clothing.txt", f"{marker} A greenhouse plant receives water through a mineral filter; a technician replaces the irrigation cartridge after each harvest."),
            ("c-water.txt", f"{marker} A spacecraft water-treatment plant filters graywater and refreshes its mineral cartridge."),
        ):
            result = clients["hermes"].call("file_put", {
                "project": project, "path": f"memos/{suffix}", "content": text,
                "expected_version": 0, "encoding": "utf-8", "content_type": "text/plain",
            })
            check(f"file_fixture_{suffix}", result.get("version") == 1)

        # Keyset pagination over a deliberately narrow prefix avoids coupling to the 64 MiB fixture.
        cursor: str | None = None
        pages: list[str] = []
        page_count = 0
        while True:
            args: dict[str, Any] = {"project": project, "path_prefix": "memos", "limit": 1}
            if cursor is not None:
                args["after"] = cursor
            page = clients["codex"].call("file_list", args)
            page_paths = [item["path"] for item in page.get("files", [])]
            pages.extend(page_paths)
            page_count += 1
            cursor = page.get("next_after")
            if cursor is None:
                break
            if page_count > 8:
                raise SmokeFailure("file_pagination_terminates")
        check("file_keyset_pagination", pages == sorted(set(pages)) and len(pages) == 3 and page_count == 3,
              {"page_count": page_count, "file_count": len(pages)})

        semantic_status = clients["hermes"].call("semantic_status", {})
        check("mcp_semantic_status_available",
              semantic_status.get("state") == "available" and semantic_status.get("ready") is True
              and isinstance(semantic_status.get("model"), str) and bool(semantic_status.get("model")),
              {"state": semantic_status.get("state"), "ready": semantic_status.get("ready"),
               "model": semantic_status.get("model"), "model_version": semantic_status.get("model_version")})
        knowledge_query = "How does an air-recycling plant keep a spacecraft safe from astronaut exhalations and refresh its purifier afterward?"
        knowledge_overlap = sorted(token_words(knowledge_query) & token_words(knowledge_title + " " + knowledge_path + " " + knowledge_v2))
        check("knowledge_paraphrase_has_no_target_keyword_overlap", not knowledge_overlap,
              {"overlap_terms": knowledge_overlap})
        knowledge_results = clients["hermes"].call("semantic_search", {
            "query": knowledge_query, "project": project, "source": "knowledge", "limit": 10,
        })
        knowledge_ranked = knowledge_results.get("results", [])
        check("semantic_knowledge_paraphrase_rank", bool(knowledge_ranked) and knowledge_ranked[0].get("path") == knowledge_path,
              {"top_path": knowledge_ranked[0].get("path") if knowledge_ranked else None,
               "top_score": knowledge_ranked[0].get("score") if knowledge_ranked else None})
        knowledge_filter_ok = all(item.get("source") == "knowledge" and item.get("project") == project for item in knowledge_ranked)
        check("semantic_knowledge_source_and_project_filters", knowledge_filter_ok and all(item.get("path") != "reference/service-cycle.md" for item in knowledge_ranked))

        file_query = "How does an air-recycling plant prevent a spacefarer's exhalations from contaminating the living compartment and refresh its purifier afterward?"
        file_overlap = sorted(token_words(file_query) & token_words(file_path + " " + file_v2))
        check("file_paraphrase_has_no_target_keyword_overlap", not file_overlap, {"overlap_terms": file_overlap})
        file_results = clients["claude"].call("semantic_search", {
            "query": file_query, "project": project, "source": "file", "limit": 10,
        })
        file_ranked = file_results.get("results", [])
        check("semantic_file_paraphrase_rank", bool(file_ranked) and file_ranked[0].get("path") == file_path
              and file_ranked[0].get("project") == project,
              {"top_path": file_ranked[0].get("path") if file_ranked else None,
               "top_project": file_ranked[0].get("project") if file_ranked else None,
               "top_score": file_ranked[0].get("score") if file_ranked else None})
        check("semantic_file_source_and_project_filters",
              bool(file_ranked) and all(item.get("source") == "file" and item.get("project") == project
                                        for item in file_ranked)
              and all(item.get("project") != other_project for item in file_ranked))
        relative_file_query = "pressure controller closes airlock opens reserve oxygen supply"
        relative_file_results = clients["claude"].call("semantic_search", {
            "query": relative_file_query, "project": project, "source": "file", "limit": 10,
        }).get("results", [])
        check("semantic_project_relative_file_search",
              any(item.get("path") == project_relative_path and item.get("project") == project
                  for item in relative_file_results)
              and all(item.get("source") == "file" and item.get("project") == project
                      for item in relative_file_results))

        exact_path = f"uploads/exact-{FILE_LIMIT}-bytes.bin"
        exact_status, exact_result = upload_size_limited(
            base_url, tokens["hermes"], f"{project}/{exact_path}", FILE_LIMIT, f"smoke{run_id[:16]}")
        check("rest_upload_exactly_64mib_accepted",
              exact_status == 201 and isinstance(exact_result, dict) and exact_result.get("size") == FILE_LIMIT,
              {"http_status": exact_status, "bytes": FILE_LIMIT,
               "returned_size": exact_result.get("size") if isinstance(exact_result, dict) else None})
        over_status, _over_result = upload_size_limited(
            base_url, tokens["hermes"], f"{project}/over-limit.bin", FILE_LIMIT + 1, f"smoke{run_id[16:32]}")
        check("rest_upload_64mib_plus_one_rejected", over_status == 413,
              {"http_status": over_status, "bytes": FILE_LIMIT + 1})

        restarted = run(["docker", "restart", container], timeout=90)
        check("container_restart_command", restarted.returncode == 0)
        restart_health = wait_healthy(container, base_url)
        check("container_restart_health", restart_health.get("ok") is True)
        after_restart = MCPClient(base_url, tokens["claude"], "claude")
        restart_tools = after_restart.initialize()
        check("mcp_reinitialize_after_restart", set(restart_tools) == EXPECTED_TOOLS and len(restart_tools) == len(EXPECTED_TOOLS))
        persisted_knowledge = after_restart.call("knowledge_get", {"project": project, "path": knowledge_path})
        persisted_file = after_restart.call("file_get", {"project": project, "path": file_path})
        persisted_binary = after_restart.call("file_get", {"project": project, "path": binary_path, "encoding": "base64"})
        check("restart_persistence_knowledge", persisted_knowledge.get("version") == 2 and persisted_knowledge.get("sha256") == sha256(knowledge_v2.encode()))
        check("restart_persistence_file_objects", persisted_file.get("version") == 2 and persisted_file.get("sha256") == sha256(file_v2.encode())
              and base64.b64decode(persisted_binary.get("content", ""), validate=True) == binary_bytes)
        post_restart_search = after_restart.call("semantic_search", {
            "query": knowledge_query, "project": project, "source": "knowledge", "limit": 10,
        })
        post_ranked = post_restart_search.get("results", [])
        check("restart_persistence_semantic_index", bool(post_ranked) and post_ranked[0].get("path") == knowledge_path)
        post_restart_file_search = after_restart.call("semantic_search", {
            "query": relative_file_query, "project": project, "source": "file", "limit": 10,
        })
        post_restart_file_ranked = post_restart_file_search.get("results", [])
        check("restart_persistence_file_semantic_index",
              any(item.get("path") == project_relative_path and item.get("project") == project
                  for item in post_restart_file_ranked))

        chmod_program = (
            "import os,pathlib; r=pathlib.Path('/var/lib/agent-message-bus'); "
            "[os.chmod(p,0o777 if p.is_dir() else 0o666) for p in r.rglob('*')]"
        )
        fixture_access = run(["docker", "exec", "--user", "568:568", container, "python", "-c", chmod_program])
        check("scratch_fixture_backup_access_enabled", fixture_access.returncode == 0)

        stopped = run(["docker", "stop", container], timeout=60)
        check("container_stopped_before_backup", stopped.returncode == 0)
        active_db = data_dir / "messages.sqlite3"
        file_root = data_dir / "files"
        sqlite_backup(active_db, backup_dir / "messages.sqlite3")
        shutil.copytree(file_root, backup_dir / "files")
        if (data_dir / "search").is_dir():
            shutil.copytree(data_dir / "search", backup_dir / "search")
        if (data_dir / "audit.jsonl").exists():
            shutil.copy2(data_dir / "audit.jsonl", backup_dir / "audit.jsonl")
        object_keys, checked_objects = validate_object_snapshot(backup_dir / "messages.sqlite3", backup_dir / "files")
        check("sqlite_backup_api_integrity", True, {"integrity_check": "ok"})
        check("file_object_backup_digest_validation", object_keys == checked_objects,
              {"object_references": object_keys, "verified_objects": checked_objects})
        data_rows = sqlite3.connect(f"file:{backup_dir / 'messages.sqlite3'}?mode=ro", uri=True)
        try:
            knowledge_count = data_rows.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
            file_count = data_rows.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            revision_count = data_rows.execute("SELECT COUNT(*) FROM file_revisions").fetchone()[0]
        finally:
            data_rows.close()
        check("backup_contains_collaboration_rows", knowledge_count >= 3 and file_count >= 5 and revision_count >= 1,
              {"knowledge_rows": knowledge_count, "file_rows": file_count, "revision_rows": revision_count})

        # Replace the stopped service's dataset with the verified snapshot and recreate
        # only this test container, proving the backup can actually restore the service.
        safe_inspect = run(["docker", "inspect", "--format", "{{index .Config.Labels \"org.hermes.live-collaboration-smoke\"}}", container])
        check("restore_container_ownership_guard", safe_inspect.returncode == 0 and safe_inspect.stdout.strip() == label_value)
        rm = run(["docker", "rm", container], timeout=60)
        check("remove_only_owned_stopped_container_for_restore", rm.returncode == 0)
        created = False
        preserved = root / "pre_restore_data"
        os.replace(data_dir, preserved)
        data_dir.mkdir(mode=0o700)
        shutil.copy2(backup_dir / "messages.sqlite3", data_dir / "messages.sqlite3")
        shutil.copytree(backup_dir / "files", data_dir / "files")
        if (backup_dir / "search").is_dir():
            shutil.copytree(backup_dir / "search", data_dir / "search")
        if (backup_dir / "audit.jsonl").exists():
            shutil.copy2(backup_dir / "audit.jsonl", data_dir / "audit.jsonl")
        chmod_fixture_tree(data_dir)
        create_container(container, data_dir, port, tokens, label_value)
        created = True
        restored_health = wait_healthy(container, base_url)
        check("restored_container_health", restored_health.get("ok") is True)
        restored = MCPClient(base_url, tokens["hermes"], "hermes")
        restored_names = restored.initialize()
        check("restored_mcp_initialize_tools", set(restored_names) == EXPECTED_TOOLS and len(restored_names) == len(EXPECTED_TOOLS))
        restored_knowledge = restored.call("knowledge_get", {"project": project, "path": knowledge_path})
        restored_file = restored.call("file_get", {"project": project, "path": file_path})
        restored_binary = restored.call("file_get", {"project": project, "path": binary_path, "encoding": "base64"})
        check("sqlite_restore_knowledge_readback", restored_knowledge.get("version") == 2 and restored_knowledge.get("sha256") == sha256(knowledge_v2.encode()))
        check("file_object_restore_readback",
              restored_file.get("version") == 2 and restored_file.get("sha256") == sha256(file_v2.encode())
              and base64.b64decode(restored_binary.get("content", ""), validate=True) == binary_bytes)
        restored_search = restored.call("semantic_search", {
            "query": knowledge_query, "project": project, "source": "knowledge", "limit": 10,
        })
        restored_ranked = restored_search.get("results", [])
        check("semantic_index_restore_readback", bool(restored_ranked) and restored_ranked[0].get("path") == knowledge_path)
        restored_file_search = restored.call("semantic_search", {
            "query": relative_file_query, "project": project, "source": "file", "limit": 10,
        })
        restored_file_ranked = restored_file_search.get("results", [])
        check("file_semantic_index_restore_readback",
              any(item.get("path") == project_relative_path and item.get("project") == project
                  for item in restored_file_ranked))
        evidence["backup"] = {
            "sqlite_backup_api": True, "sqlite_integrity": "ok", "file_objects_referenced": object_keys,
            "file_objects_verified_sha256": checked_objects, "knowledge_rows": knowledge_count,
            "file_rows": file_count, "revision_rows": revision_count,
            "restored_from_snapshot_and_read_back": True,
        }

        # Drain only this container's output; it must never contain test tokens or document payloads.
        logs = run(["docker", "logs", container], timeout=30)
        log_text = logs.stdout + logs.stderr
        forbidden = [*tokens.values(), marker, knowledge_v1, knowledge_v2, file_v1, file_v2]
        leaks = [label for label, value in zip(
            ("hermes_token", "codex_token", "claude_token", "admin_token", "content_marker",
             "knowledge_v1", "knowledge_v2", "file_v1", "file_v2"), forbidden) if value in log_text]
        check("container_logs_exclude_secrets_and_content", logs.returncode == 0 and not leaks,
              {"forbidden_values_found": leaks, "log_bytes": len(log_text.encode())})
        evidence["log_check"] = {"passed": True, "bytes_examined": len(log_text.encode()), "secret_or_content_matches": 0}
        evidence["semantic"] = {
            "model": semantic_status.get("model"), "knowledge_top_path": knowledge_ranked[0].get("path"),
            "knowledge_top_score": knowledge_ranked[0].get("score"), "file_top_path": file_ranked[0].get("path"),
            "file_top_score": file_ranked[0].get("score"), "knowledge_target_keyword_overlap": knowledge_overlap,
            "file_target_keyword_overlap": file_overlap,
        }
        evidence["mcp_tool_counts"] = tool_counts
        evidence["test_result"] = "passed"

    except BaseException as exc:
        failure = exc.test if isinstance(exc, SmokeFailure) else type(exc).__name__
        if not any(item.get("name") == failure and item.get("passed") is False for item in tests):
            tests.append({"name": failure, "passed": False})
        evidence["test_result"] = "failed"
        evidence["failure"] = {"test_or_exception": failure}
    finally:
        fixture_access_ready = False
        if created:
            owned = run(["docker", "inspect", "--format", "{{json .Config.Labels}}", container])
            owned_labels = json.loads(owned.stdout) if owned.returncode == 0 else {}
            if owned.returncode == 0 and owned_labels.get("org.hermes.live-collaboration-smoke") == label_value:
                if not fixture_access_ready:
                    running = run(["docker", "inspect", "--format", "{{.State.Running}}", container])
                    if running.returncode == 0 and running.stdout.strip() != "true":
                        run(["docker", "start", container], timeout=60)
                    access = run(["docker", "exec", "--user", "568:568", container, "python", "-c", FIXTURE_CHMOD_PROGRAM])
                    if access.returncode == 0:
                        fixture_access_ready = True
                evidence["cleanup"]["scratch_permissions_prepared"] = fixture_access_ready
                removed = run(["docker", "rm", "-f", container], timeout=60)
                evidence["cleanup"]["container_removed"] = removed.returncode == 0
            else:
                evidence["cleanup"]["container_removed"] = False
                evidence["cleanup"]["container_cleanup_refused_not_owned"] = True
        if root.exists():
            try:
                shutil.rmtree(root)
                evidence["cleanup"]["scratch_fixture_removed"] = True
            except OSError:
                evidence["cleanup"]["scratch_fixture_removed"] = False
        if initial_status:
            final_status = existing_status()
            expected_status = sorted(initial_status)
            actual_status = sorted(final_status)
            evidence["worktree"] = {
                "initial_status_count": len(initial_status), "final_status_count": len(final_status),
                "unchanged_during_run": actual_status == expected_status,
                "preexisting_status_preserved": all(item in actual_status for item in initial_status),
            }
            tests.append({"name": "repo_status_unchanged_during_run",
                          "passed": actual_status == expected_status})
            if actual_status != expected_status and failure is None:
                failure = "repo_status_unchanged_during_run"
                evidence["test_result"] = "failed"
                evidence["failure"] = {"test_or_exception": failure}
        evidence["passed_test_count"] = sum(1 for item in tests if item["passed"])
        evidence["failed_test_count"] = sum(1 for item in tests if not item["passed"])
        evidence["tests_total"] = len(tests)
        try:
            evidence_path.write_text(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n")
            os.chmod(evidence_path, 0o600)
        except OSError:
            evidence["evidence_write_error"] = True

    # The only stdout is a compact, nonsecret completion record.
    print(json.dumps({"result": evidence.get("test_result"), "evidence": str(evidence_path),
                      "tests_passed": evidence.get("passed_test_count"),
                      "tests_failed": evidence.get("failed_test_count"),
                      "container_removed": evidence["cleanup"].get("container_removed"),
                      "scratch_fixture_removed": evidence["cleanup"].get("scratch_fixture_removed")},
                     sort_keys=True))
    return 0 if evidence.get("test_result") == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
