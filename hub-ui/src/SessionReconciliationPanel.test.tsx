import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { HubApiError } from "./api";
import { SessionReconciliationPanel, SessionReconciliationView, sessionReconciliationError } from "./SessionReconciliationPanel";
import type { SessionReconciliationSnapshot } from "./sessionReconciliationApi";

const snapshot: SessionReconciliationSnapshot = { request_id: "a".repeat(64), revision: 1, state: "pending",
  evidence: "Synthetic test evidence <script>not live proof</script>", report_digest: `sha256:${"b".repeat(64)}`,
  expires_in_seconds: 120, execution_authorization: false };
const props = { snapshot, remainingSeconds: 120, busy: false, error: null, csrfAvailable: true, onRefresh: () => {}, onDecide: () => {} };
describe("staging session review presentation", () => {
  it("escapes retained evidence and explains the narrow authority and finite deadline", () => {
    const html = renderToStaticMarkup(<SessionReconciliationView {...props} />);
    expect(html).toContain("Pending review"); expect(html).toContain("2m 0s");
    expect(html).toContain("&lt;script&gt;not live proof&lt;/script&gt;"); expect(html).not.toContain("<script>");
    expect(html).toContain("does not independently verify"); expect(html).toContain("Tasks still need their independent review");
    expect(html).toContain("Accept staging session only"); expect(html).toContain("Decline session");
    expect(html).not.toContain(snapshot.request_id);
  });
  it.each([{ remainingSeconds: 0 }, { busy: true }, { error: "Refresh required" }, { csrfAvailable: false },
    { snapshot: { ...snapshot, state: "accepted" as const } }])("does not offer another decision when unavailable %#", changes => {
    const html = renderToStaticMarkup(<SessionReconciliationView {...props} {...changes} />);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Accept staging session only/);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Decline session/);
  });
  it("labels expiry and accepted state without promising readiness", () => {
    expect(renderToStaticMarkup(<SessionReconciliationView {...props} remainingSeconds={0} />)).toContain("Closed");
    const html = renderToStaticMarkup(<SessionReconciliationView {...props} snapshot={{ ...snapshot, state: "accepted" }} />);
    expect(html).toContain("not a claim that the executor is healthy");
  });
  it.each([403, 404, 409, 410, 401, 0])("explains status %s safely", status => {
    const message = sessionReconciliationError(new HubApiError(status, "untrusted-secret"));
    expect(message).not.toContain("untrusted-secret"); expect(message.length).toBeGreaterThan(30);
    if (status === 403) expect(message).toContain("designated staging operator");
    if (status === 409) expect(message).toContain("Refresh");
    if (status === 410) expect(message).toContain("new review");
  });
  it("offers no live decision in disabled/demo view", () => {
    const html = renderToStaticMarkup(<SessionReconciliationPanel csrf="csrf" enabled={false} writable={false} stale={false} />);
    expect(html).toContain("No staging session can be opened here"); expect(html).not.toContain("<button");
  });
});
