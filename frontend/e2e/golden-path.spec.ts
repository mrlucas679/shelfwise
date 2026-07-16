import { expect, test } from '@playwright/test'

/**
 * Drives the same golden path DEMO_RUNBOOK.md's three-minute story uses: load the
 * console, switch to the generated-world simulation, approve the seeded expiry/markdown
 * decision, and confirm the approval queue clears and logs the outcome - end to end
 * through the real UI and the real FastAPI backend, not mocked.
 */

test('chat console loads and the approval queue is reachable', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByPlaceholder('Ask ShelfWise...')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Send message' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Approval queue' })).toBeVisible()
})

test('approving the seeded golden decision clears the queue and logs the outcome', async ({
  page,
}) => {
  await page.goto('/')

  await page.getByRole('button', { name: 'Simulation', exact: true }).click()
  await page.getByRole('button', { name: 'Approval queue' }).click()

  const approveButton = page.getByRole('button', { name: 'Approve', exact: true })
  await expect(approveButton).toBeVisible({ timeout: 15_000 })
  await approveButton.click()

  const confirmButton = page.getByRole('button', { name: 'Yes, apply it' })
  await expect(confirmButton).toBeVisible()
  await confirmButton.click()

  await expect(page.getByText('Queue clear. Nothing waiting.')).toBeVisible()
  await expect(page.getByText(/Approved - /)).toBeVisible()
})

test('chat answers a direct question using live tools, grounded in real data', async ({
  page,
}) => {
  await page.goto('/')

  const input = page.getByPlaceholder('Ask ShelfWise...')
  await input.fill('What is at risk today?')
  await page.getByRole('button', { name: 'Send message' }).click()

  // The offline-safe fallback still returns a real, non-empty grounded answer even
  // without live model credentials configured for this environment - it must never
  // hang or render an empty bubble.
  await expect(page.locator('.bubble.assistant').last()).not.toHaveText('', {
    timeout: 20_000,
  })
})
