"""Versioned contract binding route proposals to their road-model snapshot."""

from __future__ import annotations

import hashlib
import json
from typing import Any

ROUTING_MODEL_VERSION = 2
ROAD_SPEED_PROFILE_VERSION = "osm-and-highway-defaults-v2"
HIGHWAY_DEFAULT_SPEED_KMH = {
    "motorway": 90.0,
    "motorway_link": 70.0,
    "trunk": 70.0,
    "trunk_link": 60.0,
    "primary": 50.0,
    "primary_link": 45.0,
    "secondary": 45.0,
    "secondary_link": 40.0,
    "tertiary": 40.0,
    "tertiary_link": 35.0,
    "residential": 30.0,
    "unclassified": 30.0,
    "service": 20.0,
    "living_street": 15.0,
}


def highway_default_speed(value: Any) -> float:
    if isinstance(value, (list, tuple)):
        value = next((item for item in value if item not in (None, "")), None)
    highway = str(value or "residential").split(",", 1)[0].strip().lower()
    return HIGHWAY_DEFAULT_SPEED_KMH.get(highway, 30.0)


def modeled_editable_speed(properties: dict[str, Any]) -> tuple[float, str, str]:
    """Resolve optimizer speed while preserving ambiguous legacy source data."""
    raw_speed = properties.get("speed_kmh")
    speed_source = str(properties.get("speed_source") or "").strip()
    if speed_source == "highway_class_default" and raw_speed in (None, ""):
        return highway_default_speed(properties.get("highway")), speed_source, "assumed"
    if not speed_source and raw_speed in (None, "", 30, 30.0, "30", "30.0"):
        return highway_default_speed(properties.get("highway")), "highway_class_default", "assumed"
    speed = 30.0 if raw_speed in (None, "") else float(raw_speed)
    return speed, speed_source or "legacy_explicit", str(properties.get("speed_confidence") or "unknown")


def road_network_digest(payload: dict[str, Any]) -> str:
    """Return a stable digest so same-revision content changes are detected."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
