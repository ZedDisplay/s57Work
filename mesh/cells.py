"""The mesh build, first stage: read the charts (see mesh/__init__.py for the whole build).

The build's input is the district's charts themselves: NOAA's zip (or zips, or folders) of S-57 cells. A cell is
US<band>XXXXX.000 with its update files .001, .002, ... beside it. For one run, in the run's own new folder,
this stage:

  1. unpacks each zip (a folder is read where it lies and is never written to);
  2. finds the cells. A .000 file whose name is not a NOAA cell name of bands 1 to 6 is not read, and is listed;
  3. reads each copy's edition, update number and issue date from the cell itself, updates applied;
  4. keeps one copy of each cell across the inputs, the latest issued (latest_copy);
  5. leaves out cancelled cells (edition 0 after updates: S-57 Appendix B.1, 5.7), and lists them;
  6. extracts each kept cell's layers that the mesh reads (mesh/build.py LAYERS) to GeoJSON, one file per layer.

A district's mesh is built from that district's charts and from nothing else: no other district's charts are
read, and nothing is downloaded. All reading is done by GDAL (ogrinfo, ogr2ogr), which applies a cell's update
files to its base. Nothing is kept from one run to the next and nothing of an earlier run is read: there is no
cache, no marker and no list. A cell GDAL cannot read, a cell whose state cannot be told, and copies that cannot
be decided between stop the build (ChartsError); nothing is left out silently.
"""
import json
import os
import re
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import build

CELL_NAME = re.compile(r"^US([1-6])[A-Z0-9]{5}$")
GDAL_MIN = (3, 7)                     # ogrinfo -json
SHOWN = 10                            # how many of a list of problems a message spells out


class ChartsError(RuntimeError):
    """The charts cannot be read as given; the message says what and where."""


@dataclass
class Copy:
    """One copy of a cell: where its .000 lies, which input it came from, its state, and (read for the copy
    that is kept) the names of its layers."""
    name: str
    enc: str
    source: int
    edition: str = None
    update: str = None
    issued: str = None
    layers: list = field(default_factory=list)

    @property
    def state(self):
        return f"{self.edition}.{self.update or 0}"

    @property
    def band(self):
        return f"band{self.name[2]}"


def latest_copy(name, copies):
    """Of several copies of one cell, the one to build from: the latest issued (the issue date of the base or of
    the last update applied). Not the highest edition: a cancellation is the newest state of a cell and has
    edition 0. A copy whose issue date cannot be read, or two copies issued the same day in different states
    (edition.update), cannot be decided between: ChartsError, naming the cell. Copies in the same state are the
    same cell; the first given is used."""
    unread = [c.enc for c in copies if not (c.issued and c.issued.isdigit() and len(c.issued) == 8)]
    if unread:
        raise ChartsError(f"{name}: the issue date of a copy cannot be read ({', '.join(unread)})")
    latest = max(c.issued for c in copies)
    at = [c for c in copies if c.issued == latest]
    states = sorted({c.state for c in at})
    if len(states) > 1:
        raise ChartsError(f"{name}: copies issued the same day ({latest}) in different states ({', '.join(states)}): "
                          + ", ".join(c.enc for c in at))
    return at[0]


def gdal(args):
    """Run a GDAL program; (exit status, standard output, last line of its error output)."""
    r = subprocess.run(args, capture_output=True, text=True)
    err = (r.stderr or "").strip().splitlines()
    return r.returncode, r.stdout, (err[-1] if err else "no message")


def check_gdal():
    """ogrinfo and ogr2ogr must be installed, and ogrinfo new enough to answer in JSON."""
    for exe in ("ogrinfo", "ogr2ogr"):
        if not shutil.which(exe):
            raise ChartsError(f"{exe} not found: the mesh build reads the charts with GDAL")
    rc, out, err = gdal(["ogrinfo", "--version"])
    m = re.search(r"GDAL (\d+)\.(\d+)", out)
    if rc != 0 or not m:
        raise ChartsError(f"ogrinfo --version could not be read: {err}")
    if (int(m.group(1)), int(m.group(2))) < GDAL_MIN:
        raise ChartsError(f"GDAL {m.group(1)}.{m.group(2)} found; the mesh build needs {GDAL_MIN[0]}.{GDAL_MIN[1]} or later")


def problems(what, lines):
    """One ChartsError for a list of problems: how many, and the first of them spelled out."""
    more = f"\n   ... and {len(lines) - SHOWN} more" if len(lines) > SHOWN else ""
    return ChartsError(f"{len(lines)} {what}:\n   " + "\n   ".join(lines[:SHOWN]) + more)


class Charts:
    """The charts of one build. inputs: the district's charts, each a zip or a folder, already resolved
    (mesh/cli.py resolve_paths); run_dir: the run's own new folder. Creating it does steps 1 to 5 and reads the
    layer names of every kept cell; cells() extracts."""

    def __init__(self, inputs, run_dir, workers, log):
        check_gdal()
        self.inputs, self.run_dir, self.workers, self.log = inputs, run_dir, max(1, workers), log
        self.not_cells = {k: [] for k in range(len(inputs))}
        roots = [self._unpack(k, path) for k, path in enumerate(inputs)]
        copies = [c for k, root in enumerate(roots) for c in self._find(k, root)]
        self.found = {k: sum(1 for c in copies if c.source == k) for k in range(len(inputs))}
        log(f"charts: {len(inputs)} input(s), {len(copies)} cell file(s)")
        if not copies:
            raise ChartsError("no chart cell (US<band>XXXXX.000) in " + ", ".join(inputs))
        self._read_states(copies)
        self.kept, self.cancelled, self.several = self._choose(copies)
        self._read_layers()
        log(f"charts: {len(self.kept)} live cell(s); cancelled and left out {len(self.cancelled)}; "
            f"given more than once {self.several}")

    def _each(self, fn, items):
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(fn, items))

    # -- 1, 2: unpack, find ------------------------------------------------------------------------------------
    def _unpack(self, k, path):
        if os.path.isdir(path):
            return path
        if not zipfile.is_zipfile(path):
            raise ChartsError(f"{path} is neither a zip nor a folder")
        root = os.path.join(self.run_dir, "charts", f"input{k}")
        os.makedirs(root)
        try:
            with zipfile.ZipFile(path) as zf:
                zf.extractall(root)
        except (zipfile.BadZipFile, OSError) as ex:
            raise ChartsError(f"{path} could not be unpacked: {ex}") from ex
        return root

    def _find(self, k, root):
        out = []
        for folder, subfolders, files in os.walk(root):
            subfolders.sort()
            for f in sorted(files):
                stem, ext = os.path.splitext(f)
                if ext != ".000":
                    continue
                if CELL_NAME.match(stem.upper()):
                    out.append(Copy(stem.upper(), os.path.join(folder, f), k))
                else:
                    self.not_cells[k].append(os.path.relpath(os.path.join(folder, f), root))
        if self.not_cells[k]:
            self.log(f"charts: {len(self.not_cells[k])} .000 file(s) of {self.inputs[k]} are not named as NOAA cells "
                     f"and are not read: {', '.join(self.not_cells[k][:SHOWN])}")
        return out

    # -- 3: the state of every copy ----------------------------------------------------------------------------
    def _read_states(self, copies):
        def one(c):
            rc, out, err = gdal(["ogrinfo", "-ro", "-json", "-features", c.enc, "DSID"])
            if rc != 0:
                return f"{c.name} ({c.enc}): {err}"
            try:
                p = json.loads(out)["layers"][0]["features"][0]["properties"]
            except (ValueError, LookupError, TypeError):
                return f"{c.name} ({c.enc}): no data set record (DSID)"
            c.edition, c.update, c.issued = (None if p.get(k) is None else str(p[k])
                                             for k in ("DSID_EDTN", "DSID_UPDN", "DSID_ISDT"))
            if not c.edition:
                return f"{c.name} ({c.enc}): no edition number, so it cannot be told whether the cell is cancelled"
            return None
        bad = [b for b in self._each(one, copies) if b]
        if bad:
            raise problems("cell(s) cannot be read", bad)

    # -- 4, 5: one copy of each cell; cancelled cells out ------------------------------------------------------
    def _choose(self, copies):
        by_name = {}
        for c in copies:
            by_name.setdefault(c.name, []).append(c)
        kept, cancelled, undecided = [], [], []
        for name in sorted(by_name):
            cs = by_name[name]
            try:
                c = latest_copy(name, cs) if len(cs) > 1 else cs[0]
            except ChartsError as ex:
                undecided.append(str(ex))
                continue
            (cancelled if c.edition == "0" else kept).append(c)
        if undecided:
            raise problems("cell(s) are given more than once and their copies cannot be decided between", undecided)
        return kept, [c.name for c in cancelled], sum(1 for cs in by_name.values() if len(cs) > 1)

    # -- 6: layer names and extraction -------------------------------------------------------------------------
    def _read_layers(self):
        def one(c):
            rc, out, err = gdal(["ogrinfo", "-ro", "-json", "-so", "-al", c.enc])
            if rc != 0:
                return f"{c.name} ({c.enc}): {err}"
            try:
                c.layers = [layer["name"] for layer in json.loads(out)["layers"]]
            except (ValueError, LookupError, TypeError):
                return f"{c.name} ({c.enc}): GDAL's summary of the cell cannot be read"
            return None
        bad = [b for b in self._each(one, self.kept) if b]
        if bad:
            raise problems("cell(s) cannot be read", bad)

    def cells(self):
        """Extract the layers the mesh reads of every kept cell; the records for mesh/build.py cell_index:
        [(band, name, source, {layer: file})], source being the input the cell was read from."""
        def one(c):
            folder = os.path.join(self.run_dir, "cells", c.name)
            os.makedirs(folder)
            files, bad = {}, []
            for layer in sorted(set(c.layers) & set(build.LAYERS)):
                out = os.path.join(folder, f"{layer}.geojson")
                cmd = ["ogr2ogr", "-f", "GeoJSON", "-oo", "LIST_AS_STRING=YES"]
                if layer == "SOUNDG":
                    cmd += ["-oo", "SPLIT_MULTIPOINT=YES", "-oo", "ADD_SOUNDG_DEPTH=YES"]
                rc, _, err = gdal(cmd + [out, c.enc, layer])
                if rc != 0 or not os.path.isfile(out):
                    bad.append(f"{c.name} {layer} ({c.enc}): {err}")
                files[layer] = out
            return c, files, bad
        self.log(f"charts: extracting {len(self.kept)} cell(s)")
        records, bad = [], []
        for c, files, b in self._each(one, self.kept):
            bad += b
            records.append((c.band, c.name, self.inputs[c.source], files))
        if bad:
            raise problems("layer(s) could not be extracted", bad)
        return records

    def record(self, index):
        """What the build read, for the sidecar. index: mesh/build.py cell_index of the extracted cells (those
        with charted areas)."""
        by_band = {}
        for c in index:
            by_band[c[1]] = by_band.get(c[1], 0) + 1
        return {
            "inputs": [{"file": os.path.basename(path), "bytes": os.path.getsize(path) if os.path.isfile(path) else None,
                        "cell_files_found": self.found[k], "files_not_read": self.not_cells[k]}
                       for k, path in enumerate(self.inputs)],
            "cells_given_more_than_once": self.several,
            "cancelled_cells_left_out": self.cancelled,
            "live_cells": len(self.kept),
            "cells_read": len(index),
            "cells_read_by_band": dict(sorted(by_band.items())),
        }
