"""Pure pilot profile derivation shared by deployment and baseline enrollment.

Inputs come from trusted composition and verified image defaults. These helpers
grant no enrollment, execution, health or provenance authority. Profile digests
are private evidence: never serialize/log the profile's secret environment.
"""
from dataclasses import asdict
import hashlib
import json

from .backend import EffectiveProfile


def derive_runtime_profile(manifest, defaults, generation_id, secrets):
    runtime = manifest.runtime
    environment = {}
    for item in defaults.environment:
        name, value = item.split('=', 1)
        environment[name] = value
    environment.update({'HUB_BASE_URL': runtime.environment.base_url,
                        'HUB_DB': runtime.environment.database})
    environment.update(secrets)
    tmp = runtime.tmpfs.tmp
    # Manifest digits 1777 describe octal permissions, not decimal chmod bits.
    tmp_mode = int(str(tmp.mode), 8)
    return EffectiveProfile(
        uid=runtime.user.uid, gid=runtime.user.gid,
        supplementary_groups=tuple(runtime.user.supplementary_groups),
        read_only_root=runtime.read_only_root, cap_drop=tuple(runtime.cap_drop),
        no_new_privileges=runtime.no_new_privileges, privileged=runtime.privileged,
        pid_mode=runtime.pid_mode, ipc_mode=runtime.ipc_mode,
        cgroupns_mode=runtime.cgroupns_mode,
        network_mode=runtime.network.mode, published_ports=(),
        memory_bytes=runtime.resources.memory_bytes,
        cpu_millicores=runtime.resources.cpu_millicores,
        pids_limit=runtime.resources.pids_limit,
        tmpfs=(('/tmp', tmp.size_bytes, tmp_mode, tmp.noexec, tmp.nosuid),),
        mounts=(('state_generation', runtime.state_mount.container_destination,
                 runtime.state_mount.mode, generation_id),
                ('tmpfs', '/tmp', 'rw', f'size={tmp.size_bytes}',
                 f'mode={tmp_mode:o}', 'noexec', 'nosuid')),
        state_destination=runtime.state_mount.container_destination,
        state_mode=runtime.state_mount.mode, state_generation_id=generation_id,
        environment=tuple(sorted(environment.items())),
        entrypoint=defaults.entrypoint, command=defaults.command,
        restart_policy=runtime.restart_policy,
    )


def probe_set_digest(probes):
    raw = json.dumps([probe.model_dump(mode='json') for probe in probes],
                     sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def runtime_profile_digest(profile):
    if type(profile) is not EffectiveProfile:
        raise ValueError('Exact effective runtime profile required')
    raw = json.dumps(asdict(profile), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return 'sha256:' + hashlib.sha256(raw).hexdigest()
