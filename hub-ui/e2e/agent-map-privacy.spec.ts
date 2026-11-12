import { expect, test } from '@playwright/test';
import { demoActivity } from '../src/agentActivityApi';
import { createDemoOverview } from '../src/mocks';

test('owner revocation removes previously rendered activity and credentials', async ({ page }) => {
  let authorized = true;
  await page.clock.install();
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return json({ authenticated: true, actor: 'human:owner', github_id: null,
      auth_provider: 'cloudflare_access', email: 'owner@example.test', csrf_token: 'fixture' });
    if (path.endsWith('/overview')) return json(createDemoOverview(new Date()));
    if (path.endsWith('/agents/activity')) {
      if (!authorized) return json({ detail: { code: 'agent_activity_owner_required' } }, 403);
      const activity = demoActivity();
      activity.topics[0].topic = 'private-regression-topic';
      activity.open_counts_truncated = true;
      return json(activity);
    }
    if (path.endsWith('/events/status')) return json({ available: false, reason: 'native_source_unsupported' });
    return json({}, 404);
  });
  await page.goto('/#/agents/map');
  await expect(page.locator('svg text').getByText('private-regression-topic')).toBeVisible();
  await expect(page.getByText(/Claims and credential activity do not establish/)).toBeVisible();
  await expect(page.getByText(/Activity coverage is incomplete or unavailable/)).toBeVisible();
  authorized = false;
  await page.clock.fastForward(15_001);
  await expect(page.getByText('Only the Hub owner can see the map.')).toBeVisible();
  await expect(page.getByText('private-regression-topic')).toHaveCount(0);
  await expect(page.getByText('Active credentials')).toHaveCount(0);
});
