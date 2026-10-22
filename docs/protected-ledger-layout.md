# Protected ledger layout — preparation, not a live cutover

Beau approved preparing and testing one extra protected mapping under the
existing Engineering-Hub storage tree. Beau applies TrueNAS changes. This
document is a design and acceptance checklist, **not a command to migrate or
restart the live Hub**. The current image must not receive this configuration
until the source and its dependencies are merged, published and verified.

## Proposed storage

Existing mapping remains unchanged:

```yaml
volumes:
  - /mnt/Plugins/App_Data/Engineering-Hub/:/var/lib/hub
  - /mnt/Plugins/App_Data/Engineering-Hub/private:/var/lib/hub-private
```

The second source must already exist and be independently protected. Do not
let Compose create it with permissive defaults. Settings and message-bus
database/audit/files stay under `/var/lib/hub`; the authoritative **existing**
Hub ledger moves offline to `/var/lib/hub-private/hub.sqlite3`. No automatic
copy, empty-ledger fallback, permission repair or enrollment occurs at startup.

Container UID568 must own the private root0700 and ledger/ownerlock0600,
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
Same-filesystem bind mounts are valid; identical inode aliases are not.
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
2. Establish private source ACL/rename protection without changing existing SMB
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
