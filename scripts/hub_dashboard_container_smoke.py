"""Bounded offline smoke of an already-built Hub image; no live credentials.

Uses a unique disposable container, no network, no host mounts, no host ports.
This proves image packaging and auth-disabled startup, NOT durable storage,
GitHub sign-in, TrueNAS, reboot recovery, or a real staging deployment.
"""
import argparse
import json
import subprocess
import uuid


PROBE = r'''
import json, re, time, urllib.request, urllib.error
base = 'http://127.0.0.1:8770'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path):
    try:
        with opener.open(base + path, timeout=2) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as r:
        return r.code, dict(r.headers), r.read().decode()
for attempt in range(30):
    try:
        assert get('/health')[0] == 200
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
assert len(assets) >= 2
assert all(get(asset)[0] == 200 for asset in assets)
assert get('/hub.svg')[0] == 200
assert json.loads(get('/api/hub/v1/auth/me')[2])['authenticated'] is False
assert get('/api/hub/v1/auth/login')[0] == 503
for path in ['/overview', '/tasks', '/decisions', '/runs', '/events', '/topics/topic/peers']:
    assert get('/api/hub/v1' + path)[0] == 401, path
assert get('/not-an-app-route')[0] == 404
print(json.dumps({'image_smoke': 'passed', 'assets': len(assets),
                  'human_sign_in': 'disabled_as_requested', 'private_api': 'denied',
                  'network': 'none', 'durability': 'not_tested'}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    if not args.image or args.image.startswith('-'):
        parser.error('An explicit local image reference is required')
    name = 'hub-dashboard-smoke-' + uuid.uuid4().hex
    run = [
        'docker', 'run', '--detach', '--name', name, '--network', 'none',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--pids-limit', '64', '--memory', '512m', '--cpus', '1',
        '--tmpfs', '/var/lib/hub:rw,noexec,nosuid,size=64m,uid=10001,gid=10001,mode=0700',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m',
        '--env', 'HERMES_TOKEN=fixture-only-hermes-not-a-service-token',
        '--env', 'CODEX_TOKEN=fixture-only-codex-not-a-service-token',
        '--env', 'HUB_BASE_URL=http://127.0.0.1:8770', args.image,
    ]
    # No shell, token retrieval, user configuration, or shared Docker resources.
    started = False
    try:
        subprocess.run(run, check=True, capture_output=True, text=True, timeout=60)
        started = True
        result = subprocess.run(['docker', 'exec', name, 'python', '-c', PROBE],
                                check=True, capture_output=True, text=True, timeout=30)
        print(json.dumps(json.loads(result.stdout)))
    finally:
        if started:
            subprocess.run(['docker', 'rm', '--force', name], check=True,
                           capture_output=True, text=True, timeout=30)


if __name__ == '__main__':
    main()
