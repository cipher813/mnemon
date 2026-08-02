"""Tests for remote HTTP server configuration."""

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


class TestRemoteConfig:
    def test_default_port(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORT", None)
            # Re-import to pick up env
            import importlib
            import mnemon.server_remote as sr
            importlib.reload(sr)
            assert sr.PORT == 8502

    def test_custom_port(self):
        with patch.dict(os.environ, {"PORT": "9000"}):
            import importlib
            import mnemon.server_remote as sr
            importlib.reload(sr)
            assert sr.PORT == 9000

    def test_as_config_enabled_from_env(self, tmp_path):
        env = {
            "MNEMON_AS_ENABLED": "true",
            "MNEMON_PUBLIC_URL": "https://mnemon.example.com",
            "MNEMON_AS_PASSPHRASE": "x",
            "MNEMON_AS_KEY_DIR": str(tmp_path),
        }
        with patch.dict(os.environ, env):
            from mnemon.oauth_as import AuthorizationServerConfig
            config = AuthorizationServerConfig.from_env()
            assert config.enabled
            assert config.issuer == "https://mnemon.example.com"

    def test_as_config_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            for var in (
                "MNEMON_AS_ENABLED",
                "MNEMON_AS_PASSPHRASE",
                "MNEMON_PUBLIC_URL",
            ):
                os.environ.pop(var, None)
            from mnemon.oauth_as import AuthorizationServerConfig
            config = AuthorizationServerConfig.from_env()
            assert not config.enabled


class TestSessionManagerConfig:
    """Regression tests for the StreamableHTTP session manager wiring.

    These pin ``json_response=True`` because flipping it back to False
    re-introduces a hang: upstream's ``_session_creation_lock`` is held
    for the full duration of ``handle_request``, and in SSE response
    mode ``handle_request`` keeps the per-session SSE stream open until
    the client disconnects — so once one session is alive, every
    fresh-session POST queues behind the lock indefinitely. mnemon's
    tools are all single-shot RPCs, so json_response=True is the
    correct mode and must stay pinned True.
    """

    def test_session_manager_uses_json_response(self, monkeypatch, tmp_path):
        """Capture the kwargs passed to PersistentSessionManager when
        ``server_remote.main`` runs and assert ``json_response=True``.
        Bails out via SystemExit after the manager is instantiated so
        we don't actually start uvicorn."""
        import sys

        captured: dict = {}

        def fake_manager(*args, **kwargs):
            captured.update(kwargs)
            raise SystemExit("captured — abort before uvicorn.run")

        monkeypatch.setattr(
            "mnemon.persistent_sessions.PersistentSessionManager", fake_manager
        )
        monkeypatch.setenv("MNEMON_LOCAL_TOKEN", "x" * 32)
        monkeypatch.setenv("MNEMON_VAULT_DIR", str(tmp_path))
        monkeypatch.setenv("MNEMON_PUBLIC_URL", "http://127.0.0.1:8502")
        monkeypatch.setenv("MNEMON_ALLOWED_HOSTS", "127.0.0.1,127.0.0.1:8502")
        monkeypatch.delenv("MNEMON_AS_ENABLED", raising=False)

        for mod_name in ("mnemon.server_remote",):
            sys.modules.pop(mod_name, None)
        from mnemon.server_remote import run_remote

        with pytest.raises(SystemExit):
            run_remote()

        assert captured.get("json_response") is True, (
            f"PersistentSessionManager must be wired with json_response=True; "
            f"got {captured.get('json_response')!r}. See class docstring for why."
        )


class TestRunRemoteBranches:
    """Drive run_remote()'s startup branches with the heavy deps mocked.

    run_remote() is an orchestrator that ends in a blocking uvicorn.run();
    its real behavior is exercised end-to-end by test_integration_remote.py
    (a subprocess), but coverage.py can't see subprocess lines. These unit
    tests mock the model pre-loads + uvicorn so each branch is covered
    deterministically and fast."""

    def _base_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MNEMON_LOCAL_TOKEN", "x" * 32)
        monkeypatch.setenv("MNEMON_VAULT_DIR", str(tmp_path))
        monkeypatch.setenv("MNEMON_PUBLIC_URL", "http://127.0.0.1:8502")
        monkeypatch.setenv("MNEMON_ALLOWED_HOSTS", "127.0.0.1,127.0.0.1:8502")
        monkeypatch.delenv("MNEMON_AS_ENABLED", raising=False)

    def _stub_models(self, monkeypatch):
        monkeypatch.setattr("mnemon.embedder._get_model", lambda: object())
        monkeypatch.setattr("mnemon.nli.prewarm", lambda: None)

    def test_happy_path_calls_uvicorn_run(self, monkeypatch, tmp_path, caplog):
        self._base_env(monkeypatch, tmp_path)
        self._stub_models(monkeypatch)
        fake_run = MagicMock()
        monkeypatch.setattr("uvicorn.run", fake_run)

        from mnemon.server_remote import run_remote

        caplog.set_level(logging.INFO)
        run_remote()

        fake_run.assert_called_once()
        # host/port forwarded; wrapped ASGI app passed positionally
        assert fake_run.call_args.kwargs.get("port") == int(os.environ.get("PORT", "8502"))
        assert "local static bearer token enabled" in caplog.text

    def test_embedder_preload_failure_warns_but_continues(self, monkeypatch, tmp_path, caplog):
        self._base_env(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "mnemon.embedder._get_model",
            MagicMock(side_effect=RuntimeError("model boom")),
        )
        monkeypatch.setattr("mnemon.nli.prewarm", lambda: None)
        monkeypatch.setattr("uvicorn.run", MagicMock())

        from mnemon.server_remote import run_remote

        caplog.set_level(logging.WARNING)
        run_remote()  # WARN, not fatal

        assert "failed to pre-load embedding model" in caplog.text

    def test_nli_preload_failure_warns_but_continues(self, monkeypatch, tmp_path, caplog):
        self._base_env(monkeypatch, tmp_path)
        monkeypatch.setattr("mnemon.embedder._get_model", lambda: object())
        monkeypatch.setattr(
            "mnemon.nli.prewarm",
            MagicMock(side_effect=RuntimeError("nli boom")),
        )
        monkeypatch.setattr("uvicorn.run", MagicMock())

        from mnemon.server_remote import run_remote

        caplog.set_level(logging.WARNING)
        run_remote()

        assert "failed to pre-load NLI classifier" in caplog.text

    def test_as_misconfigured_exits(self, monkeypatch, tmp_path):
        self._base_env(monkeypatch, tmp_path)
        self._stub_models(monkeypatch)
        # AS enabled but invalid → validate() returns problems → exit(1).
        fake_as = MagicMock()
        fake_as.enabled = True
        fake_as.validate.return_value = ["MNEMON_AS_PASSPHRASE not set"]
        monkeypatch.setattr(
            "mnemon.oauth_as.AuthorizationServerConfig.from_env",
            lambda: fake_as,
        )

        from mnemon.server_remote import run_remote
        with pytest.raises(SystemExit):
            run_remote()

    def test_uvicorn_missing_exits(self, monkeypatch, tmp_path):
        self._base_env(monkeypatch, tmp_path)
        self._stub_models(monkeypatch)
        # Make `import uvicorn` raise ImportError inside run_remote.
        monkeypatch.setitem(sys.modules, "uvicorn", None)

        from mnemon.server_remote import run_remote
        with pytest.raises(SystemExit):
            run_remote()

    def test_decay_sweep_closure_runs_decay_and_closes_store(self, monkeypatch, tmp_path):
        self._base_env(monkeypatch, tmp_path)
        self._stub_models(monkeypatch)

        captured: dict = {}

        def fake_manager(*_args, **kwargs):
            captured.update(kwargs)
            raise SystemExit("abort before uvicorn")

        monkeypatch.setattr(
            "mnemon.persistent_sessions.PersistentSessionManager", fake_manager
        )

        from mnemon.server_remote import run_remote
        with pytest.raises(SystemExit):
            run_remote()

        decay_fn = captured["decay_fn"]
        fake_store = MagicMock()
        monkeypatch.setattr("mnemon.store.Store", lambda: fake_store)
        monkeypatch.setattr(
            "mnemon.contradiction.apply_confidence_decay", lambda _s: 7
        )
        assert decay_fn() == 7
        fake_store.close.assert_called_once()


class TestMcpServer:
    def test_mcp_has_tools(self):
        from mnemon.server import mcp
        tools = mcp._tool_manager._tools
        # 14 originals + 3 salience-tier Phase 1 (memory_promote /
        # memory_demote / memory_list_standing, added 2026-05-22)
        # + 1 memory_export_coords (server-side PCA Graph path, rc18)
        # + 1 memory_export_relations (bulk Graph-edge export, rc19)
        assert len(tools) == 19

    def test_mcp_tool_names(self):
        from mnemon.server import mcp
        tool_names = set(mcp._tool_manager._tools.keys())
        expected = {
            "memory_search",
            "memory_get", "memory_timeline",
            "memory_save", "memory_pin", "memory_forget",
            "memory_status", "memory_sweep", "memory_related",
            "memory_export_vectors", "memory_export_coords",
            "memory_export_relations",
            "memory_rebuild", "memory_check_contradictions",
            "profile_get", "profile_update",
            # Salience tier Phase 1 (added 2026-05-22) —
            # private/mnemon-salience-tier-plan-260521.md
            "memory_promote", "memory_demote", "memory_list_standing",
        }
        assert tool_names == expected
