# OpenGerminal

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![DOI (code)](https://img.shields.io/badge/DOI%20(code)-10.5281%2Fzenodo.20755400-blue)](https://doi.org/10.5281/zenodo.20755400)
[![DOI (container)](https://img.shields.io/badge/DOI%20(container)-10.5281%2Fzenodo.20756013-blue)](https://doi.org/10.5281/zenodo.20756013)

**A PyRosetta-free implementation of the [Germinal](https://github.com/SantiagoMille/germinal) antibody design pipeline (VHH nanobodies and scFv).**

OpenGerminal replaces PyRosetta — the primary commercial dependency in Germinal — with fully open-source alternatives, enabling the pipeline to be used in academic and commercial settings without software licensing barriers. IgLM was already replaced by AbLang1 in Germinal commit `1e1c1a5` by the Germinal team; OpenGerminal adopts this update and sets AbLang1 as the default for all VHH configurations.

> ⚠️ **Patent notice:** The Germinal methodology is subject to a provisional patent
> application filed by Stanford University and Arc Institute
> (Mille-Fragoso et al., bioRxiv 2025). A provisional patent does not confer
> enforceable rights, but a formal patent may be granted in the future.
> **OpenGerminal removes software licensing barriers (PyRosetta, IgLM) only.**
> Users intending commercial applications are solely responsible for assessing
> compliance with any patent rights that may arise from the Germinal methodology.
> This repository and its authors make no representations regarding the
> patent status of the underlying design method.

> ⚠️ **Preprint coming soon.** Code is released ahead of publication to establish priority. Please cite both OpenGerminal and the original Germinal paper (see [Citation](#citation)).

---

## What Changed

| Component | Original Germinal | OpenGerminal | License |
|---|---|---|---|
| Antibody LM (gradient) | IgLM v0.1.0 | AbLang1-heavy v0.3.1 (ablang2 v0.2.1) *(updated by Germinal team, commit 1e1c1a5; adopted in OpenGerminal as default)* | Apache 2.0 |
| Structure relaxation | PyRosetta FastRelax | OpenMM + FASPR | MIT / Apache 2.0 |
| SASA calculation | PyRosetta | FreeSASA | LGPL |
| Shape complementarity | PyRosetta | sc-rs | MIT |
| Interface geometry | PyRosetta | Biopython | MIT |
| Gradient-based design | AF-Multimer | AF-Multimer | CC BY 4.0 |
| Initial & final cofolding filter | Chai-1 v0.6.1 (1 model) | Chai-1 v0.6.1 (1 model) | Apache 2.0 |
| Sequence redesign | AbMPNN | AbMPNN | MIT |

> **Note:** AbLang integration was implemented by the Germinal team (commit `1e1c1a5`, PR #55/#64/#68). OpenGerminal adopts this update and sets AbLang as the default for all VHH run configurations. The primary contribution of OpenGerminal is the PyRosetta replacement.

> **Known approximations (inherited from [FreeBindCraft](https://github.com/cytokineking/FreeBindCraft)):** `binder_score`, `interface_dG`, and `interface_hbonds` use fixed pass-through values, as these metrics depend on the Rosetta force field and have no open-source equivalent. These filters rarely reject designs in practice.

---

## Pipeline Overview

![OpenGerminal Pipeline](assets/opengerminal_pipeline.svg)

---

## Requirements

- Linux x86_64
- NVIDIA GPU with ≥ 40 GB VRAM (A100 recommended)
- Apptainer / Singularity
- AF-Multimer parameters (see below)
- Chai-1 weights (auto-downloaded on first run)

---

## Installation

### 1. Download the container

```bash
# Option A: Download pre-built container (recommended)
# Download opengerminal_v1.0.0.sif (6.1 GB) from Zenodo:
# https://doi.org/10.5281/zenodo.20756013

# Option B: Build from source
git clone https://github.com/teaninja/OpenGerminal.git
cd OpenGerminal
docker build -f Dockerfile.free -t opengerminal:v1.0.0 .
docker save opengerminal:v1.0.0 | gzip > opengerminal_docker.tar.gz
# Convert to sif on HPC:
apptainer build --fakeroot opengerminal_v1.0.0.sif docker-archive://opengerminal_docker.tar.gz
```

### 2. Download AF-Multimer parameters

```bash
mkdir -p params && cd params
aria2c -q -x 16 https://storage.googleapis.com/alphafold/alphafold_params_2022-12-06.tar
tar -xf alphafold_params_2022-12-06.tar
rm alphafold_params_2022-12-06.tar
```

Alternatively, extract from an existing `germinal.sif`:
```bash
apptainer exec germinal.sif tar -cf params_from_sif.tar /workspace/params
tar -xf params_from_sif.tar --strip-components=2 -C params/
```

### 3. Prepare AbLang weights directory

AbLang weights are downloaded automatically on the first run. Provide a writable directory:

```bash
mkdir -p ablang_weights
```

---

## Usage

### Directory structure

```
/your/workdir/
  opengerminal_v1.0.0.sif
  params/                    ← AF-Multimer parameters
  pdbs/
    nb.pdb                   ← VHH framework template
    <antigen>.pdb             ← target antigen structure
  chai_cache/                ← Chai-1 weights (auto-downloaded)
  ablang_weights/            ← AbLang weights (auto-downloaded)
  results/                   ← output directory
  <jobname>/
    <target>.yaml            ← target configuration
    run.sh                   ← SLURM submission script
```

### Target configuration (YAML)

```yaml
target_name: pdl1
target_pdb_path: pdbs/pdl1.pdb
target_chain: A
binder_chain: B
target_hotspots: A37,A39,A41,A96,A98
hotspot_residue: W40
dimer: false
structure_model: chai
```

### SLURM submission script

```bash
#!/bin/bash
#SBATCH -p gpu
#SBATCH --gres=gpu:a100:1
#SBATCH --nodes=1
#SBATCH -c 8
#SBATCH --mem=80G
#SBATCH --time=12:00:00
#SBATCH --account=<your_account>

cd /your/workdir
module load apptainer

apptainer exec --nv \
  --bind $PWD/params:/workspace/params \
  --bind $PWD/results:/workspace/results \
  --bind $PWD/pdbs:/workspace/pdbs \
  --bind $PWD/chai_cache:/opt/conda/envs/germinal/lib/python3.10/site-packages/downloads \
  --bind $PWD/ablang_weights:/opt/conda/envs/germinal/lib/python3.10/site-packages/ablang2/model-weights-ablang1-heavy \
  --bind $PWD/<jobname>/<target>.yaml:/workspace/configs/target/<target>.yaml \
  --pwd /workspace \
  opengerminal_v1.0.0.sif \
  python run_germinal.py target=<target> experiment_name=<exp> structure_model=chai
```

---

## Benchmark

Tested on NVIDIA A100 80GB against original Germinal (commit `88d7f85`).

| Metric | Original Germinal | OpenGerminal v1.0.0 |
|---|---|---|
| PD-L1 accepted designs (seeds) | 7 seeds / 274 trajectories (2.6%) | 9 seeds / 499 trajectories (1.8%) |
| PD-L1 accepted structures | 16 PDB structures | 18 PDB structures |
| PD-L1 cofolding entry rate | 18.6% | 33.7% |
| IL-3 accepted designs | 0 / 376 trajectories | 0 / 729 trajectories |
| Avg. time per trajectory (Stage 1) | ~180 sec (3.0 min) | ~258 sec (4.3 min) |
| PyRosetta required | ✅ Yes | ❌ No |
| IgLM required | ✅ Yes | ❌ No |
| Commercial use | ⚠️ License required | ✅ Free |

> **For full methods, benchmark details, and analysis, see the accompanying paper (preprint coming soon).**

> Per-trajectory runtime is ~56% slower on average (166s vs 258s; all targets combined). This reflects the OpenMM relaxation step replacing PyRosetta FastRelax. Note: trajectory counts are limited in the current benchmark; additional runs are ongoing and this table will be updated.

---

## License

© 2026 by the Rector and Visitors of the University of Virginia. All rights reserved.

OpenGerminal code modifications are released under the **Apache 2.0 License**. See [LICENSE](LICENSE) for full text.

### Software dependencies

The open-source components introduced in this work are distributed under the following licenses:

| Component | License |
|---|---|
| OpenMM 8.5.1 | MIT License |
| FASPR | Apache 2.0 License |
| FreeSASA | LGPL v2.1 |
| sc-rs v1.0.0 | MIT License |
| Biopython | Biopython License Agreement (BSD-like) |
| ablang2 v0.2.1 | Apache 2.0 License |

### Patent disclaimer

The Germinal pipeline methodology is subject to a provisional patent application
filed by Stanford University and Arc Institute. Provisional patents do not yet
confer enforceable rights, but a formal patent may be granted in future.
This notice is provided for transparency; it is not legal advice.
**Users are solely responsible for determining whether their intended use
requires a patent license from the relevant rights holders.**

This repository builds upon:
- [Germinal](https://github.com/SantiagoMille/germinal) (Apache 2.0) — Mille-Fragoso et al., 2025
- [FreeBindCraft](https://github.com/cytokineking/FreeBindCraft) (Apache 2.0) — Ring, 2025
- [AbLang2](https://github.com/oxpig/AbLang2) (Apache 2.0) — Oxford Protein Informatics Group
- [OpenMM](https://openmm.org) (MIT)
- [FreeSASA](https://freesasa.github.io) (LGPL)

---

## Citation

If you use OpenGerminal, please cite:

```bibtex
@article{opengerminal2026,
  title   = {OpenGerminal: An Open-Source Implementation of the Germinal Antibody Design Pipeline},
  author  = {Bing Han and Sheng Li},
  journal = {bioRxiv},
  year    = {2026},
  note    = {Preprint}
}
```

Please also cite the original Germinal paper:

```bibtex
@article{germinal2025,
  title   = {Germinal: De novo VHH antibody design with a language model-guided diffusion framework},
  author  = {Mille-Fragoso, Santiago and others},
  journal = {bioRxiv},
  year    = {2025}
}
```

---

## Affiliation

Department of Molecular Physiology and Biological Physics, University of Virginia School of Medicine  
[Kenworthy Lab](https://med.virginia.edu/kenworthy-lab/)

*This work was conducted as part of research activities in the Kenworthy Lab.*

---

## Acknowledgments

OpenGerminal is built on the work of the Germinal team (Santiago Mille-Fragoso et al.) and draws on the PyRosetta-free approach pioneered by FreeBindCraft (Aaron Ring, Ariax Bio). We thank both teams for making their work open source.
