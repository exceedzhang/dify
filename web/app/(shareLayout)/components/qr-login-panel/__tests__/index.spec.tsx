import { act, fireEvent, render, screen } from '@testing-library/react'
import * as React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { completeQrLogin, getQrLoginStatus, issueQrLoginTicket } from '@/service/qr-login'
import { setWebAppPassport } from '@/service/webapp-auth'
import QrLoginPanel from '../index'

vi.mock('@/service/qr-login', () => ({
  issueQrLoginTicket: vi.fn(),
  getQrLoginStatus: vi.fn(),
  completeQrLogin: vi.fn(),
}))

vi.mock('@/service/webapp-auth', () => ({
  setWebAppPassport: vi.fn(),
}))

vi.mock('@/service/webapp-address', () => ({
  resolveWebAppAddress: vi.fn(() => ({ kind: 'webapp', code: 'qr-site' })),
}))

function nowSeconds() {
  return Math.floor(Date.now() / 1000)
}

function issuedTicket(id: string) {
  return { ticket_id: id, qr_payload: `payload-${id}`, expires_at: nowSeconds() + 120 }
}

function statusResponse(override: Partial<Record<string, unknown>> = {}) {
  return {
    ticket_id: 't1',
    status: 'pending',
    created_at: nowSeconds(),
    expires_at: nowSeconds() + 120,
    ...override,
  }
}

// Advance the fake clock by `ms` and flush the microtasks the polled
// promises schedule, so component state settles before assertions.
async function settle(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
  await act(async () => {
    await Promise.resolve()
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('QrLoginPanel', () => {
  it('renders the issued QR payload with a countdown (B1)', async () => {
    vi.mocked(issueQrLoginTicket).mockResolvedValue(issuedTicket('t1'))

    const { container } = render(<QrLoginPanel appCode="qr-site" />)
    await settle()

    expect(vi.mocked(issueQrLoginTicket)).toHaveBeenCalledWith('qr-site')
    expect(container.querySelector('canvas')).not.toBeNull()
    expect(screen.getByText(/login\.webapp\.qrLogin\.scanning/)).toBeTruthy()
    expect(screen.getByText(/\(120s\)/)).toBeTruthy()
  })

  it('follows pending -> scanned status transitions (B2)', async () => {
    vi.mocked(issueQrLoginTicket).mockResolvedValue(issuedTicket('t1'))
    const responses = [statusResponse(), statusResponse({ status: 'scanned' })]
    let call = 0
    vi.mocked(getQrLoginStatus).mockImplementation(() =>
      Promise.resolve(responses[Math.min(call++, responses.length - 1)]),
    )

    render(<QrLoginPanel appCode="qr-site" />)
    await settle()
    expect(screen.getByText(/login\.webapp\.qrLogin\.scanning/)).toBeTruthy()

    await settle(2000)
    expect(vi.mocked(getQrLoginStatus)).toHaveBeenNthCalledWith(1, 'qr-site', 't1')

    await settle(2000)
    expect(screen.getByText(/login\.webapp\.qrLogin\.scanned/)).toBeTruthy()
  })

  it('completes the login on confirmed and stores the passport (B3)', async () => {
    vi.mocked(issueQrLoginTicket).mockResolvedValue(issuedTicket('t1'))
    vi.mocked(getQrLoginStatus).mockResolvedValue(statusResponse({ status: 'confirmed' }))
    vi.mocked(completeQrLogin).mockResolvedValue({
      ticket_id: 't1',
      end_user_id: 'eu-1',
      passport: 'passport-1',
    })
    const onLoggedIn = vi.fn()

    render(<QrLoginPanel appCode="qr-site" onLoggedIn={onLoggedIn} />)
    await settle()
    expect(screen.getByText(/login\.webapp\.qrLogin\.scanning/)).toBeTruthy()

    await settle(2000)

    expect(vi.mocked(completeQrLogin)).toHaveBeenCalledWith('qr-site', 't1')
    expect(vi.mocked(setWebAppPassport)).toHaveBeenCalledWith(
      { kind: 'webapp', code: 'qr-site' },
      'passport-1',
    )
    expect(onLoggedIn).toHaveBeenCalledTimes(1)
  })

  it('offers a refresh action when the ticket expires (B4)', async () => {
    vi.mocked(issueQrLoginTicket).mockResolvedValue(issuedTicket('t1'))
    vi.mocked(getQrLoginStatus).mockResolvedValue(
      statusResponse({ status: 'expired', expires_at: nowSeconds() - 1 }),
    )

    render(<QrLoginPanel appCode="qr-site" />)
    await settle()

    await settle(2000)
    expect(screen.getByText('login.webapp.qrLogin.refresh')).toBeTruthy()
  })

  it('issues a fresh ticket when refresh is clicked (B5)', async () => {
    vi.mocked(issueQrLoginTicket)
      .mockResolvedValueOnce(issuedTicket('t1'))
      .mockResolvedValueOnce(issuedTicket('t2'))
    vi.mocked(getQrLoginStatus).mockResolvedValue(
      statusResponse({ status: 'expired', expires_at: nowSeconds() - 1 }),
    )

    const { container } = render(<QrLoginPanel appCode="qr-site" />)
    await settle()
    await settle(2000)
    expect(screen.getByText('login.webapp.qrLogin.refresh')).toBeTruthy()

    fireEvent.click(screen.getByText('login.webapp.qrLogin.refresh'))
    await settle()

    expect(vi.mocked(issueQrLoginTicket)).toHaveBeenCalledTimes(2)
    expect(container.querySelectorAll('canvas').length).toBe(1)
    expect(screen.getByText(/login\.webapp\.qrLogin\.scanning/)).toBeTruthy()
  })
})
