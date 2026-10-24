/** Demo-only review. The questions are placeholders, and its sample asset deliberately doesn't load in demo mode. */
import { HubApiError } from "./api";
import type { AnswerRequest, Review, ReviewPage, ReviewSummary } from "./reviewsApi";

const at = "2026-10-20T15:00:00.000Z";
export const DEMO_REVIEW_ID = "d0e0a000-0000-4000-8000-000000000001";

export class DemoReviewStore {
  constructor(override: Partial<Review> = {}) { this.review = { ...this.review, ...override }; }
  private review: Review = {
    id: DEMO_REVIEW_ID, project: "treedar", component_id: "c0a1e000-0000-4000-8000-000000000004", component_revision: 1,
    topic: "demo-review", dataset_digest: "sha256:" + "b".repeat(64), schema: "choice-text.v1", title: "Check sample tree crowns",
    state: "open", created_by: "codex", created_at: at, updated_at: at, revision: 1, answers: {},
    items: [
      { id: "crown-1", title: "Tree 1", prompt: "Is the outlined crown a single tree?",
        choices: [{ id: "yes", label: "Yes, one tree" }, { id: "no", label: "No, several trees" }, { id: "unsure", label: "Can't tell" }],
        asset_id: "a0a0a000-0000-4000-8000-000000000001" },
      { id: "crown-2", title: "Tree 2", prompt: "Is the outlined crown a single tree?",
        choices: [{ id: "yes", label: "Yes, one tree" }, { id: "no", label: "No, several trees" }, { id: "unsure", label: "Can't tell" }] },
      { id: "notes", title: "Anything else", prompt: "Note anything the processing got wrong." },
    ],
  };

  list(project: string): ReviewPage {
    if (project !== this.review.project) return { items: [], next_after: null };
    const { items, answers, ...rest } = this.review;
    const summary: ReviewSummary = { ...rest, item_count: items.length, answered_count: Object.keys(answers).length };
    return { items: [summary], next_after: null };
  }
  read(id: string): Review {
    if (id !== this.review.id) throw new HubApiError(404, "review_not_found");
    return structuredClone(this.review);
  }
  answer(id: string, input: AnswerRequest, by = "human:1"): Review {
    const review = this.read(id);
    if (review.state !== "open") throw new HubApiError(409, "interactive_review_completed");
    if (input.expected_revision !== review.revision) throw new HubApiError(409, "review_conflict");
    const now = new Date().toISOString();
    for (const answer of input.answers) review.answers[answer.item_id] = { value: answer.value, by, at: now };
    if (input.complete && Object.keys(review.answers).length !== review.items.length) throw new HubApiError(409, "review_answers_incomplete");
    if (input.complete) review.state = "completed";
    this.review = { ...review, revision: review.revision + 1, updated_at: now };
    return this.read(id);
  }
}
