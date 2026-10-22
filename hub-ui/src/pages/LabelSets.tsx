import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { HubApiError } from "../api";
import type { Hub } from "../hub";
import {
  imageUrl, listLabelSets, readLabelSet, saveLabel, type LabelItem, type LabelSet, type LabelSetSummary,
} from "../labelSetsApi";
import { Badge, PageHeader, PanelTitle } from "../ui";
import { relativeAge } from "../dashboardText";

const denied = (error: unknown) => error instanceof HubApiError && [401, 403].includes(error.status);

/** A project's labelling sets. Hidden entirely when the server has no such feature yet. */
export function LabelSetsPanel({ hub, project }: { hub: Hub; project: string }) {
  const [sets, setSets] = useState<LabelSetSummary[] | null>(null);
  const [state, setState] = useState<"loading" | "loaded" | "unsupported" | "failed">("loading");
  useEffect(() => {
    if (hub.demo) { setState("unsupported"); return; }
    const controller = new AbortController();
    setState("loading");
    listLabelSets(project, controller.signal).then((value) => {
      if (!controller.signal.aborted) { setSets(value); setState("loaded"); }
    }).catch((error) => {
      if (controller.signal.aborted) return;
      if (denied(error)) { hub.clearPrivateWorkspace(); return; }
      setSets(null);
      setState(error instanceof HubApiError && error.status === 404 ? "unsupported" : "failed");
    });
    return () => controller.abort();
  }, [hub.demo, hub.data, project]); // eslint-disable-line react-hooks/exhaustive-deps
  if (state === "unsupported" || (state === "loaded" && sets?.length === 0)) return null;
  return (
    <section className="panel label-sets" aria-label="Labeling">
      <PanelTitle title="Labeling" count={sets?.length} />
      <p className="panel-description">Sets an agent prepared for you to label. Every choice saves as you go.</p>
      {state === "loading" && <p role="status">Reading labeling sets…</p>}
      {state === "failed" && <p role="alert">Labeling sets are unavailable right now.</p>}
      <div className="label-set-list">
        {sets?.map((set) => {
          const percent = set.item_count ? Math.round((set.labeled_count / set.item_count) * 100) : 0;
          return (
            <a key={set.id} className="label-set-card" href={`#/projects/${project}/labels/${set.id}`}>
              <strong>{set.title}</strong>
              <span className="progress" aria-hidden="true"><span style={{ width: `${percent}%` }} /></span>
              <small>{set.labeled_count} of {set.item_count} labeled · updated {relativeAge(set.updated_at) ?? "—"}</small>
            </a>
          );
        })}
      </div>
    </section>
  );
}

type Show = "all" | "todo" | "done";

export function LabelSetPage({ hub, project, setId, actions }: {
  hub: Hub; project: string; setId: string; actions: React.ReactNode;
}) {
  const [set, setSet] = useState<LabelSet | null>(null);
  const [state, setState] = useState<"loading" | "loaded" | "missing" | "failed">("loading");
  const [order, setOrder] = useState<string[]>([]);
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [show, setShow] = useState<Show>("all");
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [notice, setNotice] = useState<{ kind: "status" | "alert"; text: string } | null>(null);
  const [revision, setRevision] = useState(0);
  const grid = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    setState("loading");
    readLabelSet(setId, controller.signal).then((value) => {
      if (controller.signal.aborted) return;
      if (value.project !== project) { setSet(null); setState("missing"); return; }
      setSet(value);
      // Unlabeled first, fixed for this visit so cards don't jump while labeling.
      setOrder((previous) => previous.length ? previous
        : [...value.items].sort((a, b) => Number(!!a.label) - Number(!!b.label)).map((item) => item.id));
      setState("loaded");
    }).catch((error) => {
      if (controller.signal.aborted) return;
      if (denied(error)) { hub.clearPrivateWorkspace(); return; }
      setSet(null);
      setState(error instanceof HubApiError && error.status === 404 ? "missing" : "failed");
    });
    return () => controller.abort();
  }, [setId, project, revision]); // eslint-disable-line react-hooks/exhaustive-deps

  // Filters come from whatever text fields the agent attached to the items.
  const facets = useMemo(() => {
    if (!set) return [];
    const keys = new Set(set.items.flatMap((item) => Object.keys(item.meta)));
    return [...keys].map((key) => {
      const values = new Set<string>();
      let numeric = false;
      for (const item of set.items) {
        const value = item.meta[key];
        if (typeof value === "number") numeric = true;
        else if (value !== undefined && value !== "") values.add(value);
      }
      return { key, values: [...values].sort(), numeric };
    }).filter((facet) => !facet.numeric && facet.values.length > 1 && facet.values.length <= 30);
  }, [set]);

  if (state !== "loaded" || !set) {
    return (
      <>
        <nav className="crumbs" aria-label="Breadcrumb"><a href="#/projects">Projects</a> <span>/</span>{" "}
          <a href={`#/projects/${project}`}>{hub.projectName(project)}</a></nav>
        <PageHeader eyebrow="LABELING" title={state === "missing" ? "Labeling set not found" : "Labeling"}>{actions}</PageHeader>
        <section className="panel">
          <p role={state === "failed" ? "alert" : "status"}>
            {state === "loading" ? "Reading labeling set…" : state === "missing"
              ? "This labeling set isn't available for this project." : "The labeling set couldn't be read. Use Refresh to retry."}
          </p>
        </section>
      </>
    );
  }

  const byId = new Map(set.items.map((item) => [item.id, item]));
  const visible = order.map((itemId) => byId.get(itemId)).filter((item): item is LabelItem => !!item)
    .filter((item) => Object.entries(filters).every(([key, value]) => !value || String(item.meta[key]) === value))
    .filter((item) => show === "all" || (show === "todo" ? !item.label : !!item.label));
  const counts = Object.fromEntries(set.choices.map((choice) => [choice.key,
    set.items.filter((item) => item.label?.choice === choice.key).length]));
  const done = set.items.filter((item) => item.label).length;
  const canWrite = hub.writable && !!hub.session?.csrf_token;

  async function choose(item: LabelItem, choice: string) {
    if (!set || !canWrite || pending[item.id]) return;
    const next = item.label?.choice === choice ? null : choice;
    setPending((previous) => ({ ...previous, [item.id]: true }));
    try {
      const saved = await saveLabel(set.id, item, next, hub.session!.csrf_token!, set.choices);
      setSet((current) => current && { ...current, items: current.items.map((entry) => entry.id === saved.id ? saved : entry) });
      setNotice(null);
    } catch (error) {
      if (denied(error)) { hub.clearPrivateWorkspace(); return; }
      if (error instanceof HubApiError && error.status === 409) {
        setNotice({ kind: "alert", text: "Someone else changed that item. The set was reloaded; check it and choose again." });
        setRevision((value) => value + 1);
      } else {
        setNotice({ kind: "alert", text: "That choice wasn't saved. Check your connection and try again." });
      }
    } finally {
      setPending((previous) => { const copy = { ...previous }; delete copy[item.id]; return copy; });
    }
  }

  function onKey(event: KeyboardEvent<HTMLElement>, item: LabelItem, index: number) {
    const number = Number(event.key);
    if (!Number.isInteger(number) || number < 1 || number > set!.choices.length) return;
    event.preventDefault();
    void choose(item, set!.choices[number - 1].key);
    const cards = grid.current?.querySelectorAll<HTMLElement>(".label-card");
    cards?.[Math.min(index + 1, cards.length - 1)]?.focus();
  }

  return (
    <>
      <nav className="crumbs" aria-label="Breadcrumb"><a href="#/projects">Projects</a> <span>/</span>{" "}
        <a href={`#/projects/${project}`}>{hub.projectName(project)}</a> <span>/</span> {set.title}</nav>
      <PageHeader eyebrow="LABELING" title={set.title} subtitle={set.instructions || undefined}>{actions}</PageHeader>
      <div className="label-toolbar">
        <div className="label-tally" aria-label="Progress">
          <strong>{done}</strong><span>/{set.items.length} labeled</span>
          {set.choices.map((choice, index) => (
            <Badge key={choice.key} tone={`choice-${index}`}>{choice.label} {counts[choice.key]}</Badge>
          ))}
        </div>
        <div className="chip-row" role="group" aria-label="Show">
          {(["all", "todo", "done"] as Show[]).map((value) => (
            <button key={value} className={show === value ? "chip selected" : "chip"} onClick={() => setShow(value)}>
              {value === "all" ? "All" : value === "todo" ? "To do" : "Done"}{" "}
              <span>{value === "all" ? set.items.length : value === "todo" ? set.items.length - done : done}</span>
            </button>
          ))}
        </div>
        {facets.map((facet) => (
          <label className="select-field" key={facet.key}>
            <span className="sr-only">Filter by {facet.key}</span>
            <select aria-label={`Filter by ${facet.key}`} value={filters[facet.key] ?? ""}
              onChange={(event) => setFilters((previous) => ({ ...previous, [facet.key]: event.target.value }))}>
              <option value="">{facet.key}: all</option>
              {facet.values.map((value) => <option key={value} value={value}>{facet.key}: {value}</option>)}
            </select>
          </label>
        ))}
      </div>
      {notice && <p role={notice.kind} className={notice.kind === "alert" ? "notice error" : "notice"}>{notice.text}</p>}
      {!canWrite && <p className="notice" role="status">Refresh the workspace to make choices; this view is read-only right now.</p>}
      <p className="panel-footnote label-keyhint">Keyboard: focus a card and press {set.choices.map((choice, index) => `${index + 1} ${choice.label}`).join(", ")}. Choosing the same answer again clears it.</p>
      <div className="label-grid" ref={grid} aria-label="Items to label">
        {visible.length === 0 && <p className="empty-inline">Nothing matches these filters.</p>}
        {visible.map((item, index) => (
          <article key={item.id} className={`label-card ${item.label ? "labeled" : ""}`} tabIndex={0}
            aria-label={`Item ${item.id}${item.label ? `, labeled ${set.choices.find((c) => c.key === item.label!.choice)?.label}` : ", not labeled"}`}
            onKeyDown={(event) => onKey(event, item, index)}>
            <img src={imageUrl(set.id, item.id)} alt={`Item ${item.id}`} loading="lazy" decoding="async" />
            <div className="label-meta">
              <span className="label-id">#{item.id}</span>
              {Object.entries(item.meta).map(([key, value]) => <span key={key}>{key} {String(value)}</span>)}
            </div>
            <div className="label-choices" role="group" aria-label={`Choose for item ${item.id}`}>
              {set.choices.map((choice, choiceIndex) => (
                <button key={choice.key} type="button" disabled={!canWrite || !!pending[item.id]}
                  className={`choice choice-${choiceIndex} ${item.label?.choice === choice.key ? "selected" : ""}`}
                  aria-pressed={item.label?.choice === choice.key} onClick={() => void choose(item, choice.key)}>
                  {choice.label}
                </button>
              ))}
            </div>
          </article>
        ))}
      </div>
    </>
  );
}
