"""Unit tests for the provider-agnostic QR login service (ticket state machine)."""

import hashlib
import hmac
import types
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from models import EndUser, QrLoginIdentity
from services.entities.passport_entities import WebAppRecord
from services.qr_login_service import (
    QrLoginRateLimitedError,
    QrLoginService,
    QrLoginTicketConsumedError,
    QrLoginTicketExpiredError,
    QrLoginTicketNotConfirmedError,
    QrLoginTicketNotFoundError,
    QrLoginTicketState,
    QrLoginTransitionError,
    _resolve_enterprise_webapp_auth_type,
)
from services.web_passport_service import WebAppAuthType

APP_ID = "11111111-1111-1111-1111-111111111111"
TENANT_ID = "22222222-2222-2222-2222-222222222222"


class FakeRedis:
    """Minimal dict-backed redis for ticket storage."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.deleted_keys: list[str] = []

    def setex(self, key: str, ttl: int, value: str) -> None:  # noqa: ARG002
        self.data[key] = value

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def delete(self, key: str) -> int:
        if key in self.data:
            self.data.pop(key)
            self.deleted_keys.append(key)
            return 1
        return 0

    def zcard(self, key: str) -> int:  # noqa: ARG002
        return self.zcard_value

    def zremrangebyscore(self, key: str, low: str, high: int) -> int:  # noqa: ARG002
        return 0


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class RecordingTokenGateway:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def issue(self, payload: dict) -> str:
        self.payloads.append(payload)
        return f"passport-{len(self.payloads)}"


@pytest.fixture
def app_record() -> WebAppRecord:
    return WebAppRecord(site_id="site-1", app_id=APP_ID, tenant_id=TENANT_ID, app_code="qr-site")


@pytest.fixture
def redis() -> FakeRedis:
    fake = FakeRedis()
    fake.zcard_value = 0
    return fake


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gateway() -> RecordingTokenGateway:
    return RecordingTokenGateway()


@pytest.fixture
def service(
    monkeypatch: pytest.MonkeyPatch, redis: FakeRedis, clock: FakeClock, gateway: RecordingTokenGateway
) -> QrLoginService:
    # Pin the edition: the default resolver queries the enterprise backend when the
    # edition is ENTERPRISE, which these passport-shape tests must not depend on.
    from enums import DeploymentEdition
    from tests.helpers.unit.config_override import apply_config_overrides

    apply_config_overrides(monkeypatch, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)
    return QrLoginService(redis=redis, token_gateway=gateway, now=clock, ticket_ttl_seconds=120)


def _confirm(service: QrLoginService, app: WebAppRecord, ticket_id: str, openid: str = "openid-1") -> None:
    service.record_event(app, ticket_id, "scanned", "wechat", openid)
    service.record_event(app, ticket_id, "confirmed", "wechat", openid)


# --------------------------------------------------------------------------- #
# create_ticket
# --------------------------------------------------------------------------- #


def test_create_ticket_stores_pending_ticket_with_ttl(
    service: QrLoginService, app_record: WebAppRecord, redis: FakeRedis
) -> None:
    state = service.create_ticket(app_record, "203.0.113.9")

    assert state.status == "pending"
    assert state.created_at == 1794398400  # 2026-11-11T12:00:00Z (FakeClock base)
    assert state.expires_at - state.created_at == 120
    assert state.qr_payload == f"dify-qr-login:v1:{APP_ID}:{state.ticket_id}"

    key = f"qr_login:ticket:{APP_ID}:{state.ticket_id}"
    assert key in redis.data
    stored = service.get_status(app_record, state.ticket_id)
    assert isinstance(stored, QrLoginTicketState)
    assert stored.status == "pending"


def test_create_ticket_rate_limited_when_budget_exhausted(
    service: QrLoginService, app_record: WebAppRecord, redis: FakeRedis
) -> None:
    redis.zcard_value = 10  # equals the default max attempts budget
    with pytest.raises(QrLoginRateLimitedError):
        service.create_ticket(app_record, "203.0.113.9")


# --------------------------------------------------------------------------- #
# state machine
# --------------------------------------------------------------------------- #


def test_state_machine_scanned_then_confirmed(service: QrLoginService, app_record: WebAppRecord) -> None:
    ticket = service.create_ticket(app_record, "203.0.113.9")

    scanned = service.record_event(app_record, ticket.ticket_id, "scanned", "wechat", "openid-1")
    assert scanned.status == "scanned"
    assert scanned.provider == "wechat"
    assert scanned.provider_openid == "openid-1"

    confirmed = service.record_event(app_record, ticket.ticket_id, "confirmed", "wechat", "openid-1")
    assert confirmed.status == "confirmed"

    # idempotent duplicate
    again = service.record_event(app_record, ticket.ticket_id, "confirmed", "wechat", "openid-1")
    assert again.status == "confirmed"


def test_state_machine_rejects_conflicting_identity(service: QrLoginService, app_record: WebAppRecord) -> None:
    ticket = service.create_ticket(app_record, "203.0.113.9")
    service.record_event(app_record, ticket.ticket_id, "scanned", "wechat", "openid-1")

    with pytest.raises(QrLoginTransitionError):
        service.record_event(app_record, ticket.ticket_id, "confirmed", "wechat", "openid-other")


def test_state_machine_rejects_unknown_event(service: QrLoginService, app_record: WebAppRecord) -> None:
    ticket = service.create_ticket(app_record, "203.0.113.9")
    with pytest.raises(QrLoginTransitionError):
        service.record_event(app_record, ticket.ticket_id, "revoked", "wechat", "openid-1")


def test_unknown_ticket_raises_not_found(service: QrLoginService, app_record: WebAppRecord) -> None:
    with pytest.raises(QrLoginTicketNotFoundError):
        service.get_status(app_record, "does-not-exist")
    with pytest.raises(QrLoginTicketNotFoundError):
        service.record_event(app_record, "does-not-exist", "scanned", "wechat", "openid-1")
    with pytest.raises(QrLoginTicketNotFoundError):
        service.complete(app_record, "does-not-exist")


def test_ticket_expiry_derived_from_clock(service: QrLoginService, app_record: WebAppRecord, clock: FakeClock) -> None:
    ticket = service.create_ticket(app_record, "203.0.113.9")

    clock.advance(121)
    assert service.get_status(app_record, ticket.ticket_id).status == "expired"
    with pytest.raises(QrLoginTicketExpiredError):
        service.record_event(app_record, ticket.ticket_id, "scanned", "wechat", "openid-1")
    with pytest.raises(QrLoginTicketExpiredError):
        service.complete(app_record, ticket.ticket_id)


# --------------------------------------------------------------------------- #
# complete
# --------------------------------------------------------------------------- #


def test_complete_rejects_unconfirmed_ticket(service: QrLoginService, app_record: WebAppRecord) -> None:
    ticket = service.create_ticket(app_record, "203.0.113.9")
    with pytest.raises(QrLoginTicketNotConfirmedError):
        service.complete(app_record, ticket.ticket_id)


def test_complete_issues_passport_and_binds_identity(
    service: QrLoginService,
    app_record: WebAppRecord,
    gateway: RecordingTokenGateway,
    redis: FakeRedis,
    sqlite_session: "object",
) -> None:
    ticket = service.create_ticket(app_record, "203.0.113.9")
    _confirm(service, app_record, ticket.ticket_id, "openid-1")

    completion = service.complete(app_record, ticket.ticket_id)

    assert completion.passport == "passport-1"
    assert completion.end_user_id
    assert gateway.payloads == [
        {
            "iss": APP_ID,
            "sub": "Web API Passport",
            "app_id": APP_ID,
            "app_code": "qr-site",
            "end_user_id": completion.end_user_id,
        }
    ]
    # identity + anonymous end user persisted
    identity = sqlite_session.execute(select(QrLoginIdentity).filter_by(provider_openid="openid-1")).scalar_one()
    assert identity.end_user_id == completion.end_user_id
    end_user = sqlite_session.get(EndUser, completion.end_user_id)
    assert end_user is not None
    assert end_user.app_id == APP_ID
    assert end_user._is_anonymous is True
    # ticket consumed
    assert redis.data == {}


def test_complete_is_single_use(service: QrLoginService, app_record: WebAppRecord) -> None:
    ticket = service.create_ticket(app_record, "203.0.113.9")
    _confirm(service, app_record, ticket.ticket_id)

    first = service.complete(app_record, ticket.ticket_id)
    with pytest.raises(QrLoginTicketNotFoundError):
        service.complete(app_record, ticket.ticket_id)
    assert first.passport == "passport-1"


def test_complete_concurrent_loser_raises_consumed(
    service: QrLoginService, app_record: WebAppRecord, redis: FakeRedis
) -> None:
    ticket = service.create_ticket(app_record, "203.0.113.9")
    _confirm(service, app_record, ticket.ticket_id)

    # Simulate the single-use race: the ticket is still readable but the delete
    # reports 0 because another caller already consumed it between read and delete.
    redis.delete = lambda key: 0  # type: ignore[method-assign]  # noqa: ARG005
    with pytest.raises(QrLoginTicketConsumedError):
        service.complete(app_record, ticket.ticket_id)


def test_same_openid_rebinds_same_end_user(
    service: QrLoginService, app_record: WebAppRecord, sqlite_session: "object"
) -> None:
    first = service.create_ticket(app_record, "203.0.113.9")
    _confirm(service, app_record, first.ticket_id, "openid-1")
    completion1 = service.complete(app_record, first.ticket_id)

    second = service.create_ticket(app_record, "203.0.113.9")
    _confirm(service, app_record, second.ticket_id, "openid-1")
    completion2 = service.complete(app_record, second.ticket_id)

    assert completion2.end_user_id == completion1.end_user_id
    end_user_count = sqlite_session.execute(select(func.count(EndUser.id)).where(EndUser.app_id == APP_ID)).scalar_one()
    assert end_user_count == 1


# --------------------------------------------------------------------------- #
# complete: enterprise webapp auth claims
# --------------------------------------------------------------------------- #


def test_complete_stamps_enterprise_claims_when_auth_type_resolved(
    redis: FakeRedis,
    clock: FakeClock,
    gateway: RecordingTokenGateway,
    app_record: WebAppRecord,
    sqlite_session: "object",
) -> None:
    service = QrLoginService(
        redis=redis,
        token_gateway=gateway,
        now=clock,
        ticket_ttl_seconds=120,
        webapp_auth_type_resolver=lambda app: WebAppAuthType.INTERNAL,  # noqa: ARG005
    )
    ticket = service.create_ticket(app_record, "203.0.113.9")
    _confirm(service, app_record, ticket.ticket_id, "openid-1")

    completion = service.complete(app_record, ticket.ticket_id)

    assert completion.end_user_id
    assert gateway.payloads == [
        {
            "iss": APP_ID,
            "sub": "Web API Passport",
            "app_id": APP_ID,
            "app_code": "qr-site",
            "end_user_id": completion.end_user_id,
            # enterprise claims mirror the standard webapp token exchange so the
            # passport passes WebApiResource validation (token_source/auth_type/granted_at)
            "user_id": "openid-1",
            "auth_type": WebAppAuthType.INTERNAL,
            "granted_at": 1794398400,  # FakeClock base timestamp
            "token_source": "webapp",
        }
    ]
    identity = sqlite_session.execute(select(QrLoginIdentity).filter_by(provider_openid="openid-1")).scalar_one()
    assert identity.end_user_id == completion.end_user_id


def test_complete_keeps_minimal_passport_when_auth_type_not_required(
    redis: FakeRedis,
    clock: FakeClock,
    gateway: RecordingTokenGateway,
    app_record: WebAppRecord,
) -> None:
    # public app / community deployment: no enterprise claims (reverse check:
    # a token_source="webapp" passport is rejected for non-enforced apps)
    service = QrLoginService(
        redis=redis,
        token_gateway=gateway,
        now=clock,
        ticket_ttl_seconds=120,
        webapp_auth_type_resolver=lambda app: None,  # noqa: ARG005
    )
    ticket = service.create_ticket(app_record, "203.0.113.9")
    _confirm(service, app_record, ticket.ticket_id, "openid-1")

    completion = service.complete(app_record, ticket.ticket_id)

    assert gateway.payloads == [
        {
            "iss": APP_ID,
            "sub": "Web API Passport",
            "app_id": APP_ID,
            "app_code": "qr-site",
            "end_user_id": completion.end_user_id,
        }
    ]


def test_default_resolver_returns_none_outside_enterprise(
    monkeypatch: pytest.MonkeyPatch, app_record: WebAppRecord
) -> None:
    from enums import DeploymentEdition
    from tests.helpers.unit.config_override import apply_config_overrides

    # Pin explicitly: a local api/.env (gitignored) may flip the default to ENTERPRISE.
    apply_config_overrides(monkeypatch, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)
    assert _resolve_enterprise_webapp_auth_type(app_record) is None


def test_default_resolver_maps_enterprise_access_mode(
    monkeypatch: pytest.MonkeyPatch, app_record: WebAppRecord
) -> None:
    from enums import DeploymentEdition
    from services.enterprise.enterprise_service import EnterpriseService
    from tests.helpers.unit.config_override import apply_config_overrides

    apply_config_overrides(monkeypatch, DEPLOYMENT_EDITION=DeploymentEdition.ENTERPRISE)

    stub = types.SimpleNamespace(access_mode="private_all")
    monkeypatch.setattr(
        EnterpriseService.WebAppAuth,
        "get_app_access_mode_by_id",
        classmethod(lambda cls, app_id: stub),  # noqa: ARG005
    )
    assert _resolve_enterprise_webapp_auth_type(app_record) is WebAppAuthType.INTERNAL

    stub_public = types.SimpleNamespace(access_mode="public")
    monkeypatch.setattr(
        EnterpriseService.WebAppAuth,
        "get_app_access_mode_by_id",
        classmethod(lambda cls, app_id: stub_public),  # noqa: ARG005
    )
    assert _resolve_enterprise_webapp_auth_type(app_record) is None


# --------------------------------------------------------------------------- #
# signature verification
# --------------------------------------------------------------------------- #


def _signature(body: bytes, secret: str = "s3cr3t-key") -> str:
    return f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"


def test_verify_event_signature_accepts_valid_hmac() -> None:
    body = b'{"event": "scanned"}'
    assert QrLoginService.verify_event_signature(_signature(body), body, secret="s3cr3t-key") is True


@pytest.mark.parametrize(
    ("provided", "expected"),
    [
        (None, False),
        ("", False),
        ("sha256=deadbeef", False),
        ("md5=abc", False),
        ("sha256=", False),
    ],
)
def test_verify_event_signature_rejects_invalid(provided: str | None, expected: bool) -> None:
    assert QrLoginService.verify_event_signature(provided, b"body", secret="s3cr3t-key") is expected


def test_verify_event_signature_defaults_to_dify_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.helpers.unit.config_override import apply_config_overrides

    apply_config_overrides(monkeypatch, SECRET_KEY="dify-secret")
    body = b"raw"
    assert QrLoginService.verify_event_signature(_signature(body, "dify-secret"), body) is True
    assert QrLoginService.verify_event_signature(_signature(body, "other-secret"), body) is False
