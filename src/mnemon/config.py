"""Configuration — content types, vault paths, scoring constants."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path


class ContentType(str, Enum):
    DECISION = "decision"
    PREFERENCE = "preference"
    ANTIPATTERN = "antipattern"
    OBSERVATION = "observation"
    RESEARCH = "research"
    PROJECT = "project"
    HANDOFF = "handoff"
    NOTE = "note"


class MemoryType(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


# Content type → memory type mapping
MEMORY_TYPE_MAP: dict[ContentType, MemoryType] = {
    ContentType.DECISION: MemoryType.SEMANTIC,
    ContentType.PREFERENCE: MemoryType.SEMANTIC,
    ContentType.ANTIPATTERN: MemoryType.SEMANTIC,
    ContentType.OBSERVATION: MemoryType.SEMANTIC,
    ContentType.RESEARCH: MemoryType.SEMANTIC,
    ContentType.PROJECT: MemoryType.SEMANTIC,
    ContentType.HANDOFF: MemoryType.EPISODIC,
    ContentType.NOTE: MemoryType.SEMANTIC,
}

# Half-lives in days (None = never decay)
HALF_LIVES: dict[ContentType, int | None] = {
    ContentType.DECISION: None,
    ContentType.PREFERENCE: None,
    ContentType.ANTIPATTERN: None,
    ContentType.OBSERVATION: 90,
    ContentType.RESEARCH: 90,
    ContentType.PROJECT: 120,
    ContentType.HANDOFF: 30,
    ContentType.NOTE: 60,
}

# Default confidence per content type
DEFAULT_CONFIDENCE: dict[ContentType, float] = {
    ContentType.DECISION: 0.85,
    ContentType.PREFERENCE: 0.80,
    ContentType.ANTIPATTERN: 0.80,
    ContentType.OBSERVATION: 0.70,
    ContentType.RESEARCH: 0.70,
    ContentType.PROJECT: 0.65,
    ContentType.HANDOFF: 0.60,
    ContentType.NOTE: 0.50,
}

# Source clients whose saves are best-effort transcripts of a chat
# session, not deliberate user assertions. Their confidence is capped
# at HOOK_SOURCE_CONFIDENCE_CEILING below explicit `mnemon-mirror`
# saves (which carry the per-type default — handoff = 0.60) so an
# explicit mirror always outranks an extracted fragment at recall time.
# Closes the 2026-05-10 fragment-confidence regression (default vault
# ids 1994/1997/1998 saved as preference@0.80 / decision@0.85).
HOOK_SOURCE_CLIENTS: frozenset[str] = frozenset({"claude-code-hook"})
HOOK_SOURCE_CONFIDENCE_CEILING = 0.5

# Scoring constants
RRF_K = 60
MMR_THRESHOLD = 0.6                    # bigram Jaccard ≥ this → candidate is "too similar" to a selected result
MMR_DEMOTION_FACTOR = 0.5              # composite-score multiplier applied to MMR-demoted results
# Layer 4 (stored-injection defense): a flat composite-score multiplier
# applied to HOOK_SOURCE_CLIENTS results at rank time. Stacks on top of
# the HOOK_SOURCE_CONFIDENCE_CEILING save-time cap — the cap only moves
# the 0.25-weighted confidence term (≤0.025 composite delta), too weak
# on its own to stop an auto-captured transcript fragment dominating
# unprompted recall (the 2026-05-18 Desktop incident). 0.85 ≈ a hook
# capture needs ~18% more relevance+recency to tie an equal-relevance
# user-authored memory. Explicit memory_get(id) bypasses composite
# scoring entirely, so direct lookups are unaffected.
PROVENANCE_DEMOTION_FACTOR = 0.85
COMPOSITE_WEIGHTS = (0.5, 0.25, 0.25)  # (relevance, recency, confidence)
RECENCY_HALF_LIFE_DAYS = 30
PIN_BOOST = 0.3

# Query expansion
QUERY_EXPANSION_MAX_TOKENS = 200       # LLM token cap for alt-query generation

# Contradiction detection
CONTRADICTION_OVERLAP_THRESHOLD = 0.7  # minimum vector similarity to treat candidate as potentially conflicting
CONTRADICTION_CONTEXT_MAX_CHARS = 500  # per-memory content truncation in the LLM classification prompt

# Capture attention Phase A (added 2026-05-22) —
# private/mnemon-capture-attention-plan-260522.md
#
# When a new memory's content is semantically close to ≥MIN_HITS prior
# memories across distinct sessions, the canonical neighbor's
# confidence is boosted and a 'restates' relation is recorded — the
# new memory itself is still saved (no information loss, per the SOTA
# preserve+relate+boost pattern). Default-off through soak; flip to
# default-on once boost-rate ≤ SOAK_BOOST_RATE_MAX AND a 20-canonical
# manual review shows ≥80% precision.
CAPTURE_ATTENTION_ENABLED = False               # feature flag — flip default after soak
CAPTURE_ATTENTION_THRESHOLD = 0.85              # cosine similarity floor; data-tune via scripts/calibrate_capture_threshold.py
CAPTURE_ATTENTION_MIN_HITS = 2                  # neighbors required to trigger
CAPTURE_ATTENTION_BOOST = 0.05                  # per-trigger confidence bump on canonical
CAPTURE_ATTENTION_REQUIRE_DISTINCT_SESSIONS = True  # neighbors must span ≥MIN_HITS distinct days
CAPTURE_ATTENTION_SOAK_BOOST_RATE_MAX = 0.25    # acceptance criterion: boosts/saves ratio ceiling over 7d

# Salience tier — standing-context recall, Phase 1 (added 2026-05-22) —
# private/mnemon-salience-tier-plan-260521.md
#
# Standing-tier memories are injected unconditionally into the Layer 1
# <mnemon-context> envelope on every prompt, regardless of query
# similarity, conditioning reasoning rather than answering it. The cap
# is the contract: past ~20, salience degrades and the tier becomes
# noise again, recreating the failure mode at a different scale.
# Default-on since 2026-05-27 — Phase 1 soak passed (activated 2026-05-22
# with 3 promoted memories: #2543 / #2401 / #2084; ~5 day window with no
# runway-style under-weighting recurrence). Env-var override
# (MNEMON_STANDING_TIER_ENABLED) remains available for operators who want
# to opt out without a code change.
STANDING_TIER_ENABLED = True                    # default-on; env-var override remains
STANDING_TIER_DEFAULT_CAP = 15                  # operator-tunable runtime cap
STANDING_TIER_HARD_CEILING = 20                 # invariant — never exceed
# Reuse Layer 4 hook-sourced set: hook-sourced memories cannot be promoted —
# operator-explicit gesture only. Composes with provenance demotion.
STANDING_TIER_BLOCKED_SOURCE_CLIENTS = HOOK_SOURCE_CLIENTS

# Hook timeouts and budgets (seconds / chars)
#
# HOOK_WALLCLOCK_BUDGET_SEC — the `timeout` Claude Code's
# ~/.claude/settings.json grants the tightest mnemon hook
# (UserPromptSubmit → context_surfacing). When a hook exceeds it Claude
# Code SIGKILLs the process and records `hook_cancelled`: no stdout, no
# stderr, no additionalContext. Source of truth is the settings file
# (claude-code-config/global/settings.json); this constant mirrors it so
# the invariant below is testable here.
HOOK_WALLCLOCK_BUDGET_SEC = 8.0

# HOOK_REMOTE_TIMEOUT_SEC — the inner asyncio.wait_for on every remote
# MCP call. Longer than the 2s the original plan called for because Fly
# cold starts (wake machine + load FastEmbed ONNX) can take 15-25s before
# the machine responds; a warm machine answers in ~1s.
#
# INVARIANT: strictly below HOOK_WALLCLOCK_BUDGET_SEC, with margin for
# interpreter start-up (~0.3s) and the hook's own output write. From
# 2026-05 to 2026-08-26 this was 8.0 == the budget, so the inner timeout
# could never fire first: every slow call was killed by Claude Code
# instead of reaching the hook's `⚠ mnemon unavailable` branch — 11
# hook_cancelled across 1,075 prompts in a 5-day window and zero
# instances of the warning the hook was designed to emit. Enforced by
# tests/test_hooks_timeout_budget.py.
HOOK_REMOTE_TIMEOUT_SEC = 6.0
HOOK_TIMEOUT_MARGIN_SEC = 1.5

# HOOK_DEDUP_TIMEOUT_SEC — tighter budget for the session_extractor
# dedup check (memory_search against the Fly vault). Runs in a Stop
# hook loop so each observation gets its own call; shorter timeout keeps
# the loop from stacking up against the 30s hook ceiling.
HOOK_DEDUP_TIMEOUT_SEC = 5.0

# HOOK_DEDUP_SIMILARITY_THRESHOLD — cosine similarity above which a new
# observation is treated as a duplicate of an existing memory.
HOOK_DEDUP_SIMILARITY_THRESHOLD = 0.92

# Context surfacing budget (context_surfacing hook)
HOOK_TOKEN_BUDGET = 800          # approx tokens injected per prompt
HOOK_CHARS_PER_TOKEN = 4         # rough conversion factor
HOOK_CHAR_BUDGET = HOOK_TOKEN_BUDGET * HOOK_CHARS_PER_TOKEN

# HOOK_SITUATIONAL_MIN_VECTOR_SIMILARITY — relevance floor for unprompted
# situational-recall injection. SEARCH_LIMIT (context_surfacing.py) always
# fetches up to 12 candidates regardless of how relevant they are, so a
# query with no genuinely on-topic memory (e.g. one that happens to share a
# BM25-heavy keyword with many past session titles) previously injected all
# 12 as noise. composite_score is NOT the right gate here — same rationale
# as HOOK_DEDUP_SIMILARITY_THRESHOLD above: it's a rank-fusion score
# dominated by recency/confidence weight (COMPOSITE_WEIGHTS), not a
# similarity metric, so a recent high-confidence but topically unrelated
# memory can still score moderately high. vector_similarity (raw cosine,
# None for BM25-only matches) is the true relevance signal. Results below
# this floor, or with no vector match at all, are dropped before
# injection — the standing tier (always-on by design) is unaffected.
# First-pass heuristic for bge-small-en-v1.5; data-tune post-soak.
HOOK_SITUATIONAL_MIN_VECTOR_SIMILARITY = 0.35

# HOOK_SLOW_THRESHOLD_SEC — elapsed time above which a successful
# context_surfacing call prefixes a ⚠ slow warning. Lets the user see
# latency degradation in the prompt itself without watching logs.
HOOK_SLOW_THRESHOLD_SEC = 3.0

# Content type enum values for validation
CONTENT_TYPE_VALUES = [ct.value for ct in ContentType]


def vault_dir() -> Path:
    return Path(os.environ.get("MNEMON_VAULT_DIR", Path.home() / ".mnemon"))


def vault_path() -> Path:
    return vault_dir() / "default.sqlite"
