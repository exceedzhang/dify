"""Web-app QR login endpoints (provider-agnostic protocol).

Anonymous, app-scoped visitor flow (``app_code`` query parameter, same contract as
the other web endpoints):

- ``POST /api/qr-login/tickets``                → mint a ticket (IP rate limited)
- ``GET  /api/qr-login/tickets/<id>/status``    → poll the ticket state
- ``POST /api/qr-login/tickets/<id>/events``    → provider callback (HMAC signature)
- ``POST /api/qr-login/tickets/<id>/complete``  → exchange for a web-app passport

The provider callback is authenticated with ``X-Qr-Login-Signature: sha256=<hex>``
(HMAC-SHA256 of the raw body, keyed by the platform ``SECRET_KEY``). Tickets are
single-use: the first ``complete`` wins, later attempts get 410.
"""

from uuid import uuid4

from flask import request
from flask_restx import Resource
from pydantic import BaseModel, Field

from controllers.console.wraps import model_validate
from controllers.web import web_ns
from controllers.web.error import WebAppNotFoundError
from controllers.web.flask_admission import web_anonymous_admission
from core.db.session_factory import session_factory
from libs.exception import BaseHTTPException
from libs.external_api import error_responses
from machinery.context import RequestContext
from repositories.web_passport_repository import WebPassportRepository
from services.entities.passport_entities import WebAppRecord
from services.qr_login_service import (
    QrLoginRateLimitedError,
    QrLoginService,
    QrLoginTicketConsumedError,
    QrLoginTicketExpiredError,
    QrLoginTicketNotConfirmedError,
    QrLoginTicketNotFoundError,
    QrLoginTransitionError,
)

QR_LOGIN_SERVICE = QrLoginService()


class QrTicketNotFoundError(BaseHTTPException):
    error_code = "qr_ticket_not_found"
    description = "QR login ticket not found."
    code = 404


class QrTicketExpiredError(BaseHTTPException):
    error_code = "qr_ticket_expired"
    description = "QR login ticket has expired."
    code = 410


class QrTicketNotConfirmedError(BaseHTTPException):
    error_code = "qr_ticket_not_confirmed"
    description = "QR login ticket has not been confirmed yet."
    code = 401


class QrTicketConsumedError(BaseHTTPException):
    error_code = "qr_ticket_consumed"
    description = "QR login ticket has already been used."
    code = 410


class QrTicketTransitionError(BaseHTTPException):
    error_code = "qr_ticket_transition_conflict"
    description = "QR login ticket state conflict."
    code = 409


class QrRateLimitExceededError(BaseHTTPException):
    error_code = "qr_login_rate_limited"
    description = "Too many QR login ticket requests. Try again later."
    code = 429


class QrInvalidSignatureError(BaseHTTPException):
    error_code = "qr_invalid_signature"
    description = "QR login provider callback signature is invalid."
    code = 400


class QrLoginEventPayload(BaseModel):
    event: str = Field(max_length=16, description="Provider event: scanned or confirmed")
    provider: str = Field(min_length=1, max_length=32)
    provider_openid: str = Field(min_length=1, max_length=255)


def _resolve_web_app(app_code: str | None) -> WebAppRecord | None:
    """Resolve the active web app for ``app_code`` (None when missing/unknown)."""
    if not app_code:
        return None
    repository = WebPassportRepository(
        session_factory=session_factory.get_session_maker(),
        generate_session_id=lambda: str(uuid4()),
    )
    return repository.get_active_web_app(app_code)


def _require_web_app() -> WebAppRecord:
    app = _resolve_web_app(request.args.get("app_code"))
    if app is None:
        raise WebAppNotFoundError()
    return app


@web_ns.route("/qr-login/tickets")
class QrLoginTicketApi(Resource):
    """Mint a QR login ticket for an anonymous visitor."""

    @error_responses(WebAppNotFoundError, QrRateLimitExceededError)
    @web_anonymous_admission()
    def post(self, request_context: RequestContext):
        app = _require_web_app()
        try:
            ticket = QR_LOGIN_SERVICE.create_ticket(app, request_context.remote_ip)
        except QrLoginRateLimitedError as error:
            raise QrRateLimitExceededError() from error
        return {
            "ticket_id": ticket.ticket_id,
            "qr_payload": ticket.qr_payload,
            "expires_at": ticket.expires_at,
        }, 201


@web_ns.route("/qr-login/tickets/<string:ticket_id>/status")
class QrLoginTicketStatusApi(Resource):
    """Poll the state of a QR login ticket."""

    @error_responses(WebAppNotFoundError, QrTicketNotFoundError)
    @web_anonymous_admission()
    def get(self, request_context: RequestContext, ticket_id: str):
        app = _require_web_app()
        try:
            state = QR_LOGIN_SERVICE.get_status(app, ticket_id)
        except QrLoginTicketNotFoundError as error:
            raise QrTicketNotFoundError() from error
        return {
            "ticket_id": state.ticket_id,
            "status": state.status,
            "created_at": state.created_at,
            "expires_at": state.expires_at,
        }


@web_ns.route("/qr-login/tickets/<string:ticket_id>/events")
class QrLoginTicketEventApi(Resource):
    """Authenticated provider callback reporting scanned/confirmed events."""

    @error_responses(
        WebAppNotFoundError,
        QrInvalidSignatureError,
        QrTicketNotFoundError,
        QrTicketExpiredError,
        QrTicketTransitionError,
    )
    @web_anonymous_admission()
    @model_validate(QrLoginEventPayload)
    def post(self, payload: QrLoginEventPayload, request_context: RequestContext, ticket_id: str):
        app = _require_web_app()
        if not QrLoginService.verify_event_signature(request.headers.get("X-Qr-Login-Signature"), request.get_data()):
            raise QrInvalidSignatureError()
        try:
            state = QR_LOGIN_SERVICE.record_event(
                app,
                ticket_id,
                payload.event,
                payload.provider,
                payload.provider_openid,
            )
        except QrLoginTicketNotFoundError as error:
            raise QrTicketNotFoundError() from error
        except QrLoginTicketExpiredError as error:
            raise QrTicketExpiredError() from error
        except QrLoginTransitionError as error:
            raise QrTicketTransitionError() from error
        return {
            "ticket_id": state.ticket_id,
            "status": state.status,
            "expires_at": state.expires_at,
        }


@web_ns.route("/qr-login/tickets/<string:ticket_id>/complete")
class QrLoginTicketCompleteApi(Resource):
    """Exchange a confirmed ticket for a web-app passport (single use)."""

    @error_responses(
        WebAppNotFoundError,
        QrTicketNotFoundError,
        QrTicketExpiredError,
        QrTicketNotConfirmedError,
        QrTicketConsumedError,
    )
    @web_anonymous_admission()
    def post(self, request_context: RequestContext, ticket_id: str):
        app = _require_web_app()
        try:
            completion = QR_LOGIN_SERVICE.complete(app, ticket_id)
        except QrLoginTicketNotFoundError as error:
            raise QrTicketNotFoundError() from error
        except QrLoginTicketExpiredError as error:
            raise QrTicketExpiredError() from error
        except QrLoginTicketNotConfirmedError as error:
            raise QrTicketNotConfirmedError() from error
        except QrLoginTicketConsumedError as error:
            raise QrTicketConsumedError() from error
        return {
            "ticket_id": completion.ticket_id,
            "end_user_id": completion.end_user_id,
            "passport": completion.passport,
        }
