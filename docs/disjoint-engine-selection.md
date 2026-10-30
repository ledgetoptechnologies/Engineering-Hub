# Selecting a separate staging Engine (offline record)

Status: **source only, off by default**. Nothing reads this record at startup,
contacts an Engine, creates storage or starts a pilot. It does not change or
rebind the frozen pilot acceptance, which still names the held TEST Engine.

`engineering_hub.engine_selection` validates one closed record,
`engineering-hub.engine-selection.v1`, describing a different Docker Engine
for a future staging pilot. Its purpose is to make the owner's decision
concrete ("use Engine X, run as account Y, with this CPU, memory and disk") and
to refuse any record that would reuse what is being held.

## What it always refuses

- The held TEST Engine ID, and the TEST runtime account (UID 10001).
- Sockets inside, and data roots overlapping, the held TEST mount, the raw TEST
  image, the retained original backup or the TEST account's runtime directory.
- The ordinary system Engine: `/var/run/docker.sock`, `/run/docker.sock`,
  `/var/lib/docker` and containerd's directories.
- Paths that aren't already normal absolute spellings (`..`, `//`, `.`,
  trailing `/`), a socket inside its own data root, and a mode that disagrees
  with the account (rootless needs a non-root account, rootful needs root).
- Missing, zero, fractional or unbounded budgets, unknown fields, duplicate
  JSON keys and records over 16 KiB.
- Any namespace ID. An old namespace number is history, not proof that a new
  Engine is isolated now.

## What it does not prove

The path checks compare written paths only. They say nothing about mounts,
bind sources, symlinks, devices, user namespaces or cgroups. A record can be
`enabled` only with a human decision ID and a cited digest for each kind of
host evidence: Engine identity, storage disjointness, resource limits,
namespace review and capacity. Even then its status is `selected_unverified`:
an independent reviewer still has to check that evidence on the host, and
activation remains a separate, explicitly approved step.

A socket path or Engine ID on its own can't show that a daemon or its storage
is really separate: aliases, symlinks, bind mounts, reused process IDs, shared
backing storage and shared resource limits all hide that. Current host
evidence has to be verified independently, and checked again right before any
effect. The existing Engine connection checks (`_UnixEngine` peer and
generation identity) remain the authority at runtime; this record doesn't
replace or relax them.

## Example (disabled)

```json
{
  "schema": "engineering-hub.engine-selection.v1",
  "enabled": false,
  "machine_id": "staging-pc",
  "engine": {
    "engine_id": "<measured Engine ID>",
    "mode": "rootless",
    "run_as_uid": 10002,
    "socket_path": "/run/user/10002/eh-pilot-b/engine.sock",
    "data_root": "/var/lib/eh-pilot-b/docker-data"
  },
  "budget": {"cpu_millis": 1000, "memory_bytes": 3221225472, "disk_bytes": 3221225472},
  "approval": null,
  "evidence": []
}
```

The account, paths and budget above are placeholders, not a proposal.
