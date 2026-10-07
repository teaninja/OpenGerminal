"""
pyrosetta_free_utils.py
=======================
Drop-in replacement for Germinal's pyrosetta_utils.py using fully open-source,
commercially permissive alternatives (Apache 2.0 / MIT / BSD).

Replacement strategy:
  pr_relax / pr_relax_parallel      -> openmm_relax (OpenMM + FASPR, GPU-accelerated)
  score_interface                   -> pr_alternative_score_interface (FreeSASA + Biopython + sc-rs)
  score_interface_ensemble          -> thin wrapper around score_interface
  get_sap_score                     -> FreeSASA / Biopython + Black-Mould hydrophobicity scale
  align_pdbs                        -> Biopython Superimposer
  unaligned_rmsd                    -> Biopython CA-RMSD
  find_nearby_residues_from_pdb     -> Biopython NeighborSearch
  get_residue_contacts              -> Biopython NeighborSearch
  get_chain_length                  -> Biopython (pure Python fallback)

Known approximations (inherited from FreeBindCraft strategy):
  - binder_score       : fixed at -1.0  (Rosetta REU has no open-source equivalent;
                         filter threshold <= 0 is never triggered in practice)
  - interface_dG       : fixed at -10.0 (same rationale)
  - interface_hbonds   : fixed at 5     (filter threshold >= 3 is never triggered)
  These fixed values were validated empirically by FreeBindCraft on BindCraft targets.
  Re-calibrate filter thresholds using your own benchmark dataset after switching.

License: Apache 2.0 (inherits from FreeBindCraft pr_alternative_utils.py)
"""

import os
import time
import multiprocessing
import numpy as np

from Bio import PDB
from Bio.PDB import PDBParser, Superimposer, NeighborSearch, PDBIO

try:
    from germinal.filters.pr_alternative_utils import (
        openmm_relax,
        openmm_relax_subprocess,
        pr_alternative_score_interface,
    )
    _HAS_OPENMM = True
except ImportError as e:
    print(f"[pyrosetta_free_utils] WARNING: OpenMM not available ({e}). "
          f"Relax will be skipped (input copied to output).")
    _HAS_OPENMM = False

try:
    import freesasa
    _HAS_FREESASA = True
except ImportError:
    _HAS_FREESASA = False


def pr_relax(pdb_file, relaxed_pdb_path):
    """
    Replace PyRosetta FastRelax with OpenMM energy minimization.

    Uses GPU-accelerated L-BFGS minimization via OpenMM with OBC2 implicit
    solvent, ramped backbone restraints, and optional FASPR side-chain
    repacking. Typically 2-4x faster than Rosetta FastRelax.

    Relaxation runs in a separate process, on the CPU, one process per call.

    The in-process path failed on every call inside the pipeline on this cluster
    (CUDA_ERROR_UNSUPPORTED_PTX_VERSION, after about a second), while the same
    input relaxed on the CPU in a fresh process both with and without JAX holding
    the GPU. The root cause is not identified; process isolation is what the
    measurements support, and openmm_relax_subprocess exists for exactly that.

    The previous version of this docstring claimed the GPU path "falls back to the
    CPU anyway" and that "results are identical, only speed differs". On this
    cluster that was false in the way that matters: it did not fall back, it
    copied its input to its output, and the caller could not tell. The claim is
    recorded here because believing it is what delayed finding the defect.

    CPU relaxation costs roughly 12 minutes per call on 4 cores, against seconds
    for PyRosetta FastRelax. That is a real cost of the PyRosetta-free path and
    belongs in the results, not in a footnote.
    """
    if os.path.exists(relaxed_pdb_path):
        return
    if not _HAS_OPENMM:
        import shutil
        print("[pr_relax] WARNING: OpenMM unavailable, copying input as output.")
        shutil.copy(pdb_file, relaxed_pdb_path)
        return
    try:
        openmm_relax_subprocess(
            pdb_file_path=pdb_file,
            output_pdb_path=relaxed_pdb_path,
            use_gpu_relax=False,
            use_faspr_repack=True,
        )
    except Exception as e:
        print(f"[pr_relax] Relax failed ({e}), retrying without FASPR...")
        openmm_relax_subprocess(
            pdb_file_path=pdb_file,
            output_pdb_path=relaxed_pdb_path,
            use_gpu_relax=False,
            use_faspr_repack=False,
        )
    # No final except: if both attempts fail the exception propagates. A missing
    # relaxed structure stops the job, which is the correct outcome -- the previous
    # behaviour was to carry on with the unrelaxed one.
    if not os.path.exists(relaxed_pdb_path):
        raise RuntimeError(
            f"[pr_relax] relax reported success but produced no file: {relaxed_pdb_path}"
        )


def _relax_worker_free(pdb_file, relaxed_pdb_path, seed):
    """
    Worker function for parallel relax (runs in a child process).
    seed is accepted for API compatibility; OpenMM uses its own internal RNG.
    """
    try:
        if not os.path.exists(relaxed_pdb_path):
            openmm_relax_subprocess(
                pdb_file_path=pdb_file,
                output_pdb_path=relaxed_pdb_path,
                use_gpu_relax=False,
                use_faspr_repack=True,
            )
    except Exception as e:
        err_path = f"{relaxed_pdb_path}.err"
        with open(err_path, "w") as f:
            import traceback
            f.write(traceback.format_exc())
        print(f"[relax_worker] FAILED for {relaxed_pdb_path}: {e}")
        raise


def pr_relax_parallel(pdb_file, output_dir, design_name, dalphaball_path=None, n_relax=5):
    """
    Replace parallel PyRosetta FastRelax with parallel OpenMM relaxation.

    dalphaball_path is accepted for API compatibility but not used by OpenMM.

    Returns:
        list[str]: paths to successfully relaxed PDB files.
    """
    ctx = multiprocessing.get_context("spawn")
    relaxed_paths = []
    processes = []
    seeds = np.random.randint(0, 999999, size=n_relax).tolist()

    for i, seed in enumerate(seeds):
        relaxed_pdb_path = os.path.join(output_dir, f"{design_name}_relaxed_{i}.pdb")
        relaxed_paths.append(relaxed_pdb_path)
        if os.path.exists(relaxed_pdb_path):
            continue
        p = ctx.Process(target=_relax_worker_free, args=(pdb_file, relaxed_pdb_path, seed))
        processes.append(p)

    for p in processes:
        p.start()
    for p in processes:
        p.join()

    missing = [p for p in relaxed_paths if not os.path.exists(p)]
    if missing:
        print(f"\n{'=' * 78}\n"
              f"[RELAX ERROR] {len(missing)}/{len(relaxed_paths)} parallel relax runs FAILED\n"
              f"{'=' * 78}", flush=True)
        relaxed_paths = [p for p in relaxed_paths if os.path.exists(p)]

    return relaxed_paths


def score_interface(pdb_file, binder_chain="B", target_chain="A"):
    """
    Replace PyRosetta score_interface using open-source alternatives.

    Delegates to FreeBindCraft's pr_alternative_score_interface:
    - FreeSASA or Biopython Shrake-Rupley for SASA
    - sc-rs binary for shape complementarity
    - Biopython NeighborSearch for interface residue identification

    Returns the same (interface_scores, interface_AA, interface_residues_str)
    tuple with identical field names as the original PyRosetta implementation.
    """
    return pr_alternative_score_interface(
        pdb_file=pdb_file,
        binder_chain=binder_chain,
        target_chain=target_chain,
        sasa_engine="auto",
    )


def score_interface_ensemble(
    relaxed_pdb_paths, binder_chain="B", target_chain="A", score_mode="average"
):
    """
    Score interface metrics across an ensemble of relaxed structures.

    Logic is identical to the original PyRosetta version; only the underlying
    score_interface call is replaced.

    Returns:
        tuple: (interface_scores, best_AA, best_residues, best_pdb_path)
    """
    all_scores, all_aa, all_residues = [], [], []

    for pdb_path in relaxed_pdb_paths:
        try:
            scores, aa, residues = score_interface(pdb_path, binder_chain, target_chain)
            all_scores.append(scores)
            all_aa.append(aa)
            all_residues.append(residues)
        except Exception as e:
            print(f"[score_interface_ensemble] Warning: failed for {pdb_path}: {e}")

    if not all_scores:
        raise RuntimeError("All score_interface calls failed in ensemble scoring")

    best_idx = np.argmin([s["binder_score"] for s in all_scores])
    best_interface_AA = all_aa[best_idx]
    best_interface_residues = all_residues[best_idx]
    best_relaxed_pdb_path = relaxed_pdb_paths[best_idx]

    if score_mode == "best":
        return (all_scores[best_idx], best_interface_AA,
                best_interface_residues, best_relaxed_pdb_path)

    result_scores = {}
    for key in all_scores[0]:
        values = [s[key] for s in all_scores if s.get(key) is not None]
        if values and isinstance(values[0], (int, float)):
            result_scores[key] = round(float(np.mean(values)), 2)
        else:
            result_scores[key] = all_scores[0][key]

    return result_scores, best_interface_AA, best_interface_residues, best_relaxed_pdb_path


_HYDROPHOBIC_AA = {"LEU", "ILE", "PHE", "TRP", "VAL", "MET", "TYR", "ALA"}


def _compute_per_residue_sasa(structure, chain_id=None):
    """
    Compute per-residue SASA using FreeSASA (preferred) or Biopython fallback.
    Returns dict: {(chain_id, res_num_str): sasa_angstrom2}
    """
    if _HAS_FREESASA:
        try:
            return _compute_sasa_freesasa(structure, chain_id)
        except Exception:
            pass
    return _compute_sasa_biopython(structure, chain_id)


def _compute_sasa_freesasa(structure, chain_id=None):
    """FreeSASA-based per-residue SASA calculation."""
    import tempfile
    io = PDBIO()
    io.set_structure(structure)
    with tempfile.NamedTemporaryFile(suffix=".pdb", delete=False, mode="w") as f:
        tmp_path = f.name
        io.save(tmp_path)
    try:
        fs_structure = freesasa.Structure(tmp_path)
        result = freesasa.calc(fs_structure)
        sasa_dict = {}
        for i in range(fs_structure.nAtoms()):
            chain = fs_structure.chainLabel(i)
            res_num = fs_structure.residueNumber(i).strip()
            key = (chain, res_num)
            sasa_dict[key] = sasa_dict.get(key, 0.0) + result.atomArea(i)
        return sasa_dict
    finally:
        os.unlink(tmp_path)


def _compute_sasa_biopython(structure, chain_id=None):
    """Biopython Shrake-Rupley SASA (fallback when FreeSASA is unavailable)."""
    from Bio.PDB.SASA import ShrakeRupley
    sr = ShrakeRupley()
    sr.compute(structure, level="R")
    sasa_dict = {}
    for model in structure:
        for chain in model:
            if chain_id and chain.id != chain_id:
                continue
            for residue in chain:
                if residue.id[0] != " ":
                    continue
                key = (chain.id, str(residue.id[1]))
                sasa_dict[key] = residue.sasa
    return sasa_dict


def get_sap_score(
    pdb,
    binder_chain=None,
    only_binder=False,
    hydrophobic_aa=None,
    patch_radius=8,
    limit_sasa=1,
    avg_sasa_patch_thr=0.75,
    cdrs=None,
):
    """
    Replace PyRosetta get_sap_score.

    Computes Spatial Aggregation Propensity using FreeSASA/Biopython SASA
    and the Black-Mould hydrophobicity scale.

    Returns:
        tuple: (sap_total, cdr_sap, exposed_hydrophobic_aa, hydrophobic_patches)
               Matches the original PyRosetta return signature exactly.
    """
    if hydrophobic_aa is None:
        hydrophobic_aa = _HYDROPHOBIC_AA

    parser = PDBParser(QUIET=True)
    structure = parser.get_structure("s", pdb)
    model = next(structure.get_models())

    if binder_chain and only_binder:
        residues = [r for r in model[binder_chain] if r.id[0] == " "]
    else:
        residues = [r for chain in model for r in chain if r.id[0] == " "]

    sasa_map = _compute_per_residue_sasa(
        structure, chain_id=binder_chain if only_binder else None
    )

    res_list, sasa_vals = [], []
    for res in residues:
        sasa = sasa_map.get((res.get_parent().id, str(res.id[1])), 0.0)
        res_list.append((res, sasa))
        sasa_vals.append(sasa)

    sasa_array = np.array(sasa_vals)
    all_atoms = [atom for res, _ in res_list for atom in res.get_atoms()]
    ns = NeighborSearch(all_atoms)

    def get_nearby_res_idx(center_res, radius):
        if "CA" not in center_res:
            return []
        center = center_res["CA"].get_vector().get_array()
        hits = ns.search(center, radius, level="R")
        idx_list = []
        for nr in hits:
            nr_chain = nr.get_parent().id
            nr_num = str(nr.id[1])
            for i, (r, _) in enumerate(res_list):
                if r.get_parent().id == nr_chain and str(r.id[1]) == nr_num:
                    idx_list.append(i)
                    break
        return idx_list

    exposed_hydrophobic_aa, hydrophobic_patches = [], []

    for i, (res, sasa) in enumerate(res_list):
        aa3 = res.get_resname().strip()
        if binder_chain and res.get_parent().id != binder_chain:
            continue
        if aa3 in hydrophobic_aa and sasa >= limit_sasa:
            exposed_hydrophobic_aa.append((i + 1, aa3))
            nearby_idx = get_nearby_res_idx(res, patch_radius)
            avg_sap = float(np.mean(sasa_array[nearby_idx])) if nearby_idx else 0.0
            if avg_sap >= avg_sasa_patch_thr:
                nearby_set = set(nearby_idx)
                already = any(
                    len(nearby_set - prev) <= len(nearby_set) - 2
                    for _, prev in hydrophobic_patches
                )
                if not already:
                    hydrophobic_patches.append((avg_sap, nearby_set))

    sap_total = float(np.sum(sasa_array))
    cdr_sap = float(np.sum(sasa_array[cdrs])) if cdrs is not None else sap_total
    return sap_total, cdr_sap, exposed_hydrophobic_aa, hydrophobic_patches


def align_pdbs(reference_pdb, align_pdb, reference_chain_id, align_chain_id):
    """
    Replace PyRosetta align_pdbs.

    Superimposes align_pdb onto reference_pdb using Biopython SVD-based
    Superimposer on C-alpha atoms. Overwrites align_pdb in place.
    """
    reference_chain_id = reference_chain_id.split(",")[0]
    align_chain_id = align_chain_id.split(",")[0]

    parser = PDBParser(QUIET=True)
    ref_struct = parser.get_structure("ref", reference_pdb)
    mob_struct = parser.get_structure("mob", align_pdb)

    ref_model = next(ref_struct.get_models())
    mob_model = next(mob_struct.get_models())

    ref_ca = [r["CA"] for r in ref_model[reference_chain_id]
              if r.id[0] == " " and "CA" in r]
    mob_ca = [r["CA"] for r in mob_model[align_chain_id]
              if r.id[0] == " " and "CA" in r]

    k = min(len(ref_ca), len(mob_ca))
    if k < 3:
        raise ValueError(f"Not enough CA atoms to superimpose (got {k}, need >= 3)")

    sup = Superimposer()
    sup.set_atoms(ref_ca[:k], mob_ca[:k])
    sup.apply(mob_struct.get_atoms())

    io = PDBIO()
    io.set_structure(mob_struct)
    io.save(align_pdb)


def unaligned_rmsd(reference_pdb, align_pdb, reference_chain_id, align_chain_id):
    """
    Replace PyRosetta unaligned_rmsd.

    Computes CA-RMSD between two chains without prior superimposition.
    Falls back to the last chain if the specified chain ID is not found.

    Returns:
        float: RMSD in Angstroms, rounded to 2 decimal places.
    """
    parser = PDBParser(QUIET=True)
    ref_model = next(PDBParser(QUIET=True).get_structure("r", reference_pdb).get_models())
    mob_model = next(PDBParser(QUIET=True).get_structure("m", align_pdb).get_models())

    def get_ca(model, chain_id):
        chain_id = chain_id.split(",")[0]
        chain = model[chain_id] if chain_id in model else list(model.get_chains())[-1]
        return np.array([r["CA"].get_vector().get_array()
                         for r in chain if r.id[0] == " " and "CA" in r])

    ref_coords = get_ca(ref_model, reference_chain_id)
    mob_coords = get_ca(mob_model, align_chain_id)
    k = min(len(ref_coords), len(mob_coords))
    if k == 0:
        return 100.0
    diff = ref_coords[:k] - mob_coords[:k]
    return round(float(np.sqrt(np.mean(np.sum(diff ** 2, axis=1)))), 2)


def find_nearby_residues_from_pdb(
    pdb_path, target_residues, distance_threshold=6.0, chain="A"
):
    """
    Replace PyRosetta find_nearby_residues_from_pdb.

    Returns chain-relative positions within `chain` (1-based, counting only
    standard residues of that chain) whose CA atom lies within
    distance_threshold Angstroms of the CA atom of any target residue. The
    target residues themselves are included, and a residue may appear more
    than once when it is near several targets; both match the PyRosetta
    implementation this replaces, and the only consumer performs membership
    tests, so neither matters downstream.

    target_residues are chain-relative positions of that same chain, as
    produced by utils.idx_from_ranges(...) + 1.

    This namespace is not incidental. The result is consumed by
    is_binder_near_hotspot as the `target_hotspots` argument and compared
    against target-chain contact residue numbers from get_residue_contacts,
    which are chain-relative for both implementations. Returning residues of
    any other chain produces a membership test between two unrelated numbering
    schemes; because the ranges overlap it does not raise, it just silently
    filters on nothing.

    Unlike a PyRosetta pose, a PDB file can carry residues with no CA atom.
    A target residue without a CA is an error -- the hotspot cannot be placed
    -- while a non-target residue without a CA is simply skipped, since it
    cannot be shown to be within the threshold.
    """
    parser = PDBParser(QUIET=True)
    structure = parser.get_structure("s", pdb_path)
    model = next(structure.get_models())

    if chain not in model:
        raise ValueError(
            "Chain %s not found in %s. Available chains: %s"
            % (chain, pdb_path, [c.id for c in model])
        )

    residues = [r for r in model[chain] if r.id[0] == " "]
    n_residues = len(residues)
    if n_residues == 0:
        raise ValueError("Chain %s of %s has no standard residues" % (chain, pdb_path))

    if isinstance(target_residues, (int, np.integer)):
        target_residues = [target_residues]
    target_residues = [int(t) for t in target_residues]

    for t in target_residues:
        if t < 1 or t > n_residues:
            raise ValueError(
                "Invalid residue number %d for chain %s. Chain has %d residues."
                % (t, chain, n_residues)
            )

    ca_coords = []
    for residue in residues:
        ca_coords.append(residue["CA"].get_coord() if "CA" in residue else None)

    nearby = []
    for t in target_residues:
        target_ca = ca_coords[t - 1]
        if target_ca is None:
            raise ValueError(
                "Target residue %d of chain %s has no CA atom; the hotspot "
                "cannot be located." % (t, chain)
            )
        for position in range(1, n_residues + 1):
            if position == t:
                nearby.append(position)
                continue
            coord = ca_coords[position - 1]
            if coord is None:
                continue
            if float(np.linalg.norm(coord - target_ca)) <= distance_threshold:
                nearby.append(position)

    return np.array(nearby)


def get_residue_contacts(pdb_path, chain1="A", chain2="B", cutoff_distance=4.0):
    """
    Replace PyRosetta get_residue_contacts.

    Returns:
        dict: {(res_num_chain1, res_num_chain2): min_distance_angstroms}
    """
    parser = PDBParser(QUIET=True)
    structure = parser.get_structure("s", pdb_path)
    model = next(structure.get_models())

    if chain1 not in model or chain2 not in model:
        return {}

    atoms1 = list(model[chain1].get_atoms())
    atoms2 = list(model[chain2].get_atoms())
    ns = NeighborSearch(atoms1 + atoms2)

    contacts = {}
    for atom1 in atoms1:
        coord = atom1.get_vector().get_array()
        for atom2 in ns.search(coord, cutoff_distance, level="A"):
            if atom2.get_parent().get_parent().id != chain2:
                continue
            key = (atom1.get_parent().id[1], atom2.get_parent().id[1])
            dist = float(np.linalg.norm(coord - atom2.get_vector().get_array()))
            if key not in contacts or dist < contacts[key]:
                contacts[key] = round(dist, 3)
    return contacts


def get_chain_length(pdb_path_or_pose, chain_id="A"):
    """
    Replace PyRosetta get_chain_length.

    Accepts a PDB file path (str) or legacy PyRosetta Pose object.
    """
    if isinstance(pdb_path_or_pose, str):
        parser = PDBParser(QUIET=True)
        structure = parser.get_structure("s", pdb_path_or_pose)
        model = next(structure.get_models())
        if chain_id not in model:
            return 0
        return sum(1 for r in model[chain_id] if r.id[0] == " ")
    try:
        return pdb_path_or_pose.total_residue()
    except Exception:
        return 0
