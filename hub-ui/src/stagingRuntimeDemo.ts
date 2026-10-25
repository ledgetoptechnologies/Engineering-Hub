/** Illustrative staging PC observation for demo mode. Names are placeholders; nothing is read from a real PC. */
import type { StagingRuntime } from "./stagingRuntimeApi";

export function createDemoRuntime(now = new Date()): StagingRuntime {
  const at = (minutes: number) => new Date(now.getTime() - minutes * 60_000).toISOString();
  return { configured: true, state: "ready", snapshot: {
    schema: "hub-staging-runtime.v1", observed_at: at(0.25),
    sources: [{ id: "worker", state: "observed", observed_at: at(0.25) }, { id: "host", state: "observed", observed_at: at(0.5) }],
    containers: [
      { id: "a1".repeat(32), name: "eh-build-vite", image: "example.test/eh/vite-build:1", state: "running", scope: "worker", managed: false },
      { id: "b2".repeat(32), name: "eh-test-run", image: "example.test/eh/test-runner:1", state: "exited", scope: "worker", managed: false },
      { id: "c3".repeat(32), name: "pa-assistant", image: "example.test/pa/assistant:2", state: "running", scope: "host", managed: false },
      { id: "d4".repeat(32), name: "viewer", image: "example.test/viewer/web:5", state: "running", scope: "host", managed: false },
      { id: "e5".repeat(32), name: "viewer-thumbnails", image: "example.test/viewer/thumbs:5", state: "restarting", scope: "host", managed: false },
    ],
    services: [{ name: "asset-service.service", state: "running", scope: "host" }],
  } };
}
