# Bounded TEST runtime: actual observations

This is runtime diagnostic evidence, **not a completed application pilot**.
Beau approved the isolated staging-PC TEST pilot with aggregate limits of one
CPU,3.25GiB RAM and3GiB disk, no boot autostart, and no changes to PA/Viewer,
live MCP data, production, TrueNAS or Cloudflare.

## Corrected rootless assumption

Staging runs Docker29.5.2 (`568f755`). Its standard detached-rootless mode runs
the trusted daemon in the host network namespace. Forcing
`DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS=false` was an overly restrictive
diagnostic assumption: this version then attempted to read an inaccessible
AppArmor profile list and failed before starting a container. Do not disable
host AppArmor, alter profiles or use rootful Docker to bypass this failure.

[Docker documents AppArmor as unsupported in rootless mode](https://docs.docker.com/engine/security/rootless/troubleshoot/#known-limitations).
The [installed-version implementation](https://github.com/moby/moby/blob/568f755/daemon/apparmor_linux.go)
skips that availability check in detached mode. The bootstrap proposal now
explicitly selects standard detached mode. Its clean environment, no-autostart,
finite lifetime, delegation and resource boundaries remain unchanged. This
source change does not update an installed host unit automatically.

Daemon networking is **not** consumer networking. Every application/executor
must still have a closed `NetworkMode=none` profile, no published ports and
independently verified effective controls. Rootless by itself is not proof of
network isolation or an AppArmor-equivalent boundary.

## Actual inert consumer result

The fixed diagnostic used the locally reviewed executor's immutable runnable
manifest `sha256:e3d4e2064fb65f2afb64cbce5db7ecff73609075e4812f92c1565b27a1359325`.
It loaded no credentials, started no application and sent no Engine RPC from
inside the consumer; it only measured the permitted Unix-socket connection.

- Consumer UID/GID10001 mapped to actual host UID/GID210000; supplementary group
  was10001 inside and210000 outside. Engine socket access succeeded with mode0660.
- Seccomp was2, one filter; no-new-privileges was1; all capability sets were zero.
- Consumer network namespace differed from both daemon and host. Only loopback
  existed; no IPv4 routes and no usable non-loopback IPv6 routes existed.
- Effective consumer limits were0.2CPU,128MiB RAM, zero swap and16PIDs. Its
  kernel ancestor enforced aggregate1CPU/3.25GiB RAM/zero swap/256PIDs, including
  the daemon and operator transient. These are measured limits, not load sizing.
- Actual LSM label was `rootlesskit (unconfined)`; Engine's AppArmor profile was
  empty. **Per-container AppArmor is not available**, not a passed control.
- The consumer handled TERM and exited0, then only that exact terminal owned
  object was removed without force. The daemon/user manager stopped cleanly.
  All21 existing rootful container IDs, images and states were preserved.

Protected receipts remain under the approved TEST volume's
`evidence/daemon-standard-v4/consumer-probe-v4b`. A preceding inert attempt failed
before obtaining a process because Docker's local log driver rejects compressed
rotation with `max-file=1`; its never-started object was removed separately and
its evidence retained. The diagnostic now uses two bounded log files.

## Remaining acceptance

No baseline, candidate application, queued deployment, launch receipt or rollback
ran in this diagnostic. Build metadata has been independently checked, but
test-specific manifest/enrollment, protected issuer/controller installation and
actual success/rollback campaigns remain. The runtime is stopped; three pinned
TEST images remain within the hard3GiB filesystem, with no boot registration.
Neither this diagnostic nor container recreation establishes PC reboot recovery,
live NAS deployment, production approval or general-purpose untrusted-code safety.
