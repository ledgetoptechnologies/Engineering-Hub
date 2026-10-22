import { expect, test, type Page } from "@playwright/test";
import { audit, blocking, type Finding } from "./audit";

// Layout audit: every page and every dialog, at desktop and phone sizes, in demo mode (no API).
// It fails on sideways scrolling, controls drawn outside the window, unlabeled controls and images without alt text,
// and reports (without failing) tap targets smaller than 24px.
const sizes = [{ name: "desktop", width: 1440, height: 900 }, { name: "phone", width: 390, height: 844 }];
const project = "engineering-hub";
const pages = ["/", "/inbox", "/work", "/projects", "/agents", "/staging", "/notes", "/planning",
  ...["", "/work", "/decisions", "/notes", "/plans", "/labeling", "/activity"].map(tab => `/projects/${project}${tab}`)];


for (const size of sizes) {
  test.describe(`layout audit (${size.name})`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    const report: Record<string, Finding[]> = {};
    test.afterAll(async () => {
      await test.info().attach(`audit-${size.name}.json`, { body: JSON.stringify(report, null, 2), contentType: "application/json" });
    });

    for (const path of pages) test(`page ${path}`, async ({ page }) => {
      await page.goto(`${path}?demo=1`);
      await expect(page.locator("main, .page").first()).toBeVisible();
      await page.waitForTimeout(150);
      const findings = await audit(page, "page", size.width);
      report[`page ${path}`] = findings;
      await page.screenshot({ path: test.info().outputPath(`${size.name}${path.replace(/\//g, "_") || "_home"}.png`), fullPage: true });
      expect.soft(blocking(findings), JSON.stringify(blocking(findings), null, 1)).toEqual([]);
    });

    const dialogs: { label: string; path: string; open: (page: Page) => Promise<void> }[] = [
      { label: "new task", path: "/work", open: p => p.getByRole("button", { name: "New task" }).click() },
      { label: "pause new work", path: "/staging", open: p => p.getByRole("button", { name: "Pause new work" }).click() },
    ];
    for (const dialog of dialogs) test(`dialog ${dialog.label}`, async ({ page }) => {
      await page.goto(`${dialog.path}?demo=1`);
      await expect(page.locator("main, .page").first()).toBeVisible();
      await dialog.open(page);
      await page.locator("dialog[open]").waitFor();
      for (const more of await page.locator("dialog[open] details:not([open]) > summary").all()) await more.click();
      const findings = await audit(page, "dialog", size.width);
      report[`dialog ${dialog.label}`] = findings;
      await page.screenshot({ path: test.info().outputPath(`${size.name}_dialog_${dialog.label.replace(/ /g, "-")}.png`) });
      expect.soft(blocking(findings), JSON.stringify(blocking(findings), null, 1)).toEqual([]);
    });
  });
}
