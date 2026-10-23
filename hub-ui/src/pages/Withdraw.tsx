/** Withdrawing a superseded task or question, and the list of what was withdrawn. */
import { useState, type FormEvent } from "react";
import { link } from "../router";
import type { Hub } from "../hub";
import type { Decision, Task } from "../types";
import { Modal, names } from "../ui";
import { WITHDRAW_REASON_MAX } from "../withdrawn";

type Item = Task | Decision;

export function WithdrawDialog({ hub, kind, item, onClose }: {
  hub: Hub; kind: "task" | "decision"; item: Item; onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const [replacement, setReplacement] = useState("");
  const [saving, setSaving] = useState(false);
  // A replacement must be live work of the same kind; any project, since work can move between projects.
  const candidates = (kind === "task" ? hub.data.tasks : hub.data.decisions).filter((other) => other.id !== item.id);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!reason.trim()) return;
    setSaving(true);
    const ok = await hub.withdraw(kind, item, reason.trim(), replacement || null);
    setSaving(false);
    if (ok) onClose();
  }
  const noun = kind === "task" ? "task" : "question";
  return (
    <Modal title={`Withdraw this ${noun}?`} onClose={onClose}>
      <form onSubmit={submit}>
        <p className="withdraw-subject"><strong>{item.title}</strong></p>
        <p className="panel-footnote">
          It leaves the open lists and nobody can {kind === "task" ? "claim or work on" : "answer"} it, but nothing is
          deleted: it stays under Withdrawn with its history and the reason below.
        </p>
        <fieldset disabled={saving || !hub.writable}>
          <label className="field">
            <span className="field-label">Why (required)</span>
            <textarea value={reason} rows={3} maxLength={WITHDRAW_REASON_MAX} required autoFocus
              placeholder="e.g. Replaced by the copy under LedgeTop Processing"
              onChange={(event) => setReason(event.target.value)} />
            <small>{reason.length} / {WITHDRAW_REASON_MAX}</small>
          </label>
          <label className="field">
            <span className="field-label">Replaced by (optional)</span>
            <select value={replacement} onChange={(event) => setReplacement(event.target.value)}>
              <option value="">Nothing replaces it</option>
              {candidates.map((other) => (
                <option key={other.id} value={other.id}>{hub.projectName(other.project)}: {other.title}</option>
              ))}
            </select>
          </label>
          <footer className="dialog-footer">
            <button type="button" className="secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="dark-button" disabled={!reason.trim()}>Withdraw {noun}</button>
          </footer>
        </fieldset>
      </form>
    </Modal>
  );
}

/** Collapsed by default: withdrawn items are history, not work. */
export function WithdrawnList({ hub, kind, project }: { hub: Hub; kind: "task" | "decision"; project?: string }) {
  const all = (kind === "task" ? hub.data.withdrawn?.tasks : hub.data.withdrawn?.decisions) ?? [];
  const items = all.filter((item) => !project || item.project === project);
  if (items.length === 0) return null;
  const lookup = (id: string): Item | undefined => [...hub.data.tasks, ...hub.data.decisions,
    ...(hub.data.withdrawn?.tasks ?? []), ...(hub.data.withdrawn?.decisions ?? [])].find((other) => other.id === id);
  return (
    <details className="withdrawn-list">
      <summary>Withdrawn {kind === "task" ? "tasks" : "questions"} ({items.length})</summary>
      <ul aria-label={`Withdrawn ${kind === "task" ? "tasks" : "questions"}`}>
        {items.map((item) => {
          const replacement = item.superseded_by ? lookup(item.superseded_by) : undefined;
          const by = item.withdrawn_by ? names[item.withdrawn_by as keyof typeof names] ?? "You" : "Unknown";
          return (
            <li key={item.id}>
              <strong>{item.title}</strong>
              <small>
                {hub.projectName(item.project)} · withdrawn by {by}
                {item.withdrawn_at ? ` on ${new Date(item.withdrawn_at).toLocaleDateString()}` : ""}
              </small>
              {item.withdrawn_reason && <p>{item.withdrawn_reason}</p>}
              {item.superseded_by && (
                <p className="replaced-by">Replaced by:{" "}
                  {replacement
                    ? <a href={link(`/projects/${replacement.project}${kind === "task" ? "/work" : "/decisions"}`)}>
                        {hub.projectName(replacement.project)}: {replacement.title}</a>
                    : <code>{item.superseded_by}</code>}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </details>
  );
}
