import { answeredBy } from "../dashboardText";
import { observationTime } from "../agentStatus";
import type { Hub } from "../hub";
import type { Decision } from "../types";
import { EmptyState, Icon } from "../ui";

export function DecisionCard({ hub, decision }: { hub: Hub; decision: Decision }) {
  const choice = hub.decisionChoices[decision.id];
  return (
    <form className="decision" onSubmit={(event) => hub.recordDecision(event, decision)}>
      <div className="item-overline">
        <a className="project-chip" href={`#/projects/${decision.project}`}>{hub.projectName(decision.project)}</a>
        <span>REV {decision.revision}</span>
      </div>
      <h3>{decision.title}</h3>
      <fieldset disabled={!hub.writable}>
        <legend className="sr-only">Answer for {decision.title}</legend>
        {decision.options.map((option) => (
          <label className="radio-option" key={option}>
            <input type="radio" name="answer" value={option} required
              checked={choice?.revision === decision.revision && choice?.answer === option}
              onChange={() => hub.chooseAnswer(decision, option)} />
            <span>{option}</span>
          </label>
        ))}
        <div className="decision-footer">
          <span>
            {decision.protected ? "Protected request · not an execution grant" : "Records a decision. Does not deploy."}
          </span>
          <button type="submit" className="dark-button"
            disabled={!hub.writable || choice?.revision !== decision.revision}>
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
