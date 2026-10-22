/**
 * Optional agent-supplied context on decisions and tasks: a summary, safe links and
 * image attachments. Parsed defensively at render time; anything malformed is dropped
 * so one bad record can never break the page or smuggle markup.
 */
import { safeHref } from "./markdown";

export interface DetailLink { label: string; href: string }
export interface DetailImage { id: string; title: string; width: number; height: number; bytes: number }
export interface Details { summary: string; links: DetailLink[]; images: DetailImage[] }

const slug = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

const text = (value: unknown, max: number): string | null =>
  typeof value === "string" && value.length <= max ? value : null;
const dimension = (value: unknown): number | null =>
  Number.isSafeInteger(value) && (value as number) > 0 && (value as number) <= 20000 ? value as number : null;

export function attachmentUrl(id: string): string {
  if (!slug.test(id)) throw new Error("invalid attachment id");
  return `/api/hub/v1/attachments/${encodeURIComponent(id)}`;
}

/** Returns null when there is nothing safe to show. Never throws. */
export function parseDetails(value: unknown): Details | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const summary = text(row.summary ?? "", 4000) ?? "";
  const links: DetailLink[] = [];
  if (Array.isArray(row.links)) {
    for (const raw of row.links.slice(0, 10)) {
      const item = raw as Record<string, unknown> | null;
      const label = text(item?.label, 80)?.trim();
      const url = text(item?.url, 2048);
      const href = url ? safeHref(url) : null;
      if (label && href) links.push({ label, href });
    }
  }
  const images: DetailImage[] = [];
  if (Array.isArray(row.attachments)) {
    for (const raw of row.attachments.slice(0, 6)) {
      const item = raw as Record<string, unknown> | null;
      if (item?.kind !== "image" || typeof item.id !== "string" || !slug.test(item.id)) continue;
      const width = dimension(item.width), height = dimension(item.height);
      const bytes = Number.isSafeInteger(item.bytes) && (item.bytes as number) > 0
        && (item.bytes as number) <= MAX_IMAGE_BYTES ? item.bytes as number : null;
      const title = text(item.title, 120)?.trim() || "Attached image";
      if (width && height && bytes) images.push({ id: item.id, title, width, height, bytes });
    }
  }
  if (!summary.trim() && links.length === 0 && images.length === 0) return null;
  return { summary, links, images };
}
