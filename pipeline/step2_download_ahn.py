"""pipeline/step2_download_ahn.py — Step 2: download the AHN ground-elevation raster.

We fetch a clipped GeoTIFF of the bare-earth elevation covering the 500 m box, saved to
``data/raw/ahn_dtm.tif``. This is the elevation half of the project (BGT surface types are
Step 3); later steps resample it to a 1 m grid and turn it into Godot's heightmap.

DTM vs DSM
----------
AHN publishes two surfaces from the same LiDAR. The **DTM** (Digital Terrain Model) is the
*bare earth* — buildings and vegetation removed — so it's the ground you'd walk on. The
**DSM** (Digital Surface Model) keeps everything the laser hit first (roofs, tree canopy).
We model only the ground, so we use the DTM (coverage ``dtm_05m``), not the DSM.

Consequence to expect: a DTM over a built-up area is full of **NoData voids** where
buildings used to be — the ground under a house was never measured, so those pixels are
holes. That's normal. This script reports the void fraction; Step 4 fills the holes.

WCS, and why not Atom tiles
---------------------------
AHN is served three ways: bulk Atom tile downloads (you'd grab whole map sheets and stitch
them), a WMS (rendered pictures, no real elevation values), and a **WCS** (Web Coverage
Service). WCS is the one that returns *coverage* data — the actual float elevations — and
crucially it accepts a bounding box and clips server-side, so we get exactly our area in one
request with no tile-stitching. The coverage is published natively in EPSG:28992, our working
CRS, so no reprojection happens anywhere.

(There's no PostGIS analogue here — this is a file download, not a spatial query. The raster
we save becomes, at Step 5, the grid that BGT polygons get rasterized onto.)

Run from the repo root:
    uv run python -m pipeline.step2_download_ahn
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import rasterio
import requests

# Make ``from pipeline import config`` resolve whether run via ``-m pipeline.…`` or directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import config as cfg  # noqa: E402

WCS_URL = "https://service.pdok.nl/rws/ahn/wcs/v1_0"
WCS_VERSION = "2.0.1"
COVERAGE_ID = "dtm_05m"          # AHN bare-earth terrain at 0.5 m; dsm_05m would include buildings
WCS_FORMAT = "image/tiff"
HTTP_TIMEOUT = 60                # a ~2 MB raster, larger than the Step 1 geocode JSON

# Sanity window for elevations (meters) in this part of NL. A DTM here sits a hair below
# sea level up to a few meters; anything far outside this means an accidental reprojection
# or the wrong coverage. Generous on purpose — it's a tripwire, not a tight bound.
ELEV_MIN_PLAUSIBLE = -10.0
ELEV_MAX_PLAUSIBLE = 30.0


def build_params(bbox: tuple[float, float, float, float]) -> list[tuple[str, str]]:
    """Build the WCS 2.0.1 GetCoverage query as a list of (key, value) tuples.

    A list — not a dict — because ``subset`` appears twice (once per axis) and a dict can't
    hold duplicate keys. WCS subsetting syntax is ``subset=<axis>(lo,hi)``; the axis labels
    for this coverage are ``x``/``y`` (from DescribeCoverage). requests will percent-encode
    the parens and commas; PDOK accepts that.
    """
    xmin, ymin, xmax, ymax = bbox
    return [
        ("service", "WCS"),
        ("version", WCS_VERSION),
        ("request", "GetCoverage"),
        ("coverageId", COVERAGE_ID),
        ("format", WCS_FORMAT),
        ("subset", f"x({xmin},{xmax})"),
        ("subset", f"y({ymin},{ymax})"),
    ]


def download_coverage(bbox: tuple[float, float, float, float], out_path: Path) -> None:
    """GET the clipped DTM and write it atomically to ``out_path``.

    Raises RuntimeError if the server returns an OWS exception (those come back as XML with
    HTTP 200, so a naive write would save a ``.tif`` that's really an error document).
    """
    resp = requests.get(WCS_URL, params=build_params(bbox), timeout=HTTP_TIMEOUT, stream=True)
    resp.raise_for_status()

    ctype = resp.headers.get("Content-Type", "")
    if "tif" not in ctype.lower():
        # WCS errors are an OWS ServiceExceptionReport (XML), delivered with a 200 status.
        raise RuntimeError(
            f"WCS did not return a GeoTIFF (Content-Type: {ctype!r}). Body:\n"
            f"{resp.text[:1000]}"
        )

    # Stream to a temp file in the destination dir, then atomically replace — never leave a
    # half-written raster if the download is interrupted.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_path.parent, suffix=".tif.part")
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
        os.replace(tmp, out_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def verify(path: Path) -> bool:
    """Open the saved raster and print the same kind of cross-checks Step 1 did.

    Returns True if everything looks plausible (CRS, pixel size, coverage, elevations).
    """
    with rasterio.open(path) as ds:
        epsg = ds.crs.to_epsg() if ds.crs else None
        px, py = ds.res            # (x size, y size) in meters; y reported positive
        b = ds.bounds              # (left, bottom, right, top) in RD meters
        nodata = ds.nodata

        band = ds.read(1)
        if nodata is None:
            valid = band == band   # no nodata declared -> treat all as valid
        else:
            valid = band != nodata
        valid_frac = float(valid.mean())
        vals = band[valid]
        emin = float(vals.min()) if vals.size else float("nan")
        emax = float(vals.max()) if vals.size else float("nan")
        emean = float(vals.mean()) if vals.size else float("nan")

    xmin, ymin, xmax, ymax = cfg.BBOX
    tol = max(px, py)  # one pixel of slack for the box-coverage check
    covers = (b.left <= xmin + tol and b.bottom <= ymin + tol
              and b.right >= xmax - tol and b.top >= ymax - tol)
    crs_ok = epsg == cfg.EPSG_CODE
    px_ok = abs(px - 0.5) < 1e-6 and abs(py - 0.5) < 1e-6
    elev_ok = ELEV_MIN_PLAUSIBLE <= emin and emax <= ELEV_MAX_PLAUSIBLE

    print(f"  CRS:            EPSG:{epsg}  {'OK' if crs_ok else 'FAILED — expected 28992!'}")
    print(f"  size:           {ds.width} x {ds.height} px")
    print(f"  pixel size:     {px:g} x {py:g} m  {'OK' if px_ok else '(expected 0.5 m)'}")
    print(f"  covers the box: {'OK' if covers else 'FAILED — bbox not fully covered!'}")
    print(f"  NoData:         {nodata}")
    print(f"  valid data:     {valid_frac:.1%}  "
          f"(the rest are bare-earth voids — building/veg footprints; Step 4 fills them)")
    print(f"  elevation:      min {emin:.2f}  mean {emean:.2f}  max {emax:.2f} m  "
          f"{'OK' if elev_ok else 'FAILED — implausible, check for reprojection!'}")

    return crs_ok and px_ok and covers and elev_ok


def main() -> int:
    # Fails clearly if no center is configured (fresh clone): run Step 1 or set .env.
    cfg.require_center()

    print("Step 2 — downloading the AHN DTM via WCS")
    print(f"  endpoint:  {WCS_URL}")
    print(f"  coverage:  {COVERAGE_ID}  (bare-earth terrain, 0.5 m, EPSG:{cfg.EPSG_CODE})")
    print(f"  box:       {cfg.BOX_SIZE:g} m, exact (no margin)\n")

    try:
        download_coverage(cfg.BBOX, cfg.AHN_DTM_TIF)
    except Exception as e:  # network / HTTP / OWS exception
        print(f"  [error] WCS download failed: {type(e).__name__}: {e}")
        return 1

    print(f"  wrote {cfg.AHN_DTM_TIF.relative_to(cfg.ROOT)}\n")
    print("  cross-checks:")
    ok = verify(cfg.AHN_DTM_TIF)
    print(f"\n  load it in QGIS (hillshade or singleband pseudocolor) to confirm real relief.")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
