/**
 * The sidebar's section tree. Each section's own link is its overview page; its children are
 * focused sub-pages. Gates are decided by the caller and passed in, so a hidden page never gets
 * a sub-item either.
 */
import type { ProjectTab, Route } from "./router";
import type { IconKind } from "./ui";

export interface NavItem {
  id: string;
  label: string;
  route: Route;
  icon?: IconKind;
  count?: number;
  attention?: boolean;
  children?: NavItem[];
}

export interface NavOptions {
  /** Global, not per-project: only when the Hub grants this caller infrastructure access (or in demo). */
  infrastructure: boolean;
  /** Fixture-backed preview: demo mode only until the backend exists. */
  orchestration: boolean;
  waiting?: number;
  activeWork?: number;
  attention?: boolean;
  /** The device being viewed, shown as a sub-item under Infrastructure. */
  deviceId?: string | null;
}

export function workspaceNav(options: NavOptions): NavItem[] {
  return [
    { id: "home", route: { page: "home" }, label: "Home", icon: "grid" },
    { id: "inbox", route: { page: "inbox" }, label: "Needs you", icon: "inbox", count: options.waiting, attention: options.attention },
    { id: "work", route: { page: "work" }, label: "Work", icon: "work", count: options.activeWork,
      children: options.orchestration ? [{ id: "work/orchestration", route: { page: "orchestration" }, label: "Orchestration" }] : [] },
    { id: "projects", route: { page: "projects" }, label: "Projects", icon: "folder" },
    { id: "agents", route: { page: "agents" }, label: "Agents", icon: "agents",
      children: [{ id: "agents/installations", route: { page: "agent-installations" }, label: "Installations" }] },
    { id: "staging", route: { page: "staging" }, label: "Staging", icon: "staging",
      children: [
        { id: "staging/queues", route: { page: "staging-queues" }, label: "Queues & sessions" },
        { id: "staging/resources", route: { page: "staging-resources" }, label: "Kept resources" },
      ] },
    ...(options.infrastructure ? [{ id: "infrastructure", route: { page: "infrastructure" } as Route, label: "Infrastructure", icon: "network" as IconKind,
      children: options.deviceId ? [{ id: "infrastructure/device", route: { page: "device", id: options.deviceId } as Route, label: options.deviceId }] : [] }] : []),
  ];
}

/** The id of the deepest workspace item this route belongs to; "" when it has none (notes, planning). */
export function navKey(route: Route): string {
  switch (route.page) {
    case "home": case "inbox": case "work": case "projects": case "agents": case "staging": case "infrastructure":
      return route.page;
    case "orchestration": case "orchestration-request": return "work/orchestration";
    case "project": case "labels": case "review": return "projects/project";
    case "agent-installations": return "agents/installations";
    case "staging-queues": return "staging/queues";
    case "staging-resources": return "staging/resources";
    case "device": return "infrastructure/device";
    default: return "";
  }
}

/** The top-level section a key belongs to ("staging/queues" -> "staging"). */
export const sectionOf = (key: string) => key.split("/")[0];

/** A project's sidebar sub-pages. The project's own link is its overview. */
export const PROJECT_SUBPAGES: { tab: Exclude<ProjectTab, "overview">; label: string }[] = [
  { tab: "decisions", label: "Needs you" },
  { tab: "work", label: "Work" },
  { tab: "notes", label: "Notes" },
  { tab: "plans", label: "Plans & ideas" },
  { tab: "labeling", label: "Labeling" },
  { tab: "activity", label: "Activity" },
];

/** Which project sub-page a route is on: the tab, "overview" (including a review), or null for another project or page. */
export function projectPlace(route: Route, id: string): ProjectTab | null {
  if (route.page === "project" && route.id === id) return route.tab;
  if (route.page === "labels" && route.project === id) return "labeling";
  if (route.page === "review" && route.project === id) return "overview";
  return null;
}
