# Navigation mesh (`build-mesh.py`)

A routing mesh of each district's charted water: a triangulation of the
water with, per triangle, the charted depth, the vertical clearance, a
hazard depth, fairway, dredged and structure flags, and a shore and depth
multiplier; per triangle side, the mark of a chart line lying on it; per
triangle corner, the mark of a chart point there. It is what the
signalk-weather-router-plus plugin routes motoring legs on.

Since November 2026 the mesh is built from the chart source cells
(S-57), cut exactly. The build it replaces read the finished chart
tiles; the reasons it was replaced are at the end.

**The tile format is `WRPMESH2`, index version 2. A router that reads
only `WRPMESH1` (index version 1) cannot open these meshes.**

## Input

One or more hand-over folders written by
`s57-to-mbtiles.py --by-band --cells-only` (`data/cells`):

- `bandN/LAYER_CELL.geojson` for every layer of every live cell of every
  band (band 6 included), always named for its cell;
- `cells.json`, the list: every cell with its band, its edition and its
  files, the inputs it was exported from, and the cancelled cells that
  were left out.

The chart tool empties the folder before it exports and writes the list
last, only when every cell exported completely; when a cell fails it
exits 1 and there is no list. So the list exists only for a whole export
and describes a folder that held nothing before it.

The mesh build reads the list and the files it names, and nothing else:
a file in the folder that is not on the list is never opened. A folder
without a list, a list of a version this build does not read, a cell
listed twice, a path that leaves the folder and a listed file that is
not there each stop the build (`listed_cells` in `mesh/build.py`). The
build does not read the chart tool's working folder (`data/geojson`),
its completion markers or its error log.

Coordinates are read in the source's own whole numbers, units of 1e-7
degree, and stay in them until the mesh is written.

The first folder is the district's own cells. The build makes every
0.25° tile those cells reach (the bounding box of each cell's
skin-of-the-earth areas). Any further folders hold other districts'
cells; in the tiles being built every folder's cells are read, so the
mesh holds all charted water of those tiles. A cell present in two
folders is read from the first.

## What the mesh says

- **Only charted water is in the mesh.** Water is where the winning
  chart has a depth area, a dredged area or an unsurveyed area and no
  land. Land and uncharted space are not triangulated: a triangle side
  with no neighbour is the shore or the edge of the charts.
- **The most detailed chart wins**, spot by spot, whichever district it
  is filed under. A chart speaks where it has a skin-of-the-earth area
  (S-57 Group 1: `DEPARE`, `DRGARE`, `UNSARE`, `LNDARE`, `FLODOC`,
  `HULKES`, `PONTON`). Bands rank 6, 5, 4, 3, 2, 1. Within bands 1 and 2
  a new-scheme cell (region code `GLB`; `ARC`, `ANT`, `ATL`, `GRL`,
  `GOM`, `PAC`) wins over a legacy cell, the chart build's Stage 2b
  rule. Only the winning chart's areas, lines and points count at a
  spot. The coverage outline (`M_COVR`) is not used. The winner is
  decided by rank (band), not by cell: a shape of a cell of the winning
  rank counts also where that cell itself has no skin-of-the-earth
  area. A correctly made chart has no feature outside its own coverage,
  so this gives the same mesh as deciding by cell; kept by rank on the
  owner's decision of 2026-11-24, and may need to be revisited.
- A pontoon, hulk or floating dock (`PONTON`, `HULKES`, `FLODOC`) with
  no depth, dredged or unsurveyed area under it is not water.
- A chart line lying exactly on a tile edge is marked by each tile from
  the charts that win on its own side; where those differ the side gets
  both records together, as a side inside a tile gets its mark from the
  charts that win on both its sides.
- **Depth** of a triangle: the shallowest `DEPARE` it lies in (`DRVAL1`,
  the shallow end of the area's range); a dredged area's depth replaces
  it; an obstruction or wreck area with a depth (`VALSOU`) lowers it. Of
  two overlapping dredged areas, or two obstruction areas, the shallower
  counts. −999 is no charted depth.
- **Clearance** of a triangle under an overhead area (`BRIDGE`,
  `CBLOHD`, `PIPOHD`, `CONVYR`): the lowest. An opening bridge (CATBRG
  opening, swing, lifting, bascule, draw, transporter) carries its open
  clearance (`VERCOP`) or none, never the closed one, and sets the
  opening flag; a span with no charted height is none (−999), not 0.
- **Chart lines stay lines.** A structure, obstruction, overhead,
  recommended-track or navigation line becomes triangle sides, marked,
  over its whole length, also where it lies in open water with the same
  water on both sides. Nothing is drawn around it.
- **Chart points stay points.** A rock, wreck, obstruction, buoy,
  beacon, light, pile or mooring is a triangle corner at its own
  position, marked with what it is and, for a hazard, its depth. A
  sounding is kept, as a marked corner with its depth, where it is
  shallower than the depth of the water it lies in (or that water has
  none).
- **Multiplier**: the shore multiplier (1.0 at 2 km or more from the
  chart's land rising to 1.3 at the shore, on a 0.00025° lattice with
  origin −75.5, 38.7; it stays 1.0 in narrow water, where no lattice
  cell of the 2 km window around the spot is 1 km or more from land)
  times the depth step (×2 under 2 m, ×1.5 under 5 m, ×1.2 under 10 m).

## Stages and files

Under the work directory (`data/mesh/<name>/`, `--work`):

| Stage | Module | Writes |
|---|---|---|
| cutter | `mesh/exact_cut.cpp` | `mesh/exact_cut`, compiled by the CLI when missing or older than its source |
| tiles | `mesh/build.py` | `cell-index.json` (the cells read, from the lists), `tiles/tile_I_J.npz` (the cut of each tile), `tiles.json` (per-tile results) |
| second stage | `mesh/finalize.py` | `<out>/<name>_mesh/mesh_III_JJJ.bin` + `index.json` |
| CLI | `mesh/cli.py` | `<out>/<name>_mesh.json`, `<out>/<name>_mesh.tar.zst` (`--archive`) |

**Tiles.** Per 0.25° tile of a fixed world lattice (origin −180°, −90°
exactly, so the date line is a tile edge): every shape of every cell that reaches the
tile is handed whole to the exact cutter, a C++ program on CGAL's exact
kernel (`Exact_predicates_exact_constructions_kernel`, constrained
Delaunay triangulation with exact intersections). Crossing points are
computed exactly; no grid, no snapping, no tolerance. Each triangle's
state is the set of shapes it is inside, found by flooding from outside
everything and toggling a shape at each piece of its outline. Whole
shapes are given, not shapes trimmed to the tile, so two neighbouring
tiles cut their shared edge identically. From the cut the tile stage
takes the water's outline pieces, one checked inner point per water part
and per hole, the kept chart points, and keeps the cut triangles for the
final check.

**Second stage**, once for the whole mesh: the tiles are joined (a side
on a shared tile edge stays only where the water differs across it or a
chart line lies on it); a corner where two pieces of the same input
segment meet with the same water and the same mark on both is not a
corner; one well-shaping run (Triangle, `pq20A`: 20° minimum angle where
the input allows) with one east-west scale for the mesh, `xScale` = cos
of its middle latitude, which only affects how "well-shaped" is judged,
never where anything is; neighbours, side and corner marks, the shore
multiplier; the checks; the files.

The second stage runs in its own interpreter, so it does not inherit the
tile stage's memory.

`exact_cut.cpp` uses CGAL's 2D triangulation package, licensed
GPL-3.0-or-later. It is a separate program run by `mesh/build.py`.

## The date line

The mesh is written as the router reads one
(signalk-weather-router-plus `src/engine/mesh/store.ts`):

- stored longitudes are between −180 and 180;
- the mesh's box is the shortest run of tile columns, going east, that
  holds its tiles (the run that starts just east of the widest stretch
  of empty columns round the earth). When that run crosses the date
  line the index's `west` is greater than its `east`, and tile columns
  `i` count east from `west` across the line;
- the date line is a tile edge and stays an edge of the mesh also where
  the same water lies on both sides: no triangle lies across it. A
  corner on it is 180 in the tile west of it and −180 in the tile east
  of it; the neighbour links join the two;
- a tile's `bbox` is plain numbers and ends on the line at the latest.

NOAA ends its cells on the date line (no cell of any district has a
polygon across it, catalogue of November 2026); a cell whose charted
areas run across it stops the build. The two tiles that share the line
are each given the other side's shapes a turn away, so both cut that
edge alike, as two tiles do anywhere else. Inside the build a mesh
across the line is worked on in one piece (the columns east of the line
one turn further east); the chart's own corners are carried as their
whole numbers throughout, and only the points the well-shaping adds
come from its arithmetic.

A district that merely sits on both sides of the line (14CGD: the
western Pacific ends at 173.5°E, the central Pacific starts at 180°)
gets the short box across the line, not the long one round the earth.
`tests/test_mesh_dateline.py`.

## Checks and exit status

Every check stops the build; there are no warnings that publish.

Tile stage (exit 1): a tile whose cut fails or contradicts itself; a
water part or hole too thin to hold a point (reported with its
position); a chart point whose corner is not at the point's own
position; a side whose mark differs between its two triangles.

Second stage (exit 2):

- a side recorded more than twice, or twice other than on a tile edge;
  two tiles' records of a tile-edge side with different marks; tile-edge
  sides that overlap (two tiles cut their shared edge differently);
- the well-shaping step did not hand back the input corners unchanged;
  a triangle across the date line, or a corner of a side on it that is
  not on it; a triangle no water part's point reaches; a clockwise or
  zero-area triangle;
- a side shared by more than two triangles; a one-sided side that is not
  on an outline; a neighbour link that is not mutual;
- a chart-line segment with no triangle side on it; a chart point that
  is not a corner of any triangle;
- the mesh's water area differs from the cut's (by more than 1e-9 of
  it);
- **every finished triangle's centre, tested exactly against the cut
  triangles, must lie in water of the same attributes**;
- a shore multiplier outside 1.0 to 1.3.

The share of triangles under 20° is printed and does not gate (sharp
corners in the chart itself cannot be widened).

Exit 0: built, every check passed, sidecar (and archive) written. On 1
or 2 no sidecar is written.

## Output

In the output directory (`-o`, default `data/tiles/`):

- `<name>_mesh/index.json`: `version` 2, `format` `WRPMESH2`, `name`,
  `west`, `south`, `east`, `north` (the rectangle of the tile lattice
  around the tiles built; `west` greater than `east` across the date
  line), `tileDeg` 0.25, `xScale`, `triangles`,
  `tiles[]` with `i`, `j`, `file`, `first`, `n`, `bbox`, and `fields`,
  `sideFlagBits`, `cornerFlagBits`. Only tiles that hold triangles have
  a file. A triangle is filed under the tile holding its centre.
- `<name>_mesh/mesh_III_JJJ.bin`, little-endian: 32-byte header
  (`WRPMESH2`, uint32 n, float64 origin lon, float64 origin lat, 4 bytes
  pad), then, n triangles each:

  | Field | Type | Meaning |
  |---|---|---|
  | corners | float64[n×6] | x = lon × xScale, y = lat, counter-clockwise |
  | neighbours | int32[n×3] | triangle id across side k (corners k, k+1), −1 none |
  | mult | float32[n] | shore × depth multiplier |
  | depth | float32[n] | metres, −999 none |
  | clear | float32[n] | metres, −999 none |
  | hazv | float32[n] | hazard area depth; 1e9 no hazard, −1e9 hazard of unknown depth |
  | rev | int8[n×3] | which side of the neighbour comes back, −1 none |
  | flags | uint8[n] | bit 0 navigable (fairway, dredged, recommended track), 1 hazard area, 4 structure area, 5 fairway, 6 dredged, 7 opening bridge. Bits 2 and 3 (mark, channel mark in `WRPMESH1`) are not set: marks are corners |
  | sideFlags | uint8[n×3] | 1 structure, 2 hazard, 4 overhead, 8 opening, 16 recommended track, 32 navigation line |
  | sideClear | float32[n×3] | clearance of an overhead line, −999 none |
  | sideHazv | float32[n×3] | depth of a hazard line; 1e9 none, −1e9 unknown |
  | cornerFlags | uint8[n×3] | 1 hazard, 2 mark, 4 channel mark (lateral buoy or beacon, daymark), 8 sounding |
  | cornerVal | float32[n×3] | depth of a hazard or sounding corner; 1e9 none, −1e9 hazard of unknown depth |

  122 bytes per triangle. Both triangles of a side carry its mark; every
  triangle at a corner carries the corner's mark.
- `<name>_mesh.json`, the sidecar: `name`, `format`, `build_date`,
  `build_seconds`, `builder_version`, `workers`, `cells` (per folder: its
  role, when its list was written and from which inputs, the cells listed
  and read by band, the cancelled cells its export left out),
  `chart_build_date` and `chart_metadata` (from `--chart-json`),
  `tile_stage` (tiles, water, land and uncharted area, water without a
  depth, sides on chart lines, chart points as corners), `clusters` (one
  entry: the mesh's rectangle), `triangles`, `mesh_tiles`, `bytes`,
  `passed`. `mesh-catalog.py` builds the catalogue entry from it.
- `<name>_mesh.tar.zst` with `--archive`; its top folder is
  `<name>_mesh/`.

A district is one mesh with one rectangle and one `xScale`, also where
its water lies far apart (07CGD: Florida and Puerto Rico in one mesh).

## Running locally

```bash
pip install -r mesh/requirements.txt     # numpy scipy shapely triangle
# a C++17 compiler with CGAL, GMP and MPFR (Homebrew: cgal; Ubuntu: libcgal-dev libgmp-dev libmpfr-dev)
./s57-to-mbtiles.py 01CGD_ENCs.zip --by-band --cells-only              # the cells and their list, in data/cells (no tiles)
./build-mesh.py data/cells /path/to/05CGD/cells --name 01CGD -o data/tiles -j 7 --archive
```

Every path given is resolved once (symbolic links followed) and checked
once: the build removes `<work>/tiles` and `<out>/<name>_mesh`, and no
folder it is given may be one of those or lie inside one.

Options: `--name` (required), `-o`, `--work`, `-j/--workers`,
`--chart-json FILE` (the published chart's metadata; its `build_date`
becomes `chart_build_date`), `--archive`, `--keep-intermediate` (keep
`<work>/tiles`, several GB for a district).

## Tests

- `tests/test_mesh_clearance.py`: the clearance rule.
- `tests/test_mesh_rules.py`: what a piece of water carries from the
  shapes it lies in; the cell index (the list is the only source; order,
  new scheme before legacy; a folder with no list, a listed file that is
  missing and a list that cannot be read are refused).
- `tests/test_mesh_build.py`: `build-mesh.py` on hand-made cells, read
  back from the mesh files: only charted water, the most detailed chart
  wins across folders, lines as marked sides over their whole length,
  points as marked corners, soundings, the format, the sidecar. Needs
  the mesh packages and CGAL.
- `tests/test_mesh_pipeline.py`: `--cells-only` on the five-cell fixture
  (the folder holds the list and what it names and nothing else), then
  `build-mesh.py` on it; a cell that fails to export leaves no list and
  the mesh build refuses the folder. Needs GDAL, the mesh packages and
  CGAL.
- `tests/test_mesh_catalog.py`: the catalogue writer.

## CI: `.github/workflows/build-mesh.yml`

**Rewritten for this build on 2026-11-24 and not yet run.** Until a test
run has passed, nothing below is verified on a runner (in particular the
CGAL version Ubuntu installs: the cutter was developed with CGAL 6.1.1).

Runs after every "Build ENC Charts" run on the default branch, and on
demand. For each district in the `mesh.active` list of `enc-sources.yaml`
the plan compares the published chart's `build_date`
(`charts/<D>_ENCs.json`) with the mesh's `chart_build_date`
(`charts/<D>_mesh.json`) and rebuilds when the chart is newer or no mesh
exists; `force` rebuilds regardless; a named `builds` list restricts the
set; a test run always builds.

- `cells`: one job per district of `mesh.active` (all of them, whichever
  meshes are built), on `ubuntu-latest`: download NOAA's district zip,
  `s57-to-mbtiles.py --by-band --cells-only` (the chart build's own first
  stages: staging, cancelled cells, replacement cells; then the export of
  every band into `data/cells` with its list; no tiles), and upload
  `data/cells` as the artifact `cells-<D>` (kept one day).
- `build`: one job per mesh on the runner named by `mesh.runner`, after
  every `cells` job has succeeded: install CGAL, GMP, MPFR and the
  packages, unpack every district's cells, fetch the chart's record, run
  `build-mesh.py cells/<D>/cells <every other district's> --name <D>
  --chart-json … --archive`, upload `<D>_mesh.tar.zst` and
  `<D>_mesh.json`. A failed check fails the job and nothing of that
  district is published. If any district's cells are missing the build
  stops rather than build without them.
- `release` and `test-stage` are as before: `<D>_mesh.tar.zst` and
  `<D>_mesh.json` to the `latest` release and R2 `charts/`, the unpacked
  folder to `charts/mesh/<D>/`, `charts/mesh/index.json` by
  `mesh-catalog.py`, the notes by `release-notes.sh`; a test run stages
  to `charts-test/<branch>/` and touches nothing published.

The cells are exported from NOAA's zips at the time of the mesh run, so
they can be newer than the cells the published chart was built from;
`chart_build_date` records which chart build triggered the mesh.

**A release run publishes `WRPMESH2` meshes under the existing names in
`charts/` and in the catalogue. A plugin that reads only `WRPMESH1`
downloads the newer mesh over its working copy and then cannot open it.
Do not run `release` (and do not let a chart release trigger this
workflow from the default branch) before the router reads `WRPMESH2`.**

## Why the build from the chart tiles was replaced

Measured on District 1 in November 2026:

- The old mesh filled its whole rectangle; water no chart covers was in
  it as water of unknown depth.
- The tiles are not the charts: along 16.4 % of the land/depth border
  at zoom 16 (9.5 % at zoom 14, 5.4 % at zoom 12) the two sides of the
  border are not the same line. In the source cells they are (0.00 to
  0.01 %, overlap 0.0 m²).
- The tile reader converted tile pixels to latitude linearly instead of
  by the Web Mercator formula: zoom 9 tiles were placed 40 to 63 m off,
  zoom 10 tiles 16.8 m.
- Everything was snapped to a 0.5 m grid, and pieces were then labelled
  by one point against the unsnapped shapes: about 0.29 km² per district
  on the wrong side of a line. Without a grid the geometry library
  (GEOS) does not cut exactly: uncut crossings and hair-thin pieces.
- Lines were drawn as 3 m strips and points as 20 m discs; their corners
  fell a hair from chart lines and stopped the well-shaping step.

The new build removes each cause: source cells, exact cutting with no
grid, lines as sides and points as corners, only charted water.
