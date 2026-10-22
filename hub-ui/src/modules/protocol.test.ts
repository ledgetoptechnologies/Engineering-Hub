import { describe, expect, it } from "vitest";
import { clampHeight, isCapability, moduleAssetUrl, parseModuleMessage, rateLimiter } from "./protocol";

const instance = { nonce: "n1", generation: 2 };
const granted = ["read:dataset/crowns", "submit:response"];
const msg = (type: string, payload: unknown = {}, extra: Record<string, unknown> = {}) =>
  ({ v: 1, nonce: "n1", generation: 2, type, payload, ...extra });

describe("module protocol", () => {
  it("accepts well-formed messages from this instance only", () => {
    expect(parseModuleMessage(msg("ready"), instance, granted)).toEqual({ ok: true, message: { type: "ready" } });
    expect(parseModuleMessage({ ...msg("ready"), nonce: "other" }, instance, granted)).toEqual({ ok: false, reason: "wrong_instance" });
    expect(parseModuleMessage({ ...msg("ready"), generation: 1 }, instance, granted)).toEqual({ ok: false, reason: "wrong_instance" });
    expect(parseModuleMessage({ ...msg("ready"), v: 2 }, instance, granted)).toEqual({ ok: false, reason: "bad_envelope" });
    expect(parseModuleMessage(msg("exec"), instance, granted)).toEqual({ ok: false, reason: "unknown_type" });
  });
  it("enforces granted, read-only data capabilities and the submit grant", () => {
    expect(parseModuleMessage(msg("data.get", { capability: "read:dataset/crowns", key: "a" }, { id: 1 }), instance, granted).ok).toBe(true);
    expect(parseModuleMessage(msg("data.get", { capability: "read:dataset/other", key: "a" }, { id: 1 }), instance, granted))
      .toEqual({ ok: false, reason: "capability_denied" });
    expect(parseModuleMessage(msg("data.get", { capability: "submit:response", key: "a" }, { id: 1 }), instance, granted))
      .toEqual({ ok: false, reason: "capability_denied" });
    expect(parseModuleMessage(msg("submit", { item_key: null, payload: {}, expected_revision: 0 }, { id: 2 }), instance, ["read:dataset/crowns"]))
      .toEqual({ ok: false, reason: "capability_denied" });
  });
  it("caps size, height and notice text", () => {
    expect(parseModuleMessage(msg("notice", { level: "info", text: "x".repeat(70000) }), instance, granted)).toEqual({ ok: false, reason: "too_large" });
    expect(parseModuleMessage(msg("notice", { level: "info", text: "y".repeat(500) }), instance, granted))
      .toMatchObject({ ok: true, message: { text: "y".repeat(200) } });
    expect(clampHeight(10)).toBe(200);
    expect(clampHeight(99999)).toBe(4000);
  });
  it("limits message rate per second", () => {
    let t = 0;
    const allow = rateLimiter(3, () => t);
    expect([allow(), allow(), allow(), allow()]).toEqual([true, true, true, false]);
    t = 1001;
    expect(allow()).toBe(true);
  });
  it("only loads modules from https or local test origins, never the Hub origin", () => {
    expect(moduleAssetUrl("https://modules.example/m/a/index.html", "https://hub.example")).toBe("https://modules.example/m/a/index.html");
    expect(moduleAssetUrl("https://hub.example/m/a/index.html", "https://hub.example")).toBeNull();
    expect(moduleAssetUrl("http://evil.example/x", "https://hub.example")).toBeNull();
    expect(moduleAssetUrl("javascript:alert(1)", "https://hub.example")).toBeNull();
    expect(moduleAssetUrl("https://user:pw@modules.example/x", "https://hub.example")).toBeNull();
    expect(isCapability("read:dataset/../x")).toBe(false);
  });
});

describe("plain JSON only", async () => {
  const { toPlainJson, MAX_DEPTH } = await import("./protocol");
  it("copies plain data and refuses binary, exotic, cyclic, deep or non-finite values", () => {
    expect(toPlainJson({ a: [1, "x", null, true, { b: 2 }] })).toEqual({ a: [1, "x", null, true, { b: 2 }] });
    expect(toPlainJson({ a: 1, gone: undefined })).toEqual({ a: 1 });
    expect(toPlainJson([undefined])).toBeUndefined();
    for (const bad of [new ArrayBuffer(8), new Uint8Array(4), new Map(), new Date(), NaN, Infinity, () => 1, Symbol("s"), 10n])
      expect(toPlainJson({ v: bad })).toBeUndefined();
    const cyclic: Record<string, unknown> = {}; cyclic.self = cyclic;
    expect(toPlainJson(cyclic)).toBeUndefined();
    let deep: unknown = 1; for (let i = 0; i < MAX_DEPTH + 2; i++) deep = [deep];
    expect(toPlainJson(deep)).toBeUndefined();
    expect(toPlainJson(Object.fromEntries(Array.from({ length: 300 }, (_, i) => [`k${i}`, i])))).toBeUndefined();
  });
  it("judges size on the plain copy, so a 4 MiB buffer can't hide behind a small JSON string", () => {
    const raw = { v: 1, nonce: "n1", generation: 2, type: "submit", id: 1,
      payload: { item_key: null, payload: { blob: new ArrayBuffer(4 * 1024 * 1024) }, expected_revision: 0 } };
    expect(parseModuleMessage(raw, instance, granted)).toEqual({ ok: false, reason: "bad_envelope" });
  });
});
