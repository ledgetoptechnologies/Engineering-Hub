/**
 * Runs one custom module in an opaque-origin sandbox. The module gets no Hub origin,
 * cookies, storage, top navigation, popups or forms; it talks only through a private
 * MessagePort, within message, size and time budgets. A broken budget ends the frame.
 */
import { useEffect, useRef, useState } from "react";
import { clampHeight, MIN_HEIGHT, moduleAssetUrl, parseModuleMessage, PROTOCOL_VERSION, rateLimiter, READY_TIMEOUT_MS,
  type ModuleMessage } from "./protocol";

export interface ModuleHost {
  /** Read through a granted capability; the host enforces scope, the module never sees credentials. */
  getData: (capability: string, key: string) => Promise<unknown>;
  /** A human-confirmed save; resolves to the stored revision. */
  submit: (itemKey: string | null, payload: unknown, expectedRevision: number) => Promise<{ revision: number }>;
}

type State = { kind: "loading" } | { kind: "running" } | { kind: "stopped"; reason: string } | { kind: "blocked" };

const STOP_TEXT: Record<string, string> = {
  timeout: "The module didn't start within 10 seconds.",
  too_large: "The module sent a message that was too large.",
  rate: "The module sent too many messages.",
  bad_envelope: "The module sent a malformed message.",
  unknown_type: "The module used an unsupported request.",
  bad_fields: "The module sent an invalid request.",
  capability_denied: "The module asked for data it wasn't granted.",
};

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
    const stop = (reason: string) => {
      if (!alive) return;
      alive = false;
      channel.port1.close();
      element.src = "about:blank";
      setState({ kind: "stopped", reason });
    };
    const reply = (id: number, body: Record<string, unknown>) => {
      if (alive) channel.port1.postMessage({ v: PROTOCOL_VERSION, nonce, generation, id, type: "reply", payload: body });
    };
    const handle = async (message: ModuleMessage) => {
      if (!alive) return;
      if (message.type === "ready") { ready = true; setState({ kind: "running" }); return; }
      if (!ready) return;
      if (message.type === "resize") setHeight(clampHeight(message.height));
      else if (message.type === "notice") setNotice({ level: message.level, text: message.text });
      else if (message.type === "data.get") {
        try {
          const data = await instanceHost.getData(message.capability, message.key);
          reply(message.id, { ok: true, data });
        } catch { reply(message.id, { ok: false, error: "unavailable" }); }
      } else if (message.type === "submit") {
        const confirmed = await instanceConfirm(message.payload);
        // Stopped, restarted, unmounted or replaced while the person was deciding:
        // the confirmation is void and nothing is saved.
        if (!alive) return;
        if (!confirmed) { reply(message.id, { ok: false, error: "declined" }); return; }
        try { reply(message.id, { ok: true, ...(await instanceHost.submit(message.item_key, message.payload, message.expected_revision)) }); }
        catch { reply(message.id, { ok: false, error: "not_saved" }); }
      }
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
          <p>Module stopped. {STOP_TEXT[state.reason] ?? "It broke a safety rule."} Nothing it sent was saved.</p>
          <button type="button" className="secondary small" onClick={() => { setNotice(null); setGeneration((value) => value + 1); }}>Restart module</button>
        </div>
      )}
      {notice && state.kind === "running" && <p className={notice.level === "warning" ? "notice error" : "notice"}>{notice.text}</p>}
      <iframe ref={frame} title={title} sandbox="allow-scripts" referrerPolicy="no-referrer" allow=""
        style={{ height, display: state.kind === "stopped" ? "none" : undefined }} />
    </div>
  );
}
