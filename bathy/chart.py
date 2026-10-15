"""Turn the pilot's depths into a chart-shaped, zoom-aware .mbtiles
(docs/BATHY.md pipeline steps 6-9), for one work square.

  merge    10 m grid in the satellite raster's UTM frame: shoal-corrected
           SDB with its keep-out (sdb_final.tif) where valid, GEBCO 2026
           (bilinear, depth = -elevation, MSL) elsewhere, and in a 200 m
           band inside the SDB edge the shallower of the two; then a 3x3
           minimum filter so every cell takes its shallowest neighbour.
           Writes merged.tif (depth, m) and source.tif (1 SDB, 2 blend,
           3 GEBCO).
  vector   gdal_contour -p into depth areas (DRVAL1, DRVAL2) at
           0 2 5 10 20 30 50 100 200 500 1000 2000 5000 10000 m,
           simplified by 5 m; split by the source polygons (SOURCE sdb,
           blend, gebco); minus OSM land; everything clipped to outside
           US chart coverage (M_COVR CATCOV 1 of the given district file).
           Layers written as GeoJSON in EPSG:4326:
             DEPARE  DRVAL1, DRVAL2, SOURCE
             LNDARE  OSM land inside the square
             M_COVR  CATCOV 1 over the square minus US coverage
             M_QUAL  CATZOC 5 (D) where SDB, 6 (U) elsewhere, SOURCE
  soundings  SOUNDG points from merged.tif: per square the shallowest cell,
           DEPTH rounded down to 0.1 m; squares 2 km (from z10), 1 km
           (z12), 500 m (z13), 250 m (z14), 125 m (z15); none in keep-out,
           on land or outside the file's coverage.
  tiles    tippecanoe, z6..z16, into <name>.mbtiles, metadata type S-57;
           --drop-rate 1 so the soundings' own minzoom decides presence.

Not in this test build (stated in the metadata): tide/datum conversion
to LAT (the Virgin Islands are microtidal; the SDB carries the shoal
correction measured against MLLW chart soundings, GEBCO is MSL).
"""
import argparse
import gzip
import json
import math
import os
import sqlite3
import subprocess
import sys
import time

import numpy as np

LEVELS = [0, 2, 5, 10, 20, 30, 50, 100, 200, 500, 1000, 2000, 5000, 10000]   # international metric series (decided 2026-10-11)
BLEND_PX = 20             # 200 m at 10 m
SIMPLIFY_M = 5.0
GEN_M = 40.0              # generalisation grid: 10 m merged grid aggregated by its minimum
MIN_AREA_M2 = 10000.0     # 1 ha: smaller patches deeper than a neighbour merge into the shallowest neighbour
MIN_LOOP_M = 200.0        # closed contour loops shorter than this are dropped (their area stays)
# Generalisation per zoom range (2026-10-11): charts are generalised more at
# smaller scales. Each level: grid (m, minimum of the 10 m cells), smallest
# patch kept when it is deeper than a neighbour (m2), shortest closed contour
# loop (m), line simplification (m), smallest quality patch (m2), zooms.
GEN_LEVELS = [
    {"name": "detail", "grid": 40.0, "min_area": 1e4, "min_loop": 200.0, "simplify": 5.0, "q_area": 5e4, "zmin": 13, "zmax": 16},
    {"name": "mid", "grid": 200.0, "min_area": 25e4, "min_loop": 2000.0, "simplify": 25.0, "q_area": 25e4, "zmin": 10, "zmax": 12},
    {"name": "coarse", "grid": 1000.0, "min_area": 4e6, "min_loop": 10000.0, "simplify": 125.0, "q_area": 4e6, "zmin": 6, "zmax": 9},
]
OBJL = {"DEPARE": 42, "DEPCNT": 43, "SOUNDG": 129, "COALNE": 30, "LNDARE": 71, "M_COVR": 302, "M_QUAL": 308}
GRUP = {"DEPARE": 1, "LNDARE": 1}          # skin of the earth; everything else group 2


def log(m):
    print(m, flush=True)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{cmd[0]} failed: {r.stderr.strip()[-800:]}")
    return r


# ---------------------------------------------------------------- merge

def stage_merge(work, pilot, gebco):
    import rasterio
    from scipy.ndimage import binary_erosion, minimum_filter
    sdb_path = os.path.join(pilot, "sdb_final.tif")
    with rasterio.open(sdb_path) as s:
        prof = s.profile.copy(); b = s.bounds; crs = s.crs; res = s.res
        S = s.read(1).astype(np.float32); S[S == s.nodata] = np.nan
    g_utm = os.path.join(work, "gebco_utm.tif")
    run(["gdalwarp", "-q", "-overwrite", "-t_srs", str(crs), "-te", str(b.left), str(b.bottom), str(b.right), str(b.top),
         "-tr", str(res[0]), str(res[1]), "-r", "bilinear", "-dstnodata", "-32767", "-ot", "Float32",
         "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", "-co", "BIGTIFF=IF_SAFER", gebco, g_utm])
    with rasterio.open(g_utm) as g:
        G = g.read(1).astype(np.float32); G[G == g.nodata] = np.nan
    G = np.maximum(-G, 0.0)                         # elevation -> depth; land and drying -> 0
    valid = np.isfinite(S)
    interior = binary_erosion(valid, iterations=BLEND_PX)
    band = valid & ~interior
    D = G.copy()
    D[interior] = S[interior]
    D[band] = np.fmin(S[band], G[band])
    src = np.full(D.shape, 3, np.uint8); src[interior] = 1; src[band] = 2
    big = np.float32(1e6)
    D = np.where(np.isfinite(D), D, big)
    D = minimum_filter(D, size=3).astype(np.float32)
    D[D >= big] = np.nan
    prof.update(dtype="float32", nodata=-9999.0, count=1, compress="deflate", tiled=True, BIGTIFF="IF_SAFER")
    with rasterio.open(os.path.join(work, "merged.tif"), "w", **prof) as d:
        d.write(np.where(np.isfinite(D), D, -9999.0).astype(np.float32), 1)
    prof.update(dtype="uint8", nodata=0)
    with rasterio.open(os.path.join(work, "source.tif"), "w", **prof) as d:
        d.write(src, 1)
    log(f"merge: {D.shape[1]}x{D.shape[0]} cells; SDB {interior.mean():.1%}, blend {band.mean():.1%}, GEBCO {(src == 3).mean():.1%}; "
        f"keep-out cells (depth 0 from SDB) {(valid & (S == 0)).mean():.1%}")


# ---------------------------------------------------------------- generalize

def generalize_bands(B, min_cells, max_passes=8):
    """Merge small patches that are deeper than a neighbour into their
    shallowest neighbour's band; small shoals are kept. B: band index per
    cell (higher = deeper), 255 = no data. Returns (B, merged patches)."""
    from scipy.ndimage import label, minimum_filter, minimum
    B = B.copy()
    total = 0
    for _ in range(max_passes):
        M = minimum_filter(np.where(B == 255, 254, B), size=3)
        changed = 0
        for b in np.unique(B):
            if b == 255:
                continue
            lab, n = label(B == b)
            if not n:
                continue
            sizes = np.bincount(lab.ravel())
            small = np.flatnonzero(sizes < min_cells)
            small = small[small > 0]
            if not len(small):
                continue
            nmin = np.asarray(minimum(M, lab, small))
            go = small[nmin < b]
            if not len(go):
                continue
            newb = np.zeros(n + 1, dtype=np.int16) - 1
            newb[go] = nmin[nmin < b]
            sel = newb[lab]
            hit = sel >= 0
            B[hit] = sel[hit].astype(B.dtype)
            changed += len(go)
        total += changed
        if not changed:
            break
    return B, total


def _aggregate_min(D, f):
    H, W = (D.shape[0] // f) * f, (D.shape[1] // f) * f
    big = np.float32(1e9)
    return np.where(np.isfinite(D[:H, :W]), D[:H, :W], big).reshape(H // f, f, W // f, f).min(axis=(1, 3))


def stage_generalize(work):
    """One generalised band grid per level (gen_<level>.tif with the band
    midpoint, band_<level>.tif with the band index) plus the quality grid
    (qual_<level>.tif: 5 D where SDB, 6 U elsewhere, small D patches merged
    into U)."""
    import rasterio
    t0 = time.time()
    with rasterio.open(os.path.join(work, "merged.tif")) as m:
        D = m.read(1).astype(np.float32); prof0 = m.profile.copy(); T = m.transform; res = m.res[0]
        D[D == m.nodata] = np.nan
    with rasterio.open(os.path.join(work, "source.tif")) as sr:
        SRC = sr.read(1)
    mids = np.array([(LEVELS[i] + LEVELS[i + 1]) / 2.0 for i in range(len(LEVELS) - 1)] + [-9999.0], dtype=np.float32)
    for lv in GEN_LEVELS:
        f = int(round(lv["grid"] / res))
        A = _aggregate_min(D, f)
        nod = A >= 1e9
        B = (np.searchsorted(np.array(LEVELS, float), A, side="right") - 1).clip(0, len(LEVELS) - 2).astype(np.uint8)
        B[nod] = 255
        B2, merged = generalize_bands(B, int(math.ceil(lv["min_area"] / lv["grid"] ** 2)))
        V = mids[np.where(B2 == 255, len(LEVELS) - 1, B2)]
        # quality: a cell is D only if every 10 m cell under it is SDB (worse quality wins), then
        # small D patches merge into U (value 1 = D, 0 = U; generalize_bands merges higher into lower)
        H, W = B2.shape[0] * f, B2.shape[1] * f
        sdb_all = (SRC[:H, :W] == 1).reshape(B2.shape[0], f, B2.shape[1], f).all(axis=(1, 3))
        Q = np.where(nod, 255, sdb_all.astype(np.uint8))
        Q2, qmerged = generalize_bands(Q, int(math.ceil(lv["q_area"] / lv["grid"] ** 2)))
        prof = prof0.copy()
        prof.update(width=B2.shape[1], height=B2.shape[0], transform=rasterio.Affine(T.a * f, T.b, T.c, T.d, T.e * f, T.f),
                    dtype="float32", nodata=-9999.0, compress="deflate", tiled=False)
        with rasterio.open(os.path.join(work, f"gen_{lv['name']}.tif"), "w", **prof) as d:
            d.write(V, 1)
        prof.update(dtype="uint8", nodata=255)
        with rasterio.open(os.path.join(work, f"band_{lv['name']}.tif"), "w", **prof) as d:
            d.write(B2, 1)
        with rasterio.open(os.path.join(work, f"qual_{lv['name']}.tif"), "w", **prof) as d:
            d.write(np.where(Q2 == 255, 255, np.where(Q2 == 1, 5, 6)).astype(np.uint8), 1)
        log(f"generalize {lv['name']}: {B2.shape[1]}x{B2.shape[0]} cells of {lv['grid']:.0f} m; {merged:,} deep patches under "
            f"{lv['min_area'] / 1e4:g} ha merged; {qmerged:,} small D quality patches merged into U")
    log(f"generalize: {time.time()-t0:.0f} s")


# ---------------------------------------------------------------- vector

def _us_coverage(mbtiles, box):
    """Union of M_COVR CATCOV 1 inside box from a district file's z16 tiles."""
    import mapbox_vector_tile
    import shapely
    from shapely.geometry import shape
    Z = 16; n = 2 ** Z
    tx0 = int((box[0] + 180) / 360 * n); tx1 = int((box[2] + 180) / 360 * n)
    def ty(lat):
        r = math.radians(lat); return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    y0, y1 = ty(box[3]), ty(box[1])
    con = sqlite3.connect(mbtiles)
    rows = con.execute("SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level=? AND tile_column BETWEEN ? AND ? "
                       "AND tile_row BETWEEN ? AND ?", (Z, tx0, tx1, (n - 1) - y1, (n - 1) - y0))
    parts = []
    for x, y_tms, data in rows:
        y = (n - 1) - y_tms
        w = x / n * 360 - 180; e = (x + 1) / n * 360 - 180
        nl = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
        sl = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
        t = mapbox_vector_tile.decode(gzip.decompress(data), y_coord_down=True)
        lay = t.get("M_COVR")
        if not lay:
            continue
        ext = lay.get("extent", 4096)
        for f in lay["features"]:
            if f["properties"].get("CATCOV") != 1:
                continue
            g = shapely.transform(shape(f["geometry"]), lambda c: c * np.array([(e - w) / ext, -(nl - sl) / ext]) + np.array([w, nl]))
            g = shapely.intersection(shapely.make_valid(g), shapely.box(w, sl, e, nl))
            if not g.is_empty:
                parts.append(g)
    if not parts:
        return None
    return shapely.union_all(np.array(parts, dtype=object), grid_size=1e-7)


def _polys(g):
    import shapely
    return [p for p in shapely.get_parts(g) if p.geom_type == "Polygon" and not p.is_empty]


_RCID = [0]
SORDAT = time.strftime("%Y%m%d")


def s57_record(layer, prim, sorind, props):
    """The standard S-57 record fields an ENC feature carries (07CGD:
    RCID, PRIM, GRUP, OBJL, RVER, AGEN, LNAM, SORDAT, SORIND), in front of
    the feature's own attributes. AGEN 0 and LNAM from RCID: this is not
    an agency's chart."""
    _RCID[0] += 1
    r = _RCID[0]
    rec = {"RCID": r, "PRIM": prim, "GRUP": GRUP.get(layer, 2), "OBJL": OBJL[layer], "RVER": 1, "AGEN": 0,
           "LNAM": f"0000{r:08X}0001", "SORDAT": SORDAT, "SORIND": sorind}
    rec.update({k: v for k, v in props.items() if not k.startswith("_")})
    return rec


def _feature(layer, prim, sorind, props, geom):
    f = {"type": "Feature", "properties": s57_record(layer, prim, sorind, props), "geometry": geom}
    if "_minzoom" in props:
        f["tippecanoe"] = {"minzoom": props["_minzoom"]}
    return f


def _write_geojson(path, feats, layer, sorind):
    import shapely
    with open(path, "w") as f:
        f.write('{"type":"FeatureCollection","features":[\n')
        first = True
        for g, props in feats:
            for p in _polys(g) if g.geom_type != "Polygon" else [g]:
                if p.is_empty:
                    continue
                if not first:
                    f.write(",\n")
                f.write(json.dumps(_feature(layer, 3, sorind, props, json.loads(shapely.to_geojson(p, indent=None)))))
                first = False
        f.write("\n]}\n")


def _write_lines(path, feats, layer, sorind):
    import shapely
    with open(path, "w") as f:
        f.write('{"type":"FeatureCollection","features":[\n')
        first = True
        for g, props in feats:
            for p in shapely.get_parts(shapely.line_merge(g) if g.geom_type in ("MultiLineString",) else g):
                if p.is_empty or p.geom_type != "LineString":
                    continue
                if not first:
                    f.write(",\n")
                f.write(json.dumps(_feature(layer, 2, sorind, props, json.loads(shapely.to_geojson(p, indent=None)))))
                first = False
        f.write("\n]}\n")


def scamin_for(z):
    """SCAMIN for a feature first shown at zoom z: just above that zoom's
    scale (Web Mercator, about 559,082,264 / 2^z), ending in 9 as ENC values do."""
    return int(math.ceil(559082264.0 / 2 ** z * 1.05 / 10000.0) * 10000 - 1)


def contour_minzoom(v):
    return 11 if v <= 10 else (9 if v <= 50 else 6)


def contour_scamin(v):
    return scamin_for(contour_minzoom(v))


def _load(path, fields):
    """Features of a vector file as (shapely geometries, property dicts), via ogr2ogr GeoJSONSeq."""
    import shapely
    out = run(["ogr2ogr", "-f", "GeoJSONSeq", "/vsistdout/", path]).stdout
    geoms, props = [], []
    for line in out.splitlines():
        line = line.strip().lstrip("\x1e")
        if not line:
            continue
        f = json.loads(line)
        if not f.get("geometry"):
            continue
        geoms.append(shapely.make_valid(shapely.from_geojson(json.dumps(f["geometry"]))))
        props.append({k: f["properties"].get(k) for k in fields})
    return np.array(geoms, dtype=object), props


def _fresh(path):
    if os.path.exists(path):
        os.remove(path)
    return path


def stage_vector(work, box, land_shp, us_mbtiles):
    import shapely
    t0 = time.time()
    sq = shapely.box(*box)
    us = _us_coverage(us_mbtiles, box) if us_mbtiles else None
    area = sq if us is None else shapely.difference(sq, us)
    log(f"vector: US chart coverage in the square {0 if us is None else us.area / sq.area:.1%}; "
        f"this file covers {area.area / sq.area:.1%}")
    land_full = os.path.join(work, "land_ll.gpkg")
    run(["ogr2ogr", "-overwrite", "-spat", *[str(v) for v in box], "-clipsrc", *[str(v) for v in box],
         "-nlt", "MULTIPOLYGON", "-makevalid", land_full, land_shp])
    kx = math.cos(math.radians((box[1] + box[3]) / 2)) * 111320.0
    for lv in GEN_LEVELS:
        nm = lv["name"]
        tag = f"generalised {lv['grid']:.0f} m"
        dep_utm = _fresh(os.path.join(work, f"depare_{nm}_utm.gpkg")); dep_ll = os.path.join(work, f"depare_{nm}_ll.gpkg")
        run(["gdal_contour", "-q", "-p", "-amin", "DRVAL1", "-amax", "DRVAL2", "-fl", *[str(v) for v in LEVELS],
             os.path.join(work, f"gen_{nm}.tif"), dep_utm])
        run(["ogr2ogr", "-overwrite", "-simplify", str(lv["simplify"]), "-t_srs", "EPSG:4326", "-nlt", "MULTIPOLYGON",
             "-makevalid", dep_ll, dep_utm])
        cnt_utm = _fresh(os.path.join(work, f"depcnt_{nm}_utm.gpkg")); cnt_ll = os.path.join(work, f"depcnt_{nm}_ll.gpkg")
        run(["gdal_contour", "-q", "-a", "VALDCO", "-fl", *[str(v) for v in LEVELS if v > 0],
             os.path.join(work, f"gen_{nm}.tif"), cnt_utm])
        run(["ogr2ogr", "-overwrite", "-simplify", str(lv["simplify"]), "-t_srs", "EPSG:4326", "-nlt", "MULTILINESTRING",
             cnt_ll, cnt_utm])
        q_utm = _fresh(os.path.join(work, f"qual_{nm}_utm.gpkg")); q_ll = os.path.join(work, f"qual_{nm}_ll.gpkg")
        run(["gdal_polygonize.py", "-q", os.path.join(work, f"qual_{nm}.tif"), "-f", "GPKG", q_utm, "qual", "CATZOC"])
        run(["ogr2ogr", "-overwrite", "-simplify", str(lv["simplify"]), "-t_srs", "EPSG:4326", "-makevalid", q_ll, q_utm])
        land_lv = os.path.join(work, f"land_{nm}_ll.gpkg")
        run(["ogr2ogr", "-overwrite", "-simplify", str(lv["simplify"] / 111320.0), "-makevalid", "-nlt", "MULTIPOLYGON",
             land_lv, land_full])
        land, _ = _load(land_lv, [])
        land_u = shapely.union_all(land) if len(land) else shapely.Polygon()
        water = shapely.difference(area, land_u)
        shapely.prepare(water)

        dep, dprops = _load(dep_ll, ["DRVAL1", "DRVAL2"])
        feats_dep = []
        for g, p in zip(dep, dprops):
            g = shapely.intersection(g, water)
            if not g.is_empty and g.area > 0:
                feats_dep.append((g, {"DRVAL1": float(p["DRVAL1"]), "DRVAL2": float(p["DRVAL2"])}))
        cnt, cprops = _load(cnt_ll, ["VALDCO"])
        feats_cnt, dropped = [], 0
        for g, p in zip(cnt, cprops):
            parts = []
            for ln in shapely.get_parts(g):
                if ln.is_closed and shapely.length(shapely.transform(ln, lambda c: c * np.array([kx, 111320.0]))) < lv["min_loop"]:
                    dropped += 1
                    continue
                parts.append(ln)
            if not parts:
                continue
            gg = shapely.intersection(shapely.multilinestrings(parts), water)
            if not gg.is_empty:
                v = float(p["VALDCO"])
                feats_cnt.append((gg, {"VALDCO": v, "SCAMIN": contour_scamin(v), "_minzoom": contour_minzoom(v)}))
        qg, qprops = _load(q_ll, ["CATZOC"])
        feats_q = []
        for g, p in zip(qg, qprops):
            if p["CATZOC"] not in (5, 6):
                continue
            g = shapely.intersection(g, water)
            if not g.is_empty and g.area > 0:
                feats_q.append((g, {"CATZOC": int(p["CATZOC"]), "SOURCE": "sdb" if p["CATZOC"] == 5 else "gebco or blend"}))
        coast = shapely.intersection(shapely.boundary(land_u), sq.buffer(-1e-7))
        lay = os.path.join(work, f"layers_{nm}"); os.makedirs(lay, exist_ok=True)
        _write_geojson(os.path.join(lay, "DEPARE.geojson"), feats_dep, "DEPARE", f"LANT,SDB+GEBCO,{tag}")
        _write_geojson(os.path.join(lay, "LNDARE.geojson"), [(shapely.intersection(land_u, area), {})], "LNDARE", "LANT,OSM,land")
        _write_geojson(os.path.join(lay, "M_COVR.geojson"), [(area, {"CATCOV": 1})], "M_COVR", "LANT,coverage")
        _write_geojson(os.path.join(lay, "M_QUAL.geojson"), feats_q, "M_QUAL", f"LANT,quality,{tag}")
        _write_lines(os.path.join(lay, "DEPCNT.geojson"), feats_cnt, "DEPCNT", f"LANT,SDB+GEBCO,{tag}")
        _write_lines(os.path.join(lay, "COALNE.geojson"), [(coast, {})], "COALNE", "LANT,OSM,coastline")
        n = {k: sum(1 for _ in open(os.path.join(lay, f"{k}.geojson"))) - 2 for k in ("DEPARE", "DEPCNT", "M_QUAL", "LNDARE", "COALNE")}
        log(f"vector {nm} (z{lv['zmin']}-{lv['zmax']}): {n}; {dropped:,} contour loops under {lv['min_loop']:.0f} m dropped")
    log(f"vector: {time.time()-t0:.0f} s")


# ---------------------------------------------------------------- soundings

# Charted-style soundings from the merged grid (agreed 2026-10-11): one per
# square, at the square's SHALLOWEST cell, rounded DOWN to 0.1 m; squares
# shrink with zoom and every zoom keeps the coarser zooms' soundings. No
# soundings in keep-out (SDB depth 0) or on land, nor outside this file's
# coverage.
SOUNDING_LEVELS = [(4000, 11), (1000, 12), (500, 13), (250, 14), (125, 15)]   # (square m, first zoom); z11 start, 4 km (2026-10-11)
STRIP_M = 2000
DEEP_M = 20.0             # deeper than this: double spacing (charts thin deep water)
GEBCO_MIN_M = 500.0       # no GEBCO soundings closer than GEBCO's own cell (about 460 m)
MIN_SOUNDING_M = 1.0      # no soundings shallower than 1 m (shown as "0"); the 0-2 m area covers them


def stage_soundings(work, pilot):
    import rasterio
    import shapely
    from rasterio.warp import transform
    t0 = time.time()
    m = rasterio.open(os.path.join(work, "merged.tif"))
    srcr = rasterio.open(os.path.join(work, "source.tif"))
    sdb = rasterio.open(os.path.join(pilot, "sdb_final.tif"))
    res = m.res[0]
    rows_per = int(round(STRIP_M / res))
    chosen = {}                                   # (row, col) -> (depth, minzoom, source)
    for r0 in range(0, m.height, rows_per):
        h = min(rows_per, m.height - r0)
        win = rasterio.windows.Window(0, r0, m.width, h)
        D = m.read(1, window=win).astype(float)
        K = sdb.read(1, window=win)
        S = srcr.read(1, window=win)
        ok = (D != m.nodata) & (D > 0) & (K != 0)          # K == 0: SDB keep-out
        rr, cc = np.nonzero(ok)
        if not len(rr):
            continue
        v = D[rr, cc]; gr = rr + r0
        sv = S[rr, cc]
        keep0 = np.floor(v * 10) / 10.0 >= MIN_SOUNDING_M     # nothing under 1 m: the 0-2 m area says it
        rr, cc, v, gr, sv = rr[keep0], cc[keep0], v[keep0], gr[keep0], sv[keep0]
        if not len(rr):
            continue
        for size, z in SOUNDING_LEVELS:
            # effective square: double below DEEP_M, never under GEBCO_MIN_M where the depth is GEBCO's
            eff = np.where(v > DEEP_M, 2 * size, size).astype(float)
            eff = np.where(sv == 3, np.maximum(eff, GEBCO_MIN_M), eff)
            k = (eff.astype(np.int64) * 10 ** 14 + np.floor(gr * res / eff).astype(np.int64) * 10_000_000
                 + np.floor(cc * res / eff).astype(np.int64))
            order = np.lexsort((v, k))
            first = order[np.concatenate([[True], k[order][1:] != k[order][:-1]])]
            for i in first:
                key = (int(gr[i]), int(cc[i]))
                if key not in chosen:
                    chosen[key] = (float(v[i]), z, int(sv[i]))
    keys = np.array(list(chosen.keys()), dtype=np.int64)
    vals = list(chosen.values())
    T = m.transform
    xs = T.c + (keys[:, 1] + 0.5) * T.a; ys = T.f + (keys[:, 0] + 0.5) * T.e
    lon, lat = transform(m.crs, "EPSG:4326", list(xs), list(ys))
    lon, lat = np.asarray(lon), np.asarray(lat)
    # water of this file: coverage minus land
    def load(name):
        fc = json.load(open(os.path.join(work, "layers_detail", f"{name}.geojson")))
        gs = [shapely.from_geojson(json.dumps(f["geometry"])) for f in fc["features"]]
        return shapely.union_all(np.array(gs, dtype=object)) if gs else shapely.Polygon()
    water = shapely.difference(load("M_COVR"), load("LNDARE"))
    shapely.prepare(water)
    inside = shapely.contains_xy(water, lon, lat)
    # never deeper than the generalised depth area the sounding sits in
    with rasterio.open(os.path.join(work, "band_detail.tif")) as bt:
        bx, by = transform("EPSG:4326", bt.crs, list(lon), list(lat))
        bands = np.array([v[0] for v in bt.sample(zip(bx, by))])
    drval2 = np.array(LEVELS[1:] + [LEVELS[-1]], float)[np.minimum(bands, len(LEVELS) - 1)]
    depths = np.array([v[0] for v in vals])
    too_deep = (bands != 255) & (np.floor(depths * 10) / 10.0 > drval2)
    log(f"soundings: {int((inside & too_deep).sum()):,} dropped as deeper than their generalised depth area")
    inside &= ~too_deep
    name = {1: "sdb", 2: "blend", 3: "gebco"}
    nz = {}
    os.makedirs(os.path.join(work, "layers_snd"), exist_ok=True)
    with open(os.path.join(work, "layers_snd", "SOUNDG.geojson"), "w") as f:
        f.write('{"type":"FeatureCollection","features":[\n')
        first = True
        for i in np.flatnonzero(inside):
            d, z, sc = vals[i]
            depth = math.floor(d * 10) / 10.0                  # rounded down: never deeper than the grid
            src = name.get(sc, "gebco")
            feat = _feature("SOUNDG", 1, f"LANT,{src.upper()},sounding",
                            {"DEPTH": depth, "SCAMIN": scamin_for(z), "SOURCE": src, "_minzoom": z},
                            {"type": "Point", "coordinates": [round(float(lon[i]), 7), round(float(lat[i]), 7)]})
            f.write(("" if first else ",\n") + json.dumps(feat)); first = False
            nz[z] = nz.get(z, 0) + 1
        f.write("\n]}\n")
    log(f"soundings: {int(inside.sum()):,} written ({len(chosen) - int(inside.sum()):,} dropped outside the file's water); "
        f"first zoom: " + ", ".join(f"z{z} {n:,}" for z, n in sorted(nz.items())) + f"; {time.time()-t0:.0f} s")


# ---------------------------------------------------------------- tiles

def stage_tiles(work, name, minzoom, maxzoom):
    """One tippecanoe run per generalisation level over its own zooms, one
    for the soundings (z10-16), then tile-join into <name>.mbtiles. The
    zoom ranges of the levels do not overlap, so no layer is merged with
    another level's copy of itself."""
    t0 = time.time()
    parts = []
    common = ["--no-tile-size-limit", "--no-feature-limit", "--detect-shared-borders", "--drop-rate", "1", "-f", "-q"]
    for lv in GEN_LEVELS:
        lay = os.path.join(work, f"layers_{lv['name']}")
        out = os.path.join(work, f"part_{lv['name']}.mbtiles")
        cmd = ["tippecanoe", "-o", out, "-Z", str(max(minzoom, lv["zmin"])), "-z", str(min(maxzoom, lv["zmax"]))] + common
        for L in ("DEPARE", "DEPCNT", "COALNE", "LNDARE", "M_COVR", "M_QUAL"):
            cmd += ["-L", f"{L}:{os.path.join(lay, L + '.geojson')}"]
        run(cmd); parts.append(out)
    snd = os.path.join(work, "part_snd.mbtiles")
    run(["tippecanoe", "-o", snd, "-Z", str(min(z for _, z in SOUNDING_LEVELS)), "-z", str(maxzoom)] + common
        + ["-L", f"SOUNDG:{os.path.join(work, 'layers_snd', 'SOUNDG.geojson')}"])
    parts.append(snd)
    out = os.path.join(work, f"{name}.mbtiles")
    if os.path.exists(out):
        os.remove(out)
    run(["tile-join", "-q", "-f", "--no-tile-size-limit", "-n", name, "-o", out] + parts)
    con = sqlite3.connect(out)
    meta = {"type": "S-57", "description": f"{name}: satellite-derived and GEBCO depths, not a chart",
            "minzoom": str(minzoom), "maxzoom": str(maxzoom),
            "bathy": json.dumps({"sources": ["Sentinel-2 L2A SDB (shoal-corrected, keep-out < 3.5 m)", "GEBCO 2026"],
                                 "generalisation": [{k: lv[k] for k in ("name", "grid", "min_area", "min_loop", "zmin", "zmax")} for lv in GEN_LEVELS],
                                 "not_corrected": "no tide/datum conversion to LAT in this test build"})}
    for k, v in meta.items():
        con.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)", (k, v))
    con.commit()
    per = con.execute("SELECT zoom_level, COUNT(*), MAX(LENGTH(tile_data)) FROM tiles GROUP BY zoom_level ORDER BY zoom_level").fetchall()
    con.close()
    log(f"tiles: {out} in {time.time()-t0:.0f} s, {os.path.getsize(out)/1e6:.0f} MB; tiles (largest KB) per zoom " +
        ", ".join(f"z{z} {n:,} ({mx/1024:.0f})" for z, n, mx in per))


STAGES = ("merge", "generalize", "vector", "soundings", "tiles")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bathy-chart.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pilot", default="data/bathy/LANT/pilot", help="pilot work dir holding sdb_final.tif")
    ap.add_argument("--work", default="data/bathy/LANT/chart")
    ap.add_argument("--gebco", default="data/bathy/LANT/gebco/gebco_2026_LANT.tif")
    ap.add_argument("--land", default="/Users/mauricetamman/mesh-experiment/data/prod/land/land_polygons.shp")
    ap.add_argument("--us-chart", default=None,
                    help="district file whose coverage is cut out; default none: the layer covers its whole box, "
                         "for users without US charts (decided 2026-10-11)")
    ap.add_argument("--box", default="-65.0,18.0,-64.0,19.0")
    ap.add_argument("--name", default="LANT_bathy_pilot")
    ap.add_argument("--minzoom", type=int, default=6)
    ap.add_argument("--maxzoom", type=int, default=16)
    ap.add_argument("--stages", default=",".join(STAGES))
    a = ap.parse_args(argv)
    box = tuple(float(v) for v in a.box.split(","))
    os.makedirs(a.work, exist_ok=True)
    for st in a.stages.split(","):
        t = time.time()
        if st == "merge":
            stage_merge(a.work, a.pilot, a.gebco)
        elif st == "generalize":
            stage_generalize(a.work)
        elif st == "vector":
            stage_vector(a.work, box, a.land, a.us_chart)
        elif st == "soundings":
            stage_soundings(a.work, a.pilot)
        elif st == "tiles":
            stage_tiles(a.work, a.name, a.minzoom, a.maxzoom)
        else:
            ap.error(f"unknown stage {st}")
        log(f"{st}: {time.time()-t:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
