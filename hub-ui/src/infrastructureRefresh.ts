import { useCallback, useEffect, useRef, useState } from "react";

import { HubApiError } from "./api";

export function inventoryUnavailable(error: unknown): boolean {
  return error instanceof HubApiError && error.code !== "device_not_found"
    && (error.status === 403 || error.status === 404
      || (error.status === 503 && error.code === "infrastructure_not_configured"));
}

export function transientRefreshError(error: unknown): boolean {
  return error instanceof HubApiError && !inventoryUnavailable(error)
    && (error.status === 0 || error.status >= 500);
}

const REFRESH_MS = 30_000;
const READ_TIMEOUT_MS = 15_000;

type RunRead = <T>(read: (signal: AbortSignal) => Promise<T>, onValue: (value: T) => void,
  onError: (error: unknown) => void) => () => void;

/** Poll only while visible and idle. Explicit reads may replace a superseded query. */
export function useInfrastructureRefresh(enabled: boolean): [number, () => void, RunRead] {
  const [attempt, setAttempt] = useState(0);
  const inFlight = useRef<AbortController | null>(null);
  const refresh = useCallback(() => setAttempt(value => value + 1), []);
  const runRead = useCallback<RunRead>((read, onValue, onError) => {
    const controller = new AbortController();
    inFlight.current = controller;
    const finish = () => {
      window.clearTimeout(timeout);
      if (inFlight.current !== controller) return false;
      inFlight.current = null;
      return true;
    };
    const cancel = () => { finish(); controller.abort(); };
    const timeout = window.setTimeout(() => {
      if (!finish()) return;
      controller.abort();
      onError(new HubApiError(0, "infrastructure_read_timeout"));
    }, READ_TIMEOUT_MS);
    void Promise.resolve().then(() => {
      controller.signal.throwIfAborted();
      return read(controller.signal);
    }).then(value => { if (finish()) onValue(value); }, error => { if (finish()) onError(error); });
    return cancel;
  }, []);
  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && !inFlight.current) refresh();
    }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [enabled, refresh]);
  return [attempt, refresh, runRead];
}
