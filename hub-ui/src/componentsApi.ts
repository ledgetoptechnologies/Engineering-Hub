/**
 * Components: the apps, services and packages that make up a project.
 * People create, edit and retire them; agents only read. Nothing here grants access or starts work.
 */
import { HubApiError, isMissingRoute } from "./api";
import { isProjectId } from "./projectsApi";

export const COMPONENT_KINDS = ["web_app", "desktop_app", "mobile_app", "service", "package", "library", "tool", "other"] as const;
export type ComponentKind = typeof COMPONENT_KINDS[number];
export const COMPONENT_STATUSES = ["active", "planned", "paused", "retired"] as const;
export type ComponentStatus = typeof COMPONENT_STATUSES[number];
/** Retiring has its own action; the editor only offers the others. */
export const EDITABLE_STATUSES = ["active", "planned", "paused"] as const satisfies readonly ComponentStatus[];

export interface ProjectComponent {
  id: string; project: string; slug: string; name: string; description: string;
  kind: ComponentKind; status: ComponentStatus;
  repository_url: string | null; package_url: string | null; legacy_project_id: string | null;
  created_by: string; created_at: string; updated_at: string; revision: number;
}
export interface ComponentPage { items: ProjectComponent[]; next_after: string | null }
export interface ComponentFields {
  name: string; description: string; kind: ComponentKind; status: ComponentStatus;
  repository_url: string | null; package_url: string | null; legacy_project_id: string | null;
}
export interface CreateComponentInput extends ComponentFields { slug: string; client_request_id: string }
export type UpdateComponentInput = Partial<ComponentFields> & { expected_revision: number };

export const KIND_LABEL: Record<ComponentKind, string> = {
  web_app: "Web app", desktop_app: "Desktop app", mobile_app: "Mobile app", service: "Service",
  package: "Package", library: "Library", tool: "Tool", other: "Other",
};
export const COMPONENT_STATUS_LABEL: Record<ComponentStatus, string> = {
  active: "Active", planned: "Planned", paused: "Paused", retired: "Retired",
};
export const COMPONENT_STATUS_TONE: Record<ComponentStatus, string> = {
  active: "info", planned: "neutral", paused: "amber", retired: "neutral",
};

export const NAME_MAX = 100;
export const DESCRIPTION_MAX = 4096;
export const URL_MAX = 2048;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const isComponentId = (value: unknown): value is string => typeof value === "string" && uuid.test(value);
export const isSlug = (value: unknown): value is string => typeof value === "string" && /^[a-z0-9][a-z0-9-]{0,63}$/.exec(value)?.[0] === value;
const safeText = (value: unknown, max: number): value is string => typeof value === "string" && value.length <= max
  && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value);
const isTime = (value: unknown): value is string => typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value));
/** Links are https only and never carry a username or password. */
export function validUrl(value: unknown): value is string | null {
  if (value === null) return true;
  // Printable ASCII only, as the Hub requires: no spaces, backslashes or look-alike characters.
  if (typeof value !== "string" || value.length > URL_MAX || !/^[\x21-\x7e]+$/.test(value) || value.includes("\\")) return false;
  try { const url = new URL(value); return url.protocol === "https:" && !url.username && !url.password; } catch { return false; }
}

function fields(value: Partial<ComponentFields>): Partial<ComponentFields> {
  const result: Partial<ComponentFields> = {};
  const bad = () => new HubApiError(422, "invalid_component");
  if (value.name !== undefined) { if (!safeText(value.name, NAME_MAX) || !value.name.trim()) throw bad(); result.name = value.name; }
  if (value.description !== undefined) { if (!safeText(value.description, DESCRIPTION_MAX)) throw bad(); result.description = value.description; }
  if (value.kind !== undefined) { if (!COMPONENT_KINDS.includes(value.kind)) throw bad(); result.kind = value.kind; }
  if (value.status !== undefined) { if (!COMPONENT_STATUSES.includes(value.status)) throw bad(); result.status = value.status; }
  if (value.repository_url !== undefined) { if (!validUrl(value.repository_url)) throw bad(); result.repository_url = value.repository_url; }
  if (value.package_url !== undefined) { if (!validUrl(value.package_url)) throw bad(); result.package_url = value.package_url; }
  if (value.legacy_project_id !== undefined) {
    if (value.legacy_project_id !== null && !isProjectId(value.legacy_project_id)) throw bad();
    result.legacy_project_id = value.legacy_project_id;
  }
  return result;
}

export function parseComponent(value: unknown, expected?: { id?: string; project?: string }): ProjectComponent {
  const bad = () => new HubApiError(502, "invalid_component_response");
  if (!value || typeof value !== "object" || Array.isArray(value)) throw bad();
  const row = value as Record<string, unknown>;
  if (!isComponentId(row.id) || (expected?.id !== undefined && row.id !== expected.id)
      || typeof row.project !== "string" || !isProjectId(row.project) || (expected?.project !== undefined && row.project !== expected.project)
      || !isSlug(row.slug) || !safeText(row.name, NAME_MAX) || !row.name.trim()
      || !(row.description === null || row.description === undefined || safeText(row.description, DESCRIPTION_MAX))
      || !COMPONENT_KINDS.includes(row.kind as ComponentKind) || !COMPONENT_STATUSES.includes(row.status as ComponentStatus)
      || !validUrl(row.repository_url ?? null) || !validUrl(row.package_url ?? null)
      || !(row.legacy_project_id === null || row.legacy_project_id === undefined
        || (typeof row.legacy_project_id === "string" && isProjectId(row.legacy_project_id)))
      || !safeText(row.created_by, 200) || !isTime(row.created_at) || !isTime(row.updated_at)
      || !Number.isSafeInteger(row.revision) || (row.revision as number) < 1) throw bad();
  return { id: row.id, project: row.project, slug: row.slug, name: row.name, description: (row.description as string | null | undefined) ?? "",
    kind: row.kind as ComponentKind, status: row.status as ComponentStatus,
    repository_url: (row.repository_url as string | null | undefined) ?? null, package_url: (row.package_url as string | null | undefined) ?? null,
    legacy_project_id: (row.legacy_project_id as string | null | undefined) ?? null,
    created_by: row.created_by, created_at: row.created_at, updated_at: row.updated_at, revision: row.revision as number };
}
export function parseComponentPage(value: unknown, project: string): ComponentPage {
  const bad = () => new HubApiError(502, "invalid_component_response");
  if (!value || typeof value !== "object" || Array.isArray(value)) throw bad();
  const row = value as Record<string, unknown>;
  if (!Array.isArray(row.items) || row.items.length > 100
      || !(row.next_after === null || row.next_after === undefined || isComponentId(row.next_after))) throw bad();
  const items = row.items.map((item) => parseComponent(item, { project }));
  if (new Set(items.map((item) => item.id)).size !== items.length) throw bad();
  return { items, next_after: (row.next_after as string | null | undefined) || null };
}

/** The named server code wins; only an unnamed 404 or a gateway refusal means "this server has no components yet". */
async function failure(response: Response): Promise<HubApiError> {
  let code: unknown;
  try { code = ((await response.clone().json()) as { detail?: { code?: unknown } })?.detail?.code; } catch { /* not JSON */ }
  const named = typeof code === "string" && /^[a-z0-9_]{1,64}$/.test(code) ? code : null;
  if (named && !named.startsWith("gateway_")) {
    if (named === "revision_conflict") return new HubApiError(409, "component_conflict");
    if (named === "not_found") return new HubApiError(404, "component_not_found");
    if (named === "invalid_input" || named.startsWith("invalid_component")) return new HubApiError(response.status, "invalid_component");
    return new HubApiError(response.status, named);
  }
  if (await isMissingRoute(response)) return new HubApiError(404, "components_unavailable");
  // Server/proxy text is deliberately not reflected into the interface.
  return new HubApiError(response.status, response.status === 409 ? "component_conflict" : response.status === 401 ? "sign_in_required"
    : response.status === 403 ? "forbidden" : response.status === 422 ? "invalid_component" : "component_request_failed");
}
async function request(path: string, method: "GET" | "POST" | "PATCH", csrf: string | undefined, payload: unknown,
  fetchImpl: typeof fetch, signal?: AbortSignal): Promise<unknown> {
  if (method !== "GET" && !csrf) throw new HubApiError(403, "csrf_required");
  let response: Response;
  try {
    response = await fetchImpl.call(globalThis, `/api/hub/v1${path}`, { method, credentials: "same-origin", redirect: "error", cache: "no-store",
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
      headers: { Accept: "application/json", ...(method === "GET" ? {} : { "Content-Type": "application/json", "X-Hub-CSRF": csrf! }) },
      ...(method === "GET" ? {} : { body: JSON.stringify(payload) }) });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new HubApiError(0, "network_unavailable");
  }
  if (!response.ok) throw await failure(response);
  try { return await response.json(); } catch { throw new HubApiError(502, "invalid_component_response"); }
}
function projectPath(project: string): string { if (!isProjectId(project)) throw new HubApiError(422, "invalid_project"); return `/projects/${project}/components`; }
function componentPath(id: string): string { if (!isComponentId(id)) throw new HubApiError(422, "invalid_component"); return `/components/${id}`; }

/** Retired components are included, so the panel can offer them under "Show retired". */
export async function listComponents(project: string, after: string | null = null, signal?: AbortSignal,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<ComponentPage> {
  if (after !== null && !isComponentId(after)) throw new HubApiError(422, "invalid_component");
  const query = `?include_retired=true&limit=100${after ? `&after=${after}` : ""}`;
  return parseComponentPage(await request(projectPath(project) + query, "GET", undefined, undefined, fetchImpl, signal), project);
}
export async function readComponent(id: string, fetchImpl: typeof fetch = globalThis.fetch): Promise<ProjectComponent> {
  return parseComponent(await request(componentPath(id), "GET", undefined, undefined, fetchImpl), { id });
}
export async function createComponent(project: string, input: CreateComponentInput, csrf: string,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<ProjectComponent> {
  if (!isSlug(input.slug) || !safeText(input.client_request_id, 128) || !input.client_request_id.trim()) throw new HubApiError(422, "invalid_component");
  const payload = { slug: input.slug, client_request_id: input.client_request_id, ...fields(input) };
  return parseComponent(await request(projectPath(project), "POST", csrf, payload, fetchImpl), { project });
}
export async function updateComponent(id: string, input: UpdateComponentInput, csrf: string,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<ProjectComponent> {
  if (!Number.isSafeInteger(input.expected_revision) || input.expected_revision < 1) throw new HubApiError(422, "invalid_component_revision");
  const { expected_revision, ...rest } = input;
  return parseComponent(await request(componentPath(id), "PATCH", csrf, { expected_revision, ...fields(rest) }, fetchImpl), { id });
}
export async function retireComponent(id: string, revision: number, csrf: string,
  fetchImpl: typeof fetch = globalThis.fetch): Promise<ProjectComponent> {
  if (!Number.isSafeInteger(revision) || revision < 1) throw new HubApiError(422, "invalid_component_revision");
  return parseComponent(await request(`${componentPath(id)}/retire`, "POST", csrf, { expected_revision: revision }, fetchImpl), { id });
}

/** Only the fields a person changed are sent, so an edit never rewrites what they didn't touch. */
export function changedFields(base: ProjectComponent, draft: ComponentFields): Partial<ComponentFields> {
  const result: Partial<ComponentFields> = {};
  for (const key of Object.keys(draft) as (keyof ComponentFields)[]) {
    if (draft[key] !== base[key]) (result as Record<string, unknown>)[key] = draft[key];
  }
  return result;
}
/** Active first, then planned, paused, retired; by name within each. */
export function sortComponents(items: ProjectComponent[]): ProjectComponent[] {
  return [...items].sort((a, b) => COMPONENT_STATUSES.indexOf(a.status) - COMPONENT_STATUSES.indexOf(b.status)
    || a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
}
/** "3 components · 1 planned" style summary for cards and umbrella overviews. */
export function componentSummary(items: ProjectComponent[]): string {
  const live = items.filter((item) => item.status !== "retired");
  if (!live.length) return items.length ? "No current components" : "No components yet";
  const extra = (["planned", "paused"] as const).map((status) => [status, live.filter((item) => item.status === status).length] as const)
    .filter(([, count]) => count).map(([status, count]) => `${count} ${status}`);
  return [`${live.length} component${live.length === 1 ? "" : "s"}`, ...extra].join(" · ");
}
