"""Every ``mnemon.*`` module imports under the installed dependency set.

A dependency constraint is a claim about what the code can import; the
matrix installs the newest resolution, so a floor that admits an
unimportable version never fails there. ``mcp>=1.27`` shipped while
``persistent_sessions.py`` imported ``mcp.server.connection`` (2.0+ only);
on a 1.27 install three test modules errored at collection and the four
Claude Code hooks ran on that interpreter (mnemon-I300). ci.yml's
``deps-floor`` job runs this file with mcp pinned at the declared floor.

Optional-extra modules (``llm``, ``judge``, ``server``) are excused only
when the missing distribution is one of those extras' own packages — a
missing CORE dependency still fails.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import mnemon

# Top-level distributions that are optional extras in pyproject.toml.
# A ModuleNotFoundError naming one of these is an extra not installed,
# not a broken floor.
_OPTIONAL_EXTRA_ROOTS = {
    "llama_cpp",
    "huggingface_hub",
    "krepis",
    "fastapi",
    "uvicorn",
    "starlette",
    "sse_starlette",
    "streamlit",
    "plotly",
    "pandas",
    "altair",
}


def _all_modules() -> list[str]:
    return sorted(
        name
        for _finder, name, _ispkg in pkgutil.walk_packages(mnemon.__path__, prefix="mnemon.")
    )


@pytest.mark.parametrize("module", _all_modules())
def test_module_imports(module: str) -> None:
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as e:
        root = (e.name or "").split(".", 1)[0]
        if root in _OPTIONAL_EXTRA_ROOTS:
            pytest.skip(f"{module}: optional extra {root!r} not installed")
        raise AssertionError(
            f"{module} failed to import under the installed dependency set: {e}"
        ) from e
