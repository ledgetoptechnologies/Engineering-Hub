from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import stat
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp import types
import uvicorn

from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings

from scripts.mcp_push_worker import (
    CapabilityError,
    HermesRunner,
    PushWorker,
    QueueFull,
    RunOutcome,
    UnsafeResume,
    WorkerState,
    build_hermes_argv,
    load_profile_config,
    main,
    read_token,
    require_subscription_capability,
    validate_readonly_profile_config,
    validate_url,
    _parse_args,
)


PROFILE = "mcp-worker"
SESSION = "20261015_212152_d7d4c9"
TEST_AUTH = AuthSettings("push-worker-hermes-fixture", "push-worker-codex-fixture")


class _ReadyServer(uvicorn.Server):
    def __init__(self, config):
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        self.ready.set()


@asynccontextmanager
async def _loopback_mcp(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = _ReadyServer(uvicorn.Config(app, log_level="critical", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(server.ready.wait(), timeout=5)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            server.force_exit = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        listener.close()
        current = asyncio.current_task()
        watchers = [pending for pending in asyncio.all_tasks()
                    if pending is not current and not pending.done()
                    and pending.get_coro().__qualname__ == "_shutdown_watcher"]
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)


def message(seq: int, message_type: str = "TASK", *, topic: str = "qa", sender: str = "codex",
            correlation_id: str | None = "job-1", in_reply_to: str | None = None,
            message_id: str | None = None, status: str = "pending", recipient: str = "hermes") -> dict:
    return {
        "id": message_id or str(uuid.uuid4()), "seq": seq, "sender": sender, "recipient": recipient,
        "message_type": message_type, "priority": "normal", "status": status,
        "correlation_id": correlation_id, "in_reply_to": in_reply_to,
        "body": {"topic": topic, "description": "UNTRUSTED BODY MUST NOT BE PASSED TO CLI"},
    }


def test_page_queue_and_cursor_commit_together_and_dedupe(tmp_path):
    state = WorkerState(tmp_path / "state.sqlite3")
    state.set_initial_cursor(4)
    item = message(5)

    assert state.ingest_page([item], 5, identity="hermes", allowed_senders={"codex"},
                             allowed_topics={"qa"}, allowed_hubs=set(), profile=PROFILE) == [item["id"]]
    assert state.cursor == 5
    assert state.queued_ids() == [item["id"]]

    duplicate_delivery = {**item, "seq": 6}
    assert state.ingest_page([duplicate_delivery], 6, identity="hermes", allowed_senders={"codex"},
                             allowed_topics={"qa"}, allowed_hubs=set(), profile=PROFILE) == []
    assert state.cursor == 6
    assert state.queued_ids() == [item["id"]]


def test_topics_senders_and_non_actionable_types_are_filtered(tmp_path):
    state = WorkerState(tmp_path / "state.sqlite3")
    state.set_initial_cursor(0)
    items = [
        message(1, "INFO"), message(2, "STATUS"), message(3, "ACK"),
        message(4, "REQUEST", topic="not-opted-in"), message(5, "TASK", sender="claude"),
        message(6, "TASK", status="acknowledged"), message(7, "TASK", recipient="codex"),
        message(8, "TASK", topic="qa"),
    ]
    accepted = state.ingest_page(items, 8, identity="hermes", allowed_senders={"codex"},
                                 allowed_topics={"qa"}, allowed_hubs=set(), profile=PROFILE)
    assert accepted == [items[-1]["id"]]
    assert state.cursor == 8


def test_hub_optin_requires_one_exact_local_route(tmp_path):
    state = WorkerState(tmp_path / "state.sqlite3")
    state.set_initial_cursor(0)
    allowed = message(1)
    allowed["body"] = {"hub": "qa-hub"}
    ambiguous = message(2, correlation_id="ambiguous")
    ambiguous["body"] = {"topic": "qa", "hub": "qa-hub"}
    accepted = state.ingest_page([allowed, ambiguous], 2, identity="hermes",
                                 allowed_senders={"codex"}, allowed_topics={"qa"},
                                 allowed_hubs={"qa-hub"})
    assert accepted == [allowed["id"]]


def test_same_correlation_and_topic_reuses_session_but_other_job_does_not(tmp_path):
    state = WorkerState(tmp_path / "state.sqlite3")
    state.set_initial_cursor(0)
    first = message(1, correlation_id="same")
    state.ingest_page([first], 1, identity="hermes", allowed_senders={"codex"},
                      allowed_topics={"qa"}, allowed_hubs=set(), profile=PROFILE)
    job = state.next_job()
    assert job.message_id == first["id"]
    assert state.begin_job(job) is None
    state.finish_job(job, outcome="succeeded", session_id=SESSION)

    same = message(2, "TASK", topic="qa", correlation_id="same")
    other = message(3, "TASK", topic="other", correlation_id="same")
    fresh = message(4, "TASK", topic="qa", correlation_id="new")
    state.ingest_page([same, other, fresh], 4, identity="hermes", allowed_senders={"codex"},
                      allowed_topics={"qa", "other"}, allowed_hubs=set(), profile=PROFILE)
    next_job = state.next_job()
    assert next_job.message_id == same["id"]
    assert state.begin_job(next_job) == SESSION
    state.finish_job(next_job, outcome="succeeded", session_id=SESSION)
    unrelated = state.next_job()
    assert unrelated is not None and unrelated.message_id == other["id"]
    assert state.begin_job(unrelated) is None
    fresh_job = state.next_job()
    assert fresh_job is not None and fresh_job.message_id == fresh["id"]
    assert state.begin_job(fresh_job) is None


def test_reply_requires_an_existing_mapped_job_and_resumes_only_that_session(tmp_path):
    state = WorkerState(tmp_path / "state.sqlite3")
    state.set_initial_cursor(0)
    stray = message(1, "REPLY", in_reply_to=str(uuid.uuid4()))
    parent = message(2, "REQUEST", correlation_id="corr")
    reply = message(3, "REPLY", correlation_id="corr", in_reply_to=parent["id"])
    state.ingest_page([stray, parent], 2, identity="hermes", allowed_senders={"codex"},
                      allowed_topics={"qa"}, allowed_hubs=set(), profile=PROFILE)
    first = state.next_job()
    assert first.message_id == parent["id"]
    state.begin_job(first)
    state.finish_job(first, outcome="succeeded", session_id=SESSION)
    state.ingest_page([reply], 3, identity="hermes", allowed_senders={"codex"},
                      allowed_topics={"qa"}, allowed_hubs=set(), profile=PROFILE)
    queued = state.next_job()
    assert queued.message_id == reply["id"]
    assert state.begin_job(queued) == SESSION


def test_uncertain_worker_outcome_is_never_resumed_or_retried(tmp_path):
    state = WorkerState(tmp_path / "state.sqlite3")
    state.set_initial_cursor(0)
    first = message(1, correlation_id="unsafe")
    state.ingest_page([first], 1, identity="hermes", allowed_senders={"codex"},
                      allowed_topics={"qa"}, allowed_hubs=set(), profile=PROFILE)
    job = state.next_job()
    state.begin_job(job)
    followup = message(2, correlation_id="unsafe")
    state.ingest_page([followup], 2, identity="hermes", allowed_senders={"codex"},
                      allowed_topics={"qa"}, allowed_hubs=set(), profile=PROFILE)
    state.finish_job(job, outcome="uncertain", session_id=None)
    queued = state.next_job()
    with pytest.raises(UnsafeResume):
        state.begin_job(queued)


def test_running_job_after_restart_is_unknown_not_requeued(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = WorkerState(path)
    state.set_initial_cursor(0)
    item = message(1)
    state.ingest_page([item], 1, identity="hermes", allowed_senders={"codex"},
                      allowed_topics={"qa"}, allowed_hubs=set(), profile=PROFILE)
    job = state.next_job()
    state.begin_job(job)
    restarted = WorkerState(path)
    restarted.recover_interrupted_jobs()
    assert restarted.next_job() is None
    assert restarted.item_status(item["id"]) == "uncertain"


def test_queued_uuid_and_cursor_survive_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = WorkerState(path)
    state.set_initial_cursor(9)
    item = message(10)
    state.ingest_page([item], 10, identity="hermes", allowed_senders={"codex"},
                      allowed_topics={"qa"}, allowed_hubs=set())

    restarted = WorkerState(path)
    assert restarted.cursor == 10
    assert restarted.queued_ids() == [item["id"]]
    queued = restarted.next_job()
    assert queued is not None and queued.message_id == item["id"]


def test_queue_capacity_refuses_cursor_advance_for_the_whole_page(tmp_path):
    state = WorkerState(tmp_path / "state.sqlite3", max_queue=1)
    state.set_initial_cursor(0)
    first, second = message(1), message(2, correlation_id="second")
    state.ingest_page([first], 1, identity="hermes", allowed_senders={"codex"},
                      allowed_topics={"qa"}, allowed_hubs=set())
    with pytest.raises(QueueFull, match="queue is full"):
        state.ingest_page([second], 2, identity="hermes", allowed_senders={"codex"},
                          allowed_topics={"qa"}, allowed_hubs=set())
    assert state.cursor == 1
    assert state.queued_ids() == [first["id"]]
    assert state.item_status(second["id"]) is None


def test_readonly_capability_gate_requires_exact_server_tool_whitelist():
    safe = {"mcp_servers": {"agent-bus-production": {
        "url": "https://bus.example.test/mcp",
        "tools": {
        "include": ["get_message"], "resources": False, "prompts": False,
    }}}}
    validate_readonly_profile_config(safe)
    with pytest.raises(CapabilityError):
        validate_readonly_profile_config(safe, expected_url="https://bus.example.test/mcp/")
    with pytest.raises(CapabilityError):
        validate_readonly_profile_config(safe, expected_url="https://other.example.test/mcp")
    with pytest.raises(CapabilityError):
        validate_readonly_profile_config({"mcp_servers": {"agent-bus-production": {}}})
    unsafe = json.loads(json.dumps(safe))
    unsafe["mcp_servers"]["agent-bus-production"]["tools"]["include"].append("send_message")
    with pytest.raises(CapabilityError):
        validate_readonly_profile_config(unsafe)


def test_coordination_capability_gate_requires_exact_five_bus_tools():
    config = {"mcp_servers": {"agent-bus-production": {
        "url": "http://192.168.50.80:8765/mcp",
        "tools": {
            "include": ["get_message", "claim_message", "ack_message", "send_message", "bus_info"],
            "resources": False,
            "prompts": False,
        },
    }}}
    validate_readonly_profile_config(
        config, mode="coordination", expected_url="http://192.168.50.80:8765/mcp")

    for changed in (
        {**config["mcp_servers"]["agent-bus-production"]["tools"],
         "include": ["get_message", "claim_message", "ack_message", "send_message"]},
        {**config["mcp_servers"]["agent-bus-production"]["tools"],
         "include": ["get_message", "claim_message", "ack_message", "send_message", "bus_info", "file_put"]},
    ):
        unsafe = json.loads(json.dumps(config))
        unsafe["mcp_servers"]["agent-bus-production"]["tools"] = changed
        with pytest.raises(CapabilityError):
            validate_readonly_profile_config(unsafe, mode="coordination")

    with_other_server = json.loads(json.dumps(config))
    with_other_server["mcp_servers"]["remote-cloud"] = {"enabled": True}
    with pytest.raises(CapabilityError):
        validate_readonly_profile_config(with_other_server, mode="coordination")


def test_private_http_requires_explicit_rfc1918_ipv4_opt_in():
    validate_url("https://bus.example.test/mcp")
    validate_url("http://192.168.50.80:8765/mcp", allow_private_http=True)
    for url in (
        "http://8.8.8.8:8765/mcp",
        "http://10.5.6.7:8765/mcp",
        "http://bus.example.test:8765/mcp",
        "http://127.0.0.1:8765/mcp",
        "http://169.254.1.2:8765/mcp",
        "http://[fd00::1]:8765/mcp",
    ):
        with pytest.raises(CapabilityError):
            validate_url(url, allow_private_http=True)
    with pytest.raises(CapabilityError):
        validate_url("http://192.168.50.80:8765/mcp")


def test_missing_native_subscription_capability_fails_before_reading():
    with pytest.raises(CapabilityError):
        require_subscription_capability(SimpleNamespace(capabilities=SimpleNamespace(resources=None)))
    require_subscription_capability(SimpleNamespace(
        capabilities=SimpleNamespace(resources=SimpleNamespace(subscribe=True))))


def test_profile_yaml_gate_and_fixture_token_file_are_read_only(tmp_path):
    profile_config = tmp_path / "config.yaml"
    profile_config.write_text(
        "mcp_servers:\n"
        "  agent-bus-production:\n"
        "    url: https://bus.example.test/mcp\n"
        "    tools:\n"
        "      include: [get_message]\n"
        "      resources: false\n"
        "      prompts: false\n",
        encoding="utf-8",
    )
    config = load_profile_config(profile_config)
    validate_readonly_profile_config(config, expected_url="https://bus.example.test/mcp")

    token_dir = tmp_path / "token-dir"
    token_dir.mkdir(mode=0o700)
    token_path = token_dir / "fixture.token"
    token_path.write_text("fixture-token-only", encoding="utf-8")
    token_path.chmod(0o600)
    assert read_token(token_path) == "fixture-token-only"
    assert token_path.read_text(encoding="utf-8") == "fixture-token-only"


def test_argv_is_fixed_profile_scoped_shell_free_and_uses_only_bus_toolset():
    argv = build_hermes_argv(
        executable="/fake/hermes", message_id=str(uuid.uuid4()), profile=PROFILE,
        profile_root="/profiles/mcp-worker", budget_seconds=90, max_turns=2,
    )
    assert argv[0] == "/fake/hermes"
    assert argv[1:3] == ["--profile", PROFILE]
    assert "--toolsets" in argv and argv[argv.index("--toolsets") + 1] == "mcp-agent-bus-production"
    assert "--format" in argv and argv[argv.index("--format") + 1] == "stream-json"
    assert "--resume" not in argv
    assert "--create-if-missing" not in argv and "--yolo" not in argv and "latest" not in argv
    assert "UNTRUSTED BODY" not in " ".join(argv)
    assert "--ignore-rules" in argv


def test_coordination_argv_is_fixed_uuid_and_never_adds_toolsets():
    message_id = str(uuid.uuid4())
    argv = build_hermes_argv(
        executable="/fake/hermes", message_id=message_id, profile=PROFILE,
        profile_root="/isolated/hermes/profiles/mcp-worker", budget_seconds=90,
        max_turns=2, mode="coordination",
    )
    assert argv[argv.index("--profile") + 1] == PROFILE
    assert argv[argv.index("--toolsets") + 1] == "mcp-agent-bus-production"
    assert argv.count("--toolsets") == 1
    prompt = argv[argv.index("--query") + 1]
    assert message_id in prompt
    assert "get_message(message_id)" in prompt
    assert "claim" in prompt and "acknowledge" in prompt and "REPLY" in prompt
    assert "in_reply_to" in prompt and "owner approval" in prompt
    assert "bus_info" in prompt
    assert "no operational action was taken" in prompt
    assert "deployment" in prompt and "mutation" in prompt


def test_private_http_and_coordination_are_explicit_cli_options_only():
    common = ["--url", "https://bus.example.test/mcp", "--agent", "hermes",
              "--token-file", "token", "--state-dir", "state"]
    default = _parse_args(common)
    assert default.allow_private_http is False
    assert default.coordination is False
    opted_in = _parse_args([*common, "--allow-private-http", "--coordination"])
    assert opted_in.allow_private_http is True
    assert opted_in.coordination is True
    with pytest.raises(SystemExit):
        _parse_args([*common, "--allow-loopback-http"])


def test_coordination_profile_mismatch_fails_before_child_launch(tmp_path):
    hermes_root = tmp_path / "isolated-hermes"
    profile_root = hermes_root / "profiles" / PROFILE
    profile_root.mkdir(parents=True)
    (profile_root / "config.yaml").write_text(
        "mcp_servers:\n"
        "  agent-bus-production:\n"
        "    url: https://bus.example.test/mcp\n"
        "    tools:\n"
        "      include: [get_message]\n"
        "      resources: false\n"
        "      prompts: false\n",
        encoding="utf-8",
    )
    marker = tmp_path / "child-was-launched"
    fake = tmp_path / "fake-hermes"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    cli_args = [
        "--url", "https://bus.example.test/mcp", "--agent", "hermes",
        "--token-file", str(tmp_path / "missing-token"),
        "--state-dir", str(tmp_path / "state"), "--hermes-root", str(hermes_root),
        "--executable", str(fake), "--allow-sender", "codex", "--allow-topic", "qa",
        "--coordination",
    ]
    # main() owns its asyncio.run loop like a fresh CLI process. Keep that
    # ownership off pytest-asyncio's main-thread cleanup loop.
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(main, cli_args).result(timeout=10)
    assert result == 2
    assert not marker.exists()


@pytest.mark.asyncio
async def test_fake_cli_coordination_launch_is_shell_free_profile_and_toolset_bounded(tmp_path, monkeypatch):
    async def scenario():
        hermes_root = tmp_path / "isolated-hermes"
        profile_root = hermes_root / "profiles" / PROFILE
        profile_root.mkdir(parents=True)
        endpoint = "http://192.168.50.80:8765/mcp"
        config = {
            "mcp_servers": {
                "agent-bus-production": {
                    "url": endpoint,
                    "tools": {
                        "include": ["get_message", "claim_message", "ack_message", "send_message", "bus_info"],
                        "resources": False,
                        "prompts": False,
                    },
                },
            },
        }
        validate_readonly_profile_config(config, expected_url=endpoint, mode="coordination")
        fake = tmp_path / "fake-hermes"
        observed = tmp_path / "child-arguments.json"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            f"pathlib.Path({str(observed)!r}).write_text(json.dumps({{'argv': sys.argv[1:], 'hermes_home': os.environ.get('HERMES_HOME')}}))\n"
            "sid='20261015_212152_d7d4c9'\n"
            "print(json.dumps({'type':'system','subtype':'init','session_id':sid}))\n"
            "print(json.dumps({'type':'result','session_id':sid,'exit_code':0}))\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

        actual_exec = asyncio.create_subprocess_exec
        calls = []

        async def capture_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return await actual_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_exec)
        runner = HermesRunner(
            executable=str(fake), profile=PROFILE, profile_root=profile_root,
            budget_seconds=5, max_turns=1, mode="coordination",
        )
        message_id = str(uuid.uuid4())
        outcome = await runner.run(message_id)
        assert outcome.outcome == "succeeded"
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert "shell" not in kwargs
        assert args[args.index("--profile") + 1] == PROFILE
        assert args[args.index("--toolsets") + 1] == "mcp-agent-bus-production"
        assert args.count("--toolsets") == 1
        assert kwargs["env"]["HERMES_HOME"] == str(hermes_root)
        assert kwargs["cwd"] == str(profile_root)
        seen = json.loads(observed.read_text(encoding="utf-8"))
        assert seen["hermes_home"] == str(hermes_root)
        assert seen["argv"] == list(args[1:])
        assert message_id in seen["argv"][seen["argv"].index("--query") + 1]

    await scenario()


@pytest.mark.asyncio
async def test_real_fake_executable_session_json_and_nonzero_are_fail_closed(tmp_path, caplog):
    async def scenario():
        fake = tmp_path / "fake-hermes"
        config = tmp_path / "fake-hermes.json"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "c=json.loads(pathlib.Path(sys.argv[0] + '.json').read_text())\n"
            "resume=sys.argv[sys.argv.index('--resume')+1] if '--resume' in sys.argv else None\n"
            "sid=c.get('force_session_id') or resume or c.get('session_id','20261015_212152_d7d4c9')\n"
            "print(json.dumps({'type':'system','subtype':'init','session_id':sid}))\n"
            "print(json.dumps({'type':'text','text':c.get('secret_output','')}))\n"
            "print(c.get('secret_stderr',''), file=sys.stderr)\n"
            "print(json.dumps({'type':'result','session_id':sid,'exit_code':c.get('exit_code',0)}))\n"
            "raise SystemExit(c.get('process_exit',0))\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        config.write_text(json.dumps({"session_id": SESSION, "secret_output": "FIXTURE_SECRET_OUTPUT", "secret_stderr": "FIXTURE_SECRET_STDERR"}), encoding="utf-8")
        profile_root = tmp_path / "profiles" / PROFILE
        profile_root.mkdir(parents=True)
        runner = HermesRunner(executable=str(fake), profile=PROFILE, profile_root=profile_root,
                              budget_seconds=5, max_turns=1)
        first = await runner.run(str(uuid.uuid4()))
        assert first.outcome == "succeeded" and first.session_id == SESSION
        assert "FIXTURE_SECRET_OUTPUT" not in caplog.text
        assert "FIXTURE_SECRET_STDERR" not in caplog.text
        resumed = await runner.run(str(uuid.uuid4()), resume_session_id=SESSION)
        assert resumed.outcome == "succeeded" and resumed.session_id == SESSION
        config.write_text(json.dumps({"force_session_id": "20261015_212153_abcdef"}), encoding="utf-8")
        unavailable = await runner.run(str(uuid.uuid4()), resume_session_id=SESSION)
        assert unavailable.outcome == "uncertain" and unavailable.session_id is None
        config.write_text(json.dumps({"session_id": SESSION, "exit_code": 1, "process_exit": 1}), encoding="utf-8")
        uncertain = await runner.run(str(uuid.uuid4()))
        assert uncertain.outcome == "uncertain"

    await scenario()


@pytest.mark.asyncio
async def test_notification_callback_only_sets_event_for_authenticated_inbox_uri():
    async def scenario():
        worker = PushWorker.__new__(PushWorker)
        worker.identity = "hermes"
        worker.notification_event = asyncio.Event()
        await worker.on_mcp_message(None)  # malformed input is ignored
        assert not worker.notification_event.is_set()

        event = types.ResourceUpdatedNotification.model_validate({
            "method": "notifications/resources/updated",
            "params": {"uri": "agentbus://inbox/codex"},
        })
        wrong_recipient = types.ServerNotification(root=event)
        await worker.on_mcp_message(wrong_recipient)
        assert not worker.notification_event.is_set()

        event = types.ResourceUpdatedNotification.model_validate({
            "method": "notifications/resources/updated",
            "params": {"uri": "agentbus://inbox/hermes"},
        })
        await worker.on_mcp_message(types.ServerNotification(root=event))
        assert worker.notification_event.is_set()

    await scenario()


@pytest.mark.asyncio
async def test_initial_head_snapshot_is_one_nonblocking_read_only_call(tmp_path):
    class SnapshotSession:
        calls = 0

        async def call_tool(self, name, arguments):
            self.calls += 1
            assert name == "wait_for_messages"
            assert arguments == {"since_now": True, "timeout": 0, "fields": "pointer", "limit": 100}
            return SimpleNamespace(isError=False, structuredContent={"messages": [], "next_cursor": 12})

    async def scenario():
        worker = PushWorker.__new__(PushWorker)
        worker.state = WorkerState(tmp_path / "state.sqlite3")
        session = SnapshotSession()
        await worker._bootstrap_cursor(session)
        await worker._bootstrap_cursor(session)
        assert worker.state.cursor == 12
        assert session.calls == 1

    await scenario()


@pytest.mark.asyncio
async def test_queue_arrives_while_worker_is_busy_and_survives_shutdown(tmp_path):
    class BlockingRunner:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, _message_id, resume_session_id=None):
            self.entered.set()
            await self.release.wait()
            return RunOutcome("succeeded", SESSION)

    class PageSession:
        def __init__(self, item):
            self.item = item

        async def read_resource(self, _uri):
            return SimpleNamespace(contents=[SimpleNamespace(text=json.dumps({
                "messages": [self.item], "next_cursor": self.item["seq"],
            }))])

    async def scenario():
        state = WorkerState(tmp_path / "state.sqlite3")
        state.set_initial_cursor(0)
        first = message(1, correlation_id="shared")
        state.ingest_page([first], 1, identity="hermes", allowed_senders={"codex"},
                          allowed_topics={"qa"}, allowed_hubs=set())
        runner = BlockingRunner()
        worker = PushWorker(url="https://bus.example.test/mcp", token="fixture-only", identity="hermes",
                            state=state, runner=runner, allowed_senders={"codex"},
                            allowed_topics={"qa"}, allowed_hubs=set())
        dispatcher = asyncio.create_task(worker._dispatch_queue())
        await asyncio.wait_for(runner.entered.wait(), timeout=1)

        second = message(2, correlation_id="shared")
        await worker._read_catchup(PageSession(second))
        assert state.queued_ids() == [second["id"]]
        assert state.cursor == 2

        worker.stop_event.set()
        runner.release.set()
        await asyncio.wait_for(dispatcher, timeout=1)
        assert state.queued_ids() == [second["id"]]

    await scenario()


@pytest.mark.asyncio
async def test_empty_queue_never_calls_model_runner(tmp_path):
    class CountingRunner:
        calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return RunOutcome("succeeded", SESSION)

    async def scenario():
        runner = CountingRunner()
        worker = PushWorker(url="https://bus.example.test/mcp", token="fixture-only", identity="hermes",
                            state=WorkerState(tmp_path / "state.sqlite3"), runner=runner,
                            allowed_senders={"codex"}, allowed_topics={"qa"}, allowed_hubs=set())
        dispatcher = asyncio.create_task(worker._dispatch_queue())
        await asyncio.sleep(0.02)
        assert runner.calls == 0
        worker.stop_event.set()
        worker.queue_event.set()
        await asyncio.wait_for(dispatcher, timeout=1)

    await scenario()


@pytest.mark.asyncio
async def test_only_network_failures_reconnect_with_backoff(tmp_path):
    class ReconnectingWorker(PushWorker):
        calls = 0

        async def run_connection(self):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("fixture network failure")
            self.stop_event.set()

    async def scenario():
        worker = ReconnectingWorker(url="https://bus.example.test/mcp", token="fixture-only", identity="hermes",
                                    state=WorkerState(tmp_path / "network-state.sqlite3"),
                                    runner=object(), allowed_senders={"codex"},
                                    allowed_topics={"qa"}, allowed_hubs=set(),
                                    reconnect_base_seconds=0.01)
        await worker.run_forever()
        assert worker.calls == 2

        class PermanentFailureWorker(ReconnectingWorker):
            async def run_connection(self):
                self.calls += 1
                raise CapabilityError("missing native subscribe")

        permanent = PermanentFailureWorker(url="https://bus.example.test/mcp", token="fixture-only",
                                           identity="hermes", state=worker.state, runner=object(),
                                           allowed_senders={"codex"}, allowed_topics={"qa"}, allowed_hubs=set())
        with pytest.raises(CapabilityError):
            await permanent.run_forever()
        assert permanent.calls == 1

    await scenario()


@pytest.mark.asyncio
async def test_connector_uses_real_streamable_http_push_without_replaying_old_work(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    app = create_app(db_path=tmp_path / "bus.sqlite3", audit_path=tmp_path / "audit.jsonl",
                     auth=TEST_AUTH, push_events_operational=True)
    repository = app.state.repository

    class CountingRunner:
        def __init__(self):
            self.calls = []
            self.called = asyncio.Event()

        async def run(self, message_id, resume_session_id=None):
            self.calls.append((message_id, resume_session_id))
            self.called.set()
            return RunOutcome("succeeded", SESSION)

    class ReadyWorker(PushWorker):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.snapshot_done = asyncio.Event()
            self.initial_catchup_done = asyncio.Event()

        async def _bootstrap_cursor(self, session):
            await super()._bootstrap_cursor(session)
            self.snapshot_done.set()

        async def _read_catchup(self, session):
            await super()._read_catchup(session)
            self.initial_catchup_done.set()

    runner = CountingRunner()
    state = WorkerState(tmp_path / "worker-state.sqlite3")
    worker = ReadyWorker(url="http://127.0.0.1:8765/mcp", token=TEST_AUTH.hermes_token,
                         identity="hermes", state=state, runner=runner,
                         allowed_senders={"codex"}, allowed_topics={"qa"}, allowed_hubs=set(),
                         allow_loopback_http=True)

    async with _loopback_mcp(app) as url:
        old = repository.insert(sender="codex", recipient="hermes", message_type="TASK",
                                body={"topic": "qa", "description": "old pending fixture"},
                                correlation_id="old-job")
        worker.url = url
        task = asyncio.create_task(worker.run_connection())
        try:
            await asyncio.wait_for(worker.snapshot_done.wait(), timeout=5)
            await asyncio.wait_for(worker.initial_catchup_done.wait(), timeout=5)
            assert state.cursor == repository.sequence_for(old.id)
            assert runner.calls == []  # initial head snapshot did not replay retained work

            incoming = repository.insert(sender="codex", recipient="hermes", message_type="REQUEST",
                                         body={"topic": "qa", "description": "fixture delivery"},
                                         correlation_id="new-job")
            # The actual native commit source must deliver the invalidation;
            # an extra manual publish races that source and its dirty edges.
            await asyncio.wait_for(runner.called.wait(), timeout=5)
            assert app.state.push_publisher.sent_notifications >= 1
            await asyncio.sleep(0)
        finally:
            # End the SDK session in its owning worker task before the server
            # context exits, including when an assertion/readiness wait fails.
            worker.stop_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            except BaseException:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise

    assert runner.calls == [(incoming.id, None)]
    assert state.cursor == repository.sequence_for(incoming.id)
    assert state.item_status(incoming.id) == "succeeded"
    assert state.item_status(old.id) is None


@pytest.mark.asyncio
async def test_resource_source_failure_log_is_terminal_despite_sse_keepalive(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOWED_HOSTS", "127.0.0.1:*")
    app = create_app(db_path=tmp_path / "failure-bus.sqlite3",
                     audit_path=tmp_path / "failure-audit.jsonl",
                     auth=TEST_AUTH, push_events_operational=True)
    original_client = httpx.AsyncClient
    observed_timeouts = []

    class RecordingClient:
        def __init__(self, *args, **kwargs):
            observed_timeouts.append(kwargs["timeout"])
            self.client = original_client(*args, **kwargs)

        async def __aenter__(self):
            return await self.client.__aenter__()

        async def __aexit__(self, *args):
            return await self.client.__aexit__(*args)

    monkeypatch.setattr("scripts.mcp_push_worker.httpx.AsyncClient", RecordingClient)

    class NoopRunner:
        calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return RunOutcome("succeeded", SESSION)

    class ReadyWorker(PushWorker):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.snapshot_done = asyncio.Event()
            self.connections = 0

        async def _bootstrap_cursor(self, session):
            await super()._bootstrap_cursor(session)
            self.snapshot_done.set()

        async def run_connection(self):
            self.connections += 1
            await super().run_connection()

    runner = NoopRunner()
    worker = ReadyWorker(
        url="http://127.0.0.1:8765/mcp", token=TEST_AUTH.hermes_token,
        identity="hermes", state=WorkerState(tmp_path / "failure-state.sqlite3"),
        runner=runner, allowed_senders={"codex"}, allowed_topics={"qa"},
        allowed_hubs=set(), allow_loopback_http=True, reconnect_base_seconds=0.01,
    )
    async with _loopback_mcp(app) as url:
        worker.url = url
        task = asyncio.create_task(worker.run_forever())
        try:
            await asyncio.wait_for(worker.snapshot_done.wait(), timeout=5)
            assert observed_timeouts and observed_timeouts[0].read is not None
            assert 45 <= observed_timeouts[0].read <= 60
            app.state.push_publisher.fail_source("fixture event source failure")
            with pytest.raises(CapabilityError):
                await asyncio.wait_for(task, timeout=3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    assert worker.connections == 1
    assert runner.calls == 0
