"""A mesh across the date line, as the router reads one
(signalk-weather-router-plus src/engine/mesh/store.ts): longitudes
between -180 and 180; the index's box with its west greater than its
east; tile columns counted east from that west; the date line a tile
edge that no triangle lies across, a corner on it 180 in the tile west
of it and -180 in the tile east of it.

Hand-made cells, as NOAA cuts its own (every cell ends on the date line,
none lies across it):

    US4XX1WA  band 4  depth area 10 m, 179.9..180 x 10.0..10.1, and a rock
                      of 1 m exactly on the date line at 10.05
    US4XX1EA  band 4  depth area 10 m, -180..-179.9, and one of 4 m,
                      -179.9..-179.8, same latitudes

Also the rule the box follows (mesh.build.shortest_run), and a district
that only sits on both sides of the line without water joining across
it: its box goes the short way.

Needs the mesh packages and a C++ compiler with CGAL; skips without.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import helpers as h
from tests.test_mesh_build import area, point

try:
    import numpy as np
    import scipy, shapely, triangle  # noqa: F401
    from mesh import build as B
    HAVE_MESH_DEPS = True
except ImportError:
    HAVE_MESH_DEPS = False

C_HAZARD = 1


def build(base, name, cells):
    """Write the cells as a hand-over folder under base/own and run build-mesh.py on it; returns (process, mesh
    arrays)."""
    own = h.CellsFolder(Path(base) / "own")
    for band, cell, layers in cells:
        own.add(band, cell, layers)
    proc = subprocess.run(
        [sys.executable, str(h.ROOT / "build-mesh.py"), own.write(), "--name", name,
         "-o", str(Path(base) / "out"), "--work", str(Path(base) / "work"), "-j", "2"],
        cwd=h.ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"build-mesh failed ({proc.returncode}):\n" + proc.stdout[-4000:] + proc.stderr[-4000:])
    return proc, h.read_mesh(Path(base) / "out" / f"{name}_mesh")


@unittest.skipUnless(HAVE_MESH_DEPS, "mesh packages not installed")
class ShortestRun(unittest.TestCase):
    def test_a_plain_run(self):
        self.assertEqual(B.shortest_run([420, 421, 430]), (420, 11))
        self.assertEqual(B.shortest_run([7]), (7, 1))

    def test_across_the_date_line(self):
        # columns either side of the line: the run starts west of it and is two columns long
        self.assertEqual(B.shortest_run([0, B.N_COLS - 1]), (B.N_COLS - 1, 2))
        # Alaska's shape: from 165.5 E (column 1382) eastwards to 130 W (column 199)
        self.assertEqual(B.shortest_run(list(range(1382, B.N_COLS)) + list(range(0, 200))), (1382, 258))

    def test_both_sides_without_touching_goes_the_short_way(self):
        # 131 E to 173.5 E (columns 1244..1413) and 180 to 154 W (columns 0..103): west edge at 131 E
        west, n = B.shortest_run(list(range(1244, 1414)) + list(range(0, 104)))
        self.assertEqual((west, n), (1244, B.N_COLS - 1244 + 104))

    def test_a_tie_is_the_plain_run(self):
        self.assertEqual(B.shortest_run([0, B.N_COLS // 2]), (0, B.N_COLS // 2 + 1))


@unittest.skipUnless(HAVE_MESH_DEPS and h.HAVE_CGAL, "needs the mesh packages and a C++ compiler with CGAL")
class AcrossTheDateLine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="s57dateline-")
        cls.proc, cls.m = build(cls.tmp.name, "LINE", [
            ("band4", "US4XX1WA", {"DEPARE": [area((179.9, 10.0, 180.0, 10.1), {"DRVAL1": 10.0})],
                                   "UWTROC": [point((180.0, 10.05), {"VALSOU": 1.0})]}),
            ("band4", "US4XX1EA", {"DEPARE": [area((-180.0, 10.0, -179.9, 10.1), {"DRVAL1": 10.0}),
                                              area((-179.9, 10.0, -179.8, 10.1), {"DRVAL1": 4.0})]}),
        ])
        cls.idx = cls.m["index"]
        cls.west_tile = cls.m["tile_of"] == 0
        cls.east_tile = cls.m["tile_of"] == 1

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_index_box_has_its_west_greater_than_its_east(self):
        self.assertIn("every check passed", self.proc.stdout)
        self.assertIn("the mesh crosses the date line", self.proc.stdout)
        self.assertEqual((self.idx["west"], self.idx["east"]), (179.75, -179.75))
        self.assertEqual([(t["i"], t["j"]) for t in self.idx["tiles"]], [(0, 0), (1, 0)], "columns count east from the west")

    def test_tile_extents_are_plain_numbers_that_end_on_the_line(self):
        w, e = (t["bbox"] for t in self.idx["tiles"])
        self.assertAlmostEqual(w[0], 179.9, places=9); self.assertEqual(w[2], 180.0)
        self.assertEqual(e[0], -180.0); self.assertAlmostEqual(e[2], -179.8, places=9)

    def test_no_triangle_lies_across_the_line_and_longitudes_are_the_usual_ones(self):
        lon = self.m["lonlat"][..., 0]
        self.assertTrue((np.abs(lon) <= 180 + 1e-9).all())
        self.assertTrue((lon[self.west_tile] >= 179.9 - 1e-9).all(), "the tile west of the line: 179.9 to 180")
        self.assertTrue((lon[self.east_tile] <= -179.8 + 1e-9).all(), "the tile east of it: -180 to -179.8")
        self.assertGreater(int(self.west_tile.sum()), 0)
        self.assertGreater(int(self.east_tile.sum()), 0)

    def test_all_the_water_is_there(self):
        p = self.m["lonlat"]
        a, b, c = p[:, 0], p[:, 1], p[:, 2]
        tri = np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])) / 2
        self.assertAlmostEqual(float(tri.sum()), 0.3 * 0.1, places=9)
        self.assertEqual(float(self.m["depth"][h.mesh_triangle_at(self.m, 179.95, 10.02)]), 10.0)
        self.assertEqual(float(self.m["depth"][h.mesh_triangle_at(self.m, -179.95, 10.02)]), 10.0)
        self.assertEqual(float(self.m["depth"][h.mesh_triangle_at(self.m, -179.85, 10.02)]), 4.0)

    def test_the_water_is_joined_across_the_line(self):
        nb, rev = self.m["neighbours"], self.m["rev"]
        p = self.m["lonlat"]
        seam = []                                   # the latitude interval of each side that joins the two tiles
        for t in np.flatnonzero(self.west_tile).tolist():
            for k in range(3):
                u = int(nb[t, k])
                if u < 0 or not self.east_tile[u]:
                    continue
                r = int(rev[t, k])
                self.assertTrue(0 <= r <= 2, f"the link from triangle {t} names no side of its neighbour ({r})")
                self.assertEqual(int(nb[u, r]), t)
                self.assertEqual(int(rev[u, r]), k)
                seam.append(tuple(sorted((float(p[t, k, 1]), float(p[t, (k + 1) % 3, 1])))))
                mine = sorted([p[t, k].tolist(), p[t, (k + 1) % 3].tolist()], key=lambda q: q[1])
                theirs = sorted([p[u, r].tolist(), p[u, (r + 1) % 3].tolist()], key=lambda q: q[1])
                for (lon_w, lat_w), (lon_e, lat_e) in zip(mine, theirs):
                    self.assertAlmostEqual(lon_w, 180.0, places=9)       # the same corner: 180 west of the line,
                    self.assertAlmostEqual(lon_e, -180.0, places=9)      # -180 east of it
                    self.assertEqual(lat_w, lat_e)
        self.assertGreater(len(seam), 0, "no side joins the two tiles")
        # the joining sides cover the line from 10.0 to 10.1 end to end: no gap, no overlap
        seam.sort()
        self.assertAlmostEqual(seam[0][0], 10.0, places=9)
        self.assertAlmostEqual(seam[-1][1], 10.1, places=9)
        for (_, top), (bottom, _) in zip(seam, seam[1:]):
            self.assertEqual(top, bottom)

    def test_a_chart_point_on_the_line_is_one_corner_in_both_tiles(self):
        tt, kk = np.nonzero(self.m["corner_flags"] & C_HAZARD)
        seen = {(bool(self.east_tile[t]), round(float(self.m["lonlat"][t, k, 0]), 7), round(float(self.m["lonlat"][t, k, 1]), 7))
                for t, k in zip(tt.tolist(), kk.tolist())}
        self.assertEqual(seen, {(False, 180.0, 10.05), (True, -180.0, 10.05)})
        for t, k in zip(tt.tolist(), kk.tolist()):
            self.assertEqual(float(self.m["corner_val"][t, k]), 1.0)


@unittest.skipUnless(HAVE_MESH_DEPS and h.HAVE_CGAL, "needs the mesh packages and a C++ compiler with CGAL")
class BothSidesWithoutCrossing(unittest.TestCase):
    """Water west of the line that stops short of it, and water that starts on it: the box goes the short way
    round, across the line, and is not the whole earth the long way."""

    def test_the_box_is_the_short_way(self):
        with tempfile.TemporaryDirectory(prefix="s57dateline-") as tmp:
            proc, m = build(tmp, "SIDES", [
                ("band4", "US4XX1WA", {"DEPARE": [area((173.4, 10.0, 173.5, 10.1), {"DRVAL1": 10.0})]}),
                ("band4", "US4XX1EA", {"DEPARE": [area((-180.0, 10.0, -179.9, 10.1), {"DRVAL1": 10.0})]}),
            ])
            idx = m["index"]
            self.assertIn("every check passed", proc.stdout)
            self.assertEqual((idx["west"], idx["east"]), (173.25, -179.75))
            # columns from 173.25 E: the tile at 173.25 is column 0, the tile from -180 column 27
            self.assertEqual([t["i"] for t in idx["tiles"]], [0, 27])
            self.assertEqual(json.loads((Path(tmp) / "out" / "SIDES_mesh.json").read_text())["clusters"][0]["box"],
                             [173.25, 10.0, -179.75, 10.25])


if __name__ == "__main__":
    unittest.main()
