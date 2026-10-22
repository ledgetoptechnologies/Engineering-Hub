/** Dependency-free hash router with document-local editor guards and history restoration. */
import { useEffect, useState } from "react";

export type Route =
  | { page: "home" }
  | { page: "inbox" }
  | { page: "work" }
  | { page: "projects" }
  | { page: "project"; id: string }
  | { page: "notes" }
  | { page: "planning" }
  | { page: "agents" }
  | { page: "staging" };

const projectId = /^[a-z0-9][a-z0-9-]{0,63}$/;
// Section anchors from the single-page dashboard keep working as links.
const legacy: Record<string, Route> = {
  decisions: { page: "inbox" },
  work: { page: "work" },
  notes: { page: "notes" },
  planning: { page: "planning" },
  agents: { page: "agents" },
  staging: { page: "staging" },
  "software-queue": { page: "staging" },
};

// Guards belong to mounted editors only. Removing one (including on auth loss)
// never asks for confirmation or retains the editor's private data.
const leaveGuards = new Set<() => boolean>();
export function registerRouteLeaveGuard(guard: () => boolean): () => void {
  leaveGuards.add(guard);
  return () => { leaveGuards.delete(guard); };
}
function canLeave(): boolean {
  return [...leaveGuards].every((guard) => guard());
}

const historyKey = "__engineeringHubRoute";
interface Position { generation: string; index: number }
function position(): Position | null {
  const value = history.state?.[historyKey];
  return value && typeof value.generation === "string" && Number.isSafeInteger(value.index)
    ? value : null;
}
function stateAt(value: Position) {
  return { ...(history.state && typeof history.state === "object" ? history.state : {}), [historyKey]: value };
}
function freshPosition(): Position {
  // A UI history marker, not an identity or security token.
  return { generation: `${Date.now()}-${Math.random()}`, index: 0 };
}

export function parseRoute(hash: string): Route {
  const raw = hash.replace(/^#/, "");
  if (legacy[raw]) return legacy[raw];
  const parts = raw.replace(/^\//, "").split("/").filter(Boolean);
  switch (parts[0]) {
    case undefined: return { page: "home" };
    case "inbox": return { page: "inbox" };
    case "work": return { page: "work" };
    case "notes": return { page: "notes" };
    case "planning": return { page: "planning" };
    case "agents": return { page: "agents" };
    case "staging": return { page: "staging" };
    case "projects":
      if (parts.length === 1) return { page: "projects" };
      if (parts.length === 2 && projectId.test(parts[1])) return { page: "project", id: parts[1] };
      return { page: "projects" };
    default: return { page: "home" };
  }
}

export function href(route: Route): string {
  return route.page === "home" ? "#/" : route.page === "project" ? `#/projects/${route.id}` : `#/${route.page}`;
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(location.hash));
  useEffect(() => {
    let accepted = { hash: location.hash, position: position() ?? freshPosition() };
    history.replaceState(stateAt(accepted.position), "");
    let restoring = false;
    const show = () => {
      setRoute(parseRoute(location.hash));
      document.querySelector(".main")?.scrollTo?.({ top: 0 });
      window.scrollTo?.({ top: 0 });
    };
    const update = () => {
      const next = position();
      if (restoring) {
        if (location.hash === accepted.hash && next?.generation === accepted.position.generation
            && next.index === accepted.position.index) restoring = false;
        return;
      }
      if (location.hash === accepted.hash) return;
      const known = next?.generation === accepted.position.generation;
      if (href(parseRoute(location.hash)) !== href(parseRoute(accepted.hash)) && !canLeave()) {
        if (known && next.index !== accepted.position.index) {
          // Restore traversal, not the target entry: a cancelled Back must not
          // overwrite Home, or a later accepted Back could never reach it.
          restoring = true;
          history.go(accepted.position.index - next.index);
        } else {
          // Pre-router/unindexed history has no safe traversal distance. Keep
          // the editor mounted and restore its URL without guessing direction.
          history.replaceState(stateAt(accepted.position), "", `${location.pathname}${location.search}${accepted.hash}`);
        }
        return;
      }
      accepted = { hash: location.hash, position: known ? next : freshPosition() };
      history.replaceState(stateAt(accepted.position), "");
      show();
    };
    const click = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>("a[href]") : null;
      if (!anchor || anchor.hasAttribute("download") || (anchor.target && anchor.target !== "_self")) return;
      const next = new URL(anchor.href, location.href);
      if (next.origin !== location.origin || next.pathname !== location.pathname || next.search !== location.search || !next.hash) return;
      event.preventDefault();
      if (restoring || next.hash === accepted.hash) return;
      if (href(parseRoute(next.hash)) !== href(parseRoute(accepted.hash)) && !canLeave()) return;
      const nextPosition = { ...accepted.position, index: accepted.position.index + 1 };
      history.pushState(stateAt(nextPosition), "", next.href);
      accepted = { hash: location.hash, position: nextPosition };
      show();
    };
    document.addEventListener("click", click);
    window.addEventListener("popstate", update);
    window.addEventListener("hashchange", update);
    return () => {
      document.removeEventListener("click", click);
      window.removeEventListener("popstate", update);
      window.removeEventListener("hashchange", update);
    };
  }, []);
  return route;
}
