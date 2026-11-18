# TODO

- **Docstrings for CodeRabbit's coverage check** (PR #8, 2026-10-15). CodeRabbit's
  default "Docstring Coverage" pre-merge check wants 80% on functions a PR touches;
  PR #8 scored 16.67%, mostly test methods, which carry no docstrings in this repo.
  Decide whether to add them or to turn the check off in a `.coderabbit.yaml`.

- **Mesh: say why a triangle has no depth** (2026-11-18). A triangle with depth
  −999 is one of at least three things, and the tile format cannot tell them
  apart (`mesh/binary.py`: depth, clear, hazv and a flags byte with all eight
  bits used; `index.json` has no coverage outline):
  1. a coarser chart has the depth but the build reads z16 tiles only
     (`mesh/decode.py`), which hold the detailed bands. Seen in the 01CGD data
     of 2026-09-07 at 42.6382, −69.7644 (band 3 `DEPARE` 237.7–256 m) and
     40.3798, −72.0945 (band 3 `DEPARE` 54.8–64 m): no band 4 or 5 there;
  2. no chart has data: every band's `M_COVR` is `CATCOV` 2 (42.6307, −67.3709);
  3. the detailed chart marks the water unsurveyed, `UNSARE` (41.6202, −69.9608
     and 42.7622, −71.0986, both band 5). The build already records this per
     face (`unsurv` in `mesh/build.py`) and `mesh/finalize.py` drops it.

  The router lets a route use all three alike. Size, measured on the seven
  meshes in use on 2026-11-18: 193,497 km² without depth in tiles that hold
  chart data; 94.8 % of it large offshore regions, 0.6 % small patches at the
  shore, mostly beside water charted under 2 m. How the area splits between
  the three cases is not measured (six positions were checked).

  CAUTION, same day: case 1's explanation ("the build reads z16 only, which
  holds the detailed bands") is not established. A comparison of the mesh's
  tiles on brain with the `M_COVR` outlines of the local 01CGD data found 74
  tiles that hold charted depths in the mesh but lie outside every band 4,
  band 5 and gap-fill outline of that data (for example the tile centred
  42.625, −69.375, 47,495 triangles), inside band 3 coverage. The local data
  (2026-09-07, with extra New Jersey cells) is not the chart the mesh on
  brain was built from (mesh sidecar on R2 dated 2026-10-16), so this is
  either a difference between the two chart builds or coarser-band data
  reaching z16 in a way not understood. To settle before any design: the
  `M_COVR` and band of the features in the z16 tiles of the chart file the
  mesh was actually built from.

  To do: a design for the build to carry which case a triangle is (and, for
  case 1, whether to take the coarser chart's depth). Only case 3 then needs a
  routing decision. Nothing is changed until that design is agreed; the router
  stays as it is.

- **Meshes were not rebuilt with the 2026-11-01 charts** (found 2026-11-18).
  The scheduled chart run of 2026-11-01 (run 41263914244) built all nine
  districts. In its `release` job the step "Upload to Cloudflare R2" failed
  for two files, `11CGD_ENCs.pmtiles` and `13CGD_ENCs.mbtiles`, both with
  `InvalidPart … CompleteMultipartUpload: One or more of the specified parts
  could not be found`; every other file was uploaded. The step exits 1 when
  any upload fails, so the run concluded "failure", and the mesh workflow
  (run 41288021209) stopped at "Chart build concluded 'failure'; meshes stay
  as they are" (`build-mesh.yml`, "Decide whether this run publishes") and is
  shown as success. State of R2 `charts/` from that job's own listing:
  charts of 01, 05, 07, 08, 09, 14 and 17CGD are the 2026-11-01 build; 11CGD
  has the new `.mbtiles` and `.json` with the 2026-10-16 `.pmtiles`; 13CGD
  has the new `.json` and `.pmtiles` with the 2026-10-16 `.mbtiles`; every
  `*_mesh.json` and `*_mesh.tar.zst` is of 2026-10-16. To decide: how the two
  files get uploaded, and whether one failed upload should stop the meshes
  of the districts whose charts did publish.

- **Mesh: mark a fixed bridge or overhead cable that has no charted height**
  (2026-11-18). `clearance_of` in `mesh/build.py` returns no clearance for a
  fixed span whose chart gives none, and the face then carries `clear` −999
  and no flag: in the mesh it is the same as open sky, so the router lets any
  boat under it and cannot say so.

  01CGD harbour charts (band 5 `BRIDGE`, data of 2026-09-07), fixed bridges
  (`CATBRG` 1): 1,930, of which 457 have a height (`VERCCL` or `VERCLR`) and
  1,473 have none. Of the 1,473: 1,340 unnamed; notes "Railway bridge" on 258,
  also "Culvert", "Bridge under construction"; of the 133 named ones, 107
  share their name with a bridge feature that has a height (one bridge
  charted in several pieces, the height on one). By the deepest charted water
  (`DEPARE` `DRVAL1`) the feature touches: none 330, only drying 195, 0 to 2 m
  706, 2 m or more 242. Not checked: the raw cells, and districts other than 1.

  Owner's rule, 2026-11-18, for when the mesh marks them: "Block with load
  warning. Also add to the TODO a user override when encountered and there is
  no way around." (Read as: the boat is not routed under one, and the route
  says so loudly; the user can override when one is met and there is no way
  round.)

  To do, design first: (a) the build marks a face under a fixed bridge,
  cable, pipeline or conveyor with no charted height (the flags byte is full,
  so the format needs room); (b) the pieces of one bridge are joined before
  that, so a bridge whose height is charted on one piece is not marked
  unknown on the others; (c) the router's side is in the plugin's
  `docs/TODO.md`.
