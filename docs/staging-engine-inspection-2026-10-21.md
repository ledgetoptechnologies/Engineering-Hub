# Bounded pilot: Docker inspection compatibility

This records failed TEST attempts and their diagnosis, not a successful deployment.
Beau's direct approval covers staging `192.168.50.90`, dedicated TEST credentials,
aggregate one CPU / 3.25 GiB RAM / 3 GiB disk, and no boot autostart. Production,
live MCP data, PA/Viewer, TrueNAS and Cloudflare remain outside this test.

## Actual attempts

The first disposable baseline attempt stopped before an application create.
Its private operation journal remained empty. A GET-only diagnostic measured
image-archive verification at about 15 seconds under the operator's 0.2-CPU cap;
the original create preflight allowed only 10 seconds. A fresh reviewed manifest
therefore permits 60 seconds for create/inspection, retaining the 120-second
command, 900-second bootstrap and original resource limits.

The fresh campaign `8fd172ea-5675-4d99-811c-8a97ad304f68` reached a completed
create journal entry, but its application never started: PID zero, zero StartedAt,
and an empty precreated SQLite file. Post-create inspection rejected canonical
Docker 29 metadata. Exact terminal/never-started objects were removed without
force after matching their journal, names, image and state. Campaign files and
receipts were retained. The isolated daemon and user manager were then stopped;
all 21 pre-existing rootful container identities, images and states matched.

## Inspection is not kernel proof

The actual staging Docker 29.5.2 response omitted false `ReadOnly`, reported an
empty bind `Mode` alongside `RW: true` / `rprivate`, and omitted the
`HostConfig.Tmpfs` scratch mount from top-level `Mounts`. A separate finite inert
probe on local Docker 29.8.2 confirmed the scratch mount can also be absent from
that array while **running**. The probe was stopped and removed.

This is consistent with the installed-version upstream implementation:

- [`mount.Mount.ReadOnly` is a bool with `omitempty`](https://github.com/moby/moby/blob/568f755ebeb1ac9c6a8febbda6cd371ea0a9630b/api/types/mount/mount.go).
- [`TmpfsMounts` and `GetMountPoints` are separate representations](https://github.com/moby/moby/blob/568f755ebeb1ac9c6a8febbda6cd371ea0a9630b/daemon/container/container_unix.go).
- [`ContainerInspect` obtains `Mounts` from `GetMountPoints`](https://github.com/moby/moby/blob/568f755ebeb1ac9c6a8febbda6cd371ea0a9630b/daemon/inspect.go).

The backend must validate the complete closed `HostConfig.Tmpfs` policy whether
or not Docker repeats it in `Mounts`, and validate any reported scratch mount.
Omitted false is distinct from explicit null, zero or string values. Bind source,
destination, writability and private propagation remain exact. Additional mount
options or mounts are not implicitly approved.

These checks validate metadata from the independently pinned, trusted TEST
daemon. They do not establish kernel mount flags, cgroups or network isolation
against a malicious daemon/socket holder. Actual process/mount controls require
separate host acceptance. The Engine socket remains a privileged control-plane
capability and must never be mounted into application containers. AppArmor is
unavailable in the observed rootless configuration, not counted as passed.

## Remaining work

Build and review a new immutable executor artifact containing the compatibility
fix; do not patch or silently adopt a failed old attempt. Then use a fresh
campaign to verify the real healthy baseline, frozen backup and independent
receipt enrollment, protected issuer/controller, queued deployment, deliberate
rollback, drain and recovery. No healthy baseline or rollback has yet been
demonstrated by these host attempts. PC reboot and live NAS acceptance remain
separate and unproved.
