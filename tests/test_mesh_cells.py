"""The mesh build's first stage: reading the district's charts (mesh/cells.py).

A district's charts can come in several files that carry the same cell
(a district zip and a state zip overlap, and zips downloaded on
different days can hold different editions of it). What is pinned:
  * the copy kept is the latest issued (DSID ISDT), not the highest
    edition: a cancellation has edition 0 and is the newest state;
  * copies that cannot be decided between stop the build, naming the cell;
  * on the five-cell fixture given twice, once without the cancelled
    cell's cancellation update and once whole: every cell is read once,
    and the cancelled cell stays cancelled;
  * what is extracted: of every live cell, the layers the mesh reads;
  * a .000 file that is not named as a cell is listed and not read; a
    zip is unpacked into the run's folder and a folder is read in place.

The Charts tests need GDAL and the mesh packages; they skip without.
"""
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests import helpers as h

try:
    from mesh import build as B
    from mesh import cells as C
    HAVE = True
except ImportError:          # numpy / shapely / scipy missing
    HAVE = False

LIVE = sorted((h.FIXTURE_BAND4,) + h.FIXTURE_BAND5)


def copy(path, issued, edition, update="0"):
    return C.Copy("US5XX1AA", path, 0, edition=edition, update=update, issued=issued)


@unittest.skipUnless(HAVE, "mesh packages not installed")
class LatestCopy(unittest.TestCase):
    def test_the_latest_issued_wins(self):
        a, b = copy("a/US5XX1AA.000", "20260301", "3", "22"), copy("b/US5XX1AA.000", "20260907", "4", "1")
        self.assertIs(C.latest_copy("US5XX1AA", [a, b]), b)
        self.assertIs(C.latest_copy("US5XX1AA", [b, a]), b)

    def test_a_cancellation_is_the_newest_state_though_its_edition_is_zero(self):
        live, cancelled = copy("a/US2EC04M.000", "20260301", "6", "22"), copy("b/US2EC04M.000", "20260724", "0", "1")
        self.assertIs(C.latest_copy("US2EC04M", [live, cancelled]), cancelled)

    def test_copies_in_the_same_state_are_the_same_cell(self):
        a, b = copy("a/US5XX1AA.000", "20260907", "4", "1"), copy("b/US5XX1AA.000", "20260907", "4", "1")
        self.assertIs(C.latest_copy("US5XX1AA", [a, b]), a)

    def test_copies_that_cannot_be_decided_between_stop(self):
        a, b = copy("a/US5XX1AA.000", "20260907", "4", "1"), copy("b/US5XX1AA.000", "20260907", "4", "2")
        with self.assertRaises(C.ChartsError) as cm:
            C.latest_copy("US5XX1AA", [a, b])
        self.assertIn("US5XX1AA: copies issued the same day (20260907) in different states (4.1, 4.2)", str(cm.exception))
        with self.assertRaises(C.ChartsError) as cm:
            C.latest_copy("US5XX1AA", [copy("a/US5XX1AA.000", None, "4"), b])
        self.assertIn("the issue date of a copy cannot be read (a/US5XX1AA.000)", str(cm.exception))


@unittest.skipUnless(HAVE and h.HAVE_GDAL, "needs ogr2ogr/ogrinfo and the mesh packages")
class ReadingTheCharts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="s57charts-"))
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        self.said = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def zip_without(self, name, left_out):
        """A zip of the fixture without the files named."""
        dest = self.tmp / name
        with zipfile.ZipFile(h.FIXTURE_ZIP) as src, zipfile.ZipFile(dest, "w") as dst:
            for m in src.infolist():
                if os.path.basename(m.filename) not in left_out:
                    dst.writestr(m, src.read(m))
        return str(dest)

    def charts(self, *inputs):
        return C.Charts(list(inputs), str(self.run_dir), 2, self.said.append)

    def test_the_same_cell_in_two_inputs(self):
        # the cancelled cell without its cancellation update is an older copy of it, here given first
        older = self.zip_without("OLDER_ENCs.zip", {f"{h.FIXTURE_CANCELLED}.001"})
        whole = self.zip_without("WHOLE_ENCs.zip", set())
        ch = self.charts(older, whole)
        self.assertEqual(sorted(c.name for c in ch.kept), LIVE, "each cell once")
        self.assertEqual(ch.cancelled, [h.FIXTURE_CANCELLED], "the latest copy of the cancelled cell is its cancellation")
        self.assertEqual(ch.several, 5)
        self.assertEqual(ch.found, {0: 5, 1: 5})

    def test_what_is_extracted(self):
        whole = self.zip_without("WHOLE_ENCs.zip", set())
        ch = self.charts(whole)
        records = ch.cells()
        self.assertEqual(sorted((r[0], r[1], r[2]) for r in records),
                         sorted([("band4", h.FIXTURE_BAND4, whole)] + [("band5", c, whole) for c in h.FIXTURE_BAND5]))
        for band, name, source, files in records:
            self.assertIn("DEPARE", files)
            self.assertTrue(set(files) <= set(B.LAYERS), "only the layers the mesh reads")
            for layer, f in files.items():
                self.assertEqual(Path(f), self.run_dir / "cells" / name / f"{layer}.geojson")
                self.assertEqual(json.loads(Path(f).read_text())["type"], "FeatureCollection")
        self.assertEqual(sorted(p.name for p in (self.run_dir / "cells").iterdir()), LIVE, "the cancelled cell is not extracted")
        rec = ch.record(B.cell_index(records, 1))
        self.assertEqual(rec["inputs"], [{"file": "WHOLE_ENCs.zip", "bytes": os.path.getsize(whole), "cell_files_found": 5,
                                          "files_not_read": []}])
        self.assertEqual((rec["live_cells"], rec["cells_read"], rec["cells_read_by_band"]), (4, 4, {"band4": 1, "band5": 3}))
        self.assertEqual(rec["cancelled_cells_left_out"], [h.FIXTURE_CANCELLED])
        self.assertEqual(rec["cells_given_more_than_once"], 0)

    def test_a_folder_is_read_in_place_and_a_file_that_is_no_cell_is_listed(self):
        src = self.tmp / "src"
        with zipfile.ZipFile(h.FIXTURE_ZIP) as z:
            z.extractall(src)
        (src / "ENC_ROOT" / "NOTES.000").write_text("not a cell\n")
        before = sorted(str(p.relative_to(src)) for p in src.rglob("*"))
        ch = self.charts(str(src))
        self.assertEqual(ch.not_cells, {0: [os.path.join("ENC_ROOT", "NOTES.000")]})
        self.assertEqual(len(ch.kept), 4)
        self.assertTrue(all(c.enc.startswith(str(src)) for c in ch.kept), "read where it lies")
        ch.cells()
        self.assertEqual(sorted(str(p.relative_to(src)) for p in src.rglob("*")), before, "nothing was written into it")
        self.assertFalse((self.run_dir / "charts").exists(), "nothing was unpacked")
        self.assertTrue(any("NOTES.000" in line for line in self.said))

    def test_what_cannot_be_read_stops_the_build(self):
        src = self.tmp / "src"
        with zipfile.ZipFile(h.FIXTURE_ZIP) as z:
            z.extractall(src)
        (src / "ENC_ROOT" / "US5ZZ9ZZ.000").write_text("this is not an S-57 cell\n")
        with self.assertRaises(C.ChartsError) as cm:
            self.charts(str(src))
        self.assertIn("1 cell(s) cannot be read", str(cm.exception))
        self.assertIn("US5ZZ9ZZ", str(cm.exception))
        nozip = self.tmp / "X_ENCs.zip"
        nozip.write_text("no zip\n")
        with self.assertRaises(C.ChartsError) as cm:
            self.charts(str(nozip))
        self.assertIn("is neither a zip nor a folder", str(cm.exception))

    def test_an_update_file_that_is_not_applied_stops_the_build(self):
        # the cancelled cell's only update, .001, renamed .002: .001 is missing, so .002 cannot be applied
        src = self.tmp / "src"
        with zipfile.ZipFile(h.FIXTURE_ZIP) as z:
            z.extractall(src)
        one, = src.rglob(f"{h.FIXTURE_CANCELLED}.001")
        one.rename(one.with_suffix(".002"))
        with self.assertRaises(C.ChartsError) as cm:
            self.charts(str(src))
        self.assertIn("1 cell(s) cannot be read", str(cm.exception))
        self.assertIn(h.FIXTURE_CANCELLED, str(cm.exception))
        self.assertEqual(C.update_files(str(one.with_suffix(".000"))), [2])

    def test_the_update_files_and_numbers_read(self):
        # the cancelled cell: base update number 0, its one update .001 applied
        ch = self.charts(self.zip_without("WHOLE_ENCs.zip", set()))
        self.assertEqual(ch.cancelled, [h.FIXTURE_CANCELLED])
        enc, = (self.run_dir / "charts").rglob(f"{h.FIXTURE_CANCELLED}.000")
        self.assertEqual(C.update_files(str(enc)), [1])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root lists any folder")
    def test_a_folder_that_cannot_be_listed_stops_the_build(self):
        src = self.tmp / "src"
        with zipfile.ZipFile(h.FIXTURE_ZIP) as z:
            z.extractall(src)
        shut = src / "ENC_ROOT" / "SHUT"
        shut.mkdir()
        shut.chmod(0)
        try:
            with self.assertRaises(C.ChartsError) as cm:
                self.charts(str(src))
        finally:
            shut.chmod(0o755)
        self.assertIn(f"1 folder(s) of {src} cannot be listed", str(cm.exception))
        self.assertIn(str(shut), str(cm.exception))


if __name__ == "__main__":
    unittest.main()
