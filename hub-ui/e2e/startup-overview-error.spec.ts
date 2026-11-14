import { expect, test } from '@playwright/test';
import { createDemoOverview } from '../src/mocks';

test('signed-in overview failure offers retry rather than asking for sign-in', async ({ page }) => {
  let available = false;
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname;
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (path.endsWith('/auth/me')) return json({ authenticated: true, actor: 'human:owner', github_id: null,
      auth_provider: 'cloudflare_access', email: 'owner@example.test', csrf_token: 'fixture' });
    if (path.endsWith('/overview')) return available ? json(createDemoOverview(new Date())) : json({ detail: 'unavailable' }, 503);
    if (path.endsWith('/events/status')) return json({ available: false, reason: 'native_source_unsupported' });
    return json({}, 404);
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Your workspace could not be loaded.' })).toBeVisible();
  await expect(page.getByRole('link', { name: /Sign in securely/ })).toHaveCount(0);
  available = true;
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByRole('navigation', { name: 'Workspace navigation' })).toBeVisible();
});
