#!/usr/bin/env python3
"""Handoff generator hook — Stop event.

Generates a session summary for continuity across sessions.
Saved as a "handoff" memory with 30-day half-life.

Phase 3 unification: saves go to the Fly vault via _remote_client
(``memory_save`` tool call). LLM summarization remains local.
Falls back to regex heuristics if LLM is unavailable.

Stop fires after every assistant turn, not once per session — so without
gates this hook saves a fresh handoff per prompt. A 30-prompt
conversation produces ~30 near-duplicate ``handoff`` rows whose
``session_extractor`` cousin can't dedup cross-hook. The three gates
below collapse that to roughly one save per ``HANDOFF_DEBOUNCE_SEC``
window per session_id, plus skip the trivial / system-generated
transcripts that aren't real sessions:

1. **Trivial-prompt skip** — first user line matches a slash-command,
   notification payload, or test-output paste. These aren't real
   prompts and shouldn't seed a "Session: ..." memory.
2. **Per-session debounce** — small JSON file at
   ``~/.mnemon/handoff_session_state.json`` tracks last save time per
   ``session_id``. Inside the cooldown, the hook returns before paying
   the LLM cost.
3. **Remote vector dedup** — final belt-and-suspenders check against
   recent vault content, mirroring ``session_extractor``'s
   ``is_duplicate_remote`` posture.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from ..config import HOOK_DEDUP_SIMILARITY_THRESHOLD, HOOK_DEDUP_TIMEOUT_SEC

# ── Filters ────────────────────────────────────────────────────────────────

# How long after a successful save to treat new Stop events on the same
# session_id as no-ops. 600s ≈ 10 minutes — long enough to absorb a
# normal back-and-forth working session, short enough that genuinely
# distinct work later in the day still produces a new handoff.
HANDOFF_DEBOUNCE_SEC = 600

# Per-session debounce state. Schema: ``{session_id: last_save_ts}``.
# Pruned on read; entries older than 24h are dropped to keep the file
# from growing unbounded.
_SESSION_STATE_PATH = Path.home() / ".mnemon" / "handoff_session_state.json"
_SESSION_STATE_PRUNE_SEC = 86400

# Patterns matched against the first ``[user]:`` line in the transcript.
# These all indicate non-prompt content surfacing as the conversation's
# nominal opening message — slash-command bodies (loop fires, etc.),
# Claude Code system payloads, and pasted tool output.
_TRIVIAL_FIRST_PROMPT_PATTERNS = (
    re.compile(r"^#\s*/loop\b", re.I),
    re.compile(r"^<[a-z-]+-(?:notification|caveat|output)\b", re.I),
    re.compile(r"^<command-name>", re.I),
    re.compile(r"^={5,}"),
    re.compile(r"^short test summary info", re.I),
)

# Minimum length of the first user prompt after trimming. Catches one-
# word replies like "yes", "done", "pr merged" that don't carry enough
# signal to seed a handoff.
_MIN_FIRST_PROMPT_CHARS = 15

CLIENT_LABEL_DEDUP = "claude-code-handoff-generator-dedup"

# ── LLM Summarization ──────────────────────────────────────────────────────

HANDOFF_SYSTEM_PROMPT = (
    "You are a session summarizer. Given a conversation transcript, "
    "produce a brief handoff summary for the next session.\n\n"
    "Format your response as:\n"
    "<handoff>\n"
    "  <title>Short descriptive title of the session (max 60 chars)</title>\n"
    "  <summary>\n"
    "  2-4 bullet points covering:\n"
    "  - What was accomplished\n"
    "  - Key decisions made\n"
    "  - Open questions or unfinished work\n"
    "  - Files or systems that were modified\n"
    "  </summary>\n"
    "</handoff>\n\n"
    "Rules:\n"
    "- Be concise — this is a handoff note, not a full report.\n"
    "- Focus on what the NEXT session needs to know.\n"
    "- If the session was trivial (just a question, no real work), output: <none/>"
)

CLIENT_LABEL = "claude-code-handoff-generator"


def parse_handoff(response: str) -> dict | None:
    """Parse XML-formatted handoff from LLM response."""
    title_match = re.search(r"<title>(.*?)</title>", response, re.DOTALL)
    summary_match = re.search(r"<summary>(.*?)</summary>", response, re.DOTALL)

    if not title_match or not summary_match:
        return None

    title = title_match.group(1).strip()
    summary = summary_match.group(1).strip()

    if not title or not summary:
        return None
    return {"title": title, "summary": summary}


def generate_with_llm(transcript: str) -> dict | None:
    """Generate handoff using local LLM. Returns None if LLM unavailable."""
    from ..llm import try_generate

    response = try_generate(HANDOFF_SYSTEM_PROMPT, transcript, max_tokens=500)
    if response is None:
        return None
    if "<none/>" in response:
        return {"skip": True}
    return parse_handoff(response)


# ── Regex Fallback (when LLM unavailable) ──────────────────────────────────

def generate_with_regex(transcript: str) -> dict | None:
    """Fallback: generate handoff using regex heuristics."""
    lines = transcript.split("\n")
    user_lines = [line for line in lines if line.startswith("[user]:")]
    assistant_lines = [line for line in lines if line.startswith("[assistant]:")]

    if len(user_lines) < 2:
        return None

    first_topic = user_lines[0].replace("[user]: ", "")[:100] if user_lines else "Unknown"
    exchanges = min(len(user_lines), len(assistant_lines))

    file_patterns = re.findall(
        r"(?:created?|modified?|edited?|updated?|wrote)\s+[`'\"]?([/\w.-]+\.\w+)",
        transcript, re.I,
    )
    files_modified = list(set(file_patterns))[:10]

    bullets = [f"- Topic: {first_topic}", f"- Exchanges: {exchanges}"]
    if files_modified:
        bullets.append(f"- Files touched: {', '.join(files_modified[:5])}")

    decision_matches = re.findall(
        r"(?:decided|going with|settled on|chose)\s+(.{10,100})", transcript, re.I
    )
    for d in decision_matches[:3]:
        bullets.append(f"- Decision: {d.strip().rstrip('.')}")

    return {"title": first_topic[:60], "summary": "\n".join(bullets)}


# ── Filters & dedup ────────────────────────────────────────────────────────

def _first_user_line(transcript: str) -> str:
    """Return the first ``[user]:`` line in the transcript, with the prefix
    stripped and surrounding whitespace trimmed. Empty string if the
    transcript carries no user lines (the regex fallback rejects that
    shape elsewhere; this helper just normalizes the lookup)."""
    for line in transcript.split("\n"):
        if line.startswith("[user]: "):
            return line[len("[user]: "):].strip()
    return ""


def is_trivial_first_prompt(first_user: str) -> bool:
    """True if the first user line is a slash-command body, system
    payload, pasted tool output, or too short to carry session signal.

    Real prompts can be short, but anything under ``_MIN_FIRST_PROMPT_CHARS``
    after trimming is in practice a follow-up like "yes" / "done" /
    "pr merged" — not a session-opening message that should seed a
    "Session: ..." memory title."""
    if not first_user or len(first_user) < _MIN_FIRST_PROMPT_CHARS:
        return True
    return any(p.match(first_user) for p in _TRIVIAL_FIRST_PROMPT_PATTERNS)


def _load_session_state() -> dict[str, float]:
    """Read + prune the session debounce state file. Tolerant of missing
    files, parse errors, and OSError on read — any failure mode returns
    an empty dict so the hook saves rather than silently no-ops."""
    if not _SESSION_STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(_SESSION_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    return {
        sid: ts
        for sid, ts in raw.items()
        if isinstance(sid, str)
        and isinstance(ts, (int, float))
        and now - ts < _SESSION_STATE_PRUNE_SEC
    }


def _record_session_save(session_id: str, state: dict[str, float]) -> None:
    """Persist ``state`` with ``session_id`` stamped at now. Best-effort
    — read-only home (sandbox, container) silently swallows the write
    so the save itself isn't blocked."""
    state[session_id] = time.time()
    try:
        _SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_STATE_PATH.write_text(json.dumps(state))
    except OSError:
        pass


def should_save_for_session(session_id: str) -> bool:
    """True iff this session_id has not had a handoff saved within
    ``HANDOFF_DEBOUNCE_SEC``. Empty / missing session_id → always True
    (degrades to legacy per-Stop behavior so a malformed hook payload
    doesn't drop a real save)."""
    if not session_id:
        return True
    state = _load_session_state()
    last = state.get(session_id, 0.0)
    return time.time() - last >= HANDOFF_DEBOUNCE_SEC


def is_duplicate_remote(title: str, content: str) -> bool:
    """Vector-similarity dedup against the Fly vault — mirrors
    ``session_extractor.is_duplicate_remote``. False on any error
    (network, timeout, parse) so a transient remote failure never drops
    a novel handoff. Threshold is shared with session_extractor via
    ``HOOK_DEDUP_SIMILARITY_THRESHOLD`` for consistency."""
    try:
        from ._client import get_client

        client = get_client()
        query = f"{title}: {content}"
        raw, _elapsed = client.call_tool(
            "memory_search",
            {"query": query, "limit": 3},
            timeout=HOOK_DEDUP_TIMEOUT_SEC,
            client_label=CLIENT_LABEL_DEDUP,
        )
        results = json.loads(raw)
        for r in results:
            sim = r.get("vector_similarity")
            if sim is not None and sim > HOOK_DEDUP_SIMILARITY_THRESHOLD:
                return True
        return False
    except Exception:
        return False


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        from .framework import log_hook_error, read_stdin, read_transcript
        from ._client import RemoteClientConfigError, get_client

        hook_input = read_stdin()
        transcript = read_transcript(hook_input.get("transcript_path", ""), 6000)
        if not transcript or len(transcript) < 200:
            return

        # Trivial / system-payload skip. Cheaper than LLM extraction so
        # it runs first.
        first_user = _first_user_line(transcript)
        if first_user and is_trivial_first_prompt(first_user):
            return

        # Per-session debounce. Empty session_id degrades to "always
        # save" so a malformed hook payload can't silently drop work.
        session_id = hook_input.get("session_id", "") or ""
        if not should_save_for_session(session_id):
            return

        # Try LLM generation first, fall back to regex
        handoff = generate_with_llm(transcript)
        if handoff is None:
            handoff = generate_with_regex(transcript)

        if not handoff or handoff.get("skip"):
            return

        # Final remote-side dedup (covers the case where session_id
        # rotated but content is still near-identical to a recent save).
        if is_duplicate_remote(
            f"Session: {handoff['title']}", handoff["summary"]
        ):
            print(
                f'mnemon: skipping duplicate handoff: "{handoff["title"]}"',
                file=sys.stderr,
            )
            return

        try:
            client = get_client()
            result, elapsed = client.call_tool(
                "memory_save",
                {
                    "title": f"Session: {handoff['title']}",
                    "content": handoff["summary"],
                    "content_type": "handoff",
                    "source_client": "claude-code-hook",
                    "source_key": os.getcwd(),
                },
                client_label=CLIENT_LABEL,
            )
            if session_id:
                _record_session_save(session_id, _load_session_state())
            print(f'mnemon: saved handoff "{handoff["title"]}"', file=sys.stderr)
        except RemoteClientConfigError as e:
            log_hook_error("handoff-generator", "config error", e)
        except Exception as e:
            log_hook_error("handoff-generator", "save error", e)
    except Exception as e:
        log_hook_error("handoff-generator", "error", e)


if __name__ == "__main__":
    main()
