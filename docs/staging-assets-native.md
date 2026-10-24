# Native private staging assets

This is a trusted, read-only data service for interactive EH components. It does
not run builds, commands, Docker, processing jobs, uploaded code or web apps. The
phone interface remains behind EH's existing login and project permissions; EH
fetches registered asset bytes over private TLS. Browser cookies, Cloudflare
assertions and agent credentials never go to the asset service.

## Current acceptance state (0.1.1)

On 24 October 2026, an explicitly authorized synthetic pilot ran as UID1000
(`bkoltz`) on staging `192.168.50.90`, then stopped. The exact transient unit is
`eh-staging-assets-20261024-v1.service`. It is inactive with PID 0, has no listener
on port 8790 and has no boot autostart. Existing linger settings were observed,
not changed. No NAS-side connection or live EH-to-staging integration has been
verified; the Hub update/configuration is a separate step.

Version 0.1.1 completed one explicitly approved follow-up window after the byte
integrity fix. The original manifest, original 112-byte asset, TLS identity and
token were preserved; the old 0.1.0 package was retained without activation.
The runtime independently proved these kernel settings before serving:

| Setting | Actual value |
| --- | --- |
| CPU | `cpu.max=25000 100000` (25%) |
| RAM | `memory.max=134217728` (128MiB) |
| Swap | `memory.swap.max=0` |
| Tasks | `pids.max=16` |
| User | UID/GID 1000, unprivileged user service |

Actual 0.1.1 HTTPS acceptance verified the private-IP certificate SAN, missing-token
401, authenticated health, accurate HEAD/ETag, single-range206, exact synthetic
asset bytes and rejection of an unapproved source address before HTTP. The
asset is a 112-byte JSON tetrahedron, not client data. Its UUID is
`c4e4354d-4e12-4d5c-84e7-9dc022121eb6`; SHA256 is
`cedc532fd892192f6531884b8834768fcedb369f0e06e3d3675d1bd6fff75710`.

The first pilot's final wrapper tried rebinding the just-closed port without
SO_REUSEADDR and misreported TIME_WAIT as a remaining listener. Independent
systemd PID/state and `ss -ltn` checks confirmed cleanup. A second bounded
acceptance had already completed before the instruction to avoid another
window arrived; it also passed and stopped. No further 0.1.0 window followed.
Use process ownership plus listening-socket checks for absence, not a bare bind.

The final private evidence receipt is
`C:/Users/fstor/.codex/artifacts/native-staging-assets-011-acceptance-20261024.json`.
It records package/certificate hashes, file references, actual resource caps,
HTTPS checks and stopped-listener proof; it contains no credential values.
The historical 0.1.0 receipt is
`C:/Users/fstor/.codex/artifacts/native-staging-assets-acceptance-20261024.json`.

The final window also verified four simultaneous 8 MiB range responses under
the same limits. Actual kernel memory peak was 56,397,824 bytes. A separate
temporary synthetic asset was modified in place without changing its size and
its old mtime was restored; the service refused the changed range with 409.
That probe and its separate temporary manifest were removed only after the
exact unit stopped. The registered 112-byte asset was never replaced. These
host checks complement 34 passing local tests (one POSIX-only skip on Windows),
including byte-digest rejection when metadata observations reveal no change.

## Package and protected layout

The standalone wheel has zero runtime dependencies and runs with the installed
Python3.12.3 standard library. The tested 0.1.1 wheel is 7,529 bytes, SHA256
`396bb662c6087e4c5c1e285a79846ec6ae2e87ea15e3297114bb40a6658dca07`.
It can run directly from its wheel through PYTHONPATH; no pip installation or
network package download is needed.

The pilot's user-owned root is
`/home/bkoltz/.local/share/eh-staging-assets-20261024-v1` (0700):

| Relative path | Purpose |
| --- | --- |
| `releases/hub_staging_assets-0.1.1-py3-none-any.whl` | Pinned reviewed runtime |
| `releases/hub_staging_assets-0.1.0-py3-none-any.whl` | Retained previous package; no automatic downgrade |
| `data/synthetic-pointcloud.json` | Immutable synthetic data only |
| `manifest.json` | Operator-owned UUID/path/size/SHA/media registration |
| `private/service-token` | Dedicated asset-service bearer credential,0600 |
| `private/leaf-key.pem` | Dedicated private TLS key,0600 |
| `private/leaf-cert.pem` | Public certificate in protected storage,0600 |
| `acceptance.json`, `acceptance-0.1.1.json` | Sanitized historical/final pilot evidence |

The certificate's public PEM-file SHA256 is
`64200e9c6f96c79ff96569704b5881a6cba9eebc2a603567b46894305cf6f8cb`.
Its SAN includes `192.168.50.90` and `127.0.0.1`. Token/key values must never be
copied into notes, chat, Git, source archives or browser requests. NAS handoff
must use protected file references; no NAS files were installed by this pilot.

Installed files and records remain below 64 KiB. The deployment budget, including one
known-good rollback package, synthetic assets, SQLite-free metadata and bounded
operational records, is at most 128 MiB. Disk available after acceptance was
5,114,880,000 bytes. Recheck space before any later copy; require at least4GiB
free plus 64 MiB operating margin and the full proposed allocation. Reserve at
least 2 GiB available host RAM beyond the service's 128 MiB limit. None of the old
TEST image/backup storage is available to this service or its cleanup.

## Connection and protocol

The reviewed pilot endpoint is `https://192.168.50.90:8790`. Only exact peers
`192.168.50.80` (NAS), `192.168.50.90` (host acceptance), and `127.0.0.1` were
allowed. Unapproved sockets are closed before TLS handshaking or HTTP parsing;
forwarded-IP headers confer nothing. There were no firewall or tunnel changes.

CLI configuration uses existing protected references:

```text
python3 -B -m hub_staging_assets
  --data-root <absolute-private-data-directory>
  --manifest <absolute-private-manifest.json>
  --token-file <absolute-private-token-file>
  --host 192.168.50.90 --port 8790
  --cert-file <absolute-certificate-file>
  --key-file <absolute-private-key-file>
  --allowed-peer 192.168.50.80
  --allowed-peer 192.168.50.90
  --allowed-peer 127.0.0.1
  --max-asset-bytes 134217728
```

Default bind is127.0.0.1. A nonloopback private bind requires both certificate
files and an explicit exact-IP peer list. `--check` validates configuration,
credential protection and all manifest content hashes without opening a
listener. Actual serving is Linux/nonroot only. A transient user service must
set the resource limits above, NoNewPrivileges and UMask0077, and verify the
actual child cgroup before importing/starting the server. Do not substitute an
unbounded background process if delegation or resource controls are unavailable.

All GET/HEAD routes, including `/health`, require the dedicated Bearer token.
`/health` returns only `{ok:true,assets:<count>}`. Assets use
`/v1/assets/{canonical-UUID}`; there is no registration/upload/write endpoint.
Each manifest entry is exactly:

```json
{
  "relative_path": "synthetic-pointcloud.json",
  "size": 112,
  "sha256": "<64 lowercase hex characters>",
  "media_type": "application/json"
}
```

Startup streams and verifies each complete hash while building an immutable
SHA256 index for its 64 KiB chunks. Every range verifies its covering chunks
against that index before returning the requested bytes, so same-sized changes
within a filesystem clock tick cannot evade integrity checks. A tiny range
does not rehash an entire multi-gigabyte file. The raw catalog index is capped
at 4 MiB. Every request also checks the original inode, size, modification/change
times, ownership, mode and link count before/open/after reading. Links, path
traversal, special files, changed assets and duplicate manifest keys are rejected. The allowed
media types are JSON, JPEG, PNG, GLB and octet-stream; HTML and JavaScript are
excluded. Up to128 assets and2GiB per asset are supported by the package; this
deployment caps individual assets at128MiB within its total storage budget.

Responses provide Accept-Ranges, immutable SHA256 ETag, exact Content-Length and
nosniff/no-store headers. Only a single byte range is accepted, at most8MiB per
request; larger full GETs return416 and require a range. The final bytearray to
bytes conversion can temporarily duplicate the payload, so application buffer
peak is approximately 16 MiB plus 64 KiB per maximum-sized request, not 8 MiB.
Four threads, the index and runtime overhead remain under the hard 128 MiB
service budget; the simultaneous range test above measured the actual peak.
HEAD reports total
length without a body. Suffix/open-ended ranges are supported within the same
bound. There are at most four request threads, a five-second TLS handshake
timeout and15-second request socket timeouts. There are no redirects or cookies.

## Separation from the held build executor

Fresh inventory found no UID10001 processes or runtime socket, no bkoltz
rootless Docker service, and loop8 unbound. The original4GiB TEST file remains
root0600, inode4719223/device64512; its contents were not opened, hashed,
repaired or mounted. The new boot ID is
`f07e1786-fe16-4b9f-a5f1-949389cf607a`, so historical boot-specific maintenance
commands are stale. The logical held build reservations remain authoritative.

This small trusted data reader neither claims that build machine reservation
nor offers an alternate job/command execution path. No new Docker daemon was
started. Existing PA services, Viewer containers/unit, cloudflared, TrueNAS,
Ops and Cloudflare policies were left unchanged. Viewer was already inactive
in the fresh inventory; the service inventory did not stop or restart it.

Before a later activation, recheck the exact package/manifest and host budgets,
verify the existing unit is stopped and8790 has no listener, then use a bounded
transient user service. Keep no boot enablement or autostart unless separately
requested. A first-install rollback is simply stopping the exact owned unit;
retain its protected files/evidence. Never prune unknown containers, volumes,
the old TEST image, backups or other projects to make space for this service.
