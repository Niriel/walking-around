"""pipeline/step1_geocode_center.py — Step 1: find & verify the map's center point.

The project is a 500 m box in Groningen, centered on an address you configure privately
in a git-ignored ``.env`` (``GEOCODE_ADDRESS``). This script geocodes that address so the
center is looked up, not hardcoded — we don't build "a perfect map of the wrong place."

GEOCODING = turning a text description (an address) into coordinates. We use PDOK
Locatieserver, the Dutch government's authoritative geocoder (backed by BAG, the
official addresses/buildings registry). It returns coordinates *directly* in RD New
(EPSG:28992), our working CRS, and resolves a precise street+number+postcode to a single
building — no POI guessing needed.

PRIVACY
-------
- The address is read from a git-ignored ``.env`` (``GEOCODE_ADDRESS``); see .env.example.
- The resolved coordinates are written to git-ignored ``data/raw/center.json``, which
  ``config.py`` overlays at runtime. Neither the address nor the exact coordinates ever
  enter git.
- stdout prints coordinates rounded to 100 m, so a terminal log / screen-share can't pin
  the exact building. Full precision lives only in the git-ignored artifacts.

Run from the repo root:
    uv run python -m pipeline.step1_geocode_center
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from pyproj import Transformer

# Make ``from pipeline import config`` resolve whether run via ``-m pipeline.…`` or directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import config as cfg  # noqa: E402

PDOK_FREE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
HTTP_TIMEOUT = 25  # seconds

# always_xy=True forces (x=lon, y=lat) order; otherwise pyproj follows each CRS's official
# axis order (lat,lon for EPSG:4326), a classic foot-gun that silently swaps coordinates.
_LL_TO_RD = Transformer.from_crs("EPSG:4326", cfg.CRS, always_xy=True)
_RD_TO_LL = Transformer.from_crs(cfg.CRS, "EPSG:4326", always_xy=True)


def _parse_point(wkt: str) -> tuple[float, float]:
    """'POINT(123456.7 456789.0)' -> (123456.7, 456789.0)."""
    inside = wkt[wkt.index("(") + 1 : wkt.index(")")]
    x, y = (float(v) for v in inside.split())
    return x, y


def pdok_geocode(address: str) -> dict | None:
    """Geocode a precise address via PDOK Locatieserver; return the best 'adres' doc.

    PDOK 'type' runs adres > weg (street) > postcode > woonplaats in specificity. We want
    an 'adres' (a single building); we only fall back to a less specific hit if no address
    matched, which would signal a mistyped address.
    """
    fl = "weergavenaam type score centroide_rd centroide_ll"
    r = requests.get(
        PDOK_FREE,
        params={"q": address, "rows": 10, "fl": fl},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    docs = r.json()["response"]["docs"]
    addresses = [d for d in docs if d.get("type") == "adres"]
    pool = addresses or docs
    return max(pool, key=lambda d: d.get("score", 0.0)) if pool else None


def box_ring_ll(bbox_rd: tuple[float, float, float, float]) -> list[list[float]]:
    """Reproject the four RD corners of the bbox to a closed WGS84 [lon,lat] ring."""
    xmin, ymin, xmax, ymax = bbox_rd
    corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)]
    return [list(_RD_TO_LL.transform(x, y)) for x, y in corners]


def _r100(v: float) -> int:
    """Round to the nearest 100 m — coarse enough for a privacy-safe terminal log."""
    return int(round(v / 100.0) * 100)


def main() -> int:
    address = os.environ.get("GEOCODE_ADDRESS") or cfg.ENV.get("GEOCODE_ADDRESS")
    if not address:
        print("[error] No GEOCODE_ADDRESS found. Create a git-ignored .env at the repo root:")
        print('    GEOCODE_ADDRESS="<street + number>, <postcode> <city>"')
        print("  (copy .env.example).")
        return 1

    print("Step 1 — geocoding the map center via PDOK Locatieserver")
    print("  address: loaded from .env (hidden)\n")

    try:
        hit = pdok_geocode(address)
    except Exception as e:  # network / HTTP / parse
        print(f"  [error] PDOK request failed: {type(e).__name__}: {e}")
        return 1
    if hit is None:
        print("  [error] PDOK returned no candidate for that address.")
        return 1
    if hit.get("type") != "adres":
        print(f"  [warn] best hit is type '{hit.get('type')}', not an exact address — "
              "check the address in .env.")

    cx, cy = _parse_point(hit["centroide_rd"])
    clon, clat = _parse_point(hit["centroide_ll"])

    # Cross-checks. Printed values are rounded; full precision goes to center.json.
    reproj_err = math.dist((cx, cy), _LL_TO_RD.transform(clon, clat))
    plausible = (0 < cx < 300_000 and 300_000 < cy < 640_000
                 and 50.5 < clat < 53.7 and 3.2 < clon < 7.3)

    print(f"  matched type='{hit.get('type')}'  (score={hit.get('score', 0.0):.1f})")
    print(f"  center RD (rounded 100 m): X≈{_r100(cx):,}  Y≈{_r100(cy):,}")
    print(f"  pyproj LL->RD self-consistency: Δ={reproj_err:.3f} m  (should be ~0)")
    print(f"  plausibility (RD range + near Groningen): "
          f"{'OK' if plausible else 'FAILED — possible reprojection!'}")

    half = cfg.BOX_SIZE / 2.0
    bbox = (cx - half, cy - half, cx + half, cy + half)

    # --- Artifacts (both git-ignored under data/raw/). ---------------------------
    cfg.DATA_RAW.mkdir(parents=True, exist_ok=True)
    record = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "PDOK Locatieserver (address from .env)",
        "rd_xy": [cx, cy],            # <- config.py reads this key
        "wgs84_lonlat": [clon, clat],
        "weergavenaam": hit.get("weergavenaam"),
        "crs": cfg.CRS,
        "box_size_m": cfg.BOX_SIZE,
        "bbox_rd": list(bbox),
        "reprojection_check_m": round(reproj_err, 3),
        "plausible": plausible,
    }
    cfg.CENTER_FILE.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature",
             "properties": {"name": "map center", "rd_x": cx, "rd_y": cy},
             "geometry": {"type": "Point", "coordinates": [clon, clat]}},
            {"type": "Feature",
             "properties": {"name": f"{cfg.BOX_SIZE:g} m box", "bbox_rd": list(bbox)},
             "geometry": {"type": "Polygon", "coordinates": [box_ring_ll(bbox)]}},
        ],
    }
    center_geojson = cfg.DATA_RAW / "center.geojson"
    center_geojson.write_text(json.dumps(geojson, indent=2) + "\n")

    print(f"\n  wrote {cfg.CENTER_FILE.relative_to(cfg.ROOT)}  "
          "(git-ignored; config.py overlays it automatically)")
    print(f"  wrote {center_geojson.relative_to(cfg.ROOT)}  "
          "(load in QGIS over a basemap to confirm the point sits where you expect)")
    return 0 if plausible else 2


if __name__ == "__main__":
    raise SystemExit(main())
