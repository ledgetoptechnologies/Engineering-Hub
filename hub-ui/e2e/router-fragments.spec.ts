import { expect, test } from '@playwright/test';

test('hash route conversion preserves query and fragment through reload and Back', async ({ page }) => {
  await page.goto('/?demo=1#/notes?view=recent#later');
  await expect(page).toHaveURL(/\/notes\?demo=1&view=recent#later$/);
  await expect(page.getByRole('heading', { name: 'Shared notes', exact: true })).toBeVisible();
  await page.reload();
  await expect(page).toHaveURL(/\/notes\?demo=1&view=recent#later$/);
  await expect(page.getByRole('heading', { name: 'Shared notes', exact: true })).toBeVisible();
  await page.getByRole('navigation', { name: 'Workspace navigation' }).getByRole('link', { name: 'Projects', exact: true }).click();
  await expect(page).toHaveURL(/\/projects\?demo=1&view=recent$/);
  await page.goBack();
  await expect(page).toHaveURL(/\/notes\?demo=1&view=recent#later$/);
  await expect(page.getByRole('heading', { name: 'Shared notes', exact: true })).toBeVisible();
});

test('a legacy root anchor stays a route but a path fragment stays on its page', async ({ page }) => {
  await page.goto('/?demo=1#work');
  await expect(page).toHaveURL(/\/work\?demo=1$/);
  await page.goto('/notes?demo=1#work');
  await expect(page.getByRole('heading', { name: 'Shared notes', exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/notes\?demo=1#work$/);
});

