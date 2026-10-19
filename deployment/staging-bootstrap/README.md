# Staging bootstrap artifacts — environment revision NOT INSTALLED

These files describe the bounded bootstrap, not an automatic installer. Beau
approved the earlier bootstrap. Hermes subsequently reported installing the
reviewed prerequisites, locked `ehpilot` account and three original units, but
stopped before starting Docker when the new manager exposed unexpected socket
activation and inherited `SSH_AUTH_SOCK`. Hermes's bus report
`b4fb8c7b-bcb0-4149-a436-891a8d79932a` says the manager/runtime were stopped and
the shared daemon's objects were unchanged. That is peer evidence of a safely
stopped partial bootstrap, not acceptance of an executor or application pilot.

The environment-whitelist revision below has **not** been installed. Reconcile
the retained exact account, paths and unit hashes with that prior enrollment;
do not recreate, adopt unknown state or launch services merely because this
document exists. No executor, receipt issuer or application pilot is activated
by these artifacts.

## Exact destinations

| Source | Proposed root-owned destination |
| --- | --- |
| `eh-pilot-docker.service` | `/etc/systemd/user/eh-pilot-docker.service` |
| `50-eh-pilot-bootstrap-limits.conf` | `/etc/systemd/system/user-10001.slice.d/50-eh-pilot-bootstrap-limits.conf` |
| `50-eh-pilot-bootstrap-delegate.conf` | `/etc/systemd/system/user@10001.service.d/50-eh-pilot-bootstrap-delegate.conf` |

The uniquely named user unit is in the root-owned user-unit search path, but
`ConditionUser=10001` restricts its applicability. It has **no Install section**
and must not be enabled, linked into default.target, or given linger. The
per-instance slice/manager drop-ins apply only to the proposed new UID. They
must not be installed under global `user.slice` or `user@.service` drop-ins.

The hard parent-slice ceiling is 1 GiB memory, no swap, one CPU-equivalent and
256 tasks. It must enclose the user manager, D-Bus, daemon and all descendants,
not just the Docker service. The unit is a rootless **user** service with
notification readiness, no restart, a 30-second startup budget and a ten-minute
inspection interval. RuntimeMaxSec initiates stopping; it does not guarantee
termination within ten minutes. Stop grace is 1200 seconds with no forced SIGKILL. A stop
that does not finish is not quiescence or permission to abandon ownership.

RootlessKit networking is `none`, Docker bridge/iptables are disabled and Docker
listens only on `%t/eh-pilot-docker.sock`. No CLI context is created or selected.
Do not add `NoNewPrivileges=yes` to this bootstrap daemon as an unreviewed
hardening shortcut: the prerequisite UID-mapping helpers need their approved
setuid behavior. This is not the application's closed container profile.

## Revised daemon environment boundary

`ExecStart` now invokes the real `/usr/bin/env -i` before the fixed rootless
helper, with seven explicit assignments: `HOME`, `PATH`, `XDG_RUNTIME_DIR`,
`DBUS_SESSION_BUS_ADDRESS`, `NOTIFY_SOCKET`,
`DOCKERD_ROOTLESS_ROOTLESSKIT_NET` and
`DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR`. The helper executable and its daemon
arguments remain absolute/fixed. Inherited SSH/GPG socket pointers, proxy
settings, Docker executables/flags and rootless child-mode overrides are not
forwarded to the helper. No shell evaluates this command.

`NOTIFY_SOCKET=${NOTIFY_SOCKET}` deliberately preserves the manager-generated
endpoint as one argument. Do not replace it with a guessed path, literal dollar
escape or unbraced word expansion. See the
[systemd 255 command-line rules](https://www.freedesktop.org/software/systemd/man/255/systemd.service.html#Command%20Lines)
and [spawned-process environment](https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html#Environment%20Variables%20in%20Spawned%20Processes).
`Type=notify` and `NotifyAccess=all` are unchanged.

This is a child-environment boundary, **not** a sandbox for an untrusted manager.
The loader runs before `env`; unreviewed loader overrides are a stop condition.
It does not prevent global user socket units from starting, remove credentials
or block same-UID access to known socket/file paths. Inspect unit identities and
fresh-account ownership/credential-presence metadata without exposing secret
contents. Do not globally mask SSH/GPG units, import operator credentials or
claim the absence of an environment variable proves filesystem isolation.

Static and synthetic fixture tests do not prove real systemd notification
attribution, rootless mappings, daemon network isolation or effective cgroups.
The revised unit must pass installed-system validation and the bounded owned
start/stop checks before the bootstrap can be called successful.

## Preconditions, before privileged changes

For an initial installation, repeat package simulation, UID/GID/name/subordinate
range collision checks and absence of proposed paths/units. For the retained
approved enrollment, instead verify its manifest, exact identities, hashes and
ownership; unexpected state remains a conflict, not permission to adopt or repair
it. In both cases recheck existing host work, cleanup endpoint, free memory/disk,
no session/linger and inactive dedicated user manager/runtime.

The already observed host has systemd 255, `dbus-user-session` and
`libpam-systemd`; do not silently install extra packages if later prerequisites
differ. Only the reviewed uidmap/libsubid4 package addition is proposed. No apt
upgrade/autoremove, rootful-daemon stop, sudoers/group-membership change for an
existing user, AppArmor/sysctl change or credential reuse is authorized.

After specific approval, the operator creates locked/non-login `ehpilot`, proposed
UID/GID 10001, with no supplementary Docker/sudo groups or SSH credentials, private
`/var/lib/eh-pilot`, and separately verified subordinate IDs. Retain a manifest of
the exact approved additions, hashes, ownership and timestamps. No automatic
installer is supplied; the installed rootless setup script starts **and enables**
its service and is not suitable unchanged for this manual-only procedure.

## Owned user-runtime and manager lifecycle

The following are reviewed **post-approval** manager operations, not a receipt
that this revision was executed. Account/path/package identities and exact unit
files must be reconciled and checked first, including a controlled replacement
of the old unit only while its identified owner is terminal. Use the existing
approved administrative channel; do not change sudoers or put passwords in
commands/chat.

```sh
sudo systemctl daemon-reload
sudo systemctl start user-runtime-dir@10001.service user@10001.service

sudo -u ehpilot -- env -i PATH=/usr/bin:/bin \
  XDG_RUNTIME_DIR=/run/user/10001 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/10001/bus \
  systemctl --user daemon-reload
```

Starting `user@10001.service` explicitly establishes its dedicated user manager;
the runtime-dir unit creates the proper private runtime directory. It does not
require an interactive login or enabling linger. Verify actual directory owner,
bus availability, manager InvocationID and ControlGroup before proceeding. Read
the actual `user-10001.slice` kernel limits and verify the manager and daemon
will be descendants. Abort if anything is already active or outside that parent.

Inspect the loaded `eh-pilot-docker.service` FragmentPath and all drop-ins against
the approved hashes. An account-local override is not the approved root-owned
unit. Confirm it is static/not enabled and no default-target symlink or linger
was created. The clean environment below sanitizes the systemctl client; the
unit's separate `env -i` whitelist sanitizes the helper's environment. Before starting, inspect the
installed rootless helper's supported environment overrides and the effective
manager/service configuration without printing secret values. Require a protected
manager environment with no unreviewed binary, flags or child-mode overrides;
otherwise stop for review. The configured NET=none alone is not proof of actual
network isolation. Then, and only then:

```sh
sudo -u ehpilot -- env -i PATH=/usr/bin:/bin \
  XDG_RUNTIME_DIR=/run/user/10001 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/10001/bus \
  systemctl --user start eh-pilot-docker.service
```

Record its InvocationID, main PID/start identity and actual cgroup placement.
Within ten minutes, use only the explicit new socket for read-only Engine info;
inspect rootless/daemon identity, Unix endpoint, namespace maps and kernel limits.
No image/container operations, API exposure, worker registration, issuer or
application startup is allowed. Do not interpret the service's active state or a
configuration readback as a container resource stress test.

## Stop and retained-state handoff

Check the InvocationIDs still identify this bootstrap and that no unexpected
account sessions, user units, containers or processes have appeared. If ownership
or activity is uncertain, stop the procedure and report rather than stopping a
replacement instance or another task's work. Under the same verified ownership:

```sh
sudo -u ehpilot -- env -i PATH=/usr/bin:/bin \
  XDG_RUNTIME_DIR=/run/user/10001 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/10001/bus \
  systemctl --user stop eh-pilot-docker.service
```

**Stop here unless** the owned daemon unit and its identified processes and
descendants are proved terminal, with no accepted or unexpected work. A failed,
timed-out or ambiguous stop must retain the manager and runtime for inspection;
do not escalate by stopping the manager, whose kill policy could defeat the
daemon's no-forced-kill setting. Recheck exact manager ownership before proceeding:

```sh
sudo systemctl stop user@10001.service
```

Verify that this owned manager and its descendants are terminal before allowing
the runtime-directory stop. If the result is unknown, retain the runtime and report:

```sh
sudo systemctl stop user-runtime-dir@10001.service
```

Verify the owned service/manager and processes are terminal, resource use is
released, no unexpected state remains and the old daemon/services are unchanged.
Systemd owns the new ephemeral runtime directory; preserve the account's durable
home/data and exact configuration for review. Do not recursively delete the home,
uninstall packages, remove subordinate mappings/account, run prune, enable
autostart or restart on an observation timeout. Any cleanup is a separate
ownership-checked operation.

No automatic launcher/manager ownership protocol is implemented by these manual
steps. Same-host privileged administrators remain trusted; InvocationID snapshots
alone are not race-free conditional-stop authority. Coordinate the short
operator-owned window, and preserve unknown outcomes.

See the [host evidence and proposal](../../docs/staging-bootstrap-proposal-2026-10-19.md)
for the approval boundary and remaining full-pilot requirements.
