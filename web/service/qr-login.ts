// Scoped lint exception (oxlint-suppressions.json): the anonymous /api/qr-login/*
// endpoints are public web APIs with no web-app session, so they use the shared
// getPublic/postPublic transport, the same pattern as the other anonymous web
// services (webapp-auth, share). This fork has no generated client for the public
// web surface yet (generated clients only cover the console APIs), so the
// @deprecated note in base.ts — which allows a scoped exception for special
// transports — applies.
// TODO: migrate to a generated client once public web APIs are generated.
import { getPublic, postPublic } from './base'

export type QrLoginTicket = {
  ticket_id: string
  qr_payload: string
  expires_at: number
}

export type QrLoginTicketStatus = 'pending' | 'scanned' | 'confirmed' | 'expired'

export type QrLoginStatusResponse = {
  ticket_id: string
  status: QrLoginTicketStatus
  created_at: number
  expires_at: number
}

export type QrLoginCompleteResponse = {
  ticket_id: string
  end_user_id: string
  passport: string
}

function appCodeParam(appCode: string) {
  return `app_code=${encodeURIComponent(appCode)}`
}

export async function issueQrLoginTicket(appCode: string): Promise<QrLoginTicket> {
  return await postPublic<QrLoginTicket>(`/qr-login/tickets?${appCodeParam(appCode)}`)
}

export async function getQrLoginStatus(
  appCode: string,
  ticketId: string,
): Promise<QrLoginStatusResponse> {
  return await getPublic<QrLoginStatusResponse>(
    `/qr-login/tickets/${encodeURIComponent(ticketId)}/status?${appCodeParam(appCode)}`,
  )
}

export async function completeQrLogin(
  appCode: string,
  ticketId: string,
): Promise<QrLoginCompleteResponse> {
  return await postPublic<QrLoginCompleteResponse>(
    `/qr-login/tickets/${encodeURIComponent(ticketId)}/complete?${appCodeParam(appCode)}`,
  )
}
