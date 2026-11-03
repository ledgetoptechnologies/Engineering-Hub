import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ReleaseEvidenceView, RunReleaseEvidence } from "./ReleaseEvidencePanel";
import { parseReleaseEvidence } from "./releaseEvidenceApi";
import { evidenceBody } from "./releaseEvidenceFixture";

describe("release evidence presentation", () => {
  it("labels every result as a worker report and never as independently verified", () => {
    const html = renderToStaticMarkup(<ReleaseEvidenceView evidence={parseReleaseEvidence(evidenceBody())} />);
    expect(html).toContain("Worker-reported, not independently verified");
    expect(html).toContain("Worker reports success");
    expect(html).toContain("Worker reports failure");
    expect(html).toContain("The health check failed.");
    expect(html).toContain("Unhealthy (worker report)");
    expect(html).not.toMatch(/(?<!not )independently verified/i);
    expect(html).not.toContain("<button");
  });
  it("shows steps not yet started without inventing results", () => {
    const html = renderToStaticMarkup(<ReleaseEvidenceView evidence={parseReleaseEvidence(evidenceBody())} />);
    expect(html).toContain("Not started");
    expect(html).toContain("No request sent");
    expect(html).toContain("nothing reported");
  });
  it("flags an image the worker reports that isn't the expected one", () => {
    const body = evidenceBody();
    body.candidate_deploy.reported_image_config_id = `sha256:${"f".repeat(64)}`;
    const html = renderToStaticMarkup(<ReleaseEvidenceView evidence={parseReleaseEvidence(body)} />);
    expect(html).toContain("but expected cccccccccccc");
  });
  it("flags a review for a different commit than the one deployed", () => {
    const body = evidenceBody();
    body.review = { actor: "codex", commit: "b".repeat(40), verdict: "approve" };
    const html = renderToStaticMarkup(<ReleaseEvidenceView evidence={parseReleaseEvidence(body)} />);
    expect(html).toContain("This isn&#x27;t the commit being deployed.");
  });
  it("says when a receipt disagrees with the Hub's records", () => {
    const body = evidenceBody();
    body.candidate_deploy = { ...body.candidate_deploy, state: "inconsistent", journal_verified: false };
    const html = renderToStaticMarkup(<ReleaseEvidenceView evidence={parseReleaseEvidence(body)} />);
    expect(html).toContain("Doesn&#x27;t match the Hub&#x27;s records");
    expect(html).toContain("Receipt doesn&#x27;t match the Hub&#x27;s journal");
  });
  it("reads nothing for a run until the owner asks", () => {
    const html = renderToStaticMarkup(<RunReleaseEvidence runId="11111111-2222-4333-8444-555555555555" onUnauthorized={() => {}} />);
    expect(html).toContain("Show release evidence");
    expect(html).not.toContain("Reading release evidence");
  });
});
