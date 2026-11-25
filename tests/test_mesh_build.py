"""build-mesh.py on hand-made chart cells: what the mesh must say, read
back from the mesh files. Needs the mesh packages and a C++ compiler
with CGAL (the exact cutter); skips without.

The cells (degrees; the tile edge at -71.5 runs through the water), each folder a hand-over folder with its
cells.json:

  own folder
    US4XX1AA  band 4  depth area 10 m, -71.6..-71.4 x 41.05..41.20, with a
                      hole that is land (-71.58..-71.56 x 41.10..41.12);
                      an obstruction area of 2.5 m; two overlapping
                      dredged areas of 7 m and 5 m; a shoreline
                      construction line; a fixed bridge line of 12.5 m;
                      a rock of 1.2 m, a lateral buoy, a light; soundings
                      of 4 m and 25 m; and a rock inside the band 5 cell
    US5XX1AB  band 5  depth area 3 m, -71.52..-71.48 x 41.08..41.12
                      (across the tile edge)
    US5XX9ZZ  band 5  its file is in the folder and on no list
  another district's folder
    US5YY1AA  band 5  depth area 1.5 m inside the own band 4 water
    US5YY1AB  band 5  far away, in a tile the own cells do not reach

What is pinned:
  * the format: WRPMESH2 tiles of exactly the documented length, index
    version 2, the sidecar's fields, the archive;
  * only charted water is in the mesh: its area is the band 4 depth area
    less the land, and nothing lies outside it;
  * the most detailed chart wins, whichever folder it is in: 3 m and 1.5 m
    inside the band 5 cells, 10 m beside them; the band 4 rock inside the
    band 5 cell is not in the mesh;
  * of two dredged areas the shallower; an obstruction area lowers the
    depth and is a hazard with its depth;
  * a chart line is triangle sides, marked, over its whole length and
    nowhere else; a chart point is a triangle corner at its own position,
    marked with what it is; a sounding is kept only where it is shallower
    than the depth area it lies in;
  * a file that is in the folder and not on the list is not read, and the far cell is not in the mesh;
  * neighbour links are mutual and the multiplier is in range.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import helpers as h

try:
    import numpy as np
    import scipy, shapely, triangle  # noqa: F401
    HAVE_MESH_DEPS = True
except ImportError:
    HAVE_MESH_DEPS = False

BOX = (-71.6, 41.05, -71.4, 41.2)
ISLAND = (-71.58, 41.10, -71.56, 41.12)
FINE = (-71.52, 41.08, -71.48, 41.12)
OTHER_FINE = (-71.60, 41.06, -71.59, 41.07)
OBSTRUCTION = (-71.47, 41.13, -71.46, 41.14)
DREDGED_7 = (-71.59, 41.16, -71.57, 41.18)
DREDGED_5 = (-71.58, 41.16, -71.56, 41.18)
PONTOON = (-71.40, 41.06, -71.39, 41.07)      # against the depth area's east edge, outside it: no water under it
QUAY = ((-71.45, 41.06), (-71.45, 41.10))
BRIDGE = ((-71.41, 41.06), (-71.41, 41.10))
ROCK = (-71.44, 41.15)
BUOY = (-71.43, 41.15)
LIGHT = (-71.42, 41.15)
SOUNDING_SHALLOW = (-71.44, 41.17)
SOUNDING_DEEP = (-71.43, 41.17)
ROCK_UNDER_FINER_CHART = (-71.50, 41.10)
# sideFlags and cornerFlags bits (index.json sideFlagBits, cornerFlagBits)
S_STRUCTURE, S_OVERHEAD, S_OPENING = 1, 4, 8
C_HAZARD, C_MARK, C_CHANNEL_MARK, C_SOUNDING = 1, 2, 4, 8
# flags bits
F_NAVIGABLE, F_HAZARD, F_DREDGED = 1, 2, 64


def ring(b):
    w, s, e, n = b
    return [[w, s], [e, s], [e, n], [w, n], [w, s]]


def area(b, props, hole=None):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [ring(b)] + ([ring(hole)] if hole else [])}}


def line(ends, props):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "LineString", "coordinates": [list(ends[0]), list(ends[1])]}}


def point(xy, props):
    return {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": list(xy)}}


def write_cells(base):
    own, other = h.CellsFolder(Path(base) / "own"), h.CellsFolder(Path(base) / "other")
    own.add("band4", "US4XX1AA", {
        "DEPARE": [area(BOX, {"DRVAL1": 10.0, "DRVAL2": 20.0}, hole=ISLAND)],
        "LNDARE": [area(ISLAND, {})],
        "OBSTRN": [area(OBSTRUCTION, {"VALSOU": 2.5})],
        "DRGARE": [area(DREDGED_7, {"DRVAL1": 7.0}), area(DREDGED_5, {"DRVAL1": 5.0})],
        "PONTON": [area(PONTOON, {})],
        "SLCONS": [line(QUAY, {})],
        "BRIDGE": [line(BRIDGE, {"CATBRG": "1", "VERCLR": "12.5"})],
        "UWTROC": [point(ROCK, {"VALSOU": 1.2}), point(ROCK_UNDER_FINER_CHART, {"VALSOU": 0.5})],
        "BOYLAT": [point(BUOY, {})],
        "LIGHTS": [point(LIGHT, {})],
        "SOUNDG": [point(SOUNDING_SHALLOW, {"DEPTH": 4.0}), point(SOUNDING_DEEP, {"DEPTH": 25.0})],
    })
    own.add("band5", "US5XX1AB", {"DEPARE": [area(FINE, {"DRVAL1": 3.0})]})
    own.add("band5", "US5XX9ZZ", {"DEPARE": [area((-71.30, 41.05, -71.25, 41.10), {"DRVAL1": 0.5})]}, listed=False)
    other.add("band5", "US5YY1AA", {"DEPARE": [area(OTHER_FINE, {"DRVAL1": 1.5})]})
    other.add("band5", "US5YY1AB", {"DEPARE": [area((-70.05, 41.05, -70.0, 41.10), {"DRVAL1": 0.5})]})
    return own.write(cancelled=["US5XX8ZZ"]), other.write()


def centre(b):
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


@unittest.skipUnless(HAVE_MESH_DEPS and h.HAVE_CGAL, "needs the mesh packages and a C++ compiler with CGAL")
class HandMadeCells(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="s57meshbuild-")
        base = Path(cls.tmp.name)
        own, other = write_cells(base)
        (base / "chart.json").write_text(json.dumps({"build_date": "2026-11-19T02:11:00Z"}))
        cls.out = base / "out"
        cls.work = base / "work"
        # From another folder than the repository's: every path the build is given must work from anywhere.
        cls.proc = subprocess.run(
            [sys.executable, str(h.ROOT / "build-mesh.py"), own, other, "--name", "HAND", "-o", "out",
             "--work", "work", "-j", "2", "--chart-json", "chart.json"],
            cwd=base, capture_output=True, text=True)
        if cls.proc.returncode != 0:
            raise AssertionError(f"build-mesh failed ({cls.proc.returncode}):\n"
                                 + cls.proc.stdout[-4000:] + cls.proc.stderr[-4000:])
        cls.m = h.read_mesh(cls.out / "HAND_mesh")
        cls.side = json.loads((cls.out / "HAND_mesh.json").read_text())
        cls.own, cls.other = own, other

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def at(self, lon, lat):
        t = h.mesh_triangle_at(self.m, lon, lat)
        self.assertIsNotNone(t, f"no triangle at {lon}, {lat}")
        return t

    def depth_at(self, lon, lat):
        return float(self.m["depth"][self.at(lon, lat)])

    # -- format ------------------------------------------------------------

    def test_every_check_passed(self):
        self.assertIn("every check passed", self.proc.stdout)

    def test_index_and_tiles(self):
        idx = self.m["index"]             # read_mesh has checked each tile's magic, count, origin and length
        self.assertEqual((idx["version"], idx["format"], idx["tileDeg"]), (2, "WRPMESH2", 0.25))
        self.assertEqual(len(idx["tiles"]), 2, "the water lies in two tiles")
        self.assertEqual(idx["triangles"], sum(t["n"] for t in idx["tiles"]))
        self.assertEqual(idx["triangles"], len(self.m["depth"]))
        self.assertEqual((idx["west"], idx["south"], idx["east"], idx["north"]), (-71.75, 41.0, -71.25, 41.25))
        self.assertEqual(idx["sideFlagBits"]["structure"], S_STRUCTURE)
        self.assertEqual(idx["cornerFlagBits"]["sounding"], C_SOUNDING)

    def test_sidecar(self):
        s = self.side
        self.assertEqual(s["name"], "HAND")
        self.assertEqual(s["format"], {"magic": "WRPMESH2", "index_version": 2, "tile_deg": 0.25, "multi_cluster": False})
        self.assertEqual(s["chart_build_date"], "2026-11-19T02:11:00Z")
        self.assertTrue(s["passed"])
        self.assertEqual(s["triangles"], self.m["index"]["triangles"])
        self.assertEqual(s["mesh_tiles"], 2)
        self.assertEqual(s["bytes"], sum((self.out / "HAND_mesh" / t["file"]).stat().st_size
                                         for t in self.m["index"]["tiles"]))
        self.assertEqual(s["clusters"], [{"dir": ".", "box": [-71.75, 41.0, -71.25, 41.25],
                                          "x_scale": self.m["index"]["xScale"], "triangles": s["triangles"],
                                          "mesh_tiles": 2}])
        own, other = s["cells"]
        self.assertEqual((own["role"], own["cells"], own["cells_by_band"]), ("own", 2, {"band4": 1, "band5": 1}))
        self.assertEqual((own["cells_listed"], own["list_written"], own["inputs"]), (2, "2026-11-24T00:00:00Z", ["TEST_ENCs.zip"]))
        self.assertEqual(own["cancelled_cells_left_out"], ["US5XX8ZZ"], "as the folder's list records them")
        self.assertEqual((other["role"], other["cells"]), ("other district", 2))

    def test_the_catalogue_takes_the_sidecar(self):
        mc = h.mesh_catalog()
        e = mc.entry_from_sidecar(self.side, "https://example.test/charts")
        self.assertEqual(mc.validate_entry(e), [])

    def test_intermediates(self):
        self.assertFalse((self.work / "tiles").exists(), "the cut is removed after a build that passed")
        self.assertTrue((self.work / "tiles.json").exists())
        self.assertFalse((self.out / "HAND_mesh.tar.zst").exists(), "no archive unless asked for")

    @unittest.skipUnless(shutil.which("tar") and shutil.which("zstd"), "needs tar and zstd")
    def test_archive(self):
        # a build of its own: the other tests must not depend on tar and zstd being installed
        with tempfile.TemporaryDirectory(prefix="s57mesharchive-") as tmp:
            p = subprocess.run(
                [sys.executable, str(h.ROOT / "build-mesh.py"), self.own, "--name", "ARC", "-o", str(Path(tmp) / "out"),
                 "--work", str(Path(tmp) / "work"), "-j", "2", "--archive"], cwd=h.ROOT, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
            listing = subprocess.run(["tar", "--zstd", "-tf", str(Path(tmp) / "out" / "ARC_mesh.tar.zst")],
                                     capture_output=True, text=True)
            self.assertEqual(listing.returncode, 0, listing.stderr)
            self.assertIn("ARC_mesh/index.json", listing.stdout)

    def test_no_folder_given_may_be_inside_a_folder_the_build_removes(self):
        # the build removes <work>/tiles and <out>/<name>_mesh; every path is resolved first, so a link is judged
        # by where it points
        with tempfile.TemporaryDirectory(prefix="s57meshpaths-") as tmp:
            tmp = Path(tmp)
            w, o = tmp / "w", tmp / "o"
            (w / "tiles").mkdir(parents=True)
            (o / "X_mesh").mkdir(parents=True)
            (tmp / "elsewhere").mkdir()
            link_out = tmp / "link-into-tiles"
            link_out.symlink_to(w / "tiles", target_is_directory=True)
            cells_in_tiles = w / "tiles" / "cells"
            shutil.copytree(self.own, cells_in_tiles)
            cells_in_mesh = o / "X_mesh" / "cells"
            shutil.copytree(self.own, cells_in_mesh)
            link_cells = tmp / "link-to-cells-in-tiles"
            link_cells.symlink_to(cells_in_tiles, target_is_directory=True)
            refused = [
                ("the output directory", [self.own, "--work", str(w), "-o", str(w / "tiles" / "out")]),
                ("the output directory", [self.own, "--work", str(w), "-o", str(link_out)]),
                ("the cells folder", [str(cells_in_tiles), "--work", str(w), "-o", str(o)]),
                ("the cells folder", [str(cells_in_mesh), "--work", str(w), "-o", str(o)]),
                ("the cells folder", [str(link_cells), "--work", str(w), "-o", str(o)]),
                ("the work directory", [self.own, "--work", str(o / "X_mesh" / "w"), "-o", str(o)]),
            ]
            for what, args in refused:
                p = subprocess.run([sys.executable, str(h.ROOT / "build-mesh.py"), *args, "--name", "X"],
                                   cwd=h.ROOT, capture_output=True, text=True)
                self.assertEqual(p.returncode, 2, f"{args}: " + p.stdout[-500:] + p.stderr[-500:])
                self.assertIn(what, p.stderr, args)
                self.assertIn("the build removes that folder", p.stderr, args)
            for kept in (cells_in_tiles, cells_in_mesh):
                self.assertTrue((kept / "cells.json").exists(), "nothing was removed")

    def test_paths_given_through_links_are_used_where_they_point(self):
        # the output and the cells are symbolic links that sit inside <work>/tiles and point outside it: the
        # build empties <work>/tiles (the links go with it) and still reads the cells and writes the mesh
        with tempfile.TemporaryDirectory(prefix="s57meshlink-") as tmp:
            tmp = Path(tmp)
            real_out = tmp / "real-out"
            real_out.mkdir()
            (tmp / "w" / "tiles").mkdir(parents=True)
            link_out = tmp / "w" / "tiles" / "out"
            link_out.symlink_to(real_out, target_is_directory=True)
            link_cells = tmp / "w" / "tiles" / "cells"
            link_cells.symlink_to(self.own, target_is_directory=True)
            p = subprocess.run(
                [sys.executable, str(h.ROOT / "build-mesh.py"), str(link_cells), "--name", "LINK", "--work", str(tmp / "w"),
                 "-o", str(link_out), "-j", "2"], cwd=h.ROOT, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
            self.assertTrue((real_out / "LINK_mesh" / "index.json").exists())
            self.assertTrue((real_out / "LINK_mesh.json").exists())

    # -- what is water -----------------------------------------------------

    def test_only_charted_water(self):
        p = self.m["lonlat"]
        a, b, c = p[:, 0], p[:, 1], p[:, 2]
        tri = np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])) / 2
        charted = (BOX[2] - BOX[0]) * (BOX[3] - BOX[1]) - (ISLAND[2] - ISLAND[0]) * (ISLAND[3] - ISLAND[1])
        self.assertAlmostEqual(float(tri.sum()), charted, places=9)
        lon, lat = p[..., 0], p[..., 1]
        self.assertGreaterEqual(lon.min(), BOX[0] - 1e-9); self.assertLessEqual(lon.max(), BOX[2] + 1e-9)
        self.assertGreaterEqual(lat.min(), BOX[1] - 1e-9); self.assertLessEqual(lat.max(), BOX[3] + 1e-9)
        self.assertIsNone(h.mesh_triangle_at(self.m, *centre(ISLAND)), "land is not in the mesh")
        self.assertIsNone(h.mesh_triangle_at(self.m, *centre(PONTOON)), "a pontoon with no water area under it is not water")

    def test_unlisted_and_far_cells_are_not_in_the_mesh(self):
        self.assertTrue((Path(self.own) / "band5" / "DEPARE_US5XX9ZZ.geojson").exists(), "the file is in the folder")
        self.assertIsNone(h.mesh_triangle_at(self.m, -71.275, 41.075), "and, being on no list, was not read")
        self.assertIsNone(h.mesh_triangle_at(self.m, -70.025, 41.075))

    # -- depths ------------------------------------------------------------

    def test_most_detailed_chart_wins(self):
        self.assertEqual(self.depth_at(-71.55, 41.07), 10.0)
        self.assertEqual(self.depth_at(*centre(FINE)), 3.0)
        self.assertEqual(self.depth_at(-71.51, 41.09), 3.0, "west of the tile edge")
        self.assertEqual(self.depth_at(-71.49, 41.11), 3.0, "east of the tile edge")
        self.assertEqual(self.depth_at(*centre(OTHER_FINE)), 1.5, "a finer cell of another district's folder")

    def test_two_dredged_areas_the_shallower(self):
        t7 = self.at(-71.585, 41.17)
        t5 = self.at(-71.575, 41.17)       # where the two overlap
        t5b = self.at(-71.565, 41.17)
        self.assertEqual([float(self.m["depth"][t]) for t in (t7, t5, t5b)], [7.0, 5.0, 5.0])
        for t in (t7, t5, t5b):
            self.assertEqual(int(self.m["flags"][t]) & (F_DREDGED | F_NAVIGABLE), F_DREDGED | F_NAVIGABLE)
        self.assertEqual(int(self.m["flags"][self.at(-71.55, 41.07)]) & (F_DREDGED | F_NAVIGABLE), 0)

    def test_obstruction_area(self):
        t = self.at(*centre(OBSTRUCTION))
        self.assertEqual(float(self.m["depth"][t]), 2.5)
        self.assertEqual(float(self.m["hazv"][t]), 2.5)
        self.assertTrue(int(self.m["flags"][t]) & F_HAZARD)
        beside = self.at(-71.455, 41.135)
        self.assertEqual(float(self.m["hazv"][beside]), 1e9)
        self.assertFalse(int(self.m["flags"][beside]) & F_HAZARD)

    def test_depth_penalty_steps(self):
        # the shore multiplier is 1.0 to 1.3; the depth step multiplies it: under 2 m x2, under 5 m x1.5, under 10 m x1.2
        for (lon, lat), step in ((centre(OTHER_FINE), 2.0), (centre(FINE), 1.5), ((-71.575, 41.17), 1.2),
                                 ((-71.55, 41.07), 1.0)):
            mult = float(self.m["mult"][self.at(lon, lat)])
            self.assertGreaterEqual(mult, step * 1.0 - 1e-6)
            self.assertLessEqual(mult, step * 1.3 + 1e-6)

    # -- lines stay lines ----------------------------------------------------

    def marked_sides(self, bit):
        """{(end, end)} of the triangle sides carrying a side flag, each side once, ends rounded to 1e-7 degree."""
        out = {}
        tt, kk = np.nonzero(self.m["side_flags"] & bit)
        for t, k in zip(tt.tolist(), kk.tolist()):
            a, b = self.m["lonlat"][t, k], self.m["lonlat"][t, (k + 1) % 3]
            key = tuple(sorted([(round(float(a[0]), 7), round(float(a[1]), 7)),
                                (round(float(b[0]), 7), round(float(b[1]), 7))]))
            out[key] = (t, k)
        return out

    def assert_sides_cover(self, sides, ends):
        (x, y0), (_, y1) = ends
        self.assertGreater(len(sides), 0)
        for a, b in sides:
            self.assertEqual((a[0], b[0]), (x, x))
            self.assertTrue(y0 <= a[1] <= y1 and y0 <= b[1] <= y1, (a, b))
        self.assertAlmostEqual(sum(abs(b[1] - a[1]) for a, b in sides), y1 - y0, places=7,
                               msg="the whole line, once")

    def test_a_structure_line_is_marked_sides(self):
        sides = self.marked_sides(S_STRUCTURE)
        self.assert_sides_cover(sides, QUAY)
        for t, k in sides.values():
            self.assertEqual(float(self.m["side_clear"][t, k]), -999.0)

    def test_a_bridge_line_is_marked_sides_with_its_clearance(self):
        sides = self.marked_sides(S_OVERHEAD)
        self.assert_sides_cover(sides, BRIDGE)
        for t, k in sides.values():
            self.assertEqual(float(self.m["side_clear"][t, k]), 12.5)
            self.assertFalse(int(self.m["side_flags"][t, k]) & S_OPENING)
        # the triangles beside a line carry nothing of it: the line is the sides
        self.assertEqual(float(self.m["clear"].max()), -999.0)

    def test_both_triangles_of_a_marked_side_carry_the_mark(self):
        nb, rev = self.m["neighbours"], self.m["rev"]
        tt, kk = np.nonzero(self.m["side_flags"])
        self.assertGreater(len(tt), 0)
        for t, k in zip(tt.tolist(), kk.tolist()):
            u, r = int(nb[t, k]), int(rev[t, k])
            self.assertGreaterEqual(u, 0, "these lines lie in open water")
            self.assertEqual(int(self.m["side_flags"][u, r]), int(self.m["side_flags"][t, k]))
            self.assertEqual(float(self.m["side_clear"][u, r]), float(self.m["side_clear"][t, k]))

    # -- points stay points --------------------------------------------------

    def marked_corners(self):
        """{(lon, lat): (flags, value)} of every marked corner, positions rounded to 1e-7 degree."""
        out = {}
        tt, kk = np.nonzero(self.m["corner_flags"])
        for t, k in zip(tt.tolist(), kk.tolist()):
            p = self.m["lonlat"][t, k]
            key = (round(float(p[0]), 7), round(float(p[1]), 7))
            val = (int(self.m["corner_flags"][t, k]), round(float(self.m["corner_val"][t, k]), 5))
            self.assertEqual(out.setdefault(key, val), val, "one corner, one mark, in every triangle that has it")
        return out

    def test_chart_points_are_marked_corners(self):
        self.assertEqual(self.marked_corners(), {
            ROCK: (C_HAZARD, 1.2),
            BUOY: (C_MARK | C_CHANNEL_MARK, 1e9),
            LIGHT: (C_MARK, 1e9),
            SOUNDING_SHALLOW: (C_SOUNDING, 4.0),
        }, "the 25 m sounding in 10 m water and the band 4 rock under the band 5 chart are not marks")

    # -- the graph -----------------------------------------------------------

    def test_neighbour_links_are_mutual(self):
        nb, rev = self.m["neighbours"], self.m["rev"]
        tt, kk = np.nonzero(nb >= 0)
        # a linked side names the neighbour's side 0, 1 or 2 (-1 would read the neighbour's last side and pass)
        self.assertTrue(((rev[tt, kk] >= 0) & (rev[tt, kk] <= 2)).all())
        self.assertTrue((nb[nb[tt, kk], rev[tt, kk]] == tt).all())
        self.assertTrue((rev[nb < 0] == -1).all())
        # a side with no neighbour is on the water's outline: the charted box or the land
        p = self.m["lonlat"]
        for t, k in zip(*(a.tolist() for a in np.nonzero(nb < 0))):
            mid = (p[t, k] + p[t, (k + 1) % 3]) / 2
            on_box = min(abs(mid[0] - BOX[0]), abs(mid[0] - BOX[2]), abs(mid[1] - BOX[1]), abs(mid[1] - BOX[3])) < 1e-9
            on_island = (ISLAND[0] - 1e-9 <= mid[0] <= ISLAND[2] + 1e-9 and ISLAND[1] - 1e-9 <= mid[1] <= ISLAND[3] + 1e-9)
            self.assertTrue(on_box or on_island, mid)


@unittest.skipUnless(HAVE_MESH_DEPS and h.HAVE_CGAL, "needs the mesh packages and a C++ compiler with CGAL")
class ChartLineOnATileEdge(unittest.TestCase):
    """A band 5 cell west of the tile edge at -71.5 and a band 4 cell east of it, and a band 5 overhead cable
    lying exactly on that edge. Each tile marks the edge from the chart that wins on its own side, so the two
    tiles' records of those sides differ; the side carries the cable all the same, as it would inside a tile."""

    def test_the_line_is_kept_and_the_build_passes(self):
        cable = ((-71.5, 41.06), (-71.5, 41.08))
        with tempfile.TemporaryDirectory(prefix="s57meshedge-") as tmp:
            own = h.CellsFolder(Path(tmp) / "own")
            own.add("band5", "US5XX1AB", {
                "DEPARE": [area((-71.6, 41.05, -71.5, 41.10), {"DRVAL1": 3.0})],
                "CBLOHD": [line(cable, {"VERCLR": "9.5"})]})
            own.add("band4", "US4XX1AA", {
                "DEPARE": [area((-71.5, 41.05, -71.4, 41.10), {"DRVAL1": 10.0})]})
            p = subprocess.run(
                [sys.executable, str(h.ROOT / "build-mesh.py"), own.write(), "--name", "EDGE",
                 "-o", str(Path(tmp) / "out"), "--work", str(Path(tmp) / "work"), "-j", "2"],
                cwd=h.ROOT, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout[-3000:] + p.stderr[-3000:])
            self.assertIn("chart-line marks on tile edges joined from two differing records", p.stdout)
            m = h.read_mesh(Path(tmp) / "out" / "EDGE_mesh")
        tt, kk = np.nonzero(m["side_flags"] & S_OVERHEAD)
        self.assertGreater(len(tt), 0)
        covered = {}
        for t, k in zip(tt.tolist(), kk.tolist()):
            a, b = m["lonlat"][t, k], m["lonlat"][t, (k + 1) % 3]
            self.assertAlmostEqual(float(a[0]), -71.5, places=9); self.assertAlmostEqual(float(b[0]), -71.5, places=9)
            self.assertEqual(float(m["side_clear"][t, k]), 9.5)
            covered[tuple(sorted((round(float(a[1]), 7), round(float(b[1]), 7))))] = True
            u, r = int(m["neighbours"][t, k]), int(m["rev"][t, k])
            self.assertTrue(u >= 0 and 0 <= r <= 2, "water on both sides of the cable")
            self.assertEqual(int(m["side_flags"][u, r]), int(m["side_flags"][t, k]))
        self.assertAlmostEqual(sum(hi - lo for lo, hi in covered), cable[1][1] - cable[0][1], places=7,
                               msg="the whole cable, once")
        self.assertEqual(float(m["depth"][h.mesh_triangle_at(m, -71.55, 41.07)]), 3.0)
        self.assertEqual(float(m["depth"][h.mesh_triangle_at(m, -71.45, 41.07)]), 10.0)


if __name__ == "__main__":
    unittest.main()
