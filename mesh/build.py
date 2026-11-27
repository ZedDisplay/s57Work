"""The mesh build, tile stage (see mesh/__init__.py for the whole build).

What it does, per 0.25-degree tile of the fixed world lattice:
  1. reads every chart cell that reaches the tile (areas, lines, points, soundings), in the source's own whole
     numbers (units of 1e-7 degree);
  2. hands all of it, whole, to the exact cutter (mesh/exact_cut.cpp);
  3. takes each triangle's attributes from the shapes the cut says it is inside. The most detailed chart wins:
     a chart speaks where it has a "skin of the earth" area (DEPARE, DRGARE, UNSARE, LNDARE, FLODOC, HULKES,
     PONTON); within bands 1 and 2 a new-scheme cell wins over a legacy cell (the chart build's rule);
  4. chart lines stay lines and chart points stay points: a side lying on a line of the winning chart, and a
     corner that is a point of the winning chart, are recorded with what the chart says about them. A sounding
     is kept where it is shallower than the depth area it lies in (or lies in none);
  5. writes the tile's outline pieces (un-merged: whether a point is a real corner is decided after the tiles
     are joined), the kept chart points, and the cut triangles themselves. An outline piece runs from pa to pb
     with the cut triangle it was taken from on its left (the cut's triangles are anticlockwise and side k of
     a triangle runs from its corner k + 1 to its corner k + 2): pla is the water on its left, plb the water on
     its right (-1 not water, -2 outside the tile box).

The cells are those the first stage read from the district's charts (mesh/cells.py): one copy of each, each with
the files of its layers. The tiles built are those the cells reach (the bounding box of each cell's
skin-of-the-earth areas); the mesh holds all the water those cells chart, the most detailed of them winning at
every spot. No other district's charts are read.
"""
import functools, json, math, os, resource, subprocess, sys, time
import numpy as np, shapely
from shapely.geometry import shape
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

EXE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exact_cut")      # built from exact_cut.cpp by mesh/cli.py
U7 = 1e7
TILE = 2_500_000                      # 0.25 degree
# The tile lattice's origin: -180, -90 degrees exactly, so the date line (180 = -180) is a tile edge and no tile
# lies across it, as the router reads a mesh (signalk-weather-router-plus src/engine/mesh/store.ts). Columns
# 0 to N_COLS - 1 count east from -180; the column east of the last is the first.
ORIGIN_X = -1_800_000_000
ORIGIN_Y = -900_000_000
N_COLS = 3_600_000_000 // TILE        # 1440 columns round the earth
TURN = 3_600_000_000                  # 360 degrees
DATE_LINE = 1_800_000_000             # the date line as the tile west of it writes it


def shortest_run(columns):
    """(west column, columns in the run) of the shortest run of tile columns, going east, that holds every
    column given: the run that starts just east of the widest stretch of empty columns round the earth. The
    stretch across the date line counts like any other, and when it is the widest (or as wide as the widest)
    the run is the plain one from the lowest column to the highest. A run that starts at a higher column than
    it ends at, counted in plain numbers, crosses the date line."""
    cols = sorted(set(int(c) % N_COLS for c in columns))
    if not cols:
        raise ValueError("no columns")
    best_gap, west = cols[0] + N_COLS - cols[-1] - 1, cols[0]          # the stretch across the date line
    for a, b in zip(cols, cols[1:]):
        if b - a - 1 > best_gap:
            best_gap, west = b - a - 1, b
    return west, N_COLS - best_gap
BANDS = ["band6", "band5", "band4", "band3", "band2", "band1"]
RESCHEMED = {"band1": {"GLB"}, "band2": {"ARC", "ANT", "ATL", "GRL", "GOM", "PAC"}}     # s57-to-mbtiles.py RESCHEMED_REGION_CODES

SKIN = {"DEPARE", "DRGARE", "UNSARE", "LNDARE", "FLODOC", "HULKES", "PONTON"}
LAND = {"LNDARE"}
STRUCT = {"SLCONS", "PONTON", "FLODOC", "HULKES", "CAUSWY", "DAMCON", "GATCON", "DRYDOC", "MORFAC", "OFSPLF", "PILPNT"}
HAZ = {"OBSTRN", "WRECKS", "UWTROC"}
MARKS = {"BOYLAT", "BOYCAR", "BOYSPP", "BOYSAW", "BOYISD", "BCNLAT", "BCNSPP", "LIGHTS", "DAYMAR", "PILBOP", "MORFAC", "OFSPLF", "PILPNT"}
CHANMARK = {"BOYLAT", "BCNLAT", "DAYMAR"}
CLEAR = {"BRIDGE", "CBLOHD", "PIPOHD", "CONVYR"}
AREA_LAYERS = sorted(SKIN | STRUCT | HAZ | CLEAR | {"FAIRWY", "RECTRC"})
LINE_LAYERS = sorted(STRUCT | {"OBSTRN", "WRECKS"} | CLEAR | {"RECTRC", "NAVLNE"})
POINT_LAYERS = sorted(HAZ | MARKS | {"SOUNDG"})
LAYERS = sorted(set(AREA_LAYERS) | set(LINE_LAYERS) | set(POINT_LAYERS))
LAYER_NO = {L: k for k, L in enumerate(LAYERS)}
# what the mesh file carries per triangle
COLS = ["land", "struct", "depth", "dredged", "clear", "fair", "haz", "hazv", "is_nav", "opening", "nochart", "nowater"]
# Water is where the winning chart has one of these; a pontoon, hulk or floating dock with none under it is not water.
WATER = {"DEPARE", "DRGARE", "UNSARE"}
# side marks (bits) and corner marks (bits)
S_STRUCT, S_HAZ, S_OVERHEAD, S_OPENING, S_TRACK, S_NAVLINE = 1, 2, 4, 8, 16, 32
C_HAZ, C_MARK, C_CHANMARK, C_SOUNDING = 1, 2, 4, 8
OPENING_BRIDGE = {2, 3, 4, 5, 7, 8}


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clearance_of(L, pr):
    """(clearance in metres or None, opening) of an overhead feature. An opening bridge (CATBRG opening, swing,
    lifting, bascule, draw, transporter) carries its open clearance (VERCOP) or none, never the closed one; any
    other span the first of VERCCL, VERCLR, VERCSA that is charted. A span with no charted height is None, not 0.
    Only a BRIDGE opens: CATBRG on another layer is not read."""
    if L == "BRIDGE":
        v = pr.get("CATBRG")
        if v is not None:
            codes = str(v).replace("(", "").replace(")", "").split(",")
            if any(c.strip().isdigit() and int(c) in OPENING_BRIDGE for c in codes):
                return num(pr.get("VERCOP")), True
    return next((num(pr.get(k)) for k in ("VERCCL", "VERCLR", "VERCSA") if num(pr.get(k)) is not None), None), False


def polys(g):
    return [p for p in shapely.get_parts(g) if p.geom_type == "Polygon" and not p.is_empty]


def units(g, dx=0):
    """A shape in units of 1e-7 degree (whole numbers), dx units further east (0, or a turn either way)."""
    return shapely.transform(g, lambda c: np.round(c[:, :2] * U7) + (dx, 0))


@functools.lru_cache(maxsize=900)
def layer_file(path):
    """The features of one listed layer file: [(shape, properties) or None]."""
    out = []
    with open(path) as fh:
        features = json.load(fh)["features"]
    for ft in features:
        g = ft.get("geometry")
        if not g:
            out.append(None); continue
        try:
            out.append((shape(g), dict(ft["properties"] or {})))
        except Exception:
            out.append(None)
    return out


CELL_INDEX = "cell-index.json"        # the record of the cells the tile stage read, in the run's folder


def cell_bbox(item):
    """Bounding box (degrees) of a cell's skin-of-the-earth areas, or None when it has none."""
    band, name, source, files = item
    b = None
    for L in sorted(SKIN):
        p = files.get(L)
        if p is None:
            continue
        with open(p) as fh:
            features = json.load(fh)["features"]
        for ft in features:
            g = ft.get("geometry")
            if g and g["type"] in ("Polygon", "MultiPolygon"):
                x0, y0, x1, y1 = shape(g).bounds
                b = [x0, y0, x1, y1] if b is None else [min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)]
    return band, name, source, files, b


def cell_index(records, workers):
    """The cells the tile stage reads, of those given: [(rank, band, name, source, bbox, {layer: file})], most
    detailed first. records: [(band, name, source, {layer: file})] (mesh/cells.py), band as "band5", source the
    chart input the cell was read from. A cell with no skin-of-the-earth area is not a chart that speaks and is
    left out."""
    from multiprocessing import get_context
    out = []
    with get_context("fork").Pool(workers) as pool:
        for band, name, source, files, b in pool.imap(cell_bbox, records, chunksize=8):
            if band not in BANDS:
                raise RuntimeError(f"cell {name} ({source}): {band!r} is not a chart band")
            if b is None:
                continue
            if b[2] - b[0] > 180.0 or b[0] < -180.0 or b[2] > 180.0:
                # NOAA ends its cells on the date line (checked in its catalogue, November 2026: no cell of any
                # district has a polygon across it). A cell written across it, or with longitudes beyond 180,
                # is not something this build reads correctly, so it stops rather than mesh it wrongly.
                raise RuntimeError(f"cell {name} ({source}, {band}): its charted areas run from longitude {b[0]} to {b[2]}; "
                                   f"a cell across the date line or beyond 180 degrees is not read")
            sub = 0
            if band in RESCHEMED and name[3:6] not in RESCHEMED[band]:
                sub = 1                              # legacy cell of band 1 or 2: loses to a new-scheme cell of its band
            out.append((BANDS.index(band) * 2 + sub, band, name, source, b, files))
    out.sort(key=lambda c: c[:3])
    return out


def tiles_reached(cells):
    """The tiles (i, j) the cells reach, sorted: the tiles the build makes."""
    tiles = set()
    for c in cells:
        x0, y0, x1, y1 = c[4]
        # A cell that ends on the date line at 180 reaches the last column, not a column beyond it.
        for i in range(int((x0 * U7 - ORIGIN_X) // TILE), min(int((x1 * U7 - ORIGIN_X) // TILE), N_COLS - 1) + 1):
            for j in range(int((y0 * U7 - ORIGIN_Y) // TILE), int((y1 * U7 - ORIGIN_Y) // TILE) + 1):
                tiles.add((i, j))
    return sorted(tiles)


def rules(states_active, shapes):
    """One row of COLS per state, from the shapes of the winning chart the state is inside (None: no chart
    speaks there). Depth: the shallowest depth area (DRVAL1); a dredged area's depth replaces the depth areas';
    an obstruction area with a depth (VALSOU) lowers it; of two dredged areas, or two obstruction areas, the
    shallower counts. Clearance: the lowest overhead feature's. Fairways, dredged areas and recommended tracks
    are navigable (is_nav)."""
    ns = len(states_active)
    A = {c: np.zeros(ns) for c in COLS}
    A["depth"][:] = -999.0; A["clear"][:] = -999.0; A["hazv"][:] = 1e9; A["nochart"][:] = 1
    for a, ids in enumerate(states_active):
        if ids is None:
            continue
        A["nochart"][a] = 0
        A["nowater"][a] = 0 if any(shapes[sid][0] in WATER for sid in ids) else 1
        dep_min, drg, obs = {}, {}, {}
        for sid in ids:
            L, pr, rank, order = shapes[sid]
            if L in LAND:
                A["land"][a] = 1
            elif L in HAZ:
                v = num(pr.get("VALSOU"))
                A["haz"][a] = 1
                A["hazv"][a] = min(A["hazv"][a], -1e9 if v is None else v)
                if v is not None:
                    obs[rank] = min(obs.get(rank, v), v)          # two obstruction areas: the shallower
            elif L == "DEPARE":
                v = num(pr.get("DRVAL1"))
                if v is not None:
                    dep_min[rank] = min(dep_min.get(rank, v), v)
            elif L == "DRGARE":
                A["is_nav"][a] = 1
                v = num(pr.get("DRVAL1"))
                if v is not None:
                    drg[rank] = min(drg.get(rank, v), v)          # two dredged areas: the shallower
                    A["dredged"][a] = 1
            elif L in CLEAR:
                v, opening = clearance_of(L, pr)
                if opening:
                    A["opening"][a] = 1
                if v is not None:
                    A["clear"][a] = v if A["clear"][a] == -999.0 else min(A["clear"][a], v)
            elif L == "FAIRWY":
                A["fair"][a] = 1; A["is_nav"][a] = 1
            elif L == "RECTRC":
                A["is_nav"][a] = 1
            if L in STRUCT:
                A["struct"][a] = 1
        vals = []
        for r_ in set(dep_min) | set(drg) | set(obs):
            d_ = drg[r_] if r_ in drg else dep_min.get(r_)
            cand = [x for x in (d_, obs.get(r_)) if x is not None]
            if cand:
                vals.append(min(cand))
        if vals:
            A["depth"][a] = min(vals)
    return np.column_stack([A[c] for c in COLS])


CELLS = None
CELL_NO = None
OUT = None


def process_tile(ij):
    i, j = ij
    w = ORIGIN_X + i * TILE; s = ORIGIN_Y + j * TILE; e = w + TILE; n = s + TILE
    west, south, east, north = w / U7, s / U7, e / U7, n / U7
    st = {"i": i, "j": j, "ok": False, "water": False}
    T_ = {}
    try:
        t0 = time.perf_counter()
        # The cells that reach the tile, each with the turn it is read at (dx). Longitude goes round: a cell that
        # ends on the date line at 180 reaches the first column's west edge a turn back (at -180), and a cell that
        # starts on it at -180 reaches the last column's east edge a turn on. So the two tiles that share the date
        # line are given the same shapes on it and cut that edge alike, as two tiles do anywhere else.
        touch = []
        for c in CELLS:
            for dx in (0, TURN, -TURN):
                d = dx / U7
                if not (c[4][2] + d < west or c[4][0] + d > east or c[4][3] < south or c[4][1] > north):
                    touch.append((c, dx))
                    break
        shapes, rings = [], []          # shapes[sid] = (layer, props, rank, order); rings = (sid or -2 for a line/point, corners)
        ring_gid = []                   # per ring: its number, the same in every tile (cell, layer, feature, ring)
        line_of_ring = {}               # ring index -> (rank, side bits, clearance, hazard depth)
        points = {}                     # (x, y) -> [(rank, corner bits, value)]
        for ci, ((rank, band, name, source, bb, files), dx) in enumerate(touch):
            cell_no = CELL_NO[name]
            for L in LAYERS:
                if L not in files:
                    continue
                for fi, item in enumerate(layer_file(files[L])):
                    if item is None:
                        continue
                    g, pr = item
                    gb = shapely.bounds(g)
                    if gb[2] + dx / U7 < west or gb[0] + dx / U7 > east or gb[3] < south or gb[1] > north:
                        continue
                    tid = shapely.get_type_id(g)
                    if cell_no >= 1 << 15 or fi >= 1 << 20:
                        raise RuntimeError(f"numbering: cell number {cell_no} or feature number {fi} ({name} {L}) is beyond what a line number can hold")
                    base = (cell_no << 38) | (LAYER_NO[L] << 32) | (fi << 12)
                    if tid in (3, 6) and L in AREA_LAYERS:
                        order = {"OBSTRN": 0, "UWTROC": 1, "WRECKS": 2}.get(L, 0) * 1_000_000 + fi
                        ro = 0
                        for p in polys(units(g, dx)):
                            sid = len(shapes); shapes.append((L, pr, rank, order))
                            for r in [p.exterior, *p.interiors]:
                                rings.append((sid, np.ascontiguousarray(np.asarray(r.coords)[:, :2], dtype="<f8")))
                                if ro >= 1 << 12:
                                    raise RuntimeError(f"numbering: {name} {L} feature {fi} has more than {1 << 12} rings")
                                ring_gid.append(base | ro); ro += 1
                    elif tid in (1, 5) and L in LINE_LAYERS:
                        bits = 0; clr = -999.0; hz = 1e9
                        if L in STRUCT: bits |= S_STRUCT
                        if L in HAZ:
                            bits |= S_HAZ; v = num(pr.get("VALSOU")); hz = -1e9 if v is None else v
                        if L in CLEAR:
                            v, opening = clearance_of(L, pr)
                            bits |= S_OVERHEAD | (S_OPENING if opening else 0)
                            clr = -999.0 if v is None else v
                        if L == "RECTRC": bits |= S_TRACK
                        if L == "NAVLNE": bits |= S_NAVLINE
                        ro = 0
                        for ln in shapely.get_parts(units(g, dx)):
                            c = shapely.get_coordinates(ln)
                            if len(c) >= 2:
                                line_of_ring[len(rings)] = (rank, bits, clr, hz)
                                rings.append((-2, np.ascontiguousarray(c, dtype="<f8")))
                                if ro >= 1 << 12:
                                    raise RuntimeError(f"numbering: {name} {L} feature {fi} has more than {1 << 12} parts")
                                ring_gid.append(base | ro); ro += 1
                    elif tid in (0, 4) and L in POINT_LAYERS:
                        if L == "SOUNDG":
                            v = num(pr.get("DEPTH"))
                            if v is None:
                                continue
                            ent = (rank, C_SOUNDING, v)
                        elif L in HAZ:
                            v = num(pr.get("VALSOU"))
                            ent = (rank, C_HAZ, -1e9 if v is None else v)
                        else:
                            ent = (rank, C_MARK | (C_CHANMARK if L in CHANMARK else 0), 1e9)
                        for c in shapely.get_coordinates(units(g, dx)):
                            if w <= c[0] <= e and s <= c[1] <= n:
                                points.setdefault((float(c[0]), float(c[1])), []).append(ent)
        for xy in points:
            rings.append((-2, np.array([xy], dtype="<f8"))); ring_gid.append(-1)
        st.update(cells=len(touch), shapes=len(shapes), lines=len(line_of_ring), points=len(points))
        T_["1 read"] = time.perf_counter() - t0
        if not shapes:
            st["ok"] = True; st["T"] = T_
            return st
        # input segment number (as the cutter numbers them) -> its ring's number and its place in the ring; ring index
        seg_ring, seg_k, seg_ridx = [], [], []
        for ridx, (sid_, c_) in enumerate(rings):
            if len(c_) > 1:
                nz = np.flatnonzero(np.any(c_[1:] != c_[:-1], axis=1))
                if len(c_) > 1 << 22:
                    raise RuntimeError(f"numbering: a ring of {len(c_)} corners is beyond what a segment number can hold")
                seg_ring += [ring_gid[ridx]] * len(nz); seg_k += nz.tolist(); seg_ridx += [ridx] * len(nz)
        vx, hy = (w - ORIGIN_X) // TILE, (s - ORIGIN_Y) // TILE
        # the tile box's four sides, in the cutter's order: south, east, north, west. A meridian has one number
        # from both its tiles: the east side of the last column is the date line, the west side of the first.
        seg_ring += [-(20_000_000 + hy), -(10_000_000 + (vx + 1) % N_COLS), -(20_000_000 + hy + 1), -(10_000_000 + vx)]
        seg_k += [0, 0, 0, 0]; seg_ridx += [-1] * 4
        seg_ring = np.array(seg_ring, dtype=np.int64); seg_k = np.array(seg_k, dtype=np.int64); seg_ridx = np.array(seg_ridx, dtype=np.int64)
        fin, fout = f"{OUT}/cut_{i}_{j}.in", f"{OUT}/cut_{i}_{j}.out"
        with open(fin, "wb") as f:
            f.write(np.array([w, s, e, n], dtype="<f8").tobytes()); f.write(np.array([len(rings)], dtype="<i8").tobytes())
            for sid, c in rings:
                f.write(np.array([sid, len(c)], dtype="<i4").tobytes()); f.write(c.tobytes())
        t1 = time.perf_counter()
        r = subprocess.run([EXE, fin, fout], capture_output=True, text=True)
        T_["2 exact cut"] = time.perf_counter() - t1
        if r.returncode != 0:
            raise RuntimeError(f"exact cut failed ({r.returncode}): {r.stderr[-200:]}")
        with open(fout, "rb") as fh:
            raw = fh.read()
        os.remove(fin); os.remove(fout)
        nt, nv, nstate, nids, clash = (int(v) for v in np.frombuffer(raw, "<i8", 5, 0))
        if clash:
            raise RuntimeError(f"the cut's marking disagrees with itself in {clash} places")
        o = 40
        V = np.frombuffer(raw, "<f8", nv * 2, o).reshape(nv, 2); o += nv * 16
        T = np.frombuffer(raw, "<i4", nt * 3, o).reshape(nt, 3); o += nt * 12
        NB = np.frombuffer(raw, "<i4", nt * 3, o).reshape(nt, 3); o += nt * 12
        ST = np.frombuffer(raw, "<i4", nt, o); o += nt * 4
        OFF = np.frombuffer(raw, "<i8", nstate + 1, o); o += (nstate + 1) * 8
        IDS = np.frombuffer(raw, "<i4", nids, o); o += nids * 4
        LINE = np.frombuffer(raw, "<i4", nt * 3, o).reshape(nt, 3); o += nt * 12
        o += nt * 12                                                        # lowest chart line per side: not used
        VFLAG = np.frombuffer(raw, "i1", nv, o); o += nv
        nl = int(np.frombuffer(raw, "<i8", 1, o)[0]); o += 8
        LOFF = np.frombuffer(raw, "<i8", nt * 3 + 1, o); o += (nt * 3 + 1) * 8
        LIDS = np.frombuffer(raw, "<i4", nl, o)
        if seg_ring.shape[0] <= max(int(LINE.max()), int(LIDS.max()) if nl else -1):
            raise RuntimeError("segment numbering differs from the cutter's")
        t2 = time.perf_counter()
        # the most detailed chart wins, per state
        active = []
        top_of_state = np.full(nstate, 99, dtype=np.int64)
        overlap_states = np.zeros(nstate, bool)
        for k in range(nstate):
            ids = IDS[OFF[k]:OFF[k + 1]].tolist()
            skin = [shapes[sid][2] for sid in ids if shapes[sid][0] in SKIN]
            if not skin:
                active.append(None); continue
            top = min(skin); top_of_state[k] = top
            # The winner is decided by rank (band), not by cell: every shape of that rank counts here, also one
            # from a cell of the same rank that has no skin-of-the-earth area at this spot. A correctly made
            # chart has no feature outside its own coverage, so the two rules give the same mesh. Kept by rank
            # on the owner's decision of 2026-11-24 (PR #10 review); MAY NEED TO BE REVISITED: deciding by the
            # cells that actually supply the winning coverage is the stricter rule (and the same applies to the
            # lines and points below, which are also taken by rank).
            act = [sid for sid in ids if shapes[sid][2] == top]
            overlap_states[k] = sum(1 for sid in act if shapes[sid][0] in SKIN) > 1
            active.append(act)
        rows = rules(active, shapes)
        ukeys, kinv = np.unique(rows, axis=0, return_inverse=True)
        key = kinv.ravel()[ST]
        top_t = top_of_state[ST]
        P = V[T]
        area = np.abs((P[:, 1, 0] - P[:, 0, 0]) * (P[:, 2, 1] - P[:, 0, 1]) - (P[:, 2, 0] - P[:, 0, 0]) * (P[:, 1, 1] - P[:, 0, 1])) / 2
        NC = COLS.index("nochart")
        # not water: land, uncharted, and a charted area with no water area under it (a pontoon outside any depth area)
        hole = (ukeys[key][:, 0] != 0) | (ukeys[key][:, NC] != 0) | (ukeys[key][:, COLS.index("nowater")] != 0)
        # sides lying on a chart line of the winning chart there: their mark
        marks, mark_no = [], {}
        side_mark = np.full((nt, 3), -1, dtype=np.int64)
        has = np.flatnonzero(LOFF[1:] > LOFF[:-1])
        for q in has:
            t_, k_ = divmod(int(q), 3)
            u_ = NB[t_, k_]
            tops = {int(top_t[t_])} | ({int(top_t[u_])} if u_ >= 0 else set())
            bits = 0; clr = -999.0; hz = 1e9
            for sgi in LIDS[LOFF[q]:LOFF[q + 1]]:
                rank, b_, c_, h_ = line_of_ring[int(seg_ridx[sgi])]
                if rank in tops:
                    bits |= b_
                    if c_ != -999.0: clr = c_ if clr == -999.0 else min(clr, c_)
                    hz = min(hz, h_)
            if bits:
                mk = (bits, clr, hz)
                if mk not in mark_no:
                    mark_no[mk] = len(marks); marks.append(mk)
                side_mark[t_, k_] = mark_no[mk]
        # both triangles of a side carry the same mark
        tt, kk = np.nonzero(NB >= 0); nb = NB[tt, kk]
        back = np.argmax(NB[nb] == tt[:, None], axis=1)
        if not np.array_equal(side_mark[tt, kk], side_mark[nb, back]):
            raise RuntimeError("a side's mark differs between its two triangles")
        on_line = side_mark >= 0
        same = (key[tt] == key[nb]) & ~on_line[tt, kk]
        ncomp, comp = connected_components(coo_matrix((np.ones(int(same.sum()), bool), (tt[same], nb[same])), shape=(nt, nt)), directed=False)
        order = np.argsort(comp, kind="stable")
        groups = np.split(order, np.flatnonzero(np.diff(comp[order])) + 1)
        # labels: the water attribute rows used in this tile
        water_keys = np.unique(key[~hole])
        label_of_key = np.full(len(ukeys), -1, dtype=np.int64); label_of_key[water_keys] = np.arange(len(water_keys))
        lab = label_of_key[key]                                   # per triangle: label, -1 = not water
        part_of = np.full(nt, -1, dtype=np.int64)
        n_parts = n_holes = 0
        for members in groups:
            if hole[members[0]]:
                n_holes += 1
            else:
                part_of[members] = n_parts; n_parts += 1
        T_["3 mark+parts"] = time.perf_counter() - t2
        t3 = time.perf_counter()
        # outline pieces: a side between two parts, each side once
        pa, pb, pg, pk, pla, plb, pmk = [], [], [], [], [], [], []
        n_inside = 0
        for k in range(3):
            a_, b_ = T[:, (k + 1) % 3], T[:, (k + 2) % 3]
            other = np.where(NB[:, k] >= 0, part_of[np.maximum(NB[:, k], 0)], -1)
            edge = (part_of >= 0) & (other != part_of) & ((other < 0) | (part_of < other))
            # A chart line lying inside one water part (a pier, a cable or a track in open water) separates
            # nothing, so its sides are between a part and itself. They are outline pieces all the same, with
            # the same water on both sides: the line stays triangle sides in the finished mesh.
            inside = (part_of >= 0) & (other == part_of) & (side_mark[:, k] >= 0) & (np.arange(nt) < NB[:, k])
            n_inside += int(inside.sum())
            m = np.flatnonzero(edge | inside)
            olab = np.where(NB[m, k] >= 0, lab[np.maximum(NB[m, k], 0)], -1)
            # outside the tile box (no neighbour triangle in this tile) is -2: the next tile says what is there
            olab = np.where(NB[m, k] < 0, -2, olab)
            pa.append(V[a_[m]]); pb.append(V[b_[m]]); pg.append(seg_ring[LINE[m, k]]); pk.append(seg_k[LINE[m, k]])
            pla.append(lab[m]); plb.append(olab); pmk.append(side_mark[m, k])
        pa = np.vstack(pa); pb = np.vstack(pb); pg = np.concatenate(pg); pk = np.concatenate(pk)
        pla = np.concatenate(pla); plb = np.concatenate(plb); pmk = np.concatenate(pmk)
        # chart points that are corners in water, of the winning chart there
        water = part_of >= 0
        vt = {}
        is_pt = VFLAG >= 0
        for t_ in np.flatnonzero(water & is_pt[T].any(axis=1)):
            for v_ in T[t_]:
                if is_pt[v_]:
                    vt.setdefault(int(v_), []).append(int(t_))
        cpts = []
        for v_, ts in vt.items():
            ents = points.get((float(V[v_, 0]), float(V[v_, 1])))
            if not ents:
                raise RuntimeError("a chart point's corner is not at the point's own position")
            tops = {int(top_t[t_]) for t_ in ts}
            bits = 0; val = 1e9
            for rank, b_, v in ents:
                if rank not in tops:
                    continue
                if b_ & C_SOUNDING:
                    deps = [ukeys[key[t_]][COLS.index("depth")] for t_ in ts if top_t[t_] == rank]
                    if not any(d_ == -999.0 or v < d_ for d_ in deps):
                        continue
                bits |= b_
                if b_ & (C_HAZ | C_SOUNDING):
                    val = min(val, v)
            if bits:
                cpts.append([V[v_, 0], V[v_, 1], bits, val])
        kx = 111320.0 * math.cos(math.radians((south + north) / 2)) / U7; ky = 110570.0 / U7
        m2 = kx * ky
        st.update(water=bool(water.any()), parts=n_parts, holes=n_holes, cut_tris=int(nt), pieces=int(len(pa)),
                  line_sides=int((pmk >= 0).sum()), line_sides_inside_a_part=n_inside, corner_points=len(cpts),
                  water_m2=float(area[water].sum() * m2), land_m2=float(area[ukeys[key][:, 0] != 0].sum() * m2),
                  nochart_m2=float(area[ukeys[key][:, NC] != 0].sum() * m2),
                  nodepth_m2=float(area[water & (ukeys[key][:, COLS.index("depth")] == -999.0)].sum() * m2),
                  same_rank_overlap_m2=float(area[overlap_states[ST]].sum() * m2))
        np.savez_compressed(f"{OUT}/tile_{i}_{j}.npz", pa=pa, pb=pb, pg=pg, pk=pk, pla=pla, plb=plb, pmk=pmk,
                            labels=ukeys[water_keys], marks=np.array(marks, dtype=np.float64).reshape(-1, 3),
                            cpts=np.array(cpts, dtype=np.float64).reshape(-1, 4), CV=V, CT=T, CL=lab.astype(np.int32),
                            CLAND=(ukeys[key][:, 0] != 0))
        T_["4 outline"] = time.perf_counter() - t3
        st["ok"] = True
    except Exception as ex:
        import traceback
        st["error"] = f"{type(ex).__name__}: {ex}"[:400]; st["tb"] = traceback.format_exc()[-1500:]
    st["T"] = T_
    st["rss"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return st


def run(work, workers, cells, log=print):
    """Build every tile the cells reach into <work>/tiles, from the cells given (cell_index's rows, one per
    cell); returns 0, or 1 when a tile failed. <work> is the run's own folder."""
    global CELLS, OUT, CELL_NO
    OUT = f"{work}/tiles"; os.makedirs(OUT)
    CELLS = sorted(cells, key=lambda c: c[:3])
    names = [c[2] for c in CELLS]
    if len(set(names)) != len(names):
        raise RuntimeError("a cell is given twice: " + ", ".join(sorted({n for n in names if names.count(n) > 1})))
    CELL_NO = {n: k for k, n in enumerate(sorted(names))}
    with open(f"{work}/{CELL_INDEX}", "w") as fh:
        json.dump(CELLS, fh)
    log(f"cells: {len(CELLS)} live with charted areas; by band: "
          + ", ".join(f"{b} {sum(1 for c in CELLS if c[1] == b)}" for b in BANDS))
    tiles = tiles_reached(CELLS)
    log(f"tiles the cells reach: {len(tiles)}")
    from multiprocessing import get_context
    t1 = time.time(); res = []
    with get_context("fork").Pool(workers) as pool:
        for k, st in enumerate(pool.imap_unordered(process_tile, tiles, chunksize=1)):
            res.append(st)
            if (k + 1) % 200 == 0:
                log(f"{k + 1}/{len(tiles)} tiles, {time.time() - t1:.0f} s")
    with open(f"{work}/tiles.json", "w") as fh:
        json.dump({"results": res, "seconds": time.time() - t1}, fh)
    bad = [r for r in res if not r["ok"]]
    ok = [r for r in res if r["ok"] and r.get("shapes")]
    log(f"tile stage: {len(res)} tiles in {time.time() - t1:.0f} s; FAILED {len(bad)}")
    for r in bad[:20]:
        log(f"   FAILED: {r['i']} {r['j']} {r.get('error')}"); log(str(r.get("tb")))
    wt = [r for r in ok if r["water"]]
    log(f"tiles with chart shapes {len(ok)}; with water {len(wt)}")
    if ok:
        log(f"water {sum(r['water_m2'] for r in ok) / 1e6:,.0f} km2, of it with no depth {sum(r['nodepth_m2'] for r in ok) / 1e6:,.1f} km2; "
              f"land {sum(r['land_m2'] for r in ok) / 1e6:,.0f} km2; inside the tiles but uncharted {sum(r['nochart_m2'] for r in ok) / 1e6:,.0f} km2")
        log(f"area where two cells of the same rank both have charted areas: {sum(r['same_rank_overlap_m2'] for r in ok) / 1e6:,.3f} km2")
        log(f"water parts {sum(r['parts'] for r in ok):,}; holes {sum(r['holes'] for r in ok):,}; outline pieces {sum(r['pieces'] for r in ok):,}; "
              f"sides on chart lines {sum(r['line_sides'] for r in ok):,}, of them inside one water part "
              f"{sum(r['line_sides_inside_a_part'] for r in ok):,}; chart points kept as corners {sum(r['corner_points'] for r in ok):,}")
        log(f"processor-seconds: {sum(sum(r['T'].values()) for r in ok):.0f}, of it reading {sum(r['T'].get('1 read', 0) for r in ok):.0f}, exact cut {sum(r['T'].get('2 exact cut', 0) for r in ok):.0f}")
    return 1 if bad else 0
