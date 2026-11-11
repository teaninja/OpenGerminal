# Third-party terms for the binaries in this directory's parent

`germinal/filters/` ships three files that are not ours and that OpenGerminal
redistributes: `FASPR`, `sc`, and `dun2010bbdep.bin`. Their terms are here.

| File in `germinal/filters/` | Terms | Where the text came from |
|---|---|---|
| `FASPR` (333,456 B) | MIT | `FASPR.LICENSE`, verbatim from tommyhuangthu/FASPR |
| `sc` (934,480 B) | MIT | `sc-rs.LICENSE`, verbatim from cytokineking/sc-rs |
| `dun2010bbdep.bin` (13,763,520 B) | see `dun2010bbdep.NOTICE` | the two official statements disagree; the notice records both |

The FASPR upstream repository contradicts itself: its README says Apache 2.0
while its LICENSE file says MIT. The LICENSE file is taken as governing, and
both readings permit commercial use, so nothing downstream turns on which one
wins. An earlier version of our own README copied the Apache 2.0 side; that is
corrected.
