#!/usr/bin/env python3
"""
Radar Processor
=================
Reads ownship position from /tmp/ownship.json and traffic from
readsb's aircraft.json, computes bearing & distance for each aircraft,
filters by range (≤10 nm) and altitude (±5000 ft), and writes the
result to /tmp/radar.json for the display module.

Runs as a standalone daemon alongside bridge.py and display.py.
"""

import json
import math
import os
import time

from configuration import Config

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OWNSHIP_JSON_PATH = "/tmp/ownship.json"
READSB_JSON_PATH = "/run/readsb/aircraft.json"
RADAR_JSON_PATH = "/tmp/radar.json"

UPDATE_INTERVAL = 3.0       # seconds (0.33 Hz)
MAX_RANGE_NM = 10.0         # only show traffic within this range
ALT_FILTER_FT = 5000        # ±ft relative to ownship

# Shared configuration (for ownship aircraft override)
_cfg = Config()

# ---------------------------------------------------------------------------
# Haversine helpers
# ---------------------------------------------------------------------------
_NM_PER_RAD = 3440.065      # nautical miles per radian of Earth


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    lat1, lon1, lat2, lon2 = (math.radians(v) for v in (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * math.asin(math.sqrt(a)) * _NM_PER_RAD


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2 in degrees [0, 360)."""
    lat1, lon1, lat2, lon2 = (math.radians(v) for v in (lat1, lon1, lat2, lon2))
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360.0


# ---------------------------------------------------------------------------
# Band assignment
# ---------------------------------------------------------------------------

def _band(dist_nm: float) -> str:
    """Return colour band name based on distance."""
    if dist_nm < 2.0:
        return "red"
    elif dist_nm < 5.0:
        return "yellow"
    else:
        return "white"


# ---------------------------------------------------------------------------
# Main processing cycle
# ---------------------------------------------------------------------------

def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _process() -> None:
    """Run one radar processing cycle."""
    ownship = _read_json(OWNSHIP_JSON_PATH)
    if ownship is None or not ownship.get("has_fix"):
        # No ownship fix — write empty radar
        _write_radar(None, [], has_fix=False)
        return

    own_lat = ownship["lat"]
    own_lon = ownship["lon"]
    own_alt = ownship.get("alt_ft", 0.0)
    own_track = ownship.get("track_deg")

    readsb = _read_json(READSB_JSON_PATH)
    if readsb is None:
        _write_radar(own_track, [], has_fix=True)
        return

    # Exclude ownship override aircraft from radar
    override_icao = _cfg.get("ownship", "icao", reload=True).strip().upper()

    traffic = []
    for ac in readsb.get("aircraft", []):
        lat = ac.get("lat")
        lon = ac.get("lon")
        if lat is None or lon is None:
            continue

        # Skip the ownship override aircraft
        icao = ac.get("hex", "").strip().upper()
        if override_icao and icao == override_icao:
            continue

        # Altitude filter
        alt_baro = ac.get("alt_baro")
        if alt_baro is None or alt_baro == "ground":
            continue
        alt_diff = alt_baro - own_alt
        if abs(alt_diff) > ALT_FILTER_FT:
            continue

        # Distance filter
        dist = _haversine_nm(own_lat, own_lon, lat, lon)
        if dist > MAX_RANGE_NM:
            continue

        bearing = _bearing_deg(own_lat, own_lon, lat, lon)

        traffic.append({
            "bearing": round(bearing, 1),
            "dist_nm": round(dist, 2),
            "alt_diff_ft": round(alt_diff),
            "band": _band(dist),
        })

    _write_radar(own_track, traffic, has_fix=True)


def _write_radar(ownship_track: float | None, traffic: list, *,
                 has_fix: bool = False) -> None:
    """Atomically write /tmp/radar.json."""
    data = {
        "ownship_track": ownship_track,
        "has_fix": has_fix,
        "traffic": traffic,
    }
    tmp = RADAR_JSON_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, RADAR_JSON_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("Radar processor")
    print(f"  ownship : {OWNSHIP_JSON_PATH}")
    print(f"  readsb  : {READSB_JSON_PATH}")
    print(f"  output  : {RADAR_JSON_PATH}")
    print(f"  interval: {UPDATE_INTERVAL}s")
    print()

    while True:
        _process()
        time.sleep(UPDATE_INTERVAL)


if __name__ == "__main__":
    main()
