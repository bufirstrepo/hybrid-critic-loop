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

import contextvars
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import bracket as bracket_mod
from . import gates as gates_mod
from . import judges as judges_mod
from . import lessons as lessons_mod
from . import matrix as matrix_mod
from . import progress as progress_mod
from . import router as router_mod
from .contracts import verify_baseline
from .errors import GateError, PanelError
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
    "mm-openai": "openai",
    "mm-anthropic": "anthropic",
    "mm-xai": "xai",
    "mm-google": "google",
    "mm-alibaba": "alibaba",
    "mm-moonshot": "moonshot",
    "mm-deepseek": "deepseek",
    "mm-zai": "zai",
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


# Gate-result elimination classes -> the elimination kinds the loop
# persists on candidate rows (round-5 fix spec H5). gates.py's
# "provenance" failure is a hard-class failure — the verdict's hard
# summary already counts it as one — so it persists as "hard".
_ELIMINATED_KINDS = {
    "hard": "hard",
    "provenance": "hard",
    "measured": "measured",
    "gate_error": "gate_error",
}

# The persisted elimination kinds that are GATE failures: exactly the
# values _mark_eliminated persists for the gate stack's own
# elimination classes (round-11 fix spec Unit A). "judged" and
# "bracket_cut" are deliberately absent — those candidates passed the
# decidable gates. Deriving the set from _ELIMINATED_KINDS (rather
# than enumerating classes at each read site) means a future gate
# elimination class is a gate failure everywhere by construction.
_GATE_FAILED_KINDS = frozenset(_ELIMINATED_KINDS.values())

# The persisted elimination kinds that are NOT gate failures: the
# candidate passed the decidable gates and was eliminated later, by
# the judged gate ("judged") or by the bracket ("bracket_cut").
_NON_GATE_ELIMINATION_KINDS = frozenset({"judged", "bracket_cut"})


def _elimination_is_gate_failure(kind) -> bool:
    """Whether a candidate row's persisted elimination kind counts
    as a GATE failure for the route derivation (round-12 fix spec,
    Unit L; round-12 attack A4).

    The registered kinds answer from _GATE_FAILED_KINDS. A kind
    outside every registered class is ALSO a gate failure: the row
    was eliminated by something, it is not one of the two documented
    later-stage eliminations, and the burden of proof is on passing
    — the by-construction claim ("a future gate class is a gate
    failure everywhere") must not silently invert for a kind string
    no producer in this tree writes."""
    if not kind:
        return False
    if kind in _GATE_FAILED_KINDS:
        return True
    return kind not in _NON_GATE_ELIMINATION_KINDS


def _row_evidence(row) -> dict:
    """A candidate row's evidence as a dict — {} for any other shape
    (round-12 fix spec, Unit L; round-12 attack A5).

    Evidence is store JSON; a planted or legacy row can carry a
    non-dict value (a bare string, a list). Readers of the trusted
    elimination record coerce at the read instead of crashing on the
    shape — a crash inside _write_lesson would land AFTER the
    verdict exists and strand the run's bookkeeping."""
    if not isinstance(row, dict):
        return {}
    evidence = row.get("evidence")
    return evidence if isinstance(evidence, dict) else {}


def _measurement_failed(row) -> bool:
    """True when a measurement row records a failure, in either shape
    the rows arrive in (round-11 fix spec Unit A): store rows carry
    "pass"; raw gate rows carry "passed". Mirrors the predicate in
    lessons.derive_outcome_cause exactly — a row fails when pass is
    False, OR when "pass" is absent AND passed is False."""
    if not isinstance(row, dict):
        return False
    return row.get("pass") is False or (
        "pass" not in row and row.get("passed") is False
    )


def _claim_gate(store, verdict, decision, retry=False) -> bool:
    """Claim a verdict's pending human gate (fix spec G1; round-4
    security F4, contest-core F2).

    The store's ``claim_human_gate`` is one conditional UPDATE: the
    claim IS the decision record, taken BEFORE any merge, so two
    concurrent deciders cannot both complete one gate and a crash can
    no longer leave a merged winner whose gate still reads pending.
    With ``retry=True`` the claim also fires from the recorded
    "failed" state (round-5 fix spec H1): a gate whose completion
    failed after its claim is re-claimed by presenting the decision
    again, and completion is re-attempted from the snapshot. Stores
    without the claim (older test doubles) fall back to the state
    check plus an immediate ``set_verdict_human_gate`` — the same
    ordering, weaker atomicity."""
    claim = getattr(store, "claim_human_gate", None)
    if callable(claim):
        try:
            return bool(claim(verdict["id"], decision, retry=retry))
        except TypeError:
            # A claim predating the retry parameter (older doubles):
            # its conditional UPDATE fires from "pending" only, which
            # is exactly the non-retry semantics.
            return bool(claim(verdict["id"], decision))
    state = verdict.get("human_gate")
    if state != "pending" and not (retry and state == "failed"):
        return False
    setter = getattr(store, "set_verdict_human_gate", None)
    if callable(setter):
        setter(verdict["id"], decision)
    return True


def _record_gate_failure(store, contract_id, verdict, decision, channel,
                         exc) -> str:
    """Land a claimed-but-failed gate somewhere durable (round-5 fix
    spec H1; round-5 gate-core F1, security F1).

    The verdict's gate reads "failed" — claimable again with
    retry=True when the decision is presented afresh — the contract
    reads "gate_failed" (a named, non-terminal state), and a
    gate_failure observation on the contract names the decision, the
    reason, and the channel it arrived through. Every write is
    best-effort: the caller returns the error result either way, so a
    store that cannot record the failure still cannot make
    record_human_gate raise over a completion failure."""
    reason = str(exc) or type(exc).__name__
    try:
        store.append_observation(contract_id, {
            "kind": "gate_failure", "decision": decision,
            "reason": reason, "channel": channel,
        })
    except Exception:  # noqa: BLE001 - recording must not mask the failure
        pass
    setter = getattr(store, "set_verdict_human_gate", None)
    if callable(setter):
        try:
            setter(verdict["id"], "failed")
        except Exception:  # noqa: BLE001
            pass
    set_status = getattr(store, "set_contract_status", None)
    if callable(set_status):
        try:
            set_status(contract_id, "gate_failed")
        except Exception:  # noqa: BLE001
            pass
    return reason


#: The decided values a verdict's human_gate can hold once a claim
#: has landed (round-6 fix spec J1). "pending" is unclaimed and
#: "failed" is the recorded post-claim failure state — neither is a
#: decision.
_GATE_DECISION_VALUES = ("approved", "rejected", "waived")

#: Contract statuses that close a gate's life: a decided verdict over
#: one of these is a completed (or settled) gate, never an
#: interrupted completion. "gate_failed" is deliberately absent — it
#: is the retryable failure state, not a settlement.
_GATE_TERMINAL_STATUSES = frozenset({
    "completed", "rejected", "escalated", "refused",
})


class _FinalAnswerMismatch(Exception):
    """The final answer on record failed convergence validation
    (round-7 fix spec K1): raised out of completion so the gate
    presentation can refuse WITHOUT recording a failure state or
    writing anything further — the row is the operator's to
    resolve, and the gate stays unconsumed. (The refusal itself
    leaves one audit observation, kind ``gate_answer_refused`` —
    round-8 fix spec L4; never a failure state.)"""


#: The two interruption windows the detector names (round-7 fix spec
#: K2; round-7 security F1, gate-core F1): death after the claim with
#: the answer still owed, and death after the answer with the
#: terminal status still owed.
_INTERRUPTED_AFTER_CLAIM = (
    "interrupted completion (process death after claim)")
_INTERRUPTED_AFTER_ANSWER = (
    "interrupted completion (process death after the final answer)")


def _final_answer_row(store, contract_id):
    """The contract's final_answers row as a dict, or None (round-6
    fix spec J1; round-6 gate-core F3, security F1).

    The final answer is the completion's durable record — the same
    existence question chain._contract_completed asks — so completion
    idempotence and failure-state honesty key on it. The read itself
    is chain's (:func:`hybrid_critic_loop.chain._read_latest_final_answer`;
    round-11 fix spec, Unit B; round-11 attack F6): the LATEST row
    by rowid, the same row the chain governs on — round 10 left this
    reader on an unordered ``LIMIT 1`` (the FIRST row) while the
    chain read the latest, so on a multi-row book the gate converged
    on a row issuance never saw, and vice versa. A store with no
    readable surface answers None: such stores are doubles whose
    verdict rows are their whole world, and the pre-J1 behavior
    (completion proceeds) is correct there."""
    from . import chain as _chain
    _readable, row = _chain._read_latest_final_answer(store, contract_id)
    return row


def _recorded_gate_decision(store, contract_id, verdict):
    """The decision a retry must present (round-6 fix spec J1;
    round-6 security F2; round-7 fix spec K6; round-7 gate-core F4):
    the VERDICT's claimed value is the authority — the claim IS the
    decision record — and a gate_failure observation is consulted
    only when the verdict carries no decided value (the recorded
    failure state, where the verdict reads "failed" and the
    observation is the surviving record of what was claimed). The
    round-6 order let the failure LOG outrank the claim; where the
    two ever disagree, a retry could be refused into the opposite
    decision. None when no decision is on record (a first
    presentation, not a retry)."""
    gate = (verdict or {}).get("human_gate")
    if gate in _GATE_DECISION_VALUES:
        return gate
    contract = store.get_contract(contract_id) or {}
    for obs in reversed(contract.get("observations") or []):
        if isinstance(obs, dict) and obs.get("kind") == "gate_failure" \
                and obs.get("decision") in _GATE_DECISION_VALUES:
            return obs["decision"]
    return None


def _interrupted_completion(store, contract_id, verdict, contract):
    """The interruption reason when the gate's claim landed but its
    completion never finished, else None (round-6 fix spec J1;
    round-6 gate-core F1; round-7 fix spec K2; round-7 security F1,
    gate-core F1).

    Two durable windows, one detector. Before the answer: the
    verdict reads decided, the contract has no final answer and no
    terminal status — a process that died after the claim (host
    kill, OOM, power loss) leaves exactly this, and the H1 failure
    record was only ever written by the process that had to survive
    to write it. After the answer: the verdict reads decided and a
    final answer exists, but the contract still has no terminal
    status — death between the answer's commit and the status
    write, the one window J1's predicate originally excluded; the
    presentation converges it through the validated convergence
    instead of refusing forever on a gate issuance happily mints
    over. Never fires on a pending gate (no claim), an escalated
    verdict (escalated, and winnerless gates read pending), or a
    settled contract (terminal status)."""
    if (verdict or {}).get("human_gate") not in _GATE_DECISION_VALUES:
        return None
    if (contract or {}).get("status") in _GATE_TERMINAL_STATUSES:
        return None
    if _final_answer_row(store, contract_id) is None:
        return _INTERRUPTED_AFTER_CLAIM
    return _INTERRUPTED_AFTER_ANSWER


def _winner_snapshot(mgr, contract_id, winner):
    """The winner's verdict-time snapshot commit, resolved exactly
    as the merge path resolves it (round-5 fix spec H6; round-8 fix
    spec L1): the dedicated snapshot ref first — it survives the
    winner branch's deletion — then the winner's recorded payload
    hash. One helper, so the merge and the convergence validation
    can never resolve the snapshot differently."""
    if winner is None:
        return None
    snapshot = winner.get("payload_hash")
    ref_getter = getattr(mgr, "snapshot_ref_commit", None)
    if contract_id is not None and callable(ref_getter):
        try:
            ref_commit = ref_getter(contract_id, winner["id"])
        except Exception:  # noqa: BLE001 - the payload-hash path remains
            ref_commit = None
        if ref_commit:
            snapshot = ref_commit
    return snapshot


def _validate_final_answer(mgr, answer, decision, winner=None,
                           contract_id=None):
    """Why the final answer on record must not be converged on, or
    None when it checks out (round-7 fix spec K1; round-7 attack F1,
    security F3; round-8 fix spec L1; round-8 attack F1, security
    F1).

    Round 7 checked existence: the row's recorded decision is the
    decision being completed, and a "git:" commit names a real
    commit in the contract's repository. Round 8 showed existence
    is not identity — a plant carrying the winner's own snapshot
    hash (a real commit, a matching decision) converged and
    certified while main never received the work. So the row must
    now be the merge it claims to record:

    * the commit carries the "git:" form — a non-git string is a
      validation FAILURE, not an accepted-as-recorded form: no
      legitimate writer produces one (the sole production call
      site writes the post-merge head as "git:<sha>"), so the
      round-7 acceptance of non-git forms is withdrawn;
    * the winner's verdict-time snapshot — resolved exactly as
      the merge path resolves it — is an ANCESTOR of the answer's
      commit;
    * the answer's commit is REACHABLE from the repository's main
      head.

    A genuine row satisfies both identity legs by construction: a
    real completion merges the snapshot and writes the resulting
    head (the snapshot is its ancestor; it IS main's head at write
    time), and a fast-forward merge writes the snapshot itself,
    which satisfies both legs once merged. A plant can satisfy
    neither without actually merging the winner's work — at which
    point the completion it claims is real. A manager that cannot
    answer a primitive (older doubles) skips that leg; the real
    WorktreeManager answers all of them."""
    recorded = answer.get("human_gate")
    if recorded != decision:
        return (f"its recorded decision is {recorded!r}, not the "
                f"decision being completed ({decision!r})")
    raw = str(answer.get("commit") or "")
    if not raw.startswith("git:"):
        return (f"its commit {raw!r} does not carry the 'git:' form "
                f"— every completion this gate performs records the "
                f"merged head as 'git:<sha>', so a row in any other "
                f"form is not a completion of this gate")
    sha = raw[len("git:"):]
    is_commit = getattr(mgr, "is_commit", None)
    if callable(is_commit) and not is_commit(sha):
        return (f"its commit {raw!r} is not a commit in this "
                f"contract's repository")
    is_ancestor = getattr(mgr, "is_ancestor", None)
    if callable(is_ancestor):
        snapshot = _winner_snapshot(mgr, contract_id, winner)
        if snapshot and callable(is_commit) and is_commit(snapshot) \
                and not is_ancestor(snapshot, sha):
            return (f"its commit {raw!r} does not descend from the "
                    f"winner's verdict-time snapshot — the merge it "
                    f"claims to record never landed")
        head = None
        baseline = getattr(mgr, "baseline_hash", None)
        if callable(baseline):
            try:
                head = baseline()
            except Exception:  # noqa: BLE001 - an unreadable head skips the leg, like an unanswering manager
                head = None
        if head and not is_ancestor(sha, head):
            return (f"its commit {raw!r} is not reachable from the "
                    f"repository's main head — the merge it claims "
                    f"to record never landed")
    return None


def _append_gate_failure_once(store, contract_id, decision, channel,
                              reason):
    """Append the gate_failure observation unless an identical one
    (same decision, same reason) is already on the contract
    (round-7 fix spec K6; round-7 gate-core F5): a death inside the
    failure recording itself — the observation landed, the verdict
    and status writes never did — presents as the same interruption
    again, and one death must not become two records."""
    contract = store.get_contract(contract_id) or {}
    for obs in contract.get("observations") or []:
        if isinstance(obs, dict) and obs.get("kind") == "gate_failure" \
                and obs.get("decision") == decision \
                and obs.get("reason") == reason:
            return
    try:
        store.append_observation(contract_id, {
            "kind": "gate_failure", "decision": decision,
            "reason": reason, "channel": channel,
        })
    except Exception:  # noqa: BLE001 - recording must not mask the gate
        pass


#: The observation kind a convergence-validation refusal leaves
#: behind (round-8 fix spec L4; round-8 attack F4, security F2).
#: Deliberately NOT a gate_failure: the refusal is a live, designed
#: outcome, and its record is what lets a later presentation tell
#: the claim it is recovering from — a refused claim — apart from
#: a dead process's claim, which is the detector's crash signature
#: and otherwise indistinguishable from it.
_ANSWER_REFUSED_KIND = "gate_answer_refused"


def _append_answer_refusal_once(store, contract_id, verdict_id,
                                decision, problem):
    """Append the refusal's own audit observation unless an
    identical one (same verdict, decision, failed check) is already
    on the contract (round-8 fix spec L4): repeated presentations
    over the same planted row refuse identically, and one refusal
    must not become a pile of records. Best-effort, like every
    bookkeeping append — the refusal result stands either way."""
    contract = store.get_contract(contract_id) or {}
    for obs in contract.get("observations") or []:
        if isinstance(obs, dict) \
                and obs.get("kind") == _ANSWER_REFUSED_KIND \
                and obs.get("verdict") == verdict_id \
                and obs.get("decision") == decision \
                and obs.get("reason") == problem:
            return
    try:
        store.append_observation(contract_id, {
            "kind": _ANSWER_REFUSED_KIND, "verdict": verdict_id,
            "decision": decision, "reason": problem,
        })
    except Exception:  # noqa: BLE001 - recording must not mask the gate
        pass


def _answer_refusal_on_record(store, contract_id, verdict_id,
                              decision):
    """True when this verdict's standing claim was already refused
    over a final answer that failed validation (round-8 fix spec
    L4). The detector cannot tell a refused claim from a dead
    process's — same durable shape — but the refusal was THIS
    system's own live act, and it is on the book: a recovery from
    it must not be written up as a process death."""
    contract = store.get_contract(contract_id) or {}
    for obs in contract.get("observations") or []:
        if isinstance(obs, dict) \
                and obs.get("kind") == _ANSWER_REFUSED_KIND \
                and obs.get("verdict") == verdict_id \
                and obs.get("decision") == decision:
            return True
    return False


def _record_interruption(store, contract_id, verdict, decision,
                         channel, reason, record=True) -> bool:
    """Record a detected interrupted completion and flip the verdict
    to the failure state — conditionally (round-7 fix spec K3;
    round-7 gate-core F2, security F2).

    The observation lands first (deduped, spec K6), so the decision
    it names survives even if this process dies before the flip.
    The flip itself goes through the store's conditional setter
    when one exists: it lands only while the verdict still reads
    the decided value the detector observed. A False return means
    the flip lost — the gate moved under this presentation (a
    concurrent presenter claimed, recovered, or completed it) —
    and the caller re-reads the gate and answers per the fresh
    state instead of proceeding on a stale one. Stores without the
    conditional setter (older doubles) keep the round-6 behavior:
    the unconditional flip, and always True.

    With ``record=False`` the gate_failure observation is skipped
    but the conditional flip and the failure-shaped status still
    land (round-8 fix spec L4): recovering a claim a live refusal
    left standing must pass through the same claimable state the
    retry claim fires from, without writing the recovery up as a
    process death."""
    if record:
        _append_gate_failure_once(store, contract_id, decision,
                                  channel, reason)
    flip = getattr(store, "set_human_gate_if", None)
    if callable(flip):
        try:
            landed = bool(flip(verdict["id"],
                               verdict.get("human_gate"), "failed"))
        except Exception:  # noqa: BLE001 - a store that cannot answer leaves the legacy path in charge
            landed = True
        if not landed:
            return False
    else:
        setter = getattr(store, "set_verdict_human_gate", None)
        if callable(setter):
            try:
                setter(verdict["id"], "failed")
            except Exception:  # noqa: BLE001
                pass
    set_status = getattr(store, "set_contract_status", None)
    if callable(set_status):
        try:
            set_status(contract_id, "gate_failed")
        except Exception:  # noqa: BLE001
            pass
    return True


def _answer_for_moved_gate(store, contract_id, decision, _result):
    """Answer a presentation whose recovery flip lost the race
    (round-7 fix spec K3): re-read the gate and answer per its
    fresh state — a gate a concurrent presenter completed answers
    as completed, with its certificate; anything else is the
    standing no-pending-gate refusal. Never a fabricated failure:
    this presentation proceeds on nothing the winner's book
    contradicts."""
    try:
        verdict = store.latest_verdict(contract_id) or {}
        contract = store.get_contract(contract_id) or {}
    except Exception:  # noqa: BLE001 - unreadable is unanswerable
        verdict, contract = {}, {}
    if contract.get("status") == "completed" \
            and verdict.get("human_gate") == decision:
        result = dict(_result or {"contract_id": contract_id})
        result.update({
            "status": "completed", "human_gate": decision,
            "merge_source": "already_completed",
            "detail": ("the gate was completed by a concurrent "
                       "presenter while this presentation was being "
                       "decided; converged on its result"),
        })
        _attach_certificate(result, store, contract_id)
        return result
    return {"contract_id": contract_id, "status": "error",
            "detail": "no pending human gate for this contract"}


def _mismatch_result(store, contract_id, gate_state, _result,
                     mismatch, decision=None, verdict_id=None):
    """The refusal for a final answer that failed validation
    (round-7 fix spec K1): an error naming the failed check, no
    failure state recorded, no certificate — the gate is not
    consumed and the row is left for the operator, named in the
    detail.

    The refusal's one durable write is its own audit observation
    (round-8 fix spec L4) — kind ``gate_answer_refused``, never a
    gate_failure: the claim the refusal leaves standing is
    durably indistinguishable from a dead process's claim, and
    this record is how a later recovery tells the two apart
    instead of writing the recovery up as a process death."""
    if decision is not None and verdict_id is not None:
        _append_answer_refusal_once(
            store, contract_id, verdict_id, decision, str(mismatch))
    result = dict(_result or {"contract_id": contract_id})
    try:
        current = store.latest_verdict(contract_id) or {}
    except Exception:  # noqa: BLE001 - the entry state is the fallback
        current = {}
    result.update({
        "status": "error",
        "human_gate": current.get("human_gate", gate_state),
        "detail": (
            f"a final answer exists for this contract but does not "
            f"check out: {mismatch}; refusing to converge on it — "
            f"the completion was refused and the gate is not "
            f"consumed; resolve the row and present the decision "
            f"again"),
    })
    return result


def _granted_waiver_record(store, contract_id, verdict):
    """The verdict's waiver dict when it already reads granted, else
    None (round-7 fix spec K6). The passed verdict is a
    presentation-time snapshot; a fresh read backs it up, because a
    grant written by the attempt that died — or earlier in this
    call sequence — is the one a retry must preserve."""
    waiver = (verdict or {}).get("waiver") or {}
    if waiver.get("status") == "granted":
        return dict(waiver)
    try:
        fresh = store.latest_verdict(contract_id) or {}
    except Exception:  # noqa: BLE001 - the snapshot remains the fallback
        fresh = {}
    waiver = fresh.get("waiver") or {}
    if waiver.get("status") == "granted":
        return dict(waiver)
    return None


def _grant_waiver(store, contract_id, verdict, waiver, winner,
                  reason, channel):
    """The verdict's granted waiver record, written if not already
    granted (fix pass Wave 2.7, review H6; round-7 fix spec K6;
    round-7 gate-core F3).

    A retry — or a convergence — that finds the grant already on
    the verdict PRESERVES it: the record names the original grant's
    reason and granting channel, not the retry's defaults or its
    channel. Only a gate whose waiver is still merely offered gets
    the record built from this call and written."""
    granted = _granted_waiver_record(store, contract_id, verdict)
    if granted is not None:
        return granted
    record = dict(waiver)
    record.update({
        "gate": "measured", "status": "granted", "by": channel,
        "provenance": channel, "candidate": winner["id"],
        "reason": reason or "waived at the human gate",
    })
    setter = getattr(store, "set_verdict_waiver", None)
    if callable(setter):
        setter(verdict["id"], record)
    return record


def _is_duplicate_answer_signal(exc) -> bool:
    """True when a create_final_answer failure is the store's
    duplicate signal (round-7 fix spec K3): the store's atomic
    check-and-insert raises FinalAnswerExistsError; a uniqueness
    violation from any other store shape names itself in its type
    or message. Anything else is a real write failure and must take
    the failure path, not convergence."""
    if type(exc).__name__ == "FinalAnswerExistsError":
        return True
    message = str(exc).lower()
    return ("unique" in message or "duplicate" in message
            or "already exists" in message)


def _finish_from_existing_answer(store, contract_id, decision, reason,
                                 mgr, verdict, winner, waiver, result,
                                 channel):
    """Converge a completion on the final answer already on the book
    (round-6 fix spec J1; round-6 gate-core F3, security F1; round-7
    fix spec K1/K2/K3).

    The row is VALIDATED before it is trusted (round-7 attack F1,
    security F3; round-8 fix spec L1): its recorded decision must
    equal the decision being completed, and its commit must be the
    merge it claims to record — the winner's snapshot descends
    into it and it is reachable from main's head, the properties
    the normal merge path guarantees about the row IT writes. A
    row failing any check raises :class:`_FinalAnswerMismatch`
    before anything is written: a planted row (one raw INSERT, a
    free-text commit, a real commit that was never merged)
    completes nothing. A row passing them IS the completion's
    durable record: the merge is not repeated, no second answer
    is written, and the remaining steps (the waiver grant, the
    merge record, the terminal status, ref cleanup, the
    certificate) converge idempotently. The result reports the
    row's commit AS STORED (round-8 fix spec L5) — never stripped
    and re-prefixed, a construction that once re-issued a plant's
    free text as a "git:" claim."""
    answer = _final_answer_row(store, contract_id)
    if answer is None:
        # Callers check first; the row vanishing between the check
        # and here is not a convergence — surface a plain error for
        # the caller's failure handling.
        raise RuntimeError(
            "final answer vanished before convergence could read it")
    problem = _validate_final_answer(
        mgr, answer, decision, winner=winner, contract_id=contract_id)
    if problem is not None:
        raise _FinalAnswerMismatch(problem)
    waiver_record = None
    if decision == "waived":
        waiver_record = _grant_waiver(
            store, contract_id, verdict, waiver, winner, reason,
            channel)
    stored_commit = str(answer.get("commit") or "")
    merged_sha = stored_commit[len("git:"):] \
        if stored_commit.startswith("git:") else stored_commit
    commit = (winner or {}).get("payload_hash") or merged_sha
    result["merge_source"] = "already_completed"
    note = ("a final answer already exists for this contract; "
            "completion converged without re-merging or writing "
            "a second answer")
    result["detail"] = (
        f"{result['detail']}; {note}" if result.get("detail") else note
    )
    # The gate_completion observation is the durable merge record —
    # append it only if the first attempt died before writing it,
    # so a converged book carries exactly one. The dedupe is on
    # CONTENT, not kind alone (round-12 fix spec, Unit F; round-12
    # attack F3): a prior record suppresses the write only when it
    # records THIS merge — the same commit and the same decision.
    # A stray gate_completion with a different commit (or a
    # different decision) is not this merge's record, and must not
    # suppress the genuine one chain._contract_completed's
    # gate-record leg looks for.
    prior = store.get_contract(contract_id) or {}
    already_noted = any(
        isinstance(obs, dict) and obs.get("kind") == "gate_completion"
        and obs.get("commit") == merged_sha
        and obs.get("decision") == decision
        for obs in prior.get("observations") or [])
    if not already_noted:
        try:
            store.append_observation(contract_id, {
                "kind": "gate_completion", "decision": decision,
                "merge_source": "already_completed",
                "commit": merged_sha, "channel": channel,
            })
        except Exception:  # noqa: BLE001 - bookkeeping must not undo a merge
            pass
    store.set_contract_status(contract_id, "completed")
    if winner is not None:
        _delete_snapshot_ref(mgr, contract_id, winner["id"])
    result.update({"status": "completed", "commit": stored_commit,
                   "winner_commit": f"git:{commit}",
                   "waiver": waiver_record})
    _attach_certificate(result, store, contract_id)
    _update_gate_run_status(
        store,
        contract_id,
        "completed",
        f"Merged ({decision}) into main ({stored_commit})",
        certificate_stamp=(result.get("certificate") or {}).get("stamp"),
    )
    return result


def _update_gate_run_status(
    store,
    contract_id: str,
    phase: str,
    message: str,
    certificate_stamp: str | None = None,
) -> None:
    """Best-effort update to ``run_status.json`` when a human gate settles."""
    try:
        data_dir = progress_mod.store_data_dir(store)
        current = progress_mod.read_run_status(data_dir)
        if current.get("active") and current.get("contract_id") != contract_id:
            return
        snap = dict(current)
        snap["active"] = False
        snap["contract_id"] = contract_id
        snap["phase"] = phase
        snap["message"] = message
        snap["updated_at"] = progress_mod._utc_iso()
        if certificate_stamp:
            snap["certificate_stamp"] = certificate_stamp
        progress_mod.write_run_status(data_dir, snap)
    except Exception:  # noqa: BLE001 - telemetry must never affect gate completion
        pass


def _delete_snapshot_ref(mgr, contract_id, candidate_id):
    """Delete a candidate's snapshot ref at a terminal state
    (round-5 fix spec H6). Best-effort: the ref is an anchor, and
    cleanup must never break the outcome it follows."""
    deleter = getattr(mgr, "delete_snapshot_ref", None)
    if callable(deleter):
        try:
            deleter(contract_id, candidate_id)
        except Exception:  # noqa: BLE001
            pass


def _branch_for_worktree(wt) -> str:
    """The branch ``WorktreeManager.create`` made for a worktree path
    (``wt/<contract>/<slot>`` -> ``critic/<contract>/<slot>``)."""
    path = Path(wt)
    return f"critic/{path.parent.name}/{path.name}"


def _remove_candidate_worktrees(store, mgr, contract_id, *, delete_branches):
    """Remove every candidate worktree dir of a contract (fix spec G9;
    round-4 contest-core F4) — and, at terminal states, the branches
    behind them plus the baseline branch. Best-effort throughout:
    cleanup must never break the outcome it follows."""
    try:
        candidates = store.candidates_for(contract_id)
    except Exception:  # noqa: BLE001
        candidates = []
    for cand in candidates:
        wt = cand.get("worktree")
        if delete_branches:
            # Terminal states also drop the candidate's snapshot ref
            # (round-5 fix spec H6): the anchor's job ends with the
            # contract.
            _delete_snapshot_ref(mgr, contract_id, cand.get("id"))
        if not wt:
            continue
        try:
            mgr.remove(Path(wt))
        except Exception:  # noqa: BLE001
            pass
        if delete_branches:
            deleter = getattr(mgr, "delete_branch", None)
            if callable(deleter):
                try:
                    deleter(_branch_for_worktree(wt))
                except Exception:  # noqa: BLE001
                    pass
    if delete_branches:
        deleter = getattr(mgr, "delete_branch", None)
        if callable(deleter):
            try:
                deleter(f"critic/{contract_id}/baseline")
            except Exception:  # noqa: BLE001
                pass


class _Run:
    def __init__(self, store, contract, repo_path, executors, human_decider,
                 channel="cli"):
        self.store = store
        self.contract = contract
        self.contract_id = contract["id"]
        self.repo_path = Path(repo_path)
        self.executors = executors
        self.human_decider = human_decider
        # The channel an in-run human decision arrives through (fix
        # spec G1): the recorded gate decision names it, never a
        # hardcoded "human" (round-4 security F7).
        self.channel = channel
        self.mgr = WorktreeManager(self.repo_path)
        # Run-claim bookkeeping (round-5 fix spec H6): whether THIS
        # run holds the worktree manager's claim file.
        self._run_claim_held = False
        self._claim_note: str | None = None
        # Candidate id -> the GateError text that eliminated it (fix
        # spec G7 tail): retry notes and the ledger carry the reason.
        self.gate_errors: dict[str, str] = {}
        self.candidate_trees: dict[str, str] = {}
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
        self.progress: progress_mod.RunProgressTracker | None = None
        self._preflight_skips: dict[str, str] = {}

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
                access = "hermes" if getattr(ex, "parallel_safe", False) else "cli"
                store.upsert_expert(eid, ex.family, access=access, cost_tier=tier)
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
            hint = ("its human gate is pending — the operator must decide it using "
                    "`/critic gate <id> approve|reject|waive` or `hermes critic gate`, "
                    "not a re-run")
        elif status == "completed":
            hint = "its winner is merged and its verdict recorded"
        elif status == "running":
            hint = ("another run of this contract is in progress, or was "
                    "interrupted mid-run; the contract id is spent either way")
        elif status == "escalated":
            hint = "it escalated to the operator; open a new contract id for another attempt"
        elif status == "rejected":
            verdict = self.store.latest_verdict(self.contract_id) or {}
            follow_up = f"{self.contract_id}_rej{verdict.get('attempt', 1)}"
            hint = f"the rejection opened {follow_up}; run that contract"
        elif status == "gate_failed":
            # A gate whose completion failed after its claim is
            # retried by presenting the decision again — never by a
            # fresh contract id (round-6 fix spec J1; round-6
            # gate-core F5).
            hint = ("its human gate was claimed but its completion failed "
                    "(contract status 'gate_failed'); present the recorded "
                    "decision again using "
                    "`/critic gate <id> approve|reject|waive` or "
                    "`hermes critic gate` to retry completion — do not "
                    "open a new contract id")
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

    def _planned_worktree(self, slot_name):
        """Where a candidate's worktree WILL live, without creating it:
        the manager's own ``path_for`` when it has one, its ``root``
        layout otherwise, and — for in-memory manager doubles whose
        ``create`` is side-effect-free naming — ``create`` itself."""
        path_for = getattr(self.mgr, "path_for", None)
        if callable(path_for):
            return path_for(self.contract_id, slot_name)
        root = getattr(self.mgr, "root", None)
        if root is not None:
            return Path(root) / self.contract_id / slot_name
        return self.mgr.create(self.contract_id, slot_name)

    def _prepare_dispatch(self, expert_id, slot, attempt):
        """Main-thread setup for one candidate seat: availability check, worktree, DB row."""
        store = self.store
        executor = self.executors[expert_id]
        slot_name = f"{slot}{attempt if attempt > 1 else ''}"
        # Seat availability is checked BEFORE the worktree exists (fix
        # spec G9; round-4 contest-core F4): an unavailable seat gets a
        # candidate row and a ledger row, never a checkout it will not
        # use (before, it leaked one worktree per seat per attempt).
        if expert_id in self._preflight_skips:
            ok, why = False, self._preflight_skips[expert_id]
        else:
            ok, why = executor.available()
        if ok:
            wt = self.mgr.create(self.contract_id, slot_name)
        else:
            wt = self._planned_worktree(slot_name)
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
        return cand_id, executor, wt, ok, why

    def _finalize_dispatch(self, expert_id, executor, cand_id, wt, gen):
        """Main-thread persistence for one finished candidate generation."""
        store = self.store
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
            # Tightening spec T2: the dispatch path attributes the row
            # to the candidate's recorded provenance — a candidate row
            # always carries one (the adapter's, required by the gate),
            # so the "executor-reported" fallback is belt and braces.
            provenance=(store.get_candidate(cand_id) or {}).get("provenance")
            or "executor-reported",
        )
        if gen.status == "completed":
            diff = self.mgr.diff(wt)
            store.update_candidate(cand_id, payload_hash=sha256_text(diff))
            tree = getattr(self.mgr, "index_tree", None)
            if callable(tree):
                self.candidate_trees[cand_id] = tree(wt)
        elif gen.status == "refused":
            store.record_refusal(expert_id)
        if self.progress is not None:
            self.progress.seat_finished(
                expert_id,
                gen.status,
                duration_ms=gen.duration_ms or 0,
                model=getattr(executor, "last_model", None),
            )
        return store.get_candidate(cand_id), gen

    def _dispatch(self, expert_id, slot, attempt, context):
        """Generate one candidate. Returns (candidate_dict, gen_result)."""
        cand_id, executor, wt, ok, why = self._prepare_dispatch(expert_id, slot, attempt)
        if not ok:
            from .executors import GenResult

            gen = GenResult(
                status="unavailable",
                log=f"executor {expert_id} unavailable: {why}",
                tokens_in=None, tokens_out=None,
                cost_usd=None, request_ids=[], duration_ms=0, detail=why,
            )
        else:
            if self.progress is not None:
                self.progress.seat_started(expert_id, attempt)
            gen = executor.generate(wt, self.contract, context)
        return self._finalize_dispatch(expert_id, executor, cand_id, wt, gen)

    def _dispatch_wave(self, contestants, attempt, context):
        """Dispatch a generation wave, parallelizing ``generate()`` only when all seats opt in."""
        active_seats = [
            (i, eid) for i, eid in enumerate(contestants)
            if eid in self.executors
        ]
        all_parallel = (
            len(active_seats) > 1
            and all(
                getattr(self.executors[eid], "parallel_safe", False) is True
                for _, eid in active_seats
            )
        )
        if not all_parallel:
            wave = []
            for i, eid in active_seats:
                cand, gen = self._dispatch(eid, SLOTS[i], attempt, context)
                wave.append((cand, gen))
            return wave

        from .executors import GenResult

        prepared = []
        runnable = []
        for idx, (slot_idx, eid) in enumerate(active_seats):
            cand_id, executor, wt, ok, why = self._prepare_dispatch(
                eid, SLOTS[slot_idx], attempt
            )
            prepared.append((eid, executor, cand_id, wt, ok, why))
            if ok:
                runnable.append((idx, eid, executor, wt))

        gen_results: dict[int, object] = {}
        first_exc: BaseException | None = None
        if runnable:
            def _run_seat(eid, executor, wt):
                if self.progress is not None:
                    self.progress.seat_started(eid, attempt)
                res = executor.generate(wt, self.contract, context)
                if self.progress is not None:
                    self.progress.seat_finished(
                        eid,
                        res.status,
                        duration_ms=res.duration_ms or 0,
                        model=getattr(executor, "last_model", None),
                    )
                return res

            with ThreadPoolExecutor(max_workers=min(8, len(runnable))) as pool:
                fut_map = {}
                for idx, eid, executor, wt in runnable:
                    ctx_copy = contextvars.copy_context()
                    fut = pool.submit(ctx_copy.run, _run_seat, eid, executor, wt)
                    fut_map[fut] = idx
                for fut in as_completed(fut_map):
                    idx = fut_map[fut]
                    try:
                        gen_results[idx] = fut.result()
                    except BaseException as exc:  # noqa: BLE001 - re-raise in seat order
                        if first_exc is None:
                            first_exc = exc

        if first_exc is not None:
            raise first_exc

        wave = []
        for idx, (eid, executor, cand_id, wt, ok, why) in enumerate(prepared):
            if not ok:
                gen = GenResult(
                    status="unavailable",
                    log=f"executor {eid} unavailable: {why}",
                    tokens_in=None, tokens_out=None,
                    cost_usd=None, request_ids=[], duration_ms=0, detail=why,
                )
            else:
                gen = gen_results[idx]
            cand, gen = self._finalize_dispatch(eid, executor, cand_id, wt, gen)
            wave.append((cand, gen))
        return wave

    def _cleanup_attempt_worktrees(self, wave):
        """A failed attempt's worktree dirs go away when the run
        retries (fix spec G9; round-4 contest-core F4): the next
        attempt dispatches fresh slot dirs, and the old checkouts
        would otherwise leak one per seat per attempt. Branches stay
        until the contract reaches a terminal state."""
        for cand, _gen in wave:
            wt = cand.get("worktree")
            if not wt:
                continue
            try:
                self.mgr.remove(Path(wt))
            except Exception:  # noqa: BLE001 - cleanup must not break a retry
                pass

    def _release_run_claim(self):
        """Release this run's claim file, if it took one (round-5
        fix spec H6): called on the baseline-refusal exit and from
        run()'s finally — every exit, so a finished or failed run
        never leaves a claim behind for a later run to mistake for
        a live one."""
        if not self._run_claim_held:
            return
        self._run_claim_held = False
        release_run = getattr(self.mgr, "release_run", None)
        if callable(release_run):
            try:
                release_run(self.contract_id)
            except Exception:  # noqa: BLE001 - release must not break an exit
                pass

    # -- main ----------------------------------------------------------
    def run(self) -> dict:
        try:
            result = self._run()
        except Exception as exc:
            if self.progress is not None:
                self.progress.finish("error", f"Run aborted with error: {exc}")
            # An error exit removes the run's worktree dirs (fix spec
            # G9; round-4 contest-core F4) but keeps the branches: the
            # record of what was attempted survives for inspection, and
            # the one-run guard answers already_run on any re-run.
            _remove_candidate_worktrees(
                self.store, self.mgr, self.contract_id, delete_branches=False)
            raise
        finally:
            # The run claim is released on EVERY exit (round-5 fix
            # spec H6; round-5 gate-core F4): the file guards a live
            # run, not a finished one — from any recorded state
            # onward the store's own guards carry the one-run
            # doctrine, and a leftover file would brick a contract
            # the store itself considers un-run.
            self._release_run_claim()
        if self._claim_note:
            result["claim_note"] = self._claim_note
        return result

    def _run(self) -> dict:
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
        if status in FINISHED_STATUSES or status == "running" \
                or store.candidates_for(self.contract_id):
            return self._already_run(status)

        # Claim the run BEFORE any worktree is created (fix spec G9;
        # round-4 contest-core F7): the worktree manager's claim file
        # is atomic across processes, and the contract row flips to
        # "running" so any second run — sequential or racing — answers
        # already_run instead of colliding on worktree/branch names
        # (which carry the attempt: a racing run never gets far enough
        # to mint its own). The claim is released on every exit (see
        # run()). Never break an existing claim automatically: its owner
        # may be alive but not yet have changed the database status.
        claim_run = getattr(self.mgr, "claim_run", None)
        if callable(claim_run):
            if claim_run(self.contract_id):
                self._run_claim_held = True
            else:
                result = self._already_run("running")
                result["detail"] += (
                    "; an existing run claim must be inspected by the operator "
                    "before an abandoned claim is removed"
                )
                return result
        claimed_status = False
        set_status = getattr(store, "set_contract_status", None)
        if callable(set_status):
            set_status(self.contract_id, "running")
            claimed_status = True

        # Baseline rule (invariant 3): in fix mode, the acceptance suite must
        # have FAILED on the unmodified baseline before any candidate is
        # dispatched; in improve mode, the acceptance suite must PASS on the
        # baseline and every surviving candidate must produce a non-empty diff.
        mode = self.contract.get("mode", "fix")
        expected_baseline = "pass_improve" if mode == "improve" else "fail"
        if contract_row.get("acceptance_baseline_run") != expected_baseline:
            proceed, detail = verify_baseline(store, self.contract, self.repo_path, self.mgr)
            if not proceed:
                store.set_contract_status(self.contract_id, "refused")
                refusal_note = (
                    "baseline acceptance failed in improve mode; fix broken tests first or use mode='fix'"
                    if mode == "improve"
                    else "acceptance passed on baseline; contract refused as vacuous"
                )
                refusal_detail = (
                    "baseline acceptance tests failed in improve mode; improve mode requires a passing baseline"
                    if mode == "improve"
                    else "acceptance tests pass on the baseline; the contract is vacuous and was refused"
                )
                store.add_ledger(
                    target_id=self.target_id, contract_id=self.contract_id, candidate_id=None,
                    stage=self.contract["stage"], expert_id="queue", tokens_in=None,
                    tokens_out=None, cost_usd=None, outcome="escalated",
                    note=refusal_note,
                    provenance="internal",
                )
                progress_mod.write_run_status(
                    progress_mod.store_data_dir(store),
                    {
                        "active": False,
                        "phase": "refused_baseline",
                        "contract_id": self.contract_id,
                        "mode": mode,
                        "message": refusal_detail,
                        "seats": {},
                        "bracket": {"rounds": [], "ladder": []},
                        "updated_at": progress_mod._utc_iso(),
                    },
                )
                # A refused contract is re-verifiable on its next run
                # (Wave 2.7): the run claim goes back so that run can
                # take it. (run()'s finally releases it on every exit
                # regardless; this keeps the refusal's own sequencing.)
                self._release_run_claim()
                return {
                    "contract_id": self.contract_id, "status": "refused_baseline",
                    "detail": refusal_detail,
                    "baseline": detail, "events": self.events,
                }
            if status == "refused":
                if not claimed_status:
                    # With a run claim the row already reads "running"
                    # and this run owns it; without one, re-open as before.
                    store.set_contract_status(self.contract_id, "open")
                reopen_note = (
                    "baseline re-verified on re-run: acceptance now passes on the "
                    "baseline (improve mode); contract reopened"
                    if mode == "improve"
                    else "baseline re-verified on re-run: acceptance now fails on the "
                         "baseline; contract reopened"
                )
                store.add_ledger(
                    target_id=self.target_id, contract_id=self.contract_id, candidate_id=None,
                    stage=self.contract["stage"], expert_id="queue", tokens_in=None,
                    tokens_out=None, cost_usd=None, outcome="observed",
                    note=reopen_note,
                    provenance="internal",
                )

        category = router_mod.category_of(self.contract["domain"])
        target = store.get_target(self.target_id) or {}
        if target.get("budget"):
            return self._escalate(
                int(self.contract.get("attempt", 1)),
                "budgeted run refused: provider usage and cost cannot be bounded before dispatch",
                {"measurements": [], "hard_failures": [], "judged": None,
                 "candidate_experts": {}, "winner_expert": None, "loser_experts": [],
                 "escalation_reason": None},
            )
        route = router_mod.route_for(store, category)
        # Panel composition (v0.3): route-first order — primary,
        # compete_with, then the fallback order, then any remaining
        # executor ids. The panel is every seat the operator handed to
        # this run; each generates exactly one candidate.
        ordered = [route.get("primary"), route.get("compete_with")]
        ordered += list(route.get("fallback", []))
        ordered += sorted(self.executors)
        contestants = [c for c in dict.fromkeys(ordered) if c and c in self.executors]
        self.progress = progress_mod.RunProgressTracker(
            store, self.contract, self.repo_path, contestants, self.executors
        )
        self.progress.emit(
            "starting",
            f"Fielding {len(contestants)} panel seats ({mode} mode) for {self.contract_id}",
        )

        retry_notes = ""
        judged_retries = 0
        attempt = int(self.contract.get("attempt", 1))
        last_gate_details: dict = {"measurements": [], "hard_failures": [], "judged": None,
                                   "candidate_experts": {}, "winner_expert": None,
                                   "loser_experts": [], "escalation_reason": None}

        # Slot guard BEFORE the wave (fix spec G9; round-3 contest-core
        # F8): the check used to sit after dispatch, where it could
        # never fire — the 27th contestant died on SLOTS[i] with an
        # IndexError mid-wave and burned the contract id.
        if len(contestants) > len(SLOTS):
            return self._escalate(
                attempt,
                f"panel field {len(contestants)} exceeds the slot capacity {len(SLOTS)}",
                last_gate_details,
            )

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

            # Optional pool-deduped usage pre-flight when contract requests min_usage_fraction.
            min_uf = self.contract.get("min_usage_fraction")
            if min_uf is not None:
                try:
                    from . import usage_feed as usage_feed_mod

                    self._preflight_skips = usage_feed_mod.evaluate_panel_preflight(
                        self.executors,
                        contestants,
                        float(min_uf),
                        data_dir=progress_mod.store_data_dir(store),
                    )
                except Exception:  # noqa: BLE001 - pre-flight inspection must never crash a run
                    self._preflight_skips = {}
            else:
                self._preflight_skips = {}

            # Generation wave: identical inputs, isolated worktrees.
            context = self._context_for(retry_notes)
            wave = self._dispatch_wave(contestants, attempt, context)
            for cand, _gen in wave:
                last_gate_details["candidate_experts"][cand["id"]] = cand["expert_id"]

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

            # Gates, in rank order, per candidate.
            gate_results: dict[str, gates_mod.CandidateGateResult] = {}
            diffs: dict[str, str] = {}
            for cand, _gen in generated:
                diffs[cand["id"]] = self.mgr.diff(cand["worktree"])
                if (
                    mode == "improve"
                    and gates_mod.check_provenance(cand)
                    and not (diffs[cand["id"]] or "").strip()
                ):
                    gr = gates_mod.CandidateGateResult(
                        candidate_id=cand["id"],
                        provenance_ok=True,
                        eliminated_at="hard",
                        hard=[
                            gates_mod.HardResult(
                                cmd="git diff (non-empty change required in improve mode)",
                                expected_exit=0,
                                exit_code=1,
                                passed=False,
                                stdout_tail="",
                                stderr_tail="candidate made zero file changes in improve mode",
                            )
                        ],
                        measured=[],
                        hard_pass=False,
                        measured_pass=None,
                    )
                else:
                    gr = self._evaluate_candidate(cand)
                gate_results[cand["id"]] = gr
                if gr.eliminated_at is not None:
                    self._mark_eliminated(
                        cand,
                        _ELIMINATED_KINDS.get(gr.eliminated_at,
                                              gr.eliminated_at))
                if self.progress is not None:
                    self.progress.gate_result(cand["expert_id"], gr.eliminated_at)
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
                    outcome = self._run_judged(
                        judged_item, survivors, gate_results, diffs,
                        seed=attempt)
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
                    for c in survivors:
                        if not judged_map.get(c["id"], {}).get("pass"):
                            self._mark_eliminated(c, "judged")
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

            # No winner. A candidate that passed hard and failed ONLY
            # measured used to be offered to the human gate here as a
            # waiver candidate; the owner ruled otherwise (owner
            # decisions 2026-11-09, Item 2(a) = B): a measured-only
            # regression is NOT a valid contract — it fails like any
            # other gate failure, through the retry machinery below,
            # and no waiver is offered for it. The waiver machinery
            # itself (an offered waiver carried on a verdict, the
            # explicit "waived" decision at the gate) is unchanged;
            # the run simply never produces the offer for a measured
            # regression any more.

            # Retry logic.
            all_judged_below = bool(judged_items) and all(
                gate_results[c["id"]].eliminated_at is None for c, _ in generated
            ) and generated
            if (all_judged_below and judged_retries < JUDGED_RETRIES
                    and attempt < MAX_ATTEMPTS):
                judged_retries += 1
                retry_notes = self._failure_notes(attempt, generated, gate_results, judged_map)
                attempt += 1
                self._cleanup_attempt_worktrees(wave)
                continue
            if all_judged_below and judged_retries < JUDGED_RETRIES:
                # The judged retry remains but the attempt ceiling
                # does not: MAX_ATTEMPTS binds every retry path,
                # including this one (round-12 fix spec, Unit L;
                # round-12 gatecore G1). A contract STARTING at
                # attempt >= MAX_ATTEMPTS used to dispatch one more
                # full generation + judging wave through the judged
                # branch while the generic hard/measured branch
                # starting at the same attempt dispatched none — the
                # ceiling is a property of the contract's attempt
                # budget, not of the failure class, so the judged
                # branch escalates on the same retry-cap terms.
                self._emit(
                    "retry_cap", f"retry cap {MAX_ATTEMPTS} reached on attempt {attempt}"
                )
                return self._escalate(
                    attempt, f"retry cap {MAX_ATTEMPTS} reached",
                    last_gate_details, judged=judged_map)
            if all_judged_below:
                # The legacy judged retry is spent: escalate, as the
                # queue spec in the module docstring says ("a legacy
                # rubric retries once, then escalates"). Falling
                # through to the generic retry below silently granted
                # a pure judged failure the full hard/measured attempt
                # cap — a second full generation + judging wave — and
                # made JUDGED_RETRIES dead weight: it never caused an
                # earlier escalation (round-11 gatecore F2; the CODE
                # was corrected to the docstring, not the reverse —
                # the docstring is the spec text and the constant
                # exists for exactly this branch).
                self._emit(
                    "retry_cap",
                    f"legacy judged retry cap {JUDGED_RETRIES} reached "
                    f"on attempt {attempt}",
                )
                return self._escalate(
                    attempt,
                    f"legacy judged gate below the minimum after "
                    f"{JUDGED_RETRIES} "
                    f"{'retry' if JUDGED_RETRIES == 1 else 'retries'}",
                    last_gate_details, judged=judged_map,
                )
            if attempt < MAX_ATTEMPTS:
                retry_notes = self._failure_notes(attempt, generated, gate_results, judged_map)
                attempt += 1
                self._cleanup_attempt_worktrees(wave)
                continue
            self._emit(
                "retry_cap", f"retry cap {MAX_ATTEMPTS} reached on attempt {attempt}"
            )
            return self._escalate(attempt, f"retry cap {MAX_ATTEMPTS} reached", last_gate_details)

    # -- gates -----------------------------------------------------------
    def _mark_eliminated(self, cand, kind):
        """Persist WHY a candidate is out on its own row (round-5
        fix spec H5; round-5 gate-core F5).

        The kind rides in the candidate's evidence under
        ``eliminated_kind`` — "hard" | "measured" | "gate_error" |
        "judged" | "bracket_cut" — so calibration's decidability
        read can tell a gate-killed candidate from a bracket-cut
        passer after the fact. Before, the only record of a gate
        elimination was the gate-result object, which dies with the
        run, and the candidate row carried nothing at all: a
        candidate the dissent re-run anchored out kept its matrix
        pass and read forever as a bracket-cut passer. Best-effort:
        bookkeeping must not break a run."""
        if cand is None or not kind:
            return
        cid = cand["id"] if isinstance(cand, dict) else cand
        try:
            row = self.store.get_candidate(cid) or {}
        except Exception:  # noqa: BLE001
            row = cand if isinstance(cand, dict) else {}
        evidence = dict(row.get("evidence") or {})
        evidence["eliminated_kind"] = kind
        try:
            # The elimination record is loop-only (round-12 fix
            # spec, Unit F): the real store refuses evidence writes
            # through update_candidate that set or clear the kind,
            # so the marking goes through its dedicated channel.
            # Stores without that channel (older doubles) keep the
            # original evidence write.
            setter = getattr(
                self.store, "set_candidate_eliminated_kind", None)
            if callable(setter):
                setter(cid, kind)
            else:
                self.store.update_candidate(cid, evidence=evidence)
        except Exception:  # noqa: BLE001
            pass
        if isinstance(cand, dict):
            merged = dict(cand.get("evidence") or {})
            merged["eliminated_kind"] = kind
            cand["evidence"] = merged
            cand["eliminated_kind"] = kind

    def _evaluate_candidate(self, cand):
        """Run one candidate's gates. A ``GateError`` — a sensor that
        produced unparseable output, a metric of an unusable type —
        eliminates THAT candidate with the reason recorded (fix spec
        G7 tail; round-4 contest-core F3): the baseline path already
        treats a broken sensor as data, and a candidate's broken gate
        must never propagate out of ``run_contract`` and strand the
        contract at "open" with its id spent."""
        try:
            expected = cand.get("payload_hash")
            if isinstance(expected, str) and expected.startswith("sha256:"):
                if sha256_text(self.mgr.diff(cand["worktree"])) != expected:
                    raise GateError("candidate changed before gate evaluation")
                if cand["id"] in self.candidate_trees and self.mgr.index_tree(cand["worktree"]) != self.candidate_trees[cand["id"]]:
                    raise GateError("candidate tree changed before gate evaluation")
            result = gates_mod.evaluate_candidate(
                self.store, self.contract, cand, Path(cand["worktree"])
            )
            if isinstance(expected, str) and expected.startswith("sha256:"):
                if sha256_text(self.mgr.diff(cand["worktree"])) != expected:
                    raise GateError("gate command changed the candidate worktree")
                if cand["id"] in self.candidate_trees and self.mgr.index_tree(cand["worktree"]) != self.candidate_trees[cand["id"]]:
                    raise GateError("gate command changed the candidate tree")
            return result
        except GateError as exc:
            reason = str(exc)
            self.gate_errors[cand["id"]] = reason
            try:
                self.store.add_ledger(
                    target_id=self.target_id, contract_id=self.contract_id,
                    candidate_id=cand["id"], stage=self.contract["stage"],
                    expert_id=cand["expert_id"], tokens_in=None,
                    tokens_out=None, cost_usd=None, outcome="observed",
                    note=f"gate error eliminated candidate {cand['id']}: {reason}",
                    provenance="internal",
                )
            except Exception:  # noqa: BLE001 - bookkeeping must not break a run
                pass
            return gates_mod.CandidateGateResult(
                candidate_id=cand["id"], provenance_ok=True,
                eliminated_at="gate_error", hard=[], measured=[],
                hard_pass=False, measured_pass=None,
            )

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
            provenance="internal",
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
            if self.progress is not None:
                self.progress.matrix_round_start(round_no, len(field), len(peer_executors))
            outcome = matrix_mod.run_matrix(
                store, self.contract, field, gate_results, diffs,
                peer_executors,
                min_score=float(judged_item.get("min", 0)),
                dissent_threshold=float(judged_item.get("dissent_threshold", 3.0)),
                spread_threshold=float(judged_item.get("spread_threshold", 2.0)),
                calibrations=calibrations,
                seed=attempt,
                round_no=round_no,
                on_judge_start=(
                    (lambda jid, r=round_no: self.progress.matrix_judge_started(r, jid))
                    if self.progress is not None
                    else None
                ),
                on_judge_done=(
                    (lambda jid, r=round_no: self.progress.matrix_judge_done(r, jid))
                    if self.progress is not None
                    else None
                ),
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
                    new_gr = self._evaluate_candidate(cand)
                    gate_results[cid] = new_gr
                    if new_gr.eliminated_at is not None:
                        # A dissent re-run elimination is a gate
                        # elimination with a kind, even when it
                        # anchors the candidate out of the bracket
                        # (round-5 gate-core F5). The mark on the
                        # candidate row is also what _write_lesson's
                        # gate_passed derivation reads, so a re-run
                        # gate_error/provenance elimination — which
                        # refreshes neither hard_failures nor any
                        # measurement row below — still counts as a
                        # deciding-gate failure there (round-11 Unit A).
                        self._mark_eliminated(
                            cand,
                            _ELIMINATED_KINDS.get(new_gr.eliminated_at,
                                                  new_gr.eliminated_at))
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
                        provenance="internal",
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
            # Persist the round's two elimination classes distinctly
            # (round-5 fix spec H5): the panel's failers are judged
            # eliminations; the ladder's cut are bracket cuts —
            # passers a stronger passer outscored, which calibration
            # must NOT read as failures.
            for cand in failers:
                self._mark_eliminated(cand, "judged")
            for cand in cut["eliminated"]:
                self._mark_eliminated(cand, "bracket_cut")
            cut_out = cut["eliminated"] + failers
            if self.progress is not None:
                scores_by_expert = {
                    by_id[cid]["expert_id"]: entry.get("median_mean")
                    for cid, entry in outcome.per_candidate.items()
                    if cid in by_id and isinstance(entry, dict)
                }
                self.progress.bracket_step(
                    round_no,
                    field_in,
                    len(advanced),
                    scores_by_expert,
                    [c["expert_id"] for c in cut_out],
                )
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
                provenance="internal",
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
        # escalate. A measured-only failure in the field used to fall
        # back to a waiver offer here; the owner ruled that a measured
        # regression is not a valid contract (owner decisions
        # 2026-11-09, Item 2(a) = B), so there is no waiver fallback —
        # the escalation is the failure record.
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
            if pid in self._preflight_skips:
                skipped.append((pid, f"unavailable: {self._preflight_skips[pid]}"))
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
    def _run_judged(self, judged_item, survivors, gate_results, diffs,
                    seed=0):
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
        # The gate shuffles under the ATTEMPT seed, exactly as the
        # matrix path does (round-12 fix spec, Unit L; round-12
        # gatecore G2): a constant seed made every attempt present
        # candidates to each judge in the identical order, so the
        # persisted position_shown evidence could never vary across
        # a retry — the retry re-judged the same presentation.
        return judges_mod.run_judged_gate(
            store, self.contract, judged_item, survivors, gate_results, diffs,
            judge_executors, panel, seed=seed,
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
        def _duration(candidate):
            # A recorded None is no measurement (round-12 fix spec,
            # Unit L; round-12 gatecore G3): it sorts slowest, like
            # a missing entry — never a TypeError against a real
            # duration. Shape hardening: no in-tree executor records
            # None today (_dispatch stores duration_ms uncoerced).
            recorded = self.durations.get(candidate["id"])
            return float("inf") if recorded is None else recorded

        by_time = sorted(survivors, key=_duration)
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
            elif gr.eliminated_at == "gate_error":
                lines.append(
                    f"- candidate {cand['id']} gate error: "
                    f"{self.gate_errors.get(cand['id'], 'gate execution failed')}"
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
        try:
            self._snapshot_winner(winner)
        except Exception as exc:
            return self._escalate(attempt, f"winner snapshot failed: {exc}", last_gate_details)
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
        # The winner carries no elimination kind, whatever its gate
        # history (round-5 fix spec H5): a measured-failed candidate
        # waived through to the verdict was marked at gate-evaluation
        # time, but it is not eliminated — its failure lives on the
        # verdict's waiver record above, and calibration must not
        # read the run's winner as a gate negative.
        try:
            wrow = store.get_candidate(winner["id"]) or {}
            wev = dict(wrow.get("evidence") or {})
            if "eliminated_kind" in wev:
                # Clearing the winner's kind is the loop's own
                # write (round-12 fix spec, Unit F): through the
                # dedicated channel where the store has one —
                # update_candidate refuses to clear the key.
                setter = getattr(
                    store, "set_candidate_eliminated_kind", None)
                if callable(setter):
                    setter(winner["id"], None)
                else:
                    del wev["eliminated_kind"]
                    store.update_candidate(winner["id"], evidence=wev)
        except Exception:  # noqa: BLE001 - bookkeeping must not break a verdict
            pass
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
        # No certificate here (fix spec G1; round-4 regression F1): the
        # gate has not decided — a pending, refused, or rejected result
        # carries no stamp and no token. record_human_gate attaches
        # the certificate only once the decision completes.
        # The gate is pending from here until record_human_gate completes
        # it — whether the decider answers now or an operator does later;
        # a refused decision leaves the contract at its gate (Wave 2.7).
        store.set_contract_status(self.contract_id, "awaiting_human_gate")
        if self.progress is not None:
            self.progress.finish(
                "awaiting_human_gate",
                f"Winner {winner['expert_id']} ({winner['family']}) awaits human gate approval",
                winner_expert=winner["expert_id"],
                winner_family=winner["family"],
            )
        if self.human_decider is not None:
            decision = self.human_decider(
                {"winner": winner["id"], "waiver_offered": is_waiver, "verdict_id": verdict_id}
            )
            return self._apply_human_gate(verdict_id, decision, result)
        return result

    def _snapshot_winner(self, winner):
        """Commit the winner's worktree AT VERDICT TIME and re-point
        the candidate's payload hash at that commit (fix spec G1;
        round-4 contest-core F1).

        The snapshot must match the candidate tree presented to the
        gates and judges. Failure aborts the verdict rather than falling
        back to a mutable worktree."""
        expected = winner.get("payload_hash")
        if isinstance(expected, str) and expected.startswith("sha256:"):
            if sha256_text(self.mgr.diff(winner["worktree"])) != expected:
                raise RuntimeError("winner changed after gate evaluation")
        commit = self.mgr.commit_all(
            Path(winner["worktree"]),
            f"critic: {self.contract_id} verdict snapshot {winner['id']}",
        )
        if not commit:
            raise RuntimeError("winner snapshot was not created")
        if winner["id"] in self.candidate_trees:
            if self.mgr.commit_tree(commit) != self.candidate_trees[winner["id"]]:
                raise RuntimeError("snapshot tree differs from the gated candidate")
        snapshot_diff = getattr(self.mgr, "snapshot_diff", None)
        if callable(snapshot_diff) and isinstance(expected, str) and expected.startswith("sha256:"):
            if sha256_text(snapshot_diff(winner["baseline_hash"], commit)) != expected:
                raise RuntimeError("snapshot differs from the gated candidate")
        setter = getattr(self.store, "set_candidate_payload_hash", None)
        if callable(setter):
            setter(winner["id"], commit)
        else:
            self.store.update_candidate(winner["id"], payload_hash=commit)
        # Pin the snapshot under its own ref as well (round-5 fix
        # spec H6; round-5 gate-core F3): the payload hash is a
        # database string, and the winner's branch was the commit's
        # only git anchor — deleting that one ref plus a gc silently
        # demoted the gate to the legacy live-tree merge. The gate
        # resolves via refs/critic-snapshots/<contract>/<candidate>
        # first; terminal cleanup deletes it.
        pin = getattr(self.mgr, "pin_snapshot_ref", None)
        if callable(pin):
            pin(self.contract_id, winner["id"], commit)

    def _apply_human_gate(self, verdict_id, decision, result):
        return record_human_gate(
            self.store, self.contract_id, decision, _result=result,
            _mgr=self.mgr, channel=self.channel,
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
            provenance="internal",
        )
        # Escalated runs credit the validity they measured (fix spec
        # G8; round-3 contest-core F7): the scored candidates of the
        # deciding attempt are the recording list — before, an empty
        # list dropped every median the matrix had recorded.
        self._record_validity(None, eliminated, {}, judged)
        gate_details["escalation_reason"] = reason
        lesson_id = self._write_lesson(attempt, gate_details)
        store.set_contract_status(self.contract_id, "escalated")
        # Terminal state: the field's worktree dirs and branches go
        # away (fix spec G9; round-4 contest-core F4) — an escalated
        # run used to leave every checkout and branch behind, forever.
        _remove_candidate_worktrees(
            store, self.mgr, self.contract_id, delete_branches=True)
        if self.progress is not None:
            self.progress.finish("escalated", f"Escalated: {reason}")
        result = {
            "contract_id": self.contract_id, "status": "escalated",
            "detail": reason, "verdict_id": verdict_id, "lesson_id": lesson_id,
            "events": self.events,
        }
        if self.route_hold is not None:
            result["route_change_hold"] = self.route_hold
        # No certificate here (tightening spec T6; claims #1): an
        # escalation has no winner, and issue_certificate returns None
        # for a winnerless verdict — the escalated result carries no
        # stamp and no token.
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
                # Owner ruling 2026-11-09 (Item 1 = A): the router may
                # only promote a seat that passed the deciding gate.
                # The passed set is scoped to the deciding attempt's
                # evaluated candidates (round-9 fix spec M1): the
                # candidate→expert map spans every attempt and also
                # registers seats that never generated on this attempt
                # (an unavailable seat's row, a seat that only produced
                # in an earlier attempt), and none of those faced the
                # deciding gate. The evaluated candidates are this
                # attempt's rows whose generation completed — the same
                # idiom the verdict builders use for the deciding
                # attempt's scored candidates — mapped through the
                # expert map, minus the experts whose candidates
                # failed the deciding gate in ANY class the gate
                # stack produces (round-11 fix spec Unit A, HIGH-1 /
                # HIGH-2): hard-failure rows, failing measurement
                # rows in either shape (store "pass" / raw "passed"),
                # and the candidate rows' own elimination records —
                # gate_error and provenance eliminations append no
                # hard-failure entry and store no measurement rows,
                # so only the rows see them. Judged-eliminated
                # candidates stay in the set: they passed the
                # deciding gates.
                cand_experts = gate_details.get("candidate_experts") or {}
                failed_cids = {
                    f.get("candidate_id")
                    for f in gate_details.get("hard_failures") or []
                }
                failed_cids |= {
                    m.get("candidate_id")
                    for m in gate_details.get("measurements") or []
                    if _measurement_failed(m)
                }
                deciding_attempt = attempt
                if verdict and verdict.get("attempt") is not None:
                    deciding_attempt = verdict["attempt"]
                deciding_rows = [
                    c for c in store.candidates_for(
                        self.contract_id, attempt=deciding_attempt)
                    if c.get("status") == "completed"
                ]
                # The rows' elimination records are the source of
                # truth for the remaining gate classes (and for any
                # future one): _elimination_is_gate_failure answers
                # the registered kinds from _GATE_FAILED_KINDS and
                # treats any OTHER non-empty kind as a gate failure
                # too — only "judged" and "bracket_cut", the two
                # documented later-stage eliminations, keep a row in
                # the passed set (round-12 attack A4). The evidence
                # read is shape-coerced (_row_evidence): a non-dict
                # evidence value on a deciding row must not crash
                # this derivation after the verdict exists
                # (round-12 attack A5).
                # This also covers dissent re-run eliminations: the
                # re-run refresh path marks the same rows via
                # _mark_eliminated, so the complete derivation applies
                # after a re-run elimination with no second code path.
                failed_cids |= {
                    c["id"]
                    for c in deciding_rows
                    if _elimination_is_gate_failure(
                        _row_evidence(c).get("eliminated_kind"))
                }
                deciding_cids = {c["id"] for c in deciding_rows}
                gate_passed = {
                    cand_experts[cid]
                    for cid in deciding_cids
                    if cid in cand_experts and cid not in failed_cids
                }
                router_mod.apply_lesson_route_change(
                    store, router_mod.category_of(self.contract["domain"]),
                    lesson["route_change"], self.contract_id,
                    gate_passed=gate_passed,
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
                outcome="observed", note=note, provenance="internal",
            )
        except Exception:  # noqa: BLE001 - bookkeeping must not break a verdict
            pass
        setter = getattr(store, "set_verdict_route_hold", None)
        if callable(setter) and verdict and verdict.get("id"):
            try:
                setter(verdict["id"], hold)
            except Exception:  # noqa: BLE001
                pass


def _attach_certificate(result, store, contract_id):
    """Add the MM CERTIFIED certificate block to a run result (Unit B).

    Issued only for a run that reached a verdict — chain.issue_certificate
    returns None otherwise, and the key stays absent. Bookkeeping only:
    never raises into the loop.
    """
    if not isinstance(result, dict) or result.get("certificate") is not None:
        return result
    try:
        from . import chain as _chain

        cert = _chain.issue_certificate(store, contract_id)
    except Exception:  # noqa: BLE001 - bookkeeping must not undo a verdict
        cert = None
    if cert is not None:
        result["certificate"] = cert
    return result


def run_contract(store, contract: dict, repo_path, executors: dict, human_decider=None,
                 channel: str = "cli") -> dict:
    """Run one contract end-to-end. See module docstring for outcomes."""
    return _Run(store, contract, repo_path, executors, human_decider,
                channel=channel).run()


def record_human_gate(store, contract_id: str, decision: str, reason: str | None = None,
                      _result: dict | None = None, _mgr: WorktreeManager | None = None,
                      channel: str = "cli") -> dict:
    """Complete a pending human gate: approved merges the winner and
    writes the final answer — refused when the verdict carries an offered
    waiver (the winner failed the measured gate), where only an explicit
    ``waived`` merges and is recorded on the verdict row as granted;
    waived applies only to a measured failure, never a hard one; rejected
    creates a new contract carrying the rejection.

    The gate is claimed atomically BEFORE anything merges (fix spec G1;
    round-4 security F4, contest-core F2): the claim IS the decision
    record, so a concurrent decider gets the no-pending-gate error and
    exactly one final answer is ever written. The decision's ``by`` is
    the ``channel`` it arrived through — "cli" by default, "tool" from
    the plugin's gate tool — never a hardcoded "human" (round-4
    security F7). The merge takes the verdict-time snapshot commit
    recorded on the winner, never the live worktree (contest-core F1);
    verdicts without a valid snapshot cannot be merged. The certificate attaches only
    once the decision completes — refused, rejected, and error results
    carry no stamp and no token (round-4 regression F1).

    Everything after a successful claim is wrapped (round-5 fix spec
    H1; round-5 gate-core F1, security F1): a completion failure —
    a merge conflict, a store error — never raises out of here and
    never leaves the gate silently consumed. It lands in the recorded
    failure state (verdict "failed", contract "gate_failed", a
    gate_failure observation) and returns an error result naming it;
    presenting the decision again claims with retry=True and
    re-attempts completion from the snapshot.

    A claim whose process died before the wrap could run is detected
    from the durable state alone (round-6 fix spec J1; round-6
    gate-core F1): a decided verdict with no final answer and no
    terminal contract status IS an interrupted completion, and
    presenting the decision treats it as a retry — the interruption
    is first recorded as a gate_failure observation so the audit
    trail matches the exception path. A retry — from "failed" or
    from the interrupted state — must present the decision that was
    recorded; a different one is refused pre-claim, naming it
    (round-6 security F2). Completion is idempotent and the failure
    state is honest: once a final answer exists, a later failing
    step converges to the completed result instead of flipping the
    gate to "failed", and no second answer is ever written (round-6
    gate-core F3, security F1).

    Round 7 hardens the recovery (fix spec K1/K2/K3/K6): the
    detector also owns the post-answer window — a decided verdict
    whose final answer landed but whose terminal status never did
    is an interrupted completion too, and the presentation converges
    it instead of refusing forever. Convergence VALIDATES the
    answer it trusts: a row whose recorded decision differs, or
    whose "git:" commit names no commit in the contract's
    repository, completes nothing — the presentation refuses with
    the failed check named, records no failure state, and leaves
    the gate unconsumed for the operator. The detector's recording
    flip is conditional on the state it observed, so a presenter
    racing a live recovery loses cleanly and answers per the fresh
    state; and the verdict's claim — never the failure log — is the
    authority a retry is bound to.

    Round 8 completes the validation (fix spec L1/L4/L5):
    convergence trusts a row only when it IS the merge it claims
    to record — the winner's snapshot descends into its commit and
    the commit is reachable from main's head, and a non-git commit
    form fails outright. When a final answer exists, that
    validation runs BEFORE the interruption detector records
    anything, so a refused plant draws no "process death" record;
    and the refusal leaves its own audit observation, so the
    recovery of the claim it left standing is not written up as a
    death either. The completion result reports the answer row's
    commit as stored, never re-prefixed into a form the row did
    not carry.
    """
    if decision not in ("approved", "rejected", "waived"):
        return {"contract_id": contract_id, "status": "error",
                "detail": f"unknown human-gate decision {decision!r}"}
    verdict = store.latest_verdict(contract_id)
    if verdict is None:
        return {"contract_id": contract_id, "status": "error",
                "detail": "no pending human gate for this contract"}
    contract = store.get_contract(contract_id)
    mgr = _mgr or WorktreeManager(Path(contract["repo_path"]))
    winner = store.get_candidate(verdict["winner"]) if verdict.get("winner") else None
    waiver = verdict.get("waiver") or {}
    gate_state = verdict.get("human_gate")
    # A gate in the recorded failure state is claimable again, with
    # retry: presenting the decision IS the retry (spec H1). So is a
    # gate whose claim landed but whose completion never finished —
    # the process died after the claim and the failure state was
    # never written (round-6 fix spec J1; round-6 gate-core F1).
    interrupted = _interrupted_completion(store, contract_id, verdict, contract)
    is_retry = gate_state == "failed" or interrupted is not None
    claimable = gate_state in ("pending", "failed") or interrupted is not None

    if is_retry:
        # A retry re-attempts the completion of the decision that
        # was recorded — it is not a fresh decision surface
        # (round-6 fix spec J1; round-6 security F2). A different
        # decision is refused here, pre-claim, naming the recorded
        # one; the state is left untouched.
        recorded = _recorded_gate_decision(store, contract_id, verdict)
        if recorded is not None and decision != recorded:
            return {"contract_id": contract_id, "status": "error",
                    "human_gate": gate_state,
                    "detail": (
                        f"retry refused: this gate's recorded decision is "
                        f"{recorded!r}; a retry must present the recorded "
                        f"decision — a different decision needs an explicit "
                        f"operator reset, not the retry path")}
        if interrupted is not None:
            if interrupted == _INTERRUPTED_AFTER_ANSWER:
                # Validation precedes detector recording (round-8
                # fix spec L4; round-8 attack F4, security F2): a
                # final answer exists, so validate it BEFORE
                # anything is recorded. A row that fails refuses
                # via the mismatch path and this presentation
                # appends NO interruption record — there was no
                # completion to interrupt, and at the old order the
                # detector wrote a "process death" over a live,
                # designed refusal, then the prescribed recovery
                # wrote a second one over the refused claim it left
                # standing. A row that validates is a real
                # interruption: it records and converges as before.
                answer = _final_answer_row(store, contract_id)
                if answer is not None:
                    problem = _validate_final_answer(
                        mgr, answer, decision, winner=winner,
                        contract_id=contract_id)
                    if problem is not None:
                        return _mismatch_result(
                            store, contract_id, gate_state, _result,
                            _FinalAnswerMismatch(problem),
                            decision=decision,
                            verdict_id=verdict["id"])
            # The interruption enters the audit trail exactly as the
            # exception path would have written it, so a reader of
            # the book cannot tell the two apart afterwards. The
            # recording's flip is conditional on the state observed
            # (round-7 fix spec K3): if it loses, the gate moved
            # under this presentation — a concurrent presenter owns
            # it now — and the answer comes from the fresh state,
            # never from a fabricated failure. One exception
            # (round-8 fix spec L4): a claim this system's own
            # refusal left standing is recovered WITHOUT the
            # gate_failure record — the refusal observation is on
            # the book, the claim is known not to be a dead
            # process's, and the record is the only part of the
            # recording that would outlive the recovery as a false
            # claim about it; the flip and status still land, so
            # the retry claim below fires from the same state.
            record = not (
                interrupted == _INTERRUPTED_AFTER_CLAIM
                and _answer_refusal_on_record(
                    store, contract_id, verdict["id"], decision))
            if not _record_interruption(
                    store, contract_id, verdict, decision, channel,
                    interrupted, record=record):
                return _answer_for_moved_gate(
                    store, contract_id, decision, _result)

    # Refusals that leave the gate undecided are decided BEFORE the
    # claim: they record nothing, consume nothing, and carry no
    # certificate.
    if claimable and decision == "approved" and waiver and winner is not None:
        # The winner failed the measured gate (fix pass Wave 2.7, review
        # H6, invariant 6): the only way past a measured failure is an
        # explicit waiver, recorded as such. A plain approval would merge
        # the failure silently with the row still saying "offered".
        result = dict(_result or {"contract_id": contract_id})
        result.update({
            "status": "error", "human_gate": gate_state,
            "detail": (
                f"approval refused: winner {winner['id']} failed the measured gate and "
                "the verdict carries an offered waiver; waive it explicitly (decision "
                "'waived', with a reason) or reject — the gate stays undecided"
            ),
        })
        return result
    if claimable and decision == "waived":
        if waiver.get("gate") != "measured" or winner is None \
                or waiver.get("candidate") != winner["id"]:
            return {"contract_id": contract_id, "status": "error",
                    "detail": "waiver applies only to a measured failure, never a hard one"}

    # The claim IS the decision record — taken before any merge, never
    # replayable, never swallowed after the fact. A retry re-claims
    # from the failure state — and an interrupted completion is one
    # by now: its interruption was recorded above in exactly the
    # failure state's shape, so it re-claims with retry like any
    # other retry, and the claim restores the verdict's decided value.
    if interrupted is not None:
        claimed = _claim_gate(
            store, {**verdict, "human_gate": "failed"}, decision,
            retry=True)
    else:
        claimed = _claim_gate(store, verdict, decision, retry=is_retry)
    if not claimed:
        return {"contract_id": contract_id, "status": "error",
                "detail": "no pending human gate for this contract"}
    result = dict(_result or {"contract_id": contract_id})
    result["human_gate"] = decision
    try:
        return _complete_human_gate(
            store, contract_id, decision, reason, contract, mgr,
            verdict, winner, waiver, result, channel)
    except _FinalAnswerMismatch as mismatch:
        # The answer on record failed convergence validation
        # (round-7 fix spec K1): refuse WITHOUT the failure state —
        # nothing further is written, no certificate, and the gate
        # is not consumed; the row is the operator's to resolve.
        return _mismatch_result(
            store, contract_id, gate_state, _result, mismatch,
            decision=decision, verdict_id=verdict["id"])
    except Exception as exc:  # noqa: BLE001 - completion failures land in the failure state
        mismatch = None
        if decision != "rejected" \
                and _final_answer_row(store, contract_id) is not None:
            # The completion's durable record already exists: whatever
            # failed came after it, so the gate is completed, not
            # failed (round-6 fix spec J1; round-6 gate-core F3,
            # security F1). Converge instead of recording a failure
            # state the book would contradict. The convergence
            # validates the row like any other (round-7 fix spec
            # K1): a mismatch on re-entry is the refusal above, not
            # a failure state.
            try:
                return _complete_human_gate(
                    store, contract_id, decision, reason, contract, mgr,
                    verdict, winner, waiver, result, channel)
            except _FinalAnswerMismatch as caught:
                mismatch = caught
            except Exception:  # noqa: BLE001 - fall through to the failure state
                pass
        if mismatch is not None:
            return _mismatch_result(
                store, contract_id, gate_state, _result, mismatch,
                decision=decision, verdict_id=verdict["id"])
        failure = _record_gate_failure(
            store, contract_id, verdict, decision, channel, exc)
        result = dict(_result or {"contract_id": contract_id})
        result.update({
            "status": "error", "human_gate": "failed",
            "detail": (
                f"human gate decision {decision!r} was claimed but its "
                f"completion failed: {failure}; the gate is recorded "
                f"as failed (contract status 'gate_failed') and the "
                f"decision may be presented again to retry completion"
            ),
        })
        return result


def _complete_human_gate(store, contract_id, decision, reason, contract,
                         mgr, verdict, winner, waiver, result, channel):
    """The post-claim completion of a human gate: rejection handling,
    the waiver-grant record, the merge, the final answer. Called by
    record_human_gate under its failure wrap — anything raised here
    lands the gate in the recorded failure state, so this function
    records nothing speculatively and swallows nothing.

    Completion is idempotent (round-6 fix spec J1; round-6 gate-core
    F3, security F1): a retry — or a convergence after a post-answer
    failure — re-enters here, and whatever the first attempt already
    made durable is reused, never duplicated: an existing final
    answer means the merge already landed, so the merge is skipped
    outright and the remaining steps (status, ref cleanup,
    certificate) simply converge; an existing rejection observation
    on the child contract is reused instead of appended twice.

    Round 7 (fix spec K1/K3/K6): the existing answer is validated
    before it is trusted (:func:`_finish_from_existing_answer`) — a
    planted row completes nothing; the waiver grant is preserved
    when a previous attempt already wrote it; and the answer write
    itself tolerates the store's duplicate signal: a concurrent
    presenter who won the atomic insert leaves this completion to
    converge on their row instead of duplicating it."""
    if decision == "rejected":
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
        child = store.get_contract(new_id) or {}
        already_rejected = any(
            isinstance(obs, dict) and obs.get("kind") == "human_rejection"
            and obs.get("from_contract") == contract_id
            for obs in child.get("observations") or [])
        if not already_rejected:
            store.append_observation(
                new_id,
                {"kind": "human_rejection", "from_contract": contract_id,
                 "reason": reason or "rejected at the human gate",
                 # The deciding channel rides the observation, as it
                 # does on gate_completion (round-6 fix spec J5).
                 "channel": channel},
            )
        store.set_contract_status(contract_id, "rejected")
        # Terminal state: the field's worktree dirs and branches go
        # away (fix spec G9; round-4 contest-core F4). No certificate:
        # a rejected verdict certifies nothing (regression F1).
        _remove_candidate_worktrees(store, mgr, contract_id, delete_branches=True)
        result.update({"status": "rejected", "new_contract_id": new_id})
        _update_gate_run_status(
            store,
            contract_id,
            "rejected",
            f"Rejected at human gate; opened follow-up contract {new_id}",
        )
        return result

    # A final answer already on the book means a previous attempt
    # completed the merge and wrote the completion's durable record:
    # converge — never re-merge, never write a second answer
    # (round-6 fix spec J1; round-6 gate-core F3, security F1). The
    # row is validated before it is trusted (round-7 fix spec K1;
    # round-7 attack F1, security F3): convergence is the first
    # thing this completion does, so a row that fails validation
    # aborts before the waiver grant or anything else is written.
    existing_answer = _final_answer_row(store, contract_id)
    if existing_answer is not None:
        return _finish_from_existing_answer(
            store, contract_id, decision, reason, mgr, verdict,
            winner, waiver, result, channel)

    # The waiver-grant record is written BEFORE the merge attempt
    # (round-5 fix spec H1; round-5 gate-core F7): the decision and
    # its reason are known at claim time and the merge does not
    # change them. A failed write aborts here — into the caller's
    # failure wrap — instead of being swallowed after a merge the
    # book would then misdescribe as merely "offered". A grant a
    # previous attempt already wrote is preserved, not rewritten
    # with this call's defaults (round-7 fix spec K6).
    waiver_record = None
    if decision == "waived":
        # The waiver is granted, by the deciding channel, for a reason
        # — on the verdict row, not only in the run result (fix pass
        # Wave 2.7, review H6; the channel replaces the old hardcoded
        # "human", round-4 security F7).
        waiver_record = _grant_waiver(
            store, contract_id, verdict, waiver, winner, reason,
            channel)

    # approved (or a valid measured waiver): merge the winner's
    # verdict-time snapshot — what was gated — never the live tree.
    # The snapshot resolves via its dedicated ref first (round-5 fix
    # spec H6): the ref survives the winner branch's deletion, where
    # the payload-hash string alone would leave the commit one gc
    # away from unrecoverable. Convergence validation resolves it
    # through the same helper (:func:`_winner_snapshot`), so the two
    # can never disagree about which commit was gated.
    wt = Path(winner["worktree"])
    snapshot = _winner_snapshot(mgr, contract_id, winner)
    is_commit = getattr(mgr, "is_commit", None)
    merge_commit = getattr(mgr, "merge_commit_into_main", None)
    used_snapshot = bool(
        snapshot and callable(is_commit) and callable(merge_commit)
        and is_commit(snapshot)
    )
    if used_snapshot:
        commit = snapshot
        merged_sha = merge_commit(snapshot)
        result["merge_source"] = "verdict_snapshot"
    else:
        raise RuntimeError("winner snapshot is missing or invalid; refusing live-worktree merge")
    for loser_id in verdict.get("eliminated", []):
        loser = store.get_candidate(loser_id)
        if loser and Path(loser["worktree"]).exists():
            mgr.keep_as_ref(contract_id, loser["slot"])
    mgr.remove(wt)
    # Terminal state: the winner's branch is merged and spent, and the
    # baseline branch dies with the contract (fix spec G9). Loser
    # branches stay as refs — the keep_as_ref idiom above.
    deleter = getattr(mgr, "delete_branch", None)
    if callable(deleter):
        try:
            deleter(_branch_for_worktree(wt))
        except Exception:  # noqa: BLE001 - cleanup must not undo a merge
            pass
        try:
            deleter(f"critic/{contract_id}/baseline")
        except Exception:  # noqa: BLE001
            pass
    try:
        store.create_final_answer(
            target_id=contract["target_id"], contract_id=contract_id,
            commit=f"git:{merged_sha}",
            intent_check=None, human_gate=decision,
        )
    except Exception as exc:  # noqa: BLE001 - the duplicate signal converges; anything else takes the failure path
        if _final_answer_row(store, contract_id) is None \
                or not _is_duplicate_answer_signal(exc):
            raise
        # A concurrent presenter won the store's atomic insert
        # (round-7 fix spec K3): converge on their row — validated
        # like any existing answer — instead of duplicating it.
        # This merge landed the same snapshot, so the book agrees
        # either way.
        return _finish_from_existing_answer(
            store, contract_id, decision, reason, mgr, verdict,
            winner, waiver, result, channel)
    # The merge source is durable, not only in the result dict
    # (round-5 fix spec H1; round-5 gate-core F3): a gate_completion
    # observation on the contract names which path merged — the
    # verdict-time snapshot or the legacy live-tree fallback — so a
    # later reader never has to guess from the winner's payload hash
    # (which, on a fallback merge, is a dispatch-time diff hash, not
    # a commit). Best-effort: the final answer above is the
    # completion's durable record either way.
    try:
        store.append_observation(contract_id, {
            "kind": "gate_completion", "decision": decision,
            "merge_source": result["merge_source"],
            "commit": merged_sha, "channel": channel,
        })
    except Exception:  # noqa: BLE001 - bookkeeping must not undo a merge
        pass
    store.set_contract_status(contract_id, "completed")
    # Terminal state: the winner's snapshot ref is spent (round-5
    # fix spec H6) — the commit it anchored is merged into main and
    # reachable from there.
    _delete_snapshot_ref(mgr, contract_id, winner["id"])
    result.update({"status": "completed", "commit": f"git:{merged_sha}",
                   "winner_commit": f"git:{commit}", "waiver": waiver_record})
    # The certificate attaches ONLY now — a completed, gate-decided
    # state (fix spec G1; round-4 regression F1).
    _attach_certificate(result, store, contract_id)
    _update_gate_run_status(
        store,
        contract_id,
        "completed",
        f"Merged ({decision}) into main (git:{merged_sha})",
        certificate_stamp=(result.get("certificate") or {}).get("stamp"),
    )
    return result
