"""Persistent MCP Streamable HTTP session storage.

When a Fly.io machine running mnemon auto-stops on idle, the in-memory
session dict in ``StreamableHTTPSessionManager._server_instances`` is
lost. After the machine wakes, any client request bearing a previously-
issued ``Mcp-Session-Id`` header gets a 404 (per the MCP spec — but the
spec also says clients SHOULD reinit on 404, which Claude Code's MCP
client does not reliably do, so the symptom is "MCP UI says connected
but every tool call fails").

This module sidesteps that by persisting issued session IDs to SQLite,
so a known-but-not-in-memory session can be transparently resumed on
the new process. The resumed session is born with ``stateless=True``,
which makes the underlying ``ServerSession`` skip the init handshake
and accept tool calls immediately. mnemon does not use any client-
capability-gated features (sampling, elicitation, roots), so the
absence of negotiated client capabilities on a resumed session is a
no-op for our toolset.

The complement on the client side is the ``/health`` warm-keeper hook
installed by ``mnemon setup`` (see ``setup.py:_hooks_config``). The
hook keeps Fly warm during active sessions; this module catches the
edge case where the machine *did* cold-stop anyway (long idle, manual
stop, deploy).
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
from anyio.abc import TaskStatus
from mcp.server.connection import Connection
from mcp.server.lowlevel.server import Server as MCPServer
from mcp.server.runner import serve_connection, serve_loop
from mcp.server.streamable_http import (
    MCP_SESSION_ID_HEADER,
    EventStore,
    StreamableHTTPServerTransport,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp_types.version import LATEST_HANDSHAKE_VERSION
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 7 * 24 * 3600

# How often the in-process periodic prune task wakes up to call
# SessionStore.expire_old(). Bounded growth of mcp_sessions.sqlite under
# long warm uptimes — 6h matches the soak-watch-list note in ROADMAP.
# Set to 0 to disable (e.g. tests that don't want a background task
# bleeding into other test files).
DEFAULT_EXPIRE_INTERVAL_SECONDS = 6 * 3600

# How often the in-process periodic decay task wakes up to run
# contradiction.apply_confidence_decay() over the memory vault. Decay is
# orthogonal to session pruning — it ages confidence on stored memories
# so older facts sort lower in search — but it lives next to the prune
# task because both are background hygiene that the lifespan task group
# is the right place to schedule. Set to 0 (or pass decay_fn=None) to
# disable.
DEFAULT_DECAY_INTERVAL_SECONDS = 24 * 3600


class SessionStore:
    """SQLite-backed registry of issued MCP Streamable HTTP session IDs.

    Stores ``(session_id, created_at, last_active_at)`` for every session
    the manager hands out. Entries older than ``ttl_seconds`` are
    considered expired and treated as unknown.

    The store deliberately holds *no* per-session capability or
    handshake state — resumed sessions are born stateless on the server
    side, so there is nothing to persist beyond the ID itself.
    """

    def __init__(
        self,
        db_path: Path | str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.db_path = Path(db_path)
        self.ttl_seconds = ttl_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None puts SQLite in autocommit; each statement
        # commits on its own. Suits the single-statement ops below.
        return sqlite3.connect(str(self.db_path), isolation_level=None)

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_transport_sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    last_active_at REAL NOT NULL
                )
                """
            )

    def register(self, session_id: str) -> None:
        """Persist a newly-issued session ID (idempotent)."""
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO mcp_transport_sessions"
                "(session_id, created_at, last_active_at) VALUES (?, ?, ?)",
                (session_id, now, now),
            )

    def is_known(self, session_id: str) -> bool:
        """True if the session ID was issued and is not expired."""
        cutoff = time.time() - self.ttl_seconds
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM mcp_transport_sessions "
                "WHERE session_id = ? AND last_active_at > ?",
                (session_id, cutoff),
            ).fetchone()
        return row is not None

    def touch(self, session_id: str) -> None:
        """Update last-active timestamp on activity."""
        with self._connect() as db:
            db.execute(
                "UPDATE mcp_transport_sessions SET last_active_at = ? "
                "WHERE session_id = ?",
                (time.time(), session_id),
            )

    def expire_old(self) -> int:
        """Delete entries past TTL. Returns count deleted."""
        cutoff = time.time() - self.ttl_seconds
        with self._connect() as db:
            cur = db.execute(
                "DELETE FROM mcp_transport_sessions WHERE last_active_at <= ?",
                (cutoff,),
            )
            return cur.rowcount

    def count(self) -> int:
        """Total persisted (non-expired) sessions. Used by tests + diagnostics."""
        cutoff = time.time() - self.ttl_seconds
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM mcp_transport_sessions "
                "WHERE last_active_at > ?",
                (cutoff,),
            ).fetchone()
        return int(row[0]) if row else 0

    def oldest_age_seconds(self) -> float:
        """Age (seconds) of the least-recently-active persisted row, or 0.0.

        This is the volume-INDEPENDENT prune-health signal. ``count()``
        filters expired rows out, so it can never reveal a broken prune;
        this looks at *all* rows. Under a working ``expire_old()`` nothing
        survives past ``ttl_seconds`` (+ up to one prune interval), so this
        stays bounded near the TTL regardless of session volume. If it
        climbs well past the TTL, the periodic prune has stopped running.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT MIN(last_active_at) FROM mcp_transport_sessions"
            ).fetchone()
        if not row or row[0] is None:
            return 0.0
        return max(0.0, time.time() - float(row[0]))


class _PersistingInstanceDict(dict[str, StreamableHTTPServerTransport]):
    """Dict subclass that ``register()``s every key on assignment.

    Replaces ``StreamableHTTPSessionManager._server_instances`` so that
    every newly-minted session ID is durably persisted at the moment it
    enters the in-memory registry — without us having to reach into the
    SDK's session-creation code path.
    """

    def __init__(self, store: SessionStore) -> None:
        super().__init__()
        self._store = store

    def __setitem__(self, key: str, value: StreamableHTTPServerTransport) -> None:
        super().__setitem__(key, value)
        try:
            self._store.register(key)
        except Exception:  # noqa: BLE001
            # Persistence failure should not block session use — log and
            # continue; the session will still work in-memory, just won't
            # survive a restart. Hard-failing here would lose the request.
            logger.exception("Failed to persist session %s", key)


class PersistentSessionManager(StreamableHTTPSessionManager):
    """Session manager that resumes sessions across process restarts.

    Behavior overlay on the upstream manager:

    1. ``_server_instances`` is replaced with a dict that auto-persists
       every assignment, so newly-issued session IDs land in SQLite the
       moment they're handed out.
    2. ``_handle_stateful_request`` is overridden to add a resume branch:
       a request with an unknown-in-memory but known-in-SQLite session
       ID is served by spawning a fresh transport keyed to the same ID,
       running the MCP app against a born-ready connection so the
       server-side session is born already-initialized and tool calls
       succeed without a handshake round-trip (mcp 2.x dropped the
       v1 ``stateless=True`` Server.run flag; we construct the
       connection via ``Connection.from_envelope`` instead).

    The upstream new-session and 404 paths are otherwise untouched.
    """

    def __init__(
        self,
        app: MCPServer[Any],
        *,
        session_store: SessionStore,
        event_store: EventStore | None = None,
        json_response: bool = False,
        stateless: bool = False,
        security_settings: TransportSecuritySettings | None = None,
        retry_interval: int | None = None,
        session_idle_timeout: float | None = None,
        expire_interval_seconds: int = DEFAULT_EXPIRE_INTERVAL_SECONDS,
        decay_fn: Callable[[], int] | None = None,
        decay_interval_seconds: int = DEFAULT_DECAY_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(
            app=app,
            event_store=event_store,
            json_response=json_response,
            stateless=stateless,
            security_settings=security_settings,
            retry_interval=retry_interval,
            session_idle_timeout=session_idle_timeout,
        )
        self._session_store = session_store
        self._server_instances = _PersistingInstanceDict(session_store)
        # 0 disables periodic pruning entirely (startup-only, the prior
        # behavior). Non-zero spawns a background task during run() that
        # ticks every N seconds and calls expire_old(). This matters for
        # long warm uptimes (no cold-stop, no redeploy): the persisted-
        # sessions table otherwise grows monotonically until the next
        # restart.
        self._expire_interval_seconds = expire_interval_seconds
        # Wall-clock timestamp of the last expire_old() (periodic OR
        # request-path). The periodic task above relies on the event-loop
        # clock, which FREEZES while a Fly machine is suspended
        # (auto_stop_machines = "suspend", min_machines_running = 0): a 6h
        # anyio.sleep only advances during awake time, so on a scale-to-
        # zero deploy the prune fires far less often than every 6h of
        # WALL-CLOCK time and rows survive past the TTL (issue #215). The
        # request-path prune (_maybe_prune, called from
        # _handle_stateful_request) is gated on this real-clock timestamp
        # instead, so every machine wake bounds the oldest row near the
        # TTL regardless of suspension. 0.0 → the first request after a
        # cold boot prunes immediately.
        self._last_prune_ts: float = 0.0
        # Decay sweep is opt-in via an injected callable so this module
        # doesn't import Store directly — keeps the session-management
        # layer decoupled from the memory-vault layer. server_remote.py
        # passes a closure that opens its own thread-local Store.
        self._decay_fn = decay_fn
        self._decay_interval_seconds = decay_interval_seconds
        # Wall-clock timestamp of the last decay sweep (periodic OR
        # request-path). Same suspend hazard as _last_prune_ts: the
        # periodic decay timer freezes while the Fly machine is suspended
        # (issue #217), so the request-path trigger (_maybe_decay) gates
        # on this real-clock timestamp instead. Initialized to the
        # construction wall-clock — NOT 0.0 — so decay runs ~once per
        # interval rather than walking the full vault on every cold boot:
        # unlike the prune (which must fire on each wake to bound the
        # TTL), a missed decay cycle is cheap, so there's no value in
        # re-decaying immediately after every deploy/wake. A suspend
        # longer than the interval still trips the gate on the next
        # request, which is the failure mode #217 is about.
        self._last_decay_ts: float = time.time()
        # Process-lifetime counters scraped via /health for cold-stop diagnosis.
        # Single-event-loop server, so plain ints are race-free without a Lock.
        # NOTE: scripts/check_health.py reads these key names directly. If you
        # rename, add, or remove a key here, update the GHA monitor in the same
        # PR — the workflow hard-fails on missing keys ("schema drift").
        self._counters: dict[str, int] = {
            "in_memory_hits": 0,
            "resume_hits": 0,
            "fresh_inits": 0,
            "stale_session_misses": 0,
        }

    def metrics(self) -> dict[str, int]:
        """Snapshot of session-routing counters for /health introspection.

        ``stale_session_misses`` counts requests bearing a session ID that
        is neither in memory nor in SQLite — the actual "Session not found"
        surface. ``resume_hits`` counts the cold-stop recovery path firing.
        Persistent across process lifetime; resets on cold-stop.
        """
        m: dict[str, int] = {
            **self._counters,
            "persisted_sessions_total": self._session_store.count(),
            "in_memory_sessions_current": len(self._server_instances),
            # Volume-independent prune-health signal: bounded near the TTL
            # while expire_old() runs, climbs only if the prune stalls.
            "oldest_session_age_seconds": int(self._session_store.oldest_age_seconds()),
        }
        # Decay-health signal — emitted ONLY when the decay sweep is wired
        # (the web server passes decay_fn; stdio does not, and would have
        # no sweep to reset it). Seconds since decay was last TRIGGERED
        # (periodic timer OR request-path _maybe_decay). Bounded under the
        # interval while either path fires; climbs only if BOTH stall —
        # i.e. the suspend-freeze failure mode of issue #217, which was
        # previously silent. check_health.py soft-warns (exit 2) when it
        # exceeds the interval + grace. Its ABSENCE is tolerated (decay-
        # disabled deploy / older server) — NOT treated as schema drift.
        if self._decay_fn is not None:
            m["seconds_since_last_decay"] = int(time.time() - self._last_decay_ts)
        return m

    def health_snapshot(self) -> dict[str, int]:
        """``metrics()`` for the /health endpoint, with a prune triggered first.

        The request-path prune (:meth:`_maybe_prune`) only runs inside
        ``_handle_stateful_request`` — i.e. on real MCP traffic. ``/health``
        is served by the auth middleware and bypasses that path, so on a
        low-traffic suspend-on-idle deploy the only guaranteed hourly
        request (the health probe itself) never bounded the very metric it
        reports. ``oldest_session_age_seconds`` then drifted up past the TTL
        between real requests overnight, false-tripping check_health.py's
        soft warn even though the prune was perfectly healthy (it self-heals
        the instant any real request arrives).

        Reading the snapshot through a prune fixes that structurally: the
        hourly probe now guarantees a prune-trigger ≤ one interval, bounding
        ``oldest_session_age_seconds`` at TTL + interval, and the reported
        value is post-prune — /health never advertises a session the server
        would already have removed. The prune is the same gated, cheap,
        error-swallowing DELETE the request path uses (≤ once per
        ``expire_interval_seconds``), so it is safe to ride on the liveness
        endpoint. ``metrics()`` itself stays a pure read for every other
        caller.
        """
        self._maybe_prune()
        return self.metrics()

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Lifespan wrapper that adds a periodic ``expire_old()`` task.

        Upstream's ``run()`` creates the task group + sets
        ``self._task_group``. We layer on a background coroutine that
        ticks every ``expire_interval_seconds`` and prunes the
        persisted-sessions table. The task is started inside the parent
        task group so it auto-cancels on lifespan shutdown — no explicit
        teardown needed.
        """
        async with super().run():
            if self._expire_interval_seconds > 0:
                assert self._task_group is not None
                await self._task_group.start(self._run_periodic_expire)
            if self._decay_fn is not None and self._decay_interval_seconds > 0:
                assert self._task_group is not None
                await self._task_group.start(self._run_periodic_decay)
            yield

    async def _run_periodic_expire(
        self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
    ) -> None:
        """Wake every ``expire_interval_seconds`` and prune expired rows.

        Failures are logged and swallowed — losing one prune cycle to a
        transient SQLite hiccup must not crash the manager and take
        every active session with it. The next tick retries.
        """
        task_status.started()
        while True:
            await anyio.sleep(self._expire_interval_seconds)
            try:
                expired = self._session_store.expire_old()
            except Exception:  # noqa: BLE001
                logger.exception("Periodic session prune raised; retrying next tick")
                continue
            # Coordinate with the request-path prune so the next request
            # after a timer tick doesn't redundantly re-prune.
            self._last_prune_ts = time.time()
            if expired:
                logger.info(
                    "Periodic prune: removed %d expired MCP session(s)",
                    expired,
                )

    async def _run_periodic_decay(
        self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
    ) -> None:
        """Wake every ``decay_interval_seconds`` and apply confidence decay.

        Mirrors ``_run_periodic_expire``: failures logged + swallowed so a
        transient SQLite hiccup in the decay sweep cannot crash the
        manager and take every active MCP session with it.

        ``decay_fn`` is run in a worker thread via ``anyio.to_thread``
        because ``apply_confidence_decay`` walks the full vault and does
        blocking SQL — keeping it off the event loop avoids stalling
        request handling during the sweep.
        """
        task_status.started()
        assert self._decay_fn is not None
        while True:
            await anyio.sleep(self._decay_interval_seconds)
            # Coordinate with the request-path decay so the next request
            # after a timer tick doesn't redundantly re-sweep the vault.
            self._last_decay_ts = time.time()
            try:
                updated = await anyio.to_thread.run_sync(self._decay_fn)
            except Exception:  # noqa: BLE001
                logger.exception("Periodic memory decay raised; retrying next tick")
                continue
            if updated:
                logger.info(
                    "Periodic decay: aged %d memory document(s)",
                    updated,
                )

    def _maybe_prune(self) -> None:
        """Wall-clock-gated prune, run synchronously on the request path.

        Companion to the event-loop-timer prune (_run_periodic_expire),
        which is unreliable on a suspend-on-idle Fly machine because the
        monotonic clock freezes during suspension. This path keys off
        ``time.time()`` (real wall-clock, survives the RAM snapshot) and
        an in-memory ``_last_prune_ts`` (also survives suspend/resume),
        so the first request after the expire interval has elapsed prunes
        once — bounding ``oldest_session_age_seconds`` near the TTL on
        every wake regardless of how long the machine was suspended.

        Cheap: a single indexed DELETE, gated to at most once per
        ``_expire_interval_seconds``. Failures are logged and swallowed —
        a transient SQLite hiccup must not fail the client request it's
        riding on; the next request retries. ``_expire_interval_seconds
        <= 0`` disables it (mirrors the periodic-task opt-out).
        """
        if self._expire_interval_seconds <= 0:
            return
        now = time.time()
        if now - self._last_prune_ts < self._expire_interval_seconds:
            return
        self._last_prune_ts = now
        try:
            expired = self._session_store.expire_old()
        except Exception:  # noqa: BLE001
            logger.exception("Request-path session prune raised; retrying next request")
            return
        if expired:
            logger.info("Request-path prune: removed %d expired MCP session(s)", expired)

    def _maybe_decay(self) -> None:
        """Wall-clock-gated decay trigger on the request path — the
        suspend-robust companion to _run_periodic_decay, whose event-loop
        timer freezes while a Fly machine is suspended (issue #217). Gated
        on ``time.time()`` + the in-memory ``_last_decay_ts`` (both survive
        a RAM snapshot), so the first request after the decay interval has
        elapsed fires one sweep regardless of suspension.

        Unlike _maybe_prune, the sweep is NOT run inline:
        ``apply_confidence_decay`` walks the full vault (slow, blocking
        SQL), so it's spawned onto the lifespan task group to run in a
        worker thread (_run_request_path_decay) without blocking the
        client request it rides on. The gate is stamped synchronously
        BEFORE spawning so concurrent requests in the same interval fire
        at most one sweep (single event loop → the check-and-set can't
        interleave). Disabled when decay isn't wired or the interval <= 0.
        """
        if self._decay_fn is None or self._decay_interval_seconds <= 0:
            return
        if self._task_group is None:
            # Lifespan not started yet (e.g. a request races startup);
            # the periodic task covers warm uptime once run() spawns it.
            return
        now = time.time()
        if now - self._last_decay_ts < self._decay_interval_seconds:
            return
        self._last_decay_ts = now
        self._task_group.start_soon(self._run_request_path_decay)

    async def _run_request_path_decay(self) -> None:
        """The decay sweep spawned by :meth:`_maybe_decay`, run in a
        worker thread off the request's event-loop task. Mirrors the body
        of _run_periodic_decay; failures are logged and swallowed so a
        transient SQLite hiccup can't crash the spawning task group.
        """
        assert self._decay_fn is not None
        try:
            updated = await anyio.to_thread.run_sync(self._decay_fn)
        except Exception:  # noqa: BLE001
            logger.exception("Request-path memory decay raised; retrying next interval")
            return
        if updated:
            logger.info("Request-path decay: aged %d memory document(s)", updated)

    async def _handle_stateful_request(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        self._maybe_prune()
        self._maybe_decay()
        request = Request(scope, receive)
        session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        # In-memory hit: refresh persistence + delegate to upstream.
        if session_id is not None and session_id in self._server_instances:
            self._counters["in_memory_hits"] += 1
            self._session_store.touch(session_id)
            await super()._handle_stateful_request(scope, receive, send)
            return

        # Persisted but not in memory: this is the resume case (post
        # cold-start, redeploy, or process restart).
        if session_id is not None and self._session_store.is_known(session_id):
            self._counters["resume_hits"] += 1
            logger.info("Resuming persisted MCP session %s", session_id)
            await self._resume_session(session_id, scope, receive, send)
            return

        # Fresh-init or stale. Branch BEFORE delegating: fresh-init
        # takes our narrow-lock path (see _create_new_session); stale
        # delegates to upstream's lock-free 404 path.
        if session_id is None:
            self._counters["fresh_inits"] += 1
            await self._create_new_session(scope, receive, send)
            return

        # Stale: client sent a session ID we never issued or that
        # expired. Upstream returns 404 — this is the actual
        # "Session not found" surface seen by clients. The 404 branch
        # in upstream does NOT acquire _session_creation_lock, so
        # delegating here is safe even when the lock is contended.
        self._counters["stale_session_misses"] += 1
        logger.warning(
            "Stale session_id %s — not in memory, not in SQLite. "
            "Upstream will return 404 per MCP spec.",
            session_id,
        )
        await super()._handle_stateful_request(scope, receive, send)

    async def _resume_session(
        self,
        session_id: str,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Reconstruct an in-memory transport for ``session_id`` and dispatch.

        Mirrors the new-session path in upstream ``_handle_stateful_request``
        but reuses the supplied session_id instead of minting a fresh one
        and runs the MCP app against a born-ready connection so the
        server-side session is born already-initialized — mcp 2.x dropped
        v1's ``stateless=True`` ``Server.run`` flag, so we build a
        ``Connection`` via ``Connection.from_envelope`` (initialized pre-set)
        and drive ``serve_connection`` over it. Tool calls then succeed
        without the client re-sending an InitializeRequest.

        Lock scope is deliberately narrow: ``_session_creation_lock`` is
        held only for the race-guard read + ``_server_instances`` write,
        then released before ``transport.handle_request`` is awaited.
        See :meth:`_create_new_session` for the rationale; same wedge
        risk applies here.
        """
        transport = StreamableHTTPServerTransport(
            mcp_session_id=session_id,
            is_json_response_enabled=self.json_response,
            event_store=self.event_store,
            security_settings=self.security_settings,
            retry_interval=self.retry_interval,
        )

        async def run_server(
            *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
        ) -> None:
            async with transport.connect() as streams:
                read_stream, write_stream = streams
                task_status.started()
                try:
                    idle_scope = anyio.CancelScope()
                    if self.session_idle_timeout is not None:
                        idle_scope.deadline = (
                            anyio.current_time() + self.session_idle_timeout
                        )
                        transport.idle_scope = idle_scope

                    with idle_scope:
                        # Born-ready resume: from_envelope pre-sets
                        # ``initialized`` (and therefore
                        # ``initialize_accepted``), so the init gate opens
                        # without an InitializeRequest the client will
                        # never re-send. The client's protocol era is
                        # unknown on this path, so respond at the latest
                        # handshake version (matching what a fresh
                        # session would have negotiated). mnemon uses no
                        # client-capability-gated features, so a resumed
                        # session with no negotiated capabilities is a
                        # no-op for our toolset.
                        dispatcher = JSONRPCDispatcher(
                            read_stream,
                            write_stream,
                            inline_methods=frozenset({"initialize"}),
                        )
                        connection = Connection.from_envelope(
                            LATEST_HANDSHAKE_VERSION,
                            None,
                            None,
                            outbound=dispatcher,
                        )
                        await serve_connection(
                            self.app,
                            dispatcher,
                            connection=connection,
                            lifespan_state=self._lifespan_state,
                        )

                    if idle_scope.cancelled_caught:
                        logger.info(
                            "Resumed session %s idle timeout", session_id
                        )
                        self._server_instances.pop(session_id, None)
                        await transport.terminate()
                except Exception:
                    logger.exception("Resumed session %s crashed", session_id)
                finally:
                    if (
                        transport.mcp_session_id
                        and transport.mcp_session_id in self._server_instances
                        and not transport.is_terminated
                    ):
                        del self._server_instances[transport.mcp_session_id]

        # Narrow lock: race-guard + dict mutation only.
        async with self._session_creation_lock:
            if session_id in self._server_instances:
                # Race lost: another concurrent request resumed the
                # session while we were minting our transport. Drop
                # ours and fall through to the in-memory hit path.
                self._session_store.touch(session_id)
                race_lost = True
            else:
                self._server_instances[session_id] = transport
                self._session_store.touch(session_id)
                race_lost = False

        if race_lost:
            # Outside the lock — upstream's in-memory hit path is
            # lock-free, so delegating here can't reintroduce the
            # bottleneck.
            await super()._handle_stateful_request(scope, receive, send)
            return

        assert self._task_group is not None
        await self._task_group.start(run_server)
        await transport.handle_request(scope, receive, send)

    async def _create_new_session(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Mint + register a fresh MCP session, then dispatch outside the lock.

        Mirrors upstream's new-session creation logic but narrows
        ``_session_creation_lock`` to cover only the brief
        ``_server_instances`` mutation. Upstream holds the lock for the
        full ``handle_request`` await, which made the lock a single
        point of failure: on 2026-05-06 a wedged fresh-init handler
        held the lock and every subsequent fresh-init request queued
        behind it and timed out at the client side, while in-memory
        hits and resumes (which don't need the lock) kept working.
        Releasing earlier means one wedged handler can't take down the
        server's ability to accept new sessions.
        """
        new_session_id = uuid4().hex
        transport = StreamableHTTPServerTransport(
            mcp_session_id=new_session_id,
            is_json_response_enabled=self.json_response,
            event_store=self.event_store,
            security_settings=self.security_settings,
            retry_interval=self.retry_interval,
        )

        async def run_server(
            *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
        ) -> None:
            async with transport.connect() as streams:
                read_stream, write_stream = streams
                task_status.started()
                try:
                    idle_scope = anyio.CancelScope()
                    if self.session_idle_timeout is not None:
                        idle_scope.deadline = (
                            anyio.current_time() + self.session_idle_timeout
                        )
                        transport.idle_scope = idle_scope

                    with idle_scope:
                        # mcp 2.x rebuilt the session loop; serve_loop is
                        # what the upstream stateful path drives (the old
                        # Server.run(read, write, init_options,
                        # stateless=...) signature is gone). Handles the
                        # handshake era (fresh sessions initialize) and the
                        # modern era from one loop.
                        await serve_loop(
                            self.app,
                            read_stream,
                            write_stream,
                            lifespan_state=self._lifespan_state,
                            session_id=transport.mcp_session_id,
                        )

                    if idle_scope.cancelled_caught:
                        logger.info(
                            "Session %s idle timeout", new_session_id
                        )
                        self._server_instances.pop(new_session_id, None)
                        await transport.terminate()
                except Exception:
                    logger.exception(
                        "Session %s crashed", new_session_id
                    )
                finally:
                    if (
                        transport.mcp_session_id
                        and transport.mcp_session_id in self._server_instances
                        and not transport.is_terminated
                    ):
                        del self._server_instances[transport.mcp_session_id]

        # Narrow lock: only the dict mutation needs to be serialized.
        # transport.connect() / serve_loop() / transport.handle_request()
        # are all per-session — no shared state between concurrent
        # fresh-init requests.
        async with self._session_creation_lock:
            self._server_instances[new_session_id] = transport

        assert self._task_group is not None
        await self._task_group.start(run_server)
        await transport.handle_request(scope, receive, send)
