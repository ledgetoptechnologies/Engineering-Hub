"""Bounded offline smoke of an already-built Hub image; no live credentials.

Uses a unique disposable container, no network, no host mounts, no host ports.
This proves image packaging and auth-disabled startup, NOT durable storage,
GitHub sign-in, TrueNAS, reboot recovery, or a real staging deployment.
"""
import argparse
import json
import re
import subprocess
import uuid


PROBE = r'''
import json, os, re, time, urllib.request, urllib.error
base = 'http://127.0.0.1:8770'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 20
assert os.getuid() == 10001 and os.getgid() == 10001

def read(response):
    body = response.read(1024 * 1024 + 1)
    assert len(body) <= 1024 * 1024, 'Oversized smoke response'
    return response.status, {k.lower(): v for k, v in response.headers.items()}, body.decode()

def get(path):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Smoke deadline expired')
    try:
        with opener.open(base + path, timeout=min(1, remaining)) as r:
            return read(r)
    except urllib.error.HTTPError as r:
        with r:
            return read(r)
ready_by = time.monotonic() + 10
while time.monotonic() < ready_by:
    try:
        status, _, body = get('/health')
        assert status == 200 and json.loads(body).get('ok') is True
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
expected_tools = {'hub_overview', 'hub_task_get', 'hub_task_claim', 'hub_task_candidate',
                  'hub_task_review', 'hub_decision_request', 'hub_run_list',
                  'hub_topic_peers', 'hub_topic_preflight'}
def rpc(method, params, token):
    headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json',
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
                  'authenticated_agents': 3, 'mcp_tools': len(expected_tools)}))
'''


def run_smoke(image):
    if not isinstance(image, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise ValueError('An immutable local image ID is required')
    owner = uuid.uuid4().hex
    name = 'hub-dashboard-smoke-' + owner
    run = [
        'docker', 'run', '--detach', '--pull', 'never', '--name', name,
        '--label', 'engineering-hub.smoke-owner=' + owner, '--network', 'none',
        '--user', '10001:10001', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--pids-limit', '64', '--memory', '512m', '--cpus', '1',
        '--tmpfs', '/var/lib/hub:rw,noexec,nosuid,size=64m,uid=10001,gid=10001,mode=0700',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m',
        '--env', 'HERMES_TOKEN=fixture-only-hermes-not-a-service-token',
        '--env', 'CODEX_TOKEN=fixture-only-codex-not-a-service-token',
        '--env', 'CLAUDE_TOKEN=fixture-only-claude-not-a-service-token',
        '--env', 'HUB_BASE_URL=http://127.0.0.1:8770', image,
    ]
    # No shell, token retrieval, user configuration, or shared Docker resources.
    try:
        subprocess.run(run, check=True, capture_output=True, text=True, timeout=60)
        result = subprocess.run(['docker', 'exec', name, 'python', '-c', PROBE],
                                check=True, capture_output=True, text=True, timeout=30)
        report = json.loads(result.stdout)
        if not isinstance(report, dict) or report.get('image_smoke') != 'passed':
            raise ValueError('Smoke probe did not report success')
        return report
    finally:
        # docker run may have created the container even if its response was
        # interrupted. Reconcile by unique ownership label, never by a shared
        # name or a broad cleanup command. Inspect only public identity fields.
        identity = subprocess.run(['docker', 'container', 'inspect', '--format',
            '{{.Id}}|{{index .Config.Labels "engineering-hub.smoke-owner"}}', name],
            capture_output=True, text=True, timeout=15)
        if identity.returncode == 0:
            container_id, _, actual_owner = identity.stdout.strip().partition('|')
            if not re.fullmatch(r'[a-f0-9]{64}', container_id) or actual_owner != owner:
                raise RuntimeError('Smoke ownership could not be verified; container not removed')
            subprocess.run(['docker', 'rm', '--force', container_id], check=True,
                           capture_output=True, text=True, timeout=30)
        elif 'No such container' not in identity.stderr and 'No such object' not in identity.stderr:
            raise RuntimeError('Smoke container cleanup could not be verified')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Already-built immutable local sha256 image ID')
    args = parser.parse_args()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', args.image):
        parser.error('An immutable local sha256 image ID is required')
    print(json.dumps(run_smoke(args.image)))


if __name__ == '__main__':
    main()
