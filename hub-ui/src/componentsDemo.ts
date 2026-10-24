/** Demo-only components. Names are placeholders; none has a live address, and live mode never sees these. */
import { HubApiError } from "./api";
import type { ComponentFields, ComponentPage, CreateComponentInput, ProjectComponent, UpdateComponentInput } from "./componentsApi";

const created = "2026-10-01T09:00:00.000Z";
const row = (n: number, project: string, slug: string, name: string, kind: ProjectComponent["kind"], status: ProjectComponent["status"],
  description: string, legacy: string | null = null): ProjectComponent => ({
  id: `c0a1e000-0000-4000-8000-${String(n).padStart(12, "0")}`, project, slug, name, description, kind, status,
  repository_url: null, package_url: null, legacy_project_id: legacy, created_by: "human:1", created_at: created, updated_at: created, revision: 1,
});

export class DemoComponentStore {
  private items: ProjectComponent[] = [
    row(1, "ledge-top-ops", "operations-viewer", "Operations Viewer", "web_app", "active", "Browser view of jobs, deliveries and client files."),
    row(2, "ledge-top-ops", "thumbnail-package", "Thumbnail package", "package", "active", "Builds the small preview images the viewer shows."),
    row(3, "ledge-top-ops", "windows-app", "Windows app", "desktop_app", "planned", "Desktop version of the viewer for office PCs."),
    row(4, "treedar", "processing", "TreeDAR processing", "service", "active", "Turns uploaded scans into tree measurements."),
    { ...row(8, "treedar", "field-scanner", "Field scanner", "mobile_app", "retired", "Replaced by uploads to the processing service."), revision: 2 },
    row(5, "project-alpha", "community", "Community", "web_app", "active", "Free edition."),
    row(6, "project-alpha", "enterprise", "Enterprise", "web_app", "planned", "Paid edition with single sign-on."),
    row(7, "project-alpha", "ios", "iOS app", "mobile_app", "paused", "Waiting on the Community release."),
  ];
  private next = 9;

  list(project: string): ComponentPage {
    return { items: this.items.filter((item) => item.project === project).map((item) => ({ ...item })), next_after: null };
  }
  create(project: string, input: CreateComponentInput): ProjectComponent {
    if (this.items.some((item) => item.project === project && item.slug === input.slug)) throw new HubApiError(409, "component_exists");
    const now = new Date().toISOString();
    const { slug, client_request_id: _request, ...fields } = input;
    const item = { ...row(this.next++, project, slug, fields.name, fields.kind, fields.status, fields.description),
      repository_url: fields.repository_url, package_url: fields.package_url, legacy_project_id: fields.legacy_project_id, created_at: now, updated_at: now };
    this.items.push(item);
    return { ...item };
  }
  update(id: string, input: UpdateComponentInput): ProjectComponent {
    const { expected_revision, ...changes } = input;
    return this.change(id, expected_revision, changes);
  }
  retire(id: string, revision: number): ProjectComponent {
    return this.change(id, revision, { status: "retired" });
  }
  private change(id: string, revision: number, changes: Partial<ComponentFields>): ProjectComponent {
    const index = this.items.findIndex((item) => item.id === id);
    if (index < 0) throw new HubApiError(404, "component_not_found");
    const current = this.items[index];
    if (current.revision !== revision) throw new HubApiError(409, "component_conflict");
    const item = { ...current, ...changes, revision: current.revision + 1, updated_at: new Date().toISOString() };
    this.items[index] = item;
    return { ...item };
  }
}
