# Protected ledger layout — preparation, not a live cutover

Beau approved preparing and testing storage under the existing Engineering-Hub
root, with as many child folders/datasets as needed. Beau applies TrueNAS changes. This
document is a design and acceptance checklist, **not a command to migrate or
restart the live Hub**. The current image must not receive this configuration
until the source and its dependencies are merged, published and verified.

## Preferred preparation: selective mounts inside the existing root

Beau clarified that `/mnt/Plugins/App_Data/Engineering-Hub/` is the intended
root for all EH state. No separate top-level dataset is required by this design.
Minimize existing-data movement by keeping the settings and bus sources where
they already are, mounting only the following individual sources:

| Host child source | Container target | Contents |
| --- | --- | --- |
| `Engineering-Hub/settings.json` | `/var/lib/hub/settings.json` (read-only file) | Existing non-secret settings; reviewed version-two storage selection |
| `Engineering-Hub/agent-message-bus/` | `/var/lib/hub/agent-message-bus` | Existing complete bus database, audit, knowledge/files state, unchanged host location |
| `Engineering-Hub/private/` | `/var/lib/hub-private` | Existing Hub ledger after reviewed cold relocation/enrollment; private staging profile and executor credentials |

These are future mount mappings, **not instructions to apply now**. The current
whole-root mapping stays installed until a reviewed offline preservation and
rollback plan is ready. Replace it with the selective mappings together, not
by leaving it alongside the private child. Keep container paths, ports, tokens and human Access
policy. The sources must already exist; no automatic directory creation,
empty-ledger fallback, data move or permission repair is allowed at startup.

This leaves the existing settings and bus host/container paths unchanged.
Preserve their contents except the explicitly reviewed version-two storage
selection in settings. The selected config remains
`/var/lib/hub/settings.json`; version two selects the fixed private ledger.
The image already provides the `/var/lib/hub` directory; it is not a host bind.
Do not add another copy of the authoritative Hub ledger there.
The ordinary/default or version-one entrypoint is not valid for this selective
layout. Repository construction refuses `hub_image_state_mount_required` before
mkdir/SQLite access when a ledger under `/var/lib/hub` lacks the exact parent
mount; nested settings/bus mounts do not satisfy that check. This also applies
when the container root is writable, so safety does not depend on `read_only`.
An explicit mount is not itself proof of durable media, backup or host ACLs.

The future inactive protected startup must use this exact process argument list:

```text
python -I -B -m engineering_hub.owned_inactive_hub --config /var/lib/hub/settings.json
```

That replaces the image's ordinary entrypoint; it must not be appended as extra
arguments to `python -m engineering_hub`. This is a future handoff requirement,
not instructions to change the current Compose now. Autonomous pilot startup
has further enrollment/transport prerequisites and is not implied by this command.
Preserve the original cold state separately for reviewed rollback, never as a
second running authority. Plan backup/snapshot coverage for existing shared
state **and the private child**
and any child datasets, rather than assuming the current parent-only policy
includes newly created datasets.

The settings file is read-only inside the app, not immutable on the NAS. It is
loaded at startup. A file bind pins an inode: replacing the host file atomically
can leave the old version visible in a running container. Apply reviewed edits
before recreating the app, then verify the selected configuration. This is not
a newly implemented runtime settings editor. The Compose fragment must use
long bind syntax with `create_host_path: false` so a missing settings file is
not silently created as a directory. No new Compose recursion feature is needed.

The separately tested alternative is `Engineering-Hub/shared/` to
`/var/lib/hub`, plus `Engineering-Hub/private/` to `/var/lib/hub-private`.
It also avoids aliases, but needs an otherwise unnecessary offline move of all
settings/bus state into `shared`. Retain it as an alternative, not a prerequisite.

Beau and the agents are the current SMB users, not arbitrary outside users.
The intended benefit is predictable process access, preventing accidental edits
to execution state and keeping executor credentials out of ordinary shared
files. These filesystem checks do not isolate Beau's administrator account,
root or another process deliberately running with the same privileged identity.

## Why not keep the whole-parent bind too?

The existing whole-tree mapping is
`/mnt/Plugins/App_Data/Engineering-Hub/` to `/var/lib/hub`. Adding a second bind
from its `private` child to `/var/lib/hub-private` is **not a valid protected
layout**: the same private data remains reachable at `/var/lib/hub/private`.
A child dataset alone does not remove that alias; Docker's recursive bind can
expose the child mount through the shared tree too. The earlier nested-source
Compose example has therefore been withdrawn. Do not apply it.

A plain private directory under a group/SMB-writable parent is also subject to
source replacement or rename by accounts with parent rights. Container mode
bits do not prove that host-side boundary. A child dataset may constrain rename
behavior, but still needs effective ACL and alias-exclusion acceptance.

The selective-mount preparation avoids that alias without moving outside
Engineering-Hub. A protected private child dataset with effective permissions
may be appropriate where a plain folder's parent rights allow replacement.
The exact host arrangement still needs operator acceptance; container tests do
not prove NAS ACLs or mountpoint/source-parent protection. Do not change SMB
exports, recursively change permissions or provision anything on NAS from this
document. This is source/layout preparation, not live cutover authority.

Once a layout is chosen, the extra source must already exist and be protected;
Compose must not create it with permissive defaults. Settings and message-bus
database/audit/files stay under `/var/lib/hub`; any authoritative **existing**
Hub ledger relocation is a later offline, reviewed migration to
`/var/lib/hub-private/hub.sqlite3`. No automatic copy, empty-ledger fallback,
permission repair or enrollment occurs at startup.

Container UID 568 must own the private root 0700 and ledger/owner lock 0600,
single-link regular files on supported local storage. A separately protected
child dataset may help establish host ACLs; a container mount alone does not.
The host source parent currently participates in SMB access: verify effective
ACLs, source-parent rename/delete rights, dataset mount behavior and container
UID mapping. Reject any arrangement that lets unintended SMB users/agents
read private files or replace/rename the source. Do not recursively change
the existing shared root or bus ownership. Do not weaken the strict guards.

## Explicit non-secret configuration

Version1 settings retain their current paths and behavior. Version2 adds only:

```json
{"schema_version": 2, "storage": {"layout": "protected-ledger-v1"}}
```

That fragment is **not a complete settings file**. Retain the existing validated
base URL, Cloudflare Access settings, agent hosts and IPs. No arbitrary database
path, secret or execution switch is accepted. Settings cannot reside inside
the private mount. Protected pilot paths are fixed: `pilot.json`,
`executor.token`, `staging-ca.pem` directly under `/var/lib/hub-private`.
Never put executor credentials in shared settings, bus files, Git or chat.

The exact private mount ID/device, trusted ancestors, existing ledger and lock
metadata and shared-role aliases are checked before selecting an owned service.
Distinct same-filesystem sibling bind sources may be valid; overlapping source
trees and private aliases through other mounts are not. This is not permission
to weaken the guard merely to make the withdrawn nested layout start.
These checks are a current container-namespace snapshot, not a host ACL audit,
reboot admission mechanism or lock against equally privileged administrators.

Normal Hub startup rejects version2 before opening listeners/state. The explicit
owned-inactive entrypoint keeps ordinary Hub/messages/notes usable with no
executor, queue or credential read. The explicit queued-pilot selector requires
fresh independent enrollment/profile/runtime acceptance. Ports remain agent8770
and browser8771; no alias, tunnel, Ops or Cloudflare policy changes are included.

## Operator sequence still to prepare and review

The phased [offline cutover runbook](offline-ledger-cutover-runbook.md) makes
the inspect/apply, parity and abort checkpoints explicit. It is preparation for
one later operator handoff, not permission to apply the mappings now. In
particular, the retained old ledger cannot be used as a lossless rollback after
the private authority accepts new mutations.

1. Publish and verify the exact source/image, including owned-inactive support.
   Earlier account billing restrictions are historical, not a current verified
   blocker: Beau subsequently made the repository public and main publication
   succeeded. Check the exact candidate's current workflow and registry result;
   local tests still do not replace a published image or bypass release gates.
2. Prepare the selective mappings above and review the cold ledger relocation
   and rollback procedure. Establish effective private ACL/source-parent
   protection without recursive changes to existing shared data. Verify the
   actual host/container mappings and backup coverage of all existing and new state.
3. Arrange a maintenance window and stop **all writers to relocated or edited
   state**, including Hub, background services and independently running scripts.
   The preferred layout relocates only the ledger and edits settings; the bus
   sources remain in place. If using the shared-child alternative, additionally
   stop every writer to bus databases, audit/files/knowledge and settings before
   their cold copy. Stopping only the Hub ledger writer is insufficient for that
   alternative. Confirm coherent
   recovery coverage. Existing automatic online ZFS snapshots are acknowledged;
   online capture itself does not require stopping Hub. Restore acceptance is a
   separate check. Never copy only a live SQLite main file or discard WAL/SHM.
4. Inspect the cold authoritative ledger and prepare a reviewed migration plan.
   Preserve tasks, decisions, run/queue/reservation/history rows and database
   identity. Existing enrollment binds inode/lock identities: do not blindly
   copy enrolled files or invent a re-enrollment/restore waiver. The actual
   migration/enrollment tool and rollback procedure require their own evidence.
5. Apply only the reviewed layout/config offline. Verify no stale shared ledger
   can be accidentally started as a second authority. Start owned-inactive Hub,
   check ordinary auth/tasks/notes/messages and orderly stop/reopen preservation.
6. Separately admit a fresh bounded staging pilot after manifest/digest/baseline,
   writer exclusion, TLS credentials, review and rollback checks. Never reuse
   the closed8ec generation. No production or boot autostart is authorized.

No live migration tool, NAS changes, real workflow activation, reboot recovery
or source-parent protection is claimed by this patch. Do not apply the fragment
or mapping to the live app yet.

The disposable cold-copy regression in `test_hub_pilot_enrollment.py` creates
real local auth/queue/delivery tables through their normal constructors, copies
a quiescent synthetic ledger, explicitly enrolls the copy and reopens it with
the live ownership capability. It checks session/identity binding, task and job
idempotency, terminal history/fences and no replay of accepted bus deliveries;
the original source bytes remain unchanged. Its in-process fake bus and test
identities do not verify live Access, NAS backup/restore or a real migration.

## Mount-alias acceptance

The admission check examines canonical mount source roots across the whole
current mount namespace, even when no shared root was supplied. Any other
same-device mount exposing an ancestor, equal root or descendant of the private
source is refused, including file aliases outside the conventional shared
paths. Component comparisons preserve distinct sibling sources. The relevant
mount snapshot is rechecked before returning. This remains a snapshot, not a
guarantee against a host administrator changing mounts afterward.

`tests/test_hub_protected_alias_docker.py` is a separate, opt-in **trusted host**
test. Do not expose a Docker socket to branch CI. It creates only UUID-labelled
synthetic named volumes/containers, mounts current source read-only, uses no
network or application, and removes its exact owned fixtures afterward. Select
an already reviewed, locally cached immutable validation image with Python at
`/opt/validation-venv/bin/python`, then run on the trusted host:

```powershell
$env:EH_DISPOSABLE_DOCKER_ALIAS_TEST = '1'
$env:EH_ALIAS_TEST_CONTEXT = '<verified-local-test-context>'
$env:EH_ALIAS_TEST_IMAGE = 'sha256:<reviewed-local-image-digest>'
python -m pytest -q tests/test_hub_protected_alias_docker.py
```

The five real Docker cases prove separate same-filesystem sibling sources and
two sibling subdirectories of one common source are accepted, a
shared-parent/private-child double mount is rejected, and an equal
private source mounted outside shared paths is rejected without `shared_root`.
Alias cases verify actual matching device/inode identities before admission.
The selective-path case uses real bind mounts of a settings file (read-only),
the existing bus directory and private child from the same owned synthetic
source. It checks real config loading and metadata admission without moving
those sources or opening synthetic database contents. It does not start a Hub
service or prove live data/credentials work on the NAS.
These tests require Docker's `volume-subpath` support; the subdirectory is
created before use. Every command uses the explicitly selected local Unix-socket
or Windows-pipe context; TCP/SSH remote endpoints are refused. Verify that the
selected local engine is disposable-test appropriate before running. These
checks do not test TrueNAS ACLs, dataset rename protection,
NAS mount topology, restored state or reboot recovery.

An unrelated mount whose source root overlaps the private source on the same
filesystem can also cause safe refusal (for example a host file bind). Inspect
the intended mount topology during operator acceptance; do not weaken the
overlap guard or silently switch to another ledger to get past that refusal.

## Explicit disposable test selection

### Installed-image release gate

`scripts/hub_protected_container_smoke.py` exercises the installed release image,
not a source-mounted replacement. With an explicitly selected local Docker
context, it creates a labelled synthetic volume, cold-copies and explicitly
enrolls a disposable ledger, and starts the inactive owned entrypoint using
only the three selective mappings above. It verifies three-agent MCP access,
preserved tasks, decisions, messages and notes across two container recreations,
disabled autonomy, rejected unauthenticated browser access, unchanged original
ledger/settings bytes, resource limits and owned-fixture cleanup. No host ports
are published. Ambient `DOCKER_HOST` is refused; the verified local context is
pinned through cleanup. Missing probe-contract anchors fail closed.
Enrollment uses the actual installed CLI with its complete canonical plan
document. The fixture verifies wrong-digest refusal before lock creation,
successful explicit application, retained non-owner logical fingerprints and
refused re-application with unchanged enrolled ledger/lock bytes. These are
disposable abort checks, not proof of a real NAS rollback or post-mutation
reconciliation.

The main-only image workflow requires this profile before upload and again on
the registry digest before promoting `latest`. This is container acceptance,
not proof of NAS permissions, human Access sign-in, agent wake, workflow
execution, backup restoration or reboot recovery.

The existing live Compose remains unchanged until the operator cutover is
ready. Consolidate the protected-storage and entrypoint changes into one reviewed
handoff rather than requiring incremental Compose replacements. Routine image
updates afterward use pull/redeploy; new infrastructure requirements still need
an explicit reviewed change.

The positive runtime test deliberately skips under ordinary pytest. It must run
in its own disposable Linux container with an **empty** dedicated tmpfs, never
against a NAS mount or real credentials. Add these arguments to the existing
reviewed isolated test-container invocation (UID568, networknone, read-only
root/source and bounded writable test-copy tmpfs):

```text
--env EH_DISPOSABLE_PRIVATE_RUNTIME_TEST=1
--tmpfs /var/lib/hub-private:rw,nosuid,nodev,size=16m,mode=0700,uid=568,gid=568
```

Inside the writable source/test copy, with PYTHONPATH selecting that source:

```sh
python -W error::ResourceWarning -m pytest -q tests/test_hub_protected_mount_runtime.py -W error::pytest.PytestUnraisableExceptionWarning
```

Run `test_hub_protected_layout.py` separately with a fresh empty disposable
mount; its metadata-only fixture intentionally creates non-SQLite sentinel
files. A skipped runtime test is not positive acceptance. These fixtures never
migrate data and their explicit ephemeral-storage owner allowance is TEST-only.
