"""Controller-boundary tests for the web-app QR login endpoints."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from inspect import unwrap

import pytest
from flask import Flask

from controllers.console import wraps as console_wraps
from controllers.web import qr_login
from controllers.web.error import WebAppNotFoundError
from controllers.web.qr_login import (
    QrInvalidSignatureError,
    QrLoginEventPayload,
    QrLoginTicketApi,
    QrLoginTicketCompleteApi,
    QrLoginTicketEventApi,
    QrLoginTicketStatusApi,
    QrRateLimitExceededError,
    QrTicketConsumedError,
    QrTicketNotConfirmedError,
    QrTicketNotFoundError,
)
from machinery.context import RequestContext
from models.enums import CustomizeTokenStrategy
from models.model import App, AppModelConfig, Site
from services.entities.passport_entities import WebAppRecord
from services.qr_login_service import QrLoginService
from tests.helpers.unit.config_override import apply_config_overrides

APP_ID = "11111111-1111-1111-1111-111111111111"
TENANT_ID = "22222222-2222-2222-2222-222222222222"
SECRET = "s3cr3t-key"
BASE_NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
TICKETS_PATH = "/api/qr-login/tickets?app_code=qr-site"


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.zcard_value = 0

    def setex(self, key: str, ttl: int, value: str) -> None:  # noqa: ARG002
        self.data[key] = value

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) is not None else 0

    def zcard(self, key: str) -> int:  # noqa: ARG002
        return self.zcard_value

    def zremrangebyscore(self, key: str, low: str, high: int) -> int:  # noqa: ARG002
        return 0


class RecordingTokenGateway:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def issue(self, payload: dict) -> str:
        self.payloads.append(payload)
        return f"passport-{len(self.payloads)}"


@pytest.fixture
def app() -> Flask:
    return Flask(__name__)


@pytest.fixture
def context() -> RequestContext:
    return RequestContext("request-1", "trace-1", "", "", "127.0.0.1")


@pytest.fixture
def app_record() -> WebAppRecord:
    return WebAppRecord(site_id="site-1", app_id=APP_ID, tenant_id=TENANT_ID, app_code="qr-site")


def _make_service(redis: FakeRedis) -> QrLoginService:
    return QrLoginService(
        redis=redis, token_gateway=RecordingTokenGateway(), now=lambda: BASE_NOW, ticket_ttl_seconds=120
    )


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def bound_service(monkeypatch: pytest.MonkeyPatch, redis: FakeRedis) -> QrLoginService:
    """Install a service bound to the fake redis onto the controller module."""
    from enums import DeploymentEdition

    # Pin the edition so the default resolver stays inert (no enterprise backend calls).
    apply_config_overrides(monkeypatch, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)
    service = _make_service(redis)
    monkeypatch.setattr(qr_login, "QR_LOGIN_SERVICE", service)
    return service


@pytest.fixture
def resolve_app(monkeypatch: pytest.MonkeyPatch, app_record: WebAppRecord) -> None:
    monkeypatch.setattr(qr_login, "_resolve_web_app", lambda _app_code: app_record)


def _sign(body: bytes, secret: str = SECRET) -> str:
    return f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"


def _event_body(event: str = "scanned", openid: str = "openid-1") -> bytes:
    return json.dumps({"event": event, "provider": "wechat", "provider_openid": openid}).encode()


# --------------------------------------------------------------------------- #
# direct boundary tests (no HTTP)
# --------------------------------------------------------------------------- #


def test_ticket_create_returns_payload(
    bound_service: QrLoginService,  # noqa: ARG001
    context: RequestContext,
    app_record: WebAppRecord,  # noqa: ARG001
    app: Flask,
    resolve_app: None,  # noqa: ARG001
) -> None:
    with app.test_request_context(TICKETS_PATH, method="POST"):
        result = unwrap(QrLoginTicketApi.post)(QrLoginTicketApi(), context)

    assert isinstance(result, tuple)
    payload, status = result
    assert status == 201
    assert len(payload["ticket_id"]) == 32
    assert payload["qr_payload"] == f"dify-qr-login:v1:{APP_ID}:{payload['ticket_id']}"
    assert payload["expires_at"] > 0


def test_ticket_create_unknown_app_raises_not_found(
    monkeypatch: pytest.MonkeyPatch, context: RequestContext, redis: FakeRedis, app: Flask
) -> None:
    monkeypatch.setattr(qr_login, "QR_LOGIN_SERVICE", _make_service(redis))
    monkeypatch.setattr(qr_login, "_resolve_web_app", lambda _app_code: None)

    with app.test_request_context(TICKETS_PATH, method="POST"):
        with pytest.raises(WebAppNotFoundError):
            unwrap(QrLoginTicketApi.post)(QrLoginTicketApi(), context)


def test_ticket_create_rate_limited_maps_429(
    context: RequestContext, redis: FakeRedis, app_record: WebAppRecord, monkeypatch: pytest.MonkeyPatch, app: Flask
) -> None:
    redis.zcard_value = 10
    monkeypatch.setattr(qr_login, "QR_LOGIN_SERVICE", _make_service(redis))
    monkeypatch.setattr(qr_login, "_resolve_web_app", lambda _app_code: app_record)

    with app.test_request_context(TICKETS_PATH, method="POST"):
        with pytest.raises(QrRateLimitExceededError):
            unwrap(QrLoginTicketApi.post)(QrLoginTicketApi(), context)


def test_status_unknown_ticket_maps_404(
    bound_service: QrLoginService,  # noqa: ARG001
    context: RequestContext,
    app: Flask,
    resolve_app: None,  # noqa: ARG001
) -> None:
    with app.test_request_context("/api/qr-login/tickets/missing-ticket/status?app_code=qr-site"):
        with pytest.raises(QrTicketNotFoundError):
            unwrap(QrLoginTicketStatusApi.get)(QrLoginTicketStatusApi(), context, "missing-ticket")


def test_event_missing_signature_maps_400(
    bound_service: QrLoginService,
    context: RequestContext,
    app: Flask,
    app_record: WebAppRecord,
    resolve_app: None,  # noqa: ARG001
) -> None:
    ticket = bound_service.create_ticket(app_record, "127.0.0.1")
    body = _event_body()
    with app.test_request_context(
        f"/api/qr-login/tickets/{ticket.ticket_id}/events?app_code=qr-site", method="POST", data=body
    ):
        with pytest.raises(QrInvalidSignatureError):
            unwrap(QrLoginTicketEventApi.post)(
                QrLoginTicketEventApi(),
                QrLoginEventPayload(event="scanned", provider="wechat", provider_openid="openid-1"),
                context,
                ticket.ticket_id,
            )


def test_event_valid_signature_reports_state(
    bound_service: QrLoginService,
    context: RequestContext,
    app: Flask,
    app_record: WebAppRecord,
    resolve_app: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_config_overrides(monkeypatch, SECRET_KEY=SECRET)
    ticket = bound_service.create_ticket(app_record, "127.0.0.1")
    body = _event_body()
    with app.test_request_context(
        f"/api/qr-login/tickets/{ticket.ticket_id}/events?app_code=qr-site",
        method="POST",
        data=body,
        headers={"X-Qr-Login-Signature": _sign(body)},
    ):
        result = unwrap(QrLoginTicketEventApi.post)(
            QrLoginTicketEventApi(),
            QrLoginEventPayload(event="scanned", provider="wechat", provider_openid="openid-1"),
            context,
            ticket.ticket_id,
        )
    assert result["status"] == "scanned"


def test_complete_not_confirmed_maps_401(
    bound_service: QrLoginService,
    context: RequestContext,
    app_record: WebAppRecord,
    app: Flask,
    resolve_app: None,  # noqa: ARG001
) -> None:
    ticket = bound_service.create_ticket(app_record, "127.0.0.1")
    with app.test_request_context(f"/api/qr-login/tickets/{ticket.ticket_id}/complete?app_code=qr-site", method="POST"):
        with pytest.raises(QrTicketNotConfirmedError):
            unwrap(QrLoginTicketCompleteApi.post)(QrLoginTicketCompleteApi(), context, ticket.ticket_id)


def test_complete_consumed_race_maps_410(
    bound_service: QrLoginService,
    context: RequestContext,
    app_record: WebAppRecord,
    redis: FakeRedis,
    app: Flask,
    resolve_app: None,  # noqa: ARG001
) -> None:
    ticket = bound_service.create_ticket(app_record, "127.0.0.1")
    bound_service.record_event(app_record, ticket.ticket_id, "scanned", "wechat", "openid-1")
    bound_service.record_event(app_record, ticket.ticket_id, "confirmed", "wechat", "openid-1")
    redis.delete = lambda key: 0  # type: ignore[method-assign]  # noqa: ARG005
    with app.test_request_context(f"/api/qr-login/tickets/{ticket.ticket_id}/complete?app_code=qr-site", method="POST"):
        with pytest.raises(QrTicketConsumedError):
            unwrap(QrLoginTicketCompleteApi.post)(QrLoginTicketCompleteApi(), context, ticket.ticket_id)


def test_complete_success_maps_passport(
    bound_service: QrLoginService,
    context: RequestContext,
    app_record: WebAppRecord,
    app: Flask,
    resolve_app: None,  # noqa: ARG001
) -> None:
    ticket = bound_service.create_ticket(app_record, "127.0.0.1")
    bound_service.record_event(app_record, ticket.ticket_id, "scanned", "wechat", "openid-1")
    bound_service.record_event(app_record, ticket.ticket_id, "confirmed", "wechat", "openid-1")

    with app.test_request_context(f"/api/qr-login/tickets/{ticket.ticket_id}/complete?app_code=qr-site", method="POST"):
        result = unwrap(QrLoginTicketCompleteApi.post)(QrLoginTicketCompleteApi(), context, ticket.ticket_id)

    assert result["ticket_id"] == ticket.ticket_id
    assert result["passport"] == "passport-1"
    assert result["end_user_id"]


# --------------------------------------------------------------------------- #
# HTTP-level tests through the registered blueprint
# --------------------------------------------------------------------------- #


@pytest.fixture
def http_app(app: Flask, sqlite_session_factory, monkeypatch: pytest.MonkeyPatch) -> Flask:
    """Flask app with the web blueprint and a published App + Site fixture."""
    from controllers.web import bp

    monkeypatch.setattr(console_wraps, "_is_setup_completed", lambda: True)
    with sqlite_session_factory.begin() as session:
        config = AppModelConfig(app_id=APP_ID)
        config.id = "22222222-2222-2222-2222-222222222222"
        session.add(config)
        session.add(
            App(
                id=APP_ID,
                tenant_id=TENANT_ID,
                name="QR fixture",
                mode="chat",  # pyrefly: ignore[bad-argument-type]
                enable_site=True,
                enable_api=True,
                app_model_config_id=config.id,
            )
        )
        session.add(
            Site(
                app_id=APP_ID,
                code="qr-site",
                title="QR fixture",
                default_language="en-US",
                customize_token_strategy=CustomizeTokenStrategy.UUID,
            )
        )
        session.flush()
    apply_config_overrides(monkeypatch, SECRET_KEY=SECRET)
    app.register_blueprint(bp)
    return app


def test_http_full_flow_happy_path(http_app: Flask, bound_service: QrLoginService) -> None:  # noqa: ARG001
    client = http_app.test_client()

    create = client.post("/api/qr-login/tickets?app_code=qr-site")
    assert create.status_code == 201
    ticket_id = create.get_json()["ticket_id"]

    status = client.get(f"/api/qr-login/tickets/{ticket_id}/status?app_code=qr-site")
    assert status.status_code == 200
    assert status.get_json()["status"] == "pending"

    body = _event_body("scanned")
    scanned = client.post(
        f"/api/qr-login/tickets/{ticket_id}/events?app_code=qr-site",
        data=body,
        headers={"X-Qr-Login-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert scanned.status_code == 200
    assert scanned.get_json()["status"] == "scanned"

    body = _event_body("confirmed")
    confirmed = client.post(
        f"/api/qr-login/tickets/{ticket_id}/events?app_code=qr-site",
        data=body,
        headers={"X-Qr-Login-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["status"] == "confirmed"

    complete = client.post(f"/api/qr-login/tickets/{ticket_id}/complete?app_code=qr-site")
    assert complete.status_code == 200
    result = complete.get_json()
    assert result["ticket_id"] == ticket_id
    assert result["passport"]
    assert result["end_user_id"]


def test_http_unknown_app_is_404(http_app: Flask, bound_service: QrLoginService) -> None:  # noqa: ARG001
    client = http_app.test_client()
    response = client.post("/api/qr-login/tickets?app_code=does-not-exist")
    assert response.status_code == 404
    assert response.get_json()["code"] == "app_not_found"


def test_http_missing_app_code_is_404(http_app: Flask, bound_service: QrLoginService) -> None:  # noqa: ARG001
    client = http_app.test_client()
    response = client.post("/api/qr-login/tickets")
    assert response.status_code == 404


def test_http_forged_signature_is_400(http_app: Flask, bound_service: QrLoginService) -> None:  # noqa: ARG001
    client = http_app.test_client()
    create = client.post("/api/qr-login/tickets?app_code=qr-site")
    ticket_id = create.get_json()["ticket_id"]

    body = _event_body("scanned")
    response = client.post(
        f"/api/qr-login/tickets/{ticket_id}/events?app_code=qr-site",
        data=body,
        headers={"X-Qr-Login-Signature": "sha256=" + "0" * 64, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "qr_invalid_signature"
