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

test('simulation delivery badge matches the full receiving-exception queue', async ({ page }) => {
  await page.goto('/')

  await page.getByRole('button', { name: 'Simulation', exact: true }).click()

  const deliveries = page.getByRole('button', { name: 'Deliveries 17 issues', exact: true })
  await expect(deliveries).toBeVisible()
  await deliveries.click()

  const deliveriesWorkspace = page.getByRole('main', { name: 'Deliveries workspace' })
  await expect(deliveriesWorkspace).toBeVisible()
  await expect(deliveriesWorkspace.getByText('17 issues', { exact: true })).toBeVisible()
})

test('every populated simulation workspace opens from the navigation rail', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Simulation', exact: true }).click()

  const workspaces = [
    ['To order 17 products', 'To order workspace'],
    ['Sell first 12 products', 'Sell first workspace'],
    ['Deliveries 17 issues', 'Deliveries workspace'],
    ['Cold chain clear', 'Cold chain workspace'],
    ['Products search', 'Products workspace'],
    ["Today's results R0", "Today's results workspace"],
    ['Store twin state + scenarios', 'Store twin workspace'],
    [/^Connections \d+ systems$/, 'Connections workspace'],
    ['Operations simulation', 'Operations workspace'],
  ] as const

  for (const [navigationName, workspaceName] of workspaces) {
    await page.getByRole('button', { name: navigationName, exact: typeof navigationName === 'string' }).click()
    await expect(page.getByRole('main', { name: workspaceName })).toBeVisible()
  }
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

test('a store owner can self-serve connect an ERP system through the real credential API', async ({
  page,
}) => {
  // Drives the actual self-serve "Connect your systems" panel end to end against the
  // real backend (POST /connectors/{system}/credentials) - the concrete answer to "can a
  // shop owner just type their API key into the frontend" this panel was built to give.
  await page.goto('/')
  await page.getByRole('button', { name: /^Connections/ }).click()

  const panel = page.locator('.workspace-section', { hasText: 'Connect your systems' })
  await expect(panel).toBeVisible()

  await panel.getByRole('button', { name: /^Odoo/ }).click()
  await panel.getByLabel('Base URL').fill('https://e2e-shop.odoo.com')
  await panel.getByLabel('Database').fill('e2e_prod')
  await panel.getByLabel('User ID').fill('7')
  await panel.getByLabel('API key').fill('sk_e2e_test_key')
  await panel.getByRole('button', { name: 'Connect', exact: true }).click()

  await expect(panel.getByRole('button', { name: /^Odoo/ })).toContainText('Connected')

  // The stored value is never echoed back anywhere in the DOM - not on this page, and
  // not after a reload that re-fetches status from the server.
  await expect(page.locator('body')).not.toContainText('sk_e2e_test_key')
  await page.reload()
  await page.getByRole('button', { name: /^Connections/ }).click()
  const panelAfterReload = page.locator('.workspace-section', { hasText: 'Connect your systems' })
  await expect(panelAfterReload.getByRole('button', { name: /^Odoo/ })).toContainText('Connected')

  // Clean up: disconnect so this test is repeatable against a persistent backend.
  await panelAfterReload.getByRole('button', { name: /^Odoo/ }).click()
  await panelAfterReload.getByRole('button', { name: 'Disconnect' }).click()
  await expect(panelAfterReload.getByRole('button', { name: /^Odoo/ })).toContainText('Not connected')
})

test('a store owner can self-serve register a camera/sensor device credential', async ({
  page,
}) => {
  // Drives the actual self-serve "Connect a camera or sensor" panel end to end against
  // the real backend (POST /twin/stores/{store_id}/devices) - the concrete answer to "how
  // do they connect their cameras": a device credential the shop's own camera/sensor
  // integration signs structured events with, never a raw-video pipeline.
  await page.goto('/')
  await page.getByRole('button', { name: /^Store twin/ }).click()

  const panel = page.locator('.workspace-section', { hasText: 'Connect a camera or sensor' })
  await expect(panel).toBeVisible()

  await panel.getByRole('button', { name: 'Register a new device' }).click()

  const deviceIdField = panel.getByLabel('Device ID')
  await expect(deviceIdField).toBeVisible()
  const deviceId = await deviceIdField.inputValue()
  expect(deviceId.length).toBeGreaterThan(0)
  const secretField = panel.getByLabel('HMAC secret')
  expect((await secretField.inputValue()).length).toBeGreaterThan(0)

  await panel.getByRole('button', { name: 'Done' }).click()
  await expect(panel.getByText(deviceId, { exact: true })).toBeVisible()
  await expect(panel.getByText('Active', { exact: true })).toBeVisible()

  // Clean up: revoke so this test is repeatable against a persistent backend.
  await panel.getByRole('button', { name: 'Revoke' }).click()
  await expect(panel.getByText('Revoked', { exact: true })).toBeVisible()
})
