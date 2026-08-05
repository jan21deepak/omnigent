"""Tests for the OAuth 2.0 Client Credentials Grant (RFC 6749 §4.4).

Two layers:

1. :class:`omnigent.server.routes.client_credentials.ServiceClientConfig` —
   the env-driven, default-off configuration and its fail-loud validation.
2. ``POST /oauth/token`` end-to-end via a FastAPI TestClient in accounts
   mode — with and without the device grant also mounted — covering client
   authentication, the confinement of the issued machine token to the
   delegated path allowlist, and the non-admin principal requirement.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from omnigent.server.device_grant_store import hash_secret
from omnigent.server.routes import client_credentials
from omnigent.server.routes.client_credentials import ServiceClientConfig
from omnigent.server.routes.rate_limit import SlidingWindowRateLimiter
from tests.server.test_device_auth import _build_accounts_app

_KEY = b"k" * 32
_CLIENT_ID = "ci-bot"
_CLIENT_SECRET = "s3cret-machine-credential"
_PRINCIPAL = "ci-bot@machines.example.com"
_TTL_ENV = "OMNIGENT_SERVICE_CLIENT_TOKEN_TTL_SECONDS"
_ENV_NAMES = (
    "OMNIGENT_SERVICE_CLIENT_ID",
    "OMNIGENT_SERVICE_CLIENT_SECRET",
    "OMNIGENT_SERVICE_CLIENT_PRINCIPAL",
)


# ── Configuration (unit) ──────────────────────────────────────────


def _set_client_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    values: dict[str, str | None] = {
        "OMNIGENT_SERVICE_CLIENT_ID": _CLIENT_ID,
        "OMNIGENT_SERVICE_CLIENT_SECRET": _CLIENT_SECRET,
        "OMNIGENT_SERVICE_CLIENT_PRINCIPAL": _PRINCIPAL,
    }
    values.update(overrides)
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_config_is_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OMNIGENT_SERVICE_CLIENT_* vars ⇒ no client, nothing mounted."""
    _set_client_env(monkeypatch, **dict.fromkeys(_ENV_NAMES))
    monkeypatch.delenv(_TTL_ENV, raising=False)
    assert ServiceClientConfig.from_env(_KEY) is None


@pytest.mark.parametrize("missing", _ENV_NAMES)
def test_config_partial_is_fail_loud(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """A half-configured client is an operator error, not a silent no-op."""
    _set_client_env(monkeypatch, **{missing: None})
    with pytest.raises(RuntimeError, match=missing):
        ServiceClientConfig.from_env(_KEY)


def test_config_rejects_reserved_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_client_env(monkeypatch, OMNIGENT_SERVICE_CLIENT_PRINCIPAL="local")
    with pytest.raises(RuntimeError, match="reserved"):
        ServiceClientConfig.from_env(_KEY)


def test_config_stores_only_the_secret_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_client_env(monkeypatch)
    monkeypatch.delenv(_TTL_ENV, raising=False)
    config = ServiceClientConfig.from_env(_KEY)
    assert config is not None
    assert config.secret_hash == hash_secret(_CLIENT_SECRET, _KEY)
    assert _CLIENT_SECRET not in repr(config)
    assert config.secret_matches(_CLIENT_SECRET, _KEY)
    assert not config.secret_matches(_CLIENT_SECRET + "x", _KEY)
    # A digest keyed with a different cookie secret must not match.
    assert not config.secret_matches(_CLIENT_SECRET, b"j" * 32)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("300", 300), ("1", 60), ("999999", 3600), ("", 900)],
)
def test_config_clamps_ttl(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: int
) -> None:
    _set_client_env(monkeypatch)
    monkeypatch.setenv(_TTL_ENV, configured)
    config = ServiceClientConfig.from_env(_KEY)
    assert config is not None
    assert config.token_ttl_seconds == expected


# ── Token endpoint (integration) ──────────────────────────────────


@pytest.fixture(autouse=True)
def fresh_failed_auth_budget(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Give each test its own failed-attempt budget (the limiter is a module
    global keyed by source IP, which every TestClient shares)."""

    def reset() -> None:
        monkeypatch.setattr(
            client_credentials,
            "_failed_auth",
            SlidingWindowRateLimiter(client_credentials._FAILED_AUTH_MAX, 60, 100),
        )

    reset()
    return reset


@pytest.fixture
def app_with_device_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Accounts app with both the device grant and a machine client."""
    _set_client_env(monkeypatch)
    monkeypatch.delenv(_TTL_ENV, raising=False)
    yield from _build_accounts_app(tmp_path, monkeypatch, device_grant_enabled=True)


@pytest.fixture
def app_client_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Accounts app with ONLY the machine client — no device grant."""
    _set_client_env(monkeypatch)
    monkeypatch.delenv(_TTL_ENV, raising=False)
    yield from _build_accounts_app(tmp_path, monkeypatch, device_grant_enabled=False)


def _request_token(
    client: TestClient, *, client_id: str = _CLIENT_ID, client_secret: str = _CLIENT_SECRET
) -> httpx.Response:
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )


@pytest.mark.parametrize("fixture_name", ["app_with_device_grant", "app_client_only"])
def test_client_credentials_issues_confined_token(
    fixture_name: str, request: pytest.FixtureRequest
) -> None:
    """The machine client gets a token that acts as its own principal and
    reaches the delegated surface but not the admin surface."""
    client: TestClient = request.getfixturevalue(fixture_name)
    r = _request_token(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900
    # RFC 6749 §4.4.3: no refresh token — the client re-presents its secret.
    assert "refresh_token" not in body

    auth = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/v1/agents", headers=auth).status_code == 200
    assert client.get("/auth/users", headers=auth).status_code in (401, 403)

    me = client.get("/v1/me", headers=auth)
    # /v1/me is outside the delegated allowlist, so the machine token is not
    # an identity there either — the confinement is path-based, not per-route.
    assert me.json().get("user_id") is None


def test_client_credentials_token_acts_as_the_configured_principal(
    app_client_only: TestClient,
) -> None:
    import jwt

    token = _request_token(app_client_only).json()["access_token"]
    payload = jwt.decode(token, options={"verify_signature": False})
    assert payload["sub"] == _PRINCIPAL
    assert payload["scope"] == "sessions"
    assert payload["act"] == {"client_id": _CLIENT_ID}
    # No device grant behind a machine token.
    assert "grant_id" not in payload


def test_client_credentials_is_exempt_from_the_device_client_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device flow's shared secret gates the device endpoints only — the
    machine client authenticates with its own id + secret."""
    _set_client_env(monkeypatch)
    monkeypatch.delenv(_TTL_ENV, raising=False)
    monkeypatch.setenv("OMNIGENT_DEVICE_CLIENT_SECRET", "device-flow-shared-secret")
    clients = _build_accounts_app(tmp_path, monkeypatch, device_grant_enabled=True)
    with next(clients) as client:
        assert _request_token(client).status_code == 200
        # The device grants stay gated.
        r = client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": "whatever"},
        )
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"


def test_client_credentials_accepts_http_basic(app_client_only: TestClient) -> None:
    creds = base64.b64encode(f"{_CLIENT_ID}:{_CLIENT_SECRET}".encode()).decode()
    r = app_client_only.post(
        "/oauth/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {creds}"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [(_CLIENT_ID, "wrong"), ("other-bot", _CLIENT_SECRET)],
)
def test_client_credentials_rejects_bad_credentials(
    app_client_only: TestClient, client_id: str, client_secret: str
) -> None:
    r = _request_token(app_client_only, client_id=client_id, client_secret=client_secret)
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_client_credentials_throttles_failed_attempts(
    app_client_only: TestClient, fresh_failed_auth_budget: Callable[[], None]
) -> None:
    """The secret is the only thing guarding the endpoint, so online guessing
    is capped per source IP — and a correct client is not collateral."""
    for _ in range(client_credentials._FAILED_AUTH_MAX):
        assert _request_token(app_client_only, client_secret="guess").status_code == 401
    r = _request_token(app_client_only, client_secret="guess")
    assert r.status_code == 429
    assert r.json()["error"] == "slow_down"

    # Only failures are charged, so a correct client never spends the budget.
    fresh_failed_auth_budget()
    for _ in range(client_credentials._FAILED_AUTH_MAX + 5):
        assert _request_token(app_client_only).status_code == 200


def test_client_credentials_requires_credentials(app_client_only: TestClient) -> None:
    r = app_client_only.post("/oauth/token", data={"grant_type": "client_credentials"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_other_grant_types_are_unsupported_without_the_device_grant(
    app_client_only: TestClient,
) -> None:
    r = app_client_only.post("/oauth/token", data={"grant_type": "refresh_token"})
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


def test_grant_is_not_mounted_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default-off: without a configured client the routed surface is unchanged."""
    _set_client_env(monkeypatch, **dict.fromkeys(_ENV_NAMES))
    clients = _build_accounts_app(tmp_path, monkeypatch, device_grant_enabled=False)
    with next(clients) as client:
        r = client.post("/oauth/token", data={"grant_type": "client_credentials"})
        # Unrouted: 404, or 405 when only the SPA's GET fallback matches.
        assert r.status_code in (404, 405)


def test_admin_principal_is_refused_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The machine principal must be a distinct non-admin identity."""
    _set_client_env(monkeypatch, OMNIGENT_SERVICE_CLIENT_PRINCIPAL="admin@example.com")
    admins_file = tmp_path / "admins"
    admins_file.write_text("admin@example.com\n", encoding="utf-8")
    monkeypatch.setenv("OMNIGENT_ADMIN_LIST_PATH", str(admins_file))
    clients = _build_accounts_app(tmp_path, monkeypatch, device_grant_enabled=False)
    with pytest.raises(RuntimeError, match="must not be an admin"):
        next(clients)
