"""End to end: build-mesh.py on real chart cells, the five-cell fixture
district (tests/fixtures/mini-district.zip: a band 4 cell, three band 5
cells inside it, one cancelled band 5 cell). The build reads the zip
itself. Needs GDAL, the packages in mesh/requirements.txt and a C++
compiler with CGAL; skips without.

What is pinned here:
  * exit status 0 and every check passed; the cells read are the band 4
    cell and the three band 5 cells, the cancelled cell left out and
    recorded;
  * the mesh's rectangle holds the band 4 cell; the tiles are WRPMESH2
    of exactly the documented length, index version 2;
  * inside a band 5 cell the depth is that cell's, taken here from the
    depth areas the build extracted from it, not the band 4 cell's;
  * the sidecar and the archive; the run's folder;
  * the charts given in two files are one district's charts;
  * charts that cannot be read stop the build, and nothing is written;
  * nothing of an earlier run is read, and an earlier result does not
    stand after a build that failed;
  * the paths the build refuses.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests import helpers as h

try:
    import numpy, scipy, shapely, triangle  # noqa: F401
    from shapely.geometry import Point, shape
    HAVE_MESH_DEPS = True
except ImportError:
    HAVE_MESH_DEPS = False

NEEDS = "needs ogr2ogr/ogrinfo, the mesh packages and a C++ compiler with CGAL"
HAVE_ALL = h.HAVE_GDAL and HAVE_MESH_DEPS and h.HAVE_CGAL
BAND4_BOX = (-71.7, 40.8, -71.4, 41.1)
BAND5_AD_CENTER = (-71.5875, 41.0625)             # inside US5RI1AD, itself inside the band 4 cell
BAND4_ONLY_POINT = (-71.55, 40.9)                 # inside US4NY1BY, no band 5
LIVE = sorted((h.FIXTURE_BAND4,) + h.FIXTURE_BAND5)


def shallowest_depth_area(geojson_file, lon, lat):
    """The least DRVAL1 among a cell's extracted depth areas that hold the point, or None."""
    vals = []
    for ft in json.loads(Path(geojson_file).read_text())["features"]:
        if ft.get("geometry") and shape(ft["geometry"]).contains(Point(lon, lat)):
            v = ft["properties"].get("DRVAL1")
            if v is not None:
                vals.append(float(v))
    return min(vals) if vals else None


def fixture_zip(dest, keep=lambda cell, filename: True):
    """A zip of the fixture's files for which keep(cell name, file name) holds."""
    with zipfile.ZipFile(h.FIXTURE_ZIP) as src, zipfile.ZipFile(dest, "w") as dst:
        for m in src.infolist():
            name = os.path.basename(m.filename)
            if name and keep(name.split(".")[0], name):
                dst.writestr(m, src.read(m))
    return dest


@unittest.skipUnless(HAVE_ALL, NEEDS)
class MeshPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="s57mesh-"))
        shutil.copy(h.FIXTURE_ZIP, cls.work / "MINI_ENCs.zip")
        cls.out = cls.work / "out"
        # Run in the folder of the charts, with relative paths: every path given must work from anywhere.
        cls.proc = h.run_build_mesh(cls.work, "MINI_ENCs.zip", "-o", "out", "--name", "MINI", "--work", "meshwork",
                                    "-j", "2", "--keep-intermediate", "--archive")
        side = cls.out / "MINI_mesh.json"
        if cls.proc.returncode != 0 or not side.exists():
            raise AssertionError(f"build-mesh failed ({cls.proc.returncode}):\n"
                                 + cls.proc.stdout[-4000:] + cls.proc.stderr[-4000:])
        cls.side = json.loads(side.read_text())
        cls.m = h.read_mesh(cls.out / "MINI_mesh")
        cls.run_dir, = (cls.work / "meshwork").iterdir()          # the run's own folder, kept (--keep-intermediate)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_exit_status_and_checks(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout[-2000:])
        self.assertIn("every check passed", self.proc.stdout)
        self.assertIn("FAILED 0", self.proc.stdout)
        self.assertTrue(self.side["passed"])

    def test_cells_read(self):
        c = self.side["charts"]
        self.assertEqual(c["inputs"], [{"file": "MINI_ENCs.zip", "bytes": h.FIXTURE_ZIP.stat().st_size,
                                        "cell_files_found": 5, "files_not_read": []}])
        self.assertEqual(c["cancelled_cells_left_out"], [h.FIXTURE_CANCELLED])
        self.assertEqual((c["live_cells"], c["cells_read"]), (4, 4))
        self.assertEqual(c["cells_read_by_band"], {"band4": 1, "band5": 3})
        self.assertEqual(c["cells_given_more_than_once"], 0)
        idx = json.loads((self.run_dir / "cell-index.json").read_text())
        self.assertEqual(sorted(c[2] for c in idx), LIVE)

    def test_the_run_folder(self):
        self.assertTrue(self.run_dir.name.startswith("run-"))
        self.assertEqual(sorted(p.name for p in (self.run_dir / "cells").iterdir()), LIVE,
                         "the layers of the live cells; the cancelled cell is not extracted")
        self.assertTrue((self.run_dir / "cells" / h.FIXTURE_BAND4 / "DEPARE.geojson").exists())
        self.assertTrue(any((self.run_dir / "tiles").glob("tile_*.npz")), "--keep-intermediate keeps the cut")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["MINI_mesh", "MINI_mesh.json", "MINI_mesh.tar.zst"])

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
        fine = shallowest_depth_area(self.run_dir / "cells" / "US5RI1AD" / "DEPARE.geojson", lon, lat)
        self.assertIsNotNone(fine)
        self.assertEqual(float(self.m["depth"][t]), float(numpy.float32(fine)))

    def test_band4_depth_where_there_is_no_band5(self):
        lon, lat = BAND4_ONLY_POINT
        t = h.mesh_triangle_at(self.m, lon, lat)
        self.assertIsNotNone(t)
        coarse = shallowest_depth_area(self.run_dir / "cells" / h.FIXTURE_BAND4 / "DEPARE.geojson", lon, lat)
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
        self.assertEqual(h.mesh_catalog().validate_entry(h.mesh_catalog().entry_from_sidecar(s, "https://example.test/charts")), [])
        listing = subprocess.run(["tar", "--zstd", "-tf", str(self.out / "MINI_mesh.tar.zst")], capture_output=True, text=True)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("MINI_mesh/index.json", listing.stdout)


@unittest.skipUnless(HAVE_ALL, NEEDS)
class ChartsInSeveralFiles(unittest.TestCase):
    """A district's charts given in two files: the band 4 cell in one, the band 5 cells and the cancelled cell in
    the other. The mesh is that of all of them: built for the tiles they reach, and inside a band 5 cell the
    depth is that cell's."""

    def test_two_files_are_one_districts_charts(self):
        with tempfile.TemporaryDirectory(prefix="s57two-") as tmp:
            tmp = Path(tmp)
            one = fixture_zip(tmp / "ONE_ENCs.zip", lambda cell, f: cell == h.FIXTURE_BAND4)
            two = fixture_zip(tmp / "TWO_ENCs.zip", lambda cell, f: cell != h.FIXTURE_BAND4)
            p = h.run_build_mesh(tmp, str(one), str(two), "--name", "TWO", "-o", "out", "--work", "w", "-j", "2",
                                 "--keep-intermediate")
            self.assertEqual(p.returncode, 0, p.stdout[-3000:] + p.stderr[-3000:])
            c = json.loads((tmp / "out" / "TWO_mesh.json").read_text())["charts"]
            self.assertEqual([(i["file"], i["cell_files_found"]) for i in c["inputs"]], [("ONE_ENCs.zip", 1), ("TWO_ENCs.zip", 4)])
            self.assertEqual((c["live_cells"], c["cells_read"]), (4, 4))
            self.assertEqual(c["cancelled_cells_left_out"], [h.FIXTURE_CANCELLED])
            m = h.read_mesh(tmp / "out" / "TWO_mesh")
            run, = (tmp / "w").iterdir()
            lon, lat = BAND5_AD_CENTER
            fine = shallowest_depth_area(run / "cells" / "US5RI1AD" / "DEPARE.geojson", lon, lat)
            self.assertEqual(float(m["depth"][h.mesh_triangle_at(m, lon, lat)]), float(numpy.float32(fine)))

    def test_charts_with_no_live_cell_make_no_mesh(self):
        with tempfile.TemporaryDirectory(prefix="s57two-") as tmp:
            tmp = Path(tmp)
            only = fixture_zip(tmp / "ONLY_ENCs.zip", lambda cell, f: cell == h.FIXTURE_CANCELLED)
            p = h.run_build_mesh(tmp, str(only), "--name", "NONE", "-o", "out", "--work", "w", "-j", "2")
            self.assertEqual(p.returncode, 1, p.stdout[-2000:] + p.stderr[-2000:])
            self.assertIn("the charts hold no live cell with charted areas", p.stdout)
            self.assertFalse((tmp / "out" / "NONE_mesh.json").exists())


@unittest.skipUnless(HAVE_ALL, NEEDS)
class ChartsThatCannotBeRead(unittest.TestCase):
    """Whatever of the charts cannot be read stops the build with exit status 1, naming it; no sidecar, no mesh."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="s57badcharts-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, *charts):
        p = h.run_build_mesh(self.tmp, *charts, "--name", "BAD", "-o", "out", "--work", "w", "-j", "2")
        return p, (self.tmp / "out" / "BAD_mesh.json").exists(), (self.tmp / "out" / "BAD_mesh").exists()

    def test_a_cell_that_is_not_s57(self):
        with zipfile.ZipFile(h.FIXTURE_ZIP) as z:
            z.extractall(self.tmp / "src")
        bad = self.tmp / "src" / "ENC_ROOT" / "US5ZZ9ZZ"
        bad.mkdir(parents=True)
        (bad / "US5ZZ9ZZ.000").write_text("this is not an S-57 cell\n")
        p, sidecar, mesh = self.build("src")
        self.assertEqual(p.returncode, 1, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertIn("1 cell(s) cannot be read", p.stdout)
        self.assertIn("US5ZZ9ZZ", p.stdout)
        self.assertFalse(sidecar or mesh)
        # with the cell gone the next build, in the same folders, is whole: nothing of the failed run is read
        shutil.rmtree(bad)
        p, sidecar, mesh = self.build("src")
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertTrue(sidecar and mesh)
        self.assertEqual(json.loads((self.tmp / "out" / "BAD_mesh.json").read_text())["charts"]["cells_read"], 4)
        self.assertEqual(len(list((self.tmp / "w").iterdir())), 1, "the failed run's folder is kept, the good run's removed")
        self.assertEqual(sorted(p.name for p in (self.tmp / "src" / "ENC_ROOT").iterdir()), sorted(LIVE + [h.FIXTURE_CANCELLED]),
                         "a charts folder is read where it lies and is not written to")

    def test_a_file_that_is_no_zip(self):
        (self.tmp / "BROKEN_ENCs.zip").write_text("this is not a zip\n")
        p, sidecar, mesh = self.build("BROKEN_ENCs.zip")
        self.assertEqual(p.returncode, 1, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertIn("is neither a zip nor a folder", p.stdout)
        self.assertFalse(sidecar or mesh)

    def test_charts_with_no_cell(self):
        (self.tmp / "empty").mkdir()
        (self.tmp / "empty" / "README.000").write_text("not a cell name\n")
        p, sidecar, mesh = self.build("empty")
        self.assertEqual(p.returncode, 1, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertIn("no chart cell", p.stdout)
        self.assertIn("README.000", p.stdout, "the file that was not read is named")
        self.assertFalse(sidecar or mesh)

    def test_charts_that_are_not_there(self):
        p, sidecar, mesh = self.build("nowhere.zip")
        self.assertEqual(p.returncode, 2, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertIn("no such file or folder: nowhere.zip", p.stderr)


@unittest.skipUnless(HAVE_ALL, NEEDS)
class NothingOfAnEarlierRunIsRead(unittest.TestCase):
    """Every run reads the charts anew, in a folder of its own. Runs in the same folders, on a zip of the same
    name that changes in between."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="s57fresh-"))
        self.zip = self.tmp / "MINI_ENCs.zip"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self):
        p = h.run_build_mesh(self.tmp, "MINI_ENCs.zip", "--name", "MINI", "-o", "out", "--work", "w", "-j", "2")
        side = self.tmp / "out" / "MINI_mesh.json"
        return p, (json.loads(side.read_text()) if side.exists() else None)

    def test_a_changed_zip_of_the_same_name_is_read_anew(self):
        # first the whole fixture (its cancelled cell cancelled); then a zip of the same name in which that cell
        # has no cancellation update: the cell is live now and is in the mesh's cells
        shutil.copy(h.FIXTURE_ZIP, self.zip)
        p, side = self.build()
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertEqual((side["charts"]["cancelled_cells_left_out"], side["charts"]["cells_read"]), ([h.FIXTURE_CANCELLED], 4))
        west = side["clusters"][0]["box"][0]
        fixture_zip(self.zip, lambda cell, f: f != f"{h.FIXTURE_CANCELLED}.001")
        p, side = self.build()
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertEqual((side["charts"]["cancelled_cells_left_out"], side["charts"]["cells_read"]), ([], 5))
        self.assertLess(side["clusters"][0]["box"][0], west, "the New Jersey cell is in the mesh now")
        self.assertEqual(list((self.tmp / "w").iterdir()), [], "each run removed its own folder")

    def test_an_earlier_result_does_not_stand_after_a_build_that_failed(self):
        shutil.copy(h.FIXTURE_ZIP, self.zip)
        p, side = self.build()
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        self.zip.write_text("this is not a zip\n")
        p, side = self.build()
        self.assertEqual(p.returncode, 1, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertIsNone(side, "no sidecar of the earlier build")
        self.assertEqual(list((self.tmp / "out").iterdir()), [], "and no mesh of it")


class Paths(unittest.TestCase):
    """The build removes the earlier result of the same name, <out>/<name>_mesh with its sidecar and archive, and
    writes its working files into the work folder. What it is given may not clash with either; every path is
    resolved first, so a link is judged by where it points."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="s57meshpaths-"))
        self.o = self.tmp / "o"
        (self.o / "X_mesh").mkdir(parents=True)
        (self.o / "X_mesh" / "index.json").write_text("{}")
        self.zip = self.tmp / "MINI_ENCs.zip"
        shutil.copy(h.FIXTURE_ZIP, self.zip)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def refused(self, said, *args):
        p = h.run_build_mesh(self.tmp, *args, "--name", "X", "-o", str(self.o))
        self.assertEqual(p.returncode, 2, f"{args}: " + p.stdout[-500:] + p.stderr[-500:])
        self.assertIn(said, p.stderr, args)
        self.assertTrue((self.o / "X_mesh" / "index.json").exists(), "nothing was removed")

    def test_nothing_given_may_lie_in_the_folder_the_build_removes(self):
        inside = self.o / "X_mesh" / "IN_ENCs.zip"
        shutil.copy(h.FIXTURE_ZIP, inside)
        link = self.tmp / "link-to-zip"
        link.symlink_to(inside)
        self.refused("the build removes that folder", str(inside))
        self.refused("the build removes that folder", str(link))
        self.refused("the build removes that folder", str(self.zip), str(inside))
        self.refused("the work directory", str(self.zip), "--work", str(self.o / "X_mesh" / "w"))
        self.assertTrue(inside.exists())

    def test_the_charts_may_not_be_a_file_the_build_replaces(self):
        for name in ("X_mesh.json", "X_mesh.tar.zst"):
            shutil.copy(h.FIXTURE_ZIP, self.o / name)
            self.refused("a file the build replaces", str(self.o / name))
            self.assertTrue((self.o / name).exists())

    def test_a_charts_folder_may_not_hold_what_the_build_removes_or_writes(self):
        with zipfile.ZipFile(h.FIXTURE_ZIP) as z:
            z.extractall(self.o / "src")
        self.refused("and the build removes that folder", str(self.o))       # the output directory as the charts
        self.refused("holds the work directory", str(self.o / "src"), "--work", str(self.o / "src" / "w"))
        self.refused("holds the work directory", str(self.o / "src"), "--work", str(self.o / "src"))


if __name__ == "__main__":
    unittest.main()
