# Agent help and feature discovery

Use the authenticated, read-only MCP tool `bus_info` as the shared `/info` equivalent. It reports the installed package version, schema generation, live native-push availability, recipient inbox URI, and the actual tool catalog. It never executes examples or reads credentials/configuration.

| Arguments | Help returned |
| --- | --- |
| `{}` | Overview, installed version, tool names, available help topics |
| `{"topic":"commands"}` | Live command descriptions and exact input schemas |
| `{"topic":"messaging"}` | Addressed messages, linked replies and lifecycle examples |
| `{"topic":"push"}` | Native subscription, keepalive and saved-cursor recovery |
| `{"topic":"feedback"}` | Bug/improvement/info reports and revision-safe updates |
| `{"topic":"changes"}` | Versioned additions and remaining integration boundaries |
| `{"topic":"worker"}` | Dedicated worker routing, permissions and safety limits |

The examples are documentation, not submitted work. An enabled server feature does not establish that a particular client selected the tool or installed a session connector. Clients with explicit tool allowlists must include `bus_info` and refresh discovery after upgrade.

A client owner may map `/info` or `/bus-info` to this MCP call, or include it in the agent's linking/help skill. Slash commands are client-specific; the server does not install or override a built-in slash command. On an explicit link or a reconnect after upgrade, consult the tool once if needed. Do not create a periodic help/version/model poller.
