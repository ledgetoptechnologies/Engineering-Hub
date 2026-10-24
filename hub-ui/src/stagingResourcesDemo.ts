/** Illustrative staging resources for ?demo=1. Nothing here talks to a staging PC. */
import { HubApiError } from "./api";
import type { CleanupPlan, CleanupResult, KeepRequest, ResourcePage, ResourceQuery, StagingResource } from "./stagingResourcesApi";

const HOUR = 3_600_000, DAY = 24 * HOUR;
const id = (n: number) => `5ea9e000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const demoActor = "human:demo";

export function createDemoResources(now: Date): StagingResource[] {
  const at = (offset: number) => new Date(now.getTime() + offset).toISOString();
  const base = (n: number, over: Partial<StagingResource>): StagingResource => ({
    id: id(n), kind: "preview", project: "engineering-hub", topic: "auto-wake-v1-test", job_id: id(900 + n),
    source_commit: "497021a5d3c19f086b12e52f964e22df84720040", artifact_digest: null, created_by: "codex",
    created_at: at(-DAY), updated_at: at(-DAY), last_viewed_at: null, expires_at: at(2 * DAY), state: "ready", size_bytes: 48_000_000,
    preview_path: null, keep: null, protected: [], revision: 1, ...over,
  });
  return [
    base(1, { kind: "preview", expires_at: at(2 * DAY + 3 * HOUR) }),
    base(2, { kind: "preview", project: "treedar", created_at: at(-4 * DAY), expires_at: at(-2 * HOUR) }),
    base(3, { kind: "workspace", created_by: "hermes", size_bytes: 1_200_000_000, expires_at: at(-6 * HOUR) }),
    base(4, { kind: "build", artifact_digest: `sha256:${"a".repeat(64)}`, size_bytes: 820_000_000, expires_at: null,
      protected: [{ reason: "rollback_target", ref: "release 497021a" }] }),
    base(5, { kind: "build", artifact_digest: `sha256:${"b".repeat(64)}`, size_bytes: 790_000_000, expires_at: at(9 * DAY),
      keep: { mode: "pinned", until: null, by: demoActor, reason: "Compare against the next release", at: at(-HOUR) } }),
    base(6, { kind: "test_run", size_bytes: null, expires_at: at(20 * HOUR) }),
    base(7, { kind: "workspace", state: "provisioning", size_bytes: null, expires_at: null,
      protected: [{ reason: "active_job", ref: `job ${id(907)}` }] }),
  ];
}

/** A tiny in-browser stand-in for the staging API, so keep and cleanup can be tried in demo mode. */
export class DemoResourceStore {
  private items: StagingResource[];
  constructor(private readonly now: () => Date = () => new Date()) { this.items = createDemoResources(now()); }

  list(query: ResourceQuery): ResourcePage {
    const items = this.items.filter(item => item.state !== "deleted"
      && (!query.project || item.project === query.project) && (!query.kind || item.kind === query.kind));
    return { items: items.map(item => ({ ...item })), next_after: null };
  }
  keep(resource: StagingResource, request: KeepRequest): StagingResource {
    const index = this.items.findIndex(item => item.id === resource.id);
    const current = this.items[index];
    if (!current) throw new HubApiError(404, "staging_resource_not_found");
    if (current.revision !== resource.revision) throw new HubApiError(409, "staging_resource_conflict");
    if (current.protected.length) throw new HubApiError(409, "staging_resource_protected");
    const at = this.now().toISOString();
    const keep = request.mode === "none" ? null
      : { mode: request.mode, until: request.mode === "until" ? request.until : null, by: demoActor, reason: request.reason, at };
    this.items[index] = { ...current, keep, updated_at: at, revision: current.revision + 1 };
    return { ...this.items[index] };
  }
  plan(project: string | null): CleanupPlan {
    const now = this.now().getTime();
    const scope = this.items.filter(item => item.state !== "deleted" && (!project || item.project === project));
    const remove: CleanupPlan["remove"] = [], kept: CleanupPlan["kept"] = [];
    for (const resource of scope) {
      const until = resource.keep?.mode === "until" ? resource.keep.until : resource.expires_at;
      if (resource.protected.length) kept.push({ id: resource.id, why: resource.protected[0].reason });
      else if (resource.keep?.mode === "pinned") kept.push({ id: resource.id, why: "pinned" });
      else if (until && Date.parse(until) <= now) remove.push({ id: resource.id, kind: resource.kind, size_bytes: resource.size_bytes, why: "expired" });
      else kept.push({ id: resource.id, why: resource.keep ? "keep_until" : "not_expired" });
    }
    const total = remove.every(entry => entry.size_bytes !== null) ? remove.reduce((sum, entry) => sum + (entry.size_bytes ?? 0), 0) : null;
    return { plan_id: id(800), digest: `sha256:${"d".repeat(64)}`, expires_at: new Date(now + 10 * 60_000).toISOString(), total_bytes: total, remove, kept };
  }
  apply(plan: CleanupPlan): CleanupResult {
    const result: CleanupResult = { removed: [], failed: [], skipped_changed: [] };
    for (const entry of plan.remove) {
      const index = this.items.findIndex(item => item.id === entry.id);
      const current = this.items[index];
      // Anything pinned, kept, protected or already gone since the plan is skipped, never removed.
      if (!current || current.state === "deleted" || current.protected.length || current.keep) result.skipped_changed.push(entry.id);
      else { this.items[index] = { ...current, state: "deleted", revision: current.revision + 1 }; result.removed.push(entry.id); }
    }
    return result;
  }
}
