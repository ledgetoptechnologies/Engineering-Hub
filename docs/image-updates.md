# Updating the TrueNAS Agent Bus image

`compose.truenas.yaml` follows the private `internal-mcp:stable` tag and sets `pull_policy: always`. Non-prerelease version releases update that tag. The v0.2.0 digest-qualified Compose remains available for rollback.

## Restart is not an image update

`docker restart` and `docker compose restart` restart the existing container. Neither pulls images nor replaces the container, regardless of `pull_policy`. Docker's automatic restart policy behaves the same way.

For a standalone Compose deployment, use this instead of restart:

```sh
docker compose -f compose.truenas.yaml up -d --pull always --force-recreate
```

For a TrueNAS-managed custom app, use its update/redeploy workflow so TrueNAS remains the app supervisor. A UI action that merely restarts containers is not sufficient. The updated YAML requests a pull whenever Compose creates/redeploys the app. Private GHCR pull credentials must remain configured on TrueNAS.

Do not mount the Docker socket into the application to let it replace itself. Do not add a second container polling for updates. Container crashes and NAS reboots should not silently deploy arbitrary new code.

Before updating, take a consistent dataset backup and record the running image digest. After updating, verify `/health`, message/knowledge persistence, and the deployed OCI revision. Keep the paused bus watcher paused until separately approved.
