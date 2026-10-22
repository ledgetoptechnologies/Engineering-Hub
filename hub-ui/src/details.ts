/**
 * Optional agent-supplied context on decisions and tasks: `details` text, safe links
 * and image attachments. Parsed defensively; anything malformed is dropped so one bad
 * record can never break the page or smuggle markup.
 */
import { HubApiError, isMissingRoute } from "./api";
import { safeHref } from "./markdown";

export interface DetailLink { label: string; href: string }
export interface DetailImage { id: string; title: string; bytes: number; mediaType: string }
export interface EntityContext { summary: string; links: DetailLink[] }

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const MEDIA = new Set(["image/png", "image/jpeg", "image/webp"]);

const text = (value: unknown, max: number): string | null =>
  typeof value === "string" && value.length <= max ? value : null;

/** Built from the validated id only; the server's own url field is ignored. */
export function attachmentUrl(id: string): string {
  if (!uuid.test(id)) throw new Error("invalid attachment id");
  return `/api/hub/v1/attachments/${id}/image`;
}

/** Context fields on a task or decision. Returns null when there is nothing safe to show. Never throws. */
export function parseContext(entity: unknown): EntityContext | null {
  if (!entity || typeof entity !== "object") return null;
  const row = entity as Record<string, unknown>;
  const summary = text(row.details ?? "", 4096) ?? "";
  const links: DetailLink[] = [];
  if (Array.isArray(row.links)) {
    for (const raw of row.links.slice(0, 8)) {
      const item = raw as Record<string, unknown> | null;
      const label = text(item?.label, 120)?.trim();
      const url = text(item?.url, 2048);
      const href = url ? safeHref(url) : null;
      if (label && href) links.push({ label, href });
    }
  }
  if (!summary.trim() && links.length === 0) return null;
  return { summary, links };
}

/** GET /{tasks|decisions}/{id}/attachments body. Invalid rows are dropped; a wrong entity is rejected. */
export function parseAttachments(value: unknown, entityId: string): DetailImage[] {
  if (!value || typeof value !== "object" || !Array.isArray((value as { items?: unknown }).items)) return [];
  const images: DetailImage[] = [];
  for (const raw of (value as { items: unknown[] }).items.slice(0, 12)) {
    const item = raw as Record<string, unknown> | null;
    if (!item || typeof item.id !== "string" || !uuid.test(item.id) || item.entity_id !== entityId) continue;
    if (typeof item.media_type !== "string" || !MEDIA.has(item.media_type)) continue;
    if (!Number.isSafeInteger(item.bytes) || (item.bytes as number) <= 0 || (item.bytes as number) > MAX_IMAGE_BYTES) continue;
    images.push({ id: item.id, title: text(item.title, 200)?.trim() || "Attached image",
      bytes: item.bytes as number, mediaType: item.media_type });
  }
  return images;
}

/** Null when this server has no attachments route; never treats a gateway refusal as a sign-out. */
export async function listAttachments(kind: "task" | "decision", entityId: string, signal: AbortSignal,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<DetailImage[] | null> {
  if (!uuid.test(entityId)) return null;
  const response = await fetchImpl.call(globalThis, `/api/hub/v1/${kind}s/${entityId}/attachments`, {
    method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store",
    headers: { Accept: "application/json" }, signal,
  });
  if (await isMissingRoute(response)) return null;
  if (!response.ok) throw new HubApiError(response.status, "attachments_failed");
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return null;
  return parseAttachments(await response.json(), entityId);
}
