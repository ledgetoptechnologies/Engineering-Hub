import { describe, expect, it, vi } from "vitest";
import { HubApiError } from "./api";
import { parseRoute, pathOf } from "./router";
import {
  ANSWER_BATCH_BYTES, answerBatches, assetPath, listReviews, parseReview, parseReviewPage, progressText, readReview, sendAnswers, unsavedAnswers, type Review,
} from "./reviewsApi";
import { DemoReviewStore } from "./reviewsDemo";
import wire from "./fixtures/interactive-reviews-wire-v1.json";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const id = wire.detail.body.id;
const detail = () => parseReview(wire.detail.body, { id });

describe("review wire contract", () => {
  it("parses the backend's real list, detail, answered and completed rows", () => {
    const page = parseReviewPage(wire.list.body, "ledge-top-ops");
    expect(page.items[0]).toMatchObject({ title: "Synthetic item review", item_count: 2, answered_count: 0, state: "open" });
    expect(detail().items.map((item) => item.choices ? "choice" : "text")).toEqual(["choice", "text"]);
    expect(parseReview(wire.answered.body).answers.classification).toMatchObject({ value: "keep", by: "human:42" });
    expect(parseReview(wire.completed.body).state).toBe("completed");
  });
  it("maps the fixture's error codes without inventing a sign-out", async () => {
    const call = (entry: { status: number; body: unknown }) => readReview(id, undefined, vi.fn<typeof fetch>().mockResolvedValue(json(entry.body, entry.status)));
    await expect(call(wire.conflict)).rejects.toMatchObject({ status: 409, code: "review_conflict" });
    await expect(call(wire.forbidden)).rejects.toMatchObject({ status: 403, code: wire.forbidden.body.detail.code });
    const gateway = vi.fn<typeof fetch>().mockResolvedValue(json({ detail: { code: "gateway_route_forbidden" } }, 403));
    await expect(listReviews("ledge-top-ops", {}, undefined, gateway)).rejects.toMatchObject({ status: 404, code: "reviews_unavailable" });
  });
  it("rejects malformed reviews instead of showing them", () => {
    const body = wire.answered.body;
    expect(() => parseReview({ ...body, schema: "html.v1" })).toThrow(HubApiError);
    expect(() => parseReview({ ...body, dataset_digest: "aaaa" })).toThrow(HubApiError);
    expect(() => parseReview({ ...body, answers: { classification: { ...body.answers.classification, value: "delete" } } })).toThrow(HubApiError);
    expect(() => parseReview({ ...body, answers: { ghost: body.answers.classification } })).toThrow(HubApiError);
    expect(() => parseReview({ ...body, items: [...body.items, body.items[0]] })).toThrow(HubApiError);
    expect(() => parseReview({ ...body, items: [{ ...body.items[0], asset_id: "https://example.com/x.las" }] })).toThrow(HubApiError);
    expect(() => parseReview({ ...wire.completed.body, answers: body.answers })).toThrow(HubApiError);
    expect(() => parseReviewPage(wire.list.body, "treedar")).toThrow(HubApiError);
  });
});

describe("review requests", () => {
  it("lists by project with a bounded page", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json(wire.list.body));
    await listReviews("ledge-top-ops", { component: wire.detail.body.component_id }, undefined, fetchImpl);
    expect(fetchImpl.mock.calls[0][0]).toBe(`/api/hub/v1/interactive-reviews?project=ledge-top-ops&limit=100&component_id=${wire.detail.body.component_id}`);
  });
  it("posts answers with CSRF, the seen revision and a request key, and checks choices first", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json(wire.answered.body));
    const review = detail();
    await sendAnswers(review, { expected_revision: 1, client_request_id: "r1", answers: [{ item_id: "classification", value: "keep" }], complete: false }, "csrf", fetchImpl);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe(`/api/hub/v1/interactive-reviews/${id}/answers`);
    expect((init!.headers as Record<string, string>)["X-Hub-CSRF"]).toBe("csrf");
    expect(JSON.parse(init!.body as string)).toEqual({ expected_revision: 1, client_request_id: "r1", answers: [{ item_id: "classification", value: "keep" }], complete: false });
    await expect(sendAnswers(review, { expected_revision: 1, client_request_id: "r2", answers: [{ item_id: "classification", value: "delete" }], complete: false },
      "csrf", fetchImpl)).rejects.toMatchObject({ code: "invalid_review_answer" });
    await expect(sendAnswers(review, { expected_revision: 1, client_request_id: "r3", answers: [], complete: false }, "csrf", fetchImpl))
      .rejects.toMatchObject({ code: "invalid_review_answers" });
    await expect(sendAnswers(parseReview(wire.completed.body), { expected_revision: 3, client_request_id: "r4", answers: [], complete: true }, "csrf", fetchImpl))
      .rejects.toMatchObject({ code: "interactive_review_completed" });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
  it("reads assets only from the fixed review route", () => {
    expect(assetPath(id, wire.detail.body.component_id)).toBe(`/api/hub/v1/interactive-reviews/${id}/assets/${wire.detail.body.component_id}`);
    expect(() => assetPath(id, "../../etc")).toThrow(HubApiError);
  });
});

describe("review drafts and routes", () => {
  it("counts only changed, non-blank answers as unsaved", () => {
    const review: Review = parseReview(wire.answered.body);
    expect(unsavedAnswers(review, { classification: "keep", note: "  " })).toEqual([]);
    expect(unsavedAnswers(review, { classification: "skip", note: "Looks right" }))
      .toEqual([{ item_id: "classification", value: "skip" }, { item_id: "note", value: "Looks right" }]);
    expect(progressText(1, 2)).toBe("1 of 2 answered");
    expect(progressText(2, 2)).toBe("All 2 answered");
  });
  it("routes /projects/{id}/reviews/{uuid} and nothing looser", () => {
    expect(parseRoute(`/projects/ledge-top-ops/reviews/${id}`)).toEqual({ page: "review", project: "ledge-top-ops", id });
    expect(pathOf({ page: "review", project: "ledge-top-ops", id })).toBe(`/projects/ledge-top-ops/reviews/${id}`);
    expect(parseRoute("/projects/ledge-top-ops/reviews/not-a-uuid")).toEqual({ page: "project", id: "ledge-top-ops", tab: "overview" });
  });
  it("demo store finishes only when every question is answered", () => {
    const store = new DemoReviewStore();
    const [summary] = store.list("treedar").items;
    expect(() => store.answer(summary.id, { expected_revision: 1, client_request_id: "a", answers: [], complete: true })).toThrow(expect.objectContaining({ code: "review_answers_incomplete" }));
    const saved = store.answer(summary.id, { expected_revision: 1, client_request_id: "b", answers: [{ item_id: "crown-1", value: "yes" }], complete: false });
    expect(saved.revision).toBe(2);
    expect(() => store.answer(summary.id, { expected_revision: 1, client_request_id: "c", answers: [{ item_id: "crown-2", value: "no" }], complete: false }))
      .toThrow(expect.objectContaining({ code: "review_conflict" }));
  });
});

describe("answer batching", () => {
  const bytes = (value: unknown) => new TextEncoder().encode(JSON.stringify(value)).length;
  it("splits 100 long Unicode answers into several saves, each within 48 KiB with its envelope", () => {
    const answers = Array.from({ length: 100 }, (_, i) => ({ item_id: `q${i}`, value: "\u6811\u{1F333}".repeat(666) + "x".repeat(2000 - 666 * 3) }));
    expect(answers.every((answer) => answer.value.length === 2000)).toBe(true);
    const batches = answerBatches(answers);
    expect(batches.length).toBeGreaterThan(1);
    expect(batches.flat()).toEqual(answers);
    for (const batch of batches) {
      expect(batch.length).toBeGreaterThan(0);
      expect(bytes({ expected_revision: Number.MAX_SAFE_INTEGER, client_request_id: crypto.randomUUID(), answers: batch, complete: false }))
        .toBeLessThanOrEqual(ANSWER_BATCH_BYTES);
    }
  });
  it("still caps short answers at 100 per save and never makes an empty batch", () => {
    const answers = Array.from({ length: 250 }, (_, i) => ({ item_id: `q${i}`, value: "a" }));
    expect(answerBatches(answers).map((batch) => batch.length)).toEqual([100, 100, 50]);
    expect(answerBatches([])).toEqual([]);
  });
  it("refuses to send a body over the limit even if called directly", async () => {
    const items = Array.from({ length: 40 }, (_, i) => ({ id: `q${i}`, title: `Q${i}`, prompt: "Say something." }));
    const review = parseReview({ ...wire.detail.body, items, answers: {} });
    const fetchImpl = vi.fn<typeof fetch>();
    await expect(sendAnswers(review, { expected_revision: 1, client_request_id: "r", complete: false,
      answers: items.map((item) => ({ item_id: item.id, value: "\u6811".repeat(2000) })) }, "csrf", fetchImpl)).rejects.toMatchObject({ code: "invalid_review_answers" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
