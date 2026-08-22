#!/usr/bin/env python3
"""Pull fixed enforcement camera locations from OpenStreetMap.

    python tools/fetch_fixed_cameras.py MY ZA -o app/cameras.geojson

Why this exists: fixed speed and red-light cameras don't move, aren't secret,
and are already mapped. Knowing where they are needs no computer vision, no
camera feeds, no server and no money - just a dataset and a phone that knows
where it is. That is most of the practical value of the original idea, and it
is the part that survives having no budget at all.

OSM tags this data as:
    highway=speed_camera          fixed speed camera
    highway=speed_display         your-speed sign (informational, not enforcement)
    enforcement=maxspeed          an enforcement relation/node
    enforcement=traffic_signals   red light camera

Coverage varies enormously by country and city - OSM is volunteer-mapped, so
treat gaps as "nobody has mapped it here", never as "there is no camera here".
That asymmetry matters: this data can tell you a camera IS somewhere, but it can
never tell you one ISN'T.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="{cc}"][admin_level=2]->.a;
(
  node["highway"="speed_camera"](area.a);
  node["enforcement"="maxspeed"](area.a);
  node["enforcement"="traffic_signals"](area.a);
);
out body;
"""


def fetch(cc: str, retries: int = 3) -> list[dict]:
    body = QUERY.format(cc=cc).strip()
    last = None
    for endpoint in ENDPOINTS:
        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=urllib.parse.urlencode({"data": body}).encode(),
                    headers={"User-Agent": "roadwatch-fixed-cameras/1.0"},
                )
                with urllib.request.urlopen(req, timeout=200) as r:
                    return json.loads(r.read()).get("elements", [])
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                last = e
                # Overpass rate-limits aggressively; back off rather than hammer.
                wait = 10 * (attempt + 1)
                print(f"  {endpoint} attempt {attempt+1} failed ({e}), "
                      f"retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
    raise SystemExit(f"all Overpass endpoints failed for {cc}: {last}")


def to_feature(el: dict, cc: str) -> dict | None:
    if "lat" not in el or "lon" not in el:
        return None
    t = el.get("tags", {})
    if t.get("enforcement") == "traffic_signals":
        kind = "red_light_camera"
    elif t.get("highway") == "speed_camera" or t.get("enforcement") == "maxspeed":
        kind = "speed_camera"
    else:
        return None

    limit = t.get("maxspeed") or t.get("maxspeed:advisory") or ""
    try:
        limit_kmh = int(str(limit).split()[0])
    except (ValueError, IndexError):
        limit_kmh = None

    return {
        "type": "Feature",
        "geometry": {"type": "Point",
                     "coordinates": [round(el["lon"], 6), round(el["lat"], 6)]},
        "properties": {
            "kind": kind,
            "country": cc,
            "road": t.get("ref") or t.get("name") or "",
            "limit_kmh": limit_kmh,
            "direction": t.get("direction", ""),
            "source": "OpenStreetMap (ODbL)",
            "osm_id": el.get("id"),
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("countries", nargs="+", help="ISO-3166 alpha-2 codes, e.g. MY ZA")
    ap.add_argument("-o", "--out", default="app/cameras.geojson")
    args = ap.parse_args()

    feats: list[dict] = []
    for cc in (c.upper() for c in args.countries):
        print(f"fetching {cc} ...", file=sys.stderr)
        got = [f for f in (to_feature(e, cc) for e in fetch(cc)) if f]
        print(f"  {len(got)} enforcement points", file=sys.stderr)
        feats.extend(got)

    out = {
        "type": "FeatureCollection",
        "_source": "OpenStreetMap contributors, ODbL 1.0",
        "_generated": time.strftime("%Y-%m-%d"),
        "_warning": ("Volunteer-mapped and incomplete. Absence of a camera here "
                     "means nobody mapped one, NOT that none exists."),
        "features": feats,
    }
    with open(args.out, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"wrote {len(feats)} features to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
