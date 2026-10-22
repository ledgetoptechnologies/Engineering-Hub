/** Shared presentational pieces. No data access or authority lives here. */
import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

export const names: Record<string, string> = {
  codex: "Codex",
  hermes: "Hermes",
  claude: "Claude",
};

export const pretty = (value: string) =>
  value.replaceAll("_", " ").replace(/^\w/, (c) => c.toUpperCase());

export type IconKind =
  | "grid" | "inbox" | "work" | "folder" | "notes" | "agents" | "staging"
  | "arrow" | "plus" | "refresh" | "search" | "chevron" | "check" | "pause" | "menu";

const paths: Record<IconKind, string> = {
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  inbox: "M3 13h5l1 3h6l1-3h5 M5 5h14l2 8v6H3v-6z",
  work: "M4 7h16v12H4z M9 7V4h6v3 M4 12h16",
  folder: "M3 7V4h6l3 3h9v13H3z",
  notes: "M6 3h9l4 4v14H6z M14 3v5h5 M9 12h7 M9 16h7",
  agents: "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M2 21v-3a5 5 0 0 1 10 0v3 M17 5a3 3 0 0 1 0 6 M16 15a5 5 0 0 1 6 5",
  staging: "M4 4h16v6H4z M4 14h16v6H4z M8 7h.01 M8 17h.01",
  arrow: "M5 12h14 M13 6l6 6-6 6",
  plus: "M12 4v16 M4 12h16",
  refresh: "M20 9a8 8 0 1 0 0 6 M20 3v6h-6",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14 M21 21l-5-5",
  chevron: "M9 6l6 6-6 6",
  check: "M5 12l5 5L20 7",
  pause: "M9 5v14 M15 5v14",
  menu: "M4 6h16 M4 12h16 M4 18h16",
};

export function Icon({ kind, size = 18 }: { kind: IconKind; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={paths[kind]} />
    </svg>
  );
}

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: string }) {
  return (
    <span className={`badge ${tone}`}>
      <span className="dot" />
      {children}
    </span>
  );
}

export function PanelTitle({ number, title, count, children }: {
  number?: string; title: string; count?: number; children?: ReactNode;
}) {
  return (
    <div className="panel-title">
      {number && <span className="panel-number">{number}</span>}
      <h2>{title}</h2>
      {count !== undefined && <span className="count">{count}</span>}
      <span className="panel-action">{children}</span>
    </div>
  );
}

export function PageHeader({ eyebrow, title, subtitle, children }: {
  eyebrow: string; title: string; subtitle?: string; children?: ReactNode;
}) {
  return (
    <section className="page-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}<span>.</span></h1>
        {subtitle && <p className="subtitle">{subtitle}</p>}
      </div>
      {children && <div className="heading-actions">{children}</div>}
    </section>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty-state">
      <span><Icon kind="check" /></span>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}

export function Modal({ title, onClose, children, size = "default" }: {
  title: string; onClose: () => void; children: ReactNode;
  /** "wide" for viewers (images, maps); forms use the default width. */
  size?: "default" | "wide";
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const headingId = useId();
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog ref={ref} className={size === "wide" ? "modal-wide" : undefined} aria-labelledby={headingId} onClose={onClose} onCancel={onClose}>
      <div className="modal-head">
        <h2 id={headingId}>{title}</h2>
        <button type="button" className="icon-button" aria-label="Close dialog" onClick={() => ref.current?.close()}>
          ×
        </button>
      </div>
      {children}
    </dialog>
  );
}
