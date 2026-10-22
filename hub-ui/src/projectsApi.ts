/** Human project metadata operations. These never enroll an executor or grant agent access. */
import { HubApiError, isMissingRoute } from "./api";
import type { Agent, Project } from "./types";

export const PROJECT_STATUSES = ["on_track", "at_risk", "off_track", "paused"] as const;
export type ProjectStatus = typeof PROJECT_STATUSES[number];
export interface ProjectManagementProject extends Project {
  description?: string;
  repository_url?: string | null;
  lead_agent?: Agent | null;
  status?: ProjectStatus;
  parent_project?: string | null;
  archived?: boolean;
  revision?: number;
  created_at?: string;
  updated_at?: string;
}
export interface ProjectMetadata {
  name: string;
  description: string;
  repository_url: string | null;
  lead_agent: Agent | null;
  status: ProjectStatus;
  parent_project: string | null;
}
export interface CreateProjectInput extends ProjectMetadata { project_id: string; client_request_id: string }
export type UpdateProjectInput = Partial<ProjectMetadata> & { expected_revision: number };
export const isProjectId = (value: string) => /^[a-z0-9][a-z0-9-]{0,63}$/.exec(value)?.[0] === value;
const safeText = (value: unknown, max: number) => typeof value === "string" && value.length <= max
  && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value);
function validRepository(value: unknown): boolean {
  if (value === null) return true;
  if (typeof value !== "string" || value.length > 2048) return false;
  try { const url = new URL(value); return url.protocol === "https:" && !url.username && !url.password; } catch { return false; }
}
function metadata(value: UpdateProjectInput | CreateProjectInput): Partial<ProjectMetadata> {
  const result: Partial<ProjectMetadata> = {};
  if (value.name !== undefined) {
    if (!safeText(value.name, 200) || !value.name.trim()) throw new HubApiError(422, "invalid_project_metadata");
    result.name = value.name;
  }
  if (value.description !== undefined) {
    if (!safeText(value.description, 4000)) throw new HubApiError(422, "invalid_project_metadata");
    result.description = value.description;
  }
  if (value.repository_url !== undefined) {
    if (!validRepository(value.repository_url)) throw new HubApiError(422, "invalid_project_metadata");
    result.repository_url = value.repository_url;
  }
  if (value.lead_agent !== undefined) {
    if (value.lead_agent !== null && !["codex", "hermes", "claude"].includes(value.lead_agent)) throw new HubApiError(422, "invalid_project_metadata");
    result.lead_agent = value.lead_agent;
  }
  if (value.status !== undefined) {
    if (!PROJECT_STATUSES.includes(value.status)) throw new HubApiError(422, "invalid_project_metadata");
    result.status = value.status;
  }
  if (value.parent_project !== undefined) {
    if (value.parent_project !== null && !isProjectId(value.parent_project)) throw new HubApiError(422, "invalid_project_metadata");
    result.parent_project = value.parent_project;
  }
  return result;
}
export function parseProject(value: unknown, expectedId?: string): ProjectManagementProject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new HubApiError(502, "invalid_project_response");
  const row = value as Record<string, unknown>;
  if (typeof row.id !== "string" || !isProjectId(row.id) || (expectedId !== undefined && row.id !== expectedId)
      || !safeText(row.name, 200) || !(row.name as string).trim() || !Number.isSafeInteger(row.revision) || (row.revision as number) < 1
      || typeof row.archived !== "boolean" || !safeText(row.environment, 128) || typeof row.executor_enabled !== "boolean"
      || !safeText(row.health, 128) || !safeText(row.reason, 256)) throw new HubApiError(502, "invalid_project_response");
  try { metadata(row as unknown as UpdateProjectInput); } catch { throw new HubApiError(502, "invalid_project_response"); }
  return { id: row.id, name: row.name as string, environment: row.environment as string,
    executor_enabled: row.executor_enabled, health: row.health as string, reason: row.reason as string,
    description: typeof row.description === "string" ? row.description : "", repository_url: row.repository_url as string | null | undefined,
    lead_agent: row.lead_agent as Agent | null | undefined, status: row.status as ProjectStatus | undefined,
    parent_project: row.parent_project as string | null | undefined, archived: row.archived, revision: row.revision as number,
    created_at: typeof row.created_at === "string" ? row.created_at : undefined, updated_at: typeof row.updated_at === "string" ? row.updated_at : undefined };
}
async function request(path: string, method: "GET" | "POST" | "PATCH", csrf: string | undefined, payload: unknown,
  fetchImpl: typeof fetch): Promise<unknown> {
  if (method !== "GET" && !csrf) throw new HubApiError(403, "csrf_required");
  let response: Response;
  try {
    response = await fetchImpl.call(globalThis, `/api/hub/v1/projects${path}`, { method, credentials: "same-origin", redirect: "error", cache: "no-store",
      signal: AbortSignal.timeout(15000), headers: { Accept: "application/json", ...(method === "GET" ? {} : { "Content-Type": "application/json", "X-Hub-CSRF": csrf! }) },
      ...(method === "GET" ? {} : { body: JSON.stringify(payload) }) });
  } catch { throw new HubApiError(0, "network_unavailable"); }
  // A gateway or server without project routes is "not available", never a sign-out.
  if (!response.ok && await isMissingRoute(response)) throw new HubApiError(404, "projects_unavailable");
  // Server/proxy text is deliberately not reflected into the interface.
  if (!response.ok) throw new HubApiError(response.status, response.status === 409 ? "revision_conflict" : response.status === 401 ? "sign_in_required"
    : response.status === 403 ? "forbidden" : "project_request_failed");
  try { return await response.json(); } catch { throw new HubApiError(502, "invalid_project_response"); }
}
function path(id: string): string { if (!isProjectId(id)) throw new HubApiError(422, "invalid_project"); return `/${id}`; }
export async function readProject(id: string, fetchImpl: typeof fetch = globalThis.fetch): Promise<ProjectManagementProject> {
  return parseProject(await request(path(id), "GET", undefined, undefined, fetchImpl), id);
}
export async function createProject(input: CreateProjectInput, csrf: string, fetchImpl: typeof fetch = globalThis.fetch): Promise<ProjectManagementProject> {
  if (!isProjectId(input.project_id) || !safeText(input.client_request_id, 128) || !input.client_request_id.trim()) throw new HubApiError(422, "invalid_project");
  const payload = { project_id: input.project_id, client_request_id: input.client_request_id, ...metadata(input) };
  return parseProject(await request("", "POST", csrf, payload, fetchImpl), input.project_id);
}
export async function updateProject(id: string, input: UpdateProjectInput, csrf: string, fetchImpl: typeof fetch = globalThis.fetch): Promise<ProjectManagementProject> {
  if (!Number.isSafeInteger(input.expected_revision) || input.expected_revision < 1) throw new HubApiError(422, "invalid_project_revision");
  const payload = { expected_revision: input.expected_revision, ...metadata(input) };
  return parseProject(await request(path(id), "PATCH", csrf, payload, fetchImpl), id);
}
export async function archiveProject(id: string, revision: number, csrf: string, fetchImpl: typeof fetch = globalThis.fetch): Promise<ProjectManagementProject> {
  if (id === "engineering-hub" || !Number.isSafeInteger(revision) || revision < 1) throw new HubApiError(422, "invalid_project_archive");
  return parseProject(await request(`${path(id)}/archive`, "POST", csrf, { expected_revision: revision }, fetchImpl), id);
}
