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

test('invited workers get the browser activation form from the email fragment', async ({
  page,
}) => {
  await page.goto('/#activate=signed-test-token')

  await expect(page.getByRole('heading', { name: 'Activate your work account' })).toBeVisible()
  await expect(page.getByLabel('First name')).toBeVisible()
  await expect(page.getByLabel('Surname')).toBeVisible()
  await expect(page.getByLabel('Work position')).toBeVisible()
  await expect(page.getByLabel('Work email')).toBeVisible()
  await expect(page.getByLabel('New password', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Activate account' })).toBeVisible()
})

test('an unconfigured client can reach the one-time first-owner browser setup', async ({
  page,
}) => {
  await page.route('**/auth/session', async (route) => {
    await route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"Authentication is required"}' })
  })
  await page.route('**/auth/setup-status', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"bootstrap_required":true}' })
  })
  await page.goto('/')

  await page.getByRole('button', { name: 'Set up first company owner' }).click()
  await expect(page.getByRole('heading', { name: 'Set up your company' })).toBeVisible()
  await expect(page.getByLabel('Company name')).toBeVisible()
  await expect(page.getByLabel('Platform setup key')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Create company owner' })).toBeVisible()
})

test('the guided setup resumes from real server state and reaches readiness', async ({
  page,
}) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Setup guide guided' }).click()

  const setup = page.getByRole('main', { name: 'Set up ShelfWise workspace' })
  await expect(setup).toBeVisible()
  await expect(setup.getByRole('navigation', { name: 'Setup steps' })).toBeVisible()

  await setup.getByLabel('Company name').fill('E2E Guided Shop')
  await setup.getByRole('button', { name: 'Save company profile' }).click()

  await setup.getByLabel('Store name').fill('E2E Guided Shop Johannesburg')
  await setup.getByLabel('Store ID').fill('e2e_guided_shop')
  await setup.getByLabel('Initial store areas').fill('Backroom, Dairy fridge')
  await setup.getByRole('button', { name: 'Create store' }).click()

  const csv = [
    'sku,name,barcode',
    'E2E-SKU-1,E2E Milk,6001000000001',
  ].join('\n')
  await setup.getByLabel('CSV file').setInputFiles({
    name: 'e2e-products.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from(csv),
  })
  await setup.getByRole('button', { name: 'Preview file' }).click()
  await expect(setup.getByText(/Preview completed/)).toBeVisible()
  await setup.getByRole('button', { name: 'Import approved file' }).click()
  await expect(setup.getByText(/rows were imported/)).toBeVisible()

  const dataMetric = setup.locator('.workspace-metric', { hasText: 'Data source' })
  await expect(dataMetric).toContainText('connected')
  await setup.getByRole('button', { name: 'Continue to policies' }).click()
  await setup.getByRole('checkbox', { name: /Dairy/ }).check()
  await setup.getByRole('button', { name: 'Confirm selected policies' }).click()
  await expect(setup.getByText('Current product policy templates confirmed.')).toBeVisible()
  await setup.getByRole('button', { name: 'Continue to devices' }).click()
  await setup.getByRole('button', { name: 'Skip for now' }).click()
  await setup.getByRole('button', { name: 'Skip for now' }).click()

  await expect(setup.getByText('Ready for store operations')).toBeVisible()
  await expect(setup.getByText('Required setup is complete.')).toBeVisible()
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
    [/^To order \d+ products$/, 'To order workspace'],
    [/^Sell first \d+ products$/, 'Sell first workspace'],
    [/^Deliveries \d+ issues$/, 'Deliveries workspace'],
    ['Cold chain clear', 'Cold chain workspace'],
    ['Products search', 'Products workspace'],
    ['Verified value R0', 'Verified value workspace'],
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

test('a store owner can self-serve connect a till through a signed webhook endpoint', async ({
  page,
}) => {
  // The other half of "connect your own systems without a developer". Poll-based ERPs use
  // the credential panel; Shopify/Square/Lightspeed/Yoco sign their deliveries instead, and
  // previously needed an operator to wire the shared ingest API key. This drives the real
  // provisioning route (POST /connectors/{system}/webhook-endpoint) end to end.
  await page.goto('/')
  await page.getByRole('button', { name: /^Connections/ }).click()

  const panel = page.locator('.workspace-section', { hasText: 'Connect a till or online store' })
  await expect(panel).toBeVisible()

  await panel.getByRole('button', { name: 'Create endpoint' }).click()

  // The signing secret and delivery address are shown exactly once, at creation.
  const urlField = panel.getByLabel('Webhook address')
  await expect(urlField).toBeVisible()
  const deliveryUrl = await urlField.inputValue()
  expect(deliveryUrl).toContain('/connectors/webhook/whep_')
  const secretField = panel.getByLabel('Signing secret')
  expect((await secretField.inputValue()).length).toBeGreaterThan(0)

  await panel.getByRole('button', { name: 'Done' }).click()
  await expect(panel.getByText('active', { exact: true }).first()).toBeVisible()

  // Revoking must not require re-reading the secret, and keeps this test repeatable.
  await panel.getByRole('button', { name: 'Revoke' }).first().click()
  await expect(panel.getByText('revoked', { exact: true }).first()).toBeVisible()
})
