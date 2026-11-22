"""End-to-end: build-mesh.py on the tiles of the five-cell fixture
district (the same chart build as tests/test_pipeline.py), with the
clipped OSM land polygons in tests/fixtures/land/. Needs GDAL,
tippecanoe and the packages in mesh/requirements.txt; skips without.

What is pinned here:
  * the extent is the rectangle around the cells that hold z16 tiles,
    and it covers the fixture's band 4 cell;
  * every gating requirement passes and the exit status is 0;
  * the binary tiles are exactly the size the WRPMESH1 layout implies
    for their triangle count, and index.json is version 1;
  * the sidecar records the sources (with checksums), the land polygon
    date, the extent and the checks;
  * the archive holds the mesh folder; the intermediates are kept when
    asked for.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import helpers as h

try:
    import numpy, scipy, shapely, triangle, mapbox_vector_tile, shapefile  # noqa: F401
    HAVE_MESH_DEPS = True
except ImportError:
    HAVE_MESH_DEPS = False

LAND = h.ROOT / "tests" / "fixtures" / "land" / "land_polygons.shp"
BAND4_BOX = (-71.7, 40.8, -71.4, 41.1)
HEADER = 32
PER_TRIANGLE = 6 * 8 + 3 * 4 + 4 + 4 + 4 + 4 + 3 + 1    # corners, neighbours, mult, depth, clear, hazv, rev, flags


@unittest.skipUnless(h.HAVE_GDAL and h.HAVE_TIPPECANOE and HAVE_MESH_DEPS,
                     "needs ogr2ogr/ogrinfo, tippecanoe/tile-join and the mesh packages")
class MeshPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="s57mesh-"))
        zip_ = cls.work / "MINI_ENCs.zip"
        shutil.copy(h.FIXTURE_ZIP, zip_)
        chart = h.run_pipeline(cls.work, str(zip_), "--by-band", "--no-replacements",
                               "-j", "2", "-o", "mini.mbtiles")
        cls.mbtiles = cls.work / "data" / "tiles" / "mini.mbtiles"
        if chart.returncode != 0 or not cls.mbtiles.exists():
            raise AssertionError("chart pipeline failed:\n" + chart.stdout[-3000:] + chart.stderr[-3000:])
        cls.out = cls.work / "out"
        cls.meshwork = cls.work / "meshwork"
        cls.proc = subprocess.run(
            [sys.executable, str(h.ROOT / "build-mesh.py"), str(cls.mbtiles),
             "--land", str(LAND), "--no-catalog", "-o", str(cls.out), "--name", "MINI", "--work", str(cls.meshwork),
             "-j", "2", "--keep-intermediate", "--archive"],
            cwd=h.ROOT, capture_output=True, text=True)
        side = cls.out / "MINI_mesh.json"
        if cls.proc.returncode != 0 or not side.exists():
            raise AssertionError(f"build-mesh failed ({cls.proc.returncode}):\n"
                                 + cls.proc.stdout[-4000:] + cls.proc.stderr[-4000:])
        cls.side = json.loads(side.read_text())
        cls.index = json.loads((cls.out / "MINI_mesh" / "index.json").read_text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_exit_status_and_gating(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout[-2000:])
        self.assertTrue(self.side["passed"])
        self.assertEqual(self.side["gating_failures"], [])
        self.assertIn("all gating requirements passed", self.proc.stdout)

    def test_extent_is_the_rectangle_around_z16_cells(self):
        self.assertEqual(len(self.side["clusters"]), 1)
        c = self.side["clusters"][0]
        w, s, e, n = c["box"]
        self.assertLessEqual(w, BAND4_BOX[0]); self.assertLessEqual(s, BAND4_BOX[1])
        self.assertGreaterEqual(e, BAND4_BOX[2]); self.assertGreaterEqual(n, BAND4_BOX[3])
        self.assertEqual(c["tiles"], c["tiles_x"] * c["tiles_y"])
        self.assertGreater(c["cells_with_z16"], 0)
        self.assertLessEqual(c["cells_with_z16"], c["tiles"])
        self.assertEqual(c["tiles_built"], c["tiles"], "every tile of the rectangle is processed")
        self.assertEqual(self.index["west"], w); self.assertEqual(self.index["south"], s)
        self.assertEqual(self.index["tileDeg"], 0.25)

    def test_binary_tiles_match_the_format(self):
        self.assertEqual(self.index["version"], 1)
        self.assertGreater(len(self.index["tiles"]), 0)
        self.assertEqual(self.index["triangles"], sum(t["n"] for t in self.index["tiles"]))
        self.assertEqual(self.index["triangles"], self.side["triangles"])
        first = 0
        for t in self.index["tiles"]:
            p = self.out / "MINI_mesh" / t["file"]
            self.assertEqual(p.stat().st_size, HEADER + PER_TRIANGLE * t["n"], t["file"])
            with open(p, "rb") as f:
                head = f.read(HEADER)
            self.assertEqual(head[:8], b"WRPMESH1")
            n, ox, oy = struct.unpack("<Idd", head[8:28])
            self.assertEqual(n, t["n"])
            self.assertAlmostEqual(ox, self.index["west"] + t["i"] * 0.25)
            self.assertAlmostEqual(oy, self.index["south"] + t["j"] * 0.25)
            self.assertEqual(t["first"], first, "ids are contiguous per tile in file order")
            first += t["n"]
        # the band 5 centre lies in some tile's bbox
        lon, lat = -71.5875, 41.0625
        self.assertTrue(any(t["bbox"][0] <= lon <= t["bbox"][2] and t["bbox"][1] <= lat <= t["bbox"][3]
                            for t in self.index["tiles"]))

    def test_sidecar_provenance(self):
        s = self.side
        self.assertEqual(s["name"], "MINI")
        self.assertEqual(s["format"], {"magic": "WRPMESH1", "index_version": 1, "tile_deg": 0.25,
                                       "multi_cluster": False})
        self.assertEqual([x["file"] for x in s["sources"]], ["mini.mbtiles"])
        self.assertEqual(len(s["sources"][0]["sha256"]), 64)
        self.assertEqual(s["land_polygons"]["date"], "2026-06-28T00:00:00Z")
        self.assertGreater(s["z16_tiles_decoded"], 0)
        self.assertIsNone(s["chart_build_date"])          # no --chart-json given
        names = [r["name"] for r in s["clusters"][0]["requirements"]]
        self.assertIn("1 tiles built", names)
        self.assertIn("14 route-ready from files", names)
        # The fixture cells lie south of Block Island: no LNDARE at z16, so
        # land comes from the OSM fixture alone, outside the cells' M_COVR;
        # depth and soundings are charted.
        self.assertIn("DEPARE", s["clusters"][0]["layers"])
        self.assertIn("M_COVR", s["clusters"][0]["layers"])
        self.assertIn("SOUNDG", s["clusters"][0]["layers"])
        self.assertIn("2 layers loaded", [w["name"] for w in s["warnings"]])
        req = {r["name"]: r for r in s["clusters"][0]["requirements"]}
        self.assertTrue(req["15 chart coverage loaded"]["pass"], req["15 chart coverage loaded"]["detail"])
        self.assertTrue(s["passed"], s["gating_failures"])

    def test_archive_and_intermediates(self):
        arc = self.out / "MINI_mesh.tar.zst"
        self.assertTrue(arc.exists())
        listing = subprocess.run(["tar", "--zstd", "-tf", str(arc)], capture_output=True, text=True)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("MINI_mesh/index.json", listing.stdout)
        self.assertTrue((self.meshwork / "z16_layers" / "DEPARE.pkl").exists())
        self.assertTrue((self.meshwork / "build" / "summary.json").exists())
        self.assertTrue(any(p.name.startswith("mesh_") for p in (self.meshwork / "final").iterdir()))


if __name__ == "__main__":
    unittest.main()
