/**
 * Who's on what: open tasks grouped by their recorded owner. Built only from the task list the
 * Hub already returns, so it shows ownership and task state, never reviewer assignment or check
 * results (tasks don't carry them). Each paired installation is its own owner, not its family.
 */
import { relativeAge } from "../dashboardText";
import type { Hub } from "../hub";
import type { Task } from "../types";
import { actorAvatar, actorName } from "../actors";

export const UNCLAIMED = "unclaimed";
const RECENT_MS = 7 * 24 * 60 * 60 * 1000;
/** Clock skew allowed between the Hub and this browser, matching relativeAge (which shows such a time as "just now"). */
export const CLOCK_SKEW_MS = 30_000;

export interface OwnerRow {
  /** The exact recorded owner, or UNCLAIMED. */
  key: string;
  owner: string | null;
  family: string | null;
  notStarted: number;
  working: number;
  inReview: number;
  /** Passed review within the last seven days, by when the task entered that state. */
  passedRecently: number;
  /** Earliest valid state time among open tasks, at most CLOCK_SKEW_MS ahead of now; null when there is none. */
  oldestOpenSince: string | null;
  open: number;
}

const stamp = (value: string | null | undefined) => {
  const time = value ? Date.parse(value) : NaN;
  return Number.isFinite(time) ? time : null;
};

export function ownershipRows(tasks: Task[], now = Date.now()): OwnerRow[] {
  const rows = new Map<string, OwnerRow>();
  for (const task of tasks) {
    if (task.status === "withdrawn") continue;
    const key = task.owner ?? UNCLAIMED;
    let row = rows.get(key);
    if (!row) {
      row = { key, owner: task.owner, family: task.owner_family ?? null, notStarted: 0, working: 0, inReview: 0,
        passedRecently: 0, oldestOpenSince: null, open: 0 };
      rows.set(key, row);
    }
    // A time more than CLOCK_SKEW_MS in the future can't be how long something has waited, so it
    // counts as unknown. Within that skew it's treated as just now, as relativeAge does.
    const recorded = stamp(task.state_since);
    const since = recorded !== null && recorded <= now + CLOCK_SKEW_MS ? recorded : null;
    if (task.status === "verified") {
      if (since !== null && now - since <= RECENT_MS) row.passedRecently++;
      continue;
    }
    if (task.status === "assigned") row.notStarted++;
    else if (task.status === "claimed") row.working++;
    else row.inReview++;
    row.open++;
    const oldest = stamp(row.oldestOpenSince);
    if (since !== null && (oldest === null || since < oldest)) row.oldestOpenSince = task.state_since!;
  }
  // Busiest first; nobody's work last, so owned work leads.
  return [...rows.values()].filter((row) => row.open > 0 || row.passedRecently > 0)
    .sort((a, b) => Number(a.key === UNCLAIMED) - Number(b.key === UNCLAIMED) || b.open - a.open
      || b.passedRecently - a.passedRecently || a.key.localeCompare(b.key));
}

export function WorkOwnership({ hub, rows, selected, onSelect }: {
  hub: Hub; rows: OwnerRow[]; selected: string; onSelect: (key: string) => void;
}) {
  if (rows.length === 0) return null;
  const name = (row: OwnerRow) => row.owner
    ? actorName(row.owner, { family: row.family, viewer: hub.session?.actor }) : "Unclaimed";
  return (
    <section className="panel work-ownership" aria-label="Who's on what">
      <div className="work-ownership-head">
        <h3>Who's on what</h3>
        {selected !== "all" && <button type="button" className="text-button" onClick={() => onSelect("all")}>Show everyone</button>}
      </div>
      <ul className="work-ownership-list">
        {rows.map((row) => {
          const avatar = actorAvatar(row.owner, row.family);
          const age = relativeAge(row.oldestOpenSince);
          const counts = [
            row.working && `${row.working} working`,
            row.inReview && `${row.inReview} in review`,
            row.notStarted && `${row.notStarted} not started`,
            row.passedRecently && `${row.passedRecently} passed review in the last 7 days`,
          ].filter(Boolean).join(" · ");
          return <li key={row.key}>
            <button type="button" className={selected === row.key ? "work-owner selected" : "work-owner"}
              aria-pressed={selected === row.key} onClick={() => onSelect(selected === row.key ? "all" : row.key)}>
              <span className={`mini-avatar ${avatar.className}`} aria-hidden="true">{avatar.letter}</span>
              <span className="work-owner-text">
                <strong>{name(row)}</strong>
                <small>{counts}</small>
                {row.open > 0 && <small>{age ? `Oldest open item: in its state since ${age}` : "Time in state not available"}</small>}
              </span>
            </button>
          </li>;
        })}
      </ul>
      <p className="panel-footnote">Counts come from each task's recorded owner and state. "Passed review in the last 7 days" uses when
        a task entered that state; tasks without a valid recorded time aren't counted there. This summary doesn't show who is
        assigned to review or any test results; tasks don't track those. A recorded review appears on the task itself.</p>
    </section>
  );
}
