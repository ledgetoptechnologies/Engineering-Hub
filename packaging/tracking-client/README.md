# Tracking client distribution

This client-only build context intentionally contains only the explicit tracking
client, its shared protected-profile code and the Python MCP client dependencies.
It does not contain a Hub server, model or database and cannot grant permissions.
It is preparation, not an installed or authenticated client. Refer to
`docs/tracking-client-distribution.md` in the source repository for verification,
platform limits and installation prerequisites.
