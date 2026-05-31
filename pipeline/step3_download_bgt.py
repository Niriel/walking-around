"""pipeline/step3_download_bgt.py — Step 3: download BGT surface-type polygons.

This is the *vector* half of the project (Step 2 fetched the *raster* elevation). BGT —
Basisregistratie Grootschalige Topografie, the Dutch large-scale topography registry — maps
the country as polygons: every road, verge, lawn, canal, building footprint, etc. We pull the
polygons covering our 500 m box into ``data/raw/bgt.gpkg``, one layer per feature class. Step 5
will "burn" these polygons onto the elevation grid so each pixel knows its surface type, and
Step 7 colors the terrain by it.

WFS vs OGC API Features
-----------------------
The project brief said "BGT via WFS." It turns out BGT no longer has a general WFS — PDOK now
serves it through an **OGC API Features** endpoint. Same idea, newer standard:

  - WFS (2000s): SOAP/GET with XML request params, returns GML (an XML geometry dialect).
  - OGC API Features (2019+): a plain REST API — ``/collections/<name>/items?bbox=...`` — that
    returns **GeoJSON**. Easier to consume, paginated with web-style ``next`` links.

A "collection" here is one BGT feature class (e.g. ``wegdeel`` = road parts). We request five.

CRS gotcha (important)
----------------------
OGC API Features defaults to **CRS84** (WGS84 lon/lat) for both input and output. If you don't
say otherwise you'll get degrees, not meters, silently breaking the RD-end-to-end invariant. We
pass ``crs`` (output) AND ``bbox-crs`` (how to read our bbox) as the EPSG:28992 URI so both the
filter and the returned coordinates stay in RD New meters.

Paging
------
The server caps features per response. We pass a large ``limit`` and then follow the
``rel="next"`` link until there isn't one. PDOK does *not* report a total match count
(``numberMatched`` is absent), so "did we get everything?" can only be answered by exhausting the
next-links — never by comparing to a total. (In PostGIS this whole fetch is one
``SELECT * FROM <layer> WHERE ST_Intersects(geom, ST_MakeEnvelope(...))`` — the OGC API ``bbox``
is that spatial filter, executed server-side.)

Run from the repo root:
    uv run python -m pipeline.step3_download_bgt
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import geopandas as gpd
import requests

# Make ``from pipeline import config`` resolve whether run via ``-m pipeline.…`` or directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import config as cfg  # noqa: E402

BGT_OGC_URL = "https://api.pdok.nl/lv/bgt/ogc/v1"
# CRS as an OpenGIS URI — the form OGC API Features expects, not a bare "EPSG:28992".
RD_CRS_URI = f"http://www.opengis.net/def/crs/EPSG/0/{cfg.EPSG_CODE}"

# The five surface classes we color in Step 5/7: roads, sidewalks/verges, vegetated terrain,
# bare terrain, water. (BGT has ~49 collections; these are the ground-cover ones we care about.)
COLLECTIONS = [
    "wegdeel",                 # road parts
    "ondersteunendwegdeel",    # supporting road parts (sidewalks, verges)
    "begroeidterreindeel",     # vegetated terrain (grass, planting)
    "onbegroeidterreindeel",   # unvegetated terrain (sand, paving outside roads)
    "waterdeel",               # water bodies
]

PAGE_LIMIT = 1000   # max features per response; we then follow rel="next" links
HTTP_TIMEOUT = 60


def fetch_collection(session: requests.Session, collection: str,
                     bbox: tuple[float, float, float, float]) -> list[dict]:
    """Fetch every feature of one BGT collection within bbox, following next-links.

    Returns a list of GeoJSON Feature dicts. Raises RuntimeError if a response isn't JSON
    (e.g. an HTML error page), so we never feed garbage into geopandas.
    """
    xmin, ymin, xmax, ymax = bbox
    params: dict | None = {
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bbox-crs": RD_CRS_URI,
        "crs": RD_CRS_URI,
        "limit": PAGE_LIMIT,
        "f": "json",
    }
    url = f"{BGT_OGC_URL}/collections/{collection}/items"
    features: list[dict] = []
    pages = 0

    # First request uses params; subsequent ones use the absolute `next` href (params=None).
    while url is not None:
        resp = session.get(url, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        if "json" not in resp.headers.get("Content-Type", "").lower():
            raise RuntimeError(
                f"{collection}: expected GeoJSON, got Content-Type "
                f"{resp.headers.get('Content-Type')!r}. Body:\n{resp.text[:500]}"
            )
        doc = resp.json()
        features.extend(doc.get("features", []))
        pages += 1

        # Find the next page; OGC API puts it in a link with rel="next".
        url = next((l["href"] for l in doc.get("links", []) if l.get("rel") == "next"), None)
        params = None  # the next href already carries bbox/crs/limit

    print(f"  {collection:<24} {len(features):>5} features  ({pages} page{'s' if pages != 1 else ''})")
    return features


def summarize_fysiek_voorkomen(gdf: gpd.GeoDataFrame) -> str:
    """One-line tally of fysiek_voorkomen values — the surface vocabulary driving Step 5."""
    if "fysiek_voorkomen" not in gdf.columns:
        return "    (no fysiek_voorkomen column)"
    counts = Counter(gdf["fysiek_voorkomen"].fillna("<none>"))
    parts = ", ".join(f"{val}={n}" for val, n in counts.most_common())
    return f"    fysiek_voorkomen: {parts}"


def main() -> int:
    # Fails clearly if no center is configured (fresh clone): run Step 1 or set .env.
    cfg.require_center()

    print("Step 3 — downloading BGT surface polygons via OGC API Features")
    print(f"  endpoint:  {BGT_OGC_URL}")
    print(f"  collections: {', '.join(COLLECTIONS)}")
    print(f"  box:       {cfg.BOX_SIZE:g} m, EPSG:{cfg.EPSG_CODE}\n")

    # Rebuild the GeoPackage from scratch so reruns don't accumulate stale layers.
    cfg.BGT_GPKG.parent.mkdir(parents=True, exist_ok=True)
    cfg.BGT_GPKG.unlink(missing_ok=True)

    session = requests.Session()
    gdfs: dict[str, gpd.GeoDataFrame] = {}
    try:
        for collection in COLLECTIONS:
            features = fetch_collection(session, collection, cfg.BBOX)
            if not features:
                print(f"  [warn] {collection}: 0 features — skipping layer (may be genuine).")
                continue
            gdf = gpd.GeoDataFrame.from_features(features, crs=cfg.CRS)
            gdf.to_file(cfg.BGT_GPKG, layer=collection, driver="GPKG")
            gdfs[collection] = gdf
    except Exception as e:  # network / HTTP / parse / write
        print(f"  [error] BGT download failed: {type(e).__name__}: {e}")
        return 1

    print(f"\n  wrote {cfg.BGT_GPKG.relative_to(cfg.ROOT)}  ({len(gdfs)} layers)\n")
    print("  cross-checks:")
    all_ok = len(gdfs) == len(COLLECTIONS)
    for collection, gdf in gdfs.items():
        epsg = gdf.crs.to_epsg() if gdf.crs else None
        crs_ok = epsg == cfg.EPSG_CODE
        all_ok = all_ok and crs_ok
        geoms = ", ".join(sorted(gdf.geom_type.unique()))
        print(f"  {collection:<24} {len(gdf):>5} feat  EPSG:{epsg} "
              f"{'OK' if crs_ok else 'FAILED — expected 28992!'}  [{geoms}]")
        print(summarize_fysiek_voorkomen(gdf))

    print(f"\n  load it in QGIS, overlay on the Step 2 raster, and confirm roads/grass/water "
          "land where you expect.")
    return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
