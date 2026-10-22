/**
 * Runs one custom module in an opaque-origin sandbox. The module gets no Hub origin,
 * cookies, storage, top navigation, popups or forms; it talks only through a private
 * MessagePort, within message, size and time budgets. A broken budget ends the frame.
 */
import { useEffect, useRef, useState } from "react";
import { clampHeight, MAX_PENDING, MIN_HEIGHT, moduleAssetUrl, parseModuleMessage, PROTOCOL_VERSION, rateLimiter, READY_TIMEOUT_MS,
  REQUEST_DEADLINE_MS, type ModuleMessage } from "./protocol";

export interface ModuleHost {
  /** Read through a granted capability; the host enforces scope, the module never sees credentials. */
  getData: (capability: string, key: string) => Promise<unknown>;
  /** A human-confirmed save; resolves to the stored revision. */
  submit: (itemKey: string | null, payload: unknown, expectedRevision: number) => Promise<{ revision: number }>;
}

/** What happened to this instance's saves, so the stop message never overclaims. */
interface Outcome { saved: number; unknown: number; voided: number }
type State = { kind: "loading" } | { kind: "running" } | { kind: "stopped"; reason: string; outcome: Outcome } | { kind: "blocked" };

const STOP_TEXT: Record<string, string> = {
  timeout: "The module didn't start within 10 seconds.",
  too_large: "The module sent a message that was too large.",
  rate: "The module sent too many messages.",
  bad_envelope: "The module sent a malformed message.",
  unknown_type: "The module used an unsupported request.",
  bad_fields: "The module sent an invalid request.",
  capability_denied: "The module asked for data it wasn't granted.",
  busy: "The module had too many requests waiting.",
  duplicate: "The module reused a request number that was still in progress.",
};

function outcomeText({ saved, unknown, voided }: Outcome): string {
  const parts: string[] = [];
  if (saved) parts.push(`${saved} earlier ${saved === 1 ? "save was" : "saves were"} completed and stay saved.`);
  if (unknown) parts.push(`${unknown} ${unknown === 1 ? "save had" : "saves had"} already been sent; whether ${unknown === 1 ? "it" : "they"} finished is unknown, so check before redoing.`);
  if (voided) parts.push(`${voided} open ${voided === 1 ? "request" : "requests"} to save ${voided === 1 ? "was" : "were"} cancelled and not saved.`);
  if (!parts.length) parts.push("It hadn't saved anything.");
  return parts.join(" ");
}

export function ModuleFrame({ src, title, capabilities, host, confirmSubmit }: {
  src: string; title: string; capabilities: readonly string[]; host: ModuleHost;
  /** Native confirmation shown by the Hub before any submission is saved. */
  confirmSubmit: (payload: unknown) => Promise<boolean>;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [state, setState] = useState<State>({ kind: "loading" });
  const [height, setHeight] = useState(MIN_HEIGHT);
  const [notice, setNotice] = useState<{ level: string; text: string } | null>(null);
  const [generation, setGeneration] = useState(1);
  const url = moduleAssetUrl(src, window.location.origin);
  const hostRef = useRef(host); hostRef.current = host;
  const confirmRef = useRef(confirmSubmit); confirmRef.current = confirmSubmit;

  useEffect(() => {
    const element = frame.current;
    if (!url || !element) { setState({ kind: "blocked" }); return; }
    setState({ kind: "loading" });
    const nonce = crypto.randomUUID();
    const instance = { nonce, generation };
    // Authority is fixed for this instance: a later placement's host or confirm
    // callback can never complete a request this instance started.
    const instanceHost = hostRef.current, instanceConfirm = confirmRef.current;
    const channel = new MessageChannel();
    const allowed = rateLimiter();
    let alive = true, ready = false;
    const pending = new Set<number>();
    const outcome: Outcome = { saved: 0, unknown: 0, voided: 0 };
    let awaitingConfirm = 0, dispatched = 0;
    const stop = (reason: string) => {
      if (!alive) return;
      alive = false;
      channel.port1.close();
      element.src = "about:blank";
      // Confirmations still open are voided; saves already dispatched have an unknown result.
      setState({ kind: "stopped", reason, outcome: { ...outcome, voided: outcome.voided + awaitingConfirm, unknown: outcome.unknown + dispatched } });
    };
    const reply = (id: number, body: Record<string, unknown>) => {
      if (alive) channel.port1.postMessage({ v: PROTOCOL_VERSION, nonce, generation, id, type: "reply", payload: body });
    };
    const withDeadline = <T,>(work: Promise<T>) => new Promise<T>((resolve, reject) => {
      const timer = window.setTimeout(() => reject(new Error("deadline")), REQUEST_DEADLINE_MS);
      work.then((value) => { window.clearTimeout(timer); resolve(value); }, (error) => { window.clearTimeout(timer); reject(error); });
    });
    const handle = async (message: ModuleMessage) => {
      if (!alive) return;
      if (message.type === "ready") { ready = true; setState({ kind: "running" }); return; }
      if (!ready) return;
      if (message.type === "resize") { setHeight(clampHeight(message.height)); return; }
      if (message.type === "notice") { setNotice({ level: message.level, text: message.text }); return; }
      // Requests: bounded in number, unique ids while pending, each with a deadline.
      if (pending.has(message.id)) { stop("duplicate"); return; }
      if (pending.size >= MAX_PENDING) { stop("busy"); return; }
      pending.add(message.id);
      try {
        if (message.type === "data.get") {
          try { reply(message.id, { ok: true, data: await withDeadline(instanceHost.getData(message.capability, message.key)) }); }
          catch { if (alive) reply(message.id, { ok: false, error: "unavailable" }); }
          return;
        }
        if (awaitingConfirm > 0) { reply(message.id, { ok: false, error: "confirmation_pending" }); return; }
        awaitingConfirm++;
        let confirmed = false;
        try { confirmed = await instanceConfirm(message.payload); } catch { confirmed = false; }
        finally { awaitingConfirm--; }
        // Stopped, restarted, unmounted or replaced while the person was deciding: void.
        if (!alive) return;
        if (!confirmed) { reply(message.id, { ok: false, error: "declined" }); return; }
        dispatched++;
        try {
          const result = await withDeadline(instanceHost.submit(message.item_key, message.payload, message.expected_revision));
          dispatched--; outcome.saved++;
          reply(message.id, { ok: true, ...result });
        } catch {
          // A failed or timed-out save may still have been applied server-side.
          dispatched--; outcome.unknown++;
          reply(message.id, { ok: false, error: "outcome_unknown" });
        }
      } finally { pending.delete(message.id); }
    };
    channel.port1.onmessage = (event) => {
      if (!alive) return;
      if (!allowed()) { stop("rate"); return; }
      const parsed = parseModuleMessage(event.data, instance, capabilities);
      if (!parsed.ok) { stop(parsed.reason); return; }
      void handle(parsed.message);
    };
    // The port is transferred once the document has loaded; the opaque origin means
    // targetOrigin must be "*", which is safe because only this frame's window receives it.
    const onLoad = () => {
      if (!alive || !element.contentWindow) return;
      element.contentWindow.postMessage({ v: PROTOCOL_VERSION, type: "init", nonce, generation,
        payload: { capabilities: [...capabilities] } }, "*", [channel.port2]);
    };
    element.addEventListener("load", onLoad, { once: true });
    element.src = url;
    const timer = window.setTimeout(() => { if (!ready) stop("timeout"); }, READY_TIMEOUT_MS);
    return () => { window.clearTimeout(timer); element.removeEventListener("load", onLoad); alive = false; channel.port1.close(); };
  }, [url, generation, capabilities.join(" ")]); // eslint-disable-line react-hooks/exhaustive-deps

  if (state.kind === "blocked") return <p role="alert" className="notice error">This module's address isn't allowed, so it wasn't loaded.</p>;
  return (
    <div className="module-frame">
      {state.kind === "loading" && <p role="status" className="panel-footnote">Starting module…</p>}
      {state.kind === "stopped" && (
        <div role="alert" className="notice error">
          <p>Module stopped. {STOP_TEXT[state.reason] ?? "It broke a safety rule."} {outcomeText(state.outcome)}</p>
          <button type="button" className="secondary small" onClick={() => { setNotice(null); setGeneration((value) => value + 1); }}>Restart module</button>
        </div>
      )}
      {notice && state.kind === "running" && <p className={notice.level === "warning" ? "notice error" : "notice"}>{notice.text}</p>}
      <iframe ref={frame} title={title} sandbox="allow-scripts" referrerPolicy="no-referrer" allow=""
        style={{ height, display: state.kind === "stopped" ? "none" : undefined }} />
    </div>
  );
}
