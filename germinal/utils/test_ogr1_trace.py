#!/usr/bin/env python3
"""
Smoke tests for ogr1_trace. Standard library only; no GPU, no cluster, seconds to run.

Why this file exists: the five scenarios below were originally exercised by hand and
never kept. A validation that survives only as a memory of having run it is the same
defect, in miniature, as benchmarking code that is not in the released container --
it cannot be re-run after the next edit. Every scenario here is therefore an assertion
in a file that ships with the archive.

Run:
    python3 test_ogr1_trace.py              # all scenarios
    python3 test_ogr1_trace.py --self-test  # prove the assertions can reject a bad run

Scenarios:
    S1  normal run, counters agree with the events file
    S2  accepted designs and accepted trajectories are distinct quantities
    S3  time guard stops at a seed boundary and writes state=paused
    S4  cross-job resume: read_ledger / seeds_to_run across two event files
    S5  a seed killed mid-trajectory is retried, not silently treated as finished
    S6  the funnel invariant submitted - completed == len(exclusions)
"""

from __future__ import annotations

import json
import glob
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ogr1_trace import (                                    # noqa: E402
    JobTerminated, RunRecorder, TimeBudget, read_ledger, seeds_to_run,
)

FAILURES: list[str] = []


def check(name: str, ok: bool, detail=None):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        if detail is not None:
            print(f"        {detail}")
        FAILURES.append(name)


def events(run_dir: str) -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(run_dir, "events_*.jsonl"))):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def recorder(run_dir: str, jobid: str, budget=None,
             container_path=None) -> RunRecorder:
    """One recorder standing in for one Slurm job of the same run."""
    os.environ["SLURM_JOB_ID"] = jobid
    os.environ.pop("SLURM_RESTART_COUNT", None)
    return RunRecorder(
        run_dir=run_dir, arm="D_ablang_opensource", target="pdl1",
        language_model="ablang", scoring_backend="opensource", budget=budget,
        container_path=container_path,
    )


# ----------------------------------------------------------------------------------
# S1 + S2 + S6: one normal job
# ----------------------------------------------------------------------------------

def scenario_normal(run_dir: str):
    print("S1/S2/S6  normal run")
    rec = recorder(run_dir, "1001")

    # Trajectory A: passes cofolding, yields two accepted designs and one rejected.
    with rec.trajectory(11111) as t:
        h = t.stage_start("stage1_hallucination")
        t.stage_end(h, outcome="pass", plddt=0.91)
        h = t.stage_start("stage2_cofolding")
        t.stage_end(h, outcome="pass", i_pae=0.14)
        h = t.stage_start("stage3_abmpnn")
        t.stage_end(h, outcome="pass", n_sequences=3)
        for j, accepted in enumerate([True, True, False], start=1):
            name = f"pdl1_nb_s11111_abmpnn_{j}"
            h = t.stage_start("stage4_final_filters", design_name=name)
            t.stage_end(h, outcome="pass" if accepted else "fail",
                        fail_reason=None if accepted else "ipsae_threshold")
            if accepted:
                t.design_accepted(name)
            else:
                t.design_rejected(name, reason="ipsae_threshold")

    # Trajectory B: rejected at the cofolding gate. The `continue` in run_germinal.py
    # needs no logging call of its own; the context manager writes traj_end anyway.
    with rec.trajectory(22222) as t:
        h = t.stage_start("stage1_hallucination")
        t.stage_end(h, outcome="pass")
        h = t.stage_start("stage2_cofolding")
        t.stage_end(h, outcome="fail", fail_reason="i_pae_threshold")

    rec.finish()
    rec._finalize()

    c = rec.cumulative_counters()
    check("S1 submitted == 2", c["submitted"] == 2, c)
    check("S1 completed == 2", c["completed"] == 2, c)
    check("S1 entered_cofolding == 2", c["entered_cofolding"] == 2, c)
    check("S1 passed_cofolding == 1", c["passed_cofolding"] == 1, c)

    # The point of S2. These two must not be conflated: comparing the trajectory
    # counter against rows of accepted/designs.csv is what previously made C7 fail
    # for a reason that lived in the specification rather than in the run.
    check("S2 accepted (trajectories) == 1", c["accepted"] == 1, c)
    check("S2 accepted_designs (variants) == 2", c["accepted_designs"] == 2, c)
    check("S2 the two counters differ here", c["accepted"] != c["accepted_designs"], c)

    ev = events(run_dir)
    n_traj_end = sum(1 for e in ev if e.get("event") == "traj_end")
    n_design_acc = sum(1 for e in ev if e.get("event") == "design_accepted")
    n_design_rej = sum(1 for e in ev if e.get("event") == "design_rejected")
    check("S1 one traj_end per trajectory", n_traj_end == 2, n_traj_end)
    check("S2 design_accepted events == accepted_designs",
          n_design_acc == c["accepted_designs"], (n_design_acc, c["accepted_designs"]))
    check("S2 design_rejected recorded too", n_design_rej == 1, n_design_rej)

    cnt = json.load(open(glob.glob(os.path.join(run_dir, "counters_*.json"))[0]))
    excl = len(cnt["exclusions"])
    check("S6 submitted - completed == len(exclusions)",
          c["submitted"] - c["completed"] == excl,
          (c["submitted"], c["completed"], excl))


# ----------------------------------------------------------------------------------
# S3: the time guard
# ----------------------------------------------------------------------------------

def scenario_time_guard(run_dir: str):
    print("S3  time guard")
    # A deadline 10 minutes out, a 30 minute margin and a 90 minute default estimate:
    # no trajectory can fit, so the very first check must refuse.
    budget = TimeBudget(deadline_epoch=time.time() + 600,
                        margin_s=1800, default_estimate_s=5400)
    # The container digest is supplied by the launcher, because the container's own host
    # path is unreadable from inside it and hashing would silently yield None. The path
    # below deliberately does not exist, so a fallback to hashing could not succeed.
    declared = "ab" * 32
    os.environ["OGR1_CONTAINER_SHA256"] = declared
    rec = recorder(run_dir, "1002", budget=budget,
                   container_path="/nonexistent/opengerminal.sif")
    rec.write_manifest()
    allowed = rec.should_continue()
    rec._finalize()

    check("S3 should_continue() refuses", allowed is False)

    # The thresholds the guard decided with must be archived: a 'paused' state whose
    # margin and estimate are unknown cannot be interpreted after the fact.
    man = json.load(open(glob.glob(os.path.join(run_dir, "run_manifest_*.json"))[0]))
    tb = (man.get("time_budget") or {}).get("params") or {}
    check("S3 manifest records the guard thresholds",
          tb.get("margin_s") == 1800 and tb.get("default_estimate_s") == 5400
          and tb.get("safety_factor") == 1.25 and tb.get("quantile") == 0.90
          and tb.get("deadline_source") == "argument", tb)
    check("S3 manifest also records the decision at t=0",
          ((man.get("time_budget") or {}).get("at_start") or {}).get("ok") is False,
          man.get("time_budget"))

    cont = man.get("container") or {}
    check("S3 declared container digest is used, and its origin recorded",
          cont.get("sha256") == declared
          and cont.get("sha256_source") == "OGR1_CONTAINER_SHA256", cont)
    os.environ.pop("OGR1_CONTAINER_SHA256", None)
    # And without the declaration, an unreadable path must say so rather than look like
    # a container with no digest.
    rec3 = recorder(run_dir + "_nodigest", "1004",
                    container_path="/nonexistent/opengerminal.sif")
    rec3.write_manifest()
    rec3._finalize()
    man3 = json.load(open(glob.glob(
        os.path.join(run_dir + "_nodigest", "run_manifest_*.json"))[0]))
    check("S3 missing digest is labelled unavailable, not silently null",
          (man3.get("container") or {}).get("sha256_source") == "unavailable",
          man3.get("container"))
    state = json.load(open(os.path.join(run_dir, "state.json")))
    check("S3 state.json says paused", state["state"] == "paused", state.get("state"))
    check("S3 a run_paused event was written",
          any(e.get("event") == "run_paused" for e in events(run_dir)))

    # And the opposite case, so the guard is not simply always-refuse.
    run_dir2 = run_dir + "_generous"
    os.makedirs(run_dir2, exist_ok=True)
    budget2 = TimeBudget(deadline_epoch=time.time() + 86400,
                         margin_s=1800, default_estimate_s=5400)
    rec2 = recorder(run_dir2, "1003", budget=budget2)
    allowed2 = rec2.should_continue()
    rec2._finalize()
    check("S3 should_continue() allows when there is room", allowed2 is True)


# ----------------------------------------------------------------------------------
# S4 + S5: cross-job resume
# ----------------------------------------------------------------------------------

def scenario_resume(run_dir: str):
    print("S4/S5  cross-job resume")
    seed_list = [11111, 22222, 33333, 44444]

    # Job 1: finishes 11111, is killed during 33333, and never reaches the rest.
    rec = recorder(run_dir, "2001")
    with rec.trajectory(11111) as t:
        h = t.stage_start("stage2_cofolding")
        t.stage_end(h, outcome="pass")
        t.design_accepted("pdl1_nb_s11111_abmpnn_1")
    try:
        with rec.trajectory(33333) as t:
            t.stage_start("stage1_hallucination")
            raise JobTerminated("SIGTERM")
    except JobTerminated:
        pass
    rec._finalize()

    led = read_ledger(run_dir)
    check("S4 11111 is done", 11111 in {int(s) for s in led["done_seeds"]},
          led["done_seeds"])
    check("S5 33333 is queued for retry, not done",
          11111 not in led["retry_seeds"]
          and 33333 in {int(s) for s in led["retry_seeds"]},
          led["retry_seeds"])

    todo = [int(s) for s in seeds_to_run(seed_list, run_dir)]
    check("S5 a killed seed is offered again", 33333 in todo, todo)
    check("S4 a finished seed is not re-run", 11111 not in todo, todo)
    check("S4 untouched seeds remain",
          22222 in todo and 44444 in todo, todo)

    # Job 2: run_germinal's own existence check fires on 11111. Recording the skip is
    # what lets verify_run cross-check it against a terminal record from job 1; an
    # unrecorded skip is how a truncated trajectory disappears from the accounts.
    rec2 = recorder(run_dir, "2002")
    rec2.skipped(11111, "already_present_on_disk")
    rec2.finish()
    rec2._finalize()

    ev = events(run_dir)
    skips = [e for e in ev if e.get("event") == "seed_skipped"]
    check("S5 the skip is in the ledger", len(skips) == 1, skips)
    led2 = read_ledger(run_dir)
    check("S4 ledger spans both jobs", led2["n_jobs"] == 2, led2["n_jobs"])
    check("S4 no malformed lines", led2["malformed_lines"] == 0,
          led2["malformed_lines"])
    check("S4 accepted_designs survives the reload",
          led2["counters"]["accepted_designs"] == 1, led2["counters"])


# ----------------------------------------------------------------------------------
# --self-test: the assertions must be able to reject a bad run
# ----------------------------------------------------------------------------------

def self_test(run_dir: str):
    """A test suite that cannot fail proves nothing. Hand-write an events file in
    which a seed was skipped although it never finished -- the silent-loss mode C5 of
    verify_run.py exists to catch -- and require the ledger to expose it."""
    print("--self-test  the checks must reject a doctored run")
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, "events_9001_r0.jsonl")
    with open(path, "w") as f:
        # 55555 is skipped as 'already present' with no earlier terminal record.
        f.write(json.dumps({"event": "seed_skipped", "seed": 55555,
                            "reason": "already_present_on_disk"}) + "\n")
        # 66666 claims to be accepted but emits no design_accepted event, so the two
        # acceptance counters disagree.
        f.write(json.dumps({"event": "traj_end", "seed": 66666,
                            "outcome": "accept", "elapsed_s": 12.0}) + "\n")

    led = read_ledger(run_dir)
    ever_finished = any(e.get("event") == "traj_end" and e.get("seed") == 55555
                        for e in events(run_dir))
    check("self-test skipped-but-never-finished is detectable",
          55555 in {int(s) for s in led["done_seeds"]} and not ever_finished,
          "read_ledger marks it done purely on the skip, which is why verify_run's "
          "C5 must cross-check it against a terminal record from an earlier job")
    check("self-test accepted != accepted_designs is detectable",
          led["counters"]["accepted"] == 1
          and led["counters"]["accepted_designs"] == 0,
          led["counters"])


def scenario_at_stage(run_dir: str):
    """S7: an excluded trajectory is filed at the stage it was inside.

    exclusions[].at_stage used to report the last stage that *finished*, so a
    trajectory that died inside cofolding was filed against hallucination -- the
    stage before it. That is the shape of every accounting defect in this
    project: a field that is almost right, on a path nobody looks at twice.

    The check has to be able to fail. Stage 1 here closes successfully and stage 2
    raises, so the old implementation would answer "stage1_hallucination" and the
    assertion below would reject it.
    """
    print("S7  at_stage names the unclosed stage")
    rec = recorder(run_dir, "1701")

    try:
        with rec.trajectory(77777) as t:
            h = t.stage_start("stage1_hallucination")
            t.stage_end(h, outcome="pass")
            t.stage_start("stage2_cofolding")      # entered, never closed
            raise RuntimeError("cofolding blew up")
    except RuntimeError:
        pass

    rec.finish()
    rec._finalize()

    cnt = json.load(open(glob.glob(os.path.join(run_dir, "counters_*.json"))[0]))
    excl = cnt["exclusions"]
    check("S7 the trajectory is excluded", len(excl) == 1, excl)
    if excl:
        check("S7 at_stage is the stage it died in, not the last one that finished",
              excl[0].get("at_stage") == "stage2_cofolding", excl[0])

    # The unpaired start event is what makes the field recomputable after the
    # fact; its absence would turn a wrong field into a lost one.
    ev = events(run_dir)
    starts = [e for e in ev if e.get("event") == "start" and e.get("stage") == "stage2_cofolding"]
    ends = [e for e in ev if e.get("event") == "end" and e.get("stage") == "stage2_cofolding"]
    check("S7 stage2 start is logged with no matching end",
          len(starts) == 1 and len(ends) == 0, (len(starts), len(ends)))


def main() -> int:
    root = tempfile.mkdtemp(prefix="ogr1_trace_test_")
    try:
        scenario_normal(os.path.join(root, "normal"))
        scenario_time_guard(os.path.join(root, "guard"))
        scenario_resume(os.path.join(root, "resume"))
        scenario_at_stage(os.path.join(root, "at_stage"))
        if "--self-test" in sys.argv:
            self_test(os.path.join(root, "selftest"))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("PASS: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
