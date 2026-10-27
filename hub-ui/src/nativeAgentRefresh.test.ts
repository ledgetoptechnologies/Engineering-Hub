import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { watchAgentObservations } from "./nativeAgentRefresh";
import type { AgentObservation } from "./agentObservations";

function fixture() {
  const events = new EventTarget();
  let visibilityState = "visible", generation = 1;
  const pending: { signal: AbortSignal; resolve: (items: AgentObservation[]) => void; reject: (error: unknown) => void }[] = [];
  const read = vi.fn((signal: AbortSignal) => new Promise<AgentObservation[]>((resolve, reject) => pending.push({ signal, resolve, reject })));
  const accept = vi.fn(), failed = vi.fn();
  const controller = watchAgentObservations({ read, accept, failed, generation: () => generation,
    document: { get visibilityState() { return visibilityState as DocumentVisibilityState; },
      addEventListener: events.addEventListener.bind(events), removeEventListener: events.removeEventListener.bind(events) } });
  return { controller, read, accept, failed, pending,
    renew() { generation++; controller.pause(); controller.seed(); },
    visible(value: boolean) { visibilityState = value ? "visible" : "hidden"; events.dispatchEvent(new Event("visibilitychange")); } };
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
describe("native browser connection observation lifecycle", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(0); });
  afterEach(() => vi.useRealTimers());
  it("does not read before authenticated seed; skips hidden time and coalesces overdue visibility", async () => {
    const f = fixture();
    await vi.advanceTimersByTimeAsync(60_000); expect(f.read).not.toHaveBeenCalled();
    f.controller.seed(); f.visible(false);
    await vi.advanceTimersByTimeAsync(60_000); expect(f.read).not.toHaveBeenCalled();
    f.visible(true); f.visible(true); await flush(); expect(f.read).toHaveBeenCalledTimes(1);
    f.pending[0].resolve([]); await flush();
    await vi.advanceTimersByTimeAsync(14_999); expect(f.read).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1); expect(f.read).toHaveBeenCalledTimes(2);
    f.controller.stop();
  });
  it("times out a hung read, neutralizes evidence, and retries at normal cadence", async () => {
    const f = fixture(); f.controller.seed();
    await vi.advanceTimersByTimeAsync(15_000);
    f.visible(true); expect(f.read).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(f.failed).toHaveBeenCalledOnce(); expect(f.pending[0].signal.aborted).toBe(true);
    f.pending[0].resolve([{ id: "codex" }]); await flush(); expect(f.accept).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(5_000); expect(f.read).toHaveBeenCalledTimes(2);
    f.controller.stop();
  });
  it("fences late success and authentication failure across same-actor renewal and stop", async () => {
    const f = fixture(); f.controller.seed();
    await vi.advanceTimersByTimeAsync(15_000); f.renew(); await flush();
    f.pending[0].reject({ status: 401 }); await flush(); expect(f.failed).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(15_000); expect(f.read).toHaveBeenCalledTimes(2);
    f.controller.stop(); f.pending[1].resolve([{ id: "codex" }]); await flush();
    await vi.advanceTimersByTimeAsync(60_000); expect(f.accept).not.toHaveBeenCalled(); expect(f.read).toHaveBeenCalledTimes(2);
  });
  it("discards a response crossing hidden time without reporting authentication loss", async () => {
    const f = fixture(); f.controller.seed(); await vi.advanceTimersByTimeAsync(15_000);
    f.visible(false); f.pending[0].reject({ status: 403 }); await flush();
    expect(f.failed).not.toHaveBeenCalled(); expect(f.accept).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(30_000); f.visible(true); await flush();
    expect(f.read).toHaveBeenCalledTimes(2); f.pending[1].resolve([]); await flush();
    expect(f.accept).toHaveBeenCalledWith([]); f.controller.stop();
  });
});
