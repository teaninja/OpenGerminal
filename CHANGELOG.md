# Changelog

All notable changes to OpenGerminal are recorded here. Versions follow
[semantic versioning](https://semver.org/). A released version number is
immutable: it keeps pointing at one artefact, and a correction is published as
a new version rather than as a replacement of an old one.

Container identity is the SHA-256 digest of the `.sif` file. Image sizes and
timestamps are not identity: two generations of the benchmark image have been
observed to have byte-identical sizes.

---

## [1.1.1] — unreleased

Documentation, bundled licence texts and configurations. **No file that is
executed at run time differs from 1.1.0.** The benchmark runs reported in the
accompanying manuscript bound their own copies of the filter configurations and
archived them next to their data; those copies are byte-identical to the files
added here, so the results are unaffected by this release.

### Added

- `germinal/filters/LICENSES/` — the upstream licence texts for the three
  binaries this image redistributes:
  - `FASPR.LICENSE` (MIT, (c) 2020 Xiaoqiang Huang)
  - `sc-rs.LICENSE` (MIT, (c) 2025)
  - `dun2010bbdep.NOTICE` — the provenance of the Dunbrack 2010
    backbone-dependent rotamer library (`germinal/filters/dun2010bbdep.bin`,
    13.7 MB), together with a statement that the two upstream sources for its
    terms disagree. Attribution is given because it is required under one
    reading and harmless under the other.
- Acknowledgment of FASPR, sc-rs and the Dunbrack laboratory in the README.
- `configs/target/` and `configs/run/` and `configs/filter/{initial,final}/`
  entries for all five benchmarked targets. Version 1.1.0 shipped
  configurations for PD-L1 and IL-3 only; the configurations for insulin,
  EHD2 and caveolin-1 existed only inside the run directories of the benchmark.

### Fixed

- The README stated that FASPR is distributed under Apache 2.0. The upstream
  LICENSE file states MIT, and the upstream README states Apache 2.0; the two
  disagree. The LICENSE file is taken as governing, and both readings permit
  commercial use. The README now records both and names the governing one.
- The shipped final filter sets for PD-L1 and IL-3 carried two gates,
  `ipsae` and `pdockq2`, on metrics this pipeline never computes. A third party
  running the shipped defaults would have seen every design rejected. Both
  gates are removed. No threshold was changed: the shipped files are now
  byte-identical to the gate sets the benchmark runs actually used, which were
  derived from the shipped files by deleting whole blocks only.

### Known limitations

These change behaviour at run time and are therefore deliberately **not**
addressed in this release, which would otherwise no longer be a
documentation-only change. They are located to the line and scheduled for
1.2.0.

- `pr_alternative_utils._calculate_shape_complementarity` returns a hard-coded
  `0.70` when the `sc` binary cannot be run or its output cannot be parsed.
  `interface_shape_comp` is a final-stage gate, so a silent fallback puts a
  value inside the legal range in front of a threshold. The fallback is not
  distinguishable from a genuine measurement at the value level, because the
  column is rounded to two decimals. In the runs reported in the manuscript
  there is no evidence that it was triggered: the frequency of `0.70` is not
  elevated in the open-source arms relative to the PyRosetta arms, which never
  call `sc` at all. The fix is to raise, or to write an explicit sentinel.
- `filter_utils.evaluate_filters` writes `False` for a filter whose metric is
  absent from the metric dictionary and prints only a warning. A misspelled
  gate name therefore produces a gate that can never pass, for a whole arm,
  with no error. The fix is to raise.
- Three interface quantities have no open-source replacement and are set to
  constants in this image: `binder_score = -1.0`, `interface_dG = -10.0` and
  `interface_hbonds = 5`. One of them, `interface_hbonds`, is one of the
  thirteen final gates, so that gate cannot fail in this image. This is stated
  in `%help` and in the manuscript; it is repeated here because it is a
  property of the distributed artefact, not only of the benchmark.
- `max_trajectories` is evaluated against a per-process counter and is
  therefore a per-job limit, not a cumulative one, unless the provenance
  recorder is enabled (`provenance_dir`). `max_passing_designs` and
  `max_hallucinated_trajectories` read from disk and are cumulative.
  `max_hallucinated_trajectories` counts **rows** of
  `all_trajectories.csv`, not trajectories; with `max_mpnn_sequences > 1`
  a trajectory contributes several rows.

---

## [1.1.0] — 2026-10-17

The images used for the benchmark reported in the accompanying manuscript.
Archived for provenance. Three images were built, of which only the release
image is distributable.

| image | SHA-256 | distributable |
|---|---|---|
| `opengerminal_v1.1.0.sif` (release, arm D) | `225a7faea5c1187c26b00dc2245e704222c645d735a1c79e0bae2fb4fd07f2f7` | yes |
| `opengerminal_v1.1.0_bench.sif` (arm C) | `e142bd73b27be771295a69bfd88dc9b032cf82b9b1b8daf67625e3188bea388e` | no — contains IgLM, under the JHU Academic Software License Agreement, which is limited to non-profit academic and research institutions |
| `germinal_ref_1e1c1a5.sif` (arms A and B) | `ddca59d9c5bbcc4e25a8c3be1e5abcbfd3c5e1018f757c62cb8927829ff24952` | no — contains PyRosetta, whose non-commercial licence does not permit redistribution |

The asymmetry in that table is itself an instance of the licensing barrier this
work exists to remove: the reference implementation cannot be shipped, so the
reference arms can only be reproduced from a build recipe, which is provided.

### Removed

- IgLM. The package is absent from the release image and the import in
  `germinal/filters/filter_utils.py` is lazy. In 1.0.0 that import was
  unconditional and at module level, so the release image of 1.0.0 required
  IgLM in order to import its scoring module at all. "Removed" here means the
  package is absent and the code path is unreachable; the vendored
  `colabdesign/colabdesign/iglm/` source is retained, because the benchmark
  image needs that code path.
- cvxopt (GPL-3.0-or-later). It was imported at module level by
  `colabdesign/af/design.py` to serve `mgda()`, a gradient-merging method that
  no configuration can reach: every configuration specifies `pcgrad`, the
  default is `pcgrad`, and an illegal value falls back to `pcgrad`. The
  manuscript describes PCGrad, so removing cvxopt makes the artefact agree with
  the paper rather than changing described behaviour.
- `germinal/filters/af3.py` and the ColabFold dependency it pulled in. The
  file carried AlphaFold 3 terms (CC-BY-NC-SA 4.0 for the source, separate
  terms for the model parameters) into an image whose stated purpose is
  unrestricted commercial use, and it was imported unconditionally at module
  level even though no configuration reaches it.
- `params/DAlphaBall.gcc`. It serves PyRosetta only, which this image does not
  contain; its licence is unclear and adjacent to Rosetta.
- AlphaFold 3 derived input structures. All five shipped input structures are
  now either experimental structures from the PDB (CC0) or Chai-1 predictions
  (Apache 2.0, code and weights).

### Fixed

- The hotspot filter in the open-source scoring path evaluated on the wrong
  chain. `pyrosetta_free_utils.find_nearby_residues_from_pdb` returned residue
  numbers on the binder chain, while its only consumer requires target-chain
  numbers, and it carried an additional off-by-one offset. Three gating metrics
  were affected (`binder_near_hotspot`, `cdr_hotspot_contacts`,
  `cdr3_hotspot_contacts`), in the open-source arms only. The regression test
  requires the two implementations to return identical residue sets on the same
  complex at three distance thresholds, and is verified to reject the 1.0.0
  implementation.
- Relaxation silently copied its input to its output when it failed. The error
  handler now re-raises. The relaxation step also runs in a separate process,
  which is what made the underlying failure visible: it originated inside
  PDBFixer, which builds its own OpenMM `Context` without naming a platform,
  before any platform selection in this codebase is reached.
- Two comments that contradicted the code they documented.

### Changed

- The image is built from a source tree exported from a single commit with
  `git archive`, and the build recipe travels in that commit. A release build
  of the 1.1.0 line was audited file by file against its exported tree: of 233
  exported files, none were missing from the image and one differed — a build
  recipe that does not participate at run time, whose content matched the
  commit the image was built from rather than the later commit the tree had
  since been re-exported to — and the 86 files present only in the image were
  all `__pycache__` or `*.egg-info`. The audit is single-directional by design:
  extra files in the image are listed, not counted as failures.
- Provenance recording. With `provenance_dir` set, each run writes a manifest
  (container digest, commit, configuration digest, binds, GPU model,
  pre-specified equivalence margins and the date they were fixed), one event
  line per trajectory per stage, and a set of denominators, and a separate
  verifier cross-checks thirteen invariants across jobs. With `provenance_dir`
  unset the behaviour is unchanged from upstream. The pipeline's own counters
  are retained unmodified alongside the new ones: they double-count some
  events, so the two ledgers do not agree and are not meant to.

### Disclosed

These statements concern 1.0.0 and are recorded because the published 1.0.0
artefacts remain available.

- **The 1.0.0 release container contains two source modifications that exist in
  no commit of this repository**: a multi-chain interface-string fix and a
  `DEBUG chain_map keys` print, both in `germinal/filters/pyrosetta_utils.py`.
  The criterion is that `git log -S` on the distinguishing strings returns no
  commit over the whole history. That module requires PyRosetta, which the
  release container does not contain, so neither modification is reachable and
  no published number is affected. The 1.0.0 container was produced by
  modifying a sandbox and repacking it, not by a clean definition-file build,
  which is how such modifications came to exist outside version control.
- **The 1.0.0 source archive and the 1.0.0 container are not the same tree.**
  The `v1.0.0` tag predates the container by three weeks and does not contain
  the multi-chain fixes the container carries.
- **The 1.0.0 build recipe declares content the published artefact does not
  have.** `germinal_free.def` extracts AlphaFold-Multimer parameters into
  `/workspace/params`, and the published container has no such parameters.
  Two explanations remain open — an intended runtime bind, documented in
  `%help`; or a silent staging failure, since Apptainer mounts the host `/tmp`
  over the container's `/tmp` while `%post` runs and the recipe staged through
  `/tmp`. The same mechanism was reproduced during the 1.1.0 build. Either way
  the recipe and the artefact disagreed and no build-time assertion would have
  caught it. Every "should be present" and "should be absent" claim in the
  1.1.0 definition now carries an assertion that fails the build.

---

## [1.0.0] — 2026-06-19

First release. Archived at Zenodo as two separate records, one for the source
and one for the container; see the Disclosed section above for why those two
records are not snapshots of the same tree.
