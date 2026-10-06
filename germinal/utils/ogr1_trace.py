#!/usr/bin/env python3
"""
ogr1_trace - run provenance recorder for the OpenGerminal BIOADV-2026-518 R1 revision.

Design goal: make it structurally impossible to lose a record. Nothing here relies on
remembering to add a log line before every `return` or `continue`.

Three guarantees:

  (1) One events file per job.
      Every job (and every Slurm requeue of that job) writes its own
      events_<jobid>_r<restart>.jsonl. Two jobs never share a file, so no job can
      truncate another job's records.

  (2) No exit path escapes without a terminal record.
      `trajectory()` is a context manager whose `finally` block always writes a
      `traj_end` event. Existing `continue` / `return` / exception / SIGTERM paths in
      run_germinal.py therefore need no modification to be recorded.

  (3) A trajectory is never started unless it can finish.
      `TimeBudget` compares the remaining wall clock against an estimate derived from
      already-observed trajectory durations. When the next trajectory would not fit,
      the run stops cleanly at a seed boundary, writes counters and a `paused` state
      file, and exits 0 so the wrapper script can requeue. Nothing is left truncated
      mid-trajectory, so the resume logic in run_germinal.py
      (`io.check_existing_seed`) never mistakes a partial output for a finished seed.

The events log is also the resume ledger. `read_ledger()` reconstructs, across all
jobs of a run, which seeds already reached a terminal state and what the cumulative
counters are. No separate checkpoint file exists, so no checkpoint can disagree with
the records.

Standard library only. Import directly inside the container.

Minimal usage (existing `continue` statements need no change):

    from ogr1_trace import RunRecorder, TimeBudget

    budget = TimeBudget(default_estimate_s=5400)     # first job has no history
    rec = RunRecorder(
        run_dir=os.environ["OGR1_RUN_DIR"],
        arm="D_ablang_opensource", target="pdl1",
        language_model="ablang", scoring_backend="opensource",
        container_path=os.environ.get("OGR1_CONTAINER"),
        git_tag=os.environ.get("OGR1_GIT_TAG"),
        config_path=cfg_path,
        budget=budget,
        equivalence_margin={"spec": "0.75 * between_seed_SD(Germinal)",
                            "prespecified_at": "2026-10-10"},
    )
    rec.write_manifest()
    rec.start_heartbeat()

    for seed in seed_list:
        if not rec.should_continue():
            break                      # out of time; state file says "paused"
        if io.check_existing_seed(seed):
            rec.skipped(seed, "already_present_on_disk")
            continue
        with rec.trajectory(seed) as traj:
            h = traj.stage_start("stage1_hallucination")
            ok, m = run_stage1(...)
            traj.stage_end(h, outcome="pass" if ok else "fail",
                           fail_reason=None if ok else "plddt_threshold", **m)
            if not ok:
                continue               # the finally block still writes traj_end
            ...
            traj.accept(n_variants=4)

    rec.finish()                       # writes counters + state file
"""

from __future__ import annotations

import atexit
import glob
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA_MANIFEST = "ogr1-manifest/1"
SCHEMA_EVENTS = "ogr1-events/1"
SCHEMA_COUNTERS = "ogr1-counters/1"
SCHEMA_STATE = "ogr1-state/1"

# Closed vocabulary for `outcome`. Validated on write: free text here is what left the
# previous round with only three undifferentiated failure tallies and no way to
# reclassify after the fact.
OUTCOMES = {"pass", "fail", "error", "accept", "skip"}

# Terminal states that mean "this seed has been dealt with, do not run it again".
# `skip` is deliberately excluded: a seed skipped because the job ran out of time or
# was preempted has not been attempted and must be retried by the next job.
DONE_OUTCOMES = {"pass", "fail", "accept"}


class JobTerminated(BaseException):
    """Raised inside the signal handler on SIGTERM / SIGINT.

    Subclasses BaseException, not Exception, so that a caller's `except Exception`
    cannot swallow it. Python then unwinds normally and the `finally` block in
    `trajectory()` gets to write the terminal record.

    An earlier implementation called os.kill() from the handler; the `with` block's
    finally never ran, and a smoke test showed only a `signal` event with no
    `traj_end` and no counters file.
    """
    pass


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sha256(path: str, chunk: int = 1 << 20):
    """Handles multi-gigabyte containers. Returns None on failure rather than
    raising, so a hashing problem cannot abort a run."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception:
        return None


def _gpu_info() -> dict:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,uuid,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip().splitlines()
        gpus = []
        for line in out:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                gpus.append({"name": parts[0], "uuid": parts[1], "driver": parts[2]})
        return {"gpus": gpus, "n_gpu": len(gpus)}
    except Exception:
        return {"gpus": [], "n_gpu": 0}


def _norm_seed(seed):
    """Seeds come back from JSON as int, but a caller may pass numpy integers or
    strings. Normalise so set membership is reliable across jobs."""
    return str(int(seed)) if isinstance(seed, (int, float)) else str(seed)


# ----------------------------------------------------------------------------------
# Ledger: the events log is the only source of resume state.
# ----------------------------------------------------------------------------------

def read_ledger(run_dir: str) -> dict:
    """Scan every events_*.jsonl under run_dir and reconstruct cross-job state.

    Returns a dict with:
      done_seeds        set of normalised seeds with a terminal outcome in DONE_OUTCOMES
      retry_seeds       set of seeds whose only terminal record is `skip` or `error`
                        (interrupted or crashed; must be attempted again)
      accepted_seeds    list, in order of acceptance
      durations         list of traj_end elapsed_s for trajectories that ran to a
                        pipeline conclusion (used for the time estimate)
      counters          cumulative denominators across jobs
      n_jobs            how many event files contributed
      malformed_lines   count of unparsable lines (should be 0; a non-zero value means
                        a job was SIGKILLed mid-line, which is expected at most once
                        per job)

    A seed may appear in several jobs (skipped in one, completed in a later one).
    DONE wins over retry: membership in done_seeds is what suppresses a re-run.
    """
    done, retry, accepted, durations = set(), set(), [], []
    counters = dict(submitted=0, completed=0, entered_cofolding=0,
                    passed_cofolding=0, accepted=0, accepted_designs=0)
    files = sorted(glob.glob(os.path.join(run_dir, "events_*.jsonl")))
    malformed = 0
    for path in files:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                ev = e.get("event")
                if ev == "traj_end":
                    s = _norm_seed(e["seed"])
                    outcome = e.get("outcome")
                    if outcome in DONE_OUTCOMES:
                        done.add(s)
                        retry.discard(s)
                        if isinstance(e.get("elapsed_s"), (int, float)):
                            durations.append(float(e["elapsed_s"]))
                        counters["completed"] += 1
                        if outcome == "accept":
                            counters["accepted"] += 1
                            accepted.append(e["seed"])
                    elif s not in done:
                        retry.add(s)
                    counters["submitted"] += 1
                elif ev == "seed_skipped":
                    # Recorded when run_germinal's own existence check fires. The seed
                    # is on disk from an earlier job; treat it as done so the count of
                    # trajectories attempted stays honest.
                    s = _norm_seed(e["seed"])
                    done.add(s)
                    retry.discard(s)
                elif ev == "design_accepted":
                    # Counted per accepted design, not per trajectory. Kept separate
                    # from `accepted` on purpose; see _Trajectory.design_accepted.
                    counters["accepted_designs"] += 1
                elif ev == "start" and str(e.get("stage", "")).startswith("stage2"):
                    counters["entered_cofolding"] += 1
                elif (ev == "end" and str(e.get("stage", "")).startswith("stage2")
                      and e.get("outcome") == "pass"):
                    counters["passed_cofolding"] += 1
    return dict(done_seeds=done, retry_seeds=retry, accepted_seeds=accepted,
                durations=durations, counters=counters, n_jobs=len(files),
                malformed_lines=malformed)


def seeds_to_run(seed_list, run_dir: str) -> list:
    """Deterministic seed list minus the seeds already dealt with.

    Order is preserved, so a longer seed list is a strict superset of a shorter one
    (numpy's sequential draws from a fixed master seed are prefix-stable). Extending
    the budget therefore never invalidates work already done.
    """
    done = read_ledger(run_dir)["done_seeds"]
    return [s for s in seed_list if _norm_seed(s) not in done]


# ----------------------------------------------------------------------------------
# Time budget: never start a trajectory that cannot finish before the wall clock.
# ----------------------------------------------------------------------------------

class TimeBudget:
    """Decides whether there is enough wall clock left for one more trajectory.

    Deadline resolution order (first that yields a value wins):
      1. `deadline_epoch` argument
      2. OGR1_DEADLINE_EPOCH environment variable (set by the sbatch wrapper; the
         most explicit and the least dependent on Slurm version)
      3. SLURM_JOB_END_TIME environment variable (epoch seconds; recent Slurm only)
      4. `scontrol show job $SLURM_JOB_ID` EndTime field
      5. None, in which case the guard is inactive and a warning is recorded

    The estimate for one trajectory is a high quantile (default p90) of durations
    already observed for this run, across all its jobs, multiplied by `safety_factor`.
    With no history, `default_estimate_s` is used. p90 rather than the median because
    the cost of underestimating (a truncated trajectory) is much higher than the cost
    of overestimating (an idle tail of one job).
    """

    def __init__(self, deadline_epoch=None, margin_s: float = 1800,
                 safety_factor: float = 1.25, quantile: float = 0.90,
                 default_estimate_s: float = 5400):
        self.margin_s = float(margin_s)
        self.safety_factor = float(safety_factor)
        self.quantile = float(quantile)
        self.default_estimate_s = float(default_estimate_s)
        self.deadline_epoch, self.deadline_source = self._resolve(deadline_epoch)
        self._durations: list[float] = []

    # -- deadline ---------------------------------------------------------------

    @staticmethod
    def _resolve(explicit):
        if explicit:
            return float(explicit), "argument"
        env = os.environ.get("OGR1_DEADLINE_EPOCH")
        if env:
            try:
                return float(env), "OGR1_DEADLINE_EPOCH"
            except ValueError:
                pass
        env = os.environ.get("SLURM_JOB_END_TIME")
        if env:
            try:
                return float(env), "SLURM_JOB_END_TIME"
            except ValueError:
                pass
        jobid = os.environ.get("SLURM_JOB_ID")
        if jobid:
            try:
                out = subprocess.run(["scontrol", "show", "job", jobid],
                                     capture_output=True, text=True,
                                     timeout=20).stdout
                m = re.search(r"EndTime=(\S+)", out)
                if m and m.group(1) not in ("Unknown", "None"):
                    t = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
                    return t.timestamp(), "scontrol"
            except Exception:
                pass
        return None, "unavailable"

    # -- estimate ---------------------------------------------------------------

    def prime(self, durations):
        """Seed the estimator with durations from previous jobs of this run."""
        self._durations = [float(d) for d in durations if d and d > 0]

    def observe(self, elapsed_s: float):
        if elapsed_s and elapsed_s > 0:
            self._durations.append(float(elapsed_s))

    def estimate_s(self) -> float:
        if not self._durations:
            return self.default_estimate_s * self.safety_factor
        xs = sorted(self._durations)
        # Nearest-rank quantile; with a handful of samples this is deliberately
        # pessimistic (it lands on the largest observation).
        idx = min(len(xs) - 1, max(0, int(round(self.quantile * (len(xs) - 1)))))
        return xs[idx] * self.safety_factor

    # -- decision ---------------------------------------------------------------

    def check(self) -> dict:
        """Returns a dict describing the decision. `ok` False means stop now."""
        est = self.estimate_s()
        if self.deadline_epoch is None:
            return dict(ok=True, reason="no_deadline_known", estimate_s=round(est, 1),
                        deadline_source=self.deadline_source, n_observed=len(self._durations))
        remaining = self.deadline_epoch - time.time()
        usable = remaining - self.margin_s
        return dict(ok=usable >= est,
                    reason="fits" if usable >= est else "insufficient_time",
                    remaining_s=round(remaining, 1),
                    usable_s=round(usable, 1),
                    estimate_s=round(est, 1),
                    margin_s=self.margin_s,
                    n_observed=len(self._durations),
                    deadline_source=self.deadline_source)

    def params(self) -> dict:
        """The settings this guard is deciding with, for the manifest.

        These have to be archived with the run. The guard decides where a job stops,
        so "which settings produced this pause" is part of the run's provenance; a
        paused state file whose thresholds are unknown cannot be interpreted later.
        """
        return dict(margin_s=self.margin_s,
                    safety_factor=self.safety_factor,
                    quantile=self.quantile,
                    default_estimate_s=self.default_estimate_s,
                    deadline_epoch=self.deadline_epoch,
                    deadline_source=self.deadline_source)


# ----------------------------------------------------------------------------------
# Per-trajectory handle
# ----------------------------------------------------------------------------------

class _Trajectory:
    """Handle for one trajectory. Produced by RunRecorder.trajectory(); do not
    construct directly."""

    def __init__(self, rec: "RunRecorder", seed):
        self._rec = rec
        self.seed = seed
        self._t0 = time.monotonic()
        self._stages: list[dict] = []
        # `entered` must be tracked separately from `stages`. If a stage raises, its
        # stage_end never runs and the stage is absent from `stages`, yet its `start`
        # event is already in the log. A counter built from `stages` would then
        # disagree with the events file (a smoke test measured entered_cofolding=2
        # against three stage2 start events). Denominators must count entry.
        self._entered: set[str] = set()
        self._terminal = None            # set by accept() / fail() / skip()
        self._fail_reason = None
        self._extra: dict = {}

    # -- stages ----------------------------------------------------------------

    def stage_start(self, stage: str, **fields):
        self._entered.add(stage)
        self._rec._emit({"event": "start", "seed": self.seed, "stage": stage, **fields})
        return {"stage": stage, "t0": time.monotonic()}

    def stage_end(self, handle: dict, outcome: str, fail_reason=None, **fields):
        if outcome not in OUTCOMES:
            raise ValueError(
                f"outcome={outcome!r} is not in the closed set {sorted(OUTCOMES)}. "
                "Free text cannot be classified after the fact, which is why the "
                "previous round ended up with three undifferentiated failure tallies."
            )
        if outcome == "fail" and not fail_reason:
            raise ValueError(
                "outcome='fail' requires fail_reason, and it must name the specific "
                "threshold (for example 'i_pae_threshold'), not free text."
            )
        elapsed = time.monotonic() - handle["t0"]
        d = {"event": "end", "seed": self.seed, "stage": handle["stage"],
             "elapsed_s": round(elapsed, 3), "outcome": outcome}
        if fail_reason:
            d["fail_reason"] = fail_reason
        d.update(fields)
        self._rec._emit(d)
        self._stages.append({"stage": handle["stage"], "elapsed_s": elapsed,
                             "outcome": outcome, "fail_reason": fail_reason})
        # Inferring the trajectory terminal from a stage outcome is a convenience for
        # the single-variant gates (stage 1 and stage 2), where run_germinal.py just
        # `continue`s and never calls fail() explicitly.
        #
        # It must not downgrade an acceptance. Stage 4 runs once per redesigned
        # variant, so a trajectory routinely ends with a rejected variant after an
        # accepted one; letting that last `fail` overwrite the terminal recorded
        # `accepted = 0` while accepted_designs was 2. With max_mpnn_sequences = 4 and
        # an acceptance rate near 10%, almost every accepted trajectory carries at
        # least one rejected variant, so this would have driven the primary outcome of
        # the 2x2 systematically to zero. Caught by test_ogr1_trace.py scenario S2.
        #
        # `error` is still allowed to win: an exception is not a pipeline conclusion,
        # and the exception path in RunRecorder.trajectory() sets it regardless.
        if outcome == "error":
            self._terminal = outcome
            self._fail_reason = fail_reason
        elif outcome == "fail" and self._terminal != "accept":
            self._terminal = outcome
            self._fail_reason = fail_reason

    @contextmanager
    def stage(self, name: str, **fields):
        """`with` form. An exception is recorded as outcome='error' and re-raised."""
        h = self.stage_start(name, **fields)
        try:
            yield h
        except Exception as e:
            self.stage_end(h, outcome="error", fail_reason=None,
                           exception=type(e).__name__, message=str(e)[:500])
            raise
        else:
            if not any(s["stage"] == name for s in self._stages):
                self.stage_end(h, outcome="pass")

    # -- terminal states -------------------------------------------------------

    def accept(self, **fields):
        self._terminal = "accept"
        self._extra.update(fields)

    def fail(self, reason: str, **fields):
        self._terminal = "fail"
        self._fail_reason = reason
        self._extra.update(fields)

    def skip(self, reason: str, **fields):
        """Removal for reasons outside the pipeline: node failure, preemption, OOM.
        Goes into exclusions and is retried by a later job."""
        self._terminal = "skip"
        self._fail_reason = reason
        self._extra.update(fields)

    # -- per-design accounting -------------------------------------------------
    #
    # One trajectory yields up to `max_mpnn_sequences` redesigned variants, each of
    # which is accepted or rejected on its own. "Number of accepted designs" and
    # "number of trajectories with at least one accepted design" are therefore two
    # different quantities, and conflating them is exactly the denominator ambiguity
    # this recorder exists to remove: in the archived data pdl1_original holds 16
    # accepted designs spread over 7 seeds, and one Step 0 job produced 33
    # trajectories, 46 CSV rows and 144 structure files.
    #
    # `accepted` stays per-trajectory so that the funnel
    #   submitted >= completed >= entered_cofolding >= passed_cofolding >= accepted
    # remains monotone and means something. `accepted_designs` counts rows in
    # accepted/designs.csv. Both are recorded; neither is derived from the other.

    def design_accepted(self, design_name: str, **fields):
        """One accepted design (variant). Also marks the trajectory as accepted."""
        self._terminal = "accept"
        self._rec._counters["accepted_designs"] += 1
        self._rec._emit({"event": "design_accepted", "seed": self.seed,
                         "design_name": design_name, **fields})

    def design_rejected(self, design_name: str, reason: str = "final_filters",
                        **fields):
        """One rejected design (variant). Does not change the trajectory outcome.

        Recorded so that variant-level pass rates can be recomputed from the ledger
        instead of from a counter that mixes trajectory-level and variant-level
        events, which is what run_germinal.py's own `num_failed` does."""
        self._rec._emit({"event": "design_rejected", "seed": self.seed,
                         "design_name": design_name, "reason": reason, **fields})


# ----------------------------------------------------------------------------------
# No-op recorder
# ----------------------------------------------------------------------------------


class _NullTrajectory:
    """Accepts every call a real trajectory handle accepts and records nothing."""

    def stage_start(self, stage, **fields):
        return None

    def stage_end(self, handle, outcome, fail_reason=None, **fields):
        pass

    @contextmanager
    def stage(self, name, **fields):
        yield None

    def accept(self, **fields):
        pass

    def fail(self, reason, **fields):
        pass

    def skip(self, reason, **fields):
        pass

    def design_accepted(self, design_name, **fields):
        pass

    def design_rejected(self, design_name, reason="final_filters", **fields):
        pass


class NullRecorder:
    """Stand-in used when provenance recording is switched off.

    It exists so that run_germinal.py has ONE code path rather than one per mode. The
    alternative -- guarding each call site with `if rec is not None` -- means the
    benchmarked path and the default path differ at four places, and a divergence
    between what we measure and what a third party runs is the failure this whole
    recorder was written to prevent.

    `cumulative_counters()` returns an empty dict so that callers can write
    `rec.cumulative_counters().get("submitted", i)` and fall back to the in-process
    loop index unchanged when recording is off.
    """

    def should_continue(self) -> bool:
        return True

    def skipped(self, seed, reason: str, **fields):
        pass

    def cumulative_counters(self) -> dict:
        return {}

    def write_manifest(self):
        pass

    def start_heartbeat(self):
        pass

    def write_counters(self, extra=None):
        pass

    def write_state(self, state: str, detail=None):
        pass

    def finish(self, state: str = "complete", detail=None):
        pass

    @contextmanager
    def trajectory(self, seed, **fields):
        yield _NullTrajectory()


# ----------------------------------------------------------------------------------
# Recorder
# ----------------------------------------------------------------------------------

class RunRecorder:
    def __init__(self, run_dir, arm, target, language_model, scoring_backend,
                 container_path=None, git_tag=None, git_commit=None,
                 config_path=None, hydra_overrides=None, binds=None,
                 seeds_spec=None, equivalence_margin=None,
                 budget: "TimeBudget | None" = None, heartbeat_s=60):
        self.run_dir = os.path.abspath(run_dir)
        os.makedirs(self.run_dir, exist_ok=True)

        self.jobid = os.environ.get("SLURM_JOB_ID", f"local{os.getpid()}")
        self.restart = os.environ.get("SLURM_RESTART_COUNT", "0")

        # One file per job, including per requeue. Two jobs never share a file.
        self.events_path = os.path.join(
            self.run_dir, f"events_{self.jobid}_r{self.restart}.jsonl")
        self.manifest_path = os.path.join(
            self.run_dir, f"run_manifest_{self.jobid}_r{self.restart}.json")
        self.counters_path = os.path.join(
            self.run_dir, f"counters_{self.jobid}_r{self.restart}.json")
        self.state_path = os.path.join(self.run_dir, "state.json")

        self._meta = dict(
            arm=arm, target=target, language_model=language_model,
            scoring_backend=scoring_backend,
            container_path=container_path, git_tag=git_tag, git_commit=git_commit,
            config_path=config_path, hydra_overrides=hydra_overrides or [],
            binds=binds or [], seeds_spec=seeds_spec or {},
            equivalence_margin=equivalence_margin or {},
        )

        self._lock = threading.Lock()
        self._fh = open(self.events_path, "a", buffering=1)   # line buffered
        self._counters = dict(submitted=0, completed=0, entered_cofolding=0,
                              passed_cofolding=0, accepted=0, accepted_designs=0)
        self._exclusions: list[dict] = []
        self._accepted_seeds: list = []
        self._skipped: list[dict] = []
        self._t_start = time.monotonic()
        self._current_seed = None
        self._hb_stop = threading.Event()
        self._hb_s = heartbeat_s
        self._closed = False
        self._signalled = None
        self._counters_written = False
        self._state = "running"
        self._pause_info = None

        # Prime the time estimator from what previous jobs of this run observed.
        self.budget = budget
        self._prior = read_ledger(self.run_dir)
        if self.budget is not None:
            self.budget.prime(self._prior["durations"])

        # Slurm sends SIGTERM on timeout or preemption and waits KillWait (30 s by
        # default) before SIGKILL. That is ample time to write a terminal record and
        # fsync it.
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                pass            # not the main thread, or unsupported
        atexit.register(self._on_exit)

    # -- cross-job view --------------------------------------------------------

    @property
    def prior(self) -> dict:
        """Ledger state as of this job's start. Use `cumulative_counters()` for
        totals that include the current job."""
        return self._prior

    def cumulative_counters(self) -> dict:
        """Prior jobs plus this job. This is what a cap such as
        max_hallucinated_trajectories must be compared against; the in-process
        counters in run_germinal.py reset on every job."""
        out = dict(self._prior["counters"])
        for k, v in self._counters.items():
            out[k] = out.get(k, 0) + v
        return out

    # -- durability ------------------------------------------------------------

    def _emit(self, d: dict):
        """One JSON object per line, flushed and fsynced immediately.

        Cost: one fsync per event, a few per trajectory, low thousands per run.
        Benefit: a SIGKILL loses at most the line being written."""
        rec = {"ts": _utc(), "schema": SCHEMA_EVENTS,
               "job_id": self.jobid, "restart": self.restart, **d}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            if self._closed:
                return
            self._fh.write(line + "\n")
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass

    @staticmethod
    def _atomic_json(path: str, payload: dict):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)      # atomic; never leaves a half-written file

    def _container_block(self) -> dict:
        path = self._meta["container_path"]
        declared = os.environ.get("OGR1_CONTAINER_SHA256") or None
        if declared:
            return {"path": path, "sha256": declared,
                    "sha256_source": "OGR1_CONTAINER_SHA256"}
        digest = _sha256(path) if path else None
        return {"path": path, "sha256": digest,
                "sha256_source": "hashed_at_runtime" if digest else "unavailable"}

    def write_manifest(self):
        m = self._meta
        man = {
            "schema": SCHEMA_MANIFEST,
            "run_id": f"ogr1_{m['target']}_{m['arm']}_{self.jobid}_r{self.restart}",
            "written_at_utc": _utc(),
            "arm": m["arm"], "target": m["target"],
            "language_model": m["language_model"],
            "scoring_backend": m["scoring_backend"],
            # The container's own host path is not visible from inside the container, so
            # hashing it here returns None -- silently losing the single most important
            # provenance field. The launching script has already computed and verified
            # the digest, so it is passed in through OGR1_CONTAINER_SHA256 and preferred;
            # hashing is the fallback for a run started outside a container. Which of the
            # two was used is recorded, so the value is never of unknown origin.
            "container": self._container_block(),
            "source": {"git_tag": m["git_tag"], "git_commit": m["git_commit"]},
            "config": {
                "resolved_path": m["config_path"],
                "sha256": _sha256(m["config_path"]) if m["config_path"] else None,
                "hydra_overrides": m["hydra_overrides"],
            },
            "binds": m["binds"],
            "slurm": {
                "job_id": self.jobid,
                "job_name": os.environ.get("SLURM_JOB_NAME"),
                "restart_count": self.restart,
                "node": os.environ.get("SLURMD_NODENAME", socket.gethostname()),
                "partition": os.environ.get("SLURM_JOB_PARTITION"),
                "account": os.environ.get("SLURM_JOB_ACCOUNT"),
                "array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID"),
            },
            "hardware": _gpu_info(),
            "python": sys.version.split()[0],
            "seeds": m["seeds_spec"],
            "equivalence_margin": m["equivalence_margin"],
            "resume": {
                "prior_jobs": self._prior["n_jobs"],
                "seeds_already_done": len(self._prior["done_seeds"]),
                "seeds_to_retry": len(self._prior["retry_seeds"]),
                "prior_counters": self._prior["counters"],
                "malformed_lines_in_prior_logs": self._prior["malformed_lines"],
            },
            # Two different things, both needed. `params` is what the guard will decide
            # with for the whole job; `at_start` is the decision it would have made at
            # t=0 with no observed durations yet. Only the first explains a later pause.
            #
            # This used to be a single key holding `check()` alone, and an attempt to
            # add a second "time_budget" key earlier in this same dict literal was
            # silently discarded -- duplicate keys in a dict literal are legal Python
            # and the last one wins, with no error. Caught by test_ogr1_trace.py S3.
            "time_budget": {
                "params": self.budget.params() if self.budget is not None else None,
                "at_start": self.budget.check() if self.budget is not None else None,
            },
        }
        self._atomic_json(self.manifest_path, man)
        self._emit({"event": "run_start",
                    "manifest": os.path.basename(self.manifest_path),
                    "prior_jobs": self._prior["n_jobs"],
                    "seeds_already_done": len(self._prior["done_seeds"])})
        if self.budget is not None and self.budget.deadline_epoch is None:
            self._emit({"event": "warning",
                        "message": "no wall-clock deadline could be determined; the "
                                   "time-budget guard is inactive. Set "
                                   "OGR1_DEADLINE_EPOCH in the sbatch script."})
        return man

    def write_counters(self, extra=None):
        c = {
            "schema": SCHEMA_COUNTERS,
            "run_id": f"ogr1_{self._meta['target']}_{self._meta['arm']}_"
                      f"{self.jobid}_r{self.restart}",
            "written_at_utc": _utc(),
            "denominators": dict(self._counters),
            "cumulative_denominators": self.cumulative_counters(),
            "exclusions": self._exclusions,
            "seeds_skipped_as_present": self._skipped,
            "accepted_design_seeds": self._accepted_seeds,
            "wall_clock_s": round(time.monotonic() - self._t_start, 1),
            "state": self._state,
            "pause_info": self._pause_info,
        }
        if extra:
            c.update(extra)
        self._atomic_json(self.counters_path, c)
        self._counters_written = True
        self._emit({"event": "run_end", "state": self._state, **c["denominators"]})
        return c

    def write_state(self, state: str, detail=None):
        """Single file, overwritten by each job, telling the wrapper script what to
        do next: 'paused' means requeue, 'complete' means stop, 'failed' means a
        human should look."""
        self._state = state
        self._atomic_json(self.state_path, {
            "schema": SCHEMA_STATE,
            "state": state,
            "written_at_utc": _utc(),
            "job_id": self.jobid, "restart": self.restart,
            "detail": detail or {},
            "cumulative_denominators": self.cumulative_counters(),
        })

    def finish(self, state: str = "complete", detail=None):
        """Call once the seed loop exits normally. `state` should be 'complete' when
        the seed list is exhausted; `should_continue()` sets 'paused' itself."""
        if self._state != "paused":
            self.write_state(state, detail)
        self.write_counters()

    # -- time budget -----------------------------------------------------------

    def should_continue(self) -> bool:
        """Call immediately before starting each trajectory.

        False means: there is not enough wall clock left for another trajectory.
        Counters and a 'paused' state file are written, so the caller should break out
        of the loop and exit 0. The next job resumes at this seed boundary with
        nothing left truncated."""
        if self.budget is None:
            return True
        info = self.budget.check()
        if info["ok"]:
            return True
        self._pause_info = info
        self._emit({"event": "run_paused", **info,
                    "note": "stopping at a seed boundary before the wall clock; "
                            "requeue to continue"})
        self.write_state("paused", detail=info)
        return False

    def skipped(self, seed, reason: str, **fields):
        """Record that a seed was not run because it is already present.

        run_germinal.py's `io.check_existing_seed()` decides this by looking for
        output on disk, which cannot distinguish a finished trajectory from a
        truncated one. Writing the decision into the ledger makes it auditable:
        verify_run.py can then cross-check every skipped seed against a terminal
        record from an earlier job, and flag any seed that was skipped without ever
        having finished."""
        self._skipped.append({"seed": seed, "reason": reason,
                              "job_id": self.jobid, "restart": self.restart})
        self._emit({"event": "seed_skipped", "seed": seed, "reason": reason, **fields})

    # -- trajectory ------------------------------------------------------------

    @contextmanager
    def trajectory(self, seed, **fields):
        """Core guarantee: the finally block writes a terminal record unconditionally.

        Existing `continue` / `return` statements and any exception all trigger
        __exit__, so no early-exit path needs its own logging call."""
        self._counters["submitted"] += 1
        self._current_seed = seed
        t = _Trajectory(self, seed)
        self._emit({"event": "traj_start", "seed": seed, **fields})
        try:
            yield t
        except JobTerminated as e:
            # Slurm timeout or preemption. Neither a pipeline conclusion nor a code
            # defect, so it is recorded as `skip` with an explicit reason and stays
            # out of DONE_OUTCOMES: the next job retries this seed.
            t._terminal = "skip"
            t._fail_reason = f"slurm_{str(e).lower()}"
            raise
        except BaseException as e:
            t._terminal = "error"
            t._fail_reason = type(e).__name__
            t._extra["traceback"] = traceback.format_exc()[-2000:]
            raise
        finally:
            elapsed = time.monotonic() - t._t0
            outcome = t._terminal or "pass"
            self._emit({"event": "traj_end", "seed": seed,
                        "elapsed_s": round(elapsed, 3), "outcome": outcome,
                        **({"fail_reason": t._fail_reason} if t._fail_reason else {}),
                        "n_stages": len(t._stages), **t._extra})
            # Denominators must cross-check against the events file.
            #
            # `completed` excludes both error and skip: a trajectory cut short by an
            # exception or by an external cause (node failure, preemption, OOM) never
            # reached a pipeline conclusion. Only then does
            # submitted - completed == len(exclusions) hold; a smoke test measured
            # 1 against 2 when skip was counted as completed as well.
            if outcome not in ("error", "skip"):
                self._counters["completed"] += 1
            # Count entry, not recorded stages: a stage interrupted by an exception
            # never reaches stage_end and so never lands in `stages`.
            if any(s.startswith("stage2") for s in t._entered):
                self._counters["entered_cofolding"] += 1
            if any(s["stage"].startswith("stage2") and s["outcome"] == "pass"
                   for s in t._stages):
                self._counters["passed_cofolding"] += 1
            if outcome == "accept":
                self._counters["accepted"] += 1
                self._accepted_seeds.append(seed)
            if outcome in ("error", "skip"):
                self._exclusions.append({
                    "seed": seed, "reason": t._fail_reason or outcome,
                    "at_stage": t._stages[-1]["stage"] if t._stages else None,
                    "job_id": self.jobid, "restart": self.restart,
                })
            # Feed the estimator only with trajectories that ran to a conclusion; an
            # interrupted one would bias the estimate downwards and defeat the guard.
            if self.budget is not None and outcome in DONE_OUTCOMES:
                self.budget.observe(elapsed)
            self._current_seed = None

    # -- heartbeat and signals -------------------------------------------------

    def start_heartbeat(self):
        """One line every heartbeat_s. Even after a hard SIGKILL the log shows which
        seed was in flight."""
        def loop():
            while not self._hb_stop.wait(self._hb_s):
                info = {}
                if self.budget is not None and self.budget.deadline_epoch:
                    info["deadline_in_s"] = round(
                        self.budget.deadline_epoch - time.time(), 1)
                self._emit({"event": "heartbeat", "seed": self._current_seed,
                            "uptime_s": round(time.monotonic() - self._t_start, 1),
                            **info})
        th = threading.Thread(target=loop, daemon=True, name="ogr1-heartbeat")
        th.start()
        return th

    def _on_signal(self, signum, frame):
        name = signal.Signals(signum).name
        self._emit({"event": "signal", "signal": name,
                    "seed_in_flight": self._current_seed,
                    "note": "Slurm timeout or preemption; no trajectory after this "
                            "line was run in this job."})
        if self._signalled:        # second signal: stop being graceful
            self._finalize()
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
            return
        self._signalled = name
        # Raise rather than kill, so Python unwinds, trajectory()'s finally writes the
        # terminal record, and atexit writes the counters. Slurm's KillWait (30 s by
        # default) leaves enough time.
        raise JobTerminated(name)

    def _on_exit(self):
        """atexit: counters land even if the caller never called finish()."""
        if not self._counters_written and not self._closed:
            try:
                if self._state == "running":
                    # Neither completed nor paused deliberately: the job was cut off.
                    # Mark it for requeue rather than as complete.
                    self.write_state("interrupted",
                                     detail={"terminated_by": self._signalled})
                self.write_counters(
                    extra={"terminated_by": self._signalled} if self._signalled else None)
            except Exception:
                pass
        self._finalize()

    def _finalize(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._hb_stop.set()
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()
            except Exception:
                pass
