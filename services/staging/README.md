# Hub staging worker

This separately packaged standard-library service accepts one EH-issued build/test operation at a time. EH owns the queue and production priority. The SQLite journal supplies idempotency and resource ownership, never autonomous scheduling. Restarted accepted/running operations become held and block new execution; they are never replayed automatically.

Run `python -m hub_staging_service --config /private/staging.json --check-config`, then omit `--check-config` to serve. Install the wheel built from this directory; runtime dependencies are empty. Use an unprivileged Linux account and its isolated rootless Docker daemon. The Docker executable is fixed at `/usr/bin/docker`. A private TLS proxy must protect any traffic leaving localhost. Do not expose this worker to the public tunnel.

Kernel enforcement is mandatory, not inferred from Docker CLI flags. Before
each stage the fixed endpoint must report `name=rootless`, cgroup v2 and the
`systemd` cgroup driver. Containers explicitly request a private cgroup namespace.
After the trusted idle container starts and **before any source copy or recipe
execution**, a fixed, bounded `/bin/sh` probe invokes `/bin/cat` on `cpu.max`,
`memory.max`, `memory.swap.max` and `pids.max` inside that exact container.
Finite positive CPU quota/period, memory and PIDs must be at most the recipe
limits, and swap must be zero. Missing/unlimited/malformed/ignored limits deny
execution. The enrolled cached image must therefore contain working `/bin/sh`,
`/bin/cat`, `sleep` and `cp`; no package install or alternate probe is attempted.
Rejected checks still use the existing exact label/scope cleanup; uncertain
cleanup holds the operation. These source/mock checks do not prove a live daemon,
delegated host controllers or actual staging execution acceptance.

Storage settings are explicit integer bytes: `capacity_bytes` defaults to1073741824 (1 GiB), minimum67108864 (64 MiB); `free_floor_bytes` defaults to536870912 (512 MiB), minimum536870912. Both are validated by `--check-config` before state is opened. Configure the approved staging host with `free_floor_bytes: 4294967296` (4 GiB). At approximately4.7 GiB free, the worker must admit only reservations that preserve that4 GiB floor; the1 GiB inventory budget is not a promise that1 GiB is currently available. Capacity admission counts existing private state plus the bounded new source/workspace/output reservation. No storage setting increases kernel job limits or permits cleanup outside owned state.

Configuration contains `worker_root` (private absolute directory), `token_file` (existing dedicated secret file, mode 0600), `allowed_peer_ips` (real socket peers, including the NAS or approved TLS proxy), `docker_endpoint` (explicit local Unix socket, for example `unix:///run/user/10001/docker.sock`), `bind`, `port`, and `projects`. Every Docker invocation pins that same endpoint with `--host` and a sanitized environment; host context/environment changes cannot select another daemon. TCP/SSH endpoints and remote contexts are rejected. Each project maps to an operator-reviewed fixed recipe:

```json
{
  "image_id": "sha256:<existing cached image's 64 lowercase hex characters>",
  "build_argv": ["npm", "run", "build", "--", "--outDir", "/output"],
  "test_argv": ["npm", "test", "--", "--run"],
  "timeout_seconds": 300,
  "memory_mb": 512,
  "workspace_bytes": 268435456,
  "cpus": 1,
  "pids": 64,
  "output_bytes": 16777216,
  "log_bytes": 65536
}
```

The image must already contain `/bin/sh`, `cp`, `sleep`, the required runtime and offline dependencies/cache. Network-disabled jobs cannot fetch the npm registry or PyPI. Profile authors must explicitly use cached dependencies or a preloaded toolchain; ordinary `npm ci` without its matching offline cache will fail. Images are never pulled; source cannot supply Dockerfiles, host commands, image names, resource limits or execution recipes. Each stage reads the immutable source at `/source`; a trusted fixed prelude copies it into writable, executable, bounded tmpfs `/workspace` before the fixed recipe runs. The workspace budget defaults to half container memory capped at256 MiB; an explicit `workspace_bytes` cannot exceed half container memory. Build writes static files to `/output`; each stage can write test artifacts to `/test-output` (one quarter of `output_bytes` per stage). All tmpfs pages are charged against the container's memory limit. Logs are bounded to `log_bytes` per stage and must each fit within one quarter of the output budget. No source container receives a host-writable bind. After each foreground command completes, all container processes are paused before bounded, validated tar export; the exact owned container is then removed. Static build output becomes the immutable preview resource after both steps succeed. Commit identity remains **declared**; the actual uploaded archive SHA256 is verified.

Bearer authentication is mandatory on every route; forwarded IPs and human cookies are ignored. API: `POST /v1/sources`, `POST /v1/jobs`, `GET /v1/jobs/{UUID}`, `GET /v1/operations/{commandUUID}` (read the original cleanup receipt without replay), `GET /v1/resources/{UUID}/metadata`, `GET /v1/resources/{UUID}/files/{relative-path}`, `POST /v1/cleanup`, `GET /v1/status`. See `docs/modular-staging-v1.md` for the EH contract. Deposit source with `{source_id:UUID,project,source_digest,source_zip_base64}`; receipt is `{source_id,project,source_digest,size_bytes}`. Same source identity is idempotent; changed bytes/project conflict. Deposits are a bounded private content cache. Before new deposits, registered source files older than24 hours can be collected when unreferenced, or when every referencing job is succeeded/failed and has exactly its four registered resources confirmed deleted with no retained references. Active/held jobs, ready/pinned resources, deletion in progress, missing facts and unknown files remain untouched. Job and cleanup journals survive source collection, so repeated commands return their original receipts without executing again.

Job payload includes UUID `job_id`, UUID `command_id`, `idempotency_key`, allowlisted `project`, configured `recipe`, 40-hex `source_commit`, archive 64-hex `source_digest`, registered `source_id`, and UUID `resource_ids` for `workspace`, `build`, `test_run`, `preview`. For direct core tests only, `source_zip_base64` may replace `source_id`; EH should deposit first and retain only the reference in its authoritative queue. Configuration can select multiple reviewed recipes with `projects:{project:{recipes:{recipe_key:profile}}}`; a direct project profile has recipe key `default`. Cleanup takes command identity plus `resources: [{resource_id, expected_revision}]`; the expected revision is the private worker resource revision, not EH's independent policy revision. Repeated identical commands return their stored operation; changed payloads conflict.

Cleanup only removes registered terminal-job resources, rechecks identity/revision/references and rejects symlinks. Tombstones remain. There is no prune, unknown-resource cleanup, restart recovery execution, daemon activation, installation or remote deployment command. Held operations or uncertain container cleanup block new work and require explicit operator reconciliation outside this first worker API. Keep that limitation visible before a production rollout. Deploy the worker itself under an external CPU/memory/process policy; container limits do not limit the Python service. Its default private root budget is1 GiB with512 MiB free-host floor. The service limits HTTP concurrency to4 and request bodies to12 MiB.
