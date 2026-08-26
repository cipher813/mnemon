"""The inner remote timeout must fire BEFORE Claude Code kills the hook.

Claude Code grants each hook a wall-clock ``timeout`` from
``~/.claude/settings.json``; on expiry it SIGKILLs the process and records
``hook_cancelled`` — nothing the hook wrote after that point reaches the
prompt. ``context_surfacing`` catches a remote timeout and emits a visible
``⚠ mnemon unavailable`` block, but that branch only executes if the inner
``asyncio.wait_for`` expires first. With both at 8.0s (2026-05 → 2026-08-26)
the branch was unreachable: 11 ``hook_cancelled`` in 1,075 prompts, zero
warnings emitted.

The settings file lives in another repo (claude-code-config), so the budget
is mirrored as ``HOOK_WALLCLOCK_BUDGET_SEC``; the second test checks the
mirror against the live settings file when one is present on the machine
and skips on CI, where none exists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mnemon import config
from mnemon.hooks import _remote_client

_HOOK_MODULE = "mnemon.hooks.context_surfacing"


def test_inner_timeout_leaves_margin_below_wallclock_budget() -> None:
    assert (
        config.HOOK_REMOTE_TIMEOUT_SEC + config.HOOK_TIMEOUT_MARGIN_SEC
        <= config.HOOK_WALLCLOCK_BUDGET_SEC
    ), (
        "HOOK_REMOTE_TIMEOUT_SEC must sit at least HOOK_TIMEOUT_MARGIN_SEC "
        "below HOOK_WALLCLOCK_BUDGET_SEC, or the hook is killed before its "
        "own timeout handler can run"
    )
    assert config.HOOK_TIMEOUT_MARGIN_SEC >= 1.0


def test_remote_client_default_is_the_config_value() -> None:
    assert _remote_client.DEFAULT_TIMEOUT_SEC == config.HOOK_REMOTE_TIMEOUT_SEC


def _hook_timeouts_from_settings(path: Path) -> list[float]:
    data = json.loads(path.read_text())
    found: list[float] = []
    for event in data.get("hooks", {}).values():
        for group in event:
            for hook in group.get("hooks", []):
                if _HOOK_MODULE in str(hook.get("command", "")):
                    found.append(float(hook.get("timeout", 60)))
    return found


def test_mirrored_budget_matches_live_settings_when_present() -> None:
    settings = Path(os.environ.get("MNEMON_TEST_SETTINGS_PATH", Path.home() / ".claude" / "settings.json"))
    if not settings.is_file():
        pytest.skip(f"no Claude Code settings at {settings}")
    timeouts = _hook_timeouts_from_settings(settings)
    if not timeouts:
        pytest.skip(f"{_HOOK_MODULE} is not registered as a hook in {settings}")
    assert min(timeouts) == config.HOOK_WALLCLOCK_BUDGET_SEC, (
        f"settings grant {min(timeouts)}s but config mirrors "
        f"{config.HOOK_WALLCLOCK_BUDGET_SEC}s — update HOOK_WALLCLOCK_BUDGET_SEC"
    )
