"""Remote-proxy MCP server (stdio transport).

When a remote vault is configured (``MNEMON_REMOTE_URL`` env var or
``~/.mnemon/remote_url`` file), ``mnemon serve`` runs THIS server instead
of the local-SQLite :func:`mnemon.server.run_stdio`. Every tool call is
forwarded to the remote vault via
:func:`mnemon.hooks._remote_client.call_tool_sync` — the local SQLite
vault is never opened.

Why this exists
---------------
Before 2026-06-04, ``mnemon serve`` always opened the local vault even
when a remote was configured, so a machine pointed at a cloud vault
exposed TWO connected MCP servers backed by DIFFERENT data — the local
one a stale, near-empty vault. Reads/writes through it silently diverged
from the source of truth (a stale ``memory_status``/``memory_timeline``
answered as if authoritative). The CLI read/write commands already
routed to the remote via ``cli._remote_mode_active()``; ``serve`` was the
one command that didn't. This module closes that gap: in remote mode the
local vault is structurally inaccessible over MCP.

Design — no drift
-----------------
The proxy mirrors the EXACT tool surface of :mod:`mnemon.server` by
wrapping each registered tool function with :func:`functools.wraps`
(which copies name, docstring, signature, and annotations → an identical
MCP input schema) and swapping the body to forward remotely. The tool
set is enumerated from the local server's own tool manager, so adding a
tool to ``server.py`` automatically adds its proxy. ``tests/
test_server_proxy.py`` asserts schema + description parity as a
belt-and-suspenders guard.

Fail-loud
---------
A remote / network / auth / config failure propagates out of the tool
call (surfaced to the MCP client as a tool error) rather than degrading
to the local vault. Per the project no-silent-fails rule, the proxy must
never silently answer from the wrong store.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any

from mcp.server import MCPServer

from . import server as _local
from .hooks._remote_client import call_tool_sync

# Generous timeout for interactive tool calls proxied to the remote.
# Larger than the hooks' 8s budget: operations like memory_rebuild /
# memory_sweep / memory_export_vectors do real work on the remote and a
# human is waiting on the MCP client, not a fire-and-forget hook.
PROXY_TIMEOUT_SEC = 60.0
_CLIENT_LABEL = "claude-code-proxy"


def _forward(tool_name: str, arguments: dict[str, Any]) -> str:
    """Forward one tool call to the configured remote vault.

    Drops ``None``-valued optional arguments so the remote applies its
    own defaults (mirrors how the bare MCP client would omit them).
    Raises on any failure — the caller (MCPServer) surfaces it to the MCP
    client as a tool error. Never falls back to the local vault.
    """
    args = {k: v for k, v in arguments.items() if v is not None}
    text, _elapsed = call_tool_sync(
        tool_name,
        args,
        timeout=PROXY_TIMEOUT_SEC,
        client_label=_CLIENT_LABEL,
    )
    return text


def _make_proxy(local_fn):
    """Build a remote-forwarding wrapper with ``local_fn``'s exact
    signature/docstring so MCPServer derives an identical tool schema."""
    name = local_fn.__name__
    sig = inspect.signature(local_fn)

    @functools.wraps(local_fn)
    def wrapper(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return _forward(name, dict(bound.arguments))

    return wrapper


def build_proxy() -> MCPServer:
    """Construct the remote-proxy MCPServer instance.

    Enumerates every tool registered on the local server and registers a
    remote-forwarding twin under the same name/schema. Returns a fresh
    instance per call (cheap; MCPServer construction is in-process) so
    tests can build in isolation.

    stdio transport — mcp 2.x dropped ``transport_security`` from the
    server constructor and stdio never needs it, so unlike the pre-2.x
    FastMCP this takes no transport_security argument.
    """
    mcp = MCPServer("mnemon")
    for tool in _local.mcp._tool_manager.list_tools():
        mcp.tool()(_make_proxy(tool.fn))
    return mcp


def run_remote_proxy() -> None:
    """Start the remote-proxy MCP server on stdio transport."""
    build_proxy().run(transport="stdio")
