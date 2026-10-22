/**
 * Host side of the custom-module bridge (v1). Pure functions only, so every rule is
 * unit-testable. The frame is sandboxed without allow-same-origin, so its origin is
 * the opaque "null": authority comes from the private MessagePort handed over at init
 * (plus a per-instance nonce and generation), never from event.origin.
 */

export const PROTOCOL_VERSION = 1;
export const MAX_MESSAGE_BYTES = 64 * 1024;
export const MAX_MESSAGES_PER_SECOND = 50;
export const READY_TIMEOUT_MS = 10_000;
export const MIN_HEIGHT = 200;
export const MAX_HEIGHT = 4000;

/** Fixed capability grammar: read a named dataset or attachment, or submit responses. */
const CAPABILITY = /^(read:dataset\/[a-z0-9][a-z0-9._-]{0,63}|read:attachment\/[a-z0-9][a-z0-9._-]{0,127}|submit:response)$/;
export const isCapability = (value: unknown): value is string => typeof value === "string" && CAPABILITY.test(value);

export type ModuleMessage =
  | { type: "ready" }
  | { type: "resize"; height: number }
  | { type: "data.get"; id: number; capability: string; key: string }
  | { type: "submit"; id: number; item_key: string | null; payload: unknown; expected_revision: number }
  | { type: "notice"; level: "info" | "warning"; text: string };

export type Rejection = "too_large" | "bad_envelope" | "wrong_instance" | "unknown_type" | "bad_fields" | "capability_denied";

const byteLength = (value: string) => new TextEncoder().encode(value).length;

export const MAX_DEPTH = 16;
export const MAX_NODES = 5000;
export const MAX_KEYS = 200;
export const MAX_PENDING = 4;
export const REQUEST_DEADLINE_MS = 30_000;
/** Largest reply the host sends back into a module. */
export const MAX_REPLY_BYTES = 256 * 1024;

/** Host → module replies get the same plain-JSON domain and a size cap. */
export function boundedReply(body: Record<string, unknown>): Record<string, unknown> {
  const plain = toPlainJson(body);
  if (plain === undefined) return { ok: false, error: "reply_not_plain" };
  return new TextEncoder().encode(JSON.stringify(plain)).length > MAX_REPLY_BYTES
    ? { ok: false, error: "reply_too_large" } : plain as Record<string, unknown>;
}

/**
 * Copies a structured-clone value into plain JSON (null, boolean, finite number,
 * string, array, plain object) within depth, node and key budgets, or returns
 * undefined. ArrayBuffers, typed arrays, Blobs, Maps, Dates, cycles and the like
 * are refused, so a JSON size check is real. Only the copy is ever forwarded.
 * The browser has already received the structured clone; this is not memory isolation.
 */
export function toPlainJson(value: unknown): unknown {
  let nodes = 0;
  const seen = new Set<object>();
  const copy = (item: unknown, depth: number): unknown => {
    if (++nodes > MAX_NODES || depth > MAX_DEPTH) throw new Error("budget");
    if (item === null || typeof item === "boolean" || typeof item === "string") return item;
    if (typeof item === "number") { if (!Number.isFinite(item)) throw new Error("number"); return item; }
    if (typeof item !== "object") throw new Error("type");
    if (seen.has(item)) throw new Error("cycle");
    seen.add(item);
    try {
      if (Array.isArray(item)) {
        if (Object.getPrototypeOf(item) !== Array.prototype || item.length > MAX_NODES) throw new Error("array");
        return item.map((entry) => copy(entry, depth + 1));
      }
      const proto = Object.getPrototypeOf(item);
      if (proto !== Object.prototype && proto !== null) throw new Error("object");
      const keys = Object.keys(item);
      if (keys.length > MAX_KEYS) throw new Error("keys");
      // An own "__proto__" key would re-prototype a normal object on assignment and
      // hide fields from JSON sizing; refuse it and copy into a null-prototype object.
      if (keys.includes("__proto__")) throw new Error("proto");
      const out: Record<string, unknown> = Object.create(null);
      for (const key of keys) {
        const entry = (item as Record<string, unknown>)[key];
        // Like JSON: an undefined property is simply absent.
        if (entry !== undefined) out[key] = copy(entry, depth + 1);
      }
      return out;
    } finally { seen.delete(item); }
  };
  try { return copy(value, 0); } catch { return undefined; }
}

/**
 * Validates one message from the module port. `instance` is this frame's nonce and
 * generation; anything from an older generation or another instance is rejected.
 */
export function parseModuleMessage(raw: unknown, instance: { nonce: string; generation: number }, granted: readonly string[]):
  { ok: true; message: ModuleMessage } | { ok: false; reason: Rejection } {
  const plain = toPlainJson(raw);
  if (plain === undefined) return { ok: false, reason: "bad_envelope" };
  raw = plain;
  let size: number;
  try { size = byteLength(JSON.stringify(raw) ?? ""); } catch { return { ok: false, reason: "bad_envelope" }; }
  if (size > MAX_MESSAGE_BYTES) return { ok: false, reason: "too_large" };
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { ok: false, reason: "bad_envelope" };
  const envelope = raw as Record<string, unknown>;
  if (envelope.v !== PROTOCOL_VERSION || typeof envelope.type !== "string") return { ok: false, reason: "bad_envelope" };
  if (envelope.nonce !== instance.nonce || envelope.generation !== instance.generation) return { ok: false, reason: "wrong_instance" };
  const body = (envelope.payload ?? {}) as Record<string, unknown>;
  if (typeof body !== "object" || body === null || Array.isArray(body)) return { ok: false, reason: "bad_fields" };
  const id = envelope.id;
  const validId = Number.isSafeInteger(id) && (id as number) >= 0;
  switch (envelope.type) {
    case "ready": return { ok: true, message: { type: "ready" } };
    case "resize":
      return Number.isFinite(body.height) ? { ok: true, message: { type: "resize", height: clampHeight(body.height as number) } }
        : { ok: false, reason: "bad_fields" };
    case "data.get":
      if (!validId || typeof body.key !== "string" || body.key.length > 200) return { ok: false, reason: "bad_fields" };
      if (!isCapability(body.capability) || !granted.includes(body.capability) || !body.capability.startsWith("read:"))
        return { ok: false, reason: "capability_denied" };
      return { ok: true, message: { type: "data.get", id: id as number, capability: body.capability, key: body.key } };
    case "submit":
      if (!granted.includes("submit:response")) return { ok: false, reason: "capability_denied" };
      if (!validId || !(body.item_key === null || (typeof body.item_key === "string" && body.item_key.length <= 128))
          || !Number.isSafeInteger(body.expected_revision) || (body.expected_revision as number) < 0) return { ok: false, reason: "bad_fields" };
      return { ok: true, message: { type: "submit", id: id as number, item_key: body.item_key as string | null,
        payload: body.payload, expected_revision: body.expected_revision as number } };
    case "notice":
      if ((body.level !== "info" && body.level !== "warning") || typeof body.text !== "string") return { ok: false, reason: "bad_fields" };
      return { ok: true, message: { type: "notice", level: body.level, text: body.text.slice(0, 200) } };
    default: return { ok: false, reason: "unknown_type" };
  }
}

export const clampHeight = (value: number) => Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Math.round(value)));

/** Sliding one-second window; returns false once the module exceeds its budget. */
export function rateLimiter(limit = MAX_MESSAGES_PER_SECOND, now: () => number = () => performance.now()) {
  const times: number[] = [];
  return () => {
    const t = now();
    while (times.length && t - times[0] >= 1000) times.shift();
    times.push(t);
    return times.length <= limit;
  };
}

/** Module asset URLs must be https (or a local test origin) and never the Hub's own origin. */
export function moduleAssetUrl(value: string, hubOrigin: string): string | null {
  try {
    const url = new URL(value);
    const local = url.protocol === "http:" && (url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname.endsWith(".test"));
    if (url.protocol !== "https:" && !local) return null;
    if (url.origin === hubOrigin || url.username || url.password) return null;
    return url.href;
  } catch {
    return null;
  }
}
