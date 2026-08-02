"""Tests for the self-hosted OAuth Authorization Server scaffolding.

Phase 2, PR #36 scope: key management, well-known metadata documents,
ASGI handlers. The endpoints themselves (/authorize, /token, /register)
are not yet implemented — tests for those land with PR #37/#38.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mnemon.oauth_as import (
    AuthorizationServerConfig,
    authorization_server_metadata,
    ensure_keypair,
    jwks_document,
    public_key_jwk,
)


# ── Config loading ──────────────────────────────────────────────────────────


class TestAuthorizationServerConfig:
    def test_defaults_to_disabled(self, monkeypatch):
        """A fresh environment with no AS vars set must produce a
        disabled config — guards against accidentally enabling the AS
        in production before the endpoints are implemented."""
        for var in ("MNEMON_AS_ENABLED", "MNEMON_AS_PASSPHRASE",
                    "MNEMON_AS_KEY_DIR", "MNEMON_PUBLIC_URL"):
            monkeypatch.delenv(var, raising=False)
        config = AuthorizationServerConfig.from_env()
        assert config.enabled is False

    def test_enabled_requires_public_url_and_passphrase(self, monkeypatch):
        monkeypatch.setenv("MNEMON_AS_ENABLED", "true")
        monkeypatch.delenv("MNEMON_PUBLIC_URL", raising=False)
        monkeypatch.delenv("MNEMON_AS_PASSPHRASE", raising=False)
        config = AuthorizationServerConfig.from_env()
        problems = config.validate()
        assert any("MNEMON_PUBLIC_URL" in p for p in problems)
        # Passphrase validation moved to validate_passphrase (config#2819)
        pp_problems = AuthorizationServerConfig.validate_passphrase(
            has_passphrase=False, passphrase_length=0
        )
        assert any("MNEMON_AS_PASSPHRASE" in p for p in pp_problems)

    def test_enabled_with_full_config_validates(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MNEMON_AS_ENABLED", "true")
        monkeypatch.setenv("MNEMON_PUBLIC_URL", "https://example.fly.dev")
        monkeypatch.setenv("MNEMON_AS_PASSPHRASE", "correct-horse-battery-staple")
        monkeypatch.setenv("MNEMON_AS_KEY_DIR", str(tmp_path))
        config = AuthorizationServerConfig.from_env()
        assert config.enabled is True
        assert config.issuer == "https://example.fly.dev"
        assert config.passphrase == "correct-horse-battery-staple"
        assert config.key_dir == tmp_path
        assert config.validate() == []

    def test_disabled_passes_validation_regardless(self):
        """When disabled, the AS has nothing to validate — don't block
        server startup because optional AS vars happen to be empty."""
        config = AuthorizationServerConfig(enabled=False)
        assert config.validate() == []

    def test_short_passphrase_rejected(self, tmp_path):
        """The passphrase is the sole credential gating the vault and the
        AS has no rate limiting. A 6-char passphrase is brute-forceable;
        require 16+ chars and point users at secrets.token_urlsafe(32)
        in the error message so they know what to do."""
        problems = AuthorizationServerConfig.validate_passphrase(
            has_passphrase=True, passphrase_length=5
        )
        assert any("too short" in p for p in problems)
        assert any("secrets.token_urlsafe" in p for p in problems)

    def test_exactly_16_char_passphrase_accepted(self):
        """Boundary: 16 chars is the minimum, not 17."""
        problems = AuthorizationServerConfig.validate_passphrase(
            has_passphrase=True, passphrase_length=16
        )
        assert problems == []

    def test_issuer_strips_trailing_slash(self):
        """Issuer claim must be exact — RFC 8414 requires no trailing
        slash in the issuer value even if the public URL has one."""
        config = AuthorizationServerConfig(
            enabled=True,
            public_url="https://example.fly.dev/",
            passphrase="x",
        )
        assert config.issuer == "https://example.fly.dev"

    def test_boolean_parsing_accepts_common_truthy_values(self, monkeypatch):
        for val in ("1", "true", "TRUE", "yes"):
            monkeypatch.setenv("MNEMON_AS_ENABLED", val)
            config = AuthorizationServerConfig.from_env()
            assert config.enabled is True, f"expected {val!r} to enable AS"

    def test_boolean_parsing_rejects_falsy_values(self, monkeypatch):
        for val in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("MNEMON_AS_ENABLED", val)
            config = AuthorizationServerConfig.from_env()
            assert config.enabled is False, f"expected {val!r} to disable AS"


# ── Key management ──────────────────────────────────────────────────────────


class TestEnsureKeypair:
    def test_generates_new_keypair_on_first_call(self, tmp_path):
        private_pem, public_pem = ensure_keypair(tmp_path)
        assert private_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
        assert public_pem.startswith(b"-----BEGIN PUBLIC KEY-----")
        assert (tmp_path / "private.pem").exists()

    def test_persists_private_key_with_0600_perms(self, tmp_path):
        ensure_keypair(tmp_path)
        mode = oct((tmp_path / "private.pem").stat().st_mode)[-3:]
        assert mode == "600", (
            "private key must be readable only by owner — leaking it "
            "lets anyone sign tokens"
        )

    def test_subsequent_calls_load_existing_key(self, tmp_path):
        """Repeated calls must be idempotent — an AS restart must not
        invalidate existing tokens by generating a fresh key."""
        first_priv, first_pub = ensure_keypair(tmp_path)
        second_priv, second_pub = ensure_keypair(tmp_path)
        assert first_priv == second_priv
        assert first_pub == second_pub

    def test_creates_missing_parent_directory(self, tmp_path):
        key_dir = tmp_path / "nonexistent" / "oauth_keys"
        ensure_keypair(key_dir)
        assert key_dir.exists()


# ── JWK / JWKS documents ────────────────────────────────────────────────────


class TestPublicKeyJwk:
    def test_includes_required_fields(self, tmp_path):
        jwk = public_key_jwk(tmp_path)
        required = {"kty", "alg", "use", "kid", "n", "e"}
        assert required.issubset(jwk.keys()), f"missing fields: {required - jwk.keys()}"

    def test_advertises_rs256(self, tmp_path):
        jwk = public_key_jwk(tmp_path)
        assert jwk["alg"] == "RS256"
        assert jwk["kty"] == "RSA"
        assert jwk["use"] == "sig"

    def test_kid_is_stable(self, tmp_path):
        """JWKS kid lets clients know which key signed a given JWT. It
        must not change across calls for the same keypair."""
        jwk_a = public_key_jwk(tmp_path)
        jwk_b = public_key_jwk(tmp_path)
        assert jwk_a["kid"] == jwk_b["kid"]


class TestJwksDocument:
    def test_wraps_public_key_in_keys_array(self, tmp_path):
        doc = jwks_document(tmp_path)
        assert "keys" in doc
        assert isinstance(doc["keys"], list)
        assert len(doc["keys"]) == 1
        assert doc["keys"][0]["kty"] == "RSA"


# ── Authorization Server metadata (RFC 8414) ────────────────────────────────


class TestAuthorizationServerMetadata:
    def test_metadata_shape(self, tmp_path):
        config = AuthorizationServerConfig(
            enabled=True,
            public_url="https://example.fly.dev",
            passphrase="x",
            key_dir=tmp_path,
        )
        meta = authorization_server_metadata(config)
        assert meta["issuer"] == "https://example.fly.dev"
        assert meta["authorization_endpoint"] == "https://example.fly.dev/oauth/authorize"
        assert meta["token_endpoint"] == "https://example.fly.dev/oauth/token"
        assert meta["registration_endpoint"] == "https://example.fly.dev/oauth/register"
        assert meta["jwks_uri"] == "https://example.fly.dev/.well-known/jwks.json"

    def test_only_supports_authorization_code_and_refresh(self, tmp_path):
        """Password grant, client_credentials, and implicit are deprecated
        and must not be advertised — OAuth 2.1 compliance."""
        config = AuthorizationServerConfig(
            enabled=True,
            public_url="https://x",
            passphrase="x",
            key_dir=tmp_path,
        )
        meta = authorization_server_metadata(config)
        assert meta["grant_types_supported"] == ["authorization_code", "refresh_token"]

    def test_only_supports_pkce_s256(self, tmp_path):
        config = AuthorizationServerConfig(
            enabled=True,
            public_url="https://x",
            passphrase="x",
            key_dir=tmp_path,
        )
        meta = authorization_server_metadata(config)
        assert meta["code_challenge_methods_supported"] == ["S256"]

    def test_public_client_only_no_client_secrets(self, tmp_path):
        """All MCP clients are public (no secure secret storage). Must
        advertise token_endpoint_auth_methods=["none"] so DCR doesn't
        hand out secrets it can't actually keep secret."""
        config = AuthorizationServerConfig(
            enabled=True,
            public_url="https://x",
            passphrase="x",
            key_dir=tmp_path,
        )
        meta = authorization_server_metadata(config)
        assert meta["token_endpoint_auth_methods_supported"] == ["none"]


# ── ASGI handlers ────────────────────────────────────────────────────────────


async def _capture_response(handler, config):
    """Invoke an ASGI send-only handler and return (status, body_json)."""
    sent = []

    async def send(msg):
        sent.append(msg)

    await handler(config, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    body_msg = next(m for m in sent if m["type"] == "http.response.body")
    body_json = json.loads(body_msg["body"].decode("utf-8"))
    return start["status"], body_json


class TestServeJwks:
    @pytest.mark.asyncio
    async def test_returns_404_when_as_disabled(self):
        from mnemon.oauth_as import serve_jwks

        config = AuthorizationServerConfig(enabled=False)
        status, body = await _capture_response(serve_jwks, config)
        assert status == 404
        assert "not enabled" in body["error"]

    @pytest.mark.asyncio
    async def test_returns_jwks_when_enabled(self, tmp_path):
        from mnemon.oauth_as import serve_jwks

        config = AuthorizationServerConfig(
            enabled=True,
            public_url="https://x",
            passphrase="x",
            key_dir=tmp_path,
        )
        status, body = await _capture_response(serve_jwks, config)
        assert status == 200
        assert "keys" in body
        assert body["keys"][0]["kty"] == "RSA"


class TestServeAsMetadata:
    @pytest.mark.asyncio
    async def test_returns_404_when_as_disabled(self):
        from mnemon.oauth_as import serve_as_metadata

        config = AuthorizationServerConfig(enabled=False)
        status, body = await _capture_response(serve_as_metadata, config)
        assert status == 404

    @pytest.mark.asyncio
    async def test_returns_metadata_when_enabled(self, tmp_path):
        from mnemon.oauth_as import serve_as_metadata

        config = AuthorizationServerConfig(
            enabled=True,
            public_url="https://example.fly.dev",
            passphrase="x",
            key_dir=tmp_path,
        )
        status, body = await _capture_response(serve_as_metadata, config)
        assert status == 200
        assert body["issuer"] == "https://example.fly.dev"


# ── /oauth/authorize ────────────────────────────────────────────────────────


import base64
import hashlib
from urllib.parse import parse_qs, urlparse


@pytest.fixture
def as_config(tmp_path):
    return AuthorizationServerConfig(
        enabled=True,
        public_url="https://example.fly.dev",
        passphrase="correct-horse-battery",
        key_dir=tmp_path,
    )


@pytest.fixture(autouse=True)
def reset_oauth_state():
    from mnemon.oauth_as import _reset_state_for_tests
    _reset_state_for_tests()
    yield
    _reset_state_for_tests()


@pytest.fixture
def registered_client(as_config):
    """Register a test client via the DCR code path so /authorize and
    /token accept its client_id. Tests that exercise unknown-client
    rejection should NOT use this fixture."""
    from mnemon.oauth_as import register_client

    return register_client(as_config, {
        "redirect_uris": ["https://client/cb", "https://client.example/cb"],
        "client_name": "Test Client",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })


def _pkce_pair():
    """Generate a PKCE (code_verifier, code_challenge) pair matching RFC 7636."""
    import secrets
    verifier = secrets.token_urlsafe(64)[:64]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def _run_asgi(handler, config, method="GET", query=b"", body=b"", headers=None):
    """Invoke an AS ASGI handler and return (status, headers_dict, body_bytes)."""
    scope = {"type": "http", "method": method, "path": "/oauth/test",
             "query_string": query, "headers": headers or []}
    sent = []

    async def send(msg):
        sent.append(msg)

    body_sent = {"done": False}

    async def receive():
        if body_sent["done"]:
            return {"type": "http.disconnect"}
        body_sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    # Handlers take different arg shapes depending on whether they need receive.
    import inspect
    sig = inspect.signature(handler)
    if "receive" in sig.parameters:
        await handler(config, scope, receive, send)
    else:
        await handler(config, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    body_msgs = [m for m in sent if m["type"] == "http.response.body"]
    body_bytes = b"".join(m.get("body", b"") for m in body_msgs)
    headers = {k.decode(): v.decode() for k, v in start.get("headers", [])}
    return start["status"], headers, body_bytes


class TestServeAuthorizeGet:
    @pytest.mark.asyncio
    async def test_404_when_as_disabled(self):
        from mnemon.oauth_as import serve_authorize

        config = AuthorizationServerConfig(enabled=False)
        status, _, _ = await _run_asgi(serve_authorize, config, method="GET")
        assert status == 404

    @pytest.mark.asyncio
    async def test_rejects_missing_params(self, as_config):
        from mnemon.oauth_as import serve_authorize

        status, _, body = await _run_asgi(
            serve_authorize, as_config, method="GET",
            query=b"client_id=x",  # everything else missing
        )
        assert status == 400
        assert b"missing required parameter" in body

    @pytest.mark.asyncio
    async def test_rejects_non_s256_pkce(self, as_config, registered_client):
        from mnemon.oauth_as import serve_authorize

        cid = registered_client["client_id"]
        query = (
            f"client_id={cid}&redirect_uri=https://client/cb&response_type=code&"
            f"code_challenge=abc&code_challenge_method=plain"
        ).encode()
        status, _, body = await _run_asgi(
            serve_authorize, as_config, method="GET", query=query
        )
        assert status == 400
        assert b"S256" in body

    @pytest.mark.asyncio
    async def test_rejects_non_code_response_type(self, as_config, registered_client):
        """OAuth 2.1 removes implicit grant — only response_type=code."""
        from mnemon.oauth_as import serve_authorize

        cid = registered_client["client_id"]
        query = (
            f"client_id={cid}&redirect_uri=https://client/cb&response_type=token&"
            f"code_challenge=abc&code_challenge_method=S256"
        ).encode()
        status, _, _ = await _run_asgi(
            serve_authorize, as_config, method="GET", query=query
        )
        assert status == 400

    @pytest.mark.asyncio
    async def test_rejects_unknown_client_id(self, as_config):
        """Unregistered client_ids must be rejected — the DCR endpoint
        is the only way to obtain a valid one."""
        from mnemon.oauth_as import serve_authorize

        _, challenge = _pkce_pair()
        query = (
            f"client_id=never-registered&redirect_uri=https://x/cb&"
            f"response_type=code&code_challenge={challenge}&"
            f"code_challenge_method=S256"
        ).encode()
        status, _, body = await _run_asgi(
            serve_authorize, as_config, method="GET", query=query
        )
        assert status == 400
        assert b"unknown client_id" in body

    @pytest.mark.asyncio
    async def test_rejects_unregistered_redirect_uri(self, as_config, registered_client):
        """Redirect URI pinning: a registered client can only use
        redirect_uris it explicitly registered."""
        from mnemon.oauth_as import serve_authorize

        cid = registered_client["client_id"]
        _, challenge = _pkce_pair()
        query = (
            f"client_id={cid}&redirect_uri=https://attacker.example/cb&"
            f"response_type=code&code_challenge={challenge}&"
            f"code_challenge_method=S256"
        ).encode()
        status, _, body = await _run_asgi(
            serve_authorize, as_config, method="GET", query=query
        )
        assert status == 400
        assert b"redirect_uri not registered" in body

    @pytest.mark.asyncio
    async def test_renders_login_form_with_valid_params(
        self, as_config, registered_client
    ):
        from mnemon.oauth_as import serve_authorize

        cid = registered_client["client_id"]
        _, challenge = _pkce_pair()
        query = (
            f"client_id={cid}&redirect_uri=https://client.example/cb&"
            f"response_type=code&code_challenge={challenge}&"
            f"code_challenge_method=S256&state=abc123"
        ).encode()
        status, headers, body = await _run_asgi(
            serve_authorize, as_config, method="GET", query=query
        )
        assert status == 200
        assert "text/html" in headers.get("content-type", "")
        # Hidden fields round-trip the authorize params back on POST
        assert b'name="client_id"' in body
        assert cid.encode() in body
        assert b'name="state"' in body
        assert b'value="abc123"' in body
        # Never echo the passphrase field as a value
        assert b'name="passphrase"' in body

    @pytest.mark.asyncio
    async def test_escapes_html_in_params(self, as_config, registered_client):
        """state and redirect_uri are untrusted — must be HTML-escaped to
        avoid XSS via the login form."""
        from mnemon.oauth_as import serve_authorize

        cid = registered_client["client_id"]
        _, challenge = _pkce_pair()
        query = (
            f"client_id={cid}&redirect_uri=https://client/cb&response_type=code&"
            f"code_challenge={challenge}&code_challenge_method=S256&"
            f"state=<img src=x>"
        ).encode()
        _, _, body = await _run_asgi(
            serve_authorize, as_config, method="GET", query=query
        )
        assert b"<img src=x>" not in body


class TestServeAuthorizePost:
    @pytest.mark.asyncio
    async def test_rejects_wrong_passphrase(self, as_config, registered_client):
        from mnemon.oauth_as import serve_authorize

        cid = registered_client["client_id"]
        _, challenge = _pkce_pair()
        form = (
            f"client_id={cid}&redirect_uri=https://client/cb&response_type=code&"
            f"code_challenge={challenge}&code_challenge_method=S256&"
            f"passphrase=wrong"
        ).encode()
        status, headers, body = await _run_asgi(
            serve_authorize, as_config, method="POST", body=form
        )
        assert status == 401
        assert "text/html" in headers.get("content-type", "")
        # Must NOT redirect back to client with a code on failure —
        # keeps the client from logging invalid-attempt URLs.
        assert "location" not in headers
        assert b"Invalid passphrase" in body

    @pytest.mark.asyncio
    async def test_empty_configured_passphrase_never_matches(self, tmp_path):
        """If AS is somehow enabled without a passphrase set, an empty
        submission must NOT match — guards the misconfig case."""
        from mnemon.oauth_as import register_client, serve_authorize

        # Directly construct config without validation to simulate boot
        # skipping the validate() call.
        config = AuthorizationServerConfig(
            enabled=True,
            public_url="https://example.fly.dev",
            passphrase="",  # misconfigured
            key_dir=tmp_path,
        )
        cid = register_client(config, {"redirect_uris": ["https://client/cb"]})["client_id"]
        _, challenge = _pkce_pair()
        form = (
            f"client_id={cid}&redirect_uri=https://client/cb&response_type=code&"
            f"code_challenge={challenge}&code_challenge_method=S256&"
            f"passphrase="
        ).encode()
        status, _, _ = await _run_asgi(
            serve_authorize, config, method="POST", body=form
        )
        assert status == 401

    @pytest.mark.asyncio
    async def test_correct_passphrase_redirects_with_code(
        self, as_config, registered_client
    ):
        from mnemon.oauth_as import _load_auth_codes, serve_authorize

        cid = registered_client["client_id"]
        _, challenge = _pkce_pair()
        form = (
            f"client_id={cid}&redirect_uri=https://client/cb&"
            f"response_type=code&code_challenge={challenge}&"
            f"code_challenge_method=S256&state=xyz&passphrase=correct-horse-battery"
        ).encode()
        status, headers, _ = await _run_asgi(
            serve_authorize, as_config, method="POST", body=form
        )
        assert status == 302
        location = headers["location"]
        parsed = urlparse(location)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://client/cb"
        qs = parse_qs(parsed.query)
        assert "code" in qs
        assert qs["state"] == ["xyz"]
        # Code persisted to auth_codes.json for later exchange
        assert qs["code"][0] in _load_auth_codes(as_config)


# ── /oauth/token ────────────────────────────────────────────────────────────


async def _issue_code(as_config, client_id, **overrides):
    """Helper: run an authorize POST with correct passphrase, return (code,
    code_verifier, params-used-for-issuance).

    Callers must pass a pre-registered ``client_id`` — unknown clients
    are rejected at the authorize step since DCR was added in PR #38.
    """
    from mnemon.oauth_as import serve_authorize

    verifier, challenge = _pkce_pair()
    params = {
        "client_id": client_id,
        "redirect_uri": "https://client/cb",
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "mcp",
        "passphrase": "correct-horse-battery",
    }
    params.update(overrides)
    from urllib.parse import urlencode
    form = urlencode(params).encode()
    _, headers, _ = await _run_asgi(
        serve_authorize, as_config, method="POST", body=form
    )
    location = headers["location"]
    parsed = urlparse(location)
    code = parse_qs(parsed.query)["code"][0]
    return code, verifier, params


class TestServeTokenAuthorizationCode:
    @pytest.mark.asyncio
    async def test_404_when_as_disabled(self):
        from mnemon.oauth_as import serve_token

        config = AuthorizationServerConfig(enabled=False)
        status, _, _ = await _run_asgi(serve_token, config, method="POST")
        assert status == 404

    @pytest.mark.asyncio
    async def test_rejects_non_post(self, as_config):
        from mnemon.oauth_as import serve_token

        status, _, _ = await _run_asgi(serve_token, as_config, method="GET")
        assert status == 405

    @pytest.mark.asyncio
    async def test_rejects_unsupported_grant_type(self, as_config):
        from mnemon.oauth_as import serve_token

        form = b"grant_type=password"
        status, _, body = await _run_asgi(
            serve_token, as_config, method="POST", body=form
        )
        assert status == 400
        assert b"unsupported_grant_type" in body

    @pytest.mark.asyncio
    async def test_happy_path_returns_tokens(self, as_config, registered_client):
        from mnemon.oauth_as import serve_token

        code, verifier, params = await _issue_code(as_config, registered_client["client_id"])
        from urllib.parse import urlencode
        form = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "client_id": params["client_id"],
            "code_verifier": verifier,
        }).encode()
        status, _, body_bytes = await _run_asgi(
            serve_token, as_config, method="POST", body=form
        )
        assert status == 200
        doc = json.loads(body_bytes)
        assert doc["token_type"] == "Bearer"
        assert doc["access_token"]
        assert doc["refresh_token"]
        assert doc["expires_in"] == 3600
        assert doc["scope"] == "mcp"

    @pytest.mark.asyncio
    async def test_access_token_verifies_against_jwks(self, as_config, registered_client):
        """Minted access token must be verifiable with the public key
        published at /.well-known/jwks.json — the contract the future
        resource-server swap (PR #39) relies on."""
        import jwt
        from mnemon.oauth_as import jwks_document, serve_token

        code, verifier, params = await _issue_code(as_config, registered_client["client_id"])
        from urllib.parse import urlencode
        form = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "client_id": params["client_id"],
            "code_verifier": verifier,
        }).encode()
        _, _, body_bytes = await _run_asgi(
            serve_token, as_config, method="POST", body=form
        )
        token = json.loads(body_bytes)["access_token"]

        jwks = jwks_document(as_config.key_dir)
        signing_key = jwt.PyJWK(jwks["keys"][0])
        payload = jwt.decode(
            token, signing_key.key, algorithms=["RS256"],
            audience=f"{as_config.issuer}/mcp",
            issuer=as_config.issuer,
        )
        assert payload["sub"] == "owner"
        assert payload["scope"] == "mcp"

    @pytest.mark.asyncio
    async def test_code_is_single_use(self, as_config, registered_client):
        from mnemon.oauth_as import serve_token

        code, verifier, params = await _issue_code(as_config, registered_client["client_id"])
        from urllib.parse import urlencode
        form = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "client_id": params["client_id"],
            "code_verifier": verifier,
        }).encode()

        # First exchange succeeds...
        status1, _, _ = await _run_asgi(serve_token, as_config, method="POST", body=form)
        assert status1 == 200
        # ...second with the same code fails.
        status2, _, body2 = await _run_asgi(serve_token, as_config, method="POST", body=form)
        assert status2 == 400
        assert b"invalid_grant" in body2

    @pytest.mark.asyncio
    async def test_wrong_code_verifier_rejected(self, as_config, registered_client):
        from mnemon.oauth_as import serve_token

        code, _, params = await _issue_code(as_config, registered_client["client_id"])
        from urllib.parse import urlencode
        form = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "client_id": params["client_id"],
            "code_verifier": "a-different-verifier-than-the-one-used",
        }).encode()
        status, _, body = await _run_asgi(
            serve_token, as_config, method="POST", body=form
        )
        assert status == 400
        assert b"PKCE verification failed" in body

    @pytest.mark.asyncio
    async def test_wrong_redirect_uri_rejected(self, as_config, registered_client):
        from mnemon.oauth_as import serve_token

        code, verifier, params = await _issue_code(as_config, registered_client["client_id"])
        from urllib.parse import urlencode
        form = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://attacker.example/cb",  # different
            "client_id": params["client_id"],
            "code_verifier": verifier,
        }).encode()
        status, _, body = await _run_asgi(
            serve_token, as_config, method="POST", body=form
        )
        assert status == 400
        assert b"redirect_uri" in body

    @pytest.mark.asyncio
    async def test_wrong_client_id_rejected(self, as_config, registered_client):
        from mnemon.oauth_as import serve_token

        code, verifier, params = await _issue_code(as_config, registered_client["client_id"])
        from urllib.parse import urlencode
        form = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "client_id": "different-client",
            "code_verifier": verifier,
        }).encode()
        status, _, body = await _run_asgi(
            serve_token, as_config, method="POST", body=form
        )
        assert status == 400
        assert b"client_id" in body

    @pytest.mark.asyncio
    async def test_unknown_code_rejected(self, as_config):
        from mnemon.oauth_as import serve_token

        form = (
            b"grant_type=authorization_code&code=nope&redirect_uri=https://x/cb&"
            b"client_id=c&code_verifier=v"
        )
        status, _, body = await _run_asgi(
            serve_token, as_config, method="POST", body=form
        )
        assert status == 400
        assert b"invalid_grant" in body


class TestServeTokenRefreshGrant:
    @pytest.mark.asyncio
    async def test_refresh_token_rotates(self, as_config, registered_client):
        """Rotating refresh tokens means each refresh consumes the old
        one and issues a new one. A leaked refresh token only works once."""
        from mnemon.oauth_as import serve_token

        # Get an initial pair.
        code, verifier, params = await _issue_code(as_config, registered_client["client_id"])
        from urllib.parse import urlencode
        code_form = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "client_id": params["client_id"],
            "code_verifier": verifier,
        }).encode()
        _, _, body1 = await _run_asgi(serve_token, as_config, method="POST", body=code_form)
        first = json.loads(body1)

        # Refresh → new pair.
        refresh_form = urlencode({
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
        }).encode()
        status, _, body2 = await _run_asgi(
            serve_token, as_config, method="POST", body=refresh_form
        )
        assert status == 200
        second = json.loads(body2)
        assert second["access_token"] != first["access_token"]
        assert second["refresh_token"] != first["refresh_token"]

        # Old refresh token still works *within the grace window*, but
        # returns the same new pair (idempotent retry support — see
        # _REFRESH_TOKEN_GRACE_SEC). Replay after the window is rejected;
        # that case is exercised in test_refresh_token_replay_after_grace_rejected.
        status3, _, body3 = await _run_asgi(
            serve_token, as_config, method="POST", body=refresh_form
        )
        assert status3 == 200
        third = json.loads(body3)
        assert third == second

    @pytest.mark.asyncio
    async def test_refresh_token_replay_within_grace_returns_same_pair(
        self, as_config, registered_client
    ):
        """A retried /oauth/token call presenting an already-rotated RT
        within the grace window must return the exact same response —
        same access_token, same refresh_token. This is the bug that
        spontaneously disconnected the claude.ai mnemon connector when
        a network blip caused mcp-proxy to retry a successful refresh."""
        from mnemon.oauth_as import serve_token

        code, verifier, params = await _issue_code(as_config, registered_client["client_id"])
        from urllib.parse import urlencode
        code_form = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "client_id": params["client_id"],
            "code_verifier": verifier,
        }).encode()
        _, _, body1 = await _run_asgi(serve_token, as_config, method="POST", body=code_form)
        first = json.loads(body1)

        refresh_form = urlencode({
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
        }).encode()
        _, _, body2 = await _run_asgi(serve_token, as_config, method="POST", body=refresh_form)
        rotated_pair = json.loads(body2)

        # Three more retries with the *same* old RT — all within the
        # grace window — must each return the same rotated pair.
        for _ in range(3):
            status, _, body = await _run_asgi(
                serve_token, as_config, method="POST", body=refresh_form
            )
            assert status == 200
            assert json.loads(body) == rotated_pair

    @pytest.mark.asyncio
    async def test_refresh_token_replay_after_grace_rejected(
        self, as_config, registered_client, monkeypatch
    ):
        """Replay of an already-rotated RT *after* the grace window
        expires must fail with invalid_grant — the grace window is for
        idempotent retries, not indefinite token reuse."""
        import mnemon.oauth_as as oauth_as
        from mnemon.oauth_as import serve_token

        code, verifier, params = await _issue_code(as_config, registered_client["client_id"])
        from urllib.parse import urlencode
        code_form = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "client_id": params["client_id"],
            "code_verifier": verifier,
        }).encode()
        _, _, body1 = await _run_asgi(serve_token, as_config, method="POST", body=code_form)
        first = json.loads(body1)

        refresh_form = urlencode({
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
        }).encode()
        await _run_asgi(serve_token, as_config, method="POST", body=refresh_form)

        # Advance the clock past the grace window. The cached entry's
        # expires_at < now means _load_token_file's prune drops it on
        # next read, so the replay falls through to invalid_grant.
        real_now = oauth_as._now
        monkeypatch.setattr(
            oauth_as, "_now", lambda: real_now() + oauth_as._REFRESH_TOKEN_GRACE_SEC + 1
        )
        status, _, body = await _run_asgi(
            serve_token, as_config, method="POST", body=refresh_form
        )
        assert status == 400
        assert b"invalid_grant" in body

    @pytest.mark.asyncio
    async def test_rotation_grace_persists_across_restart(
        self, as_config, registered_client
    ):
        """The rotation cache lives in rotated_tokens.json on the same
        volume as refresh_tokens.json, so a Fly cold-start between the
        original rotation and the retry must still serve the cached
        pair. Without persistence, a wake-on-request retry would brick
        the connector — exactly the regression we're guarding against."""
        from mnemon.oauth_as import (
            serve_token,
            _load_rotated_tokens,
            _save_rotated_tokens,
        )

        code, verifier, params = await _issue_code(as_config, registered_client["client_id"])
        from urllib.parse import urlencode
        code_form = urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "client_id": params["client_id"],
            "code_verifier": verifier,
        }).encode()
        _, _, body1 = await _run_asgi(serve_token, as_config, method="POST", body=code_form)
        first = json.loads(body1)

        refresh_form = urlencode({
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
        }).encode()
        _, _, body2 = await _run_asgi(serve_token, as_config, method="POST", body=refresh_form)
        rotated_pair = json.loads(body2)

        # Simulate a process restart by round-tripping the rotated cache
        # through the on-disk file. If persistence is broken, the retry
        # below will fail with invalid_grant.
        cache = _load_rotated_tokens(as_config)
        assert first["refresh_token"] in cache
        _save_rotated_tokens(as_config, cache)

        status, _, body = await _run_asgi(
            serve_token, as_config, method="POST", body=refresh_form
        )
        assert status == 200
        assert json.loads(body) == rotated_pair

    @pytest.mark.asyncio
    async def test_unknown_refresh_token_rejected(self, as_config):
        from mnemon.oauth_as import serve_token

        form = b"grant_type=refresh_token&refresh_token=nope"
        status, _, body = await _run_asgi(
            serve_token, as_config, method="POST", body=form
        )
        assert status == 400
        assert b"invalid_grant" in body

    @pytest.mark.asyncio
    async def test_missing_refresh_token_rejected(self, as_config):
        from mnemon.oauth_as import serve_token

        form = b"grant_type=refresh_token"
        status, _, body = await _run_asgi(
            serve_token, as_config, method="POST", body=form
        )
        assert status == 400
        assert b"invalid_request" in body


class TestPKCEVerification:
    def test_correct_verifier_validates(self):
        from mnemon.oauth_as import _verify_pkce_s256
        verifier, challenge = _pkce_pair()
        assert _verify_pkce_s256(verifier, challenge) is True

    def test_wrong_verifier_fails(self):
        from mnemon.oauth_as import _verify_pkce_s256
        _, challenge = _pkce_pair()
        assert _verify_pkce_s256("not-the-verifier", challenge) is False


class TestMintAccessToken:
    def test_token_decodes_to_expected_claims(self, as_config):
        import jwt
        from mnemon.oauth_as import mint_access_token, public_key_jwk

        token = mint_access_token(
            as_config, subject="owner", scope="mcp", ttl_sec=60
        )
        jwk = public_key_jwk(as_config.key_dir)
        signing_key = jwt.PyJWK(jwk)
        payload = jwt.decode(
            token, signing_key.key, algorithms=["RS256"],
            audience=f"{as_config.issuer}/mcp",
            issuer=as_config.issuer,
        )
        assert payload["sub"] == "owner"
        assert payload["scope"] == "mcp"
        assert payload["iss"] == as_config.issuer
        assert payload["aud"] == f"{as_config.issuer}/mcp"

    def test_token_header_includes_kid(self, as_config):
        import jwt
        from mnemon.oauth_as import mint_access_token

        token = mint_access_token(as_config, subject="owner", scope="mcp")
        header = jwt.get_unverified_header(token)
        assert header["kid"] == "mnemon-as-1"
        assert header["alg"] == "RS256"


# ── /oauth/register (RFC 7591 DCR) ───────────────────────────────────────────


class TestServeRegister:
    @pytest.mark.asyncio
    async def test_404_when_as_disabled(self):
        from mnemon.oauth_as import serve_register

        config = AuthorizationServerConfig(enabled=False)
        body = json.dumps({"redirect_uris": ["https://client/cb"]}).encode()
        status, _, _ = await _run_asgi(
            serve_register, config, method="POST", body=body
        )
        assert status == 404

    @pytest.mark.asyncio
    async def test_rejects_non_post(self, as_config):
        from mnemon.oauth_as import serve_register

        status, _, _ = await _run_asgi(serve_register, as_config, method="GET")
        assert status == 405

    @pytest.mark.asyncio
    async def test_rejects_non_json_body(self, as_config):
        from mnemon.oauth_as import serve_register

        status, _, body = await _run_asgi(
            serve_register, as_config, method="POST",
            body=b"not json at all",
        )
        assert status == 400
        assert b"invalid_client_metadata" in body

    @pytest.mark.asyncio
    async def test_rejects_non_object_json(self, as_config):
        from mnemon.oauth_as import serve_register

        status, _, body = await _run_asgi(
            serve_register, as_config, method="POST",
            body=b'["not", "an", "object"]',
        )
        assert status == 400
        assert b"JSON object" in body

    @pytest.mark.asyncio
    async def test_rejects_missing_redirect_uris(self, as_config):
        from mnemon.oauth_as import serve_register

        body = json.dumps({"client_name": "X"}).encode()
        status, _, body_bytes = await _run_asgi(
            serve_register, as_config, method="POST", body=body,
        )
        assert status == 400
        assert b"redirect_uris is required" in body_bytes

    @pytest.mark.asyncio
    async def test_rejects_non_https_redirect(self, as_config):
        from mnemon.oauth_as import serve_register

        body = json.dumps({"redirect_uris": ["http://evil.example/cb"]}).encode()
        status, _, body_bytes = await _run_asgi(
            serve_register, as_config, method="POST", body=body,
        )
        assert status == 400
        assert b"https" in body_bytes

    @pytest.mark.asyncio
    async def test_allows_localhost_http(self, as_config):
        """Local development callbacks (http://localhost) are exempt from
        the HTTPS requirement — matches RFC 8252 guidance for native apps."""
        from mnemon.oauth_as import serve_register

        body = json.dumps({
            "redirect_uris": ["http://localhost:8080/cb", "http://127.0.0.1/cb"],
        }).encode()
        status, _, _ = await _run_asgi(
            serve_register, as_config, method="POST", body=body,
        )
        assert status == 201

    @pytest.mark.asyncio
    async def test_rejects_unsupported_grant_type(self, as_config):
        from mnemon.oauth_as import serve_register

        body = json.dumps({
            "redirect_uris": ["https://client/cb"],
            "grant_types": ["password"],
        }).encode()
        status, _, body_bytes = await _run_asgi(
            serve_register, as_config, method="POST", body=body,
        )
        assert status == 400
        assert b"password" in body_bytes

    @pytest.mark.asyncio
    async def test_happy_path_returns_client_id(self, as_config):
        from mnemon.oauth_as import serve_register

        body = json.dumps({
            "redirect_uris": ["https://client.example/cb"],
            "client_name": "Test",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }).encode()
        status, headers, body_bytes = await _run_asgi(
            serve_register, as_config, method="POST", body=body,
        )
        assert status == 201
        doc = json.loads(body_bytes)
        # RFC 7591 mandates a client_id in the response.
        assert "client_id" in doc
        assert doc["client_id"]
        # Public client — no secret returned.
        assert "client_secret" not in doc
        assert doc["token_endpoint_auth_method"] == "none"
        assert doc["client_id_issued_at"] > 0
        # Echoes the submitted metadata.
        assert doc["redirect_uris"] == ["https://client.example/cb"]
        assert doc["client_name"] == "Test"

    @pytest.mark.asyncio
    async def test_registered_client_persists_across_lookups(self, as_config):
        """After DCR, the client_id must be usable by the authorize
        endpoint — i.e., get_client() can find it on subsequent calls."""
        from mnemon.oauth_as import get_client, serve_register

        body = json.dumps({"redirect_uris": ["https://a/cb"]}).encode()
        _, _, body_bytes = await _run_asgi(
            serve_register, as_config, method="POST", body=body,
        )
        client_id = json.loads(body_bytes)["client_id"]
        record = get_client(as_config, client_id)
        assert record is not None
        assert record["redirect_uris"] == ["https://a/cb"]

    @pytest.mark.asyncio
    async def test_each_registration_yields_unique_client_id(self, as_config):
        from mnemon.oauth_as import serve_register

        body = json.dumps({"redirect_uris": ["https://a/cb"]}).encode()
        _, _, b1 = await _run_asgi(serve_register, as_config, method="POST", body=body)
        _, _, b2 = await _run_asgi(serve_register, as_config, method="POST", body=body)
        id1 = json.loads(b1)["client_id"]
        id2 = json.loads(b2)["client_id"]
        assert id1 != id2

    @pytest.mark.asyncio
    async def test_clients_persist_across_load(self, as_config):
        """Persistence sanity: write via DCR, read via _load_clients
        directly — proves clients.json is the durable store, not just
        the in-process dict."""
        from mnemon.oauth_as import _load_clients, serve_register

        body = json.dumps({"redirect_uris": ["https://a/cb"]}).encode()
        _, _, body_bytes = await _run_asgi(
            serve_register, as_config, method="POST", body=body,
        )
        client_id = json.loads(body_bytes)["client_id"]

        # Fresh load from disk — simulates a server restart.
        clients = _load_clients(as_config)
        assert client_id in clients


# ── Middleware routing (sanity that /oauth/register goes to the handler) ────


class TestRegisterRouting:
    @pytest.mark.asyncio
    async def test_middleware_dispatches_register_when_as_enabled(
        self, as_config
    ):
        """End-to-end sanity: the OAuth middleware actually routes
        /oauth/register to the DCR handler when as_config is wired."""
        from mnemon.auth import OAuthConfig, OAuthMiddleware

        async def downstream(scope, receive, send):
            raise AssertionError("downstream must not be called for /oauth/register")

        mw = OAuthMiddleware(downstream, OAuthConfig(), as_config=as_config)

        sent = []

        async def send(msg):
            sent.append(msg)

        body = json.dumps({"redirect_uris": ["https://c/cb"]}).encode()
        body_done = {"done": False}

        async def receive():
            if body_done["done"]:
                return {"type": "http.disconnect"}
            body_done["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http", "method": "POST", "path": "/oauth/register",
            "query_string": b"", "headers": [],
        }
        await mw(scope, receive, send)

        status = next(m for m in sent if m["type"] == "http.response.start")["status"]
        assert status == 201


# ── verify_self_hosted_token (resource-server side) ─────────────────────────


class TestVerifySelfHostedToken:
    def test_accepts_token_minted_by_this_as(self, as_config):
        from mnemon.oauth_as import mint_access_token, verify_self_hosted_token

        token = mint_access_token(as_config, subject="owner", scope="mcp")
        claims = verify_self_hosted_token(as_config, token)
        assert claims["sub"] == "owner"
        assert claims["scope"] == "mcp"
        assert claims["iss"] == as_config.issuer
        assert claims["aud"] == f"{as_config.issuer}/mcp"

    def test_rejects_expired_token(self, as_config):
        from mnemon.oauth_as import mint_access_token, verify_self_hosted_token

        token = mint_access_token(
            as_config, subject="owner", scope="mcp", ttl_sec=-60
        )
        with pytest.raises(ValueError, match="expired"):
            verify_self_hosted_token(as_config, token)

    def test_rejects_wrong_audience(self, as_config):
        from mnemon.oauth_as import mint_access_token, verify_self_hosted_token

        token = mint_access_token(
            as_config, subject="owner", scope="mcp",
            audience="https://some-other-resource.example",
        )
        with pytest.raises(ValueError, match="audience"):
            verify_self_hosted_token(as_config, token)

    def test_rejects_token_signed_by_different_key(self, as_config, tmp_path):
        """A token minted against a different keypair must fail — the
        resource server must not accept tokens from any RS256 issuer,
        only ours."""
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from mnemon.oauth_as import verify_self_hosted_token

        foreign_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        foreign_pem = foreign_priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        import time as _time
        payload = {
            "iss": as_config.issuer,
            "aud": f"{as_config.issuer}/mcp",
            "sub": "owner",
            "iat": int(_time.time()),
            "exp": int(_time.time()) + 60,
            "scope": "mcp",
        }
        fake_token = jwt.encode(payload, foreign_pem, algorithm="RS256")
        with pytest.raises(ValueError):
            verify_self_hosted_token(as_config, fake_token)

    def test_rejects_token_with_wrong_issuer(self, as_config):
        """Token signed by our key but claiming a different issuer —
        the jwt library must catch the iss mismatch."""
        import jwt
        from mnemon.oauth_as import ensure_keypair, verify_self_hosted_token

        private_pem, _ = ensure_keypair(as_config.key_dir)
        import time as _time
        payload = {
            "iss": "https://someone-else.example",
            "aud": f"{as_config.issuer}/mcp",
            "sub": "owner",
            "iat": int(_time.time()),
            "exp": int(_time.time()) + 60,
            "scope": "mcp",
        }
        token = jwt.encode(payload, private_pem, algorithm="RS256")
        with pytest.raises(ValueError, match="issuer"):
            verify_self_hosted_token(as_config, token)

    def test_rejects_malformed_token(self, as_config):
        from mnemon.oauth_as import verify_self_hosted_token

        with pytest.raises(ValueError):
            verify_self_hosted_token(as_config, "not.a.jwt")

    def test_requires_sub_claim(self, as_config):
        """All minted tokens include sub=owner. A token missing sub —
        maliciously or from a bug — must be rejected, not silently
        forwarded as anonymous."""
        import jwt
        from mnemon.oauth_as import ensure_keypair, verify_self_hosted_token

        private_pem, _ = ensure_keypair(as_config.key_dir)
        import time as _time
        payload = {
            "iss": as_config.issuer,
            "aud": f"{as_config.issuer}/mcp",
            "iat": int(_time.time()),
            "exp": int(_time.time()) + 60,
            # sub intentionally omitted
        }
        token = jwt.encode(payload, private_pem, algorithm="RS256")
        with pytest.raises(ValueError):
            verify_self_hosted_token(as_config, token)


# ── Volume-backed persistence of auth codes + refresh tokens ────────────────


class TestStatePersistence:
    """Auth codes and refresh tokens must survive process restarts so
    Fly autostop wake doesn't force every claude.ai client to re-enter
    the passphrase. File format: JSON at ``{key_dir}/auth_codes.json``
    and ``{key_dir}/refresh_tokens.json``, mode 0600."""

    @pytest.mark.asyncio
    async def test_refresh_token_survives_simulated_restart(
        self, as_config, registered_client
    ):
        """Issue a refresh token, simulate a restart (drop in-memory
        state by reimporting the module), then redeem via /token —
        must succeed because the token was persisted to disk."""
        import importlib
        import mnemon.oauth_as as as_mod

        issued = as_mod._issue_token_pair(
            as_config,
            subject="owner",
            scope="mcp",
            client_id=registered_client["client_id"],
        )
        refresh_token = issued["refresh_token"]

        # Simulate restart: re-import the module so any module-level
        # caches are dropped. The on-disk refresh_tokens.json must be
        # the sole source of truth.
        as_mod = importlib.reload(as_mod)

        body = (
            f"grant_type=refresh_token&refresh_token={refresh_token}"
        ).encode()
        status, _, resp_body = await _run_asgi(
            as_mod.serve_token, as_config, method="POST", body=body
        )
        assert status == 200, resp_body
        resp = json.loads(resp_body)
        assert "access_token" in resp
        assert "refresh_token" in resp
        # Rotation: old token must no longer be redeemable.
        assert resp["refresh_token"] != refresh_token

    def test_refresh_tokens_file_is_mode_0600(
        self, as_config, registered_client
    ):
        """Refresh tokens are bearer-equivalent secrets; file perms must
        match the AS private key (0600)."""
        from mnemon.oauth_as import _issue_token_pair, _refresh_tokens_path

        _issue_token_pair(
            as_config, subject="owner", scope="mcp",
            client_id=registered_client["client_id"],
        )
        path = _refresh_tokens_path(as_config)
        assert path.exists()
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_expired_refresh_tokens_pruned_on_load(self, as_config):
        """Entries past their expires_at must be dropped on load so the
        file doesn't grow forever."""
        from mnemon.oauth_as import (
            _load_refresh_tokens,
            _refresh_tokens_path,
            _save_refresh_tokens,
            _now,
        )

        _save_refresh_tokens(as_config, {
            "fresh": {"subject": "owner", "scope": "mcp",
                      "client_id": "c", "expires_at": _now() + 3600},
            "stale": {"subject": "owner", "scope": "mcp",
                      "client_id": "c", "expires_at": _now() - 1},
        })
        loaded = _load_refresh_tokens(as_config)
        assert "fresh" in loaded
        assert "stale" not in loaded

    def test_corrupt_refresh_tokens_file_treated_as_empty(self, as_config):
        """Unreadable JSON must not crash the server — affected clients
        just re-auth, which is the same fallback as losing the dict."""
        from mnemon.oauth_as import _load_refresh_tokens, _refresh_tokens_path

        _refresh_tokens_path(as_config).write_text("{{not valid json")
        assert _load_refresh_tokens(as_config) == {}

    @pytest.mark.asyncio
    async def test_auth_code_survives_simulated_restart(
        self, as_config, registered_client
    ):
        """Issue an auth code via /authorize, simulate a restart, then
        redeem at /token — must succeed. Covers the edge case where the
        machine idles between authorize and token steps of an OAuth
        flow."""
        from mnemon.oauth_as import (
            _load_auth_codes,
            serve_authorize,
            serve_token,
        )

        cid = registered_client["client_id"]
        verifier, challenge = _pkce_pair()
        form = (
            f"client_id={cid}&redirect_uri=https://client/cb&"
            f"response_type=code&code_challenge={challenge}&"
            f"code_challenge_method=S256&passphrase=correct-horse-battery"
        ).encode()
        status, headers, _ = await _run_asgi(
            serve_authorize, as_config, method="POST", body=form
        )
        assert status == 302
        code = parse_qs(urlparse(headers["location"]).query)["code"][0]

        # Confirm code persisted to disk, not just a dict.
        assert code in _load_auth_codes(as_config)

        # Simulate restart: in-memory state gone, but file survives.
        token_body = (
            f"grant_type=authorization_code&code={code}&"
            f"redirect_uri=https://client/cb&client_id={cid}&"
            f"code_verifier={verifier}"
        ).encode()
        status, _, resp_body = await _run_asgi(
            serve_token, as_config, method="POST", body=token_body
        )
        assert status == 200, resp_body
        resp = json.loads(resp_body)
        assert "access_token" in resp

    def test_auth_codes_file_is_mode_0600(self, as_config):
        """Auth codes are short-lived but still authorization-grant
        material; match private-key perms."""
        from mnemon.oauth_as import _auth_codes_path, _save_auth_codes, _now

        _save_auth_codes(as_config, {
            "c": {"client_id": "x", "redirect_uri": "https://x/cb",
                  "code_challenge": "x", "code_challenge_method": "S256",
                  "scope": "mcp", "subject": "owner",
                  "expires_at": _now() + 60},
        })
        mode = _auth_codes_path(as_config).stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"


# ── Rate limiting on /oauth/authorize ───────────────────────────────────────


class TestAuthorizeRateLimit:
    """Per-IP rate limit on failed passphrase attempts.

    The passphrase is single-user and has no second factor, so a bad
    actor who discovers a deployment URL can brute-force offline-infeasibly-
    slowly against a wide-open endpoint. 10 failures per 5 min per IP
    is generous for a legit user fat-fingering while making automated
    guessing infeasible without a distributed botnet."""

    @pytest.fixture(autouse=True)
    def reset_rate_limit(self):
        from mnemon.oauth_as import _reset_rate_limit_for_tests
        _reset_rate_limit_for_tests()
        yield
        _reset_rate_limit_for_tests()

    def _bad_passphrase_form(self, cid, challenge):
        return (
            f"client_id={cid}&redirect_uri=https://client/cb&"
            f"response_type=code&code_challenge={challenge}&"
            f"code_challenge_method=S256&passphrase=wrong"
        ).encode()

    def _ip_headers(self, ip):
        return [(b"fly-client-ip", ip.encode("ascii"))]

    @pytest.mark.asyncio
    async def test_blocks_after_10_failures_from_same_ip(
        self, as_config, registered_client
    ):
        from mnemon.oauth_as import serve_authorize

        cid = registered_client["client_id"]
        _, challenge = _pkce_pair()
        form = self._bad_passphrase_form(cid, challenge)
        headers = self._ip_headers("198.51.100.1")

        # First 10 return 401 (wrong passphrase, under the cap).
        for _ in range(10):
            status, _, _ = await _run_asgi(
                serve_authorize, as_config,
                method="POST", body=form, headers=headers,
            )
            assert status == 401

        # 11th attempt from same IP hits the cap.
        status, resp_headers, body = await _run_asgi(
            serve_authorize, as_config,
            method="POST", body=form, headers=headers,
        )
        assert status == 429
        assert "retry-after" in resp_headers
        # Retry-After is a non-negative integer count of seconds.
        assert resp_headers["retry-after"].isdigit()
        assert b"Too many failed attempts" in body

    @pytest.mark.asyncio
    async def test_different_ips_tracked_independently(
        self, as_config, registered_client
    ):
        """One bad actor must not lock out a legit user on a different IP."""
        from mnemon.oauth_as import serve_authorize

        cid = registered_client["client_id"]
        _, challenge = _pkce_pair()
        form = self._bad_passphrase_form(cid, challenge)

        # Exhaust IP A.
        for _ in range(10):
            await _run_asgi(
                serve_authorize, as_config, method="POST",
                body=form, headers=self._ip_headers("198.51.100.1"),
            )
        status, _, _ = await _run_asgi(
            serve_authorize, as_config, method="POST",
            body=form, headers=self._ip_headers("198.51.100.1"),
        )
        assert status == 429

        # IP B still gets a normal 401 (wrong passphrase).
        status, _, _ = await _run_asgi(
            serve_authorize, as_config, method="POST",
            body=form, headers=self._ip_headers("198.51.100.2"),
        )
        assert status == 401

    @pytest.mark.asyncio
    async def test_successful_login_clears_penalty(
        self, as_config, registered_client
    ):
        """A legit user who fat-fingered a few times and then got it right
        should not still be counting toward the cap — otherwise the next
        day they're penalized for something they've already fixed."""
        from mnemon.oauth_as import serve_authorize, _failed_attempts

        cid = registered_client["client_id"]
        _, challenge = _pkce_pair()
        ip = "198.51.100.5"
        headers = self._ip_headers(ip)

        # Two wrong attempts.
        for _ in range(2):
            await _run_asgi(
                serve_authorize, as_config, method="POST",
                body=self._bad_passphrase_form(cid, challenge),
                headers=headers,
            )
        assert len(_failed_attempts.get(ip, [])) == 2

        # One correct attempt clears the counter.
        good_form = (
            f"client_id={cid}&redirect_uri=https://client/cb&"
            f"response_type=code&code_challenge={challenge}&"
            f"code_challenge_method=S256&passphrase=correct-horse-battery"
        ).encode()
        status, _, _ = await _run_asgi(
            serve_authorize, as_config, method="POST",
            body=good_form, headers=headers,
        )
        assert status == 302
        assert ip not in _failed_attempts

    @pytest.mark.asyncio
    async def test_old_attempts_age_out(self, as_config, registered_client):
        """After the window elapses, prior failures don't count any more."""
        from mnemon.oauth_as import (
            serve_authorize,
            _failed_attempts,
            _FAILED_ATTEMPT_WINDOW_SEC,
        )

        cid = registered_client["client_id"]
        _, challenge = _pkce_pair()
        ip = "198.51.100.9"
        headers = self._ip_headers(ip)

        # Manually seed 10 stale failures (older than the window).
        import time
        stale_ts = time.monotonic() - (_FAILED_ATTEMPT_WINDOW_SEC + 10)
        _failed_attempts[ip] = [stale_ts] * 10

        # Next attempt should succeed to 401 (wrong passphrase) not 429,
        # because the prior failures aged out of the window.
        status, _, _ = await _run_asgi(
            serve_authorize, as_config, method="POST",
            body=self._bad_passphrase_form(cid, challenge),
            headers=headers,
        )
        assert status == 401

    @pytest.mark.asyncio
    async def test_x_forwarded_for_falls_back_when_fly_header_absent(
        self, as_config, registered_client
    ):
        """Deploys not behind Fly should still get per-IP tracking via XFF."""
        from mnemon.oauth_as import serve_authorize, _failed_attempts

        cid = registered_client["client_id"]
        _, challenge = _pkce_pair()
        # XFF can hold multiple hops — first is the original client.
        headers = [(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")]

        await _run_asgi(
            serve_authorize, as_config, method="POST",
            body=self._bad_passphrase_form(cid, challenge),
            headers=headers,
        )
        assert "203.0.113.7" in _failed_attempts


