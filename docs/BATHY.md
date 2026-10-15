# Bathymetry layer outside US chart coverage (design)

Status: **parked 2026-10-15** (see **Outcome**). Pilot code kept on branch `a-world-apart`, not merged.

## Purpose

The district meshes cover the waters NOAA charts. A route that leaves US
coverage has no depths. This layer supplies coarse, conservative depths
for a named region outside US coverage, published like a district: a
zoom-aware `.mbtiles` for viewing and a mesh built from it by the
existing mesh builder. The plugin uses a district mesh wherever one
covers a route and falls back to this mesh outside.

It is not a chart. No survey stands behind it; rocks, reefs and wrecks
smaller than the source resolution are absent. Every feature carries its
source, and every triangle carries a data-quality label the router can
penalise (see **Quality marking**).

## First region: Lesser Antilles and British Virgin Islands

Box 66.0°W to 59.0°W, 11.0°N to 19.0°N (the BVI through Grenada),
minus everything inside US chart coverage (07CGD's `M_COVR`, CATCOV 1,
around Puerto Rico and the USVI). Name `LANT`; outputs
`LANT_bathy.mbtiles`, `LANT_bathy_mesh/`.

Chosen for clear water, where satellite-derived depth works well, and a
popular cruising ground. Its weakness is validation: no open survey grid
covers it. The USVI, inside 07CGD and in the same water type, supplies
the chart truth for calibration and checking (see **Validation**).

## Sources

| Source | Use | Resolution | Vertical reference | Access |
|---|---|---|---|---|
| GEBCO 2026 grid | depth everywhere, the fallback | 15 arc-sec, about 460 m | not formally defined; treated as MSL | download (NetCDF), public |
| Sentinel-2 L2A | satellite-derived depth (SDB), 0 to 20 m | 10 m (B2, B3) | relative, calibrated to LAT | Google Earth Engine, `COPERNICUS/S2_SR_HARMONIZED` |
| ICESat-2 ATL24 | SDB calibration and held-out checks | along-track points | depth below the instantaneous water surface | NASA Earthdata via `earthaccess` |
| NOAA ENC, 07CGD (USVI) | validation truth, never output | chart | chart datum (MLLW) | our own published `.mbtiles` |
| OSM land polygons | land | coastline | none | the split package the mesh already uses |
| Tide model (FES or TPXO) via `pyTMD` | LAT surface; tide at ATL24 pass times | model grid | | model files (licence per model, to check) |

To verify before building: that GEBCO 2026 is released and its
terms; ATL24 coverage density in the box; which tide model `pyTMD`
can use here under a licence compatible with publishing.

## Pipeline

All artifacts under `data/bathy/LANT/`, stage by stage, each resumable.

1. **Fetch.** GEBCO subset for the box; Sentinel-2 L2A scenes
   intersecting the box over a chosen window (default the last 24
   months); ATL24 granules in the box; the 07CGD chart already on R2.
2. **Datum surfaces.** A LAT-below-MSL surface on a 0.05° grid from the
   tide model. LAT is the lowest predicted tide over 18.6 years; the
   prediction runs per grid node, once, and is cached. Where a published
   LAT surface exists it is used instead.
3. **ATL24 to LAT.** Each bathymetric photon depth is below the water
   surface at the pass time. Depth to LAT = depth − (tide at pass time −
   LAT at the point), both from `pyTMD`. Points with ATL24 quality flags
   below the recommended level are dropped.
4. **SDB composite, in Google Earth Engine.** Sentinel-2 L2A over the
   last 24 months, filtered by scene cloud cover; per-pixel masks from
   the scene classification (cloud, shadow, cirrus) and a water mask
   (NDWI); glint correction (subtract NIR scaled per scene) before
   compositing; median composite of B2 and B3. Earth Engine exports the
   two-band composite per 1° cell as a 10 m GeoTIFF to a Cloud Storage
   bucket, and the pipeline downloads it; everything after this step
   runs in Python as for the districts. Atmospheric correction: L2A as
   delivered; ACOLITE (not available in Earth Engine) is the fall-back
   only if the validation numbers say L2A water reflectance is too
   noisy. If a 1° cell has too few clean scenes in 24 months, the window
   for that cell extends to 36.
5. **SDB calibration.** ratio = ln(1000·B2) / ln(1000·B3); depth =
   m1·ratio − m0, fitted robustly (RANSAC or Huber) against ATL24 LAT
   depths, per 1° cell where enough points exist, else per region.
   20 percent of ATL24 points are held out per cell for checking.
   ICESat-2 is not in the Earth Engine catalogue, so the fit runs
   locally against the downloaded composite.
   Validity: depths from 0 to 20 m where the fit residual is acceptable;
   optically deep and turbid pixels, and pixels outside the calibrated
   ratio range, are left empty, never extrapolated.
6. **Merge to one grid per tile.** All sources to LAT and resampled to
   one grid per 0.25° tile (10 m where SDB exists, GEBCO bilinear
   elsewhere). Priority: SDB where valid, GEBCO elsewhere. In a 200 m
   band at the edge of valid SDB the shallower of the two is taken, so
   the hand-over can only err shallow.
7. **Contour.** Positive depths; `scipy.ndimage.minimum_filter` 3×3 so
   each cell takes the shallowest of its neighbours; `gdal_contour -p
   -amin DRVAL1 -amax DRVAL2 -fl 0 2 5 10 20 30 50 100 200 500 1000`.
   Depth bands keep DRVAL1 as the shallow edge, which is what the mesh
   uses.
8. **Chart-shaped layers.** The file must look like a chart to the mesh
   builder, whose rules apply unchanged:
   - `DEPARE` from step 7, with `SOURCE` (`sdb`, `gebco`, `blend`);
   - `LNDARE` from the OSM land polygons clipped to the box;
   - `M_COVR` with CATCOV 1 over the whole layer, so the builder's
     coverage rule holds (inside coverage the file's own `LNDARE` is
     land) and requirement 15 passes;
   - `M_QUAL` over the whole layer (see **Quality marking**).
   Everything is clipped to outside US `M_COVR` first, so this file and
   a district file never overlap.
9. **Tiles.** tippecanoe to a zoom-aware `.mbtiles`: GEBCO-only areas
   from z6, SDB detail to z16. The top zoom is z16 everywhere so the mesh
   builder reads it unchanged (its decoder stays z16-only). Metadata as
   the district files, `type` `S-57`, plus a `bathy` row naming the
   sources.
10. **Mesh.** `build-mesh.py LANT_bathy.mbtiles` exactly as for a
    district. Gating as for districts, plus the validation gate below.

## Quality marking

`M_QUAL` polygons with S-57 CATZOC, which the mesh already carries per
triangle (`catzoc`), so the router can prefer, penalise or forbid them
without new mesh fields:

- **CATZOC 5 (D)**: satellite-derived depths in a band that passed
  validation;
- **CATZOC 6 (U, unassessed)**: GEBCO, the blend band at the SDB edge,
  and SDB in any band that failed validation (which is not published
  as SDB; GEBCO stands in).

## Validation (gates publishing)

Measured and written to the sidecar, per depth band (0 to 2, 2 to 5,
5 to 10, 10 to 20 m):

- **Against USVI chart depths** (07CGD `DEPARE` and `SOUNDG`, converted
  from MLLW to LAT): SDB median absolute error, 90th percentile, and
  the share of points where SDB is deeper than the chart by more than
  1 m. The last is the dangerous direction.
- **Against held-out ATL24 points** across the whole region, the same
  three figures.
- **GEBCO near shore**: the same comparison in the USVI, to show what the
  fallback is worth where there is no SDB.

Threshold, per band including 0 to 2 m: at most 5 percent of check
points deeper than truth by more than 1 m; a band that fails is not
published as SDB and GEBCO stands in. These are starting values, to be
re-evaluated on the first region's results. The 0 to 2 m band is
published from SDB when it passes, like every other band.

## Pilot results (2026-10-11, square 65-64 W, 18-19 N)

Run with `bathy-pilot.py`; outputs in `data/bathy/LANT/pilot/`.

- **Composite**: 634 Sentinel-2 scenes over 24 months; Earth Engine export
  22 min, 67,133 EECU-seconds (18.6 EECU-hours) for one 1° square.
- **ICESat-2**: 214 ATL24 v002 granules, 19 GB downloaded, 1.3 M
  bathymetry photons from about 184 passes.
- **Truth**: 8,575 USVI/PR chart soundings from 07CGD at z16.
- **Fit v1** (one straight line, random point hold-out): median error
  0.5-1 m but the shallows read too deep (0-2 m: +0.7 m mean); every
  band but 5-10 m failed the 5 % rule against ICESat-2, all against the
  charts.
- **Fit v2** (shallow and deep lines blended over 4-6 m, per 10 km cell
  where 2,000 points from 3 passes exist, hold-out by whole pass): against
  held-out ICESat-2 2-5 and 5-10 m pass, 0-2 and 10-20 m just miss;
  against the charts every band fails. ICESat-2 itself reads 0.5-0.6 m
  deeper than the chart at the same spots (310 pairs within 15 m), so
  most of the chart gap is in the calibration truth, not the model: chart
  soundings are shoal-biased by design and on MLLW.
- **Decisions**: shoal correction sized on the stricter of chart and
  ICESat-2; corrected depth under 3.5 m published as 0-2 m, not
  navigable; the correction must pass a two-fold check (chart soundings
  split by 10 km area, ICESat-2 hold-out by pass, size on one half, check
  on the other, both ways), the smallest sizing margin that passes both
  folds is used and the published curve is sized on all check data.
- **Result**: margin 3 % (sizing target 2 %) was needed. Published curve
  (predicted depth, shift): 1 m 0.32, 3.5 m 0.91, 7.5 m 2.90, 15 m
  4.81. Both folds pass every band from 2 m down (chart 0.3-4.6 % too
  deep); 0-2 m is 0-5.3 %. The cost: predicted depths under about 4.4 m
  become keep-out (12 % of the satellite-depth pixels), and water reads
  on average about 2-3 m too shallow at 5-10 m and 4 m at 10-20 m.

## Outputs and publishing

Separate workflow `build-bathy.yml`, dispatched by hand per region; the
depths change slowly, so it does not follow the monthly chart build.
Outputs to R2 `bathy/` and the `latest` release, in the district
pattern: `LANT_bathy.mbtiles`, `.pmtiles`, `.json` (chart sidecar);
`LANT_bathy_mesh.tar.zst`, `LANT_bathy_mesh.json`, unpacked
`bathy/mesh/LANT/`. The release notes get a "Bathymetry outside US
coverage" section with the not-a-chart statement.

## Not in this version

The plugin's fall-back logic (separate work in the plugin repository);
regional grids (none open for this area); other regions; hazards;
continuous updates.

## Decisions (2026-10-11)

1. Quality: CATZOC D for validated SDB, U for everything else.
2. Validation threshold: 5 percent of check points more than 1 m too
   deep, per band; evaluated again after the first region.
3. Sentinel-2 window: 24 months.
4. 0 to 2 m is published from SDB when that band passes the check.
5. Compositing in Google Earth Engine.

## Set-up needed before the first run (owner's accounts)

- A Google Cloud project registered for Earth Engine. Non-commercial
  use is free; the terms must fit how the layer is used and published.
- A service account with Earth Engine access, its key stored as a
  repository secret, so the workflow can start exports without a
  personal login.
- A Cloud Storage bucket for the exported composites, readable by that
  service account.
- A NASA Earthdata login for ICESat-2, as a repository secret.

## Outcome (2026-10-15): parked

Wider test, 2026-10-14: five more 1° squares under 07CGD's charts (Puerto
Rico NW, NE, SW, SE, and St Croix) plus the pilot, tide-corrected to LAT
from the Copernicus hourly record (passes from 2022-09 only), checked
leave-one-square-out (`bathy-loso.py`). The shoal correction passed in
every held-out square only at the strictest sizing margin (4 %), and the
resulting curve (predicted depth, shift: 1 m 0.35, 3.5 m 0.58, 7.5 m
3.77, 15 m 3.92) turns everything the satellite puts under about 6 m into
keep-out and shows 5-10 m water 3-4 m too shallow. Northeast Puerto Rico
drives it: held out, the 7.5 m shift is 2.2 m instead of about 3.8 m.
Northwest Puerto Rico's fit was flat (few clear-water points, steep
shelf); southeast Puerto Rico had almost no usable check data. Earth
Engine: about 120 of the roughly 150 monthly EECU-hours for six squares.

Conclusion (user): satellite-derived depth from the Sentinel-2 band ratio
does not work well enough here: safe only when made so conservative that
it adds little, and with no way to know in advance where the next
northeast-Puerto-Rico-like water is.

Worth keeping: the chart-file pipeline (`bathy/chart.py`: merge,
three-level generalisation, international contours, soundings with SCAMIN,
S-57 record fields, coastline) and the mesh's `--only-coverage` switch work
for any depth source; ICESat-2 ATL24 depths are accurate to about half a
metre and in the pilot square put half of all 0-20 m water within 250 m of
a measured depth (96 % within 1 km), as track lines rather than areas;
a GEBCO-only layer would be an honest offshore product.

