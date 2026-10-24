/**
 * Reviews an agent has asked a person to answer, and the page for answering one.
 * Questions are shown as plain text with native controls; nothing from the review is run or rendered as HTML.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { HubApiError } from "./api";
import { PointCloudReview } from "./components/PointCloudReview";
import { useComponentSource } from "./ComponentsPanel";
import type { ProjectComponent } from "./componentsApi";
import type { Hub } from "./hub";
import { relativeAge } from "./dashboardText";
import {
  ANSWER_BATCH, ANSWER_MAX, assetPath, listReviews, progressText, readReview, sendAnswers, unsavedAnswers,
  type AnswerRequest, type Review, type ReviewItem, type ReviewPage,
} from "./reviewsApi";
import { DemoReviewStore } from "./reviewsDemo";
import { link, registerRouteLeaveGuard } from "./router";
import { names } from "./ui";

export interface ReviewSource {
  list(project: string, after: string | null, signal: AbortSignal): Promise<ReviewPage>;
  read(id: string, signal: AbortSignal): Promise<Review>;
  answer(review: Review, input: AnswerRequest): Promise<Review>;
}
export function liveReviews(csrf: string | null): ReviewSource {
  return {
    list: (project, after, signal) => listReviews(project, { after }, signal),
    read: (id, signal) => readReview(id, signal),
    answer: (review, input) => { if (!csrf) throw new HubApiError(403, "csrf_required"); return sendAnswers(review, input, csrf); },
  };
}
let demoStore: DemoReviewStore | null = null;
export function demoReviews(store?: DemoReviewStore): ReviewSource {
  const current = () => store ?? (demoStore ??= new DemoReviewStore());
  return {
    list: async (project) => current().list(project),
    read: async (id) => current().read(id),
    answer: async (review, input) => current().answer(review.id, input),
  };
}
function useReviewSource(hub: Hub, given?: ReviewSource): ReviewSource {
  const csrf = hub.session?.csrf_token ?? null;
  return useMemo(() => given ?? (hub.demo ? demoReviews() : liveReviews(csrf)), [given, hub.demo, csrf]);
}

const errorText: Record<string, string> = {
  review_conflict: "Someone saved answers to this review meanwhile. Nothing was overwritten. Reload to see theirs; your unsaved answers stay here.",
  interactive_review_completed: "This review was already finished, so it can't be changed. Reload to see the final answers.",
  review_answers_incomplete: "Every question needs an answer before the review can be finished.",
  review_component_changed: "The component changed after this review was made, so it can't take answers any more. Nothing was saved.",
  review_component_disabled: "The component is paused or retired, so this review can't take answers right now. Nothing was saved.",
  review_not_found: "This review no longer exists.",
  idempotency_conflict: "That save was already used for different answers. Reload and try again.",
  csrf_required: "Your session needs refreshing before answers can be saved.",
  network_unavailable: "The Hub didn't answer. Your answers are still here; nothing was confirmed saved.",
  review_request_too_large: "That's too much to save at once. Nothing was saved; try again.",
};
const denials = new Set(["forbidden", "interactive_forbidden", "interactive_review_forbidden", "review_component_scope", "human_session_required",
  "review_asset_not_registered"]);
const describe = (error: unknown) => {
  if (!(error instanceof HubApiError)) return "That didn't work. Your answers are still here; nothing was confirmed saved.";
  if (denials.has(error.code) || error.status === 403) return "You don't have access to answer this review. Nothing was saved.";
  return errorText[error.code] ?? (error.status === 422 ? "One of the answers isn't accepted. Nothing was saved."
    : "That didn't work. Your answers are still here; nothing was confirmed saved.");
};
const isSignOut = (error: unknown) => error instanceof HubApiError && error.status === 401;

/** On a project's overview: reviews waiting for a person, then finished ones. Hidden when there are none. */
export function ReviewsPanel({ hub, project, source: given }: { hub: Hub; project: string; source?: ReviewSource }) {
  const source = useReviewSource(hub, given);
  const components = useComponentSource(hub);
  const [page, setPage] = useState<ReviewPage | null>(null);
  const [failed, setFailed] = useState(false);
  const [componentNames, setComponentNames] = useState<Record<string, string>>({});
  const clear = useRef(hub.clearPrivateWorkspace);
  clear.current = hub.clearPrivateWorkspace;
  useEffect(() => {
    const controller = new AbortController();
    setPage(null); setFailed(false);
    source.list(project, null, controller.signal).then((value) => { if (!controller.signal.aborted) setPage(value); }).catch((error) => {
      if (controller.signal.aborted) return;
      if (isSignOut(error)) { clear.current(); return; }
      // A Hub without reviews, or no access to them, shows nothing rather than an error.
      if (!(error instanceof HubApiError && (error.code === "reviews_unavailable" || error.status === 403))) setFailed(true);
    });
    components.list(project, null, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setComponentNames(Object.fromEntries(value.items.map((item) => [item.id, item.name]))); })
      .catch(() => undefined);
    return () => controller.abort();
  }, [source, components, project]);
  if (failed) return <section className="panel" aria-label="Reviews"><div className="panel-title"><h2>Reviews</h2></div>
    <p role="alert" className="notice error">Reviews couldn't be loaded. Refresh to try again.</p></section>;
  if (!page?.items.length) return null;
  const sorted = [...page.items].sort((a, b) => Number(a.state === "completed") - Number(b.state === "completed")
    || Date.parse(b.updated_at) - Date.parse(a.updated_at));
  const open = sorted.filter((item) => item.state === "open").length;
  return (
    <section className="panel" aria-label="Reviews">
      <div className="panel-title"><h2>Reviews</h2>{open > 0 && <span className="count attention">{open}</span>}</div>
      <p className="panel-description">Questions an agent asked about this project's components. Open one to answer on any screen.</p>
      <ul className="preview-list">
        {sorted.map((item) => (
          <li key={item.id}><a href={link(`/projects/${project}/reviews/${item.id}`)}>
            <span className="preview-title">{item.title}</span>
            <span className="preview-meta">{item.state === "completed" ? "Finished" : progressText(item.answered_count, item.item_count)}
              {componentNames[item.component_id] ? ` · ${componentNames[item.component_id]}` : ""} · {relativeAge(item.updated_at)}</span>
          </a></li>
        ))}
      </ul>
      {page.next_after && <p className="panel-footnote">Showing the first {page.items.length} reviews.</p>}
    </section>
  );
}

const PAGE_SIZE = 20;

export function ReviewPage({ hub, project, id, actions, source: given }: {
  hub: Hub; project: string; id: string; actions?: React.ReactNode; source?: ReviewSource;
}) {
  const source = useReviewSource(hub, given);
  const components = useComponentSource(hub);
  const [review, setReview] = useState<Review | null>(null);
  const [state, setState] = useState<"loading" | "loaded" | "missing" | "denied" | "unsupported" | "failed">("loading");
  const [component, setComponent] = useState<ProjectComponent | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<"saving" | "finishing" | null>(null);
  const [notice, setNotice] = useState<{ text: string; error: boolean; reload?: boolean } | null>(null);
  const [onlyUnanswered, setOnlyUnanswered] = useState(false);
  const [pageIndex, setPageIndex] = useState(0);
  const [reload, setReload] = useState(0);
  const clear = useRef(hub.clearPrivateWorkspace);
  clear.current = hub.clearPrivateWorkspace;
  // Bumped whenever this page stops being the same review for the same session (or unmounts):
  // late answers from an earlier load or save are then dropped, and no further batch is sent.
  const generation = useRef(0);
  const identity = useRef<{ id: string; project: string; source: ReviewSource } | null>(null);
  const unsaved = review ? unsavedAnswers(review, drafts) : [];
  const unsavedRef = useRef(0); unsavedRef.current = unsaved.length;
  useEffect(() => registerRouteLeaveGuard(() => !unsavedRef.current || window.confirm("Leave without saving your answers?")), []);

  useEffect(() => {
    const previous = identity.current;
    if (!previous || previous.id !== id || previous.project !== project || previous.source !== source) {
      // A new review or session never shows the old one's questions, answers or drafts, even while loading.
      generation.current++;
      identity.current = { id, project, source };
      setReview(null); setComponent(null); setDrafts({}); setNotice(null); setPageIndex(0); setOnlyUnanswered(false); setBusy(null); setState("loading");
    }
    const current = generation.current;
    const controller = new AbortController();
    source.read(id, controller.signal).then((value) => {
      if (controller.signal.aborted || generation.current !== current) return;
      if (value.project !== project) { setReview(null); setState("missing"); return; }
      setReview(value); setState("loaded");
      components.read(value.component_id).then((item) => {
        if (!controller.signal.aborted && generation.current === current) setComponent(item);
      }).catch(() => undefined);
    }).catch((error) => {
      if (controller.signal.aborted || generation.current !== current) return;
      if (isSignOut(error)) { clear.current(); return; }
      setState(!(error instanceof HubApiError) ? "failed" : error.status === 404 && error.code !== "reviews_unavailable" ? "missing"
        : error.code === "reviews_unavailable" ? "unsupported" : error.status === 403 ? "denied" : "failed");
    });
    return () => controller.abort();
  }, [source, components, id, project, reload]);
  useEffect(() => () => { generation.current++; }, []);

  const projectName = hub.projectName(project);
  const crumbs = <nav className="crumbs" aria-label="Breadcrumb"><a href={link("/projects")}>Projects</a> <span>/</span>{" "}
    <a href={link(`/projects/${project}`)}>{projectName}</a> <span>/</span> Review</nav>;
  if (!review) {
    const message = { loading: "Reading the review…", missing: "This review isn't in this project.", denied: "You don't have access to this review.",
      unsupported: "This Hub can't show reviews yet. It needs an update.", failed: "The review couldn't be loaded.", loaded: "" }[state];
    return <>{crumbs}<header className="list-header"><div><p className="eyebrow">REVIEW</p>
      <h1>{state === "loading" ? "Loading review" : "Review unavailable"}<span>.</span></h1></div></header>
      <section className="panel"><p role={state === "loading" ? "status" : undefined}>{message}{" "}
        {state === "failed" && <button type="button" className="link-button" onClick={() => { setState("loading"); setReload((v) => v + 1); }}>Try again</button>}{" "}
        <a className="text-link" href={link(`/projects/${project}`)}>Back to {projectName}</a></p></section></>;
  }

  const open = review.state === "open";
  const canAnswer = open && hub.writable && (hub.demo || (!!hub.session?.csrf_token && !!hub.session.actor?.startsWith("human:")));
  const answered = review.items.filter((item) => review.answers[item.id]).length;
  const visible = onlyUnanswered ? review.items.filter((item) => !review.answers[item.id] || unsaved.some((u) => u.item_id === item.id)) : review.items;
  const pages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  const pageNow = Math.min(pageIndex, pages - 1);
  const shown = visible.slice(pageNow * PAGE_SIZE, (pageNow + 1) * PAGE_SIZE);
  const who = (actor: string) => actor === hub.session?.actor ? "you" : names[actor] ?? (actor.startsWith("human:") ? "a person" : actor);

  async function save(finish: boolean) {
    if (!review || busy) return;
    const started = generation.current;
    const live = () => generation.current === started;
    setBusy(finish ? "finishing" : "saving"); setNotice(null);
    let current = review;
    try {
      // The Hub takes at most 100 answers per save; each batch builds on the revision the last one returned.
      // If the page changed meanwhile, no further batch is sent and nothing is shown for the old review.
      const pending = unsavedAnswers(current, drafts);
      for (let start = 0; start < pending.length; start += ANSWER_BATCH) {
        if (!live()) return;
        current = await source.answer(current, { expected_revision: current.revision, client_request_id: crypto.randomUUID(),
          answers: pending.slice(start, start + ANSWER_BATCH), complete: false });
        if (!live()) return;
        setReview(current);
      }
      if (finish) {
        if (!live()) return;
        current = await source.answer(current, { expected_revision: current.revision, client_request_id: crypto.randomUUID(), answers: [], complete: true });
        if (!live()) return;
        setReview(current);
      }
      setNotice({ text: finish ? "Review finished. The answers are final and the agent can read them." : pending.length === 1 ? "Answer saved." : `${pending.length} answers saved.`, error: false });
    } catch (error) {
      if (!live()) return;
      if (isSignOut(error)) { clear.current(); return; }
      const code = error instanceof HubApiError ? error.code : "";
      setNotice({ text: current !== review && !finish ? `Some answers were saved before this: ${describe(error)}` : describe(error), error: true,
        reload: ["review_conflict", "interactive_review_completed", "idempotency_conflict"].includes(code) });
    } finally { if (live()) setBusy(null); }
  }

  return (
    <>
      {crumbs}
      <header className="list-header review-header">
        <div>
          <p className="eyebrow">REVIEW{component ? ` · ${component.name.toUpperCase()}` : ""}</p>
          <h1>{review.title}<span>.</span></h1>
          <p className="review-meta">
            <span className={`status-pill ${open ? "info" : "neutral"}`}>{open ? "Open" : "Finished"}</span>
            <span>Asked by {who(review.created_by)} {relativeAge(review.created_at)}</span>
          </p>
        </div>
        <div className="heading-actions">{actions}</div>
      </header>
      <div className="review-progress" aria-label="Progress">
        <progress max={review.items.length} value={answered} aria-label={progressText(answered, review.items.length)} />
        <span>{progressText(answered, review.items.length)}</span>
      </div>
      {!open && <p className="notice">This review is finished. The answers are final.</p>}
      {open && !canAnswer && <p className="notice">You can read this review, but only a signed-in person can answer it.</p>}
      {review.items.length > PAGE_SIZE || answered > 0 ? <div className="review-filter">
        <label><input type="checkbox" checked={onlyUnanswered} onChange={(event) => { setOnlyUnanswered(event.target.checked); setPageIndex(0); }} />
          {" "}Show only unanswered</label></div> : null}
      <ol className="review-items" start={review.items.indexOf(shown[0] ?? review.items[0]) + 1}>
        {shown.map((item) => <ReviewQuestion key={`${review.id}:${item.id}`} item={item} reviewId={review.id} saved={review.answers[item.id]?.value}
          savedBy={review.answers[item.id] ? who(review.answers[item.id].by) : null} draft={drafts[item.id]} disabled={!canAnswer || !!busy} demo={hub.demo}
          onChange={(value) => setDrafts((current) => ({ ...current, [item.id]: value }))} />)}
        {!shown.length && <li className="panel"><p>Every question has an answer.</p></li>}
      </ol>
      {pages > 1 && <nav className="review-pages" aria-label="Question pages">
        <button type="button" className="secondary" disabled={pageNow === 0} onClick={() => setPageIndex(pageNow - 1)}>Previous</button>
        <span>Page {pageNow + 1} of {pages}</span>
        <button type="button" className="secondary" disabled={pageNow >= pages - 1} onClick={() => setPageIndex(pageNow + 1)}>Next</button>
      </nav>}
      {canAnswer && <div className="review-bar">
        <p role="status">{notice?.error ? "" : notice?.text ?? (unsaved.length ? `${unsaved.length} unsaved answer${unsaved.length === 1 ? "" : "s"}` : "Everything is saved.")}</p>
        <div className="review-actions">
          <button type="button" className="secondary" disabled={!unsaved.length || !!busy} onClick={() => void save(false)}>
            {busy === "saving" ? "Saving…" : "Save answers"}</button>
          <button type="button" className="primary" disabled={!!busy || answered + unsaved.filter((u) => !review.answers[u.item_id]).length < review.items.length}
            onClick={() => void save(true)}>{busy === "finishing" ? "Finishing…" : "Finish review"}</button>
        </div>
      </div>}
      {notice?.error && <p role="alert" className="notice error">{notice.text}{" "}
        {notice.reload && <button type="button" className="link-button" onClick={() => { setNotice(null); setReload((v) => v + 1); }}>Reload</button>}</p>}
      <p className="panel-footnote">Answers record who gave them and when. Saving doesn't start any work; the agent reads the answers.</p>
    </>
  );
}

function ReviewQuestion({ item, reviewId, saved, savedBy, draft, disabled, demo, onChange }: {
  item: ReviewItem; reviewId: string; saved: string | undefined; savedBy: string | null; draft: string | undefined;
  disabled: boolean; demo: boolean; onChange: (value: string) => void;
}) {
  const [showAsset, setShowAsset] = useState(false);
  const value = draft ?? saved ?? "";
  const changed = draft !== undefined && draft.trim() !== "" && draft !== saved;
  return (
    <li className="review-item">
      <fieldset>
        <legend>{item.title}</legend>
        <p className="review-prompt">{item.prompt}</p>
        {/* Answer controls are disabled one by one, never the whole group, so the sample stays viewable on finished reviews. */}
        {item.asset_id && (showAsset
          ? <div className="review-asset">{demo
              // Demo mode makes no Hub requests, so there is no sample to read.
              ? <p role="status">Samples don't load in demo mode.</p> : <PointCloudReview url={assetPath(reviewId, item.asset_id)} />}
              <button type="button" className="link-button" onClick={() => setShowAsset(false)}>Hide sample</button></div>
          // The sample loads only when asked for, so a phone doesn't download every one.
          : <button type="button" className="secondary review-asset-button" onClick={() => setShowAsset(true)}>Show 3D sample</button>)}
        {item.choices ? (
          <div className="review-choices" role="radiogroup" aria-label={item.title}>
            {item.choices.map((choice) => (
              <label key={choice.id} className={`review-choice${value === choice.id ? " selected" : ""}`}>
                <input type="radio" name={`review-${reviewId}-${item.id}`} value={choice.id} checked={value === choice.id} disabled={disabled}
                  onChange={() => onChange(choice.id)} />
                <span>{choice.label}</span>
              </label>
            ))}
          </div>
        ) : (
          <label className="field">
            <span className="field-label">Your answer</span>
            <textarea rows={3} maxLength={ANSWER_MAX} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)} />
            <small>{value.length} / {ANSWER_MAX}</small>
          </label>
        )}
        <p className="review-saved">{changed ? "Not saved yet" : saved !== undefined ? `Saved${savedBy ? ` by ${savedBy}` : ""}` : "No answer yet"}</p>
      </fieldset>
    </li>
  );
}
