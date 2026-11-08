"""The critic loop: contract -> baseline -> wave -> gates -> verdict ->
human gate -> lesson.

Queue outcomes implemented (spec, Pipeline):
- hard/measured fail: retry with the error log attached, attempt + 1,
  cap 3, then escalate.
- no seat produces a candidate (every seat refused or was unavailable):
  escalate — every seat is already in the contest, so there is nobody to
  re-dispatch to; the ledger records what each seat did (fix pass Wave 2.8).
- below the judged minimum: a legacy rubric retries once, then escalates;
  the peer matrix escalates as soon as it passes nobody.
- human rejection: a new contract against the same target, with the
  rejection written in as a constraint (an observation on the new row).
- Phase-0 tie (both pass, no judged gate): fewer tokens wins; when
  tokens are unreported, wall-clock decides and the basis is recorded.
- a contract id is one run: a contract that already finished (completed,
  awaiting its human gate, escalated, rejected) or that left candidates
  behind answers ``already_run`` and is never dispatched again; a refused
  contract re-verifies its baseline on its next run (fix pass Wave 2.7).

Exactly four event types ever reach the operator: spread_over_threshold,
budget_exhaustion, retry_cap, human_gate. Every event is also written
to the durable events table (owner review item 8: events are surfaced
in the verdict and the events log — there is no ping API).

v0.3 (The Millionaire Matrix): the contest is a PANEL — one candidate
per available panel family, composed route-first from the operator's
seats. When the contract's judged item names the validity_matrix_v1
rubric, the judged gate is the peer validity matrix (blind, self-cells
stripped in code, calibration x confidence weighting) run as a
step-down bracket: every round the surviving field faces the full
gate stack and the field narrows by recorded validity score (see
bracket.py) until one candidate remains for the human gate. Legacy
rubrics keep the v0.2 judge-panel path below, unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import bracket as bracket_mod
from . import gates as gates_mod
from . import judges as judges_mod
from . import lessons as lessons_mod
from . import matrix as matrix_mod
from . import router as router_mod
from .contracts import verify_baseline
from .errors import PanelError
from .shellrun import sha256_text
from .worktrees import WorktreeManager

MAX_ATTEMPTS = 3
JUDGED_RETRIES = 1
# Panel fields can reach 16 candidates; slots are single letters.
SLOTS = [chr(c) for c in range(ord("a"), ord("z") + 1)]

# Well-known judge-pool families, used to seed registry rows for panel
# selection. Executors present in the run override these with their own
# family.
KNOWN_FAMILIES = {
    "codex": "openai",
    "grok": "xai",
    "claude": "anthropic",
    "gemini": "google",
    "agy": "google",
    "beam": "reflection",
    "qwen-local": "alibaba",
    "qwen-cli": "alibaba",
    "kimi": "moonshot",
    "deepseek": "deepseek",
    "solver-naive": "local-naive",
    "solver-ring": "local-ring",
    "solver-balanced": "local-balanced",
    "local-solver": "local",
}

EVENT_TYPES = ("spread_over_threshold", "budget_exhaustion", "retry_cap", "human_gate")

# Contract statuses that end a run. The contract id is spent (fix pass
# Wave 2.7, review H4 + 1C-11): a second run answers ``already_run``.
FINISHED_STATUSES = ("completed", "awaiting_human_gate", "escalated", "rejected")

# What a dispatched seat can have done; ledgered as itself (fix pass Wave
# 2.8, review M3). Mirrors state._GENERATION_OUTCOMES.
GENERATION_OUTCOMES = ("completed", "refused", "timeout", "unavailable", "error")


def _can_judge(executor) -> bool:
    """Whether a seat can act as a matrix peer / judge. Executors declare it
    with ``can_judge()``; older adapters fall back to "overrides judge()"."""
    fn = getattr(executor, "can_judge", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return False
    from .executors import Executor

    return type(executor).judge is not Executor.judge


def _is_available(executor) -> bool:
    try:
        ok, _why = executor.available()
        return bool(ok)
    except Exception:  # noqa: BLE001 - a broken probe is an unavailable seat
        return False


def _lesson_data_dir(store):
    """Where the readable ``lessons.md`` mirror goes: next to the store's
    database. A store with no location (``:memory:``, or no ``db_path``)
    gets None and no mirror is written — never the current directory,
    which may be the plugin's own (fix pass Wave 2.4, packet F3)."""
    db_path = getattr(store, "db_path", None)
    if not db_path or str(db_path) == ":memory:":
        return None
    return Path(str(db_path)).expanduser().parent


def _round_scores(outcome) -> dict:
    """A bracket round's own score record: every candidate the matrix
    scored in it (fix pass Wave 2.8, review M5)."""
    return {
        cid: {"median_mean": entry["median_mean"], "min_item": entry["min_row"],
              "spread": entry["spread"], "pass": entry["pass"],
              "scored": entry.get("scored", entry["median_mean"] is not None)}
        for cid, entry in outcome.per_candidate.items()
        if isinstance(entry, dict)
    }


def _tokens_total(candidate: dict) -> float:
    tin, tout = candidate.get("tokens_in"), candidate.get("tokens_out")
    if tin is None and tout is None:
        return float("inf")
    return float((tin or 0) + (tout or 0))


class _Run:
    def __init__(self, store, contract, repo_path, executors, human_decider):
        self.store = store
        self.contract = contract
        self.contract_id = contract["id"]
        self.repo_path = Path(repo_path)
        self.executors = executors
        self.human_decider = human_decider
        self.mgr = WorktreeManager(self.repo_path)
        self.events: list[dict] = []
        self.durations: dict[str, int] = {}
        self.target_id = contract.get("target_id")
        # Set when a lesson's route change was recorded but HELD because
        # the verdict's panel was degraded (owner floor rule, README
        # "Panel floor"): the run result carries it as route_change_hold.
        self.route_hold: dict | None = None
        # Candidate id -> the last validity median the peer matrix recorded
        # for it in the final attempt's bracket (fix pass Wave 2.7, review
        # H3): the only source of validity points.
        self.matrix_validity: dict[str, float] = {}

    # -- events ---------------------------------------------------------
    def _emit(self, type_: str, detail: str):
        """Record an operator event in the run result AND the durable
        events table (when the store has one). Only the four event
        types in EVENT_TYPES are ever emitted."""
        self.events.append({"type": type_, "detail": detail})
        if hasattr(self.store, "add_event"):
            try:
                self.store.add_event(self.contract_id, type_, detail)
            except Exception:  # noqa: BLE001 - events must not break a run
                pass

    # -- setup ---------------------------------------------------------
    def _ensure_rows(self):
        store = self.store
        if not self.target_id:
            self.target_id = store.create_target(
                ask=self.contract.get("ask") or self.contract["task"],
                domain=self.contract["domain"],
                constraints=self.contract.get("constraints") or [],
                budget=self.contract.get("budget"),
                done_when="human_gate",
            )
        if store.get_contract(self.contract_id) is None:
            store.create_contract(
                target_id=self.target_id,
                stage=self.contract["stage"],
                task=self.contract["task"],
                domain=self.contract["domain"],
                tags=self.contract.get("tags", []),
                acceptance=self.contract["acceptance"],
                depends_on=self.contract.get("depends_on", []),
                test_author_family=self.contract.get("test_author_family"),
                repo_path=str(self.repo_path),
                attempt=self.contract.get("attempt", 1),
                contract_id=self.contract_id,
            )
        for eid, ex in self.executors.items():
            if store.get_expert(eid) is None:
                tier = "free" if ex.family == "local" else "subscription"
                store.upsert_expert(eid, ex.family, access="cli", cost_tier=tier)
        router_mod.ensure_routes(store)

    def _budget_exhausted(self) -> str | None:
        target = self.store.get_target(self.target_id) or {}
        budget = target.get("budget") or {}
        if not budget:
            return None
        rows = [r for r in self.store.ledger_rows() if r.get("target_id") == self.target_id]
        tokens = sum((r.get("tokens_in") or 0) + (r.get("tokens_out") or 0) for r in rows)
        cost = sum(r.get("cost_usd") or 0.0 for r in rows)
        if budget.get("tokens") is not None and tokens >= budget["tokens"]:
            return f"token budget exhausted ({tokens} >= {budget['tokens']})"
        if budget.get("usd") is not None and cost >= budget["usd"]:
            return f"usd budget exhausted ({cost} >= {budget['usd']})"
        return None

    def _already_run(self, status) -> dict:
        """A contract id is one run (fix pass Wave 2.7, review H4 + 1C-11):
        the recorded status, and for a pending gate the surface that
        completes it, instead of a second dispatch."""
        if status == "awaiting_human_gate":
            hint = ("its human gate is pending — decide it with the critic_gate tool, "
                    "`/critic gate <id> approve|reject|waive` or `hermes critic gate`, "
                    "not a re-run")
        elif status == "completed":
            hint = "its winner is merged and its verdict recorded"
        elif status == "escalated":
            hint = "it escalated to the operator; open a new contract id for another attempt"
        elif status == "rejected":
            verdict = self.store.latest_verdict(self.contract_id) or {}
            follow_up = f"{self.contract_id}_rej{verdict.get('attempt', 1)}"
            hint = f"the rejection opened {follow_up}; run that contract"
        else:
            hint = ("a previous run left candidates behind; open a new contract id "
                    "for another attempt")
        return {
            "contract_id": self.contract_id, "status": "already_run",
            "contract_status": status,
            "detail": (f"contract {self.contract_id} already ran (status {status}); "
                       f"a contract id is one run — {hint}"),
            "events": self.events,
        }

    # -- generation ----------------------------------------------------
    def _context_for(self, retry_notes: str) -> str:
        parts = []
        lesson_text = lessons_mod.lessons_context(
            self.store, self.contract["domain"], self.contract.get("tags", [])
        )
        if lesson_text:
            parts.append(lesson_text)
        if retry_notes:
            parts.append(retry_notes)
        return "\n\n".join(parts)

    def _dispatch(self, expert_id, slot, attempt, context):
        """Generate one candidate. Returns (candidate_dict, gen_result)."""
        store = self.store
        executor = self.executors[expert_id]
        wt = self.mgr.create(self.contract_id, f"{slot}{attempt if attempt > 1 else ''}")
        baseline_hash = self.mgr.baseline_hash()
        cand_id = store.create_candidate(
            contract_id=self.contract_id,
            attempt=attempt,
            stage=self.contract["stage"],
            expert_id=expert_id,
            family=executor.family,
            slot=slot,
            worktree=str(wt),
            baseline_hash=baseline_hash,
        )
        ok, why = executor.available()
        if not ok:
            from .executors import GenResult

            gen = GenResult(
                status="unavailable",
                log=f"executor {expert_id} unavailable: {why}",
                tokens_in=None, tokens_out=None,
                cost_usd=None, request_ids=[], duration_ms=0, detail=why,
            )
        else:
            gen = executor.generate(wt, self.contract, context)
        self.durations[cand_id] = gen.duration_ms
        evidence = {
            "log_hash": sha256_text(gen.log) if gen.log else None,
            "provider_request_ids": gen.request_ids,
            "detail": gen.detail,
        }
        store.update_candidate(
            cand_id,
            payload_hash=None,
            evidence=evidence,
            tokens_in=gen.tokens_in,
            tokens_out=gen.tokens_out,
            cost_usd=gen.cost_usd,
            status=gen.status,
            # The adapter is the source of the provenance the gate checks
            # (fix pass Wave 2.10, review 1D-18), not a literal per row.
            provenance=getattr(gen, "provenance", None) or "generated",
        )
        # The ledger records what the seat did: a refusal, a missing binary
        # and an adapter error are different facts. Unknown -> error.
        outcome = gen.status if gen.status in GENERATION_OUTCOMES else "error"
        store.add_ledger(
            target_id=self.target_id, contract_id=self.contract_id, candidate_id=cand_id,
            stage=self.contract["stage"], expert_id=expert_id,
            tokens_in=gen.tokens_in, tokens_out=gen.tokens_out, cost_usd=gen.cost_usd,
            outcome=outcome, note=gen.detail or gen.status,
        )
        if gen.status == "completed":
            diff = self.mgr.diff(wt)
            store.update_candidate(cand_id, payload_hash=sha256_text(diff))
        elif gen.status == "refused":
            store.record_refusal(expert_id)
        return store.get_candidate(cand_id), gen

    # -- main ----------------------------------------------------------
    def run(self) -> dict:
        store = self.store
        self._ensure_rows()
        contract_row = store.get_contract(self.contract_id)

        # A contract id is one run (fix pass Wave 2.7, review H4 + 1C-11).
        # A finished contract is never dispatched again — its verdict,
        # candidates and merge are facts — and a run interrupted after
        # dispatch left candidates whose ids a second dispatch would
        # collide with. Before, the only guard was the baseline row, which
        # a first run had already written.
        status = contract_row.get("status")
        if status in FINISHED_STATUSES or store.candidates_for(self.contract_id):
            return self._already_run(status)

        # Baseline rule (invariant 3): the acceptance suite must have FAILED
        # on the unmodified baseline before any candidate is dispatched.
        # Only a recorded failing baseline skips the check; a refused
        # contract (its row says "pass") is re-verified against the current
        # tree on its next run instead of slipping past the rule.
        if contract_row.get("acceptance_baseline_run") != "fail":
            proceed, detail = verify_baseline(store, self.contract, self.repo_path, self.mgr)
            if not proceed:
                store.set_contract_status(self.contract_id, "refused")
                store.add_ledger(
                    target_id=self.target_id, contract_id=self.contract_id, candidate_id=None,
                    stage=self.contract["stage"], expert_id="queue", tokens_in=None,
                    tokens_out=None, cost_usd=None, outcome="escalated",
                    note="acceptance passed on baseline; contract refused as vacuous",
                )
                return {
                    "contract_id": self.contract_id, "status": "refused_baseline",
                    "detail": "acceptance tests pass on the baseline; the contract is vacuous and was refused",
                    "baseline": detail, "events": self.events,
                }
            if status == "refused":
                store.set_contract_status(self.contract_id, "open")
                store.add_ledger(
                    target_id=self.target_id, contract_id=self.contract_id, candidate_id=None,
                    stage=self.contract["stage"], expert_id="queue", tokens_in=None,
                    tokens_out=None, cost_usd=None, outcome="observed",
                    note=("baseline re-verified on re-run: acceptance now fails on the "
                          "baseline; contract reopened"),
                )

        category = router_mod.category_of(self.contract["domain"])
        route = router_mod.route_for(store, category)
        # Panel composition (v0.3): route-first order — primary,
        # compete_with, then the fallback order, then any remaining
        # executor ids. The panel is every seat the operator handed to
        # this run; each generates exactly one candidate.
        ordered = [route.get("primary"), route.get("compete_with")]
        ordered += list(route.get("fallback", []))
        ordered += sorted(self.executors)
        contestants = [c for c in dict.fromkeys(ordered) if c and c in self.executors]

        retry_notes = ""
        judged_retries = 0
        attempt = int(self.contract.get("attempt", 1))
        last_gate_details: dict = {"measurements": [], "hard_failures": [], "judged": None,
                                   "candidate_experts": {}, "winner_expert": None,
                                   "loser_experts": [], "escalation_reason": None}

        while True:
            exhausted = self._budget_exhausted()
            if exhausted:
                self._emit("budget_exhaustion", exhausted)
                return self._escalate(attempt, exhausted, last_gate_details)

            # Gate evidence is per attempt (fix pass Wave 2.7, review H7):
            # the Lesson and the escalation flags describe the attempt that
            # decided, not a hard failure a retry already recovered from.
            # Candidate -> expert stays: it is keyed by candidate id.
            last_gate_details["hard_failures"] = []
            last_gate_details["measurements"] = []
            last_gate_details["judged"] = None

            # Generation wave: identical inputs, isolated worktrees.
            context = self._context_for(retry_notes)
            wave = []  # (candidate, gen)
            for i, expert_id in enumerate(contestants):
                if expert_id not in self.executors:
                    continue
                cand, gen = self._dispatch(expert_id, SLOTS[i], attempt, context)
                wave.append((cand, gen))
                last_gate_details["candidate_experts"][cand["id"]] = expert_id

            generated = [(c, g) for c, g in wave if g.status == "completed"]
            if not generated:
                # Every seat is already in the contest: there is nobody to
                # re-dispatch to (the v0.2 fallback re-dispatch was dead
                # code — fix pass Wave 2.8, review M2). The ledger rows say
                # what each seat did.
                return self._escalate(
                    attempt,
                    "no seat produced a candidate: every seat refused or was unavailable",
                    last_gate_details,
                )

            if len(generated) > len(SLOTS):
                return self._escalate(
                    attempt,
                    f"panel field {len(generated)} exceeds the slot capacity {len(SLOTS)}",
                    last_gate_details,
                )

            # Gates, in rank order, per candidate.
            gate_results: dict[str, gates_mod.CandidateGateResult] = {}
            diffs: dict[str, str] = {}
            for cand, _gen in generated:
                gr = gates_mod.evaluate_candidate(
                    store, self.contract, cand, Path(cand["worktree"])
                )
                gate_results[cand["id"]] = gr
                diffs[cand["id"]] = self.mgr.diff(cand["worktree"])
                for mlist in gr.measured:
                    pass  # measurements already stored by the gate runner
                last_gate_details["measurements"] = [
                    m for c2, _ in generated for m in store.measurements_for(c2["id"])
                ]
                for hr in gr.hard:
                    if not hr.passed:
                        last_gate_details["hard_failures"].append(
                            {"candidate_id": cand["id"], "cmd": hr.cmd, "exit_code": hr.exit_code}
                        )

            survivors = [
                c for c, _ in generated
                if gate_results[c["id"]].eliminated_at is None
            ]

            # Judged gate, only for survivors, only when the contract asks.
            judged_items = [a for a in self.contract["acceptance"] if a["class"] == "judged"]
            judged_map: dict[str, dict] = {}
            if judged_items and survivors:
                judged_item = judged_items[0]
                if judged_item.get("rubric") == "validity_matrix_v1" and len(survivors) >= 2:
                    return self._run_matrix(
                        judged_item, survivors, generated, gate_results, diffs,
                        contestants, attempt, last_gate_details,
                    )
                if judged_item.get("rubric") == "validity_matrix_v1":
                    # A single anchor survivor: no contest to score.
                    # Fall through to the single_passer verdict below.
                    pass
                else:
                    outcome = self._run_judged(judged_item, survivors, gate_results, diffs)
                    if outcome is None:  # panel could not be fielded
                        return self._escalate(
                            attempt,
                            "judged gate could not field a family-clean panel",
                            last_gate_details,
                        )
                    if outcome.escalated:
                        reason = outcome.escalation_reason or "judge spread above threshold"
                        if "spread" in reason:
                            self._emit("spread_over_threshold", reason)
                        last_gate_details["judged"] = outcome.per_candidate
                        return self._escalate(attempt, reason, last_gate_details, judged=outcome.per_candidate)
                    judged_map = outcome.per_candidate
                    last_gate_details["judged"] = judged_map
                    survivors = [c for c in survivors if judged_map.get(c["id"], {}).get("pass")]

            # Anchor-only shortcut: when the contract carries judged
            # items but the hard/measured anchor left exactly ONE
            # survivor, the matrix has no contest to score — the
            # anchor decided (the v1 demo shape: naive dies on the
            # measured gate, ring stands alone). The matrix only runs
            # over fields of 2+.
            if judged_items and len(survivors) == 1:
                winner = survivors[0]
                eliminated = [c["id"] for c, _ in generated if c["id"] != winner["id"]]
                return self._verdict_and_human_gate(
                    attempt, winner, eliminated, gate_results, judged_map,
                    "single_passer", waiver_candidate=None,
                    last_gate_details=last_gate_details,
                )

            if len(survivors) == 1:
                winner = survivors[0]
                tie_break = "single_passer"
            elif len(survivors) > 1:
                winner, tie_break = self._pick_winner(survivors, judged_map)
            else:
                winner, tie_break = None, None

            if winner is not None:
                eliminated = [c["id"] for c, _ in generated if c["id"] != winner["id"]]
                return self._verdict_and_human_gate(
                    attempt, winner, eliminated, gate_results, judged_map, tie_break,
                    waiver_candidate=None, last_gate_details=last_gate_details,
                )

            # No winner. Waiver path: a candidate that passed hard and
            # failed ONLY measured may be waived by the human gate.
            waiver_candidate = next(
                (c for c, _ in generated if gate_results[c["id"]].eliminated_at == "measured"),
                None,
            )
            if waiver_candidate is not None:
                eliminated = [c["id"] for c, _ in generated if c["id"] != waiver_candidate["id"]]
                return self._verdict_and_human_gate(
                    attempt, waiver_candidate, eliminated, gate_results, judged_map, None,
                    waiver_candidate=waiver_candidate, last_gate_details=last_gate_details,
                )

            # Retry logic.
            all_judged_below = bool(judged_items) and all(
                gate_results[c["id"]].eliminated_at is None for c, _ in generated
            ) and generated
            if all_judged_below and judged_retries < JUDGED_RETRIES:
                judged_retries += 1
                retry_notes = self._failure_notes(attempt, generated, gate_results, judged_map)
                attempt += 1
                continue
            if attempt < MAX_ATTEMPTS:
                retry_notes = self._failure_notes(attempt, generated, gate_results, judged_map)
                attempt += 1
                continue
            self._emit(
                "retry_cap", f"retry cap {MAX_ATTEMPTS} reached on attempt {attempt}"
            )
            return self._escalate(attempt, f"retry cap {MAX_ATTEMPTS} reached", last_gate_details)

    # -- matrix bracket (v0.3) -------------------------------------------
    def _run_matrix(self, judged_item, field, generated, gate_results, diffs,
                    panel_ids, attempt, last_gate_details):
        """Peer validity matrix + scored step-down bracket.

        Every round, every surviving candidate faces the full gate
        stack (hard/measured results are already in ``gate_results``)
        plus the peer matrix; the field then steps down by recorded
        validity (bracket.py) with the round recorded in the ledger.
        A dissent citing a gate the majority may have missed re-runs
        that gate class once per attempt before anything merges; the
        bracket then re-runs its matrix for that round once.
        """
        store = self.store
        bracket_cfg = bracket_mod.bracket_config(self.contract)
        n_judges = int(judged_item.get("judges", 3))
        peer_ids, skipped = self._seat_peers(panel_ids, n_judges)
        peer_executors = {pid: self.executors[pid] for pid in peer_ids}
        store.add_ledger(
            target_id=self.target_id, contract_id=self.contract_id, candidate_id=None,
            stage=self.contract["stage"], expert_id="queue",
            tokens_in=None, tokens_out=None, cost_usd=None, outcome="observed",
            note=f"matrix peers seated={peer_ids}; skipped={skipped}",
        )
        calibrations = None
        experts_arg = None
        try:
            experts_arg = store.list_experts()
            calibrations = matrix_mod.compute_calibration(store, experts_arg)
        except Exception:  # noqa: BLE001 - calibration history is optional
            calibrations = None

        by_id = {c["id"]: c for c, _ in generated}
        # Validity points come from this attempt's bracket only (review H3).
        self.matrix_validity = {}
        # Every candidate the matrix scored, with the round it was last
        # scored in (fix pass Wave 2.8, review M5): the verdict's judged
        # block covers the whole bracket, not only the final round.
        history: dict[str, dict] = {}
        eliminated_total: list[str] = []
        rounds: list[dict] = []
        judged_map: dict[str, dict] = {}
        outcome = None
        rerun_used = False

        while True:
            # A dissent re-run repeats the matrix for the SAME round; the
            # round number advances only when a round is recorded.
            round_no = len(rounds) + 1
            outcome = matrix_mod.run_matrix(
                store, self.contract, field, gate_results, diffs,
                peer_executors,
                min_score=float(judged_item.get("min", 0)),
                dissent_threshold=float(judged_item.get("dissent_threshold", 3.0)),
                spread_threshold=float(judged_item.get("spread_threshold", 2.0)),
                calibrations=calibrations,
                seed=attempt,
            )
            # Every recorded median is a measured validity score (fix pass
            # Wave 2.7, review H3): keep the last one per candidate, so a
            # candidate the bracket cuts in an early round still earns
            # exactly what the matrix gave it — and nothing else does.
            for cid, entry in outcome.per_candidate.items():
                if isinstance(entry, dict) and entry.get("median_mean") is not None:
                    self.matrix_validity[cid] = float(entry["median_mean"])
            for cid in outcome.per_candidate:
                history[cid] = {"round": round_no, "outcome": outcome}
            # Seated peers that produced no valid cell this round (Phase 3
            # live-run finding): the record names them, so an unscored
            # candidate's missing median has a visible cause.
            silent_peers = [pid for pid in peer_ids if pid not in outcome.peers_used]

            if outcome.rerun_requests and not rerun_used:
                # A dissent cites a gate the majority may have missed:
                # re-run that gate class for the cited candidates, then
                # re-run the matrix for this round once. If the dissent
                # still cites afterwards, proceed and let the record
                # speak (the dissent stays in the verdict).
                rerun_used = True
                field_before = [c["id"] for c in field]
                anchored: list[str] = []
                for cid in outcome.rerun_requests:
                    cand = by_id.get(cid)
                    if cand is None:
                        continue
                    new_gr = gates_mod.evaluate_candidate(
                        store, self.contract, cand, Path(cand["worktree"])
                    )
                    gate_results[cid] = new_gr
                    # The re-run's rows are the deciding evidence from here
                    # (fix pass Wave 2.8, review M5): refresh what the Lesson
                    # and the escalation flags read.
                    for hr in new_gr.hard:
                        if not hr.passed:
                            last_gate_details["hard_failures"].append(
                                {"candidate_id": cid, "cmd": hr.cmd, "exit_code": hr.exit_code}
                            )
                    last_gate_details["measurements"] = [
                        m for c2, _ in generated for m in store.measurements_for(c2["id"])
                    ]
                    if new_gr.eliminated_at is not None and cid in field_before and cid not in anchored:
                        # The re-run anchored the dissent: the candidate
                        # is out regardless of its matrix score.
                        field = [c for c in field if c["id"] != cid]
                        eliminated_total.append(cid)
                        anchored.append(cid)
                if anchored:
                    # The anchor decided this round: record it (before, a
                    # candidate the re-run eliminated appeared in no round
                    # of the bracket record). The matrix then re-scores the
                    # survivors as the next round.
                    rounds.append({
                        "round": round_no,
                        "field_in": len(field_before),
                        "field_out": len(field),
                        "passed": sum(
                            1 for cid in field_before
                            if outcome.per_candidate.get(cid, {}).get("pass")
                        ),
                        "cut_line": None,
                        "eliminated": list(anchored),
                        "anchored_by_gate_rerun": list(anchored),
                        "scores": _round_scores(outcome),
                        "dissent": list(outcome.dissent),
                        "peers_without_cells": list(silent_peers),
                    })
                    store.add_ledger(
                        target_id=self.target_id, contract_id=self.contract_id,
                        candidate_id=None, stage=self.contract["stage"], expert_id="queue",
                        tokens_in=None, tokens_out=None, cost_usd=None,
                        outcome="observed",
                        note=(
                            f"bracket round {round_no}: field {len(field_before)} -> {len(field)}; "
                            f"dissent gate re-run anchored out {anchored}; "
                            f"panel_status={outcome.panel_status}"
                        ),
                    )
                if not field:
                    last_gate_details["judged"] = self._matrix_judged_map(
                        outcome, bracket_cfg, rounds, history)
                    last_gate_details["panel_status"] = outcome.panel_status
                    return self._escalate(
                        attempt,
                        "dissent gate re-run eliminated the whole field",
                        last_gate_details,
                        judged=last_gate_details["judged"],
                    )
                continue

            if outcome.escalated:
                reason = outcome.escalation_reason or "matrix escalation"
                if "spread" in reason:
                    self._emit("spread_over_threshold", reason)
                last_gate_details["judged"] = self._matrix_judged_map(
                    outcome, bracket_cfg, rounds, history)
                last_gate_details["panel_status"] = outcome.panel_status
                last_gate_details["dissent"] = list(outcome.dissent)
                return self._escalate(
                    attempt, reason, last_gate_details, judged=last_gate_details["judged"]
                )

            field_in = len(field)
            validity = {
                cid: entry["median_mean"] for cid, entry in outcome.per_candidate.items()
            }
            # Anchor BEFORE the cut (fix pass Wave 2.8, review M5): a
            # candidate the panel failed is out by score at this round and
            # never holds a slot a passer below it would have advanced
            # into. The ladder's cut ranks the passers only.
            passers, failers = [], []
            for cand in field:
                entry = outcome.per_candidate.get(cand["id"], {})
                (passers if entry.get("pass") else failers).append(cand)
            cut = bracket_mod.apply_cut(
                passers, validity, bracket_cfg, self.durations, field_size=field_in
            )
            advanced = cut["advanced"]
            cut_out = cut["eliminated"] + failers
            rounds.append({
                "round": round_no,
                "field_in": field_in,
                "field_out": len(advanced),
                "passed": len(passers),
                "cut_line": cut["cut_line"],
                "eliminated": [c["id"] for c in cut_out],
                # The round's own record: every scored candidate and the
                # dissent raised in it (the verdict keeps all rounds).
                "scores": _round_scores(outcome),
                "dissent": list(outcome.dissent),
                "peers_without_cells": list(silent_peers),
            })
            no_pass = "" if advanced else "; no candidate passed the matrix — nothing left to cut"
            silent_note = f"; peers_without_cells={silent_peers}" if silent_peers else ""
            store.add_ledger(
                target_id=self.target_id, contract_id=self.contract_id,
                candidate_id=None, stage=self.contract["stage"], expert_id="queue",
                tokens_in=None, tokens_out=None, cost_usd=None,
                outcome="observed",
                note=(
                    f"bracket round {round_no}: field {field_in} -> {len(advanced)}; "
                    f"passed={len(passers)}; cut_line={cut['cut_line']}; "
                    f"eliminated={[c['id'] for c in cut_out]}; "
                    f"panel_status={outcome.panel_status}{silent_note}{no_pass}"
                ),
            )
            eliminated_total.extend(c["id"] for c in cut_out)
            field = advanced
            if len(field) <= 1:
                break

        judged_map = self._matrix_judged_map(outcome, bracket_cfg, rounds, history)
        last_gate_details["judged"] = judged_map
        last_gate_details["panel_status"] = outcome.panel_status if outcome else "full"
        last_gate_details["dissent"] = list(outcome.dissent) if outcome else []
        last_gate_details["bracket"] = {
            "config": bracket_cfg, "rounds": rounds,
            "panel_status": last_gate_details["panel_status"],
        }
        if len(field) == 1:
            winner = field[0]
            return self._verdict_and_human_gate(
                attempt, winner, eliminated_total, gate_results, judged_map,
                "bracket_validity", waiver_candidate=None,
                last_gate_details=last_gate_details,
            )
        # The matrix failed every candidate (no passes anywhere):
        # fall back to the standard retry / measured-waiver machinery.
        waiver_candidate = next(
            (c for c, _ in generated if gate_results[c["id"]].eliminated_at == "measured"),
            None,
        )
        if waiver_candidate is not None:
            eliminated = [c["id"] for c, _ in generated if c["id"] != waiver_candidate["id"]]
            return self._verdict_and_human_gate(
                attempt, waiver_candidate, eliminated, gate_results, judged_map, None,
                waiver_candidate=waiver_candidate, last_gate_details=last_gate_details,
            )
        # No retry cap was reached here; the verdict row and the run result
        # carry the reason (fix pass Wave 2.5: a false retry_cap event was
        # emitted before).
        return self._escalate(
            attempt, "peer matrix passed no candidate", last_gate_details,
            judged=judged_map,
        )

    def _seat_peers(self, panel_ids, n_judges):
        """Peers for the validity matrix (fix pass Wave 2.5, review H1).

        Every executor that can judge and is available is a peer, in route
        order — **contestants included**: the matrix strips self-cells by
        family (``matrix.build_matrix_cells``), so a seat never scores its
        own family's candidate, and every other family's candidate gets
        scored by it. Seats whose family is unknown, that declare they
        cannot judge, or that report unavailable are skipped and named.
        The contract's judge count caps the seated peers (a cost dial).
        Returns ``(seated_ids, [(id, reason), ...])``.
        """
        ordered = [pid for pid in panel_ids if pid in self.executors]
        ordered += [e for e in sorted(self.executors) if e not in ordered]
        seated, skipped = [], []
        for pid in ordered:
            ex = self.executors[pid]
            if getattr(ex, "family", None) is None:
                skipped.append((pid, "family unknown"))
                continue
            if not _can_judge(ex):
                skipped.append((pid, "cannot judge"))
                continue
            try:
                ok, why = ex.available()
            except Exception as exc:  # noqa: BLE001 - a broken probe is an unavailable seat
                ok, why = False, f"available() raised {type(exc).__name__}"
            if not ok:
                skipped.append((pid, f"unavailable: {why}"))
                continue
            seated.append(pid)
        return seated[:max(n_judges, 1)], skipped

    def _matrix_judged_map(self, outcome, bracket_cfg, rounds, history=None):
        """The verdict's judged block for a matrix run: one entry per
        candidate the matrix scored in ANY round — taken from the round it
        was last scored in, which the entry names (fix pass Wave 2.8,
        review M5) — plus the bracket record with every round's scores."""
        judged: dict = {}
        scored: dict = {
            cid: (record["round"], record["outcome"])
            for cid, record in (history or {}).items()
        }
        if outcome is not None:
            final_round = len(rounds) if rounds else 1
            for cid in outcome.per_candidate:
                scored.setdefault(cid, (final_round, outcome))
        for cid, (round_no, out) in scored.items():
            entry = out.per_candidate.get(cid)
            if not isinstance(entry, dict):
                continue
            judged[cid] = {
                "median_mean": entry["median_mean"],
                "min_item": entry["min_row"],
                "spread": entry["spread"],
                "pass": entry["pass"],
                "scored": entry.get("scored", entry["median_mean"] is not None),
                "round": round_no,
                "dissent": [d for d in out.dissent if d["candidate_id"] == cid],
                "panel_status": entry["panel_status"],
                "mean_confidence": entry["mean_confidence"],
                "rows": entry["rows"],
            }
        judged["panel_status"] = outcome.panel_status if outcome is not None else "full"
        judged["bracket"] = {"config": bracket_cfg, "rounds": rounds}
        return judged

    # -- judged ----------------------------------------------------------
    def _run_judged(self, judged_item, survivors, gate_results, diffs):
        store = self.store
        category = router_mod.category_of(self.contract["domain"])
        route = router_mod.route_for(store, category)
        experts_map: dict[str, dict] = {e["id"]: e for e in store.list_experts()}
        for eid, ex in self.executors.items():
            experts_map.setdefault(eid, {"id": eid, "family": ex.family})
        for jid in route.get("judges", []):
            if jid not in experts_map and jid in KNOWN_FAMILIES:
                experts_map[jid] = {"id": jid, "family": KNOWN_FAMILIES[jid]}
        # A seat judges only if it declares it can and reports available
        # (fix pass Wave 2.5, review 1D-03): Agy, Beam and the local
        # solvers declare they cannot.
        judge_available = {
            eid for eid, ex in self.executors.items()
            if _can_judge(ex) and _is_available(ex)
        }
        contest_families = {c["family"] for c in survivors}
        try:
            panel = judges_mod.select_panel(
                route.get("judges", []), experts_map, contest_families,
                int(judged_item.get("judges", 3)), available=judge_available,
            )
        except PanelError:
            return None
        judge_executors = {jid: self.executors[jid] for jid in panel}
        return judges_mod.run_judged_gate(
            store, self.contract, judged_item, survivors, gate_results, diffs,
            judge_executors, panel,
        )

    # -- winner / verdict ------------------------------------------------
    def _pick_winner(self, survivors, judged_map):
        if judged_map:
            ranked = sorted(
                survivors,
                key=lambda c: judged_map.get(c["id"], {}).get("median_mean", 0.0),
                reverse=True,
            )
            top = judged_map.get(ranked[0]["id"], {}).get("median_mean", 0.0)
            tied = [c for c in ranked if judged_map.get(c["id"], {}).get("median_mean", 0.0) == top]
            if len(tied) == 1:
                return ranked[0], "judged_median"
            survivors = tied
        by_tokens = sorted(survivors, key=_tokens_total)
        if _tokens_total(by_tokens[0]) < _tokens_total(by_tokens[-1]):
            return by_tokens[0], "fewer_tokens"
        by_time = sorted(survivors, key=lambda c: self.durations.get(c["id"], 0))
        return by_time[0], "wall_clock_ms"

    def _failure_notes(self, attempt, generated, gate_results, judged_map) -> str:
        lines = [f"PREVIOUS ATTEMPT {attempt} FAILED AT THE GATES. Error log:"]
        for cand, _ in generated:
            gr = gate_results[cand["id"]]
            if gr.eliminated_at == "hard":
                bad = [h for h in gr.hard if not h.passed]
                for h in bad:
                    lines.append(
                        f"- candidate {cand['id']} hard gate: `{h.cmd}` exited {h.exit_code}\n"
                        f"  stderr tail: {h.stderr_tail[-500:]}"
                    )
            elif gr.eliminated_at == "measured":
                for m in self.store.measurements_for(cand["id"]):
                    if not m["pass"]:
                        lines.append(
                            f"- candidate {cand['id']} measured gate: {m['metric']} = {m['value']}, "
                            f"required {m['op']} {m['threshold']}"
                        )
            elif judged_map and cand["id"] in judged_map and not judged_map[cand["id"]]["pass"]:
                lines.append(
                    f"- candidate {cand['id']} judged gate: median {judged_map[cand['id']]['median_mean']} "
                    f"below the contract minimum"
                )
        lines.append("Fix the cause, not the test.")
        return "\n".join(lines)

    def _gate_summaries(self, gate_results, judged_map):
        hard = "pass"
        measured = "pass"
        for gr in gate_results.values():
            if gr.eliminated_at == "provenance":
                hard = "fail"
            if gr.eliminated_at == "hard":
                hard = "fail"
            if gr.eliminated_at == "measured":
                measured = "fail"
        judged_summary = None
        if judged_map:
            judged_summary = {}
            for cid, v in judged_map.items():
                if not isinstance(v, dict):
                    # Top-level bracket metadata (panel_status is a
                    # plain string) rides along unchanged.
                    judged_summary[cid] = v
                    continue
                if "median_mean" not in v:
                    # Top-level bracket metadata keys (the bracket
                    # record) ride along unchanged.
                    judged_summary[cid] = v
                elif "rows" in v or "dissent" in v:
                    judged_summary[cid] = {
                        "median_mean": v["median_mean"], "min_item": v["min_item"],
                        "spread": v["spread"], "pass": v["pass"],
                        "scored": v.get("scored", v.get("median_mean") is not None),
                        "round": v.get("round"),
                        "dissent": v.get("dissent", []),
                        "panel_status": v.get("panel_status"),
                        "mean_confidence": v.get("mean_confidence"),
                        "rows": v.get("rows"),
                    }
                else:
                    judged_summary[cid] = {
                        "median_mean": v["median_mean"], "min_item": v["min_item"],
                        "spread": v["spread"], "pass": v["pass"],
                    }
        return hard, measured, judged_summary

    # -- validity / calibration -------------------------------------------
    def _record_validity(self, winner, eliminated, gate_results, judged_map):
        """Record validity points once, at the final verdict — and only
        from recorded matrix medians (fix pass Wave 2.7, review H3).

        A validity point is one point of final validity score from the
        peer matrix: a candidate's ``median_mean`` in the verdict's judged
        map or, for a candidate the bracket cut in an earlier round of the
        same attempt, the median that cut it (``self.matrix_validity``).
        A candidate the matrix never scored — an anchor-only winner, a
        gate-eliminated candidate, a seat that produced nothing, a run
        whose matrix never ran — earns NO entry: its ledger line keeps
        ``validity_points`` unreported instead of an invented 10.0 or 0.0,
        and cost per validity point stays "not reported" (invariant 5:
        never estimated). Cost per expert is the candidate's recorded
        cost_usd — None when the executor did not report one (owner item
        4). Calibration is then refreshed from the score/verdict history.
        """
        store = self.store
        if not hasattr(store, "record_validity"):
            return
        by_id = {cand["id"]: cand for cand in store.candidates_for(self.contract_id)}
        winner_cand = winner if isinstance(winner, dict) else by_id.get(winner)
        ordered = ([winner_cand] if winner_cand else []) + [
            by_id[cid] for cid in eliminated if cid in by_id
        ]
        seen = set()
        for cand in ordered:
            if cand is None or cand["expert_id"] in seen:
                continue
            entry = (judged_map or {}).get(cand["id"])
            points = entry.get("median_mean") if isinstance(entry, dict) else None
            if points is None:
                points = self.matrix_validity.get(cand["id"])
            if points is None:
                continue  # never scored by the matrix: nothing to record
            seen.add(cand["expert_id"])
            try:
                store.record_validity(cand["expert_id"], float(points), cand.get("cost_usd"))
            except Exception:  # noqa: BLE001 - bookkeeping must not break a verdict
                pass
        try:
            matrix_mod.compute_calibration(store, store.list_experts())
        except Exception:  # noqa: BLE001
            pass

    def _verdict_and_human_gate(self, attempt, winner, eliminated, gate_results,
                                judged_map, tie_break, waiver_candidate, last_gate_details):
        store = self.store
        # The verdict's eliminated list covers every generated
        # candidate that did not win — gate-eliminated and
        # bracket-eliminated alike (an elimination is a verdict row).
        winner_id = winner["id"] if isinstance(winner, dict) else winner
        all_ids = [c["id"] for c in store.candidates_for(self.contract_id)]
        if all_ids:
            covered = set(eliminated or []) | {winner_id}
            eliminated = list(eliminated or []) + [
                cid for cid in all_ids if cid not in covered
            ]
        hard, measured, judged_summary = self._gate_summaries(gate_results, judged_map)
        is_waiver = waiver_candidate is not None and winner["id"] == waiver_candidate["id"]
        waiver = None
        if is_waiver:
            waiver = {"gate": "measured", "status": "offered", "candidate": winner["id"]}
            measured = "fail"
        verdict_id = store.create_verdict(
            contract_id=self.contract_id, attempt=attempt, winner=winner["id"],
            eliminated=eliminated, hard=hard, measured=measured, judged=judged_summary,
            escalated=False, escalation_reason=None, waiver=waiver,
            human_gate="pending", tie_break=tie_break,
        )
        category = router_mod.category_of(self.contract["domain"])
        store.record_expert_result(winner["expert_id"], category, won=True)
        self._record_validity(winner, eliminated, gate_results, judged_map)
        # A loss is charged once to each seat that FIELDED a candidate and
        # did not win (fix pass Wave 2.8, review M3) — never to a seat that
        # produced nothing (unavailable, refused) and never to the winner
        # for its own earlier attempt.
        by_id = {c["id"]: c for c in store.candidates_for(self.contract_id)}
        losers: list = []
        for cid in eliminated:
            cand = by_id.get(cid)
            if cand is None or cand.get("status") != "completed":
                continue
            eid = cand.get("expert_id")
            if eid and eid != winner["expert_id"] and eid not in losers:
                losers.append(eid)
        for eid in losers:
            store.record_expert_result(eid, category, won=False)
        last_gate_details["winner_expert"] = winner["expert_id"]
        last_gate_details["loser_experts"] = [
            last_gate_details["candidate_experts"].get(c) for c in eliminated
        ]
        lesson_id = self._write_lesson(attempt, last_gate_details)
        self._emit("human_gate", f"winner {winner['id']} awaits the human gate")
        result = {
            "contract_id": self.contract_id, "status": "awaiting_human_gate",
            "winner": winner["id"], "verdict_id": verdict_id, "lesson_id": lesson_id,
            "tie_break": tie_break, "events": self.events,
        }
        if self.route_hold is not None:
            result["route_change_hold"] = self.route_hold
        # The gate is pending from here until record_human_gate completes
        # it — whether the decider answers now or an operator does later;
        # a refused decision leaves the contract at its gate (Wave 2.7).
        store.set_contract_status(self.contract_id, "awaiting_human_gate")
        if self.human_decider is not None:
            decision = self.human_decider(
                {"winner": winner["id"], "waiver_offered": is_waiver, "verdict_id": verdict_id}
            )
            return self._apply_human_gate(verdict_id, decision, result)
        return result

    def _apply_human_gate(self, verdict_id, decision, result):
        return record_human_gate(
            self.store, self.contract_id, decision, _result=result, _mgr=self.mgr
        )

    # -- escalation / lesson ---------------------------------------------
    def _escalate(self, attempt, reason, gate_details, judged=None):
        store = self.store
        # Nobody won: every candidate fielded in the deciding attempt is out,
        # and the verdict row says so (fix pass Wave 2.10, review 1B-20).
        eliminated = [
            c["id"] for c in store.candidates_for(self.contract_id)
            if c.get("attempt") == attempt and c.get("status") == "completed"
        ]
        verdict_id = store.create_verdict(
            contract_id=self.contract_id, attempt=attempt, winner=None, eliminated=eliminated,
            hard="fail" if gate_details.get("hard_failures") else "pass",
            measured="fail" if any(not m["pass"] for m in gate_details.get("measurements", [])) else "pass",
            judged=judged, escalated=True, escalation_reason=reason, waiver=None,
            human_gate="not_reached", tie_break=None,
        )
        # The ledger carries the escalation as the queue's own row (fix pass
        # Wave 2.8, review M3), beside the baseline refusals it already had.
        store.add_ledger(
            target_id=self.target_id, contract_id=self.contract_id, candidate_id=None,
            stage=self.contract["stage"], expert_id="queue", tokens_in=None,
            tokens_out=None, cost_usd=None, outcome="escalated", note=reason,
        )
        self._record_validity(None, [], {}, judged)
        gate_details["escalation_reason"] = reason
        lesson_id = self._write_lesson(attempt, gate_details)
        store.set_contract_status(self.contract_id, "escalated")
        result = {
            "contract_id": self.contract_id, "status": "escalated",
            "detail": reason, "verdict_id": verdict_id, "lesson_id": lesson_id,
            "events": self.events,
        }
        if self.route_hold is not None:
            result["route_change_hold"] = self.route_hold
        return result

    def _write_lesson(self, attempt, gate_details):
        """Write the Lesson; apply its route change unless the verdict's
        panel is degraded.

        Owner floor rule (README, "Panel floor"): a degraded matrix can
        never produce the Phase-1 lesson-loop exit on its own. The Lesson
        (evidence-derived) is always stored; when the verdict that carries
        it has ``panel_status == "degraded"`` the route change is recorded
        and HELD — a ledger row for the expert, a ``route_hold`` on the
        verdict row, and ``route_change_hold`` on the run result — never
        applied automatically. Verdicts where the matrix never ran carry
        no panel status: the anchor decided, and the demotion applies.
        """
        store = self.store
        verdict = store.latest_verdict(self.contract_id)
        lesson = lessons_mod.write_lesson(
            store, _lesson_data_dir(store), self.contract, verdict, gate_details
        )
        if lesson and lesson.get("route_change"):
            if gate_details.get("panel_status") == "degraded":
                self._hold_route_change(lesson, verdict)
            else:
                router_mod.apply_lesson_route_change(
                    store, router_mod.category_of(self.contract["domain"]),
                    lesson["route_change"], self.contract_id,
                )
        return lesson["id"] if lesson else None

    def _hold_route_change(self, lesson, verdict):
        """Record a held route change visibly: ledger row, verdict row,
        run result. Nothing is applied."""
        store = self.store
        route_change = dict(lesson.get("route_change") or {})
        demote = route_change.get("demote")
        reason = route_change.get("reason") or ""
        hold = {
            "status": "held",
            "panel_status": "degraded",
            "lesson_id": lesson.get("id"),
            "route_change": route_change,
            "reason": (
                "verdict panel degraded (fewer than two peer families scored a "
                "candidate): the route change is recorded, not applied; "
                "operator review required"
            ),
        }
        self.route_hold = hold
        note = (
            f"route_change held (panel degraded): demote {demote} ({reason}) "
            f"not applied; operator review required; lesson {lesson.get('id')} "
            f"contract {self.contract_id}"
        )
        try:
            store.add_ledger(
                target_id=self.target_id, contract_id=self.contract_id,
                candidate_id=None, stage=self.contract["stage"], expert_id=demote,
                tokens_in=None, tokens_out=None, cost_usd=None,
                outcome="observed", note=note,
            )
        except Exception:  # noqa: BLE001 - bookkeeping must not break a verdict
            pass
        setter = getattr(store, "set_verdict_route_hold", None)
        if callable(setter) and verdict and verdict.get("id"):
            try:
                setter(verdict["id"], hold)
            except Exception:  # noqa: BLE001
                pass


def run_contract(store, contract: dict, repo_path, executors: dict, human_decider=None) -> dict:
    """Run one contract end-to-end. See module docstring for outcomes."""
    return _Run(store, contract, repo_path, executors, human_decider).run()


def record_human_gate(store, contract_id: str, decision: str, reason: str | None = None,
                      _result: dict | None = None, _mgr: WorktreeManager | None = None) -> dict:
    """Complete a pending human gate: approved merges the winner and
    writes the final answer — refused when the verdict carries an offered
    waiver (the winner failed the measured gate), where only an explicit
    ``waived`` merges and is recorded on the verdict row as granted;
    waived applies only to a measured failure, never a hard one; rejected
    creates a new contract carrying the rejection."""
    if decision not in ("approved", "rejected", "waived"):
        return {"contract_id": contract_id, "status": "error",
                "detail": f"unknown human-gate decision {decision!r}"}
    verdict = store.latest_verdict(contract_id)
    if verdict is None or verdict.get("human_gate") != "pending":
        return {"contract_id": contract_id, "status": "error",
                "detail": "no pending human gate for this contract"}
    contract = store.get_contract(contract_id)
    mgr = _mgr or WorktreeManager(Path(contract["repo_path"]))
    result = dict(_result or {"contract_id": contract_id})
    result["human_gate"] = decision

    def _record_decision(value):
        # The decision is a verdict-row fact (fix pass Wave 2.6, review H5):
        # once recorded, the gate is no longer pending and cannot be replayed.
        setter = getattr(store, "set_verdict_human_gate", None)
        if callable(setter):
            try:
                setter(verdict["id"], value)
            except Exception:  # noqa: BLE001 - bookkeeping must not undo a decision
                pass

    if decision == "rejected":
        _record_decision("rejected")
        new_id = f"{contract_id}_rej{verdict['attempt']}"
        if store.get_contract(new_id) is None:
            store.create_contract(
                target_id=contract["target_id"], stage=contract["stage"],
                task=contract["task"], domain=contract["domain"], tags=contract["tags"],
                acceptance=contract["acceptance"], depends_on=contract["depends_on"],
                test_author_family=contract.get("test_author_family"),
                # A fresh contract starts at attempt 1 with the full retry
                # budget (fix pass Wave 2.10, review 1B-21); its lineage is
                # the id suffix and the human_rejection observation.
                repo_path=contract.get("repo_path"), attempt=1,
                contract_id=new_id,
            )
        store.append_observation(
            new_id,
            {"kind": "human_rejection", "from_contract": contract_id,
             "reason": reason or "rejected at the human gate"},
        )
        store.set_contract_status(contract_id, "rejected")
        result.update({"status": "rejected", "new_contract_id": new_id})
        return result

    winner = store.get_candidate(verdict["winner"])
    waiver = verdict.get("waiver") or {}
    if decision == "approved" and waiver:
        # The winner failed the measured gate (fix pass Wave 2.7, review
        # H6, invariant 6): the only way past a measured failure is an
        # explicit waiver, recorded as such. A plain approval would merge
        # the failure silently with the row still saying "offered".
        result.update({
            "status": "error", "human_gate": "pending",
            "detail": (
                f"approval refused: winner {winner['id']} failed the measured gate and "
                "the verdict carries an offered waiver; waive it explicitly (decision "
                "'waived', with a reason) or reject — the gate stays pending"
            ),
        })
        return result
    if decision == "waived":
        if waiver.get("gate") != "measured" or waiver.get("candidate") != winner["id"]:
            return {"contract_id": contract_id, "status": "error",
                    "detail": "waiver applies only to a measured failure, never a hard one"}
    # approved (or a valid measured waiver): merge the winner.
    wt = Path(winner["worktree"])
    commit = mgr.commit_all(wt, f"critic: {contract_id} winner {winner['id']} ({decision})")
    merged_sha = mgr.merge_into_main(wt)
    for loser_id in verdict.get("eliminated", []):
        loser = store.get_candidate(loser_id)
        if loser and Path(loser["worktree"]).exists():
            mgr.keep_as_ref(contract_id, loser["slot"])
    mgr.remove(wt)
    waiver_record = None
    if decision == "waived":
        # The waiver is granted, by a human, for a reason — on the verdict
        # row, not only in the run result (fix pass Wave 2.7, review H6).
        waiver_record = dict(waiver)
        waiver_record.update({
            "gate": "measured", "status": "granted", "by": "human",
            "provenance": "human", "candidate": winner["id"],
            "reason": reason or "waived at the human gate",
        })
        setter = getattr(store, "set_verdict_waiver", None)
        if callable(setter):
            try:
                setter(verdict["id"], waiver_record)
            except Exception:  # noqa: BLE001 - bookkeeping must not undo a merge
                pass
    store.create_final_answer(
        target_id=contract["target_id"], contract_id=contract_id,
        commit=f"git:{merged_sha}",
        intent_check=None, human_gate=decision,
    )
    _record_decision(decision)
    store.set_contract_status(contract_id, "completed")
    result.update({"status": "completed", "commit": f"git:{merged_sha}",
                   "winner_commit": f"git:{commit}", "waiver": waiver_record})
    return result
