# How raster (GeoTIFF) data is georeferenced

A companion to `vector-data-representations.md`, which traces a *polygon* through the pipeline. This
doc covers the *raster* side: when `heightmap.tif` or `surface.tif` is opened in QGIS, it lands in
the right place on the map even though it's just a 2D grid of values. Where do the coordinates come
from?

## A GeoTIFF = pixels + a tiny bit of header metadata

At heart, `surface.tif` really is a plain 500×500 grid of bytes: pixel `[row, col]` holds a
category code, with no coordinate attached to the individual pixel. A raster never stores a
coordinate *per pixel* — that would be wasteful and redundant. Instead it stores **one small rule**
for converting any `(row, col)` into a world coordinate, plus a label saying which world those
coordinates live in. Two pieces of header metadata:

1. **The affine transform** (the "geotransform") — *where* the grid sits and how big each pixel is.
2. **The CRS / SRID** — *which coordinate system* those numbers mean.

That's it. QGIS reads those from the header and can then place all 250,000 pixels correctly.

This is the key distinction: a raster *image* (PNG, ordinary TIFF) is pixels only; a raster
*dataset* (GeoTIFF) is pixels **plus** this header.

## 1. The affine transform — `cfg.GRID_TRANSFORM_AFFINE`

This is the line doing the work, in `pipeline/config.py`:

```python
GRID_TRANSFORM_AFFINE = (RESOLUTION, 0.0, XMIN, 0.0, -RESOLUTION, YMAX)
#                        ( a,         b,   c,    d,   e,           f )
```

Those six numbers `(a, b, c, d, e, f)` define a linear map from pixel space to world space:

```
world_x = a·col + b·row + c
world_y = d·col + e·row + f
```

Plugging in this project's values (`a = 1.0`, `e = -1.0`, `c = XMIN`, `f = YMAX`, `b = d = 0`):

```
world_x = XMIN + col·1.0     →  XMIN at the left edge, +1 m per column eastward
world_y = YMAX - row·1.0     →  YMAX at the top,       -1 m per row (downward)
```

So the six numbers encode four facts:

- **`c, f` = `(XMIN, YMAX)`** — the world coordinate of the **top-left corner** of the grid (the
  origin). For this project that's the northwest corner of the 500 m box, in RD meters (~O(100,000)).
- **`a` = `RESOLUTION` = 1.0** — pixel **width**: each column is 1 m of ground.
- **`e` = `-RESOLUTION` = -1.0** — pixel **height**, and crucially **negative**. This is the gotcha
  worth internalizing: image rows count *downward* (row 0 at top), but world northing counts
  *upward* (north = bigger Y). The minus sign flips that axis, so "row 0 = north edge." A positive
  `e` would render the map upside-down. The `config.py` comment calls this out: "Row 0 = north
  (origin at ymax, y step -res)."
- **`b, d` = 0** — the off-diagonal terms. Zero means the grid is axis-aligned (north-up, no
  rotation or shear). They're only nonzero for a rotated or skewed raster.

To find where pixel `(row=10, col=20)` lands, QGIS computes `(XMIN + 20, YMAX − 10)` — done. The
entire georeferencing of 250k pixels is six numbers.

## 2. The CRS — what those numbers *mean*

The transform yields a pair like `(233020, 581990)`, but bare numbers are meaningless without a
coordinate system. That's the second piece of header metadata: the **CRS**, written from `step5`'s
profile:

```python
profile = { ..., "crs": cfg.CRS, "transform": transform, ... }   # cfg.CRS = "EPSG:28992"
```

`EPSG:28992` tells QGIS "these are RD New meters, with the false easting/northing and projection the
EPSG registry defines for code 28992." Without it, QGIS would have raw numbers but no way to overlay
them on anything else — it couldn't tell Dutch RD meters from UTM meters or anything else.

**This is what makes the two rasters line up with each other and with the BGT vectors:** they all
declare EPSG:28992, so QGIS places them in the same world.

In GeoTIFF specifically, the CRS is stored as embedded **GeoKeys** (a TIFF-tag convention from the
GeoTIFF spec); the transform is stored either as `ModelPixelScale` + `ModelTiepoint` tags or a
`ModelTransformation` tag. rasterio/GDAL read and write all of that for you — you just hand it `crs`
and `transform`.

## Why the two rasters register pixel-for-pixel

This is the payoff of `step5` reusing the *same* transform as `step4`. Because `heightmap.tif` and
`surface.tif` share identical `transform`, `width`/`height`, and `crs`, pixel `[r, c]` in one is the
**exact same square meter of ground** as `[r, c]` in the other. That's why `step5` asserts it:

```python
aligned = (s.transform == h.transform and (s.width, s.height) == (h.width, h.height)
           and s.crs == h.crs)
```

If those matched only approximately, Godot would drape the surface colors slightly off from the
elevation — roads sliding into the canal. Sharing one transform makes misalignment impossible by
construction.

## The mental model

- A **plain image** (PNG, ordinary TIFF): pixels only. "Pixel (0,0) is top-left" is all the
  geometry there is.
- A **GeoTIFF**: the same pixels **plus a header** saying *(a)* here's the corner and pixel size
  (the affine transform), *(b)* here's the coordinate system (the CRS). From those, every pixel's
  ground footprint is computable.
- A **world file** (`.tfw`, `.pgw`): the same six transform numbers in a tiny sidecar text file, for
  image formats that can't embed them. Same idea, external instead of in-header.

So it's not coordinates per pixel — it's **a corner, a pixel size, an axis-flip, and a CRS label.**
Six numbers and a code, and QGIS reconstructs the rest.

## See it for yourself

`gdalinfo` prints exactly these two pieces of metadata:

```bash
gdalinfo data/processed/surface.tif
```

Look for the **`Origin`** (that's `c, f` = top-left corner), **`Pixel Size`** (`a, e` =
`1, -1` — note the negative height), the **`Coordinate System` / GeoKeys** block (EPSG:28992), and
the **`Corner Coordinates`** GDAL computes for you by applying the transform to the four corners.
Run it on `heightmap.tif` too and confirm Origin / Pixel Size / CRS match — that's the same equality
`step5` asserts, by eye.
