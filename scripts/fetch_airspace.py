#!/usr/bin/env python3
"""Pull real airspace ceilings from the FAA and write them as Attaché volumes.

    python3 scripts/fetch_airspace.py --bbox -74.02,40.69,-73.95,40.76 --out configs/airspace/nyc.json

The FAA publishes UAS Facility Maps through its UAS Data Delivery System: a grid of cells,
each carrying the maximum altitude at which a Part 107 flight may be authorised there.
Zero means no flight without a separate authorisation. This is the real shape of the rule
we have been faking with a circle: a neighbourhood is not one ceiling.

No API key. The service is public.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

FAA_LAYER = (
    "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services"
    "/FAA_UAS_FacilityMap_Data_Primary/FeatureServer/0/query"
)
FEET_TO_METRES = 0.3048


def fetch(bbox: tuple[float, float, float, float], limit: int) -> list[dict]:
    lon_min, lat_min, lon_max, lat_max = bbox
    envelope = {
        "xmin": lon_min, "ymin": lat_min, "xmax": lon_max, "ymax": lat_max,
        "spatialReference": {"wkid": 4326},
    }
    query = urllib.parse.urlencode({
        "where": "1=1",
        "geometry": json.dumps(envelope),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "outSR": "4326",
        "outFields": "OBJECTID,CEILING,UNIT,MAP_EFF,APT1_NAME,APT1_ICAO",
        "resultRecordCount": str(limit),
        "f": "geojson",
    })
    with urllib.request.urlopen(f"{FAA_LAYER}?{query}", timeout=60) as response:
        return json.loads(response.read()).get("features", [])


def to_volume(feature: dict) -> dict | None:
    properties = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}
    if geometry.get("type") != "Polygon":
        return None
    ring = geometry["coordinates"][0]
    ceiling_ft = properties.get("CEILING")
    if ceiling_ft is None:
        return None
    ceiling_m = round(float(ceiling_ft) * FEET_TO_METRES, 1)
    airport = properties.get("APT1_ICAO") or properties.get("APT1_NAME") or ""
    return {
        "id": f"uasfm-{properties['OBJECTID']}",
        "name": f"{airport} 격자 {ceiling_ft}ft".strip(),
        # GeoJSON 은 [lon, lat] 인데 우리 Volume 은 (lat, lon) 입니다
        "polygon": [[point[1], point[0]] for point in ring[:-1]],
        "floor_m": 0.0,
        # 천장 0 은 "허가 없이는 못 난다"는 뜻입니다. 고도 제한이 아니라 금지입니다.
        "ceiling_m": None if ceiling_m == 0 else ceiling_m,
        "reference": "AGL",
        "rule": "forbidden" if ceiling_m == 0 else "ceiling",
        "reason": (
            "허가 없이 비행 불가 (UASFM 0ft)" if ceiling_m == 0
            else f"허용 고도 {ceiling_ft}ft ({ceiling_m:.0f}m AGL)"
        ),
        "source": "FAA UAS Facility Map",
        "tags": {"ceiling_ft": ceiling_ft, "effective": properties.get("MAP_EFF")},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bbox", default="-74.02,40.69,-73.95,40.76",
                        help="lon_min,lat_min,lon_max,lat_max")
    parser.add_argument("--out", default="configs/airspace/nyc.json")
    parser.add_argument("--limit", type=int, default=600)
    args = parser.parse_args()

    bbox = tuple(float(x) for x in args.bbox.split(","))
    features = fetch(bbox, args.limit)
    volumes = [v for v in (to_volume(f) for f in features) if v]
    if not volumes:
        print("격자를 못 받았습니다. bbox 를 확인하세요.", file=sys.stderr)
        return 1

    payload = {
        "source": "FAA UAS Facility Map (UAS Data Delivery System)",
        "fetched_bbox": list(bbox),
        "count": len(volumes),
        "volumes": volumes,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    bands: dict = {}
    for volume in volumes:
        key = volume["tags"]["ceiling_ft"]
        bands[key] = bands.get(key, 0) + 1
    print(f"{len(volumes)}개 격자 → {out}")
    for ceiling in sorted(bands):
        note = "  ← 허가 없이 비행 불가" if ceiling == 0 else ""
        print(f"  {ceiling:>4}ft : {bands[ceiling]:>3}칸{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
