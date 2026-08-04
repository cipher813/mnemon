# Changelog

## [Unreleased]

### Changed
- **MCP SDK 2.x compatibility.** mcp 2.0.0 (2026-07-28) removed
  `mcp.server.fastmcp.FastMCP` (renamed `MCPServer`), removed v1's
  `ClientSession`+`streamablehttp_client` client layering (replaced by the
  unified `Client`), dropped the `stateless=` flag on the lowlevel
  `Server.run`, and moved `transport_security`/`event_store`/`stateless`
  out of the server constructor and into the transport call sites. mnemon
  was migrated end-to-end: `server.py`/`server_proxy.py` now build an
  `MCPServer`, `hooks/_remote_client.py` drives the new `Client` over an
  `httpx2`-configured Streamable HTTP transport, and `persistent_sessions.py`
  reimplements the cold-stop resume trick (born-already-initialized session)
  via a born-ready `Connection.from_envelope`, since v2's session loop no
  longer accepts `stateless=True`. `server_remote.py` now mounts the
  `PersistentSessionManager` directly instead of pre-injecting it into a
  FastMCP lazy path that no longer exists. The `mcp>=1.27,<3` upper bound
  (relaxed from `<2`) lets the dependency resolver pick 2.x.

## [0.7.9] - 2026-07-13

External-readiness audit — no behavior changes, packaging/docs only. Bumped
specifically because `publish.yml` is `skip-existing`, so the `pyproject.toml`
metadata fixes below would never actually reach PyPI without a version bump.

### Fixed
- **PyPI project-urls pointed at the pre-transfer `cipher813` org.** All four
  `pyproject.toml` `urls.*` fields (Homepage/Repository/Issues/Changelog) —
  the "Project links" sidebar every visitor to the PyPI page sees — still
  said `cipher813/mnemon` after the org moved to `nousergon`. Same fix applied
  everywhere else the stale org leaked: `README.md`, `SECURITY.md`,
  `CONTRIBUTING.md`, `CLAUDE.md` (`mnemon-ops` reference), the CLI's own
  `--help` docs line, the OAuth AS discovery document and the Protected
  Resource Metadata document (both served live to real OAuth clients), and
  `scripts/promote_stable.sh`'s release-URL echo.
- **Both README screenshots were broken on the PyPI page.** `docs/images/*.png`
  were referenced as relative paths, which don't resolve against PyPI's
  long-description renderer (no repo base to resolve against) — GitHub
  rendered them fine, PyPI showed two broken-image icons. Switched to
  absolute `raw.githubusercontent.com` URLs, which render correctly on both.
- **MCP tool count under-reported as 17 in five places** — `README.md`'s tools
  table, `CLAUDE.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md`, and the feature-
  request issue template. Actual count is 19; the README table was missing
  `memory_export_coords` and `memory_export_relations` entirely. Added the two
  missing rows and corrected the count everywhere.
- **`changelog.yml`'s reusable-action reference was doubly stale**
  (`cipher813/alpha-engine-docs` → `nousergon/nousergon-docs`, both the org
  and the repo renamed). Also added a `github.repository ==
  'nousergon/mnemon'` guard — this job assumes an AWS OIDC role in the
  maintainer's account, so an external fork pushing to its own `main` was
  getting a spurious red run with no way to satisfy it.
- CONTRIBUTING.md's "~1010 tests" quick-start claim had drifted to ~1160.

## [0.7.8] - 2026-06-15

### Fixed
- **`/health` no longer false-warns on an idle suspend-on-idle deploy.** The
  request-path prune (`_maybe_prune`) only runs on real MCP traffic, but
  `/health` is served by the auth middleware and bypasses that path — so on a
  low-traffic Fly deploy the only guaranteed hourly request (the health probe
  itself) never bounded the metric it reports. `oldest_session_age_seconds`
  drifted past the TTL between real requests overnight, soft-tripping
  `check_health.py`'s prune-health warn (exit 2 → red run + tracker issue)
  even though the prune was perfectly healthy and self-heals the instant any
  real request arrives. The endpoint now reads through a new
  `PersistentSessionManager.health_snapshot()` that triggers the gated,
  cheap, error-swallowing prune before snapshotting: the hourly probe now
  guarantees a prune-trigger ≤ one expire interval, bounding
  `oldest_session_age_seconds` at TTL + interval, and the reported value is
  post-prune (`/health` never advertises a session the server would already
  have removed). `metrics()` itself stays a pure read for every other caller.

## [0.7.7] - 2026-06-11

### Added
- **`memory_save` auto-detects prose supersession.** When `correction_of`
  is omitted but the saved content explicitly states it supersedes a
  specific id (`supersedes id N` / `supersedes #N`, including the
  motivating "Supersedes the partial financial framings in id 2402"
  form), the `'supersedes'` relation is now recorded automatically. It is
  additive and safe: it WARNs (never silent), tolerates a non-existent id
  (the save still succeeds, unlike the explicit param which raises), and
  the detection is clause-local + bounded so cross-clause mentions
  ("supersedes the old way; see id 5") don't false-link. Pass
  `correction_of` explicitly to opt out of the scan. (The explicit param
  always takes precedence.)

### Fixed
- **`examples/quickstart.py` and `bench/search_stress.py` run again on a
  machine with a remote vault configured.** Both create an isolated
  throwaway local vault but predated the remote-mode `Store` guard, so
  they crashed with `LocalVaultInaccessibleError` whenever a remote was
  set (i.e. on any dev machine that had run `mnemon upgrade web`). They
  now set `MNEMON_ALLOW_LOCAL_STORE=1` — the sanctioned escape hatch for
  a deliberately-isolated local vault. Fresh users (no remote) were
  unaffected.

## [0.7.6] - 2026-06-11

### Fixed
- **Confidence-decay sweep now fires on a wall-clock cadence, so it
  survives the suspend-on-idle Fly machine — and is no longer silent
  (issue #217).** Same root cause as the 0.7.5 session-prune fix: the
  periodic decay task (`_run_periodic_decay`, 24h) is an event-loop
  interval timer, and that clock freezes while the Fly machine is
  suspended (`auto_stop_machines = "suspend"`, `min_machines_running =
  0`), so on the web tier decay effectively stopped running — degrading
  scoring quality with **no red signal**, because (unlike the prune)
  nothing on `/health` tracked it. Added a request-path decay trigger
  (`_maybe_decay`, called from `_handle_stateful_request`) gated on
  `time.time()` + an in-memory `_last_decay_ts` (both survive a RAM
  snapshot). Because `apply_confidence_decay` walks the full vault
  (slow, blocking SQL), the sweep is **not** run inline — it's spawned
  onto the lifespan task group to run in a worker thread without blocking
  the client request. `_last_decay_ts` initializes to construction time
  (not `0.0`), so decay runs ~once per interval rather than re-walking
  the vault on every cold boot; a suspend longer than the interval still
  trips the gate on the next request. The periodic timer is retained for
  warm uptimes and stamps `_last_decay_ts` to coordinate.
- **`/health` now exposes `seconds_since_last_decay`** (emitted only when
  decay is wired — the web server, not stdio). `scripts/check_health.py`
  soft-warns (exit 2) when it exceeds the 24h interval + 12h grace, so a
  stalled decay surfaces on the Health Monitor instead of failing
  silently. Absence is tolerated (decay-disabled or pre-0.7.6 servers).

## [0.7.5] - 2026-06-11

### Fixed
- **Session prune now fires on a wall-clock cadence, so it survives the
  suspend-on-idle Fly machine (regression from 0.7.4).** The expired-MCP-
  session prune was driven solely by an event-loop interval timer
  (`await anyio.sleep(6h)` in `_run_periodic_expire`). When 0.7.4 switched
  the idle machine to `auto_stop_machines = "suspend"`, that clock began
  freezing during suspension: on a `min_machines_running = 0` deploy the
  machine is suspended most of the day, so the 6h timer only advanced
  during *awake* time and `expire_old()` fired far less often than every
  6h of wall-clock. Rows survived past the 7-day TTL and
  `oldest_session_age_seconds` climbed 1:1 with real time — the
  health-monitor's prune-health check (added 0.7.1) caught it and went
  red (issue #215). Added a request-path prune (`_maybe_prune`, called at
  the top of `_handle_stateful_request`) gated on `time.time()` plus an
  in-memory `_last_prune_ts` — both of which survive a RAM snapshot — so
  the first request after the expire interval has elapsed prunes once,
  bounding the oldest row near the TTL on every machine wake regardless of
  how long it was suspended. The periodic timer is retained for long warm
  uptimes and now stamps `_last_prune_ts` to coordinate with the
  request-path prune. The prune is a single indexed `DELETE`, gated to at
  most once per `expire_interval_seconds`, and its failures are logged and
  swallowed so a transient SQLite hiccup can't fail the client request it
  rides on.

## [0.7.4] - 2026-06-10

### Fixed
- **Self-hosted web deploys now `suspend` an idle Fly machine instead of
  `stop`, so a connector survives the host going to sleep.** The
  self-hosted OAuth AS (`/oauth/token`) introduced in rc10/rc11 runs on the
  same Fly machine as the MCP server. With `auto_stop_machines = "stop"` an
  idle machine cold-boots on wake, which is slow enough that a connector's
  silent token refresh after the operator's host reboots (e.g. Claude
  Desktop after an overnight shutdown) times out — dropping the connector
  to "disconnected" and forcing a full passphrase re-auth (observed as
  repeated DCR client registrations piling up). Switching the generated
  `fly.toml` (and `fly.toml.example`) to `auto_stop_machines = "suspend"`
  snapshots RAM and resumes in ~1s, keeping refresh under the client
  timeout so the connection persists. Server-side token/key state already
  persisted on the volume; this only addresses cold-start latency on the
  auth endpoint. Existing deployments can apply the fix without a redeploy
  via `fly machine update <id> --autostop=suspend`.

## [0.7.3] - 2026-06-08

### Fixed
- **Redeploy could silently rotate a live OAuth AS passphrase (fail-open
  guard).** The rc11 back-fill in `_redeploy_web` provisions the OAuth AS
  only when `MNEMON_AS_PASSPHRASE` is absent from the Fly secret list, and
  is meant to *never* rotate an existing passphrase (rotation invalidates
  every issued token and breaks the operator's claude.ai/Desktop login).
  But the absence check trusted `_fly_secret_names` unconditionally: if
  flyctl's `secrets list --json` shape ever changed (e.g. the `Name` field
  renamed), the parse would silently yield an empty/wrong set, the naive
  `"MNEMON_AS_PASSPHRASE" not in secrets` check would pass, and the
  redeploy would **rotate a live passphrase**. Made the guard fail-closed:
  an existing Fly app always carries `MNEMON_LOCAL_TOKEN`, so it's now used
  as a parse-sanity **sentinel** — the back-fill fires only on "sentinel
  present AND AS absent." A parsed list missing the sentinel is treated as
  an untrustworthy read: the passphrase is left untouched and a loud
  `WARNING` is emitted to stderr instead of silently rotating. Every other
  read failure (list call fails → `None`) already skipped. Coverage:
  `tests/test_upgrade.py::TestRedeployASBackfill` (new regression guard
  that a renamed-`Name`/empty-set read does NOT provision and DOES warn).

## [0.7.2] - 2026-06-07

### Fixed
- **Health-monitor perpetual false-positive (issue #208).** The checker
  warned when `persisted_sessions_total >= 5000`, opening a tracking
  issue every run for active users. But that metric is computed by
  `SessionStore.count()`, which filters `WHERE last_active_at > cutoff` —
  it only counts *non-expired* sessions, so it tracks legitimate volume
  and can never reveal a broken prune. Replaced the volume threshold with
  a **volume-independent prune-health signal**: new
  `oldest_session_age_seconds` metric (via `SessionStore.oldest_age_seconds()`)
  reports the age of the least-recently-active row across *all* rows. The
  checker now warns only if that exceeds the 7-day TTL + 2 prune cycles
  (12h grace) — i.e. only when the periodic `expire_old()` task has
  actually stalled. `persisted_sessions_total` is kept for observability
  (no longer thresholded). Coverage: `tests/test_check_health.py` (incl. a
  regression guard that high volume no longer warns) +
  `oldest_age_seconds` store tests.

## [0.7.1] - 2026-06-07

### Fixed
- **Graph chart title was wrong and collided with the legend.** It
  hard-coded "Memory Vector Space (UMAP 2D)" — overlapping the top
  legend, and inaccurate in remote mode (which projects via PCA, not
  UMAP, since rc18). The title now reflects the actual reducer
  (`Memory vector space · PCA 2D` remote / `· UMAP 2D` local) and the
  legend moved to the bottom of the chart so it can't collide.

### Docs
- README: added the Memory Graph screenshot (`docs/images/graph.png`)
  under "Visualize your vault".

## [0.7.0] - 2026-06-07

First stable release of the 0.7.0 line. Consolidates the rc1–rc19
prereleases below. Highlights since 0.6.0:

- **Cross-device is the default.** `mnemon upgrade web` auto-provisions the
  self-hosted OAuth 2.1 Authorization Server (PKCE + Dynamic Client
  Registration) alongside the bearer token, so claude.ai web/mobile and
  Claude Desktop connect turnkey to one shared Fly-hosted vault.
- **Salience / standing tier.** First-class `documents.tier` with
  `memory_promote` / `memory_demote` / `memory_list_standing`; standing
  memories are injected unconditionally as always-on context.
- **Capture-attention.** Recurring saves are preserved + related (via
  `restates`) + boosted rather than dropped — non-destructive dedup.
- **NLI-based contradiction detection** (cross-encoder, baked into the
  Fly image) replacing the optional LLM path.
- **Scalable dashboard Graph.** Server-side PCA projection
  (`memory_export_coords`) + bulk relation export
  (`memory_export_relations`) — the Graph renders at any vault size
  (validated on a 3,000+ doc vault) instead of timing out.
- **Comprehensive test suite.** 1,100+ tests; multi-platform CI with
  model caching + timeouts; every dashboard page covered for both
  render and remote-failure paths; full MCP-tool round-trip canary.
- **PyPI auto-publish** on push to `main` via OIDC trusted publishing.

## [0.7.0rc19] - 2026-06-07

### Added
- **Memory Graph relation-edge overlay now scales too** (the second
  large-vault Graph timeout). The edge overlay fetched relations **one
  document at a time** — N MCP round-trips (3,000+ on a large remote),
  which timed out even when the projection itself was fast. New
  `memory_export_relations` MCP tool returns every edge between live
  documents in **one** call (`{count, truncated, edges}`), backed by a
  single `Store.export_relations()` query; the Graph page builds the
  overlay from that one payload via a new `load_relations_bulk()` loader.
  Edges are capped at `_RELATIONS_EXPORT_MAX` (20,000) and the bulk call
  is wrapped in the page's `remote_guard`. Together with rc18's
  server-side PCA projection, the Graph now renders end-to-end at any
  vault size. Coverage: server `memory_export_relations` tests +
  `Store.export_relations` tests (live edges, invalidated-endpoint
  exclusion) + a dashboard edges-failure-degrade AppTest. Tool inventory
  18 → 19.

## [0.7.0rc18] - 2026-06-07

### Added
- **Memory Graph now scales to large vaults.** The Graph page previously
  pulled every document's full 384-d vector over MCP
  (`memory_export_vectors` — ~10 MB JSON for a few thousand docs) and ran
  UMAP client-side; on a large or cold remote that transfer timed out and
  the page couldn't render. New `memory_export_coords` MCP tool collapses
  each document to one mean-pooled, L2-normalized vector and runs **PCA →
  2-D server-side** (numpy SVD, zero new deps), returning only the
  coordinates + metadata. The payload is now O(n_docs × 2) regardless of
  embedding dimension, so the Graph renders at any vault size.
  - Remote mode uses the server-side PCA projection; **local mode keeps
    client-side UMAP** (small demo vaults, no transfer cost). The Graph
    page dispatches via a new `load_graph_projection()` loader and labels
    which projection is in use.
  - Coverage: server tests for `memory_export_coords` + `_pca_2d`
    (collapse, invalidated-doc skip, cap, cluster separation) and dashboard
    AppTests for both the remote-PCA render path and a remote-coords
    failure-degrade path.

## [0.7.0rc17] - 2026-06-07

### Added / Fixed
- **Every dashboard page now degrades gracefully on a remote-call failure**
  (comprehensive UI resilience). Added a shared `remote_guard()` context
  manager in `loaders.py` and wrapped every page's top-level loader call
  (Home, Search, Timeline, Graph, Profile): a slow/cold/unreachable remote
  now yields a clean `st.error` + `st.stop` instead of an unhandled
  `ExceptionGroup` traceback. (rc16 fixed only Graph; this generalizes it.)
  The guard passes Streamlit's own control-flow exceptions (`st.stop` /
  `st.rerun`) through untouched. **Every page now has both a success-render
  AppTest and a loader-failure AppTest** (13 dashboard tests) — so the UI
  is comprehensively in the suite for both render and failure paths.

## [0.7.0rc16] - 2026-06-07

### Fixed
- **Dashboard Graph page crash when the vector export fails.**
  `memory_export_vectors` is the heaviest remote call (it exports every
  vector); on a large or cold remote it can time out / fail at the
  transport layer (surfacing as an unhandled anyio `ExceptionGroup`),
  which crashed the Graph page with a raw traceback. The page now wraps
  the load in try/except → a clear, actionable `st.error` + `st.stop`
  (retry / run `mnemon doctor`). Covered by a new loader-*failure*-path
  AppTest (the success-mocked render tests didn't exercise it).

## [0.7.0rc15] - 2026-06-07

### Added
- **Dashboard render-smoke tests** (`tests/test_dashboard_pages.py`) — the
  UI is now folded into the test suite. Streamlit's headless `AppTest`
  renders every page (Home, Search, Timeline, Graph, Profile) against
  **remote-shaped** mocked loader output and asserts no exception. This
  catches the local-vs-remote serialization `KeyError` class (the rc14
  `recency_score` crash) automatically in CI — including an explicit case
  for an older remote that omits `recency_score`. `streamlit` + `plotly`
  added to `[dev]`; the tests `importorskip` so the `[server]`-only job
  skips them cleanly. (Dashboard stays out of the coverage denominator —
  the value is crash detection on every page render, not a coverage %.)

## [0.7.0rc14] - 2026-06-07

### Fixed
- **Dashboard Search page crash (`KeyError: 'recency_score'`).** The
  dashboard's remote path renders `memory_search` JSON, but that tool
  hand-built its result dict and omitted `recency_score` (the local
  `dataclasses.asdict` path included it). `memory_search` now serializes
  `recency_score` too (additive), and the Search page reads score fields
  with `.get(..., 0.0)` so it also tolerates an older remote (e.g. a Fly
  app on a pre-`recency_score` mnemon) that doesn't send it. Caught
  during the dashboard manual smoke. (`memory_search` was the only tool
  using a hand-picked dict; the others use `asdict`, so no other page is
  affected.)

## [0.7.0rc13] - 2026-06-07

### Changed
- **Clarified connector-setup wording** (operator-caught during live
  validation). The `upgrade web` summary, `validate_cross_device.sh`
  output, and the README cross-device section now say: add the connector
  with a **name + URL only — leave Client ID/Secret blank** (the
  self-hosted AS registers clients via DCR), and the **passphrase is the
  OAuth login prompt shown on Connect, not a connector field**. Also notes
  to disable a co-enabled prod connector during a test so save/search
  routes to the intended app (both expose the same tool names).

## [0.7.0rc12] - 2026-06-07

### Added
- **`scripts/validate_cross_device.sh`** — scripts the cross-device
  live-validation: deploys a throwaway Fly app from the current version,
  asserts the auto-provisioned OAuth AS serves correct RFC-8414 metadata
  (`mnemon doctor --fail-on-warn`, issuer match), surfaces the generated
  passphrase + connector URL for the one manual claude.ai-connector step,
  then tears down. Safety mirrors `promote_stable.sh layer3`: a prod-app
  guard, fully isolated test state, and an EXIT-trap that restores
  `~/.mnemon/{remote_url,local_token,as_passphrase}` even on Ctrl-C.
  `--dry-run` prints the plan (no side effects); `--keep` leaves the app
  up for unhurried browser testing. CI-smoke-tested in
  `tests/test_validate_cross_device.py` (syntax, dry-run plan, prod guard).

### Fixed
- **`promote_stable.sh layer3` now snapshots/restores `as_passphrase`.**
  rc10+ `upgrade web` writes `~/.mnemon/as_passphrase`; layer3 previously
  only snapshotted `remote_url`/`local_token`, so a layer3 run would leave
  the operator's prod passphrase as the destroyed test app's.

## [0.7.0rc11] - 2026-06-07

Auth-completeness + CI hardening — toward rock-solid.

### Added
- **Redeploy path back-fills the OAuth AS.** rc10 auto-provisioned the
  AS on *first-time* deploys; apps deployed earlier wouldn't gain it on
  a plain `mnemon upgrade web` redeploy (Fly secrets are first-time-only).
  Now `_redeploy_web` checks `flyctl secrets list` and, only if
  `MNEMON_AS_PASSPHRASE` is absent, generates + sets the AS secrets and
  surfaces the passphrase. It never rotates an existing passphrase (that
  would invalidate issued tokens) and never acts when the secret list
  can't be read. So **every** deploy path now provisions browser-client
  auth.
- **CI runs on macOS too.** The test matrix adds `macos-latest` (one
  Python version) alongside the Linux × {3.10, 3.12, 3.13} grid, to
  catch Mac-specific regressions on the primary dev platform. Windows is
  explicitly scoped out (unix-only deploy paths).

### Fixed
- CI comment referenced the old 80% coverage floor; the gate is 88%.

## [0.7.0rc10] - 2026-06-07

Cross-device is now the **default** path, and it's turnkey.

### Added
- **`mnemon upgrade web` auto-provisions the self-hosted OAuth AS.** Previously `upgrade web` set up a bearer token (headless clients: Claude Code, Cursor) but left the OAuth Authorization Server — which the **browser** clients (claude.ai web, Claude Desktop, mobile) need — to manual `flyctl secrets` setup. So the cross-device path, mnemon's headline feature, wasn't actually turnkey. Now first-time deploys generate a passphrase and set `MNEMON_AS_ENABLED` + `MNEMON_AS_PASSPHRASE` + `MNEMON_PUBLIC_URL` automatically (the AS keypair already persists on the Fly volume at `/data/oauth_keys`). The passphrase is surfaced in the deploy summary and saved 0600 to `~/.mnemon/as_passphrase` — it's the operator's claude.ai/Desktop login.

### Changed
- **README leads with cross-device as the default**; local-only is demoted to a "quick demo" you elect *after* seeing the full setup. Reflects that the whole reason to run mnemon (vs. Claude's siloed native memory) is the cross-client vault.

## [0.7.0rc9] - 2026-06-07

Continuing the 0.7 rc cycle — hardening toward a `0.7.0` stable that is
genuinely rock-solid before the version drops the `rc`. (Not stable yet;
the `status: alpha` posture stands.)

First prerelease to actually ship the post-rc8 work below: rc8 was the
last published artifact, and the two commits after it (#193, #194) merged
to `main` without a version bump, so their PyPI publishes skip-existing'd
as rc8. This rc carries them.

### Fixed (release plumbing)
- **Version is now single-sourced** from `src/mnemon/__init__.py` via
  `[tool.hatch.version]` (`dynamic = ["version"]`). Previously the version
  lived in BOTH `pyproject.toml` (static) and `__init__.py`; they drifted
  in the 0.7.0 attempt (init→0.7.0, pyproject→rc8) and the build published
  the wrong version. One bump site now; they can't disagree.

### Added
- **`mnemon verify-sharing`** — write+read-back a sentinel against the
  configured remote (proves CLI↔remote), then print the exact search term
  to confirm another client (Claude Desktop / claude.ai) is wired to the
  same vault. `--cleanup` removes sentinels. Turns the recurring "is the
  vault actually shared?" question into a deterministic check.
- **`doctor` two-vaults shadow guard** (`check_no_shadow_local_vault`,
  remote mode) — warns if a *populated* local `default.sqlite` is
  shadowing the remote (the trap that twice served stale local reads),
  with the archive recipe. Empty stub / absent passes.

### Changed / internal
- **Comprehensive test coverage pass** — all 17 MCP tools now have a
  direct server-surface test, and the previously-unmeasured local↔web
  migration (`upgrade.py` / `downgrade.py`) is un-omitted and covered.
  Suite ~1016 → 1069; coverage gate raised **80% → 88%** to lock it in.

## [0.7.0rc8] - 2026-06-04

### Fix: local vault is inaccessible in remote mode (two-vaults bug, residual)

PR #188 (rc7) made `mnemon serve` proxy to the remote, but every *other*
default-vault open — the local-vault CLI commands (`rebuild` / `forget` /
`standing` / `doctor` / `sync`), the dashboard, and the api — still opened,
and would **re-create**, a local `~/.mnemon/default.sqlite` when a remote
vault is configured. That silently resurrects a second, divergent source of
truth (the same two-vaults trap, now via a freshly-created empty vault).

The `Store` constructor now refuses to open the **default** local vault in
remote mode (`MNEMON_REMOTE_URL` / `~/.mnemon/remote_url`) and fails loud with
`LocalVaultInaccessibleError`, instead of silently serving/creating a local
vault. Exempt: an explicit `db_path` (tests, migrations), the `serve-remote`
server (it *is* the vault), and `MNEMON_ALLOW_LOCAL_STORE=1` for genuine local
maintenance. `remote_mode_active()` is lifted to `hooks._remote_client` as the
shared chokepoint for both the CLI router and the Store guard.

Closes the residual hole behind the principle: **if a cloud vault exists, the
local vault must be inaccessible** — a second reachable source of truth is a
silent-divergence trap.

## [0.7.0rc7] - 2026-06-04

### Fix: `mnemon serve` honors remote-vault mode (two-vaults bug)

When a remote vault was configured (`MNEMON_REMOTE_URL` env or
`~/.mnemon/remote_url` file), the CLI read/write commands (`status`,
`search`, `save`) routed to the remote — but `mnemon serve` did not: it
unconditionally opened the local SQLite store. A machine pointed at a
cloud vault therefore exposed **two** connected MCP servers backed by
**different** data, the local one a stale near-empty vault. Reads/writes
through it silently diverged from the source of truth.

- **New `mnemon.server_proxy`** — a stdio MCP server that mirrors the
  exact tool surface of `mnemon.server` (every tool wrapped via
  `functools.wraps`, so names/docstrings/schemas are identical and can't
  drift) and forwards each call to the remote vault via
  `hooks._remote_client.call_tool_sync`. The local store is never opened
  in remote mode. Fail-loud: remote/network/auth errors propagate to the
  MCP client rather than degrading to the local vault.
- **`serve` dispatch** now checks `_remote_mode_active()` and runs the
  proxy in remote mode, `run_stdio` (local) otherwise — mirroring the
  existing read/write routing.
- `tests/test_server_proxy.py` asserts tool name/schema/description
  parity between the two servers + forwarding + fail-loud behavior.

## [0.7.0rc6] - 2026-05-27

### Phase 2 / 3 salience tier + Phase B / C capture-attention substrate

Substantial roadmap-closure release consolidating the 2026-05-27 sweep.
Every change is gated default-off or operator-explicit — Phase A
capture-attention auto-firing stays default-off pending the re-soak
gate (see "Re-soak prereq" below).

**Salience tier**
- **Phase 2 promotion signals** (#178): new `documents.correction_count`
  + `documents.contradiction_win_count` columns + `Store.salience_report`
  + `mnemon salience-report` CLI. Bumps on operator `correction_of=`
  gestures and NLI contradiction-win events respectively.
- **Phase 3 observability** (#179): new `documents.last_injected_at`
  column; `Store.list_standing` bumps it on every injection event;
  `mnemon standing list` renders aging table with ⚠ stale marker
  for members not injected in ≥90d.
- **Vault-derived auto-exemplars** in `scripts/build_standing_set.py`
  (#168): `--exemplar-source {hybrid, vault, hand-tuned}`. Default
  hybrid; samples high-confidence preference/decision/antipattern
  as positives and recent handoffs as negatives.
- **LLM-judge opt-in** (#175): `--judge anthropic`, requires
  `ANTHROPIC_API_KEY` + `pip install anthropic`. 4-dim rubric.
  Default `--judge embedding` unchanged.
- **`memory_promote` coherence check** (#173): post-promote NLI
  bidirectional classification against existing standing-tier
  members; conflicts surface as a warning (not blocking — NLI
  false-neg on numeric updates is a known limitation).

**Capture attention**
- **Phase B access-count feedback loop** (#177):
  `documents.access_count` now increments on every `memory_search`
  hit; new `Store.attention_report` ranks by access × recency;
  `mnemon attention-report` CLI.
- **Phase C operator-reviewed consolidation** (#183):
  `Store.find_clusters` + `mnemon consolidate [--apply <idx>]` with
  y/N confirmation gate. Operator-review only per plan invariant.
- **Retroactive contradiction sweep** (#180):
  `contradiction.sweep_contradictions` + `mnemon sweep-contradictions
  [--max-pairs N] [--dry-run]`. Closes the save-time miss gap.

**`memory_save` / explicit supersession**
- **`correction_of` is now a structural relation** (#171). When set,
  `Store.save` inserts `'supersedes'` (new → target) + bumps the
  target's `correction_count`. Raises `ValueError` on missing target.
  `memory_save` MCP tool exposes the parameter.

**`mnemon` CLI**
- **`status` / `search` / `save` honor remote mode** (#176). When
  `MNEMON_REMOTE_URL` is set, routes through `call_tool_sync` to
  the remote vault. Closes the 2026-05-21 Layer-3 silent-fallback gap.
- **`attention-status --strict`** (#167) exits 1 when boost-rate >
  ceiling — for periodic health-check wiring.

**Operator tooling**
- **`scripts/mnemon_ops.sh`** (#172): `cleanup-test-apps`,
  `recover-token`, `restart-machine`, `vault-stats`,
  `changelog-extract`.
- **`scripts/` smoke-test CI** (#170): pytest parametrized over
  `scripts/*.py --help`.

**Release engineering**
- **TestPyPI integration** (#181): `mnemon upgrade web --testpypi`
  + `promote_stable.sh testpublish` subcommand. Enables true
  pre-publish validation of candidate code rather than the
  latest-published proxy.
- **`promote_stable.sh` harness expansion** (#182):
  `MNEMON_VENV_BIN` env-var override; trap destroy retries + stderr
  capture; step-2 `remote_url` isolation regression test.
- **Drop `_fly_dump_vault` inline-Python script** (#169) —
  `mnemon sync push` is now the canonical primitive.

**Polish + fixes**
- `context_surfacing` balances dangling `**` mid-bold truncation
  (#167).
- `scripts/calibrate_capture_threshold.py --use-fixture` falls back
  to `.example.json` on fresh clones (#167).
- NLI cache resolution docs in Dockerfile + nli.py (#167).
- `cli.py` coverage 62% → 85% (#174).
- README cross-client recall guidance (#163).

### Re-soak prereq

`CAPTURE_ATTENTION_ENABLED` stays default-off in this rc. Operator-
side workflow to start the Phase A re-soak:

1. Publish rc6 to PyPI (`twine upload`).
2. `mnemon upgrade web --app-name mnemon-memory --mnemon-version 0.7.0rc6`.
3. Verify `mnemon doctor` 7/7 green.
4. `flyctl ssh console -a mnemon-memory -C 'mnemon attention-status'`
   — confirm `Flag enabled: False`, baseline boost-rate.
5. `flyctl secrets set MNEMON_CAPTURE_ATTENTION_ENABLED=true` —
   starts the re-soak clock.
6. Soak ≥1 week; pass condition `boost_rate ≤ 0.25` per the ROADMAP
   gate.

Suite 875 → 996 across the sweep (+121 tests).

## [0.7.0rc5] - 2026-05-27

### Salience tier — Phase 1 default-on

- **`config.STANDING_TIER_ENABLED` flipped to `True`.** Phase 1
  standing-tier soak passed: activated 2026-05-22 with 3 promoted
  memories (#2543 composite runway / #2401 recruiter posture / #2084
  severance), observed ~5 days with no runway-style under-weighting
  recurrence. Standing-tier injection is now on by default; operators
  who want to opt out can still set `MNEMON_STANDING_TIER_ENABLED=0`.
  Note: capture-attention Phase A (`CAPTURE_ATTENTION_ENABLED`) stays
  default-off — its soak surfaced an over-firing defect (boost-rate
  0.714 vs 0.25 ceiling); the candidate-filter half of that fix lands
  in the section below; a live-traffic threshold re-calibration + fresh
  ≥1 week re-soak gate the eventual default-on flip.

### Capture-attention — hook-source provenance gate

- **`Store.apply_capture_attention()` early-returns when the saving
  doc's `source_client` is in `HOOK_SOURCE_CLIENTS`.** Mirrors the
  existing Layer 4 demotion + `STANDING_TIER_BLOCKED_SOURCE_CLIENTS`
  policy: the same provenance set that's capped at
  `HOOK_SOURCE_CONFIDENCE_CEILING` at save and demoted by
  `PROVENANCE_DEMOTION_FACTOR` at recall — and forbidden from
  standing-tier promotion — also cannot drive capture-attention
  boosts. Surfaced 2026-05-27 by the Phase A soak: boost-rate hit
  232/325 = 0.714 vs the documented 0.25 ceiling, with canonicals
  like "Session: pr merged, continue" — session_extractor hook output
  self-boosting. The gate restores the mechanism to its intent
  (consolidate operator-authored signal, not session noise).
- **2 new regression tests** (`TestHookSourcedSaveSkipped`):
  hook-sourced saves must not emit `'restates'` relations or
  increment `recurrence_count` on neighbors; user-authored saves
  with hook-sourced neighbors continue to fire (the defense is
  one-sided on purpose — consolidating operator signal against
  hook-source echoes is still valid).

## [0.7.0rc4] - 2026-05-24

### Capture-attention Phase A — activation infrastructure

- **New `MNEMON_CAPTURE_ATTENTION_ENABLED` env-var override** on the
  Phase A feature flag. Mirrors the standing-tier pattern
  (`MNEMON_STANDING_TIER_ENABLED`) — operators can flip activation on
  Fly via `flyctl secrets set` without a code change + redeploy, and
  the next save picks it up without restarting the server. Accepts
  `1`/`true`/`yes`/`on` (truthy) or `0`/`false`/`no`/`off` (falsy);
  unset / unrecognized falls back to `config.CAPTURE_ATTENTION_ENABLED`
  (still default `False` through soak). New
  `store._capture_attention_enabled()` helper called at request time
  from `Store.save` and `cli attention-status`. 5 new tests.
- **`mnemon attention-status` now reports the effective flag value**
  with the env-var override applied — a Fly secret flip shows up here
  immediately instead of misleading the operator with the unchanged
  config default.

### Calibration fixture privacy hardening

- **`tests/fixtures/capture_attention_pairs.json` is now gitignored.**
  PR #153 shipped this path tracked with a placeholder schema —
  intended as a seed, but every operator calibration run overwrites
  it with real vault titles + snippets (personal context, in-flight
  work, etc.) that must not land in a public-repo commit. The
  placeholder schema moves to
  `tests/fixtures/capture_attention_pairs.example.json` (tracked) so
  future contributors still see the format; the operator output stays
  local-only.

### Calibration script fixes (`scripts/calibrate_capture_threshold.py`)

- **`VecStore.get(vec_id) -> np.ndarray | None`** added — mirrors the
  `has` / `delete` single-id shape; returns a defensive copy. The
  calibration script's `vs.get(vec_id)` call site failed on first
  invocation because the method did not exist. 3 new tests (returns
  vector, missing → None, defensive-copy invariant).
- **Near-neighbor pair sampling** replaces uniform-random. The previous
  random sample across a 2510-memory vault produced pair cosines
  clustered at 0.1-0.4 (clearly-different topics) — operator verdicts
  carried no information about whether the threshold cut should be
  0.80 or 0.85. New sampler picks anchors, takes each one's top
  non-self neighbor above cosine 0.55 (well below the lowest
  calibration threshold so edge-negatives survive), and sorts
  descending so the operator tags high-confidence near-dupes first.
  Verified against the 2026-05-24 prod snapshot: 20-pair sample spans
  cosine 0.751-0.999, entirely in the calibration-relevant range.
  Calibration on that snapshot recommended
  `CAPTURE_ATTENTION_THRESHOLD = 0.85` — matches the existing default,
  so no config change needed.

## [0.7.0rc3] - 2026-05-22

### Test coverage

- **CI now enforces ≥80% test coverage.** `pyproject.toml` gains
  `[tool.coverage.run]` + `[tool.coverage.report]` config with
  `fail_under = 80`; `ci.yml` runs `pytest --cov` so a PR that drops
  coverage below the floor fails the build. Excluded modules
  (`dashboard/*`, `__main__.py`, `upgrade.py`, `downgrade.py`,
  `llm.py`) are under-testable-by-design and documented in the
  config — Streamlit UI / entry-point shim / release-engineering
  scripts requiring real Fly+AWS / deprecated optional-LLM module
  the deployed product doesn't use.
- **Current coverage: 86%** (suite 850 → 855 passing).
- **README coverage badge** added: `coverage-86%-brightgreen`.
  Static, manually updated on each release (matches the existing
  static-badge pattern for Status / Python / License / MCP).
- New `tests/test_nli.py` additions cover: `_ensure_loaded` HF
  download failure → `NLIUnavailableError`; `_ensure_loaded`
  unexpected label-set rejection; `prewarm()` swallows
  unavailability per acceptable-secondary-observability category;
  `classify_pair` softmax + input-building path with stubbed session.

### CI / release tooling

- **New `.github/workflows/ci-server-extras.yml` workflow.** Installs
  `mnemon-memory[server]` ONLY (the production-equivalent install
  used by the Fly Docker image) plus pytest as a separate test
  runner, and runs the full suite under that minimal install. Catches
  the failure class that bit `memory_check_contradictions` on
  2026-05-22 — production code that imports something from `[llm]` /
  `[ui]` would pass `ci.yml` (full `[dev]` extras installed) but
  fail this workflow. Includes a guard assertion that
  `llama-cpp-python` is NOT installed under `[server]` — so a future
  PR can't accidentally move it across without flipping the
  intentional "mnemon is LLM-free by default" posture.

- **`scripts/promote_stable.sh layer3 --exercise-all-tools`.** New
  opt-in flag that, after the test Fly app is up but before the
  downgrade step, iterates every registered MCP tool against the
  remote and asserts each returns cleanly (no opaque error envelope,
  no unhandled exception, no NLI/embedder/baked-model breakage).
  Composes with `tests/test_tools_integration.py` (PR #158, local-
  process Python-level canary): this Fly-level probe catches the
  failure modes the local canary can't see (missing baked models,
  Anthropic MCP proxy timeouts, transport regressions). Tool list
  resolved dynamically from `mcp._tool_manager._tools` so tools
  added in future PRs are exercised automatically. Adds ~30-60s to
  the layer3 run; opt-in so non-NLI-touching releases aren't taxed.

- **`scripts/_layer3_remote_helper.py`** gains an `exercise-all-tools`
  subcommand wired through the FastMCP tool manager. Two regression-
  lock tests added to `tests/test_promote_stable.sh` harness (15
  passing, was 13) covering helper dispatch + flag plumbing through
  the bash dispatcher.

## [0.7.0rc2] - 2026-05-22

### Features

- **Contradiction detection rebuilt with NLI (no LLM dep).** The
  `memory_check_contradictions` MCP tool now uses a Natural Language
  Inference cross-encoder (`cross-encoder/nli-deberta-v3-xsmall`,
  22M params, ~87 MB INT8 ONNX) instead of an LLM classifier. NLI
  is the canonical non-LLM ML primitive for this exact task —
  entailment / contradiction / neutral classification on a sentence
  pair — and ships through the same FastEmbed-style ONNX runtime
  path already in mnemon. **Zero new dependencies** (onnxruntime +
  tokenizers + huggingface_hub all transitively required by
  FastEmbed already). Replaces the prior LLM-based path that
  couldn't work on Fly (`[server]` extras don't install
  `llama-cpp-python` per the 2026-05-21 "mnemon is LLM-free by
  design" decision, so the LLM path was effectively broken since
  the original `[server]`/`[llm]` split).
  - **Bidirectional classification.** Each candidate pair is run
    through the cross-encoder twice (premise→hypothesis +
    hypothesis→premise, ~10-20ms total on CPU INT8). The two
    directions disambiguate the mnemon taxonomy: both entail →
    `same`; new entails old but not vice versa → `update`;
    contradiction in either direction → `contradiction`; both
    neutral → `unrelated`.
  - **Cosine gate preserved.** Existing
    `CONTRADICTION_OVERLAP_THRESHOLD=0.7` still filters candidates
    before NLI — protects against the rare NLI false-positive on
    obviously-unrelated pairs.
  - **Model baked into Fly image.** Dockerfile downloads the 87 MB
    quantized ONNX model + tokenizer at build time, mirroring the
    existing FastEmbed bake. Cold start adds the NLI load to the
    pre-warm path (~5-8 seconds total vs 3-5 seconds prior). Health
    check start period bumped 30s → 45s.
  - **Clean error surface.** When NLI isn't loadable (e.g., model
    download fails on a fresh local install without network), the
    MCP tool returns a clear "skipped — NLI classifier unavailable"
    message instead of an opaque "Error occurred during tool
    execution" envelope. Fail-loud per
    `feedback_no_silent_fails`. Composes with the recalled
    `feedback-mnemon-pypi-upload-claude-is-authorized` mental model:
    surface failure causes specifically, never the generic envelope.

- **`dry_run` parameter on `memory_check_contradictions`.** When
  `dry_run=True`, the tool reports what WOULD have decayed without
  applying any mutations (no confidence changes, no relations
  inserted). Closes the read/command-separation violation in the
  prior `check_*` naming; useful for operator audit before
  committing destructive changes (the 2026-05-22 standing-tier
  promotion incident — operator review of three contradictory
  liquidity figures — would have benefited from this).

### Internal

- **New module `src/mnemon/nli.py`** mirroring `embedder.py`:
  lazy-loaded singleton, `prewarm()` for lifespan startup,
  `classify_pair()` for single-direction, `classify_pair_bidirectional()`
  for the mnemon-taxonomy mapping, `is_available()` probe,
  `NLIUnavailableError` named exception. Operator override:
  `MNEMON_NLI_ONNX_VARIANT` env var to swap between FP32 / FP16 /
  INT8 variants (default INT8 AVX-512 for x86 Fly).
- **`contradiction.py` refactored**: LLM imports + prompt
  construction removed; vector gate + NLI classify pipeline now
  explicit in the docstring. Return shape gains `nli_unavailable`
  and `dry_run` flags for caller-side handling.
- **Tests**: `tests/test_nli.py` (11 new) covers bidirectional
  label mapping, error surfacing, availability probe.
  `tests/test_contradiction.py` refactored to mock the NLI layer
  instead of `mnemon.llm.generate`; adds dry-run mutation-skip
  test + nli-unavailable clean-flag test.

- **`tests/test_tools_integration.py` (3 new) — every-MCP-tool
  round-trip canary.** Closes the unit-test-coverage gap that let
  `memory_check_contradictions` ship to Fly with a hidden bug
  (mocked unit tests passed; the real call path raised an opaque
  envelope client-side). Iterates the entire registered tool
  manager, invokes each tool with minimal-valid inputs against a
  seeded vault, asserts no unhandled exception + sane return shape
  + no opaque-error strings in outputs. Per-tool fixture entries
  enforce that future tools can't ship without being exercised
  here (registered-tools-vs-fixture-keys diff check). Stubs the
  heavy external calls (NLI classify, FastEmbed re-embed) so the
  full suite stays ~17s. Composes with the existing
  `test_server.py` per-tool unit tests (mocked deps, isolated
  contracts) — that suite covers logic; this suite covers
  no-exception-escapes-the-boundary. Suite 836 → 850 passing.

## [0.7.0rc1] - 2026-05-22

### Fixes

- **`build_standing_set.py` exemplar bias — added declarative-posture
  patterns.** The pre-fix `CONSTRAINT_EXEMPLARS` list leaned heavily
  imperative ("never," "always," "must," "default to"). Surfaced
  2026-05-22: against the real prod vault the auto-selected top-10
  was 100% engineering rules — career / lifestyle / posture
  constraints spanning multi-year load-bearing facts (runway,
  recruiter posture, start-date framing, job-search mode) did not
  surface despite being equally durable, because the user encodes
  them declaratively ("Brian's stance," "current preference,"
  "passive/selective mode") rather than imperatively. Added 10
  declarative-posture exemplars representing the same constraint
  class in declarative shape. Exemplar list 22 → 30; imperative /
  declarative split now roughly balanced. ROADMAP audit-finding
  follow-up per `feedback_audit_findings_become_roadmap_followups`.
  Operator should re-run `scripts/salience_phase0.sh snapshot &&
  scripts/salience_phase0.sh score` to verify the bias fix surfaces
  career-context memories alongside the engineering rules in the
  top-10.

### Features

- **Salience tier Phase 1 — first-class standing-context recall
  (default-off, soak-gated).** Memories explicitly promoted via
  `memory_promote` are injected into every `<mnemon-context>`
  envelope on every prompt, regardless of query similarity. The cap
  is the contract: default 15, hard ceiling 20. Plan:
  `private/mnemon-salience-tier-plan-260521.md`.
  - **Reframed validation gate (2026-05-22).** Phase 1 IS the
    validation. Earlier plan called for a synthetic A/B against the
    Phase 0 env-var-flagged form before committing to schema +
    tooling. Reframed because the injection mechanism is identical
    between the two forms — an A/B of the gated env-var path
    carries no marginal information once Phase 1 ships gated behind
    `STANDING_TIER_ENABLED=false`. Operator promotes ~5 career-
    context memories, flips the flag, observes ≥1 week soak for
    runway-style under-weighting recurrence vs absence. Per
    `feedback_phase_gated_soak_consumer_must_be_ready`: ship the
    substrate gated, flip activation at a separate milestone.
  - **Schema migration**: `documents.tier TEXT NOT NULL DEFAULT
    'situational'` via `_migrate_tier()`. Index `idx_documents_tier`
    on live rows for the cap-count probe + search exclusion filter.
    Additive + harmless if `STANDING_TIER_ENABLED` stays off.
  - **`Store.promote_to_standing(id)`** + **`demote_to_situational(id)`**
    + **`list_standing()`** + **`standing_tier_status()`**. Promote
    raises **`StandingTierCapReached`** at the runtime cap,
    **`StandingTierProvenanceRejected`** when source_client is in
    `STANDING_TIER_BLOCKED_SOURCE_CLIENTS` (Layer 4 composition —
    hook-sourced memories cannot be promoted; operator-explicit
    gesture only), and **`StandingTierError`** on missing /
    invalidated docs. Idempotent re-promote returns True; demote
    of a situational doc returns False (no-op).
  - **`Store.search_bm25` + `Store.search_vector`** gain
    `include_standing: bool = False` keyword param. Standing-tier
    docs excluded from ranked retrieval by default — they're
    injected unconditionally already; ranking them too would
    double-count and crowd the situational signal. Threaded through
    `search.search()` so the higher-level entry respects the
    invariant.
  - **MCP tools**: `memory_promote(id)`, `memory_demote(id)`,
    `memory_list_standing()` — both stdio (`server.py`) and
    Streamable HTTP (`server_remote.py` reuses the same `mcp`
    object). 14 → 17 registered tools.
  - **CLI**: `mnemon standing list / promote <id> / demote <id>`.
    `mnemon status` gains a `Standing tier: N/CAP` line.
  - **`build_context` integration**: when `STANDING_TIER_ENABLED`
    (config constant OR `MNEMON_STANDING_TIER_ENABLED` env override,
    accepting `1/true/yes/on`), build_context calls
    `memory_list_standing` via the remote client in a single
    round-trip and renders the result as the "Standing context"
    sub-section ahead of "Situational recall." Phase 0 env-var path
    (`MNEMON_STANDING_TIER_FILE` → standing.json → standing-rendered.md
    cache) is **preserved as fallback** so operators retain a
    per-session override mechanism.
  - **Composability invariants** (all preserved):
    - Layer 0 (`is_well_shaped`) — capture rejection runs before
      anything reaches the standing-tier promotion path
    - Layer 1 envelope — standing block sits inside the same
      `<mnemon-context>` data-marking + nonce as situational
    - Layer 4 (`HOOK_SOURCE_CONFIDENCE_CEILING` + provenance) —
      hook-sourced memories cannot be promoted; explicit
      `StandingTierProvenanceRejected` rejection
    - rc16 `source_key` upsert — unchanged; tier orthogonal
    - Capture attention Phase A — `recurrence_count` accretes
      against canonical situational memories; standing-tier
      promotion is operator-gated on top of that signal
  - **Soak gates** for flipping default-on: (a) ≥1 week with the
    flag on; (b) observed reduction in runway-style under-weighting
    recurrence on real career-strategy conversations; (c) zero
    spurious-injection complaints from operator review of every
    promoted memory.
  - 22 new tests in `tests/test_standing_tier.py` covering: promote
    success / cap-rejection (cap=2 in test, 3rd raises) /
    hook-sourced rejection / invalidated rejection / missing rejection
    / cap respects invalidated (freed slot reclaimable) / demote
    round-trip / demote idempotent on situational / demote frees cap
    slot / list_standing ordering + content / search excludes by
    default / search includes when requested / build_context
    flag-off no-fetch / flag-on memory_list_standing call /
    env-var truthy value parsing. Suite 814 → 836 passing
    (`test_server_remote.py` tool-count assertions bumped 14 → 17).

### Schema

- **`documents.tier TEXT NOT NULL DEFAULT 'situational'`** —
  additive migration in `_migrate_tier()` after the existing
  `_migrate_recurrence_count`. Index `idx_documents_tier` scoped to
  live rows for cap-count + search-filter queries.

- **Capture attention Phase A — recurrence-weighted memory convergence
  (default-off, soak-gated).** When a new save's content is semantically
  close to ≥2 prior memories spanning distinct sessions, capture
  attention preserves the new memory + inserts `'restates'` relations
  to each cluster member + boosts the canonical neighbor's confidence
  + increments the canonical's new `recurrence_count` column. The
  cluster of restatements stays discoverable; the load-bearing signal
  accretes on the canonical; MMR diversity at recall naturally
  suppresses near-duplicates without us dropping them at capture.
  Plan: `private/mnemon-capture-attention-plan-260522.md`. Driver: the
  2026-05-22 finding that load-bearing facts stated across many
  sessions land as fragmented memories rather than a single canonical
  assertion (the operator was implicitly substituting for a missing
  mechanism).
  - **SOTA invariant: preserve+relate+boost, never skip-the-save.**
    Earlier draft considered "boost canonical + skip the new save"
    as the auto-apply path — rejected because each restatement
    carries different framing and discarding it throws away the very
    signal the recurrence detector is honoring. The institutional
    pattern is preserve the data, link via relations, accrete the
    importance signal — operator-reviewed merge is Phase C of the
    plan, not Phase A's job.
  - **Embedding-only (no LLM dependency).** Same SOTA-for-public-
    release-constraint logic that drove `build_standing_set.py`'s
    embedding-based scorer (the roadmapped LLM-judge opt-in P2 item
    composes as an advanced mode but isn't required).
  - **Feature flag `CAPTURE_ATTENTION_ENABLED` default-off** through
    soak. Two acceptance criteria to flip default-on (per plan
    §"Soak acceptance criteria"): (1) `boost_rate ≤ 0.25` over a 7-day
    window measured via `mnemon attention-status`; (2) ≥80% precision
    on a 20-canonical manual review.
  - **`correction_of` parameter on `Store.save()`** (forward-compat
    for salience-tier Phase 2 promotion signals). When set, capture
    attention is skipped — operator explicit gesture beats automated
    recurrence detection.
  - **`mnemon attention-status` CLI** — soak monitor: boost-rate
    ratio over 7 days, recurrence-count distribution, top-10
    canonicals, last-10 `'restates'` relations audit trail.
  - **`scripts/calibrate_capture_threshold.py`** — data-tuned
    threshold selection. Samples N pairs from the operator's vault
    snapshot, prompts for same/different tagging, computes
    precision-recall at {0.70, 0.75, 0.80, 0.85, 0.90}, recommends
    the precision-leaning sweet spot. Persists tagged pairs to
    `tests/fixtures/capture_attention_pairs.json` for regression
    locking.
  - **Failure mode: named exception + WARN swallow.** Embedder /
    vecstore unavailability raises `CaptureAttentionUnavailableError`
    from `apply_capture_attention()`; `Store.save()` catches +
    `logger.warning`s + continues (the new memory is saved; only the
    recurrence-boost side effect is skipped). Acceptable swallow per
    `feedback_no_silent_fails` category (b) — secondary observability
    hung off a primary save path that records the failure.
  - **Composes with the existing layered defenses unchanged.**
    Capture attention runs AFTER Layer 0 (`is_well_shaped` rejects
    scaffolding before the path is reached) + AFTER Layer 4 ceiling
    (`HOOK_SOURCE_CONFIDENCE_CEILING` clamp survives the boost).
    `'restates'` is a new relation type — doesn't collide with the
    existing `'supersedes'` / `'contradicts'` / `'related'`.
  - 13 new tests in `tests/test_capture_attention.py` covering: the
    preserve-everything invariant, feature-flag-off no-behavior-
    change, distinct-sessions trigger, same-session no-trigger,
    threshold respected, hook ceiling, user uncapped, pinned-canonical
    selection, `correction_of` override, fail-loud on embedder
    unavailability, schema migration idempotency. Suite 801 → 814
    passing.

### Schema

- **`documents.recurrence_count INTEGER NOT NULL DEFAULT 0`** —
  additive migration in `_migrate_recurrence_count()`. Pre-existing
  rows get count=0 and recurrence detection starts forward from the
  next save. Harmless if `CAPTURE_ATTENTION_ENABLED` stays off.

## [0.6.0] - 2026-05-21

### Release

- **Promotion from `0.6.0rc18` to `0.6.0` stable.** Closes the rc cycle
  that ran from `0.6.0rc1` (2026-04-21, the simplification arc →
  two-product split) through `0.6.0rc18` (2026-05-18, the layered
  stored-injection defense). `0.6.0` is `rc18` plus several
  upgrade/downgrade-correctness fixes surfaced 2026-05-21 while
  exercising the pre-promote Layer-3 web test for the first time
  (see the Fixes section below).

### Fixes

- **`mnemon upgrade web` now forwards `MNEMON_S3_PREFIX` and
  `MNEMON_VAULT_NAME` to the Fly container** as secrets, so a
  non-default operator override propagates to the container's
  `mnemon sync pull` seed step. Previously the Fly side fell back
  to `sync.S3_PREFIX_DEFAULT` (`mnemon/vaults`) regardless of what
  the operator set locally — which broke the runbook's
  "test against an isolated S3 prefix" ritual. Not user-affecting
  for normal prod redeploys (both sides default identically); only
  affects ad-hoc test deploys where the operator overrides the
  prefix. Surfaced + fixed during the 0.6.0 Layer-3 attempt; 4
  regression tests cover the forwarding contract.

- **`mnemon downgrade local` now dumps the current Fly vault to S3
  before pulling.** Previously, downgrade did `S3 → local` only and
  silently skipped the `Fly → S3` step, so any memory added via
  remote between upgrade time and downgrade time was lost — the
  local vault was seeded from a stale S3 snapshot. For ad-hoc
  testing this manifested as "docs added post-upgrade missing
  after downgrade." For prod operators it would have been a quiet,
  severe data-loss bug: weeks of remote-added memories vanishing
  on the first `mnemon downgrade local` call. Now SSHes into the
  Fly machine and runs `mnemon sync push` before the local
  `mnemon sync pull` — mirror of `upgrade._fly_seed_vault` in the
  opposite direction. New `--skip-fly-push` flag as an operator
  escape hatch (e.g., when the Fly machine is unreachable);
  default behavior is to fail-loud if the dump SSH errors out,
  rather than silently fall through to the stale-pull data loss.
  5 regression tests cover the call order (`fly_dump → s3_pull`),
  the override flag, the fail-loud on SSH error, the
  custom-domain skip, and the SSH command shape.

- **`mnemon sync push` now uses SQLite's online-backup API as the
  canonical cross-host transfer primitive** (replaces an earlier
  WAL-checkpoint approach that was wrong cross-process). Raw
  `aws s3 cp default.sqlite` uploads only the main sqlite file's
  bytes — for short-lived CLI processes that's fine because SQLite
  auto-checkpoints WAL on connection close, but for long-running
  `mnemon serve-remote` the WAL accumulates indefinitely (default
  auto-checkpoint at 1000 pages). The natural-seeming fix —
  `PRAGMA wal_checkpoint(TRUNCATE)` from a transient connection — is
  silently broken when another process holds the connection open:
  it returns `(busy=0, total=0, checkpointed=0)`, reports success,
  flushes zero frames. Verified against a long-lived holder + 3
  commits: PRAGMA reported success, main file stayed at 8KB (just
  schema), no frames moved; `Connection.backup()` from the same
  position captured all 3 rows. `push()` now snapshots via the
  online-backup API to a transient `.sqlite.snapshot` file beside
  the source, `aws s3 cp`'s the snapshot, then removes it. The
  online-backup API uses SQLite's WAL-aware backup protocol and
  produces a consistent atomic snapshot even with concurrent
  writers. Vec store (`default.vec.npz`) is a binary numpy file with
  no SQLite semantics — uploaded directly. 6 regression tests
  cover the snapshot helper, the cross-process write capture, the
  source-is-read-only contract, the error-string contract for
  invalid sources, the snapshot-before-cp call order, the transient
  cleanup, and the vec.npz direct-upload path.

- **`mnemon downgrade local` (`_fly_dump_vault`) uses SQLite's
  online-backup API directly** for the version-skew bootstrap.
  When Layer-3 runs Pre-publish validation, the Fly container is
  pinned to the latest-published mnemon (e.g. `0.6.0rc18`) which
  predates the `sync.push` backup-API fix above — so SSHing
  `flyctl ssh -C "mnemon sync push"` would invoke the older
  broken push. To handle this version-skew, `_fly_dump_vault`
  SSHes a stdlib Python script that does its own `Connection.backup()`
  + `aws s3 cp` of `/data/default.sqlite` (plus `default.vec.npz`
  best-effort), independent of installed mnemon version. Once the
  Fly side is reliably on `0.6.0+`, this can simplify to
  `flyctl ssh -C "mnemon sync push"` and rely on the canonical
  primitive — tracked as a follow-up.

  The rc cycle delivered:
  - The simplification arc — mnemon local (stdio + single-file vault)
    and mnemon web (Fly + S3 backup) as one codebase, symmetric
    `upgrade web` / `downgrade local`, single source of truth
    invariant. (`rc1` → `rc7`.)
  - Runtime hardening from rc11-deploy observations: fresh-session
    deadlock fix (`json_response=True`), `_session_creation_lock`
    narrowing, periodic `expire_old()` + decay sweeps in the lifespan
    task, OAuth refresh-token rotation grace, warm-keeper +
    persistent sessions. (`rc8` → `rc14`.)
  - Auto-mirror discipline: shape gate (`is_well_shaped`) + confidence
    cap to keep transcript fragments from outranking deliberate
    user-authored memories; upsert-by-slug (`source_key`) to stop the
    multi-edit duplication pattern. (`rc15`, `rc16`.)
  - The five-layer stored-injection defense end-to-end: token defang
    allowlist (Layer 2), capture-time scaffolding rejection (Layer 0
    — root cause), provenance trust-tiering (Layer 4), spotlighting
    data envelope at recall (Layer 1, Claude Code path). (`rc17`,
    `rc18`.)

  `0.7.0` will open the salience-tier work — separating standing
  constraints (capped, unconditionally injected) from situational
  recall.

## [0.6.0rc18] - 2026-05-18

### Security

- **Layered hardening of the rc17 stored-injection fix.** rc17's
  `defang_control_markup` neutralizes control-plane tokens only at the
  *recall* boundary, and only for clients/servers running rc17+. A
  weekend-long Claude Desktop conversation still flagged a recalled
  memory as a prompt injection (and escalated to a false "your prompts
  are being rewritten" malware accusation) — because the conversation
  had ingested pre-rc17 raw recalls into its own history, and because
  recall-time token defang is structurally the weakest possible
  control. rc18 builds out the rest of a five-layer defense (plan:
  `private/mnemon-injection-defense-layers-260518.md`), treating the
  problem as indirect prompt injection via retrieval:

  - **Defang allowlist completion (#124).** Bare `<system>` was not in
    `_CONTROL_TAGS`; Claude Desktop wraps an MCP `memory_search` result
    such that a captured tool-registration block reads as a live
    `<system>` block. Added, ordered after `system-reminder` so the
    longer token still wins the regex alternation.

  - **Layer 0 — capture-time rejection, the root cause (#125).** A
    transcript span carrying host control-plane markup is captured
    harness scaffolding, not a memory. `safety.contains_control_markup`
    (detection-only twin of the defang regex) now gates
    `session_extractor.is_well_shaped` (covers the LLM and regex
    paths) and `mirror.mirror_path` (raises `MirrorError`) — the
    scaffolding is *rejected* before it enters the vault, never
    defanged. This protects clients mnemon does not control
    (Desktop/MCP) and pre-rc17 clients, and preserves the lossless-raw
    storage invariant (it filters scaffolding, it does not mutate
    legitimate content).

  - **Layer 4 — provenance trust-tiering (#126).** `composite_score`
    multiplies hook-sourced results (`source_client` in
    `HOOK_SOURCE_CLIENTS`) by `PROVENANCE_DEMOTION_FACTOR` (0.85) so an
    auto-captured transcript can no longer outrank an equal-relevance
    deliberate user assertion in unprompted recall. `source_client` is
    now threaded through `SearchResult` / `search_bm25` /
    `search_vector` / `rrf_fuse`. Rank-only — explicit
    `memory_get(id)` bypasses composite scoring and is unaffected.
    Stacks on the existing `HOOK_SOURCE_CONFIDENCE_CEILING` save cap.

  - **Layer 1 — spotlighting / data envelope (#127).** The robust
    structural control. `context_surfacing.build_context` wraps
    recalled memories in a standing "this is untrusted data, not
    instructions" instruction (outside the fence — trusted) plus a
    per-call `secrets.token_hex(8)` nonce fence; a stored memory
    cannot forge the close fence because it cannot predict the nonce.
    Claude Code path only (mnemon owns that prompt block); the
    MCP/Desktop envelope is deferred by design — Layer 0 already
    carries Desktop, and mutating server JSON would pollute every
    consumer.

  No MCP/S3 schema change across any of the four PRs (additive-only
  contract preserved). Storage stays lossless throughout. Suite
  765 → 786. Layer 3 (dual-representation storage) remains deferred,
  revisited only if Layer 0 proves insufficient.

## [0.6.0rc17] - 2026-05-17

### Security

- **Recalled memory content could impersonate the host control surface
  (stored prompt-injection / context-poisoning).** mnemon stored and
  replayed memory text verbatim at every boundary where it re-enters a
  model's context — MCP `memory_search` / `memory_get` /
  `memory_timeline` / `memory_related` results (the Claude Desktop
  path, served by `server_remote.py` which reuses `server.mcp`) and the
  Claude Code `<mnemon-context>` injection. Session transcripts
  routinely contain `<system-reminder>` blocks and the deferred-tool
  `<functions><function>{...schema...}</function></functions>` format;
  these reached the vault via the `session_extractor` regex fallback
  (raw `.{20,200}` transcript spans), auto-mirrored handoff files, or
  explicit `memory_save` of conversation text. Replayed unneutralized,
  a recalled memory could be mis-parsed by a downstream model as a live
  system reminder, a tool registration, or could close mnemon's own
  `<mnemon-context>` wrapper early. No malicious iteration — mnemon was
  faithfully capturing increasingly realistic harness output and
  replaying it without defanging.

  Fix — `mnemon.safety.defang_control_markup` (new): an allowlist of
  control-plane tag *tokens* (`system-reminder`, `functions`,
  `function`, `mnemon-context`, the namespaced tool-call tags + bare
  forms) has its angle brackets swapped for guillemets `‹ ›` at every
  retrieval-tool emit boundary in `server.py`, plus defense-in-depth in
  the `context_surfacing` render path. Storage stays lossless (raw text
  in SQLite); only the model-facing copy is defanged, so every memory
  already in the vault is remediated with no migration. Scoped to the
  allowlist so ordinary XML/code in memories (`List<T>`,
  `<observation>`) is untouched. Idempotent. +11 safety tests + 4
  server emit-boundary tests; suite 754 → 765.

## [0.6.0rc16] - 2026-05-15

### Fixed

- **Auto-mirror re-inserted a new memory on every local-file edit
  instead of upserting by stable slug (P0).** A Claude Code
  auto-memory file's normal lifecycle is draft → refine →
  finalize-on-merge — often several edits within one session. The
  `mnemon-mirror` save path wrote a brand-new mnemon document on each
  edit rather than updating the one keyed by the file's slug, so a
  single intentional memory edited 3× became 3 near-identical docs
  (concrete: `reference-morning-signal-iam-decoupled` mirrored 3× on
  2026-05-15 — ids 2253/2256/2259). The at-save vector-overlap dedup
  did not catch it: successive edits diverge enough to clear the
  threshold while still being the same memory by identity.

  Fix — `source_key` (stable caller-owned identity):

  - **`store.save()` upsert-by-key** (`store.py`): a new optional
    `source_key` argument. At most one *live* (non-invalidated)
    document exists per `(collection, source_client, source_key)`. An
    unchanged re-save is idempotent; a changed re-save invalidates the
    prior live row(s) and inserts a fresh one, recording an auditable
    supersession chain via `invalidated_by`. The generic content-hash
    dedup branch is now scoped to `invalidated_at IS NULL` so a
    revert (A → B → A) surfaces a fresh visible memory instead of
    resurrecting a dead row's `access_count`. Additive
    `documents.source_key` column + lookup index migrate in place on
    existing vaults; rows that predate it stay `NULL` (insert-only,
    exactly the old behaviour).
  - **`memory_save` tool** (`server.py` / shared by `server_remote`):
    threads the optional `source_key` through.
  - **Auto-mirror** (`mirror.py`): keys `source_key` to the memory
    file's frontmatter `name` (slug), so a memory edited several
    times in one session stays a single document.

  Also hardens the auto-generated `documents.path` with a uuid salt —
  the upsert path can re-insert identical content within the same
  millisecond (an in-session A → B → A revert), which collided with
  the `UNIQUE(collection, path)` invariant under the old
  `time-ms + hash-prefix` scheme.

  Operator follow-up (not in this change): one-shot `memory_forget`
  sweep of pre-fix same-slug pile-ups in the live default vault
  (`source_client='mnemon-mirror'` grouped by title, keep
  `max(created_at)`).

## [0.6.0rc15] - 2026-05-10

### Fixed

- **Auto-mirror hook captures sentence fragments as high-confidence
  semantic memories.** `claude-code-hook` was saving substrings of
  assistant chat output as standalone `preference` / `decision`
  memories at confidence 0.8 / 0.85. Examples from the live default
  vault: `"argmax-routed"` (id 1998), `"repeat the pattern?"`
  (id 1994), 200-char regex truncations cut mid-word (id 1997). At
  confidence 0.80 these crowded out explicit `mnemon-mirror` saves
  (which carry the per-type default — handoff = 0.60) at recall
  time, and `preference` / `decision` half-lives are `None` so the
  noise would never decay.

  Two-layer fix:

  - **Hook-side shape gate** (`hooks/session_extractor.py`):
    `is_well_shaped()` runs before the dedup roundtrip. Drops
    captures shorter than `MIN_OBSERVATION_CHARS` (20), captures
    ending with `?` (questions, not assertions), captures missing a
    sentence terminator (mid-cut fragments), and captures whose
    content equals the title (no expansion).
  - **Server-side confidence cap** (`store.py` /`config.py`): saves
    where `source_client in HOOK_SOURCE_CLIENTS` are capped at
    `HOOK_SOURCE_CONFIDENCE_CEILING = 0.5`, below the 0.6 explicit
    `mnemon-mirror` band. Defense in depth — even if a future
    extractor path slips a fragment past the shape gate, it can
    never outrank a deliberate mirror save.

  Existing fragments (default vault ids 1994/1997/1998/2000) were
  soft-deleted via `memory_forget` as part of the rollout.

## [0.6.0rc14] - 2026-05-07

### Added

- **Periodic confidence-decay sweep on the Fly server lifespan** (#119).
  `apply_confidence_decay()` in `contradiction.py` was an orphan helper
  with no caller — stored memories never aged after save, so search
  ranking treated a 6-month-old memory the same as one written
  yesterday. `PersistentSessionManager.run()` now schedules a daily
  decay sweep next to the existing prune task; both share the same
  failure-isolation contract (logged + swallowed). Default interval
  `DEFAULT_DECAY_INTERVAL_SECONDS = 24h`. Sweep runs in a worker thread
  via `anyio.to_thread.run_sync` so the full-vault SQL walk doesn't
  stall the event loop. `decay_fn` is injected as a callable so
  `persistent_sessions.py` stays decoupled from `Store`;
  `server_remote.py` supplies a closure that opens its own thread-local
  `Store` (sqlite3 default `check_same_thread=True` forbids reusing the
  foreground singleton across the worker thread).

### Fixed

- **Health-monitor false alarms during Fly cold-start window** (#118).
  `scripts/check_health.py` had a 10s `urllib` read timeout that raced
  the Fly cold-start (machine wake + Python boot + bge-small ONNX load
  + SQLite/FastMCP startup) when the SJC machine auto-stopped during
  the overnight idle window. Four false-alarm comments landed on issue
  #117 between 03:54–11:56 UTC on 2026-05-07 before the timeout was
  bumped to 30s. App was healthy throughout (`/health` <200ms warm).

- **Stale comment + WARN message in `scripts/check_health.py`.** The
  `PERSISTED_SESSIONS_WARN_THRESHOLD` block claimed `expire_old()` runs
  only at startup; that fact has been false since rc12 added the
  periodic prune (#115). Updated the comment + the WARN body to point
  operators at TTL or prune-interval tuning instead of "pruning is
  broken."

### Tests

- 6 new cases in `TestPeriodicDecayConfig` / `TestPeriodicDecayTask`
  shipped in #119 mirror the existing prune-task suite: default
  interval, opt-in via `decay_fn`, fires-on-each-tick,
  logs-decayed-count-when-nonzero, swallows-decay-failures. Full repo
  suite: 732 passing on the rc14 cut.

## [0.6.0rc13] - 2026-05-06

### Fixed

- **Fresh-init MCP requests wedged the server-wide session-creation lock.**
  Diagnosed live during the rc12 soak: `mnemon doctor` and any
  `streamablehttp_client` consumer that opens a new MCP session
  ([no `Mcp-Session-Id` header) hung past 15s while warm-keeper
  PingRequests and tool calls on existing sessions kept working. Direct
  curl confirmed: `tools/list` with the persisted session ID returned
  in <300ms; `initialize` with no session ID hung 20s with no
  `Processing request of type` log on the server. Root cause: upstream
  `StreamableHTTPSessionManager._handle_stateful_request` holds
  `_session_creation_lock` for the **full** duration of the new-session
  branch — including the `await transport.handle_request(...)` that
  dispatches the JSON-RPC payload and waits for the response. If that
  handler wedges for any reason (we never identified the underlying
  trigger, but the wedge reproduced reliably across rc11 and rc12
  fresh-deploys), every subsequent fresh-init queues behind the lock
  and times out at the client side. PR #109 patched the SSE-mode
  variant of this same lock-held-too-long pattern; the
  `json_response=True` variant still had its own way to wedge.

  Fix: `PersistentSessionManager` now bypasses upstream's locked path
  for fresh-init via a new `_create_new_session` method that holds
  `_session_creation_lock` only for the brief `_server_instances`
  mutation, then releases before `task_group.start()` and
  `transport.handle_request()`. `_resume_session` got the same
  narrowing — the lock now covers only the race-guard read + dict
  write, not the dispatch. One wedged handler can no longer take down
  the server's ability to accept new sessions.

### Tests

- `TestSessionCreationLockNarrowing` (5 cases) in
  `tests/test_persistent_sessions.py`: lock-released-before-dispatch
  invariant for both `_create_new_session` and `_resume_session`,
  wedged-handler-doesn't-block-concurrent-fresh-init reproduction
  (this directly mirrors the live-prod symptom), 5-concurrent-fresh-
  inits-get-distinct-ids sanity, and resume-race-lost-falls-through
  coverage. 708 → 713 passing.

## [0.6.0rc12] - 2026-05-06

### Fixed

- **Post-deploy doctor probe wedged a freshly-redeployed Fly machine.**
  During the 0.6.0rc11 redeploy, `mnemon doctor` failed twice in a row
  immediately after `mnemon upgrade web` returned. The deploy itself
  succeeded, but doctor's 7 rapid-fire checks landed on the machine
  while FastEmbed pre-load was still finishing and the in-memory
  session manager was fresh — exactly the request-pile-up shape that
  exposes the latent CallToolRequest hang. `fly machine restart`
  cleared it; doctor passed clean after. Fix: `_redeploy_web` (and the
  first-time `upgrade_web` path) now sleeps for a configurable settle
  window (default 30s) between `flyctl deploy` returning and the
  inline doctor invocation. Override via
  `MNEMON_UPGRADE_SETTLE_SECONDS` (set to `0` to disable).
- **`mcp_sessions.sqlite` grew unbounded under long warm uptimes.**
  `expire_old()` previously ran only at server startup
  (`server_remote.run_remote`), which is fine under
  `auto_stop_machines = "stop"` (every wake = a prune) but lets the
  table drift up under any long-running configuration or between
  cold-stops. Pre-restart on 2026-05-06 the table held 4,171 persisted
  sessions with the 7-day TTL. Fix: `PersistentSessionManager` now
  spawns an in-process anyio task during the lifespan that ticks every
  6h (default) and calls `SessionStore.expire_old()`. Failures are
  logged and swallowed so a transient SQLite hiccup can't take active
  sessions down. Pass `expire_interval_seconds=0` to disable.

### Roadmap

- `--mnemon-version` flag on `mnemon upgrade web` and `CONTRIBUTING.md`
  marked done in `private/ROADMAP.md` — both shipped earlier; the
  rc11-deploy P0 audit caught the missing strikethroughs.

### Tests

- `TestPostDeploySettleWindow` (5 cases) in `tests/test_upgrade.py`
  covers ordering (deploy → settle → doctor), `--skip-doctor`
  short-circuit, env-var override to zero/custom/garbage, and the
  first-time upgrade path. Autouse fixture sets
  `MNEMON_UPGRADE_SETTLE_SECONDS=0` so existing tests don't pay 30s
  per doctor-invoking case.
- `TestPeriodicExpireConfig` + `TestPeriodicExpireTask` (5 cases) in
  `tests/test_persistent_sessions.py` cover the default 6h interval,
  zero-disables-prune, per-tick `expire_old()` call, log-on-prune,
  and swallow-then-retry on failure.

## [0.6.0rc11] - 2026-05-06

### Fixed

- **Per-Stop handoff noise — `handoff_generator` now gates on three
  conditions before saving.** Stop fires after every assistant turn,
  not once per session, and `handoff_generator` previously had no dedup
  — every turn produced a fresh `handoff` memory. Observed in the
  production vault as 710/1376 documents (51.6%) being `handoff`-typed,
  dominated by `claude-code-hook`-sourced rows whose first-user-line
  was a `/loop` body, `<task-notification>`, pasted test output, or a
  one-word reply ("yes", "done", "pr merged"). Three gates now run in
  `main()` cheapest-first: trivial-prompt skip (regex match against
  slash-command / notification / test-output patterns plus a 15-char
  floor), per-session debounce (`~/.mnemon/handoff_session_state.json`
  keyed by hook-payload `session_id` with 600s cooldown, degrades to
  legacy "always save" on empty session_id), and a final remote vector
  dedup mirroring `session_extractor.is_duplicate_remote` posture. PR
  #112.

### Tests

- 20 new tests across `TestHandoffGeneratorTrivialPromptSkip`,
  `TestHandoffGeneratorSessionDebounce`, and
  `TestHandoffGeneratorRemoteDedup` in `tests/test_hooks_extended.py`.
  4 existing `TestHandoffGeneratorMain` tests updated to patch
  `is_duplicate_remote` so they keep exercising the save path. 689 →
  709 passing.

## [0.6.0rc10] - 2026-05-02

### Fixed

- **MCP fresh-session deadlock — `json_response=True` for the
  StreamableHTTP session manager.** `mnemon doctor`, any
  `streamablehttp_client` consumer, and direct curl POSTs to `/mcp`
  were hanging past 60s on `session.initialize()` against a freshly-
  deployed rc9 server. Reproduces immediately on a fresh
  `fly machine restart` — not stale state. Root cause: upstream's
  `StreamableHTTPSessionManager._handle_stateful_request` holds
  `_session_creation_lock` for the full duration of `handle_request`,
  and in SSE response mode (`json_response=False`, the previous
  default) `handle_request` keeps the per-session SSE stream open
  until the client disconnects — so once one session is alive,
  every fresh-session POST queues behind the lock indefinitely.
  mnemon's tools are all single-shot RPCs, so the SSE channel buys
  nothing and only exposes this hang. Fix: pin `json_response=True`
  in `server_remote.run_remote` so each POST is a discrete request/
  response pair. Symptom user-side was Claude Desktop `memory_save`
  calls stalling indefinitely. PR #109.

### Tests

- New `TestSessionManagerConfig::test_session_manager_uses_json_response`
  in `tests/test_server_remote.py` captures
  `PersistentSessionManager` kwargs during `run_remote()` and asserts
  `json_response=True` so this can't silently flip back. 688 → 689
  passing.

## [0.6.0rc9] - 2026-05-02

### Fixed

- **OAuth refresh-token rotation grace window.** A retried `/oauth/token`
  call presenting an already-rotated refresh token within 60 seconds
  now returns the *same* new pair (idempotent retry) instead of
  `invalid_grant`. Closes a class of spontaneous claude.ai connector
  disconnects: when Fly auto-stops the machine and a wake-on-request
  cold-start delays the response, claude.ai's mcp-proxy retries the
  refresh; the original request had already rotated the RT, so the
  retry would brick the connector and force the user to manually
  reconnect. The cache (`{key_dir}/rotated_tokens.json`) is persisted
  to the same volume as `refresh_tokens.json` so it survives the very
  Fly restarts that trigger the bug. Replay after the grace window
  still returns `invalid_grant` — leaked-token defense preserved.
  PR #107.

### Tests

- 3 new tests in `TestServeTokenRefreshGrant`: replay-within-grace
  returns identical pair (covering 3 retries), replay-after-grace
  rejected, and rotation-cache persists across simulated restart
  (the Fly cold-start scenario). 1 existing test updated to reflect
  the new replay-within-grace behavior. 684 → 688 passing.

## [0.6.0rc8] - 2026-04-29

### Fixed

- **`read_transcript` supports Claude Code's nested message envelope.**
  Real Claude Code JSONL nests `{role, content}` under
  `msg.message.role` / `msg.message.content` alongside metadata fields
  (`parentUuid`, `sessionId`, `timestamp`, `cwd`, etc.). The existing
  parser only handled the flat top-level `{"role": ..., "content":
  ...}` format used by synthesized test fixtures, so it silently
  returned an empty string against every real Claude Code session.
  Result: `handoff_generator` and `session_extractor` Stop hooks both
  short-circuit on the `len(transcript) < 200` check before saving
  anything. Fix accepts both wire formats and tightens content-block
  parsing so non-text blocks (`tool_use`, `tool_result`, `image`) are
  explicitly skipped — only `{"type": "text", "text": ...}` blocks
  contribute. PR #101.

  Diagnosed against a real session JSONL: 1,151 lines, 0 with
  top-level `role`, 814 with nested `message.role`. Live verified on
  the same session post-fix: extracted 7,156 chars (was 0). Both Stop
  hooks now save reliably from real Claude Code usage; the
  productized auto-handoff infra finally works end-to-end against
  the wire format Claude Code actually emits.

### Tests

- 6 new regression cases in `TestReadTranscript` using the nested
  Claude Code wire format (envelope + metadata siblings + content as
  list-of-blocks + `file-history-snapshot` lines + mixed flat/nested
  in the same transcript). Test count 675 → 681.

## [0.6.0rc7] - 2026-04-28

### Added

- **PostToolUse auto-mirror hook.** Closes the longstanding gap where
  Claude Code (and any other MCP client with a local auto-memory
  system) writes memory files to its private store but never
  propagates them to the central mnemon vault. The hook installed by
  `mnemon setup` fires on every `Write` / `Edit` / `MultiEdit` tool
  call, no-ops unless the touched file lives under an auto-memory
  directory (`~/.claude/projects/*/memory/*.md` or
  `~/.config/mnemon/auto-memory/*.md`), and otherwise dispatches the
  file's contents to mnemon via the same `MemoryClient` abstraction
  used by the existing UserPromptSubmit / Stop hooks. Local + remote
  modes both supported. Errors are surfaced via stderr per the
  no-silent-fails posture; the hook never blocks Claude Code's
  continued operation. Motivated by the 2026-04-28 alpha-test
  incident where a session handoff written to local memory was never
  mirrored to mnemon. With auto-mirror installed, `mnemon setup` is
  the only step required: subsequent client memory writes appear in
  mnemon automatically.
- **`mnemon mirror <path> [--auto] [--timeout SEC]`** CLI subcommand
  that the hook shells out to. Reads the file, parses YAML
  frontmatter (PyYAML when available, minimal fallback parser
  otherwise), dispatches `memory_save` with `title` from the `name`
  frontmatter field, `content_type` from `type` (default `note`),
  `description` prepended to the body as an italicized line, and
  `source_client = "mnemon-mirror"`. `--auto` short-circuits when the
  path doesn't match an auto-memory pattern. Idempotent: SHA-256
  (title + content) dedup with a 600s window via
  `~/.mnemon/mirror_dedup.json` so repeat saves of the same content
  within ten minutes skip cleanly. Sync-loop guard: files with
  `mnemon_sync_source: <doc_id>` in frontmatter are skipped
  (placeholder for future `mnemon sync down` integration).
- **`mnemon.mirror`** module exposing `mirror_path()` for programmatic
  use and `run_cli()` for the subcommand entry.

### Tests

- 22 new tests in `tests/test_mirror.py` covering the auto-memory
  path filter, frontmatter parsing, the four save/skip outcomes,
  error paths, and CLI exit codes.
- 13 new tests in `tests/test_hooks_auto_mirror.py` for the hook
  handler — file_path extraction, trigger-tool whitelist
  (Write/Edit/MultiEdit), unrelated-tool no-op, malformed stdin
  absorption, expected `MirrorError` surfacing, and unexpected
  exception swallow with stderr surfacing.
- 3 new assertions in `tests/test_setup.py` locking the PostToolUse
  entry shape (matcher = `Write|Edit|MultiEdit`, command points at
  `mnemon.hooks.auto_mirror`, 12s timeout) in both local and remote
  modes, plus a `setup_claude_code` integration check that the entry
  reaches `~/.claude/settings.json`.

Full suite: 675 passed (+38 from this feature; 4 sandbox-only setup
errors in `tests/test_integration_remote.py` pre-exist on `main`,
not introduced here).

## [0.6.0rc6] - 2026-04-26

### Added

- **`--mnemon-version <ver>` flag on `mnemon upgrade web`.** Pins the
  version in the deployed Dockerfile explicitly instead of always
  inheriting from the locally-installed `__version__`. Closes the trap
  where a user publishes a new RC to PyPI then forgets to `pip install
  -U` locally before redeploying, silently shipping the prior version.
  The internal redeploy path already supported the parameter; this
  exposes it on the CLI.
- **`examples/quickstart.py`** demonstrating the public Python API
  (`mnemon.store.Store` + `mnemon.search.search`) without an MCP
  transport. Three queries cover lexical, semantic, and diagnostic
  cases.
- **`bench/search_stress.py`** + a JSON baseline at 1k memories.
  Hybrid BM25 + vector search measures p50 / p95 / p99 over a
  deterministic synthetic corpus. Initial numbers on consumer hardware:
  sub-4 ms p99 at 1k memories, sub-8 ms p99 at 5k.
- **`CONTRIBUTING.md`** with dev setup, style, PR conventions, and a
  pointer to `SECURITY.md`.
- **Hourly health-monitor GitHub Actions workflow.** Hits
  `https://mnemon-memory.fly.dev/health`, asserts on the metrics shape,
  and opens / comments on / closes a single tracker GH issue
  automatically. Resets to green when the next run passes.
- **Fresh-install CI workflow.** Builds the wheel from PR source,
  installs it in clean `python:X.Y-slim` containers (3.10 / 3.12 /
  3.13), verifies CLI + import. Weekly schedule also smoke-tests the
  latest published version on PyPI.
- **Daily `pip-audit --strict .` CI** scanning the declared dependency
  graph for CVEs.
- **`/health` session-routing metrics counters.** New `metrics` key on
  the `/health` payload exposes `in_memory_hits`, `resume_hits`,
  `fresh_inits`, `stale_session_misses`, `persisted_sessions_total`,
  and `in_memory_sessions_current` — for cold-start diagnostics and
  regression detection. Backward-compatible: clients that only check
  `status == "ok"` are unaffected.

### Changed

- **`mnemon setup claude-code` and `mnemon setup claude-desktop` no
  longer frame the claude.ai-synced + stdio dual config as a "shadow"
  problem.** New copy reads as state-of-the-world: `ℹ Both local + web
  mnemon configured. <Client> will use the web version (claude.ai-
  synced) by default.` Behavior is unchanged — both registrations
  still written, web wins by default, stdio activates automatically if
  the user later removes the claude.ai entry. The previous "⚠ shadow"
  alarm framing implied the user did something wrong; they didn't.
- **`mnemon setup claude-desktop` now surfaces the dual-config notice
  too.** Claude Desktop syncs MCP from claude.ai (same Anthropic
  account) the same way Claude Code does, so the same coexistence
  applies.
- **`mnemon uninstall` no longer says "no mnemon registration found
  (or CLI errored silently)"** when the claude CLI returns non-zero.
  The non-zero case is the common one (registration was already gone);
  reworded to "no user-scope mnemon registration to remove" and
  surfaces stderr if it doesn't look like a "not found"-family error.
- **README PyPI badge is now dynamic** via `shields.io/pypi/v/...`.
  Previously hardcoded to a specific RC and went stale across releases.

## [0.6.0rc5] - 2026-04-26

### Added

- **`/health` exposes session-routing metrics.** Same shape and feature
  as listed in 0.6.0rc6 — listed here for traceability since rc5 was
  the first version on PyPI to ship the counters. (rc5 was published
  without a CHANGELOG entry; this back-fills it.)

## [0.6.0rc4] - 2026-04-25

### Added

- **Persistent MCP Streamable HTTP sessions across process restarts.**
  Issued `Mcp-Session-Id` values are now stored in SQLite (default at
  `<vault_dir>/mcp_sessions.sqlite`, 7-day TTL). When a request bearing
  a previously-issued session ID arrives at a fresh process — for
  example after a Fly cold-stop, a redeploy, or any other restart — the
  server transparently resumes the session instead of returning 404,
  by spawning a new transport keyed to the same ID and running the MCP
  app `stateless=True` so the server-side session is born already-
  initialized. The client sees no break.

  Pairs with the `/health` warm-keeper hook from the previous release:
  the hook minimizes the *frequency* of cold-stops during active use,
  this change handles the *consequence* when one happens anyway. With
  both shipped, mnemon survives Fly auto-stop without the "MCP UI says
  connected, every tool call returns 404" failure mode.

  Caveat: resumed sessions are stateless on the server side. Mnemon
  uses no client-capability-gated features (sampling, elicitation,
  roots), so this is a no-op for our toolset, but third-party forks
  adding those features will need to re-handshake explicitly.

### Fixed

- **Mid-session disconnects on Fly when `auto_stop_machines = "stop"`.**
  `mnemon setup` (any target with `--remote-url`) now installs a
  lightweight `/health` warm-keeper as the first `UserPromptSubmit`
  hook. It pings the Fly app on every prompt, resetting Fly's idle
  timer so the machine stays warm during active Claude Code sessions
  and wakes on the first prompt after idle. Independent of MCP session
  state, so it works even after a cold-stop has invalidated the
  `Mcp-Session-Id` (which the existing context-surfacing hook's MCP
  call cannot recover from). `|| true` ensures a slow Fly wake never
  blocks the user's prompt.

  Previously: machine autostops after ~5 min idle, the open MCP
  Streamable HTTP session goes stale, and Claude Code's MCP UI keeps
  showing "connected" while every subsequent tool call returns 404.

## [0.6.0rc3] - 2026-04-22

### Fixed

- **`mnemon upgrade web` is now idempotent.** Rerunning it against an
  already-deployed Fly app (previously a hard failure / confusing
  relaunch) now detects the existing app via `flyctl status` and
  switches to a redeploy-only path: skip S3 push, volume create,
  secrets set, vault seed, local vault archive, and MCP client
  reconfigure — all of which are no-ops once the web tier is
  established. Clients keep their URL and bearer token; the new image
  is picked up on the next request. First-time deploy flow is
  unchanged.

  Upgrading to a newer mnemon version is now a two-command flow:
  `pip install -U 'mnemon-memory[server]'` then
  `mnemon upgrade web --app-name <existing-app>`.

  Redeploy only requires `flyctl`; AWS creds and an S3 bucket are no
  longer required for the version-bump case.

## [0.5.0] - 2026-04-14

### Breaking

- **MCP tools now return JSON instead of pre-formatted prose.** The
  ``_structured`` paired variant (``memory_search_structured``) is gone
  — ``memory_search`` itself returns the JSON array directly. Same
  treatment for ``memory_get``, ``memory_timeline``, ``memory_status``,
  ``memory_sweep``, ``memory_related``, ``profile_get``. The motivation:
  a single clean-format contract beats paired tools with the same
  concern but different shapes, and modern LLM clients (claude.ai,
  Claude Code, Cursor, Claude Desktop) all parse JSON cleanly.
  Mutation/side-effect tools (``memory_save``, ``memory_pin``,
  ``memory_forget``, ``memory_rebuild``, ``memory_check_contradictions``,
  ``profile_update``) still return short confirmation strings.
- **Breaking for direct MCP consumers** that regex the old prose
  output. The ``context_surfacing`` hook was the main such consumer
  in-tree and has been migrated to JSON parsing + client-side
  formatting. Migration for your own consumers: call the same tool
  name, ``json.loads()`` the result, format or filter as you need.
  Empty result is now ``"[]"`` (not a prose sentinel).

### Added

- ``memory_export_vectors`` — new tool exposing the full embedding
  matrix joined to document metadata, for remote-aware clients that
  want to run UMAP / visualization / similarity work client-side. JSON
  only; capped at 5000 vectors per response with a ``truncated`` flag
  for vaults past that size.
- ``VecStore.export_all()`` — internal helper returning a snapshot
  copy of (ids, vectors) so ``memory_export_vectors`` callers can
  mutate the returned array safely.

### Changed

- Total tool count: 14 (was 14 in 0.4.3 — ``memory_search_structured``
  was removed and ``memory_export_vectors`` added; net zero).
- ``context_surfacing`` hook now parses JSON from ``memory_search`` and
  formats the markdown context block client-side. Output in the
  ``<mnemon-context>`` block is shape-identical to 0.4.x — Claude sees
  the same injected context, just formatted by the hook rather than
  the server.
- **Dashboard is remote-aware.** The Streamlit dashboard detects
  ``MNEMON_REMOTE_URL`` / ``~/.mnemon/remote_url`` and routes every
  loader through the remote MCP server, so a browser-side user sees
  the same vault their hooks write to. Local SQLite stays as the
  development fallback. (PR #59)
- **Graph page collapses chunks to one point per memory.** Long
  documents produce multiple embedding chunks; previously each chunk
  rendered as its own UMAP point, which read as duplicates. The graph
  now mean-pools (L2-normalized) per document so point count matches
  memory count. (PR #60)
- **Dashboard remote-call timeouts raised.** The dashboard was
  inheriting the 8s hook timeout, which is sized for Claude Code's
  hook budget and could time out on Fly cold-start or on the heavier
  ``memory_export_vectors`` call. Dashboard now uses 30s general / 60s
  for vector export; hook callers unchanged. (PR #61)
- **``mnemon doctor`` works in local mode.** Auto-detects config and
  runs three parallel checks (vault reachable, embedder loadable,
  save/search/forget round-trip via ``Store``) when no remote URL is
  configured. Remote mode unchanged. No new flag — same command,
  adaptive behavior. (PR #62)
- **README onboarding rewrite.** Quick Start leads with the 60-second
  local path instead of the 10-minute Fly self-host runbook; dashboard
  visible in Install and Quick Start with an embedded screenshot;
  self-host secret handling switched from ``echo "$SECRET" > file`` to
  ``read -rs`` + ``printf %s`` so tokens don't land in shell history;
  FastEmbed cold-start warning added to the first-run path. (PR #62)

## [0.4.3] - 2026-04-14

Rolls up all 0.4.2 changes (which was built but never uploaded to PyPI) plus a final ruff-clean pass.

### Fixed
- **`mnemon doctor` round-trip now actually forgets the probe memory.** Previously called `memory_forget` with wrong kwarg (`document_id` vs `id`); FastMCP returned a tool-error payload rather than raising, so the save/search/forget check always reported success while leaking probe memories into the vault on every run (PR #54).

### Changed
- `MMR_DEMOTION_FACTOR`, `QUERY_EXPANSION_MAX_TOKENS`, `CONTRADICTION_OVERLAP_THRESHOLD` (renamed from `OVERLAP_THRESHOLD`), and `CONTRADICTION_CONTEXT_MAX_CHARS` moved from inline literals to `config.py`. Tunable surface now discoverable in one file (PR #54).
- Hook error logging consolidated via `framework.log_hook_error(hook_name, context, exc)`. All three hooks (`context_surfacing`, `session_extractor`, `handoff_generator`) now emit a single greppable format: `mnemon {hook} {context}: {Type}: {message}`. Nine call sites replaced (PR #55).
- `Store.save()` dropped the unused `path` kwarg. Path was auto-generated in every call; the parameter had no callers. **Minor API break** — removed rather than deferring to a major version since 0.4.1 was published for only a few hours with no known external adoption (PR #55).
- `DEFAULT_CONFIDENCE` lookups in `contradiction.py` and `store.py` switched from `.get(ct, 0.5)` to strict `[ct]`. The enum has an explicit mapping for every value; the fallback was dead code that would mask future enum additions (PR #54).
- `llm.try_generate` now logs a WARNING on LLM failure instead of silently returning `None`. LLM is optional infra but hidden crashes (OOM, llama-cpp version mismatch) previously had no operator signal (PR #54).

### Removed (dead code / lint cleanup)
- `contradiction.SearchResult` import, `server.CONTENT_TYPE_VALUES` import, `store.dataclasses.field` import, and a dead `local_token = _ensure_local_token(...)` assignment in `setup.py` — all caught by `ruff check --select F`.
- Renamed ambiguous `l` loop variables to `label` / `line` in `charts.py` and `handoff_generator.py` (E741).
- Added `[tool.ruff.lint.per-file-ignores]` suppressing E402 under `src/mnemon/dashboard/` — Streamlit pages require `st.set_page_config(...)` before other imports, legit convention.
- Test-count claims switched from fixed "460 tests" to drift-tolerant "450+ tests" in `README.md` and `CLAUDE.md`.

## [0.4.1] - 2026-04-14

### Added
- **`mnemon doctor` exercises the OAuth AS metadata endpoint** (PR #49). Fetches `/.well-known/oauth-authorization-server`, validates required RFC 8414 fields, asserts `issuer` matches deployment base URL. Catches silent browser-client breakage from `MNEMON_PUBLIC_URL` typos that the local-token path doesn't surface. Warns (not fails) when the AS isn't enabled — legitimate for local-token-only deploys.
- **Per-IP rate limit on failed `/oauth/authorize` attempts** (PR #50). 10 failures per 5-minute sliding window returns HTTP 429 with `Retry-After`. Correct passphrase clears the counter. Client IP resolved from `Fly-Client-IP` → `X-Forwarded-For` → `scope["client"][0]`.

### Changed
- `CHANGELOG.md` backfilled with 0.3.0 and 0.4.0 entries (PR #48).
- `fly volume create` command in README's self-host runbook now includes `--yes` to skip the single-region-volume confirmation prompt (PR #48).

## [0.4.0] - 2026-04-14

### Added
- **Self-hosted OAuth 2.1 Authorization Server** (Phase 2). MCP-compliant `/oauth/authorize`, `/oauth/token`, `/oauth/register` endpoints with PKCE, RS256 JWTs, and RFC 7591 Dynamic Client Registration. No third-party auth vendor required — `fly deploy` gives you a working OAuth-protected MCP endpoint (PRs #36–#39).
- **Volume-backed persistence for OAuth state.** Auth codes and refresh tokens now persist to `{key_dir}/auth_codes.json` and `{key_dir}/refresh_tokens.json` (mode 0600, atomic write-through) so Fly autostop wake doesn't force every client to re-enter the passphrase (PR #44).
- **`fly.toml.example` template + self-host runbook in README.** Placeholder-driven config plus an end-to-end walkthrough (`fly launch` → `fly volume create` → secrets → deploy → `mnemon doctor` → client connection) so a new user can stand up a vault in ~10 minutes with zero Fly prior knowledge (PR #46).
- **16-char minimum on `MNEMON_AS_PASSPHRASE`.** Validated at server boot via `validate()`; error message points at `secrets.token_urlsafe(32)` (PR #47).
- Auth code + refresh token rotation: one-time-use auth codes, rotation on every refresh.

### Changed
- **Removed Auth0 / external-AS code path** (PR #40). `OAuthConfig` now only carries `local_token`; `MNEMON_OAUTH_ISSUER` / `JWKS_URL` / `AUDIENCE` / `USERINFO_URL` env vars are gone. The self-hosted AS is the only browser-auth path.
- `fly.toml` is now gitignored; `fly.toml.example` is the canonical template (PR #46).
- Test count: 253 → 460.

### Removed
- Auth0 JWKS validation, userinfo fallback, and all `MNEMON_OAUTH_*` env var parsing.
- `MNEMON_OAUTH_AUDIENCE` from `fly.toml` (dead after PR #40; PR #45).

## [0.3.0] - 2026-04-13 (not published to PyPI)

### Added
- **Remote-first architecture unification** (Phase 3). Claude Code hooks (context_surfacing, session_extractor, handoff_generator) rewritten to call the remote Fly vault via Streamable HTTP instead of the local SQLite vault (PRs #25–#30).
- `MNEMON_LOCAL_TOKEN` static bearer path for headless clients (hooks, Cursor) that can't complete a browser OAuth flow (PR #24).
- `mnemon setup` CLI: auto-configures Claude Code / Cursor / Gemini with a remote URL and bearer token (PR #28).
- `mnemon doctor` CLI: six end-to-end diagnostics (remote URL, local token, file perms, health endpoint, auth + MCP tool call, save/search/forget round-trip) (PR #41).
- Claude Code hook integration tests (PR #34).

### Changed
- Hooks read `MNEMON_REMOTE_URL` + `MNEMON_LOCAL_TOKEN` (or `~/.mnemon/remote_url` + `~/.mnemon/local_token`) instead of opening a local SQLite file.
- Dedup in hooks switched from text-parsing to structured `memory_search_structured` MCP tool (PR #33).

## [0.2.0] - 2026-04-09

### Added
- Full Python rewrite (was TypeScript/Bun in v0.1.x)
- 13 MCP tools: search, get, timeline, save, pin, forget, status, sweep, related, rebuild, check_contradictions, profile_get, profile_update
- Hybrid BM25 + vector search with Reciprocal Rank Fusion and MMR diversity filtering
- Composite scoring: 0.5 * relevance + 0.25 * recency + 0.25 * confidence
- FastEmbed embeddings (bge-small-en-v1.5, 384d ONNX — no PyTorch needed)
- Memory lifecycle: content-type-based half-life decay, pinning, archival via sweep
- Contradiction detection with confidence decay (vector similarity + optional LLM classification)
- Claude Code hooks: context surfacing (UserPromptSubmit), session extraction (Stop), handoff generation (Stop)
- Auto-configure command: `mnemon setup claude-code|cursor|gemini|hooks`
- Remote Streamable HTTP server via FastMCP native transport
- S3 vault sync (push/pull via AWS CLI)
- CLI: serve, serve-remote, status, search, save, forget, sync, setup
- Optional local LLM (QMD-1.7B via llama-cpp-python) for query expansion, extraction, contradiction detection
- 253 tests, 90% coverage

### Changed
- Rewritten from TypeScript (Bun) to Python (>=3.10)
- Storage: SQLite + FTS5 + numpy vector store (was SQLite + FTS5 + TypeScript vector store)
- Embedding: FastEmbed bge-small-en-v1.5 (was EmbeddingGemma-300M)
- Build: hatchling (was Bun bundler)
- Package distribution: PyPI (was npm)

## [0.1.x] - 2026-04-08

Initial TypeScript implementation (deprecated, replaced by Python rewrite in v0.2.0).
