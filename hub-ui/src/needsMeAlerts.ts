import { useCallback, useEffect, useRef, useState } from "react";
import { waitingItemKey, waitingTasks } from "./backlog";
import { link } from "./router";
import type { Overview } from "./types";

const supported = () => window.isSecureContext && typeof window.Notification === "function";

/** Track only identities, not question text. Permission and opt-in are separate. */
export function useNeedsMeAlerts() {
  const previous = useRef<Set<string> | null>(null);
  const pending = useRef(new Set<string>());
  const active = useRef<Notification | null>(null);
  const enabled = useRef(false);
  const generation = useRef(0);
  const revisionValue = useRef(0);
  const needsBaseline = useRef(false);
  const [state, setState] = useState<"off" | "on" | "denied" | "unavailable">("off");

  const revision = useCallback(() => revisionValue.current, []);

  const close = useCallback(() => {
    active.current?.close();
    active.current = null;
  }, []);

  const reset = useCallback(() => {
    generation.current++;
    revisionValue.current++;
    needsBaseline.current = false;
    enabled.current = false;
    previous.current = null;
    pending.current.clear();
    close();
    setState("off");
  }, [close]);

  useEffect(() => () => {
    generation.current++;
    revisionValue.current++;
    enabled.current = false;
    close();
  }, [close]);

  const toggle = useCallback(async (isFresh: boolean) => {
    if (enabled.current) {
      generation.current++;
      revisionValue.current++;
      needsBaseline.current = false;
      enabled.current = false;
      pending.current.clear();
      close();
      setState("off");
      return;
    }
    const attempt = ++generation.current;
    revisionValue.current++;
    needsBaseline.current = !isFresh;
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

  const reconcile = useCallback((overview: Overview, allowAlert: boolean, expectedRevision: number) => {
    if (expectedRevision !== revisionValue.current) return;
    const ids = new Set([
      ...overview.decisions.filter(item => item.status === "open").map(item => `decision:${item.id}`),
      ...waitingTasks(overview.tasks).map(item => `task:${waitingItemKey(item)}`),
    ]);
    const before = previous.current;
    if (needsBaseline.current) {
      if (!allowAlert) return;
      previous.current = ids;
      needsBaseline.current = false;
      return;
    }
    previous.current = ids;
    if (!before || !enabled.current || !supported() || Notification.permission !== "granted") {
      pending.current.clear();
      return;
    }
    const foreground = document.visibilityState === "visible" && document.hasFocus();
    for (const id of pending.current) if (!ids.has(id)) pending.current.delete(id);
    // Record background arrivals even when this refresh cannot send an alert.
    if (!foreground) for (const id of ids) if (!before.has(id)) pending.current.add(id);
    if (!allowAlert || pending.current.size === 0) return;
    pending.current.clear();
    if (foreground) return;
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

  return { state, reset, toggle, reconcile, revision };
}
