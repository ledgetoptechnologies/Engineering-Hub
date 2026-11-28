# Hub feature-switch audit

| Setting | Disposition |
| --- | --- |
| `HUB_REGISTRY_ENABLED` | Registry reads default on when a registry origin is configured. Keep the explicit off override so an isolated service can decline outbound registry access even when an environment file supplies an origin. |
| `HUB_REGISTRY_EVENTS_ENABLED` | Events follow the registry by default. Keep the explicit off override so operators can stop the long-lived external connection while retaining reads. Events cannot run with the registry disabled. |
| `HUB_WORK_QUEUE_ENABLED` | Keep off by default. The switch initializes the work queue and can participate in staging dispatch when its other prerequisites are configured. It is an execution boundary, not a dashboard preference; inactive ownership also requires it off. |
| `HUB_BUS_STARTUP_CLEANUP` | Keep the operator control. Startup retention cleanup mutates stored data; inactive and isolated modes must be able to forbid it. |
| `HUB_AGENT_SESSIONS_MODE`, `HUB_AGENT_SESSIONS_HUB_BRIDGE` | Keep the explicit session enrollment and bridge boundary. These select an authenticated transport mode, not visibility of ordinary Hub screens. |
| `HUB_HUMAN_AUTH_MODE`, `HUB_INGRESS_MODE`, owner identities, credentials, host/origin/IP restrictions, protected ledger and staging service settings | Keep. They define authentication, network admission, ownership, or execution prerequisites, not internal feature visibility. |
| Registry and metrics endpoints, database and storage paths, UI directory, ports and machine identity | Keep as deployment inputs. Missing providers must not be replaced by guessed endpoints. |
| Browser demo, alert opt-in, and view preferences | Keep as explicit user choices. Browser code has no environment switch for enabling ordinary Hub screens. Notifications require user consent. |

Neither registry override is required to turn the registry on. Without an origin, the registry remains unavailable. The isolated agent-session Compose profile keeps its explicit opt-outs. No production deployment configuration or queue default is changed by this PR.
