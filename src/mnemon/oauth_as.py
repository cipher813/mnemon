"""Self-hosted OAuth 2.1 Authorization Server.

Removes mnemon's external-AS (Auth0) dependency so self-hosted deployments
can run as a single process with no third-party auth vendor. This module
is the AS-side counterpart to ``auth.py`` (which is the Resource Server).

What's implemented
------------------
- RSA keypair management: generated on first run, persisted to the Fly
  volume, loaded on subsequent starts. Private key never leaves the
  server; public key is published at ``/.well-known/jwks.json``.
- ``/.well-known/oauth-authorization-server`` metadata (RFC 8414).
- ``/.well-known/jwks.json`` — the public key in JWKS format.
- ``/oauth/authorize`` — HTML passphrase login form (GET) and code
  issuance after successful login (POST). PKCE code_challenge stored
  server-side for later verification.
- ``/oauth/token`` — authorization_code and refresh_token grants. RS256
  JWT access tokens, opaque refresh tokens with rotation.
- ``/register`` — Dynamic Client Registration per RFC 7591. Browser
  clients self-register; no manual client provisioning.
- Feature flag ``MNEMON_AS_ENABLED``. Off by default.

Design notes
------------
- **Single-user only.** This AS does not implement a user database. The
  server owner is the sole authorized principal, authenticated by a
  setup-time passphrase (``MNEMON_AS_PASSPHRASE``). No signup UI, no
  password reset, no account recovery — intentional scope reduction for
  personal self-host. Multi-user is explicitly out of scope indefinitely.
- **RS256 signing.** Standard for OIDC/OAuth 2.1.
- **Key storage on Fly volume.** ``/data/oauth_keys/private.pem`` persists
  across deploys because the volume does. If the volume is destroyed,
  all issued tokens become invalid — acceptable for personal use; in
  production you'd back up the key or accept forced re-registration.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Environment variable names.
ENV_AS_ENABLED = "MNEMON_AS_ENABLED"
ENV_AS_PASSPHRASE = "MNEMON_AS_PASSPHRASE"
ENV_PUBLIC_URL = "MNEMON_PUBLIC_URL"
ENV_KEY_DIR = "MNEMON_AS_KEY_DIR"

# Minimum passphrase length. 16 chars is well below what
# `secrets.token_urlsafe(32)` produces (43 chars) but above most
# casually-chosen passwords. The AS has no rate limiting and the
# passphrase is the only credential gating the entire vault — a
# brute-force-friendly passphrase defeats the whole self-hosted story.
_MIN_PASSPHRASE_LEN = 16

# RS256 is the default for OIDC/OAuth 2.1 JWT signing. Don't add alg
# negotiation — pick one and stick with it.
JWT_ALG = "RS256"
KEY_ID = "mnemon-as-1"  # bump if rotating keys in a future PR


class AuthorizationServerConfig:
    """Configuration for the self-hosted AS loaded from environment.

    Attributes:
        enabled: whether the AS is active. When False, all AS endpoints
            (well-known metadata, JWKS, /authorize, /token, /register)
            return 404. Browser clients cannot authenticate; the
            resource server still accepts ``MNEMON_LOCAL_TOKEN`` bearer
            auth for headless clients.
        public_url: externally-reachable base URL (e.g.,
            ``https://mnemon-memory.fly.dev``). Used to build issuer
            claim and endpoint URLs in metadata documents.
        passphrase: single-user login credential. Required when enabled;
            server refuses to start without it.
        key_dir: directory holding the RSA keypair. Defaults to
            ``/data/oauth_keys`` on Fly, ``~/.mnemon/oauth_keys``
            locally.
    """

    def __init__(
        self,
        enabled: bool = False,
        public_url: str | None = None,
        passphrase: str | None = None,
        key_dir: Path | None = None,
    ) -> None:
        self.enabled = enabled
        self.public_url = public_url
        self.passphrase = passphrase
        self.key_dir = key_dir or _default_key_dir()

    @classmethod
    def from_env(cls) -> AuthorizationServerConfig:
        enabled = os.environ.get(ENV_AS_ENABLED, "").lower() in ("1", "true", "yes")
        public_url = os.environ.get(ENV_PUBLIC_URL) or None
        passphrase = os.environ.get(ENV_AS_PASSPHRASE) or None
        key_dir_env = os.environ.get(ENV_KEY_DIR)
        key_dir = Path(key_dir_env) if key_dir_env else _default_key_dir()
        return cls(enabled=enabled, public_url=public_url,
                   passphrase=passphrase, key_dir=key_dir)

    @property
    def issuer(self) -> str:
        """The ``iss`` claim value for tokens the AS issues.

        Per OAuth 2.1 Authorization Server Metadata (RFC 8414), this is
        also the base from which well-known URLs are built (appending
        ``/.well-known/oauth-authorization-server``).
        """
        return (self.public_url or "").rstrip("/")

    def validate(self) -> list[str]:
        """Return a list of config problems, empty if OK to start.

        Callers should refuse to enable the AS when this returns a
        non-empty list — better to fail fast at boot than serve broken
        metadata.

        Does NOT validate the passphrase — callers must check
        ``validate_passphrase`` separately so the passphrase value never
        flows through the same code path as logged validation output.
        """
        problems: list[str] = []
        if not self.enabled:
            return problems
        if not self.public_url:
            problems.append(f"{ENV_PUBLIC_URL} must be set when AS is enabled")
        return problems

    @staticmethod
    def validate_passphrase(*, has_passphrase: bool,
                            passphrase_length: int) -> list[str]:
        """Validate passphrase config from metadata only (no value).

        This method is deliberately a static method that takes only
        non-sensitive metadata (boolean + integer) rather than the
        passphrase string itself, so CodeQL's taint-tracking can see
        that no password-classified data reaches any downstream log
        sink.

        Returns a list of problem strings (empty = passphrase is OK).
        """
        problems: list[str] = []
        if not has_passphrase:
            problems.append(
                f"{ENV_AS_PASSPHRASE} must be set when AS is enabled "
                "(single-user login credential)"
            )
        elif passphrase_length < _MIN_PASSPHRASE_LEN:
            problems.append(
                f"{ENV_AS_PASSPHRASE} is too short "
                f"(minimum {_MIN_PASSPHRASE_LEN} chars). "
                "Generate a high-entropy value: "
                "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        return problems


def _default_key_dir() -> Path:
    """Return the default key directory.

    Uses ``MNEMON_VAULT_DIR/oauth_keys`` so keys live alongside the
    vault data on the Fly volume. Falls back to ``~/.mnemon/oauth_keys``
    for local development.
    """
    vault_dir = os.environ.get("MNEMON_VAULT_DIR")
    if vault_dir:
        return Path(vault_dir) / "oauth_keys"
    return Path.home() / ".mnemon" / "oauth_keys"


# ── Key management ───────────────────────────────────────────────────────────


def ensure_keypair(key_dir: Path) -> tuple[bytes, bytes]:
    """Load or generate the AS signing keypair.

    On first call, generates a fresh 2048-bit RSA keypair and persists
    the private key to ``key_dir/private.pem`` with 0600 permissions.
    Subsequent calls load the existing key. The public key is derived
    from the private key on every call (cheap) rather than stored
    separately to avoid the private/public drift that comes from two
    separate files.

    Returns:
        A ``(private_pem, public_pem)`` tuple. Both are PEM-encoded bytes.

    Raises:
        ImportError: if cryptography is not installed (required by authlib).
        OSError: if the key directory cannot be created.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key_dir.mkdir(parents=True, exist_ok=True)
    private_path = key_dir / "private.pem"

    if private_path.exists():
        private_pem = private_path.read_bytes()
        private_key = serialization.load_pem_private_key(private_pem, password=None)
    else:
        logger.info(
            "oauth_as: generating new RSA keypair at %s (first run)",
            private_path,
        )
        private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        private_path.write_bytes(private_pem)
        private_path.chmod(0o600)

    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def public_key_jwk(key_dir: Path) -> dict[str, Any]:
    """Return the AS public key in JWK format (RFC 7517).

    Used to serve ``/.well-known/jwks.json``. The returned dict always
    contains ``kty``, ``alg``, ``use``, ``kid``, ``n``, ``e``.
    """
    from authlib.jose import JsonWebKey

    _, public_pem = ensure_keypair(key_dir)
    jwk = JsonWebKey.import_key(public_pem, {"kty": "RSA"}).as_dict()
    jwk.update({
        "alg": JWT_ALG,
        "use": "sig",
        "kid": KEY_ID,
    })
    return jwk


def jwks_document(key_dir: Path) -> dict[str, Any]:
    """Return the full JWKS document (RFC 7517) for ``/.well-known/jwks.json``."""
    return {"keys": [public_key_jwk(key_dir)]}


# ── Metadata ────────────────────────────────────────────────────────────────


def authorization_server_metadata(config: AuthorizationServerConfig) -> dict[str, Any]:
    """Return RFC 8414 Authorization Server Metadata.

    Served at ``/.well-known/oauth-authorization-server``. Some of the
    endpoint URLs point at routes that are not yet implemented (see the
    module docstring). That's expected for PR #36; clients will fail
    loudly if they try to use them before PRs #37/#38 land, which is
    better than silently half-working.
    """
    issuer = config.issuer
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],  # PKCE-only, no client secrets
        "scopes_supported": ["mcp"],
        "service_documentation": "https://github.com/nousergon/mnemon",
    }


# ── ASGI handlers ────────────────────────────────────────────────────────────


async def serve_jwks(config: AuthorizationServerConfig, send) -> None:
    """ASGI handler: serve ``/.well-known/jwks.json``.

    Returns 404 when the AS is disabled so clients can tell the
    difference between "this server doesn't host its own AS" and "AS is
    misconfigured."
    """
    from .auth import _send_json  # reuse existing JSON helper

    if not config.enabled:
        await _send_json(send, 404, {"error": "authorization server not enabled"})
        return
    try:
        doc = jwks_document(config.key_dir)
    except Exception as e:  # noqa: BLE001
        logger.exception("oauth_as: failed to build JWKS: %s", e)
        await _send_json(send, 500, {"error": "jwks unavailable"})
        return
    await _send_json(send, 200, doc)


async def serve_as_metadata(config: AuthorizationServerConfig, send) -> None:
    """ASGI handler: serve ``/.well-known/oauth-authorization-server``."""
    from .auth import _send_json

    if not config.enabled:
        await _send_json(send, 404, {"error": "authorization server not enabled"})
        return
    await _send_json(send, 200, authorization_server_metadata(config))


# ── Authorization codes + refresh tokens (volume-backed storage) ────────────
#
# Persisted to JSON files in ``key_dir`` so Fly restarts (deploys,
# autostop wake, secret rotation) don't invalidate outstanding refresh
# tokens — which would force every connected MCP client to re-auth via
# passphrase on every wake. Auth codes are persisted too for symmetry
# and to cover the edge case of the machine idling during a 10-minute
# OAuth flow.
#
# Files are mode 0600 (same as the AS private key). Writes go through
# tmp + atomic rename so a crash mid-write can't corrupt the files.
# Expired entries are dropped on load so the files don't grow forever.

_AUTH_CODE_TTL_SEC = 600          # 10 min — RFC recommends ≤ 10 min
_ACCESS_TOKEN_TTL_SEC = 3600      # 1 hour
_REFRESH_TOKEN_TTL_SEC = 30 * 24 * 3600  # 30 days

# After a refresh token is rotated, the old RT remains exchangeable for
# the *same* new pair for this many seconds. Covers the case where the
# token endpoint succeeded but the response didn't reach the client
# (network blip, Fly cold-start delay, mcp-proxy retry) — the client's
# retry presents the old RT, gets the same response back, and avoids
# being kicked into a "reconnect required" state. Replay after this
# window still returns invalid_grant. OAuth 2.1 recommends exactly this
# pattern for rotating refresh tokens.
_REFRESH_TOKEN_GRACE_SEC = 60

_AUTH_CODES_FILENAME = "auth_codes.json"
_REFRESH_TOKENS_FILENAME = "refresh_tokens.json"
_ROTATED_TOKENS_FILENAME = "rotated_tokens.json"

# Per-IP rate limit on failed /oauth/authorize passphrase attempts.
# Deliberately in-memory (resets on restart) — this is best-effort
# brute-force hygiene, not a lockout mechanism that needs to persist.
# A passphrase generated via `secrets.token_urlsafe(32)` has ~256 bits of
# entropy so this is belt-and-suspenders, but at 10 tries per 5 minutes
# per IP it raises the cost of even a 16-char low-entropy passphrase
# from "seconds on one box" to "years across a distributed botnet."
_FAILED_ATTEMPT_WINDOW_SEC = 300
_MAX_FAILED_ATTEMPTS = 10
_failed_attempts: dict[str, list[float]] = {}


def _load_token_file(path: Path, label: str) -> dict[str, dict[str, Any]]:
    """Load a token JSON file, pruning expired entries. Returns {} on any
    error — the cost is that clients re-auth, which is the same fallback
    as losing the in-memory dict before this was persisted."""
    if not path.exists():
        return {}
    try:
        entries: dict[str, dict[str, Any]] = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "oauth_as: %s unreadable (%s); treating as empty. "
            "Affected clients will need to re-auth.",
            label, e,
        )
        return {}
    now = _now()
    return {k: v for k, v in entries.items() if v.get("expires_at", 0) > now}


def _save_token_file(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Atomic-rename write of a token JSON file at mode 0600. These files
    contain bearer-equivalent secrets, so perms must be as tight as the
    AS private key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def _auth_codes_path(config: AuthorizationServerConfig) -> Path:
    return config.key_dir / _AUTH_CODES_FILENAME


def _refresh_tokens_path(config: AuthorizationServerConfig) -> Path:
    return config.key_dir / _REFRESH_TOKENS_FILENAME


def _load_auth_codes(config: AuthorizationServerConfig) -> dict[str, dict[str, Any]]:
    return _load_token_file(_auth_codes_path(config), "auth_codes.json")


def _save_auth_codes(
    config: AuthorizationServerConfig, codes: dict[str, dict[str, Any]]
) -> None:
    _save_token_file(_auth_codes_path(config), codes)


def _load_refresh_tokens(config: AuthorizationServerConfig) -> dict[str, dict[str, Any]]:
    return _load_token_file(_refresh_tokens_path(config), "refresh_tokens.json")


def _rotated_tokens_path(config: AuthorizationServerConfig) -> Path:
    return config.key_dir / _ROTATED_TOKENS_FILENAME


def _load_rotated_tokens(config: AuthorizationServerConfig) -> dict[str, dict[str, Any]]:
    """Load the rotated-RT idempotency cache, pruning entries past their
    grace window. Used by /oauth/token to return the same new pair for a
    retried refresh of an already-rotated RT."""
    return _load_token_file(_rotated_tokens_path(config), "rotated_tokens.json")


def _save_rotated_tokens(
    config: AuthorizationServerConfig, entries: dict[str, dict[str, Any]]
) -> None:
    _save_token_file(_rotated_tokens_path(config), entries)


def _save_refresh_tokens(
    config: AuthorizationServerConfig, tokens: dict[str, dict[str, Any]]
) -> None:
    _save_token_file(_refresh_tokens_path(config), tokens)


def _reset_state_for_tests() -> None:
    """No-op — kept for backward compat with existing test fixtures.
    State now lives per-``key_dir`` on disk, so each test's ``tmp_path``
    fixture provides natural isolation."""


def _now() -> int:
    import time
    return int(time.time())


# ── PKCE ────────────────────────────────────────────────────────────────────


def _verify_pkce_s256(code_verifier: str, code_challenge: str) -> bool:
    """Return True if BASE64URL(SHA256(code_verifier)) == code_challenge.

    Per RFC 7636. Constant-time comparison to avoid timing leaks.
    """
    import base64
    import hashlib
    import hmac

    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected, code_challenge)


# ── Token minting ───────────────────────────────────────────────────────────


def mint_access_token(
    config: AuthorizationServerConfig,
    *,
    subject: str,
    scope: str,
    audience: str | None = None,
    ttl_sec: int = _ACCESS_TOKEN_TTL_SEC,
) -> str:
    """Mint an RS256 JWT access token signed with the AS's private key.

    The ``aud`` claim defaults to ``{issuer}/mcp`` — matching the Resource
    Server's audience convention so the existing OAuthMiddleware JWKS
    validation path will accept these tokens once PR #39 points it at
    the AS's own JWKS.
    """
    import jwt

    private_pem, _ = ensure_keypair(config.key_dir)
    now = _now()
    aud = audience or f"{config.issuer}/mcp"
    payload = {
        "iss": config.issuer,
        "aud": aud,
        "sub": subject,
        "iat": now,
        "exp": now + ttl_sec,
        "scope": scope,
        # jti makes each token unique even when minted in the same second
        # (back-to-back refreshes) and enables future revocation by
        # blocklisting specific jti values.
        "jti": _new_random_token(16),
    }
    return jwt.encode(
        payload, private_pem, algorithm=JWT_ALG, headers={"kid": KEY_ID}
    )


def _new_random_token(nbytes: int = 32) -> str:
    import secrets
    return secrets.token_urlsafe(nbytes)


# ── Resource-server verification side ───────────────────────────────────────


def verify_self_hosted_token(
    config: AuthorizationServerConfig, token: str
) -> dict[str, Any]:
    """Verify a JWT was issued by this AS and return its claims.

    Used by the resource-server middleware (``auth.py``) when
    ``MNEMON_AS_ENABLED=true`` to validate bearer tokens without any
    network hop — keys come from the local filesystem, issuer/audience
    from the local config. This is the read-path counterpart to
    ``mint_access_token``; the two must agree on iss/aud/alg.

    Raises:
        ValueError: on any validation failure (expired, wrong issuer,
            wrong audience, bad signature, missing required claim).
            The message is safe to surface in error responses — it
            describes the class of failure, not the token internals.
    """
    import jwt

    jwk_dict = public_key_jwk(config.key_dir)
    # PyJWK accepts the JWK directly; no PyJWKClient round-trip needed.
    signing_key = jwt.PyJWK(jwk_dict)

    expected_aud = f"{config.issuer}/mcp"
    try:
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[JWT_ALG],
            audience=expected_aud,
            issuer=config.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise ValueError("token expired") from e
    except jwt.InvalidAudienceError as e:
        raise ValueError(f"audience mismatch (expected {expected_aud})") from e
    except jwt.InvalidIssuerError as e:
        raise ValueError(f"issuer mismatch (expected {config.issuer})") from e
    except jwt.InvalidTokenError as e:
        raise ValueError(f"invalid token: {e}") from e


def _issue_token_pair(
    config: AuthorizationServerConfig,
    *,
    subject: str,
    scope: str,
    client_id: str,
) -> dict[str, Any]:
    """Mint an access token + refresh token and persist the refresh token
    so it can be exchanged later via the refresh_token grant."""
    access_token = mint_access_token(config, subject=subject, scope=scope)
    refresh_token = _new_random_token()
    tokens = _load_refresh_tokens(config)
    tokens[refresh_token] = {
        "subject": subject,
        "scope": scope,
        "client_id": client_id,
        "expires_at": _now() + _REFRESH_TOKEN_TTL_SEC,
    }
    _save_refresh_tokens(config, tokens)
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": _ACCESS_TOKEN_TTL_SEC,
        "refresh_token": refresh_token,
        "scope": scope,
    }


# ── Request helpers ─────────────────────────────────────────────────────────


async def _read_body(receive) -> bytes:
    """Read the full request body from an ASGI receive callable."""
    body = b""
    more_body = True
    while more_body:
        message = await receive()
        if message["type"] == "http.request":
            body += message.get("body", b"")
            more_body = message.get("more_body", False)
        else:
            break
    return body


def _parse_query_or_form(raw: bytes) -> dict[str, str]:
    """Parse URL-encoded form or query-string bytes to a dict. Last
    value wins for repeated keys (matches stdlib parse_qs behavior)."""
    from urllib.parse import parse_qs

    parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {k: v[-1] for k, v in parsed.items()}


def _scope_query(scope: dict[str, Any]) -> dict[str, str]:
    qs = scope.get("query_string", b"")
    return _parse_query_or_form(qs)


# ── Login form ──────────────────────────────────────────────────────────────


_LOGIN_FORM_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>mnemon — sign in</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 400px; margin: 5em auto; padding: 0 1em; }}
h1 {{ font-size: 1.2em; }}
.error {{ color: #b00020; margin: 1em 0; }}
input[type=password] {{ width: 100%; padding: 0.5em; font-size: 1em; box-sizing: border-box; }}
button {{ margin-top: 1em; padding: 0.5em 1em; font-size: 1em; }}
.meta {{ color: #666; font-size: 0.9em; margin-top: 2em; }}
</style>
</head>
<body>
<h1>mnemon — sign in</h1>
{error_html}
<form method="post" action="/oauth/authorize">
{hidden_inputs}
<label for="passphrase">Passphrase</label>
<input id="passphrase" type="password" name="passphrase" autofocus required>
<button type="submit">Sign in</button>
</form>
<p class="meta">Signing in as client <code>{client_id}</code>.</p>
</body>
</html>
"""


def _render_login_form(params: dict[str, str], error: str | None = None) -> bytes:
    """Render the passphrase login page with the original authorize params
    as hidden inputs so POST can round-trip them.

    All values HTML-escaped — these are untrusted client inputs (state,
    redirect_uri) and even one unescaped field would be an XSS vector."""
    import html

    hidden = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in params.items()
    )
    error_html = (
        f'<p class="error">{html.escape(error)}</p>' if error else ""
    )
    client_id = html.escape(params.get("client_id", "(unknown)"))
    body = _LOGIN_FORM_HTML.format(
        hidden_inputs=hidden,
        error_html=error_html,
        client_id=client_id,
    ).encode("utf-8")
    return body


async def _send_html(
    send,
    status: int,
    body: bytes,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    headers = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": headers,
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _redirect(send, location: str) -> None:
    await send({
        "type": "http.response.start",
        "status": 302,
        "headers": [
            (b"location", location.encode("utf-8")),
            (b"content-length", b"0"),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": b"", "more_body": False})


# ── /oauth/authorize ────────────────────────────────────────────────────────


_REQUIRED_AUTHORIZE_PARAMS = (
    "client_id", "redirect_uri", "response_type",
    "code_challenge", "code_challenge_method",
)


def _validate_authorize_params(params: dict[str, str]) -> str | None:
    """Return an error message if the authorize params are malformed,
    else None. Intentionally strict — we reject anything not matching
    OAuth 2.1 + PKCE S256 up front rather than silently falling back."""
    missing = [p for p in _REQUIRED_AUTHORIZE_PARAMS if not params.get(p)]
    if missing:
        return f"missing required parameter(s): {', '.join(missing)}"
    if params["response_type"] != "code":
        return "only response_type=code is supported (OAuth 2.1)"
    if params["code_challenge_method"] != "S256":
        return "only code_challenge_method=S256 is supported (OAuth 2.1 requires PKCE)"
    return None


def _validate_registered_client(
    config: AuthorizationServerConfig, client_id: str, redirect_uri: str
) -> str | None:
    """Return an error message if ``client_id`` is unknown or the given
    ``redirect_uri`` is not one this client registered with.

    Unknown-client rejection prevents a stranger from initiating an auth
    flow with a made-up client_id. Redirect-URI pinning prevents an
    attacker from hijacking a legitimate client_id and redirecting the
    auth code to a URI the client's owner never registered.
    """
    client = get_client(config, client_id)
    if client is None:
        return (
            "unknown client_id; clients must register via /oauth/register "
            "before starting an authorize flow"
        )
    registered_uris = client.get("redirect_uris", [])
    if redirect_uri not in registered_uris:
        return (
            f"redirect_uri not registered for this client; registered: "
            f"{registered_uris}"
        )
    return None


async def serve_authorize(
    config: AuthorizationServerConfig, scope: dict, receive, send
) -> None:
    """ASGI handler: GET serves the login form, POST validates the
    passphrase and issues an authorization code via redirect."""
    from .auth import _send_json

    if not config.enabled:
        await _send_json(send, 404, {"error": "authorization server not enabled"})
        return

    method = scope.get("method", "GET").upper()

    if method == "GET":
        params = _scope_query(scope)
        err = _validate_authorize_params(params)
        if err:
            await _send_json(send, 400, {"error": "invalid_request", "error_description": err})
            return
        client_err = _validate_registered_client(
            config, params["client_id"], params["redirect_uri"]
        )
        if client_err:
            await _send_json(send, 400, {
                "error": "invalid_request", "error_description": client_err,
            })
            return
        await _send_html(send, 200, _render_login_form(params))
        return

    if method != "POST":
        await _send_json(send, 405, {"error": "method_not_allowed"})
        return

    # POST — login attempt
    body = await _read_body(receive)
    params = _parse_query_or_form(body)
    err = _validate_authorize_params(params)
    if err:
        await _send_json(send, 400, {"error": "invalid_request", "error_description": err})
        return
    client_err = _validate_registered_client(
        config, params["client_id"], params["redirect_uri"]
    )
    if client_err:
        await _send_json(send, 400, {
            "error": "invalid_request", "error_description": client_err,
        })
        return

    # Rate-limit check before examining the passphrase — a brute-forcer
    # must not be able to drive CPU into constant-time comparison while
    # over the failure cap.
    client_ip = _client_ip_from_scope(scope)
    retry_after = _check_rate_limit(client_ip)
    if retry_after is not None:
        from math import ceil
        headers = [(b"retry-after", str(ceil(retry_after)).encode("ascii"))]
        await _send_html(
            send, 429,
            _render_login_form(
                {k: v for k, v in params.items() if k != "passphrase"},
                error=f"Too many failed attempts. Try again in "
                      f"{ceil(retry_after)}s.",
            ),
            extra_headers=headers,
        )
        return

    passphrase = params.get("passphrase", "")
    if not _verify_passphrase(passphrase, config.passphrase or ""):
        _record_failed_attempt(client_ip)
        # Re-render form with error — don't redirect back to client with
        # an error code, because that would leak "this URL is a valid
        # OAuth init" to anyone who can read network logs. A re-rendered
        # form keeps the failure contained.
        form_params = {k: v for k, v in params.items() if k != "passphrase"}
        await _send_html(
            send, 401,
            _render_login_form(form_params, error="Invalid passphrase.")
        )
        return

    _clear_failed_attempts(client_ip)

    # Issue auth code and redirect to client's redirect_uri.
    code = _new_random_token()
    codes = _load_auth_codes(config)
    codes[code] = {
        "client_id": params["client_id"],
        "redirect_uri": params["redirect_uri"],
        "code_challenge": params["code_challenge"],
        "code_challenge_method": params["code_challenge_method"],
        "scope": params.get("scope", "mcp"),
        "subject": "owner",  # single-user AS
        "expires_at": _now() + _AUTH_CODE_TTL_SEC,
    }
    _save_auth_codes(config, codes)

    from urllib.parse import urlencode
    redirect_params = {"code": code}
    if "state" in params:
        redirect_params["state"] = params["state"]
    sep = "&" if "?" in params["redirect_uri"] else "?"
    location = f"{params['redirect_uri']}{sep}{urlencode(redirect_params)}"
    await _redirect(send, location)


def _verify_passphrase(submitted: str, configured: str) -> bool:
    """Constant-time passphrase comparison. Rejects empty configured
    values outright — prevents a misconfiguration where an unset
    passphrase accidentally matches an empty submission."""
    import hmac
    if not configured:
        return False
    return hmac.compare_digest(submitted, configured)


def _client_ip_from_scope(scope: dict) -> str:
    """Extract best-effort client IP for rate-limit keying.

    On Fly, requests come through fly-proxy and the true client IP is
    in the ``Fly-Client-IP`` header. Behind other reverse proxies,
    ``X-Forwarded-For`` is the convention (first entry = original
    client). Falls back to the ASGI scope's ``client`` tuple for local
    dev where no proxy is in front.

    Returns ``"unknown"`` if no IP can be determined — still a valid
    rate-limit key, just lumps all such requests together.
    """
    headers = {k.lower(): v for k, v in scope.get("headers", [])}
    fly_ip = headers.get(b"fly-client-ip")
    if fly_ip:
        return fly_ip.decode("latin-1", errors="replace")
    xff = headers.get(b"x-forwarded-for")
    if xff:
        return xff.decode("latin-1", errors="replace").split(",")[0].strip()
    client = scope.get("client")
    if client and isinstance(client, (list, tuple)) and client:
        return str(client[0])
    return "unknown"


def _check_rate_limit(ip: str) -> float | None:
    """Return seconds-until-retry if ``ip`` is over the failure limit,
    else None. Prunes stale timestamps inline."""
    import time
    now = time.monotonic()
    cutoff = now - _FAILED_ATTEMPT_WINDOW_SEC
    attempts = [t for t in _failed_attempts.get(ip, []) if t > cutoff]
    if not attempts:
        _failed_attempts.pop(ip, None)
        return None
    _failed_attempts[ip] = attempts
    if len(attempts) >= _MAX_FAILED_ATTEMPTS:
        # Oldest attempt in the window pins when the oldest will age out.
        return (attempts[0] + _FAILED_ATTEMPT_WINDOW_SEC) - now
    return None


def _record_failed_attempt(ip: str) -> None:
    import time
    _failed_attempts.setdefault(ip, []).append(time.monotonic())


def _clear_failed_attempts(ip: str) -> None:
    """Called on successful passphrase — legitimate users shouldn't
    carry penalty state from prior typos."""
    _failed_attempts.pop(ip, None)


def _reset_rate_limit_for_tests() -> None:
    _failed_attempts.clear()


# ── /oauth/token ────────────────────────────────────────────────────────────


async def serve_token(
    config: AuthorizationServerConfig, scope: dict, receive, send
) -> None:
    """ASGI handler: POST /oauth/token. Supports authorization_code and
    refresh_token grants."""
    from .auth import _send_json

    if not config.enabled:
        await _send_json(send, 404, {"error": "authorization server not enabled"})
        return
    if scope.get("method", "GET").upper() != "POST":
        await _send_json(send, 405, {"error": "method_not_allowed"})
        return

    body = await _read_body(receive)
    params = _parse_query_or_form(body)
    grant_type = params.get("grant_type", "")

    if grant_type == "authorization_code":
        await _token_authorization_code(config, params, send)
        return
    if grant_type == "refresh_token":
        await _token_refresh(config, params, send)
        return
    await _send_json(send, 400, {
        "error": "unsupported_grant_type",
        "error_description": (
            f"grant_type={grant_type!r} not supported; use "
            "authorization_code or refresh_token"
        ),
    })


async def _token_authorization_code(
    config: AuthorizationServerConfig, params: dict[str, str], send
) -> None:
    from .auth import _send_json

    code = params.get("code", "")
    redirect_uri = params.get("redirect_uri", "")
    client_id = params.get("client_id", "")
    code_verifier = params.get("code_verifier", "")

    for required in ("code", "redirect_uri", "client_id", "code_verifier"):
        if not params.get(required):
            await _send_json(send, 400, {
                "error": "invalid_request",
                "error_description": f"missing {required}",
            })
            return

    # One-time use — consume on lookup.
    codes = _load_auth_codes(config)
    record = codes.pop(code, None)
    if record is None:
        await _send_json(send, 400, {
            "error": "invalid_grant",
            "error_description": "unknown, expired, or already-used code",
        })
        return
    _save_auth_codes(config, codes)
    if record["expires_at"] < _now():
        await _send_json(send, 400, {
            "error": "invalid_grant",
            "error_description": "code expired",
        })
        return
    if record["client_id"] != client_id:
        await _send_json(send, 400, {
            "error": "invalid_grant",
            "error_description": "client_id mismatch",
        })
        return
    if record["redirect_uri"] != redirect_uri:
        await _send_json(send, 400, {
            "error": "invalid_grant",
            "error_description": "redirect_uri mismatch",
        })
        return
    if not _verify_pkce_s256(code_verifier, record["code_challenge"]):
        await _send_json(send, 400, {
            "error": "invalid_grant",
            "error_description": "PKCE verification failed",
        })
        return

    tokens = _issue_token_pair(
        config,
        subject=record["subject"],
        scope=record["scope"],
        client_id=client_id,
    )
    await _send_json(send, 200, tokens)


async def _token_refresh(
    config: AuthorizationServerConfig, params: dict[str, str], send
) -> None:
    from .auth import _send_json

    refresh_token = params.get("refresh_token", "")
    if not refresh_token:
        await _send_json(send, 400, {
            "error": "invalid_request",
            "error_description": "missing refresh_token",
        })
        return

    # Rotation: consume the old refresh token regardless of outcome so a
    # leaked token can only be used once. A small grace window (see
    # _REFRESH_TOKEN_GRACE_SEC) lets a retried refresh of an already-
    # rotated RT return the same new pair — without it, a network blip
    # between the token endpoint and the client (claude.ai's mcp-proxy,
    # in particular) silently bricks the connector.
    tokens = _load_refresh_tokens(config)
    record = tokens.pop(refresh_token, None)
    if record is None:
        # Not in the live set. Check the rotated-RT cache: if this RT was
        # rotated within the grace window, return the same pair we
        # issued then (idempotent retry).
        rotated = _load_rotated_tokens(config)
        cached = rotated.get(refresh_token)
        if cached is not None:
            await _send_json(send, 200, cached["response"])
            return
        await _send_json(send, 400, {
            "error": "invalid_grant",
            "error_description": "unknown, expired, or already-rotated refresh token",
        })
        return
    _save_refresh_tokens(config, tokens)
    if record["expires_at"] < _now():
        await _send_json(send, 400, {
            "error": "invalid_grant",
            "error_description": "refresh token expired",
        })
        return

    new_pair = _issue_token_pair(
        config,
        subject=record["subject"],
        scope=record["scope"],
        client_id=record["client_id"],
    )

    # Cache the response keyed by the *old* RT so a retried request with
    # that RT gets the same pair back. expires_at drives the prune in
    # _load_token_file, so the entry self-cleans after the grace window.
    rotated = _load_rotated_tokens(config)
    rotated[refresh_token] = {
        "response": new_pair,
        "expires_at": _now() + _REFRESH_TOKEN_GRACE_SEC,
    }
    _save_rotated_tokens(config, rotated)

    await _send_json(send, 200, new_pair)


# ── /oauth/register — Dynamic Client Registration (RFC 7591) ────────────────
#
# MCP clients (claude.ai web, Claude Desktop, Claude mobile) use DCR to
# self-register without a human manually creating a client config. We
# only support public clients (no client_secret) since all MCP clients
# are public — authentication happens via PKCE on the token endpoint.
#
# Clients are persisted to ``{key_dir}/clients.json`` so a server
# restart doesn't invalidate claude.ai's cached client_id. The file
# starts empty; each DCR request appends one entry.


_CLIENTS_FILENAME = "clients.json"


def _clients_path(config: AuthorizationServerConfig) -> Path:
    return config.key_dir / _CLIENTS_FILENAME


def _load_clients(config: AuthorizationServerConfig) -> dict[str, dict[str, Any]]:
    path = _clients_path(config)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "oauth_as: clients.json unreadable (%s); treating as empty. "
            "Registered clients will need to re-register.",
            e,
        )
        return {}


def _save_clients(
    config: AuthorizationServerConfig, clients: dict[str, dict[str, Any]]
) -> None:
    path = _clients_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write then atomic-rename so a crash mid-write doesn't corrupt the
    # clients file. Failing that write would force all registered
    # clients to re-register on next use.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clients, indent=2))
    tmp.replace(path)


def get_client(config: AuthorizationServerConfig, client_id: str) -> dict[str, Any] | None:
    """Look up a registered client by id. Returns None if not found.

    Used by /authorize and /token to reject unknown client_ids (which
    before DCR was implemented, were silently accepted — any string
    worked). That check lands in this PR alongside the registry.
    """
    clients = _load_clients(config)
    return clients.get(client_id)


def register_client(
    config: AuthorizationServerConfig, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Persist a new client registration and return the RFC 7591 response.

    Generates a new ``client_id``, stores the provided metadata, and
    returns the registration response (which includes the client_id the
    caller should use going forward). ``client_id_issued_at`` is set to
    the current unix timestamp per RFC 7591.

    No ``client_secret`` is returned because this AS only supports
    public clients (PKCE). This matches the
    ``token_endpoint_auth_methods_supported=["none"]`` advertised in
    the AS metadata.
    """
    client_id = _new_random_token(16)
    now = _now()
    record = {
        "client_id": client_id,
        "client_id_issued_at": now,
        "token_endpoint_auth_method": "none",  # public client
        **metadata,
    }
    clients = _load_clients(config)
    clients[client_id] = record
    _save_clients(config, clients)
    return record


def _validate_registration_metadata(metadata: dict[str, Any]) -> str | None:
    """Return an error description if the client metadata is invalid,
    else None. Strict: reject anything that would let a later /authorize
    or /token fail in a hard-to-debug way."""
    redirect_uris = metadata.get("redirect_uris")
    if not redirect_uris or not isinstance(redirect_uris, list):
        return "redirect_uris is required and must be a non-empty array"
    for uri in redirect_uris:
        if not isinstance(uri, str) or not uri:
            return "redirect_uris entries must be non-empty strings"
        # RFC 7591 doesn't mandate HTTPS, but for public clients over
        # the internet it's the only safe choice. Allow localhost for
        # local dev (http://localhost, http://127.0.0.1). Reject all
        # other http URIs — prevents claude.ai-ish clients from
        # accidentally registering non-TLS callbacks.
        if uri.startswith("http://") and not (
            uri.startswith("http://localhost")
            or uri.startswith("http://127.0.0.1")
        ):
            return f"redirect_uris must use https:// (not {uri!r})"

    grant_types = metadata.get("grant_types")
    if grant_types is not None:
        if not isinstance(grant_types, list):
            return "grant_types must be an array"
        allowed = {"authorization_code", "refresh_token"}
        unsupported = [g for g in grant_types if g not in allowed]
        if unsupported:
            return (
                f"grant_types contains unsupported values: {unsupported}. "
                f"Only {sorted(allowed)} are supported."
            )

    response_types = metadata.get("response_types")
    if response_types is not None:
        if not isinstance(response_types, list) or response_types != ["code"]:
            return "response_types must be [\"code\"] (OAuth 2.1)"

    return None


async def serve_register(
    config: AuthorizationServerConfig, scope: dict, receive, send
) -> None:
    """ASGI handler: POST /oauth/register (RFC 7591 DCR).

    Accepts client metadata as JSON, generates a new client_id, persists
    the registration, and returns the full registration response. No
    client_secret issued — public clients with PKCE only.
    """
    from .auth import _send_json

    if not config.enabled:
        await _send_json(send, 404, {"error": "authorization server not enabled"})
        return
    if scope.get("method", "GET").upper() != "POST":
        await _send_json(send, 405, {"error": "method_not_allowed"})
        return

    body = await _read_body(receive)
    try:
        metadata = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        await _send_json(send, 400, {
            "error": "invalid_client_metadata",
            "error_description": f"request body must be valid JSON: {e}",
        })
        return

    if not isinstance(metadata, dict):
        await _send_json(send, 400, {
            "error": "invalid_client_metadata",
            "error_description": "request body must be a JSON object",
        })
        return

    err = _validate_registration_metadata(metadata)
    if err:
        await _send_json(send, 400, {
            "error": "invalid_client_metadata",
            "error_description": err,
        })
        return

    try:
        registration = register_client(config, metadata)
    except OSError as e:
        logger.exception("oauth_as: failed to persist client registration")
        await _send_json(send, 500, {
            "error": "server_error",
            "error_description": f"could not persist registration: {e}",
        })
        return
    await _send_json(send, 201, registration)
