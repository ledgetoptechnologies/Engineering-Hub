# Protected ledger layout — preparation, not a live cutover

Beau approved preparing and testing one extra protected mapping under the
existing Engineering-Hub storage tree. Beau applies TrueNAS changes. This
document is a design and acceptance checklist, **not a command to migrate or
restart the live Hub**. The current image must not receive this configuration
until the source and its dependencies are merged, published and verified.

## Storage choice is not ready to apply

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

The simplest candidate is an independently protected source **outside both the
SMB export and the shared bind source**, while leaving existing shared settings
and bus paths unchanged. This is a proposal, not an approved host path: Beau's
current approval covers preparation/testing under Engineering-Hub only. Do not
provision a sibling dataset or change the existing shared mapping without the
corresponding direct approval. An alternative retaining that parent would need
a separately reviewed export/mount arrangement that excludes every private
alias; this patch does not claim such an arrangement exists.

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

1. Publish and verify the exact source/image, including owned-inactive support.
   GitHub Actions currently has an account billing/spending restriction; local
   tests do not replace a published image or authorize bypassing release gates.
2. Obtain the layout decision above, then establish private source ACL/rename protection without changing existing SMB
   settings/bus behavior. Verify host/container metadata and the extra mount.
3. Arrange a maintenance window and stop **all ledger writers**. Confirm coherent
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

The three real Docker cases prove separate same-filesystem sibling sources are
accepted, a shared-parent/private-child double mount is rejected, and an equal
private source mounted outside shared paths is rejected without `shared_root`.
Alias cases verify actual matching device/inode identities before admission.
These tests require Docker's `volume-subpath` support; the subdirectory is
created before use. Every command uses the explicitly selected local Unix-socket
or Windows-pipe context; TCP/SSH remote endpoints are refused. Verify that the
selected local engine is disposable-test appropriate before running. These
checks do not test TrueNAS ACLs, dataset rename protection,
NAS mount topology, restored state or reboot recovery.

## Explicit disposable test selection

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
