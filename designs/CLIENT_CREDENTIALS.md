# Machine Auth — Client Credentials Grant (RFC 6749 §4.4)

> **IMPLEMENTED.**
>
> Server: `omnigent/server/routes/client_credentials.py` (the
> `ServiceClientConfig` + the grant handler + a standalone router),
> dispatch from `POST /oauth/token` in
> `omnigent/server/routes/device_auth.py`, and the scope-based path
> confinement in `omnigent/server/auth.py` (`delegated_path_allowed`).
> Wired in `omnigent/server/app.py`, **opt-in and default-off** — nothing
> is mounted unless an operator configures a client — and then only in
> **accounts** mode, which is the only mode with a server-mintable
> identity and a cookie secret to sign with.
>
> Tests: `tests/server/test_client_credentials.py`.

## Problem

A headless process — a bot, a scheduled job, a CI step — had no way to
authenticate to the server *as itself*. Everything that mints a credential
assumes a human at a browser: the device grant (`designs/DEVICE_AUTH.md`)
walks a person through a consent screen, and cookie sessions come from an
interactive login. That left an operator two bad options: run a browser
login for a pseudo-human service account (a hand-refreshed cookie that is
indistinguishable from a real person in the audit trail), or hand the
process the cookie-signing secret (the key to every session on the server,
for one narrow capability). Header/proxy mode is a third route but moves
the problem outward — identity is then asserted by an upstream proxy that
has to be configured and trusted for this purpose.

Beyond convenience, a machine principal is the identity half of several
things that are otherwise hard: attributing a bot's actions in an audit
trail, giving an automated actor *less* access than a human, and revoking
one automated client without disturbing anybody's session.

## Design

One confidential client, configured entirely by environment:

| Variable | Meaning |
| --- | --- |
| `OMNIGENT_SERVICE_CLIENT_ID` | The client's public id, e.g. `ci-bot`. |
| `OMNIGENT_SERVICE_CLIENT_SECRET` | The client's secret. Hashed at startup with `hash_secret` (keyed with the cookie secret); only the digest is kept. |
| `OMNIGENT_SERVICE_CLIENT_PRINCIPAL` | The Omnigent identity issued tokens act as. |
| `OMNIGENT_SERVICE_CLIENT_TOKEN_TTL_SECONDS` | Optional, default 900, clamped to 60–3600. |

Setting some but not all of the three required vars is a configuration
error and fails loudly at startup, as does a principal that is a reserved
identity (`local`, `__public__`) or an admin — the machine client must be a
distinct non-admin identity so it cannot inherit an admin's privilege level
inside an allowlisted path.

### Flow

```
  POST /oauth/token
    grant_type=client_credentials
    client_id / client_secret        (form body, or HTTP Basic per §2.3.1)
  →  { access_token, token_type: Bearer, expires_in }
```

No refresh token (RFC 6749 §4.4.3): the token is short-lived and the client
re-presents its secret. The secret is compared in constant time against the
stored digest; a mismatch is `401 invalid_client`.

### Reuse of the delegated machinery

The issued token is an ordinary delegated token — `mint_delegated_token`
with the existing `sessions` scope — so the fail-closed allowlist in
`delegated_path_allowed` confines it to the delegated surface and keeps it
off the admin and user-management paths. It differs from a device-grant
token in one claim: it carries **no `grant_id`**, because there is no
device grant behind it. `UnifiedAuthProvider._check_cookie` therefore keys
the delegated checks off the `scope` claim (the claim the allowlist was
always documented against) and applies the live grant-revocation lookup
only to tokens that name a grant.

Its `act` claim names the client, so a machine's actions are attributable
in logs and audit exactly like a delegated client's.

### Endpoint ownership

The device-grant router owns `POST /oauth/token`. When it is mounted, the
configured client is handed to it and it dispatches `client_credentials`
itself; when it is not, a standalone router provides the same endpoint. The
two never both claim the path, and either grant works without the other.

The device flow's optional shared secret (`OMNIGENT_DEVICE_CLIENT_SECRET`)
gates the *device* endpoints, whose initiator is otherwise unauthenticated.
The machine client presents its own id and secret, so it is dispatched before
that gate and never needs the device secret — the grant behaves identically
whether or not the device grant is mounted alongside it.

### Revocation

A machine token names no grant, so unlike a device-grant token it is not
revocable mid-flight: it is a stateless HS256 JWT and stays valid for its TTL
(≤ 1 h), and rotating the client secret only stops *new* tokens from being
issued. The short TTL is the bound. Making machine tokens revocable would mean
persisting a per-client grant row and paying its lookup on every request — the
thing the token's statelessness buys back for a caller that re-authenticates
cheaply.

## Out of scope / follow-ups

- More than one confidential client (this is a single env-configured
  client; a table of clients would want an admin UI and a store).
- Per-client scopes beyond the single "session APIs, no admin" scope.
- Rotating a client secret without a restart, and a kill switch for tokens
  already issued to a client (see Revocation above).
