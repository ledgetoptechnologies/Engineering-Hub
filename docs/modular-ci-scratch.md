# Exclusive development CI scratch

This is an optional worker source contract. It does not provision storage,
mount filesystems, resize TEST storage, enroll a recipe or release existing
holds. Synthetic tests and local Docker Desktop checks do not establish actual
rootless staging acceptance.

## Operator-owned slot

The worker configuration may include `scratch_slot`, with exactly these fields:

- `path`: an absolute dedicated mount point, not a subdirectory of another mount.
- `filesystem_uuid` and `mount_device`: the measured ext4 UUID and major:minor.
- `ceiling_bytes`, `ceiling_inodes`: maximum accepted actual filesystem capacity.
- `free_floor_bytes`, `free_floor_inodes`: measured admission floors.
- `container_uid`, `container_gid`: the fixed recipe execution identity.
- `mapped_uid`, `mapped_gid`: its independently measured rootless host mapping.

The whole ext4 filesystem must be mounted at its root, writable, with `nosuid`
and `nodev`, and executable. The worker journal must be on a different filesystem.
The operator must establish ownership, isolation from other writers and the
physical capacity before supplying configuration. A directory size estimate,
free-space reading or Docker memory limit is not a filesystem quota.

One lifetime kernel lock excludes another worker from the slot. A permanent
marker binds the slot to the original worker root and journal identity. It is
not recreated over unknown contents or silently rebound to a replacement journal.
Configuration removal, a restart or a missing old process does not make held
scratch reusable. These checks are independent of the Hub's queue ownership.

## Job lifetime

The accepted command and scratch lease are recorded in the same worker
transaction before execution. The public source/workspace/build/test resource
IDs remain unchanged; scratch is an internal command-bound lease. One slot is
exclusive to one command, including its build and test stages.

Only a trusted bound lease mounts the exact directory at `/ci-scratch` and sets
the fixed `EH_CI_SCRATCH=1` signal. Source does not select a host path, quota or
identity. The backend checks actual mount facts and mapped identity before
source execution. The image-owned bootstrap also checks its mount and available
space; this child observation does not replace host enforcement.

Build and test use separate, newly created phase directories. HOME, temporary
files, npm cache and pytest temporary/cache paths live there. Full test execution
requires scratch; it does not fall back to the small `/tmp` tmpfs. The legacy
build-only path remains available without scratch. Existing phase directories
are refused instead of reused or recursively cleared by the bootstrap.

## Cleanup and uncertainty

Source may create private directories owned by its mapped subordinate UID. The
worker cannot assume it can traverse those directories. Cleanup uses a bounded,
fixed image-owned helper under that same execution identity, after proving the
original runtime has no remaining references. The helper receives only the exact
scratch bind, no source archive, Docker socket or network. It does not delete the
worker-owned top directory. The core removes only that empty, identity-checked
top directory after the durable terminal/absence receipt.

An unreadable reference scan, changed runtime identity, unknown helper outcome,
unexpected directory, filesystem drift or unproven absence keeps the lease held.
There is no broad prune, host recursive-delete fallback or adoption of an old
daemon's resources. A new endpoint's empty inventory cannot prove an old
endpoint's writers stopped.

## Acceptance still required

Before enrollment, verify the exact immutable cache/bootstrap binding, real
rootless UID/GID maps, actual ext4 capacity/inode limits, parent aggregate memory
limit and physical free-space floor. Exercise successful build/test, deliberate
failure, private-directory cleanup, endpoint replacement, interruption and
restart with the original journal. Confirm held work blocks reuse and that
cleanup affects only the original command's directory.

The current journal receipt is not physically preallocated. ENOSPC-safe private
terminal receipts and failed diagnostic registration/download remain separate
acceptance requirements. Passing focused source tests is not a claim that the
complete suite, worker recovery or TrueNAS application acceptance has passed.
