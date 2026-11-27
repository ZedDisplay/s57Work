"""Navigation mesh built from the chart source cells (S-57), cut exactly.

The mesh is a triangulation of charted water with, per triangle, the
chart's depth, clearance, hazard, structure, fairway and dredged values;
per triangle side, the mark of a chart line lying on it (a structure, a
hazard, an overhead clearance, a recommended track, a navigation line);
and per corner, the mark of a chart point there (a hazard with its depth,
a mark, a sounding). It is written as flat binary tiles (magic
``WRPMESH2``, index version 2) in a folder.

Build (``build-mesh.py``, mesh/cli.py):

0. ``cells``    - read the charts themselves (NOAA's zips of S-57 cells)
                  with GDAL: one copy of each cell, the latest issued;
                  cancelled cells left out; the layers the mesh reads
                  extracted, in a folder that is the run's own.
1. ``build``    - per 0.25-degree tile: read every chart cell that reaches
                  the tile, in the source's own whole numbers (units of
                  1e-7 degree); cut all of it exactly (exact_cut.cpp,
                  CGAL's exact kernel); take each triangle's values from
                  the shapes the cut says it is inside. The most detailed
                  chart wins. Chart lines stay lines, chart points stay
                  points; nothing is drawn around them.
2. ``finalize`` - join the tiles; decide once, for the whole mesh, which
                  points are real corners; one well-shaping run (Triangle
                  q20, given the outline and the chart points only); each
                  triangle's water from the cut; neighbours, side and
                  corner marks, the shore multiplier; the checks, every
                  one a gate, including every triangle's water against
                  the outline pieces' own record; the binary tiles and
                  index.json.

The mesh covers wherever the district's own cells have charted water.
Nothing here is district-specific.

exact_cut.cpp uses CGAL's 2D triangulation package, which is licensed
GPL-3.0-or-later; it is a separate program, run by mesh/build.py.
"""

MESH_FORMAT_MAGIC = "WRPMESH2"
MESH_INDEX_VERSION = 2
