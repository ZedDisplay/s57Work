"""End-to-end: build-mesh.py on the chart cells of the five-cell fixture
district, exported by the chart tool itself (s57-to-mbtiles.py --by-band
--cells-only; its hand-over folder data/cells, with cells.json, is the
mesh build's input). Needs GDAL, the packages in mesh/requirements.txt
and a C++ compiler with CGAL; skips without.

What is pinned here:
  * exit status 0 and every check passed;
  * the hand-over folder holds the list and the files it names and
    nothing else; the chart tool's working folder data/geojson is not
    made; the cells listed and read are the band 4 cell and the three
    band 5 cells, the cancelled cell being left out and recorded;
  * the mesh's rectangle holds the band 4 cell; the tiles are WRPMESH2
    of exactly the documented length, index version 2;
  * inside a band 5 cell the depth is that cell's, taken here from its
    exported depth areas, not the band 4 cell's;
  * the sidecar and the archive.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import helpers as h

try:
    import numpy, scipy, shapely, triangle  # noqa: F401
    from shapely.geometry import Point, shape
    HAVE_MESH_DEPS = True
except ImportError:
    HAVE_MESH_DEPS = False

BAND4_BOX = (-71.7, 40.8, -71.4, 41.1)
BAND5_AD_CENTER = (-71.5875, 41.0625)             # inside US5RI1AD, itself inside the band 4 cell
BAND4_ONLY_POINT = (-71.55, 40.9)                 # inside US4NY1BY, no band 5


def shallowest_depth_area(geojson_file, lon, lat):
    """The least DRVAL1 among a cell's exported depth areas that hold the point, or None."""
    vals = []
    for ft in json.loads(Path(geojson_file).read_text())["features"]:
        if ft.get("geometry") and shape(ft["geometry"]).contains(Point(lon, lat)):
            v = ft["properties"].get("DRVAL1")
            if v is not None:
                vals.append(float(v))
    return min(vals) if vals else None


@unittest.skipUnless(h.HAVE_GDAL and HAVE_MESH_DEPS and h.HAVE_CGAL,
                     "needs ogr2ogr/ogrinfo, the mesh packages and a C++ compiler with CGAL")
class MeshPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="s57mesh-"))
        zip_ = cls.work / "MINI_ENCs.zip"
        shutil.copy(h.FIXTURE_ZIP, zip_)
        chart = h.run_pipeline(cls.work, str(zip_), "--by-band", "--no-replacements", "--cells-only", "-j", "2")
        cls.cells = cls.work / "data" / "cells"
        if chart.returncode != 0 or not (cls.cells / "cells.json").exists():
            raise AssertionError("cell export failed:\n" + chart.stdout[-3000:] + chart.stderr[-3000:])
        cls.chart = chart
        cls.out = cls.work / "out"
        cls.meshwork = cls.work / "meshwork"
        cls.proc = subprocess.run(
            [sys.executable, str(h.ROOT / "build-mesh.py"), str(cls.cells), "-o", str(cls.out), "--name", "MINI",
             "--work", str(cls.meshwork), "-j", "2", "--keep-intermediate", "--archive"],
            cwd=h.ROOT, capture_output=True, text=True)
        side = cls.out / "MINI_mesh.json"
        if cls.proc.returncode != 0 or not side.exists():
            raise AssertionError(f"build-mesh failed ({cls.proc.returncode}):\n"
                                 + cls.proc.stdout[-4000:] + cls.proc.stderr[-4000:])
        cls.side = json.loads(side.read_text())
        cls.m = h.read_mesh(cls.out / "MINI_mesh")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_cells_only_builds_no_tiles(self):
        self.assertIn("Cells exported (no tiles built)", self.chart.stdout)
        self.assertEqual(list((self.work / "data" / "tiles").glob("*.mbtiles")), [])
        self.assertFalse(any((self.work / "data" / "merged").glob("*/*.geojson")), "nothing is consolidated")
        self.assertFalse(any((self.work / "data" / "geojson").glob("*/*")), "the tile build's working folder is not used")

    def test_the_folder_holds_the_list_and_what_it_names_and_nothing_else(self):
        listed = json.loads((self.cells / "cells.json").read_text())
        self.assertEqual(listed["version"], 1)
        self.assertEqual(listed["inputs"], ["MINI_ENCs.zip"])
        self.assertEqual(listed["cancelled_cells_left_out"], [h.FIXTURE_CANCELLED])
        self.assertEqual(sorted((c["cell"], c["band"]) for c in listed["cells"]),
                         sorted([(h.FIXTURE_BAND4, 4)] + [(c, 5) for c in h.FIXTURE_BAND5]))
        named = {f for c in listed["cells"] for f in c["files"].values()}
        on_disk = {str(p.relative_to(self.cells)) for p in self.cells.rglob("*") if p.is_file()}
        self.assertEqual(on_disk, named | {"cells.json"})
        for c in listed["cells"]:
            self.assertIn("DEPARE", c["files"])
            for layer, f in c["files"].items():
                self.assertEqual(f, f"band{c['band']}/{layer}_{c['cell']}.geojson", "one naming form, also for a one-cell band")

    def test_exit_status_and_checks(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout[-2000:])
        self.assertIn("every check passed", self.proc.stdout)
        self.assertIn("FAILED 0", self.proc.stdout)
        self.assertTrue(self.side["passed"])

    def test_cells_read(self):
        own, = self.side["cells"]
        self.assertEqual(own["role"], "own")
        self.assertEqual((own["cells_listed"], own["cells_by_band"]), (4, {"band4": 1, "band5": 3}))
        self.assertEqual(own["cancelled_cells_left_out"], [h.FIXTURE_CANCELLED])
        idx = json.loads((self.meshwork / "cell-index.json").read_text())
        self.assertEqual(sorted(c[2] for c in idx), sorted((h.FIXTURE_BAND4,) + h.FIXTURE_BAND5))

    def test_rectangle_and_format(self):
        idx = self.m["index"]                 # read_mesh has checked each tile's magic, count, origin and length
        self.assertEqual((idx["version"], idx["format"], idx["tileDeg"]), (2, "WRPMESH2", 0.25))
        self.assertLessEqual(idx["west"], BAND4_BOX[0]); self.assertLessEqual(idx["south"], BAND4_BOX[1])
        self.assertGreaterEqual(idx["east"], BAND4_BOX[2]); self.assertGreaterEqual(idx["north"], BAND4_BOX[3])
        self.assertGreater(idx["west"], -72.0, "the cancelled New Jersey cell reached the mesh")
        self.assertEqual(idx["triangles"], sum(t["n"] for t in idx["tiles"]))
        self.assertEqual(idx["triangles"], self.side["triangles"])
        self.assertEqual(self.side["clusters"][0]["box"], [idx["west"], idx["south"], idx["east"], idx["north"]])

    def test_band5_depth_inside_band5(self):
        lon, lat = BAND5_AD_CENTER
        t = h.mesh_triangle_at(self.m, lon, lat)
        self.assertIsNotNone(t, "the band 5 centre is water in the fixture")
        fine = shallowest_depth_area(self.cells / "band5" / "DEPARE_US5RI1AD.geojson", lon, lat)
        self.assertIsNotNone(fine)
        self.assertEqual(float(self.m["depth"][t]), float(numpy.float32(fine)))

    def test_band4_depth_where_there_is_no_band5(self):
        lon, lat = BAND4_ONLY_POINT
        t = h.mesh_triangle_at(self.m, lon, lat)
        self.assertIsNotNone(t)
        coarse = shallowest_depth_area(self.cells / "band4" / f"DEPARE_{h.FIXTURE_BAND4}.geojson", lon, lat)
        self.assertIsNotNone(coarse)
        self.assertEqual(float(self.m["depth"][t]), float(numpy.float32(coarse)))

    def test_sidecar_and_archive(self):
        s = self.side
        self.assertEqual(s["name"], "MINI")
        self.assertEqual(s["format"], {"magic": "WRPMESH2", "index_version": 2, "tile_deg": 0.25,
                                       "multi_cluster": False})
        self.assertIsNone(s["chart_build_date"])          # no --chart-json given
        self.assertGreater(s["tile_stage"]["water_km2"], 0)
        self.assertEqual(s["tile_stage"]["same_rank_overlap_km2"], 0)
        arc = self.out / "MINI_mesh.tar.zst"
        listing = subprocess.run(["tar", "--zstd", "-tf", str(arc)], capture_output=True, text=True)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("MINI_mesh/index.json", listing.stdout)
        self.assertTrue(any((self.meshwork / "tiles").glob("tile_*.npz")), "--keep-intermediate keeps the cut")


@unittest.skipUnless(h.HAVE_GDAL, "needs ogr2ogr/ogrinfo")
class ACellThatDoesNotExport(unittest.TestCase):
    """The fixture's cells plus one cell file that is not S-57: GDAL cannot list its layers. The cell export must
    exit 1 and write no list, and the mesh build must refuse the folder for having none. With the bad cell gone
    the next export is whole again: the folder is made anew every time, so nothing of the failed run remains."""

    @classmethod
    def setUpClass(cls):
        import zipfile
        cls.work = Path(tempfile.mkdtemp(prefix="s57badcell-"))
        with zipfile.ZipFile(h.FIXTURE_ZIP) as z:
            z.extractall(cls.work / "src")
        bad = cls.work / "src" / "ENC_ROOT" / "US5ZZ9ZZ"
        bad.mkdir(parents=True)
        (bad / "US5ZZ9ZZ.000").write_text("this is not an S-57 cell\n")
        cls.args = (str(cls.work / "src"), "--by-band", "--no-replacements", "--cells-only", "-j", "2")
        cls.export = h.run_pipeline(cls.work, *cls.args)
        cls.cells = cls.work / "data" / "cells"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_cells_only_fails_and_writes_no_list(self):
        self.assertEqual(self.export.returncode, 1, self.export.stdout[-2000:] + self.export.stderr[-2000:])
        self.assertIn("1 cell(s) did not export completely: US5ZZ9ZZ (band 5)", self.export.stderr)
        self.assertNotIn("Cells exported (no tiles built)", self.export.stdout)
        self.assertFalse((self.cells / "cells.json").exists())
        self.assertIn("US5ZZ9ZZ", (self.cells / "band5" / ".export-errors.log").read_text(), "the reason stays readable")

    @unittest.skipUnless(HAVE_MESH_DEPS and h.HAVE_CGAL, "needs the mesh packages and a C++ compiler with CGAL")
    def test_the_mesh_build_refuses_the_folder(self):
        p = subprocess.run(
            [sys.executable, str(h.ROOT / "build-mesh.py"), str(self.cells), "--name", "BAD",
             "-o", str(self.work / "out"), "--work", str(self.work / "meshwork"), "-j", "2"],
            cwd=h.ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertIn("no cells.json", p.stdout)
        self.assertFalse((self.work / "out" / "BAD_mesh.json").exists())

    def test_with_the_cell_gone_the_next_export_is_whole(self):
        # (runs after the two above: unittest takes a class's tests in name order)
        shutil.rmtree(self.work / "src" / "ENC_ROOT" / "US5ZZ9ZZ")
        again = h.run_pipeline(self.work, *self.args)
        self.assertEqual(again.returncode, 0, again.stdout[-2000:] + again.stderr[-2000:])
        listed = json.loads((self.cells / "cells.json").read_text())
        self.assertEqual(sorted(c["cell"] for c in listed["cells"]), sorted((h.FIXTURE_BAND4,) + h.FIXTURE_BAND5))
        self.assertEqual(sorted(p.name for p in (self.cells / "band5").iterdir() if p.name.startswith(".")), [],
                         "no error log and no marker of the failed run or of this one")


@unittest.skipUnless(h.HAVE_GDAL, "needs ogr2ogr/ogrinfo")
class NothingOfAnEarlierRunIsReused(unittest.TestCase):
    """--cells-only works in data/cells-work, emptied at the start of every run, and reads nothing an earlier
    run left. Two runs on the same folder and the same input name, the input changed in between."""

    def setUp(self):
        import zipfile
        self.work = Path(tempfile.mkdtemp(prefix="s57fresh-"))
        self.zipfile = zipfile

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def run_cells(self, inp):
        r = h.run_pipeline(self.work, str(inp), "--by-band", "--no-replacements", "--cells-only", "-j", "2")
        listed = self.work / "data" / "cells" / "cells.json"
        return r, (json.loads(listed.read_text()) if listed.exists() else None)

    def test_a_changed_input_of_the_same_name_is_read_anew(self):
        # first the whole fixture (its cancelled cell cancelled); then a zip of the same name in which that cell
        # has no cancellation update: the cell is live now and must be exported, not taken from the first run
        zp = self.work / "MINI_ENCs.zip"
        shutil.copy(h.FIXTURE_ZIP, zp)
        first, listed = self.run_cells(zp)
        self.assertEqual(first.returncode, 0, first.stdout[-2000:] + first.stderr[-2000:])
        self.assertEqual(listed["cancelled_cells_left_out"], [h.FIXTURE_CANCELLED])
        with self.zipfile.ZipFile(h.FIXTURE_ZIP) as src, self.zipfile.ZipFile(zp, "w") as dst:
            for m in src.infolist():
                if os.path.basename(m.filename) != f"{h.FIXTURE_CANCELLED}.001":
                    dst.writestr(m, src.read(m))
        second, listed = self.run_cells(zp)
        self.assertEqual(second.returncode, 0, second.stdout[-2000:] + second.stderr[-2000:])
        self.assertNotIn("already staged", second.stdout)
        self.assertEqual(listed["cancelled_cells_left_out"], [])
        self.assertIn(h.FIXTURE_CANCELLED, [c["cell"] for c in listed["cells"]])

    def test_a_run_that_fails_before_the_export_leaves_no_list(self):
        # after a whole run, a run whose input cannot be read at all (a file that is no zip) fails while staging,
        # long before the export: the list of the earlier run is gone all the same
        zp = self.work / "MINI_ENCs.zip"
        shutil.copy(h.FIXTURE_ZIP, zp)
        first, listed = self.run_cells(zp)
        self.assertEqual(first.returncode, 0, first.stdout[-2000:] + first.stderr[-2000:])
        bad = self.work / "BROKEN_ENCs.zip"
        bad.write_text("this is not a zip\n")
        second, listed = self.run_cells(bad)
        self.assertEqual(second.returncode, 1, second.stdout[-2000:] + second.stderr[-2000:])
        self.assertIn("is not a ZIP or directory", second.stderr)
        self.assertIsNone(listed)
        self.assertFalse((self.work / "data" / "cells").exists(), "nothing of the earlier hand-over is left")

    def test_nothing_given_may_lie_in_a_folder_the_run_empties(self):
        # refused before anything is emptied: the input, the catalogue and the earlier hand-over are untouched
        zp = self.work / "MINI_ENCs.zip"
        shutil.copy(h.FIXTURE_ZIP, zp)
        first, _ = self.run_cells(zp)
        self.assertEqual(first.returncode, 0, first.stdout[-2000:] + first.stderr[-2000:])
        data = self.work / "data"
        inside = [data / "cells" / "IN_ENCs.zip", data / "cells-work" / "IN_ENCs.zip"]
        for p in inside:
            shutil.copy(h.FIXTURE_ZIP, p)
        catalog = data / "cells-work" / "cat.xml"
        catalog.write_text("<x/>")
        for args, what in (([str(inside[0])], "input"), ([str(inside[1])], "input"),
                           ([str(zp), "--catalog", str(catalog)], "--catalog")):
            r = h.run_pipeline(self.work, *args, "--by-band", "--no-replacements", "--cells-only", "-j", "2")
            self.assertEqual(r.returncode, 2, r.stdout[-2000:] + r.stderr[-2000:])
            self.assertIn(what, r.stderr)
            self.assertIn("which --cells-only empties before it reads its inputs", r.stderr)
            for p in inside + [catalog]:
                self.assertTrue(p.exists(), f"{p} untouched")
            self.assertTrue((data / "cells" / "cells.json").exists(), "the earlier hand-over is untouched")

    def test_a_cell_that_no_longer_exports_leaves_no_list(self):
        # every cell exports; then one cell of the input is replaced by something GDAL cannot read: the next run
        # exits 1 and leaves no list (the list of the first run does not stand over a folder that no longer
        # matches it)
        with self.zipfile.ZipFile(h.FIXTURE_ZIP) as z:
            z.extractall(self.work / "src")
        first, listed = self.run_cells(self.work / "src")
        self.assertEqual(first.returncode, 0, first.stdout[-2000:] + first.stderr[-2000:])
        cell = h.FIXTURE_BAND5[0]
        (self.work / "src" / "ENC_ROOT" / cell / f"{cell}.000").write_text("this is no longer an S-57 cell\n")
        second, listed = self.run_cells(self.work / "src")
        self.assertEqual(second.returncode, 1, second.stdout[-2000:] + second.stderr[-2000:])
        self.assertIn(f"did not export completely: {cell} (band 5)", second.stderr)
        self.assertIsNone(listed)


class OrphanFiles(unittest.TestCase):
    """The chart tool's working folder: a layer file without a cell name belongs to the folder's one cell. When
    the folder's inventory is not exactly one cell it belongs to none (it is from when the folder held one) and
    goes with the other orphans; it used to stay and be merged into the layer a second time. Needs nothing but
    the chart tool itself."""

    def folder(self, tmp, names):
        d = Path(tmp) / "band4"
        d.mkdir()
        for n in names:
            (d / n).write_text("{}")
        return d

    def test_a_plain_file_goes_when_the_folder_holds_more_than_one_cell(self):
        p = h.pipeline()
        with tempfile.TemporaryDirectory(prefix="s57orphan-") as tmp:
            d = self.folder(tmp, ["DEPARE.geojson", "M_COVR.geojson", ".US4XX1AA.exported", "DEPARE_US4XX1AB.geojson",
                                  ".US4XX1AB.exported", "DEPARE_US4XX9ZZ.geojson", ".US4XX9ZZ.exported"])
            p._remove_orphan_cells(d, {"US4XX1AA", "US4XX1AB"})
            self.assertEqual(sorted(f.name for f in d.iterdir()),
                             [".US4XX1AA.exported", ".US4XX1AB.exported", "DEPARE_US4XX1AB.geojson"])

    def test_a_plain_file_stays_when_the_folder_holds_one_cell(self):
        p = h.pipeline()
        with tempfile.TemporaryDirectory(prefix="s57orphan-") as tmp:
            d = self.folder(tmp, ["DEPARE.geojson", "M_COVR.geojson", ".US4XX1AA.exported"])
            p._remove_orphan_cells(d, {"US4XX1AA"})
            self.assertEqual(sorted(f.name for f in d.iterdir()), [".US4XX1AA.exported", "DEPARE.geojson", "M_COVR.geojson"])


if __name__ == "__main__":
    unittest.main()
