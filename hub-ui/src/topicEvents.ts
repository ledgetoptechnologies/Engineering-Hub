import type { TopicSnapshot } from "./types";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const cursor = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0;
const reasons = new Set(["initial_snapshot", "epoch_changed", "retention_gap", "catchup_limit", "subscriber_overflow"]);
export function registryNotice(kind: string, data: string, topic: string, id = "") {
  const value = JSON.parse(data);
  if (!value || Array.isArray(value) || typeof value !== "object" || value.topic !== topic)
    throw new Error("Invalid topic notice");
  const keys = Object.keys(value).sort().join(",");
  if (kind === "registry.unavailable") {
    if (keys !== "reason,registry_epoch,topic" || value.reason !== "observer_unavailable" ||
        !(value.registry_epoch === null || typeof value.registry_epoch === "string" && uuid.test(value.registry_epoch)))
      throw new Error("Invalid unavailable notice");
  } else {
    if (typeof value.registry_epoch !== "string" || !uuid.test(value.registry_epoch)) throw new Error("Invalid epoch");
    if (kind === "registry.invalidated") {
      if (keys !== "cursor,registry_epoch,snapshot_revision,topic" || !cursor(value.cursor) ||
          typeof value.snapshot_revision !== "string" || !uuid.test(value.snapshot_revision) ||
          id !== `${value.registry_epoch}:${topic}:${value.cursor}`) throw new Error("Invalid pointer");
    } else if (kind === "registry.snapshot_required") {
      if (keys !== "head,reason,registry_epoch,topic" || !cursor(value.head) || !reasons.has(value.reason))
        throw new Error("Invalid recovery notice");
    } else throw new Error("Unknown notice");
  }
  return value;
}

/** One topic only. No cache, timer, polling, or browser automatic reconnect. */
export function watchTopicRegistry(options: {
  topic: string;
  epoch: string;
  fetchSnapshot: () => Promise<TopicSnapshot>;
  accept: (snapshot: TopicSnapshot) => void;
  invalidate: () => void;
  failed: (error?: unknown) => void;
  source?: (url: string) => EventSource;
}) {
  let alive = true;
  let stream: EventSource | null = null;
  let generation = 0;
  let refreshing = false;
  let dirty = false;
  let epoch = options.epoch;
  let lastCursor = -1;
  let consecutiveRecoveries = 0;
  let recovery: { epoch: string; head: number } | null = null;
  const close = () => { generation++; stream?.close(); stream = null; };
  const fail = (error?: unknown) => {
    if (!alive) return;
    alive = false;
    close();
    options.invalidate();
    options.failed(error);
  };
  async function refresh() {
    if (refreshing) { dirty = true; return; }
    refreshing = true;
    try {
      do {
        dirty = false;
        const expected = generation;
        const snapshot = await options.fetchSnapshot();
        if (!alive || expected !== generation) continue;
        if (snapshot.topic !== options.topic || snapshot.event_source !== "canonical_registry" ||
            snapshot.registry_epoch !== epoch) throw new Error("Registry snapshot changed");
        if (dirty) continue;
        options.accept(snapshot);
        if (recovery) {
          const resume = recovery;
          recovery = null;
          connect(resume);
        }
      } while (alive && dirty);
    } catch (error) { fail(error); }
    finally { refreshing = false; }
  }
  function connect(resume?: { epoch: string; head: number }) {
    if (!alive) return;
    const expected = ++generation;
    const query = resume ? `?after=${resume.head}&epoch=${resume.epoch}` : "";
    let next: EventSource;
    try {
      next = (options.source ?? ((url) => new EventSource(url, { withCredentials: true })))(
        `/api/hub/v1/topics/${encodeURIComponent(options.topic)}/events${query}`,
      );
    } catch (error) { fail(error); return; }
    stream = next;
    const current = () => alive && generation === expected && stream === next;
    for (const kind of ["registry.invalidated", "registry.snapshot_required", "registry.unavailable"]) {
      next.addEventListener(kind, (raw) => {
        if (!current()) return;
        try {
          const event = raw as MessageEvent;
          const notice = registryNotice(kind, event.data, options.topic, event.lastEventId);
          if (kind === "registry.unavailable") { fail(); return; }
          if (kind === "registry.snapshot_required") {
            if (resume && notice.reason === "initial_snapshot") throw new Error("Repeated initial recovery");
            if (++consecutiveRecoveries > 3) throw new Error("Recovery did not converge");
            if (notice.reason === "epoch_changed" ? notice.registry_epoch === epoch : notice.registry_epoch !== epoch)
              throw new Error("Recovery epoch mismatch");
            if (notice.reason !== "epoch_changed" && notice.head < lastCursor) throw new Error("Recovery head regressed");
            close();
            epoch = notice.registry_epoch;
            lastCursor = notice.head;
            recovery = { epoch, head: notice.head };
            options.invalidate();
            dirty = true;
            void refresh();
            return;
          }
          if (notice.registry_epoch !== epoch || notice.cursor < lastCursor) throw new Error("Invalid pointer ordering");
          if (notice.cursor === lastCursor) return;
          lastCursor = notice.cursor;
          consecutiveRecoveries = 0;
          options.invalidate();
          void refresh();
        } catch (error) { fail(error); }
      });
    }
    next.onerror = () => { if (current()) fail(); };
  }
  if (!uuid.test(epoch) || !/^[a-z0-9][a-z0-9_-]{0,127}$/.test(options.topic)) fail();
  else connect();
  return () => { alive = false; close(); };
}
