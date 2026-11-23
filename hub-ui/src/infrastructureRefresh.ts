import { useCallback, useEffect, useState } from "react";

import { HubApiError } from "./api";

export function inventoryUnavailable(error: unknown): boolean {
  return error instanceof HubApiError && error.code !== "device_not_found"
    && (error.status === 403 || error.status === 404
      || (error.status === 503 && error.code === "infrastructure_disabled"));
}

export function transientRefreshError(error: unknown): boolean {
  return error instanceof HubApiError && (error.status === 0 || error.status >= 500);
}

const REFRESH_MS = 30_000;

/** Keep polling local to the mounted view; hidden tabs never start a periodic read. */
export function useInfrastructureRefresh(enabled: boolean): [number, () => void] {
  const [attempt, setAttempt] = useState(0);
  const refresh = useCallback(() => setAttempt(value => value + 1), []);
  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [enabled, refresh]);
  return [attempt, refresh];
}
