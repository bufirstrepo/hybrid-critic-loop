"""Standalone CLI for the Hybrid Critic Loop.

Usage:
    python -m hybrid_critic_loop.cli run <contract.json> --repo PATH \
        [--executors live|local] [--approve|--reject|--waive] [--data-dir PATH]
    python -m hybrid_critic_loop.cli lessons [--domain D] [--tags a,b] [--data-dir PATH]
    python -m hybrid_critic_loop.cli ledger [--data-dir PATH]
    python -m hybrid_critic_loop.cli session-start [--domain D] [--data-dir PATH]

The same argparse shape is reused by the Hermes CLI command registered in
plugin.py (``hermes critic ...``) via :func:`add_parser` / :func:`dispatch`.

Exit codes: 0 on completed / awaiting-human-gate, 2 on escalated or
refused-baseline, 1 on error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _resolve_data_dir(value=None) -> Path:
    if value:
        return Path(value).expanduser()
    env_dir = os.environ.get("HCL_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path("~/.hermes").expanduser() / "plugin-data" / "hybrid-critic-loop"


def _open(data_dir):
    """Open the store for ``data_dir`` and seed routes. Returns (store, dir)."""
    from .plugin import _open_store
    from .router import ensure_routes

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = _open_store(data_dir)
    ensure_routes(store)
    return store, data_dir


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

def _cmd_run(contract_path, repo, executors_spec, gate, data_dir) -> int:
    from . import hooks as _hooks  # noqa: F401  (keeps import surface explicit)
    from .plugin import _build_executors, _is_awaiting, _load_contract
    from .reports import verdict_report

    store, data_dir = _open(data_dir)
    contract = _load_contract({"contract_path": contract_path})
    if contract is None:
        print("error: could not load contract", file=sys.stderr)
        return 1
    executors = _build_executors(executors_spec or "live")

    if gate in ("approved", "rejected", "waived"):
        decider = lambda *a, **kw: gate  # noqa: E731
    elif sys.stdin is not None and sys.stdin.isatty():
        def decider(*a, **kw):
            answer = input(
                "Human gate — approve winner? [approve/reject/waive]: "
            ).strip().lower()
            return {
                "approve": "approved",
                "approved": "approved",
                "reject": "rejected",
                "rejected": "rejected",
                "waive": "waived",
                "waived": "waived",
            }.get(answer, answer)
    else:
        decider = None

    from . import loop as loop_mod

    result = loop_mod.run_contract(
        store, contract, repo, executors, human_decider=decider
    )
    print(json.dumps(result, indent=2, default=str))
    contract_id = contract.get("id") if isinstance(contract, dict) else None
    if contract_id:
        print(verdict_report(store, contract_id))
    if isinstance(result, dict):
        if result.get("error"):
            return 1
        status = str(result.get("status") or result.get("outcome") or "")
        if (
            result.get("escalated")
            or "escalat" in status
            or "refused" in status
        ):
            return 2
        if _is_awaiting(result) or status in ("completed", "complete", "approved", ""):
            return 0
    return 0


def _cmd_lessons(domain, tags_value, limit, data_dir) -> int:
    from .reports import lessons_report

    store, data_dir = _open(data_dir)
    tags = None
    if tags_value:
        tags = [t.strip() for t in str(tags_value).split(",") if t.strip()]
    print(lessons_report(store, domain=domain, tags=tags, limit=limit or 10))
    return 0


def _cmd_ledger(data_dir) -> int:
    from .reports import ledger_report

    store, data_dir = _open(data_dir)
    print(ledger_report(store))
    return 0


def _cmd_session_start(domain, data_dir) -> int:
    from . import hooks as hooks_mod

    store, data_dir = _open(data_dir)
    hooks_mod.on_session_start(store, data_dir, domain=domain or "code")
    result = hooks_mod.pre_llm_call(store, data_dir, domain=domain)
    if result and result.get("context"):
        print(result["context"])
    else:
        print("")
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring (shared by standalone main and the Hermes CLI command)
# ---------------------------------------------------------------------------

def _add_subcommands(subparsers):
    run_p = subparsers.add_parser("run", help="run a contract through the loop")
    run_p.add_argument("contract", help="path to a contract JSON file")
    run_p.add_argument("--repo", default=".", help="repository path (default: .)")
    run_p.add_argument(
        "--executors", choices=["live", "local"], default="live",
        help="'live' expert CLIs (default) or 'local' built-in solvers",
    )
    gate = run_p.add_mutually_exclusive_group()
    gate.add_argument("--approve", dest="gate", action="store_const",
                      const="approved", help="approve the winner at the human gate")
    gate.add_argument("--reject", dest="gate", action="store_const",
                      const="rejected", help="reject the winner at the human gate")
    gate.add_argument("--waive", dest="gate", action="store_const",
                      const="waived", help="waive the human gate")
    run_p.add_argument("--data-dir", default=None)

    lessons_p = subparsers.add_parser("lessons", help="list stored lessons")
    lessons_p.add_argument("--domain", default=None)
    lessons_p.add_argument("--tags", default=None, help="comma-separated tags")
    lessons_p.add_argument("--limit", type=int, default=10)
    lessons_p.add_argument("--data-dir", default=None)

    ledger_p = subparsers.add_parser("ledger", help="show the expert ledger")
    ledger_p.add_argument("--data-dir", default=None)

    session_p = subparsers.add_parser(
        "session-start",
        help="print the lessons context a fresh session would inject",
    )
    session_p.add_argument("--domain", default=None)
    session_p.add_argument("--data-dir", default=None)
    return subparsers


def add_parser(subparsers):
    """Add the ``critic`` parser (with its subcommands) to ``subparsers``."""
    parser = subparsers.add_parser(
        "critic", help="Hybrid Critic Loop: run contracts, lessons, ledger"
    )
    inner = parser.add_subparsers(dest="critic_command")
    _add_subcommands(inner)
    return parser


def dispatch(args, data_dir=None) -> int:
    """Dispatch a parsed argparse namespace to a subcommand. Returns exit code."""
    sub = getattr(args, "critic_command", None) or getattr(args, "command", None)
    effective_dir = (
        Path(data_dir) if data_dir else _resolve_data_dir(getattr(args, "data_dir", None))
    )
    try:
        if sub == "run":
            return _cmd_run(
                args.contract, getattr(args, "repo", "."),
                getattr(args, "executors", "live"), getattr(args, "gate", None),
                effective_dir,
            )
        if sub == "lessons":
            return _cmd_lessons(
                getattr(args, "domain", None), getattr(args, "tags", None),
                getattr(args, "limit", 10), effective_dir,
            )
        if sub == "ledger":
            return _cmd_ledger(effective_dir)
        if sub == "session-start":
            return _cmd_session_start(getattr(args, "domain", None), effective_dir)
    except Exception as exc:  # CLI boundary: errors are exit 1, not tracebacks
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("error: no critic subcommand given (run|lessons|ledger|session-start)",
          file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="critic", description="Hybrid Critic Loop command line"
    )
    subparsers = parser.add_subparsers(dest="command")
    _add_subcommands(subparsers)
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    return dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
