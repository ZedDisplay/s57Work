"""Stage 3: the navigation-mesh build, timed per stage (port of the
experiment's build_mesh_full.py; the algorithm and its constants are
unchanged, the District 1 box, the output/land paths and the env knobs
became `configure()` parameters).

Inputs
  pkl        decoded z16 MBTiles layers (mesh.decode)       -- or --
  data       merged S-57 GeoJSON, one directory per source (bands + gap
             fills), with `priority` naming the sources finest first
  land       OSM land_polygons.shp (the land mask the old grid also used),
             applied only OUTSIDE the charts' coverage (M_COVR, CATCOV=1):
             inside it the chart decides, LNDARE is land and what the chart
             draws as water is water. The OSM polygons come from coastline
             only, so the Great Lakes, Lake Champlain, harbours behind a
             coarse shoreline and every river are land in them; with OSM
             overriding the chart, 09CGD meshed to no water at all and
             01CGD lost the Hudson (2026-10-09).
  out        output directory

Layers (every class the old grid/tile checks use, plus structures):
  land        LNDARE, OSM land polygons
  structures  SLCONS PONTON FLODOC HULKES CAUSWY DAMCON GATCON DRYDOC MORFAC
              OFSPLF PILPNT  (areas; lines as 3 m strips; points -> mark discs)
  depth       DEPARE DRGARE, SOUNDG fills faces without a depth area, UNSARE
  hazards     OBSTRN WRECKS UWTROC with VALSOU (areas; lines 3 m; points 20 m discs)
  marks       BOYLAT BOYCAR BOYSPP BOYSAW BOYISD BCNLAT BCNSPP LIGHTS DAYMAR
              PILBOP (+ point MORFAC/OFSPLF/PILPNT): 20 m discs, channel marks flagged
  clearance   BRIDGE CBLOHD PIPOHD CONVYR (areas; lines as 5 m strips), the
              lowest over a face; an opening bridge (CATBRG 2-5, 7, 8) is
              flagged and carries its OPEN clearance (VERCOP) or none, never
              its closed one
  zones       FAIRWY, TSSLPT (ORIENT), TSEZNE, RESARE (RESTRN codes), M_QUAL
              (CATZOC), RECTRC/NAVLNE (lines as 5 m corridors)
Source priority (GeoJSON input only): each source is cut by the M_COVR
(CATCOV=1) coverage of every higher one. The z16 input is one source
with finer-wins already resolved by the chart build.

Per 0.25 deg tile (processed with a 100 m overlap, then cropped):
  1 band merge, 2 boundaries, 3 node+faces (snap-rounded 1e-7), 4 classify +
  dissolve, 5a CDT (GEOS), 5b quality (Triangle q20, no boundary Steiner
  points), 6 medial axis (Voronoi of boundary every 50 m), 7 write,
  8 checks (every dangerous hazard point and every mark inside a flagged
  face or on land/structure, tested within the snap grid GS; land area vs
  charted+OSM land).
Then seam check + reconcile across neighbouring tiles, one global quality
refinement, neighbours, split into per-tile mesh files, final checks.
"""
import json
import math
import os
import pickle
import resource
import sys
import time
from multiprocessing import get_context

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import box, shape
from scipy.spatial import cKDTree
from scipy.ndimage import maximum_filter, distance_transform_edt
import triangle

# ---- configuration (set by configure(); module globals so forked workers see them)
DATA = ""
PKL = None
OSM = None
OUT = None
BOX = None                 # (west, south, east, north) of the mesh rectangle
TILE = 0.25
K = 1.0                    # x scale: cos(mid-latitude of BOX)
SNAP = False               # move disc corners within SNAP_TOL onto nearby lines
ONLY_COVERAGE = False      # mesh only water inside the input's M_COVR (CATCOV 1); bathymetry builds (2026-10-11)
DEBUG_POINTS = []          # [(lon, lat)] to dump face/layer details for
PRIORITY = ["z16"]

GS = 5e-6                      # snap grid ~0.5 m: below chart positional accuracy, removes cm slivers
DEG_M = 111_320.0
DISC_R = 20.0 / DEG_M
SEAM_STEP = 200.0 / DEG_M
MEDIAL_STEP = 10.0 / DEG_M     # medial axis position error <= 5 m
MARGIN = 100.0 / DEG_M
SNAP_TOL = 2.0 / DEG_M         # circle corners within 2 m of another line are moved onto it
DENSIFY = 50.0 / DEG_M      # max input segment length: a segment crossing a seam lies inside both overlaps
MIN_DEPTH_CHECK = 2.5
GEOJSON_PRIORITY = ["band5", "gapfill-east_maine_lubec_eastport", "band4",
                    "gapfill-east_maine_band2345_holes", "band3",
                    "gapfill-east_maine_offshore_band3", "band2"]

LAND = {"LNDARE"}
STRUCT = {"SLCONS", "PONTON", "FLODOC", "HULKES", "CAUSWY", "DAMCON", "GATCON",
          "DRYDOC", "MORFAC", "OFSPLF", "PILPNT"}
DEPTH = {"DEPARE", "DRGARE"}
HAZ = {"OBSTRN", "WRECKS", "UWTROC"}
MARKS = {"BOYLAT", "BOYCAR", "BOYSPP", "BOYSAW", "BOYISD", "BCNLAT", "BCNSPP",
         "LIGHTS", "DAYMAR", "PILBOP", "MORFAC", "OFSPLF", "PILPNT"}
CHANMARK = {"BOYLAT", "BCNLAT", "DAYMAR"}
CLEAR = {"BRIDGE", "CBLOHD", "PIPOHD", "CONVYR"}
ZONES = {"FAIRWY", "TSSLPT", "TSEZNE", "RESARE", "M_QUAL", "UNSARE", "RECTRC", "NAVLNE"}
LINE_W = {**{L: 3.0 for L in STRUCT}, "OBSTRN": 3.0, "WRECKS": 3.0,
          **{L: 5.0 for L in CLEAR}, "RECTRC": 2 * 0.000125 * 111_320.0, "NAVLNE": 5.0}
# The old routing grid's cell lattice: soundings become squares of its
# cells, FAIRWY/DRGARE/RECTRC reach half a cell, and the shore distance is
# measured on it. Its origin is kept verbatim; the arithmetic below is
# index-relative, so negative offsets west/south of it are fine.
PG_W, PG_S, PG_RES = -75.5, 38.7, 0.00025
DISC = HAZ | MARKS
LAYERS = sorted(LAND | STRUCT | DEPTH | HAZ | MARKS | CLEAR | ZONES)

SRC = {}    # (src, L) -> (polys, props, from_point, STRtree)
PTS = {}    # (src, L) -> (points, props)   hazard/mark points, for the checks
SND = {}    # src -> (N, 3) soundings x, y, depth
COV = {}    # src -> (polys, STRtree)
COVN = 0    # CATCOV=1 coverage polygons loaded (all sources); COV is freed before the checks
OSML = None  # (polys, STRtree)
INV = {}     # layer -> features loaded (all sources)
REJOIN = {}  # layer -> (tile pieces, features after rejoining)


def configure(box_, out, land=None, pkl=None, data="", tile=0.25, snap=False,
              debug_points=(), priority=None, only_coverage=False):
    """Set the build's parameters. `box_` is (west, south, east, north);
    the x scale is cos of its mid-latitude. One of `pkl` (decoded z16
    layers) or `data` (merged GeoJSON per source) must be given."""
    global BOX, OUT, OSM, PKL, DATA, TILE, K, SNAP, DEBUG_POINTS, PRIORITY, ONLY_COVERAGE
    BOX = tuple(float(v) for v in box_)
    OUT, OSM, PKL, DATA, TILE = out, land, pkl, data, tile
    K = math.cos(math.radians((BOX[1] + BOX[3]) / 2))
    SNAP = bool(snap)
    ONLY_COVERAGE = bool(only_coverage)
    DEBUG_POINTS = list(debug_points)
    if pkl:
        PRIORITY = ["z16"]          # the grid reads z16 tiles as one source, no band priority
    else:
        PRIORITY = list(priority) if priority else list(GEOJSON_PRIORITY)
    for d in (SRC, PTS, SND, COV, INV, REJOIN):
        d.clear()


def tf(geoms):
    return shapely.transform(geoms, lambda c: c * np.array([K, 1.0]))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


OPENING_BRIDGE = {2, 3, 4, 5, 7, 8}   # CATBRG: opening, swing, lifting, bascule, draw, transporter


def is_opening_bridge(pr):
    """True when the feature's CATBRG (an S-57 list attribute, "4" or
    "1,9") names a bridge that opens for traffic."""
    v = pr.get("CATBRG")
    if v is None:
        return False
    codes = str(v).replace("(", "").replace(")", "").split(",")
    return any(c.strip().isdigit() and int(c) in OPENING_BRIDGE for c in codes)


def clearance_of(L, pr):
    """(clearance, opening) of a BRIDGE/CBLOHD/PIPOHD/CONVYR feature.
    A fixed span: the first of VERCCL, VERCLR, VERCSA, or None when the
    chart gives no height (the experiment stored 0.0, which blocked every
    vessel). An opening bridge: the open clearance VERCOP, or None, and
    opening=True; its closed clearance is never used, so the router sees
    the bridge as passable and the flag tells it the span must open."""
    if L == "BRIDGE" and is_opening_bridge(pr):
        return num(pr.get("VERCOP")), True
    v = next((num(pr.get(k)) for k in ("VERCCL", "VERCLR", "VERCSA")
              if num(pr.get(k)) is not None), None)
    return v, False


def restrn_bits(v):
    if v is None:
        return 0
    codes = v if isinstance(v, list) else str(v).replace(";", ",").split(",")
    b = 0
    for c in codes:
        try:
            b |= 1 << int(c)
        except (TypeError, ValueError):
            pass
    return b


def outside_coverage(land_polys, coverage):
    """The OSM land pieces with the charts' coverage removed: inside
    M_COVR the chart alone says what is land (LNDARE) and what is water.
    `coverage` is the union of the coverage polygons in the box, or None."""
    land_polys = np.asarray(land_polys, dtype=object)
    if coverage is None or not len(land_polys):
        return land_polys
    out = polyonly(shapely.difference(land_polys, coverage))
    return out[~shapely.is_empty(out)]


def polyonly(geoms):
    out = np.empty(len(geoms), dtype=object)
    for k, g in enumerate(geoms):
        if g.is_empty or g.geom_type in ("Polygon", "MultiPolygon"):
            out[k] = g
        else:
            ps = [p for p in shapely.get_parts(g) if p.geom_type == "Polygon"]
            out[k] = shapely.multipolygons(ps) if ps else shapely.Polygon()
    return out


def polys(g):
    return [p for p in shapely.get_parts(g) if p.geom_type == "Polygon" and not p.is_empty]


def load():
    global OSML, COVN
    pad = 0.01
    bb = box(BOX[0] - pad, BOX[1] - pad, BOX[2] + pad, BOX[3] + pad)
    n_feat = 0
    INV.clear()
    COVN = 0
    for src in PRIORITY:
        for L in LAYERS + ["M_COVR", "SOUNDG"]:
            geoms, props = [], []
            if PKL:
                p = f"{PKL}/{L}.pkl"
                if not os.path.exists(p):
                    continue
                with open(p, "rb") as fh:
                    items = pickle.load(fh)
                geoms = list(shapely.from_wkb([w for w, _ in items]))
                props = [json.loads(pp) or {} for _, pp in items]
                del items
                # Tile pieces of one chart feature are rejoined (per source
                # file and LNAM), so z16 tile edges don't become mesh
                # boundaries. Not for DRGARE / OBSTRN / UWTROC / WRECKS:
                # the grid lets the LAST of those in a tile win, so their
                # per-tile pieces are kept. Points on a tile edge appear
                # in both tiles: deduplicated.
                if L not in ("DRGARE", "OBSTRN", "UWTROC", "WRECKS", "SOUNDG"):
                    groups = {}
                    for k_, (g_, pr_) in enumerate(zip(geoms, props)):
                        fam = 0 if g_.geom_type in ("Point", "MultiPoint") else 1
                        key = (pr_.get("__src"), pr_.get("LNAM"), fam)
                        if pr_.get("LNAM") is None:
                            key = ("__nolnam__", k_)
                        groups.setdefault(key, []).append(k_)
                    g2, p2 = [], []
                    for key, ks in groups.items():
                        if len(ks) == 1:
                            g2.append(geoms[ks[0]])
                        elif key[2] == 0:
                            g2.append(shapely.union_all([geoms[k_] for k_ in ks]))   # identical points collapse
                        else:
                            g2.append(shapely.union_all([geoms[k_] for k_ in ks]))
                        p2.append(props[ks[0]])
                    REJOIN[L] = (len(geoms), len(g2))
                    geoms, props = g2, p2
            else:
                p = f"{DATA}/{src}/{L}.geojson"
                if not os.path.exists(p):
                    continue
                with open(p) as f:
                    fc = json.load(f)
                for feat in fc["features"]:
                    g = feat.get("geometry")
                    if not g:
                        continue
                    try:
                        geoms.append(shape(g))
                    except Exception:
                        continue
                    props.append(feat["properties"] or {})
                del fc
            if not geoms:
                continue
            arr = np.array(geoms, dtype=object)
            keep = shapely.intersects(arr, bb)
            arr = arr[keep]
            props = [pr for pr, k in zip(props, keep) if k]
            if not len(arr):
                continue
            n_feat += len(arr)
            INV[L] = INV.get(L, 0) + len(arr)
            if L == "SOUNDG":
                if PKL:   # MVT points are 2-D; the depth is the DEPTH property
                    xy = shapely.get_coordinates(arr)
                    z = np.array([num(pr.get("DEPTH")) if num(pr.get("DEPTH")) is not None else np.nan
                                  for pr in props])
                    c = np.column_stack([xy, z])
                else:
                    c = shapely.get_coordinates(arr, include_z=True)
                c = c[np.isfinite(c[:, 2])]
                if PKL:
                    n0 = len(c)
                    c = np.unique(c, axis=0)             # a sounding on a tile edge appears in both tiles
                    REJOIN["SOUNDG"] = (n0, len(c))
                SND[src] = c * np.array([K, 1.0, 1.0])
                continue
            arr = tf(arr)
            if L == "M_COVR":
                m = np.array([pr.get("CATCOV") == 1 for pr in props], dtype=bool)
                cv = shapely.set_precision(shapely.segmentize(polyonly(arr[m]), DENSIFY), GS)
                cv = cv[~shapely.is_empty(cv)]
                if len(cv):
                    COV[src] = (cv, STRtree(cv))
                    COVN += len(cv)
                continue
            tid = shapely.get_type_id(arr)
            og, op, od, pg, pp = [], [], [], [], []
            for g, t, pr in zip(arr, tid, props):
                if t in (3, 6):
                    og.append(g); op.append(pr); od.append(0)
                elif t in (1, 5) and L in LINE_W:
                    og.append(shapely.buffer(g, LINE_W[L] / 2 / DEG_M, quad_segs=2))
                    op.append(pr); od.append(2)
                elif t in (0, 4) and L in DISC:
                    og.append(shapely.buffer(g, DISC_R, quad_segs=4)); op.append(pr); od.append(1)
                    for part in shapely.get_parts(g):
                        pg.append(part); pp.append(pr)
            if og:
                ga = shapely.set_precision(shapely.segmentize(np.array(og, dtype=object), DENSIFY), GS)
                ok = ~shapely.is_empty(ga)
                ga = ga[ok]
                op = [x for x, k in zip(op, ok) if k]
                od = np.array(od, dtype=np.int8)[ok]
                if len(ga):
                    SRC[(src, L)] = (ga, op, od, STRtree(ga))
            if pg:
                PTS[(src, L)] = (np.array(pg, dtype=object), pp)
    # OSM land polygons inside the rectangle
    og = []
    if OSM:
        import shapefile
        r = shapefile.Reader(OSM)
        for shp in r.iterShapes(bbox=[BOX[0] - pad, BOX[1] - pad, BOX[2] + pad, BOX[3] + pad]):
            og.append(shape(shp.__geo_interface__))
    if og:
        ga = shapely.set_precision(shapely.segmentize(tf(np.array(og, dtype=object)), DENSIFY), GS)
        ga = ga[~shapely.is_empty(ga)]
    else:
        ga = np.empty(0, dtype=object)
    OSML = (ga, STRtree(ga))
    return n_feat, len(ga)


TRI_OPTS = ("pq20AQ", "pAQ")


def tri_safe(tin, opts):
    """Triangle in a forked child: Triangle exits the process on internal
    errors, which would kill the pool worker and hang the run."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            res = triangle.triangulate(tin, opts)
            out = {k: res[k] for k in ("vertices", "triangles", "triangle_attributes", "segments") if k in res}
        except Exception as ex:
            out = {"error": f"{type(ex).__name__}: {ex}"}
        with os.fdopen(w, "wb") as f:
            f.write(pickle.dumps(out))
        os._exit(0)
    os.close(w)
    chunks = []
    with os.fdopen(r, "rb") as f:
        while True:
            ch = f.read(1 << 24)
            if not ch:
                break
            chunks.append(ch)
    data = b"".join(chunks)
    _, status = os.waitpid(pid, 0)
    if not data:
        return {"error": f"Triangle process died (status {status})"}
    return pickle.loads(data)


def beat(i, j, stage):
    with open(f"{OUT}/progress_{os.getpid()}.log", "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} tile {i},{j} {stage}\n")


def within_pairs(geoms, reps):
    if not len(geoms):
        return np.empty((2, 0), dtype=np.int64)
    return STRtree(geoms).query(reps, predicate="within")


def edge_vertices(V, x0, y0, x1, y1):
    sx0, sx1 = np.round(x0 / GS) * GS, np.round(x1 / GS) * GS
    sy0, sy1 = np.round(y0 / GS) * GS, np.round(y1 / GS) * GS
    tol = GS * 0.6
    return {name: V[np.abs(V[:, axis] - val) < tol]
            for name, axis, val in (("L", 0, sx0), ("R", 0, sx1), ("B", 1, sy0), ("T", 1, sy1))}


def process_tile(args):
    i, j, w, s, e, n = args
    os.makedirs(OUT, exist_ok=True)
    T, st = {}, {"i": i, "j": j}
    x0, x1 = w * K, e * K
    tb = box(x0, s, x1, n)
    tbig = box(x0 - MARGIN, s - MARGIN, x1 + MARGIN, n + MARGIN)
    try:
        # 1 band merge
        beat(i, j, "start")
        t = time.perf_counter()
        feats = {L: [] for L in LAYERS}
        feats["OSM"] = []
        checks = []          # (point, kind, layer, valsou)
        snd = []
        higher = None
        for src in PRIORITY:
            for L in LAYERS:
                if (src, L) in PTS:
                    pa, pp = PTS[(src, L)]
                    m = shapely.intersects(pa, tb)
                    if higher is not None and m.any():
                        m[m] &= ~shapely.within(pa[m], higher)
                    for k in np.nonzero(m)[0]:
                        kind = "haz" if L in HAZ else "mark"
                        checks.append((pa[k], kind, L, num(pp[k].get("VALSOU"))))
                if (src, L) not in SRC:
                    continue
                arr, props, disc, tree = SRC[(src, L)]
                idx = tree.query(tbig)
                if not len(idx):
                    continue
                g = polyonly(shapely.intersection(arr[idx], tbig))
                if higher is not None:
                    g = polyonly(shapely.difference(g, higher))
                for k, gg in zip(idx, g):
                    if not gg.is_empty:
                        feats[L].append((gg, props[k], disc[k]))
            if src in SND:
                c = SND[src]
                m = (c[:, 0] >= x0 - MARGIN) & (c[:, 0] <= x1 + MARGIN) & \
                    (c[:, 1] >= s - MARGIN) & (c[:, 1] <= n + MARGIN)
                if higher is not None and m.any():
                    m[m] &= ~shapely.contains_xy(higher, c[m, 0], c[m, 1])
                snd.append(c[m])
            if src in COV:
                carr, ctree = COV[src]
                ci = ctree.query(tbig)
                if len(ci):
                    cg = shapely.union_all(polyonly(shapely.intersection(carr[ci], tbig)))
                    higher = cg if higher is None else shapely.union(higher, cg)
        oarr, otree = OSML
        oi = otree.query(tbig)
        if len(oi):
            og = polyonly(shapely.intersection(oarr[oi], tbig))
            og = outside_coverage(og, higher)        # the chart decides inside its coverage
            for gg in og:
                if not gg.is_empty:
                    feats["OSM"].append((gg, {}, False))
        snd = np.vstack(snd) if snd else np.empty((0, 3))
        T["1 band merge"] = time.perf_counter() - t
        beat(i, j, "done 1 band merge")

        # 2 boundaries
        t = time.perf_counter()
        area = {L: [(p, pr, d) for g, pr, d in feats[L] for p in polys(g)] for L in feats}
        ref = [shapely.boundary(p) for L in area for p, _, d in area[L] if d != 1]
        n_snapped = 0
        if ref and SNAP:
            ref_arr = np.array(ref, dtype=object)
            rtree = STRtree(ref_arr)
            for L in area:
                new = []
                for p, pr, d in area[L]:
                    if d == 1:
                        near = rtree.query(p, predicate="dwithin", distance=SNAP_TOL)
                        if len(near):
                            q = shapely.snap(p, shapely.geometrycollections(list(ref_arr[near])), SNAP_TOL)
                            if not q.equals(p):
                                n_snapped += 1
                                q = shapely.set_precision(shapely.make_valid(q), GS)
                                for qq in polys(q):
                                    new.append((qq, pr, d))
                                continue
                    new.append((p, pr, d))
                area[L] = new
        st["discs_snapped"] = n_snapped
        # soundings: as the old grid, a sounding shallower than its depth
        # area (or with none) sets the depth of the grid cell it falls in
        sq = []
        if len(snd):
            P_ = shapely.points(snd[:, :2])
            dep = np.full(len(snd), np.inf)
            for L in ("DEPARE", "DRGARE"):
                if area.get(L):
                    gl = np.array([p for p, _, _ in area[L]], dtype=object)
                    vals = [num(pr.get("DRVAL1")) for _, pr, _ in area[L]]
                    hit_ = STRtree(gl).query(P_, predicate="within")
                    for pt, k in hit_.T:
                        v = vals[k]
                        if v is None:
                            continue
                        if L == "DEPARE":
                            dep[pt] = min(dep[pt], v)
                        else:
                            dep[pt] = v
            use = snd[:, 2] < dep
            lon_ = snd[use, 0] / K; lat_ = snd[use, 1]
            jj = np.ceil((lon_ - PG_W) / PG_RES - 0.5).astype(np.int64)
            ii = np.ceil((lat_ - PG_S) / PG_RES - 0.5).astype(np.int64)
            cells = {}
            for a_, b_, z in zip(ii, jj, snd[use, 2]):
                cells[(a_, b_)] = min(cells.get((a_, b_), np.inf), z)
            for (a_, b_), z in cells.items():
                w_ = (PG_W + b_ * PG_RES) * K; s_ = PG_S + a_ * PG_RES
                q = shapely.intersection(box(w_, s_, w_ + PG_RES * K, s_ + PG_RES), tbig)
                q = shapely.set_precision(q, GS)
                if not q.is_empty:
                    for qq in polys(q):
                        sq.append((qq, {"DEPTH": float(z)}, 3))
        area["SNDSQ"] = sq
        # old grid is_navigable: a cell touching FAIRWY / DRGARE / RECTRC
        # -> reach of half a grid cell beyond the outline
        reach = []
        for L in ("FAIRWY", "DRGARE", "RECTRC"):
            for p, pr, d in area.get(L, []):
                if L == "RECTRC" and d != 0:
                    continue                    # line tracks: strip already has the reach
                q = shapely.set_precision(shapely.intersection(shapely.buffer(p, PG_RES / 2, quad_segs=2), tbig), GS)
                for qq in polys(q):
                    reach.append((qq, pr, 0))
        area["NAVREACH"] = reach
        st["snd_squares"] = len(sq)
        lines = [shapely.boundary(p) for L in area for p, _, _ in area[L]]
        for (ax, ay, bx, by) in ((x0, s, x0, n), (x1, s, x1, n), (x0, s, x1, s), (x0, n, x1, n)):
            m = max(1, math.ceil(math.hypot(bx - ax, by - ay) / SEAM_STEP))
            k = np.arange(m + 1) / m
            lines.append(shapely.linestrings(np.column_stack([ax + (bx - ax) * k, ay + (by - ay) * k])))
        lines.append(tbig.boundary)
        # --only-coverage: the coverage edge cuts the faces, so the mesh stops exactly there
        if ONLY_COVERAGE and higher is not None:
            lines.append(shapely.set_precision(shapely.boundary(shapely.intersection(higher, tbig)), GS))
        st["input_polys"] = sum(len(v) for v in area.values())
        st["input_vertices"] = int(sum(shapely.get_num_coordinates(np.array(lines, dtype=object))))
        T["2 boundaries"] = time.perf_counter() - t
        beat(i, j, f"done 2 boundaries ({st['input_vertices']} vertices)")

        # 3 node + polygonize + crop
        t = time.perf_counter()
        noded = shapely.union_all(np.array(lines, dtype=object), grid_size=GS)
        faces = shapely.get_parts(shapely.polygonize(shapely.get_parts(noded)))
        faces = faces[shapely.within(shapely.point_on_surface(faces), tb)]
        T["3 node+faces"] = time.perf_counter() - t
        beat(i, j, f"done 3 node+faces ({len(faces)} faces)")

        # 4 classify + dissolve
        t = time.perf_counter()
        nf = len(faces)
        reps = shapely.point_on_surface(faces)
        A = {k: np.zeros(nf) for k in ("land", "struct", "dredged", "unsurv", "catzoc", "fair",
                                       "tsez", "resare", "track", "navlne", "haz", "mark", "chanmark", "is_nav",
                                       "opening", "outside")}
        # --only-coverage: water outside the input's own coverage is not meshed; it becomes
        # a hole, but not land (the land-area check counts only land holes)
        if ONLY_COVERAGE:
            if higher is None:
                A["outside"][:] = 1
            else:
                shapely.prepare(higher)
                A["outside"][~shapely.contains_xy(higher, *shapely.get_coordinates(reps).T)] = 1
        A["depth"] = np.full(nf, -999.0)
        A["clear"] = np.full(nf, -999.0)
        A["tss"] = np.full(nf, -999.0)
        A["hazv"] = np.full(nf, 1e9)
        # depth as the old grid builds it, per source then merged
        dep_min, drg, drg_o, obs, obs_o, sq_min = {}, {}, {}, {}, {}, {}
        for L in area:
            if not area[L]:
                continue
            gl = np.array([p for p, _, _ in area[L]], dtype=object)
            prs = [pr for _, pr, _ in area[L]]
            dsc = [d for _, _, d in area[L]]
            f = within_pairs(gl, reps)
            for a, b in f.T:
                pr, d = prs[b], dsc[b]
                if L in LAND or L == "OSM":
                    A["land"][a] = 1
                elif L in HAZ:
                    v = num(pr.get("VALSOU"))
                    A["haz"][a] = 1
                    A["hazv"][a] = min(A["hazv"][a], -1e9 if v is None else v)
                    # old grid: an obstruction/wreck/rock AREA with a charted
                    # depth sets the cell's obstruction depth, the LAST such
                    # feature in the tile winning; points and lines never
                    # match there; strips are ours
                    if d == 0 and v is not None:
                        k_ = (a, pr.get("__src", "single")); o_ = pr.get("__ord", 0)
                        if k_ not in obs_o or o_ >= obs_o[k_]:
                            obs[k_], obs_o[k_] = v, o_
                elif L in STRUCT and d != 1:
                    A["struct"][a] = 1
                elif L in MARKS and d == 1:
                    A["mark"][a] = 1
                    if L in CHANMARK:
                        A["chanmark"][a] = 1
                elif L == "DEPARE":
                    v = num(pr.get("DRVAL1"))
                    if v is not None:
                        k_ = (a, pr.get("__src", "single"))
                        dep_min[k_] = min(dep_min.get(k_, v), v)
                elif L == "DRGARE":
                    v = num(pr.get("DRVAL1"))
                    if v is not None:
                        k_ = (a, pr.get("__src", "single")); o_ = pr.get("__ord", 0)
                        if k_ not in drg_o or o_ >= drg_o[k_]:
                            drg[k_], drg_o[k_] = v, o_
                        A["dredged"][a] = 1
                elif L in CLEAR:
                    v, opening = clearance_of(L, pr)
                    if opening:
                        A["opening"][a] = 1
                    if v is not None:
                        A["clear"][a] = v if A["clear"][a] == -999.0 else min(A["clear"][a], v)
                elif L == "FAIRWY":
                    A["fair"][a] = 1
                elif L == "TSSLPT":
                    A["tss"][a] = num(pr.get("ORIENT")) or 0.0
                elif L == "TSEZNE":
                    A["tsez"][a] = 1
                elif L == "RESARE":
                    A["resare"][a] = float(int(A["resare"][a]) | restrn_bits(pr.get("RESTRN")))
                elif L == "M_QUAL":
                    A["catzoc"][a] = num(pr.get("CATZOC")) or 0
                elif L == "UNSARE":
                    A["unsurv"][a] = 1
                elif L == "RECTRC":
                    A["track"][a] = 1
                    A["is_nav"][a] = 1          # line tracks are already half a cell wide
                elif L == "NAVREACH":
                    A["is_nav"][a] = 1
                elif L == "NAVLNE":
                    A["navlne"][a] = 1
                elif L == "SNDSQ":
                    z = pr["DEPTH"]
                    sq_min[a] = min(sq_min.get(a, z), z)
        per_face = {}
        for (a, src_), v in dep_min.items():
            per_face.setdefault(a, set()).add(src_)
        for (a, src_) in list(drg) + list(obs):
            per_face.setdefault(a, set()).add(src_)
        for a in set(per_face) | set(sq_min):
            vals = []
            for src_ in per_face.get(a, ()):
                k_ = (a, src_)
                d_ = drg[k_] if k_ in drg else dep_min.get(k_)        # dredged replaces
                cand = [x for x in (d_, obs.get(k_)) if x is not None]  # obstruction min at merge
                if cand:
                    vals.append(min(cand))
            if a in sq_min:
                vals.append(sq_min[a])                                 # soundings
            A["depth"][a] = min(vals) if vals else -999.0
        cols = ["land", "struct", "depth", "dredged", "unsurv", "catzoc", "clear", "fair", "tss",
                "tsez", "resare", "track", "navlne", "haz", "hazv", "mark", "chanmark", "is_nav", "opening", "outside"]
        keys = np.column_stack([A[c] for c in cols])
        ukeys, inv = np.unique(keys, axis=0, return_inverse=True)
        inv = inv.ravel()
        parts, part_key, holes = [], [], []
        for g in range(len(ukeys)):
            members = faces[inv == g]
            try:
                u = shapely.coverage_union_all(members)
            except Exception:
                u = shapely.union_all(members, grid_size=GS)
            out_col = ukeys[g][cols.index("outside")]
            for p in polys(u):
                if ukeys[g][0] or out_col:
                    holes.append((p, int(ukeys[g][0])))
                else:
                    parts.append(p); part_key.append(g)
        if DEBUG_POINTS:
            dbg = []
            for lo, la in DEBUG_POINTS:
                P = shapely.Point(lo * K, la)
                if not shapely.intersects(tbig, P):
                    continue
                rec = {"pt": [lo, la], "faces": [], "layers": []}
                for fi in np.flatnonzero(shapely.intersects(faces, P.buffer(GS))):
                    rec["faces"].append({c: float(A[c][fi]) for c in cols} | {"area_m2": float(faces[fi].area) * DEG_M ** 2})
                for L in area:
                    for g, pr, d in area[L]:
                        if shapely.intersects(g, P.buffer(GS)):
                            rec["layers"].append([L, bool(d), {k: pr.get(k) for k in ("VALSOU", "DRVAL1", "CATZOC", "OBJNAM", "WATLEV") if pr.get(k) is not None}])
                rec["hz_src_points_near"] = [[L, round(pp.x / K, 6), round(pp.y, 6), v] for pp, kind, L, v in checks
                                             if shapely.distance(pp, P) < 30 / DEG_M]
                dbg.append(rec)
            if dbg:
                with open(f"{OUT}/debug_{i}_{j}.json", "w") as f:
                    json.dump(dbg, f, indent=1, default=str)
        T["4 classify+dissolve"] = time.perf_counter() - t
        st.update(faces=nf, water_parts=len(parts), hole_parts=len(holes),
                  unknown_depth_parts=int(sum(ukeys[g][2] == -999.0 for g in part_key)))
        beat(i, j, f"done 4 classify ({len(parts)} water parts)")

        # 5a CDT
        t = time.perf_counter()
        if parts:
            tri_a = shapely.constrained_delaunay_triangles(np.array(parts, dtype=object))
            a_coords = shapely.get_coordinates(shapely.get_parts(tri_a))
            n_a = int(shapely.get_num_geometries(tri_a).sum())
        else:
            a_coords, n_a = np.empty((0, 2)), 0
        T["5a CDT"] = time.perf_counter() - t
        st["tris_a"] = n_a
        beat(i, j, f"done 5a CDT ({n_a} triangles)")

        # 5b PSLG for the global quality refinement
        t = time.perf_counter()
        U = np.empty((0, 2)); S = np.empty((0, 2), int); regions = np.empty((0, 4)); hole_pts = np.empty((0, 2))
        seg_part = np.empty(0, dtype=int)
        if parts:
            pts, segs, segp, off = [], [], [], 0
            for pk, p in enumerate(parts):
                for ring in [p.exterior, *p.interiors]:
                    c = np.asarray(ring.coords)[:-1]
                    m = len(c)
                    pts.append(c)
                    segs.append(np.column_stack([np.arange(m), (np.arange(m) + 1) % m]) + off)
                    segp.append(np.full(m, pk))
                    off += m
            U, uinv = np.unique(np.vstack(pts), axis=0, return_inverse=True)
            S = np.sort(uinv.ravel()[np.vstack(segs)], axis=1)
            SP = np.concatenate(segp)
            keep_ = S[:, 0] != S[:, 1]
            S, SP = S[keep_], SP[keep_]
            S, first = np.unique(S, axis=0, return_index=True)
            seg_part = SP[first]
            regions = np.array([[*shapely.get_coordinates(shapely.point_on_surface(p))[0], k, 0.0]
                                for k, p in enumerate(parts)])
            if holes:
                hole_pts = shapely.get_coordinates(shapely.point_on_surface(
                    np.array([h for h, _ in holes], dtype=object)))
        st["water_area"] = float(sum(p.area for p in parts))
        st["pslg_vertices"] = len(U)
        sharp_xy, sharp_src = np.empty((0, 2)), np.empty(0, dtype=np.int8)
        if len(S):
            inc = {}
            for a_, b_ in S:
                inc.setdefault(a_, []).append(b_); inc.setdefault(b_, []).append(a_)
            sh = []
            for v, nb in inc.items():
                if len(nb) < 2:
                    continue
                dd = U[nb] - U[v]
                th = np.sort(np.degrees(np.arctan2(dd[:, 1], dd[:, 0])) % 360)
                if np.diff(np.append(th, th[0] + 360)).min() < 20.0:
                    sh.append(v)
            if sh:
                sharp_xy = U[sh]
                sharp_src = np.zeros(len(sh), dtype=np.int8)
                P_ = shapely.points(sharp_xy)
                for code, want in ((2, 2), (1, 1)):          # disc overrides strip
                    bl = [shapely.boundary(p) for L in area for p, _, d in area[L] if d == want]
                    if bl:
                        hit_ = STRtree(np.array(bl, dtype=object)).query(P_, predicate="dwithin", distance=GS)
                        sharp_src[np.unique(hit_[0])] = code
                on_edge = ((np.abs(sharp_xy[:, 0] - x0) < 0.6 * GS) | (np.abs(sharp_xy[:, 0] - x1) < 0.6 * GS) |
                           (np.abs(sharp_xy[:, 1] - s) < 0.6 * GS) | (np.abs(sharp_xy[:, 1] - n) < 0.6 * GS))
                sharp_src[(sharp_src == 0) & on_edge] = 3
        T["5b PSLG"] = time.perf_counter() - t
        st["edges"] = {k: v.tolist() for k, v in edge_vertices(U, x0, s, x1, n).items()}

        # 6 medial axis
        t = time.perf_counter()
        n_med, med = 0, np.empty((0, 2))
        if parts:
            water = shapely.coverage_union_all(np.array(parts, dtype=object))
            bpts = np.unique(shapely.get_coordinates(
                shapely.segmentize(shapely.boundary(water), MEDIAL_STEP)), axis=0)
            if len(bpts) >= 3:
                edges = shapely.get_parts(shapely.voronoi_polygons(
                    shapely.multipoints(bpts), only_edges=True, extend_to=tb))
                shapely.prepare(water)
                med_e = edges[shapely.contains_properly(water, edges)]
                n_med = len(med_e)
                med = shapely.get_coordinates(med_e)
        T["6 medial"] = time.perf_counter() - t
        st["medial_edges"] = n_med
        beat(i, j, "done 6 medial")

        # 6b dist_to_shore and shore penalty, computed as the old grid does:
        # land at each grid cell centre (OSM + chart land), Euclidean
        # distance transform in cells, metres = cells x cell_m, penalty
        # x1.0..1.3 within 2 km unless the 71-cell window's max distance is
        # < 1 km. Region: tile +/- 0.03 deg, enough for 2 km + half a window.
        t = time.perf_counter()
        m3 = 0.03
        lon_w, lon_e = x0 / K, x1 / K
        j0 = int(math.floor((lon_w - m3 - PG_W) / PG_RES)); j1 = int(math.ceil((lon_e + m3 - PG_W) / PG_RES))
        i0 = int(math.floor((s - m3 - PG_S) / PG_RES)); i1 = int(math.ceil((n + m3 - PG_S) / PG_RES))
        clon = PG_W + (np.arange(j0, j1) + 0.5) * PG_RES
        clat = PG_S + (np.arange(i0, i1) + 0.5) * PG_RES
        GX, GY = np.meshgrid(clon * K, clat)
        tb3 = box((lon_w - m3) * K, s - m3, (lon_e + m3) * K, n + m3)
        land_g, higher3 = [], None
        for src in PRIORITY:                      # chart land, band priority as the mesh
            if (src, "LNDARE") in SRC:
                arr, _, _, tree = SRC[(src, "LNDARE")]
                ii_ = tree.query(tb3)
                ii_ = ii_[np.isin(shapely.get_type_id(arr[ii_]), (3, 6))]
                if len(ii_):
                    g3 = polyonly(shapely.intersection(arr[ii_], tb3))
                    if higher3 is not None:
                        g3 = polyonly(shapely.difference(g3, higher3))
                    land_g += [gg for gg in g3 if not gg.is_empty]
            if src in COV:
                carr, ctree = COV[src]
                ci = ctree.query(tb3)
                if len(ci):
                    cg3 = shapely.union_all(polyonly(shapely.intersection(carr[ci], tb3)))
                    higher3 = cg3 if higher3 is None else shapely.union(higher3, cg3)
        land_g += [g for g in outside_coverage(OSML[0][OSML[1].query(tb3)], higher3) if not g.is_empty]
        if land_g:
            land_u = shapely.union_all(np.array(land_g, dtype=object))
            shapely.prepare(land_u)
            is_land = shapely.contains_xy(land_u, GX.ravel(), GY.ravel()).reshape(GX.shape)
        else:
            is_land = np.zeros(GX.shape, bool)
        cell_m = PG_RES * (6_371_008.8 * math.pi / 180.0)
        if is_land.any():
            dist_cells = distance_transform_edt(~is_land).astype(np.float32)
        else:
            dist_cells = np.full(GX.shape, np.float32(1e6))   # no land within reach
        dist_m = dist_cells * cell_m
        mx = maximum_filter(dist_m, size=max(3, int(2000.0 / cell_m)))
        shore_full = np.ones(GX.shape, dtype=np.float32)
        pen = (dist_m < 2000.0) & ~(mx < 1000.0) & ~is_land
        shore_full[pen] = 1.0 + 0.3 * (1.0 - dist_m[pen] / 2000.0)
        # keep only the tile's own cells
        cj0 = int(math.floor((lon_w - PG_W) / PG_RES)) - j0; cj1 = int(math.ceil((lon_e - PG_W) / PG_RES)) - j0
        ci0 = int(math.floor((s - PG_S) / PG_RES)) - i0; ci1 = int(math.ceil((n - PG_S) / PG_RES)) - i0
        shore = shore_full[ci0:ci1 + 1, cj0:cj1 + 1]
        dist_keep = dist_cells[ci0:ci1 + 1, cj0:cj1 + 1]
        shore_i0, shore_j0 = i0 + ci0, j0 + cj0
        T["6b dist_to_shore"] = time.perf_counter() - t

        # 7 write
        t = time.perf_counter()
        path = f"{OUT}/tile_{i:03d}_{j:03d}.npz"
        np.savez_compressed(path, a_coords=a_coords,
                            part_key=np.array(part_key, dtype=np.int32), keys=ukeys,
                            key_cols=np.array(cols), medial=med,
                            pslg_V=U, pslg_S=S, pslg_R=regions, pslg_H=hole_pts,
                            sharp_xy=sharp_xy, sharp_src=sharp_src, seg_part=seg_part,
                            shore_i0=shore_i0, shore_j0=shore_j0, shore=shore, dist_cells=dist_keep)
        st["bytes"] = os.path.getsize(path)
        T["7 write"] = time.perf_counter() - t

        # 8 checks
        t = time.perf_counter()
        c = {"haz_checked": 0, "haz_ok": 0, "haz_onhole": 0, "haz_bad": 0,
             "mark_checked": 0, "mark_ok": 0, "mark_onhole": 0, "mark_bad": 0}
        bad_examples = []
        ptree = STRtree(np.array(parts, dtype=object)) if parts else None
        htree = STRtree(np.array([h for h, _ in holes], dtype=object)) if holes else None
        for p, kind, L, v in checks:
            if kind == "haz" and v is not None and v >= MIN_DEPTH_CHECK:
                continue
            c[f"{kind}_checked"] += 1
            # within the snap grid, not an exact hit: a point under 0.5 m from a
            # tile edge can fall between the snapped edge and the tile box, in no
            # face of either tile (New Buffalo, 0.17 m east of a seam, 2026-10-09)
            hit = ptree.query(p, predicate="dwithin", distance=GS) if ptree is not None else []
            if len(hit):
                ci_ = cols.index("haz" if kind == "haz" else "mark")
                flag = all(ukeys[part_key[h]][ci_] for h in hit)
                if flag:
                    c[f"{kind}_ok"] += 1
                else:
                    c[f"{kind}_bad"] += 1
                    if len(bad_examples) < 3:
                        bad_examples.append((L, round(p.x / K, 6), round(p.y, 6)))
            elif htree is not None and len(htree.query(p, predicate="dwithin", distance=GS)):
                c[f"{kind}_onhole"] += 1
            else:
                c[f"{kind}_bad"] += 1
                if len(bad_examples) < 3:
                    bad_examples.append((L, round(p.x / K, 6), round(p.y, 6)))
        st.update(c)
        st["bad_examples"] = bad_examples
        land_src = [p for p, _, _ in area["LNDARE"]] + [p for p, _, _ in area["OSM"]]
        st["land_area_chart"] = shapely.intersection(shapely.union_all(
            np.array(land_src, dtype=object), grid_size=GS), tb, grid_size=GS).area if land_src else 0.0
        st["land_area_mesh"] = float(sum(h.area for h, isl in holes if isl))
        T["8 checks"] = time.perf_counter() - t
        st["ok"] = True
        beat(i, j, "done")
    except Exception as ex:
        st["ok"] = False
        st["error"] = f"{type(ex).__name__}: {ex}"[:300]
        beat(i, j, "FAILED " + st["error"])
    st["T"] = T
    st["rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return st


def tpath(t):
    return f"{OUT}/tile_{t[0]:03d}_{t[1]:03d}.npz"


class BuildError(RuntimeError):
    pass


def split_segments_through_vertices(tp, moved):
    """After the seam weld moved the vertices `moved` (indices into
    tp["U"]), a segment ending at one of them may now pass through another
    vertex of the tile: a T-junction, which Triangle's quality refinement
    does not survive (01CGD, 2026-10-10: one weld in New York Harbor put a
    vertex exactly on a segment and the global refinement never finished;
    the same block refined in 7 s without the weld). Split every such
    segment at the vertices on it, keeping its part label. Returns the
    number of splits."""
    if not moved:
        return 0
    U, S_ = tp["U"], tp["S"]
    mv = np.fromiter(moved, dtype=np.int64)
    inc = np.flatnonzero(np.isin(S_[:, 0], mv) | np.isin(S_[:, 1], mv))
    if not len(inc):
        return 0
    segs = shapely.linestrings(U[S_[inc]])
    hits = STRtree(shapely.points(U)).query(segs, predicate="dwithin", distance=0.9 * GS)
    n_split = 0
    new_S, new_SP = [], []
    for k in np.unique(hits[0]):
        sidx = inc[k]
        a_, b_ = S_[sidx]
        vs = [int(v) for v in hits[1][hits[0] == k] if v != a_ and v != b_]
        if not vs:
            continue
        d = U[b_] - U[a_]
        L2 = float(d @ d)
        if L2 == 0.0:
            continue        # the weld collapsed this segment; the merge drops it when the two endpoints become one vertex
        # strictly interior, measured along the segment
        tv = [(float((U[v] - U[a_]) @ d) / L2, v) for v in vs]
        tv = [(t, v) for t, v in tv if 0.0 < t < 1.0
              and np.hypot(*(U[v] - U[a_])) > 0.5 * GS and np.hypot(*(U[v] - U[b_])) > 0.5 * GS]
        if not tv:
            continue
        chain = [a_] + [v for _, v in sorted(tv)] + [b_]
        S_[sidx] = [chain[0], chain[1]]
        for u, v in zip(chain[1:-1], chain[2:]):
            new_S.append([u, v]); new_SP.append(tp["SP"][sidx])
        n_split += len(tv)
    if new_S:
        tp["S"] = np.vstack([S_, np.array(new_S, dtype=S_.dtype)])
        tp["SP"] = np.append(tp["SP"], np.array(new_SP, dtype=tp["SP"].dtype))
    return n_split


RSS_UNIT = 2**20 if sys.platform == "darwin" else 1024   # ru_maxrss: bytes on macOS, kB on Linux


def _rss_mb(who=resource.RUSAGE_SELF):
    return resource.getrusage(who).ru_maxrss / RSS_UNIT


def _jsonable(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def run(workers, log=print):
    """Build the mesh for the configured rectangle into OUT, both stages in
    this process. Returns the summary dict (also written to
    OUT/summary.json). Raises BuildError if the global triangulation fails.

    The CLI runs the two stages in separate fresh interpreters instead
    (run_fresh): the tile stage leaves several GB of chart data and
    allocator residue in its process, and the global refinement, the
    memory peak of the build, must not inherit it (a 16 GB runner lost
    01CGD there, 2026-10-09). The handoff is on disk: the tile files plus
    OUT/tiles.json."""
    run_tiles(workers, log=log)
    return run_global(workers, log=log)


def run_tiles(workers, log=print):
    """Stage 3a: load the chart data and build every tile of the rectangle
    into OUT/tile_*.npz. The per-tile results the global stage and the
    summary need go to OUT/tiles.json. Returns that dict."""
    os.makedirs(OUT, exist_ok=True)
    log(f"box {BOX}  tile {TILE} deg  workers {workers}  K {K:.6f}")
    t0 = time.time()
    n_feat, n_osm = load()
    t_load = time.time() - t0
    log(f"load {t_load:.1f} s  features {n_feat:,}  OSM land polygons {n_osm:,}  "
        f"layer sets {len(SRC)}  point sets {len(PTS)}  sounding sets {len(SND)}  "
        f"rss {_rss_mb():.0f} MB")

    tiles = []
    nx = math.ceil((BOX[2] - BOX[0]) / TILE)
    ny = math.ceil((BOX[3] - BOX[1]) / TILE)
    for i in range(nx):
        for j in range(ny):
            w = BOX[0] + i * TILE; e = min(BOX[0] + (i + 1) * TILE, BOX[2])
            s = BOX[1] + j * TILE; n = min(BOX[1] + (j + 1) * TILE, BOX[3])
            tiles.append((i, j, w, s, e, n))
    log(f"tiles {len(tiles)}")

    t1 = time.time()
    results = []
    with get_context("fork").Pool(workers) as pool:
        for k, st in enumerate(pool.imap_unordered(process_tile, tiles, chunksize=1)):
            results.append(st)
            log(f"[{time.time()-t1:7.0f}s] {k+1}/{len(tiles)} tile {st['i']},{st['j']} "
                f"{'ok' if st['ok'] else 'FAIL ' + st.get('error', '')} "
                f"{sum(st['T'].values()):.1f}s cdt={st.get('tris_a', '-')}")
    t_tiles = time.time() - t1
    handoff = {"results": results, "t_load": t_load, "t_tiles": t_tiles, "workers": workers,
               "n_feat": n_feat, "n_osm": n_osm, "inv": dict(INV), "rejoin": {k: list(v) for k, v in REJOIN.items()},
               "covn": int(COVN),
               "rss_mb": {"tiles_main": _rss_mb(), "tiles_worker": _rss_mb(resource.RUSAGE_CHILDREN)}}
    with open(f"{OUT}/tiles.json", "w") as f:
        json.dump(handoff, f, default=_jsonable)
    log(f"tiles stage done: {len(results)} tiles in {t_tiles:.0f} s, peak RSS main {handoff['rss_mb']['tiles_main']:.0f} MB, "
        f"largest worker {handoff['rss_mb']['tiles_worker']:.0f} MB; handoff in {OUT}/tiles.json")
    return handoff


def run_global(workers, log=print, handoff=None):
    """Stage 3b: seam check and weld, the global quality refinement,
    neighbours, split into per-tile mesh files, checks. Reads the tile
    files and OUT/tiles.json written by run_tiles (or takes its dict).
    Returns the summary dict, also written to OUT/summary.json."""
    if handoff is None:
        with open(f"{OUT}/tiles.json") as f:
            handoff = json.load(f)
    results = handoff["results"]
    t_load, t_tiles = handoff["t_load"], handoff["t_tiles"]
    INV.clear(); INV.update(handoff["inv"])
    REJOIN.clear(); REJOIN.update({k: tuple(v) for k, v in handoff["rejoin"].items()})
    global COVN
    COVN = int(handoff.get("covn", COVN))        # requirement 15 counts coverage at load time
    t0 = time.time() - t_load - t_tiles          # so t_total spans both stages

    # ---- seam consistency of the tiles' noded input (PSLG): identical seam
    # vertex sets wherever both sides have water
    t2 = time.time()
    by = {(r["i"], r["j"]): r for r in results if r["ok"]}
    pairs = identical = seam_mismatch = 0
    for (i, j), r in sorted(by.items()):
        for (di, dj, a, b) in ((1, 0, "R", "L"), (0, 1, "T", "B")):
            o = by.get((i + di, j + dj))
            if o is None:
                continue
            A_ = {tuple(p) for p in r["edges"][a]}; B_ = {tuple(p) for p in o["edges"][b]}
            if not A_ and not B_:
                continue
            pairs += 1
            if A_ == B_:
                identical += 1
            else:
                seam_mismatch += len(A_ ^ B_)
    t_seamchk = time.time() - t2

    # ---- global PSLG and one quality refinement for the whole rectangle
    t3 = time.time()
    SRC.clear(); PTS.clear(); SND.clear(); COV.clear()     # free the chart data first
    nx_ = math.ceil((BOX[2] - BOX[0]) / TILE); ny_ = math.ceil((BOX[3] - BOX[1]) / TILE)
    seamX = {float(np.round((BOX[0] + k * TILE) * K / GS) * GS) for k in range(1, nx_)}
    seamY = {float(np.round((BOX[1] + k * TILE) / GS) * GS) for k in range(1, ny_)}
    TP = {}
    for t in sorted(by):
        z = np.load(tpath(t))
        TP[t] = {"U": z["pslg_V"].copy(), "S": z["pslg_S"], "R": z["pslg_R"], "H": z["pslg_H"],
                 "SP": z["seg_part"], "lab": z["keys"][z["part_key"]] if len(z["part_key"]) else np.empty((0, len(z["key_cols"])))}
    # seam weld: a seam vertex within 2 grid units of a seam vertex of the
    # neighbour takes the neighbour's coordinates; a seam vertex lying inside
    # a seam segment of the neighbour splits that segment; what is left faces
    # land on the other side (no segment there) or is a defect
    def split_at(tp, axis, val, p):
        U, S_ = tp["U"], tp["S"]
        o = 1 - axis
        on = np.abs(U[:, axis] - val) < 0.6 * GS
        cand = np.flatnonzero(on[S_[:, 0]] & on[S_[:, 1]])
        for k in cand:
            a_, b_ = S_[k]
            lo, hi = sorted((U[a_, o], U[b_, o]))
            if lo < p[o] < hi:
                tp["U"] = np.vstack([U, p]); n_ = len(tp["U"]) - 1
                S2 = np.vstack([S_, [a_, n_]]); S2[k] = [n_, b_]
                tp["S"] = S2
                tp["SP"] = np.append(tp["SP"], tp["SP"][k])
                return True
        return False
    welded = split = facing_land = 0
    moved = {}                      # tile -> vertex indices the weld moved
    for (i, j) in sorted(by):
        for (di, dj, axis, val_of) in ((1, 0, 0, lambda i, j: (BOX[0] + (i + 1) * TILE) * K),
                                       (0, 1, 1, lambda i, j: BOX[1] + (j + 1) * TILE)):
            nb = (i + di, j + dj)
            if nb not in by:
                continue
            val = np.round(val_of(i, j) / GS) * GS
            o = 1 - axis
            for X, Y in (((i, j), nb), (nb, (i, j))):
                UX, UY = TP[X]["U"], TP[Y]["U"]
                ix = np.flatnonzero(np.abs(UX[:, axis] - val) < 0.6 * GS)
                iy = np.flatnonzero(np.abs(UY[:, axis] - val) < 0.6 * GS)
                sy = {tuple(UY[k]) for k in iy}
                for kx in ix:
                    if tuple(UX[kx]) in sy:
                        continue
                    if len(iy):
                        d = np.abs(UY[iy, o] - UX[kx, o])
                        m = int(np.argmin(d))
                        if d[m] <= 2.01 * GS:
                            UX[kx] = UY[iy[m]]; welded += 1
                            moved.setdefault(X, set()).add(int(kx))
                            continue
            # after welding, split segments at the other side's remaining vertices
            for X, Y in (((i, j), nb), (nb, (i, j))):
                UX = TP[X]["U"]
                ix = np.flatnonzero(np.abs(UX[:, axis] - val) < 0.6 * GS)
                for kx in ix:
                    UY = TP[Y]["U"]
                    iy = np.flatnonzero(np.abs(UY[:, axis] - val) < 0.6 * GS)
                    if any(np.array_equal(UY[k], UX[kx]) for k in iy):
                        continue
                    if split_at(TP[Y], axis, val, UX[kx].copy()):
                        split += 1
                    else:
                        facing_land += 1
    # a moved vertex can put one of its segments exactly through another
    # vertex of its tile; split those segments there (see the helper)
    tjunction_split = sum(split_segments_through_vertices(TP[t], moved.get(t, ())) for t in sorted(by))
    Vs, Ss, Rs, Hs, part_tile, part_local, LABs = [], [], [], [], [], [], []
    off = 0
    for t in sorted(by):
        U, S_, R_, H_ = TP[t]["U"], TP[t]["S"], TP[t]["R"], TP[t]["H"]
        if len(U):
            Vs.append(U); Ss.append(S_ + off)
            LABs.append(TP[t]["lab"][TP[t]["SP"]])
            g0 = len(part_tile)
            Rs.append(np.column_stack([R_[:, :2], R_[:, 2] + g0, R_[:, 3]]))
            part_tile += [t] * len(R_); part_local += list(range(len(R_)))
            off += len(U)
        if len(H_):
            Hs.append(H_)
    del TP
    if not Vs:
        raise BuildError("no water in the rectangle: nothing to triangulate")
    allV = np.vstack(Vs)
    gV, ginv = np.unique(allV, axis=0, return_inverse=True)
    gS = np.sort(ginv.ravel()[np.vstack(Ss)], axis=1)
    LAB = np.vstack(LABs)
    keep_ = gS[:, 0] != gS[:, 1]
    gS, LAB = gS[keep_], LAB[keep_]
    gS, sinv, scount = np.unique(gS, axis=0, return_inverse=True, return_counts=True)
    sinv = sinv.ravel()
    # labels on the two sides of each segment that occurs twice (one per tile)
    order_ = np.argsort(sinv, kind="stable")
    starts = np.concatenate([[0], np.cumsum(scount)[:-1]])
    same_label = np.ones(len(gS), dtype=bool)
    two = np.flatnonzero(scount == 2)
    o1, o2 = order_[starts[two]], order_[starts[two] + 1]
    same_label[two] = np.all(LAB[o1] == LAB[o2], axis=1)
    del LAB, LABs, Vs, Ss, allV, ginv, order_, starts, o1, o2    # not needed past here; the refinement is the memory peak
    p_, q_ = gV[gS[:, 0]], gV[gS[:, 1]]
    on_vx = (p_[:, 0] == q_[:, 0]) & np.isin(p_[:, 0], list(seamX))
    on_hy = (p_[:, 1] == q_[:, 1]) & np.isin(p_[:, 1], list(seamY))
    interior_seam = on_vx | on_hy
    drop = interior_seam & (scount == 2) & same_label
    seam_label_kept = int((interior_seam & (scount == 2) & ~same_label).sum())
    seam_single = int((interior_seam & (scount == 1)).sum())
    gS = gS[~drop]
    used = np.unique(gS)
    remap = -np.ones(len(gV), dtype=np.int64); remap[used] = np.arange(len(used))
    gV = gV[used]; gS = remap[gS]
    seam_dropped = int(drop.sum())
    gR = np.vstack(Rs)
    gH = np.vstack(Hs) if Hs else np.empty((0, 2))
    log(f"seam weld: {welded} vertices welded (<= 2 grid units), {split} segments split, {facing_land} facing land, "
        f"{tjunction_split} segments split where a welded vertex's segment passed through a vertex; "
        f"interior seam segments dropped {seam_dropped:,}, kept because the two sides' labels differ {seam_label_kept}, "
        f"single-sided seam segments kept {seam_single}")
    C = (gV.min(0) + gV.max(0)) / 2
    tin = {"vertices": gV - C, "segments": gS,
           "regions": np.column_stack([gR[:, :2] - C, gR[:, 2:]])}
    if len(gH):
        hull = shapely.convex_hull(shapely.multipoints(gV))
        gH = gH[shapely.contains_xy(hull, gH[:, 0], gH[:, 1])]
        tin["holes"] = gH - C
    t_merge = time.time() - t3
    log(f"global PSLG: {len(gV):,} vertices, {len(gS):,} segments, {len(gR):,} regions, {len(gH):,} holes; "
        f"merge {t_merge:.1f} s")
    t4 = time.time()
    res = tri_safe(tin, "pq20AQ")
    t_refine = time.time() - t4
    if "error" in res:
        raise BuildError(f"global triangulation failed: {res['error']}")
    V = res["vertices"] + C
    T = res["triangles"].astype(np.int64)
    TA = res["triangle_attributes"].ravel().astype(np.int64)
    log(f"global refinement: {len(T):,} triangles, {len(V):,} vertices in {t_refine:.1f} s")

    # ---- neighbours: for every triangle, the triangle across each edge (-1 = none)
    t5n = time.time()
    nT = len(T)
    E = np.sort(np.vstack([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]]), axis=1)
    tri_id = np.concatenate([np.arange(nT)] * 3)
    edge_k = np.repeat(np.arange(3), nT)
    order_e = np.lexsort((E[:, 1], E[:, 0]))
    Es = E[order_e]
    same = np.all(Es[1:] == Es[:-1], axis=1)
    ia, ib = order_e[:-1][same], order_e[1:][same]
    NB = np.full((nT, 3), -1, dtype=np.int64)
    NB[tri_id[ia], edge_k[ia]] = tri_id[ib]
    NB[tri_id[ib], edge_k[ib]] = tri_id[ia]
    # edge use counts for the checks (an edge in >2 triangles shows as a run of 3)
    run3 = int((same[:-1] & same[1:]).sum()) if len(same) > 1 else 0
    one_sided = np.ones(len(Es), bool); one_sided[1:][same] = False; one_sided[:-1][same] = False
    bnd = Es[one_sided]
    del E, Es, tri_id, edge_k, order_e, same, ia, ib, one_sided
    t_nb = time.time() - t5n
    log(f"neighbours: {nT:,} triangles in {t_nb:.1f} s")

    # ---- split into per-tile files (by triangle centroid), global vertex ids kept,
    # with the routing data: neighbours, shore and depth penalties per triangle
    t5 = time.time()
    cen = V[T].mean(axis=1)
    ti = np.clip(np.floor((cen[:, 0] / K - BOX[0]) / TILE).astype(int), 0, None)
    tj = np.clip(np.floor((cen[:, 1] - BOX[1]) / TILE).astype(int), 0, None)
    order = np.lexsort((tj, ti))
    part_tile_a = np.array(part_tile); part_local_a = np.array(part_local)
    LAB_CACHE = {}
    SHORE_ALL = np.zeros(nT, dtype=np.float32); DM_ALL = np.zeros(nT, dtype=np.float32)
    bytes_out = 0
    keys_t = ti[order] * 10000 + tj[order]
    bounds_idx = np.flatnonzero(np.diff(keys_t)) + 1
    tile_tris = {}
    for grp in np.split(order, bounds_idx):
        if not len(grp):
            continue
        ii, jj = int(ti[grp[0]]), int(tj[grp[0]])
        tt = T[grp]
        vid = np.unique(tt)
        loc = np.searchsorted(vid, tt)
        # dist_to_shore and shore penalty: the grid cell holding each
        # triangle's centroid (cell index by truncation)
        zt = np.load(tpath((ii, jj)))
        cg = cen[grp]
        gj = np.floor((cg[:, 0] / K - PG_W) / PG_RES).astype(np.int64) - int(zt["shore_j0"])
        gi = np.floor((cg[:, 1] - PG_S) / PG_RES).astype(np.int64) - int(zt["shore_i0"])
        gi = np.clip(gi, 0, zt["shore"].shape[0] - 1); gj = np.clip(gj, 0, zt["shore"].shape[1] - 1)
        shore_t = zt["shore"][gi, gj]
        dist_t = zt["dist_cells"][gi, gj]
        # is_navigable per triangle, from its label
        pg_ = TA[grp]
        isnav_t = np.zeros(len(grp), dtype=bool)
        for p_ in np.unique(pg_):
            t_ = tuple(part_tile_a[p_]); zl = LAB_CACHE.get(t_)
            if zl is None:
                zz = np.load(tpath(t_)); zl = LAB_CACHE[t_] = (zz["keys"], zz["part_key"], list(zz["key_cols"]))
            isnav_t[pg_ == p_] = zl[0][zl[1][part_local_a[p_]], zl[2].index("is_nav")] > 0
        # depth penalty from each triangle's label (old router's bands)
        pg = TA[grp]
        dep = np.empty(len(grp))
        for p_ in np.unique(pg):
            t_ = tuple(part_tile_a[p_]); zl = LAB_CACHE.get(t_)
            if zl is None:
                zz = np.load(tpath(t_)); zl = LAB_CACHE[t_] = (zz["keys"], zz["part_key"], list(zz["key_cols"]))
            dep[pg == p_] = zl[0][zl[1][part_local_a[p_]], zl[2].index("depth")]
        dmult = np.ones(len(grp), dtype=np.float32)
        kn = dep != -999.0
        dmult[kn & (dep < 2.0)] = 2.0
        dmult[kn & (dep >= 2.0) & (dep < 5.0)] = 1.5
        dmult[kn & (dep >= 5.0) & (dep < 10.0)] = 1.2
        path = f"{OUT}/mesh_{ii:03d}_{jj:03d}.npz"
        np.savez_compressed(path, vertex_id=vid, vertices=V[vid], triangles=loc.astype(np.int32),
                            part_global=pg.astype(np.int32), tri_id=grp.astype(np.int64),
                            neighbours=NB[grp], shore_mult=shore_t.astype(np.float32), depth_mult=dmult,
                            dist_to_shore=dist_t.astype(np.float32), is_navigable=isnav_t)
        SHORE_ALL[grp] = shore_t; DM_ALL[grp] = dmult
        bytes_out += os.path.getsize(path)
        tile_tris[(ii, jj)] = len(grp)
    np.savez_compressed(f"{OUT}/parts.npz", part_tile=part_tile_a, part_local=part_local_a)
    t_split = time.time() - t5

    # ---- checks on the global mesh
    t6 = time.time()
    a_, b_, c_ = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    cr = (b_[:, 0] - a_[:, 0]) * (c_[:, 1] - a_[:, 1]) - (b_[:, 1] - a_[:, 1]) * (c_[:, 0] - a_[:, 0])
    cw, zero = int((cr < 0).sum()), int((cr == 0).sum())
    mesh_area = np.abs(cr).sum() / 2
    water_area = sum(r.get("water_area", 0.0) for r in results if r["ok"])
    area_rel = abs(mesh_area - water_area) / water_area
    nonmanifold = run3
    subseg = np.sort(res["segments"].astype(np.int64), axis=1)
    sub_set = set(map(tuple, subseg))
    cracks = sum(1 for e in map(tuple, bnd) if e not in sub_set)
    # neighbour symmetry, penalty ranges
    nb_asym = 0
    for c0 in range(0, nT, 2_000_000):
        blk = NB[c0:c0 + 2_000_000]
        tt_, kk_ = np.nonzero(blk >= 0)
        back = NB[blk[tt_, kk_]]
        nb_asym += int((~np.any(back == (tt_ + c0)[:, None], axis=1)).sum())
    shore_bad = int(((SHORE_ALL < 1.0) | (SHORE_ALL > 1.3 + 1e-6)).sum())
    dm_bad = int((~np.isin(DM_ALL, np.array([1.0, 1.2, 1.5, 2.0], dtype=np.float32))).sum())
    # route-ready: load a region from the files alone, nothing computed.
    # The region is the 3x3 block around the tile with the most triangles
    # (the experiment used a fixed Woods Hole box).
    tr0 = time.time()
    rv, rt, rn, rs = [], [], [], 0
    ci_, cj_ = max(tile_tris, key=tile_tris.get)
    region_tiles = [(ci_ + di, cj_ + dj) for di in (-1, 0, 1) for dj in (-1, 0, 1)]
    for fi, fj in region_tiles:
        fpath = f"{OUT}/mesh_{fi:03d}_{fj:03d}.npz"
        if os.path.exists(fpath):
            zr = np.load(fpath)
            need_ = ("vertex_id", "vertices", "triangles", "part_global", "tri_id", "neighbours", "shore_mult", "depth_mult", "dist_to_shore", "is_navigable")
            if not all(k_ in zr.files for k_ in need_):
                rs += 1
            rt.append(zr["tri_id"]); rn.append(zr["neighbours"])
    region_tri = np.concatenate(rt) if rt else np.empty(0, int)
    region_nb = np.vstack(rn) if rn else np.empty((0, 3), int)
    inside = np.isin(region_nb[region_nb >= 0], region_tri)
    t_region = time.time() - tr0
    region_ok = rs == 0 and len(region_tri) > 0
    region_lonlat = (BOX[0] + ci_ * TILE, BOX[1] + cj_ * TILE)

    def ang(p, q, r):
        u, v = q - p, r - p
        return np.degrees(np.arccos(np.clip((u * v).sum(1) / np.hypot(*u.T) / np.hypot(*v.T), -1, 1)))
    m = np.minimum(np.minimum(ang(a_, b_, c_), ang(b_, c_, a_)), ang(c_, a_, b_))
    small = np.flatnonzero(m < 20.0)
    # sharp input vertices: two input segments meeting at < 20 deg
    inc = [[] for _ in range(len(gV))]
    for x, y in gS:
        inc[x].append(y); inc[y].append(x)
    sharp = []
    for v, nb in enumerate(inc):
        if len(nb) < 2:
            continue
        d = gV[nb] - gV[v]
        th = np.sort(np.degrees(np.arctan2(d[:, 1], d[:, 0])) % 360)
        if np.diff(np.append(th, th[0] + 360)).min() < 20.0:
            sharp.append(v)
    unexplained = len(small)
    hit = np.zeros(len(small), bool)
    if len(small) and sharp:
        tree = cKDTree(gV[sharp])
        for col in range(3):
            d, _ = tree.query(V[T[small, col]])
            hit |= d == 0
        unexplained = int((~hit).sum())
    attr_bad = int(((TA < 0) | (TA >= len(part_tile))).sum())
    SRCNAME = {0: "chart", 1: "circle", 2: "strip", 3: "tile edge", 4: "seam merge"}
    lookup = {}
    for t in sorted(by):
        z = np.load(tpath(t))
        for xy, sc in zip(z["sharp_xy"], z["sharp_src"]):
            lookup[(float(xy[0]), float(xy[1]))] = int(sc)
    gsrc = np.array([lookup.get((float(gV[v][0]), float(gV[v][1])), 4) for v in sharp], dtype=int)
    sharp_by_src = {SRCNAME[c]: int((gsrc == c).sum()) for c in SRCNAME}
    small_by_src = {}
    if len(small) and sharp:
        dsm, ism = tree.query(cen[small])
        for c in SRCNAME:
            small_by_src[SRCNAME[c]] = int((gsrc[ism] == c).sum())
    t_check = time.time() - t6
    t_total = time.time() - t0

    ok = [r for r in results if r["ok"]]
    stages = {}
    for r in ok:
        for k, v in r["T"].items():
            stages[k] = stages.get(k, 0.0) + v
    tot = lambda key: sum(r.get(key, 0) for r in ok)
    rss = dict(handoff.get("rss_mb", {}))
    rss["global_main"] = _rss_mb()
    rss["refine_child"] = _rss_mb(resource.RUSAGE_CHILDREN)     # the Triangle fork
    rss_main = max(rss.get("tiles_main", 0.0), rss["global_main"])
    rss_worker = max(rss.get("tiles_worker", 0.0), rss["refine_child"])
    log("\n==== summary")
    log(f"load (single process)       {t_load:9.1f} s")
    log(f"tiles wall ({workers} workers)      {t_tiles:9.1f} s")
    log(f"seam consistency check      {t_seamchk:9.1f} s")
    log(f"global PSLG merge           {t_merge:9.1f} s")
    log(f"global quality refinement   {t_refine:9.1f} s")
    log(f"neighbours                  {t_nb:9.1f} s")
    log(f"split into tile files       {t_split:9.1f} s  (incl. shore and depth penalties per triangle)")
    log(f"final checks                {t_check:9.1f} s")
    log(f"TOTAL wall                  {t_total:9.1f} s  ({t_total/60:.1f} min)")
    log("stage CPU-seconds summed over tiles:")
    for k in sorted(stages):
        log(f"  {k:22s} {stages[k]:10.1f}")
    log(f"layers loaded (features, all sources): " + ", ".join(f"{k} {v:,}" for k, v in sorted(INV.items())))
    if REJOIN:
        log("z16 tile pieces rejoined per feature: " + ", ".join(f"{k} {a:,}->{b:,}" for k, (a, b) in sorted(REJOIN.items())))
    log(f"triangles {len(T):,}  vertices {len(V):,}  water parts {tot('water_parts'):,}  "
        f"plain CDT triangles {tot('tris_a'):,}  medial edges {tot('medial_edges'):,}  mesh files {bytes_out/1e6:.1f} MB")
    log(f"peak RSS: tiles stage main {rss.get('tiles_main', 0):.0f} MB, largest tile worker {rss.get('tiles_worker', 0):.0f} MB; "
        f"global stage main {rss['global_main']:.0f} MB, refinement child {rss['refine_child']:.0f} MB")

    need = sorted(LAND | STRUCT | DEPTH | HAZ | MARKS | CLEAR | ZONES | {"SOUNDG", "M_COVR"})
    missing_layers = [L for L in need if INV.get(L, 0) == 0]
    lc, lm = tot("land_area_chart"), tot("land_area_mesh")
    R = [
        ("1 tiles built", len(ok) == len(results), f"{len(ok)}/{len(results)}"),
        ("2 layers loaded", not missing_layers, f"missing: {missing_layers}" if missing_layers else f"{len(need)} layers"),
        ("3 hazards+marks covered", tot("haz_bad") + tot("mark_bad") == 0,
         f"haz {tot('haz_checked'):,} checked, {tot('haz_bad')} not covered; marks {tot('mark_checked'):,} checked, {tot('mark_bad')} not covered"),
        ("4 land area", lc > 0 and abs(lm / lc - 1) <= 1e-6, f"mesh/chart {lm/lc:.9f}" if lc > 0 else "no land in the rectangle"),
        ("5 seams / no walls", nonmanifold == 0 and area_rel <= 1e-8 and cracks == 0,
         f"tile inputs identical on {identical}/{pairs} seams ({seam_mismatch} differing vertices): welded {welded}, "
         f"segments split {split}, vertices facing land on the other side {facing_land}; water seam segments removed "
         f"{seam_dropped:,}, land-boundary seam segments kept {seam_single}; edges in >2 triangles {nonmanifold}; "
         f"mesh/water area difference {area_rel:.2e}; seam segments kept for differing labels {seam_label_kept}; "
         f"one-sided edges not on a chart boundary (cracks = walls) {cracks}"),
        ("6 triangles valid", cw + zero == 0, f"clockwise {cw}, zero-area {zero}"),
        ("7 angles >= 20 deg", unexplained == 0,
         f"{len(small):,} triangles under 20 deg ({len(small)/len(T)*100:.3f} %), at a sharp input vertex {len(small)-unexplained:,}, "
         f"UNEXPLAINED {unexplained:,}; smallest {m.min():.3f} deg; sharp input vertices {len(sharp):,}"),
        ("8 attributes", attr_bad == 0, f"triangles without a valid part {attr_bad}"),
        ("12 neighbours stored", nb_asym == 0, f"asymmetric neighbour links {nb_asym}; computed in {t_nb:.1f} s"),
        ("13 penalties stored", shore_bad == 0 and dm_bad == 0,
         f"shore penalty outside 1.0-1.3: {shore_bad}; depth penalty not in {{1,1.2,1.5,2}}: {dm_bad}"),
        ("14 route-ready from files", region_ok,
         f"3x3 region around the densest tile (SW corner {region_lonlat[0]:.2f}, {region_lonlat[1]:.2f}) loaded from files alone "
         f"in {t_region:.2f} s: {len(region_tri):,} triangles, files missing routing arrays {rs}, "
         f"neighbour links leaving the region {int((~inside).sum()):,} of {len(inside):,}"),
        # Without usable coverage (M_COVR with CATCOV=1) the OSM land polygons
        # override the chart everywhere (the Great Lakes, the Hudson, every
        # harbour behind the OSM coastline become land), so its absence
        # gates: an old z16_layers reused with --reuse-decoded, or a chart
        # whose tiles lack it. Counted at load time; COV is freed before here.
        ("15 chart coverage loaded", COVN > 0,
         f"M_COVR CATCOV=1 polygons {COVN:,} (M_COVR features {INV.get('M_COVR', 0):,})" if COVN
         else f"no M_COVR with CATCOV=1 in the decoded input (M_COVR features {INV.get('M_COVR', 0):,})"),
        ("9 medial axis", MEDIAL_STEP * DEG_M <= 10.0 + 1e-9, f"boundary sampled every {MEDIAL_STEP*DEG_M:.0f} m"),
        ("10 hazard data stored", True, "VALSOU (hazv) and CATZOC columns in every tile's attribute table"),
        ("11 time measured", True, f"{t_total:.1f} s end to end"),
    ]
    log(f"circles snapped onto nearby lines: {tot('discs_snapped'):,}; sounding squares {tot('snd_squares'):,}")
    log(f"sharp input corners by source: {sharp_by_src}")
    log(f"triangles under 20 deg by source of the nearest sharp corner: {small_by_src}")
    log("\n==== REQUIREMENTS")
    for name, passed, detail in R:
        log(f"{'PASS' if passed else 'FAIL'}  {name}: {detail}")
    bad_h = [b for r in ok for b in r.get("bad_examples", [])]
    if bad_h:
        log(f"  not-covered hazards/marks (layer, lon, lat): {bad_h[:10]}")
    if unexplained and sharp:
        une = small[~hit]
        dist, _ = tree.query(cen[une])
        L_ = np.max(np.column_stack([np.hypot(*(a_[une] - b_[une]).T), np.hypot(*(b_[une] - c_[une]).T),
                                     np.hypot(*(c_[une] - a_[une]).T)]), axis=1)
        ratio = dist / L_
        log(f"  unexplained: distance to nearest sharp input vertex in triangle-lengths: median {np.median(ratio):.1f}, "
            f"90th pct {np.percentile(ratio, 90):.1f}, within 5 lengths {(ratio <= 5).mean()*100:.1f} %; "
            f"metres median {np.median(dist)*DEG_M:.2f}")
    if unexplained:
        log(f"  unexplained small-angle examples (lon, lat, angle): " + ", ".join(
            f"({cen[k,0]/K:.6f}, {cen[k,1]:.6f}, {m[k]:.2f})" for k in small[:5]))
    failed_tiles = [(r["i"], r["j"], r.get("error", "")) for r in results if not r["ok"]]
    summary = {
        "box": list(BOX), "tile_deg": TILE, "x_scale": K, "workers": workers,
        "t_total": t_total,
        "timings": {"load": t_load, "tiles": t_tiles, "seam_check": t_seamchk, "merge": t_merge,
                    "refine": t_refine, "neighbours": t_nb, "split": t_split, "checks": t_check},
        "stages": stages,
        "tiles": len(results), "tiles_ok": len(ok), "failed_tiles": failed_tiles,
        "mesh_files": len(tile_tris),
        "triangles": int(len(T)), "vertices": int(len(V)),
        "water_parts": int(tot("water_parts")), "medial_edges": int(tot("medial_edges")),
        "layers": dict(sorted(INV.items())),
        "rejoined": {k: list(v) for k, v in REJOIN.items()},
        "peak_rss_mb": {"main": rss_main, "largest_worker": rss_worker, **rss},
        "requirements": [(a, bool(b), c) for a, b, c in R],
        "not_covered_examples": bad_h[:10],
    }
    with open(f"{OUT}/summary.json", "w") as f:
        json.dump(summary, f)
    return summary


# ---- the two stages in fresh interpreters (what the CLI uses)

def _stage_entry(stage, config, workers, log_path):
    """Target of a spawned interpreter: configure, run one stage, log to
    stdout and to log_path. A BuildError is written to OUT/error.txt and
    exits 1 so the parent can raise it."""
    configure(**config)

    def log(msg=""):
        print(msg, flush=True)
        if log_path:
            with open(log_path, "a") as f:
                f.write(msg + "\n")
    try:
        if stage == "tiles":
            run_tiles(workers, log=log)
        else:
            run_global(workers, log=log)
    except BuildError as ex:
        with open(f"{config['out']}/error.txt", "w") as f:
            f.write(str(ex))
        sys.exit(1)


def run_fresh(config, workers, log_path=None):
    """Run stage 3a and then 3b, each in a freshly spawned interpreter, so
    the global refinement starts with an empty heap (see run). `config`
    is the keyword dict for configure(). Returns the summary dict read
    from OUT/summary.json. Raises BuildError when a stage fails."""
    import multiprocessing
    out = config["out"]
    os.makedirs(out, exist_ok=True)
    err = f"{out}/error.txt"
    if os.path.exists(err):
        os.remove(err)
    ctx = multiprocessing.get_context("spawn")
    for stage in ("tiles", "global"):
        p = ctx.Process(target=_stage_entry, args=(stage, config, workers, log_path), name=f"mesh-{stage}")
        p.start()
        p.join()
        if p.exitcode != 0:
            if os.path.exists(err):
                raise BuildError(open(err).read())
            raise BuildError(f"stage {stage} exited with {p.exitcode}"
                             + (" (killed by signal; out of memory?)" if p.exitcode and p.exitcode < 0 else ""))
    with open(f"{out}/summary.json") as f:
        return json.load(f)
