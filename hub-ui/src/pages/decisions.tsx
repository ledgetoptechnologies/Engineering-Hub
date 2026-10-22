import { link } from "../router";
import { answeredBy } from "../dashboardText";
import { observationTime } from "../agentStatus";
import type { Hub } from "../hub";
import type { Decision } from "../types";
import { EmptyState, Icon } from "../ui";
import { DetailsBlock } from "../DetailsBlock";
import { useState } from "react";

export const MAX_FREE_TEXT = 4096;
/** Typed answers only when the server says so, and never on protected requests. */
export const allowsFreeText = (decision: Decision) => decision.allow_text === true && !decision.protected;
export const acceptsAnswer = (decision: Decision, answer: string) =>
  decision.options.includes(answer)
  || (allowsFreeText(decision) && answer.trim() !== "" && answer.length <= MAX_FREE_TEXT);

export function DecisionCard({ hub, decision }: { hub: Hub; decision: Decision }) {
  const choice = hub.decisionChoices[decision.id];
  const [other, setOther] = useState("");
  const current = choice?.revision === decision.revision ? choice.answer : null;
  const typing = current !== null && !decision.options.includes(current);
  return (
    <form className="decision" onSubmit={(event) => hub.recordDecision(event, decision)}>
      <div className="item-overline">
        <a className="project-chip" href={link(`/projects/${decision.project}`)}>{hub.projectName(decision.project)}</a>
        <span>REV {decision.revision}</span>
      </div>
      <h3>{decision.title}</h3>
      <DetailsBlock entity={decision} kind="decision" load={!hub.demo} onUnauthorized={hub.clearPrivateWorkspace} />
      <fieldset disabled={!hub.writable}>
        <legend className="sr-only">Answer for {decision.title}</legend>
        {decision.options.map((option) => (
          <label className="radio-option" key={option}>
            <input type="radio" name="answer" value={option} required
              checked={current === option}
              onChange={() => hub.chooseAnswer(decision, option)} />
            <span>{option}</span>
          </label>
        ))}
        {allowsFreeText(decision) && (
          <div className="free-answer">
            <label className="radio-option">
              <input type="radio" name="answer" value="__other" checked={typing}
                onChange={() => hub.chooseAnswer(decision, other)} />
              <span>Type my own answer</span>
            </label>
            {typing && (
              <label className="field">
                <span className="sr-only">Your answer</span>
                <textarea value={other} rows={3} maxLength={MAX_FREE_TEXT} required autoFocus
                  placeholder="Write your answer for the agents"
                  onChange={(event) => { setOther(event.target.value); hub.chooseAnswer(decision, event.target.value); }} />
                <small>{other.length} / {MAX_FREE_TEXT}</small>
              </label>
            )}
          </div>
        )}
        <div className="decision-footer">
          <span>
            {decision.protected ? "Protected request · not an execution grant" : "Records a decision. Does not deploy."}
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
