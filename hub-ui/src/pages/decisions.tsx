import { link } from "../router";
import { answeredBy } from "../dashboardText";
import { observationTime } from "../agentStatus";
import type { Hub } from "../hub";
import type { Decision } from "../types";
import { EmptyState, Icon, Modal } from "../ui";
import { DetailLinks, DetailsBlock, ImageViewer, useAttachments } from "../DetailsBlock";
import { attachmentUrl, parseContext, type DetailImage } from "../details";
import { Markdown } from "../markdown";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { canWithdrawDecision } from "../withdrawn";
import { WithdrawDialog } from "./Withdraw";

export const MAX_FREE_TEXT = 4096;
/** Typed answers only when the server says so, and never on protected requests. */
export const allowsFreeText = (decision: Decision) => decision.allow_text === true && !decision.protected;
export const acceptsAnswer = (decision: Decision, answer: string) =>
  decision.options.includes(answer)
  || (allowsFreeText(decision) && answer.trim() !== "" && answer.length <= MAX_FREE_TEXT);
/** An empty custom draft is retainable, but is never a submittable answer. */
export const retainsAnswerDraft = (decision: Decision, answer: string) =>
  decision.options.includes(answer) || (allowsFreeText(decision) && answer.length <= MAX_FREE_TEXT);

interface Draft { revision: number; text: string; selected: boolean }

export function DecisionCard({ hub, decision }: { hub: Hub; decision: Decision }) {
  const choice = hub.decisionChoices[decision.id];
  const current = choice?.revision === decision.revision ? choice.answer : null;
  const [draft, setDraft] = useState<Draft>(() => ({ revision: decision.revision,
    text: current !== null && !decision.options.includes(current) ? current : "",
    selected: current !== null && !decision.options.includes(current) }));
  // The pop-up is a sibling of the card's form, never inside it, so its buttons can't submit the card.
  const [full, setFull] = useState<{ focus: string | null } | null>(null);
  const [withdrawing, setWithdrawing] = useState(false);
  return (
    <>
      <DecisionForm hub={hub} decision={decision} draft={draft} setDraft={setDraft}
        onOpenFull={(image) => setFull({ focus: image?.id ?? null })}
        onWithdraw={canWithdrawDecision(decision) ? () => setWithdrawing(true) : undefined} />
      {full && <QuestionView hub={hub} decision={decision} draft={draft} setDraft={setDraft} focus={full.focus}
        onClose={() => setFull(null)} />}
      {withdrawing && <WithdrawDialog hub={hub} kind="decision" item={decision} onClose={() => setWithdrawing(false)} />}
    </>
  );
}

/** Image columns: one large image, two side by side, four as a 2 x 2 grid, otherwise three across. */
export const galleryColumns = (count: number) => (count <= 1 ? 1 : count === 2 || count === 4 ? 2 : 3);

/** The whole question in a large pop-up: text and links, every image large (click one to zoom), and the answer. */
function QuestionView({ hub, decision, draft, setDraft, focus, onClose }: {
  hub: Hub; decision: Decision; draft: Draft; setDraft: (draft: Draft) => void; focus: string | null; onClose: () => void;
}) {
  const context = parseContext(decision);
  const { images, failed } = useAttachments("decision", decision, !hub.demo, hub.clearPrivateWorkspace);
  const [zoomed, setZoomed] = useState<DetailImage | null>(null);
  const gallery = useRef<HTMLUListElement>(null);
  useEffect(() => {
    if (focus) gallery.current?.querySelector<HTMLElement>(`[data-image="${CSS.escape(focus)}"]`)?.scrollIntoView({ block: "nearest" });
  }, [focus, images]);
  return (
    <Modal title={decision.title} onClose={onClose} size="full">
      <div className={images.length > 0 ? "question-view" : "question-view text-only"}>
        {images.length > 0 && (
          <ul ref={gallery} className="question-gallery" aria-label="Images"
            style={{ "--gallery-columns": galleryColumns(images.length),
              "--gallery-rows": Math.min(2, Math.ceil(images.length / galleryColumns(images.length))) } as CSSProperties}>
            {images.map((image) => (
              <li key={image.id} data-image={image.id}>
                <button type="button" onClick={() => setZoomed(image)} aria-label={`Zoom ${image.title}`}>
                  <img src={attachmentUrl(image.id)} alt={image.title} />
                </button>
                <span>{image.title}</span>
              </li>
            ))}
          </ul>
        )}
        <div className="question-side">
          <div className="item-overline">
            <a className="project-chip" href={link(`/projects/${decision.project}`)}>{hub.projectName(decision.project)}</a>
            <span>REV {decision.revision}</span>
          </div>
          {context?.summary.trim() && <div className="item-summary"><Markdown source={context.summary} /></div>}
          {failed && <p className="panel-footnote">Attached images could not be loaded right now.</p>}
          {context && <DetailLinks links={context.links} />}
          <DecisionForm hub={hub} decision={decision} draft={draft} setDraft={setDraft} full />
        </div>
      </div>
      {zoomed && <ImageViewer image={zoomed} onClose={() => setZoomed(null)} />}
    </Modal>
  );
}

/** The answer form, shared by the card and the full-question view so a choice or typed answer carries across. */
function DecisionForm({ hub, decision, draft, setDraft, onOpenFull, onWithdraw, full = false }: {
  hub: Hub; decision: Decision; draft: Draft; setDraft: (draft: Draft) => void;
  onOpenFull?: (image?: DetailImage) => void; onWithdraw?: () => void; full?: boolean;
}) {
  const choice = hub.decisionChoices[decision.id];
  const current = choice?.revision === decision.revision ? choice.answer : null;
  const other = draft.revision === decision.revision ? draft.text : "";
  const typing = allowsFreeText(decision) && draft.revision === decision.revision && draft.selected;
  return (
    <form className={full ? "decision decision-full" : "decision"} onSubmit={(event) => hub.recordDecision(event, decision)}>
      {!full && <>
        <div className="item-overline">
          <a className="project-chip" href={link(`/projects/${decision.project}`)}>{hub.projectName(decision.project)}</a>
          <span>REV {decision.revision}</span>
          {onOpenFull && <button type="button" className="link-button open-full" onClick={() => onOpenFull()}>Open full view</button>}
        </div>
        <h3>{decision.title}</h3>
        <DetailsBlock entity={decision} kind="decision" load={!hub.demo} onUnauthorized={hub.clearPrivateWorkspace}
          onOpenImage={onOpenFull} />
      </>}
      <fieldset disabled={!hub.writable}>
        <legend className="sr-only">Answer for {decision.title}</legend>
        {decision.options.map((option) => (
          <label className="radio-option" key={option}>
            <input type="radio" name="answer" value={option} required
              checked={!typing && current === option}
              onChange={() => { setDraft({ revision: decision.revision, text: other, selected: false });
                hub.chooseAnswer(decision, option); }} />
            <span>{option}</span>
          </label>
        ))}
        {allowsFreeText(decision) && (
          <div className="free-answer">
            <label className="radio-option">
              <input type="radio" name="answer" value="__other" checked={typing}
                onChange={() => { setDraft({ revision: decision.revision, text: other, selected: true });
                  hub.chooseAnswer(decision, other); }} />
              <span>Type my own answer</span>
            </label>
            {typing && (
              <label className="field">
                <span className="sr-only">Your answer</span>
                <textarea value={other} rows={3} maxLength={MAX_FREE_TEXT} required autoFocus
                  placeholder="Write your answer for the agents"
                  onChange={(event) => { setDraft({ revision: decision.revision, text: event.target.value, selected: true });
                    hub.chooseAnswer(decision, event.target.value); }} />
                <small>{other.length} / {MAX_FREE_TEXT}</small>
              </label>
            )}
          </div>
        )}
        <div className="decision-footer">
          <span>
            {decision.protected ? "Protected request · not an execution grant" : "Records a decision. Does not deploy."}
            {onWithdraw && !full && <> · <button type="button" className="link-button" onClick={onWithdraw}>
              No longer needed? Withdraw</button></>}
          </span>
          <button type="submit" className="dark-button"
            disabled={!hub.writable || current === null || !acceptsAnswer(decision, current)}>
            Record decision <Icon kind="arrow" />
          </button>
        </div>
      </fieldset>
    </form>
  );
}

export function DecisionList({ hub, decisions, emptyNote }: { hub: Hub; decisions: Decision[]; emptyNote?: string }) {
  if (decisions.length === 0)
    return <EmptyState title="Nothing waiting on you.">{emptyNote ?? "New decision requests will appear here."}</EmptyState>;
  return <div className="decision-list">{decisions.map((decision) => <DecisionCard key={decision.id} hub={hub} decision={decision} />)}</div>;
}

export function RecentAnswers({ hub, decisions }: { hub: Hub; decisions: Decision[] }) {
  if (decisions.length === 0) return null;
  return (
    <div className="recent-answers" aria-label="Recent answers">
      <h3>Recent answers</h3>
      {decisions.map((decision) => (
        <div className="recent-answer" key={decision.id}>
          <strong>{decision.title}</strong>
          <small>
            {hub.projectName(decision.project)} · Answered{" "}
            {answeredBy(decision.answered_by, hub.session?.actor)}{" "}
            {observationTime(decision.updated_at) ?? ""}
          </small>
          <p>Answer: {decision.answer}</p>
          {decision.protected && (
            <small>
              This answer did not by itself authorize running anything. Run approval and run status are
              tracked separately.
            </small>
          )}
        </div>
      ))}
    </div>
  );
}

export const openDecisions = (hub: Hub, project?: string) =>
  hub.data.decisions.filter((item) => item.status === "open" && (!project || item.project === project));

/** Answered decisions are history, never waiting work. */
export const answeredDecisions = (hub: Hub, project?: string, limit = 3) =>
  hub.data.decisions
    .filter((item) => item.status === "answered" && (!project || item.project === project))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .slice(0, limit);
