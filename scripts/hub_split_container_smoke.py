"""Offline packaged split-listener smoke with synthetic identities only.

No build, pull, publication, host mounts/ports, live identity, or TrueNAS access.
Recreates one disposable image process over one owned named volume, not an
autonomous pilot, real Cloudflare login, host reboot or storage acceptance.
"""
import argparse
import json
import re
import uuid

from hub_release_smoke import owned_exists, remove_owned, run


PROBE = r'''
import json, os, sys, time, urllib.error, urllib.request
from pathlib import Path
assert os.getuid() == 568 and os.getgid() == 568
expected_commit = sys.argv[1]
expected_host = 'hub.ledgetoptechnologies.com'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 90

def request(port, path, *, headers=None, payload=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Split smoke deadline expired')
    values = {'Host': expected_host, **(headers or {})}
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request('http://127.0.0.1:' + str(port) + path, headers=values, data=data)
    try:
        response = opener.open(req, timeout=min(2, remaining))
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read(1024 * 1024 + 1)
        assert len(body) <= 1024 * 1024, 'Oversized split smoke response'
        return response.status, body.decode()

for port in (8770, 8771):
    ready_by = time.monotonic() + 20
    while time.monotonic() < ready_by:
        try:
            status, body = request(port, '/health')
            health = json.loads(body)
            assert status == 200 and health['ok'] is True
            assert health['build_commit'] == expected_commit
            break
        except (OSError, AssertionError):
            time.sleep(.2)
    else:
        raise RuntimeError('Split listener did not become ready')

assert request(8770, '/health') == request(8771, '/health')
tokens = {agent: os.environ[agent.upper() + '_TOKEN'] for agent in ('hermes', 'codex', 'claude')}
valid = {'Authorization': 'Bearer ' + tokens['codex']}
spoof = {**valid, 'Host': expected_host, 'X-Forwarded-Host': expected_host,
         'X-Forwarded-For': '127.0.0.1', 'CF-Access-Jwt-Assertion': 'forged',
         'CF-Access-Authenticated-User-Email': 'beau@example.invalid'}
for path in ('/', '/index.html', '/hub.svg', '/assets/anything.js', '/api/hub/v1/auth/login',
             '/api/hub/v1/auth/me', '/docs', '/openapi.json'):
    assert request(8770, path, headers=spoof)[0] == 403, 'Agent frontend exposed browser route'
for token in (None, 'synthetic-invalid-token'):
    headers = {} if token is None else {'Authorization': 'Bearer ' + token}
    assert request(8770, '/api/hub/v1/overview', headers=headers)[0] == 401

expected_tools = {'hub_overview', 'hub_task_get', 'hub_task_claim', 'hub_task_candidate',
                  'hub_task_review', 'hub_decision_request', 'hub_run_list',
                  'hub_topic_peers', 'hub_topic_preflight', 'hub_staging_task_propose',
                  'hub_planning_list', 'hub_planning_get', 'hub_planning_put',
                  'hub_label_set_create', 'hub_label_set_put_image', 'hub_label_set_results',
                  'hub_label_set_list', 'hub_entity_put_image', 'hub_entity_images',
                  'hub_project_list', 'hub_project_get'}
def rpc(method, params, token):
    headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json',
               'MCP-Protocol-Version': '2025-06-18'}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    return request(8770, '/mcp/', headers=headers,
                   payload={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})

for bad in (None, 'synthetic-invalid-token'):
    assert rpc('tools/list', {}, bad)[0] == 401
for agent, token in tokens.items():
    assert request(8770, '/api/hub/v1/overview', headers={'Authorization': 'Bearer ' + token})[0] == 200
    status, body = rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
        'clientInfo': {'name': 'split-image-smoke', 'version': '1'}}, token)
    assert status == 200 and 'result' in json.loads(body)
    status, body = rpc('tools/list', {}, token)
    assert status == 200
    tools = json.loads(body)['result']['tools']
    assert len(tools) == 21 and {tool['name'] for tool in tools} == expected_tools

for path in ('/', '/index.html', '/hub.svg', '/assets/anything.js', '/api/hub/v1/auth/login',
             '/api/hub/v1/auth/me', '/api/hub/v1/overview'):
    for headers in ({}, {'CF-Access-Jwt-Assertion': 'forged'},
                    {'CF-Access-Authenticated-User-Email': 'beau@example.invalid'}):
        assert request(8771, path, headers=headers)[0] == 401, 'Browser route did not require valid Access'
assert request(8771, '/mcp/')[0] == 403
assert request(8771, '/', headers=valid)[0] == 403
assert request(8771, '/api/hub/v1/overview', headers=valid)[0] == 403

# Actual kernel socket state, not command-line intent or an advertised URL.
listeners = {}
for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
    fields = line.split()
    if fields[3] == '0A':
        address, port = fields[1].split(':')
        listeners.setdefault(int(port, 16), set()).add(address)
assert listeners.get(8770) == {'00000000'}, 'Agent socket binding mismatch'
assert listeners.get(8771) == {'0100007F'}, 'Browser socket is not IPv4 loopback only'
for line in Path('/proc/net/tcp6').read_text().splitlines()[1:]:
    fields = line.split()
    assert not (fields[3] == '0A' and int(fields[1].split(':')[1], 16) in (8770, 8771)), 'Unexpected IPv6 Hub listener'
print(json.dumps({'split_image_smoke': 'passed', 'build_commit': expected_commit,
                  'authenticated_agents': 3, 'mcp_tools': 21,
                  'agent_bind': '0.0.0.0:8770', 'browser_bind': '127.0.0.1:8771',
                  'human_sign_in': 'not_tested', 'network': 'none'}))
'''


SEED_RECORDS = r'''
import json, os
from pathlib import Path
from engineering_hub.repository import HubRepository
# Isolated test-data setup only: never manufactures a browser cookie, changes
# an auth guard, enrolls a pilot owner, or writes to an operator database.
database = Path(os.environ['HUB_DB'])
fixture = database.parent / '.split-records.json'
fd = os.open(fixture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    repo = HubRepository(database)
    task = repo.create_task('human:123', 'treedar', 'Isolated durable split task', 'split-durable-fixture')
    json.dump({'schema': 1, 'task': task, 'decision': None}, stream, separators=(',', ':'))
    stream.flush()
    os.fsync(stream.fileno())
'''


DURABILITY_PROBE = r'''
import json, os, sys, time, urllib.error, urllib.request
from pathlib import Path
from uuid import UUID
phase = int(sys.argv[1])
assert phase in (0, 1)
fixture = Path(os.environ['HUB_DB']).parent / '.split-records.json'
assert fixture.stat().st_mode & 0o777 == 0o600, 'Fixture record permissions mismatch'
state = json.loads(fixture.read_text())
assert state['schema'] == 1
assert str(UUID(state['task']['id'])) == state['task']['id']
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 30

def api(agent, path, payload=None):
    remaining = deadline - time.monotonic()
    assert remaining > 0, 'Durability request deadline expired'
    headers = {'Host': 'hub.ledgetoptechnologies.com', 'Content-Type': 'application/json',
               'Authorization': 'Bearer ' + os.environ[agent.upper() + '_TOKEN']}
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request('http://127.0.0.1:8770/api/hub/v1' + path,
                                     headers=headers, data=body)
    with opener.open(request, timeout=min(2, remaining)) as response:
        assert response.status == (201 if path == '/decisions' and body is not None else 200)
        data = response.read(1024 * 1024 + 1)
        assert len(data) <= 1024 * 1024, 'Oversized durability response'
        return json.loads(data)

if phase == 0:
    assert state['task']['revision'] == 1 and state['task']['status'] == 'assigned'
    assert state['decision'] is None, 'Durability setup must not replay existing work'
    claimed = api('codex', '/tasks/' + state['task']['id'] + '/claim', {'expected_revision': 1})
    assert claimed['id'] == state['task']['id'] and claimed['revision'] == 2
    assert claimed['owner'] == 'codex' and claimed['status'] == 'claimed'
    decision = api('hermes', '/decisions', {'project': 'treedar', 'title': 'Isolated durable split decision',
                                          'options': ['hold', 'review'], 'protected': False})
    assert str(UUID(decision['id'])) == decision['id']
    assert decision['revision'] == 1 and decision['status'] == 'open'
    assert decision['execution_authorization'] is False
    state.update(task=claimed, decision=decision)
    with fixture.open('w') as stream:
        json.dump(state, stream, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())

assert state['task']['title'] == 'Isolated durable split task'
assert state['task']['project'] == 'treedar' and state['task']['revision'] == 2
assert state['task']['owner'] == 'codex' and state['task']['status'] == 'claimed'
assert state['decision']['title'] == 'Isolated durable split decision'
assert state['decision']['project'] == 'treedar' and state['decision']['revision'] == 1
assert state['decision']['status'] == 'open' and state['decision']['answer'] is None
assert state['decision']['options'] == ['hold', 'review'] and state['decision']['protected'] is False
assert state['decision']['execution_authorization'] is False
for agent in ('hermes', 'codex', 'claude'):
    assert api(agent, '/tasks/' + state['task']['id']) == state['task'], 'Durable task mismatch'
    decisions = api(agent, '/decisions')['items']
    matches = [item for item in decisions if item['id'] == state['decision']['id']]
    assert matches == [state['decision']], 'Durable decision mismatch'
print(json.dumps({'durable_records_verified': True, 'authenticated_readers': 3,
                  'durable_task_decision_recreation': phase == 1}))
'''


def _validate(image, commit):
    if (not isinstance(image, str) or not re.fullmatch(
            r"ghcr\.io/ledgetoptechnologies/engineering-hub-control:(?:build-)[a-z0-9-]+|"
            r"ghcr\.io/ledgetoptechnologies/engineering-hub-control@sha256:[a-f0-9]{64}", image)):
        raise ValueError("Expected the explicit Hub release artifact")
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Expected exact source commit")


def _verify_profile(name, volume):
    profile = json.loads(run("docker", "container", "inspect", "--format",
        '{"user":{{json .Config.User}},"host":{{json .HostConfig}},"mounts":{{json .Mounts}}}', name))
    host = profile["host"]
    assert profile["user"] == "568:568"
    assert host["NetworkMode"] == "none" and host["CgroupnsMode"] == "private"
    assert host["ReadonlyRootfs"] is True and host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"] and "no-new-privileges:true" in host["SecurityOpt"]
    assert host["Memory"] == host["MemorySwap"] == 512 * 1024 * 1024
    assert host["NanoCpus"] == 1_000_000_000 and host["PidsLimit"] == 128
    assert not host.get("PortBindings") and not host.get("Devices") and not host.get("Binds")
    assert set(host["Tmpfs"]) == {"/tmp"}
    mounts = profile["mounts"]
    assert len(mounts) == 1 and mounts[0]["Type"] == "volume"
    assert mounts[0]["Name"] == volume and mounts[0]["Destination"] == "/var/lib/hub"


def smoke(image, commit):
    _validate(image, commit)
    owner = uuid.uuid4().hex
    volume, names = "hub-split-state-" + owner, []
    try:
        run("docker", "volume", "create", "--label", "hub.release-owner=" + owner, volume)
        if not owned_exists("volume", volume, owner):
            raise RuntimeError("Owned split smoke volume not found")
        for iteration in range(2):
            name = "hub-split-smoke-" + owner + "-" + str(iteration)
            names.append(name)  # Reconcile even if Docker creates then disconnects.
            run("docker", "run", "-d", "--pull", "never", "--name", name,
                "--label", "hub.release-owner=" + owner, "--network", "none",
                "--user", "568:568", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--cgroupns", "private",
                "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "128",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
                "--mount", "type=volume,source=" + volume + ",target=/var/lib/hub",
                "-e", "HUB_BASE_URL=https://hub.ledgetoptechnologies.com",
                "-e", "HERMES_TOKEN=synthetic-split-hermes-only",
                "-e", "CODEX_TOKEN=synthetic-split-codex-only",
                "-e", "CLAUDE_TOKEN=synthetic-split-claude-only",
                "-e", "AGENT_BUS_ALLOWED_IPS=127.0.0.1",
                "-e", "HUB_AGENT_ALLOWED_HOSTS=127.0.0.1:8770",
                "-e", "HUB_HUMAN_AUTH_MODE=cloudflare_access",
                "-e", "HUB_CF_ACCESS_ISSUER=https://split-fixture.cloudflareaccess.com",
                "-e", "HUB_CF_ACCESS_AUDIENCE=synthetic-split-audience",
                "-e", "HUB_CF_ACCESS_ALLOWED_EMAILS=beau@example.invalid",
                image, "--split-ingress", "--browser-port", "8771")
            if not owned_exists("container", name, owner):
                raise RuntimeError("Owned split smoke container not found")
            _verify_profile(name, volume)
            report = json.loads(run("docker", "exec", name, "python", "-c", PROBE, commit, timeout=110))
            assert report["split_image_smoke"] == "passed" and report["build_commit"] == commit
            assert report["authenticated_agents"] == 3 and report["mcp_tools"] == 21
            assert report["agent_bind"] == "0.0.0.0:8770" and report["browser_bind"] == "127.0.0.1:8771"
            if iteration == 0:
                run("docker", "exec", name, "python", "-c", SEED_RECORDS)
            durable = json.loads(run("docker", "exec", name, "python", "-c", DURABILITY_PROBE,
                                     str(iteration), timeout=45))
            assert durable["durable_records_verified"] is True and durable["authenticated_readers"] == 3
            assert durable["durable_task_decision_recreation"] is (iteration == 1)
            marker = "from pathlib import Path; p=Path('/var/lib/hub/.split-smoke'); "
            marker += ("p.write_text('isolated-split-fixture')" if iteration == 0 else
                       "assert p.read_text() == 'isolated-split-fixture'")
            run("docker", "exec", name, "python", "-c", marker)
            remove_owned("container", name, owner)
            names.remove(name)
        return {**report, "named_volume_recreation": "passed", "durable_task_decision_recreation": True,
                "truenas": "not_deployed",
                "registry_publication": "not_tested", "autonomous_pilot": "not_tested"}
    finally:
        # Reuse existing ownership-verifying reconciliation. If container state
        # is unknown, stop here instead of deleting its potentially live volume.
        for name in names:
            remove_owned("container", name, owner)
        remove_owned("volume", volume, owner)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    print(json.dumps(smoke(args.image, args.commit)))
