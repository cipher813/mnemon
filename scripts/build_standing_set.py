"""scripts/build_standing_set.py — Phase 0 standing-tier scorer (v3).

Embedding-based exemplar-projection scoring for the salience-tier
standing set. Uses mnemon's existing FastEmbed infrastructure
(bge-small-en-v1.5, 384d, ONNX). NO external LLM, NO new dependency.

This is the SOTA pattern for few-shot text classification with
hand-defined class prototypes — anchor texts as positive/negative
exemplars, project candidates against them, score by max cosine.
See: SetFit, prototype networks, anchor-based reranking.

LLM-judge would be a more accurate alternative but is queued as a
future opt-in (private/ROADMAP.md) to keep mnemon's public-release
onboarding dependency-free.

Plan: private/mnemon-salience-tier-plan-260521.md

Scoring (all signals in [0, 1] before weighting):

    constraint_score   max cosine(memory, constraint_exemplars)
                       "does this look like a standing rule?"
    time_penalty       max cosine(memory, time_bounded_exemplars)
                       "is this a time-bounded status update?"
    correction         1.0 if content_type='feedback' AND confidence
                       ≥ 0.85 AND title/content matches correction
                       pattern. Behavioral signal — operator-corrected
                       memories are explicit standing-tier material.
    contradiction      normalized count of incoming relations where
                       memory is winning side of 'contradicts' or
                       'supersedes'. Behavioral signal — memories
                       that displace others on load-bearing facts.
    breadth            normalized count of distinct content_types
                       in top-50 FTS neighbors. Cross-domain proxy.

    combined = 2.0·constraint + 2.0·correction + 1.0·contradiction
             + 0.5·breadth − 1.5·time_penalty

Weights are operator-tunable at the top of this file. Default
auto-selects top-10 (capped at hard ceiling 20 per the plan's
"cap is the contract" invariant). Operator can override via the
companion `scripts/salience_phase0.sh select <IDS>` subcommand.

Outputs (two files, both consumed by the recall hook):

    ~/.mnemon/standing.json          {"ids": [...]}
    ~/.mnemon/standing-rendered.md   pre-fetched rendered content,
                                     so the recall hook reads from
                                     disk (microseconds) instead of
                                     per-prompt HTTP fetch (~5s for
                                     N=10)

Usage:
    .venv/bin/python scripts/build_standing_set.py --db /path/to/snapshot.sqlite
    .venv/bin/python scripts/build_standing_set.py --top 10  # default
    .venv/bin/python scripts/build_standing_set.py --print-only  # don't write standing.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# Add src/ to path so we can use mnemon's embedder + safety modules.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import numpy as np  # noqa: E402

# ── Tunable exemplars ──────────────────────────────────────────────
#
# Positive exemplars: anchor texts representing the "standing
# constraint" shape. A memory with high max-cosine to any of these
# is structurally like a standing rule.
#
# Negative exemplars: time-bounded status updates. A memory with
# high cosine here gets penalized — even if its content seems
# rule-like, anchoring to date/event markers means it's not durable.

CONSTRAINT_EXEMPLARS = [
    # SOTA / institutional rules
    "default to the SOTA / institutional approach, no shortcuts",
    "use the right primitive, not the smaller diff",
    "the most-correct, most-robust route is also the faster path to production",
    "only deviate from SOTA with an explicit written rationale",
    "lift to library when two or more consumers exist",
    # Verification / discipline
    "always verify before promoting to production",
    "audit before scoping, the codebase is the source of truth",
    "every audit finding becomes a ROADMAP follow-up item",
    "verify branch state before running apply.sh",
    "test reproduction before shipping the static root cause",
    # Failure / error handling
    "fail loud and fast on errors, no silent swallows",
    "any swallow must carry an inline comment naming the failure mode",
    "raise so the failure surfaces at the earliest possible callsite",
    "graceful degrade is forbidden on producer / writer paths",
    # Process / coordination
    "every PR deploy appends to the system-wide changelog automatically",
    "never argmax-route to a per-regime sub-model",
    "use canonical alpha labels with explicit clipping over raw arithmetic returns",
    # Existential constraints
    "runway is not a constraint, optimize for preference not necessity",
    "X is not Y — assert the constraint explicitly",
    "this fact conditions reasoning regardless of query similarity",
    # ── Declarative-posture exemplars (added 2026-05-22 per ROADMAP P1) ──
    # Imperative-shape exemplars above ("never," "always," "must,"
    # "default to") under-weight career / lifestyle / posture constraints
    # that the user encodes declaratively. The 2026-05-22 finding: the
    # auto-selected top-10 against the real vault was 100% engineering
    # rules; career-context memories spanning multi-year load-bearing
    # posture (runway, recruiter posture, start-date framing, search
    # mode) did not surface despite being equally durable. These
    # exemplars represent the declarative shape of the same constraint
    # class — facts stated as if they govern future advice across many
    # domains, but phrased as posture not as imperative.
    "Brian's stance is correct as-is, posture is by design",
    "current preference is to wait, not to push",
    "his stated preference: keep replies minimal, not desperate",
    "passive / selective mode is correct given runway and pipeline",
    "their silence is information; outreach signals desperation",
    "this decouples cash pressure from outreach push timing",
    "lump sum severance through August is in hand, not biweekly",
    "deliberately niche, not chasing scale or virality",
    "the constraint binding decisions is preference, not necessity",
    "soft target for start date preserves negotiating leverage",
]

TIME_BOUNDED_EXEMPLARS = [
    # Session handoffs (these consistently get caught by FTS-breadth noise)
    "Session: proceed with option A",
    "Session: pr merged",
    "Session: ok so you are telling me",
    "Session: i was wondering",
    "Session: i think it may be best",
    # Status updates with date markers
    "today's saturday SF run completed successfully",
    "yesterday's deploy of PR was merged to main",
    "shipped this week as part of the rc18 release",
    "tomorrow's market open at 6:30 AM PT",
    "the 2026-05-21 incident response writeup",
    "scheduled for Wednesday afternoon trigger",
    "merged commit abc123 to main on Friday",
    "this morning's MorningEnrich step in the weekday SF",
    "post-market reconciliation completed for May 21",
    # PR / commit specific
    "PR #143 merged at 22:17:17Z",
    "v0.6.0 tag pushed to origin",
    # Tiny single-thought memories (no constraint, no context)
    "halt the run",
    "propagate",
    "Option 1 or 3",
]

# Operator-tunable correction patterns (existing heuristic, kept).
CORRECTION_PATTERNS = [
    re.compile(r"^\s*(stop|don'?t|never|always)\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\b(correction|corrected|wrong about|i was wrong)\b", re.IGNORECASE),
]

# Weights — tune by inspection of top-N output.
W_CONSTRAINT = 2.0
W_CORRECTION = 2.0
W_CONTRADICTION = 1.0
W_BREADTH = 0.5
W_TIME_PENALTY = 1.5

DEFAULT_TOP_N = 10
HARD_CEILING = 20  # plan invariant: never exceed 20

# Vault-derived exemplars (added 2026-05-27 per ROADMAP P1).
#
# The hand-tuned CONSTRAINT_EXEMPLARS / TIME_BOUNDED_EXEMPLARS lists
# encode the maintainer's prior beliefs about what "constraint shape"
# and "time-bounded shape" look like. Real prototype-network design
# samples the user's own vault for exemplars — high-confidence
# preference/feedback memories represent durable constraints; recent
# session-handoff memories represent ephemeral status updates.
# Adapts per-user without hand-tuning maintenance.
#
# Defense-in-depth: by default we EXTEND the hand-tuned lists with
# vault-derived exemplars rather than replacing — hand-tuned encode
# general institutional patterns the vault may not yet contain; vault-
# derived encode user-specific patterns the hand-tuned lists miss.
# Operators can swap the strategy via --exemplar-source.
VAULT_EXEMPLAR_DEFAULT_COUNT = 15
VAULT_POSITIVE_CONFIDENCE_FLOOR = 0.80

# LLM-judge constants (added 2026-05-27 per ROADMAP P2 follow-up;
# provider-agnostic via krepis 2026-07-03; migrated onto the krepis
# model-GROUP router 2026-08-29 (mnemon-I<N>) per Brian's 2026-08-29 ruling
# that the entire fleet routes through the krepis router with no parallel
# setups, and that direct Anthropic API use is retired fleet-wide. The
# previous version of this constant block pinned a literal "provider:model"
# spec string (bypassing the registry's group/fallback layer) and carried a
# "--judge anthropic" back-compat alias that called the Anthropic API
# directly — both are policy violations under the 2026-08-29 rulings and
# are removed, not merely defaulted-off.
#
# Opt-in via --judge llm. Default remains the embedding scorer (zero new
# deps, public-release-friendly).
#
# The judge model is a REGISTRY GROUP, not a pinned model: MNEMON_JUDGE_GROUP
# selects one of krepis's model_groups (low / med / high / ultra — see
# alpha-engine-config/private-docs/LLM_MODEL_REGISTRY.yaml). krepis resolves
# the group to a live member — with cross-provider fallback and fail-closed
# refusal of any non-compelled route (model-router-policy §5) — so a
# provider outage or account exhaustion degrades within the registry rather
# than needing a code or spec-string change here. Default "low": this judge
# task (a mechanical structured-rubric score) matches the flash-tier
# mechanical/deterministic tier, mirroring the original cheap-flash default.
JUDGE_GROUP_ENV = "MNEMON_JUDGE_GROUP"
JUDGE_GROUP_DEFAULT = "low"
JUDGE_MAX_TOKENS = 300


class RouterUnresolvable(RuntimeError):
    """The configured judge ``model_group`` could not be resolved to a
    callable endpoint through the krepis router.

    A distinct type (mirrors ``flow_doctor.core.router.RouterUnresolvable``
    / ``morning_signal.claude.RouterGroupUnresolvable`` — model-router-policy
    R20) so callers never mistake "the router could not be reached" for "the
    router was reached and declined": either way, the LLM call did not
    happen at all.
    """


#: Routes a judge call may be served on — the COMPELLED PATHS, per
#: `model-router-policy.md` §5, mirroring
#: `flow_doctor.core.router.COMPELLED_ROUTES` and
#: `morning_signal.claude._COMPELLED_ROUTES`. Anything else (a bare
#: `openrouter`/`anthropic` direct route chosen by krepis's own fallback) is
#: refused: alpha-engine-config-I6367 forbids direct-OpenRouter linkage, and
#: the direct-Anthropic API budget is $0 (2026-07-17 ruling, reaffirmed
#: 2026-08-29).
_COMPELLED_ROUTES = frozenset({"litellm_proxy", "egress_proxy"})


def _resolve_judge_router_edge(group: str, *, max_tokens: int):
    """Resolve *group* to a callable ``ModelSpec`` via the krepis router.

    Mirrors ``flow_doctor.core.router.resolve_router_edge`` — the same
    resolve → check-compelled-route → degrade-if-not-primary shape, kept in
    sync by hand for now (candidate for lifting into ``nousergon-lib`` on a
    third adoption per ``policy-shared-code``; tracked in the same issue as
    this migration).

    Never falls back to a direct provider or a caller-pinned model on
    failure — raises :class:`RouterUnresolvable` instead (fail closed, R20).
    """
    try:
        from krepis.router import resolve_group_spec
    except ImportError as e:
        raise RouterUnresolvable(
            "--judge llm requires the `krepis` adapter. Install via "
            "`pip install 'mnemon-memory[judge]'`, or use --judge "
            "embedding (default)."
        ) from e

    # Declared, never inferred (model-router-policy R29).
    exec_context = os.environ.get("KREPIS_EXEC_CONTEXT") or None
    try:
        spec, route = resolve_group_spec(
            group,
            exec_context=exec_context,
            # The router edge speaks OpenAI-compatible chat completions.
            wire="openai",
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raise RouterUnresolvable(
            f"judge model_group {group!r} did not resolve "
            f"(exec_context={exec_context!r}): {exc}"
        ) from exc

    resolved_route = route.get("route")
    if resolved_route not in _COMPELLED_ROUTES:
        raise RouterUnresolvable(
            f"judge model_group {group!r} resolved to route "
            f"{resolved_route!r} (provider={spec.provider!r}), which is "
            f"not a compelled path — refusing to call a direct provider "
            f"chosen by fallback (alpha-engine-config-I6367). Compelled "
            f"routes: {sorted(_COMPELLED_ROUTES)}"
        )
    if resolved_route != "litellm_proxy":
        print(
            f"# DEGRADED: judge model_group {group!r} resolved to route "
            f"{resolved_route!r} (provider={spec.provider!r} model="
            f"{spec.model!r}) rather than the authenticated edge — the "
            f"edge's health probe did not pass. This is the registry-"
            f"derived degraded route (model-router-policy §5), still a "
            f"compelled path.",
            file=sys.stderr,
        )
    return spec
# SFT trace sink for the distillation corpus (rows written only when
# LLM_SFT_CAPTURE_ENABLED / ALPHA_ENGINE_DECISION_CAPTURE_ENABLED is set).
JUDGE_SFT_SINK_ENV = "MNEMON_JUDGE_SFT_SINK"
JUDGE_RUBRIC_DIMENSIONS = ("generality", "durability", "imperative_shape", "cross_domain")
JUDGE_RUBRIC_PROMPT = """\
You are scoring a memory's fit for a "standing context" tier — a capped
set of facts that condition reasoning on EVERY future prompt, regardless
of query similarity. Members must be durable constraints, not ephemeral
status.

Score the memory on FOUR dimensions, each 1 (very low) to 5 (very high):
  - generality: how widely does this rule apply? (specific event = 1,
    cross-cutting principle = 5)
  - durability: how long does this remain true? (will change in days = 1,
    multi-year invariant = 5)
  - imperative_shape: how rule-like vs context-like? (status update = 1,
    explicit norm = 5)
  - cross_domain: does this condition reasoning across unrelated query
    types? (single-domain = 1, conditions advice everywhere = 5)

Return a JSON object with each dimension as a key + a one-sentence
rationale field. Be terse and consistent."""
# Durable non-decaying SEMANTIC types (decision / preference / antipattern
# per HALF_LIVES). Excludes observation/research/project/note: those
# decay and represent context, not constraints.
VAULT_POSITIVE_TYPES = ("decision", "preference", "antipattern")
# Handoffs are session summaries — explicitly time-bounded (30d half-life,
# 0.60 default confidence). The session_extractor hook produces these
# automatically, so they're the natural negative anchor distribution.
VAULT_NEGATIVE_TYPES = ("handoff",)

# Minimum content length for standing-tier consideration. A 2-word
# memory like "halt the run" or "propagate" technically scores via
# breadth (FTS-matches many queries) but carries no actual constraint
# — too thin to condition reasoning. Hard-filter rather than soft-penalty
# because no amount of other signal saves a 2-word memory from being
# noise in a standing-tier context.
DEFAULT_MIN_CONTENT_LENGTH = 50


def _resolve_db(vault_override: str | None, db_override: str | None) -> Path:
    if db_override:
        return Path(db_override)
    if vault_override:
        return Path(vault_override) / "default.sqlite"
    env = os.environ.get("MNEMON_VAULT_DIR")
    if env:
        return Path(env) / "default.sqlite"
    return Path.home() / ".mnemon" / "default.sqlite"


def _resolve_vecstore(db_path: Path) -> Path:
    """The vecstore lives next to the sqlite as <vault>/default.vec.npz."""
    return db_path.with_suffix(".vec.npz")


def _normalize(scores: dict[int, float]) -> dict[int, float]:
    """Min-max normalize a score dict to [0, 1]. Returns 0s if all equal."""
    if not scores:
        return scores
    vals = list(scores.values())
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return {k: 0.0 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


def _sample_vault_exemplars(
    conn: sqlite3.Connection, n: int = VAULT_EXEMPLAR_DEFAULT_COUNT,
) -> tuple[list[str], list[str]]:
    """Sample positive + negative exemplar texts from the vault itself.

    Positive exemplars: highest-confidence ``preference`` / ``decision`` /
    ``antipattern`` memories (≥ ``VAULT_POSITIVE_CONFIDENCE_FLOOR``).
    These types are non-decaying SEMANTIC (per ``HALF_LIVES`` in
    ``config.py``) — durable by content-type design. Pulled by
    confidence DESC so the most-load-bearing user-authored constraints
    anchor the positive class.

    Negative exemplars: most recent ``handoff`` memories. These are
    session summaries — explicitly time-bounded by both confidence
    default (0.60) and half-life (30d). The session_extractor hook
    produces them automatically, so they form a stable negative
    distribution per vault.

    Returns ``([positive_texts], [negative_texts])``. Each text is
    ``"{title}: {content[:200]}"`` — title + snippet gives the
    embedder enough signal to anchor the class without dominating.
    Returns empty lists when the vault lacks material; caller decides
    whether to fall back to the hand-tuned lists.
    """
    pos_types = ",".join(["?"] * len(VAULT_POSITIVE_TYPES))
    pos_rows = conn.execute(
        f"""
        SELECT d.title, c.doc AS content
        FROM documents d
        JOIN content c ON d.hash = c.hash
        WHERE d.invalidated_at IS NULL
          AND d.content_type IN ({pos_types})
          AND d.confidence >= ?
        ORDER BY d.confidence DESC, d.id DESC
        LIMIT ?
        """,
        (*VAULT_POSITIVE_TYPES, VAULT_POSITIVE_CONFIDENCE_FLOOR, n),
    ).fetchall()

    neg_types = ",".join(["?"] * len(VAULT_NEGATIVE_TYPES))
    neg_rows = conn.execute(
        f"""
        SELECT d.title, c.doc AS content
        FROM documents d
        JOIN content c ON d.hash = c.hash
        WHERE d.invalidated_at IS NULL
          AND d.content_type IN ({neg_types})
        ORDER BY d.created_at DESC
        LIMIT ?
        """,
        (*VAULT_NEGATIVE_TYPES, n),
    ).fetchall()

    def _fmt(row) -> str:
        title = (row["title"] or "").strip()
        snippet = (row["content"] or "")[:200].strip()
        if title and snippet:
            return f"{title}: {snippet}"
        return title or snippet

    return [_fmt(r) for r in pos_rows], [_fmt(r) for r in neg_rows]


def _score_via_llm_judge(docs: list[dict], *, group: str) -> dict[int, float]:
    """Score each memory's 'constraint-ness' via an LLM rubric judge.

    Returns {doc_id: score_in_0_to_1} — rubric mean over the 4
    JUDGE_RUBRIC_DIMENSIONS normalized to [0.2, 1.0] (raw range
    1-5 / 5 = 0.2-1.0).

    Routed via the krepis model-group router (model-router-policy §2):
    *group* (from ``MNEMON_JUDGE_GROUP``, default ``"low"``) resolves to a
    live registry member through ``krepis.router.resolve_group_spec``, with
    cross-provider fallback and fail-closed refusal of any route outside
    the compelled set (``_resolve_judge_router_edge`` /
    :class:`RouterUnresolvable`). Swapping the tier is an env edit or
    ``--judge`` invocation, never a pinned model string in this file — a
    pinned string is exactly the parallel-routing shape the 2026-08-29
    "everything funnels through the krepis router" ruling forbids.

    Per-memory rationale is printed to stderr for audit; the existing
    scoring pipeline downstream only consumes the scalar score, so the
    rationale is observability not a return value. When SFT capture is
    enabled (``LLM_SFT_CAPTURE_ENABLED=1``), every judge call is also
    staged to the distillation corpus sink (``MNEMON_JUDGE_SFT_SINK``,
    default ``~/.mnemon/sft/judge.jsonl``).

    Activation requirements:
      - ``krepis`` must be importable (operator-side install:
        ``pip install 'mnemon-memory[judge]'``).
      - *group* must resolve through the router to a compelled route
        (``litellm_proxy`` or ``egress_proxy``) — credential resolution is
        the router's responsibility (SSM/env, per model-router-policy),
        never a key this script reads directly.

    Raises ``RuntimeError`` with operator-facing instructions if either
    requirement is missing — fail loud per the no-silent-fail rule.
    """
    try:
        from krepis.llm import LLMClient
        from krepis.llm_capture import capture_llm_call
    except ImportError as e:
        raise RuntimeError(
            "--judge llm requires the `krepis` adapter. Install via "
            "`pip install 'mnemon-memory[judge]'`, or use --judge "
            "embedding (default)."
        ) from e

    try:
        spec = _resolve_judge_router_edge(group, max_tokens=JUDGE_MAX_TOKENS)
    except RouterUnresolvable as exc:
        raise RuntimeError(str(exc)) from exc

    client = LLMClient(spec, callsite_id="mnemon.build_standing_set.judge")
    sft_sink = os.environ.get(JUDGE_SFT_SINK_ENV) or str(
        Path.home() / ".mnemon" / "sft" / "judge.jsonl"
    )
    scores: dict[int, float] = {}

    print(
        f"# LLM-judge scoring {len(docs)} memories via group={group!r} "
        f"({spec.provider}:{spec.model}) ...",
        file=sys.stderr,
    )
    for d in docs:
        user_block = (
            f"Memory title: {d['title'] or '(no title)'}\n"
            f"Content type: {d['content_type']}\n"
            f"Content: {(d['content'] or '')[:1500]}"
        )
        result = None
        try:
            result = client.complete(
                system=JUDGE_RUBRIC_PROMPT,
                user_content=user_block,
                max_tokens=JUDGE_MAX_TOKENS,
            )
            text = result.text
            parsed = _parse_judge_response(text)
            dim_values = [parsed.get(k, 3) for k in JUDGE_RUBRIC_DIMENSIONS]
            mean_raw = sum(dim_values) / len(dim_values)
            scores[d["id"]] = mean_raw / 5.0  # normalize 1-5 → 0.2-1.0
            rationale = parsed.get("rationale", "")
            print(
                f"  #{d['id']:>5}  score={scores[d['id']]:.2f}  "
                f"({'+'.join(str(v) for v in dim_values)})  {rationale}",
                file=sys.stderr,
            )
        except Exception as exc:
            # Best-effort — a single failed call shouldn't abort the
            # whole run. Default to 0.0 (no constraint signal) so the
            # other behavioral signals (correction, contradiction,
            # breadth) still rank the memory.
            print(
                f"  #{d['id']:>5}  ERROR ({type(exc).__name__}: {exc}); "
                f"falling back to 0.0",
                file=sys.stderr,
            )
            scores[d["id"]] = 0.0

        # Distillation capture — OUTSIDE the best-effort block above: a
        # capture failure with the flag enabled must abort loudly (per
        # krepis SftCaptureWriteError semantics), never silently zero a
        # valid score or silently drop training data. No-op when the
        # capture flag is off.
        if result is not None:
            capture_llm_call(
                result,
                producer="mnemon_judge",
                sink_path=sft_sink,
                meta={
                    "memory_id": d["id"],
                    "content_type": d["content_type"],
                    "score": scores.get(d["id"]),
                },
            )

    return scores


def _parse_judge_response(text: str) -> dict:
    """Extract the rubric JSON object from a Haiku response.

    Haiku reliably returns valid JSON when the system prompt requests it,
    but the response may include preamble text. Locate the first ``{``
    through the matching ``}`` via bracket-counting and json.loads that
    slice. Returns ``{}`` on parse failure — caller defaults all dims
    to 3 (neutral) when keys are missing.
    """
    import json as _json
    start = text.find("{")
    if start == -1:
        return {}
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return _json.loads(text[start:i + 1])
                except _json.JSONDecodeError:
                    return {}
    return {}


def _correction_score(title: str, content: str, content_type: str, confidence: float) -> float:
    if content_type != "feedback" or confidence < 0.85:
        return 0.0
    text = f"{title or ''}\n{content or ''}"
    return 1.0 if any(p.search(text) for p in CORRECTION_PATTERNS) else 0.0


def _contradiction_counts(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT source_id, COUNT(*) FROM relations
        WHERE relation_type IN ('contradicts', 'supersedes')
        GROUP BY source_id
        """
    ).fetchall()
    return {sid: n for sid, n in rows}


def _breadth_counts(conn: sqlite3.Connection, doc_ids: list[int]) -> dict[int, int]:
    out: dict[int, int] = {}
    for doc_id in doc_ids:
        title_row = conn.execute(
            "SELECT title FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if not title_row or not title_row[0]:
            out[doc_id] = 0
            continue
        cleaned = re.sub(r"[\"'*\-+:^]", " ", title_row[0])
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            out[doc_id] = 0
            continue
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT d.content_type
                FROM documents_fts f
                JOIN documents d ON d.id = f.rowid
                WHERE documents_fts MATCH ?
                  AND d.invalidated_at IS NULL
                LIMIT 50
                """,
                (cleaned,),
            ).fetchall()
            out[doc_id] = len(rows)
        except sqlite3.OperationalError:
            out[doc_id] = 0
    return out


def _load_vectors_for_docs(
    vecstore_path: Path, docs: list[dict]
) -> tuple[list[int], np.ndarray]:
    """Load embeddings for the given docs. Returns (doc_ids_with_emb,
    embedding_matrix) — embeddings stored keyed to f'{hash}_0' in mnemon's
    VecStore convention.
    """
    if not vecstore_path.exists():
        print(
            f"WARN: vecstore not found at {vecstore_path} — embedding-based "
            f"signals (constraint/time_penalty) will be zero.",
            file=sys.stderr,
        )
        return [], np.zeros((0, 384), dtype=np.float32)

    data = np.load(str(vecstore_path), allow_pickle=True)
    stored_ids = list(data["ids"])
    stored_vecs = np.asarray(data["vectors"], dtype=np.float32)
    idx_by_vec_id = {vid: i for i, vid in enumerate(stored_ids)}

    doc_ids: list[int] = []
    rows: list[np.ndarray] = []
    for d in docs:
        vec_id = f"{d['hash']}_0"
        idx = idx_by_vec_id.get(vec_id)
        if idx is None:
            continue
        doc_ids.append(d["id"])
        rows.append(stored_vecs[idx])
    if not rows:
        return [], np.zeros((0, 384), dtype=np.float32)
    return doc_ids, np.vstack(rows)


def _cosine_max(memory_vecs: np.ndarray, exemplar_vecs: np.ndarray) -> np.ndarray:
    """For each memory, max cosine sim to any exemplar. Returns shape (N,)."""
    if memory_vecs.shape[0] == 0 or exemplar_vecs.shape[0] == 0:
        return np.zeros(memory_vecs.shape[0], dtype=np.float32)
    # L2-normalize both → cosine = dot product.
    m_norm = memory_vecs / (np.linalg.norm(memory_vecs, axis=1, keepdims=True) + 1e-9)
    e_norm = exemplar_vecs / (np.linalg.norm(exemplar_vecs, axis=1, keepdims=True) + 1e-9)
    sims = m_norm @ e_norm.T  # shape (N_mem, N_exemplar)
    return sims.max(axis=1)


def _render_block(memories: list[dict]) -> str:
    """Render selected memories as the markdown block the recall hook
    will inject. Format mirrors `_format_results` in
    context_surfacing.py for stable token shape."""
    from mnemon.safety import defang_control_markup

    SNIPPET_CHARS = 300
    lines = []
    for m in memories:
        title = defang_control_markup(m.get("title", "") or "")
        content = m.get("content", "") or ""
        ct = m.get("content_type", "note")
        snippet = defang_control_markup(content[:SNIPPET_CHARS])
        ellipsis = "..." if len(content) > SNIPPET_CHARS else ""
        lines.append(
            f"- [{ct}] **{title}** (id={m['id']})\n"
            f"  {snippet}{ellipsis}"
        )
    return "\n\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. Extracted from :func:`main` so tests can
    exercise argument validation (e.g. that the retired ``--judge
    anthropic`` choice is refused) without running the full pipeline."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--db", default=None, help="direct sqlite path")
    ap.add_argument("--vault", default=None, help="vault dir (default: ~/.mnemon)")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                    help=f"how many to auto-select (default: {DEFAULT_TOP_N}, hard ceiling: {HARD_CEILING})")
    ap.add_argument("--print-only", action="store_true",
                    help="print candidates but don't write standing.json / standing-rendered.md")
    ap.add_argument("--show", type=int, default=30,
                    help="how many top-scored candidates to display (default: 30)")
    ap.add_argument("--min-content-length", type=int, default=DEFAULT_MIN_CONTENT_LENGTH,
                    help=f"drop memories whose content is shorter than N chars BEFORE scoring "
                         f"(default: {DEFAULT_MIN_CONTENT_LENGTH}; set to 0 to disable). "
                         f"Filters out tiny noise memories like 'halt the run' / 'propagate' that "
                         f"FTS-match many queries but carry no actual constraint.")
    ap.add_argument("--exemplar-source", choices=("hybrid", "vault", "hand-tuned"),
                    default="hybrid",
                    help="exemplar selection strategy: 'hybrid' (default) extends the "
                         "hand-tuned lists with vault-sampled exemplars (defense-in-depth); "
                         "'vault' uses only vault-sampled (falls back to hand-tuned on empty "
                         "vault); 'hand-tuned' uses only the static lists (regression baseline).")
    ap.add_argument("--vault-exemplar-count", type=int, default=VAULT_EXEMPLAR_DEFAULT_COUNT,
                    help=f"how many vault exemplars to sample per class (positive + negative) "
                         f"when --exemplar-source ∈ {{hybrid, vault}} (default: "
                         f"{VAULT_EXEMPLAR_DEFAULT_COUNT})")
    ap.add_argument("--judge", choices=("embedding", "llm"),
                    default="embedding",
                    help="constraint-score backend: 'embedding' (default, no deps — "
                         "max cosine vs exemplars) or 'llm' (opt-in higher-fidelity "
                         "rubric scoring, routed through the krepis model-group "
                         f"router; tier from the {JUDGE_GROUP_ENV} env var, default "
                         f"{JUDGE_GROUP_DEFAULT!r} — one of the registry's low/med/"
                         "high/ultra groups, never a pinned model. Requires "
                         "`pip install 'mnemon-memory[judge]'`. LLM-judge replaces "
                         "the embedding constraint signal only; correction/"
                         "contradiction/breadth/time signals remain. Direct-provider "
                         "specs and the old 'anthropic' alias are retired — see "
                         "mnemon-I<N> — the judge always resolves through the "
                         "router.")
    return ap


def main() -> int:
    ap = build_arg_parser()
    args = ap.parse_args()

    if os.environ.get("MNEMON_JUDGE_LLM"):
        print(
            "ERROR: MNEMON_JUDGE_LLM is retired (it pinned a direct "
            "'provider:model' spec, bypassing the krepis router). Set "
            f"{JUDGE_GROUP_ENV} to a registry group (low/med/high/ultra) "
            "instead, or unset it to use the default "
            f"{JUDGE_GROUP_DEFAULT!r}.",
            file=sys.stderr,
        )
        return 2

    if args.top > HARD_CEILING:
        print(f"ERROR: --top {args.top} exceeds the hard ceiling of {HARD_CEILING} "
              f"(the cap is the contract — past ~20 standing stops being salient)",
              file=sys.stderr)
        return 2

    db_path = _resolve_db(args.vault, args.db)
    if not db_path.exists():
        print(f"ERROR: sqlite not found: {db_path}", file=sys.stderr)
        return 2

    vecstore_path = _resolve_vecstore(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    all_docs = [dict(r) for r in conn.execute(
        """
        SELECT d.id, d.title, d.content_type, d.confidence, d.hash, d.source_key, c.doc AS content
        FROM documents d
        JOIN content c ON d.hash = c.hash
        WHERE d.invalidated_at IS NULL
        """
    ).fetchall()]

    if not all_docs:
        print(f"vault {db_path} has no live memories", file=sys.stderr)
        return 1

    # Length filter — drop tiny noise BEFORE scoring.
    n_before = len(all_docs)
    min_len = max(0, args.min_content_length)
    if min_len > 0:
        docs = [d for d in all_docs if len(d["content"] or "") >= min_len]
    else:
        docs = all_docs
    n_filtered = n_before - len(docs)

    # Dedup near-duplicate iterations of the same memory. Caught
    # 2026-05-21 when "System-wide deploy changelog" appeared 3x in
    # auto-selected top-10 — same title, content edited iteratively
    # across sessions, three different content_hashes.
    #
    # Dedup key priority:
    #   1. source_key (post-rc16 canonical identity from mnemon.store.save)
    #   2. title (lowercased, stripped)
    #   3. id (untouchable — no-title memories or unique titles)
    #
    # Keep the most recent (highest id) per dedup key — it has the
    # most current title / confidence / content metadata.
    by_key: dict[str, dict] = {}
    for d in docs:
        sk = (d["source_key"] or "").strip() if "source_key" in d.keys() else ""
        title = (d["title"] or "").strip().lower()
        if sk:
            key = f"sk:{sk}"
        elif title:
            key = f"title:{title}"
        else:
            # No source_key, no title — never dedup with another memory.
            key = f"id:{d['id']}"
        existing = by_key.get(key)
        if existing is None or d["id"] > existing["id"]:
            by_key[key] = d
    n_deduped = len(docs) - len(by_key)
    docs = list(by_key.values())

    print(f"# Standing-tier scoring (embedding-based, SOTA non-LLM)", file=sys.stderr)
    print(f"# Vault:    {db_path}", file=sys.stderr)
    print(f"# Vecstore: {vecstore_path}", file=sys.stderr)
    print(
        f"# Live memories: {len(docs)} "
        f"(filtered {n_filtered} below {min_len}-char, "
        f"deduped {n_deduped} content-hash duplicates)",
        file=sys.stderr,
    )

    # Resolve exemplar lists per --exemplar-source.
    # vault-derived exemplars adapt per-user without hand-tuning maintenance;
    # hand-tuned encode general institutional patterns; hybrid (default)
    # gives defense-in-depth via both. See ROADMAP P1 vault-derived
    # auto-exemplars, 2026-05-27.
    constraint_texts = list(CONSTRAINT_EXEMPLARS)
    time_texts = list(TIME_BOUNDED_EXEMPLARS)
    if args.exemplar_source in ("hybrid", "vault"):
        vault_pos, vault_neg = _sample_vault_exemplars(
            conn, n=args.vault_exemplar_count,
        )
        if args.exemplar_source == "vault":
            if vault_pos:
                constraint_texts = vault_pos
            else:
                print(
                    "# WARN: --exemplar-source=vault but vault has no eligible "
                    "positive exemplars; falling back to hand-tuned list",
                    file=sys.stderr,
                )
            if vault_neg:
                time_texts = vault_neg
            else:
                print(
                    "# WARN: --exemplar-source=vault but vault has no eligible "
                    "negative exemplars; falling back to hand-tuned list",
                    file=sys.stderr,
                )
        else:  # hybrid — extend, don't replace
            constraint_texts = list(CONSTRAINT_EXEMPLARS) + vault_pos
            time_texts = list(TIME_BOUNDED_EXEMPLARS) + vault_neg
    print(
        f"# Exemplars: {len(constraint_texts)} constraint + "
        f"{len(time_texts)} time-bounded "
        f"(source: {args.exemplar_source})",
        file=sys.stderr,
    )

    # Embedding-based signals (time-penalty always uses embedding —
    # the negative class isn't a fit for rubric scoring; "is this
    # ephemeral?" is what the time exemplars measure structurally).
    print(f"# Embedding exemplars + memories ...", file=sys.stderr)
    from mnemon.embedder import embed_batch

    time_emb = np.vstack(embed_batch(time_texts))
    embedded_ids, memory_vecs = _load_vectors_for_docs(vecstore_path, docs)
    time_raw = dict(zip(embedded_ids, _cosine_max(memory_vecs, time_emb).tolist()))

    # Constraint signal: --judge picks embedding (default) or an LLM judge
    # ('llm' = routed through the krepis model-group router, tier from
    # MNEMON_JUDGE_GROUP, default "low"). Aborts with instructions if
    # LLM-judge activation or router resolution fails.
    if args.judge == "llm":
        group = os.environ.get(JUDGE_GROUP_ENV) or JUDGE_GROUP_DEFAULT
        try:
            constraint_raw = _score_via_llm_judge(docs, group=group)
        except RuntimeError as exc:
            print(f"# ERROR: {exc}", file=sys.stderr)
            return 2
    else:
        constraint_emb = np.vstack(embed_batch(constraint_texts))
        constraint_raw = dict(
            zip(embedded_ids, _cosine_max(memory_vecs, constraint_emb).tolist())
        )

    # Behavioral + breadth signals (cheap, all docs)
    correction = {
        d["id"]: _correction_score(d["title"], d["content"], d["content_type"], d["confidence"])
        for d in docs
    }
    contradiction_raw = _contradiction_counts(conn)
    breadth_raw = _breadth_counts(conn, [d["id"] for d in docs])

    # Normalize all to [0, 1]
    constraint = _normalize(constraint_raw)
    time_penalty = _normalize(time_raw)
    contradiction = _normalize({k: float(v) for k, v in contradiction_raw.items()})
    breadth = _normalize({k: float(v) for k, v in breadth_raw.items()})

    combined: dict[int, dict] = {}
    for d in docs:
        doc_id = d["id"]
        c = constraint.get(doc_id, 0.0)
        cor = correction.get(doc_id, 0.0)
        x = contradiction.get(doc_id, 0.0)
        b = breadth.get(doc_id, 0.0)
        tp = time_penalty.get(doc_id, 0.0)
        score = (
            W_CONSTRAINT * c
            + W_CORRECTION * cor
            + W_CONTRADICTION * x
            + W_BREADTH * b
            - W_TIME_PENALTY * tp
        )
        combined[doc_id] = {
            "score": score, "c": c, "cor": cor, "x": x, "b": b, "tp": tp,
        }

    by_id = {d["id"]: d for d in docs}
    ranked = sorted(combined.items(), key=lambda kv: kv[1]["score"], reverse=True)

    # Display top-show
    print(f"\n# Top-{args.show} candidates (by combined score)")
    print(f"# Format: [id] score=X.XX (constraint=C, correction=R, contra=X, breadth=B, time=T)")
    print(f"#         type confidence | title | snippet\n")
    for rank, (doc_id, sc) in enumerate(ranked[: args.show], 1):
        d = by_id[doc_id]
        title = d["title"] or "(no title)"
        snippet = (d["content"] or "")[:200].replace("\n", " ")
        marker = "★" if rank <= args.top else " "
        print(
            f"{marker} [{doc_id:>5}] score={sc['score']:6.3f} "
            f"(c={sc['c']:.2f}, cor={sc['cor']:.2f}, x={sc['x']:.2f}, "
            f"b={sc['b']:.2f}, tp={sc['tp']:.2f}) "
            f"{d['content_type']} conf={d['confidence']:.2f}"
        )
        print(f"        {title}")
        print(f"        {snippet}{'...' if len(d['content'] or '') > 200 else ''}\n")

    # Auto-select top-N and write outputs
    selected = [doc_id for doc_id, _ in ranked[: args.top]]
    selected_memories = [by_id[i] for i in selected]

    if args.print_only:
        print(f"\n# --print-only set; not writing standing.json", file=sys.stderr)
        return 0

    mnemon_dir = Path.home() / ".mnemon"
    mnemon_dir.mkdir(parents=True, exist_ok=True)
    standing_json = mnemon_dir / "standing.json"
    standing_md = mnemon_dir / "standing-rendered.md"

    standing_json.write_text(json.dumps({"ids": selected}, indent=2) + "\n")
    standing_md.write_text(_render_block(selected_memories) + "\n")

    print(f"\n# Wrote:")
    print(f"#   {standing_json} ({len(selected)} IDs)")
    print(f"#   {standing_md} (pre-rendered content for fast recall)")
    print(f"# Activate: export MNEMON_STANDING_TIER_FILE={standing_json}")
    print(f"# Override: scripts/salience_phase0.sh select <id1,id2,...>")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
