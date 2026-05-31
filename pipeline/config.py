"""pipeline/config.py — single source of truth for the spatial parameters of the
walking-around project.

Every pipeline script imports from here; no bounding box, grid size, or output paths
are hardcoded anywhere else. Keeping this module dependency-light (stdlib only — no
rasterio/geopandas) means importing the config is fast and can never fail because of
the heavy GIS stack.

CRS is EPSG:28992 (Amersfoort / RD New), meters, end-to-end. RD coordinates over the
Netherlands are O(1e5)-O(6e5); a stray ~6.5 or ~3.7e6 means an accidental reprojection.

CENTER (PRIVACY)
----------------
There is no built-in location. The map center is read at import time, in order, from
git-ignored sources only:

    1. CENTER_X / CENTER_Y in .env        (your cached, authoritative center)
    2. rd_xy in data/raw/center.json      (most recent Step-1 geocode)

If neither is present the center is unset (None) — configure GEOCODE_ADDRESS in .env and
run Step 1, or set CENTER_X / CENTER_Y in .env (see .env.example). Nothing about the
location ever enters git.

    uv run python -m pipeline.config     # print a human-readable summary
"""

from __future__ import annotations

import json
from pathlib import Path

# --- Coordinate reference system --------------------------------------------
# Amersfoort / RD New. Units are meters (false easting 155000, false northing 463000).
CRS = "EPSG:28992"
EPSG_CODE = 28992

# --- Box & resolution -------------------------------------------------------
BOX_SIZE = 500.0   # length of the (square) area's side, in meters
RESOLUTION = 1.0   # ground sampling distance, meters per pixel

# --- Filesystem paths -------------------------------------------------------
# Anchored to the repo root (the parent of pipeline/) so scripts resolve the same
# paths no matter what the current working directory is.
ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_PROCESSED = ROOT / "data" / "processed"

# Private overlay sources (both git-ignored): the center never enters git.
ENV_FILE = ROOT / ".env"
CENTER_FILE = DATA_RAW / "center.json"

# Raw downloads (Steps 2-3)
AHN_DTM_TIF = DATA_RAW / "ahn_dtm.tif"
BGT_GPKG = DATA_RAW / "bgt.gpkg"

# Processed artifacts (Steps 4-6)
HEIGHTMAP_TIF = DATA_PROCESSED / "heightmap.tif"
HEIGHTMAP_PREVIEW_PNG = DATA_PROCESSED / "heightmap_preview.png"  # Step 4 sanity preview (8-bit)
SURFACE_TIF = DATA_PROCESSED / "surface.tif"
HEIGHTMAP_PNG = DATA_PROCESSED / "heightmap.png"
HEIGHTMAP_RAW_F32 = DATA_PROCESSED / "heightmap.f32"  # raw float32 meters, Godot reads losslessly
SURFACE_PNG = DATA_PROCESSED / "surface.png"
HEIGHTMAP_META_JSON = DATA_PROCESSED / "heightmap_meta.json"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``KEY=VALUE`` .env file (stdlib only). Missing file -> {}."""
    env: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return env
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


# Parsed once at import. Holds GEOCODE_ADDRESS (Step 1 input) and, once cached,
# CENTER_X / CENTER_Y. .env is git-ignored, so none of this enters version control.
ENV = read_env_file(ENV_FILE)


def _load_center() -> tuple[float | None, float | None, str | None]:
    """Resolve the map center from git-ignored sources only; no built-in fallback.

    Precedence: .env CENTER_X/CENTER_Y -> data/raw/center.json. Returns (None, None,
    None) if unconfigured. Any malformed source is skipped; ``import pipeline.config``
    never fails, even on a fresh clone where Step 1 hasn't run.
    """
    try:
        return float(ENV["CENTER_X"]), float(ENV["CENTER_Y"]), "env"
    except (KeyError, ValueError, TypeError):
        pass
    try:
        rec = json.loads(CENTER_FILE.read_text())
        x, y = rec["rd_xy"]
        return float(x), float(y), "center.json"
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return None, None, None


CENTER_X, CENTER_Y, CENTER_SOURCE = _load_center()
CENTER_IS_CONFIGURED = CENTER_SOURCE is not None

# --- Derived: bounding box & raster grid ------------------------------------
# The grid size is location-independent; the bbox needs a center, so it stays None
# until one is configured.
GRID_WIDTH = round(BOX_SIZE / RESOLUTION)
GRID_HEIGHT = round(BOX_SIZE / RESOLUTION)

if CENTER_IS_CONFIGURED:
    _HALF = BOX_SIZE / 2.0
    XMIN = CENTER_X - _HALF
    YMIN = CENTER_Y - _HALF
    XMAX = CENTER_X + _HALF
    YMAX = CENTER_Y + _HALF
    BBOX = (XMIN, YMIN, XMAX, YMAX)  # (xmin, ymin, xmax, ymax) in RD meters
    # The canonical raster georeferencing, shared by every gridded step (4 = heightmap,
    # 5 = surface) so the rasters are pixel-identical. Stored as the 6 affine coefficients
    # in GDAL/affine order (a, b, c, d, e, f) = (pixel_w, 0, xmin, 0, -pixel_h, ymax) — a plain
    # tuple, not an Affine object, to keep this module stdlib-only. Build with
    # ``affine.Affine(*cfg.GRID_TRANSFORM_AFFINE)``. Row 0 = north (origin at ymax, y step -res).
    GRID_TRANSFORM_AFFINE = (RESOLUTION, 0.0, XMIN, 0.0, -RESOLUTION, YMAX)
else:
    XMIN = YMIN = XMAX = YMAX = None
    BBOX = None
    GRID_TRANSFORM_AFFINE = None


def require_center() -> tuple[float, float]:
    """Return (CENTER_X, CENTER_Y), or raise a clear error if no center is configured."""
    if not CENTER_IS_CONFIGURED:
        raise RuntimeError(
            "No map center configured. Set GEOCODE_ADDRESS in .env and run "
            "`python -m pipeline.step1_geocode_center`, or set CENTER_X / CENTER_Y "
            "in .env (see .env.example)."
        )
    return CENTER_X, CENTER_Y


def summary() -> str:
    """Return a human-readable dump of the spatial config.

    Printed by ``python -m pipeline.config``. The import path itself stays silent —
    side-effect-free imports are the norm, so scripts that ``import pipeline.config``
    don't spam stdout.
    """
    if CENTER_IS_CONFIGURED:
        origin = {
            "env": "   <-- from .env (local, git-ignored)",
            "center.json": "   <-- from data/raw/center.json (local, git-ignored)",
        }[CENTER_SOURCE]
        center = f"X={CENTER_X:,.1f}  Y={CENTER_Y:,.1f}{origin}"
        bbox = str(BBOX)
    else:
        center = "unset (configure .env / run Step 1)"
        bbox = "unset"
    return "\n".join(
        [
            "walking-around — spatial config",
            f"  CRS:        {CRS}",
            f"  center:     {center}",
            f"  box:        {BOX_SIZE:g} m  @  {RESOLUTION:g} m/px  ->  {GRID_WIDTH} x {GRID_HEIGHT} px",
            f"  bbox (RD):  {bbox}",
            f"  root:       {ROOT}",
        ]
    )


if __name__ == "__main__":
    print(summary())
