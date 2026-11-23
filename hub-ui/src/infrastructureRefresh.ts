import { useCallback, useEffect, useState } from "react";

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
