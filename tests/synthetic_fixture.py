"""Deterministic, offline transport-network fixture shared by planning tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

DEPOT = (34.9000, 38.6300)


def _road(
    road_id: str,
    coordinates: list[list[float]],
    *,
    speed_kmh: float,
    direction: str = "both",
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": road_id,
        "properties": {
            "road_id": road_id,
            "name": f"Sentetik Yol {road_id}",
            "highway": "residential",
            "speed_kmh": speed_kmh,
            "direction": direction,
            "oneway": direction != "both",
        },
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }


def synthetic_road_network() -> dict[str, Any]:
    """Return a connected ring/grid with varied speeds and one one-way link."""
    roads = [
        _road("r-da", [[34.9000, 38.6300], [34.9010, 38.6300]], speed_kmh=20),
        _road("r-ab", [[34.9010, 38.6300], [34.9020, 38.6300]], speed_kmh=30),
        _road("r-bg", [[34.9020, 38.6300], [34.9030, 38.6300]], speed_kmh=40),
        _road("r-bc", [[34.9020, 38.6300], [34.9020, 38.6310]], speed_kmh=25),
        _road("r-ce", [[34.9020, 38.6310], [34.9010, 38.6310]], speed_kmh=35),
        _road("r-ef", [[34.9010, 38.6310], [34.9000, 38.6310]], speed_kmh=30),
        _road("r-fd", [[34.9000, 38.6310], [34.9000, 38.6300]], speed_kmh=20),
        _road(
            "r-ae-oneway",
            [[34.9010, 38.6300], [34.9010, 38.6310]],
            speed_kmh=15,
            direction="forward",
        ),
    ]
    return {
        "type": "FeatureCollection",
        "schema_version": 1,
        "revision": 3,
        "source": "synthetic_test_fixture",
        "features": roads,
    }


def _stop(
    index: int,
    name: str,
    coordinates: tuple[float, float],
    route_id: str,
    sequence: int,
    demand: float,
    *,
    depot: bool = False,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "name": name,
        "folder_path": route_id,
        "route_id": route_id,
        "stop_id": f"s-{index}",
        "sequence": sequence,
        "demand_weight": demand,
    }
    if depot:
        properties["location_role"] = "depot"
    return {
        "type": "Feature",
        "id": f"s-{index}",
        "properties": properties,
        "geometry": {"type": "Point", "coordinates": list(coordinates)},
    }


def synthetic_draft(*, demand: float = 1.0) -> dict[str, Any]:
    """Return one depot, six customers, two routes, and two neighborhoods."""
    stops = [
        _stop(0, "Merkez Durak", DEPOT, "west", 0, demand, depot=True),
        _stop(1, "Batı 1", (34.9010, 38.6300), "west", 1, demand),
        _stop(2, "Batı 2", (34.9010, 38.6310), "west", 2, demand),
        _stop(3, "Batı 3", (34.9000, 38.6310), "west", 3, demand),
        _stop(4, "Doğu 1", (34.9020, 38.6300), "east", 1, demand),
        _stop(5, "Doğu 2", (34.9020, 38.6310), "east", 2, demand),
        _stop(6, "Doğu 3", (34.9030, 38.6300), "east", 3, demand),
    ]
    routes = [
        {
            "type": "Feature",
            "id": "west",
            "properties": {
                "name": "Batı Hattı",
                "folder_path": "west",
                "route_id": "west",
                "color": "#2563eb",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    list(DEPOT),
                    [34.9010, 38.6300],
                    [34.9010, 38.6310],
                    [34.9000, 38.6310],
                    list(DEPOT),
                ],
            },
        },
        {
            "type": "Feature",
            "id": "east",
            "properties": {
                "name": "Doğu Hattı",
                "folder_path": "east",
                "route_id": "east",
                "color": "#16a34a",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    list(DEPOT),
                    [34.9020, 38.6300],
                    [34.9030, 38.6300],
                    [34.9020, 38.6310],
                    list(DEPOT),
                ],
            },
        },
    ]
    neighborhoods = [
        {
            "type": "Feature",
            "id": "west-area",
            "properties": {"name": "Batı Mahallesi"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [34.8995, 38.6295],
                    [34.9015, 38.6295],
                    [34.9015, 38.6315],
                    [34.8995, 38.6315],
                    [34.8995, 38.6295],
                ]],
            },
        },
        {
            "type": "Feature",
            "id": "east-area",
            "properties": {"name": "Doğu Mahallesi"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [34.9015, 38.6295],
                    [34.9035, 38.6295],
                    [34.9035, 38.6315],
                    [34.9015, 38.6315],
                    [34.9015, 38.6295],
                ]],
            },
        },
    ]
    return {
        "schema_version": 2,
        "revision": 7,
        "created_at_utc": "2026-01-01T00:00:00+00:00",
        "updated_at_utc": "2026-01-01T00:00:00+00:00",
        "layers": {
            "routes": {"type": "FeatureCollection", "features": routes},
            "stops": {"type": "FeatureCollection", "features": stops},
            "mahalle": {"type": "FeatureCollection", "features": neighborhoods},
        },
    }


def demand_profile(demand: float) -> dict[str, Any]:
    """Return a fresh draft for a low/high passenger-intensity profile."""
    return deepcopy(synthetic_draft(demand=demand))
