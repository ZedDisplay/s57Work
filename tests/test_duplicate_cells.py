"""The by-band inventory holds one copy of each cell (s57-to-mbtiles.py
one_copy_per_cell, latest_copy, stage_band_cells).

Several inputs can carry the same cell: a district zip and the state zips
overlap, and inputs downloaded on different days can hold different
editions of it. What is pinned:
  * the copy kept is the latest issued (DSID ISDT), not the highest
    edition: a cancellation has edition 0 and is the newest state;
  * copies that cannot be decided between stop the run, naming the cell;
  * a cell's band copy is exactly its source: an update file left from an
    earlier copy goes;
  * end to end on the five-cell fixture given twice, once whole and once
    with the cancelled cell's cancellation update removed: every cell is
    exported and listed once, and the cancelled cell stays cancelled.
"""
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests import helpers as h

p = h.pipeline()


def info(isdt, edtn, updn="0"):
    return {"ISDT": isdt, "EDTN": edtn, "UPDN": updn}


class LatestCopy(unittest.TestCase):
    def test_the_latest_issued_wins(self):
        a, b = Path("a/US5XX1AA.000"), Path("b/US5XX1AA.000")
        self.assertEqual(p.latest_copy("US5XX1AA", [(a, info("20260301", "3", "22")), (b, info("20260907", "4", "1"))]), b)
        self.assertEqual(p.latest_copy("US5XX1AA", [(b, info("20260907", "4", "1")), (a, info("20260301", "3", "22"))]), b)

    def test_a_cancellation_is_the_newest_state_though_its_edition_is_zero(self):
        live, cancelled = Path("a/US2EC04M.000"), Path("b/US2EC04M.000")
        self.assertEqual(p.latest_copy("US2EC04M", [(live, info("20260301", "6", "22")), (cancelled, info("20260724", "0", "1"))]),
                         cancelled)

    def test_copies_in_the_same_state_are_the_same_cell(self):
        a, b = Path("a/US5XX1AA.000"), Path("b/US5XX1AA.000")
        self.assertEqual(p.latest_copy("US5XX1AA", [(a, info("20260907", "4", "1")), (b, info("20260907", "4", "1"))]), a)

    def test_copies_that_cannot_be_decided_between_stop(self):
        a, b = Path("a/US5XX1AA.000"), Path("b/US5XX1AA.000")
        with self.assertRaises(ValueError) as cm:
            p.latest_copy("US5XX1AA", [(a, info("20260907", "4", "1")), (b, info("20260907", "4", "2"))])
        self.assertIn("US5XX1AA: copies issued the same day (20260907) in different states (4.1, 4.2)", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            p.latest_copy("US5XX1AA", [(a, info(None, "4")), (b, info("20260907", "4"))])
        self.assertIn("the issue date of a copy cannot be read", str(cm.exception))


class BandCopy(unittest.TestCase):
    def test_the_band_copy_is_exactly_the_source(self):
        with tempfile.TemporaryDirectory(prefix="s57bandcopy-") as tmp:
            old, new, band = Path(tmp) / "old", Path(tmp) / "new", Path(tmp) / "band4"
            for d, names in ((old, ["000", "001", "002", "003"]), (new, ["000", "001"])):
                d.mkdir()
                for ext in names:
                    (d / f"US4XX1AA.{ext}").write_text(f"{d.name} {ext}")
            p.stage_band_cells([old / "US4XX1AA.000"], band)
            self.assertEqual(sorted(f.name for f in band.iterdir()), [f"US4XX1AA.{e}" for e in ("000", "001", "002", "003")])
            # a later run builds the cell from another copy: a new edition with one update
            p.stage_band_cells([new / "US4XX1AA.000"], band)
            self.assertEqual(sorted(f.name for f in band.iterdir()), ["US4XX1AA.000", "US4XX1AA.001"])
            self.assertEqual([(band / f"US4XX1AA.{e}").read_text() for e in ("000", "001")], ["new 000", "new 001"],
                             "the copy is the source's, whichever file is newer on disk")


@unittest.skipUnless(h.HAVE_GDAL, "needs ogr2ogr/ogrinfo")
class TheSameCellInTwoInputs(unittest.TestCase):
    def test_each_cell_once_and_the_latest_copy(self):
        work = Path(tempfile.mkdtemp(prefix="s57twice-"))
        try:
            whole = work / "WHOLE_ENCs.zip"
            shutil.copy(h.FIXTURE_ZIP, whole)
            # the same cells, but the cancelled cell without its cancellation update: an older copy of it
            older = work / "OLDER_ENCs.zip"
            with zipfile.ZipFile(h.FIXTURE_ZIP) as src, zipfile.ZipFile(older, "w") as dst:
                for m in src.infolist():
                    if os.path.basename(m.filename) != f"{h.FIXTURE_CANCELLED}.001":
                        dst.writestr(m, src.read(m))
            r = h.run_pipeline(work, str(older), str(whole), "--by-band", "--no-replacements", "--cells-only", "-j", "2")
            self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
            self.assertIn("5 cell(s) are in more than one input", r.stdout)
            listed = json.loads((work / "data" / "cells" / "cells.json").read_text())
            names = [c["cell"] for c in listed["cells"]]
            self.assertEqual(sorted(names), sorted((h.FIXTURE_BAND4,) + h.FIXTURE_BAND5), "each cell once")
            self.assertEqual(listed["cancelled_cells_left_out"], [h.FIXTURE_CANCELLED],
                             "the latest copy of the cancelled cell is its cancellation")
            record = json.loads((work / "data" / "cells-work" / "duplicate-cells.json").read_text())
            kept = {c["cell"]: c["kept"] for c in record["cells"]}
            self.assertEqual(len(kept), 5)
            self.assertIn("/input1/", kept[h.FIXTURE_CANCELLED], "the copy from the whole zip, given second")
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
