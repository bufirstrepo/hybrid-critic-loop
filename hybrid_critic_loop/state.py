"""SQLite state store for the Hybrid Critic Loop plugin.

Stdlib ``sqlite3`` only, WAL mode. Exactly 12 tables mirroring the
spec objects: targets, contracts, candidates, measurements, scores,
verdicts, final_answers, experts, routes, ledger, lessons, events.

JSON-shaped columns are stored as TEXT and returned parsed.
Timestamps are UTC ISO strings.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["Store"]

_COST_TIERS = {"free", "subscription", "api"}
_LEDGER_OUTCOMES = {"completed", "refused", "timeout", "escalated", "observed"}
_CANDIDATE_UPDATE_FIELDS = {
    "payload_hash",
    "provenance",
    "evidence",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "status",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id TEXT PRIMARY KEY,
    ask TEXT NOT NULL,
    domain TEXT NOT NULL,
    constraints_json TEXT NOT NULL DEFAULT '[]',
    budget_json TEXT,
    done_when TEXT NOT NULL DEFAULT 'human_gate',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    task TEXT NOT NULL,
    domain TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    acceptance_json TEXT NOT NULL DEFAULT '[]',
    acceptance_baseline_run TEXT,
    test_author_family TEXT,
    repo_path TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    observations_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    stage TEXT NOT NULL,
    expert_id TEXT,
    family TEXT,
    slot TEXT NOT NULL,
    worktree TEXT,
    payload_hash TEXT,
    baseline_hash TEXT,
    provenance TEXT NOT NULL DEFAULT 'generated',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    sensor TEXT NOT NULL,
    metric TEXT NOT NULL,
    value_json TEXT,
    op TEXT,
    threshold_json TEXT,
    passed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    gate TEXT,
    rubric TEXT,
    judge_id TEXT,
    judge_family TEXT,
    blind INTEGER NOT NULL DEFAULT 0,
    position_shown INTEGER,
    items_json TEXT NOT NULL DEFAULT '{}',
    mean REAL,
    confidence REAL,
    details_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verdicts (
    id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    winner TEXT,
    eliminated_json TEXT NOT NULL DEFAULT '[]',
    hard TEXT,
    measured TEXT,
    judged_json TEXT,
    escalated INTEGER NOT NULL DEFAULT 0,
    escalation_reason TEXT,
    waiver_json TEXT,
    human_gate TEXT,
    tie_break TEXT,
    route_hold_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS final_answers (
    id INTEGER PRIMARY KEY,
    target_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    "commit" TEXT,
    intent_check_json TEXT,
    human_gate TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experts (
    id TEXT PRIMARY KEY,
    family TEXT,
    access TEXT,
    cost_tier TEXT NOT NULL,
    cost_tier_note TEXT,
    category_scores_json TEXT NOT NULL DEFAULT '{}',
    category_record_json TEXT NOT NULL DEFAULT '{}',
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    judge_calibration REAL,
    refusal_rate REAL NOT NULL DEFAULT 0.0,
    refusals INTEGER NOT NULL DEFAULT 0,
    dispatches INTEGER NOT NULL DEFAULT 0,
    last_scored TEXT,
    validity_points REAL,
    validity_cost_usd REAL,
    calibration_n INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routes (
    category TEXT PRIMARY KEY,
    "primary" TEXT,
    compete_with TEXT,
    fallback_json TEXT NOT NULL DEFAULT '[]',
    judges_json TEXT NOT NULL DEFAULT '[]',
    "rule" TEXT,
    panel_json TEXT,
    scoring TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY,
    target_id TEXT,
    contract_id TEXT,
    candidate_id TEXT,
    stage TEXT,
    expert_id TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    outcome TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY,
    contract_id TEXT,
    domain TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    outcome TEXT,
    cause TEXT,
    "rule" TEXT,
    route_change_json TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    written_to TEXT,
    provenance TEXT NOT NULL DEFAULT 'generated',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    contract_id TEXT,
    type TEXT,
    detail TEXT,
    created_at TEXT
);
"""

# Columns added after v1. Existing databases created with the old
# schema are migrated in ``Store.__init__`` (PRAGMA table_info check
# + ALTER TABLE ADD COLUMN for any column that is missing).
_MIGRATION_COLUMNS = {
    "scores": (
        ("confidence", "REAL"),
        ("details_json", "TEXT"),
    ),
    "experts": (
        ("validity_points", "REAL"),
        ("validity_cost_usd", "REAL"),
        ("calibration_n", "INTEGER"),
    ),
    "routes": (
        ("panel_json", "TEXT"),
        ("scoring", "TEXT"),
    ),
    # v0.3.1 (fix pass Wave 2.1): a route change held on a degraded verdict.
    "verdicts": (
        ("route_hold_json", "TEXT"),
    ),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _dumps(value):
    if value is None:
        return None
    return json.dumps(value)


def _loads(text, default=None):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _col(row, name, default=None):
    """Return ``row[name]`` if the column is present, else ``default``.

    Lets dict builders tolerate rows from a pre-migration schema
    (defensive; ``Store.__init__`` migrates before any query runs).
    """
    if row is None:
        return default
    try:
        keys = row.keys()
    except Exception:
        return default
    if name not in keys:
        return default
    return row[name]


def _cost_per_validity_point(validity_points, validity_cost_usd):
    if validity_cost_usd is not None and validity_points not in (None, 0):
        return validity_cost_usd / validity_points
    return None


class Store:
    """SQLite-backed store for Hybrid Critic Loop state."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            parent = Path(self.db_path).expanduser().parent
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self):
        """Add post-v1 columns to pre-existing tables, if missing.

        Checks ``PRAGMA table_info`` for each table in
        ``_MIGRATION_COLUMNS`` and issues ``ALTER TABLE ADD COLUMN``
        for any column that is absent, so v1 databases open cleanly.
        Idempotent: a no-op on databases already at the new schema.
        Table/column identifiers come only from the internal constant.
        """
        for table, columns in _MIGRATION_COLUMNS.items():
            existing = {
                row[1]
                for row in self._conn.execute(
                    f"PRAGMA table_info({table})").fetchall()
            }
            for col_name, col_type in columns:
                if col_name not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN"
                        f" {col_name} {col_type}")
        self._conn.commit()

    # -- internal helpers -------------------------------------------------

    def _next_id(self, prefix, table):
        """Derive the next sequential id like ``tgt_0007`` for a table.

        Scans existing ids in ``table`` for the ``<prefix>_`` pattern and
        takes max numeric suffix + 1. Table/prefix are internal constants.
        """
        max_n = 0
        rows = self._conn.execute(f"SELECT id FROM {table}").fetchall()
        head = prefix + "_"
        for row in rows:
            rid = row[0]
            if not rid or not str(rid).startswith(head):
                continue
            suffix = str(rid)[len(head):]
            digits = ""
            for ch in suffix:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                max_n = max(max_n, int(digits))
        return f"{prefix}_{max_n + 1:04d}"

    def _one(self, sql, params=()):
        return self._conn.execute(sql, params).fetchone()

    def _all(self, sql, params=()):
        return self._conn.execute(sql, params).fetchall()

    # -- targets -----------------------------------------------------------

    def create_target(self, ask, domain, constraints=None, budget=None,
                      done_when="human_gate"):
        target_id = self._next_id("tgt", "targets")
        created = _now()
        self._conn.execute(
            "INSERT INTO targets (id, ask, domain, constraints_json,"
            " budget_json, done_when, created_at) VALUES (?,?,?,?,?,?,?)",
            (target_id, ask, domain, _dumps(list(constraints or [])),
             _dumps(budget), done_when, created),
        )
        self._conn.commit()
        return target_id

    def get_target(self, target_id):
        row = self._one("SELECT * FROM targets WHERE id=?", (target_id,))
        if row is None:
            return None
        return self._target_dict(row)

    @staticmethod
    def _target_dict(row):
        return {
            "id": row["id"],
            "ask": row["ask"],
            "domain": row["domain"],
            "constraints": _loads(row["constraints_json"], []),
            "budget": _loads(row["budget_json"], None),
            "done_when": row["done_when"],
            "created_at": row["created_at"],
        }

    # -- contracts ---------------------------------------------------------

    def create_contract(self, target_id, stage, task, domain, tags,
                        acceptance, depends_on=None, test_author_family=None,
                        repo_path=None, attempt=1, contract_id=None):
        cid = contract_id if contract_id is not None else self._next_id(
            "ctr", "contracts")
        created = _now()
        self._conn.execute(
            "INSERT INTO contracts (id, target_id, stage, attempt, task,"
            " domain, tags_json, depends_on_json, acceptance_json,"
            " acceptance_baseline_run, test_author_family, repo_path,"
            " status, observations_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, target_id, stage, attempt, task, domain,
             _dumps(list(tags or [])), _dumps(list(depends_on or [])),
             _dumps(list(acceptance or [])), None, test_author_family,
             repo_path, "open", _dumps([]), created),
        )
        self._conn.commit()
        return cid

    def get_contract(self, contract_id):
        row = self._one("SELECT * FROM contracts WHERE id=?", (contract_id,))
        if row is None:
            return None
        return self._contract_dict(row)

    @staticmethod
    def _contract_dict(row):
        return {
            "id": row["id"],
            "target_id": row["target_id"],
            "stage": row["stage"],
            "attempt": row["attempt"],
            "task": row["task"],
            "domain": row["domain"],
            "tags": _loads(row["tags_json"], []),
            "depends_on": _loads(row["depends_on_json"], []),
            "acceptance": _loads(row["acceptance_json"], []),
            "acceptance_baseline_run": row["acceptance_baseline_run"],
            "test_author_family": row["test_author_family"],
            "repo_path": row["repo_path"],
            "status": row["status"],
            "observations": _loads(row["observations_json"], []),
            "created_at": row["created_at"],
        }

    def set_baseline_run(self, contract_id, result):
        self._conn.execute(
            "UPDATE contracts SET acceptance_baseline_run=? WHERE id=?",
            (result, contract_id),
        )
        self._conn.commit()

    def set_contract_status(self, contract_id, status):
        self._conn.execute(
            "UPDATE contracts SET status=? WHERE id=?", (status, contract_id))
        self._conn.commit()

    def append_observation(self, contract_id, obs):
        row = self._one(
            "SELECT observations_json FROM contracts WHERE id=?",
            (contract_id,))
        if row is None:
            raise KeyError(f"unknown contract: {contract_id}")
        observations = _loads(row["observations_json"], [])
        observations.append(obs)
        observations = observations[-50:]
        self._conn.execute(
            "UPDATE contracts SET observations_json=? WHERE id=?",
            (_dumps(observations), contract_id),
        )
        self._conn.commit()

    def latest_open_contract(self):
        row = self._one(
            "SELECT * FROM contracts WHERE status='open'"
            " ORDER BY rowid DESC LIMIT 1")
        if row is None:
            return None
        return self._contract_dict(row)

    # -- candidates ----------------------------------------------------------

    def create_candidate(self, contract_id, attempt, stage, expert_id,
                         family, slot, worktree, baseline_hash):
        if attempt == 1:
            candidate_id = f"{contract_id}_{slot}"
        else:
            candidate_id = f"{contract_id}_{slot}_r{attempt}"
        created = _now()
        self._conn.execute(
            "INSERT INTO candidates (id, contract_id, attempt, stage,"
            " expert_id, family, slot, worktree, payload_hash, baseline_hash,"
            " provenance, evidence_json, tokens_in, tokens_out, cost_usd,"
            " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (candidate_id, contract_id, attempt, stage, expert_id, family,
             str(slot), worktree, None, baseline_hash, "generated",
             _dumps({}), None, None, None, "created", created),
        )
        self._conn.commit()
        return candidate_id

    def update_candidate(self, candidate_id, **fields):
        unknown = set(fields) - _CANDIDATE_UPDATE_FIELDS
        if unknown:
            raise ValueError(
                f"unsupported candidate fields: {sorted(unknown)}")
        if not fields:
            return
        sets = []
        params = []
        for key, value in fields.items():
            if key == "evidence":
                sets.append("evidence_json=?")
                params.append(_dumps(value if value is not None else {}))
            else:
                sets.append(f"{key}=?")
                params.append(value)
        params.append(candidate_id)
        self._conn.execute(
            f"UPDATE candidates SET {', '.join(sets)} WHERE id=?", params)
        self._conn.commit()

    def get_candidate(self, candidate_id):
        row = self._one(
            "SELECT * FROM candidates WHERE id=?", (candidate_id,))
        if row is None:
            return None
        return self._candidate_dict(row)

    @staticmethod
    def _candidate_dict(row):
        return {
            "id": row["id"],
            "contract_id": row["contract_id"],
            "attempt": row["attempt"],
            "stage": row["stage"],
            "expert_id": row["expert_id"],
            "family": row["family"],
            "slot": row["slot"],
            "worktree": row["worktree"],
            "payload_hash": row["payload_hash"],
            "baseline_hash": row["baseline_hash"],
            "provenance": row["provenance"],
            "evidence": _loads(row["evidence_json"], {}),
            "tokens_in": row["tokens_in"],
            "tokens_out": row["tokens_out"],
            "cost_usd": row["cost_usd"],
            "status": row["status"],
            "created_at": row["created_at"],
        }

    def candidates_for(self, contract_id, attempt=None):
        if attempt is None:
            rows = self._all(
                "SELECT * FROM candidates WHERE contract_id=?"
                " ORDER BY slot ASC, attempt ASC", (contract_id,))
        else:
            rows = self._all(
                "SELECT * FROM candidates WHERE contract_id=? AND attempt=?"
                " ORDER BY slot ASC", (contract_id, attempt))
        return [self._candidate_dict(r) for r in rows]

    # -- measurements --------------------------------------------------------

    def add_measurement(self, candidate_id, sensor, metric, value, op,
                        threshold, passed):
        cur = self._conn.execute(
            "INSERT INTO measurements (candidate_id, sensor, metric,"
            " value_json, op, threshold_json, passed, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (candidate_id, sensor, metric, _dumps(value), op,
             _dumps(threshold), 1 if passed else 0, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def measurements_for(self, candidate_id):
        rows = self._all(
            "SELECT * FROM measurements WHERE candidate_id=? ORDER BY id ASC",
            (candidate_id,))
        return [self._measurement_dict(r) for r in rows]

    @staticmethod
    def _measurement_dict(row):
        return {
            "id": row["id"],
            "candidate_id": row["candidate_id"],
            "sensor": row["sensor"],
            "metric": row["metric"],
            "value": _loads(row["value_json"], None),
            "op": row["op"],
            "threshold": _loads(row["threshold_json"], None),
            "pass": bool(row["passed"]),
            "created_at": row["created_at"],
        }

    # -- scores --------------------------------------------------------------

    def add_score(self, candidate_id, gate, rubric, judge_id, judge_family,
                  blind, position_shown, items, mean, confidence=None,
                  details=None):
        cur = self._conn.execute(
            "INSERT INTO scores (candidate_id, gate, rubric, judge_id,"
            " judge_family, blind, position_shown, items_json, mean,"
            " confidence, details_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (candidate_id, gate, rubric, judge_id, judge_family,
             1 if blind else 0, position_shown,
             _dumps(items if items is not None else {}), mean, confidence,
             _dumps(details if details is not None else {}), _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def scores_for(self, candidate_id):
        rows = self._all(
            "SELECT * FROM scores WHERE candidate_id=? ORDER BY id ASC",
            (candidate_id,))
        return [self._score_dict(r) for r in rows]

    @staticmethod
    def _score_dict(row):
        return {
            "id": row["id"],
            "candidate_id": row["candidate_id"],
            "gate": row["gate"],
            "rubric": row["rubric"],
            "judge_id": row["judge_id"],
            "judge_family": row["judge_family"],
            "blind": bool(row["blind"]),
            "position_shown": row["position_shown"],
            "items": _loads(row["items_json"], {}),
            "mean": row["mean"],
            "confidence": _col(row, "confidence"),
            "details": _loads(_col(row, "details_json"), {}),
            "created_at": row["created_at"],
        }

    # -- verdicts --------------------------------------------------------------

    def create_verdict(self, contract_id, attempt, winner, eliminated, hard,
                       measured, judged, escalated, escalation_reason, waiver,
                       human_gate, tie_break):
        verdict_id = self._next_id("vrd", "verdicts")
        self._conn.execute(
            "INSERT INTO verdicts (id, contract_id, attempt, winner,"
            " eliminated_json, hard, measured, judged_json, escalated,"
            " escalation_reason, waiver_json, human_gate, tie_break,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (verdict_id, contract_id, attempt, winner,
             _dumps(list(eliminated or [])), hard, measured, _dumps(judged),
             1 if escalated else 0, escalation_reason, _dumps(waiver),
             human_gate, tie_break, _now()),
        )
        self._conn.commit()
        return verdict_id

    def get_verdict(self, contract_id, attempt):
        row = self._one(
            "SELECT * FROM verdicts WHERE contract_id=? AND attempt=?"
            " ORDER BY rowid DESC LIMIT 1", (contract_id, attempt))
        if row is None:
            return None
        return self._verdict_dict(row)

    def latest_verdict(self, contract_id):
        row = self._one(
            "SELECT * FROM verdicts WHERE contract_id=?"
            " ORDER BY rowid DESC LIMIT 1", (contract_id,))
        if row is None:
            return None
        return self._verdict_dict(row)

    def set_verdict_route_hold(self, verdict_id, hold):
        """Record a route change held on a degraded verdict (owner floor
        rule, README "Panel floor"). ``hold`` is a dict stored as JSON, or
        None to clear it."""
        self._conn.execute(
            "UPDATE verdicts SET route_hold_json=? WHERE id=?",
            (_dumps(hold), verdict_id),
        )
        self._conn.commit()

    @staticmethod
    def _verdict_dict(row):
        return {
            "id": row["id"],
            "contract_id": row["contract_id"],
            "attempt": row["attempt"],
            "winner": row["winner"],
            "eliminated": _loads(row["eliminated_json"], []),
            "hard": row["hard"],
            "measured": row["measured"],
            "judged": _loads(row["judged_json"], None),
            "escalated": bool(row["escalated"]),
            "escalation_reason": row["escalation_reason"],
            "waiver": _loads(row["waiver_json"], None),
            "human_gate": row["human_gate"],
            "tie_break": row["tie_break"],
            "route_hold": _loads(_col(row, "route_hold_json"), None),
            "created_at": row["created_at"],
        }

    # -- final answers ---------------------------------------------------------

    def create_final_answer(self, target_id, contract_id, commit,
                            intent_check=None, human_gate=None):
        cur = self._conn.execute(
            'INSERT INTO final_answers (target_id, contract_id, "commit",'
            " intent_check_json, human_gate, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (target_id, contract_id, commit, _dumps(intent_check),
             human_gate, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    # -- experts ---------------------------------------------------------------

    def upsert_expert(self, expert_id, family, access, cost_tier,
                      cost_tier_note=None):
        if cost_tier not in _COST_TIERS:
            raise ValueError(
                f"invalid cost_tier {cost_tier!r}: must be one of"
                f" {sorted(_COST_TIERS)} (put qualifiers like"
                " '(unverified)' in cost_tier_note)")
        now = _now()
        existing = self._one("SELECT id FROM experts WHERE id=?", (expert_id,))
        if existing is None:
            self._conn.execute(
                "INSERT INTO experts (id, family, access, cost_tier,"
                " cost_tier_note, category_scores_json, category_record_json,"
                " wins, losses, judge_calibration, refusal_rate, refusals,"
                " dispatches, last_scored, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (expert_id, family, access, cost_tier, cost_tier_note,
                 _dumps({}), _dumps({}), 0, 0, None, 0.0, 0, 0, None,
                 now, now),
            )
        else:
            self._conn.execute(
                "UPDATE experts SET family=?, access=?, cost_tier=?,"
                " cost_tier_note=?, updated_at=? WHERE id=?",
                (family, access, cost_tier, cost_tier_note, now, expert_id),
            )
        self._conn.commit()

    def get_expert(self, expert_id):
        row = self._one("SELECT * FROM experts WHERE id=?", (expert_id,))
        if row is None:
            return None
        return self._expert_dict(row)

    def list_experts(self):
        rows = self._all("SELECT * FROM experts ORDER BY id ASC")
        return [self._expert_dict(r) for r in rows]

    @staticmethod
    def _expert_dict(row):
        validity_points = _col(row, "validity_points")
        validity_cost_usd = _col(row, "validity_cost_usd")
        return {
            "id": row["id"],
            "family": row["family"],
            "access": row["access"],
            "cost_tier": row["cost_tier"],
            "cost_tier_note": row["cost_tier_note"],
            "category_scores": _loads(row["category_scores_json"], {}),
            "wins": row["wins"],
            "losses": row["losses"],
            "judge_calibration": row["judge_calibration"],
            "calibration_n": _col(row, "calibration_n"),
            "refusal_rate": row["refusal_rate"],
            "last_scored": row["last_scored"],
            "validity_points": validity_points,
            "validity_cost_usd": validity_cost_usd,
            "cost_per_validity_point": _cost_per_validity_point(
                validity_points, validity_cost_usd),
        }

    def _expert_row_or_raise(self, expert_id):
        row = self._one("SELECT * FROM experts WHERE id=?", (expert_id,))
        if row is None:
            raise KeyError(f"unknown expert: {expert_id}")
        return row

    def record_expert_result(self, expert_id, category, won):
        row = self._expert_row_or_raise(expert_id)
        wins = row["wins"] + (1 if won else 0)
        losses = row["losses"] + (0 if won else 1)
        dispatches = row["dispatches"] + 1
        record = _loads(row["category_record_json"], {})
        cat = record.get(category, {"wins": 0, "losses": 0})
        if won:
            cat["wins"] += 1
        else:
            cat["losses"] += 1
        record[category] = cat
        scores = _loads(row["category_scores_json"], {})
        total = cat["wins"] + cat["losses"]
        scores[category] = (cat["wins"] / total) if total else None
        self._conn.execute(
            "UPDATE experts SET wins=?, losses=?, dispatches=?,"
            " category_record_json=?, category_scores_json=?,"
            " last_scored=?, updated_at=? WHERE id=?",
            (wins, losses, dispatches, _dumps(record), _dumps(scores),
             _today(), _now(), expert_id),
        )
        self._conn.commit()

    def record_refusal(self, expert_id):
        row = self._expert_row_or_raise(expert_id)
        refusals = row["refusals"] + 1
        dispatches = row["dispatches"] + 1
        refusal_rate = (refusals / dispatches) if dispatches else 0.0
        self._conn.execute(
            "UPDATE experts SET refusals=?, dispatches=?, refusal_rate=?,"
            " updated_at=? WHERE id=?",
            (refusals, dispatches, refusal_rate, _now(), expert_id),
        )
        self._conn.commit()

    def record_validity(self, expert_id, validity_points, cost_usd=None):
        row = self._expert_row_or_raise(expert_id)
        current_points = _col(row, "validity_points")
        new_points = (current_points if current_points is not None
                      else 0) + validity_points
        current_cost = _col(row, "validity_cost_usd")
        if cost_usd is not None:
            new_cost = (current_cost if current_cost is not None
                        else 0) + cost_usd
        else:
            # No reported cost: leave the column untouched (NULL if
            # no cost was ever reported -- never invent one).
            new_cost = current_cost
        self._conn.execute(
            "UPDATE experts SET validity_points=?, validity_cost_usd=?,"
            " updated_at=? WHERE id=?",
            (new_points, new_cost, _now(), expert_id),
        )
        self._conn.commit()

    def update_expert_calibration(self, expert_id, value, n):
        self._expert_row_or_raise(expert_id)
        self._conn.execute(
            "UPDATE experts SET judge_calibration=?, calibration_n=?,"
            " updated_at=? WHERE id=?",
            (value, n, _now(), expert_id),
        )
        self._conn.commit()

    # -- routes ------------------------------------------------------------------

    def upsert_route(self, category, primary, compete_with, fallback, judges,
                     rule, panel=None, scoring=None):
        # panel/scoring (v0.3): the 8-family target panel and the scoring
        # mode travel with the route. On conflict they are only
        # overwritten when the caller supplies them, so legacy callers
        # (positional v1 shape) never clobber a stored panel.
        self._conn.execute(
            'INSERT INTO routes (category, "primary", compete_with,'
            ' fallback_json, judges_json, "rule", panel_json, scoring,'
            " updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            ' ON CONFLICT(category) DO UPDATE SET "primary"=excluded."primary",'
            " compete_with=excluded.compete_with,"
            " fallback_json=excluded.fallback_json,"
            " judges_json=excluded.judges_json,"
            ' "rule"=excluded."rule",'
            " panel_json=COALESCE(excluded.panel_json, routes.panel_json),"
            " scoring=COALESCE(excluded.scoring, routes.scoring),"
            " updated_at=excluded.updated_at",
            (category, primary, compete_with,
             _dumps(list(fallback or [])), _dumps(list(judges or [])),
             rule, _dumps(list(panel)) if panel is not None else None,
             scoring, _now()),
        )
        self._conn.commit()

    def get_route(self, category):
        row = self._one("SELECT * FROM routes WHERE category=?", (category,))
        if row is None:
            return None
        return self._route_dict(row)

    @staticmethod
    def _route_dict(row):
        return {
            "category": row["category"],
            "primary": row["primary"],
            "compete_with": row["compete_with"],
            "fallback": _loads(row["fallback_json"], []),
            "judges": _loads(row["judges_json"], []),
            "rule": row["rule"],
            "panel": _loads(row["panel_json"], []) if row["panel_json"] else [],
            "scoring": row["scoring"],
            "updated_at": row["updated_at"],
        }

    def apply_route_demotion(self, category, demote_expert, promote_expert,
                             reason):
        route = self.get_route(category)
        if route is None:
            raise KeyError(f"unknown route category: {category}")
        primary = route["primary"]
        compete_with = route["compete_with"]
        fallback = list(route["fallback"])
        if demote_expert == primary and promote_expert:
            primary = promote_expert
            compete_with = demote_expert
        elif demote_expert == compete_with:
            if demote_expert in fallback:
                fallback.remove(demote_expert)
            fallback.insert(0, demote_expert)
            compete_with = promote_expert if promote_expert else None
        self._conn.execute(
            'UPDATE routes SET "primary"=?, compete_with=?, fallback_json=?,'
            " updated_at=? WHERE category=?",
            (primary, compete_with, _dumps(fallback), _now(), category),
        )
        self._conn.commit()
        return self.get_route(category)

    # -- ledger --------------------------------------------------------------------

    def add_ledger(self, target_id, contract_id, candidate_id, stage,
                   expert_id, tokens_in, tokens_out, cost_usd, outcome,
                   note=None):
        if outcome not in _LEDGER_OUTCOMES:
            raise ValueError(
                f"invalid ledger outcome {outcome!r}: must be one of"
                f" {sorted(_LEDGER_OUTCOMES)}")
        cur = self._conn.execute(
            "INSERT INTO ledger (target_id, contract_id, candidate_id,"
            " stage, expert_id, tokens_in, tokens_out, cost_usd, outcome,"
            " note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (target_id, contract_id, candidate_id, stage, expert_id,
             tokens_in, tokens_out, cost_usd, outcome, note, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def ledger_rows(self, expert_id=None):
        if expert_id is None:
            rows = self._all("SELECT * FROM ledger ORDER BY id ASC")
        else:
            rows = self._all(
                "SELECT * FROM ledger WHERE expert_id=? ORDER BY id ASC",
                (expert_id,))
        return [self._ledger_dict(r) for r in rows]

    @staticmethod
    def _ledger_dict(row):
        return {
            "id": row["id"],
            "target_id": row["target_id"],
            "contract_id": row["contract_id"],
            "candidate_id": row["candidate_id"],
            "stage": row["stage"],
            "expert_id": row["expert_id"],
            "tokens_in": row["tokens_in"],
            "tokens_out": row["tokens_out"],
            "cost_usd": row["cost_usd"],
            "outcome": row["outcome"],
            "note": row["note"],
            "created_at": row["created_at"],
        }

    def ledger_summary(self):
        rows = self.ledger_rows()
        order = []
        agg = {}
        for row in rows:
            eid = row["expert_id"]
            if eid not in agg:
                order.append(eid)
                agg[eid] = {
                    "expert_id": eid,
                    "dispatches": 0,
                    "completed": 0,
                    "refused": 0,
                    "timeout": 0,
                    "_tin": [],
                    "_tout": [],
                    "_cost": [],
                }
            entry = agg[eid]
            entry["dispatches"] += 1
            if row["outcome"] in ("completed", "refused", "timeout"):
                entry[row["outcome"]] += 1
            entry["_tin"].append(row["tokens_in"])
            entry["_tout"].append(row["tokens_out"])
            entry["_cost"].append(row["cost_usd"])
        summary = []
        for eid in order:
            entry = agg[eid]

            def _sum_or_none(values):
                present = [v for v in values if v is not None]
                if not present:
                    return None
                return sum(present)

            expert = self.get_expert(eid)
            if expert is not None:
                wins = expert["wins"]
                losses = expert["losses"]
                validity_points = expert.get("validity_points")
                validity_cost_usd = expert.get("validity_cost_usd")
                cost_per_validity_point = expert.get(
                    "cost_per_validity_point")
            else:
                wins, losses = 0, 0
                validity_points = None
                validity_cost_usd = None
                cost_per_validity_point = None
            total = wins + losses
            summary.append({
                "expert_id": eid,
                "dispatches": entry["dispatches"],
                "completed": entry["completed"],
                "refused": entry["refused"],
                "timeout": entry["timeout"],
                "tokens_in": _sum_or_none(entry["_tin"]),
                "tokens_out": _sum_or_none(entry["_tout"]),
                "cost_usd": _sum_or_none(entry["_cost"]),
                "wins": wins,
                "losses": losses,
                "win_rate": (wins / total) if total else None,
                "validity_points": validity_points,
                "validity_cost_usd": validity_cost_usd,
                "cost_per_validity_point": cost_per_validity_point,
            })
        return summary

    # -- lessons ---------------------------------------------------------------------

    def create_lesson(self, contract_id, domain, tags, outcome, cause, rule,
                      route_change, evidence, written_to,
                      provenance="generated"):
        lesson_id = self._next_id("lsn", "lessons")
        self._conn.execute(
            'INSERT INTO lessons (id, contract_id, domain, tags_json,'
            ' outcome, cause, "rule", route_change_json, evidence_json,'
            " written_to, provenance, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (lesson_id, contract_id, domain, _dumps(list(tags or [])),
             outcome, cause, rule, _dumps(route_change),
             _dumps(list(evidence or [])), written_to, provenance, _now()),
        )
        self._conn.commit()
        return lesson_id

    def get_lesson(self, lesson_id):
        row = self._one("SELECT * FROM lessons WHERE id=?", (lesson_id,))
        if row is None:
            return None
        return self._lesson_dict(row)

    @staticmethod
    def _lesson_dict(row):
        return {
            "id": row["id"],
            "contract_id": row["contract_id"],
            "domain": row["domain"],
            "tags": _loads(row["tags_json"], []),
            "outcome": row["outcome"],
            "cause": row["cause"],
            "rule": row["rule"],
            "route_change": _loads(row["route_change_json"], None),
            "evidence": _loads(row["evidence_json"], []),
            "written_to": row["written_to"],
            "provenance": row["provenance"],
            "created_at": row["created_at"],
        }

    def lessons_for(self, domain=None, tags=None, limit=5):
        if domain is None:
            rows = self._all("SELECT * FROM lessons")
        else:
            rows = self._all(
                "SELECT * FROM lessons WHERE domain=?", (domain,))
        lessons = [self._lesson_dict(r) for r in rows]
        wanted = set(tags or [])

        def sort_key(lesson):
            overlap = len(wanted.intersection(lesson["tags"]))
            # sort desc by overlap, then newest first (created_at, id)
            return (overlap, lesson["created_at"], lesson["id"])

        lessons.sort(key=sort_key, reverse=True)
        if limit is not None:
            lessons = lessons[:limit]
        return lessons

    def count_lessons(self):
        row = self._one("SELECT COUNT(*) AS n FROM lessons")
        return int(row["n"])

    # -- events ------------------------------------------------------------------------

    def add_event(self, contract_id, type, detail):
        stored = detail if (detail is None or isinstance(detail, str)) \
            else _dumps(detail)
        cur = self._conn.execute(
            "INSERT INTO events (contract_id, type, detail, created_at)"
            " VALUES (?,?,?,?)",
            (contract_id, type, stored, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def events_for(self, contract_id=None, limit=100):
        if contract_id is None:
            rows = self._all(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (limit,))
        else:
            rows = self._all(
                "SELECT * FROM events WHERE contract_id=?"
                " ORDER BY id DESC LIMIT ?",
                (contract_id, limit))
        return [self._event_dict(r) for r in rows]

    @staticmethod
    def _event_dict(row):
        return {
            "id": row["id"],
            "contract_id": row["contract_id"],
            "type": row["type"],
            "detail": row["detail"],
            "created_at": row["created_at"],
        }

    # -- lifecycle ---------------------------------------------------------------------

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
