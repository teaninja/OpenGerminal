# Third-party terms for files redistributed by OpenGerminal

`germinal/filters/` ships seven files that are not ours and that OpenGerminal
redistributes: four Python modules and three binary or data files. One further
redistributed file lives outside this directory and is covered here too, for the
reason given at the end. All of their terms are in this directory.

## From FreeBindCraft

Four modules and three of the files below come from FreeBindCraft
(https://github.com/cytokineking/FreeBindCraft), used under the MIT License. The
licence text is in `BindCraft-FreeBindCraft.LICENSE`, verbatim, including its own
first line stating that it is an unmodified copy of BindCraft's
(https://github.com/martinpacesa/BindCraft, © 2024 Martin Pacesa).

| File in `germinal/filters/` | Relation to FreeBindCraft's copy |
|---|---|
| `logging_utils.py` | byte-identical to `functions/logging_utils.py` |
| `pr_alternative_utils.py` | derived; 1,482 of its 1,505 non-blank lines unchanged |
| `biopython_utils.py` | derived from `functions/biopython_utils.py` |
| `generic_utils.py` | derived from `functions/generic_utils.py` |
| `FASPR` (333,456 B) | byte-identical to `functions/FASPR` |
| `sc` (934,480 B) | byte-identical to `functions/sc` |
| `dun2010bbdep.bin` (13,763,520 B) | byte-identical to `functions/dun2010bbdep.bin` |

Byte-identical here means equal git blob hashes, which is an exact comparison of
content and not of names.

`FASPR` and `sc` are built from projects of their own, and those projects' terms
are reproduced separately because they govern the executables themselves:

| File | Terms | Where the text came from |
|---|---|---|
| `FASPR` | MIT | `FASPR.LICENSE`, verbatim from tommyhuangthu/FASPR |
| `sc` | MIT | `sc-rs.LICENSE`, verbatim from cytokineking/sc-rs |
| `dun2010bbdep.bin` | see `dun2010bbdep.NOTICE` | the two official statements disagree; the notice records both |

The FASPR upstream repository contradicts itself: its README says Apache 2.0
while its LICENSE file says MIT. The LICENSE file is taken as governing, and
both readings permit commercial use, so nothing downstream turns on which one
wins. An earlier version of our own README copied the Apache 2.0 side; that is
corrected.

## AbMPNN model weights

`AbMPNN-weights.NOTICE` carries the attribution required by CC BY 4.0 for

    colabdesign/colabdesign/mpnn/weights_abmpnn/abmpnn.pkl

which is a format conversion of the published AbMPNN checkpoint.

That file is not in this directory, and the notice is here rather than beside it
for a reason worth stating: `colabdesign/colabdesign/mpnn` is byte-identical to
the corresponding directory of the upstream fork by git tree hash, and adding a
file to it would end that. The identity is useful to anyone auditing what in
this repository is ours and what arrived with the fork, so it is kept, and the
attribution is placed in the one directory that collects such notices. CC BY 4.0
asks for attribution in any reasonable manner; a single collected location with
a pointer from the repository README is the same arrangement already used for
the Dunbrack rotamer library.

## What is not here

Terms for components installed by the package manager rather than redistributed
as files are not in this directory. They are in the image's own metadata and in
the project's SBOM, which enumerates them from the image rather than from a
hand-written list.
