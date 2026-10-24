import { defineConfig } from "@playwright/test";

// Standalone integration: no Vite server and no mocked browser routes.
export default defineConfig({
  testDir: "./preview-live",
  testMatch: "staging-preview-live.spec.ts",
  workers: 1,
  fullyParallel: false,
  timeout: 45_000,
  expect: { timeout: 8_000 },
  reporter: "list",
  use: {
    browserName: "chromium",
    ...(process.platform === "win32" ? { channel: "msedge" } : {}),
    ignoreHTTPSErrors: true, // Disposable self-signed loopback fixture only.
    trace: "off", // Capability URLs/test cookies must not be retained in traces.
    screenshot: "off",
    video: "off",
  },
});
