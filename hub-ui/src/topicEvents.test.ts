import { describe, expect, it, vi } from "vitest";
import { registryNotice, TOPIC_SNAPSHOT_BUDGET, TopicRefreshBudgetExhausted, watchTopicRegistry } from "./topicEvents";
import type { TopicSnapshot } from "./types";

const epoch = "12345678-1234-1234-1234-123456789abc";
const topic = "test-topic";
const snapshot: TopicSnapshot = { topic, items: [], advisory: true, delivery_guaranteed: false,
  snapshot_revision: epoch, registry_epoch: epoch, event_source: "canonical_registry" };
class Source {
  closed = false;
  onerror: (() => void) | null = null;
  handlers: Record<string, (event: unknown) => void> = {};
  constructor(public url: string) {}
  close() { this.closed = true; }
  addEventListener(kind: string, fn: (event: unknown) => void) { this.handlers[kind] = fn; }
  emit(kind: string, body: unknown, id = "") { this.handlers[kind]?.({ data: JSON.stringify(body), lastEventId: id }); }
  pointer(cursor: number) { this.emit("registry.invalidated", { topic, registry_epoch: epoch, snapshot_revision: epoch, cursor }, `${epoch}:${topic}:${cursor}`); }
}
function setup(fetchSnapshot = vi.fn(async () => snapshot)) {
  const sources: Source[] = [];
  const accept = vi.fn(), invalidate = vi.fn(), failed = vi.fn();
  const stop = watchTopicRegistry({ topic, epoch, fetchSnapshot, accept, invalidate, failed,
    source: (url) => { const source = new Source(url); sources.push(source); return source as unknown as EventSource; } });
  return { sources, accept, invalidate, failed, stop, fetchSnapshot };
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
describe("canonical topic event channel", () => {
  it("validates exact scoped pointers and rejects unsafe cursors, IDs and extra fields", () => {
    const body = { topic, registry_epoch: epoch, snapshot_revision: epoch, cursor: 1 };
    expect(registryNotice("registry.invalidated", JSON.stringify(body), topic, `${epoch}:${topic}:1`)).toEqual(body);
    for (const candidate of [{ ...body, cursor: 2 ** 53 }, { ...body, topic: "other" }, { ...body, secret: "no" }])
      expect(() => registryNotice("registry.invalidated", JSON.stringify(candidate), topic, `${epoch}:${topic}:1`)).toThrow();
    expect(() => registryNotice("registry.invalidated", JSON.stringify(body), topic, "bad")).toThrow();
  });
  it("coalesces topic-only invalidation and ignores late callbacks after stop", async () => {
    let resolve!: (value: TopicSnapshot) => void;
    const fetch = vi.fn(() => new Promise<TopicSnapshot>((done) => { resolve = done; }));
    const state = setup(fetch);
    state.sources[0].pointer(1); state.sources[0].pointer(2); state.sources[0].pointer(3);
    expect(fetch).toHaveBeenCalledTimes(1);
    resolve(snapshot); await flush();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(state.accept).not.toHaveBeenCalled();
    state.stop(); resolve(snapshot); await flush();
    expect(state.accept).not.toHaveBeenCalled();
    state.sources[0].pointer(4);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it("closes terminal recovery then resumes only after a matching fresh snapshot", async () => {
    const state = setup();
    state.sources[0].emit("registry.snapshot_required", { topic, registry_epoch: epoch, head: 5, reason: "initial_snapshot" });
    expect(state.sources[0].closed).toBe(true);
    await flush();
    expect(state.accept).toHaveBeenCalledWith(snapshot);
    expect(state.sources[1].url).toContain(`?after=5&epoch=${epoch}`);
    state.sources[1].emit("registry.snapshot_required", { topic, registry_epoch: epoch, head: 5, reason: "initial_snapshot" });
    expect(state.failed).toHaveBeenCalledOnce();
    expect(state.sources).toHaveLength(2);
  });
  it("fails closed on snapshot epoch mismatch and transport error, without automatic reconnect", async () => {
    const mismatch = setup(vi.fn(async () => ({ ...snapshot, registry_epoch: "87654321-1234-1234-1234-123456789abc" })));
    mismatch.sources[0].pointer(1); await flush();
    expect(mismatch.failed).toHaveBeenCalledOnce();
    const lost = setup(); lost.sources[0].onerror?.(); await flush();
    expect(lost.sources[0].closed).toBe(true);
    expect(lost.invalidate).toHaveBeenCalledOnce();
    expect(lost.sources).toHaveLength(1);
    lost.sources[0].pointer(1);
    expect(lost.fetchSnapshot).not.toHaveBeenCalled();
  });
  it("bounds consecutive recovery notices even if they are otherwise valid", async () => {
    const state = setup();
    for (let index = 0; index < 4; index++) {
      state.sources.at(-1)!.emit("registry.snapshot_required", {
        topic, registry_epoch: epoch, head: 1,
        reason: index === 0 ? "initial_snapshot" : "retention_gap",
      });
      await flush();
    }
    expect(state.failed).toHaveBeenCalledOnce();
    expect(state.fetchSnapshot).toHaveBeenCalledTimes(3);
    expect(state.sources).toHaveLength(4);
    expect(state.sources.at(-1)!.closed).toBe(true);
  });
  it("invalidates unavailable source and rejects regressing cursors", async () => {
    const unavailable = setup();
    unavailable.sources[0].emit("registry.unavailable", { topic, registry_epoch: epoch, reason: "observer_unavailable" });
    expect(unavailable.failed).toHaveBeenCalledOnce();
    expect(unavailable.sources[0].closed).toBe(true);
    const regressing = setup();
    regressing.sources[0].pointer(2); await flush();
    regressing.sources[0].pointer(1);
    expect(regressing.failed).toHaveBeenCalledOnce();
  });
  it("recovers an epoch change and ignores late callbacks from the closed source", async () => {
    const nextEpoch = "87654321-1234-1234-1234-123456789abc";
    const nextSnapshot = { ...snapshot, registry_epoch: nextEpoch };
    const state = setup(vi.fn(async () => nextSnapshot));
    const old = state.sources[0];
    old.emit("registry.snapshot_required", { topic, registry_epoch: nextEpoch, head: 0, reason: "epoch_changed" });
    await flush();
    expect(state.accept).toHaveBeenCalledWith(nextSnapshot);
    expect(state.sources[1].url).toContain(`?after=0&epoch=${nextEpoch}`);
    old.pointer(500); old.onerror?.();
    expect(state.failed).not.toHaveBeenCalled();
    expect(state.fetchSnapshot).toHaveBeenCalledOnce();
  });
  it("accepts a newer same-epoch snapshot revision than the invalidation pointer", async () => {
    const newer = { ...snapshot, snapshot_revision: "87654321-1234-1234-1234-123456789abc" };
    const state = setup(vi.fn(async () => newer));
    state.sources[0].pointer(1); await flush();
    expect(state.accept).toHaveBeenCalledWith(newer);
    expect(state.failed).not.toHaveBeenCalled();
  });
  it("bounds event-driven snapshot attempts with explicit manual recovery, not timers", async () => {
    const state = setup();
    for (let cursor = 1; cursor <= TOPIC_SNAPSHOT_BUDGET + 1; cursor++) {
      state.sources[0].pointer(cursor); await flush();
    }
    expect(state.fetchSnapshot).toHaveBeenCalledTimes(TOPIC_SNAPSHOT_BUDGET);
    expect(state.failed).toHaveBeenCalledExactlyOnceWith(expect.any(TopicRefreshBudgetExhausted));
    expect(state.sources[0].closed).toBe(true);
    expect(state.sources).toHaveLength(1);
  });
});
