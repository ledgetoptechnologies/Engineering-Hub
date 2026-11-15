import { expect, test } from '@playwright/test';

test('theme selection persists across reload and follows the system setting', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/?demo=1#/');
  const picker = page.getByLabel('Theme');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await picker.selectOption('light');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.reload();
  await expect(picker).toHaveValue('light');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await picker.selectOption('system');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('pre-paint theme script runs under the Hub script CSP', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('engineeringHub.theme', 'light'));
  await page.route('**/theme-csp-check', route => route.fulfill({
    status: 200,
    contentType: 'text/html',
    headers: { 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'" },
    body: '<!doctype html><html><head><script src="/assets/theme-init.js"></script></head><body>Theme</body></html>',
  }));
  await page.goto('/theme-csp-check');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
});
