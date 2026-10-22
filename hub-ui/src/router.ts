/** Client router: real paths (/projects/treedar) when the server serves them, else #/ hashes. */
import { useEffect, useState } from "react";

export const PROJECT_TABS = ["overview", "work", "decisions", "notes", "labeling", "activity"] as const;
export type ProjectTab = (typeof PROJECT_TABS)[number];

export type Route =
  | { page: "home" }
  | { page: "inbox" }
  | { page: "work" }
  | { page: "projects" }
  | { page: "project"; id: string; tab: ProjectTab }
  | { page: "labels"; project: string; set: string }
  | { page: "notes" }
  | { page: "agents" }
  | { page: "staging" };

const projectId = /^[a-z0-9][a-z0-9-]{0,63}$/;
const setId = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
// Section anchors from the single-page dashboard keep working as links.
const legacy: Record<string, Route> = {
  decisions: { page: "inbox" },
  work: { page: "work" },
  notes: { page: "notes" },
  agents: { page: "agents" },
  staging: { page: "staging" },
  "software-queue": { page: "staging" },
};

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
    case "agents": return { page: "agents" };
    case "staging": return { page: "staging" };
    case "projects": {
      if (parts.length === 1 || !projectId.test(parts[1])) return { page: "projects" };
      if (parts.length === 2) return { page: "project", id: parts[1], tab: "overview" };
      if (parts.length === 3 && (PROJECT_TABS as readonly string[]).includes(parts[2]))
        return { page: "project", id: parts[1], tab: parts[2] as ProjectTab };
      if (parts.length === 4 && parts[2] === "labels" && setId.test(parts[3]))
        return { page: "labels", project: parts[1], set: parts[3] };
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

export function navigate(route: Route) {
  if (pathMode) {
    history.pushState(null, "", pathOf(route) + location.search);
    notify();
  } else {
    location.hash = pathOf(route);
  }
}

function current(): Route {
  if (location.hash.length > 1) return parseRoute(location.hash);
  return parseRoute(location.pathname);
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
  if (location.hash.length > 1) history.replaceState(null, "", pathOf(parseRoute(location.hash)) + location.search);
  notify();
}

export function useRoute(): Route {
  const [route, setRoute] = useState(current);
  useEffect(() => {
    const update = () => {
      setRoute(current());
      window.scrollTo?.({ top: 0 });
    };
    listeners.add(update);
    window.addEventListener("hashchange", update);
    window.addEventListener("popstate", update);
    // In path mode, plain in-app links navigate without a page load.
    const onClick = (event: MouseEvent) => {
      if (!pathMode || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = (event.target as Element | null)?.closest?.("a[href^='/']");
      if (!(anchor instanceof HTMLAnchorElement) || anchor.target || anchor.hasAttribute("download")) return;
      const url = new URL(anchor.href);
      if (url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
      event.preventDefault();
      history.pushState(null, "", url.pathname);
      update();
    };
    document.addEventListener("click", onClick);
    if (location.pathname === "/" || location.pathname === "/index.html") {
      void detectPathRouting().then((ok) => { if (ok) enablePathMode(); });
    } else {
      // We were served at a client route, so the server already has the fallback.
      enablePathMode();
    }
    return () => {
      listeners.delete(update);
      window.removeEventListener("hashchange", update);
      window.removeEventListener("popstate", update);
      document.removeEventListener("click", onClick);
    };
  }, []);
  return route;
}
