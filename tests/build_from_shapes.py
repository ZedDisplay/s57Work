"""Run the mesh build on hand-made cells: python -m tests.build_from_shapes SPEC.json (tests/helpers.py
build_from_shapes writes the spec). The cells are given as layer files already written, in place of the charts
build-mesh.py reads; everything after the reading is mesh.cli.build_mesh, as build-mesh.py runs it."""
import json
import os
import sys

from mesh import cli


class GivenCells:
    """Stands where mesh.cells.Charts stands in mesh.cli.build_mesh: the cells, as cell_index's records."""

    def __init__(self, cells):
        self._cells = [(band, name, "shapes", files) for band, name, files in cells]

    def cells(self):
        return self._cells

    def record(self, index):
        return {"given": "shapes", "cells_read": len(index)}


def main(spec_path):
    with open(spec_path) as f:
        spec = json.load(f)
    chart_meta = None
    if spec["chart_json"]:
        with open(spec["chart_json"]) as f:
            chart_meta = json.load(f)
    return cli.build_mesh(spec["name"], os.path.realpath(spec["out"]), os.path.realpath(spec["work"]), spec["workers"],
                          lambda run_dir, log: GivenCells(spec["cells"]), chart_meta=chart_meta,
                          archive=spec["archive"], keep=spec["keep"], log=lambda m="": print(m, flush=True))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
