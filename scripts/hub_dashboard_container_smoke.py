"""Bounded offline smoke of an already-built Hub image; no live credentials.

Uses a unique disposable container, no network, no host mounts, no host ports.
This proves image packaging and auth-disabled startup, NOT durable storage,
GitHub sign-in, TrueNAS, reboot recovery, or a real staging deployment.
"""
import argparse
import json
from os import stat as endpoint_stat
from os.path import realpath as endpoint_realpath
import re
import stat
import shutil
import tempfile
import subprocess
import uuid


PROBE = r'''
import json, os, re, sys, time, urllib.request, urllib.error
from urllib.parse import urlsplit
base = 'http://127.0.0.1:8770'
expected_host = urlsplit(os.environ['HUB_BASE_URL']).netloc
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 20
assert os.getuid() == 568 and os.getgid() == 568
expected_commit = sys.argv[1] if len(sys.argv) == 2 else None

def read(response):
    body = response.read(1024 * 1024 + 1)
    assert len(body) <= 1024 * 1024, 'Oversized smoke response'
    return response.status, {k.lower(): v for k, v in response.headers.items()}, body.decode()

def get(path):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Smoke deadline expired')
    try:
        request = urllib.request.Request(base + path, headers={'Host': expected_host})
        with opener.open(request, timeout=min(1, remaining)) as r:
            return read(r)
    except urllib.error.HTTPError as r:
        with r:
            return read(r)
ready_by = time.monotonic() + 10
while time.monotonic() < ready_by:
    try:
        status, _, body = get('/health')
        health = json.loads(body)
        assert status == 200 and health.get('ok') is True
        actual_commit = health.get('build_commit')
        assert actual_commit is None or isinstance(actual_commit, str) and re.fullmatch('[0-9a-f]{40}', actual_commit)
        if expected_commit is not None:
            assert actual_commit == expected_commit
        break
    except (OSError, AssertionError):
        time.sleep(.2)
else:
    raise RuntimeError('Hub did not become ready')
status, headers, html = get('/')
assert status == 200 and '<title>Engineering Hub</title>' in html
assert headers.get('cache-control') == 'no-store'
assert "script-src 'self'" in headers.get('content-security-policy', '')
assets = re.findall(r'(?:src|href)="(/assets/[^"<>]+)"', html)
assert 2 <= len(assets) <= 16
assert any(asset.endswith('.js') for asset in assets) and any(asset.endswith('.css') for asset in assets)
for asset in assets:
    status, asset_headers, body = get(asset)
    assert status == 200 and body and '<html' not in body[:100].lower()
    expected = 'javascript' if asset.endswith('.js') else 'css'
    assert expected in asset_headers.get('content-type', '')
assert get('/hub.svg')[0] == 200
status, _, body = get('/api/hub/v1/auth/me')
assert status == 200 and json.loads(body)['authenticated'] is False
assert get('/api/hub/v1/auth/login')[0] == 503
for path in ['/overview', '/tasks', '/decisions', '/runs', '/events', '/topics/topic/peers']:
    assert get('/api/hub/v1' + path)[0] == 401, path
assert get('/not-an-app-route')[0] == 404
expected_tools = {'hub_overview', 'hub_project_tasks', 'hub_tracking_task_propose', 'hub_tracking_task_origin', 'hub_orchestration_request_tasks',
                  'hub_tracking_task_update', 'hub_tracking_task_list', 'hub_tracking_task_review', 'hub_role_list', 'hub_role_get', 'hub_role_acquire', 'hub_role_renew', 'hub_role_release', 'hub_role_claim', 'hub_role_claims', 'hub_role_claim_release', 'hub_task_progress_get', 'hub_task_progress_update', 'hub_orchestration_request_create', 'hub_orchestration_request_get', 'hub_orchestration_request_list', 'hub_dispatch_create', 'hub_dispatch_get', 'hub_dispatch_list', 'hub_role_dispatch_inbox', 'hub_dispatch_report', 'hub_dispatch_checkpoint', 'hub_dispatch_decisions', 'hub_dispatch_reports', 'hub_orchestrator_attention_list', 'hub_orchestrator_attention_reconcile_closed', 'hub_addressed_attention_list', 'hub_addressed_attention_ack', 'hub_worker_dispatch_pending', 'hub_worker_dispatch_ack', 'hub_worker_decision_inspect', 'hub_orchestrator_report_inspect', 'hub_tracking_task_history', 'hub_tracking_task_compact',
                  'hub_task_get', 'hub_task_claim', 'hub_task_candidate',
                  'hub_task_review', 'hub_decision_request', 'hub_decision_get', 'hub_run_list',
                  'hub_topic_peers', 'hub_topic_preflight', 'hub_staging_task_propose',
                  'hub_planning_list', 'hub_planning_get', 'hub_planning_put',
                  'hub_label_set_create', 'hub_label_set_put_image', 'hub_label_set_results',
                  'hub_label_set_list', 'hub_entity_put_image', 'hub_entity_images', 'hub_entity_withdraw',
                  'hub_project_list', 'hub_project_get', 'hub_component_list', 'hub_component_get',
                  'hub_component_create', 'hub_component_update', 'hub_component_retire',
                  'hub_interactive_review_create', 'hub_interactive_review_get', 'hub_interactive_review_list'}
def rpc(method, params, token):
    headers = {'Host': expected_host, 'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json',
               'MCP-Protocol-Version': '2025-06-18'}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
    request = urllib.request.Request(base + '/mcp/', data=payload, headers=headers)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Smoke deadline expired')
    try:
        with opener.open(request, timeout=min(2, remaining)) as response:
            return read(response)
    except urllib.error.HTTPError as response:
        with response:
            return read(response)
for bad in (None, 'fixture-invalid-not-a-service-token'):
    assert rpc('tools/list', {}, bad)[0] == 401
for agent in ('HERMES', 'CODEX', 'CLAUDE'):
    token = os.environ[agent + '_TOKEN']
    status, _, body = rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
        'clientInfo': {'name': 'isolated-image-smoke', 'version': '1'}}, token)
    assert status == 200 and 'result' in json.loads(body)
    status, _, body = rpc('tools/list', {}, token)
    assert status == 200
    tools = json.loads(body)['result']['tools']
    assert len(tools) == len(expected_tools) and {tool['name'] for tool in tools} == expected_tools
print(json.dumps({'image_smoke': 'passed', 'assets': len(assets),
                  'human_sign_in': 'disabled_as_requested', 'private_api': 'denied',
                  'network': 'none', 'durability': 'not_tested',
                  'build_commit': actual_commit,
                  'authenticated_agents': 3, 'mcp_tools': len(expected_tools)}))
'''


def run_smoke(image, expected_commit=None, *, docker_host=None):
    if not isinstance(image, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise ValueError('An immutable local image ID is required')
    if expected_commit is not None and (not isinstance(expected_commit, str) or not re.fullmatch('[0-9a-f]{40}', expected_commit)):
        raise ValueError('Expected commit must be lowercase 40-character SHA')
    if not isinstance(docker_host, str) or not re.fullmatch(r'unix:///run/[A-Za-z0-9_./-]+\.sock', docker_host) or any(part in ('', '.', '..') for part in docker_host[8:].split('/')) or docker_host in ('unix:///run/docker.sock', 'unix:///run/containerd/containerd.sock'):
        raise ValueError('An explicit dedicated local Unix Docker socket is required')
    socket_path = docker_host[7:]
    if endpoint_realpath(socket_path) != socket_path:
        raise ValueError('Docker socket aliases are not allowed')
    socket_info = endpoint_stat(socket_path)
    if not stat.S_ISSOCK(socket_info.st_mode):
        raise ValueError('Docker endpoint must be an existing socket')
    for shared in ('/run/docker.sock', '/var/run/docker.sock'):
        try:
            shared_info = endpoint_stat(shared)
        except FileNotFoundError:
            continue
        if (shared_info.st_dev, shared_info.st_ino) == (socket_info.st_dev, socket_info.st_ino):
            raise ValueError('Shared system Docker socket is not allowed')
    executable = shutil.which('docker', path='/usr/bin:/bin')
    if executable is None:
        raise RuntimeError('System Docker CLI is unavailable')
    docker = [executable, '--host', docker_host]
    with tempfile.TemporaryDirectory(prefix='eh-smoke-docker-config-') as config_dir:
        child_env = {'PATH': '/usr/bin:/bin', 'DOCKER_CONFIG': config_dir}

        def invoke(argv, **kwargs):
            return subprocess.run(argv, env=child_env, **kwargs)

        owner = uuid.uuid4().hex
        name = 'hub-dashboard-smoke-' + owner
        run = [
            *docker, 'run', '--detach', '--pull', 'never', '--name', name,
            '--label', 'engineering-hub.smoke-owner=' + owner, '--network', 'none',
            '--user', '568:568', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--pids-limit', '64', '--memory', '512m', '--cpus', '1',
            '--tmpfs', '/var/lib/hub:rw,noexec,nosuid,size=64m,uid=568,gid=568,mode=0700',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m',
            '--env', 'HERMES_TOKEN=fixture-only-hermes-not-a-service-token',
            '--env', 'CODEX_TOKEN=fixture-only-codex-not-a-service-token',
            '--env', 'CLAUDE_TOKEN=fixture-only-claude-not-a-service-token',
            '--env', 'HUB_BASE_URL=http://127.0.0.1:8770', image,
        ]
        # No shell, token retrieval, user configuration, or shared Docker resources.
        try:
            invoke(run, check=True, capture_output=True, text=True, timeout=60)
            probe = [*docker, 'exec', name, 'python', '-c', PROBE]
            if expected_commit is not None:
                probe.append(expected_commit)
            result = invoke(probe,
                                    check=True, capture_output=True, text=True, timeout=30)
            report = json.loads(result.stdout)
            if not isinstance(report, dict) or report.get('image_smoke') != 'passed':
                raise ValueError('Smoke probe did not report success')
            if expected_commit is not None and report.get('build_commit') != expected_commit:
                raise ValueError('Smoke build identity did not match')
            return report
        finally:
            # docker run may have created the container even if its response was
            # interrupted. Reconcile by unique ownership label, never by a shared
            # name or a broad cleanup command. Inspect only public identity fields.
            identity = invoke([*docker, 'container', 'inspect', '--format',
                '{{.Id}}|{{index .Config.Labels "engineering-hub.smoke-owner"}}', name],
                capture_output=True, text=True, timeout=15)
            if identity.returncode == 0:
                container_id, _, actual_owner = identity.stdout.strip().partition('|')
                if not re.fullmatch(r'[a-f0-9]{64}', container_id) or actual_owner != owner:
                    raise RuntimeError('Smoke ownership could not be verified; container not removed')
                invoke([*docker, 'rm', '--force', container_id], check=True,
                               capture_output=True, text=True, timeout=30)
            elif 'No such container' not in identity.stderr and 'No such object' not in identity.stderr:
                raise RuntimeError('Smoke container cleanup could not be verified')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Already-built immutable local sha256 image ID')
    parser.add_argument('--docker-host', required=True, help='Verified dedicated local Unix Docker socket; never the shared system Engine')
    parser.add_argument('--expected-commit', help='Require the app health response to identify this exact source commit')
    args = parser.parse_args()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', args.image):
        parser.error('An immutable local sha256 image ID is required')
    print(json.dumps(run_smoke(args.image, args.expected_commit, docker_host=args.docker_host)))


if __name__ == '__main__':
    main()
