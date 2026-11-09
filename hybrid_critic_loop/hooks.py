"""Hermes lifecycle hooks for the Hybrid Critic Loop.

Plain functions; the caller (plugin.py) resolves ``data_dir`` and binds the
store.  Observers must never break the host: every hook swallows its own
exceptions.  ``pre_llm_call`` is the one directive hook — on the session's
first turn it returns ``{"context": ...}`` to inject the store's standing
line above the prior lessons (ranked by domain family, the whole block
under the 10,000-character cap), and None on every later turn or when
there is nothing to inject.
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
    """Return {"context": ...} for the session's first turn, or None.

    The context is the store's standing line (the client book: lifetime
    tier) above the prior lessons, under the same 10,000-character cap.
    A store with dispatches on record but no lessons injects the standing
    line alone; a store with nothing on record injects nothing.

    Hermes passes ``is_first_turn``; when it is False nothing is injected
    (the first turn already carried the context into the conversation).
    Callers that pass no turn information (the CLI, the demo) get the
    context. The domain is the host's ``domain`` kwarg, else the session's
    recorded domain, else ``code`` — and the store matches the whole domain
    family, so a ``code`` session sees ``code.game`` lessons. Never raises.
    """
    try:
        if kw.get("is_first_turn") is False:
            return None
        domain = kw.get("domain") or _session_domain(data_dir) or "code"
        tags = kw.get("tags")
        line = None
        try:
            # Lazy import (tightening spec T9; host F4): the standing
            # lookup runs inside this try, and the hook's outer
            # never-raises guard, so a standing module that fails to
            # import degrades to lessons-only injection instead of
            # taking the hook down at module load.
            #
            # has_standing is an existence probe (round-4 fix spec
            # G3): it does not compute standing. standing_line then
            # computes the standing at most once and reuses it for the
            # line — and it is chain-gated (fix spec G8), so on a
            # broken chain the line degrades to the unavailable
            # notice while the lessons below still inject.
            from .standing import has_standing, standing_line

            if has_standing(store):
                line = standing_line(store)
        except Exception:
            line = None
        budget = 10000 - (len(line) + 1 if line else 0)
        ctx_text = lessons_context(store, domain, tags, max_chars=max(budget, 0))
        parts = [part for part in (line, ctx_text) if part]
        if parts:
            return {"context": "\n".join(parts)}
        return None
    except Exception:
        return None

