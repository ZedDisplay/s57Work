"""The seam weld must not leave a vertex on the interior of a segment.

On 2026-10-10 one weld in New York Harbor moved a segment endpoint one
grid step and the segment then passed exactly through a neighbouring
vertex (a T-junction). Triangle's q20 refinement never terminated on it:
the four harbour squares refined in 7 s without the weld and not in 60 s
(16 GB) with it. `split_segments_through_vertices` splits such segments
at the vertex after the weld. Needs numpy and shapely; skips without.
"""
import unittest

try:
    import numpy as np
    from mesh import build as B
    HAVE = True
except ImportError:
    HAVE = False


@unittest.skipUnless(HAVE, "needs numpy and shapely")
class PostWeldSplit(unittest.TestCase):
    def tile(self, U, S):
        return {"U": np.array(U, dtype=float), "S": np.array(S, dtype=np.int64),
                "SP": np.arange(len(S), dtype=np.int64)}

    def test_segment_through_a_vertex_is_split_there(self):
        GS = B.GS
        # segment 0 from v0 to v1; v2 lies exactly on its interior (the
        # Harbor case after the weld); v3 is a separate endpoint.
        tp = self.tile([[0, 0], [8 * GS, 0], [6 * GS, 0], [6 * GS, 2 * GS]], [[0, 1], [2, 3]])
        n = B.split_segments_through_vertices(tp, {1})      # v1 was the welded vertex
        self.assertEqual(n, 1)
        segs = {tuple(sorted(s)) for s in tp["S"].tolist()}
        self.assertEqual(segs, {(0, 2), (1, 2), (2, 3)})
        self.assertEqual(len(tp["SP"]), 3)
        self.assertEqual(tp["SP"][2], tp["SP"][0], "the new piece keeps the split segment's part")

    def test_vertex_within_a_grid_step_counts(self):
        GS = B.GS
        tp = self.tile([[0, 0], [8 * GS, 0], [4 * GS, 0.5 * GS]], [[0, 1]])
        self.assertEqual(B.split_segments_through_vertices(tp, {0}), 1)

    def test_endpoints_and_far_vertices_are_left_alone(self):
        GS = B.GS
        tp = self.tile([[0, 0], [8 * GS, 0], [4 * GS, 3 * GS], [8 * GS, 0.4 * GS]], [[0, 1]])
        self.assertEqual(B.split_segments_through_vertices(tp, {0, 1}), 0)
        self.assertEqual(len(tp["S"]), 1)

    def test_only_segments_at_moved_vertices_are_examined(self):
        GS = B.GS
        tp = self.tile([[0, 0], [8 * GS, 0], [4 * GS, 0], [20 * GS, 20 * GS]], [[0, 1]])
        self.assertEqual(B.split_segments_through_vertices(tp, {3}), 0)
        self.assertEqual(B.split_segments_through_vertices(tp, set()), 0)

    def test_segment_collapsed_by_the_weld_is_skipped(self):
        # v1 was welded onto v0's coordinates: segment 0 has zero length;
        # v2 sits on it (distance 0) and must not cause a division by zero
        # (01CGD, 2026-10-10). The merge removes the segment later.
        GS = B.GS
        tp = self.tile([[0, 0], [0, 0], [0, 0], [5 * GS, 5 * GS]], [[0, 1], [2, 3]])
        self.assertEqual(B.split_segments_through_vertices(tp, {1}), 0)
        self.assertEqual(len(tp["S"]), 2)

    def test_two_vertices_on_one_segment_are_chained_in_order(self):
        GS = B.GS
        tp = self.tile([[0, 0], [10 * GS, 0], [7 * GS, 0], [3 * GS, 0]], [[0, 1]])
        self.assertEqual(B.split_segments_through_vertices(tp, {1}), 2)
        segs = {tuple(sorted(s)) for s in tp["S"].tolist()}
        self.assertEqual(segs, {(0, 3), (2, 3), (1, 2)})


if __name__ == "__main__":
    unittest.main()
