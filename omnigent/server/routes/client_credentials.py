"""OAuth 2.0 Client Credentials Grant (RFC 6749 §4.4).

Lets a headless process — a bot, a scheduled job, a CI step — authenticate
to the server *as itself* instead of borrowing a human's session cookie or
holding the cookie-signing secret. A confidential client presents its id
and secret to ``POST /oauth/token`` with
``grant_type=client_credentials`` and receives a short-lived access token
that acts as a configured machine principal.

Opt-in and default-off, following the device grant's precedent: nothing is
mounted unless an operator configures a client via the environment.

Env vars (all start with ``OMNIGENT_SERVICE_CLIENT_``):

- ``ID`` — the confidential client's id, e.g. ``"ci-bot"``.
- ``SECRET`` — the client's secret. Never stored: it is hashed with
  :func:`omnigent.server.device_grant_store.hash_secret` (keyed with the
  server's cookie secret) at startup and only the digest is kept, which is
  what a presented secret is compared against in constant time.
- ``PRINCIPAL`` — the Omnigent identity the issued token acts as. Must be
  a distinct non-admin identity so the machine client cannot inherit an
  admin's privilege level inside an allowlisted path.
- ``TOKEN_TTL_SECONDS`` — optional, default 900, capped at 3600. Tokens
  are short-lived and not refreshable; the client re-presents its secret.

Setting any of the three required vars without the others is a
configuration error and fails loudly at startup.

The issued token is an ordinary delegated token
(:func:`omnigent.server.routes.device_auth.mint_delegated_token`) carrying
the ``sessions`` scope, so the existing fail-closed path allowlist in
:func:`omnigent.server.auth.delegated_path_allowed` confines it to the
delegated surface — admin and user-management endpoints stay unreachable.
It carries no ``grant_id``: there is no device grant behind it, and its
``act`` claim names the client so a machine's actions are attributable in
the audit trail.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter, Request
from starlette.datastructures import FormData
from starlette.responses import JSONResponse, Response

from omnigent.server.auth import RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC
from omnigent.server.device_grant_store import hash_secret

_logger = logging.getLogger(__name__)

_ENV_PREFIX = "OMNIGENT_SERVICE_CLIENT_"
_ID_ENV = f"{_ENV_PREFIX}ID"
_SECRET_ENV = f"{_ENV_PREFIX}SECRET"
_PRINCIPAL_ENV = f"{_ENV_PREFIX}PRINCIPAL"
_TTL_ENV = f"{_ENV_PREFIX}TOKEN_TTL_SECONDS"

GRANT_TYPE = "client_credentials"

_DEFAULT_TOKEN_TTL_SECONDS = 900
# Machine tokens are not refreshable, so the ceiling is the whole blast
# radius of a leaked one — kept at the delegated access-token ceiling.
_MAX_TOKEN_TTL_SECONDS = 3600
_MIN_TOKEN_TTL_SECONDS = 60

_RESERVED_PRINCIPALS = frozenset({RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC})


@dataclass(frozen=True)
class ServiceClientConfig:
    """A single confidential client, configured by environment.

    :param client_id: The client's public id, presented on every token
        request and recorded in the token's ``act`` claim.
    :param secret_hash: Keyed HMAC-SHA256 digest of the client's secret
        (see :func:`omnigent.server.device_grant_store.hash_secret`). The
        raw secret is never retained.
    :param principal: The Omnigent identity issued tokens act as.
    :param token_ttl_seconds: Lifetime of an issued access token.
    """

    client_id: str
    secret_hash: str
    principal: str
    token_ttl_seconds: int

    @classmethod
    def from_env(cls, cookie_secret: bytes) -> ServiceClientConfig | None:
        """Build the configured client from the environment.

        :param cookie_secret: The server's cookie secret, used as the HMAC
            key for the stored secret digest.
        :returns: The configured client, or ``None`` when none of the
            ``OMNIGENT_SERVICE_CLIENT_*`` vars are set (the default).
        :raises RuntimeError: When the configuration is partial or invalid.
        """
        client_id = os.environ.get(_ID_ENV, "").strip()
        secret = os.environ.get(_SECRET_ENV, "").strip()
        principal = os.environ.get(_PRINCIPAL_ENV, "").strip()
        if not (client_id or secret or principal):
            return None
        missing = [
            name
            for name, value in (
                (_ID_ENV, client_id),
                (_SECRET_ENV, secret),
                (_PRINCIPAL_ENV, principal),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Incomplete OAuth client-credentials configuration: "
                f"{', '.join(missing)} must be set alongside the others."
            )
        if principal in _RESERVED_PRINCIPALS:
            raise RuntimeError(
                f"{_PRINCIPAL_ENV} must not be the reserved identity {principal!r}."
            )
        return cls(
            client_id=client_id,
            secret_hash=hash_secret(secret, cookie_secret),
            principal=principal,
            token_ttl_seconds=_resolve_ttl(),
        )

    def secret_matches(self, presented: str, cookie_secret: bytes) -> bool:
        """Whether *presented* is this client's secret (constant time)."""
        return hmac.compare_digest(hash_secret(presented, cookie_secret), self.secret_hash)


def _resolve_ttl() -> int:
    """Read the token TTL, clamped to the supported range."""
    raw = os.environ.get(_TTL_ENV, "").strip()
    if not raw:
        return _DEFAULT_TOKEN_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{_TTL_ENV} must be an integer number of seconds (got {raw!r})."
        ) from exc
    return max(_MIN_TOKEN_TTL_SECONDS, min(ttl, _MAX_TOKEN_TTL_SECONDS))


def _basic_auth_credentials(request: Request) -> tuple[str, str] | None:
    """Return ``(client_id, client_secret)`` from an HTTP Basic header.

    RFC 6749 §2.3.1 makes Basic the preferred way a confidential client
    presents its credentials; the form-body alternative is handled by the
    caller. Returns ``None`` when the header is absent or malformed.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    client_id, sep, client_secret = decoded.partition(":")
    if not sep:
        return None
    return client_id, client_secret


def handle_client_credentials_grant(
    request: Request,
    form: FormData,
    *,
    service_client: ServiceClientConfig,
    cookie_secret: bytes,
    provider_name: str,
    is_admin: Callable[[str], bool] | None = None,
) -> Response:
    """Authenticate a confidential client and issue its access token.

    :param request: The token request (read for HTTP Basic credentials).
    :param form: The already-parsed request form.
    :param service_client: The configured confidential client.
    :param cookie_secret: HMAC key for hashing + signing.
    :param provider_name: Auth provider name recorded on the token.
    :param is_admin: Optional live admin check for the configured
        principal. A principal that has become an admin is refused, so a
        machine token can never carry an admin's privilege level.
    :returns: An RFC 6749 §4.4.3 token response, or an OAuth error.
    """
    from omnigent.server.routes.device_auth import mint_delegated_token

    basic = _basic_auth_credentials(request)
    if basic is not None:
        client_id, client_secret = basic
    else:
        client_id = str(form.get("client_id") or "")
        client_secret = str(form.get("client_secret") or "")
    if not client_id or not client_secret:
        return JSONResponse(status_code=400, content={"error": "invalid_request"})

    # Compare the id in constant time too: it is not a secret, but an
    # early-exit here would leak which id is configured.
    id_ok = hmac.compare_digest(
        client_id.encode("utf-8"), service_client.client_id.encode("utf-8")
    )
    secret_ok = service_client.secret_matches(client_secret, cookie_secret)
    if not (id_ok and secret_ok):
        _logger.warning("oauth/token: client_credentials authentication failed")
        return JSONResponse(status_code=401, content={"error": "invalid_client"})

    if is_admin is not None and is_admin(service_client.principal):
        _logger.error(
            "oauth/token: refusing client_credentials token — principal %s is an admin",
            service_client.principal,
        )
        return JSONResponse(status_code=403, content={"error": "invalid_client"})

    access_token = mint_delegated_token(
        service_client.principal,
        cookie_secret,
        service_client.token_ttl_seconds,
        provider_name,
        grant_id=None,
        client_id=service_client.client_id,
        jti=secrets.token_urlsafe(16),
    )
    _logger.info(
        "oauth/token: issued client_credentials token for client=%s as %s",
        service_client.client_id,
        service_client.principal,
    )
    # No refresh token: RFC 6749 §4.4.3 — the client re-presents its
    # credentials instead.
    return JSONResponse(
        status_code=200,
        content={
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": service_client.token_ttl_seconds,
        },
    )


def create_client_credentials_router(
    service_client: ServiceClientConfig,
    cookie_secret: bytes,
    provider_name: str,
    *,
    is_admin: Callable[[str], bool] | None = None,
) -> APIRouter:
    """Build a standalone ``POST /oauth/token`` client-credentials router.

    Mounted only when the device-grant router is absent — that router owns
    ``/oauth/token`` and dispatches the client-credentials grant itself, so
    the two never both claim the path.

    :param service_client: The configured confidential client.
    :param cookie_secret: HMAC key for hashing + signing.
    :param provider_name: Auth provider name recorded on issued tokens.
    :param is_admin: Optional live admin check for the principal.
    :returns: APIRouter to mount at the app root.
    """
    router = APIRouter()

    @router.post("/oauth/token", dependencies=[])
    async def token(request: Request) -> Response:
        """Exchange client credentials for a short-lived access token."""
        form = await request.form()
        if str(form.get("grant_type") or "") != GRANT_TYPE:
            return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})
        return handle_client_credentials_grant(
            request,
            form,
            service_client=service_client,
            cookie_secret=cookie_secret,
            provider_name=provider_name,
            is_admin=is_admin,
        )

    return router
