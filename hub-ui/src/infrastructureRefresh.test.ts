import { describe, expect, it } from "vitest";
import { HubApiError } from "./api";
import { inventoryUnavailable, transientRefreshError } from "./infrastructureRefresh";

describe("Infrastructure refresh error classification", () => {
  it.each([
    [503, "infrastructure_not_configured", true, false],
    [503, "infrastructure_unavailable", false, true],
    [503, "service_unavailable", false, true],
    [502, "bad_gateway", false, true],
    [0, "network_unavailable", false, true],
    [403, "gateway_route_forbidden", true, false],
    [404, "device_not_found", false, false],
    [401, "human_session_required", false, false],
  ] as const)("classifies HTTP %i %s", (status, code, unavailable, transient) => {
    const error = new HubApiError(status, code);
    expect(inventoryUnavailable(error)).toBe(unavailable);
    expect(transientRefreshError(error)).toBe(transient);
  });
});
