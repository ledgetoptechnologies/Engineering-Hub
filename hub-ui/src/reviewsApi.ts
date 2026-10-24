/**
 * Interactive reviews: an agent asks a fixed list of choice or short-text questions about a component,
 * and a person answers them here. Questions are plain text; nothing an agent supplies is run or rendered as HTML.
 */
import { HubApiError, isMissingRoute } from "./api";
import { isComponentId } from "./componentsApi";
import { isProjectId } from "./projectsApi";

export const ANSWER_MAX = 2000;
/** The Hub takes at most this many answers per save; longer reviews save in several batches. */
export const ANSWER_BATCH = 100;
const ITEM_MAX = 1000;
const keyPattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/;
const digestPattern = /^sha256:[a-f0-9]{64}$/;

export interface ReviewChoice { id: string; label: string }
export interface ReviewItem { id: string; title: string; prompt: string; choices?: ReviewChoice[]; asset_id?: string }
export interface ReviewAnswer { value: string; by: string; at: string }
interface ReviewBase {
  id: string; project: string; component_id: string; component_revision: number; topic: string; dataset_digest: string;
  schema: "choice-text.v1"; title: string; state: "open" | "completed"; created_by: string; created_at: string; updated_at: string; revision: number;
}
export interface ReviewSummary extends ReviewBase { item_count: number; answered_count: number }
export interface Review extends ReviewBase { items: ReviewItem[]; answers: Record<string, ReviewAnswer> }
export interface ReviewPage { items: ReviewSummary[]; next_after: string | null }
export interface AnswerRequest { expected_revision: number; client_request_id: string; answers: { item_id: string; value: string }[]; complete: boolean }

const isKey = (value: unknown): value is string => typeof value === "string" && keyPattern.test(value);
const text = (value: unknown, max: number, empty = false): value is string => typeof value === "string" && value.length <= max
  && (empty || value.trim() !== "") && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value);
const isTime = (value: unknown): value is string => typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value));
const count = (value: unknown, max: number): value is number => Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= max;
const bad = () => new HubApiError(502, "invalid_review_response");

function base(row: Record<string, unknown>, expected?: { id?: string; project?: string }): ReviewBase {
  if (!isComponentId(row.id) || (expected?.id !== undefined && row.id !== expected.id)
      || typeof row.project !== "string" || !isProjectId(row.project) || (expected?.project !== undefined && row.project !== expected.project)
      || !isComponentId(row.component_id) || !Number.isSafeInteger(row.component_revision) || (row.component_revision as number) < 1
      || !text(row.topic, 200) || typeof row.dataset_digest !== "string" || !digestPattern.test(row.dataset_digest)
      || row.schema !== "choice-text.v1" || !text(row.title, 100) || (row.state !== "open" && row.state !== "completed")
      || !text(row.created_by, 200) || !isTime(row.created_at) || !isTime(row.updated_at)
      || !Number.isSafeInteger(row.revision) || (row.revision as number) < 1) throw bad();
  return { id: row.id, project: row.project, component_id: row.component_id, component_revision: row.component_revision as number,
    topic: row.topic, dataset_digest: row.dataset_digest, schema: "choice-text.v1", title: row.title, state: row.state,
    created_by: row.created_by, created_at: row.created_at, updated_at: row.updated_at, revision: row.revision as number };
}
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw bad();
  return value as Record<string, unknown>;
}
export function parseReviewSummary(value: unknown, project?: string): ReviewSummary {
  const row = object(value);
  if (!count(row.item_count, ITEM_MAX) || (row.item_count as number) < 1 || !count(row.answered_count, row.item_count as number)) throw bad();
  return { ...base(row, { project }), item_count: row.item_count as number, answered_count: row.answered_count as number };
}
function parseItem(value: unknown): ReviewItem {
  const row = object(value);
  if (!isKey(row.id) || !text(row.title, 100) || !text(row.prompt, 4096)
      || !(row.asset_id === undefined || row.asset_id === null || isComponentId(row.asset_id))) throw bad();
  const item: ReviewItem = { id: row.id, title: row.title, prompt: row.prompt };
  if (row.choices !== undefined && row.choices !== null) {
    if (!Array.isArray(row.choices) || row.choices.length < 2 || row.choices.length > 20) throw bad();
    item.choices = row.choices.map((choice) => {
      const entry = object(choice);
      if (!isKey(entry.id) || !text(entry.label, 100)) throw bad();
      return { id: entry.id, label: entry.label };
    });
    if (new Set(item.choices.map((choice) => choice.id)).size !== item.choices.length) throw bad();
  }
  if (typeof row.asset_id === "string") item.asset_id = row.asset_id;
  return item;
}
export function parseReview(value: unknown, expected?: { id?: string; project?: string }): Review {
  const row = object(value);
  const result = base(row, expected);
  if (!Array.isArray(row.items) || row.items.length < 1 || row.items.length > ITEM_MAX) throw bad();
  const items = row.items.map(parseItem);
  const byId = new Map(items.map((item) => [item.id, item]));
  if (byId.size !== items.length) throw bad();
  const answers: Record<string, ReviewAnswer> = {};
  for (const [id, answer] of Object.entries(object(row.answers))) {
    const item = byId.get(id), entry = object(answer);
    if (!item || !text(entry.value, ANSWER_MAX) || !text(entry.by, 200) || !isTime(entry.at)
        || (item.choices && !item.choices.some((choice) => choice.id === entry.value))) throw bad();
    answers[id] = { value: entry.value, by: entry.by, at: entry.at };
  }
  if (result.state === "completed" && Object.keys(answers).length !== items.length) throw bad();
  return { ...result, items, answers };
}
export function parseReviewPage(value: unknown, project: string): ReviewPage {
  const row = object(value);
  if (!Array.isArray(row.items) || row.items.length > 100
      || !(row.next_after === null || row.next_after === undefined || isComponentId(row.next_after))) throw bad();
  const items = row.items.map((item) => parseReviewSummary(item, project));
  if (new Set(items.map((item) => item.id)).size !== items.length) throw bad();
  return { items, next_after: (row.next_after as string | null | undefined) ?? null };
}

const conflicts: Record<string, string> = { revision_conflict: "review_conflict", not_found: "review_not_found" };
/** The named server code wins; only an unnamed 404 or a gateway refusal means "this Hub has no reviews yet". */
async function failure(response: Response): Promise<HubApiError> {
  let code: unknown;
  try { code = ((await response.clone().json()) as { detail?: { code?: unknown } })?.detail?.code; } catch { /* not JSON */ }
  if (typeof code === "string" && /^[a-z0-9_]{1,64}$/.test(code) && !code.startsWith("gateway_"))
    return new HubApiError(response.status, conflicts[code] ?? code);
  if (await isMissingRoute(response)) return new HubApiError(404, "reviews_unavailable");
  return new HubApiError(response.status, response.status === 409 ? "review_conflict" : response.status === 401 ? "sign_in_required"
    : response.status === 403 ? "forbidden" : "review_request_failed");
}
async function request(path: string, csrf: string | null, payload: unknown, fetchImpl: typeof fetch, signal?: AbortSignal): Promise<unknown> {
  const post = payload !== undefined;
  if (post && !csrf) throw new HubApiError(403, "csrf_required");
  let response: Response;
  try {
    response = await fetchImpl.call(globalThis, `/api/hub/v1/interactive-reviews${path}`, { method: post ? "POST" : "GET",
      credentials: "same-origin", redirect: "error", cache: "no-store",
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
      headers: { Accept: "application/json", ...(post ? { "Content-Type": "application/json", "X-Hub-CSRF": csrf! } : {}) },
      ...(post ? { body: JSON.stringify(payload) } : {}) });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new HubApiError(0, "network_unavailable");
  }
  if (!response.ok) throw await failure(response);
  try { return await response.json(); } catch { throw bad(); }
}
const reviewPath = (id: string) => { if (!isComponentId(id)) throw new HubApiError(422, "invalid_review"); return `/${id}`; };

export async function listReviews(project: string, options: { component?: string; after?: string | null } = {}, signal?: AbortSignal,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<ReviewPage> {
  if (!isProjectId(project) || (options.component !== undefined && !isComponentId(options.component))
      || (options.after && !isComponentId(options.after))) throw new HubApiError(422, "invalid_review");
  const query = new URLSearchParams({ project, limit: "100" });
  if (options.component) query.set("component_id", options.component);
  if (options.after) query.set("after", options.after);
  return parseReviewPage(await request(`?${query}`, null, undefined, fetchImpl, signal), project);
}
export async function readReview(id: string, signal?: AbortSignal, fetchImpl: typeof fetch = globalThis.fetch): Promise<Review> {
  return parseReview(await request(reviewPath(id), null, undefined, fetchImpl, signal), { id });
}
export async function sendAnswers(review: Review, input: AnswerRequest, csrf: string, fetchImpl: typeof fetch = globalThis.fetch): Promise<Review> {
  if (review.state !== "open") throw new HubApiError(409, "interactive_review_completed");
  if (!Number.isSafeInteger(input.expected_revision) || input.expected_revision < 1 || !text(input.client_request_id, 128)
      || input.answers.length > ANSWER_BATCH || (!input.answers.length && !input.complete)) throw new HubApiError(422, "invalid_review_answers");
  for (const answer of input.answers) {
    const item = review.items.find((entry) => entry.id === answer.item_id);
    if (!item || !text(answer.value, ANSWER_MAX) || (item.choices && !item.choices.some((choice) => choice.id === answer.value)))
      throw new HubApiError(422, "invalid_review_answer");
  }
  return parseReview(await request(`${reviewPath(review.id)}/answers`, csrf, input, fetchImpl), { id: review.id, project: review.project });
}
/** The one fixed address an item's asset may be read from; never a URL the review supplies. */
export function assetPath(reviewId: string, assetId: string): string {
  if (!isComponentId(reviewId) || !isComponentId(assetId)) throw new HubApiError(422, "invalid_review");
  return `/api/hub/v1/interactive-reviews/${reviewId}/assets/${assetId}`;
}

/** Answers typed here that differ from what the Hub has saved. Blank text is not an answer. */
export function unsavedAnswers(review: Review, drafts: Record<string, string>): { item_id: string; value: string }[] {
  return review.items.filter((item) => drafts[item.id] !== undefined && drafts[item.id].trim() !== ""
    && drafts[item.id] !== review.answers[item.id]?.value).map((item) => ({ item_id: item.id, value: drafts[item.id] }));
}
export const progressText = (answered: number, total: number) =>
  answered === total ? `All ${total} answered` : `${answered} of ${total} answered`;
