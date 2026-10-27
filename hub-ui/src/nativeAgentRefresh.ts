import type { AgentObservation } from "./agentObservations";

export const AGENT_REFRESH_INTERVAL = 15_000;
const TIMEOUT = 10_000;

/** Browser metadata only. No workspace/topic refresh or delivery authority. */
export function watchAgentObservations(options: {
  read: (signal: AbortSignal) => Promise<AgentObservation[]>;
  accept: (items: AgentObservation[]) => void;
  failed: (error: unknown) => void;
  generation: () => number;
  document: Pick<Document, "visibilityState" | "addEventListener" | "removeEventListener">;
}) {
  let alive = true, enabled = false, epoch = 0, lastAttempt = Date.now();
  let pending: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const visible = () => options.document.visibilityState === "visible";
  const invalidate = () => { epoch++; pending?.abort(); };
  function schedule() {
    clearTimeout(timer);
    if (alive && enabled && visible()) timer = setTimeout(() => void tick(),
      Math.max(0, AGENT_REFRESH_INTERVAL - (Date.now() - lastAttempt)));
  }
  async function tick() {
    if (!alive || !enabled || pending || !visible() || Date.now() - lastAttempt < AGENT_REFRESH_INTERVAL) return;
    const controller = new AbortController();
    pending = controller;
    lastAttempt = Date.now();
    const expected = epoch, generation = options.generation();
    const current = () => alive && enabled && expected === epoch && generation === options.generation() && visible();
    let timeout: ReturnType<typeof setTimeout> | undefined;
    let abort: (() => void) | undefined;
    try {
      const cancelled = new Promise<never>((_, reject) => {
        abort = () => reject(new Error("agent_read_cancelled"));
        controller.signal.addEventListener("abort", abort, { once: true });
        timeout = setTimeout(() => controller.abort(), TIMEOUT);
      });
      const items = await Promise.race([options.read(controller.signal), cancelled]);
      if (current()) options.accept(items);
    } catch (error) {
      if (current()) options.failed(error);
    } finally {
      clearTimeout(timeout);
      if (abort) controller.signal.removeEventListener("abort", abort);
      if (pending === controller) pending = null;
      schedule();
    }
  }
  const visibility = () => {
    if (!visible()) { clearTimeout(timer); invalidate(); } // Responses crossing hidden time cannot restore evidence.
    else { void tick(); schedule(); }
  };
  options.document.addEventListener("visibilitychange", visibility);
  return {
    pause() { enabled = false; clearTimeout(timer); invalidate(); },
    seed() { invalidate(); enabled = true; lastAttempt = Date.now(); schedule(); },
    stop() {
      alive = false; enabled = false; invalidate(); clearTimeout(timer);
      options.document.removeEventListener("visibilitychange", visibility);
    },
  };
}
