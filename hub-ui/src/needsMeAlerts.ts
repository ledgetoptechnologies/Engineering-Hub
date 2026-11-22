import { useCallback, useEffect, useRef, useState } from "react";
import { waitingItemKey, waitingTasks } from "./backlog";
import { link } from "./router";
import type { Overview } from "./types";

const supported = () => window.isSecureContext && typeof window.Notification === "function";

/** Track only identities, not question text. Permission and opt-in are separate. */
export function useNeedsMeAlerts() {
  const previous = useRef<Set<string> | null>(null);
  const active = useRef<Notification | null>(null);
  const enabled = useRef(false);
  const generation = useRef(0);
  const [state, setState] = useState<"off" | "on" | "denied" | "unavailable">("off");

  const close = useCallback(() => {
    active.current?.close();
    active.current = null;
  }, []);

  const reset = useCallback(() => {
    generation.current++;
    enabled.current = false;
    previous.current = null;
    close();
    setState("off");
  }, [close]);

  useEffect(() => () => { generation.current++; enabled.current = false; close(); }, [close]);

  const toggle = useCallback(async () => {
    if (enabled.current) {
      generation.current++;
      enabled.current = false;
      close();
      setState("off");
      return;
    }
    const attempt = ++generation.current;
    if (!supported()) { setState("unavailable"); return; }
    try {
      const permission = Notification.permission === "default"
        ? await Notification.requestPermission() : Notification.permission;
      if (attempt !== generation.current) return;
      enabled.current = permission === "granted";
      setState(enabled.current ? "on" : "denied");
    } catch {
      if (attempt === generation.current) setState("unavailable");
    }
  }, [close]);

  const reconcile = useCallback((overview: Overview, allowAlert: boolean) => {
    const ids = new Set([
      ...overview.decisions.filter(item => item.status === "open").map(item => `decision:${item.id}`),
      ...waitingTasks(overview.tasks).map(item => `task:${waitingItemKey(item)}`),
    ]);
    const before = previous.current;
    previous.current = ids;
    if (!allowAlert || !before || !enabled.current || !supported()
        || Notification.permission !== "granted"
        || (document.visibilityState === "visible" && document.hasFocus())
        || !Array.from(ids).some(id => !before.has(id))) return;
    try {
      close();
      const notification = new Notification("Engineering Hub needs your attention", {
        body: "Open Engineering Hub to view your inbox.", tag: "engineering-hub-needs-me",
      });
      active.current = notification;
      notification.onclick = () => {
        close();
        window.focus();
        window.location.assign(link("/inbox"));
      };
    } catch {
      enabled.current = false;
      setState("unavailable");
    }
  }, [close]);

  return { state, reset, toggle, reconcile };
}
