#!/usr/bin/env python3
"""Real Docker/MCP acceptance for v0.4.0 cursor messaging and feedback.

This uses isolated random bearer tokens and a private scratch bind mount only.
It verifies the notification feed and shared review inbox; it does not test or
claim host-client auto-wake/session routing.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SCRATCH = REPO.parent
IMAGE = "agent-message-bus:collaboration-v040-smoke"
IMAGE_VERSION = "collaboration-v040-smoke"
CONTAINER_PORT = 8765
EXPECTED_TOOLS = {
    "ack_message", "claim_message", "file_get", "file_history", "file_list", "file_put",
    "get_message", "get_send_receipt", "presence_status", "knowledge_get", "knowledge_list", "knowledge_put", "knowledge_search",
    "receive_messages", "reject_message", "semantic_search", "semantic_status", "send_message",
    "wait_for_messages", "feedback_submit", "feedback_get", "feedback_list", "feedback_update", "bus_info",
}
POINTER_KEYS = {
    "id", "seq", "sender", "recipient", "message_type", "priority", "correlation_id",
    "in_reply_to", "hop_count", "requires_user_approval", "created_at", "subject",
}
FIXTURE_CHMOD_PROGRAM = (
    "import os,pathlib; r=pathlib.Path('/var/lib/agent-message-bus'); "
    "[os.chmod(p,0o777 if p.is_dir() else 0o666) for p in r.rglob('*') if p.stat().st_uid==os.geteuid()]"
)


def run(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=REPO, text=True, capture_output=True, timeout=timeout, check=False)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SmokeFailure(RuntimeError):
    def __init__(self, test: str):
        super().__init__(test)
        self.test = test


def rest_json(base_url: str, token: str, method: str, path: str,
              payload: Any = None, *, timeout: float = 12) -> tuple[int, Any]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw, status = response.read(), response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        body = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        body = None
    return status, body


class MCPClient:
    def __init__(self, base_url: str, token: str, identity: str):
        self.base_url, self.token, self.identity = base_url, token, identity
        self.session_id: str | None = None
        self.next_id = 1

    def _http(self, payload: dict[str, Any], *, initialize: bool = False) -> tuple[int, Any, dict[str, str]]:
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
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
            status = exc.code
        if not raw:
            return status, None, response_headers
        content_type = response_headers.get("content-type", "")
        if "text/event-stream" in content_type:
            events = [line[6:].strip() for line in raw.decode("utf-8", "replace").splitlines()
                      if line.startswith("data:")]
            body = json.loads(events[-1]) if events else None
        else:
            body = json.loads(raw)
        return status, body, response_headers

    def initialize(self) -> list[str]:
        request_id = self.next_id
        self.next_id += 1
        status, body, headers = self._http({
            "jsonrpc": "2.0", "id": request_id, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": f"messaging-smoke-{self.identity}", "version": "1"},
            },
        }, initialize=True)
        if status != 200 or not body or body.get("id") != request_id or "result" not in body:
            raise SmokeFailure(f"mcp_initialize_{self.identity}")
        self.session_id = headers.get("mcp-session-id")
        if not self.session_id:
            raise SmokeFailure(f"mcp_session_{self.identity}")
        status, _, _ = self._http({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if status not in (200, 202):
            raise SmokeFailure(f"mcp_initialized_{self.identity}")
        result = self.rpc("tools/list", {})
        return sorted(item["name"] for item in result["tools"])

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        status, body, _ = self._http({"jsonrpc": "2.0", "id": request_id,
                                      "method": method, "params": params})
        if status != 200 or not body or body.get("id") != request_id or "error" in body:
            raise SmokeFailure(f"mcp_rpc_{method}")
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


def wait_healthy(container: str, base_url: str) -> dict[str, Any]:
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        inspect = run(["docker", "inspect", "--format", "{{.State.Running}}", container])
        if inspect.returncode != 0 or inspect.stdout.strip() != "true":
            raise SmokeFailure("container_running")
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=4) as response:
                status = json.loads(response.read())
            if status.get("ok") is True:
                return status
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(1)
    raise SmokeFailure("container_health")


def create_container(container: str, data_dir: Path, port: int, tokens: dict[str, str], label: str) -> None:
    host_allow = f"127.0.0.1:{port},127.0.0.1,localhost:{port},localhost"
    args = [
        "docker", "run", "--detach", "--name", container,
        "--label", f"org.hermes.live-messaging-smoke={label}",
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
        "--env", "ACK_RETENTION_DAYS=1",
        "--env", "REJECT_RETENTION_DAYS=1",
        "--env", f"HERMES_TOKEN={tokens['hermes']}",
        "--env", f"CODEX_TOKEN={tokens['codex']}",
        "--env", f"CLAUDE_TOKEN={tokens['claude']}",
        "--env", f"ADMIN_TOKEN={tokens['admin']}",
        IMAGE,
    ]
    result = run(args, timeout=90)
    if result.returncode != 0:
        raise SmokeFailure("docker_run")


def container_metadata(container: str, label: str, data_dir: Path, port: int) -> dict[str, Any]:
    fmt = "{{json .Mounts}}|{{.Config.User}}|{{.HostConfig.ReadonlyRootfs}}|{{json .HostConfig.PortBindings}}|{{index .Config.Labels \"org.hermes.live-messaging-smoke\"}}"
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
    published = bindings.get(f"{CONTAINER_PORT}/tcp", [])
    loopback = any(item.get("HostIp") == "127.0.0.1" and int(item.get("HostPort", 0)) == port
                   for item in published)
    valid = mount_ok and parts[1] == "568:568" and parts[2] == "true" and loopback and parts[4] == label
    return {"passed": valid, "bind_mount_count": len(mounts), "uid_gid": parts[1],
            "read_only_rootfs": parts[2] == "true", "loopback_port": loopback}


def chmod_fixture_tree(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise SmokeFailure("scratch_fixture_has_no_symlinks")
        os.chmod(path, 0o777 if path.is_dir() else 0o666, follow_symlinks=False)


def prepare_fixture_permissions(container: str) -> tuple[bool, str]:
    prepared = run(["docker", "exec", "--user", "568:568", container,
                    "python", "-c", FIXTURE_CHMOD_PROGRAM])
    return prepared.returncode == 0, prepared.stderr.strip()[-300:]


def copy_runtime_tree(data_dir: Path, backup_dir: Path) -> list[str]:
    copied: list[str] = []
    for name in ("files", "search"):
        source = data_dir / name
        if source.is_dir():
            shutil.copytree(source, backup_dir / name)
            copied.append(name)
    audit = data_dir / "audit.jsonl"
    if audit.is_file():
        shutil.copy2(audit, backup_dir / audit.name)
        copied.append(audit.name)
    return copied


def restore_runtime_tree(backup_dir: Path, data_dir: Path) -> None:
    for name in ("files", "search"):
        source = backup_dir / name
        if source.is_dir():
            shutil.copytree(source, data_dir / name)
    audit = backup_dir / "audit.jsonl"
    if audit.is_file():
        shutil.copy2(audit, data_dir / audit.name)


def container_inventory() -> list[str]:
    ids = run(["docker", "ps", "-aq", "--no-trunc"])
    if ids.returncode != 0:
        raise SmokeFailure("docker_container_inventory")
    output: list[str] = []
    for container_id in ids.stdout.splitlines():
        item = run(["docker", "inspect", "--format",
                    "{{.Id}}|{{.Image}}|{{.Name}}|{{.State.Status}}|{{.RestartCount}}", container_id])
        if item.returncode != 0:
            raise SmokeFailure("docker_container_inspect_inventory")
        output.append(item.stdout.strip())
    return sorted(output)


def image_ids() -> set[str]:
    result = run(["docker", "image", "ls", "-aq", "--no-trunc"])
    if result.returncode != 0:
        raise SmokeFailure("docker_image_inventory")
    return set(result.stdout.split())


def volume_names() -> set[str]:
    result = run(["docker", "volume", "ls", "--format", "{{.Name}}"])
    if result.returncode != 0:
        raise SmokeFailure("docker_volume_inventory")
    return set(result.stdout.split())


def sqlite_backup(source: Path, destination: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SmokeFailure("backup_sqlite_integrity")
    finally:
        dst.close()
        src.close()


def main() -> int:
    run_id = uuid.uuid4().hex
    label = run_id
    container = f"agent-bus-msg-{run_id[:12]}"
    root = SCRATCH / f".live-messaging-{run_id}"
    data_dir = root / "data"
    backup_dir = root / "backup"
    evidence_path = SCRATCH / f"live-messaging-evidence-{run_id[:12]}.json"
    tests: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "schema": "agent-message-bus-live-messaging-smoke/v1",
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
        "feed_scope": "notification feed only; host-client autowake and session routing not tested",
        "host_client_autowake_claimed": False,
        "production_deployed": False,
        "cleanup": {"container_removed": False, "scratch_fixture_removed": False},
    }
    tokens = {name: secrets.token_urlsafe(32) for name in ("hermes", "codex", "claude", "admin")}
    clients: dict[str, MCPClient] = {}
    created = False
    baseline_containers: list[str] = []
    baseline_images: set[str] = set()
    baseline_volumes: set[str] = set()
    initial_status: subprocess.CompletedProcess[str] | None = None
    failure: str | None = None

    def check(name: str, condition: bool, details: dict[str, Any] | None = None) -> None:
        tests.append({"name": name, "passed": bool(condition), **(details or {})})
        if not condition:
            raise SmokeFailure(name)

    def wait_path(after: int | None = None, *, since_now: bool = False,
                  timeout: float = 0, fields: str = "pointer", limit: int = 50) -> str:
        query: dict[str, str] = {"timeout": str(timeout), "fields": fields, "limit": str(limit)}
        if after is not None:
            query["after"] = str(after)
        if since_now:
            query["since_now"] = "true"
        return "/api/messages/wait?" + urllib.parse.urlencode(query)

    def send_message(base_url: str, sender: str, recipient: str, message_type: str,
                     body: dict[str, Any]) -> tuple[int, Any]:
        payload = {"recipient": recipient, "message_type": message_type, "body": body}
        return rest_json(base_url, tokens[sender], "POST", "/api/messages", payload)

    try:
        head = run(["git", "rev-parse", "HEAD"])
        check("repo_head_available", head.returncode == 0 and bool(head.stdout.strip()))
        evidence["git_head"] = head.stdout.strip()
        initial_status = run(["git", "status", "--porcelain=v1"])
        check("repo_status_available", initial_status.returncode == 0)
        evidence["initial_worktree_status_count"] = len(initial_status.stdout.splitlines())
        baseline_containers = container_inventory()
        baseline_images = image_ids()
        baseline_volumes = volume_names()
        evidence["docker_baseline"] = {"container_count": len(baseline_containers),
                                       "image_id_count": len(baseline_images),
                                       "volume_count": len(baseline_volumes)}

        existing = run(["docker", "image", "inspect", "--format", "{{.Id}}|{{json .Config.Labels}}", IMAGE])
        previous_image_id = None
        image_owned = existing.returncode != 0
        if existing.returncode == 0:
            previous_image_id, labels_text = existing.stdout.strip().split("|", 1)
            labels = json.loads(labels_text)
            image_owned = (labels.get("org.opencontainers.image.revision") == evidence["git_head"]
                           and labels.get("org.opencontainers.image.version") == IMAGE_VERSION)
        check("unique_v040_image_tag_is_unowned_or_matching", image_owned,
              {"tag_was_preexisting": previous_image_id is not None})
        evidence["image_before"] = previous_image_id
        if existing.returncode != 0:
            build = run(["docker", "build", "--tag", IMAGE,
                         "--build-arg", f"VCS_REF={evidence['git_head']}",
                         "--build-arg", f"VERSION={IMAGE_VERSION}", "."], timeout=900)
            check("docker_build_working_tree_image", build.returncode == 0)
        else:
            check("docker_reuse_matching_v040_image", True)
        image = run(["docker", "image", "inspect", "--format", "{{.Id}}|{{json .Config.Labels}}", IMAGE])
        check("v040_image_present_with_expected_labels", image.returncode == 0)
        image_id, image_labels_text = image.stdout.strip().split("|", 1)
        image_labels = json.loads(image_labels_text)
        check("v040_image_revision_and_version_labels",
              image_labels.get("org.opencontainers.image.revision") == evidence["git_head"]
              and image_labels.get("org.opencontainers.image.version") == IMAGE_VERSION)
        evidence["image_id"] = image_id

        root.mkdir(mode=0o700)
        data_dir.mkdir(mode=0o777)
        backup_dir.mkdir(mode=0o700)
        os.chmod(data_dir, 0o777)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        evidence["loopback_port"] = port
        evidence["bind_host"] = "127.0.0.1"
        create_container(container, data_dir, port, tokens, label)
        created = True
        metadata = container_metadata(container, label, data_dir, port)
        check("container_uid568_readonly_single_private_bind_loopback", metadata["passed"], metadata)
        health = wait_healthy(container, base_url)
        check("health_and_database_ok", health.get("ok") is True and health.get("database") == "ok")

        tool_counts: dict[str, int] = {}
        for identity in ("hermes", "codex", "claude"):
            client = MCPClient(base_url, tokens[identity], identity)
            names = client.initialize()
            check(f"mcp_tools_exact_v040_set_{identity}", set(names) == EXPECTED_TOOLS
                  and len(names) == len(EXPECTED_TOOLS), {"tool_count": len(names)})
            clients[identity] = client
            tool_counts[identity] = len(names)
        evidence["mcp_tool_counts"] = tool_counts

        marker = f"LIVE_MESSAGING_PRIVATE_{run_id}"
        subject = f"pointer-safe-subject-{run_id[:8]}"
        private_values = [marker, subject]
        sent1_status, sent1 = send_message(base_url, "hermes", "codex", "cursor-pending",
                                           {"subject": subject, "private": marker})
        sent2_status, sent2 = send_message(base_url, "hermes", "claude", "other-recipient",
                                           {"subject": "separate recipient"})
        sent3_status, sent3 = send_message(base_url, "hermes", "codex", "cursor-acknowledged",
                                           {"subject": "acknowledged fixture", "private": marker})
        sent4_status, sent4 = send_message(base_url, "hermes", "codex", "cursor-rejected",
                                           {"subject": "rejected fixture", "private": marker})
        check("cursor_message_fixtures_committed",
              all(status == 201 and isinstance(body, dict) and body.get("id")
                  for status, body in ((sent1_status, sent1), (sent2_status, sent2),
                                       (sent3_status, sent3), (sent4_status, sent4))))
        ids = {"pending": sent1["id"], "other_recipient": sent2["id"],
               "acknowledged": sent3["id"], "rejected": sent4["id"]}

        status, claimed = rest_json(base_url, tokens["codex"], "POST", f"/api/messages/{ids['acknowledged']}/claim")
        status_ack, acked = rest_json(base_url, tokens["codex"], "POST", f"/api/messages/{ids['acknowledged']}/ack")
        status, rejected_claim = rest_json(base_url, tokens["codex"], "POST", f"/api/messages/{ids['rejected']}/claim")
        status_reject, rejected = rest_json(base_url, tokens["codex"], "POST",
                                            f"/api/messages/{ids['rejected']}/reject", {"reason": "test only"})
        check("cursor_fixtures_have_mixed_statuses",
              claimed.get("status") == "claimed" and status_ack == 200 and acked.get("status") == "acknowledged"
              and rejected_claim.get("status") == "claimed" and status_reject == 200
              and rejected.get("status") == "rejected")

        page1_status, page1 = rest_json(base_url, tokens["codex"], "GET",
                                        wait_path(after=0, timeout=0, fields="pointer", limit=2))
        p1 = page1.get("messages", []) if isinstance(page1, dict) else []
        pointer_shape = all(set(item) == POINTER_KEYS for item in p1)
        check("recipient_cursor_pointers_are_private_and_paginated",
              page1_status == 200 and [item.get("id") for item in p1] == [ids["pending"], ids["acknowledged"]]
              and pointer_shape and all(item.get("recipient") == "codex" for item in p1)
              and page1.get("next_cursor") == p1[-1].get("seq")
              and p1[0].get("subject") == subject and "body" not in p1[0]
              and marker not in json.dumps(page1, sort_keys=True)
              and ids["other_recipient"] not in json.dumps(page1),
              {"pointer_count": len(p1), "first_page_cursor": page1.get("next_cursor")})
        page2_status, page2 = rest_json(base_url, tokens["codex"], "GET",
                                        wait_path(after=page1["next_cursor"], timeout=0,
                                                  fields="pointer", limit=2))
        p2 = page2.get("messages", []) if isinstance(page2, dict) else []
        check("recipient_cursor_next_page_uses_last_returned_seq",
              page2_status == 200 and [item.get("id") for item in p2] == [ids["rejected"]]
              and set(p2[0]) == POINTER_KEYS and p2[0]["seq"] > p1[-1]["seq"]
              and page2.get("next_cursor") == p2[0]["seq"])
        full_status, full = rest_json(base_url, tokens["codex"], "GET",
                                      wait_path(after=0, timeout=0, fields="full", limit=50))
        full_by_id = {item.get("id"): item for item in full.get("messages", [])} if isinstance(full, dict) else {}
        check("cursor_feed_includes_acknowledged_and_rejected_statuses_without_claiming",
              full_status == 200 and {ids["pending"], ids["acknowledged"], ids["rejected"]} <= full_by_id.keys()
              and full_by_id[ids["acknowledged"]].get("status") == "acknowledged"
              and full_by_id[ids["rejected"]].get("status") == "rejected"
              and full_by_id[ids["pending"]].get("status") == "pending")
        wrong_status, _ = rest_json(base_url, "not-a-valid-token", "GET",
                                    wait_path(after=0, timeout=0, fields="pointer"))
        check("cursor_wait_wrong_bearer_returns_401", wrong_status == 401, {"http_status": wrong_status})

        codex_mcp_cursor = clients["codex"].call("wait_for_messages", {
            "after": 0, "fields": "pointer", "limit": 2, "timeout": 0,
        })
        mcp_pointers = codex_mcp_cursor.get("messages", [])
        check("mcp_wait_for_messages_returns_pointer_only",
              len(mcp_pointers) == 2 and all(set(item) == POINTER_KEYS for item in mcp_pointers)
              and marker not in json.dumps(codex_mcp_cursor, sort_keys=True))

        snapshot_status, snapshot = rest_json(base_url, tokens["codex"], "GET",
                                              wait_path(since_now=True, timeout=0, fields="pointer"))
        old_backlog_head = snapshot.get("next_cursor") if isinstance(snapshot, dict) else None
        backlog_start = time.monotonic()
        backlog_status, backlog = rest_json(base_url, tokens["codex"], "GET",
                                            wait_path(since_now=True, timeout=0.25, fields="pointer"), timeout=4)
        backlog_elapsed = time.monotonic() - backlog_start
        check("since_now_ignores_old_backlog_and_times_out_at_snapshot",
              snapshot_status == 200 and backlog_status == 200 and old_backlog_head is not None
              and backlog.get("messages") == [] and backlog.get("next_cursor") == old_backlog_head
              and backlog_elapsed >= 0.18,
              {"elapsed_seconds": round(backlog_elapsed, 3), "snapshot_cursor": old_backlog_head})

        longpoll_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending_wait = executor.submit(
                rest_json, base_url, tokens["codex"], "GET",
                wait_path(after=old_backlog_head, timeout=2, fields="pointer", limit=10), None,
            )
            time.sleep(0.25)
            send5_status, send5 = send_message(base_url, "hermes", "codex", "committed-longpoll",
                                               {"subject": "committed while waiting", "private": marker})
            longpoll_status, longpoll = pending_wait.result(timeout=2)
        longpoll_elapsed = time.monotonic() - longpoll_start
        longpoll_messages = longpoll.get("messages", []) if isinstance(longpoll, dict) else []
        check("committed_longpoll_delivery_within_two_seconds",
              send5_status == 201 and longpoll_status == 200 and len(longpoll_messages) == 1
              and longpoll_messages[0].get("id") == send5.get("id")
              and longpoll_messages[0].get("seq", 0) > old_backlog_head
              and longpoll_elapsed < 2.0,
              {"elapsed_seconds": round(longpoll_elapsed, 3), "messages_returned": len(longpoll_messages)})

        since_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as executor:
            since_wait = executor.submit(
                rest_json, base_url, tokens["codex"], "GET",
                wait_path(since_now=True, timeout=2, fields="pointer", limit=10), None,
            )
            time.sleep(0.25)
            send6_status, send6 = send_message(base_url, "hermes", "codex", "since-now-after-snapshot",
                                               {"subject": "after fixed request snapshot", "private": marker})
            since_status, since_data = since_wait.result(timeout=2)
        since_elapsed = time.monotonic() - since_start
        since_messages = since_data.get("messages", []) if isinstance(since_data, dict) else []
        check("since_now_uses_one_snapshot_and_delivers_later_commit",
              send6_status == 201 and since_status == 200 and len(since_messages) == 1
              and since_messages[0].get("id") == send6.get("id")
              and since_messages[0].get("seq", 0) > send5.get("seq", 0)
              and since_elapsed < 2.0,
              {"elapsed_seconds": round(since_elapsed, 3), "messages_returned": len(since_messages)})
        timeout_start = time.monotonic()
        timeout_status, timeout_data = rest_json(
            base_url, tokens["codex"], "GET",
            wait_path(after=since_messages[0]["seq"], timeout=0.25, fields="pointer"), timeout=4,
        )
        timeout_elapsed = time.monotonic() - timeout_start
        check("cursor_timeout_preserves_requested_cursor",
              timeout_status == 200 and timeout_data.get("messages") == []
              and timeout_data.get("next_cursor") == since_messages[0]["seq"]
              and timeout_elapsed >= 0.18,
              {"elapsed_seconds": round(timeout_elapsed, 3), "unchanged_cursor": timeout_data.get("next_cursor")})

        # All three authenticated MCP identities submit a distinct report; each can read/list
        # the common inbox, and the server, not payload data, assigns the author.
        project = f"smoke-{run_id[:10]}"
        report_payloads = {
            "hermes": {"kind": "bug", "title": f"{marker} Hermes report",
                       "description": f"{marker} Hermes private report body", "project": project,
                       "severity": "high", "client_request_id": f"{run_id}-hermes"},
            "codex": {"kind": "improvement", "title": f"{marker} Codex report",
                      "description": f"{marker} Codex private report body", "project": project,
                      "severity": "normal", "client_request_id": f"{run_id}-codex"},
            "claude": {"kind": "info", "title": f"{marker} Claude report",
                       "description": f"{marker} Claude private report body", "project": project,
                       "severity": "low", "client_request_id": f"{run_id}-claude"},
        }
        private_values.extend(value for payload in report_payloads.values()
                              for key, value in payload.items() if key in {"title", "description"})
        reports: dict[str, dict[str, Any]] = {}
        for identity, payload in report_payloads.items():
            report = clients[identity].call("feedback_submit", payload)
            reports[identity] = report
        check("feedback_submit_uses_all_three_authenticated_identities",
              all(reports[identity].get("author") == identity and reports[identity].get("revision") == 1
                  and reports[identity].get("status") == "open" for identity in reports),
              {"reports_created": len(reports)})

        shared_read_checks = []
        for reader, owner in (("hermes", "codex"), ("codex", "claude"), ("claude", "hermes")):
            item = clients[reader].call("feedback_get", {"feedback_id": reports[owner]["id"]})
            listing = clients[reader].call("feedback_list", {"project": project, "limit": 20})
            shared_read_checks.append(
                item.get("feedback", {}).get("author") == owner
                and any(entry.get("id") == reports[owner]["id"] for entry in listing.get("items", []))
            )
        check("feedback_get_and_list_are_shared_across_all_agents", all(shared_read_checks),
              {"agent_read_paths": len(shared_read_checks)})

        claude_payload = report_payloads["claude"]
        idempotent = clients["claude"].call("feedback_submit", claude_payload)
        conflict_raw = clients["claude"].call_raw("feedback_submit", {
            **claude_payload, "description": f"{marker} changed idempotent payload",
        })
        rest_idempotent_status, rest_idempotent = rest_json(
            base_url, tokens["claude"], "POST", "/api/feedback", claude_payload)
        rest_conflict_status, rest_conflict = rest_json(
            base_url, tokens["claude"], "POST", "/api/feedback",
            {**claude_payload, "description": f"{marker} changed REST retry"})
        check("feedback_idempotency_returns_original_and_conflicts_on_changed_payload",
              idempotent.get("id") == reports["claude"].get("id")
              and conflict_raw.get("isError") is True
              and rest_idempotent_status == 201 and rest_idempotent.get("id") == reports["claude"].get("id")
              and rest_conflict_status == 409
              and marker not in json.dumps(rest_conflict, sort_keys=True),
              {"changed_payload_http_status": rest_conflict_status})

        spoof_status, spoof_body = rest_json(base_url, tokens["claude"], "POST", "/api/feedback",
                                              {**claude_payload, "author": "hermes", "identity": "hermes"})
        invalid_auth_status, _ = rest_json(base_url, "bad-feedback-token", "POST", "/api/feedback",
                                            report_payloads["hermes"])
        check("feedback_rejects_identity_spoof_and_wrong_bearer",
              spoof_status == 422 and invalid_auth_status == 401
              and marker not in json.dumps(spoof_body, sort_keys=True),
              {"spoof_http_status": spoof_status, "wrong_bearer_http_status": invalid_auth_status})

        claude_update_raw = clients["claude"].call_raw("feedback_update", {
            "feedback_id": reports["claude"]["id"], "expected_revision": 1,
            "status": "triaged", "note": f"{marker} Claude evidence note",
        })
        codex_forbidden_raw = clients["codex"].call_raw("feedback_update", {
            "feedback_id": reports["claude"]["id"], "expected_revision": 1,
            "status": "resolved", "note": "unauthorized update test",
        })
        hermes_maintainer = clients["hermes"].call("feedback_update", {
            "feedback_id": reports["codex"]["id"], "expected_revision": 1,
            "status": "planned", "note": "reviewed; not approved to execute",
        })
        stale_raw = clients["claude"].call_raw("feedback_update", {
            "feedback_id": reports["claude"]["id"], "expected_revision": 1,
            "status": "resolved", "note": "stale CAS test",
        })
        check("feedback_update_enforces_author_maintainer_forbidden_and_cas",
              not claude_update_raw.get("isError") and claude_update_raw.get("structuredContent", {}).get("revision", 0) == 2
              and codex_forbidden_raw.get("isError") is True
              and hermes_maintainer.get("revision") == 2 and hermes_maintainer.get("status") == "planned"
              and stale_raw.get("isError") is True,
              {"author_update": not claude_update_raw.get("isError"),
               "forbidden_update_rejected": codex_forbidden_raw.get("isError") is True,
               "stale_cas_rejected": stale_raw.get("isError") is True})

        history_first = clients["codex"].call("feedback_get", {
            "feedback_id": reports["claude"]["id"], "after_revision": 0, "history_limit": 1,
        })
        history_second = clients["codex"].call("feedback_get", {
            "feedback_id": reports["claude"]["id"], "after_revision": 1, "history_limit": 1,
        })
        first_history = history_first.get("history", [])
        second_history = history_second.get("history", [])
        check("feedback_history_is_immutable_attributed_and_paginated",
              history_first.get("feedback", {}).get("revision") == 2
              and len(first_history) == 1 and first_history[0].get("revision") == 1
              and history_first.get("next_revision") == 1
              and len(second_history) == 1 and second_history[0].get("revision") == 2
              and second_history[0].get("actor") == "claude"
              and second_history[0].get("status") == "triaged"
              and second_history[0].get("note") == f"{marker} Claude evidence note"
              and history_second.get("next_revision") is None)

        final_snapshot_status, final_snapshot = rest_json(
            base_url, tokens["codex"], "GET", wait_path(since_now=True, timeout=0, fields="pointer"))
        snapshot_cursor = final_snapshot.get("next_cursor") if isinstance(final_snapshot, dict) else None
        restarted = run(["docker", "restart", container], timeout=90)
        check("container_restart_command", restarted.returncode == 0)
        restart_health = wait_healthy(container, base_url)
        check("container_restart_health", restart_health.get("ok") is True)
        after_restart: dict[str, MCPClient] = {}
        for identity in ("hermes", "codex", "claude"):
            client = MCPClient(base_url, tokens[identity], identity)
            names = client.initialize()
            check(f"mcp_tools_after_restart_{identity}", set(names) == EXPECTED_TOOLS
                  and len(names) == len(EXPECTED_TOOLS), {"tool_count": len(names)})
            after_restart[identity] = client
        restart_feed = after_restart["codex"].call("wait_for_messages", {
            "after": 0, "fields": "pointer", "limit": 100, "timeout": 0,
        })
        restart_pointers = restart_feed.get("messages", [])
        restart_seq = max((item.get("seq", 0) for item in restart_pointers), default=0)
        persisted_feedback = after_restart["codex"].call("feedback_get", {
            "feedback_id": reports["claude"]["id"], "after_revision": 1, "history_limit": 2,
        })
        check("message_sequence_and_feedback_history_persist_after_restart",
              final_snapshot_status == 200 and snapshot_cursor is not None
              and restart_seq == snapshot_cursor and any(item.get("id") == send6.get("id") for item in restart_pointers)
              and persisted_feedback.get("feedback", {}).get("revision") == 2
              and any(item.get("revision") == 2 for item in persisted_feedback.get("history", [])))

        # Age only this private test fixture's acknowledged/rejected timestamps inside
        # the running UID-568 container, then exercise the public cleanup endpoint.
        age_sql = (
            "import sqlite3; c=sqlite3.connect('/var/lib/agent-message-bus/messages.sqlite3',timeout=10); "
            f"a=c.execute(\"UPDATE messages SET acknowledged_at='2000-01-01T00:00:00Z' WHERE id=? AND status='acknowledged'\","
            f"({ids['acknowledged']!r},)).rowcount; "
            f"r=c.execute(\"UPDATE messages SET rejected_at='2000-01-01T00:00:00Z' WHERE id=? AND status='rejected'\","
            f"({ids['rejected']!r},)).rowcount; c.commit(); print(a,r); c.close()"
        )
        aged = run(["docker", "exec", "--user", "568:568", container, "python", "-c", age_sql])
        age_counts = aged.stdout.strip().split() if aged.returncode == 0 else []
        aged_rows = age_counts == ["1", "1"]
        cleanup_status, cleanup_result = rest_json(base_url, tokens["admin"], "POST", "/api/admin/cleanup")
        check("retention_cleanup_deleted_old_acknowledged_and_rejected_rows",
              aged_rows and cleanup_status == 200
              and cleanup_result.get("acknowledged_deleted") == 1
              and cleanup_result.get("rejected_deleted") == 1,
              {"http_status": cleanup_status,
               "acknowledged_deleted": cleanup_result.get("acknowledged_deleted") if isinstance(cleanup_result, dict) else None,
               "rejected_deleted": cleanup_result.get("rejected_deleted") if isinstance(cleanup_result, dict) else None})
        deleted_ack_status, _ = rest_json(base_url, tokens["codex"], "GET", f"/api/messages/{ids['acknowledged']}")
        deleted_reject_status, _ = rest_json(base_url, tokens["codex"], "GET", f"/api/messages/{ids['rejected']}")
        after_retention_status, after_retention = rest_json(
            base_url, tokens["codex"], "GET",
            wait_path(after=snapshot_cursor, timeout=0, fields="pointer"))
        send7_status, send7 = send_message(base_url, "hermes", "codex", "after-retention",
                                           {"subject": "sequence continues after retention"})
        seq_after_retention_status, seq_after_retention = rest_json(
            base_url, tokens["codex"], "GET",
            wait_path(after=snapshot_cursor, timeout=0, fields="pointer"))
        seq_after = seq_after_retention.get("messages", []) if isinstance(seq_after_retention, dict) else []
        check("retention_preserves_high_water_and_next_network_cursor",
              deleted_ack_status == 404 and deleted_reject_status == 404
              and after_retention_status == 200 and after_retention.get("messages") == []
              and after_retention.get("next_cursor") == snapshot_cursor
              and send7_status == 201 and seq_after_retention_status == 200 and len(seq_after) == 1
              and seq_after[0].get("id") == send7.get("id")
              and seq_after[0].get("seq", 0) > snapshot_cursor,
              {"snapshot_cursor": snapshot_cursor,
               "next_sequence": seq_after[0].get("seq") if seq_after else None})

        # Take a coherent SQLite backup plus the complete immutable object tree, then
        # replace only the labeled smoke container's private bind and prove readback.
        coherent_status, coherent_head = rest_json(
            base_url, tokens["codex"], "GET", wait_path(since_now=True, timeout=0, fields="pointer"))
        backup_cursor = coherent_head.get("next_cursor") if isinstance(coherent_head, dict) else None
        backup_permissions, backup_permission_error = prepare_fixture_permissions(container)
        check("scratch_fixture_backup_permissions", backup_permissions,
              {"permission_error": backup_permission_error})
        stopped_for_backup = run(["docker", "stop", container], timeout=60)
        check("container_stopped_for_coherent_backup", coherent_status == 200
              and backup_cursor is not None and stopped_for_backup.returncode == 0)
        active_db = data_dir / "messages.sqlite3"
        sqlite_backup(active_db, backup_dir / "messages.sqlite3")
        copied_runtime = copy_runtime_tree(data_dir, backup_dir)
        backup_conn = sqlite3.connect(f"file:{backup_dir / 'messages.sqlite3'}?mode=ro", uri=True)
        try:
            backup_integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
            feedback_rows = backup_conn.execute("SELECT COUNT(*) FROM feedback_entries").fetchone()[0]
            feedback_history_rows = backup_conn.execute("SELECT COUNT(*) FROM feedback_history").fetchone()[0]
            sequence_row = backup_conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='message_delivery_sequence'"
            ).fetchone()
            backup_sequence = sequence_row[0] if sequence_row else 0
            seq_map_count = backup_conn.execute("SELECT COUNT(*) FROM message_delivery_sequence").fetchone()[0]
        finally:
            backup_conn.close()
        check("coherent_backup_contains_cursor_and_feedback_state",
              backup_integrity == "ok" and feedback_rows == 3 and feedback_history_rows >= 5
              and backup_sequence == backup_cursor and seq_map_count >= 5,
              {"integrity_check": backup_integrity, "feedback_rows": feedback_rows,
               "feedback_history_rows": feedback_history_rows, "sequence_high_water": backup_sequence,
               "sequence_map_rows": seq_map_count, "runtime_paths_copied": copied_runtime})

        ownership = run(["docker", "inspect", "--format",
                         "{{index .Config.Labels \"org.hermes.live-messaging-smoke\"}}", container])
        check("restore_container_owned_by_this_run", ownership.returncode == 0
              and ownership.stdout.strip() == label)
        removed_for_restore = run(["docker", "rm", container], timeout=60)
        check("remove_only_owned_container_for_restore", removed_for_restore.returncode == 0)
        created = False
        os.replace(data_dir, root / "pre_restore_data")
        data_dir.mkdir(mode=0o777)
        shutil.copy2(backup_dir / "messages.sqlite3", data_dir / "messages.sqlite3")
        restore_runtime_tree(backup_dir, data_dir)
        chmod_fixture_tree(data_dir)
        create_container(container, data_dir, port, tokens, label)
        created = True
        restored_health = wait_healthy(container, base_url)
        check("restored_container_health", restored_health.get("ok") is True)
        restored_clients: dict[str, MCPClient] = {}
        restored_tool_counts: dict[str, int] = {}
        for identity in ("hermes", "codex", "claude"):
            client = MCPClient(base_url, tokens[identity], identity)
            names = client.initialize()
            check(f"restored_mcp_tools_{identity}", set(names) == EXPECTED_TOOLS
                  and len(names) == len(EXPECTED_TOOLS), {"tool_count": len(names)})
            restored_clients[identity] = client
            restored_tool_counts[identity] = len(names)
        restored_feed = restored_clients["codex"].call("wait_for_messages", {
            "after": 0, "fields": "pointer", "limit": 100, "timeout": 0,
        })
        restored_pointers = restored_feed.get("messages", [])
        restored_head = max((item.get("seq", 0) for item in restored_pointers), default=0)
        restored_report = restored_clients["claude"].call("feedback_get", {
            "feedback_id": reports["claude"]["id"], "after_revision": 0, "history_limit": 10,
        })
        restored_list = restored_clients["hermes"].call("feedback_list", {"project": project, "limit": 10})
        check("coherent_restore_reads_cursor_feedback_and_history",
              restored_head == backup_cursor and any(item.get("id") == send7.get("id") for item in restored_pointers)
              and restored_report.get("feedback", {}).get("revision") == 2
              and len(restored_report.get("history", [])) == 2
              and any(item.get("id") == reports["hermes"]["id"] for item in restored_list.get("items", [])))
        evidence["backup"] = {
            "sqlite_integrity": backup_integrity,
            "sequence_high_water": backup_sequence,
            "sequence_rows_after_retention": seq_map_count,
            "feedback_rows": feedback_rows,
            "feedback_history_rows": feedback_history_rows,
            "runtime_paths_copied": copied_runtime,
            "restored_cursor_head": restored_head,
            "restored_feedback_revision": restored_report.get("feedback", {}).get("revision"),
            "restored_from_snapshot": True,
        }

        logs = run(["docker", "logs", container], timeout=30)
        log_text = logs.stdout + logs.stderr
        leaks = sum(1 for value in [*tokens.values(), *private_values]
                    if value and value in log_text)
        check("container_logs_exclude_tokens_and_private_feedback", logs.returncode == 0 and leaks == 0,
              {"private_values_found": leaks, "log_bytes": len(log_text.encode())})
        evidence["log_check"] = {"passed": True, "bytes_examined": len(log_text.encode()),
                                 "secret_or_private_matches": 0}
        evidence["test_result"] = "passed"

    except BaseException as exc:
        failure = exc.test if isinstance(exc, SmokeFailure) else type(exc).__name__
        if not any(item.get("name") == failure and item.get("passed") is False for item in tests):
            tests.append({"name": failure, "passed": False})
        evidence["test_result"] = "failed"
        evidence["failure"] = {"test_or_exception": failure}
    finally:
        if created:
            owned = run(["docker", "inspect", "--format", "{{json .Config.Labels}}", container])
            labels = json.loads(owned.stdout) if owned.returncode == 0 else {}
            if owned.returncode == 0 and labels.get("org.hermes.live-messaging-smoke") == label:
                running = run(["docker", "inspect", "--format", "{{.State.Running}}", container])
                if running.returncode == 0 and running.stdout.strip() != "true":
                    run(["docker", "start", container], timeout=60)
                permissions_ready, _permission_error = prepare_fixture_permissions(container)
                evidence["cleanup"]["scratch_permissions_prepared"] = permissions_ready
                removed = run(["docker", "rm", "-f", container], timeout=60)
                evidence["cleanup"]["container_removed"] = removed.returncode == 0
            else:
                evidence["cleanup"]["container_cleanup_refused_not_owned"] = True
        else:
            exists = run(["docker", "inspect", container])
            evidence["cleanup"]["container_removed"] = exists.returncode != 0
        if root.exists():
            try:
                shutil.rmtree(root)
                evidence["cleanup"]["scratch_fixture_removed"] = True
            except OSError:
                evidence["cleanup"]["scratch_fixture_removed"] = False

        try:
            current_containers = container_inventory()
            current_images = image_ids()
            current_volumes = volume_names()
            no_preexisting_container_changes = current_containers == baseline_containers
            no_preexisting_image_removals = baseline_images <= current_images
            no_volume_changes = baseline_volumes == current_volumes
            evidence["docker_preservation"] = {
                "preexisting_containers_unchanged": no_preexisting_container_changes,
                "preexisting_image_ids_preserved": no_preexisting_image_removals,
                "volumes_unchanged": no_volume_changes,
                "baseline_container_count": len(baseline_containers),
                "final_container_count": len(current_containers),
                "baseline_image_count": len(baseline_images),
                "final_image_count": len(current_images),
                "baseline_volume_count": len(baseline_volumes),
                "final_volume_count": len(current_volumes),
            }
            preservation_ok = (no_preexisting_container_changes and no_preexisting_image_removals
                               and no_volume_changes)
            tests.append({"name": "preexisting_docker_containers_images_volumes_preserved",
                          "passed": preservation_ok})
            if not preservation_ok and failure is None:
                failure = "preexisting_docker_containers_images_volumes_preserved"
                evidence["test_result"] = "failed"
                evidence["failure"] = {"test_or_exception": failure}
        except BaseException:
            tests.append({"name": "docker_preservation_inventory", "passed": False})
            if failure is None:
                failure = "docker_preservation_inventory"
                evidence["test_result"] = "failed"
                evidence["failure"] = {"test_or_exception": failure}

        current_status = run(["git", "status", "--porcelain=v1"])
        unchanged_status = (current_status.returncode == 0 and initial_status is not None
                            and current_status.stdout.splitlines() == initial_status.stdout.splitlines())
        tests.append({"name": "repo_status_unchanged_during_run", "passed": unchanged_status})
        evidence["worktree"] = {
            "initial_status_count": len(initial_status.stdout.splitlines()) if initial_status is not None else None,
            "final_status_count": len(current_status.stdout.splitlines()) if current_status.returncode == 0 else None,
            "unchanged_during_run": unchanged_status,
        }
        if not unchanged_status and failure is None:
            failure = "repo_status_unchanged_during_run"
            evidence["test_result"] = "failed"
            evidence["failure"] = {"test_or_exception": failure}

        evidence["passed_test_count"] = sum(item["passed"] is True for item in tests)
        evidence["failed_test_count"] = sum(item["passed"] is False for item in tests)
        evidence["tests_total"] = len(tests)
        try:
            evidence_path.write_text(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n")
            os.chmod(evidence_path, 0o600)
        except OSError:
            evidence["evidence_write_error"] = True

    print(json.dumps({"result": evidence.get("test_result"), "evidence": str(evidence_path),
                      "tests_passed": evidence.get("passed_test_count"),
                      "tests_failed": evidence.get("failed_test_count"),
                      "container_removed": evidence["cleanup"].get("container_removed"),
                      "scratch_fixture_removed": evidence["cleanup"].get("scratch_fixture_removed")},
                     sort_keys=True))
    return 0 if evidence.get("test_result") == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
