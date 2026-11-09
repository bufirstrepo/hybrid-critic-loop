"""SQLite state store for the Hybrid Critic Loop plugin.

Stdlib ``sqlite3`` only, WAL mode. Exactly 12 tables mirroring the
spec objects: targets, contracts, candidates, measurements, scores,
verdicts, final_answers, experts, routes, ledger, lessons, events.

JSON-shaped columns are stored as TEXT and returned parsed.
Timestamps are UTC ISO strings.
"""

import json
import functools
import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import chain as _chain

__all__ = ["Store"]

_COST_TIERS = {"free", "subscription", "api"}
# What a dispatched seat did — one ledger row per dispatch (fix pass Wave
# 2.8, review M3: a refusal, a missing binary and an adapter error are
# different facts) ...
_GENERATION_OUTCOMES = ("completed", "refused", "timeout", "unavailable", "error")
# ... plus the queue's own notes: an escalation, or an observation (bracket
# rounds, seated peers, held route changes). Only generation rows count as
# dispatches in ``ledger_summary``.
_LEDGER_OUTCOMES = set(_GENERATION_OUTCOMES) | {"escalated", "observed"}
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
    created_at TEXT NOT NULL,
    prev_hash TEXT,
    row_hash TEXT,
    provenance TEXT,
    sealed_via TEXT
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
-- Hot-path indexes (fix spec G3; round-4 performance P5: the package
-- had no indexes at all, so every filtered query was a full scan).
-- Created here so fresh and pre-existing databases alike gain them
-- through the same executescript on open.
CREATE INDEX IF NOT EXISTS idx_ledger_contract_id
    ON ledger (contract_id);
CREATE INDEX IF NOT EXISTS idx_ledger_expert_id
    ON ledger (expert_id);
CREATE INDEX IF NOT EXISTS idx_verdicts_contract_id
    ON verdicts (contract_id);
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
    # Standing spec (Unit B): the ledger hash chain — every row seals the
    # previous row's hash (see chain.py). Backfilled once on first open;
    # PRAGMA user_version bit 1 is the store meta flag recording that the
    # backfill has run.
    # Tightening spec T2: ledger provenance (who reported the row's
    # numbers) and sealed_via (how the row got its chain hashes:
    # "append" / "backfill" / "repair" / "upgrade" — the last added by
    # fix spec G2 for the canonical-form cascade). Pre-migration rows
    # backfill provenance to "legacy-import" (see _migrate).
    "ledger": (
        ("prev_hash", "TEXT"),
        ("row_hash", "TEXT"),
        ("provenance", "TEXT"),
        ("sealed_via", "TEXT"),
    ),
}

#: PRAGMA user_version value marking the ledger-chain backfill as done.
_CHAIN_BACKFILL_VERSION = 1


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


def domain_match(requested, actual):
    """Rank a lesson's domain against the requested one.

    2 = exact; 1 = same family (``actual`` is a sub-domain or an ancestor of
    ``requested`` along the dotted path, e.g. ``code`` vs ``code.game``);
    0 = no match (siblings such as ``code.web`` vs ``code.game``, or
    unrelated). ``requested=None`` matches every domain at rank 1.
    """
    if requested is None:
        return 1
    if actual is None:
        return 0
    requested, actual = str(requested), str(actual)
    if actual == requested:
        return 2
    if actual.startswith(requested + ".") or requested.startswith(actual + "."):
        return 1
    return 0


def _cost_per_validity_point(validity_points, validity_cost_usd):
    if validity_cost_usd is not None and validity_points not in (None, 0):
        return validity_cost_usd / validity_points
    return None


def _validate_token_count(name, value):
    """Ledger token counts (tightening spec T7; security F8,
    correctness F5): None, or a non-negative integer. Integral floats
    are accepted and stored as int; bool, str, NaN/Inf, fractions and
    negatives raise ValueError — the store never writes a row its own
    chain seal would reject or a stamp would print as negative spend.
    """
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        result = value
    elif (isinstance(value, float) and math.isfinite(value)
            and value.is_integer()):
        result = int(value)
    else:
        raise ValueError(
            f"invalid {name} {value!r}: must be None or a non-negative"
            " integer")
    if result < 0:
        raise ValueError(
            f"invalid {name} {value!r}: must be None or a non-negative"
            " integer")
    return result


def _validate_cost_usd(value):
    """Ledger cost (tightening spec T7): None, or a finite
    non-negative number, stored as float. Bool/str/NaN/Inf/negative
    raise ValueError."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"invalid cost_usd {value!r}: must be None or a finite"
            " non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(
            f"invalid cost_usd {value!r}: must be None or a finite"
            " non-negative number")
    return result


def _row_variants(prev_hash, entry):
    """Digests of a ledger row under every recognized canonical form.

    Delegates to :func:`chain.row_hash_variants` (fix-spec pinned
    interface): the row dict handed over carries its ``prev_hash``,
    and the result maps ``"current"`` / ``"pre_sealed_via"`` /
    ``"legacy"`` to the digest under that form. The sealing pass
    recognizes a stored hash matching ANY form; only ``"current"``
    is ever written.
    """
    row = dict(entry)
    row["prev_hash"] = prev_hash
    return _chain.row_hash_variants(row)


class Store:
    """SQLite-backed store for Hybrid Critic Loop state.

    Thread-safe (fix pass Wave 2.6, review H8): Hermes runs hook callbacks
    and tool handlers on worker threads while one Store serves a whole
    registration, so the connection is opened with ``check_same_thread=False``
    and every method runs under one reentrant lock (applied after the class
    body) — the host's own convention for plugin databases.
    """

    def __init__(self, db_path):
        self._lock = threading.RLock()
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            parent = Path(self.db_path).expanduser().parent
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._migrate()
            # Tightening spec T3: rows the open-time sealing pass sealed, by
            # route — "backfill" (pre-chain store) / "repair" (flagged
            # store, unsealed suffix). Fix spec G2 adds "upgrade" (the
            # canonical-form cascade) as chain_upgrades. All three are
            # set by _backfill_chain below.
            self.chain_backfilled = 0
            self.chain_repairs = 0
            self.chain_upgrades = 0
            self._backfill_chain()
        except sqlite3.OperationalError as exc:
            # Fix spec G2 (round-4 upgrade F1): a locked database at
            # open almost always means another process — typically a
            # long-lived host session holding its store for its whole
            # life — has the write lock. Name the likely holder instead
            # of surfacing sqlite's bare "database is locked", which
            # sent operators hunting for corruption.
            if "locked" not in str(exc).lower():
                raise
            raise sqlite3.OperationalError(
                "database is locked: another process is likely holding"
                f" this store open (database: {self.db_path}); a"
                " long-lived host session is the usual holder — close"
                " it or wait for it to exit, then retry") from exc

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
        # Tightening spec T2: rows written before the provenance column
        # existed carry no attribution; label them "legacy-import"
        # (counted by the totals, but never mistaken for rows a writer
        # attributed at insert time). Idempotent: after the first open
        # no row has NULL provenance.
        ledger_cols = {
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info(ledger)").fetchall()
        }
        if "provenance" in ledger_cols:
            # Round-4 performance P8: probe before updating — the
            # unconditional UPDATE scans the whole ledger and takes
            # the WAL write path on every open while matching zero
            # rows on a healthy book, so opening was never read-only.
            unlabelled = self._conn.execute(
                "SELECT 1 FROM ledger WHERE provenance IS NULL"
                " LIMIT 1").fetchone()
            if unlabelled is not None:
                self._conn.execute(
                    "UPDATE ledger SET provenance='legacy-import'"
                    " WHERE provenance IS NULL")
        self._conn.commit()

    def _chain_intact_probe(self):
        """Cheap open-time probe (fix spec G3; round-4 performance P1).

        True when the sealing pass has nothing to do: no ledger row is
        missing hashes, and the tail row's stored hash recomputes
        under the CURRENT canonical form and links to its
        predecessor's stored hash. A healthy current-form store thus
        skips the full recompute walk on open (the old pass rehashed
        every row on every open — ~27 µs/row, seconds at book scale).
        A book failing the probe — unsealed rows, a historical-form
        tail, a broken link — takes the full pass below, and
        ``chain.verify_chain`` keeps the full walk for callers that
        want every row checked.
        """
        unsealed = self._one(
            "SELECT 1 AS x FROM ledger"
            " WHERE COALESCE(prev_hash, '') = ''"
            " OR COALESCE(row_hash, '') = '' LIMIT 1")
        if unsealed is not None:
            return False
        tail = self._all(
            "SELECT * FROM ledger ORDER BY id DESC LIMIT 2")
        if not tail:
            return True
        last = tail[0]
        entry = self._ledger_dict(last)
        variants = _row_variants(_col(last, "prev_hash"), entry)
        if variants["current"] != _col(last, "row_hash"):
            return False
        if len(tail) > 1:
            expected_prev = _col(tail[1], "row_hash")
        else:
            expected_prev = _chain.GENESIS_HASH
        return _col(last, "prev_hash") == expected_prev

    def _backfill_chain(self):
        """Seal unsealed ledger rows on open (tightening spec T3;
        correctness F1/F9, security F1/F4; fix spec G2/G3).

        Gated first on :meth:`_chain_intact_probe` — a healthy
        current-form store skips the walk entirely. Three cases when
        the full pass runs:

        - Pre-chain store (``user_version`` below
          ``_CHAIN_BACKFILL_VERSION``): every unsealed row is sealed in
          id order with ``sealed_via="backfill"`` and the flag is set.
        - Flagged store: an unsealed SUFFIX — rows after the last hashed
          row, the legacy-writer append case — is sealed with
          ``sealed_via="repair"``.
        - Corruption: NULL hashes in the middle (a hashed row exists
          after them), or a pre-existing hash that recomputes under
          no recognized canonical form. Pre-existing hashes are
          recomputed during the pass, never trusted as links (the old
          pass trusted them, so a tampered row's stored hash became
          the seal for everything after it). Corrupt rows and
          everything after them are left exactly as found:
          ``verify_chain`` names the first bad row, and ``add_ledger``
          refuses to extend a book whose last row is unsealed.
        - Canonical-form upgrade (tightening spec T12; fix spec G2):
          a sealed row whose stored hash matches a historical form
          (``chain.row_hash_variants``: "pre_sealed_via" — the form
          before ``sealed_via`` joined the canonical fields — or
          "legacy", which also lacks ``provenance``) is re-sealed
          under the current form with ``sealed_via="upgrade"``.
          Re-sealing changes that row's hash, so the re-seal cascades
          over every later sealed row (each still validated against
          its stored link first); a store sealed under an older form
          thus survives the upgrade with its chain intact.

        Fix spec G2 (round-3 F1, round-4 upgrade F1/F2): the pass
        COMMITS before every non-abort return whenever it changed
        anything — the T12 cascade previously returned with its
        UPDATEs still in an open transaction, so the upgrade never
        landed on disk, repeated on every open, and the dangling
        write transaction locked every other process out for the
        holder's whole life. Counters accumulate (``+=``) and are
        published only on commit paths, so an aborted pass reports
        the zeroes it actually sealed.

        ``self.chain_backfilled`` / ``self.chain_repairs`` /
        ``self.chain_upgrades`` record how many rows this open sealed
        by each route.
        """
        self.chain_backfilled = 0
        self.chain_repairs = 0
        self.chain_upgrades = 0
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        pre_chain = version < _CHAIN_BACKFILL_VERSION
        if self._chain_intact_probe():
            if pre_chain:
                self._conn.execute(
                    f"PRAGMA user_version = {_CHAIN_BACKFILL_VERSION}")
                self._conn.commit()
            return
        rows = self._all("SELECT * FROM ledger ORDER BY id ASC")
        if not rows:
            if pre_chain:
                self._conn.execute(
                    f"PRAGMA user_version = {_CHAIN_BACKFILL_VERSION}")
                self._conn.commit()
            return
        first_unsealed = None
        for idx, row in enumerate(rows):
            if not _col(row, "prev_hash") or not _col(row, "row_hash"):
                first_unsealed = idx
                break
        # Recompute the sealed prefix (every row before the first
        # unsealed one), following the STORED links: a row's hash is
        # always computed over its predecessor's stored hash. A stored
        # hash that matches no recognized canonical form, or does not
        # link to its predecessor, is corruption — seal nothing. A row
        # sealed under a historical form starts a re-seal cascade:
        # its new-form digest differs from its stored hash, so every
        # later row's prev link changes and the rest of the prefix is
        # re-sealed under the current form too, each row still
        # validated against its stored link before it is rewritten —
        # a tampered row aborts the whole pass.
        n_repairs = 0
        n_upgrades = 0
        prefix = rows if first_unsealed is None else rows[:first_unsealed]
        prev = _chain.GENESIS_HASH        # stored link expected next
        prev_new = _chain.GENESIS_HASH    # new-form chain value
        resealing = False
        changed = False
        for row in prefix:
            entry = self._ledger_dict(row)
            stored_prev = _col(row, "prev_hash")
            stored = _col(row, "row_hash")
            if stored_prev != prev:
                self._conn.rollback()
                return
            variants = _row_variants(stored_prev, entry)
            if variants["current"] == stored:
                if not resealing:
                    prev = stored
                    prev_new = stored
                    continue
                # A current-form row caught in a cascade: its link
                # changed, so it re-seals too (fall through).
            elif (variants["pre_sealed_via"] == stored
                    or variants["legacy"] == stored):
                resealing = True
            else:
                self._conn.rollback()
                return
            new_entry = dict(entry)
            new_entry["sealed_via"] = "upgrade"
            digest = _row_variants(prev_new, new_entry)["current"]
            self._conn.execute(
                "UPDATE ledger SET prev_hash=?, row_hash=?,"
                " sealed_via=? WHERE id=?",
                (prev_new, digest, "upgrade", row["id"]),
            )
            n_upgrades += 1
            changed = True
            prev = stored
            prev_new = digest
        prev = prev_new
        if first_unsealed is None:
            if pre_chain:
                self._conn.execute(
                    f"PRAGMA user_version = {_CHAIN_BACKFILL_VERSION}")
                changed = True
            self.chain_upgrades = n_upgrades
            if changed:
                # G2: the cascade's UPDATEs commit HERE, on the open
                # that performed them — not whenever some later write
                # happens to commit.
                self._conn.commit()
            return
        suffix = rows[first_unsealed:]
        if any(_col(row, "row_hash") for row in suffix):
            # A hashed row exists after an unsealed one: middle-NULL
            # corruption. Leave the book exactly as found.
            self._conn.rollback()
            return
        sealed_via = "backfill" if pre_chain else "repair"
        for row in suffix:
            entry = self._ledger_dict(row)
            entry["sealed_via"] = sealed_via
            digest = _row_variants(prev, entry)["current"]
            self._conn.execute(
                "UPDATE ledger SET prev_hash=?, row_hash=?, sealed_via=?"
                " WHERE id=?",
                (prev, digest, sealed_via, row["id"]),
            )
            prev = digest
        if pre_chain:
            self.chain_backfilled = len(suffix)
            self._conn.execute(
                f"PRAGMA user_version = {_CHAIN_BACKFILL_VERSION}")
        else:
            n_repairs += len(suffix)
        self.chain_repairs = n_repairs
        self.chain_upgrades = n_upgrades
        self._conn.commit()

    # -- internal helpers -------------------------------------------------

    def _begin_write(self):
        """Open a write transaction before an id is generated (fix pass Wave
        2.8, review M7 / 1C-11, F-W20-07): ``BEGIN IMMEDIATE`` takes the
        database's write lock, so a second Store on the same file — the CLI
        beside the plugin — waits for this insert to commit instead of
        scanning the same ids. The INSERT that follows runs inside it and
        the method's ``commit`` ends it."""
        if not self._conn.in_transaction:
            self._conn.execute("BEGIN IMMEDIATE")

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
        self._begin_write()
        try:
            target_id = self._next_id("tgt", "targets")
            created = _now()
            self._conn.execute(
                "INSERT INTO targets (id, ask, domain, constraints_json,"
                " budget_json, done_when, created_at) VALUES (?,?,?,?,?,?,?)",
                (target_id, ask, domain, _dumps(list(constraints or [])),
                 _dumps(budget), done_when, created),
            )
            self._conn.commit()
        except Exception:
            # Tightening spec T7 (correctness F7 audit): roll back so a
            # failed write never leaks its open transaction.
            self._conn.rollback()
            raise
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
        self._begin_write()
        try:
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
        except Exception:
            # Tightening spec T7 (correctness F7 audit): roll back so a
            # failed write never leaks its open transaction.
            self._conn.rollback()
            raise
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

    def set_candidate_payload_hash(self, candidate_id, payload_hash):
        """Record a candidate's payload hash (fix spec G1: the
        verdict-time snapshot — when a verdict names a winner, the
        loop re-points the winner's hash at the snapshot commit the
        human gate will later merge, so what merges is what was
        gated). Committed before returning."""
        self._conn.execute(
            "UPDATE candidates SET payload_hash=? WHERE id=?",
            (payload_hash, candidate_id))
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
        self._begin_write()
        try:
            verdict_id = self._next_id("vrd", "verdicts")
            self._conn.execute(
                "INSERT INTO verdicts (id, contract_id, attempt, winner,"
                " eliminated_json, hard, measured, judged_json, escalated,"
                " escalation_reason, waiver_json, human_gate, tie_break,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (verdict_id, contract_id, attempt, winner,
                 _dumps(list(eliminated or [])), hard, measured,
                 _dumps(judged), 1 if escalated else 0, escalation_reason,
                 _dumps(waiver), human_gate, tie_break, _now()),
            )
            self._conn.commit()
        except Exception:
            # Tightening spec T7 (correctness F7 audit): roll back so a
            # failed write never leaks its open transaction.
            self._conn.rollback()
            raise
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

    def verdict_for(self, contract_id, verdict_id):
        """The named verdict of a contract, or None (tightening spec
        T4: certificate verification must be able to check that the
        verdict a certificate names exists for that contract)."""
        row = self._one(
            "SELECT * FROM verdicts WHERE contract_id=? AND id=?",
            (contract_id, verdict_id))
        if row is None:
            return None
        return self._verdict_dict(row)

    def set_verdict_human_gate(self, verdict_id, decision):
        """Record the human's decision on the verdict row (fix pass Wave 2.6,
        review H5): after this the gate is no longer pending."""
        self._conn.execute(
            "UPDATE verdicts SET human_gate=? WHERE id=?", (decision, verdict_id),
        )
        self._conn.commit()

    def claim_human_gate(self, verdict_id, decision):
        """Atomically claim a verdict's pending human gate (fix spec
        G1; round-4 security F4, contest-core F2).

        One conditional UPDATE that fires only while the gate still
        reads ``"pending"``, committed before returning. True iff
        THIS caller recorded the decision — a concurrent or replayed
        caller gets False and must not merge. The claim IS the record:
        it replaces the old check-then-act sequence whose decision
        write landed after the merge and was swallowed on failure, so
        two processes could both complete one gate (two final
        answers) and a crash could leave a merged winner whose gate
        still read pending.
        """
        cur = self._conn.execute(
            "UPDATE verdicts SET human_gate=? WHERE id=?"
            " AND human_gate='pending'",
            (decision, verdict_id))
        self._conn.commit()
        return cur.rowcount == 1

    def set_verdict_route_hold(self, verdict_id, hold):
        """Record a route change held on a degraded verdict (owner floor
        rule, README "Panel floor"). ``hold`` is a dict stored as JSON, or
        None to clear it."""
        self._conn.execute(
            "UPDATE verdicts SET route_hold_json=? WHERE id=?",
            (_dumps(hold), verdict_id),
        )
        self._conn.commit()

    def set_verdict_waiver(self, verdict_id, waiver):
        """Record the verdict's waiver state (fix pass Wave 2.7, review H6):
        ``offered`` at the verdict when the winner failed the measured gate,
        ``granted`` once a human waived it explicitly. ``waiver`` is a dict
        stored as JSON, or None to clear it."""
        self._conn.execute(
            "UPDATE verdicts SET waiver_json=? WHERE id=?",
            (_dumps(waiver), verdict_id),
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
        """Apply a route demotion and report what actually changed
        (fix spec G8; round-3 contest-core F5).

        Branches, by the seat the demoted expert holds: the route's
        ``primary`` (the promoted expert takes the seat; the demoted
        expert becomes ``compete_with`` — with no promoted expert,
        nothing can move), its ``compete_with`` (the demoted expert
        drops to the head of the fallback list; the promoted expert,
        if any, takes the compete seat), or a ``fallback`` member
        (moved to the END of the fallback list). An expert holding no
        route position changes nothing — the old code had no fallback
        or absent branch at all, so most demotions were silent no-ops
        that callers nevertheless reported as done.

        Returns the resulting route dict (the ``get_route`` shape)
        plus the change report callers must speak from: ``changed``
        (bool — any route field actually moved), ``branch``
        ("primary" / "compete_with" / "fallback" / "none"),
        ``demoted``, and ``promoted`` (the expert that actually took
        a vacated seat, else None). When nothing changed the route
        row is not rewritten at all.
        """
        route = self.get_route(category)
        if route is None:
            raise KeyError(f"unknown route category: {category}")
        primary = route["primary"]
        compete_with = route["compete_with"]
        fallback = list(route["fallback"])
        before = (primary, compete_with, list(fallback))
        branch = "none"
        promoted = None
        if demote_expert == primary:
            branch = "primary"
            if promote_expert:
                primary = promote_expert
                compete_with = demote_expert
                promoted = promote_expert
        elif demote_expert == compete_with:
            branch = "compete_with"
            if demote_expert in fallback:
                fallback.remove(demote_expert)
            fallback.insert(0, demote_expert)
            compete_with = promote_expert if promote_expert else None
            promoted = compete_with
        elif demote_expert in fallback:
            branch = "fallback"
            fallback.remove(demote_expert)
            fallback.append(demote_expert)
        changed = (primary, compete_with, fallback) != before
        if changed:
            self._conn.execute(
                'UPDATE routes SET "primary"=?, compete_with=?,'
                " fallback_json=?, updated_at=? WHERE category=?",
                (primary, compete_with, _dumps(fallback), _now(),
                 category),
            )
            self._conn.commit()
        result = self.get_route(category)
        result["changed"] = changed
        result["branch"] = branch
        result["demoted"] = demote_expert
        result["promoted"] = promoted if changed else None
        return result

    # -- ledger --------------------------------------------------------------------

    def add_ledger(self, target_id, contract_id, candidate_id, stage,
                   expert_id, tokens_in, tokens_out, cost_usd, outcome,
                   note=None, provenance=None):
        if outcome not in _LEDGER_OUTCOMES:
            raise ValueError(
                f"invalid ledger outcome {outcome!r}: must be one of"
                f" {sorted(_LEDGER_OUTCOMES)}")
        # Input discipline (tightening spec T7): validate before the
        # write transaction opens, so a rejected row never touches the
        # book. Provenance (T2): an unattributed call is recorded as
        # such — the totals never count it.
        tokens_in = _validate_token_count("tokens_in", tokens_in)
        tokens_out = _validate_token_count("tokens_out", tokens_out)
        cost_usd = _validate_cost_usd(cost_usd)
        if provenance is None:
            provenance = "unattributed"
        # Hash chain (standing spec, Unit B): the new row seals the
        # previous row's hash. The id is assigned here, inside the write
        # transaction and the store lock, because the id is one of the
        # row's own canonical fields. On any failure the transaction is
        # rolled back before the error propagates (correctness F7) —
        # a leaked open transaction would let the next unrelated write
        # commit this partial row under its own commit.
        self._begin_write()
        try:
            last = self._one(
                "SELECT id, row_hash FROM ledger ORDER BY id DESC LIMIT 1")
            if last is None:
                next_id, prev = 1, _chain.GENESIS_HASH
            elif not last["row_hash"]:
                # Tightening spec T3: never seal onto GENESIS over an
                # unsealed predecessor — that would paper over an
                # unsealed/corrupt tail. The open-time sealing pass or
                # the operator deals with the tail first.
                raise ValueError(
                    f"cannot append ledger row: predecessor row"
                    f" {last['id']} is unsealed (no row_hash); the"
                    " chain must be repaired before the book grows")
            else:
                next_id = int(last["id"]) + 1
                prev = last["row_hash"]
            created = _now()
            entry = {
                "id": next_id,
                "target_id": target_id,
                "contract_id": contract_id,
                "candidate_id": candidate_id,
                "stage": stage,
                "expert_id": expert_id,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost_usd,
                "outcome": outcome,
                "note": note,
                "created_at": created,
                "provenance": provenance,
                "sealed_via": "append",
            }
            digest = _chain.row_hash(prev, entry)
            cur = self._conn.execute(
                "INSERT INTO ledger (id, target_id, contract_id,"
                " candidate_id, stage, expert_id, tokens_in, tokens_out,"
                " cost_usd, outcome, note, created_at, prev_hash,"
                " row_hash, provenance, sealed_via)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (next_id, target_id, contract_id, candidate_id, stage,
                 expert_id, tokens_in, tokens_out, cost_usd, outcome,
                 note, created, prev, digest, provenance, "append"),
            )
            row_id = cur.lastrowid
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return row_id

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
            "prev_hash": _col(row, "prev_hash"),
            "row_hash": _col(row, "row_hash"),
            "provenance": _col(row, "provenance"),
            "sealed_via": _col(row, "sealed_via"),
        }

    def ledger_summary(self):
        """One row per expert that was dispatched at least once.

        A dispatch is a generation row (completed / refused / timeout /
        unavailable / error). The queue's ``observed`` and ``escalated``
        notes are not dispatches, and an expert with no generation rows —
        the ``queue`` pseudo-expert — has no summary row (fix pass Wave 2.8,
        review M3). Tokens and cost are summed over reported values only;
        None when nothing was reported.
        """
        rows = self.ledger_rows()
        order = []
        agg = {}
        for row in rows:
            if row["outcome"] not in _GENERATION_OUTCOMES:
                continue
            eid = row["expert_id"]
            if eid not in agg:
                order.append(eid)
                agg[eid] = {
                    "expert_id": eid,
                    "dispatches": 0,
                    "completed": 0,
                    "refused": 0,
                    "timeout": 0,
                    "unavailable": 0,
                    "error": 0,
                    "_tin": [],
                    "_tout": [],
                    "_cost": [],
                }
            entry = agg[eid]
            entry["dispatches"] += 1
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
                "unavailable": entry["unavailable"],
                "error": entry["error"],
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
        self._begin_write()
        try:
            lesson_id = self._next_id("lsn", "lessons")
            self._conn.execute(
                'INSERT INTO lessons (id, contract_id, domain, tags_json,'
                ' outcome, cause, "rule", route_change_json, evidence_json,'
                " written_to, provenance, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (lesson_id, contract_id, domain, _dumps(list(tags or [])),
                 outcome, cause, rule, _dumps(route_change),
                 _dumps(list(evidence or [])), written_to, provenance,
                 _now()),
            )
            self._conn.commit()
        except Exception:
            # Tightening spec T7 (correctness F7 audit): roll back so a
            # failed write never leaks its open transaction.
            self._conn.rollback()
            raise
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
        """Lessons for ``domain``, ranked by domain match, tag overlap, newest.

        Domain match (fix pass Wave 2.3): the exact domain ranks first; a
        lesson in the same family — a sub-domain of the requested one
        (``code`` sees ``code.game``) or an ancestor of it (``code.game``
        sees ``code``) along the dotted path — ranks next; siblings
        (``code.web`` for ``code.game``) and unrelated domains do not match.
        ``domain=None`` matches everything. ``limit=None`` returns every
        match; otherwise the top ``limit`` after ranking.
        """
        rows = self._all("SELECT * FROM lessons")
        wanted = set(tags or [])
        ranked = []
        for row in rows:
            lesson = self._lesson_dict(row)
            rank = domain_match(domain, lesson.get("domain"))
            if rank:
                overlap = len(wanted.intersection(lesson["tags"]))
                ranked.append(((rank, overlap, lesson["created_at"], lesson["id"]), lesson))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        lessons = [lesson for _key, lesson in ranked]
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
            # Fix spec G2 (round-3 F1): belt-and-braces — if a sealing
            # transaction is somehow still open when the store closes,
            # commit it rather than letting the new hashes die with
            # the connection (the round-3 cascade survived only when
            # an unrelated write happened to commit it).
            if self._conn.in_transaction:
                self._conn.commit()
            self._conn.close()
            self._conn = None


def _synchronized(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


for _name, _member in list(vars(Store).items()):
    if _name.startswith("__") or isinstance(_member, (staticmethod, classmethod, property)):
        continue
    if callable(_member):
        setattr(Store, _name, _synchronized(_member))
