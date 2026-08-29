"""Tests for scripts/build_standing_set.py — vault-derived auto-exemplars.

Focused unit tests for the new ``_sample_vault_exemplars`` function added
in the 2026-05-27 vault-derived-auto-exemplars PR. The function pulls
positive exemplars (high-confidence preference / decision / antipattern)
and negative exemplars (recent handoffs) from the operator's own vault
so the embedding-based scorer adapts per-user without hand-tuning
maintenance.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "build_standing_set.py"


@pytest.fixture(scope="module")
def bss():
    """Load build_standing_set.py as a module via importlib."""
    spec = importlib.util.spec_from_file_location(
        "build_standing_set", SCRIPT_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_standing_set"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def conn():
    """In-memory sqlite seeded with the documents + content schema the
    script's SQL targets. Mirrors the production schema's columns we
    actually query (id, hash, title, content_type, confidence,
    invalidated_at, created_at) without pulling the full Store
    machinery."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            hash TEXT NOT NULL,
            title TEXT,
            content_type TEXT,
            confidence REAL,
            invalidated_at TEXT,
            created_at TEXT
        );
        CREATE TABLE content (
            hash TEXT PRIMARY KEY,
            doc TEXT
        );
    """)
    return db


def _seed(conn, doc_id, content_type, confidence, title, content,
          invalidated=None, created_at="2026-05-01"):
    h = f"h{doc_id}"
    conn.execute(
        "INSERT INTO content (hash, doc) VALUES (?, ?)", (h, content),
    )
    conn.execute(
        """INSERT INTO documents
           (id, hash, title, content_type, confidence, invalidated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (doc_id, h, title, content_type, confidence, invalidated, created_at),
    )


class TestSampleVaultExemplars:
    def test_pulls_high_conf_preferences_and_decisions_as_positives(self, bss, conn):
        _seed(conn, 1, "preference", 0.85, "Pref one", "always X")
        _seed(conn, 2, "decision", 0.90, "Dec one", "chose Y over Z")
        _seed(conn, 3, "antipattern", 0.80, "Anti one", "do not Q")
        pos, neg = bss._sample_vault_exemplars(conn, n=10)
        assert len(pos) == 3
        assert any("Pref one" in p for p in pos)
        assert any("Dec one" in p for p in pos)
        assert any("Anti one" in p for p in pos)
        assert neg == []  # no handoffs seeded

    def test_excludes_below_confidence_floor(self, bss, conn):
        _seed(conn, 1, "preference", 0.85, "Strong", "high conf preference")
        _seed(conn, 2, "preference", 0.50, "Weak", "below floor")
        pos, _ = bss._sample_vault_exemplars(conn, n=10)
        assert len(pos) == 1
        assert "Strong" in pos[0]

    def test_excludes_non_durable_types(self, bss, conn):
        _seed(conn, 1, "observation", 0.95, "Obs", "passes confidence but wrong type")
        _seed(conn, 2, "research", 0.90, "Res", "also wrong type")
        _seed(conn, 3, "note", 0.95, "Note", "still wrong type")
        _seed(conn, 4, "project", 0.95, "Proj", "wrong type")
        _seed(conn, 5, "preference", 0.80, "Pref", "right type")
        pos, _ = bss._sample_vault_exemplars(conn, n=10)
        assert len(pos) == 1
        assert "Pref" in pos[0]

    def test_excludes_invalidated_memories(self, bss, conn):
        _seed(conn, 1, "preference", 0.90, "Live", "active",
              invalidated=None)
        _seed(conn, 2, "preference", 0.90, "Dead", "soft-deleted",
              invalidated="2026-05-20")
        pos, _ = bss._sample_vault_exemplars(conn, n=10)
        assert len(pos) == 1
        assert "Live" in pos[0]

    def test_pulls_recent_handoffs_as_negatives(self, bss, conn):
        _seed(conn, 1, "handoff", 0.60, "Session A",
              "first session", created_at="2026-05-20")
        _seed(conn, 2, "handoff", 0.60, "Session B",
              "second session", created_at="2026-05-25")
        _seed(conn, 3, "handoff", 0.60, "Session C",
              "third session", created_at="2026-05-15")
        _, neg = bss._sample_vault_exemplars(conn, n=10)
        assert len(neg) == 3
        # Most-recent-first ordering — recency is the negative signal.
        assert "Session B" in neg[0]
        assert "Session A" in neg[1]
        assert "Session C" in neg[2]

    def test_n_caps_sample_size(self, bss, conn):
        for i in range(20):
            _seed(conn, i + 1, "preference", 0.85,
                  f"Pref {i}", f"content for memory {i}")
        pos, _ = bss._sample_vault_exemplars(conn, n=5)
        assert len(pos) == 5

    def test_format_combines_title_and_snippet(self, bss, conn):
        _seed(conn, 1, "preference", 0.90, "Short title",
              "longer body content with multiple words")
        pos, _ = bss._sample_vault_exemplars(conn, n=10)
        assert pos[0].startswith("Short title: ")
        assert "longer body content" in pos[0]

    def test_empty_vault_returns_empty_lists(self, bss, conn):
        pos, neg = bss._sample_vault_exemplars(conn, n=10)
        assert pos == []
        assert neg == []


class TestParseJudgeResponse:
    """The judge parses Haiku's JSON response. Robust against preamble
    text, missing keys, or invalid JSON — never raises, returns {}
    on failure so caller can default missing keys."""

    def test_pure_json_object(self, bss):
        text = '{"generality": 4, "durability": 5, "imperative_shape": 3, "cross_domain": 4, "rationale": "Multi-year preference"}'
        parsed = bss._parse_judge_response(text)
        assert parsed["generality"] == 4
        assert parsed["durability"] == 5
        assert parsed["rationale"] == "Multi-year preference"

    def test_json_with_preamble(self, bss):
        text = (
            "Here is my assessment:\n\n"
            '{"generality": 5, "durability": 5, "imperative_shape": 5, '
            '"cross_domain": 5, "rationale": "perfect rule"}'
        )
        parsed = bss._parse_judge_response(text)
        assert parsed["generality"] == 5
        assert parsed["rationale"] == "perfect rule"

    def test_invalid_json_returns_empty(self, bss):
        text = "This is not JSON at all { not-valid"
        assert bss._parse_judge_response(text) == {}

    def test_no_braces_returns_empty(self, bss):
        text = "no object here, just prose"
        assert bss._parse_judge_response(text) == {}

    def test_nested_json_returns_outermost(self, bss):
        # Nested object inside a value — the bracket counter handles it.
        text = '{"a": {"b": 1}, "c": 2}'
        parsed = bss._parse_judge_response(text)
        assert parsed == {"a": {"b": 1}, "c": 2}


class TestScoreViaLlmJudge:
    """Tests for the LLM-judge backend (opt-in --judge llm).

    Migrated 2026-08-29 (mnemon-I<N>) off a caller-pinned "provider:model"
    spec string onto the krepis model-GROUP router
    (``krepis.router.resolve_group_spec``), per Brian's 2026-08-29 ruling
    that the fleet routes through the krepis router with no parallel
    setups, and that direct-Anthropic API use is retired. The krepis
    adapter (``krepis.llm``, ``krepis.llm_capture``, ``krepis.router``) is
    faked in sys.modules — no real key, SDK, or network needed (krepis is
    an operator-side opt-in install).

    ``test_pre_fix_regressions`` pins the two defects the migration fixes
    and fails against the pre-migration code: (1) ``LLMClient`` constructed
    without the now-REQUIRED ``callsite_id`` kwarg, and (2) a direct
    ``anthropic:`` spec string reachable at all. Both are asserted via the
    fake's own contract rather than by re-importing pre-fix source, so the
    test stays meaningful after the fix lands (it continues to assert the
    fixed behavior, not just "did not crash").
    """

    DOC = {"id": 1, "title": "T", "content": "C", "content_type": "preference"}
    RUBRIC_08 = (
        '{"generality": 4, "durability": 4, "imperative_shape": 4, '
        '"cross_domain": 4, "rationale": "solid"}'
    )

    def _inject_fake_krepis(
        self, monkeypatch, complete_fn, captured=None, *, route="litellm_proxy",
        provider="openrouter", model="deepseek/deepseek-v4-flash",
    ):
        """Register fake krepis.{llm,llm_capture,router} modules so the
        script's lazy imports resolve to stubs. Mirrors the real contract:
        ``resolve_group_spec(group, ...) -> (ModelSpec, route_dict)``, and
        ``LLMClient(spec, callsite_id=...)`` REQUIRES callsite_id (matching
        krepis>=0.59's real constructor, which raises on a missing/empty
        one)."""
        import sys as _sys
        import types as _types

        constructed = []
        resolve_calls = []

        class FakeClient:
            def __init__(self, spec, *, callsite_id=None, **kw):
                if not isinstance(callsite_id, str) or not callsite_id.strip():
                    raise TypeError(
                        "LLMClient requires a non-empty callsite_id — it is "
                        "the join key for cost attribution (krepis contract)."
                    )
                self.spec = spec
                self.callsite_id = callsite_id
                constructed.append(spec)

            def complete(self, **kw):
                return complete_fn(**kw)

        def resolve_group_spec(group, *, exec_context=None, wire="openai",
                                max_tokens=None, structured_outputs=None,
                                requires=()):
            resolve_calls.append(
                {"group": group, "exec_context": exec_context, "wire": wire}
            )
            spec = _types.SimpleNamespace(provider=provider, model=model)
            route_dict = {"route": route}
            return spec, route_dict

        def capture_llm_call(result, **kw):
            if captured is not None:
                captured.append((result, kw))
            return captured is not None

        pkg = _types.ModuleType("krepis")
        llm_mod = _types.ModuleType("krepis.llm")
        llm_mod.LLMClient = FakeClient
        cap_mod = _types.ModuleType("krepis.llm_capture")
        cap_mod.capture_llm_call = capture_llm_call
        router_mod = _types.ModuleType("krepis.router")
        router_mod.resolve_group_spec = resolve_group_spec
        pkg.llm, pkg.llm_capture, pkg.router = llm_mod, cap_mod, router_mod

        monkeypatch.setitem(_sys.modules, "krepis", pkg)
        monkeypatch.setitem(_sys.modules, "krepis.llm", llm_mod)
        monkeypatch.setitem(_sys.modules, "krepis.llm_capture", cap_mod)
        monkeypatch.setitem(_sys.modules, "krepis.router", router_mod)
        return constructed, resolve_calls

    def _result(self, text):
        import types as _types

        return _types.SimpleNamespace(text=text, raw_request={}, raw_response=None)

    def test_missing_krepis_raises_runtime_error(self, bss, monkeypatch):
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "krepis", None)
        monkeypatch.setitem(_sys.modules, "krepis.llm", None)
        import pytest as _p
        with _p.raises(RuntimeError, match=r"mnemon-memory\[judge\]"):
            bss._score_via_llm_judge(
                [dict(self.DOC)], group=bss.JUDGE_GROUP_DEFAULT
            )

    def test_anthropic_choice_removed_from_cli(self, bss):
        """'anthropic' is no longer a legal --judge value — direct-Anthropic
        is retired fleet-wide (2026-08-29 ruling)."""
        with pytest.raises(SystemExit):
            bss.build_arg_parser().parse_args(["--judge", "anthropic"])

    def test_judge_group_env_selects_group(self, bss, monkeypatch):
        constructed, resolve_calls = self._inject_fake_krepis(
            monkeypatch, lambda **kw: self._result("{}"), provider="openrouter",
            model="deepseek-v4-pro",
        )
        bss._score_via_llm_judge([dict(self.DOC)], group="med")
        assert resolve_calls[0]["group"] == "med"

    def test_default_group_is_low(self, bss):
        assert bss.JUDGE_GROUP_DEFAULT == "low"

    def test_happy_path_scores_each_doc(self, bss, monkeypatch):
        seen_calls = []

        def complete(**kw):
            seen_calls.append(kw)
            return self._result(self.RUBRIC_08)

        constructed, resolve_calls = self._inject_fake_krepis(monkeypatch, complete)
        scores = bss._score_via_llm_judge(
            [
                {"id": 1, "title": "A", "content": "first", "content_type": "preference"},
                {"id": 2, "title": "B", "content": "second", "content_type": "decision"},
            ],
            group="low",
        )
        assert scores[1] == pytest.approx(0.8)
        assert scores[2] == pytest.approx(0.8)
        assert len(seen_calls) == 2
        # rubric rides the system prompt; the memory rides user_content
        assert seen_calls[0]["system"] == bss.JUDGE_RUBRIC_PROMPT
        assert "first" in seen_calls[0]["user_content"]
        assert seen_calls[0]["max_tokens"] == bss.JUDGE_MAX_TOKENS
        # resolved via the router, group "low", never a pinned spec string
        assert resolve_calls[0]["group"] == "low"
        assert constructed[0].provider == "openrouter"
        # cost-attribution join key was supplied (krepis-required contract)
        # — proven by FakeClient not raising; see test_pre_fix_regressions
        # for the explicit negative case.

    def test_non_compelled_route_refused(self, bss, monkeypatch):
        """A group that resolves outside {litellm_proxy, egress_proxy}
        (e.g. krepis's own fallback landing on a bare direct-provider
        route) is refused rather than silently called —
        alpha-engine-config-I6367 / the $0 direct-Anthropic budget."""
        self._inject_fake_krepis(
            monkeypatch, lambda **kw: self._result("{}"),
            route="openrouter",  # NOT a compelled route
        )
        with pytest.raises(RuntimeError, match="not a compelled path"):
            bss._score_via_llm_judge([dict(self.DOC)], group="low")

    def test_degraded_route_is_allowed_and_logged(self, bss, monkeypatch, capsys):
        """egress_proxy IS a compelled route (registry-derived degrade path,
        model-router-policy §5) — allowed, but logged as degraded."""
        self._inject_fake_krepis(
            monkeypatch, lambda **kw: self._result(self.RUBRIC_08),
            route="egress_proxy",
        )
        bss._score_via_llm_judge([dict(self.DOC)], group="low")
        err = capsys.readouterr().err
        assert "DEGRADED" in err

    def test_router_unresolvable_becomes_runtime_error(self, bss, monkeypatch):
        import sys as _sys
        import types as _types

        def boom(*a, **kw):
            raise RuntimeError("registry has no member for group")

        pkg = _types.ModuleType("krepis")
        llm_mod = _types.ModuleType("krepis.llm")
        llm_mod.LLMClient = lambda *a, **kw: None
        cap_mod = _types.ModuleType("krepis.llm_capture")
        cap_mod.capture_llm_call = lambda *a, **kw: None
        router_mod = _types.ModuleType("krepis.router")
        router_mod.resolve_group_spec = boom
        pkg.llm, pkg.llm_capture, pkg.router = llm_mod, cap_mod, router_mod
        monkeypatch.setitem(_sys.modules, "krepis", pkg)
        monkeypatch.setitem(_sys.modules, "krepis.llm", llm_mod)
        monkeypatch.setitem(_sys.modules, "krepis.llm_capture", cap_mod)
        monkeypatch.setitem(_sys.modules, "krepis.router", router_mod)

        with pytest.raises(RuntimeError, match="did not resolve"):
            bss._score_via_llm_judge([dict(self.DOC)], group="low")

    def test_classify_failure_falls_back_to_zero(self, bss, monkeypatch):
        def complete(**kw):
            raise RuntimeError("rate limit")

        self._inject_fake_krepis(monkeypatch, complete)
        scores = bss._score_via_llm_judge(
            [{"id": 1, "title": "A", "content": "x", "content_type": "preference"}],
            group="low",
        )
        assert scores[1] == 0.0  # fallback, doesn't crash the run

    def test_missing_dims_default_to_neutral(self, bss, monkeypatch):
        """When the rubric JSON is missing a dimension, the caller
        defaults it to 3 (neutral). Score = (5 + 1 + 3 + 3) / 4 / 5 = 0.6"""
        self._inject_fake_krepis(
            monkeypatch,
            lambda **kw: self._result('{"generality": 5, "durability": 1}'),
        )
        scores = bss._score_via_llm_judge([dict(self.DOC)], group="low")
        assert scores[1] == pytest.approx(0.6)

    def test_sft_capture_invoked_per_call(self, bss, monkeypatch):
        captured = []
        self._inject_fake_krepis(
            monkeypatch, lambda **kw: self._result(self.RUBRIC_08),
            captured=captured,
        )
        bss._score_via_llm_judge([dict(self.DOC)], group="low")
        assert len(captured) == 1
        _result_obj, kw = captured[0]
        assert kw["producer"] == "mnemon_judge"
        assert kw["meta"]["memory_id"] == 1
        assert kw["meta"]["score"] == pytest.approx(0.8)

    def test_pre_fix_regressions(self, bss, monkeypatch):
        """Pins the two defects fixed by the 2026-08-29 router migration.
        Both assertions FAIL against the pre-fix source (verified by
        running this test against a checkout of the pre-fix file: the old
        ``LLMClient(spec)`` call has no ``callsite_id`` kwarg at all, and
        ``JUDGE_LLM_ANTHROPIC_SPEC`` / ``--judge anthropic`` reached
        ``anthropic:claude-haiku-4-5-20251001`` directly with no router
        involved)."""
        # 1. callsite_id is now a hard requirement of the real krepis
        #    LLMClient contract (krepis>=0.59) — the fake enforces it, and
        #    the happy path above only passes because build_standing_set.py
        #    now supplies one. Prove the attribute exists on the module's
        #    real call by constructing the fake directly the way the
        #    pre-fix code did (positional-only, no callsite_id):
        import sys as _sys
        import types as _types

        constructed, _ = self._inject_fake_krepis(
            monkeypatch, lambda **kw: self._result("{}"),
        )
        fake_llm_mod = _sys.modules["krepis.llm"]
        with pytest.raises(TypeError, match="callsite_id"):
            fake_llm_mod.LLMClient(_types.SimpleNamespace(provider="x", model="y"))

        # 2. The anthropic back-compat constant/CLI choice no longer exists.
        assert not hasattr(bss, "JUDGE_LLM_ANTHROPIC_SPEC")
        assert not hasattr(bss, "JUDGE_LLM_DEFAULT_SPEC")
        assert not hasattr(bss, "JUDGE_LLM_ENV")

        # 3. The old direct-pin env var is refused with a migration message
        #    rather than silently honored.
        monkeypatch.setenv("MNEMON_JUDGE_LLM", "anthropic:claude-haiku-4-5-20251001")
        import io as _io
        import contextlib as _contextlib
        stderr_buf = _io.StringIO()
        old_argv = _sys.argv
        _sys.argv = ["build_standing_set.py", "--print-only"]
        try:
            with _contextlib.redirect_stderr(stderr_buf):
                rc = bss.main()
        finally:
            _sys.argv = old_argv
        assert rc == 2
        assert "MNEMON_JUDGE_LLM is retired" in stderr_buf.getvalue()
