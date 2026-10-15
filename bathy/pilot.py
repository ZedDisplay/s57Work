"""Bathymetry pilot: one 1° square from satellite image to measured error.

Stages, each writing under the work directory and each skipped when its
output already exists (``--force STAGE`` redoes one):

  composite  Earth Engine: Sentinel-2 L2A over the window, cloud/shadow
             masked (SCL), glint removed by subtracting SWIR (B11), median
             of B2, B3, B8 and the count of clear observations; exported
             to Cloud Storage as int16 GeoTIFF (reflectance x 10000) at
             10 m in UTM, downloaded and mosaicked to composite.vrt.
  icesat     ATL24 v002 granules over the square from NASA Earthdata;
             bathymetry photons (class_ph 40) that are not low-confidence
             and not beyond the sensor depth; depth = surface_h - ortho_h,
             i.e. below the instantaneous water surface, refraction
             already corrected by ATL24. Saved as icesat.npz.
  truth      Chart depths for the accuracy check from a district
             .mbtiles at z16: SOUNDG points (DEPTH) and DEPARE intervals
             (DRVAL1..DRVAL2) inside the square. Never used in the fit.
  fit        ratio = ln(1000 B2) / ln(1000 B3) on the composite; robust
             linear fit depth = m1 ratio - m0 against 80 % of the ICESat-2
             points; depth raster sdb.tif for water pixels whose ratio is
             inside the calibrated range, 0 to 20 m, else nodata.
             Default model: a shallow line (0-6 m) and a deep line
             (4-25 m) blended over 4-6 m, fitted per 10 km cell where
             the cell has 2000 points from 3 passes, else square-wide.
             The hold-out is whole passes, never points of a pass that
             is also in the training set.
  report     Error per depth band (0-2, 2-5, 5-10, 10-20 m) against the
             held-out ICESat-2 points and the chart soundings: median and
             90th-percentile absolute error and the share of points where
             SDB is more than 1 m deeper than the truth (the dangerous
             direction). A band passes when that share is at most 5 %.
             report.json and report.txt.

Not in the pilot (stated in the report): no tide or datum correction.
The Virgin Islands are microtidal (spring range about 0.3 m), so ICESat-2
depths below the instantaneous surface, the chart's MLLW and the composite
are within a few decimetres of each other; the full pipeline converts all
to LAT with a tide model.

Credentials, all from the environment:
  EE_PROJECT, and EE_SERVICE_ACCOUNT_KEY (the key JSON itself) or
  EE_KEY_FILE (a path to it); without a key, ee.Initialize uses the
  personal login from `earthengine authenticate`.
  GCS_BUCKET for the export.
  EARTHDATA_USERNAME and EARTHDATA_PASSWORD for ICESat-2.
"""
import argparse
import glob
import gzip
import json
import math
import os
import sqlite3
import subprocess
import sys
import time

import numpy as np

PILOT_BOX = (-65.0, 18.0, -64.0, 19.0)      # W, S, E, N: USVI (St Thomas, St John) and BVI
UTM = "EPSG:32620"                           # zone 20N covers 66-60 W
BANDS = [(0, 2), (2, 5), (5, 10), (10, 20)]
DEEPER_TOL = 1.0                             # m: SDB deeper than truth by more than this is a "too deep" point
DEEPER_MAX_SHARE = 0.05                      # a band passes when at most this share is too deep
SDB_MAX_DEPTH = 20.0
SCALE = 10000.0                              # composite int16 = reflectance x SCALE


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- composite

def _ee_init():
    import ee
    project = os.environ["EE_PROJECT"]
    key = os.environ.get("EE_SERVICE_ACCOUNT_KEY")
    keyfile = os.environ.get("EE_KEY_FILE")
    info = None
    if key or keyfile:
        info = json.loads(key) if key else json.load(open(keyfile))
        creds = ee.ServiceAccountCredentials(info["client_email"], key_data=json.dumps(info))
        ee.Initialize(creds, project=project)
    else:
        ee.Initialize(project=project)
    return ee, info


def _gcs_client(info):
    from google.cloud import storage
    if info:
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(info)
        return storage.Client(project=os.environ["EE_PROJECT"], credentials=creds)
    return storage.Client(project=os.environ["EE_PROJECT"])


def stage_composite(work, box, months, max_cloud, glint_max):
    ee, info = _ee_init()
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - months * 30.44 * 86400))
    geom = ee.Geometry.Rectangle(list(box), proj="EPSG:4326", geodesic=False)
    col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
           .filterBounds(geom).filterDate(start, end)
           .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud)))
    n_scenes = col.size().getInfo()
    log(f"composite: {n_scenes} Sentinel-2 L2A scenes {start}..{end} with scene cloud < {max_cloud} %")
    if n_scenes == 0:
        raise SystemExit("no scenes")

    def prep(img):
        scl = img.select("SCL")
        clear = (scl.neq(0).And(scl.neq(1)).And(scl.neq(3)).And(scl.neq(8))
                 .And(scl.neq(9)).And(scl.neq(10)).And(scl.neq(11)))
        r = img.select(["B2", "B3", "B8", "B11"]).divide(SCALE)
        swir = r.select("B11")
        ok = clear.And(swir.lt(glint_max))          # strong SWIR over water = glint, whitecaps, haze
        b2 = r.select("B2").subtract(swir).rename("B2")
        b3 = r.select("B3").subtract(swir).rename("B3")
        return ee.Image.cat([b2, b3, r.select("B8").rename("B8")]).updateMask(ok)

    prepped = col.map(prep)
    med = prepped.median()
    nobs = prepped.select("B2").count().rename("N")
    img = (med.multiply(SCALE).round().addBands(nobs)).toInt16().unmask(-32768)
    prefix = f"bathy-pilot/{os.path.basename(os.path.abspath(work))}/composite"
    bucket = os.environ["GCS_BUCKET"]
    task = ee.batch.Export.image.toCloudStorage(
        image=img, description=f"bathy_pilot_{int(time.time())}", bucket=bucket,
        fileNamePrefix=prefix, region=geom, scale=10, crs=UTM, maxPixels=1e10,
        fileFormat="GeoTIFF", formatOptions={"cloudOptimized": True, "noData": -32768})
    task.start()
    t0 = time.time()
    log(f"composite: Earth Engine export {task.id} to gs://{bucket}/{prefix}*")
    while True:
        st = task.status()
        if st["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        log(f"  [{time.time()-t0:5.0f} s] {st['state']}")
        time.sleep(30)
    if st["state"] != "COMPLETED":
        raise SystemExit(f"export {st['state']}: {st.get('error_message')}")
    eecu = st.get("batch_eecu_usage_seconds")
    log(f"composite: export done in {time.time()-t0:.0f} s" + (f", {eecu:.0f} EECU-seconds" if eecu else ""))
    client = _gcs_client(info)
    files = []
    for blob in client.list_blobs(bucket, prefix=prefix):
        dst = os.path.join(work, "composite", os.path.basename(blob.name))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        blob.download_to_filename(dst)
        files.append(dst)
        log(f"  downloaded {blob.name} ({blob.size/1e6:.0f} MB)")
    subprocess.run(["gdalbuildvrt", os.path.join(work, "composite.vrt")] + sorted(files), check=True,
                   capture_output=True)
    json.dump({"scenes": n_scenes, "start": start, "end": end, "max_cloud": max_cloud, "glint_max": glint_max,
               "export_seconds": time.time() - t0, "eecu_seconds": eecu, "files": [os.path.basename(f) for f in files]},
              open(os.path.join(work, "composite.json"), "w"), indent=1)


# ---------------------------------------------------------------- icesat

BEAMS = ("gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r")
ICESAT_BATCH = 8
KEEP_ATL24 = False         # raw granules are deleted once their photons are extracted


def read_atl24(path, box, min_conf):
    """Bathymetry photons of one ATL24 granule inside box: lon, lat, depth
    below the instantaneous surface, delta_time, sigma_tvu, night."""
    import h5py
    out = []
    with h5py.File(path, "r") as f:
        for b in BEAMS:
            if b not in f:
                continue
            g = f[b]
            cls = g["class_ph"][:]
            lon = g["lon_ph"][:]; lat = g["lat_ph"][:]
            m = ((cls == 40) & (g["low_confidence_flag"][:] == 0) & (g["sensor_depth_exceeded"][:] == 0)
                 & (g["confidence"][:] >= min_conf)
                 & (lon >= box[0]) & (lon <= box[2]) & (lat >= box[1]) & (lat <= box[3]))
            if not m.any():
                continue
            depth = g["surface_h"][:][m] - g["ortho_h"][:][m]
            out.append(np.column_stack([lon[m], lat[m], depth, g["delta_time"][:][m],
                                        g["sigma_tvu"][:][m], g["night_flag"][:][m]]))
    return np.vstack(out) if out else np.empty((0, 6))


def stage_icesat(work, box, min_conf):
    import earthaccess
    earthaccess.login(strategy="environment")
    res = earthaccess.search_data(short_name="ATL24", version="002", bounding_box=tuple(box))
    log(f"icesat: {len(res)} ATL24 v002 granules intersect the square")
    d = os.path.join(work, "atl24")
    os.makedirs(d, exist_ok=True)
    pts, paths = [], []
    for k in range(0, len(res), ICESAT_BATCH):        # download, extract, delete: disk stays small
        got = earthaccess.download(res[k:k + ICESAT_BATCH], d)
        for p in got:
            a = read_atl24(p, box, min_conf)
            if len(a):
                pts.append(a)
            paths.append(p)
            if not KEEP_ATL24:
                os.remove(p)
        log(f"icesat: {min(k + ICESAT_BATCH, len(res))}/{len(res)} granules read")
    P = np.vstack(pts) if pts else np.empty((0, 6))
    np.savez_compressed(os.path.join(work, "icesat.npz"), pts=P,
                        cols=np.array(["lon", "lat", "depth", "delta_time", "sigma_tvu", "night"]))
    log(f"icesat: {len(P):,} bathymetry photons kept from {len(paths)} granules; depth "
        f"{np.percentile(P[:, 2], 1) if len(P) else 0:.1f}..{np.percentile(P[:, 2], 99) if len(P) else 0:.1f} m (1-99 %)")


# ---------------------------------------------------------------- tide

# Copernicus Marine hourly sea level (the signalk-weather-router-plus
# source, src/tides/sealevel.ts): FES2014 ocean_tide and total_sea_level on
# 1/12 deg, hourly from 2022-09-01, read anonymously from the ARCO Zarr
# store. Option 1 of 2026-10-14: only ICESat-2 passes inside that record are
# tide-corrected and used; LAT is the lowest predicted tide in the record
# (about four years, not the 18.6-year cycle; within a few cm in microtidal
# water, not safe where tides are large).
CMEMS_STORE = ("https://s3.waw3-1.cloudferro.com/mdl-arco-geo-015/arco/GLOBAL_ANALYSISFORECAST_PHY_001_024/"
               "cmems_mod_glo_phy_anfc_merged-sl_PT1H-i_202411/geoChunked.zarr")
ATLAS_EPOCH = np.datetime64("2018-01-01T00:00:00")
ICESAT_FILE = "icesat.npz"        # the fit/report/correct input; "icesat_lat.npz" after the tide stage


def _fill_nearest(a):
    """Cells with no model value (land in the 1/12 deg model) take the
    nearest valid cell's series (a: [t, y, x])."""
    from scipy.ndimage import distance_transform_edt
    bad = np.isnan(a).all(axis=0)
    if not bad.any() or bad.all():
        return a
    _, (iy, ix) = distance_transform_edt(bad, return_indices=True)
    return a[:, iy, ix]


def stage_tide(work, box):
    import xarray as xr
    import rasterio
    t0 = time.time()
    ds = xr.open_zarr(CMEMS_STORE, consolidated=True).isel(elevation=0)
    lat = ds.latitude.values; lon = ds.longitude.values
    pad = 3 / 12.0
    ys = np.flatnonzero((lat >= box[1] - pad) & (lat <= box[3] + pad))
    xs = np.flatnonzero((lon >= box[0] - pad) & (lon <= box[2] + pad))
    sub = ds[["ocean_tide", "total_sea_level"]].isel(latitude=slice(ys[0], ys[-1] + 1), longitude=slice(xs[0], xs[-1] + 1)).load()
    tide = _fill_nearest(sub.ocean_tide.values.astype(np.float64))
    total = _fill_nearest(sub.total_sea_level.values.astype(np.float64))
    times = sub.time.values
    offset = np.nanmean(total - tide, axis=0)                 # geoid -> local mean sea level (the plugin's offset)
    level = total - offset                                     # water level re mean sea level: tide + surge
    L = -np.nanmin(tide, axis=0)                               # LAT below mean sea level, m (positive)
    glat = sub.latitude.values; glon = sub.longitude.values
    dlat = float(glat[1] - glat[0]); dlon = float(glon[1] - glon[0])
    log(f"tide: {tide.shape[1]}x{tide.shape[2]} cells x {tide.shape[0]:,} hours ({str(times[0])[:10]}..{str(times[-1])[:10]}) "
        f"in {time.time()-t0:.0f} s; LAT below MSL {np.nanmin(L):.2f}..{np.nanmax(L):.2f} m")

    P = np.load(os.path.join(work, "icesat.npz"))["pts"]
    pt = ATLAS_EPOCH + (P[:, 3] * 1e6).astype("timedelta64[us]")
    th = (pt - times[0]) / np.timedelta64(1, "h")
    inside = (th >= 0) & (th <= len(times) - 1)
    Q = P[inside]; th = th[inside]
    i0 = np.clip(np.floor(th).astype(int), 0, len(times) - 2); wt = th - i0
    y = (Q[:, 1] - glat[0]) / dlat; x = (Q[:, 0] - glon[0]) / dlon
    y0 = np.clip(np.floor(y).astype(int), 0, len(glat) - 2); x0 = np.clip(np.floor(x).astype(int), 0, len(glon) - 2)
    ty = y - y0; tx = x - x0

    def corners(a, ti):
        return ((1 - ty) * (1 - tx) * a[ti, y0, x0] + (1 - ty) * tx * a[ti, y0, x0 + 1]
                + ty * (1 - tx) * a[ti, y0 + 1, x0] + ty * tx * a[ti, y0 + 1, x0 + 1])
    wl = (1 - wt) * corners(level, i0) + wt * corners(level, i0 + 1)
    Lp = ((1 - ty) * (1 - tx) * L[y0, x0] + (1 - ty) * tx * L[y0, x0 + 1] + ty * (1 - tx) * L[y0 + 1, x0] + ty * tx * L[y0 + 1, x0 + 1])
    d_lat = Q[:, 2] - wl - Lp                                   # below the pass-time surface -> below MSL -> below LAT
    out = Q.copy(); out[:, 2] = d_lat
    np.savez_compressed(os.path.join(work, "icesat_lat.npz"), pts=out, water_level=wl, lat_offset=Lp, depth_inst=Q[:, 2],
                        cols=np.array(["lon", "lat", "depth_lat", "delta_time", "sigma_tvu", "night"]))
    # LAT surface for the chart stage (GEBCO MSL -> LAT), EPSG:4326 at the model grid
    from rasterio.transform import from_origin
    north_up = L[::-1] if dlat > 0 else L
    tr = from_origin(glon[0] - dlon / 2, glat.max() + abs(dlat) / 2, dlon, abs(dlat))
    with rasterio.open(os.path.join(work, "lat_offset.tif"), "w", driver="GTiff", width=L.shape[1], height=L.shape[0],
                       count=1, dtype="float32", crs="EPSG:4326", transform=tr, nodata=-9999.0) as d:
        d.write(np.where(np.isfinite(north_up), north_up, -9999.0).astype(np.float32), 1)
    npass = len(np.unique(pass_ids(Q[:, 3])))
    log(f"tide: {len(Q):,} of {len(P):,} photons from {npass} passes inside the record; water level at pass time "
        f"{np.percentile(wl, 1):+.2f}..{np.percentile(wl, 99):+.2f} m (1-99 %); depth change to LAT "
        f"median {np.median(d_lat - Q[:, 2]):+.2f} m; written icesat_lat.npz and lat_offset.tif")


# ---------------------------------------------------------------- truth

def _tile_bounds(z, x, y):
    n = 2 ** z
    w = x / n * 360 - 180; e = (x + 1) / n * 360 - 180
    nl = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    sl = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return w, sl, e, nl


def stage_truth(work, box, mbtiles):
    """SOUNDG points and DEPARE polygons inside the square from the chart's z16 tiles."""
    import mapbox_vector_tile
    import shapely
    from shapely.geometry import shape
    Z = 16; n = 2 ** Z
    tx0 = int((box[0] + 180) / 360 * n); tx1 = int((box[2] + 180) / 360 * n)
    def ty(lat):
        r = math.radians(lat); return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    y0, y1 = ty(box[3]), ty(box[1])
    con = sqlite3.connect(mbtiles)
    rows = con.execute("SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level=? AND tile_column BETWEEN ? AND ? "
                       "AND tile_row BETWEEN ? AND ?", (Z, tx0, tx1, (n - 1) - y1, (n - 1) - y0))
    snd, dep, ntiles = {}, [], 0
    for x, y_tms, data in rows:
        ntiles += 1
        y = (n - 1) - y_tms
        w, s, e, nl = _tile_bounds(Z, x, y)
        t = mapbox_vector_tile.decode(gzip.decompress(data), y_coord_down=True)
        for L in ("SOUNDG", "DEPARE"):
            lay = t.get(L)
            if not lay:
                continue
            ext = lay.get("extent", 4096)
            for f in lay["features"]:
                g = shapely.transform(shape(f["geometry"]),
                                      lambda c: c * np.array([(e - w) / ext, -(nl - s) / ext]) + np.array([w, nl]))
                p = f["properties"]
                if L == "SOUNDG":
                    v = p.get("DEPTH")
                    if v is None:
                        continue
                    for pt in shapely.get_parts(g):
                        snd[(round(pt.x, 7), round(pt.y, 7))] = float(v)     # a sounding on a tile edge is in both tiles
                else:
                    d1, d2 = p.get("DRVAL1"), p.get("DRVAL2")
                    if d1 is None:
                        continue
                    g = shapely.intersection(shapely.make_valid(g), shapely.box(w, s, e, nl))
                    if not g.is_empty:
                        dep.append((shapely.to_wkb(g), float(d1), float(d2) if d2 is not None else float("nan")))
    S = np.array([(k[0], k[1], v) for k, v in snd.items()]) if snd else np.empty((0, 3))
    np.savez_compressed(os.path.join(work, "truth.npz"), soundings=S,
                        depare_wkb=np.array([d[0] for d in dep], dtype=object),
                        depare_d1=np.array([d[1] for d in dep]), depare_d2=np.array([d[2] for d in dep]),
                        source=os.path.basename(mbtiles))
    log(f"truth: {ntiles:,} z16 tiles; {len(S):,} soundings, {len(dep):,} DEPARE pieces from {os.path.basename(mbtiles)}")


# ---------------------------------------------------------------- fit

def ratio_of(b2, b3):
    """ln(1000 B2) / ln(1000 B3) on reflectance; NaN where either log is not positive."""
    with np.errstate(divide="ignore", invalid="ignore"):
        a = np.log(1000.0 * b2); b = np.log(1000.0 * b3)
        r = a / b
    bad = ~((b2 > 0.001) & (b3 > 0.001)) | ~np.isfinite(r) | (b <= 0)
    r[bad] = np.nan
    return r


def robust_line(x, y):
    """depth = m1 * ratio - m0, soft-L1 robust least squares. Returns (m1, m0)."""
    from scipy.optimize import least_squares
    A = np.polyfit(x, y, 1)
    f = least_squares(lambda p: p[0] * x - p[1] - y, x0=[A[0], -A[1]], loss="soft_l1", f_scale=1.0)
    return float(f.x[0]), float(f.x[1])


def _sample(ds, lon, lat, bands):
    """Values of the raster bands at lon/lat (nearest pixel); NaN outside or at nodata."""
    from rasterio.warp import transform
    xs, ys = transform("EPSG:4326", ds.crs, list(lon), list(lat))
    vals = np.array([v for v in ds.sample(zip(xs, ys), indexes=bands)], dtype=float)
    nod = ds.nodata
    if nod is not None:
        vals[vals == nod] = np.nan
    return vals


# Switching model (shallow and deep lines blended over an overlap), after
# the Caballero and Stumpf switching approach: one straight line over
# 0-25 m bends in the shallows (pilot v1, 2026-10-11: 0-2 m read 0.7 m
# too deep on average).
SW_SHALLOW_MAX = 6.0      # shallow line fitted on 0..6 m
SW_DEEP_MIN = 4.0         # deep line fitted on 4..25 m
SW_BLEND = (4.0, 6.0)     # shallow-line depth where the blend runs from shallow to deep
CELL_M = 10000.0          # calibration cells, 10 km in UTM (about 0.1 degree)
CELL_MIN_POINTS = 2000
CELL_MIN_PASSES = 3


def pass_ids(delta_time, gap=60.0):
    """Number the ICESat-2 passes: points more than `gap` seconds apart in
    delta_time belong to different passes. Returns an int array aligned
    with delta_time."""
    order = np.argsort(delta_time, kind="stable")
    t = delta_time[order]
    pid_sorted = np.concatenate([[0], np.cumsum(np.diff(t) > gap)])
    out = np.empty(len(t), dtype=np.int64)
    out[order] = pid_sorted
    return out


def fit_model(r, d, kind):
    """Fit depth from ratio. kind 'linear': {m1, m0}; 'switch': a shallow
    and a deep line. Returns a dict, or None when there are too few points."""
    if kind == "linear":
        if len(r) < 50:
            return None
        m1, m0 = robust_line(r, d)
        return {"kind": "linear", "m1": m1, "m0": m0}
    sh = d <= SW_SHALLOW_MAX; dp = d >= SW_DEEP_MIN
    if sh.sum() < 50 or dp.sum() < 50:
        return None
    s1, s0 = robust_line(r[sh], d[sh]); d1, d0 = robust_line(r[dp], d[dp])
    return {"kind": "switch", "shallow": [s1, s0], "deep": [d1, d0]}


def predict_model(m, r):
    """Depth from ratio with a fitted model (see fit_model)."""
    if m["kind"] == "linear":
        return m["m1"] * r - m["m0"]
    s = m["shallow"][0] * r - m["shallow"][1]
    dp = m["deep"][0] * r - m["deep"][1]
    a, b = SW_BLEND
    w = np.clip((s - a) / (b - a), 0.0, 1.0)      # 0 = shallow line, 1 = deep line
    return (1.0 - w) * s + w * dp


def _cell_keys(x, y):
    return (np.floor(x / CELL_M).astype(np.int64) * 100000 + np.floor(y / CELL_M).astype(np.int64))


def stage_fit(work, min_obs, holdout, seed, model="switch", by_cell=True):
    import rasterio
    from rasterio.warp import transform
    P = np.load(os.path.join(work, ICESAT_FILE))["pts"]
    ds = rasterio.open(os.path.join(work, "composite.vrt"))
    v = _sample(ds, P[:, 0], P[:, 1], [1, 2, 3, 4])
    b2, b3, b8, nobs = v[:, 0] / SCALE, v[:, 1] / SCALE, v[:, 2] / SCALE, v[:, 3]
    r = ratio_of(b2, b3)
    with np.errstate(divide="ignore", invalid="ignore"):
        ndwi = (b3 - b8) / (b3 + b8)
    ok = np.isfinite(r) & (nobs >= min_obs) & (ndwi > 0) & (P[:, 2] > 0) & (P[:, 2] <= 25)
    xs, ys = transform("EPSG:4326", ds.crs, list(P[:, 0]), list(P[:, 1]))
    xs, ys = np.asarray(xs), np.asarray(ys)
    pid = pass_ids(P[:, 3])

    # hold out whole passes, so no held-out point shares a pass with training
    passes = np.unique(pid[ok])
    rng = np.random.default_rng(seed)
    hold_p = rng.choice(passes, size=max(1, int(round(len(passes) * holdout))), replace=False)
    is_hold = np.isin(pid, hold_p)
    train = np.flatnonzero(ok & ~is_hold); hold = np.flatnonzero(ok & is_hold)
    glob = fit_model(r[train], P[train, 2], model)
    if glob is None:
        raise SystemExit(f"only {len(train)} usable ICESat-2 points for the fit")
    rlo, rhi = np.percentile(r[train], [1, 99])
    cells = {}
    if by_cell:
        ck = _cell_keys(xs[train], ys[train])
        for k in np.unique(ck):
            m = ck == k
            if m.sum() < CELL_MIN_POINTS or len(np.unique(pid[train][m])) < CELL_MIN_PASSES:
                continue
            fm = fit_model(r[train][m], P[train, 2][m], model)
            if fm is None:
                continue
            fm["ratio_range"] = [float(x) for x in np.percentile(r[train][m], [1, 99])]
            fm["n"] = int(m.sum()); fm["passes"] = int(len(np.unique(pid[train][m])))
            cells[int(k)] = fm
    log(f"fit: model {model}, {len(train):,} training points from {len(passes) - len(hold_p)} passes, "
        f"{len(hold):,} held out from {len(hold_p)} whole passes; {len(cells)} of "
        f"{len(np.unique(_cell_keys(xs[ok], ys[ok])))} 10 km cells have their own fit, the rest use the square-wide fit")
    log(f"fit: square-wide {json.dumps({k: v for k, v in glob.items() if k != 'kind'})}")

    def predict_at(rr, keys):
        out = predict_model(glob, rr)
        lo = np.full(rr.shape, rlo); hi = np.full(rr.shape, rhi)
        for k in np.unique(keys):
            fm = cells.get(int(k))
            if fm is None:
                continue
            m = keys == k
            out[m] = predict_model(fm, rr[m]); lo[m], hi[m] = fm["ratio_range"]
        return out, lo, hi

    prof = ds.profile.copy()
    prof.update(driver="GTiff", count=1, dtype="float32", nodata=-9999.0, compress="deflate", tiled=True,
                blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER")
    out = os.path.join(work, "sdb.tif")
    npix = 0
    with rasterio.open(out, "w", **prof) as dst:
        for _, win in dst.block_windows(1):
            a = ds.read([1, 2, 3, 4], window=win).astype(float)
            nod = ds.nodata
            valid = np.all(a != nod, axis=0) if nod is not None else np.ones(a.shape[1:], bool)
            B2, B3, B8, N = a[0] / SCALE, a[1] / SCALE, a[2] / SCALE, a[3]
            R = ratio_of(B2, B3)
            with np.errstate(divide="ignore", invalid="ignore"):
                W = (B3 - B8) / (B3 + B8)
            T = ds.window_transform(win)
            cols, rows = np.meshgrid(np.arange(win.width) + 0.5, np.arange(win.height) + 0.5)
            px = T.c + cols * T.a + rows * T.b; py = T.f + cols * T.d + rows * T.e
            D, lo, hi = predict_at(R, _cell_keys(px, py))
            keep = (valid & np.isfinite(R) & (R >= lo) & (R <= hi) & (N >= min_obs) & (W > 0)
                    & (D >= 0) & (D <= SDB_MAX_DEPTH))
            o = np.full(B2.shape, -9999.0, dtype=np.float32)
            o[keep] = D[keep]
            npix += int(keep.sum())
            dst.write(o, 1, window=win)
    json.dump({"model": model, "global": glob, "cells": {str(k): v for k, v in cells.items()},
               "train": int(len(train)), "holdout_idx": hold.tolist(), "holdout_passes": [int(x) for x in hold_p],
               "passes": int(len(passes)), "ratio_range": [float(rlo), float(rhi)], "min_obs": min_obs, "sdb_pixels": npix},
              open(os.path.join(work, "fit.json"), "w"))
    log(f"fit: {npix:,} SDB pixels (0-{SDB_MAX_DEPTH:.0f} m) written to {out}")


# ---------------------------------------------------------------- report

def band_stats(truth, sdb):
    """Per band of truth depth: n, median and p90 abs error, signed mean
    and p95 error, share too deep, pass, and the shoal shift that would
    bring the too-deep share to the limit."""
    rows = []
    for lo, hi in BANDS:
        m = (truth >= lo) & (truth < hi) & np.isfinite(sdb)
        e = sdb[m] - truth[m]
        n = int(m.sum())
        if not n:
            rows.append({"band": f"{lo}-{hi} m", "n": 0, "median_abs_m": None, "p90_abs_m": None, "mean_signed_m": None,
                         "p95_signed_m": None, "share_too_deep": None, "pass": None, "shoal_shift_m": None})
            continue
        deep = float((e > DEEPER_TOL).mean())
        p_lim = float(np.percentile(e, 100 * (1 - DEEPER_MAX_SHARE)))
        rows.append({"band": f"{lo}-{hi} m", "n": n,
                     "median_abs_m": float(np.median(np.abs(e))), "p90_abs_m": float(np.percentile(np.abs(e), 90)),
                     "mean_signed_m": float(e.mean()), "p95_signed_m": p_lim,
                     "share_too_deep": deep, "pass": bool(n >= 30 and deep <= DEEPER_MAX_SHARE),
                     "shoal_shift_m": max(0.0, p_lim - DEEPER_TOL)})
    return rows


def stage_report(work):
    import rasterio
    fit = json.load(open(os.path.join(work, "fit.json")))
    P = np.load(os.path.join(work, ICESAT_FILE))["pts"]
    T = np.load(os.path.join(work, "truth.npz"), allow_pickle=True)
    ds = rasterio.open(os.path.join(work, "sdb.tif"))
    H = P[np.array(fit["holdout_idx"], dtype=int)]
    sdb_h = _sample(ds, H[:, 0], H[:, 1], [1])[:, 0]
    S = T["soundings"]
    sdb_s = _sample(ds, S[:, 0], S[:, 1], [1])[:, 0] if len(S) else np.empty(0)
    rep = {"region": "pilot", "box": list(PILOT_BOX), "model": fit.get("model", "linear"),
           "fit": {k: fit.get(k) for k in ("global", "train", "passes", "holdout_passes", "ratio_range", "sdb_pixels")},
           "cells_with_own_fit": len(fit.get("cells", {})),
           "composite": json.load(open(os.path.join(work, "composite.json"))),
           "rule": f"a band passes when at most {DEEPER_MAX_SHARE:.0%} of at least 30 check points are more than "
                   f"{DEEPER_TOL:.0f} m deeper in SDB than in the truth",
           "vs_icesat_holdout": band_stats(H[:, 2], sdb_h),
           "vs_chart_soundings": band_stats(S[:, 2], sdb_s) if len(S) else [],
           "coverage": {"icesat_holdout_with_sdb": int(np.isfinite(sdb_h).sum()), "icesat_holdout": int(len(H)),
                        "soundings_with_sdb": int(np.isfinite(sdb_s).sum()), "soundings": int(len(S))},
           "not_corrected": ("ICESat-2 depths tide-corrected to LAT (Copernicus hourly sea level, passes from 2022-09 only; "
                             "LAT = lowest tide in the 2022-2026 record); chart soundings left on MLLW"
                             if ICESAT_FILE == "icesat_lat.npz" else
                             "no tide or datum correction in the pilot; Virgin Islands spring range ~0.3 m")}
    json.dump(rep, open(os.path.join(work, "report.json"), "w"), indent=1)
    g = fit.get("global", {})
    lines = [f"SDB pilot {PILOT_BOX}: model {rep['model']}, square-wide {json.dumps({k: v for k, v in g.items() if k != 'kind'})}",
             f"training {fit['train']:,} points; held out {len(H):,} points from {len(fit.get('holdout_passes', []))} whole passes "
             f"of {fit.get('passes')}; {rep['cells_with_own_fit']} 10 km cells with their own fit",
             rep["rule"], "shift = how much shallower SDB would have to be made for the band to pass", ""]
    for name, rows in (("held-out ICESat-2 passes", rep["vs_icesat_holdout"]), ("chart soundings (USVI)", rep["vs_chart_soundings"])):
        lines.append(f"vs {name}:")
        lines.append(f"  {'band':8s} {'n':>7s} {'median':>7s} {'p90':>7s} {'mean+/-':>8s} {'too deep':>9s} {'pass':>5s} {'shift':>6s}")
        for r in rows:
            if not r["n"]:
                lines.append(f"  {r['band']:8s} {0:7d}"); continue
            lines.append(f"  {r['band']:8s} {r['n']:7,d} {r['median_abs_m']:6.2f}m {r['p90_abs_m']:6.2f}m {r['mean_signed_m']:+7.2f}m "
                         f"{r['share_too_deep']:8.1%} {'yes' if r['pass'] else 'NO':>5s} {r['shoal_shift_m']:5.2f}m")
        lines.append("")
    lines.append(f"coverage: {rep['coverage']}")
    lines.append(rep["not_corrected"])
    open(os.path.join(work, "report.txt"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))


# ---------------------------------------------------------------- correct

# Shoal correction: too much water is far worse than too little, so the
# satellite depth is made shallower by a shift sized from measured errors.
# The shift is a function of the PREDICTED depth (all that is known in the
# field), piecewise linear through one value per predicted-depth bin, held
# constant beyond the end bins, and its slope is capped below 1 so the
# corrected depth never decreases as the predicted depth increases. Each
# bin's shift is the larger of what the chart soundings and the held-out
# ICESat-2 points need for at most DEEPER_MAX_SHARE of them to be more than
# DEEPER_TOL too deep. Sized on one half of each truth, checked on the other
# half (chart soundings split by 10 km cell, ICESat-2 hold-out split by pass).
CORR_BINS = [(0, 2), (2, 5), (5, 10), (10, 20)]
CORR_MAX_SLOPE = 0.9
# Below this corrected depth the satellite cannot be trusted to see a
# shoal (pilot 2026-10-11: chart 0-2 m soundings read 2-4 m), so the pixel
# is published as keep-out, 0-2 m and not navigable, instead of a depth.
# 3.5 m caught 94 % of the check half's 0-2 m chart soundings (300; 4.0 m
# caught 95 %, within sampling noise) and turned 11 % of the area keep-out.
KEEPOUT_BELOW = 3.5
KEEPOUT_VALUE = 0.0       # keep-out pixels in sdb_final.tif


def shift_needed(e):
    """Shift (m) that leaves at most DEEPER_MAX_SHARE of errors e above DEEPER_TOL."""
    if len(e) < 30:
        return None
    return max(0.0, float(np.percentile(e, 100 * (1 - DEEPER_MAX_SHARE))) - DEEPER_TOL)


def shift_curve(knots):
    """knots: [(predicted depth, shift)], sorted. Returns f(d) piecewise
    linear, constant outside, slope capped at CORR_MAX_SLOPE."""
    xs = [k[0] for k in knots]; ys = [k[1] for k in knots]
    for i in range(1, len(xs)):            # cap rising slopes so d - f(d) never decreases
        ys[i] = min(ys[i], ys[i - 1] + CORR_MAX_SLOPE * (xs[i] - xs[i - 1]))
    for i in range(len(xs) - 2, -1, -1):   # and falling ones, by symmetry of the cap
        ys[i] = min(ys[i], ys[i + 1] + CORR_MAX_SLOPE * (xs[i + 1] - xs[i]))
    xs = np.array(xs, float); ys = np.array(ys, float)
    return (lambda d: np.interp(d, xs, ys)), list(zip(xs.tolist(), ys.tolist()))


CV_MARGINS = (0.0, 0.01, 0.02, 0.03)   # sizing target = 5 % minus this; the smallest that passes both folds is used


def size_curve(sets, target):
    """Shift curve sized on `sets` ({name: (truth, prediction)}):
    1. per predicted-depth bin, the larger of the shifts the truths need;
    2. then, per TRUE depth band from 2 m down (0-2 m is the keep-out's
       job), raise the knot nearest the failing points' median prediction
       in 0.05 m steps until at most `target` of each band is more than
       DEEPER_TOL too deep after the shift and the keep-out cutoff.
    Returns (f, knots after the slope cap, per-bin detail)."""
    knots, detail = [], []
    for lo, hi in CORR_BINS:
        need = {}
        for name, (t, pred) in sets.items():
            m = np.isfinite(pred) & (pred >= lo) & (pred < hi)
            need[name] = shift_needed(pred[m] - t[m])
        vals = [v for v in need.values() if v is not None]
        sh = max(vals) if vals else (knots[-1][1] if knots else 0.0)
        knots.append([(lo + hi) / 2.0, sh])
        detail.append({"predicted_bin": f"{lo}-{hi} m", "shift_needed": need, "shift_from_bins": sh})
    f, capped = shift_curve([tuple(k) for k in knots])
    for _ in range(600):
        worst = None
        for name, (t, pred) in sets.items():
            for lo, hi in CORR_BINS[1:]:
                m = np.isfinite(pred) & (t >= lo) & (t < hi)
                if m.sum() < 30:
                    continue
                e = apply_final(f, pred[m]) - t[m]
                share = float((e > DEEPER_TOL).mean())
                if share > target and (worst is None or share > worst[0]):
                    worst = (share, float(np.median(pred[m][e > DEEPER_TOL])))
        if worst is None:
            break
        k = int(np.argmin([abs(x - worst[1]) for x, _ in knots]))
        knots[k][1] += 0.05
        f, capped = shift_curve([tuple(x) for x in knots])
    return f, capped, detail


def apply_final(f, pred):
    """Shoal shift, then the keep-out cutoff."""
    c = np.array(pred, dtype=float)
    m = np.isfinite(c)
    c[m] = np.maximum(0.0, c[m] - f(c[m]))
    c[m & (c < KEEPOUT_BELOW)] = KEEPOUT_VALUE
    return c


def _passes(rows):
    """True when every band from 2 m down that has a verdict passes (0-2 m is the keep-out's, reported only)."""
    return all(r["pass"] for r in rows if r["n"] and not r["band"].startswith("0-"))


def stage_correct(work, seed):
    """Two-fold check: chart soundings split by 10 km cell and the
    held-out ICESat-2 points split by pass into halves A and B. For each
    sizing margin, size on A and check on B, then size on B and check on
    A; the smallest margin with every band from 2 m down passing in both
    checks is chosen, and the published curve is sized on A and B
    together with that margin."""
    import rasterio
    from rasterio.warp import transform
    fit = json.load(open(os.path.join(work, "fit.json")))
    P = np.load(os.path.join(work, ICESAT_FILE))["pts"]
    T = np.load(os.path.join(work, "truth.npz"), allow_pickle=True)
    ds = rasterio.open(os.path.join(work, "sdb.tif"))
    rng = np.random.default_rng(seed + 1)
    hidx = np.array(fit["holdout_idx"], dtype=int)
    hp = pass_ids(P[:, 3])[hidx]
    up = np.unique(hp); rng.shuffle(up)
    hA = np.isin(hp, up[: len(up) // 2])
    S = T["soundings"]
    sx, sy = transform("EPSG:4326", ds.crs, list(S[:, 0]), list(S[:, 1]))
    ck = _cell_keys(np.asarray(sx), np.asarray(sy))
    uc = np.unique(ck); rng.shuffle(uc)
    sA = np.isin(ck, uc[: len(uc) // 2])
    pred_h = _sample(ds, P[hidx, 0], P[hidx, 1], [1])[:, 0]
    pred_s = _sample(ds, S[:, 0], S[:, 1], [1])[:, 0]

    def sets_of(hm, sm):
        return {"icesat": (P[hidx][hm, 2], pred_h[hm]), "chart": (S[sm, 2], pred_s[sm])}
    halves = {"A": sets_of(hA, sA), "B": sets_of(~hA, ~sA)}

    def check(f, sets):
        return {name: band_stats(t, apply_final(f, pr)) for name, (t, pr) in sets.items()}

    chosen, cv = None, []
    for margin in CV_MARGINS:
        target = DEEPER_MAX_SHARE - margin
        folds = {}
        for size_on, check_on in (("A", "B"), ("B", "A")):
            f, capped, _ = size_curve(halves[size_on], target)
            res = check(f, halves[check_on])
            folds[f"size {size_on}, check {check_on}"] = {"knots": capped, "check": res,
                                                          "pass": all(_passes(rows) for rows in res.values())}
        ok = all(v["pass"] for v in folds.values())
        cv.append({"margin": margin, "target": target, "folds": folds, "pass": ok})
        log(f"correct: margin {margin:.0%} (target {target:.0%}): " + "; ".join(
            f"{k}: {'pass' if v['pass'] else 'FAIL'}" for k, v in folds.items()))
        if ok:
            chosen = margin
            break
    if chosen is None:
        log("correct: NO margin passes both folds; publishing nothing as corrected SDB")
        json.dump({"cv": cv, "chosen_margin": None}, open(os.path.join(work, "correct.json"), "w"), indent=1, default=str)
        return

    allsets = {"icesat": (P[hidx, 2], pred_h), "chart": (S[:, 2], pred_s)}
    f, capped, detail = size_curve(allsets, DEEPER_MAX_SHARE - chosen)
    log("correct: published curve (sized on all check data): " + ", ".join(f"({x:.1f} m, {y:.2f} m)" for x, y in capped))
    prof = ds.profile.copy()
    out = os.path.join(work, "sdb_corrected.tif"); final = os.path.join(work, "sdb_final.tif")
    nk = nd = 0
    with rasterio.open(out, "w", **prof) as d1, rasterio.open(final, "w", **prof) as d2:
        for _, win in d1.block_windows(1):
            a = ds.read(1, window=win).astype(float)
            keep = a != ds.nodata
            c = np.full(a.shape, ds.nodata, dtype=np.float32)
            c[keep] = np.maximum(0.0, a[keep] - f(a[keep]))
            d1.write(c, 1, window=win)
            o = c.copy()
            ko = keep & (c < KEEPOUT_BELOW)
            o[ko] = KEEPOUT_VALUE
            nk += int(ko.sum()); nd += int(keep.sum())
            d2.write(o, 1, window=win)
    rep = {"chosen_margin": chosen, "curve": capped, "bins": detail, "cv": cv,
           "keepout": {"below_m": KEEPOUT_BELOW, "share_of_sdb_pixels": nk / max(nd, 1)}}
    json.dump(rep, open(os.path.join(work, "correct.json"), "w"), indent=1, default=str)
    lines = [f"shoal correction, two-fold check (chart by 10 km area, ICESat-2 by pass); chosen sizing margin {chosen:.0%}",
             "published curve (predicted depth, shift): " + ", ".join(f"({x:.1f} m, {y:.2f} m)" for x, y in capped),
             f"keep-out under {KEEPOUT_BELOW} m: {nk / max(nd, 1):.0%} of satellite-depth pixels", ""]
    last = cv[-1]["folds"]
    for fold, v in last.items():
        lines.append(f"{fold}  (curve {', '.join(f'{x:.1f}:{y:.2f}' for x, y in v['knots'])})")
        for name in ("chart", "icesat"):
            lines.append(f"  vs {name:6s}: " + "; ".join(
                f"{r['band']} {r['share_too_deep']:.1%} too deep, median err {r['median_abs_m']:.2f} m" +
                ("" if r["band"].startswith("0-") else f" {'pass' if r['pass'] else 'FAIL'}")
                for r in v["check"][name] if r["n"]))
        lines.append("")
    open(os.path.join(work, "correct.txt"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))


# ---------------------------------------------------------------- cli

STAGES = ("composite", "icesat", "tide", "truth", "fit", "report", "correct")
OUTPUT = {"composite": "composite.vrt", "icesat": "icesat.npz", "tide": "icesat_lat.npz", "truth": "truth.npz", "fit": "sdb.tif", "report": "report.json", "correct": "sdb_corrected.tif"}


def main(argv=None):
    global ICESAT_FILE, PILOT_BOX
    ap = argparse.ArgumentParser(prog="bathy-pilot.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default="data/bathy/LANT/pilot")
    ap.add_argument("--box", default=",".join(str(v) for v in PILOT_BOX), help="W,S,E,N of the 1 degree square")
    ap.add_argument("--chart", default="data/bathy/LANT/charts/07CGD_ENCs.mbtiles", help="district .mbtiles for the truth stage")
    ap.add_argument("--stages", default=",".join(STAGES), help="comma list, default all")
    ap.add_argument("--force", default="", help="comma list of stages to redo even if their output exists")
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--max-cloud", type=float, default=20.0, help="scene cloud cover limit, percent")
    ap.add_argument("--glint-max", type=float, default=0.03, help="B11 reflectance above this is masked as glint")
    ap.add_argument("--min-conf", type=float, default=0.6, help="ATL24 ensemble confidence threshold")
    ap.add_argument("--min-obs", type=int, default=5, help="clear observations needed per pixel")
    ap.add_argument("--holdout", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=24)
    ap.add_argument("--model", choices=("linear", "switch"), default="switch",
                    help="one straight line (pilot v1) or shallow and deep lines blended over 4-6 m")
    ap.add_argument("--no-cells", action="store_true", help="one square-wide fit instead of per 10 km cell")
    ap.add_argument("--use-tide", action="store_true",
                    help="fit, report and correct on the tide-corrected LAT depths (icesat_lat.npz from the tide stage)")
    a = ap.parse_args(argv)
    PILOT_BOX = tuple(float(v) for v in a.box.split(","))
    if a.use_tide:
        ICESAT_FILE = "icesat_lat.npz"
    os.makedirs(a.work, exist_ok=True)
    force = set(filter(None, a.force.split(",")))
    for st in [s for s in a.stages.split(",") if s]:
        if st not in STAGES:
            ap.error(f"unknown stage {st}")
        if os.path.exists(os.path.join(a.work, OUTPUT[st])) and st not in force:
            log(f"{st}: {OUTPUT[st]} exists, skipped")
            continue
        t = time.time()
        if st == "composite":
            stage_composite(a.work, PILOT_BOX, a.months, a.max_cloud, a.glint_max)
        elif st == "icesat":
            stage_icesat(a.work, PILOT_BOX, a.min_conf)
        elif st == "tide":
            stage_tide(a.work, PILOT_BOX)
        elif st == "truth":
            stage_truth(a.work, PILOT_BOX, a.chart)
        elif st == "fit":
            stage_fit(a.work, a.min_obs, a.holdout, a.seed, model=a.model, by_cell=not a.no_cells)
        elif st == "report":
            stage_report(a.work)
        elif st == "correct":
            stage_correct(a.work, a.seed)
        log(f"{st}: {time.time()-t:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
