"""pipeline/step6_export_godot.py — Step 6: export Godot-ready assets.

Godot can't open GeoTIFFs, so this step re-encodes the processed rasters into formats the engine
loads, plus a sidecar JSON so the real-world heights can be reconstructed.

WHY A RAW FLOAT FILE (not just the 16-bit PNG)
----------------------------------------------
A probe against Godot 4.6.3 showed ``Image.load()`` on a 16-bit grayscale PNG returns FORMAT_L8 —
Godot *down-converts to 8 bits*, leaving only 256 height levels (visible terracing). So we don't
trust the PNG for elevation. Instead we also dump a **raw float32** heightmap in meters, which Godot
reads losslessly with ``FileAccess.get_float()`` — no decode, no precision loss. The 16-bit PNG is
kept as the standard, QGIS-inspectable GIS deliverable.

ORIENTATION
-----------
The processed rasters have their transform origin at ymax with a negative y-step, so **array row 0 =
north**. rasterio reads row 0 = top, Pillow writes row 0 = top, and the f32 dump is row-major from
row 0 — so row 0 = north end-to-end. Step 7 must keep that (row 0 -> north) or it mirrors the map.

Outputs (all 500×500):
    heightmap.png        16-bit grayscale, [min,max] elev -> [0,65535]   (GIS deliverable)
    heightmap.f32        raw float32 meters, little-endian, row 0 = north (Godot terrain)
    heightmap_meta.json  min/max/bbox/res/encoding/orientation            (reconstruct meters)
    surface.png          8-bit, pixel value = category code 0..5          (Godot shader input)

Run from the repo root:
    uv run python -m pipeline.step6_export_godot
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image

# Make ``from pipeline import config`` resolve whether run via ``-m pipeline.…`` or directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import config as cfg  # noqa: E402

U16_MAX = 65535


def main() -> int:
    cfg.require_center()
    print("Step 6 — exporting Godot-ready assets\n")

    with rasterio.open(cfg.HEIGHTMAP_TIF) as ds:
        h = ds.read(1).astype("float32")
    with rasterio.open(cfg.SURFACE_TIF) as ds:
        surf = ds.read(1).astype("uint8")

    min_elev, max_elev = float(h.min()), float(h.max())
    span = max_elev - min_elev or 1.0

    cfg.DATA_PROCESSED.mkdir(parents=True, exist_ok=True)

    # --- heightmap.png: 16-bit grayscale, linear [min,max] -> [0,65535] ----------------
    u16 = np.round((h - min_elev) / span * U16_MAX).astype("uint16")
    Image.fromarray(u16).save(cfg.HEIGHTMAP_PNG)   # uint16 array -> mode "I;16"

    # --- heightmap.f32: raw little-endian float32 meters, row-major, row 0 = north ------
    h.astype("<f4").tofile(cfg.HEIGHTMAP_RAW_F32)

    # --- heightmap_meta.json: everything needed to rebuild meters -----------------------
    meta = {
        "width": cfg.GRID_WIDTH, "height": cfg.GRID_HEIGHT,
        "resolution_m": cfg.RESOLUTION, "crs": cfg.CRS, "bbox_rd": list(cfg.BBOX),
        "min_elev_m": min_elev, "max_elev_m": max_elev,
        "orientation": "row0=north",
        "png_encoding": "uint16; elev_m = min_elev_m + (px / 65535) * (max_elev_m - min_elev_m)",
        "f32_encoding": "raw float32, little-endian, row-major, meters (no decode needed)",
    }
    cfg.HEIGHTMAP_META_JSON.write_text(json.dumps(meta, indent=2) + "\n")

    # --- surface.png: 8-bit, pixel value = category code (0..5) -------------------------
    Image.fromarray(surf, mode="L").save(cfg.SURFACE_PNG)

    for p in (cfg.HEIGHTMAP_PNG, cfg.HEIGHTMAP_RAW_F32, cfg.HEIGHTMAP_META_JSON, cfg.SURFACE_PNG):
        print(f"  wrote {p.relative_to(cfg.ROOT)}")
    print(f"\n  elevation range: {min_elev:.2f} … {max_elev:.2f} m\n")
    print("  cross-checks:")
    return _verify(h, surf, min_elev, span)


def _verify(h: np.ndarray, surf: np.ndarray, min_elev: float, span: float) -> int:
    ok = True

    # heightmap.png: dtype/size + round-trip within one quantization step.
    png = np.asarray(Image.open(cfg.HEIGHTMAP_PNG))
    png_ok = png.dtype == np.uint16 and png.shape == (cfg.GRID_HEIGHT, cfg.GRID_WIDTH)
    decoded = min_elev + png.astype("float64") / U16_MAX * span
    max_err = float(np.abs(decoded - h).max())
    quant = span / U16_MAX
    rt_ok = max_err <= quant + 1e-6
    ok = ok and png_ok and rt_ok
    print(f"  heightmap.png:  {png.dtype} {png.shape[1]}x{png.shape[0]}  "
          f"{'OK' if png_ok else 'FAILED'}")
    print(f"  png round-trip: max err {max_err*1000:.2f} mm  (<= 1 step {quant*1000:.2f} mm)  "
          f"{'OK' if rt_ok else 'FAILED'}")

    # heightmap.f32: exact size + bit-exact reload.
    expected_bytes = cfg.GRID_WIDTH * cfg.GRID_HEIGHT * 4
    actual_bytes = cfg.HEIGHTMAP_RAW_F32.stat().st_size
    reload = np.fromfile(cfg.HEIGHTMAP_RAW_F32, dtype="<f4").reshape(cfg.GRID_HEIGHT, cfg.GRID_WIDTH)
    f32_ok = actual_bytes == expected_bytes and np.array_equal(reload, h)
    ok = ok and f32_ok
    print(f"  heightmap.f32:  {actual_bytes:,} bytes (expect {expected_bytes:,}), bit-exact reload  "
          f"{'OK' if f32_ok else 'FAILED'}")

    # surface.png: mode/size, values in range, histogram matches the source raster.
    sp = np.asarray(Image.open(cfg.SURFACE_PNG))
    vals_ok = sp.shape == (cfg.GRID_HEIGHT, cfg.GRID_WIDTH) and set(np.unique(sp)) <= set(range(6))
    hist_ok = np.array_equal(sp, surf)
    ok = ok and vals_ok and hist_ok
    print(f"  surface.png:    {sp.dtype} {sp.shape[1]}x{sp.shape[0]}  values⊆0..5 {vals_ok}  "
          f"matches surface.tif {hist_ok}")

    print(f"\n  Godot reads heightmap.f32 (lossless meters) + surface.png (category codes).")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
