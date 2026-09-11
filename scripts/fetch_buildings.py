#!/usr/bin/env python3
"""Pull real building footprints from the City of New York and write them as volumes.

    python3 scripts/fetch_buildings.py --bbox -74.0174,40.6657,-73.9226,40.7381 \
        --min-height-m 25 --out configs/airspace/nyc_buildings.json

A building is not a new kind of rule. It is a volume you may not be inside: forbidden from
the ground to the roof, and open above that. `Volume(floor_m=0, ceiling_m=roof, rule=
"forbidden")` says exactly that, so `geo.first_breach` and the router already understand
it and no new judge is needed.

The city publishes Building Footprints with a LiDAR-derived roof height for every
structure, which is why this is not OpenStreetMap: OSM heights in New York are patchy, and
a missing height here reads as "no obstacle", which is the dangerous direction to be wrong.

Only buildings above --min-height-m are kept. Anything a lawful cruise altitude clears is
not an obstacle, and carrying it would only slow the route search and bloat the file.

No API key. The service is public, and rate-limited without one.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# NYC Open Data — Building Footprints. height_roof and ground_elevation are in feet.
NYC_BUILDINGS = "https://data.cityofnewyork.us/resource/5zhs-2jue.json"
FEET_TO_METRES = 0.3048
PAGE = 2000


def fetch(bbox: tuple[float, float, float, float], min_height_m: float) -> list[dict]:
    lon_min, lat_min, lon_max, lat_max = bbox
    # within_box takes north-west, then south-east.
    where = (
        f"within_box(the_geom, {lat_max}, {lon_min}, {lat_min}, {lon_max})"
        f" AND height_roof > {min_height_m / FEET_TO_METRES:.2f}"
    )
    rows, offset = [], 0
    while True:
        query = urllib.parse.urlencode({
            "$select": "bin,height_roof,ground_elevation,construction_year,the_geom",
            "$where": where,
            "$order": "bin",
            "$limit": PAGE,
            "$offset": offset,
        })
        with urllib.request.urlopen(f"{NYC_BUILDINGS}?{query}", timeout=120) as response:
            page = json.loads(response.read())
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += PAGE


def rings(geometry: dict) -> list[list]:
    """Outer rings only. No route sends a drone into a building's courtyard."""
    kind = geometry.get("type")
    if kind == "Polygon":
        return [geometry["coordinates"][0]]
    if kind == "MultiPolygon":
        return [part[0] for part in geometry["coordinates"]]
    return []


def thin(ring: list, tolerance_deg: float) -> list:
    """Drops points that are nearly in line. A 30-point footprint makes judging 30 times dearer.

    Errs only towards keeping — if dropping points shrank a building, a route would slip
    through the gap.
    """
    kept = [ring[0]]
    for point in ring[1:-1]:
        previous, following = kept[-1], point
        if abs(previous[0] - following[0]) + abs(previous[1] - following[1]) >= tolerance_deg:
            kept.append(point)
    kept.append(ring[-1])
    return kept if len(kept) >= 4 else ring


def to_volumes(row: dict, tolerance_deg: float) -> list[dict]:
    height_ft = float(row.get("height_roof") or 0.0)
    if height_ft <= 0:
        return []
    height_m = round(height_ft * FEET_TO_METRES, 1)
    out = []
    for index, ring in enumerate(rings(row.get("the_geom") or {})):
        polygon = [
            [round(float(lat), 6), round(float(lon), 6)]
            for lon, lat in thin(ring, tolerance_deg)
        ]
        if len(polygon) < 4:
            continue
        if polygon[0] == polygon[-1]:
            polygon.pop()          # geo.Volume takes open rings
        suffix = "" if index == 0 else f"-{index}"
        out.append({
            "id": f"bldg-{row.get('bin', '?')}{suffix}",
            "name": f"건물 {height_m:.0f}m",
            "polygon": polygon,
            "floor_m": 0.0,
            "ceiling_m": height_m,
            "reference": "AGL",
            "rule": "forbidden",
            "reason": f"건물 관통 불가 (옥상 {height_m:.0f}m AGL)",
            "source": "NYC Open Data Building Footprints",
            "tags": {
                "height_ft": round(height_ft, 1),
                "built": row.get("construction_year"),
            },
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bbox", required=True,
                        help="lon_min,lat_min,lon_max,lat_max")
    parser.add_argument("--min-height-m", type=float, default=25.0)
    parser.add_argument("--simplify-m", type=float, default=2.0,
                        help="drop footprint vertices closer together than this")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    bbox = tuple(float(part) for part in args.bbox.split(","))
    if len(bbox) != 4:
        print("bbox needs four values: lon_min,lat_min,lon_max,lat_max", file=sys.stderr)
        return 2

    rows = fetch(bbox, args.min_height_m)
    tolerance = args.simplify_m / 111_320.0
    volumes = [v for row in rows for v in to_volumes(row, tolerance)]
    if not volumes:
        print("No buildings came back. Check the bbox.", file=sys.stderr)
        return 1

    tallest = max(volumes, key=lambda v: v["ceiling_m"])
    payload = {
        "source": "NYC Open Data Building Footprints (5zhs-2jue)",
        "fetched_bbox": list(bbox),
        "min_height_m": args.min_height_m,
        "count": len(volumes),
        "volumes": volumes,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    points = sum(len(v["polygon"]) for v in volumes)
    print(f"{len(volumes)} buildings · {points} vertices · tallest {tallest['ceiling_m']:.0f}m"
          f" → {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
