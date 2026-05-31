"""pipeline/step5_rasterize_bgt.py — Step 5: rasterize BGT polygons into a surface grid.

Step 3 gave us BGT surface polygons (vector); Step 4 gave us the canonical 500×500 / 1 m elevation
grid (raster). This step **rasterizes** the polygons onto that exact grid: for every pixel, decide
which BGT polygon covers it and write a one-byte category code. The result, ``surface.tif``, is the
per-pixel "what am I standing on" map that Step 7 turns into colors.

RASTERIZE = vector -> raster
----------------------------
We use ``rasterio.features.rasterize`` (a binding to GDAL's scanline polygon fill: for each raster
row, find where polygon edges cross it, fill the spans between crossings). This is the standard
tool — numpy/scipy have no polygon rasterizer, and hand-rolling point-in-polygon over 250k pixels
would be slow and pointless. (PostGIS analogue: ``ST_AsRaster`` — same operation, server-side.)

The georeferencing comes from ``cfg.GRID_TRANSFORM_AFFINE`` — the *same* transform Step 4 used — so
``surface.tif`` and ``heightmap.tif`` are pixel-for-pixel registered. We assert that at the end;
it's the whole reason the surface colors will land on the right terrain in Godot.

Category codes (user decisions; colors provisional — refined at Step 7):
    0 unknown/background   1 paved   2 unpaved   3 grass   4 water

Run from the repo root:
    uv run python -m pipeline.step5_rasterize_bgt
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import rasterio
from affine import Affine
from rasterio.features import rasterize

# Make ``from pipeline import config`` resolve whether run via ``-m pipeline.…`` or directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import config as cfg  # noqa: E402

LAYERS = [
    "wegdeel", "ondersteunendwegdeel",
    "begroeidterreindeel", "onbegroeidterreindeel", "waterdeel",
]

# Coarse fysiek_voorkomen -> category code. Brick/tile paving (open verharding) and the bricked
# backyards (erf) -> 1 brick; closed paving (gesloten verharding = asphalt/concrete) -> 5 asphalt.
VALUE_MAP = {
    "open verharding": 1, "erf": 1,
    "gesloten verharding": 5,
    "onverhard": 2, "half verhard": 2, "zand": 2,
    "groenvoorziening": 3, "struiken": 3,
}
# Fallback per layer: used for `transitie` (inherits its layer's type), nulls, and any value not in
# VALUE_MAP. waterdeel has no fysiek_voorkomen column, so every feature takes its default (water).
LAYER_DEFAULT = {
    "wegdeel": 1, "ondersteunendwegdeel": 1,
    "begroeidterreindeel": 3, "onbegroeidterreindeel": 2, "waterdeel": 4,
}

# Overlap precedence: water > asphalt > brick > grass > unpaved. We rasterize shapes in ascending
# rank so higher-precedence polygons burn last and win on overlap.
PRECEDENCE_RANK = {2: 0, 3: 1, 1: 2, 5: 3, 4: 4}

CATEGORY_NAMES = {0: "unknown", 1: "brick", 2: "unpaved", 3: "grass", 4: "water", 5: "asphalt"}
# Provisional colors for the embedded colormap so QGIS shows the grid in color immediately.
# Finalized at Step 7.
COLORMAP = {
    0: (40, 40, 40, 255),     # unknown/background — dark
    1: (170, 95, 70, 255),    # brick paving — terracotta
    2: (200, 175, 125, 255),  # unpaved — tan
    3: (95, 150, 70, 255),    # grass — green
    4: (70, 110, 180, 255),   # water — blue
    5: (95, 95, 100, 255),    # asphalt/concrete roads — gray
}


def collect_shapes() -> list[tuple[object, int]]:
    """Read every BGT layer and return (geometry, category_code) pairs, precedence-ordered."""
    shapes: list[tuple[object, int]] = []
    for layer in LAYERS:
        gdf = gpd.read_file(cfg.BGT_GPKG, layer=layer)
        has_fv = "fysiek_voorkomen" in gdf.columns
        unmapped: dict[str, int] = {}
        for geom, fv in zip(gdf.geometry, gdf["fysiek_voorkomen"] if has_fv else [None] * len(gdf)):
            if geom is None or geom.is_empty:
                continue
            code = VALUE_MAP.get(fv)
            if code is None:
                code = LAYER_DEFAULT[layer]
                if fv not in (None, "transitie"):   # a genuinely unexpected value — flag it
                    unmapped[fv] = unmapped.get(fv, 0) + 1
            shapes.append((geom, code))
        print(f"  {layer:<24} {len(gdf):>5} features")
        for val, n in sorted(unmapped.items()):
            print(f"      [warn] unmapped fysiek_voorkomen {val!r} ({n}) -> layer default "
                  f"{LAYER_DEFAULT[layer]} ({CATEGORY_NAMES[LAYER_DEFAULT[layer]]})")
    shapes.sort(key=lambda gc: PRECEDENCE_RANK[gc[1]])
    return shapes


def main() -> int:
    cfg.require_center()
    transform = Affine(*cfg.GRID_TRANSFORM_AFFINE)

    print("Step 5 — rasterizing BGT surface polygons onto the grid")
    print(f"  grid:   {cfg.GRID_WIDTH} x {cfg.GRID_HEIGHT} px @ {cfg.RESOLUTION:g} m  EPSG:{cfg.EPSG_CODE}")
    print(f"  codes:  " + "  ".join(f"{c}={n}" for c, n in CATEGORY_NAMES.items()) + "\n")

    shapes = collect_shapes()
    surface = rasterize(
        shapes=shapes,
        out_shape=(cfg.GRID_HEIGHT, cfg.GRID_WIDTH),
        transform=transform,
        fill=0,                 # background = unknown (no polygon covers the pixel)
        all_touched=False,      # pixel-center rule (standard); sub-meter slivers may drop
        dtype="uint8",
    )

    profile = {
        "driver": "GTiff", "dtype": "uint8", "count": 1,
        "width": cfg.GRID_WIDTH, "height": cfg.GRID_HEIGHT,
        "crs": cfg.CRS, "transform": transform, "compress": "deflate", "nodata": None,
    }
    cfg.SURFACE_TIF.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(cfg.SURFACE_TIF, "w", **profile) as dst:
        dst.write(surface, 1)
        dst.write_colormap(1, COLORMAP)

    print(f"\n  wrote {cfg.SURFACE_TIF.relative_to(cfg.ROOT)}\n")
    print("  cross-checks:")
    return _verify(surface)


def _verify(surface) -> int:
    import numpy as np

    vals, counts = np.unique(surface, return_counts=True)
    total = surface.size
    values_ok = set(vals.tolist()) <= set(CATEGORY_NAMES)
    for v, c in zip(vals.tolist(), counts.tolist()):
        name = CATEGORY_NAMES.get(v, f"UNEXPECTED({v})")
        print(f"  {v} {name:<9} {c:>7,} px  {c/total:6.1%}")
    if not values_ok:
        print("  [error] unexpected category values present!")

    # Grid-alignment assert: surface.tif must register exactly with heightmap.tif.
    with rasterio.open(cfg.SURFACE_TIF) as s, rasterio.open(cfg.HEIGHTMAP_TIF) as h:
        aligned = (s.transform == h.transform and (s.width, s.height) == (h.width, h.height)
                   and s.crs == h.crs)
    print(f"  grid aligned with heightmap.tif: {'OK' if aligned else 'FAILED — grids differ!'}")
    bg = int((surface == 0).sum())
    print(f"  background (0): {bg/total:.1%}  (expected small — mostly building footprints; "
          "we didn't fetch the pand layer)")
    print(f"\n  load surface.tif in QGIS over the BGT vectors to confirm placement.")

    return 0 if (values_ok and aligned) else 2


if __name__ == "__main__":
    raise SystemExit(main())
