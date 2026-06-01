# 2D, 2.5D, and true 3D — and where geology comes in

A companion to `vector-data-representations.md` and `raster-georeferencing.md`. Those docs cover how
geometry is stored and georeferenced; this one covers a dimensionality question raised by the
spatial-query examples in them: **`ST_Intersects` and `ST_MakeEnvelope` are 2D — what happens with
3D data like geology?**

The short version: this project's terrain is a **2.5D surface**, so 2D queries are the correct
tool, not a limitation. True 3D is a different regime with different data structures and different
functions — and subsurface geology is its flagship use case.

## The spatial queries this project uses are 2D

In the `step3` bbox filter (and its PostGIS analogue):

- **`ST_MakeEnvelope(xmin, ymin, xmax, ymax, srid)`** builds a flat rectangular `POLYGON` in the XY
  plane. There is no Z argument — it is strictly a 2D box.
- **`ST_Intersects(a, b)`** tests overlap **in the XY projection**. If the geometries carry Z
  values, it *ignores them*: two pipes crossing in plan view but 10 m apart vertically still report
  as intersecting. So the bbox query for the 500 m box is a purely horizontal filter — exactly what
  this project needs.

## PostGIS can *store* 3D, but most functions are "2.5D"

The key nuance: PostGIS geometries **can** hold a Z coordinate (`POINTZ`, `POLYGONZ`, …) and even a
fourth "M" measure value (`POINTZM`). The coordinate is stored. But **most topological functions
ignore Z by default**, so storing 3D ≠ computing in 3D. That's why standard PostGIS is often called
**"2.5D"**: it remembers each vertex's height, but its relationship logic is planar.

Two flavors of "3D" matter, and the difference is whether one `(x, y)` can have more than one `z`:

- **2.5D — a single Z per (x, y).** A surface: a heightmap, a DTM, terrain. **This project's
  `heightmap.tif` is exactly this** — one elevation per cell, no overhangs possible. This is why a
  2D envelope suffices for the whole pipeline.
- **True 3D — multiple Z at the same (x, y).** Caves, overhangs, stacked building floors, and —
  the geology case — subsurface **layers**: a sand lens *above* a clay layer *above* bedrock, all
  sharing the same map footprint. A 2.5D surface literally cannot represent this; you need volumes.

## The true-3D toolset

When you genuinely need 3D, PostGIS has functions with `3D` in the name that *do* respect Z:

- `ST_3DIntersects`, `ST_3DDistance`, `ST_3DDWithin`, `ST_3DClosestPoint` — Z-aware relationships.
- `ST_3DExtent` and the **`&&&` operator** — an **n-dimensional bounding box** overlap, the 3D
  analogue of the 2D `&&` that drives the spatial index. So you *can* index and pre-filter in 3D;
  there's just no `ST_3DMakeEnvelope` convenience constructor — you build a 3D box geometry yourself.
- For real volumetric work — solids, polyhedral surfaces, TINs, `ST_Volume`, `ST_3DIntersection`,
  `ST_Extrude` — PostGIS relies on the optional **SFCGAL** extension (a binding to CGAL, a
  computational-geometry library). This is the "serious 3D" mode: more capable, heavier, slower.

## Data structures change in 3D

- **3D vectors** become **TINs** (triangulated irregular networks), **polyhedral surfaces**, and
  **solids** — essentially Blender-style meshes describing a fault plane or a layer boundary.
- **3D rasters** become **voxels** — volumetric pixels: a 3D grid where each cell carries a value
  (lithology, porosity). The natural extension of this project's 2D raster grid into a third axis.

## 3D geology in practice — especially in the Netherlands

Subsurface geology is a flagship 3D-GIS domain, and the Netherlands publishes **national 3D
subsurface models**. From TNO's Geological Survey, served via **DINOloket**:

- **GeoTOP** — a **voxel** model of the shallow subsurface (~100×100×0.5 m cells, each classified by
  lithology). The geological sibling of the AHN/BGT surface data this project uses.
- **DGM** (Digitaal Geologisch Model) — layer-boundary **surfaces** for deeper stratigraphy.

So "Dutch national geodata" includes a 3D voxel stack, not just 2D maps.

The honest practitioner's caveat, though: even PostGIS + SFCGAL is rarely where heavy geological
modeling *lives*. That work usually happens in specialized subsurface tools — Leapfrog, Petrel,
GemPy (open-source, Python), GOCAD/SKUA — which handle implicit modeling, faulting, and stratigraphy
that a general spatial database isn't built for. **PostGIS is excellent for storing and querying 3D
geometry; dedicated geomodelers are for building it.**

## The takeaway for this project

For a walkable terrain map, 2D queries are not a compromise — the ground is a 2.5D surface and a
flat envelope is the right tool. The moment you'd reach for true 3D is when one `(x, y)` needs more
than one `z`: stacked geology, tunnels, multi-storey indoor maps. Then the toolkit shifts:

| | 2D / 2.5D (this project) | True 3D |
|---|---|---|
| Vector | polygons, surfaces (one Z per XY) | solids, polyhedral surfaces, TINs |
| Raster | grid (heightmap) | voxel grid |
| PostGIS predicate | `ST_Intersects`, `ST_MakeEnvelope` | `ST_3DIntersects`, `&&&`, SFCGAL ops |
| Index filter | `&&` (2D bbox) | `&&&` (n-D bbox) |
