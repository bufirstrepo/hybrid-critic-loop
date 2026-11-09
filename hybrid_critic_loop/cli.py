"""Standalone CLI for the Hybrid Critic Loop.

Usage:
    python -m hybrid_critic_loop.cli run <contract.json> --repo PATH \
        [--executors live|local] [--approve|--reject|--waive] [--data-dir PATH]
    python -m hybrid_critic_loop.cli gate <contract_id> approved|rejected|waived [--reason R] [--data-dir PATH]
    python -m hybrid_critic_loop.cli lessons [--domain D] [--tags a,b] [--data-dir PATH]
    python -m hybrid_critic_loop.cli ledger [--data-dir PATH]
    python -m hybrid_critic_loop.cli standing [--data-dir PATH]
    python -m hybrid_critic_loop.cli session-start [--domain D] [--data-dir PATH]
    python -m hybrid_critic_loop.cli verify-cert <token> [--data-dir PATH]

The same argparse shape is reused by the Hermes CLI command registered in
plugin.py (``hermes critic ...``) via :func:`add_parser` / :func:`dispatch`.

Exit codes: 0 on completed / awaiting-human-gate, 2 on escalated or
refused-baseline, 1 on error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _resolve_data_dir(value=None) -> Path:
    """``--data-dir`` wins; otherwise the plugin's own chain (``HCL_DATA_DIR``,
    then Hermes's storage helper when importable, then
    ``<HERMES_HOME>/plugin-data/hybrid-critic-loop``), so the standalone CLI
    reads the same store the plugin writes (Wave 2.0, F-W20-05)."""
    if value:
        return Path(value).expanduser()
    from .plugin import resolve_data_dir

    return resolve_data_dir(None)


def _open_store_cli(data_dir):
    """``plugin._open_store`` with the lock courtesy (fix spec G2).

    When the store is locked, sqlite's bare "database is locked" names
    neither the holder nor the remedy; the CLI's error names both — the
    likely holder is another process (a Hermes session running this
    plugin holds its store for its whole lifetime), and the remedy is
    to close it or wait for it to finish.
    """
    from .plugin import _open_store

    try:
        return _open_store(data_dir)
    except Exception as exc:
        if "database is locked" in str(exc):
            raise RuntimeError(
                f"hybrid-critic-loop: cannot open its database at "
                f"{Path(data_dir) / 'data.db'}: the database is locked — "
                "another process (e.g. a Hermes session running this "
                "plugin) is holding the store open; close that process, "
                "or wait for it to finish, then retry"
            ) from exc
        raise


def _open(data_dir):
    """Open the store for ``data_dir`` and seed routes. Returns (store, dir)."""
    from .router import ensure_routes

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = _open_store_cli(data_dir)
    ensure_routes(store)
    return store, data_dir


class _NoStoreError(Exception):
    """The read-only command found no store to read (tightening spec T9)."""


_SQLITE_MAGIC = b"SQLite format 3\x00"


def _is_sqlite_file(path) -> bool:
    """True iff ``path`` starts with the SQLite database header.

    A 0-byte file reads as empty and fails the check, as does any
    non-database content (fix spec G10).
    """
    try:
        with open(path, "rb") as fh:
            return fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        return False


def _open_existing(data_dir):
    """Open an existing store read-only — never create one.

    Read-only commands (``standing``, ``verify-cert``, ``session-start``)
    inspect a store; pointing one at a fresh directory must not conjure
    the store it was asked to inspect (correctness F11), so when
    ``<data dir>/data.db`` does not exist this raises
    :class:`_NoStoreError` and creates nothing — not even the directory.

    A file that exists but is not a database — 0 bytes, or content
    without the SQLite header — is an error in its own right (fix spec
    G10): opening it as a store would conjure a schema into whatever
    the file happens to be and report on an empty book.
    """
    data_dir = Path(data_dir)
    db_file = data_dir / "data.db"
    if not db_file.exists():
        raise _NoStoreError(
            f"no store found at {data_dir} (data.db does not exist); "
            "run a contract first, or pass --data-dir for the store "
            "you meant"
        )
    if not _is_sqlite_file(db_file):
        raise _NoStoreError(
            f"store file exists but is not a database at {db_file}; "
            "the file is empty or does not carry the SQLite header — "
            "restore the book from backup (data.db, data.db-wal, "
            "data.db-shm and cert_key travel together), or remove the "
            "file and run a contract to start a new book"
        )
    store = _open_store_cli(data_dir)
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


def _cmd_gate(contract_id, decision, reason, data_dir) -> int:
    """Complete a pending human gate (fix pass Wave 2.6, review H11)."""
    from . import loop as loop_mod
    from .reports import verdict_report

    store, data_dir = _open(data_dir)
    # The decision is recorded with its real channel (fix spec G1,
    # round 4 security F7): this surface is the operator's CLI.
    result = loop_mod.record_human_gate(
        store, contract_id, decision, reason, channel="cli"
    )
    print(json.dumps(result, indent=2, default=str))
    print(verdict_report(store, contract_id))
    return 1 if result.get("status") == "error" else 0


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


def _cmd_verify_cert(token, data_dir) -> int:
    """Verify an MM CERTIFIED certificate token against this store."""
    from .chain import verify_certificate, verify_chain

    store, data_dir = _open_existing(data_dir)
    ok, payload_or_reason = verify_certificate(store, token)
    if ok:
        print("certificate: VALID")
        print(json.dumps(payload_or_reason, indent=2, default=str))
        # Scope the claim (fix spec G4): the certificate seals the
        # book's prefix as it stood at issuance. Say how the WHOLE
        # chain stands now — a break beyond the prefix does not
        # invalidate this certificate, but the operator hears about
        # it, with the first bad row named.
        chain_ok, first_bad = verify_chain(store)
        if chain_ok:
            print("chain: the whole ledger chain verifies")
        else:
            print(
                f"warning: the certificate's prefix verifies, but the "
                f"ledger chain is broken at row {first_bad} — the book "
                f"beyond this certificate does not seal"
            )
        return 0
    print(f"certificate: INVALID — {payload_or_reason}")
    return 1


def _cmd_standing(data_dir) -> int:
    from .standing import standing_report

    store, data_dir = _open_existing(data_dir)
    print(standing_report(store))
    return 0


def _cmd_session_start(domain, data_dir) -> int:
    from . import hooks as hooks_mod
    from .standing import has_standing, standing_line

    store, data_dir = _open_existing(data_dir)
    hooks_mod.on_session_start(store, data_dir, domain=domain or "code")
    result = hooks_mod.pre_llm_call(store, data_dir, domain=domain)
    if result and result.get("context"):
        # The injected context opens with the standing line (hooks.py);
        # it is the session-start standing surface as well.
        print(result["context"])
    else:
        # Nothing to inject — still show the standing line, but only
        # when the store has standing on record: the fallback gates on
        # has_standing exactly as the hook does (correctness F12,
        # host F5), so an empty store prints nothing here either.
        line = None
        try:
            if has_standing(store):
                line = standing_line(store)
        except Exception:
            line = None
        print(line or "")
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

    gate_p = subparsers.add_parser(
        "gate", help="complete a pending human gate: approve, reject or waive"
    )
    gate_p.add_argument("contract_id", help="contract whose human gate is pending")
    gate_p.add_argument("decision", choices=["approved", "rejected", "waived"])
    gate_p.add_argument("--reason", default=None, help="recorded with a rejection")
    gate_p.add_argument("--data-dir", default=None)

    lessons_p = subparsers.add_parser("lessons", help="list stored lessons")
    lessons_p.add_argument("--domain", default=None)
    lessons_p.add_argument("--tags", default=None, help="comma-separated tags")
    lessons_p.add_argument("--limit", type=int, default=10)
    lessons_p.add_argument("--data-dir", default=None)

    ledger_p = subparsers.add_parser("ledger", help="show the expert ledger")
    ledger_p.add_argument("--data-dir", default=None)

    standing_p = subparsers.add_parser(
        "standing", help="show the client book: lifetime tier and spend"
    )
    standing_p.add_argument("--data-dir", default=None)

    cert_p = subparsers.add_parser(
        "verify-cert",
        help="verify an MM CERTIFIED certificate token against this store",
    )
    cert_p.add_argument("token", help="the mm1.… certificate token")
    cert_p.add_argument("--data-dir", default=None)

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
        "critic",
        help="Hybrid Critic Loop: run contracts, lessons, ledger, standing, verify-cert",
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
        if sub == "gate":
            return _cmd_gate(
                args.contract_id, args.decision, getattr(args, "reason", None), effective_dir,
            )
        if sub == "lessons":
            return _cmd_lessons(
                getattr(args, "domain", None), getattr(args, "tags", None),
                getattr(args, "limit", 10), effective_dir,
            )
        if sub == "ledger":
            return _cmd_ledger(effective_dir)
        if sub == "standing":
            return _cmd_standing(effective_dir)
        if sub == "verify-cert":
            return _cmd_verify_cert(args.token, effective_dir)
        if sub == "session-start":
            return _cmd_session_start(getattr(args, "domain", None), effective_dir)
    except Exception as exc:  # CLI boundary: errors are exit 1, not tracebacks
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("error: no critic subcommand given (run|gate|lessons|ledger|standing|verify-cert|session-start)",
          file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="critic",
        description="Hybrid Critic Loop command line: run contracts, show "
                    "lessons, the ledger, lifetime standing, and verify "
                    "MM CERTIFIED certificates",
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
