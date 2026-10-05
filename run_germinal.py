"""
Run Germinal for Antibody design.
"""

import os
import time
from omegaconf import DictConfig
import hydra
try:
    import pyrosetta as pr
except ImportError:
    pr = None
import numpy as np
import torch

from germinal.design.design import germinal_design
from germinal.filters import filter_utils, redesign
from germinal.utils import utils, config
from germinal.utils.io import Trajectory
from germinal.utils.ogr1_trace import NullRecorder, RunRecorder, TimeBudget


@hydra.main(version_base=None, config_path="configs", config_name="config")
def main(cfg: DictConfig):
    # process hydra configuration
    processed_cfg = config.process_config(cfg)
    # get the individual settings
    prelim_run_settings, target_settings, initial_filters, final_filters = (
        processed_cfg.values()
    )
    # initialize run. adds design_path to run_settings
    io, run_settings = config.initialize_germinal_run(
        prelim_run_settings, target_settings
    )

    io.save_run_config(run_settings, target_settings)
    # initialize pyrosetta (optional, skipped in PyRosetta-free mode)
    if pr is not None:
        pr.init(
            f"-ignore_unrecognized_res -ignore_zero_occupancy -mute all "
            f"-holes:dalphaball {run_settings['dalphaball_path']} "
            f"-corrections::beta_nov16 true -relax:default_repeats 1"
        )
    print(f"============================\nExperiment name: {run_settings['experiment_name']}\n============================")
    print(f"Processed config: {target_settings}\n{initial_filters}\n{final_filters}")

    # Initialize pre-set seeds for experiments if desired
    if run_settings["pregenerate_seeds"]:
        # Pre-generate seeds for reproducibility
        np.random.seed(1)
        run_seeds = [
            np.random.randint(2**32 - 1)
            for _ in range(run_settings["max_trajectories"])
        ]
    else:
        # Use a random initial seed for this run
        init_seed = int(time.time_ns()) % (2**32 - 1)
        print(f"Initial seed: {init_seed}")
        np.random.seed(init_seed)
    
    # ---- provenance recording (optional) -------------------------------------
    #
    # Off unless run_settings["provenance_dir"] is set. When off, NullRecorder
    # makes every call below a no-op, so there is one code path rather than one
    # per mode and the default run writes no additional files.
    #
    # Switched on by a config key rather than an environment variable so that the
    # fact that recording was active is written into final_config.yaml, which is
    # the composed, on-disk record of what the run actually used.
    #
    # Fail-closed on purpose: a mistyped key or an unwritable directory must stop
    # the run. An arm that produced no records looks exactly like a successful one
    # until the analysis stage, which is far too late.
    provenance_dir = run_settings.get("provenance_dir")
    if provenance_dir:
        rec = RunRecorder(
            run_dir=provenance_dir,
            arm=run_settings.get("arm", "unspecified"),
            target=target_settings.get("target_name", "unspecified"),
            language_model=run_settings.get("ablm_model", "unspecified"),
            scoring_backend="pyrosetta" if pr is not None else "opensource",
            container_path=os.environ.get("OGR1_CONTAINER"),
            git_commit=os.environ.get("OGR1_GIT_COMMIT"),
            config_path=str(io.layout.root / "final_config.yaml"),
            seeds_spec={
                "pregenerate_seeds": run_settings["pregenerate_seeds"],
                "max_trajectories": run_settings["max_trajectories"],
            },
            # The guard decides where a job stops, so its thresholds belong to
            # the run and are written into run_manifest.json. They are read from
            # the environment because the right values depend on how long a
            # trajectory takes on the cluster in question, and because the
            # interesting path -- run N trajectories, then stop cleanly at a seed
            # boundary -- is only reachable in a test if they can be set. With the
            # defaults, the first check alone demands 2 h 22 min of remaining wall
            # clock, so a short job can only ever exercise "refuse immediately".
            budget=TimeBudget(
                margin_s=float(os.environ.get("OGR1_MARGIN_S", 1800)),
                default_estimate_s=float(
                    os.environ.get("OGR1_DEFAULT_ESTIMATE_S", 5400)),
            ),
        )
        rec.write_manifest()
        rec.start_heartbeat()
        print(f"[ogr1] provenance recording active: {provenance_dir}")
    else:
        rec = NullRecorder()

    start_time = time.time()
    failed_design = 0
    num_accepted = 0
    num_failed = 0
    
    # =================================== Germinal hallucination loop
    for i in range(run_settings["max_trajectories"]):
        # Check termination conditions
        # n_trajectories has to be cumulative when recording: `i` restarts at 0 in
        # every job, and no on-disk artefact supplies the count either. One Step 0
        # job produced 33 trajectories, 46 CSV rows and 144 structure files, so
        # substituting rows or files would silently miscount. Seeds skipped as
        # already present are not attempts and are not counted here.
        n_attempted = rec.cumulative_counters().get("submitted", i)
        terminate, reason = io.check_termination_conditions(
            run_settings, n_trajectories=n_attempted
        )
        if terminate:
            print(reason)
            break

        # Stop at a seed boundary while a whole trajectory still fits in the time
        # that is left. Being killed mid-trajectory leaves stage-1 output on disk
        # with no terminal record, and io.check_existing_seed() cannot tell that
        # from a finished seed, so every later job would skip it forever.
        if not rec.should_continue():
            print("[ogr1] stopping at a seed boundary: not enough wall clock for "
                  "another trajectory. state.json is 'paused'; requeue to resume.")
            break

        # set seed for trajectory
        trajectory_start_time = time.time()
        if run_settings["pregenerate_seeds"]:
            seed = run_seeds[i]
        else:
            seed = int(np.random.randint(0, 999999, size=1, dtype=int)[0])

        # Unique design name
        design_name = f"{target_settings['target_name']}_{run_settings['type']}_s{seed}"

        if io.check_existing_seed(seed):
            print(f"Trajectory {i} with seed {seed} already exists. Trying new seed.")
            # Upstream decides this from output on disk, which looks the same for a
            # finished, a rejected and a truncated trajectory. Writing the decision
            # down lets verify_run.py check every skipped seed against a terminal
            # record from an earlier job (check C5).
            rec.skipped(seed, "already_present_on_disk")
            continue

        # One context manager around the whole body. Its finally clause writes the
        # terminal record unconditionally, so every existing `continue`, every early
        # return and every exception is recorded without a logging call of its own.
        with rec.trajectory(seed, design_name=design_name) as traj:
            trajectory = Trajectory(
                design_name,
                run_settings["experiment_name"],
                ",".join(map(str, run_settings["cdr_lengths"])),
                target_settings["target_hotspots"],
            )
            trajectory.set_save_location("trajectories")

            print(f"\nStarting trajectory {i + 1}: {design_name}")

            # Germinal design function
            h_stage1 = traj.stage_start("stage1_hallucination")
            design_output = germinal_design(
                design_name, run_settings, target_settings, io, seed
            )

            # retrieve hallucination status
            trajectory_metrics_last = utils.copy_dict(design_output.aux["log"])  # final log
            hallucination_success = trajectory_metrics_last.get("terminate", "") == ""
            traj.stage_end(
                h_stage1,
                outcome="pass" if hallucination_success else "fail",
                fail_reason=None if hallucination_success
                else (trajectory_metrics_last.get("terminate")
                      or "hallucination_terminated"),
            )
            # if hallucination failed, continue to next trajectory
            if not hallucination_success:
                trajectory_time = utils.get_clean_time(time.time(), trajectory_start_time)
                print(f"Trajectory took: {trajectory_time}\n")
                failed_design += 1
                num_failed += 1
                continue

            # retrieve hallucination metrics
            trajectory_metrics = utils.copy_dict(design_output._tmp["best"]["log"])
            trajectory.update_trajectory_metrics(trajectory_metrics)
            trajectory_sequence = design_output._tmp["best"]["seq"]
            target_len = design_output._target_len
            trajectory_pdb_af = str(
                io.layout.trajectories / f"structures/{design_name}.pdb"
            )
            trajectory.update_other_metrics(
                {
                    "trajectory_pdb_af": trajectory_pdb_af,
                    "trajectory_sequence": trajectory_sequence,
                    "target_len": target_len,
                }
            )
            trajectory.set_save_location("trajectories")
            # calculate time for trajectory
            trajectory_time = utils.get_clean_time(time.time(), trajectory_start_time)
            print(f"Trajectory took: {trajectory_time}\n")

            # Offload AbLang from GPU before structure prediction — frees PyTorch CUDA memory
            # for AF3/Protenix. prep_inputs() reloads it at the start of the next trajectory.
            if hasattr(design_output, 'ablm_model') and design_output.ablm_model is not None:
                design_output.ablm_model = None
                torch.cuda.empty_cache()

            # ====================================================================================
            # First filter check - cofold and check basic structural filters
            # ====================================================================================
            select_mode = run_settings.get("af3_structure_select_mode", "best")
            multi_relax = run_settings.get("multi_relax", False)

            print("Running initial cofolding filters")
            h_stage2 = traj.stage_start("stage2_cofolding")
            filter_metrics, filter_results, pass_initial_filters, final_struct, _ = (
                filter_utils.run_filters(
                    trajectory,
                    run_settings,
                    target_settings,
                    initial_filters,
                    io,
                    trajectory_sequence,
                    trajectory_pdb_af,
                    target_len,
                    select_mode=select_mode,
                    af3_seed_size=3,  # fixed for initial filters — not user-configurable
                )
            )

            utils.clear_memory(clear_jax=False)
            # The stage name has to start with 'stage2': that prefix is what drives the
            # entered_cofolding and passed_cofolding denominators.
            traj.stage_end(
                h_stage2,
                outcome="pass" if pass_initial_filters else "fail",
                fail_reason=None if pass_initial_filters
                else "initial_cofolding_filters",
            )
            if not pass_initial_filters:
                # save trajectory as not passing initial cofolding filters
                print("Trajectory not passing initial cofolding filters, skipping to next trajectory")
                complete_filter_data = {**filter_metrics, **filter_results}
                trajectory.update_filtering_metrics(complete_filter_data)
                trajectory.set_final_struct(final_struct)
                trajectory.update_other_metrics(
                    {
                        "design_time": utils.get_clean_time(
                            time.time(), trajectory_start_time
                        ),
                    }
                )
                trajectory.set_save_location("trajectories")
                io.save_trajectory(trajectory)
                num_failed += 1
                continue

            # ====================================================================================
            # AbMPNN redesign
            # ====================================================================================
            print("\nStarting AbMPNN redesign...\n")
            h_stage3 = traj.stage_start("stage3_abmpnn")
            abmpnn_sequences, abmpnn_success = redesign.run_abmpnn_redesign_pipeline(
                trajectory_pdb_af=trajectory_pdb_af,
                run_settings=run_settings,
                atom_distance_cutoff=run_settings["atom_distance_cutoff"],
            )
            # n_sequences is recorded because a redesign can succeed and still return an
            # empty list, in which case the loop below does nothing and upstream's
            # counters do not move at all.
            traj.stage_end(
                h_stage3,
                outcome="pass" if abmpnn_success else "fail",
                fail_reason=None if abmpnn_success else "abmpnn_redesign_failed",
                n_sequences=len(abmpnn_sequences) if abmpnn_sequences else 0,
            )

            if not abmpnn_success:
                print("MPNN redesign failed, skipping to next trajectory")
                continue

            # ====================================================================================
            # Final filter check - run filters on MPNN redesigned sequences
            # ====================================================================================

            # Process MPNN redesigned sequences
            if len(abmpnn_sequences) > 0:
                for j, abmpnn_sequence in enumerate(abmpnn_sequences):
                    mpnn_trajectory = trajectory.copy()
                    mpnn_trajectory.rename(f"{design_name}_abmpnn_{j + 1}")
                
                    # run final set of filters on AbMPNN redesigned sequences
                    print("Running final filters on AbMPNN redesigned sequences")
                    h_stage4 = traj.stage_start(
                        "stage4_final_filters",
                        design_name=mpnn_trajectory.design_name,
                    )
                    filter_metrics, filter_results, accepted, final_struct, _ = (
                        filter_utils.run_filters(
                            mpnn_trajectory,
                            run_settings,
                            target_settings,
                            final_filters,
                            io,
                            abmpnn_sequence["seq"],
                            trajectory_pdb_af,
                            target_len,
                            multi_relax=multi_relax,
                            select_mode=select_mode,
                            af3_seed_size=run_settings.get("num_af3_seed", 5),
                        )
                    )
                    # save trajectory
                    design_time = utils.get_clean_time(time.time(), trajectory_start_time)
                    complete_filter_data = {**filter_metrics, **filter_results}
                    mpnn_trajectory.update_filtering_metrics(complete_filter_data)
                    mpnn_trajectory.set_final_struct(final_struct)
                    mpnn_trajectory.update_other_metrics(
                        {
                            "trajectory_sequence": abmpnn_sequence["seq"],
                            "design_time": design_time,
                            "abmpnn_score": abmpnn_sequence["score"],
                            "abmpnn_seqid": abmpnn_sequence["seqid"],
                        }
                    )
                    mpnn_trajectory.set_save_location(
                        "accepted" if accepted else "redesign_candidates"
                    )
                    io.save_trajectory(mpnn_trajectory)
                    traj.stage_end(
                        h_stage4,
                        outcome="pass" if accepted else "fail",
                        fail_reason=None if accepted else "final_filters",
                    )
                    if accepted:
                        print(f"========================================")
                        print(f"Design {mpnn_trajectory.design_name} accepted!")
                        print(f"========================================")
                        num_accepted += 1
                        # Per accepted design, not per accepted trajectory. The two are
                        # different counts and both are recorded: a trajectory yields up
                        # to max_mpnn_sequences variants.
                        traj.design_accepted(mpnn_trajectory.design_name)
                    else:
                        num_failed += 1
                        traj.design_rejected(
                            mpnn_trajectory.design_name, reason="final_filters"
                        )
                    utils.clear_memory(clear_jax=False)
    
    # Writes counters.json and state.json. A 'paused' state set by
    # should_continue() is preserved and not overwritten with 'complete'.
    rec.finish()

    # print and save final run summary
    #
    # These are upstream's counters and are left exactly as they are, overlaps
    # included: a trajectory that fails hallucination increments both failed_design
    # and num_failed, num_failed also counts per-variant rejections, a trajectory
    # that fails AbMPNN redesign increments nothing at all, and `i + 1` counts one
    # trajectory that never ran whenever the loop breaks. They are reported here
    # unchanged so that the default path matches upstream; the recorder keeps a
    # separate set of mutually exclusive denominators. See CHANGELOG.
    total_runtime = utils.get_clean_time(time.time(), start_time)
    run_summary = f"Finished all designs after {i + 1} attempted trajectories.\n" \
                  f"{failed_design} designs failed initial Germinal design.\n" \
                  f"{num_failed} designs failed filters and were rejected.\n" \
                  f"{num_accepted} designs passed all filters and were accepted.\n" \
                  f"Elapsed: {total_runtime}."
    print(run_summary)
    with open(str(io.layout.root / "run_summary.txt"), "w") as f:
        f.write(run_summary)


if __name__ == "__main__":
    main()
