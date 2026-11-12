import type { Page, Response } from '@playwright/test'
import type { DifyWorld } from '../../support/world.ts'
import { Buffer } from 'node:buffer'
import { createHmac } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { Given, Then, When } from '@cucumber/cucumber'
import { expect } from '@playwright/test'
import { apiURL, baseURL } from '../../../test-env.ts'

type QrTicketIssuance = {
  ticket_id: string
  qr_payload: string
  expires_at: number
}

type QrCallbackOutcome = {
  status: number
  code: string | undefined
}

const FORGED_SIGNATURE = `sha256=${'0'.repeat(64)}`
const ISSUANCE_PATH = '/api/qr-login/tickets'
// The simulated scanner is Bilibili: in production the QR payload is decoded
// by the provider and it reports the scan/confirmation through the signed
// callback API with the user's provider openid.
const QR_LOGIN_PROVIDER = 'bilibili'

let qrAppCode: string | undefined
let qrTicketId: string | undefined
let qrProviderOpenid: string | undefined
let qrFirstEndUserId: string | undefined
let qrCompletePromise: Promise<Response> | undefined
let qrLastCallback: QrCallbackOutcome | undefined

function requireAppCode() {
  if (!qrAppCode) throw new Error('A web app for QR login is not available for this scenario.')
  return qrAppCode
}

function requireTicketId() {
  if (!qrTicketId) throw new Error('No QR login ticket is showing for this scenario.')
  return qrTicketId
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function resolveQrLoginSecret() {
  const fromEnv = process.env.E2E_QR_LOGIN_SECRET
  if (fromEnv) return fromEnv
  let directory = process.cwd()
  for (let depth = 0; depth < 5; depth++) {
    const envFile = resolve(directory, 'docker', '.env')
    if (existsSync(envFile)) {
      const line = readFileSync(envFile, 'utf8')
        .split('\n')
        .find((entry) => entry.startsWith('SECRET_KEY='))
      if (line) return line.slice('SECRET_KEY='.length).trim()
    }
    const parent = dirname(directory)
    if (parent === directory) break
    directory = parent
  }
  throw new Error(
    'Unable to resolve the platform SECRET_KEY (set E2E_QR_LOGIN_SECRET or provide docker/.env).',
  )
}

function signCallbackBody(body: string) {
  return `sha256=${createHmac('sha256', resolveQrLoginSecret()).update(body).digest('hex')}`
}

function ticketsPath() {
  return `${apiURL}/api/qr-login/tickets`
}

function isTicketIssuance(response: Response) {
  const url = new URL(response.url())
  return url.pathname === ISSUANCE_PATH && response.request().method() === 'POST'
}

function isCompleteCall(response: Response) {
  const url = new URL(response.url())
  return (
    url.pathname.includes('/api/qr-login/tickets/') &&
    url.pathname.endsWith('/complete') &&
    response.request().method() === 'POST'
  )
}

async function postProviderEvent(
  page: Page,
  event: 'scanned' | 'confirmed',
  providerOpenid: string,
  forged = false,
) {
  const body = JSON.stringify({
    event,
    provider: QR_LOGIN_PROVIDER,
    provider_openid: providerOpenid,
  })
  return page
    .context()
    .request.post(
      `${ticketsPath()}/${requireTicketId()}/events?app_code=${encodeURIComponent(requireAppCode())}`,
      {
        data: body,
        headers: {
          'Content-Type': 'application/json',
          'X-Qr-Login-Signature': forged ? FORGED_SIGNATURE : signCallbackBody(body),
        },
      },
    )
}

async function scanVisibleTicket(page: Page, providerOpenid: string) {
  qrProviderOpenid = providerOpenid
  const response = await postProviderEvent(page, 'scanned', providerOpenid)
  expect(response.status()).toBe(200)
}

Given('a web app for QR login is available', async () => {
  const appCode = process.env.E2E_QR_LOGIN_APP_CODE
  if (!appCode)
    throw new Error(
      'E2E_QR_LOGIN_APP_CODE is not set. Prepare a web-auth-enabled web app whose visitor access rules admit anonymous visitors and set its app code.',
    )
  qrAppCode = appCode
  qrTicketId = undefined
  qrProviderOpenid = undefined
  qrFirstEndUserId = undefined
  qrCompletePromise = undefined
  qrLastCallback = undefined
})

When('I open the web app sign-in page', async function (this: DifyWorld) {
  const page = this.getPage()
  const appCode = requireAppCode()
  const issuePromise = page.waitForResponse(isTicketIssuance)
  const redirectTarget = `${baseURL}/agent/${appCode}`
  await page.goto(`/webapp-signin?redirect_url=${encodeURIComponent(redirectTarget)}`)
  const issuance = (await (await issuePromise).json()) as QrTicketIssuance
  qrTicketId = issuance.ticket_id
})

When('the QR login panel is showing a ticket', async function (this: DifyWorld) {
  const page = this.getPage()
  await expect(page.getByText('Scan to sign in')).toBeVisible()
  await expect(page.locator('canvas').first()).toBeVisible()
})

When(
  'the QR ticket is scanned with provider openid {string}',
  async function (this: DifyWorld, providerOpenid: string) {
    await scanVisibleTicket(this.getPage(), providerOpenid)
  },
)

When(
  'the fresh QR ticket is scanned with provider openid {string}',
  async function (this: DifyWorld, providerOpenid: string) {
    await scanVisibleTicket(this.getPage(), providerOpenid)
  },
)

When('the QR ticket is confirmed', async function (this: DifyWorld) {
  const page = this.getPage()
  requireTicketId()
  // The panel polls at most every 2s, so the exchange happens shortly after
  // this event lands; arm the watcher before the callback is accepted.
  qrCompletePromise = page.waitForResponse(isCompleteCall)
  const response = await postProviderEvent(page, 'confirmed', qrProviderOpenid ?? '349118612')
  expect(response.status()).toBe(200)
})

When('I refresh the QR login ticket', async function (this: DifyWorld) {
  const page = this.getPage()
  const oldTicketId = requireTicketId()
  // The real ticket TTL is minutes long; force the expired state by answering
  // the old ticket's status polls so the panel offers a fresh ticket at once.
  await page.route(new RegExp(`/api/qr-login/tickets/${oldTicketId}/status`), (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ticket_id: oldTicketId,
        status: 'expired',
        created_at: 0,
        expires_at: 0,
      }),
    }),
  )
  const refreshButton = page.getByRole('button', { name: 'Get a new QR code' })
  await expect(refreshButton).toBeVisible()
  const freshPromise = page.waitForResponse(isTicketIssuance)
  await refreshButton.click()
  const issuance = (await (await freshPromise).json()) as QrTicketIssuance
  qrTicketId = issuance.ticket_id
  await expect(page.locator('canvas').first()).toBeVisible()
})

Then('the visitor is signed in through the QR login panel', async function (this: DifyWorld) {
  const page = this.getPage()
  const appCode = requireAppCode()
  await page.waitForURL(new RegExp(`^${escapeRegExp(baseURL)}/agent/${escapeRegExp(appCode)}`))
  if (qrCompletePromise) {
    expect((await qrCompletePromise).status()).toBe(200)
  }
  // The panel persists the passport to localStorage before redirecting, so the
  // stored token is the identity source of record. Reading it avoids racing the
  // complete response body, which the CDP session discards once the page navigates.
  const handle = await page.waitForFunction(
    (key: string) => window.localStorage.getItem(key) ?? null,
    `passport-${appCode}`,
  )
  const passport = (await handle.jsonValue()) as string
  const payloadSegment = passport.split('.')[1]
  if (!payloadSegment) throw new Error('Unexpected passport format: missing JWT payload segment.')
  const payload = JSON.parse(Buffer.from(payloadSegment, 'base64url').toString('utf8')) as {
    end_user_id: string
  }
  qrFirstEndUserId = payload.end_user_id
})

When(
  'the QR login identity for openid {string} is stable across logins',
  async function (this: DifyWorld, providerOpenid: string) {
    const request = this.getPage().context().request
    const appCode = requireAppCode()
    const issue = await request.post(`${ticketsPath()}?app_code=${encodeURIComponent(appCode)}`)
    expect(issue.status()).toBe(201)
    const ticket = (await issue.json()) as QrTicketIssuance
    for (const event of ['scanned', 'confirmed'] as const) {
      const body = JSON.stringify({
        event,
        provider: QR_LOGIN_PROVIDER,
        provider_openid: providerOpenid,
      })
      const response = await request.post(
        `${ticketsPath()}/${ticket.ticket_id}/events?app_code=${encodeURIComponent(appCode)}`,
        {
          data: body,
          headers: {
            'Content-Type': 'application/json',
            'X-Qr-Login-Signature': signCallbackBody(body),
          },
        },
      )
      expect(response.status()).toBe(200)
    }
    const complete = await request.post(
      `${ticketsPath()}/${ticket.ticket_id}/complete?app_code=${encodeURIComponent(appCode)}`,
    )
    expect(complete.status()).toBe(200)
    const result = (await complete.json()) as { end_user_id: string }
    expect(result.end_user_id).toBe(qrFirstEndUserId)
  },
)

When('a forged QR ticket event is submitted', async function (this: DifyWorld) {
  const response = await postProviderEvent(this.getPage(), 'scanned', 'intruder-openid', true)
  const body = (await response.json()) as { code?: string }
  qrLastCallback = { status: response.status(), code: body.code }
})

Then('the provider callback is rejected as invalid', async () => {
  if (!qrLastCallback) throw new Error('No forged provider callback was submitted.')
  expect(qrLastCallback.status).toBe(400)
  expect(qrLastCallback.code).toBe('qr_invalid_signature')
})
