import { expect, test } from "@playwright/test";

const demo = "/?demo=1#/";

test.use({ serviceWorkers: "block" });

test.beforeEach(async ({ page, baseURL }) => {
  const origin = new URL(baseURL!).origin;
  await page.route("**/*", (route) => {
    const url = new URL(route.request().url());
    return url.origin === origin && !url.pathname.startsWith("/api/") ? route.continue() : route.abort();
  });
});

test("sets the default dark theme before the dashboard starts", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.route("**/src/main.tsx", (route) => route.abort());
  await page.goto(demo);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("#root")).toBeEmpty();
});

test("keeps the phone theme choice on reload and follows the OS only in System", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto(demo);
  const picker = page.getByRole("combobox", { name: "Theme" });
  const root = page.locator("html");
  await expect(picker).toBeInViewport();
  await expect(picker).toHaveValue("dark");
  await expect(root).toHaveCSS("color-scheme", "dark");
  await picker.selectOption("light");
  await expect(root).toHaveCSS("color-scheme", "light");
  await page.reload();
  await expect(picker).toHaveValue("light");
  await expect(root).toHaveCSS("color-scheme", "light");
  await picker.selectOption("system");
  await expect(root).toHaveCSS("color-scheme", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(root).toHaveCSS("color-scheme", "light");
  await picker.selectOption("dark");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.emulateMedia({ colorScheme: "light" });
  await expect(root).toHaveCSS("color-scheme", "dark");
});

test("keeps compact Machine Health meter fills visible in both themes", async ({ page }) => {
  await page.goto("/?demo=1#/infrastructure");
  const health = page.getByRole("region", { name: "Machine health" });
  const normal = health.locator(".machine-card-meter:not(.warm):not(.high) i").first();
  const warm = health.locator(".machine-card-meter:is(.warm, .high) i").first();
  const transparent = "rgba(0, 0, 0, 0)";
  await expect(normal).toBeVisible();
  await expect(warm).toBeVisible();
  await expect(normal).not.toHaveCSS("background-color", transparent);
  await expect(warm).not.toHaveCSS("background-color", transparent);
  await page.getByRole("combobox", { name: "Theme" }).selectOption("light");
  await expect(normal).not.toHaveCSS("background-color", transparent);
  await expect(warm).not.toHaveCSS("background-color", transparent);
});

test("recolors the home task stages and preserves Machine Health history in both themes", async ({ page }) => {
  await page.goto(demo);
  const picker = page.getByRole("combobox", { name: "Theme" });
  const legend = page.getByRole("list", { name: "Stages" });
  await expect(legend.getByRole("listitem")).toHaveCount(6);
  await expect(legend).toContainText("Waiting on Beau");
  await expect(legend).toContainText("Done");
  const assigned = legend.getByRole("listitem").filter({ hasText: "Assigned" }).locator(".swatch");
  await expect(assigned).toHaveCSS("background-color", "rgb(79, 124, 196)");
  await picker.selectOption("light");
  await expect(assigned).toHaveCSS("background-color", "rgb(188, 212, 245)");

  await page.goto("/?demo=1#/infrastructure/build-pc?range=30d");
  const history = page.getByRole("region", { name: "Machine history" });
  await expect(history.getByRole("img")).toHaveCount(4);
  const cpu = history.getByRole("img", { name: /^CPU:/ });
  const line = cpu.locator(".history-line").first();
  await expect(line).toHaveCSS("stroke", "rgb(42, 120, 214)");
  await picker.selectOption("dark");
  await expect(line).toHaveCSS("stroke", "rgb(57, 135, 229)");
  await cpu.focus();
  await page.keyboard.press("ArrowRight");
  await expect(history.locator(".history-tip")).toBeVisible();
});
