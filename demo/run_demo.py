#!/usr/bin/env python3
"""The Millionaire Matrix — end-to-end demo script.

Runs the loop against the python-sampler fixture:

  Phase A (live attempt): the demo contract is dispatched to the real
  model CLIs (Codex, Grok) and whatever happens is recorded. In the
  build sandbox both were unavailable (codex installed but not logged
  in, grok absent) and the ledger recorded the refusals. On a machine
  where the CLIs are logged in, this phase dispatches them for real —
  and spends their tokens — and the outcome printed below is read back
  from the recorded rows, never from a canned sentence.

  Phase B (local panel): the same contract pair runs with a panel of
  three deterministic local solver families (local-naive, local-ring,
  local-balanced — labelled as such everywhere) plus three local demo
  judges (deterministic packet-text scorers, NOT models). Run 1: the
  naive and balanced candidates die on the measured gate (the anchor),
  one survivor stands, so the anchor alone decides — the matrix does
  not run. A fresh session then injects run 1's Lesson. Run 2: the
  lesson-conditioned panel all passes the measured gate, the peer
  matrix fills in over the full field and the bracket steps it down
  3 -> 2 -> 1.

Phase B always runs this local panel; nothing in this script switches
it to live seats. The C# fixture and its contracts
(fixture/godot-csharp/, contracts/demo*-csharp.json) are for a live
contest driven from the CLI or the critic_run tool on the operator's
machine, not from this script.

Usage: python3 demo/run_demo.py [--keep]
Output is a transcript; state lands in demo-output/ (regenerable).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hybrid_critic_loop import hooks as hooks_mod  # noqa: E402
from hybrid_critic_loop import loop as loop_mod  # noqa: E402
from hybrid_critic_loop.contracts import load_contract_file  # noqa: E402
from hybrid_critic_loop.executors import (  # noqa: E402
    CodexExecutor,
    GrokExecutor,
    LocalJudgeExecutor,
    LocalSolverExecutor,
)
from hybrid_critic_loop.reports import (  # noqa: E402
    ledger_report,
    lessons_report,
    matrix_report,
)
from hybrid_critic_loop.state import Store  # noqa: E402

OUT = ROOT / "demo-output"


class DemoSolver(LocalSolverExecutor):
    """Local solver with a distinct panel family per seat.

    The base solver reports family "local" for every profile; the
    demo panel needs one family per seat, exactly like the operator's
    real panel. Generation behaviour is the base solver's, unchanged.
    """

    def __init__(self, expert_id, profile, family):
        super().__init__(expert_id, profile)
        self.family = family


def demo_panel():
    return {
        "solver-naive": DemoSolver("solver-naive", "naive", "local-naive"),
        "solver-ring": DemoSolver("solver-ring", "ring", "local-ring"),
        "solver-balanced": DemoSolver("solver-balanced", "balanced", "local-balanced"),
        "judge-naive": LocalJudgeExecutor("judge-naive", "local-naive", "strict"),
        "judge-ring": LocalJudgeExecutor("judge-ring", "local-ring", "evidence-first"),
        "judge-balanced": LocalJudgeExecutor("judge-balanced", "local-balanced", "lenient"),
    }


def panel_route(store):
    store.upsert_route(
        "code", "solver-naive", "solver-balanced",
        ["solver-ring", "judge-naive", "judge-ring", "judge-balanced"],
        ["judge-naive", "judge-ring", "judge-balanced"],
        "peer_matrix; no self-scoring; blind",
        panel=["anthropic", "openai", "xai", "google", "reflection",
               "alibaba", "moonshot", "deepseek"],
        scoring="peer_matrix; no self-scoring; blind",
    )


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def git(args, cwd):
    subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True,
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )


def fresh_repo(name: str) -> Path:
    repo = OUT / name
    shutil.copytree(ROOT / "fixture" / "python-sampler", repo)
    git(["init", "-q", "."], repo)
    git(["add", "-A"], repo)
    git(["-c", "user.email=demo@local", "-c", "user.name=demo",
         "commit", "-q", "-m", "baseline: stubs"], repo)
    return repo


def show_result(result: dict) -> None:
    print(json.dumps(result, indent=2, default=str))


def phase_a_outcome_lines(result: dict, summary) -> list:
    """Phase A's outcome, read back from the run result and the ledger
    summary rows — never a canned expectation (fix pass Wave 2.2).

    ``summary`` is ``store.ledger_summary()``: one row per expert with
    dispatches / completed / refused / timeout counts.
    """
    status = (result or {}).get("status")
    head = f"Phase A outcome (recorded): status={status}"
    detail = (result or {}).get("detail")
    if detail:
        head += f" — {detail}"
    lines = [head]
    for row in summary or []:
        if row.get("expert_id") not in ("codex", "grok"):
            continue
        lines.append(
            f"  {row.get('expert_id')}: dispatches={row.get('dispatches')} "
            f"completed={row.get('completed')} refused={row.get('refused')} "
            f"timeout={row.get('timeout')}"
        )
    lines.append("A seat that completed produced a real candidate from its CLI; a refused or")
    lines.append("unavailable seat produced none and is recorded as such. On a machine with")
    lines.append("logged-in CLIs this call dispatches the real seats and spends their tokens.")
    return lines


def show_bracket(store, contract_id: str) -> None:
    verdict = store.latest_verdict(contract_id)
    judged = (verdict or {}).get("judged") or {}
    bracket = judged.get("bracket") or {}
    for rnd in bracket.get("rounds", []):
        print(f"bracket round {rnd['round']}: field {rnd['field_in']} -> "
              f"{rnd['field_out']}, cut line {rnd['cut_line']}, "
              f"eliminated {rnd['eliminated']}")
    print(f"panel_status: {judged.get('panel_status')}")


def main() -> int:
    keep = "--keep" in sys.argv
    if OUT.exists() and not keep:
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    failures = []

    banner("THE MILLIONAIRE MATRIX — END-TO-END DEMO")
    print("Eight models walk in. Two battle in the final round. One")
    print("answer walks out. (Owner's framing — the field here is the")
    print("honest local one: three solver families, labelled as such.)")
    print("Generators in this environment: the live model CLIs are attempted")
    print("first (Phase A); the full loop then runs on deterministic local")
    print("solver programs (Phase B). No output below is simulated: every")
    print("number comes from a real run of the plugin's own machinery.")

    # ------------------------------------------------------------------
    banner("PHASE A — live CLI attempt (codex vs grok) on contract 1")
    repo_a = fresh_repo("repo-live")
    store_a = Store(OUT / "live-data" / "data.db")
    store_a.upsert_route(
        "code", "codex", "grok", ["claude"],
        ["gemini", "beam", "qwen-local"],
        "judge.family not in contest.families; panel recomputed on fallback",
    )
    for eid, ex in (("codex", CodexExecutor()), ("grok", GrokExecutor())):
        ok, why = ex.available()
        print(f"executor {eid}: available={ok} ({why})")
    contract1 = load_contract_file(ROOT / "contracts" / "demo1.json")
    result_a = loop_mod.run_contract(
        store_a, contract1, repo_a,
        {"codex": CodexExecutor(), "grok": GrokExecutor()},
        human_decider=lambda info: "approved",
    )
    show_result(result_a)
    print()
    print(ledger_report(store_a))
    print()
    for line in phase_a_outcome_lines(result_a, store_a.ledger_summary()):
        print(line)

    # ------------------------------------------------------------------
    banner("PHASE B — contract 1: panel of three local solver families")
    repo = fresh_repo("repo-local")
    data_dir = OUT / "local-data"
    store = Store(data_dir / "data.db")
    panel_route(store)
    executors = demo_panel()
    print("Panel: solver-naive (local-naive), solver-ring (local-ring),")
    print("solver-balanced (local-balanced); peer judges are deterministic")
    print("local demo judges over packet text — labelled, not models.")
    print("Route before run 1: primary=solver-naive, compete_with=solver-balanced")
    result1 = loop_mod.run_contract(
        store, contract1, repo, executors,
        human_decider=lambda info: "approved",
    )
    show_result(result1)
    if result1.get("status") != "completed":
        failures.append(f"run 1 status {result1.get('status')} != completed")
    winner1 = store.get_candidate(result1.get("winner") or "")
    if winner1 and winner1["expert_id"] == "solver-naive":
        failures.append("run 1 winner is solver-naive (it must die on the measured gate)")
    for cand in store.candidates_for("ctr_demo_1"):
        for m in store.measurements_for(cand["id"]):
            print(f"measurement {cand['id']} ({cand['expert_id']}): "
                  f"{m['metric']} = {m['value']} (required {m['op']} {m['threshold']}) pass={m['pass']}")
    print()
    print("The validity matrix for contract 1 (peer scores, blind):")
    print(matrix_report(store, "ctr_demo_1"))
    _v1 = store.latest_verdict("ctr_demo_1") or {}
    if not ((_v1.get("judged") or {}).get("bracket")):
        print("(run 1's matrix did not run: the measured gate left a single")
        print("survivor, so the anchor alone decided — see run 2 for the matrix.)")
    show_bracket(store, "ctr_demo_1")
    print()
    print("Lesson written at the verdict:")
    print(lessons_report(store, domain="code.game"))
    lesson = store.lessons_for(domain="code.game")[0]
    print(f"lesson rule: {lesson['rule']}")
    print(f"lesson route_change: {lesson['route_change']}")
    print(f"lesson written_to: {lesson['written_to']}")
    route = store.get_route("code")
    print(f"Route after run 1: primary={route['primary']}, compete_with={route['compete_with']}")
    if route["primary"] == "solver-naive":
        failures.append("route demotion did not apply (solver-naive still primary)")
    print()
    print(ledger_report(store))

    # ------------------------------------------------------------------
    banner("PHASE B — fresh session: the lesson is already in context")
    hooks_mod.on_session_start(store, data_dir, domain="code.game")
    injected = hooks_mod.pre_llm_call(store, data_dir, domain="code.game")
    if injected and injected.get("context"):
        print(injected["context"])
    else:
        failures.append("pre_llm_call injected nothing on the fresh session")
        print("(nothing injected — FAILURE)")

    # ------------------------------------------------------------------
    banner("PHASE B — contract 2: the bracket steps the full field down")
    contract2 = load_contract_file(ROOT / "contracts" / "demo2.json")
    print(f"Route for run 2: primary={route['primary']}, compete_with={route['compete_with']}")
    result2 = loop_mod.run_contract(
        store, contract2, repo, executors,
        human_decider=lambda info: "approved",
    )
    show_result(result2)
    if result2.get("status") != "completed":
        failures.append(f"run 2 status {result2.get('status')} != completed")
    for cand in store.candidates_for("ctr_demo_2"):
        for m in store.measurements_for(cand["id"]):
            print(f"measurement {cand['id']} ({cand['expert_id']}): "
                  f"{m['metric']} = {m['value']} (required {m['op']} {m['threshold']}) pass={m['pass']}")
            if not m["pass"]:
                failures.append(f"run 2 measurement failed for {cand['id']}")
    print()
    print("The validity matrix for contract 2:")
    print(matrix_report(store, "ctr_demo_2"))
    show_bracket(store, "ctr_demo_2")
    verdict2 = store.latest_verdict("ctr_demo_2")
    rounds2 = ((verdict2 or {}).get("judged") or {}).get("bracket", {}).get("rounds", [])
    if [ (r["field_in"], r["field_out"]) for r in rounds2 ] != [(3, 2), (2, 1)]:
        failures.append(f"run 2 bracket rounds unexpected: {rounds2}")
    print()
    print(ledger_report(store))
    merged = (repo / "src" / "steering_sampler.py").read_text()
    print()
    print("Winner merged on main:", "NotImplementedError" not in merged)

    # ------------------------------------------------------------------
    banner("DEMO RESULT")
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("All demo expectations met: the measured gate eliminated the")
    print("allocating candidates in run 1 (anchor decision, one survivor),")
    print("the lesson was written and demoted its expert, a fresh session")
    print("injected the lesson, and in run 2 the peer matrix filled in")
    print("over the full field and the bracket stepped it down 3 -> 2 -> 1.")
    print("Ledger, matrix and lessons above are database rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
