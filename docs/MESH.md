# Navigation mesh (`build-mesh.py`)

A routing mesh of each district's charted water: a constrained Delaunay
triangulation of the water with, per triangle, the charted depth, the
vertical clearance, hazard and mark flags, fairway, dredged and
traffic-zone labels, a shore penalty and a depth penalty. It is what the
signalk-weather-router-plus plugin routes motoring legs on (its
`meshDir` setting points at the unpacked folder).

The build is the District 1 experiment of October 2026 made
district-agnostic: the same four stages, the same intermediate files,
the same constants and checks. The only things that changed are the
ones that were hard-coded for District 1, and each is listed at the end.

## Inputs

- A finished chart `.mbtiles` (the published district file). Only its
  z16 tiles are read, decoded exactly as the old routing grid read them,
  so the mesh is built from what the chart publishes, with finer-wins
  already resolved by the chart build. Several files may be given; each
  is a separate source and the last obstruction or dredged area of a
  tile wins within a source.
- The OSM land polygons, `land_polygons.shp` from the
  [osmdata.openstreetmap.de split package](https://osmdata.openstreetmap.de/data/land-polygons.html)
  (`land-polygons-split-4326.zip`, ~700 MB). The data date is read from
  the package `README.txt` and recorded. It applies only outside the
  charts' coverage (`M_COVR`, CATCOV 1); inside it the chart decides,
  `LNDARE` is land and what the chart draws as water is water. The OSM
  polygons are built from coastline only, so the Great Lakes, Lake
  Champlain, harbours behind a coarse shoreline and every river are land
  in them; while OSM overrode the chart (to 2026-10-09) 09CGD meshed to
  no water at all and 01CGD had no Hudson River. Without `--land` only
  the charts' `LNDARE` is land; the CI always passes it.

## Stages and files

Under the work directory (`data/mesh/<name>/`, `--work`):

| Stage | Module | Writes |
|---|---|---|
| 1 decode | `mesh/decode.py` | `z16_layers/<LAYER>.pkl`: per layer, a list of (WKB, properties) pieces clipped to their z16 tile, with `__src` and `__ord` |
| 2 extent | `mesh/extent.py` | nothing; the rectangle(s) to mesh, see below |
| 3a tiles | `mesh/build.py` `run_tiles` | `build/tile_III_JJJ.npz` (faces, labels, PSLG, medial axis, shore grid), `build/tiles.json` (per-tile results, timings, seam vertices, layer counts: the handoff to 3b), `build/progress_<pid>.log` |
| 3b global | `mesh/build.py` `run_global` | `build/mesh_III_JJJ.npz` (triangles after the global refinement, neighbours, penalties), `build/parts.npz`, `build/summary.json` |
| 4a finalize | `mesh/finalize.py` | `final/mesh_III_JJJ.npz`: uncompressed route-ready tiles with reverse edges and per-triangle labels |
| 4b binary | `mesh/binary.py` | `<out>/<name>_mesh/mesh_III_JJJ.bin` + `index.json` |

Stage 3 per 0.25° tile (with a 100 m overlap, cropped afterwards): band
merge, boundaries (lines as 3 m or 5 m strips, points as 20 m discs,
soundings as squares of the old grid's 0.00025° cells), noding on a
0.5 m grid, polygonize, classify and dissolve by label, plain CDT, the
PSLG for the global step, medial axis, shore distance; then across
tiles: seam weld, one global Triangle `q20` refinement, neighbours,
split back into tile files, checks. The experiment's write-up of why
each step is there is in the module docstrings.

The CLI runs 3a and 3b in two freshly spawned interpreters
(`run_fresh`). The tile stage leaves several GB of chart data and
allocator residue in its process, and the global refinement is the
memory peak of the build: on 2026-10-09 a 16 GB runner killed 01CGD
there twice (9.28 M PSLG vertices) while the parent still held 6 to 7 GB
it no longer needed. With the boundary the refinement starts from an
empty heap and the label table is freed before Triangle runs. The
outputs are byte-identical to the single-process `run()`, which stays
for tests and direct use; `tiles.json` is the only new file.

In the output directory (`-o`, default `data/tiles/`):

- `<name>_mesh/` — the mesh folder. `index.json` (version 1: `west`,
  `south`, `east`, `north`, `tileDeg`, `xScale`, `triangles`, `tiles[]`
  with `i`, `j`, `file`, `first`, `n`, `bbox`) and one `.bin` per tile.
  Tile layout, little-endian: 32-byte header (`WRPMESH1`, uint32 n,
  float64 origin lon, float64 origin lat, 4 bytes pad), then
  `corners float64[n*6]` (x = lon × xScale, y = lat), `neighbours
  int32[n*3]`, `mult float32[n]` (shore × depth penalty), `depth
  float32[n]` (−999 unknown), `clear float32[n]` (−999 none), `hazv
  float32[n]` (1e9 no hazard, −1e9 hazard of unknown depth), `rev
  int8[n*3]`, `flags uint8[n]` (bit 0 navigable, 1 hazard, 2 mark, 3
  channel mark, 4 structure, 5 fairway, 6 dredged, 7 opening bridge).
  Under an opening bridge (CATBRG opening, swing, lifting, bascule,
  draw, transporter) `clear` is the open clearance (VERCOP) or none,
  never the closed one; a fixed span with no charted height is none,
  not 0. The layout and the index version are unchanged: a reader that
  ignores bit 7 routes through an opening bridge as if it stood open.
- `<name>_mesh.json` — the sidecar: sources with size and SHA-256, the
  chart's `build_date` when `--chart-json` is given, land polygon date,
  z16 tile and cell counts, each cluster's box, counts, timings, peak
  RSS and the full requirement list with PASS/FAIL and detail, gating
  failures, warnings, `passed`.
- `<name>_mesh.tar.zst` with `--archive`.

## Extent

The rectangle around every 0.25° cell that holds a z16 tile of the
input. Every tile of the rectangle is processed, as the experiment
processed every tile of its District 1 box; a tile without chart data
meshes as open water of unknown depth. For 01CGD this is 36 × 23 = 828
tiles of which 499 hold z16 data (the experiment's box had 704).

The cells are counted from −180° + 0.000125°, −90° + 0.000125°, half a
cell of the old routing grid's 0.00025° lattice (origin −75.5°, 38.7°)
on which the build draws sounding squares and the fairway reach. A tile
seam lying exactly on a lattice line puts every square abutting it on
one side only: the labels differ across the seam, the seam stays as a
constraint cut at every square corner, and the refinement inflates
around it. Measured on 01CGD (same file, same code): seams on the
lattice kept 14,954 label-differing seam segments and refined in
1,646 s; the experiment's box, which happened to miss the lattice, and
the offset grid keep 26 and refine in about 300 s. With the offset a
seam and a lattice line always differ by half a cell
(`tests/test_mesh_extent.py` asserts it).

When the occupied columns fall into groups separated by more than 4° of
empty columns, each group is a cluster built as its own rectangle with
its own `xScale` (cos of its mid-latitude): 07CGD gives Florida and
Puerto Rico, 14CGD gives Hawaii, Guam and American Samoa. A
single-cluster district has `index.json` at the root of `<name>_mesh/`;
a multi-cluster district has one sub-folder per cluster (named like
`w158n20`: hemisphere and whole degrees of the rectangle's south-west
corner) and a `meshes.json` listing them. Cells on both sides of the
antimeridian are refused (Alaska).

## Checks and exit status

The experiment's requirement list is evaluated per cluster and written
to the sidecar. All of them gate except two:

| # | Requirement | Gates |
|---|---|---|
| 1 | every tile built | yes |
| 2 | every layer class present in the input | warning |
| 3 | every dangerous hazard point and every mark inside a flagged face or on land | yes |
| 4 | mesh land area equals charted + OSM land area | yes |
| 5 | seams: no non-manifold edges, mesh area equals water area, no cracks | yes |
| 6 | no clockwise or zero-area triangles | yes |
| 7 | minimum angle 20° except at sharp input corners | warning |
| 8 | every triangle has a valid part | yes |
| 12 | neighbour links symmetric | yes |
| 13 | penalties in range | yes |
| 14 | the 3 × 3 block around the densest tile loads from the files alone | yes |
| 15 | usable chart coverage (`M_COVR`, `CATCOV` 1) present in the decoded input, also under `--reuse-decoded` | yes |
| 9, 10, 11 | medial axis step, hazard columns, time measured | yes (always pass) |

Plus two more that gate: the finalize step's reverse-edge check, and
"no z16 tile failed to decode" (a tile that does not decode drops its
hazards, depth areas and land silently, and no later check can see
that). Exit 0 when all gating checks pass, 2 when one fails (the
outputs and the intermediates are left in place for diagnosis;
`--allow-fail` makes it 0), 1 when the build itself fails.

## Running locally

```bash
pip install -r mesh/requirements.txt     # numpy scipy shapely mapbox-vector-tile pyshp triangle
./build-mesh.py data/tiles/01CGD_ENCs.mbtiles \
    --land /path/to/land-polygons-split-4326/land_polygons.shp \
    -o data/tiles -j 7 --archive
```

Options: `--name` (default: the input's stem without `_ENCs`),
`--work`, `-j/--workers`, `--reuse-decoded` (skip stage 1 when the
pickles exist), `--box W,S,E,N` (diagnostic: mesh this rectangle
instead of the derived one), `--snap-discs` (move disc corners within 2 m of another
line onto it; off, as in the experiment's kept build), `--debug-point
LON,LAT` (dump the faces and layers at a point to `build/debug_I_J.json`),
`--chart-json`, `--archive`, `--allow-fail`, `--keep-intermediate`.

District 1 on an M-series Mac with 7 workers: about 20 minutes, 33
million triangles, peak RSS 6 GB in the main process and 4 GB in the
largest worker. Memory, not time, is what limits the worker count on a
16 GB machine.

## CI: `.github/workflows/build-mesh.yml`

Runs after every "Build ENC Charts" run on the default branch, and on
demand. It is not told which districts changed: for each district in the
`mesh.active` list of `enc-sources.yaml` it fetches the published chart's
`charts/<D>_ENCs.json` and the mesh's `charts/<D>_mesh.json` from the R2
public URL and rebuilds when the chart's `build_date` is newer than the
mesh's `chart_build_date`, or when no mesh exists. A chart run that did
not publish changes no dates, so the mesh run ends in seconds. `force`
rebuilds regardless; a named `builds` list restricts the set.

Each district is one matrix job on the runner named by `mesh.runner`
with `mesh.workers` tile workers: install the packages, restore or
download the land polygons (cached per calendar month), download the
chart from R2, run `build-mesh.py --archive --chart-json`, upload
`<D>_mesh.tar.zst` and `<D>_mesh.json` as the run artifact. A gating
failure fails the job and nothing of that district is published.

Publishing (`release`, scheduled chart runs and manual `release` runs on
the default branch), one district at a time so the runner's disk holds
it:

- `<D>_mesh.tar.zst` (when under GitHub's 2 GiB asset limit) and
  `<D>_mesh.json` to the `latest` release, replacing the previous ones;
- the same two files to R2 `charts/`;
- the unpacked folder to R2 `charts/mesh/<D>/` with `aws s3 sync
  --delete`, so a mirror of that prefix is a valid `meshDir`;
- `charts/mesh/index.json` rebuilt by `mesh-catalog.py` from every
  `*_mesh.json` in `charts/`: version 1, one entry per mesh with `name`,
  `dir`, `archive` (relative, `<D>_mesh.tar.zst`), `archive_url` (the
  absolute HTTPS URL of that archive), `sidecar`, `build_date`,
  `chart_build_date` (or null), `format`, `triangles`, `bytes` and
  `clusters[].box`. The script validates every field the plugin requires
  (name charset, `.tar.zst`, ISO dates, non-negative counts, a non-empty
  cluster list with finite `[west, south, east, north]` boxes) and HEADs
  every `archive_url` for 200 and a plausible `Content-Length`; one bad
  entry fails the step and the previous catalogue stays. The archive's
  top folder is `<D>_mesh/`, which the plugin unpacks from;
- the release notes rewritten by `release-notes.sh` (shared with the
  chart workflow): charts section, then a navigation meshes section.

First CI run (2026-10-08, branch test, 01CGD from the chart staged the
same day): `ubuntu-latest`, `workers: 2`; 864 tiles, 35,668,991
triangles in 494 tiles, every gating check passed; build step 33 min,
job 40 min, staging 2.5 min; peak RSS as Linux reports it 15.0 GB main
process and 11.6 GB largest child (the refinement fork), so two workers
fit the 16 GB runner with little margin. The land-polygon download took
5.5 min uncached. GitHub's larger runners are not available to personal
accounts, so the standard runner is the only one.

Test pathway (`publish: test`, the default for manual runs): the same
outputs go to R2 `charts-test/<branch>/` (archive, sidecar and
`mesh/<D>/` folder) and the run artifacts; a branch's run reads the
chart from `charts-test/<branch>/` when one is staged there, else from
`charts/`. Nothing published is touched.

## Measured against the experiment (2026-10-08)

Same chart file (01CGD, 2026-09-07), same code path, three runs:

| Run | Box | Kept seam segments | Refinement | Triangles | Tiles |
|---|---|---|---|---|---|
| experiment (4 files) | hand-drawn D1 | 26 | 288 s | 33,044,145 | 437 |
| `--box` D1, 01CGD only | hand-drawn D1 | 26 | 628 s | 35,541,267 | 439 |
| derived, lattice-aligned | 36 × 23 from −180/−90 | 14,954 | 1,646 s | 35,404,354 | 460 |
| derived, offset grid | 36 × 23 from −180/−90 + ½ cell | 39 | 485 s | 35,520,554 | 460 |

Findings, each measured on the tile files rather than inferred:

- **Seams on the lattice** (above): the only cause of the 14,954 kept
  segments; moving the box reproduces the experiment's 26 exactly.
- **Penobscot Bay and Mount Desert** carry 2 to 4 times the boundary
  vertices of the experiment in a dozen tiles, from sounding squares.
  The experiment's fourth input, `ME_ENCs.mbtiles` of 2026-08-21, was
  built before the chart pipeline's finer-wins erase and stacks a
  coarse-band `DEPARE` (DRVAL1 0, DRVAL2 18.2) under the band 5 areas
  across the bay. The build takes the minimum over depth areas, so the
  experiment had depth 0 there and almost no sounding squares. The
  district file alone has the erase applied and the sounding rule works
  against the band 5 values. The district file is right; the
  experiment's input was not.
- **New Jersey shore** (lon −74.5..−74.0, lat 39.5..40.3): the
  experiment's box reached into 05CGD waters, which 01CGD does not
  chart. The derived box starts at 40.0°, so this water is outside the
  01CGD mesh and belongs to the 05CGD mesh.
- 633 of the 704 D1-box tiles are identical in every count.
- New York Harbor, Woods Hole and Massachusetts Bay probes agree with
  the experiment within 1 percent (`count-mesh-triangles.py`).

## Differences from the experiment

Everything below was agreed before it was coded; everything else is the
experiment as it was.

- **Extent**: the rectangle around the z16 cells instead of the
  hand-drawn District 1 box; split into longitude clusters when the
  district is disjoint. The cell grid is offset half a lattice cell from
  −180°/−90° so no seam lies on the sounding-grid lattice (above).
  `--box W,S,E,N` meshes an explicit rectangle instead, with the tile
  grid from its south-west corner as the experiment's was; it exists to
  reproduce the experiment and to test seam placement.
- **Projection scale**: cos of each rectangle's mid-latitude, as the
  experiment's `K` was for its box.
- **Requirement 14**: the route-ready region is the 3 × 3 block around
  the tile with the most triangles instead of a fixed Woods Hole box.
- **Gating**: the experiment printed the requirements and exited 0;
  here all but 2 and 7 gate, and requirement 15 (chart coverage
  loaded) is new: without `M_COVR` the OSM land overrides the chart
  everywhere, so a decoded input without it is never published.
- **Parameters** replace env vars and constants: output and land paths,
  `--snap-discs` (the experiment's `MESH_SNAP`), `--debug-point`
  (`MESH_DEBUG`). The old grid's cell lattice origin (−75.5, 38.7,
  0.00025°) is kept verbatim; it is index-relative arithmetic and works
  west and south of the origin unchanged.
- **MVT extent**: the decoder refuses a layer whose extent is not 4096
  instead of silently mis-scaling it.
- **Chart wins inside its coverage** (2026-10-09): the OSM land polygons
  are clipped to the outside of the charts' `M_COVR` before they enter
  the faces, the shore distance and requirement 4; the decoder now reads
  `M_COVR` from the z16 tiles for that. The experiment let OSM land
  override charted water, which cost the Great Lakes (09CGD: "no water
  in the rectangle"), the Hudson and every harbour or river behind the
  OSM coastline. `check-mesh-land-mask.py` measures the charted water a
  land mask turns into land, per 0.25° cell.
- **Requirement 3 within the snap grid** (2026-10-09): a hazard or mark
  point is looked up in the faces within GS (0.5 m) of it, not by an
  exact hit. Every input is snapped to that grid, so a point under 0.5 m
  from a tile edge could sit between the snapped edge and the tile box,
  in no face of either tile, and fail the gate although its disc was
  built and flagged (an obstruction 0.17 m east of a seam at New
  Buffalo, 09CGD).
- **Opening bridges** (2026-10-08): the experiment stored the first of
  VERCCL, VERCLR, VERCSA for every bridge, so a bascule, swing or lift
  bridge was a fixed span at its closed height (Broadway Bridge, lift,
  7.3 m although it opens to 41.1 m) and a 5 m strip across the channel
  closed the waterway for any taller mast; a bridge with no clearance
  attribute became 0 m and blocked every vessel (2,071 of the 2,978
  District 1 bridges). Now `clearance_of` reads CATBRG: an opening
  bridge sets flag bit 7 and carries VERCOP or none, a missing height
  is none. The experiment also sorted overhead pipelines and conveyors
  (PIPOHD, CONVYR) into a second column, `clear2`, that its finalize
  never wrote, so they never blocked anything; all four layers now share
  `clear`, the lowest over a face (District 1: 41 pipelines, 3 with a
  charted height, one of 8.5 m; 2 conveyors, none with a height).
- **Seam weld, T-junction split** (2026-10-10): the experiment's weld
  moves a seam vertex up to two grid steps onto the neighbour's vertex
  without looking at what the vertex's own segments now pass through.
  In New York Harbor one such move put a segment exactly through a
  neighbouring vertex, and Triangle's refinement of the merged domain
  never terminated: the four harbour squares refined in 7 s without the
  weld, not in 60 s (16 GB) with it; the whole district was killed at
  that step on 16 GB and 32 GB runners alike, while every tile refined
  on its own in 36 s in total. The water that exposed it (the Kills,
  Newark Bay, the Hudson) only became mesh water with the chart-wins
  coverage change, which is why earlier builds never hit it. After the
  weld, `split_segments_through_vertices` splits any segment ending at a
  moved vertex at the vertices that lie on it (within a grid step),
  keeping the segment's part; the seam line reports the count. With the
  fix the 8 × 8 New York block refines to 9.30 M triangles in 64 s at
  1.8 GB, ratio 1.00 to the per-tile sum (`tests/test_mesh_weld.py`).
- **Not ported**: the experiment's Python routers, grid comparison and
  live-API timing scripts, and the funnel test. Router parity against
  the saved trips is the plugin repository's test.
