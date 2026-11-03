import { describe, expect, it } from "vitest";
import { HubApiError } from "./api";
import { candidate, commit, evidenceBody, previous, run, step } from "./releaseEvidenceFixture";
import { parseReleaseEvidence, readReleaseEvidence, reasonText, releaseEvidencePath, STEP_REASONS, STEP_STATES, stepStateText } from "./releaseEvidenceApi";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

describe("parseReleaseEvidence", () => {
  it("accepts the Hub's DTO", () => {
    const parsed = parseReleaseEvidence(evidenceBody());
    expect(parsed.candidate_health_check.reason).toBe("health_failed");
    expect(parsed.independently_verified).toBe(false);
  });
  it("refuses anything claiming independent verification or another basis", () => {
    expect(() => parseReleaseEvidence({ ...evidenceBody(), independently_verified: true })).toThrow(HubApiError);
    expect(() => parseReleaseEvidence({ ...evidenceBody(), evidence_basis: "hub_inspection" })).toThrow(HubApiError);
  });
  it("refuses extra or missing fields, wrong step slots and malformed digests", () => {
    expect(() => parseReleaseEvidence({ ...evidenceBody(), approved: true })).toThrow(HubApiError);
    const { rollback_deploy: _, ...missing } = evidenceBody();
    expect(() => parseReleaseEvidence(missing)).toThrow(HubApiError);
    const swapped = evidenceBody(); swapped.rollback_deploy = step("deploy_container", previous);
    expect(() => parseReleaseEvidence(swapped)).toThrow(HubApiError);
    expect(() => parseReleaseEvidence({ ...evidenceBody(), candidate_image_config_id: "sha256:short" })).toThrow(HubApiError);
    expect(() => parseReleaseEvidence({ ...evidenceBody(), review: { actor: "someone", commit, verdict: "approve" } })).toThrow(HubApiError);
  });
  it("refuses worker claims on a step without a checked receipt", () => {
    const body = evidenceBody();
    body.rollback_deploy = step("rollback_container", previous, { reported_health: "healthy" });
    expect(() => parseReleaseEvidence(body)).toThrow(HubApiError);
  });
  it("has wording for every state and reason", () => {
    expect(Object.keys(stepStateText).sort()).toEqual([...STEP_STATES].sort());
    expect(Object.keys(reasonText).sort()).toEqual([...STEP_REASONS].sort());
    expect(stepStateText.succeeded.title).toContain("Worker reports");
  });
});

describe("readReleaseEvidence", () => {
  it("reads a same-origin GET for one canonical run id", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => { calls.push({ url, init }); return json(evidenceBody()); }) as unknown as typeof fetch;
    await readReleaseEvidence(run, new AbortController().signal, fetchImpl);
    expect(calls[0].url).toBe(`/api/hub/v1/container-pilot/runs/${run}/release-evidence`);
    expect(calls[0].init).toMatchObject({ method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store" });
    expect(() => releaseEvidencePath("../x")).toThrow(HubApiError);
  });
  it("maps failures to codes without reflecting server text", async () => {
    const fail = (response: Response) => readReleaseEvidence(run, new AbortController().signal, (async () => response) as unknown as typeof fetch);
    await expect(fail(json({ detail: { code: "container_pilot_not_enabled" } }, 423))).rejects.toMatchObject({ code: "container_pilot_not_enabled" });
    await expect(fail(new Response("<html>proxy</html>", { status: 502 }))).rejects.toMatchObject({ code: "release_evidence_request_failed" });
    await expect(fail(new Response("{}", { status: 200, headers: { "content-type": "text/html" } }))).rejects.toMatchObject({ code: "invalid_release_evidence" });
    await expect(readReleaseEvidence(run, new AbortController().signal, (async () => { throw new TypeError("x"); }) as unknown as typeof fetch))
      .rejects.toMatchObject({ code: "network_unavailable" });
  });
});
