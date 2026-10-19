#!/usr/bin/env python3
"""Write and validate the mesh catalogue, charts/mesh/index.json, that
the signalk-weather-router-plus plugin downloads meshes from.

    mesh-catalog.py --sidecars DIR --public-url https://host/US-ENC/charts -o index.json [--check-urls]

One entry per `<name>_mesh.json` sidecar in DIR. Every entry carries,
and is checked for, the fields the plugin requires (it sets aside an
entry that fails any of these):

  name              letters, digits, '-' and '_' only
  archive           '<name>_mesh.tar.zst', relative to the catalogue's parent folder
                    (kept for older plugin versions)
  archive_url       the absolute HTTPS URL of that archive
  build_date        ISO date
  chart_build_date  ISO date or null
  bytes, triangles  non-negative numbers
  clusters          non-empty; each with box [west, south, east, north], finite,
                    west < east and south < north

The archive's top folder is '<name>_mesh/' (build-mesh.py --archive
writes it so; the plugin unpacks from that folder). With --check-urls
every archive_url must answer HEAD with 200 and a Content-Length of at
least MIN_ARCHIVE_BYTES and no more than the unpacked `bytes`. Any
failure exits 1 and nothing is written.

Catalogue version 1: `archive_url` is additive, so a reader of version 1
without it keeps working; the version is unchanged.
"""
import argparse
import datetime as dt
import glob
import json
import math
import os
import re
import sys
import urllib.request

CATALOG_VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$")
MIN_ARCHIVE_BYTES = 1_000_000


def entry_from_sidecar(s, public_url):
    """The catalogue entry of one mesh sidecar; `public_url` is the
    absolute URL of the folder holding the archives (…/charts)."""
    name = s["name"]
    return {
        "name": name,
        "dir": f"mesh/{name}/",
        "archive": f"{name}_mesh.tar.zst",
        "archive_url": f"{public_url.rstrip('/')}/{name}_mesh.tar.zst",
        "sidecar": f"{name}_mesh.json",
        "build_date": s["build_date"],
        "chart_build_date": s.get("chart_build_date"),
        "format": s["format"],
        "triangles": s["triangles"],
        "bytes": s["bytes"],
        "clusters": [{"dir": c["dir"], "box": c["box"]} for c in s["clusters"]],
    }


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_entry(e):
    """The problems with one entry, as strings; empty when it passes."""
    bad = []
    name = e.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        bad.append(f"name {name!r}: letters, digits, '-' and '_' only")
    archive = e.get("archive")
    if not isinstance(archive, str) or not archive.endswith(".tar.zst"):
        bad.append(f"archive {archive!r}: must end with .tar.zst")
    elif isinstance(name, str) and archive != f"{name}_mesh.tar.zst":
        bad.append(f"archive {archive!r}: must be {name}_mesh.tar.zst (its top folder is {name}_mesh/)")
    url = e.get("archive_url")
    if not isinstance(url, str) or not url.startswith("https://") or (isinstance(archive, str) and not url.endswith("/" + archive)):
        bad.append(f"archive_url {url!r}: must be an https URL ending in /{archive}")
    if not isinstance(e.get("build_date"), str) or not ISO_RE.match(e["build_date"]):
        bad.append(f"build_date {e.get('build_date')!r}: ISO date")
    cbd = e.get("chart_build_date")
    if cbd is not None and (not isinstance(cbd, str) or not ISO_RE.match(cbd)):
        bad.append(f"chart_build_date {cbd!r}: ISO date or null")
    for k in ("bytes", "triangles"):
        if not _is_number(e.get(k)) or e[k] < 0:
            bad.append(f"{k} {e.get(k)!r}: non-negative number")
    cl = e.get("clusters")
    if not isinstance(cl, list) or not cl:
        bad.append("clusters: non-empty array")
    else:
        for i, c in enumerate(cl):
            box = c.get("box") if isinstance(c, dict) else None
            if (not isinstance(box, list) or len(box) != 4 or not all(_is_number(v) for v in box)
                    or not (box[0] < box[2] and box[1] < box[3])):
                bad.append(f"clusters[{i}].box {box!r}: [west, south, east, north], four finite numbers, west < east, south < north")
    return bad


def head(url, timeout=30):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "s57Work-mesh-catalog"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Length")


def check_url(e, head_fn=head):
    """The problems with an entry's archive_url, as strings."""
    try:
        status, length = head_fn(e["archive_url"])
    except Exception as ex:                                   # HTTPError, URLError, timeout
        return [f"{e['archive_url']}: HEAD failed: {ex}"]
    bad = []
    if status != 200:
        bad.append(f"{e['archive_url']}: HEAD {status}, expected 200")
    try:
        n = int(length)
    except (TypeError, ValueError):
        return bad + [f"{e['archive_url']}: no Content-Length"]
    if n < MIN_ARCHIVE_BYTES:
        bad.append(f"{e['archive_url']}: Content-Length {n:,} under {MIN_ARCHIVE_BYTES:,}")
    if _is_number(e.get("bytes")) and n > e["bytes"]:
        bad.append(f"{e['archive_url']}: Content-Length {n:,} exceeds the unpacked size {e['bytes']:,}")
    return bad


def build_catalog(sidecar_paths, public_url, check_urls=False, head_fn=head, now=None):
    """(catalogue dict, problems). The catalogue is complete only when
    problems is empty."""
    meshes, problems = [], []
    for p in sorted(sidecar_paths):
        try:
            s = json.load(open(p))
            if os.path.basename(p) != f"{s['name']}_mesh.json":
                problems.append(f"{os.path.basename(p)}: sidecar name {s['name']!r} does not match its filename")
                continue
            e = entry_from_sidecar(s, public_url)
        except (KeyError, TypeError, ValueError, OSError) as ex:
            problems.append(f"{os.path.basename(p)}: unreadable sidecar: {ex!r}")
            continue
        bad = validate_entry(e)
        if not bad and check_urls:
            bad = check_url(e, head_fn)
        problems += [f"{e['name']}: {b}" for b in bad]
        meshes.append(e)
    updated = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"version": CATALOG_VERSION, "updated": updated, "meshes": meshes}, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sidecars", required=True, help="directory of <name>_mesh.json sidecars")
    ap.add_argument("--public-url", required=True, help="absolute URL of the folder holding the archives (…/charts)")
    ap.add_argument("-o", "--out", required=True, help="index.json to write")
    ap.add_argument("--check-urls", action="store_true", help="HEAD every archive_url (200, plausible Content-Length)")
    a = ap.parse_args(argv)
    if not a.public_url.startswith("https://"):
        sys.exit(f"--public-url must be https: {a.public_url}")
    paths = glob.glob(os.path.join(a.sidecars, "*_mesh.json"))
    if not paths:
        sys.exit(f"no *_mesh.json under {a.sidecars}")
    cat, problems = build_catalog(paths, a.public_url, check_urls=a.check_urls)
    if not problems:
        for e in cat["meshes"]:
            print(f"{e['name']}: {e['triangles']:,} triangles, {e['bytes']/1e9:.2f} GB unpacked, "
                  f"built {e['build_date']} from chart {e['chart_build_date']}, {len(e['clusters'])} cluster(s), {e['archive_url']}")
    else:
        print(f"\n{len(problems)} problem(s); index.json NOT written:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        sys.exit(1)
    with open(a.out, "w") as fh:
        json.dump(cat, fh, indent=1)
    print(f"\nwrote {a.out}: version {cat['version']}, {len(cat['meshes'])} meshes")


if __name__ == "__main__":
    main()
