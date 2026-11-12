'use client'

import { QRCodeCanvas } from 'qrcode.react'
import * as React from 'react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { completeQrLogin, getQrLoginStatus, issueQrLoginTicket } from '@/service/qr-login'
import { resolveWebAppAddress } from '@/service/webapp-address'
import { setWebAppPassport } from '@/service/webapp-auth'

const POLL_INTERVAL_MS = 2000

type QrLoginPanelProps = {
  appCode: string
  onLoggedIn?: () => void
}

type PanelTicket = {
  id: string
  payload: string
  expiresAt: number
}

type PanelStatus = 'loading' | 'pending' | 'scanned' | 'confirmed' | 'expired' | 'error'

export default function QrLoginPanel({ appCode, onLoggedIn }: QrLoginPanelProps) {
  const { t } = useTranslation(['login'])
  const [ticket, setTicket] = useState<PanelTicket | null>(null)
  const [status, setStatus] = useState<PanelStatus>('loading')
  const [remainingSeconds, setRemainingSeconds] = useState<number | null>(null)
  const [epoch, setEpoch] = useState(0)
  const onLoggedInRef = useRef(onLoggedIn)
  onLoggedInRef.current = onLoggedIn

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const nowSeconds = () => Math.floor(Date.now() / 1000)
    const finish = () => {
      if (timer !== undefined) window.clearInterval(timer)
    }

    void (async () => {
      setStatus('loading')
      setTicket(null)
      try {
        const issued = await issueQrLoginTicket(appCode)
        if (cancelled) return
        setTicket({
          id: issued.ticket_id,
          payload: issued.qr_payload,
          expiresAt: issued.expires_at,
        })
        setRemainingSeconds(Math.max(0, issued.expires_at - nowSeconds()))
        setStatus('pending')
        timer = window.setInterval(async () => {
          if (cancelled) {
            finish()
            return
          }
          let state: Awaited<ReturnType<typeof getQrLoginStatus>>
          try {
            state = await getQrLoginStatus(appCode, issued.ticket_id)
          } catch {
            return
          }
          if (cancelled) {
            finish()
            return
          }
          setRemainingSeconds(Math.max(0, state.expires_at - nowSeconds()))
          if (state.status === 'confirmed') {
            finish()
            setStatus('confirmed')
            try {
              const { passport } = await completeQrLogin(appCode, issued.ticket_id)
              const address = resolveWebAppAddress()
              if (address) setWebAppPassport(address, passport)
            } catch {
              // The ticket is single-use: a failed exchange cannot be retried, so
              // surface the error state (the panel offers a fresh ticket).
              if (!cancelled) setStatus('error')
              return
            }
            onLoggedInRef.current?.()
          } else if (state.status === 'expired') {
            finish()
            setStatus('expired')
          } else {
            setStatus(state.status)
          }
        }, POLL_INTERVAL_MS)
      } catch {
        if (!cancelled) setStatus('error')
      }
    })()

    return () => {
      cancelled = true
      finish()
    }
  }, [appCode, epoch])

  const refresh = useCallback(() => setEpoch((n) => n + 1), [])

  // 'loading' reuses the scanning text (the panel briefly shows a placeholder
  // ticket before the first poll); 'error' shows the refresh button instead.
  const statusText =
    status === 'scanned'
      ? t(($) => $['webapp.qrLogin.scanned'], { ns: 'login' })
      : status === 'expired'
        ? t(($) => $['webapp.qrLogin.expired'], { ns: 'login' })
        : t(($) => $['webapp.qrLogin.scanning'], { ns: 'login' })

  return (
    <div className="border-line-200 bg-bg-app flex w-full flex-col items-center gap-3 rounded-2xl border px-4 py-5">
      <div className="system-2xs-semibold text-text-secondary">
        {t(($) => $['webapp.qrLogin.title'], { ns: 'login' })}
      </div>
      {ticket ? (
        <div className="border-line-200 rounded-xl border bg-white p-3">
          <QRCodeCanvas size={160} value={ticket.payload} />
        </div>
      ) : (
        <div className="border-line-200 h-[160px] w-[160px] rounded-xl border bg-white" />
      )}
      {status === 'expired' || status === 'error' ? (
        <button
          type="button"
          className="hover:text-brand cursor-pointer appearance-none system-xs-medium text-text-primary focus-visible:ring-2 focus-visible:ring-state-accent-solid focus-visible:outline-hidden"
          onClick={refresh}
        >
          {t(($) => $['webapp.qrLogin.refresh'], { ns: 'login' })}
        </button>
      ) : (
        <div className="system-xs-regular text-text-tertiary">
          {statusText}
          {remainingSeconds !== null ? ` (${remainingSeconds}s)` : ''}
        </div>
      )}
    </div>
  )
}
