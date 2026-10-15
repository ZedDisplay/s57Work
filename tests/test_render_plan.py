"""The finer-wins render plan: which zooms each source is tiled at, and
whose footprints are erased from it first. These expectations are the
runs the real 01CGD build produces (band + gap fill), so a change here
changes what ships."""
import unittest
from pathlib import Path

from tests.helpers import pipeline

s57 = pipeline()


def source(label, priority, zmin, zmax, native=None):
    return s57.RenderSource(priority=priority, label=label,
                            merged_dir=Path(f"/m/{label}"), footprint_files=[],
                            zoom_range=(zmin, zmax), layer_rules={},
                            native_zoom=native)


def plan():
    return [source("band2", 2, 9, 12, (9, 10)),
            source("band3", 3, 11, 14, (11, 12)),
            source("band4", 4, 13, 16, (13, 14)),
            source("band5", 5, 15, 16, (15, 16)),
            source("fill", 4.9, 9, 16)]


def runs_by_label(runs):
    out = {}
    for r in runs:
        out.setdefault(r.source.label, []).append(
            (r.zoom_range, sorted(e.label for e in r.erased_by)))
    return out


class PriorityAt(unittest.TestCase):
    def test_native_band_outranks_everything(self):
        band2 = source("band2", 2, 9, 12, (9, 10))
        fill = source("fill", 4.9, 9, 16)
        self.assertGreater(s57.priority_at(band2, 9), s57.priority_at(fill, 9))
        self.assertLess(s57.priority_at(band2, 11), s57.priority_at(fill, 11))

    def test_fill_beats_coarser_bands_loses_to_its_own_band(self):
        band4 = source("band4", 4, 13, 16, (13, 14))
        fill = source("fill", 4.9, 9, 16)
        self.assertLess(s57.priority_at(fill, 13), s57.priority_at(band4, 13))
        self.assertGreater(s57.priority_at(fill, 15), s57.priority_at(band4, 15))


class DownwardZooms(unittest.TestCase):
    """BAND_ZOOM_DOWNWARD: a band also renders below its native floor,
    ranked by its band like an extension: the native band wins, and in
    a hole the finer chart beats a coarser band's extension."""

    def test_downward_zooms_lose_to_native_beat_coarser_extensions(self):
        band3 = source("band3", 3, 9, 14, (11, 12))
        band1 = source("band1", 1, 9, 10, (7, 8))
        band2 = source("band2", 2, 9, 12, (9, 10))
        fill = source("fill", 4.9, 9, 16)
        self.assertGreater(s57.priority_at(band2, 9), s57.priority_at(band3, 9))
        self.assertGreater(s57.priority_at(fill, 9), s57.priority_at(band3, 9))
        self.assertGreater(s57.priority_at(band3, 9), s57.priority_at(band1, 9))
        # z13-14 with band 3 and band 5 but no band 4: band 5 wins.
        band5 = source("band5", 5, 13, 16, (15, 16))
        self.assertGreater(s57.priority_at(band5, 13), s57.priority_at(band3, 13))

    def test_band_render_range(self):
        self.assertEqual(s57.band_render_range(1, 9, 16), (9, 10))
        self.assertEqual(s57.band_render_range(2, 9, 16), (9, 12))   # capped at minzoom
        self.assertEqual(s57.band_render_range(3, 9, 16), (9, 14))
        self.assertEqual(s57.band_render_range(4, 9, 16), (11, 16))
        self.assertEqual(s57.band_render_range(5, 9, 16), (13, 16))  # capped at maxzoom
        lo, hi = s57.band_render_range(6, 9, 16)                     # floor above maxzoom
        self.assertGreater(lo, hi)
        self.assertEqual(s57.band_render_range(6, 9, 18), (15, 18))

    def test_downward_run_is_erased_by_native_and_finer_sources(self):
        runs = runs_by_label(s57.plan_render_runs([
            source("band1", 1, 9, 10, (7, 8)),
            source("band2", 2, 9, 12, (9, 10)),
            source("band3", 3, 9, 14, (11, 12)),
            source("band4", 4, 11, 16, (13, 14))]))
        self.assertEqual(runs["band3"][0], ((9, 10), ["band2"]))
        self.assertEqual(runs["band4"][0], ((11, 12), ["band3"]))
        # The coarser extensions give way to the finer downward zooms.
        self.assertEqual(runs["band1"], [((9, 10), ["band2", "band3"])])
        self.assertIn(((11, 12), ["band3", "band4"]), runs["band2"])
        # Native zooms stay unerased.
        self.assertIn(((11, 12), []), runs["band3"])
        self.assertIn(((13, 16), []), runs["band4"])  # no band 5 here: one native+extension run


class PlanRenderRuns(unittest.TestCase):
    def test_runs_match_the_district_plan(self):
        got = runs_by_label(s57.plan_render_runs(plan()))
        self.assertEqual(got["band2"], [((9, 10), []),
                                        ((11, 12), ["band3", "fill"])])
        self.assertEqual(got["band3"], [((11, 12), []),
                                        ((13, 14), ["band4", "fill"])])
        self.assertEqual(got["band4"], [((13, 14), []),
                                        ((15, 16), ["band5", "fill"])])
        self.assertEqual(got["band5"], [((15, 16), [])])
        self.assertEqual(got["fill"], [((9, 10), ["band2"]),
                                       ((11, 12), ["band3"]),
                                       ((13, 14), ["band4"]),
                                       ((15, 16), ["band5"])])

    def test_native_zooms_are_never_erased(self):
        for r in s57.plan_render_runs(plan()):
            nz = r.source.native_zoom
            if nz and nz[0] <= r.zoom_range[0] <= nz[1]:
                self.assertEqual(r.erased_by, [], r.stem)

    def test_runs_sorted_coarse_to_fine(self):
        runs = s57.plan_render_runs(plan())
        keys = [(r.source.priority, r.zoom_range[0]) for r in runs]
        self.assertEqual(keys, sorted(keys))

    def test_stem_names_zoom_subrange_and_erasers(self):
        stems = {r.stem for r in s57.plan_render_runs(plan())}
        # A source tiled in one piece keeps its bare label; a sub-range
        # names its zooms, and an erased run names its erasers.
        self.assertIn("band5", stems)
        self.assertIn("band4_z13-14", stems)
        self.assertIn("band4_z15-16.minus-band5+fill", stems)
        self.assertIn("fill_z9-10.minus-band2", stems)
        self.assertNotIn("band4", stems)

    def test_single_source_is_one_untouched_run(self):
        runs = s57.plan_render_runs([source("band5", 5, 15, 16, (15, 16))])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].zoom_range, (15, 16))
        self.assertEqual(runs[0].erased_by, [])
        self.assertEqual(runs[0].stem, "band5")


if __name__ == "__main__":
    unittest.main()
