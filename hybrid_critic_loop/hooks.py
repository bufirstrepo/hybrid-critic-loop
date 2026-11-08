"""Hermes lifecycle hooks for the Hybrid Critic Loop.

Plain functions; the caller (plugin.py) resolves ``data_dir`` and binds the
store.  Observers must never break the host: every hook swallows its own
exceptions.  ``pre_llm_call`` is the one directive hook — on the session's
first turn it returns ``{"context": ...}`` to inject prior lessons (ranked
by domain family, under the 10,000-character cap), and None on every later
turn or when there is nothing to inject.
"""

from __future__ import annotations

import json
from pathlib import Path

from .lessons import lessons_context


def _session_domain(data_dir):
    try:
        raw = (Path(data_dir) / "session.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        return data.get("domain") or None
    except Exception:
        return None


def on_session_start(store, data_dir, **kw) -> None:
    """Bookkeeping only: remember the session's domain. Never raises."""
    try:
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "domain": kw.get("domain") or "code",
            "session_id": kw.get("session_id"),
        }
        (data_dir / "session.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    except Exception:
        pass
    return None


def pre_llm_call(store, data_dir, **kw):
    """Return {"context": lessons} for the session's first turn, or None.

    Hermes passes ``is_first_turn``; when it is False nothing is injected
    (the first turn already carried the lessons into the conversation).
    Callers that pass no turn information (the CLI, the demo) get the
    lessons. The domain is the host's ``domain`` kwarg, else the session's
    recorded domain, else ``code`` — and the store matches the whole domain
    family, so a ``code`` session sees ``code.game`` lessons. Never raises.
    """
    try:
        if kw.get("is_first_turn") is False:
            return None
        domain = kw.get("domain") or _session_domain(data_dir) or "code"
        tags = kw.get("tags")
        ctx_text = lessons_context(store, domain, tags)
        if ctx_text:
            return {"context": ctx_text}
        return None
    except Exception:
        return None

