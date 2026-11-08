"""Hermes plugin registration for the Hybrid Critic Loop.

``register(ctx)`` wires tools, a slash command, a CLI command, and the four
lifecycle hooks onto the Hermes context.  Everything is built on ``ctx``
plus the standard library — no other Hermes imports.  The loop, contracts,
and executors modules are imported lazily inside handlers so registration
itself never depends on them.

State lives in the plugin data dir, never in the plugin directory.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from . import hooks as hooks_mod
from .reports import build_matrix_view, ledger_report, lessons_report
from .router import ensure_routes

REGISTRATION_NOTES = []

# The store created by the most recent register() call (handy for tests
# and for the CLI handler bound to the same registration).
ACTIVE_STORE = None
ACTIVE_DATA_DIR = None


# --------------------------------------------------------------------------
# Data dir resolution (defensive chain)
# --------------------------------------------------------------------------

def resolve_data_dir(ctx=None) -> Path:
    """Resolve the plugin data dir without ever touching the plugin dir.

    Precedence, in order:

    1. the Hermes ctx's own ``plugin_data_dir()`` — the host is
       authoritative when it hands us a directory;
    2. an explicit ``HCL_DATA_DIR`` override;
    3. Hermes's ``plugins.plugin_storage`` helper, when importable;
    4. ``<HERMES_HOME>/plugin-data/hybrid-critic-loop``.

    The explicit override sits above the importable helper on purpose:
    the test suite, the standalone CLI and an operator pointing a run
    elsewhere must never land in the live store just because Hermes's
    package happens to be on ``sys.path`` (Wave 2.0, F-W20-02).
    """
    if ctx is not None:
        getter = getattr(ctx, "plugin_data_dir", None)
        if callable(getter):
            try:
                result = getter()
                if result:
                    return Path(result)
            except Exception:
                pass
    env_dir = os.environ.get("HCL_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    try:
        from plugins.plugin_storage import plugin_data_dir

        result = None
        try:
            result = plugin_data_dir("hybrid-critic-loop")
        except TypeError:
            result = plugin_data_dir()
        if result:
            return Path(result)
    except Exception:
        pass
    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return hermes_home / "plugin-data" / "hybrid-critic-loop"


# --------------------------------------------------------------------------
# Store opening (real Store preferred; in-memory fallback for bare checkouts)
# --------------------------------------------------------------------------

class _FallbackStore:
    """Minimal in-memory Store subset, used only if state.py is absent.

    Implements exactly the methods this plugin (and the reports/hooks)
    call, so registration and the read-only tools still work in a partial
    checkout.  The real SQLite Store from state.py is always preferred.
    """

    def __init__(self, db_path=None):
        self.db_path = str(db_path) if db_path else None
        self._lessons = []
        self._routes = {}
        self._ledger = []
        self._summary_rows = None
        self._verdicts = {}
        self._contracts = {}
        self._open_contract = None
        self._observations = {}
        self._next = 1

    # lessons
    def create_lesson(self, contract_id=None, domain=None, tags=None,
                      outcome=None, cause=None, rule=None, route_change=None,
                      evidence=None, written_to=None, provenance="generated",
                      **kw):
        lesson_id = f"lsn-{self._next}"
        self._next += 1
        self._lessons.append({
            "id": lesson_id, "contract_id": contract_id, "domain": domain,
            "tags": list(tags or []), "outcome": outcome, "cause": cause,
            "rule": rule, "route_change": route_change,
            "evidence": list(evidence or []), "written_to": written_to,
            "provenance": provenance,
        })
        return lesson_id

    def get_lesson(self, lesson_id):
        for lesson in self._lessons:
            if lesson["id"] == lesson_id:
                return lesson
        return None

    def lessons_for(self, domain=None, tags=None, limit=5):
        # Same ranking rule as state.Store.lessons_for: exact domain first,
        # then the same family (sub-domains and ancestors), then tag
        # overlap, then newest; siblings excluded.
        def rank(actual):
            if domain is None:
                return 1
            if actual == domain:
                return 2
            if actual and (str(actual).startswith(f"{domain}.")
                           or str(domain).startswith(f"{actual}.")):
                return 1
            return 0

        rows = [l for l in self._lessons if rank(l["domain"])]
        wanted = set(tags or [])

        def key(lesson):
            overlap = len(wanted.intersection(lesson["tags"]))
            return (rank(lesson["domain"]), overlap, lesson["id"])

        rows.sort(key=key, reverse=True)
        return rows[:limit] if limit is not None else rows

    def count_lessons(self):
        return len(self._lessons)

    # routes
    def get_route(self, category):
        route = self._routes.get(category)
        return dict(route) if route else None

    def set_route(self, category, route):
        self._routes[category] = dict(route)

    def upsert_route(self, category, primary=None, compete_with=None,
                     fallback=None, judges=None, rule=None, **kw):
        if isinstance(primary, dict):
            self._routes[category] = dict(primary)
        else:
            self._routes[category] = {
                "primary": primary, "compete_with": compete_with,
                "fallback": list(fallback or []), "judges": list(judges or []),
                "rule": rule,
            }

    def apply_route_demotion(self, category, demote, promote, reason):
        route = dict(self._routes.get(category) or {})
        if route.get("primary") == demote and promote:
            route["compete_with"] = demote
            route["primary"] = promote
        self._routes[category] = route
        return dict(route)

    # ledger
    def add_ledger(self, *args, **kw):
        row = dict(kw)
        if args and not row:
            row = {"expert_id": args[0], "outcome": args[1] if len(args) > 1 else None,
                   "note": args[2] if len(args) > 2 else None}
        self._ledger.append(row)
        return len(self._ledger)

    def ledger_rows(self, expert_id=None):
        if expert_id is None:
            return list(self._ledger)
        return [r for r in self._ledger if r.get("expert_id") == expert_id]

    def ledger_summary(self):
        if self._summary_rows is not None:
            return list(self._summary_rows)
        return []

    # candidates / scores (matrix view reads; no rows in the fallback)
    def candidates_for(self, contract_id, attempt=None):
        return []

    def scores_for(self, candidate_id):
        return []

    # contracts / verdicts / observations
    def latest_verdict(self, contract_id):
        return self._verdicts.get(contract_id)

    def latest_open_contract(self):
        return self._open_contract

    def append_observation(self, contract_id, obs):
        self._observations.setdefault(contract_id, []).append(obs)

    def get_contract(self, contract_id):
        return self._contracts.get(contract_id)

    def record_expert_result(self, expert_id, category, won):
        return None


def _open_store(data_dir):
    db_path = Path(data_dir) / "data.db"
    try:
        from .state import Store

        return Store(db_path)
    except Exception:
        pass
    try:
        from hybrid_critic_loop.state import Store

        return Store(db_path)
    except Exception:
        return _FallbackStore(db_path)


# --------------------------------------------------------------------------
# Contract / executor helpers (lazy imports live here)
# --------------------------------------------------------------------------

def _load_contract(args):
    """Return a contract dict from tool/CLI args, validating if possible."""
    raw = None
    if args.get("contract") is not None:
        raw = args["contract"]
    elif args.get("contract_path"):
        path = args["contract_path"]
        try:
            from . import contracts as contracts_mod

            return contracts_mod.load_contract_file(path)
        except ImportError:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw is None:
        return None
    try:
        from . import contracts as contracts_mod

        return contracts_mod.validate_contract(raw)
    except ImportError:
        return raw


def _build_executors(spec):
    """Resolve the executors argument: 'live' (default), 'local', or a dict."""
    if isinstance(spec, dict):
        return spec
    if spec == "local":
        from .executors import LocalSolverExecutor

        return {
            "solver-naive": LocalSolverExecutor("solver-naive", "naive"),
            "solver-ring": LocalSolverExecutor("solver-ring", "ring"),
        }
    from .executors import default_executors

    return default_executors()


def _human_decider(human_gate):
    if human_gate in ("approved", "rejected", "waived"):
        return lambda *a, **kw: human_gate
    return None


def _is_awaiting(result):
    if not isinstance(result, dict):
        return False
    if result.get("awaiting_human_gate"):
        return True
    status = str(result.get("status") or result.get("outcome") or "")
    return status in ("awaiting_human_gate", "awaiting", "pending_human_gate")


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

_CRITIC_RUN_SCHEMA = {
    "description": (
        "Run one Hybrid Critic Loop contract: competing experts implement "
        "the task in isolated worktrees, hard/measured/judged gates pick a "
        "winner, and the result ends at a human gate unless human_gate is "
        "given. Returns the run result as JSON."
    ),
    "type": "object",
    "properties": {
        "contract": {
            "type": "object",
            "description": "Contract object (task, domain, tags, structured acceptance items).",
        },
        "contract_path": {
            "type": "string",
            "description": "Path to a contract JSON file, used when contract is not given inline.",
        },
        "repo_path": {
            "type": "string",
            "description": "Path to the repository the candidates work against.",
        },
        "executors": {
            "type": "string",
            "enum": ["live", "local"],
            "description": "'live' uses the configured expert CLIs (default); 'local' uses the two built-in local solvers for demos/tests.",
        },
        "human_gate": {
            "type": "string",
            "enum": ["approved", "rejected", "waived"],
            "description": "Pre-recorded human gate decision. Omit to end the run awaiting the human gate.",
        },
    },
    "required": ["repo_path"],
}

_CRITIC_LESSONS_SCHEMA = {
    "description": (
        "Fetch verified lessons from prior Hybrid Critic Loop runs for a "
        "domain family (the exact domain first, then its sub-domains and "
        "ancestors), ranked by domain match, tag overlap, newest first. Use "
        "these to avoid repeating measured failures from earlier contracts."
    ),
    "type": "object",
    "properties": {
        "domain": {"type": "string", "description": "Domain to fetch lessons for, e.g. 'code.game'."},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tags to rank by overlap with the lessons' tags.",
        },
        "limit": {"type": "integer", "description": "Maximum lessons to return (default 5)."},
    },
}

_CRITIC_MATRIX_SCHEMA = {
    "description": (
        "Show the Hybrid Critic Loop validity matrix for one contract: "
        "every candidate scored by peer judges on the validity_matrix_v1 "
        "rows (scores recorded with gate='matrix'), with per-candidate row "
        "results, dissent flags, and panel status. Returns JSON with "
        "note='not run' when no matrix scores were recorded."
    ),
    "type": "object",
    "properties": {
        "contract_id": {
            "type": "string",
            "description": "Contract whose candidates' matrix to show.",
        },
        "attempt": {
            "type": "integer",
            "description": "Optional attempt number to restrict candidates to.",
        },
    },
    "required": ["contract_id"],
}

_CRITIC_LEDGER_SCHEMA = {
    "description": (
        "Summarize the Hybrid Critic Loop expert ledger: dispatches, "
        "completions, refusals, timeouts, tokens, cost, and win rates per "
        "expert, straight from the recorded rows."
    ),
    "type": "object",
    "properties": {},
}


def register(ctx):
    """Register tools, the /critic command, the `hermes critic` CLI, hooks."""
    global ACTIVE_STORE, ACTIVE_DATA_DIR
    data_dir = resolve_data_dir(ctx)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = _open_store(data_dir)
    ACTIVE_STORE = store
    ACTIVE_DATA_DIR = data_dir
    ensure_routes(store)

    # -- tools -------------------------------------------------------------
    def critic_run_handler(args):
        args = args or {}
        repo_path = args.get("repo_path")
        if not repo_path:
            return json.dumps({"error": "repo_path is required"})
        try:
            contract = _load_contract(args)
        except Exception as exc:  # contract load/validation failure
            return json.dumps({"error": f"contract load failed: {exc}"})
        if contract is None:
            return json.dumps({"error": "contract or contract_path is required"})
        try:
            from . import loop as loop_mod
        except Exception as exc:
            return json.dumps({"error": f"loop module not available: {exc}"})
        try:
            executors = _build_executors(args.get("executors", "live"))
        except Exception as exc:
            return json.dumps({"error": f"executors not available: {exc}"})
        decider = _human_decider(args.get("human_gate"))
        result = loop_mod.run_contract(
            store, contract, repo_path, executors, human_decider=decider
        )
        return json.dumps(result, default=str)

    def critic_lessons_handler(args):
        args = args or {}
        lessons = store.lessons_for(
            domain=args.get("domain"),
            tags=args.get("tags"),
            limit=args.get("limit", 5),
        )
        return json.dumps({"lessons": lessons}, default=str)

    def critic_matrix_handler(args):
        args = args or {}
        contract_id = args.get("contract_id")
        if not contract_id:
            return json.dumps({"error": "contract_id is required"})
        view = build_matrix_view(store, contract_id, attempt=args.get("attempt"))
        return json.dumps(view, default=str)

    def critic_ledger_handler(args):
        payload = {
            "summary": store.ledger_summary(),
            "report": ledger_report(store),
        }
        # Events surface (owner review item 8): present only when the
        # store records events (events_for lands in a sibling
        # workstream); guarded so older stores keep the old shape.
        if hasattr(store, "events_for"):
            try:
                payload["events"] = store.events_for(limit=50)
            except TypeError:
                try:
                    payload["events"] = store.events_for(50)
                except Exception:
                    payload["events"] = []
            except Exception:
                payload["events"] = []
        return json.dumps(payload, default=str)

    ctx.register_tool(
        name="critic_run", toolset="hybrid_critic_loop",
        schema=_CRITIC_RUN_SCHEMA, handler=critic_run_handler,
    )
    ctx.register_tool(
        name="critic_matrix", toolset="hybrid_critic_loop",
        schema=_CRITIC_MATRIX_SCHEMA, handler=critic_matrix_handler,
    )
    ctx.register_tool(
        name="critic_lessons", toolset="hybrid_critic_loop",
        schema=_CRITIC_LESSONS_SCHEMA, handler=critic_lessons_handler,
    )
    ctx.register_tool(
        name="critic_ledger", toolset="hybrid_critic_loop",
        schema=_CRITIC_LEDGER_SCHEMA, handler=critic_ledger_handler,
    )

    # -- slash command -------------------------------------------------------
    def critic_command(raw_args="", **kw):
        text = (raw_args or "").strip()
        try:
            parts = shlex.split(text) if text else []
        except ValueError:
            parts = text.split()
        if not parts or parts[0] in ("help", "--help", "-h"):
            return (
                "critic — Hybrid Critic Loop\n"
                "  critic run <contract.json> [repo_path]\n"
                "  critic lessons [domain]\n"
                "  critic ledger"
            )
        sub = parts[0]
        if sub == "run":
            if len(parts) < 2:
                return "usage: critic run <contract.json> [repo_path]"
            repo_path = parts[2] if len(parts) > 2 else "."
            out = critic_run_handler(
                {"contract_path": parts[1], "repo_path": repo_path}
            )
            try:
                result = json.loads(out)
            except Exception:
                return out
            rendered = json.dumps(result, indent=2, default=str)
            if _is_awaiting(result):
                rendered += (
                    "\nHuman gate pending — approve/reject via the critic_run "
                    "tool with human_gate set, or the CLI."
                )
            return rendered
        if sub == "lessons":
            domain = parts[1] if len(parts) > 1 else None
            return lessons_report(store, domain=domain)
        if sub == "ledger":
            return ledger_report(store)
        return f"unknown critic subcommand: {sub} (try: run, lessons, ledger)"

    ctx.register_command(
        "critic",
        critic_command,
        "Hybrid Critic Loop: run <contract.json> [repo_path] | lessons [domain] | ledger",
    )

    # -- CLI command -----------------------------------------------------------
    def cli_setup(subparsers):
        from .cli import add_parser

        return add_parser(subparsers)

    def cli_handler(args):
        from .cli import dispatch

        return dispatch(args, data_dir=data_dir)

    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):
        register_cli(
            "critic",
            "Hybrid Critic Loop: run contracts, view lessons and the expert ledger",
            cli_setup,
            cli_handler,
        )
    else:
        REGISTRATION_NOTES.append(
            "register_cli_command not available on this ctx; "
            "CLI command 'critic' not registered (standalone cli.py still works)"
        )

    # -- hooks -----------------------------------------------------------------
    def _on_session_start(**kw):
        return hooks_mod.on_session_start(store, data_dir, **kw)

    def _pre_llm_call(**kw):
        return hooks_mod.pre_llm_call(store, data_dir, **kw)

    def _post_tool_call(**kw):
        return hooks_mod.post_tool_call(store, **kw)

    def _post_llm_call(**kw):
        return hooks_mod.post_llm_call(store, **kw)

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("post_llm_call", _post_llm_call)

    return store
