"""The mesh build, second stage: join the tiles, make the triangles well-shaped, mark sides and corners, compute
the shore multiplier, check the result against the chart, write the mesh. Run by mesh/cli.py in its own
interpreter: python -m mesh.finalize <work dir> <out mesh dir> <workers> <name>

  join      Two tiles' records of a side on their shared edge are one side with a label on each side; it stays
            only where the two labels differ or a chart line lies on it. Then, once for the whole mesh: a corner
            where exactly two outline pieces of the same input segment meet, with the same water on both sides
            and the same mark, is not a corner. No corner is moved or matched by distance anywhere.
  shape     Triangle ("pq20"), with one east-west scale for the mesh (cos of its middle latitude); that scale
            only affects how "well-shaped" is judged, never where anything is. Chart points are corners.
            Triangle is given the outline and the chart points only: no point per water part and no point per
            hole. It triangulates everything the outline encloses, land included; each triangle is then
            looked up in the cut by its centre (exactly) and takes that cut triangle's label, and the ones
            that are not water are dropped. (Until 2026-11-27 Triangle was given a point per water part and a
            point per hole; a hole point outside the hull of the corners made Triangle crash.)
  marks     Each triangle side lying on a chart line carries that line's mark; each corner that is a chart point
            carries the point's mark.
  shore     The shore multiplier: on a 0.00025-degree lattice (origin -75.5, 38.7), 1.0 at 2 km or more from
            the chart's land (as cut) rising to 1.3 at the shore, except in narrow water (no lattice cell of
            the 2 km window around the spot is 1 km or more from land), where it stays 1.0.
  checks    All stop the build: a triangle whose centre lies in a tile that was cut but in none of its cut
            triangles, or in cut triangles of different labels; outline pieces joined into one segment that
            disagree about the water on each side; a clockwise or zero-area triangle; a neighbour link that is
            not mutual; a side shared by more than two triangles; a one-sided side that is not on an outline; a
            triangle whose water is not what the outline pieces recorded on its side of an outline segment, or
            differs from the water across a side on no outline (this fixes every triangle's water from the
            outline record, independently of the lookup the labels were taken from); a chart point that is not
            a corner; a chart line with no side on it; water area differing from the cut's.
"""
import glob, json, math, os, struct, sys, time
from fractions import Fraction as Fr
import numpy as np, shapely
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.ndimage import maximum_filter, distance_transform_edt
from . import MESH_FORMAT_MAGIC, MESH_INDEX_VERSION
from . import build as CT

work, outdir, workers, name = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
U7, TILE = CT.U7, CT.TILE
COLS = CT.COLS
t_start = time.time()


def log(m=""):
    print(m, flush=True)


def fail(msg):
    log("BUILD FAILED: " + msg)
    sys.exit(2)


# ---------------------------------------------------------------- load the tiles
files = sorted(glob.glob(f"{work}/tiles/tile_*.npz"))
found = []
for f in files:
    i, j = (int(v) for v in os.path.basename(f)[5:-4].split("_"))
    found.append((i, j, f))
if not found:
    fail("no tile with chart shapes")
# The mesh's run of longitude: the shortest run of tile columns, going east, that holds its tiles. When that
# run crosses the date line the columns east of it are counted on (column + N_COLS) and everything in them is
# worked on one turn further east (x + TURN), so the mesh is in one piece here. What is written at the end is
# the router's convention (store.ts): longitudes between -180 and 180, the index's west greater than its east,
# columns counted east from its west, and the date line an edge no triangle lies across.
WEST_COL, RUN_COLS = CT.shortest_run(t[0] for t in found)
CROSSES = WEST_COL + RUN_COLS > CT.N_COLS
if RUN_COLS == CT.N_COLS:
    fail("the mesh's tiles go all the way round the earth: it has no west edge to count from")
SHIFT = {}                                   # tile file -> what is added to its x (0, or one turn)
tiles = []
for i, j, f in found:
    east_of_line = CROSSES and i < WEST_COL
    SHIFT[f] = float(CT.TURN) if east_of_line else 0.0
    tiles.append((i + CT.N_COLS if east_of_line else i, j, f))
tiles.sort()


def shifted(a, f):
    """An array of positions of tile file f with the tile's turn added to its x (a copy when one is added)."""
    if SHIFT[f] == 0.0 or not len(a):
        return a
    a = a.copy(); a[:, 0] += SHIFT[f]
    return a


tile_set = {(t[0], t[1]): t[2] for t in tiles}          # tile -> its file
tile_no = {(t[0], t[1]): n_ for n_, t in enumerate(tiles)}


def tile_number(x, origin):
    """The tile a position falls in, decided against the tile's own edges (whole numbers): a centre a hair west
    of an edge is in the tile west of it, though the division alone rounds it onto the edge."""
    t = np.floor((x - origin) / TILE).astype(np.int64)
    t -= x < origin + t * TILE
    t += x >= origin + (t + 1) * TILE
    return t


def orient_exact(ax, ay, bx, by, px, py):
    return (Fr(bx) - Fr(ax)) * (Fr(py) - Fr(ay)) - (Fr(by) - Fr(ay)) * (Fr(px) - Fr(ax))


def in_closed(A_, B_, C_, Pp):
    """Per row: is point Pp in the closed triangle A_ B_ C_? Decided in floating point where the rounding
    cannot change the sign, and exactly (fractions) where it could."""
    def orient(a, b, p):
        d = (b[:, 0] - a[:, 0]) * (p[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (p[:, 0] - a[:, 0])
        err = 4e-16 * (np.abs((b[:, 0] - a[:, 0]) * (p[:, 1] - a[:, 1])) + np.abs((b[:, 1] - a[:, 1]) * (p[:, 0] - a[:, 0])))
        return d, np.abs(d) <= err
    d1, u1 = orient(A_, B_, Pp); d2, u2 = orient(B_, C_, Pp); d3, u3 = orient(C_, A_, Pp)
    inside = ((d1 >= 0) & (d2 >= 0) & (d3 >= 0)) | ((d1 <= 0) & (d2 <= 0) & (d3 <= 0))
    for n_ in np.flatnonzero(u1 | u2 | u3):
        s1 = orient_exact(A_[n_, 0], A_[n_, 1], B_[n_, 0], B_[n_, 1], Pp[n_, 0], Pp[n_, 1])
        s2 = orient_exact(B_[n_, 0], B_[n_, 1], C_[n_, 0], C_[n_, 1], Pp[n_, 0], Pp[n_, 1])
        s3 = orient_exact(C_[n_, 0], C_[n_, 1], A_[n_, 0], A_[n_, 1], Pp[n_, 0], Pp[n_, 1])
        inside[n_] = (s1 >= 0 and s2 >= 0 and s3 >= 0) or (s1 <= 0 and s2 <= 0 and s3 <= 0)
    return inside


lab_rows, mark_rows, lab_off, mark_off = [], [], [], []
PA, PB, PG, PK, PLA, PLB, PMK = [], [], [], [], [], [], []
CPTS = []
nl = nm = 0
cut_water_units = 0.0
for i, j, f in tiles:
    z = np.load(f)
    lab_off.append(nl); mark_off.append(nm)
    lab_rows.append(z["labels"]); mark_rows.append(z["marks"])
    la = z["pla"].astype(np.int64) + nl
    lb = z["plb"].astype(np.int64); lb = np.where(lb >= 0, lb + nl, lb)
    mk = z["pmk"].astype(np.int64); mk = np.where(mk >= 0, mk + nm, mk)
    PA.append(shifted(z["pa"], f)); PB.append(shifted(z["pb"], f)); PG.append(z["pg"]); PK.append(z["pk"])
    PLA.append(la); PLB.append(lb); PMK.append(mk)
    CPTS.append(shifted(z["cpts"], f))
    nl += len(z["labels"]); nm += len(z["marks"])
    cv, ct, cl = z["CV"], z["CT"], z["CL"]
    p = cv[ct[cl >= 0]]
    cut_water_units += float(np.abs((p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1]) - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1])).sum() / 2)
labels_all = np.vstack(lab_rows)
LAB, lab_inv = np.unique(labels_all, axis=0, return_inverse=True); lab_inv = lab_inv.ravel()
marks_all = np.vstack(mark_rows) if nm else np.empty((0, 3))
MARK, mark_inv = (np.unique(marks_all, axis=0, return_inverse=True) if nm else (np.empty((0, 3)), np.empty(0, np.int64)))
mark_inv = np.asarray(mark_inv).ravel()
PA = np.vstack(PA); PB = np.vstack(PB); PG = np.concatenate(PG); PK = np.concatenate(PK)
PLA = lab_inv[np.concatenate(PLA)]
plb = np.concatenate(PLB); PLB = np.where(plb >= 0, lab_inv[np.maximum(plb, 0)], plb)
pmk = np.concatenate(PMK); PMK = np.where(pmk >= 0, mark_inv[np.maximum(pmk, 0)] if nm else -1, -1)
CPTS = np.vstack(CPTS)
log(f"tiles with chart shapes {len(tiles)}; outline pieces {len(PA):,}; water labels {len(LAB):,}; side marks {len(MARK):,}; "
    f"chart points {len(CPTS):,}")

# ---------------------------------------------------------------- join
t0 = time.time()
XY, inv = np.unique(np.vstack([PA, PB]), axis=0, return_inverse=True); inv = inv.ravel()
va, vb = inv[:len(PA)], inv[len(PA):]
if (va == vb).any():
    fail(f"{int((va == vb).sum())} outline pieces have both ends at the same number")
lo, hi = np.minimum(va, vb), np.maximum(va, vb)
key = lo * len(XY) + hi
order = np.argsort(key, kind="stable"); ks = key[order]
start = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]]); cnt = np.diff(np.r_[start, len(ks)])
if (cnt > 2).any():
    fail(f"{int((cnt > 2).sum())} sides are recorded more than twice")
first = order[start]
second = np.where(cnt == 2, order[np.minimum(start + 1, len(order) - 1)], -1)
two = second >= 0
if not ((PLB[first[two]] == -2) & (PLB[second[two]] == -2)).all():
    fail("a side recorded twice is not a tile-edge side in both tiles")
# A chart line lying on a tile edge is marked by each tile from the charts that win on its own side of the edge
# (it cannot see the other side). Where the winning charts differ across the edge the two records differ, and
# the side's mark is what a side inside a tile gets from the charts that win on both its sides: the two
# records together (the lines of either, the lower clearance, the shallower hazard).
_d = np.flatnonzero(PMK[first[two]] != PMK[second[two]])
n_edge_marks_joined = len(_d)
if n_edge_marks_joined:
    _none = np.array([0.0, -999.0, 1e9])

    def _rows(m):
        return np.where((m >= 0)[:, None], MARK[np.maximum(m, 0)] if len(MARK) else _none, _none)
    _a, _b = _rows(PMK[first[two]][_d]), _rows(PMK[second[two]][_d])
    _clr = np.where(_a[:, 1] == -999.0, _b[:, 1], np.where(_b[:, 1] == -999.0, _a[:, 1], np.minimum(_a[:, 1], _b[:, 1])))
    _both = np.column_stack([(_a[:, 0].astype(np.int64) | _b[:, 0].astype(np.int64)).astype(np.float64), _clr,
                             np.minimum(_a[:, 2], _b[:, 2])])
    _n_old = len(MARK)
    MARK, _inv = np.unique(np.vstack([MARK, _both]) if _n_old else _both, axis=0, return_inverse=True)
    _inv = np.asarray(_inv).ravel()
    PMK = np.where(PMK >= 0, _inv[np.maximum(PMK, 0)] if _n_old else -1, -1)
    _joined = _inv[_n_old:]
    _f2, _s2 = first[two][_d], second[two][_d]
    PMK[_f2] = _joined; PMK[_s2] = _joined
s_va, s_vb = va[first], vb[first]
s_g, s_k, s_mk = PG[first], PK[first], PMK[first]
s_la = PLA[first]
s_lb = np.where(two, PLA[np.maximum(second, 0)], np.where(PLB[first] == -2, -1, PLB[first]))
edge_single = int(((~two) & (PLB[first] == -2)).sum())
# A tile-edge side recorded by one tile only must not overlap one recorded only by the tile across that edge:
# that would mean the two tiles cut their shared edge differently.
_sg = np.flatnonzero((~two) & (PLB[first] == -2))
_a, _b = XY[va[first[_sg]]], XY[vb[first[_sg]]]
_vert = _a[:, 0] == _b[:, 0]
if not (_vert | (_a[:, 1] == _b[:, 1])).all():
    fail("a tile-edge side is neither vertical nor horizontal")
_line = np.where(_vert, _a[:, 0], _a[:, 1]); _kind = _vert.astype(np.int64)
_lo = np.where(_vert, np.minimum(_a[:, 1], _b[:, 1]), np.minimum(_a[:, 0], _b[:, 0]))
_hi = np.where(_vert, np.maximum(_a[:, 1], _b[:, 1]), np.maximum(_a[:, 0], _b[:, 0]))
_o = np.lexsort((_lo, _line, _kind))
_n_overl = 0; _where = None; _reach = None; _cur = None
for _q in _o:                                  # along each tile edge in turn, in order of position
    _ln = (int(_kind[_q]), float(_line[_q]))
    if _ln != _cur:
        _cur, _reach = _ln, -np.inf
    if _lo[_q] < _reach:
        _n_overl += 1
        if _where is None:
            _where = _a[_q] / U7
    _reach = max(_reach, _hi[_q])
if _n_overl:
    fail(f"{_n_overl} tile-edge sides overlap another: two tiles cut their shared edge differently; the first near {_where}")
# The date line stays an edge of the mesh also where the same water lies on both sides of it: the router reads
# a tile's corners as plain numbers, 180 west of the line and -180 east of it, so no triangle may lie across it.
on_date_line = CROSSES & (XY[s_va, 0] == CT.DATE_LINE) & (XY[s_vb, 0] == CT.DATE_LINE)
keep = ~(two & (s_la == s_lb) & (s_mk < 0) & ~on_date_line)
n_date_line = int((two & on_date_line).sum())
n_seam_dropped = int((~keep).sum())
s_va, s_vb, s_g, s_k, s_mk, s_la, s_lb = (a[keep] for a in (s_va, s_vb, s_g, s_k, s_mk, s_la, s_lb))
ne = len(s_va)
lmin, lmax = np.minimum(s_la, s_lb), np.maximum(s_la, s_lb)
incv = np.concatenate([s_va, s_vb]); eid = np.concatenate([np.arange(ne), np.arange(ne)])
o_ = np.argsort(incv, kind="stable"); iv, ie = incv[o_], eid[o_]
st_ = np.flatnonzero(np.r_[True, iv[1:] != iv[:-1]]); c_ = np.diff(np.r_[st_, len(iv)])
tw = st_[c_ == 2]
e1, e2 = ie[tw], ie[tw + 1]
okp = (s_g[e1] == s_g[e2]) & (s_k[e1] == s_k[e2]) & (lmin[e1] == lmin[e2]) & (lmax[e1] == lmax[e2]) & (s_mk[e1] == s_mk[e2])
e1, e2 = e1[okp], e2[okp]
nch, ch = connected_components(coo_matrix((np.ones(len(e1), bool), (e1, e2)), shape=(ne, ne)), directed=False)
cv_ = np.column_stack([np.concatenate([ch, ch]), np.concatenate([s_va, s_vb])])
ucv, cc = np.unique(cv_, axis=0, return_counts=True)
ends = ucv[cc % 2 == 1]
if len(ends) != 2 * nch:
    fail(f"outline chains: {len(ends)} ends for {nch} chains")
ends = ends[np.argsort(ends[:, 0], kind="stable")]
SEG = np.column_stack([ends[0::2, 1], ends[1::2, 1]])
fp = np.zeros(nch, dtype=np.int64); fp[ch[::-1]] = np.arange(ne)[::-1]
SEG_MK = s_mk[fp]
# The water on each side of an outline segment, as its pieces recorded it: each piece runs from s_va to s_vb with
# s_la on its left and s_lb on its right. A segment's left and right are those of its first piece, in that piece's
# direction; every piece joined into it must say the same, turned round where it runs the other way (the pieces of
# a segment lie on one line, and their ends are whole numbers, so the direction test is exact).
_dir = XY[s_vb] - XY[s_va]
_way = (_dir * _dir[fp[ch]]).sum(axis=1)
if (_way == 0).any():
    fail(f"{int((_way == 0).sum())} outline pieces are not along the line of the segment they are joined into")
_l, _r = np.where(_way > 0, s_la, s_lb), np.where(_way > 0, s_lb, s_la)
SEG_LEFT, SEG_RIGHT = s_la[fp], s_lb[fp]
SEG_DIR = _dir[fp]
if ((_l != SEG_LEFT[ch]) | (_r != SEG_RIGHT[ch])).any():
    _b0 = int(np.flatnonzero((_l != SEG_LEFT[ch]) | (_r != SEG_RIGHT[ch]))[0])
    fail(f"{int(((_l != SEG_LEFT[ch]) | (_r != SEG_RIGHT[ch])).sum()):,} outline pieces disagree with the segment they are "
         f"joined into about the water on each side; the first near {XY[s_va[_b0]] / U7}")
log(f"join: tile-edge sides between the same water, dropped {n_seam_dropped:,}; tile-edge sides with nothing recorded on the other side {edge_single:,}; "
    f"corners that are not corners, removed {len(e1):,}; outline segments {nch:,}; {time.time() - t0:.0f} s"
    + (f"; chart-line marks on tile edges joined from two differing records {n_edge_marks_joined:,}" if n_edge_marks_joined else "")
    + (f"; the mesh crosses the date line: {n_date_line:,} sides on it joined from both tiles and kept" if CROSSES else ""))

# ---------------------------------------------------------------- shape
i0 = min(t[0] for t in tiles); i1 = max(t[0] for t in tiles) + 1
j0 = min(t[1] for t in tiles); j1 = max(t[1] for t in tiles) + 1
WEST, SOUTH = (CT.ORIGIN_X + i0 * TILE) / U7, (CT.ORIGIN_Y + j0 * TILE) / U7
EAST, NORTH = (CT.ORIGIN_X + i1 * TILE) / U7, (CT.ORIGIN_Y + j1 * TILE) / U7
K = math.cos(math.radians((SOUTH + NORTH) / 2))
used = np.unique(SEG)
remap = np.full(len(XY), -1, dtype=np.int64); remap[used] = np.arange(len(used))
VIN = XY[used]
S = remap[SEG]
# chart points: corners of their own unless already an outline corner
cp_xy = CPTS[:, :2]
vin_set = {tuple(p) for p in VIN.tolist()}
extra = np.array([p for p in np.unique(cp_xy, axis=0).tolist() if tuple(p) not in vin_set]).reshape(-1, 2)
VIN = np.vstack([VIN, extra])
n_in = len(VIN)
sc = np.array([K, 1.0])
# Triangle works on the corners laid out about the mesh's middle (P). The chart's own corners are never taken
# back from that frame: they are carried as their whole numbers (VIN), and only the points Triangle adds come
# from its arithmetic.
C = (VIN.min(0) + VIN.max(0)) / 2 * sc
P = VIN * sc - C
LINE_X = CT.DATE_LINE * sc[0]                # the date line in the mesh's x, and (LINE_P) as Triangle sees it
LINE_P = CT.DATE_LINE * sc[0] - C[0]
seg_on_line = CROSSES & (VIN[S[:, 0], 0] == CT.DATE_LINE) & (VIN[S[:, 1], 0] == CT.DATE_LINE)
tin = {"vertices": P, "segments": S, "segment_markers": (np.arange(len(S)) + 2).reshape(-1, 1)}
log("well-shaping: Triangle is given the outline and the chart points only (no water-part points, no hole points)")
log(f"mesh box lon {WEST} to {EAST}, lat {SOUTH} to {NORTH}; east-west scale {K:.9f}; corners in {n_in:,}; segments {len(S):,}")
t0 = time.time()
pid = os.fork()
if pid == 0:
    import triangle
    try:
        res = triangle.triangulate(tin, "pq20")
        np.save(f"{work}/tri_v.npy", res["vertices"]); np.save(f"{work}/tri_t.npy", res["triangles"])
        np.save(f"{work}/tri_s.npy", res["segments"])
        np.save(f"{work}/tri_m.npy", res["segment_markers"])
        os._exit(0)
    except BaseException as ex:
        sys.stderr.write(f"well-shaping: {type(ex).__name__}: {ex}\n")
        os._exit(1)
_, status = os.waitpid(pid, 0)
if status != 0:
    fail(f"the well-shaping step stopped (status {status})")
VR = np.load(f"{work}/tri_v.npy")
T = np.load(f"{work}/tri_t.npy").astype(np.int64)
TA = None
SS = np.load(f"{work}/tri_s.npy").astype(np.int64); SM = np.load(f"{work}/tri_m.npy").ravel().astype(np.int64) - 2
for f in ("tri_v", "tri_t", "tri_s", "tri_m"):
    os.remove(f"{work}/{f}.npy")
nT = len(T)
log(f"well-shaping: {nT:,} triangles, {len(VR):,} corners in {time.time() - t0:.0f} s")
if len(VR) < n_in or not np.array_equal(VR[:n_in], P):
    fail("the well-shaping step did not hand back the input corners first, in order and unchanged")
V = VR + C
V[:n_in] = VIN * sc                          # the chart's own corners, from their whole numbers
if CROSSES:
    # A point Triangle adds on the date line has the line's own x in Triangle's frame (it lies on a vertical
    # segment between two corners that have it); it is given the line's own x here, not that x plus the middle.
    V[VR[:, 0] == LINE_P, 0] = LINE_X
    on_line_v = SS[(SM >= 0) & seg_on_line[np.maximum(SM, 0)]].ravel()
    if (V[on_line_v, 0] != LINE_X).any():
        fail(f"{int((V[on_line_v, 0] != LINE_X).sum())} corners of sides on the date line are not on it")
# ---- label every triangle from the cut by its centre, and keep the water
_t0 = time.time()
_cen = V[T].mean(axis=1) / sc
_ti = tile_number(_cen[:, 0], CT.ORIGIN_X); _tj = tile_number(_cen[:, 1], CT.ORIGIN_Y)
_key = _ti * 100000 + _tj
_o = np.argsort(_key, kind="stable")
_b = np.flatnonzero(np.r_[True, _key[_o][1:] != _key[_o][:-1]])
_groups = {(int(_ti[_o[s_]]), int(_tj[_o[s_]])): _o[s_:e_] for s_, e_ in zip(_b, np.r_[_b[1:], len(_o)])}


def _label_job(k_):
    """Per triangle filed under tile k_: the cut's label at its centre (-1 not water; -2 in a tile that was
    cut, but in none of its cut triangles; -3 in cut triangles of different labels; -4 in a tile no chart
    reaches, which has no cut: not charted, so not in the mesh)."""
    g = _groups[k_]
    if k_ not in tile_set:
        return k_, np.full(len(g), -4, dtype=np.int64)
    z = np.load(tile_set[k_])
    cv, ct, cl = shifted(z["CV"], tile_set[k_]), z["CT"], z["CL"]
    want = lab_inv[lab_off[tile_no[k_]] + np.maximum(cl, 0)]
    want = np.where(cl >= 0, want, -1)
    pts = _cen[g]
    pi, tq = shapely.STRtree(shapely.polygons(cv[ct])).query(shapely.points(pts), predicate="intersects")
    inside = in_closed(cv[ct[tq, 0]], cv[ct[tq, 1]], cv[ct[tq, 2]], pts[pi])
    lab = np.full(len(g), -2, dtype=np.int64)
    for i_, w_ in zip(pi[inside].tolist(), want[tq[inside]].tolist()):
        lab[i_] = w_ if lab[i_] in (-2, w_) else -3
    return k_, lab


TA_ALL = np.full(len(T), -2, dtype=np.int64)
from multiprocessing import get_context as _gc
with _gc("fork").Pool(workers) as _pool:
    for k_, lab_ in _pool.imap_unordered(_label_job, sorted(_groups), chunksize=1):
        TA_ALL[_groups[k_]] = lab_
_keep = TA_ALL >= 0
log(f"labels: Triangle drew {len(T):,} triangles; looked up in the cut: water {int(_keep.sum()):,}, not water {int((TA_ALL == -1).sum()):,}, "
    f"in a tile no chart reaches {int((TA_ALL == -4).sum()):,}, in a cut tile but in no cut triangle {int((TA_ALL == -2).sum()):,}, "
    f"between two labels {int((TA_ALL == -3).sum()):,}; {time.time() - _t0:.0f} s")
if (TA_ALL == -3).any():
    fail("a triangle's centre lies in cut triangles of different labels")
if (TA_ALL == -2).any():
    _b0 = int(np.flatnonzero(TA_ALL == -2)[0])
    fail(f"{int((TA_ALL == -2).sum()):,} triangles have their centre in a tile that was cut but in none of its cut triangles; "
         f"the first at lat {_cen[_b0, 1] / U7:.7f}, lon {_cen[_b0, 0] / U7:.7f}")
T = T[_keep]; TA = TA_ALL[_keep]; nT = len(T)
if CROSSES:
    tx = V[T][:, :, 0]
    if ((tx.min(axis=1) < LINE_X) & (tx.max(axis=1) > LINE_X)).any():
        bad = np.flatnonzero((tx.min(axis=1) < LINE_X) & (tx.max(axis=1) > LINE_X))
        c = V[T[bad[0]]].mean(axis=0)
        fail(f"{len(bad):,} triangles lie across the date line; the first at lat {c[1] / U7:.7f}")
a_, b_, c_ = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
cr = (b_[:, 0] - a_[:, 0]) * (c_[:, 1] - a_[:, 1]) - (b_[:, 1] - a_[:, 1]) * (c_[:, 0] - a_[:, 0])
if (cr <= 0).any():
    bad = np.flatnonzero(cr <= 0)
    c = V[T[bad[0]]].mean(axis=0)
    fail(f"{int((cr < 0).sum())} clockwise and {int((cr == 0).sum())} zero-area triangles; the first at lat {c[1] / U7:.7f}, lon {c[0] / K / U7:.7f}")
mesh_water_units = float(cr.sum() / 2 / K)

# neighbours (side k of a triangle joins its corners k and k+1)
t0 = time.time()
E = np.sort(np.vstack([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]]), axis=1)
ekey = E[:, 0] * len(V) + E[:, 1]
del E
tri_id = np.concatenate([np.arange(nT)] * 3); edge_k = np.repeat(np.arange(3), nT)
oe = np.argsort(ekey, kind="stable"); es = ekey[oe]
same = es[1:] == es[:-1]
if len(same) > 1 and (same[:-1] & same[1:]).any():
    fail("a side is shared by more than two triangles")
ia, ib = oe[:-1][same], oe[1:][same]
NB = np.full((nT, 3), -1, dtype=np.int64)
NB[tri_id[ia], edge_k[ia]] = tri_id[ib]; NB[tri_id[ib], edge_k[ib]] = tri_id[ia]
one_sided = np.ones(len(es), bool); one_sided[1:][same] = False; one_sided[:-1][same] = False
skey = np.sort(SS, axis=1); skey = skey[:, 0] * len(V) + skey[:, 1]
so = np.argsort(skey); skey_s = skey[so]; smark_s = SM[so]
bnd = es[one_sided]
pos = np.searchsorted(skey_s, bnd)
if not (skey_s[np.minimum(pos, len(skey_s) - 1)] == bnd).all():
    fail(f"{int((skey_s[np.minimum(pos, len(skey_s) - 1)] != bnd).sum())} one-sided sides are not on an outline (cracks)")
# side marks: the mark of the outline segment a side lies on
pos = np.searchsorted(skey_s, ekey)
pos_c = np.minimum(pos, len(skey_s) - 1)
on_seg = skey_s[pos_c] == ekey
seg_of_side = np.where(on_seg, smark_s[pos_c], -1)
if (seg_of_side[on_seg] < 0).any():
    fail("a side lies on an outline piece the well-shaping step did not attribute to an input segment")
side_mark = np.where(seg_of_side >= 0, SEG_MK[np.maximum(seg_of_side, 0)], -1).reshape(3, nT).T      # (nT, 3)
log(f"neighbours and side marks: {time.time() - t0:.0f} s; sides on a chart line {int((side_mark >= 0).sum()):,}")
# ---- every triangle's water against the outline's own record of the water on each side (not the cut lookup the
# labels came from). Side k of a triangle runs from its corner k to its corner k + 1 with the triangle on its left
# (all triangles are anticlockwise, checked above). A side on an outline segment: the triangle's water is the
# segment's water on the triangle's side, and where no triangle lies across, the segment's water on the far side is
# not water. A side on no outline segment: the same water on both sides. Every triangle is joined to the outline
# through sides of the second kind, so this fixes every triangle's water from the outline record alone.
t0 = time.time()
_q = np.flatnonzero(seg_of_side >= 0)
_t, _k, _m = tri_id[_q], edge_k[_q], seg_of_side[_q]
_side = V[T[_t, (_k + 1) % 3]] - V[T[_t, _k]]
_along = (_side * (SEG_DIR[_m] * sc)).sum(axis=1)
if (_along == 0).any():
    fail(f"{int((_along == 0).sum())} triangle sides on an outline segment are not along it")
_own = np.where(_along > 0, SEG_LEFT[_m], SEG_RIGHT[_m])
_far = np.where(_along > 0, SEG_RIGHT[_m], SEG_LEFT[_m])
_bad = (TA[_t] != _own) | ((NB[_t, _k] < 0) & (_far >= 0))
_q2 = np.flatnonzero(seg_of_side < 0)
_t2, _k2 = tri_id[_q2], edge_k[_q2]
_bad2 = TA[_t2] != TA[NB[_t2, _k2]]                 # one-sided sides off the outline have failed above (cracks)
for _n, _what, _tt in ((int(_bad.sum()), "not the water the outline records on their side", _t[_bad]),
                       (int(_bad2.sum()), "not the same water as the triangle across a side that is on no outline", _t2[_bad2])):
    if _n:
        c = V[T[_tt[0]]].mean(axis=0)
        fail(f"{_n:,} triangle sides: the triangle's water is {_what}; the first at lat {c[1] / U7:.7f}, lon {c[0] / K / U7:.7f}")
log(f"water against the outline record: {len(_q):,} triangle sides on the outline, {len(_q2):,} off it; all agree; "
    f"{time.time() - t0:.0f} s")
marked_segs = np.flatnonzero(SEG_MK >= 0)
seen_seg = np.zeros(len(SEG_MK), bool); seen_seg[seg_of_side[seg_of_side >= 0]] = True
if not seen_seg[marked_segs].all():
    fail(f"{int((~seen_seg[marked_segs]).sum())} chart-line segments have no triangle side on them")
# corner marks
cp_bits = {}; cp_val = {}
for x, y, b, v in CPTS.tolist():
    k_ = (x, y)
    cp_bits[k_] = cp_bits.get(k_, 0) | int(b); cp_val[k_] = min(cp_val.get(k_, 1e9), v)
vbits = np.zeros(len(V), dtype=np.uint8); vval = np.full(len(V), 1e9, dtype=np.float32)
used_v = np.zeros(len(V), bool); used_v[T.ravel()] = True
missing = 0
idx_of = {tuple(p): k for k, p in enumerate(VIN.tolist())}
for k_, b in cp_bits.items():
    vi = idx_of[k_]
    if not used_v[vi]:
        missing += 1
    vbits[vi] = b; vval[vi] = cp_val[k_]
if missing:
    fail(f"{missing} chart points are not corners of any triangle")
log(f"chart points that are corners: {len(cp_bits):,}; area: mesh {mesh_water_units:.6e}, cut {cut_water_units:.6e} (units squared), difference {abs(mesh_water_units - cut_water_units) / cut_water_units:.3e}")
if abs(mesh_water_units - cut_water_units) > 1e-9 * cut_water_units:
    fail("the mesh's water area differs from the cut's")

# which tile each triangle is filed under: the tile holding its centre
cen = V[T].mean(axis=1)
cen_u = cen / sc
ti = tile_number(cen_u[:, 0], CT.ORIGIN_X)
tj = tile_number(cen_u[:, 1], CT.ORIGIN_Y)
tkey = ti * 100000 + tj
ot = np.argsort(tkey, kind="stable")
tb_ = np.flatnonzero(np.r_[True, tkey[ot][1:] != tkey[ot][:-1]])
groups = {(int(ti[ot[s_]]), int(tj[ot[s_]])): ot[s_:e_] for s_, e_ in zip(tb_, np.r_[tb_[1:], len(ot)])}
missing_t = [k_ for k_ in groups if k_ not in tile_set]
if missing_t:
    fail(f"triangles lie in {len(missing_t)} tiles that have no cut: {missing_t[:5]}")


# ---------------------------------------------------------------- the shore multiplier, per tile
def tile_job(k_):
    """For one tile: the shore multiplier of every triangle filed under it."""
    i, j = k_
    g = groups[k_]
    z = np.load(tile_set[k_])
    pts = cen_u[g]
    # ---- shore multiplier (land = chart land as cut, from this tile and its neighbours)
    PG_W, PG_S, PG_RES = -75.5, 38.7, 0.00025
    m3 = 0.03
    lon_w = (CT.ORIGIN_X + i * TILE) / U7; lon_e = lon_w + 0.25; s = (CT.ORIGIN_Y + j * TILE) / U7; n = s + 0.25
    land = []
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            f = tile_set.get((i + di, j + dj))
            if f is None:
                continue
            zz = z if (di, dj) == (0, 0) else np.load(f)
            lt = zz["CT"][zz["CLAND"]]
            if len(lt):
                tri = shifted(zz["CV"], f)[lt] / U7
                b = tri.reshape(-1, 6)
                near = (b[:, 0::2].max(1) >= lon_w - m3) & (b[:, 0::2].min(1) <= lon_e + m3) & (b[:, 1::2].max(1) >= s - m3) & (b[:, 1::2].min(1) <= n + m3)
                land.append(tri[near])
    land = np.vstack(land) if land else np.empty((0, 3, 2))
    j0_ = int(math.floor((lon_w - m3 - PG_W) / PG_RES)); j1_ = int(math.ceil((lon_e + m3 - PG_W) / PG_RES))
    i0_ = int(math.floor((s - m3 - PG_S) / PG_RES)); i1_ = int(math.ceil((n + m3 - PG_S) / PG_RES))
    if len(land):
        clon = PG_W + (np.arange(j0_, j1_) + 0.5) * PG_RES; clat = PG_S + (np.arange(i0_, i1_) + 0.5) * PG_RES
        GX, GY = np.meshgrid(clon, clat)
        lt_ = shapely.STRtree(shapely.polygons(land))
        hit = lt_.query(shapely.points(np.column_stack([GX.ravel(), GY.ravel()])), predicate="intersects")
        is_land = np.zeros(GX.size, bool); is_land[np.unique(hit[0])] = True
        is_land = is_land.reshape(GX.shape)
        cell_m = PG_RES * (6_371_008.8 * math.pi / 180.0)
        dist_m = distance_transform_edt(~is_land).astype(np.float32) * cell_m
        mx = maximum_filter(dist_m, size=max(3, int(2000.0 / cell_m)))
        shore = np.ones(GX.shape, dtype=np.float32)
        pen = (dist_m < 2000.0) & ~(mx < 1000.0) & ~is_land
        shore[pen] = 1.0 + 0.3 * (1.0 - dist_m[pen] / 2000.0)
        gj = np.clip(np.floor((pts[:, 0] / U7 - PG_W) / PG_RES).astype(np.int64) - j0_, 0, shore.shape[1] - 1)
        gi = np.clip(np.floor((pts[:, 1] / U7 - PG_S) / PG_RES).astype(np.int64) - i0_, 0, shore.shape[0] - 1)
        sm = shore[gi, gj]
    else:
        sm = np.ones(len(g), dtype=np.float32)
    return k_, sm


t0 = time.time()
SHORE = np.ones(nT, dtype=np.float32)
from multiprocessing import get_context
with get_context("fork").Pool(workers) as pool:
    for k_, sm in pool.imap_unordered(tile_job, sorted(groups), chunksize=1):
        SHORE[groups[k_]] = sm
log(f"shore multiplier computed; {time.time() - t0:.0f} s")
if ((SHORE < 1.0) | (SHORE > 1.3 + 1e-6)).any():
    fail("shore multiplier outside 1.0 to 1.3")

# ---------------------------------------------------------------- per-triangle values and the mesh files
rows = LAB[TA]
depth = rows[:, COLS.index("depth")].astype(np.float32)
clear = rows[:, COLS.index("clear")].astype(np.float32)
hazv = rows[:, COLS.index("hazv")].astype(np.float32)
dmult = np.ones(nT, dtype=np.float32)
kn = depth != -999.0
dmult[kn & (depth < 2.0)] = 2.0; dmult[kn & (depth >= 2.0) & (depth < 5.0)] = 1.5; dmult[kn & (depth >= 5.0) & (depth < 10.0)] = 1.2
flags = ((rows[:, COLS.index("is_nav")] > 0).astype(np.uint8) | ((rows[:, COLS.index("haz")] > 0).astype(np.uint8) << 1)
         | ((rows[:, COLS.index("struct")] > 0).astype(np.uint8) << 4) | ((rows[:, COLS.index("fair")] > 0).astype(np.uint8) << 5)
         | ((rows[:, COLS.index("dredged")] > 0).astype(np.uint8) << 6) | ((rows[:, COLS.index("opening")] > 0).astype(np.uint8) << 7)).astype(np.uint8)
sm_ = np.maximum(side_mark, 0)
side_bits = np.where(side_mark >= 0, MARK[sm_, 0] if len(MARK) else 0, 0).astype(np.uint8)
side_clear = np.where(side_mark >= 0, MARK[sm_, 1] if len(MARK) else -999.0, -999.0).astype(np.float32)
side_hazv = np.where(side_mark >= 0, MARK[sm_, 2] if len(MARK) else 1e9, 1e9).astype(np.float32)
corner_bits = vbits[T]; corner_val = vval[T]
os.makedirs(outdir, exist_ok=True)
order_t = sorted(groups)
new_id = np.full(nT, -1, dtype=np.int64)
firsts = {}
f0 = 0
for k_ in order_t:
    g = groups[k_]
    new_id[g] = np.arange(f0, f0 + len(g)); firsts[k_] = f0; f0 += len(g)
REV = np.full((nT, 3), -1, dtype=np.int8)
tt, kk = np.nonzero(NB >= 0)
back = np.argmax(NB[NB[tt, kk]] == tt[:, None], axis=1)
if not (NB[NB[tt, kk], back] == tt).all():
    fail("a neighbour link is not mutual")
REV[tt, kk] = back
index = []
nbytes = 0
Vdeg = V / U7                                              # x = lon * K, y = lat, in degrees
# A tile east of the date line is written with its longitudes from -180: one turn off its x. The turn is twice
# the line's own x, so a corner on the line is exactly 180 x K in the tile west of it and -180 x K east of it.
Vdeg_east = Vdeg.copy(); Vdeg_east[:, 0] = (V[:, 0] - 2 * LINE_X) / U7
for k_ in order_t:
    g = groups[k_]
    n_ = len(g)
    fname = f"mesh_{k_[0] - i0:03d}_{k_[1] - j0:03d}.bin"
    east_of_line = k_[0] >= CT.N_COLS
    corners = (Vdeg_east if east_of_line else Vdeg)[T[g]]
    origin_lon = (CT.ORIGIN_X + (k_[0] - CT.N_COLS if east_of_line else k_[0]) * TILE) / U7
    nbs = np.where(NB[g] >= 0, new_id[np.maximum(NB[g], 0)], -1).astype(np.int32)
    with open(f"{outdir}/{fname}", "wb") as fh:
        fh.write(MESH_FORMAT_MAGIC.encode() + struct.pack("<I", n_) + struct.pack("<dd", origin_lon, SOUTH + (k_[1] - j0) * 0.25) + b"\0" * 4)
        fh.write(corners.reshape(n_ * 6).astype("<f8").tobytes())
        # every multi-byte field little-endian, whatever the machine that builds
        fh.write(nbs.reshape(n_ * 3).astype("<i4").tobytes())
        fh.write((SHORE[g] * dmult[g]).astype("<f4").tobytes())
        fh.write(depth[g].astype("<f4").tobytes()); fh.write(clear[g].astype("<f4").tobytes()); fh.write(hazv[g].astype("<f4").tobytes())
        fh.write(REV[g].reshape(n_ * 3).tobytes()); fh.write(flags[g].tobytes())
        fh.write(side_bits[g].reshape(n_ * 3).tobytes())
        fh.write(side_clear[g].reshape(n_ * 3).astype("<f4").tobytes()); fh.write(side_hazv[g].reshape(n_ * 3).astype("<f4").tobytes())
        fh.write(corner_bits[g].reshape(n_ * 3).tobytes()); fh.write(corner_val[g].reshape(n_ * 3).astype("<f4").tobytes())
    nbytes += os.path.getsize(f"{outdir}/{fname}")
    lo_ = corners.reshape(-1, 2).min(axis=0); hi_ = corners.reshape(-1, 2).max(axis=0)
    bw, be = float(lo_[0] / K), float(hi_[0] / K)
    if CROSSES:
        # No triangle lies across the date line (checked above), so a tile's corners end on it at the latest:
        # its extent is stated as that, not as the line's x divided back by the scale.
        if east_of_line:
            bw = max(bw, -180.0)
        else:
            be = min(be, 180.0)
    index.append({"i": k_[0] - i0, "j": k_[1] - j0, "file": fname, "first": int(firsts[k_]), "n": int(n_),
                  "bbox": [bw, float(lo_[1]), be, float(hi_[1])]})


def ang(p, q, r):
    u, v = q - p, r - p
    return np.degrees(np.arccos(np.clip((u * v).sum(1) / np.hypot(*u.T) / np.hypot(*v.T), -1, 1)))
mn = np.minimum(np.minimum(ang(a_, b_, c_), ang(b_, c_, a_)), ang(c_, a_, b_))
thin = int((mn < 20.0).sum())
# The index's box: its west greater than its east when the mesh crosses the date line (store.ts).
json.dump({"version": MESH_INDEX_VERSION, "format": MESH_FORMAT_MAGIC, "name": name, "west": WEST, "south": SOUTH,
           "east": EAST - 360.0 if EAST > 180.0 else EAST, "north": NORTH, "tileDeg": 0.25,
           "xScale": K, "triangles": int(nT), "tiles": index,
           "fields": ["corners f64[6]", "neighbours i32[3]", "mult f32", "depth f32", "clear f32", "hazv f32", "rev i8[3]", "flags u8",
                      "sideFlags u8[3]", "sideClear f32[3]", "sideHazv f32[3]", "cornerFlags u8[3]", "cornerVal f32[3]"],
           "sideFlagBits": {"structure": 1, "hazard": 2, "overhead": 4, "opening": 8, "recommendedTrack": 16, "navigationLine": 32},
           "cornerFlagBits": {"hazard": 1, "mark": 2, "channelMark": 4, "sounding": 8}}, _index_file := open(f"{outdir}/index.json", "w"))
_index_file.close()
log(f"\n==== {name}: {nT:,} triangles in {len(index)} tiles, {nbytes / 1e6:.0f} MB; under 20 degrees {thin:,} ({thin / nT * 100:.3f} %), not a gate; "
    f"second stage {time.time() - t_start:.0f} s")
log("every check passed")
