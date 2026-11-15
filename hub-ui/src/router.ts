/**
 * Dependency-free client router. Real paths (/projects/treedar) when the server serves them,
 * else #/ hashes. Editors can guard leaving, and a cancelled Back/Forward restores history.
 */
import { useEffect, useState } from "react";

export const PROJECT_TABS = ["overview", "work", "decisions", "notes", "plans", "labeling", "activity"] as const;
export type ProjectTab = (typeof PROJECT_TABS)[number];

export type Route =
  | { page: "home" }
  | { page: "inbox" }
  | { page: "work" }
  | { page: "projects" }
  | { page: "project"; id: string; tab: ProjectTab }
  | { page: "labels"; project: string; set: string }
  | { page: "review"; project: string; id: string }
  | { page: "notes" }
  | { page: "planning" }
  | { page: "agents" }
  | { page: "agent-installations" }
  | { page: "agent-map" }
  | { page: "staging" }
  | { page: "staging-queues" }
  | { page: "staging-resources" }
  | { page: "infrastructure" }
  | { page: "device"; id: string }
  // Demo-only until the orchestration backend exists; App gates it with orchestrationEnabled().
  | { page: "orchestration" }
  | { page: "orchestration-request"; id: string };

const projectId = /^[a-z0-9][a-z0-9-]{0,63}$/;
const setId = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const requestId = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const reviewId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// Section anchors from the single-page dashboard keep working as links.
const legacy: Record<string, Route> = {
  decisions: { page: "inbox" },
  work: { page: "work" },
  notes: { page: "notes" },
  planning: { page: "planning" },
  agents: { page: "agents" },
  staging: { page: "staging" },
  // The validation queue now lives on the staging queues sub-page.
  "software-queue": { page: "staging-queues" },
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

/** Parse a path ("/projects/x") or hash ("#/projects/x"). Unknown input is home, never a guess. */
export function parseRoute(value: string): Route {
  const raw = value.replace(/^#/, "");
  if (legacy[raw]) return legacy[raw];
  const parts = raw.replace(/^\//, "").split(/[?#]/)[0].split("/").filter(Boolean);
  switch (parts[0]) {
    case undefined: return { page: "home" };
    case "inbox": return parts.length === 1 ? { page: "inbox" } : { page: "home" };
    case "work": return { page: "work" };
    case "notes": return { page: "notes" };
    case "planning": return { page: "planning" };
    case "agents":
      return parts.length === 2 && parts[1] === "installations" ? { page: "agent-installations" }
        : parts.length === 2 && parts[1] === "map" ? { page: "agent-map" } : { page: "agents" };
    case "staging":
      if (parts.length === 2 && parts[1] === "queues") return { page: "staging-queues" };
      if (parts.length === 2 && parts[1] === "resources") return { page: "staging-resources" };
      return { page: "staging" };
    case "orchestration":
      if (parts.length === 1) return { page: "orchestration" };
      return parts.length === 2 && requestId.test(parts[1]) ? { page: "orchestration-request", id: parts[1] } : { page: "orchestration" };
    case "infrastructure":
      if (parts.length === 1) return { page: "infrastructure" };
      return parts.length === 2 && projectId.test(parts[1]) ? { page: "device", id: parts[1] } : { page: "infrastructure" };
    case "projects": {
      if (parts.length === 1 || !projectId.test(parts[1])) return { page: "projects" };
      if (parts.length === 2) return { page: "project", id: parts[1], tab: "overview" };
      if (parts.length === 3 && (PROJECT_TABS as readonly string[]).includes(parts[2]))
        return { page: "project", id: parts[1], tab: parts[2] as ProjectTab };
      if (parts.length === 4 && parts[2] === "labels" && setId.test(parts[3]))
        return { page: "labels", project: parts[1], set: parts[3] };
      if (parts.length === 4 && parts[2] === "reviews" && reviewId.test(parts[3]))
        return { page: "review", project: parts[1], id: parts[3] };
      return { page: "project", id: parts[1], tab: "overview" };
    }
    default: return { page: "home" };
  }
}

export function pathOf(route: Route): string {
  switch (route.page) {
    case "home": return "/";
    case "project": return `/projects/${route.id}${route.tab === "overview" ? "" : "/" + route.tab}`;
    case "labels": return `/projects/${route.project}/labels/${route.set}`;
    case "review": return `/projects/${route.project}/reviews/${route.id}`;
    case "device": return `/infrastructure/${route.id}`;
    case "orchestration-request": return `/orchestration/${route.id}`;
    case "agent-installations": return "/agents/installations";
    case "agent-map": return "/agents/map";
    case "staging-queues": return "/staging/queues";
    case "staging-resources": return "/staging/resources";
    default: return `/${route.page}`;
  }
}

// Path routing turns on only after the server proves it serves client routes.
let pathMode = false;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());

export function href(route: Route): string {
  return pathMode ? pathOf(route) + location.search : "#" + pathOf(route);
}

/** In-app link for a literal path ("/inbox"), in whichever mode is active. The query (e.g. ?demo=1) is kept. */
export function link(path: string): string {
  return pathMode ? path + location.search : "#" + path;
}

function isRouteHash(url: { pathname: string; hash: string }): boolean {
  // An explicit #/ route works at any path. Old single-page anchors are routes
  // only at the app root; on a real path #name is that page's fragment.
  return url.hash.startsWith('#/') ||
    ((url.pathname === '/' || url.pathname === '/index.html') && Object.hasOwn(legacy, url.hash.slice(1)));
}

function routeAt(url: { pathname: string; hash: string }): Route {
  return isRouteHash(url) ? parseRoute(url.hash) : parseRoute(url.pathname);
}
const current = () => routeAt(location);
const here = () => location.pathname + location.search + location.hash;

// The entry the router last accepted; a declined leave returns here.
let accepted: { url: string; position: Position } | null = null;
let restoring = false;

/** Go to an in-app URL. Returns false when an open editor keeps the user on this page. */
function go(target: string): boolean {
  const next = new URL(target, location.href);
  const url = next.pathname + next.search + next.hash;
  if (!accepted || restoring) return false;
  if (url === accepted.url) return true;
  if (pathOf(routeAt(next)) !== pathOf(current()) && !canLeave()) return false;
  const nextPosition = { ...accepted.position, index: accepted.position.index + 1 };
  history.pushState(stateAt(nextPosition), "", url);
  accepted = { url: here(), position: nextPosition };
  notify();
  return true;
}

export function navigate(route: Route): boolean {
  return go(href(route));
}

/** One same-origin GET of a client route; HTML back means the server has the fallback. */
export async function detectPathRouting(fetchImpl: typeof fetch = globalThis.fetch): Promise<boolean> {
  try {
    const response = await fetchImpl.call(globalThis, "/projects", {
      method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store", headers: { Accept: "text/html" },
    });
    return response.ok && !!response.headers.get("content-type")?.toLowerCase().startsWith("text/html");
  } catch {
    return false;
  }
}

function enablePathMode() {
  if (pathMode) return;
  pathMode = true;
  // Move an old #/ address onto the real path without adding a history entry.
  if (isRouteHash(location) && !restoring) {
    const value = accepted?.position ?? position() ?? freshPosition();
    const inner = location.hash.startsWith('#/') ? location.hash.slice(1) : '';
    const fragmentAt = inner.indexOf('#');
    const beforeFragment = fragmentAt < 0 ? inner : inner.slice(0, fragmentAt);
    const fragment = fragmentAt < 0 ? '' : inner.slice(fragmentAt);
    const queryAt = beforeFragment.indexOf('?');
    const innerQuery = queryAt < 0 ? '' : beforeFragment.slice(queryAt + 1);
    const query = location.search + (innerQuery ? (location.search ? '&' : '?') + innerQuery : '');
    history.replaceState(stateAt(value), "", pathOf(parseRoute(location.hash)) + query + fragment);
    accepted = { url: here(), position: value };
  }
  notify();
}

export function useRoute(): Route {
  const [route, setRoute] = useState(current);
  useEffect(() => {
    accepted = { url: here(), position: position() ?? freshPosition() };
    history.replaceState(stateAt(accepted.position), "");
    const show = () => {
      setRoute(current());
      // A new page starts at the top, like a normal navigation.
      document.querySelector(".main")?.scrollTo?.({ top: 0 });
      window.scrollTo?.({ top: 0 });
    };
    listeners.add(show);
    // Back/Forward and typed hash changes.
    const update = () => {
      if (!accepted) return;
      const next = position();
      if (restoring) {
        if (here() === accepted.url && next?.generation === accepted.position.generation
            && next.index === accepted.position.index) restoring = false;
        return;
      }
      if (here() === accepted.url) return;
      const known = next?.generation === accepted.position.generation;
      const leaving = pathOf(current()) !== pathOf(routeAt(new URL(accepted.url, location.href)));
      if (leaving && !canLeave()) {
        if (known && next.index !== accepted.position.index) {
          // Restore traversal, not the target entry: a cancelled Back must not
          // overwrite Home, or a later accepted Back could never reach it.
          restoring = true;
          history.go(accepted.position.index - next.index);
        } else {
          // Pre-router/unindexed history has no safe traversal distance. Keep
          // the editor mounted and restore its URL without guessing direction.
          history.replaceState(stateAt(accepted.position), "", accepted.url);
        }
        return;
      }
      accepted = { url: here(), position: known ? next : freshPosition() };
      history.replaceState(stateAt(accepted.position), "");
      show();
    };
    // In-app links navigate without a page load, in either mode.
    const click = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>("a[href]") : null;
      if (!anchor || anchor.hasAttribute("download") || (anchor.target && anchor.target !== "_self")) return;
      const next = new URL(anchor.href, location.href);
      if (next.origin !== location.origin) return;
      const inApp = pathMode
        ? !next.pathname.startsWith("/api/") && !/\.[A-Za-z0-9]+$/.test(next.pathname)
        : next.pathname === location.pathname && next.search === location.search && next.hash.length > 1;
      if (!inApp) return;
      event.preventDefault();
      go(next.href);
    };
    document.addEventListener("click", click);
    window.addEventListener("popstate", update);
    window.addEventListener("hashchange", update);
    if (location.pathname === "/" || location.pathname === "/index.html") {
      void detectPathRouting().then((ok) => { if (ok) enablePathMode(); });
    } else {
      // We were served at a client route, so the server already has the fallback.
      enablePathMode();
    }
    return () => {
      listeners.delete(show);
      document.removeEventListener("click", click);
      window.removeEventListener("popstate", update);
      window.removeEventListener("hashchange", update);
    };
  }, []);
  return route;
}
