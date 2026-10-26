"""Explicit staging-only executor publication approved by Beau on 2026-10-26.

Uses Docker's existing login and the job's existing GH_TOKEN for one fixed
package metadata read; never reads operator credentials or configuration.
An intent tag is a locator, not an atomic immutable registry reservation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request
import uuid

import hub_executor_compatibility_ci as ci
from hub_executor_image_smoke import OWNER, remove_owned, smoke, verify_defaults
from verify_hub_executor_wheel import verify_wheel

PACKAGE = 'ghcr.io/ledgetoptechnologies/engineering-hub-control'
RECEIPT = Path('executor-publication-receipt.json')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
PACKAGE_API = 'https://api.github.com/orgs/ledgetoptechnologies/packages/container/engineering-hub-control'
INGRESS_PROBE = r'''
import inspect, os, sys
from engineering_hub.build_info import read_build_commit
from engineering_hub.frontends import HubFrontends
from engineering_hub.ingress import HubIngressMiddleware
from engineering_hub.private_gateway import PrivateBrowserGateway
from engineering_hub.queued_pilot_service import serve_owned_queued_pilot
assert os.getuid() == os.geteuid() == os.getgid() == os.getegid() == 10001
assert os.getpid() == 1 and sys.flags.isolated and sys.flags.dont_write_bytecode
assert read_build_commit() == sys.argv[1]
for obj in (HubFrontends, HubIngressMiddleware, PrivateBrowserGateway, serve_owned_queued_pilot):
    parameter = inspect.signature(obj).parameters['access_verifier']
    assert parameter.kind == inspect.Parameter.KEYWORD_ONLY and parameter.default is None
print('installed-ingress-seam-passed')
'''


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def private_package():
    """One fixed, authenticated, bounded package read; no policy modifications."""
    try:
        token = os.environ.get('GH_TOKEN', '')
        if not 0 < len(token) <= 4096 or '\r' in token or '\n' in token:
            raise ValueError('Missing job token')
        request = urllib.request.Request(PACKAGE_API, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=20) as response:
            if response.status != 200 or response.geturl() != PACKAGE_API:
                raise ValueError('Unexpected package response')
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('Package response too large')
        package = json.loads(raw)
        if (package.get('visibility') != 'private'
                or package.get('name') != 'engineering-hub-control'
                or package.get('package_type') != 'container'
                or package.get('owner', {}).get('login', '').casefold() != 'ledgetoptechnologies'):
            raise ValueError('Wrong existing private package')
    except Exception:
        raise RuntimeError('Existing private Hub package not verified; refusing upload') from None


def installed_ingress(expected, core_commit, evidence=None):
    evidence = evidence if evidence is not None else {}
    image(expected, expected)
    owner = uuid.uuid4().hex
    name = 'hub-executor-ingress-' + owner
    evidence.update(ingress_probe_container=name, ingress_probe_owner=owner,
                    ingress_probe_cleanup='pending')
    try:
        run('docker', 'container', 'create', '--pull', 'never', '--name', name,
            '--label', OWNER + '=' + owner, '--network', 'none',
            '--read-only', '--user', '10001:10001', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges:true', '--cpus', '1',
            '--memory', '256m', '--memory-swap', '256m', '--pids-limit', '64',
            '--entrypoint', '/opt/venv/bin/python', expected, '-I', '-B', '-c',
            INGRESS_PROBE, core_commit, timeout=60)
        output = run('docker', 'container', 'start', '--attach', name, timeout=60)
        if output != b'installed-ingress-seam-passed\n':
            raise ValueError('Installed ingress seam not verified')
        inspected, = json.loads(run('docker', 'container', 'inspect', name, timeout=60))
        host = inspected.get('HostConfig', {})
        if (inspected['Config']['Labels'].get(OWNER) != owner
                or inspected['Config'].get('User') != '10001:10001'
                or inspected.get('Image') != expected or inspected.get('Mounts')
                or inspected['State']['Running'] or inspected['State']['ExitCode'] != 0
                or host.get('NetworkMode') != 'none' or host.get('ReadonlyRootfs') is not True
                or host.get('CapDrop') != ['ALL'] or host.get('CapAdd')
                or host.get('SecurityOpt') != ['no-new-privileges:true']
                or host.get('Privileged') or host.get('PidMode') not in ('', None)
                or host.get('Memory') != 256 * 1024**2
                or host.get('MemorySwap') != 256 * 1024**2
                or host.get('NanoCpus') != 1_000_000_000 or host.get('PidsLimit') != 64
                or host.get('Binds') or host.get('Tmpfs') or host.get('PortBindings')
                or host.get('PublishAllPorts')):
            raise ValueError('Installed ingress probe identity or limits mismatch')
    finally:
        try:
            # Existing CI fixture cleanup verifies the exact owner label before
            # removing only this unique inert local container. Never prune.
            remove_owned(name, owner)
            evidence['ingress_probe_cleanup'] = 'removed'
        except Exception:
            evidence['ingress_probe_cleanup'] = 'held'
            raise RuntimeError('Ingress probe cleanup not verified; retain owned locator') from None
    image(expected, expected)


def invocation(env):
    values = ci.inputs(env)
    for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
        if not re.fullmatch(r'[1-9][0-9]{0,19}', env.get(key, '')):
            raise ValueError('Exact workflow run identity required')
    suffix = env['GITHUB_RUN_ID'] + '-' + env['GITHUB_RUN_ATTEMPT']
    return values, 'engineering-hub-executor:build-compatibility-' + suffix, PACKAGE + ':staging-executor-' + suffix


def run(*args, timeout=180):
    return ci.run(*args, timeout=timeout)


def absent(target):
    """Only Docker's exact manifest-not-found result permits publication.

    Authorization errors, timeouts, different CLI wording and transport errors
    are unknown, never absence. This is not an atomic create-if-absent operation.
    """
    result = subprocess.run(['docker', 'manifest', 'inspect', target],
                            capture_output=True, timeout=60)
    if result.returncode == 0:
        raise ValueError('Publication intent tag already exists')
    if (result.returncode != 1 or result.stdout.strip()
            or result.stderr.strip() != ('no such manifest: ' + target).encode()):
        raise RuntimeError('Registry absence not established')


def image(reference, expected=None):
    info, = json.loads(run('docker', 'image', 'inspect', reference))
    verify_defaults(info)
    image_id = info.get('Id')
    if (type(image_id) is not str or not DIGEST.fullmatch(image_id)
            or info.get('Os') != 'linux' or info.get('Architecture') != 'amd64'
            or type(info.get('Size')) is not int or not 0 < info['Size'] <= 2**63 - 1
            or (expected is not None and image_id != expected)):
        raise ValueError('Executor image identity mismatch')
    return info


def descriptor_size(row):
    size = row.get('size')
    if (type(size) is not int or not 0 < size <= 2**63 - 1
            or type(row.get('digest')) is not str or not DIGEST.fullmatch(row['digest'])):
        raise ValueError('Invalid registry descriptor')
    return size


def registry_manifest(reference, expected_config):
    raw = run('docker', 'buildx', 'imagetools', 'inspect', reference, '--raw', timeout=60)
    if not 0 < len(raw) <= 1024 * 1024:
        raise ValueError('Registry manifest size invalid')
    descriptor = json.loads(run('docker', 'buildx', 'imagetools', 'inspect', reference,
                               '--format', '{{json .Manifest}}', timeout=60))
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    manifest = json.loads(raw)
    media = manifest.get('mediaType')
    if (descriptor.get('digest') != digest or manifest.get('schemaVersion') != 2
            or media not in ('application/vnd.oci.image.manifest.v1+json',
                             'application/vnd.docker.distribution.manifest.v2+json')
            or manifest.get('config', {}).get('digest') != expected_config
            or not isinstance(manifest.get('layers'), list)):
        raise ValueError('Registry manifest identity mismatch')
    config_size = descriptor_size(manifest['config'])
    layer_sizes = [descriptor_size(row) for row in manifest['layers']]
    total = sum(layer_sizes)
    if not layer_sizes or len(layer_sizes) > 1024 or total > 2**63 - 1:
        raise ValueError('Invalid registry layer sizes')
    return {'manifest_digest': digest, 'manifest_bytes': len(raw),
            'compressed_layer_bytes': total, 'config_bytes': config_size}


def persist(record):
    # Fixed schema and sanitized values only; no raw CLI output or image config.
    RECEIPT.write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')


def publish(values, local, target, record):
    record['stage'] = 'compatibility_receipt'
    compatibility = json.loads(Path('executor-compatibility-receipt.json').read_text())
    if (compatibility.get('state') != 'passed' or compatibility.get('inputs') != values
            or compatibility.get('image_published') is not False
            or compatibility.get('compatibility', {}).get('core_config_digest') != 'sha256:' + values['CORE_CONFIG']
            or compatibility.get('compatibility', {}).get('core_commit') != values['CORE_COMMIT']
            or compatibility.get('compatibility', {}).get('installed_core_identity') != 'passed'):
        raise ValueError('Exact compatibility receipt required')
    expected = compatibility['compatibility']['executor_imports']['image_id']
    if type(expected) is not str or not DIGEST.fullmatch(expected):
        raise ValueError('Exact executor config required')
    record['stage'] = 'wheel_reverify'
    verify_wheel(wheel='compatibility-work/context/executor.whl', sha256=values['WHEEL_SHA256'],
                 source_commit=values['SOURCE_COMMIT'], core_image=ci.PACKAGE + values['CORE_DIGEST'])
    record['stage'] = 'local_image_reverify'
    inspected = image(local, expected)
    report = smoke(local, values['SOURCE_COMMIT'])
    if report.get('image_id') != expected:
        raise ValueError('Smoke image identity mismatch')
    record['config_digest'] = expected
    record['local_image_size_estimate_bytes'] = inspected['Size']
    record['stage'] = 'installed_ingress_seam'
    installed_ingress(expected, values['CORE_COMMIT'], record)
    record['installed_ingress_seam'] = 'passed'
    record['stage'] = 'private_package_verify'
    private_package()
    record['private_package_verified'] = True
    record['stage'] = 'intent_absence'
    absent(target)
    record['stage'] = 'local_intent_tag'
    # Tag the exact verified config identity, never a potentially moved build tag.
    run('docker', 'tag', expected, target)
    image(target, expected)
    record.update(stage='registry_push', state='push_outcome_unknown',
                  publication_attempted=True, image_published=None)
    # Write local evidence before push. Interrupted overwrites or runner loss
    # before artifact upload can truncate/lose it; independently reconcile the
    # original run/attempt tag after interruption. Never blindly rerun.
    persist(record)
    run('docker', 'push', target, timeout=600)
    record['stage'] = 'registry_readback'
    descriptor = registry_manifest(target, expected)
    digest = descriptor['manifest_digest']
    immutable = PACKAGE + '@' + digest
    # Read by immutable digest as well; a racing tag writer cannot change it.
    if registry_manifest(immutable, expected) != descriptor:
        raise ValueError('Immutable registry readback mismatch')
    record.update(manifest_digest=digest, immutable_reference=immutable,
                  image_published=True, state='published_verification_pending')
    record.update(descriptor)
    persist(record)
    record['stage'] = 'published_image_pull'
    run('docker', 'pull', '--platform', 'linux/amd64', immutable)
    pulled = image(immutable, expected)
    if immutable not in pulled.get('RepoDigests', []):
        raise ValueError('Pulled immutable reference mismatch')
    # Existing smoke accepts only executor build aliases. Alias ONLY the verified
    # digest-pulled image; its exact ID/defaults are checked again by the smoke.
    alias = local.replace(':build-compatibility-', ':build-publication-verified-')
    run('docker', 'tag', immutable, alias)
    record['stage'] = 'published_import_smoke'
    report = smoke(alias, values['SOURCE_COMMIT'])
    if report.get('image_id') != expected:
        raise ValueError('Published smoke identity mismatch')
    record.update(stage='complete', state='published_verified',
                  installed_imports='passed', staging_acceptance='not_tested')


def main():
    # Fail before registry access on every invalid invocation. Workflow is also
    # restricted to the approved manual main publication route.
    values, local, target = invocation(os.environ)
    ci.git('merge-base', '--is-ancestor', values['SOURCE_COMMIT'], 'HEAD')
    if RECEIPT.exists():
        raise RuntimeError('Existing publication attempt evidence; refusing retry')
    record = {'schema': 'engineering-hub.executor-publication.v1', 'inputs': values,
              'intent_tag': target, 'state': 'preflight', 'stage': 'start',
              'publication_attempted': False, 'image_published': False,
              'staging_acceptance': 'not_tested'}
    persist(record)
    try:
        publish(values, local, target, record)
    except Exception as exc:
        record['failure'] = ci.failure(exc, record['stage'])
        if not record['publication_attempted']:
            record['state'] = 'preflight_failed'
        raise RuntimeError('Executor publication not verified; retain receipt, do not retry blindly') from None
    finally:
        persist(record)


if __name__ == '__main__':
    main()
