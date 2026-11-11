"""Read-only FastAPI backend for The Millionaire Matrix Desktop & Dashboard UI.

Mounted by Hermes under ``/api/plugins/the-millionaire-matrix/``.
Strictly read-only: inspects ``run_status.json`` and existing SQLite rows
without ever creating ``data.db`` on a fresh install or mutating contest state.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

try:
    from fastapi import APIRouter
except ImportError:  # pragma: no cover - stdlib test fallback when fastapi is absent

    class APIRouter:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self.routes: list[tuple[str, str, object]] = []

        def get(self, path: str):
            def _wrap(fn):
                self.routes.append(("GET", path, fn))
                return fn

            return _wrap


from hybrid_critic_loop.lessons import sanitize_lesson_row
from hybrid_critic_loop.matrix import VALIDITY_ROWS
from hybrid_critic_loop.plugin import _build_executors, resolve_data_dir
from hybrid_critic_loop.progress import read_run_status
from hybrid_critic_loop.reports import build_matrix_view
from hybrid_critic_loop.standing import standing_line
from hybrid_critic_loop.usage_feed import collect_usage_feed, pool_kind_for_seat

router = APIRouter()

_SQLITE_MAGIC = b"SQLite format 3\x00"
_PANEL_CACHE: dict[str, object] = {"ts": 0.0, "seats": []}


def _has_valid_db(data_dir: Path) -> Path | None:
    db_file = Path(data_dir) / "data.db"
    if not db_file.is_file():
        return None
    try:
        with open(db_file, "rb") as fh:
            if fh.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
                return None
    except OSError:
        return None
    return db_file


def _panel_snapshot(force: bool = False) -> list[dict]:
    now = time.monotonic()
    cached_ts = float(_PANEL_CACHE.get("ts") or 0.0)
    cached_seats = _PANEL_CACHE.get("seats")
    if not force and isinstance(cached_seats, list) and cached_seats and (now - cached_ts) < 30.0:
        return cached_seats
    seats: list[dict] = []
    try:
        executors = _build_executors("live")
        for eid, ex in executors.items():
            try:
                ok, detail = ex.available()
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, str(exc)
            try:
                _, pool_id = pool_kind_for_seat(eid, ex)
            except Exception:  # noqa: BLE001
                pool_id = None
            seats.append({
                "expert_id": eid,
                "family": getattr(ex, "family", None),
                "model": getattr(ex, "last_model", None) or getattr(ex, "default_model", None) or eid,
                "pool_id": pool_id,
                "ready": bool(ok),
                "detail": str(detail or ""),
            })
    except Exception:  # noqa: BLE001
        seats = []
    _PANEL_CACHE["ts"] = now
    _PANEL_CACHE["seats"] = seats
    return seats


def _recent_contracts_ro(db_file: Path, limit: int = 8) -> list[dict]:
    uri = f"file:{db_file}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, stage, attempt, task, domain, status, created_at "
            "FROM contracts ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "stage": r["stage"],
                "attempt": r["attempt"],
                "task": str(r["task"] or "")[:160],
                "domain": r["domain"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def _open_store_if_idle(data_dir: Path, active: bool):
    """Open Store on an existing DB when not write-locked; never create data.db."""
    db_file = _has_valid_db(data_dir)
    if db_file is None or active:
        return None
    from hybrid_critic_loop.state import Store

    try:
        return Store(db_file)
    except Exception:  # noqa: BLE001
        return None


@router.get("/status")
async def get_status(refresh_panel: bool = False) -> dict:
    """Return live run state, 8-family panel readiness, standing, and recent lessons."""
    data_dir = resolve_data_dir(None)
    active_run = read_run_status(data_dir)
    is_active = bool(active_run.get("active"))
    panel = _panel_snapshot(force=bool(refresh_panel))

    recent_contracts: list[dict] = []
    db_file = _has_valid_db(data_dir)
    if db_file is not None:
        try:
            recent_contracts = _recent_contracts_ro(db_file, limit=8)
        except Exception:  # noqa: BLE001
            recent_contracts = []

    ledger_summary: list[dict] = []
    lessons: list[dict] = []
    standing_text: str | None = None
    latest_matrix: dict | None = None

    store = _open_store_if_idle(data_dir, active=is_active)
    if store is not None:
        try:
            ledger_summary = store.ledger_summary()
            raw_lessons = store.lessons_for(limit=5)
            lessons = [sanitize_lesson_row(r) for r in raw_lessons]
            try:
                standing_text = standing_line(store, cached_chain=True)
            except TypeError:
                standing_text = standing_line(store)
            except Exception:  # noqa: BLE001
                standing_text = None
            target_cid = active_run.get("contract_id") or (
                recent_contracts[0]["id"] if recent_contracts else None
            )
            if target_cid:
                latest_matrix = build_matrix_view(store, str(target_cid))
        except Exception:  # noqa: BLE001
            pass
        finally:
            store.close()

    ready_count = sum(1 for s in panel if s.get("ready"))
    try:
        exec_map = _build_executors("live")
    except Exception:  # noqa: BLE001
        exec_map = {}
    usage_data = collect_usage_feed(
        exec_map,
        data_dir=data_dir,
        refresh=bool(refresh_panel),
        include_cli_pools=True,
    )
    seat_usage = usage_data.get("seats") or {}
    enriched_panel = []
    for s in panel:
        scopy = dict(s)
        sid = scopy.get("expert_id")
        if sid and sid in seat_usage:
            scopy["usage"] = seat_usage[sid]
            scopy.setdefault("pool_id", seat_usage[sid].get("pool_id"))
        enriched_panel.append(scopy)

    return {
        "ok": True,
        "active_run": active_run,
        "panel": {
            "seats": enriched_panel,
            "ready_count": ready_count,
            "total_count": len(enriched_panel),
        },
        "usage": usage_data,
        "recent_contracts": recent_contracts,
        "latest_matrix": latest_matrix,
        "ledger_summary": ledger_summary,
        "standing_line": standing_text,
        "lessons": lessons,
    }


@router.get("/usage")
async def get_usage(refresh: bool = True, include_cli_pools: bool = True) -> dict:
    """Return normalized per-pool and per-seat plan-usage-left plus live seat activity."""
    data_dir = resolve_data_dir(None)
    active_run = read_run_status(data_dir)
    try:
        exec_map = _build_executors("live")
    except Exception:  # noqa: BLE001
        exec_map = {}
    feed = collect_usage_feed(
        exec_map,
        data_dir=data_dir,
        refresh=bool(refresh),
        include_cli_pools=bool(include_cli_pools),
    )
    return {
        "ok": True,
        "generated_at": feed.get("generated_at"),
        "pools": feed.get("pools") or {},
        "seats": feed.get("seats") or {},
        "summary": feed.get("summary") or {},
        "active_run_phase": active_run.get("phase"),
        "active_seat_ids": list(active_run.get("active_seat_ids") or []),
        "agent_cursor": active_run.get("agent_cursor"),
    }


@router.get("/matrix")
async def get_matrix(contract_id: str = "") -> dict:
    """Return the peer validity matrix view for ``contract_id`` (or latest contract)."""
    data_dir = resolve_data_dir(None)
    db_file = _has_valid_db(data_dir)
    cid = (contract_id or "").strip()
    if not cid and db_file is not None:
        try:
            recent = _recent_contracts_ro(db_file, limit=1)
            if recent:
                cid = str(recent[0]["id"])
        except Exception:  # noqa: BLE001
            cid = ""
    if not cid:
        return {
            "contract_id": None,
            "rubric": "validity_matrix_v1",
            "rows": list(VALIDITY_ROWS),
            "candidates": {},
            "panel_status": "not_run",
            "note": "not run",
        }
    store = _open_store_if_idle(data_dir, active=False)
    if store is None:
        return {
            "contract_id": cid,
            "rubric": "validity_matrix_v1",
            "rows": list(VALIDITY_ROWS),
            "candidates": {},
            "panel_status": "not_run",
            "note": "not run",
        }
    try:
        return build_matrix_view(store, cid)
    finally:
        store.close()


@router.get("/standing")
async def get_standing() -> dict:
    """Return the client standing summary without creating a database."""
    data_dir = resolve_data_dir(None)
    store = _open_store_if_idle(data_dir, active=False)
    if store is None:
        return {"ok": True, "has_store": False, "standing_line": None, "report": "standing: not run"}
    try:
        from hybrid_critic_loop.standing import standing_report

        try:
            line = standing_line(store, cached_chain=True)
        except TypeError:
            line = standing_line(store)
        return {
            "ok": True,
            "has_store": True,
            "standing_line": line,
            "report": standing_report(store),
        }
    finally:
        store.close()
