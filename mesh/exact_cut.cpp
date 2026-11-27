// The mesh build's exact cutter, run once per tile by mesh/build.py. Exact cutting of one tile (CGAL exact
// kernel: crossing points are computed exactly, nothing is rounded while cutting), with every triangle marked
// from the cut itself. Uses CGAL's 2D triangulation package (GPL-3.0-or-later).
//
// Input (binary, little-endian):
//   4 doubles           the tile box x0 y0 x1 y1 (its outline is cut in too, so no triangle straddles it)
//   int64 nrings
//   per ring: int32 shape, int32 n, n x 2 doubles (closed: first corner repeated at the end)
// Whole shapes are given, not shapes trimmed to the tile, so neighbouring tiles cut the very same lines.
//
// Marking: starting from outside everything, crossing a piece of line flips "inside" for each shape that line
// belongs to. A triangle's state is the set of shapes it is inside.
//
// Output (binary): int64 nt, nv, nstates, nids, clash;
//   nv x 2 doubles (corners, written as ordinary numbers only here), nt x 3 int32 corners,
//   nt x 3 int32 neighbours (neighbour k is across the side opposite corner k; -1 = none inside the tile box),
//   nt int32 state, (nstates + 1) int64 offsets, nids int32 shape ids,
//   nt x 3 int32 line: for the side opposite corner k, the number of the input segment it lies on (the lowest
//   when several; segments are numbered in input order, the tile box's four sides last), -1 = on no input line;
//   nt x 3 int32 line2: the same among chart lines only (rings given with shape <= -2), -1 = on none;
//   nv int8: per corner, -1, or >= 0 where a chart point (a ring of one corner) lies there;
//   int64 nl; (nt x 3 + 1) int64 offsets; nl int32: for each side, in order, every chart-line input segment it
//   lies on (side q's run is ids[offsets[q] .. offsets[q + 1])).
// mesh/build.py process_tile reads exactly this layout.
// Every input segment is given to CGAL as its own constraint, so each piece of line knows its input segment.
// Only triangles inside the tile box are written.
#include <CGAL/Exact_predicates_exact_constructions_kernel.h>
#include <CGAL/Constrained_Delaunay_triangulation_2.h>
#include <CGAL/Constrained_triangulation_plus_2.h>
#include <CGAL/Triangulation_face_base_with_info_2.h>
#include <CGAL/Triangulation_vertex_base_with_info_2.h>
#include <cstdio>
#include <cstdint>
#include <map>
#include <vector>
#include <algorithm>
#include <deque>
#include <set>

typedef CGAL::Exact_predicates_exact_constructions_kernel K;
typedef CGAL::Triangulation_vertex_base_with_info_2<int, K> Vb;
typedef CGAL::Triangulation_face_base_with_info_2<int, K> Fbi;
typedef CGAL::Constrained_triangulation_face_base_2<K, Fbi> Fb;
typedef CGAL::Triangulation_data_structure_2<Vb, Fb> TDS;
typedef CGAL::Exact_intersections_tag Itag;
typedef CGAL::Constrained_Delaunay_triangulation_2<K, TDS, Itag> CDT;
typedef CGAL::Constrained_triangulation_plus_2<CDT> CDTP;
typedef CDTP::Point Point;
typedef CDTP::Vertex_handle Vh;
typedef CDTP::Face_handle Fh;

static bool rd(FILE* f, void* p, size_t n) { return fread(p, 1, n, f) == n; }
static bool wr(FILE* f, const void* p, size_t size, size_t n) { return n == 0 || fwrite(p, size, n, f) == n; }

int main(int argc, char** argv) {
  if (argc < 3) { fprintf(stderr, "usage: exact_cut in out\n"); return 1; }
  FILE* in = fopen(argv[1], "rb");
  if (!in) { perror("in"); return 1; }
  double box[4];
  int64_t nr;
  if (!rd(in, box, 32) || !rd(in, &nr, 8)) return 1;
  double lo[2] = {box[0], box[1]}, hi[2] = {box[2], box[3]};
  std::vector<std::vector<Point> > rings(nr);
  std::vector<int> ring_shape(nr);
  for (int64_t r = 0; r < nr; ++r) {
    int32_t sid, n;
    if (!rd(in, &sid, 4) || !rd(in, &n, 4)) return 1;
    std::vector<double> c(2 * (size_t)n);
    if (!rd(in, c.data(), 16 * (size_t)n)) return 1;
    ring_shape[r] = sid;
    rings[r].reserve(n);
    for (int32_t k = 0; k < n; ++k) {
      rings[r].push_back(Point(c[2 * k], c[2 * k + 1]));
      lo[0] = std::min(lo[0], c[2 * k]); hi[0] = std::max(hi[0], c[2 * k]);
      lo[1] = std::min(lo[1], c[2 * k + 1]); hi[1] = std::max(hi[1], c[2 * k + 1]);
    }
  }
  fclose(in);
  CDTP cdt;
  double m = 1000.0 + 0.01 * std::max(hi[0] - lo[0], hi[1] - lo[1]);
  cdt.insert(Point(lo[0] - m, lo[1] - m)); cdt.insert(Point(hi[0] + m, lo[1] - m));
  cdt.insert(Point(hi[0] + m, hi[1] + m)); cdt.insert(Point(lo[0] - m, hi[1] + m));
  // a "ring" of one corner is a chart point: a corner of the cut, nothing else
  std::map<Vh, int> point_rank;                 // corner -> lowest band rank among the chart points there
  for (int64_t r = 0; r < nr; ++r)
    if (rings[r].size() == 1) {
      Vh v = cdt.insert(rings[r][0]);
      int rank = -2 - ring_shape[r];
      auto it = point_rank.find(v);
      if (it == point_rank.end() || rank < it->second) point_rank[v] = rank;
    }
  std::vector<std::pair<CDTP::Constraint_id, int> > cons;      // one per input segment, in input order; shape, or < 0
  for (int64_t r = 0; r < nr; ++r)
    for (size_t k = 0; k + 1 < rings[r].size(); ++k)
      if (rings[r][k] != rings[r][k + 1])
        cons.push_back(std::make_pair(cdt.insert_constraint(rings[r][k], rings[r][k + 1]), ring_shape[r]));
  rings.clear();
  {
    Point b[5] = {Point(box[0], box[1]), Point(box[2], box[1]), Point(box[2], box[3]), Point(box[0], box[3]), Point(box[0], box[1])};
    for (int k = 0; k < 4; ++k) cons.push_back(std::make_pair(cdt.insert_constraint(b[k], b[k + 1]), -1));
  }
  std::map<std::pair<Vh, Vh>, std::vector<int> > E;            // piece of line -> shapes whose outline it is on
  std::map<std::pair<Vh, Vh>, int> ELINE;                      // piece of line -> lowest input segment number
  std::map<std::pair<Vh, Vh>, int> ELINE2;                     // the same, among chart LINES only (shape <= -2)
  std::map<std::pair<Vh, Vh>, std::vector<int> > ELINES;       // piece of line -> every chart-line input segment on it
  for (size_t c = 0; c < cons.size(); ++c) {
    Vh prev; bool have = false;
    for (auto it = cdt.vertices_in_constraint_begin(cons[c].first); it != cdt.vertices_in_constraint_end(cons[c].first); ++it) {
      Vh v = *it;
      if (have && v != prev) {
        std::pair<Vh, Vh> key = prev < v ? std::make_pair(prev, v) : std::make_pair(v, prev);
        if (cons[c].second >= 0) E[key].push_back(cons[c].second);
        if (ELINE.find(key) == ELINE.end()) ELINE[key] = (int)c;
        if (cons[c].second <= -2 && ELINE2.find(key) == ELINE2.end()) ELINE2[key] = (int)c;
        if (cons[c].second <= -2) ELINES[key].push_back((int)c);
      }
      prev = v; have = true;
    }
  }
  // states, interned
  std::map<std::vector<int>, int> intern;
  std::vector<std::vector<int> > states;
  auto state_id = [&](const std::vector<int>& s) {
    auto it = intern.find(s);
    if (it != intern.end()) return it->second;
    int id = (int)states.size();
    intern[s] = id; states.push_back(s);
    return id;
  };
  int nf = 0;
  for (auto f = cdt.all_faces_begin(); f != cdt.all_faces_end(); ++f) f->info() = nf++;
  std::vector<int> fstate(nf, -1);
  int64_t clash = 0;
  std::deque<Fh> q;
  Fh f0 = cdt.infinite_face();
  fstate[f0->info()] = state_id(std::vector<int>());
  q.push_back(f0);
  while (!q.empty()) {
    Fh f = q.front(); q.pop_front();
    for (int i = 0; i < 3; ++i) {
      Fh nb = f->neighbor(i);
      Vh a = f->vertex(cdt.cw(i)), b = f->vertex(cdt.ccw(i));
      int ns = fstate[f->info()];
      if (!cdt.is_infinite(a) && !cdt.is_infinite(b)) {
        auto it = E.find(a < b ? std::make_pair(a, b) : std::make_pair(b, a));
        if (it != E.end()) {
          std::vector<int> nxt = states[ns];
          for (int sid : it->second) {
            auto p = std::lower_bound(nxt.begin(), nxt.end(), sid);
            if (p != nxt.end() && *p == sid) nxt.erase(p); else nxt.insert(p, sid);
          }
          ns = state_id(nxt);
        }
      }
      if (fstate[nb->info()] < 0) { fstate[nb->info()] = ns; q.push_back(nb); }
      else if (fstate[nb->info()] != ns) ++clash;
    }
  }
  // triangles inside the tile box (exact test of the centre)
  K::FT bx0(box[0]), by0(box[1]), bx1(box[2]), by1(box[3]);
  std::vector<int> keep(nf, -1);
  std::vector<Fh> kept;
  for (auto f = cdt.finite_faces_begin(); f != cdt.finite_faces_end(); ++f) {
    Point c = CGAL::centroid(f->vertex(0)->point(), f->vertex(1)->point(), f->vertex(2)->point());
    if (c.x() > bx0 && c.x() < bx1 && c.y() > by0 && c.y() < by1) { keep[f->info()] = (int)kept.size(); kept.push_back(f); }
  }
  for (auto v = cdt.finite_vertices_begin(); v != cdt.finite_vertices_end(); ++v) v->info() = -1;
  std::vector<double> xy;
  std::vector<int32_t> tri, nbr, st, line, line2, lids;
  std::vector<int64_t> loff(1, 0);
  std::vector<signed char> vflag;
  // states used, renumbered
  std::vector<int> smap(states.size(), -1);
  std::vector<int> sused;
  for (Fh f : kept) {
    for (int k = 0; k < 3; ++k) {
      Vh v = f->vertex(k);
      if (v->info() < 0) {
        v->info() = (int)(xy.size() / 2);
        xy.push_back(CGAL::to_double(v->point().x().exact())); xy.push_back(CGAL::to_double(v->point().y().exact()));
        { auto pr = point_rank.find(v); vflag.push_back(pr == point_rank.end() ? (signed char)-1 : (signed char)pr->second); }
      }
      tri.push_back(v->info());
      Fh nb = f->neighbor(k);
      nbr.push_back(cdt.is_infinite(nb) ? -1 : keep[nb->info()]);
      Vh ea = f->vertex(cdt.cw(k)), eb = f->vertex(cdt.ccw(k));
      auto fl = ELINE.find(ea < eb ? std::make_pair(ea, eb) : std::make_pair(eb, ea));
      line.push_back(fl == ELINE.end() ? -1 : fl->second);
      auto fl2 = ELINE2.find(ea < eb ? std::make_pair(ea, eb) : std::make_pair(eb, ea));
      line2.push_back(fl2 == ELINE2.end() ? -1 : fl2->second);
      auto fls = ELINES.find(ea < eb ? std::make_pair(ea, eb) : std::make_pair(eb, ea));
      if (fls != ELINES.end()) for (int id : fls->second) lids.push_back(id);
      loff.push_back((int64_t)lids.size());
    }
    int s = fstate[f->info()];
    if (smap[s] < 0) { smap[s] = (int)sused.size(); sused.push_back(s); }
    st.push_back(smap[s]);
  }
  std::vector<int64_t> off(1, 0);
  std::vector<int32_t> ids;
  for (int s : sused) { for (int sid : states[s]) ids.push_back(sid); off.push_back((int64_t)ids.size()); }
  FILE* out = fopen(argv[2], "wb");
  if (!out) { perror("out"); return 1; }
  int64_t hdr[5] = {(int64_t)kept.size(), (int64_t)(xy.size() / 2), (int64_t)sused.size(), (int64_t)ids.size(), clash / 2};
  int64_t nl = (int64_t)lids.size();
  // Every write is checked, and so is the close (which flushes the last of it): a full disk must not leave a
  // short file behind a zero exit status.
  bool ok = wr(out, hdr, 8, 5)
    && wr(out, xy.data(), 8, xy.size())
    && wr(out, tri.data(), 4, tri.size())
    && wr(out, nbr.data(), 4, nbr.size())
    && wr(out, st.data(), 4, st.size())
    && wr(out, off.data(), 8, off.size())
    && wr(out, ids.data(), 4, ids.size())
    && wr(out, line.data(), 4, line.size())
    && wr(out, line2.data(), 4, line2.size())      // per side: lowest input segment number among chart lines, -1 = none
    && wr(out, vflag.data(), 1, vflag.size())      // per corner: band rank of the most detailed chart point there, -1 = none
    && wr(out, &nl, 8, 1)
    && wr(out, loff.data(), 8, loff.size())        // nt*3 + 1 offsets into the ids below, one run per side
    && wr(out, lids.data(), 4, lids.size());       // every chart-line input segment each side lies on
  if (fclose(out) != 0) ok = false;
  if (!ok) { fprintf(stderr, "exact_cut: writing %s failed\n", argv[2]); return 1; }
  return 0;
}
