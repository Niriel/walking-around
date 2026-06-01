# How vector data is represented through the pipeline

A trace of one polygon — say a patch of grass — as it moves through the pipeline, from where it
lives on PDOK's servers all the way to a colored grid in Godot. The same geometry wears five
different costumes along the way, and looks completely different in each.

## The five representations

| Stage | Where | Format | What a geometry literally *is* |
|---|---|---|---|
| 0. At the source | PDOK's database (PostGIS, *inferred*) | **EWKB** in a `geometry` column | A binary blob in a DB row, spatially indexed |
| 1. On the wire | PDOK OGC API response (`step3`) | **GeoJSON** (text/JSON) | A nested array of coordinate numbers |
| 2. In memory | GeoPandas `GeoDataFrame` (`step3`, `step5`) | **Shapely objects** | A Python object wrapping a C geometry |
| 3. On disk | `data/raw/bgt.gpkg` | **GeoPackage** (= a SQLite DB) | A **binary blob** in a table cell |
| 4. After rasterize | `data/processed/surface.tif` | **GeoTIFF raster** | *Gone* — replaced by pixel values |

Vector all the way through stages 0–3 (different encodings of the same shapes), then a hard,
lossy conversion to raster at stage 4.

## 0. At the source — PostGIS on PDOK's servers

> **A caveat up front:** PDOK doesn't fully publish its serving architecture, so this stage is an
> *educated inference*, not documented fact. But it's a very safe one — PostGIS is the de-facto
> backend for serving vector geodata in the Dutch government stack. Treat the specifics as "this is
> how it's almost certainly done," not gospel.

Before the pipeline touches anything, the BGT polygons already live somewhere — a national
database serving the whole country. That backend is almost certainly **PostGIS**: the spatial
extension to **PostgreSQL** that teaches a plain relational database to store and query geometry.

What does a BGT feature look like there? Reassuringly, **exactly like stage 3 of this pipeline** —
because GeoPackage borrowed the model from PostGIS. A `wegdeel` (road part) is:

- **A row** in a normal PostgreSQL table.
- Its attributes (`fysiek_voorkomen`, IDs, timestamps) are **ordinary typed columns** — TEXT,
  INTEGER, TIMESTAMP.
- Its shape lives in a special **`geometry` column** whose type is e.g. `geometry(MultiPolygon, 28992)`
  — the type pins both the geometry kind and the **SRID** (28992 = RD New).
- The value in that column is stored as **EWKB** — *Extended* Well-Known Binary. It's the WKB from
  stage 3 with the **SRID baked into the bytes**, so the geometry always knows which CRS it's in.
  Same binary blob idea, just self-describing about its coordinate system.

So at the source, a polygon is a compact binary blob in one cell of one row — alongside that row's
text attributes. Not one row per vertex, not a file path.

### The one thing a database adds: a spatial index

The reason PDOK uses a database and not a pile of files is **querying**. When `step3` sends a
`bbox=...` request, the server runs the equivalent of:

```sql
SELECT * FROM wegdeel
WHERE ST_Intersects(geom, ST_MakeEnvelope(233000, 582000, 233500, 582500, 28992));
```

To avoid scanning all ~millions of national rows, PostGIS keeps a **GiST spatial index** on the
`geometry` column. It's an R-tree of **bounding boxes**: every geometry's min/max extent is stored
in a tree, so the database can discard everything far from the 500 m box in log-time, then do the
exact `ST_Intersects` test only on the handful of candidates that remain. This two-phase
"index-filter then exact-test" is the core of why spatial databases are fast.

### Reading the binary back as something legible

In PostGIS you never look at raw EWKB; you ask for a readable form, exactly mirroring the
WKB/WKT/GeoJSON trio from stage 3:

```sql
SELECT ST_AsText(geom)    FROM wegdeel LIMIT 1;   -- WKT:   MULTIPOLYGON(((233000 582000, ...)))
SELECT ST_AsEWKT(geom)    FROM wegdeel LIMIT 1;   -- EWKT:  SRID=28992;MULTIPOLYGON(((...)))
SELECT ST_AsGeoJSON(geom) FROM wegdeel LIMIT 1;   -- the GeoJSON stage 1 receives
```

That last function is, in effect, **what turns stage 0 into stage 1**: the OGC API Features
endpoint runs `ST_AsGeoJSON` (or equivalent) on the query results and streams the text out. So the
JSON the pipeline parses is a *serialization of the database's binary geometry* — same shape,
decoded from EWKB into coordinate text for transport.

> **Could the backend be something else?** Possibly. Large read-only serving layers sometimes sit
> on cloud-optimized formats (vector tiles, FlatGeobuf, GeoParquet) or a GeoPackage rather than a
> live PostGIS instance, for speed. But the *authoring/master* copy of a registry like BGT is
> almost certainly a PostGIS database, and the conceptual model — rows + a binary geometry column +
> a spatial index — is identical whichever of these it is. That model is the thing worth knowing.

## 1. On the wire — GeoJSON, plain text

When `step3_download_bgt.py` calls the PDOK endpoint, the polygon arrives as **text** —
human-readable JSON. One grass feature looks roughly like:

```json
{
  "type": "Feature",
  "properties": { "fysiek_voorkomen": "groenvoorziening", ... },
  "geometry": {
    "type": "Polygon",
    "coordinates": [[[233000.1, 582000.4], [233010.2, 582000.5], ...]]
  }
}
```

At this stage a geometry really *is* "a long list of points." A polygon is an array of `[x, y]`
pairs (the outer ring, plus more arrays for any holes). The coordinates are ~233000, ~582000 — RD
New meters, the O(100,000) range expected for RD coordinates over the Netherlands. A value like
`[6.5, 53.2]` would signal it had silently reprojected to WGS84 degrees.

## 2. In memory — GeoPandas / Shapely objects

**Is GeoPandas just Pandas?** Yes, almost literally. `GeoDataFrame` is a subclass of the pandas
`DataFrame` — the same table-of-rows-and-columns tool used for any tabular data. The one addition
is a special **`geometry` column**.

This line in `step3` does the conversion:

```python
gdf = gpd.GeoDataFrame.from_features(features, crs=cfg.CRS)
```

It takes the GeoJSON features and produces a table where:

- Ordinary attributes (`fysiek_voorkomen`, IDs, etc.) become normal columns, exactly like Pandas.
- The geometry becomes a column of **Shapely objects** — `Polygon`, `MultiPolygon`, etc.

So the format **has** changed from step 1. It's no longer a JSON array of numbers; it's a Python
`Polygon` object. Shapely is a thin wrapper over GEOS (a C++ geometry library), so the coordinates
actually live in efficient C memory, and the object gives you methods like `.area`,
`.intersects()`, `.is_empty` (which `step5` uses: `if geom is None or geom.is_empty`). Think of it
like a NumPy array versus a Python list of numbers — same values, but now a typed object with
operations attached.

Conceptually a `GeoDataFrame` is a **spreadsheet where one column holds shapes instead of text**.

## 3. On disk — GeoPackage, which *is* a database

This line writes it out:

```python
gdf.to_file(cfg.BGT_GPKG, layer=collection, driver="GPKG")
```

A **GeoPackage (`.gpkg`) is literally a SQLite database file.** You could open `data/raw/bgt.gpkg`
with `sqlite3` and run SQL on it. Each "layer" (`wegdeel`, `waterdeel`, …) is a **table**. Each
feature is a **row**. The attributes are ordinary columns (TEXT, INTEGER…).

And the geometry lives in a single **`geom` column whose cell holds a binary blob** — not one row
per point, not a file path. The blob is **WKB (Well-Known Binary)**: a compact byte encoding of the
geometry (a header saying "Polygon, n points" followed by the raw coordinate doubles). GeoPackage
wraps WKB in a small header with the SRID. So one polygon = one BLOB in one cell.

The key mental model: **vector geometry is stored as binary inside a normal database column,
alongside its attributes in the same row.** This is exactly how PostGIS does it too — a `geometry`
column on a regular table — which is why the pipeline's code comments keep noting PostGIS analogues
(`ST_Intersects`, `ST_AsRaster`). All three of those layouts (points, binary blob, path) exist in
the wild, but binary-in-a-column is what virtually every spatial database uses, because it's
compact and fast to parse.

Three text/binary cousins worth knowing by name:

- **WKB** — Well-Known **Binary**: the compact byte form stored in databases.
- **WKT** — Well-Known **Text**: the same thing, human-readable, e.g. `POLYGON((233000 582000, ...))`.
- **GeoJSON** — the JSON form from stage 1.

Same geometry, different clothing.

## 4. After rasterize — converted to pixels, geometry discarded

Rasterization is another conversion — and the most drastic one, because it's **lossy and
one-way**. `step5` reads the polygons back from the GeoPackage into Shapely objects, then:

```python
surface = rasterize(shapes=shapes, out_shape=(500, 500), transform=transform, ...)
```

This "burns" the polygons onto a 500×500 grid: for each pixel, it asks *which polygon covers my
center?* and writes that polygon's category code (1=brick, 3=grass, 4=water…). The output
`surface.tif` is a **raster** — just a grid of one-byte numbers. The individual polygon
boundaries, the vertex coordinates, the attributes — all gone, collapsed into "this pixel is a 3."

So the full arc is: **points-as-text → shape-objects-in-a-table → binary-in-a-database →
grid-of-numbers.**

## Two things worth flagging

- **There is no running database server in this project.** The "database" is the self-contained
  GeoPackage *file*. The code comments mention `ST_Intersects` / `ST_AsRaster` only to show "this is
  what the server-side equivalent would be." Loading `bgt.gpkg` into PostGIS and running the actual
  spatial SQL is a good way to see that server-side equivalent in action.
- **Why convert vector → raster at all?** Because Godot wants a heightmap and a per-pixel color
  grid, not polygons. Game engines render terrain from grids. The vector form is better for
  *editing and precision*; the raster form is better for *fast lookup and rendering*. Knowing when
  to use which is a core GIS judgment call.

## Anatomy of a vector geometry

A few structural details that aren't obvious until you stare at the raw `coordinates`. These apply
to GeoJSON, and the same model (OGC Simple Features) underlies WKT/WKB and Shapely too.

### Closure: first point = last point, written explicitly

A polygon ring is a **LinearRing**, and the GeoJSON spec (RFC 7946 §3.1.6) *requires* the first and
last positions to be **identical**. There's no flag and no auto-closing — you write the closing
point out in full:

```json
"coordinates": [
  [[233000.0, 582000.0],
   [233010.0, 582000.0],
   [233010.0, 582010.0],
   [233000.0, 582010.0],
   [233000.0, 582000.0]]    ← same as the first, repeated
]
```

So a square is **5 points, not 4**. The redundancy is the point: there's no convention a reader has
to *know* — the ring is closed because the data says so literally. A ring that doesn't close is
invalid GeoJSON.

Across the stack the rule is consistent:

- **WKT / WKB** (the database forms): same rule — rings explicitly closed, first vertex repeated.
  So the closing point survives the round-trip into the GeoPackage; it's not invented or dropped.
- **Shapely** (in-memory): forgiving on *input* — you can build `Polygon([(0,0),(1,0),(1,1),(0,1)])`
  with 4 distinct points and it's treated as closed — but it normalizes to explicit closure on
  *output*: `polygon.exterior.coords` gives back **5** points, first repeated. This is why `step5`
  never has to think about it; geometries read from the GeoPackage are already valid closed rings.

One nuance worth knowing: **winding order**. GeoJSON *should* use counter-clockwise exterior
rings and clockwise holes (the right-hand rule), but unlike closure it's a SHOULD, not a MUST —
many real files (including Dutch government exports) ignore it. So robust code never relies on
winding to tell outer rings from holes; it uses ring *position* (see below).

### Ring nesting: `[[[...]]]` is one polygon, not several

The bracket nesting in a `Polygon`'s `coordinates` encodes **rings, not separate shapes**:

```json
"type": "Polygon",
"coordinates": [            ← level 1: the list of RINGS
  [                         ← level 2: one ring = a list of positions
    [233000.0, 582000.0],   ← level 3: one position [x, y]
    ...
    [233000.0, 582000.0]
  ]
]
```

- **Level 3** `[x, y]` — a single position (point).
- **Level 2** `[[x,y], ...]` — one **ring** (a closed loop).
- **Level 1** `[ring, ring, ...]` — the **list of rings** making up the polygon.

A polygon can have **holes** — a grass field with a pond cut out, a courtyard inside a footprint.
Ring `[0]` is always the **exterior** outline; rings `[1..]` are **interior** rings, i.e. holes.
A simple patch with no hole is still wrapped as a one-element list of rings — hence the `[[[...]]]`
even with a single ring. That triple bracket is the *minimum* for a Polygon, not a sign of multiple
shapes.

### When it IS several shapes: MultiPolygon

A **MultiPolygon** adds **one more** bracket level — a list of polygons:

```json
"type": "MultiPolygon",
"coordinates": [            ← list of POLYGONS
  [ [ [x,y], ... ] ],       ←   polygon 1 (its own list of rings)
  [ [ [x,y], ... ] ]        ←   polygon 2
]
```

This is how BGT represents one logical feature made of disconnected pieces — a road surface split
in two, or a lake with a detached pond. This is why `step3`'s cross-check prints `geom_type`: the
BGT layers contain **both** `Polygon` and `MultiPolygon` features.

**Bracket-counting rule of thumb** — count the opening brackets before the first number:

| Type | Brackets | Reads as |
|---|---|---|
| `Point` | `[x, y]` | one position |
| `LineString` | `[[x,y], ...]` | list of positions |
| `Polygon` | `[[[x,y], ...]]` | list of **rings** (ring 0 = outline, rest = holes) |
| `MultiPolygon` | `[[[[x,y], ...]]]` | list of **polygons** |

Three brackets → one polygon (possibly with holes). Four → multiple polygons. Depth tells you type.

### Validity: a valid polygon can't self-intersect

This comes from the **OGC Simple Features** model that all these formats share — the word "Simple"
is doing real work. For a polygon to be *valid*:

- Each **ring must be "simple"** — it can't cross itself. A figure-eight loop is not a valid ring.
- Rings must be **closed** (first = last).
- **Holes must lie inside the exterior** and not poke outside it.
- Rings may **touch** at a single point but must not **overlap** or cross — a hole can kiss the
  exterior at one vertex, but not straddle it.
- No repeated/spike vertices doubling back on a zero-width sliver.

The classic invalid case is the **bowtie** — a square traversed in the wrong vertex order so the
edges cross in the middle:

```
□──────□
 ╲    ╱
  ╲  ╱
   ╳        ← self-intersection: "what's inside?" has no consistent answer → invalid
  ╱  ╲
 ╱    ╲
□──────□
```

The rule exists because area, point-in-polygon, and intersection all become ambiguous when a
boundary crosses itself — "which side is inside?" stops having one answer.

**But the file format will happily store invalid geometry.** GeoJSON and the WKB in a GeoPackage
are just containers; nothing validates on write. Validity is checked on demand by the geometry
engine (GEOS via Shapely, or PostGIS). Invalid polygons show up routinely in real data —
digitizing errors, reprojection slivers, editing spikes — so "the data is dirty" is the default
state of GIS work.

Detect and repair in Shapely:

```python
gdf.geometry.is_valid              # boolean mask — which rows are invalid
gdf[~gdf.geometry.is_valid]        # inspect the offenders

from shapely import make_valid
gdf.geometry = gdf.geometry.apply(make_valid)   # repair (Shapely 2.x)
```

`make_valid` resolves a bowtie by **splitting it into two valid polygons** (often a MultiPolygon) —
it doesn't guess intent, it just produces something topologically legal. The older `geom.buffer(0)`
trick does much the same and appears in lots of GIS code, but `make_valid` is the explicit,
preferred form now.

PostGIS mirrors this exactly: `ST_IsValid(geom)`, `ST_IsValidReason(geom)` (says *why* and
*where*), and `ST_MakeValid(geom)`. In `step5`, `rasterize` is fairly tolerant, but operations like
`.intersection()` or `.area` can throw `TopologyException` on an invalid polygon, so an `is_valid`
check on the BGT layers is cheap insurance before the rasterizer.

## See it for yourself

You can open the GeoPackage with `sqlite3` to confirm "spatial data" is just normal database rows
plus a binary geometry column:

```bash
sqlite3 data/raw/bgt.gpkg ".tables"                       # layers = tables
sqlite3 data/raw/bgt.gpkg "SELECT name FROM gpkg_geometry_columns;"
sqlite3 data/raw/bgt.gpkg "SELECT fysiek_voorkomen, length(geom) FROM begroeidterreindeel LIMIT 5;"
```

The last query shows the `geom` column's byte length per row — proof it's a binary blob, not text
or a path.
