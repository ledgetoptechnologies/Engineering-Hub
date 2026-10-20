# Small Engineering Hub deployment

This profile requires the release supporting `--config`. Do not install a
new Compose against an older image. Publication and actual NAS acceptance
are separate checks.

`compose.hub-unified.yaml` contains the container definition and protected token
placeholders. Replace those privately with the existing four distinct tokens.
It tracks tested main publication with `:latest`; explicitly pull and redeploy,
because a restart alone does not download a replacement.

The only host mount is:

```yaml
volumes:
  - /mnt/Plugins/App_Data/Engineering-Hub:/var/lib/hub
```

There is no WebODM media mount or SELinux relabel option. This is TrueNAS Hub
storage, not a processing engine's data directory. The image already runs as
UID/GID568 and includes its health check; duplicate declarations are unnecessary.
Small CPU/memory limits, capability drop and no-new-privileges remain, along with
the existing graceful-stop allowance. This simpler profile does not declare a
read-only root or private tmpfs; it is not a sandbox for executing untrusted code.
It does not enable a workflow executor or grant production deployment authority.

## One-time persisted configuration

Copy `hub-settings.example.json` as `settings.json` inside the mounted host
directory. It contains no credentials. Review the hostname, Cloudflare Access
issuer/application audience, exact allowed emails and agent source addresses.
Configure allowed users and login/session policy in the Cloudflare dashboard;
Hub only verifies the resulting signed identity and applies its local allowlist.
There is no separate GitHub login and no dependency on Ops.

This version reads settings at startup; edit the file and restart to change it.
A browser settings editor is not implemented by this release. Missing, malformed,
duplicate, unknown or credential-bearing configuration fails closed. It does
not serve an unauthenticated setup page or silently fall back to another login.
The explicit configuration file owns the non-secret deployment values; legacy
environment-only startup remains supported when `--config` is absent. Agent
tokens still come only from the protected environment, not the JSON file.

The file's parent determines storage:

```text
Engineering-Hub/
  settings.json
  hub.sqlite3
  agent-message-bus/
    messages.sqlite3
    audit.jsonl
    files/
```

The bus state must already exist. Stop old and new writers, obtain coherent
recoverable backups, copy complete preserved state (including dependent files
and SQLite sidecars), keep the source intact, and verify parity/UID568 access.
Never overwrite an existing destination or create an empty replacement history.
Nothing in this profile copies, migrates, deletes or resets existing state.

## Connections

Agents use `http://192.168.50.80:8770/mcp` and their existing separate tokens.
There is no 8765 alias. The existing bridged Cloudflare connector forwards only
Hub's hostname to `http://192.168.50.80:8771`; keep Project Alpha's destination
and tunnel networking unchanged. Humans use `https://hub.ledgetoptechnologies.com`.
The UI port is LAN-reachable but requires a valid signed Hub Access assertion
before HTML/assets/API; a spoofed Host, email header or agent token is not enough.
Health remains a non-secret diagnostic route.

After operator deployment, verify both ports, actual allowed human login and
unsigned/forged LAN rejection, all three agents and preserved data, native
correct-session delivery and restart recovery. Isolated container tests with
synthetic identities do not prove those NAS, identity or reboot checks.
