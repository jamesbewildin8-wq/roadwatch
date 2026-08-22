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

# Three sources, because OSM maps this inconsistently and querying only the
# obvious tag under-counts badly.
#
#  1. node[highway=speed_camera] - the simple, most common mapping.
#  2. type=enforcement relations, whose "device" members are the authoritative
#     mapping. The wiki is explicit that enforcement=* belongs on RELATIONS and
#     should not be used on nodes, so querying node[enforcement=maxspeed] - as
#     this script originally did - looks for a documented tagging mistake and
#     misses every correctly-mapped camera whose device node carries only a
#     role. Red-light cameras are frequently in this category, because the
#     wiki itself notes it is unclear how to tag the device node directly.
#  3. man_made=surveillance with an ALPR/speed subtype, used by some mappers
#     for number-plate enforcement instead of highway=speed_camera.
#
# Relations also carry enforcement type and maxspeed, which the bare nodes
# often lack - so relation metadata is used to enrich the device nodes below.
QUERY = """
[out:json][timeout:240];
area["ISO3166-1"="{cc}"][admin_level=2]->.a;
rel["type"="enforcement"](area.a)->.enf;
(
  node["highway"="speed_camera"](area.a);
  node["man_made"="surveillance"]["surveillance:type"~"ALPR|speed",i](area.a);
  node(r.enf:"device");
  way(r.enf:"device");
);
out center;
.enf out body;
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


KIND_BY_ENFORCEMENT = {
    "traffic_signals": "red_light_camera",
    "average_speed":   "average_speed",
    "maxspeed":        "speed_camera",
    "check":           "checkpoint",
}


def parse_limit(*values) -> int | None:
    for v in values:
        if not v:
            continue
        try:
            return int(str(v).split()[0])
        except (ValueError, IndexError):
            continue
    return None


def index_relations(elements: list[dict]) -> dict[int, dict]:
    """Map each device member id to its relation's metadata.

    The relation knows what kind of enforcement it is and what the limit is;
    the device node often knows neither. Without this join, a correctly-mapped
    average-speed camera comes out as a generic speed camera with no limit.
    """
    out: dict[int, dict] = {}
    for el in elements:
        if el.get("type") != "relation":
            continue
        t = el.get("tags", {})
        meta = {
            "enforcement": t.get("enforcement", ""),
            "maxspeed": t.get("maxspeed"),
            "ref": t.get("ref") or t.get("name") or "",
        }
        for m in el.get("members", []):
            if m.get("role") == "device":
                out[m.get("ref")] = meta
    return out


def to_feature(el: dict, cc: str, rel_meta: dict[int, dict]) -> dict | None:
    # Ways/areas come back from `out center` with a center block instead of
    # top-level coordinates.
    lat = el.get("lat", (el.get("center") or {}).get("lat"))
    lon = el.get("lon", (el.get("center") or {}).get("lon"))
    if lat is None or lon is None:
        return None

    t = el.get("tags", {})
    meta = rel_meta.get(el.get("id"), {})

    kind = KIND_BY_ENFORCEMENT.get(meta.get("enforcement", ""))
    if not kind:
        if t.get("highway") == "speed_camera":
            kind = "speed_camera"
        elif t.get("man_made") == "surveillance":
            kind = "anpr_camera"
        elif el.get("id") in rel_meta:
            kind = "speed_camera"      # device of an untyped enforcement relation
        else:
            return None

    return {
        "type": "Feature",
        "geometry": {"type": "Point",
                     "coordinates": [round(lon, 6), round(lat, 6)]},
        "properties": {
            "kind": kind,
            "country": cc,
            "road": t.get("ref") or t.get("name") or meta.get("ref", ""),
            "limit_kmh": parse_limit(t.get("maxspeed"), meta.get("maxspeed"),
                                     t.get("maxspeed:advisory")),
            "direction": t.get("direction", ""),
            "source": "OpenStreetMap (ODbL)",
            "osm_id": el.get("id"),
            "in_relation": el.get("id") in rel_meta,
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
        elements = fetch(cc)
        rel_meta = index_relations(elements)
        got = [f for f in (to_feature(e, cc, rel_meta) for e in elements) if f]

        # A device node reachable both directly and through a relation appears
        # twice. Dedupe on position rather than id, which also collapses the
        # rare case of two ids at the same spot.
        seen, uniq = set(), []
        for f in got:
            key = tuple(f["geometry"]["coordinates"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(f)

        by_kind: dict[str, int] = {}
        for f in uniq:
            by_kind[f["properties"]["kind"]] = by_kind.get(f["properties"]["kind"], 0) + 1
        in_rel = sum(1 for f in uniq if f["properties"]["in_relation"])
        print(f"  {len(uniq)} points ({len(got) - len(uniq)} duplicates dropped)",
              file=sys.stderr)
        print(f"    by kind: {by_kind}", file=sys.stderr)
        print(f"    {in_rel} from enforcement relations, "
              f"{sum(1 for f in uniq if f['properties']['limit_kmh'])} with a speed limit",
              file=sys.stderr)
        feats.extend(uniq)

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
