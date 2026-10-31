import { describe, expect, it, vi } from "vitest";
import { parseQueueSnapshot, readMachineReservation, readQueueSnapshot } from "./workQueueApi";

const id = "12345678-1234-1234-1234-123456789012";
function fixture() { return {
  paused: false, queue_paused: false, master_paused: false, claim_admission_state: "unconfigured",
  counts: { queued: 1, held: 2 }, actions: {}, jobs: [{ id, machine_id: "staging-pc", action_key: "engineering-hub.validate",
    environment: "staging", state: "queued", target: { task_id: id, source_commit: "a".repeat(40), artifact_digest: "sha256:" + "b".repeat(64) },
    blocked_reason: "admission_not_configured", requires_reconciliation: false, eligible: false,
    eligible_requires_worker_admission: false, private_unexpected_field: "not-rendered" }],
}; }
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { "Content-Type": "application/json" },
});
describe("read-only software queue", () => {
  it("derives unique staging machines from registered actions", () => {
    const value = fixture();
    value.actions = { a: { machine_id: "pc-one", environments: ["staging"] },
      b: { machine_id: "pc-one", environments: ["staging", "production"] },
      c: { machine_id: "pc-two", environments: ["production"] } };
    expect(parseQueueSnapshot(value).stagingMachines).toEqual(["pc-one"]);
  });
  it.each(["not_reserved", "active", "held", "unknown"] as const)("reads exact %s reservation", async state => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ machine_id: "pc-one", queue_reservation: state,
      requires_reconciliation: state === "unknown" ? null : state === "held" }));
    expect(await readMachineReservation("pc-one", new AbortController().signal, fetchImpl)).toBe(state);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
  it("rejects mismatched identity and inconsistent reservation, and does not treat 423 as clear", async () => {
    for (const payload of [{ machine_id: "other", queue_reservation: "active", requires_reconciliation: false },
      { machine_id: "pc-one", queue_reservation: "held", requires_reconciliation: false }]) {
      await expect(readMachineReservation("pc-one", new AbortController().signal,
        vi.fn<typeof fetch>().mockResolvedValue(response(payload)))).rejects.toThrow();
    }
    await expect(readMachineReservation("pc-one", new AbortController().signal,
      vi.fn<typeof fetch>().mockResolvedValue(response({}, 423)))).rejects.toMatchObject({ status: 423 });
  });
  it("uses canonical unescaped paths for validated colon machine IDs", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ machine_id: "staging:pc", queue_reservation: "held", requires_reconciliation: true }));
    await readMachineReservation("staging:pc", new AbortController().signal, fetchImpl);
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/hub/v1/work/machines/staging:pc");
    await expect(readMachineReservation("staging/pc", new AbortController().signal, fetchImpl)).rejects.toThrow();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
  it("projects only displayed fields, preserving closed admission and exact target", () => {
    const result = parseQueueSnapshot(fixture());
    expect(result.claim_admission_state).toBe("unconfigured");
    expect(result.jobs[0].target.source_commit).toBe("a".repeat(40));
    expect(JSON.stringify(result)).not.toContain("not-rendered");
    expect(result.counts.held).toBe(2);
  });
  it.each([null, {}, { ...fixture(), paused: "false" }, { ...fixture(), master_paused: true },
    { ...fixture(), claim_admission_state: "ready" }, { ...fixture(), counts: { queued: -1 } },
    { ...fixture(), counts: { unknown_state: 1 } }, { ...fixture(), jobs: Array(51).fill(fixture().jobs[0]) },
    { ...fixture(), jobs: [fixture().jobs[0], fixture().jobs[0]] }])("rejects malformed or misleading snapshots %#", value => {
    expect(() => parseQueueSnapshot(value)).toThrow("invalid_queue_snapshot");
  });
  it.each([{ environment: "deploy-production" }, { state: "ready" }, { blocked_reason: "private host output" },
    { eligible: "true" }, { requires_reconciliation: 0 }, { machine_id: "<unsafe>" },
    { target: { task_id: id, source_commit: "bad", artifact_digest: "bad" } }])("rejects unsafe job fields %#", change => {
    const value = fixture(); Object.assign(value.jobs[0], change);
    expect(() => parseQueueSnapshot(value)).toThrow("invalid_queue_snapshot");
  });
  it("uses one GET, same-origin cookies, no bearer and propagates abort signal", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response(fixture()));
    const controller = new AbortController();
    await readQueueSnapshot(controller.signal, fetchImpl);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledWith("/api/hub/v1/work/status?limit=50", {
      method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store", headers: { Accept: "application/json" }, signal: controller.signal,
    });
  });
  it.each([[423, "work_queue_not_configured"], [404, "work_queue_unavailable"], [401, "queue_read_failed"],
    [403, "queue_read_failed"], [503, "queue_read_failed"]])("retains HTTP status %s without reflecting server body", async (status, code) => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(response({ secret: "private" }, status as number));
    await expect(readQueueSnapshot(new AbortController().signal, fetchImpl)).rejects.toMatchObject({ status, code });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
  it("does not mistake login HTML for an empty queue", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(new Response("login", { headers: { "Content-Type": "text/html" } }));
    await expect(readQueueSnapshot(new AbortController().signal, fetchImpl)).rejects.toMatchObject({ code: "invalid_queue_snapshot" });
  });
  it("does not retry network failure or abort", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValue(new DOMException("Aborted", "AbortError"));
    await expect(readQueueSnapshot(new AbortController().signal, fetchImpl)).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
