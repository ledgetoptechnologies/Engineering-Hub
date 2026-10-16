# v0.5.1: native MCP push, agent help and complete source packaging

Includes the native push, `bus_info` and opt-in dedicated worker implementation described in [v0.5.0](release-v0.5.0.md). No push, authentication, storage or worker-policy changes in this patch.

Final v0.5.0 source-archive testing found three repository configuration tests could not run because deployment templates were absent. This patch includes `Dockerfile`, both Compose templates, `.env.example` placeholders and `.dockerignore` in the source distribution. It does not include real `.env` files, tokens or private deployment configuration. The v0.5.0 Git/image tag remains immutable; deploy v0.5.1 instead.

## Deployment

Pull/redeploy `ghcr.io/ledgetoptechnologies/internal-mcp:0.5.1` or its verified digest using the supported TrueNAS app action. `stable` also moves to this release. Keep the existing volume mapping, dataset, tokens, network binding and `568:568` runtime identity exactly. Do not paste a generic template over the installed configuration. Take a coherent backup first, stop old writers for cutover and retain the prior image for rollback. A restart alone does not pull an image.

Verify `/health` version0.5.1/schema3, `push.state=healthy` and subscriptions enabled, then authenticated22-tool discovery and `bus_info`. Server push does not itself install a host connector or prove an agent woke. The dedicated worker remains unactivated until its actual model/auth/permissions, endpoint/topic/sender policy and fresh peer-request lifecycle are verified. Paused watchers remain paused; there is no polling fallback.

See [native push](mcp-push.md), [help discovery](bus-info.md) and [worker setup](hermes-push-worker.md). `/info` or `/bus-info` is a client-owned alias for the MCP tool `bus_info`, not an automatically installed global slash command.
