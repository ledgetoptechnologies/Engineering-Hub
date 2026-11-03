/**
 * Drill-down sidebar. The current section opens to show its sub-pages; a disclosure button opens
 * or closes any section. Sections someone opened by hand are remembered on this device only.
 */
import { useEffect, useState } from "react";
import { href, link, type Route } from "./router";
import { navKey, PROJECT_SUBPAGES, projectPlace, sectionOf, type NavItem } from "./navModel";
import { Icon } from "./ui";

const storageKey = "engineeringHub.navOpen";

function readPinned(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(storageKey) ?? "[]");
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").slice(0, 50) : [];
  } catch {
    return [];
  }
}

function writePinned(value: string[]) {
  try { localStorage.setItem(storageKey, JSON.stringify(value)); } catch { /* storage unavailable: keep it in memory */ }
}

/**
 * Open state for disclosure groups. The active group is open unless closed by hand while it stays
 * active; any other group is open only when someone opened it.
 */
function useDisclosure(active: string, place: string) {
  const [pinned, setPinned] = useState<string[]>(readPinned);
  const [closedActive, setClosedActive] = useState<string | null>(null);
  // Moving to another page shows where you are again.
  useEffect(() => setClosedActive(null), [active, place]);
  const isOpen = (id: string) => id === active ? closedActive !== id : pinned.includes(id);
  const toggle = (id: string) => {
    if (id === active) { setClosedActive(isOpen(id) ? id : null); return; }
    const next = pinned.includes(id) ? pinned.filter((item) => item !== id) : [...pinned, id];
    setPinned(next);
    writePinned(next);
  };
  return { isOpen, toggle };
}

const domId = (id: string) => "nav-sub-" + id.replace(/[^a-z0-9-]/gi, "-");

function Disclosure({ id, label, open, onToggle }: { id: string; label: string; open: boolean; onToggle: () => void }) {
  return (
    <button type="button" className={open ? "nav-disclosure open" : "nav-disclosure"} aria-expanded={open}
      aria-controls={domId(id)} aria-label={`${label} pages`} onClick={onToggle}>
      <Icon kind="chevron" size={14} />
    </button>
  );
}

export function WorkspaceNav({ items, route }: { items: NavItem[]; route: Route }) {
  const key = navKey(route);
  const { isOpen, toggle } = useDisclosure(sectionOf(key), key);
  return (
    <nav aria-label="Workspace navigation">
      {items.map((item) => {
        const exact = key === item.id;
        const inSection = sectionOf(key) === item.id;
        const children = item.children ?? [];
        const open = children.length > 0 && isOpen(item.id);
        return (
          <div key={item.id} className="nav-group">
            <div className="nav-row">
              <a className={inSection ? "nav-item selected" : "nav-item"} href={href(item.route)}
                aria-current={exact ? "page" : undefined}>
                {item.icon && <Icon kind={item.icon} />} {item.label}
                {item.count !== undefined && item.count > 0 && (
                  <span className={`nav-count ${item.attention ? "attention" : ""}`}>{item.count}</span>
                )}
              </a>
              {children.length > 0 && <Disclosure id={item.id} label={item.label} open={open} onToggle={() => toggle(item.id)} />}
            </div>
            {children.length > 0 && (
              <ul className="nav-sub" id={domId(item.id)} hidden={!open}>
                {children.map((child) => (
                  <li key={child.id}>
                    <a className={key === child.id ? "nav-subitem current" : "nav-subitem"} href={href(child.route)}
                      aria-current={key === child.id ? "page" : undefined}>{child.label}</a>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </nav>
  );
}

export function ProjectNav({ projects, route }: { projects: { id: string; name: string }[] | null; route: Route }) {
  const current = route.page === "project" ? route.id : route.page === "labels" || route.page === "review" ? route.project : null;
  const { isOpen, toggle } = useDisclosure(current ? "project:" + current : "", JSON.stringify(route));
  return (
    <nav aria-label="Projects" className="project-nav">
      {projects?.map((item) => {
        const place = projectPlace(route, item.id);
        const id = "project:" + item.id;
        // Only the selected project drills down, so the list stays short.
        const selected = place !== null;
        const open = selected && isOpen(id);
        return (
          <div key={item.id} className="nav-group">
            <div className="nav-row">
              <a href={link(`/projects/${item.id}`)} className={selected ? "project-link current" : "project-link"}
                aria-current={place === "overview" && route.page === "project" ? "page" : undefined}>
                <span className="project-square" />
                {item.name}
              </a>
              {selected && <Disclosure id={id} label={item.name} open={open} onToggle={() => toggle(id)} />}
            </div>
            {selected && (
              <ul className="nav-sub" id={domId(id)} hidden={!open}>
                {PROJECT_SUBPAGES.map((sub) => (
                  <li key={sub.tab}>
                    <a className={place === sub.tab ? "nav-subitem current" : "nav-subitem"} href={link(`/projects/${item.id}/${sub.tab}`)}
                      aria-current={place === sub.tab && route.page === "project" ? "page" : undefined}>{sub.label}</a>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      }) ?? <span className="sidebar-help">Sign in to load projects.</span>}
    </nav>
  );
}
