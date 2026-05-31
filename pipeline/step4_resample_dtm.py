"""pipeline/step4_resample_dtm.py — Step 4: resample the DTM to the grid and fill voids.

Step 2 gave us raw AHN elevation at 0.5 m with ~44% NoData holes (bare-earth voids where
buildings/vegetation were stripped). This step produces the project's **canonical elevation
grid**: exactly 500×500 px at 1 m, pixel-aligned to the box, with every hole filled so Godot
has a continuous surface to walk on (you can't stand on a NoData pixel).

Two distinct operations, in order:

1. RESAMPLE 0.5 m -> 1 m. We're *downsampling* (output pixels are bigger), exactly 2× — each
   output pixel covers a 2×2 block of source pixels. We use **average**: the mean of the block.
   For an integer-factor downsample that's the correct anti-aliasing choice (every source pixel
   contributes once, no aliasing), it smooths LiDAR speckle, and — crucially for our void-heavy
   data — GDAL's average *skips* NoData and only emits NoData when all four inputs are void, so it
   slightly *shrinks* the holes. (Bilinear would do the opposite: propagate NoData and grow them.)

2. FILL the remaining voids with ``rasterio.fill.fillnodata`` — a four-direction conic search that
   interpolates each hole from its surrounding valid pixels via inverse-distance weighting. Ideal
   for "fairly continuously varying rasters such as elevation models" (its docstring's words).

GRID ALIGNMENT
--------------
The output georeferencing comes from ``cfg.GRID_TRANSFORM_AFFINE`` — the *same* transform Step 5
will rasterize BGT onto. Defining it once in config is what guarantees the elevation grid and the
surface grid are pixel-for-pixel identical. Row 0 = north (origin at ymax), pinned now so later
steps don't mirror the map.

NO MARGIN (deliberate)
----------------------
Step 2 fetched the box exactly, no margin, so edge pixels here resample from in-box data only —
expect minor artifacts on the boundary row/column. That was a conscious choice to see what they
look like; inspect the edges in QGIS.

Run from the repo root:
    uv run python -m pipeline.step4_resample_dtm
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from rasterio.enums import Resampling
from rasterio.fill import fillnodata
from rasterio.warp import reproject

# Make ``from pipeline import config`` resolve whether run via ``-m pipeline.…`` or directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import config as cfg  # noqa: E402

RESAMPLING = Resampling.average   # NoData-aware mean of each 2×2 source block (user decision)
FILL_MAX_SEARCH = 100.0           # px; max distance fillnodata searches for source values
FILL_SMOOTHING = 0                # 3×3 smoothing passes over filled pixels (0 = none)

ELEV_MIN_PLAUSIBLE = -10.0
ELEV_MAX_PLAUSIBLE = 30.0


def main() -> int:
    cfg.require_center()
    transform = Affine(*cfg.GRID_TRANSFORM_AFFINE)

    print("Step 4 — resampling the DTM to the grid and filling voids")
    print(f"  in:        {cfg.AHN_DTM_TIF.relative_to(cfg.ROOT)}  (0.5 m)")
    print(f"  grid:      {cfg.GRID_WIDTH} x {cfg.GRID_HEIGHT} px @ {cfg.RESOLUTION:g} m  EPSG:{cfg.EPSG_CODE}")
    print(f"  resample:  {RESAMPLING.name}   fill: fillnodata(search={FILL_MAX_SEARCH:g}px)\n")

    with rasterio.open(cfg.AHN_DTM_TIF) as src:
        nodata = src.nodata if src.nodata is not None else np.float32(3.4028235e38)
        grid = np.full((cfg.GRID_HEIGHT, cfg.GRID_WIDTH), nodata, dtype="float32")
        reproject(
            source=rasterio.band(src, 1),
            destination=grid,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=transform, dst_crs=src.crs,
            src_nodata=nodata, dst_nodata=nodata,
            resampling=RESAMPLING,
        )

    # Void accounting before/after fill.
    valid = grid != nodata
    valid_frac_before = float(valid.mean())
    filled = fillnodata(
        grid.copy(), mask=valid.astype("uint8"),
        max_search_distance=FILL_MAX_SEARCH, smoothing_iterations=FILL_SMOOTHING,
    )
    voids_after = int(np.count_nonzero(filled == nodata))

    # If everything filled, write a clean continuous surface (no nodata tag); otherwise keep it.
    out_nodata = None if voids_after == 0 else nodata
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "width": cfg.GRID_WIDTH, "height": cfg.GRID_HEIGHT,
        "crs": cfg.CRS, "transform": transform,
        "compress": "deflate", "nodata": out_nodata,
    }
    cfg.HEIGHTMAP_TIF.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(cfg.HEIGHTMAP_TIF, "w", **profile) as dst:
        dst.write(filled, 1)

    _write_preview(filled, out_nodata, cfg.HEIGHTMAP_PREVIEW_PNG)

    print(f"  wrote {cfg.HEIGHTMAP_TIF.relative_to(cfg.ROOT)}")
    print(f"  wrote {cfg.HEIGHTMAP_PREVIEW_PNG.relative_to(cfg.ROOT)}  (8-bit sanity preview)\n")
    print("  cross-checks:")
    return _verify(valid_frac_before, voids_after)


def _write_preview(arr: np.ndarray, nodata, path: Path) -> None:
    """8-bit grayscale stretch of the valid elevation range. Row 0 = north (top)."""
    from PIL import Image
    finite = arr if nodata is None else arr[arr != nodata]
    lo, hi = float(finite.min()), float(finite.max())
    span = hi - lo or 1.0
    scaled = np.clip((arr - lo) / span, 0.0, 1.0)
    if nodata is not None:
        scaled[arr == nodata] = 0.0
    Image.fromarray((scaled * 255).astype("uint8"), mode="L").save(path)


def _verify(valid_frac_before: float, voids_after: int) -> int:
    with rasterio.open(cfg.HEIGHTMAP_TIF) as ds:
        epsg = ds.crs.to_epsg() if ds.crs else None
        px, py = ds.res
        b = ds.bounds
        a = ds.read(1)

    xmin, ymin, xmax, ymax = cfg.BBOX
    crs_ok = epsg == cfg.EPSG_CODE
    size_ok = ds.width == cfg.GRID_WIDTH and ds.height == cfg.GRID_HEIGHT
    px_ok = abs(px - cfg.RESOLUTION) < 1e-9 and abs(py - cfg.RESOLUTION) < 1e-9
    bounds_ok = all(abs(g - e) < 1e-6 for g, e in
                    zip((b.left, b.bottom, b.right, b.top), (xmin, ymin, xmax, ymax)))
    emin, emean, emax = float(a.min()), float(a.mean()), float(a.max())
    elev_ok = ELEV_MIN_PLAUSIBLE <= emin and emax <= ELEV_MAX_PLAUSIBLE

    print(f"  CRS:            EPSG:{epsg}  {'OK' if crs_ok else 'FAILED — expected 28992!'}")
    print(f"  size:           {ds.width} x {ds.height} px  {'OK' if size_ok else 'FAILED!'}")
    print(f"  pixel size:     {px:g} x {py:g} m  {'OK' if px_ok else 'FAILED (expected 1 m)'}")
    print(f"  bounds == box:  {'OK' if bounds_ok else 'FAILED — grid not aligned to box!'}")
    print(f"  valid pre-fill: {valid_frac_before:.1%}  (average resample shrank the 0.5 m voids)")
    print(f"  voids post-fill:{voids_after}  {'OK (continuous surface)' if voids_after == 0 else 'remain'}")
    print(f"  elevation:      min {emin:.2f}  mean {emean:.2f}  max {emax:.2f} m  "
          f"{'OK' if elev_ok else 'FAILED — implausible!'}")
    print(f"\n  hillshade heightmap.tif in QGIS; inspect the box edges for resampling artifacts.")

    return 0 if (crs_ok and size_ok and px_ok and bounds_ok and elev_ok and voids_after == 0) else 2


if __name__ == "__main__":
    raise SystemExit(main())
