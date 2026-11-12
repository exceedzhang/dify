"""QR login service (provider-agnostic protocol).

Implements the four operations of the web-app QR login flow:

- ``create_ticket``  — mints a pending ticket for a scanning client; rate limited per IP.
- ``get_status``     — reports the current ticket state (polling endpoint).
- ``record_event``   — authenticated provider callback (``scanned`` / ``confirmed``).
- ``complete``       — exchanges a confirmed ticket for a web-app passport, binding the
                       provider identity to a stable anonymous ``EndUser`` (single use).

Tickets live in Redis as JSON under ``qr_login:ticket:{app_id}:{ticket_id}``; identity
bindings persist in ``qr_login_identities`` so the same scanning user always rebinds
to the same visitor.

Provider-agnostic by design: ``provider`` / ``provider_openid`` are opaque strings
supplied by the provider callback. Callback authenticity is established with the
``X-Qr-Login-Signature: sha256=<hmac-sha256(raw body)>`` header keyed by the
platform ``SECRET_KEY`` (``verify_event_signature``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from configs import dify_config
from core.db.session_factory import session_factory
from enums import DeploymentEdition, WebAppAccessMode
from libs.helper import RateLimiter
from libs.passport import PassportService
from models import EndUser, QrLoginIdentity
from models.enums import EndUserType
from services.entities.passport_entities import WebAppRecord

if TYPE_CHECKING:
    from extensions.ext_redis import RedisClientWrapper
    from services.web_passport_service import WebAppAuthType

# Ticket state values. ``expired`` is derived (``now >= expires_at``) and never stored.
QR_STATUS_PENDING = "pending"
QR_STATUS_SCANNED = "scanned"
QR_STATUS_CONFIRMED = "confirmed"
QR_STATUS_EXPIRED = "expired"

QR_EVENTS = frozenset({"scanned", "confirmed"})

QR_TICKET_PREFIX = "qr_login:ticket"
QR_LOGIN_QR_PAYLOAD_PREFIX = "dify-qr-login:v1"
QR_SIGNATURE_HEADER = "X-Qr-Login-Signature"
QR_SIGNATURE_SCHEME = "sha256"


def _resolve_enterprise_webapp_auth_type(app: WebAppRecord) -> WebAppAuthType | None:
    """Return the web-app auth type to stamp on the QR passport, or ``None``.

    Community deployments and public apps keep the minimal anonymous passport.
    Enterprise apps that enforce Web App auth must receive the same enterprise
    passport shape as the standard webapp token exchange, otherwise the passport
    is rejected by ``WebApiResource`` (missing ``token_source`` / ``auth_type`` /
    ``granted_at`` claims).
    """
    if dify_config.DEPLOYMENT_EDITION != DeploymentEdition.ENTERPRISE:
        return None
    from services.enterprise.enterprise_service import EnterpriseService
    from services.web_passport_gateways import resolve_web_app_auth_type

    access_mode = EnterpriseService.WebAppAuth.get_app_access_mode_by_id(app.app_id).access_mode
    if access_mode == WebAppAccessMode.PUBLIC:
        return None
    return resolve_web_app_auth_type(access_mode)


class QrLoginTicketNotFoundError(Exception):
    """The ticket is unknown, already consumed, or gone from the store."""


class QrLoginTicketExpiredError(Exception):
    """The ticket TTL elapsed before the operation completed."""


class QrLoginTicketNotConfirmedError(Exception):
    """``complete`` was called before the provider confirmed the ticket."""


class QrLoginTicketConsumedError(Exception):
    """The ticket was already exchanged for a passport (single use)."""


class QrLoginTransitionError(Exception):
    """A provider event conflicts with the ticket state or a different identity."""


class QrLoginRateLimitedError(Exception):
    """Too many ticket issuances from one IP within the window."""


class QrLoginInvalidSignatureError(Exception):
    """The provider callback signature is missing or invalid."""


class QrLoginTokenGateway(Protocol):
    """Passport issuer with the same surface as ``libs.passport.PassportService``."""

    def issue(self, payload: dict[str, Any]) -> str: ...


@dataclass(frozen=True, slots=True)
class QrLoginTicketState:
    """Ticket state as observed at ``now`` (``status`` may be the derived ``expired``)."""

    ticket_id: str
    status: str
    created_at: int
    expires_at: int
    provider: str | None = None
    provider_openid: str | None = None
    qr_payload: str | None = None


@dataclass(frozen=True, slots=True)
class QrLoginCompletion:
    """Result of a successful ticket exchange."""

    ticket_id: str
    end_user_id: str
    passport: str


class QrLoginService:
    """State machine + passport exchange for QR login tickets.

    Injected collaborators keep the service testable: ``redis`` (ticket store),
    ``token_gateway`` (passport minting), ``now`` (clock), and the ticket TTL /
    rate-limit budget (defaulted from ``dify_config``). Identity lookups use the
    global ``db`` session so tests hit the shared SQLite unit-test schema.
    """

    def __init__(
        self,
        redis: RedisClientWrapper | None = None,
        token_gateway: QrLoginTokenGateway | None = None,
        now: Callable[[], datetime] | None = None,
        ticket_ttl_seconds: int | None = None,
        rate_limit_max_attempts: int | None = None,
        rate_limit_window_seconds: int | None = None,
        webapp_auth_type_resolver: Callable[[WebAppRecord], WebAppAuthType | None] | None = None,
    ) -> None:
        from extensions.ext_redis import redis_client

        self._redis = redis if redis is not None else redis_client
        self._token_gateway = token_gateway if token_gateway is not None else PassportService()
        self._now = now if now is not None else lambda: datetime.now(UTC)
        self._webapp_auth_type_resolver = (
            webapp_auth_type_resolver if webapp_auth_type_resolver is not None else _resolve_enterprise_webapp_auth_type
        )
        self._ticket_ttl_seconds = (
            ticket_ttl_seconds if ticket_ttl_seconds is not None else dify_config.WEB_QR_LOGIN_TICKET_TTL_SECONDS
        )
        self._rate_limiter = RateLimiter(
            prefix="web_qr_login_issue_rate_limit",
            max_attempts=(
                rate_limit_max_attempts
                if rate_limit_max_attempts is not None
                else dify_config.WEB_QR_LOGIN_ISSUE_RATE_LIMIT_MAX_ATTEMPTS
            ),
            time_window=(
                rate_limit_window_seconds
                if rate_limit_window_seconds is not None
                else dify_config.WEB_QR_LOGIN_ISSUE_RATE_LIMIT_WINDOW_SECONDS
            ),
            member_factory=lambda: str(uuid.uuid4()),
            redis_client=self._redis,
        )

    # ------------------------------------------------------------------ #
    # ticket lifecycle
    # ------------------------------------------------------------------ #

    def create_ticket(self, app: WebAppRecord, remote_ip: str) -> QrLoginTicketState:
        """Mint a pending ticket for ``app``; raises QrLoginRateLimitedError when the
        IP has already exhausted its issuance budget in the current window."""
        if self._rate_limiter.is_rate_limited(remote_ip):
            raise QrLoginRateLimitedError()

        now = self._now()
        ticket_id = uuid.uuid4().hex
        expires_at = now + timedelta(seconds=self._ticket_ttl_seconds)
        ticket = {
            "status": QR_STATUS_PENDING,
            "provider": None,
            "provider_openid": None,
            "created_at": int(now.timestamp()),
            "expires_at": int(expires_at.timestamp()),
        }
        self._redis.setex(self._ticket_key(app.app_id, ticket_id), self._ticket_ttl_seconds, json.dumps(ticket))
        return self._to_state(app.app_id, ticket_id, ticket)

    def get_status(self, app: WebAppRecord, ticket_id: str) -> QrLoginTicketState:
        """Return the current state; unknown tickets raise QrLoginTicketNotFoundError."""
        ticket = self._load_ticket(app, ticket_id)
        return self._to_state(app.app_id, ticket_id, ticket)

    def record_event(
        self,
        app: WebAppRecord,
        ticket_id: str,
        event: str,
        provider: str,
        provider_openid: str,
    ) -> QrLoginTicketState:
        """Apply a provider event (``scanned`` / ``confirmed``).

        - ``scanned`` on a pending ticket moves it to ``scanned``; repeating the same
          openid is idempotent; a different openid is a conflict.
        - ``confirmed`` moves pending/scanned tickets to ``confirmed`` with the same
          idempotency/conflict rules.
        """
        if event not in QR_EVENTS:
            raise QrLoginTransitionError(f"unknown QR login event: {event!r}")

        ticket = self._load_ticket(app, ticket_id)
        if self._expired(ticket):
            raise QrLoginTicketExpiredError()

        current = ticket["status"]
        if current in (QR_STATUS_SCANNED, QR_STATUS_CONFIRMED) and ticket["provider_openid"] != provider_openid:
            raise QrLoginTransitionError("ticket is bound to a different identity")
        if current == QR_STATUS_CONFIRMED and event == QR_STATUS_SCANNED:
            # A late duplicate "scanned" after confirmation is harmless: idempotent.
            return self._to_state(app.app_id, ticket_id, ticket)

        if event == QR_STATUS_SCANNED:
            if current == QR_STATUS_PENDING:
                ticket["status"] = QR_STATUS_SCANNED
                ticket["provider"] = provider
                ticket["provider_openid"] = provider_openid
                self._save_ticket(app, ticket_id, ticket)
        else:  # confirmed
            ticket["status"] = QR_STATUS_CONFIRMED
            ticket["provider"] = provider
            ticket["provider_openid"] = provider_openid
            self._save_ticket(app, ticket_id, ticket)
        return self._to_state(app.app_id, ticket_id, ticket)

    def complete(self, app: WebAppRecord, ticket_id: str) -> QrLoginCompletion:
        """Exchange a confirmed ticket for a web-app passport (single use).

        The provider identity is find-or-created in ``qr_login_identities`` so the
        same scanning user always maps to the same anonymous ``EndUser``; the
        winning exchange deletes the ticket, making concurrent completers lose with
        QrLoginTicketConsumedError.
        """
        ticket = self._load_ticket(app, ticket_id)
        if self._expired(ticket):
            raise QrLoginTicketExpiredError()
        if ticket["status"] != QR_STATUS_CONFIRMED:
            raise QrLoginTicketNotConfirmedError()

        session = session_factory.create_session()
        try:
            identity = self._find_or_create_identity(session, app, ticket["provider"], ticket["provider_openid"])
            end_user_id = identity.end_user_id

            claims: dict[str, Any] = {
                "iss": app.app_id,
                "sub": "Web API Passport",
                "app_id": app.app_id,
                "app_code": app.app_code,
                "end_user_id": end_user_id,
            }
            auth_type = self._webapp_auth_type_resolver(app)
            if auth_type is not None:
                # Enterprise apps that enforce Web App auth reject the minimal
                # anonymous passport, so stamp the enterprise claims that mirror
                # the standard webapp token exchange (user asserted by the provider).
                claims.update(
                    {
                        "user_id": ticket["provider_openid"],
                        "auth_type": auth_type,
                        "granted_at": int(self._now().timestamp()),
                        "token_source": "webapp",
                    }
                )
            passport = self._token_gateway.issue(claims)
            deleted = self._redis.delete(self._ticket_key(app.app_id, ticket_id))
            if not deleted:
                # Lost the single-use race: another exchange already consumed the ticket.
                session.rollback()
                raise QrLoginTicketConsumedError()
            session.commit()
        finally:
            session.close()
        return QrLoginCompletion(ticket_id=ticket_id, end_user_id=end_user_id, passport=passport)

    # ------------------------------------------------------------------ #
    # signature verification
    # ------------------------------------------------------------------ #

    @staticmethod
    def verify_event_signature(provided: str | None, raw_body: bytes, *, secret: str | None = None) -> bool:
        """Validate ``X-Qr-Login-Signature: sha256=<hex>`` over the raw request body."""
        if not provided:
            return False
        prefix, _, hexdigest = provided.partition("=")
        if prefix.strip().lower() != QR_SIGNATURE_SCHEME or not hexdigest:
            return False
        key = (secret if secret is not None else dify_config.SECRET_KEY).encode()
        expected = hmac.new(key, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, hexdigest)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _ticket_key(app_id: str, ticket_id: str) -> str:
        return f"{QR_TICKET_PREFIX}:{app_id}:{ticket_id}"

    def _load_ticket(self, app: WebAppRecord, ticket_id: str) -> dict[str, Any]:
        raw = self._redis.get(self._ticket_key(app.app_id, ticket_id))
        if raw is None:
            raise QrLoginTicketNotFoundError()
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)

    def _save_ticket(self, app: WebAppRecord, ticket_id: str, ticket: dict[str, Any]) -> None:
        remaining = int(ticket["expires_at"] - self._now().timestamp())
        self._redis.setex(self._ticket_key(app.app_id, ticket_id), max(remaining, 1), json.dumps(ticket))

    def _expired(self, ticket: dict[str, Any]) -> bool:
        return ticket["expires_at"] <= int(self._now().timestamp())

    def _to_state(self, app_id: str, ticket_id: str, ticket: dict[str, Any]) -> QrLoginTicketState:
        status = ticket["status"] if not self._expired(ticket) else QR_STATUS_EXPIRED
        return QrLoginTicketState(
            ticket_id=ticket_id,
            status=status,
            created_at=ticket["created_at"],
            expires_at=ticket["expires_at"],
            provider=ticket.get("provider"),
            provider_openid=ticket.get("provider_openid"),
            qr_payload=f"{QR_LOGIN_QR_PAYLOAD_PREFIX}:{app_id}:{ticket_id}",
        )

    def _find_or_create_identity(
        self, session: Session, app: WebAppRecord, provider: str, provider_openid: str
    ) -> QrLoginIdentity:
        query = session.query(QrLoginIdentity).filter_by(
            app_id=app.app_id, provider=provider, provider_openid=provider_openid
        )
        identity = query.first()
        if identity is not None:
            return identity

        end_user = EndUser(
            tenant_id=app.tenant_id,
            app_id=app.app_id,
            type=EndUserType.BROWSER,
            _is_anonymous=True,
            session_id=str(uuid.uuid4()),
        )
        session.add(end_user)
        session.flush()
        identity = QrLoginIdentity(
            app_id=app.app_id,
            provider=provider,
            provider_openid=provider_openid,
            end_user_id=end_user.id,
        )
        session.add(identity)
        try:
            session.flush()
        except IntegrityError:
            # Concurrent first exchange won the unique-constraint race: fall back to
            # the winner's row so both callers bind the same visitor identity.
            session.rollback()
            identity = (
                session.query(QrLoginIdentity)
                .filter_by(app_id=app.app_id, provider=provider, provider_openid=provider_openid)
                .first()
            )
            if identity is None:
                raise
        return identity
