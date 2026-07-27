"""End-to-end integration test for the hook → remote server → vault path.

Spins up a real ``mnemon serve-remote`` subprocess on an ephemeral port
with a throwaway vault directory, configures the hook's ``_remote_client``
to point at it via ``MNEMON_REMOTE_URL`` + ``MNEMON_LOCAL_TOKEN``, and
exercises the actual MCP Streamable HTTP round-trip:

1. ``memory_save`` lands in the server's vault
2. ``memory_search`` retrieves the saved memory
3. Bearer token auth works end-to-end (wrong token returns 401-ish error)

Unit tests in ``test_hooks_extended.py`` mock ``call_tool_sync``. Those
catch logic bugs in the hook but not protocol-level regressions — e.g.,
an MCP SDK update changing tool call payload shape, the server changing
JSON schema, or the auth middleware rejecting a valid request. This test
closes that gap.

Marked ``@pytest.mark.integration`` so it can be skipped when not running
a full test pass::

    pytest -m 'not integration'   # skip
    pytest -m integration         # only this
    pytest                        # both

First run downloads the FastEmbed ONNX model (~13MB) if not already
cached — subsequent runs are fast (~5 sec for server boot + full test).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import urllib.error
import urllib.request

pytestmark = pytest.mark.integration


TEST_TOKEN = "test-token-integration-0123456789abcdef"
SERVER_BOOT_TIMEOUT_SEC = 60.0


def _free_port() -> int:
    """Reserve an ephemeral TCP port. The OS keeps it unlikely to be
    reused before the test server binds it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last_err = e
        time.sleep(0.5)
    raise TimeoutError(
        f"server at {url} did not become healthy within {timeout}s "
        f"(last error: {last_err})"
    )


@pytest.fixture(scope="module")
def remote_server(tmp_path_factory):
    """Start ``mnemon serve-remote`` in a subprocess on a free port.

    Scoped per-module so we pay server boot (including FastEmbed model
    load) once for the whole file, not per test.
    """
    port = _free_port()
    vault_dir = tmp_path_factory.mktemp("integration_vault")

    env = os.environ.copy()
    env["PORT"] = str(port)
    env["MNEMON_VAULT_DIR"] = str(vault_dir)
    env["MNEMON_LOCAL_TOKEN"] = TEST_TOKEN
    # Don't enable OAuth — local token only. Removes external deps.
    for k in [
        "MNEMON_OAUTH_ISSUER",
        "MNEMON_OAUTH_JWKS_URL",
        "MNEMON_OAUTH_AUDIENCE",
        "MNEMON_OAUTH_USERINFO_URL",
        "MNEMON_ALLOWED_HOSTS",
    ]:
        env.pop(k, None)

    # Use the same Python that's running pytest so virtualenv-installed
    # mnemon is picked up. Running via ``-m mnemon`` avoids any reliance
    # on the console-script entry point being on PATH in CI.
    proc = subprocess.Popen(
        [sys.executable, "-m", "mnemon", "serve-remote"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _wait_for_health(f"http://127.0.0.1:{port}/health", SERVER_BOOT_TIMEOUT_SEC)
    except TimeoutError:
        # Capture logs so the test failure is debuggable rather than
        # just "server didn't start".
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        pytest.fail(
            f"server failed to start on port {port}\n"
            f"stdout: {stdout.decode(errors='replace')[:2000]}\n"
            f"stderr: {stderr.decode(errors='replace')[:2000]}"
        )

    yield {
        "port": port,
        "url": f"http://127.0.0.1:{port}/mcp",
        "vault_dir": Path(vault_dir),
    }

    # Teardown — terminate the subprocess cleanly, kill if it hangs.
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def remote_env(monkeypatch, remote_server):
    """Point the hook's _remote_client at the subprocess for this test."""
    monkeypatch.setenv("MNEMON_REMOTE_URL", remote_server["url"])
    monkeypatch.setenv("MNEMON_LOCAL_TOKEN", TEST_TOKEN)
    return remote_server


def test_save_and_search_round_trip(remote_env):
    """Full path: call memory_save via _remote_client, then retrieve it
    via memory_search. Proves the hook → MCP SDK → Streamable
    HTTP → FastMCP → Store pipeline is intact end-to-end."""
    from mnemon.hooks._remote_client import call_tool_sync

    save_result, save_elapsed = call_tool_sync(
        "memory_save",
        {
            "title": "Integration test memory",
            "content": "This memory was saved by the integration test.",
            "content_type": "note",
            "source_client": "pytest-integration",
        },
        timeout=30.0,
        client_label="pytest-integration",
    )
    assert save_elapsed < 30.0
    # The server returns a confirmation string containing the memory title.
    assert "Integration test memory" in save_result

    # Round-trip via structured search — verifies the save actually
    # landed and indexing worked.
    search_result, _ = call_tool_sync(
        "memory_search",
        {"query": "integration test memory", "limit": 5},
        timeout=30.0,
        client_label="pytest-integration",
    )
    import json

    results = json.loads(search_result)
    assert len(results) >= 1
    titles = [r["title"] for r in results]
    assert "Integration test memory" in titles


def test_wrong_token_is_rejected(monkeypatch, remote_server):
    """A bearer token that doesn't match MNEMON_LOCAL_TOKEN must not be
    accepted. Guards the auth middleware from silently regressing to
    'any bearer token accepted' (happened in the wild with a subtle
    middleware ordering bug during Phase 3)."""
    from mnemon.hooks._remote_client import call_tool_sync

    monkeypatch.setenv("MNEMON_REMOTE_URL", remote_server["url"])
    monkeypatch.setenv("MNEMON_LOCAL_TOKEN", "this-is-not-the-right-token")

    with pytest.raises(Exception):
        call_tool_sync(
            "memory_search",
            {"query": "anything", "limit": 1},
            timeout=10.0,
            client_label="pytest-integration",
        )


def test_structured_search_parses_real_server_json(remote_env):
    """Client-side JSON parsing must handle the real server's JSON output,
    not just a mocked shape. Guards against the server changing its JSON
    schema but the client's json.loads path still superficially working
    against unit-test fixtures.
    """
    import json

    from mnemon.hooks._remote_client import call_tool_sync

    call_tool_sync(
        "memory_save",
        {
            "title": "Schema guard memory",
            "content": "A memory saved to verify JSON shape parsing end-to-end.",
            "content_type": "observation",
            "source_client": "pytest-integration",
        },
        timeout=30.0,
    )

    raw, _ = call_tool_sync(
        "memory_search",
        {"query": "schema guard memory", "limit": 5},
        timeout=30.0,
    )
    results = json.loads(raw)
    assert isinstance(results, list)
    assert len(results) >= 1
    # Every result must have the fields the client relies on. Pin the
    # schema so a server-side rename would fail loudly here.
    required = {"doc_id", "title", "content", "content_type",
                "confidence", "composite_score", "vector_similarity",
                "created_at"}
    for r in results:
        assert required.issubset(r.keys()), f"missing fields in {r}"
        assert isinstance(r["composite_score"], (int, float))
        assert isinstance(r["confidence"], (int, float))
        # vector_similarity can be None (BM25-only) or a float
        if r["vector_similarity"] is not None:
            assert isinstance(r["vector_similarity"], (int, float))
            assert 0.0 <= r["vector_similarity"] <= 1.0


def test_dedup_triggers_on_near_identical_memory(remote_env):
    """End-to-end dedup: save a memory, then ask is_duplicate_remote
    whether an identical observation would be a duplicate. The fix for
    C7 (use vector_similarity instead of composite_score) means this
    should now reliably return True. Before the fix, the threshold was
    unreachable and dedup silently never triggered."""
    from mnemon.hooks._remote_client import call_tool_sync
    from mnemon.hooks.session_extractor import is_duplicate_remote

    call_tool_sync(
        "memory_save",
        {
            "title": "Dedup end-to-end guard",
            "content": "A memory saved to verify the dedup cosine-similarity path works end-to-end.",
            "content_type": "observation",
            "source_client": "pytest-integration",
        },
        timeout=30.0,
    )

    # Same title + content → very high cosine similarity → dedup triggers.
    assert is_duplicate_remote(
        "Dedup end-to-end guard",
        "A memory saved to verify the dedup cosine-similarity path works end-to-end.",
    ) is True

    # Unrelated content → low cosine similarity → dedup does NOT trigger,
    # even if BM25 would catch stray keyword overlap.
    assert is_duplicate_remote(
        "Completely unrelated topic about alpine climbing routes in Patagonia",
        "Nothing to do with any saved memory in this test vault.",
    ) is False
