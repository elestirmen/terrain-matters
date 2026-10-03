"""Revisioned road-network editing and optimizer-facing GeoJSON access."""

from __future__ import annotations

import json
import math
import re
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any

from shapely import wkt
from shapely.geometry import LineString, Point, mapping

from .editor_store import (
    DraftValidationError,
    RevisionConflictError,
    atomic_write_json,
    exclusive_store_lock,
)
from .optimizer import DEFAULT_EDITABLE_ROAD_NETWORK, DEFAULT_ROAD_GRAPH, haversine_m
from .routing_contract import highway_default_speed, modeled_editable_speed, road_network_digest

_STORE_LOCK = threading.RLock()

def _first_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return next((item for item in value if item not in (None, "")), None)
    return value


def _highway_default_speed(value: Any) -> float:
    return highway_default_speed(_first_value(value))


def _parse_osm_speed(value: Any) -> float | None:
    value = _first_value(value)
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) and float(value) > 0 else None
    text = str(value).strip().lower()
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    speed = float(match.group(0).replace(",", "."))
    if "mph" in text:
        speed *= 1.609344
    return speed if math.isfinite(speed) and speed > 0 else None


def _graph_edge_speed(data: dict[str, Any], highway: Any) -> tuple[float, str, str]:
    speed = _parse_osm_speed(data.get("speed_kph"))
    if speed is not None:
        return _validated_speed(speed), "osm_speed_kph", "modelled"
    speed = _parse_osm_speed(data.get("maxspeed"))
    if speed is not None:
        return _validated_speed(speed), "osm_maxspeed", "signed_or_tagged"
    return _highway_default_speed(highway), "highway_class_default", "assumed"


def _validated_speed(value: float) -> float:
    if not math.isfinite(value) or not 5 <= value <= 150:
        raise DraftValidationError("Yol hızı 5-150 km/sa arasında olmalı.")
    return value


def _edge_line(graph: Any, u: Any, v: Any, data: dict[str, Any]) -> LineString | None:
    geometry = data.get("geometry")
    if isinstance(geometry, str):
        geometry = wkt.loads(geometry)
    if isinstance(geometry, LineString):
        return geometry
    u_data = graph.nodes.get(u, {})
    v_data = graph.nodes.get(v, {})
    try:
        return LineString(
            [(float(u_data["x"]), float(u_data["y"])), (float(v_data["x"]), float(v_data["y"]))]
        )
    except (KeyError, TypeError, ValueError):
        return None


def graph_to_feature_collection(graph: Any) -> dict[str, Any]:
    """Convert a directed OSM graph to compact, de-duplicated GeoJSON."""
    features: list[dict[str, Any]] = []
    seen: set[tuple[tuple[float, float], ...]] = set()
    for u, v, _key, data in graph.edges(keys=True, data=True):
        line = _edge_line(graph, u, v, data)
        if line is None or line.is_empty:
            continue
        coordinates = tuple((round(x, 7), round(y, 7)) for x, y in line.coords)
        signature = min(coordinates, tuple(reversed(coordinates)))
        if signature in seen:
            continue
        seen.add(signature)
        name = data.get("name")
        highway = data.get("highway")
        if isinstance(name, list):
            name = " / ".join(map(str, name))
        if isinstance(highway, list):
            highway = ", ".join(map(str, highway))
        speed_kmh, speed_source, speed_confidence = _graph_edge_speed(data, highway)
        road_id = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(coordinates, separators=(",", ":"))))
        features.append(
            {
                "type": "Feature",
                "id": road_id,
                "properties": {
                    "road_id": road_id,
                    "name": str(name or "İsimsiz Yol"),
                    "highway": str(highway or ""),
                    "oneway": str(data.get("oneway") or "false").lower() in {"true", "1", "yes"},
                    "direction": "forward"
                    if str(data.get("oneway") or "false").lower() in {"true", "1", "yes"}
                    else "both",
                    "speed_kmh": round(speed_kmh, 1),
                    "speed_source": speed_source,
                    "speed_confidence": speed_confidence,
                },
                "geometry": mapping(line),
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _load_graph(path: Path):
    import osmnx as ox

    return ox.load_graphml(path)


@lru_cache(maxsize=2)
def _road_context(path_text: str, mtime_ns: int):
    del mtime_ns  # cache key only
    path = Path(path_text)
    graph = _load_graph(path)
    feature_collection = graph_to_feature_collection(graph)
    lines = [
        LineString(feature["geometry"]["coordinates"])
        for feature in feature_collection["features"]
    ]
    return feature_collection, lines


def _context(path: Path = DEFAULT_ROAD_GRAPH):
    if not path.exists():
        raise FileNotFoundError(f"Optimizer yol ağı bulunamadı: {path}")
    return _road_context(str(path.resolve()), path.stat().st_mtime_ns)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_road_feature(feature: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(feature, dict):
        raise DraftValidationError("Yol kaydı nesne olmalı.")
    geometry = feature.get("geometry") or {}
    if geometry.get("type") != "LineString":
        raise DraftValidationError("Yol geometrisi LineString olmalı.")
    try:
        line = LineString(geometry.get("coordinates") or [])
    except Exception as exc:
        raise DraftValidationError(f"Yol geometrisi okunamadı: {exc}") from exc
    if line.is_empty or not line.is_valid or len(line.coords) < 2:
        raise DraftValidationError("Yol en az iki noktadan oluşan geçerli bir çizgi olmalı.")
    coordinates = list(line.coords)
    if any(tuple(start) == tuple(end) for start, end in pairwise(coordinates)):
        raise DraftValidationError("Yol üzerinde art arda aynı koordinat kullanılamaz.")
    minx, miny, maxx, maxy = line.bounds
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90):
        raise DraftValidationError("Yol koordinatları EPSG:4326 sınırları dışında.")
    properties = deepcopy(feature.get("properties") or {})
    feature_id = str(feature.get("id") or properties.get("road_id") or uuid.uuid4())
    properties["road_id"] = feature_id
    properties["name"] = str(properties.get("name") or "İsimsiz Yol").strip()
    properties["highway"] = str(properties.get("highway") or "residential").strip()
    direction = str(properties.get("direction") or "").strip().lower()
    if direction not in {"both", "forward", "reverse"}:
        direction = "forward" if properties.get("oneway") in {True, "1", "true", "yes", "on"} else "both"
    properties["direction"] = direction
    properties["oneway"] = direction != "both"
    for key in ("effective_speed_kmh", "effective_speed_source", "effective_speed_confidence"):
        properties.pop(key, None)
    try:
        raw_speed = properties.get("speed_kmh")
        speed_source = str(properties.get("speed_source") or "").strip()
        allowed_sources = {
            "",
            "operator_input",
            "highway_class_default",
            "osm_speed_kph",
            "osm_maxspeed",
            "legacy_explicit",
            "legacy_manual",
        }
        if speed_source not in allowed_sources:
            raise DraftValidationError("Yol hız kaynağı tanınmıyor.")
        if raw_speed in (None, ""):
            if speed_source == "highway_class_default":
                speed = _highway_default_speed(properties.get("highway"))
            elif speed_source:
                raise DraftValidationError("Belirtilen hız kaynağı için speed_kmh zorunlu.")
            else:
                speed = 30.0
        else:
            speed = float(raw_speed)
    except DraftValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise DraftValidationError("Yol hızı sayı olmalı.") from exc
    properties["speed_kmh"] = _validated_speed(speed)
    return {
        "type": "Feature",
        "id": feature_id,
        "properties": properties,
        "geometry": mapping(line),
    }


class EditableRoadNetworkStore:
    def __init__(self, path: Path = DEFAULT_EDITABLE_ROAD_NETWORK, graph_path: Path = DEFAULT_ROAD_GRAPH):
        self.path = path
        self.graph_path = graph_path

    def ensure(self) -> dict[str, Any]:
        with _STORE_LOCK, exclusive_store_lock(self.path):
            if not self.path.exists():
                payload = deepcopy(_context(self.graph_path)[0])
                payload.update({"schema_version": 1, "revision": 0, "updated_at_utc": _utc_now()})
                atomic_write_json(self.path, payload)
        return self.read()

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.ensure()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DraftValidationError(f"Yol ağı okunamadı: {exc}") from exc
        if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
            raise DraftValidationError("Yol ağı FeatureCollection olmalı.")
        payload["features"] = [validate_road_feature(feature) for feature in payload["features"]]
        # Fill in the fields the contract promises, so a caller never has to
        # guess whether an older file happened to carry them.
        payload.setdefault("schema_version", 1)
        payload.setdefault("revision", 0)
        payload.setdefault("updated_at_utc", _utc_now())
        payload.setdefault("source", "OpenStreetMap + local editor changes")
        return payload

    @staticmethod
    def _check_revision(payload: dict[str, Any], expected_revision: int | None) -> None:
        if expected_revision is not None and int(expected_revision) != int(payload.get("revision", 0)):
            raise RevisionConflictError("Yol ağı başka bir oturumda değişti. Veriyi yenileyin.")

    def cache_token(self) -> tuple[str, int]:
        """A cheap change signal, so snapping need not re-read an unchanged network.

        The file's mtime is the natural one here; the database store uses its
        revision. Either way the caller only re-reads on a miss.
        """
        try:
            return (str(self.path.resolve()), self.path.stat().st_mtime_ns)
        except OSError:
            return (str(self.path), 0)

    @contextmanager
    def frozen(self):
        """Block road edits and yield the current revision and digest.

        Applying an optimization proposal validates it against the road network
        and then writes the draft; both must see the same network. Lock order is
        road → draft, and road edits never take the draft lock, so there is no
        cycle.
        """
        with exclusive_store_lock(self.path):
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DraftValidationError(f"Yol ağı revizyonu okunamadı: {exc}") from exc
            if not isinstance(payload, dict):
                raise DraftValidationError("Yol ağı revizyonu geçerli bir nesne değil.")
            yield {
                "revision": int(payload.get("revision", 0)),
                "digest": road_network_digest(payload),
            }

    def save_feature(
        self,
        feature: dict[str, Any],
        *,
        feature_id: str | None = None,
        expected_revision: int | None = None,
        actor: Any = None,
    ):
        # actor is accepted so both stores share one call shape. This store has
        # nowhere to record who made the change — one of the reasons the
        # database store exists.
        del actor
        with _STORE_LOCK, exclusive_store_lock(self.path):
            payload = self.read()
            self._check_revision(payload, expected_revision)
            normalized = validate_road_feature({**feature, "id": feature_id or feature.get("id")})
            features = payload["features"]
            match = next((i for i, item in enumerate(features) if str(item.get("id")) == normalized["id"]), None)
            if feature_id and match is None:
                raise KeyError(f"Yol bulunamadı: {feature_id}")
            if match is None:
                features.append(normalized)
            else:
                features[match] = normalized
            payload["revision"] = int(payload.get("revision", 0)) + 1
            payload["updated_at_utc"] = _utc_now()
            atomic_write_json(self.path, payload)
            _SNAPPING_LINES.clear()
            return {"revision": payload["revision"], "feature": normalized}

    def delete_feature(self, feature_id: str, *, expected_revision: int | None = None, actor: Any = None):
        del actor
        with _STORE_LOCK, exclusive_store_lock(self.path):
            payload = self.read()
            self._check_revision(payload, expected_revision)
            remaining = [item for item in payload["features"] if str(item.get("id")) != feature_id]
            if len(remaining) == len(payload["features"]):
                raise KeyError(f"Yol bulunamadı: {feature_id}")
            payload["features"] = remaining
            payload["revision"] = int(payload.get("revision", 0)) + 1
            payload["updated_at_utc"] = _utc_now()
            atomic_write_json(self.path, payload)
            _SNAPPING_LINES.clear()
            return {"revision": payload["revision"], "deleted_id": feature_id}


road_network_store = EditableRoadNetworkStore()


def get_road_network() -> dict[str, Any]:
    from .stores import road_store

    payload = deepcopy(road_store().ensure())
    for feature in payload.get("features", []):
        properties = feature.get("properties") or {}
        try:
            speed, source, confidence = modeled_editable_speed(properties)
        except (TypeError, ValueError):
            continue
        properties["effective_speed_kmh"] = round(speed, 1)
        properties["effective_speed_source"] = source
        properties["effective_speed_confidence"] = confidence
    return payload


# One entry: the only network anyone snaps against is the current one.
_SNAPPING_LINES: dict[tuple[str, int], list[LineString]] = {}


def _snapping_lines(token: tuple[str, int], payload_reader) -> list[LineString]:
    """Geometries for snapping, rebuilt only when the network's token changes.

    `payload_reader` is a callable so an unchanged network costs nothing: the
    1527-segment read only happens on a miss.
    """
    cached = _SNAPPING_LINES.get(token)
    if cached is not None:
        return cached
    lines = [
        LineString(feature["geometry"]["coordinates"])
        for feature in payload_reader().get("features", [])
    ]
    _SNAPPING_LINES.clear()
    _SNAPPING_LINES[token] = lines
    return lines


def nearest_road_point(
    lng: float,
    lat: float,
    max_distance_m: float = 120,
    path: Path = DEFAULT_EDITABLE_ROAD_NETWORK,
) -> dict[str, Any]:
    """Return the closest position on the same cached graph used by optimization."""
    from .stores import road_store_for

    store = road_store_for(path if path is not None else None)
    lines = _snapping_lines(store.cache_token(), store.ensure)
    point = Point(lng, lat)
    best_line: LineString | None = None
    best_degree_distance = float("inf")
    for line in lines:
        distance = line.distance(point)
        if distance < best_degree_distance:
            best_degree_distance = distance
            best_line = line
    if best_line is None:
        return {"snapped": False, "coordinates": [lng, lat], "distance_m": None}
    snapped = best_line.interpolate(best_line.project(point))
    distance_m = haversine_m((lng, lat), (snapped.x, snapped.y))
    if distance_m > max_distance_m:
        return {"snapped": False, "coordinates": [lng, lat], "distance_m": round(distance_m, 1)}
    return {
        "snapped": True,
        "coordinates": [round(snapped.x, 7), round(snapped.y, 7)],
        "distance_m": round(distance_m, 1),
    }

# ── connectivity ─────────────────────────────────────────────────────────
# A road that does not touch the network is unusable, and used to be unusable in
# silence. The graph is noded by `unary_union`, which splits lines where they
# genuinely cross; a gap of twenty-three centimetres is as good as a gap of two
# hundred metres to it. Drawn on a map at any usable zoom, the two look the same.

#: A gap this small is somebody's hand missing the line, not a road that stops
#: short of another one. Wide enough to catch a mis-click, narrow enough that a
#: service road ending near a dual carriageway is not swept up with it.
DEFAULT_GAP_TOLERANCE_M = 5.0

#: Closer than this and the two are the same point; the remainder is the
#: arithmetic, not the drawing. A micrometre, not a millimetre: a snap that
#: lands *on* a line leaves nanometres behind, and anything wider than that is a
#: point beside the line, which is exactly the case a millimetre floor would
#: hide while `unary_union` went on refusing to node there.
TOUCHING_M = 1e-6


def _endpoint_gaps(features: list[dict[str, Any]], tolerance_m: float) -> list[dict[str, Any]]:
    """Endpoints that nearly touch another road, with how far off they are.

    A candidate already sharing the endpoint is a junction, not a gap, and is
    passed over rather than allowed to win the search. Letting it win was
    hiding real breaks: Halıcılar 1 and 2 meet each other exactly and the
    network they both wanted is ten metres further on, so the nearest line was
    the sibling at zero, the endpoint counted as connected, and no gap was
    reported at any tolerance. Network-wide that silenced 1180 endpoints.
    """
    from shapely.strtree import STRtree

    lines = [LineString(feature["geometry"]["coordinates"]) for feature in features]
    if not lines:
        return []
    tree = STRtree(lines)
    gaps: list[dict[str, Any]] = []
    for index, (feature, line) in enumerate(zip(features, lines, strict=True)):
        coordinates = list(line.coords)
        for position, name in ((0, "start"), (-1, "end")):
            endpoint = Point(coordinates[position])
            candidates = [
                other
                for other in tree.query(endpoint.buffer(_degrees_for(tolerance_m, endpoint.y)))
                if int(other) != index
            ]
            best_distance, best_index = float("inf"), None
            for other in candidates:
                distance = haversine_m(
                    (endpoint.x, endpoint.y),
                    _nearest_on(lines[int(other)], endpoint),
                )
                # The floor is a millimetre rather than zero: a point placed
                # *on* a line still measures a few nanometres off it in
                # floating point, and treating that as a gap would leave a
                # report nobody can ever clear.
                if distance <= TOUCHING_M:
                    continue
                if distance < best_distance:
                    best_distance, best_index = distance, int(other)
            if best_index is None or best_distance > tolerance_m:
                continue
            gaps.append(
                {
                    "road_id": str(feature["properties"].get("road_id") or feature.get("id") or ""),
                    "name": str(feature["properties"].get("name") or "İsimsiz Yol"),
                    "endpoint": name,
                    "gap_m": round(best_distance, 2),
                    "nearest_name": str(features[best_index]["properties"].get("name") or "İsimsiz Yol"),
                    "nearest_road_id": str(features[best_index]["properties"].get("road_id") or ""),
                    # Unrounded. The point has to land *on* the other line for
                    # `unary_union` to node there, and rounding moves it off:
                    # seven decimals is a centimetre of slop, nine is a tenth of
                    # a millimetre, and both leave the junction resting on
                    # whatever tolerance GEOS happens to apply inside an overlay
                    # rather than on the geometry being right.
                    "snapped_to": list(_nearest_on(lines[best_index], endpoint)),
                }
            )
    return sorted(gaps, key=lambda gap: gap["gap_m"])


def _false_junctions(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Endpoints that lie on another road without sharing a node with it."""
    from shapely.strtree import STRtree

    lines = [LineString(feature["geometry"]["coordinates"]) for feature in features]
    if not lines:
        return []
    tree = STRtree(lines)
    # Degrees, not metres: `distance` here is in the coordinate system, and the
    # residue of a projected point is around 1e-15. A tenth of a millimetre is
    # wide enough to catch that and far too tight to catch a real gap.
    on_the_line = 1e-9
    found: list[dict[str, Any]] = []
    for index, (feature, line) in enumerate(zip(features, lines, strict=True)):
        for position, name in ((0, "start"), (-1, "end")):
            endpoint = Point(line.coords[position])
            for other in tree.query(endpoint.buffer(on_the_line)):
                other_index = int(other)
                target = lines[other_index]
                if other_index == index or target.distance(endpoint) > on_the_line:
                    continue
                if target.intersects(endpoint):
                    continue
                found.append(
                    {
                        "road_id": str(feature["properties"].get("road_id") or ""),
                        "name": str(feature["properties"].get("name") or "İsimsiz Yol"),
                        "endpoint": name,
                        "target_road_id": str(features[other_index]["properties"].get("road_id") or ""),
                        "target_name": str(features[other_index]["properties"].get("name") or "İsimsiz Yol"),
                        "point": list(line.coords[position]),
                    }
                )
    return found


def _nearest_on(line: LineString, point: Point) -> tuple[float, float]:
    """The point on `line` closest to `point`, measured on the ground.

    `project` and `interpolate` work in whatever coordinate system they are
    handed, and ours is degrees. At Ürgüp's latitude a degree of longitude is
    0.78 of a degree of latitude on the ground, so projecting in raw degrees
    walks the line at the wrong speed and lands somewhere that is not the
    perpendicular foot. `haversine_m` then measures honestly to the wrong
    point: one endpoint of Davut Ağa Sokak sits 9.99 m from its neighbour and
    was reported at 12.20 m, which is how a 12 m tolerance came to miss a gap
    of ten. Scaling longitude by cos(latitude) makes the space locally metric,
    which is all the projection needs; scaling is affine, so the point still
    lands exactly on the original line and `unary_union` still nodes there.
    """
    scale = max(math.cos(math.radians(point.y)), 0.01)
    flattened = LineString([(x * scale, y) for x, y in line.coords])
    projected = flattened.interpolate(flattened.project(Point(point.x * scale, point.y)))
    return (projected.x / scale, projected.y)


def _degrees_for(metres: float, latitude: float) -> float:
    """A degree box that covers `metres` at this latitude, for the query index."""
    return metres / (111_320.0 * max(math.cos(math.radians(latitude)), 0.01))


def connectivity_report(
    payload: dict[str, Any], *, tolerance_m: float = DEFAULT_GAP_TOLERANCE_M
) -> dict[str, Any]:
    """Which roads the optimizer can reach, and which it cannot — and why.

    Reported against the same graph the optimizer routes on, so the answer is
    about what will actually happen rather than about the drawing.
    """
    import networkx as nx

    from .optimizer import geojson_road_graph

    features = [
        feature
        for feature in payload.get("features", [])
        if (feature.get("geometry") or {}).get("type") == "LineString"
    ]
    graph = geojson_road_graph(payload)
    components = sorted(nx.connected_components(nx.Graph(graph)), key=len, reverse=True)
    reachable = components[0] if components else set()

    stranded: list[dict[str, Any]] = []
    for feature in features:
        coordinates = feature["geometry"]["coordinates"]
        ends = {
            (round(coordinates[0][0], 7), round(coordinates[0][1], 7)),
            (round(coordinates[-1][0], 7), round(coordinates[-1][1], 7)),
        }
        # A road whose own endpoints are nodes of the graph but not of the main
        # component. One that was split by noding keeps interior nodes in the
        # main component, so it is reachable and not reported.
        if ends & reachable or not (ends & set(graph.nodes)):
            continue
        stranded.append(
            {
                "road_id": str(feature["properties"].get("road_id") or feature.get("id") or ""),
                "name": str(feature["properties"].get("name") or "İsimsiz Yol"),
                "length_m": round(
                    sum(haversine_m(a, b) for a, b in pairwise(coordinates)), 1
                ),
            }
        )

    # A junction you can leave and never enter, or enter and never leave. The
    # geometry is connected, so nothing above reports it, and a vehicle still
    # cannot use the street — the same silence as a gap, arrived at through the
    # one-way rules instead of through the drawing.
    dead_ends = []
    for node in graph.nodes:
        entering, leaving = graph.in_degree(node), graph.out_degree(node)
        if (entering == 0) == (leaving == 0):
            continue
        names = sorted(
            {
                str(data.get("name") or "")
                for _, _, data in list(graph.in_edges(node, data=True))
                + list(graph.out_edges(node, data=True))
                if data.get("name")
            }
        )
        dead_ends.append(
            {
                "point": [node[0], node[1]],
                "direction": "girilemez" if entering == 0 else "çıkılamaz",
                "roads": names,
            }
        )

    # An endpoint sitting *on* another road is not a junction. `unary_union`
    # nodes by exact predicate, and a point placed on a segment by projection is
    # a hair off collinear in double arithmetic — `intersects` says False, the
    # overlay does not split the other line, and no node is shared. The distance
    # reads as zero the whole time, so a report that measures distance calls it
    # repaired while no vehicle can turn there. The fix is a vertex on the other
    # road, not a smaller number.
    false_junctions = _false_junctions(features)

    gaps = _endpoint_gaps(features, tolerance_m)
    stranded_ids = {road["road_id"] for road in stranded}
    for road in stranded:
        road["gaps"] = [gap for gap in gaps if gap["road_id"] == road["road_id"]]

    return {
        "road_count": len(features),
        "component_count": len(components),
        "reachable_node_count": len(reachable),
        "stranded_roads": sorted(stranded, key=lambda road: -road["length_m"]),
        "near_miss_endpoints": gaps,
        "false_junctions": false_junctions,
        "one_way_dead_ends": dead_ends,
        "tolerance_m": tolerance_m,
        # The distinction worth drawing: a gap on a road nothing can reach is
        # why it cannot be reached, and one elsewhere is a detour nobody asked
        # for. Both are worth fixing; only the first stops a route existing.
        "stranded_gap_count": sum(1 for gap in gaps if gap["road_id"] in stranded_ids),
    }
