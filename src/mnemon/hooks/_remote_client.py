"""Remote MCP client helper for mnemon hooks.

Shared helper used by all three Claude Code hooks
(``context_surfacing``, ``session_extractor``, ``handoff_generator``) to
call mnemon tools on the Fly-hosted vault via Streamable HTTP instead of
touching the local SQLite store directly. This is the client-side half of
Phase 3 unification — the server-side half is the ``MNEMON_LOCAL_TOKEN``
auth path in ``src/mnemon/auth.py``.

Configuration
-------------
Both the remote URL and the local static bearer token are resolved from
environment variables first, then from files under ``~/.mnemon/``. There
is intentionally **no hardcoded default URL**: the helper must not silently
point at any specific deployment, because a forked or shared config with
an accidental default would send memory writes into someone else's vault.
If neither source is set, the helper raises :class:`RemoteClientConfigError`
with a message pointing at both options.

Resolution order (first hit wins):

1. ``MNEMON_REMOTE_URL`` env var / ``~/.mnemon/remote_url`` file
2. ``MNEMON_LOCAL_TOKEN`` env var / ``~/.mnemon/local_token`` file

The ``local_token`` file is expected to be 0600 (never world-readable);
``remote_url`` has no special permissions since the URL itself isn't
sensitive.

Behavior
--------
Each call goes through the MCP Streamable HTTP client, which opens a
short-lived session, runs the MCP ``initialize`` handshake, calls the
requested tool, and closes the session. The entire call is wrapped in a
single ``asyncio.wait_for`` with a 2-second default timeout so hooks never
block Claude Code's UI beyond the hook's own 8-second allowance.

All failures (network, timeout, auth, protocol errors) are surfaced to
the caller as exceptions — the helper does not degrade silently. Hooks
are responsible for catching and logging to stderr so that Claude Code
itself is never crashed.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from ..config import HOOK_REMOTE_TIMEOUT_SEC

# Re-exported for callers that import DEFAULT_TIMEOUT_SEC directly. See
# ``config.HOOK_REMOTE_TIMEOUT_SEC`` for the rationale on why this is 8s
# rather than the plan's original 2s (Fly cold-start handling).
DEFAULT_TIMEOUT_SEC = HOOK_REMOTE_TIMEOUT_SEC
DEFAULT_CLIENT_LABEL = "claude-code"

MNEMON_DIR = Path.home() / ".mnemon"
LOCAL_TOKEN_FILE = MNEMON_DIR / "local_token"
REMOTE_URL_FILE = MNEMON_DIR / "remote_url"


class RemoteClientConfigError(Exception):
    """Raised when the remote URL or local token cannot be resolved."""


def get_remote_url() -> str:
    """Resolve the mnemon remote URL.

    Order: ``MNEMON_REMOTE_URL`` env var → ``~/.mnemon/remote_url`` file.
    Raises :class:`RemoteClientConfigError` if neither is set.
    """
    env = os.environ.get("MNEMON_REMOTE_URL", "").strip()
    if env:
        return env
    if REMOTE_URL_FILE.exists():
        try:
            content = REMOTE_URL_FILE.read_text().strip()
            if content:
                return content
        except OSError:
            pass
    raise RemoteClientConfigError(
        "mnemon remote URL not configured. "
        "Set MNEMON_REMOTE_URL env var or write the URL to "
        f"{REMOTE_URL_FILE}. Example: "
        "https://<your-app>.fly.dev/mcp"
    )


def remote_mode_active() -> bool:
    """True iff a remote vault is configured (non-raising probe).

    Same resolution order as :func:`get_remote_url` (env → file) but
    returns a bool instead of raising — the low-level chokepoint both the
    CLI router and the Store guard key on so a machine pointed at a cloud
    vault never silently opens the local one.
    """
    if os.environ.get("MNEMON_REMOTE_URL", "").strip():
        return True
    if REMOTE_URL_FILE.exists():
        try:
            return bool(REMOTE_URL_FILE.read_text().strip())
        except OSError:
            return False
    return False


def get_local_token() -> str:
    """Resolve the mnemon local bearer token.

    Order: ``MNEMON_LOCAL_TOKEN`` env var → ``~/.mnemon/local_token`` file.
    Raises :class:`RemoteClientConfigError` if neither is set.
    """
    env = os.environ.get("MNEMON_LOCAL_TOKEN", "").strip()
    if env:
        return env
    if LOCAL_TOKEN_FILE.exists():
        try:
            content = LOCAL_TOKEN_FILE.read_text().strip()
            if content:
                return content
        except OSError:
            pass
    raise RemoteClientConfigError(
        "mnemon local token not configured. "
        "Set MNEMON_LOCAL_TOKEN env var or write the token value to "
        f"{LOCAL_TOKEN_FILE} (chmod 600)."
    )


async def _call_tool_async(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout: float,
    client_label: str,
) -> str:
    """Open a Streamable HTTP session, call a tool, return its text output.

    Split out from :func:`call_tool_sync` so it can be patched directly in
    tests without monkey-patching the MCP SDK's async context managers.

    mcp 2.x replaced v1's transport-plus-``ClientSession``-plus-
    ``initialize()`` layering with a single ``Client`` object. Headers are
    configured on an ``httpx2.AsyncClient`` passed to
    ``streamable_http_client``; ``Client`` negotiates the protocol version
    and runs the handshake automatically on enter.
    """
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    url = get_remote_url()
    token = get_local_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Mnemon-Client": client_label,
    }

    async def _run() -> str:
        async with httpx2.AsyncClient(headers=headers) as http_client:
            # streamable_http_client is itself the Transport the Client
            # drives (an async context manager yielding the read/write
            # streams); passing it as the server arg keeps the HTTP client
            # (auth headers) alive for the whole call.
            transport = streamable_http_client(url, http_client=http_client)
            async with Client(transport) as client:
                result = await client.call_tool(tool_name, arguments)
                # MCP tool results contain a list of content blocks. The
                # first text block is the one we want for memory_search /
                # memory_save / etc. — all current mnemon tools return
                # a single string.
                for content in getattr(result, "content", []):
                    text = getattr(content, "text", None)
                    if text is not None:
                        return text
                return ""

    return await asyncio.wait_for(_run(), timeout=timeout)


def call_tool_sync(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    client_label: str = DEFAULT_CLIENT_LABEL,
) -> tuple[str, float]:
    """Synchronous wrapper for hook scripts.

    Runs :func:`_call_tool_async` on a fresh event loop per call. Hooks
    are short-lived processes that invoke the helper at most once per
    run, so the per-call loop cost is negligible compared to the network
    round-trip.

    Returns:
        A ``(result, elapsed_seconds)`` tuple. ``elapsed_seconds`` is the
        wall-clock time for the full call, measured with
        :func:`time.monotonic`. Callers can use it to surface latency
        warnings when the round-trip exceeds a threshold.

    Raises:
        RemoteClientConfigError: if URL or token can't be resolved.
        asyncio.TimeoutError: if the call exceeds ``timeout`` seconds.
        Other exceptions propagate from the MCP SDK / httpx for the caller
        to log as it sees fit.
    """
    t0 = time.monotonic()
    result = asyncio.run(
        _call_tool_async(
            tool_name,
            arguments,
            timeout=timeout,
            client_label=client_label,
        )
    )
    return result, time.monotonic() - t0
