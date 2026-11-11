"""Hermes plugin registration for the Hybrid Critic Loop.

``register(ctx)`` wires tools, a slash command, a CLI command, and the two
lifecycle hooks onto the Hermes context.  Everything is built on ``ctx``
plus the standard library — no other Hermes imports.  The loop, contracts,
executors and standing modules are imported lazily inside handlers so
registration itself never depends on them.

State lives in the plugin data dir, never in the plugin directory.
"""

from __future__ import annotations

import json
import os
import shlex
import time
import uuid
from pathlib import Path

from . import hooks as hooks_mod
from .progress import format_status_report, read_run_status
from .reports import build_matrix_view, ledger_report, lessons_report
from .router import ensure_routes

REGISTRATION_NOTES = []

# The store created by the most recent register() call (handy for tests
# and for the CLI handler bound to the same registration).
ACTIVE_STORE = None
ACTIVE_DATA_DIR = None
ACTIVE_CTX = None

_SYSTEM_PROMPT_GUIDANCE = (
    "## The Millionaire Matrix (8-to-1 Multi-Family Critic Loop)\n"
    "When the user asks to run, inspect, fix, or improve code using **Millionaire Matrix** "
    "(or `/mm` / `/critic`):\n"
    "- **Improve / refactor passing code**: call `critic_run` with `repo_path`, `task`, "
    "`mode='improve'`, a single-command `test_cmd` (executed directly with `shell=False`, "
    "e.g. `'pytest -q'` or `'python3 -m unittest'` — never shell `&&` or `|`), and "
    "`target_files` listing the repo-relative files to improve.\n"
    "- **Fix failing tests**: call `critic_run` with `mode='fix'` so the baseline failure "
    "is verified before the 8 model families compete.\n"
    "- **Live status / 'is it running?'**: inspect `active_run` via `critic_ledger`, or point "
    "the user to `/mm status`, `/mm panel`, `hermes critic status`, or the Desktop "
    "**Millionaire Matrix** status-bar chip and right-side pane.\n"
    "- **Human gate**: `critic_run` stops at `awaiting_human_gate`. Show the winning seat, "
    "summary diff/scores (`critic_matrix`), and ask the user before calling `critic_gate`."
)


# --------------------------------------------------------------------------
# Data dir resolution (defensive chain)
# --------------------------------------------------------------------------

def resolve_data_dir(ctx=None) -> Path:
    """Resolve the plugin data dir without ever touching the plugin dir.

    Precedence, in order:

    1. the Hermes ctx's own ``plugin_data_dir()`` — a forward-compat
       probe only: the real host's PluginContext has no such attribute,
       so this step is inert today and exists in case a future host
       grows one;
    2. an explicit ``HCL_DATA_DIR`` override;
    3. Hermes's ``plugins.plugin_storage`` helper, when importable —
       in practice the authoritative in-host source;
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
    call, so registration and the store-inspecting tools still work in a
    partial checkout.  The real SQLite Store from state.py is always
    preferred.
    """

    def close(self):
        """No-op (fix spec H2; round-5 upgrade F2): the fallback holds
        no file handle or connection, but callers close every store
        uniformly — the real Store's ``close()`` must not become an
        AttributeError just because a partial checkout fell back."""
        return None

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
    """Open the plugin's SQLite store at ``<data_dir>/data.db``.

    The in-memory ``_FallbackStore`` serves exactly one case: a partial
    checkout without ``state.py``. A database that cannot be opened (locked,
    corrupt, a directory in its place) raises with the path in the message
    — silently falling back would register a plugin that forgets every run
    (fix pass Wave 2.8, review M7 / 1C-10).
    """
    db_path = Path(data_dir) / "data.db"
    try:
        from .state import Store
    except ImportError:
        try:
            from hybrid_critic_loop.state import Store
        except ImportError:
            return _FallbackStore(db_path)
    try:
        return Store(db_path)
    except Exception as exc:
        raise RuntimeError(
            f"hybrid-critic-loop: cannot open its database at {db_path}: {exc}"
        ) from exc


# --------------------------------------------------------------------------
# Contract / executor helpers (lazy imports live here)
# --------------------------------------------------------------------------

def _infer_test_cmd(repo_path: str | Path | None) -> str:
    """Detect a safe single-executable hard gate command when shorthand ``test_cmd`` is omitted."""
    if repo_path:
        root = Path(repo_path)
        if (root / "Cargo.toml").is_file():
            return "cargo test"
        if (root / "go.mod").is_file():
            return "go test ./..."
        if (root / "package.json").is_file():
            return "npm test"
        has_py_tests = (
            (root / "tests").is_dir()
            or any(root.glob("test_*.py"))
            or any(root.glob("*_test.py"))
        )
        if has_py_tests:
            return "python3 -m unittest"
        if any(root.rglob("*.py")):
            return "python3 -m compileall -q ."
    raise ValueError(
        "test_cmd is required for shorthand task runs when no standard test suite is detected in repo_path"
    )


def _build_shorthand_contract(args: dict) -> dict:
    """Synthesize a Millionaire Matrix contract from shorthand ``critic_run`` arguments."""
    task = str(args.get("task") or "").strip()
    mode = str(args.get("mode") or "improve").strip().lower()
    contract_id = str(
        args.get("contract_id") or f"mm_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    ).strip()
    domain = str(args.get("domain") or "code").strip()
    test_cmd = str(args.get("test_cmd") or "").strip() or _infer_test_cmd(args.get("repo_path"))
    min_score = float(args.get("min_score", 6.0))
    judges = int(args.get("judges", 8))
    spread_threshold = float(args.get("spread_threshold", 3.5))
    raw: dict = {
        "id": contract_id,
        "mode": mode,
        "stage": str(args.get("stage") or "implement"),
        "task": task,
        "domain": domain,
        "tags": list(args.get("tags") or [mode, "millionaire-matrix"]),
        "acceptance": [
            {"class": "hard", "cmd": test_cmd, "expect_exit": 0},
            {
                "class": "judged",
                "rubric": "validity_matrix_v1",
                "min": min_score,
                "judges": judges,
                "spread_threshold": spread_threshold,
            },
        ],
    }
    if args.get("target_file"):
        raw["target_file"] = args["target_file"]
    if args.get("target_files"):
        raw["target_files"] = list(args["target_files"])
    if args.get("min_usage_fraction") is not None:
        raw["min_usage_fraction"] = args["min_usage_fraction"]
    return raw


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
    elif args.get("task"):
        raw = _build_shorthand_contract(args)
    if raw is None:
        return None
    try:
        from . import contracts as contracts_mod

        return contracts_mod.validate_contract(raw)
    except ImportError:
        return raw


def _build_executors(spec, ctx=None):
    """Resolve the executors argument: 'live' (default), 'hermes', 'cli', 'local', or a dict."""
    if isinstance(spec, dict):
        return spec
    if spec == "local":
        from .executors import LocalSolverExecutor

        return {
            "solver-naive": LocalSolverExecutor("solver-naive", "naive"),
            "solver-ring": LocalSolverExecutor("solver-ring", "ring"),
        }
    target_ctx = ctx if ctx is not None else ACTIVE_CTX
    if spec == "hermes":
        from .executors_hermes import build_hermes_executors

        return build_hermes_executors(target_ctx)
    if spec == "cli":
        from .executors import default_executors

        return default_executors()
    if target_ctx is not None and getattr(target_ctx, "llm", None) is not None:
        from .executors_hermes import build_hermes_executors

        hermes_seats = build_hermes_executors(target_ctx)
        if hermes_seats and any(ex.available()[0] for ex in hermes_seats.values()):
            return hermes_seats
    from .executors import default_executors

    return default_executors()


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
        "Run one Millionaire Matrix / Hybrid Critic Loop contract: 8 model "
        "families implement or improve the task in isolated git worktrees, "
        "hard/measured gates plus the blind 8->6->4->2->1 peer validity matrix "
        "pick one winner, and the result stops at the human gate — complete it "
        "with critic_gate or the CLI after user confirmation. Accepts either a "
        "full contract/contract_path OR shorthand task + mode ('improve' for "
        "passing repos, 'fix' for failing tests) + test_cmd + target_files. "
        "Gate commands execute on the operator's machine, starting with the "
        "baseline run: author contracts only from parties trusted to run code. "
        "Returns the run result as JSON."
    ),
    "type": "object",
    "properties": {
        "contract": {
            "type": "object",
            "description": "Full contract object (task, domain, tags, mode, structured acceptance items).",
        },
        "contract_path": {
            "type": "string",
            "description": "Path to a contract JSON file, used when contract is not given inline.",
        },
        "repo_path": {
            "type": "string",
            "description": "Path to the git repository the candidates work against.",
        },
        "task": {
            "type": "string",
            "description": "Shorthand task description when contract/contract_path is omitted.",
        },
        "mode": {
            "type": "string",
            "enum": ["improve", "fix"],
            "description": "'improve' (shorthand default) requires baseline tests to pass and non-empty code changes; 'fix' requires baseline tests to fail first.",
        },
        "test_cmd": {
            "type": "string",
            "description": "Single executable test command for shorthand runs (run with shell=False, e.g. 'pytest -q' or 'python3 -m unittest').",
        },
        "target_file": {
            "type": "string",
            "description": "Repo-relative file path for the 8-family panel to inspect and edit.",
        },
        "target_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Repo-relative file paths for the 8-family panel to inspect and edit.",
        },
        "domain": {
            "type": "string",
            "description": "Task domain for routing and lessons (default 'code').",
        },
        "min_score": {
            "type": "number",
            "description": "Minimum peer validity score on the 0-10 rubric (default 6.0).",
        },
        "spread_threshold": {
            "type": "number",
            "description": "Maximum inner peer-mean spread before escalating for panel split (shorthand default 3.5).",
        },
        "min_usage_fraction": {
            "type": "number",
            "description": "Optional minimum verified quota fraction left [0.0..1.0] required per pool before fielding a seat.",
        },
        "contract_id": {
            "type": "string",
            "description": "Optional contract id for shorthand runs (auto-generated when omitted).",
        },
        "executors": {
            "type": "string",
            "enum": ["live", "hermes", "cli", "local"],
            "description": "'live' uses the 8-family Hermes panel when available (falling back to expert CLIs); 'hermes' forces Hermes 8-family seats; 'cli' forces local expert CLIs; 'local' uses the two built-in demo solvers.",
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

_CRITIC_GATE_SCHEMA = {
    "description": (
        "Complete the human gate of a contract whose run ended awaiting it: "
        "approve merges the winner, reject opens a follow-up contract carrying "
        "the rejection, waive accepts a measured-gate failure (never a hard one). "
        "The decision is recorded on the verdict and cannot be replayed."
    ),
    "type": "object",
    "properties": {
        "contract_id": {"type": "string", "description": "Contract whose human gate is pending."},
        "decision": {
            "type": "string",
            "enum": ["approved", "rejected", "waived"],
            "description": "The human's decision.",
        },
        "reason": {"type": "string", "description": "Optional reason, recorded with a rejection."},
    },
    "required": ["contract_id", "decision"],
}


def _tool_schema(name: str, raw: dict) -> dict:
    """The schema shape Hermes serialises into the provider request:
    ``{"name", "description", "parameters": {JSON Schema object}}``.
    Without the ``parameters`` block the model is shown an argument-less
    tool (fix pass Wave 2.6, review H9)."""
    return {
        "name": name,
        "description": raw.get("description", ""),
        "parameters": {
            "type": "object",
            "properties": dict(raw.get("properties") or {}),
            "required": list(raw.get("required") or []),
        },
    }


def register(ctx):
    """Register tools, the /critic & /mm commands, the `hermes critic` CLI, hooks."""
    global ACTIVE_STORE, ACTIVE_DATA_DIR, ACTIVE_CTX
    data_dir = resolve_data_dir(ctx)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = _open_store(data_dir)
    ACTIVE_STORE = store
    ACTIVE_DATA_DIR = data_dir
    ACTIVE_CTX = ctx
    ensure_routes(store)

    try:
        from .executors_hermes import register_auxiliary_tasks

        register_auxiliary_tasks(ctx)
    except Exception:
        pass

    reg_prompt = getattr(ctx, "register_system_prompt_section", None)
    if callable(reg_prompt):
        try:
            reg_prompt("millionaire_matrix", _SYSTEM_PROMPT_GUIDANCE)
        except Exception:
            pass

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
            return json.dumps({"error": "contract, contract_path, or task is required"})
        try:
            from . import loop as loop_mod
        except Exception as exc:
            return json.dumps({"error": f"loop module not available: {exc}"})
        # The run tool cannot decide its own gate (fix spec G1, round 4
        # security F7): no human_gate argument is read here — a run ends
        # awaiting the gate, and only critic_gate / the slash command /
        # the CLI complete it. And the loop call sits behind the same
        # exception boundary as every other fallible step in this handler
        # (fix spec G10, round 4 regression F2): a loop failure returns
        # the handler's {"error": …} JSON instead of raising into the
        # host, exactly as the CLI's dispatch boundary contains it.
        try:
            executors = _build_executors(args.get("executors", "live"), ctx=ctx)
        except Exception as exc:
            return json.dumps({"error": f"executors not available: {exc}"})
        try:
            result = loop_mod.run_contract(store, contract, repo_path, executors)
        except Exception as exc:  # noqa: BLE001 - surface boundary
            return json.dumps({"error": f"run failed: {exc}"})
        return json.dumps(result, default=str)

    def critic_lessons_handler(args):
        args = args or {}
        rows = store.lessons_for(
            domain=args.get("domain"),
            tags=args.get("tags"),
            limit=args.get("limit", 5),
        )
        # Render-time sanitization (round-7 fix spec K5; round-7
        # security F5): this tool hands whole stored rows to the
        # host model as JSON, and rows written before the write-time
        # nets can carry raw multi-line text — J5 swept
        # reports.lessons_report (the CLI and slash-command surface)
        # but this handler dumped the rows verbatim.  Each row now
        # passes through lessons.sanitize_lesson_row, the same
        # sanitize_free_text render the report uses: single-line,
        # capped fields; ids, counts and structure unchanged.
        from .lessons import sanitize_lesson_row

        lessons = [sanitize_lesson_row(row) for row in rows]
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
            "active_run": read_run_status(data_dir),
        }
        try:
            from .usage_feed import collect_usage_feed

            exec_map = _build_executors("live", ctx=ctx)
            payload["usage"] = collect_usage_feed(
                exec_map, data_dir=data_dir, refresh=False, include_cli_pools=True
            )
        except Exception:
            payload["usage"] = None
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

    def critic_gate_handler(args):
        args = args or {}
        contract_id = args.get("contract_id")
        decision = args.get("decision")
        if not contract_id or not decision:
            return json.dumps({"error": "contract_id and decision are required"})
        try:
            from . import loop as loop_mod
        except Exception as exc:
            return json.dumps({"error": f"loop module not available: {exc}"})
        # The decision is recorded with its real channel (fix spec G1,
        # round 4 security F7): a decision that arrived through this
        # tool is "tool", not a blanket "human". The call is wrapped
        # like the run handler's (fix spec G10, round 4 regression F2).
        try:
            result = loop_mod.record_human_gate(
                store, contract_id, decision, args.get("reason"), channel="tool"
            )
        except Exception as exc:  # noqa: BLE001 - surface boundary
            return json.dumps({"error": f"gate failed: {exc}"})
        return json.dumps(result, default=str)

    def _register(name, raw_schema, handler):
        schema = _tool_schema(name, raw_schema)
        try:
            ctx.register_tool(
                name=name, toolset="hybrid_critic_loop", schema=schema,
                handler=handler, description=schema["description"],
            )
        except TypeError:
            # Older ctx surfaces take no description keyword.
            ctx.register_tool(
                name=name, toolset="hybrid_critic_loop", schema=schema, handler=handler,
            )

    _register("critic_run", _CRITIC_RUN_SCHEMA, critic_run_handler)
    _register("critic_matrix", _CRITIC_MATRIX_SCHEMA, critic_matrix_handler)
    _register("critic_lessons", _CRITIC_LESSONS_SCHEMA, critic_lessons_handler)
    _register("critic_ledger", _CRITIC_LEDGER_SCHEMA, critic_ledger_handler)
    _register("critic_gate", _CRITIC_GATE_SCHEMA, critic_gate_handler)

    # -- slash command -------------------------------------------------------
    def critic_command(raw_args="", **kw):
        text = (raw_args or "").strip()
        try:
            parts = shlex.split(text) if text else []
        except ValueError:
            parts = text.split()
        if not parts or parts[0] in ("help", "--help", "-h"):
            return (
                "critic / mm — The Millionaire Matrix (Hybrid Critic Loop)\n"
                "  critic run <contract.json> [repo_path]\n"
                "  critic status\n"
                "  critic panel\n"
                "  critic usage\n"
                "  critic gate <contract_id> approve|reject|waive [reason]\n"
                "  critic lessons [domain]\n"
                "  critic ledger\n"
                "  critic standing"
            )
        sub = parts[0]
        if sub == "status":
            return format_status_report(data_dir)
        if sub == "panel":
            try:
                panel_execs = _build_executors("live", ctx=ctx)
            except Exception:
                panel_execs = None
            return format_status_report(data_dir, panel_executors=panel_execs)
        if sub == "usage":
            try:
                from .usage_feed import collect_usage_feed, format_usage_report

                panel_execs = _build_executors("live", ctx=ctx)
                feed = collect_usage_feed(
                    panel_execs, data_dir=data_dir, refresh=True, include_cli_pools=True
                )
                return format_usage_report(feed)
            except Exception as exc:
                return f"usage unavailable: {exc}"
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
                    "\nHuman gate pending — approve/reject via the critic_gate "
                    "tool, `critic gate <contract_id> approve|reject|waive`, "
                    "or the CLI."
                )
            return rendered
        if sub == "gate":
            if len(parts) < 3:
                return "usage: critic gate <contract_id> approve|reject|waive [reason]"
            decision = {
                "approve": "approved", "approved": "approved",
                "reject": "rejected", "rejected": "rejected",
                "waive": "waived", "waived": "waived",
            }.get(parts[2].lower(), parts[2])
            reason = " ".join(parts[3:]) or None
            out = critic_gate_handler(
                {"contract_id": parts[1], "decision": decision, "reason": reason}
            )
            try:
                return json.dumps(json.loads(out), indent=2, default=str)
            except Exception:
                return out
        if sub == "lessons":
            domain = parts[1] if len(parts) > 1 else None
            return lessons_report(store, domain=domain)
        if sub == "ledger":
            return ledger_report(store)
        if sub == "standing":
            # Lazy import (tightening spec T9; host F4): a missing or
            # broken standing module must not take registration — or
            # every other subcommand — down with it, mirroring the
            # _certificate_lines pattern in reports.py.
            try:
                from .standing import standing_report
            except ImportError:
                return "standing unavailable (the standing module failed to load)"
            return standing_report(store)
        return f"unknown critic subcommand: {sub} (try: run, status, panel, usage, gate, lessons, ledger, standing)"

    ctx.register_command(
        "critic",
        critic_command,
        "Hybrid Critic Loop: run <contract.json> [repo_path] | status | panel | usage | gate | lessons [domain] | ledger | standing",
    )
    ctx.register_command(
        "mm",
        critic_command,
        "The Millionaire Matrix: run <contract.json> [repo_path] | status | panel | usage | gate | lessons [domain] | ledger | standing",
    )

    # -- CLI command -----------------------------------------------------------
    def cli_setup(target):
        """Hermes hands the created ``hermes critic`` parser to ``setup_fn``
        (hermes_cli/main.py: ``cmd_info["setup_fn"](plugin_parser)``); older
        surfaces and the tests hand a subparsers action. Serve both (fix
        pass Wave 2.6, review 1C-02)."""
        from .cli import _add_subcommands, add_parser

        if hasattr(target, "add_parser"):
            return add_parser(target)
        return _add_subcommands(target.add_subparsers(dest="critic_command"))

    def cli_handler(args):
        from .cli import dispatch

        return dispatch(args, data_dir=data_dir)

    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):
        register_cli(
            "critic",
            "Hybrid Critic Loop / Millionaire Matrix: run | status | panel | usage | claude-statusline-tee | gate | lessons | ledger | standing | verify-cert | session-start",
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

    # Two hooks (fix pass Wave 2.10, review 1C-17). The post_tool_call /
    # post_llm_call observers of v0.2 read fields Hermes never sends and
    # wrote against a contract no finished run leaves open — a declared
    # capability that observed nothing. They are gone from the registration,
    # the manifest and the catalog alike.
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _pre_llm_call)

    return store
