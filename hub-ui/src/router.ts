/** Minimal hash router: no dependency, no history API, works behind any proxy. */
import { useEffect, useState } from "react";

export type Route =
  | { page: "home" }
  | { page: "inbox" }
  | { page: "work" }
  | { page: "projects" }
  | { page: "project"; id: string }
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

export function parseRoute(hash: string): Route {
  const raw = hash.replace(/^#/, "");
  if (legacy[raw]) return legacy[raw];
  const parts = raw.replace(/^\//, "").split("/").filter(Boolean);
  switch (parts[0]) {
    case undefined: return { page: "home" };
    case "inbox": return { page: "inbox" };
    case "work": return { page: "work" };
    case "notes": return { page: "notes" };
    case "agents": return { page: "agents" };
    case "staging": return { page: "staging" };
    case "projects":
      if (parts.length === 1) return { page: "projects" };
      if (parts.length === 2 && projectId.test(parts[1])) return { page: "project", id: parts[1] };
      if (parts.length === 4 && projectId.test(parts[1]) && parts[2] === "labels" && setId.test(parts[3]) && parts[3] !== "..")
        return { page: "labels", project: parts[1], set: parts[3] };
      return { page: "projects" };
    default: return { page: "home" };
  }
}

export function href(route: Route): string {
  if (route.page === "home") return "#/";
  if (route.page === "project") return `#/projects/${route.id}`;
  if (route.page === "labels") return `#/projects/${route.project}/labels/${route.set}`;
  return `#/${route.page}`;
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(location.hash));
  useEffect(() => {
    const update = () => {
      setRoute(parseRoute(location.hash));
      // A new page starts at the top, like a normal navigation.
      document.querySelector(".main")?.scrollTo?.({ top: 0 });
      window.scrollTo?.({ top: 0 });
    };
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return route;
}
