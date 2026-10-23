import type { Page } from "@playwright/test";

/** Layout and labelling checks for the page or the open dialog; shared by the layout audit and dialog-specific tests. */
export interface Finding { kind: string; detail: string }

export async function audit(page: Page, scope: string, width: number): Promise<Finding[]> {
  return page.evaluate(({ scope, width }) => {
    const root = scope === "page" ? document.body : document.querySelector("dialog[open]");
    if (!root) return [{ kind: "missing", detail: "no open dialog" }];
    const out: { kind: string; detail: string }[] = [];
    const name = (el: Element) => `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).join(".") : ""} "${(el.textContent || "").trim().slice(0, 40)}"`;
    const doc = document.documentElement;
    if (scope === "page" && doc.scrollWidth > doc.clientWidth + 1) out.push({ kind: "page-overflow-x", detail: `${doc.scrollWidth} > ${doc.clientWidth}` });
    if (scope !== "page" && root.scrollWidth > root.clientWidth + 1) out.push({ kind: "dialog-overflow-x", detail: `${root.scrollWidth} > ${root.clientWidth}` });
    const visible = (el: Element) => { const s = getComputedStyle(el); const r = el.getBoundingClientRect();
      return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0; };
    const clipped = (el: Element) => { for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const s = getComputedStyle(p); if (/(auto|scroll|hidden|clip)/.test(s.overflowX)) return true; } return false; };
    for (const el of root.querySelectorAll("button, a[href], input, select, textarea, summary, [role=button], [role=tab]")) {
      if (!visible(el)) continue;
      const r = el.getBoundingClientRect();
      if ((r.right > width + 1 || r.left < -1) && !clipped(el)) out.push({ kind: "outside-window", detail: `${name(el)} ${Math.round(r.left)}..${Math.round(r.right)}` });
      const tag = el.tagName.toLowerCase();
      const type = (el as HTMLInputElement).type;
      if ((tag === "input" && type !== "hidden" && type !== "submit" && type !== "button") || tag === "select" || tag === "textarea") {
        const id = el.id; const labelled = el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") || el.closest("label")
          || (id && document.querySelector(`label[for="${CSS.escape(id)}"]`)) || el.getAttribute("title");
        if (!labelled) out.push({ kind: "unlabeled-field", detail: name(el) });
      } else if (!(el.textContent || "").trim() && !el.getAttribute("aria-label") && !el.getAttribute("aria-labelledby") && !el.getAttribute("title")
        && !el.querySelector("img[alt]:not([alt=''])")) out.push({ kind: "unnamed-control", detail: name(el) });
      // A checkbox or radio inside its label is tapped through the whole label row, so measure the label instead.
      const target = (tag === "input" && el.closest("label")) ? el.closest("label")!.getBoundingClientRect() : r;
      if ((target.height < 24 || target.width < 24) && !(tag === "a" && getComputedStyle(el).display === "inline"))
        out.push({ kind: "small-target", detail: `${name(el)} ${Math.round(target.width)}x${Math.round(target.height)}` });
    }
    for (const img of root.querySelectorAll("img")) if (!img.hasAttribute("alt")) out.push({ kind: "img-no-alt", detail: img.src.slice(0, 60) });
    return out;
  }, { scope, width });
}

/** Findings that fail a test; small tap targets are reported only. */
export const blocking = (findings: Finding[]) => findings.filter(f => f.kind !== "small-target");
