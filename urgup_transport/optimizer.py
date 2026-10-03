"""Road-aware TSP, vehicle-routing, and genetic transport optimizers."""

from __future__ import annotations

import csv
import itertools
import json
import math
import random
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_FALLBACK_ROAD_SPEED_KMH,
    DEFAULT_LAYOVER_MINUTES,
    DEFAULT_MAX_STOP_SNAP_DISTANCE_M,
    STOPPING_PLACES_GEOJSON,
)
from .routing_contract import (
    ROAD_SPEED_PROFILE_VERSION,
    ROUTING_MODEL_VERSION,
    file_digest,
    modeled_editable_speed,
    road_network_digest,
)
from . import energy as energy_model
from .elevation import ElevationUnavailableError, load_grid

#: Per-directed-edge attributes written by `_prepare_graph_energy`. The first
#: is the true traversal energy; the second is the potential-corrected,
#: non-negative weight Dijkstra runs on.
ENERGY_KEY = "energy_j"
ENERGY_ADJUSTED_KEY = "energy_adjusted_j"

EARTH_RADIUS_M = 6_371_008.8
ROUTE_COLORS = ("#2563eb", "#7c3aed", "#db2777", "#ea580c", "#0891b2", "#16a34a", "#4f46e5")
DEFAULT_ROAD_GRAPH = Path(__file__).resolve().parents[1] / "data/raw/osm/urgup_drive.graphml"
DEFAULT_EDITABLE_ROAD_NETWORK = Path(__file__).resolve().parents[1] / "data/editable/road_network.geojson"
DEFAULT_DEMAND_CSV = Path(__file__).resolve().parents[1] / "data/processed/tabular/demand_cleaned.csv"
class PlanningValidationError(ValueError):
    """A structured validation failure raised before route generation."""

    def __init__(self, code: str, message: str, diagnostics: list[dict[str, Any]] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.diagnostics = diagnostics or []


#: The stop fields an optimization decides. Everything else on a stop record is
#: carried through a proposal untouched: the optimizer chooses which line calls
#: where and in what order, and knows nothing about the rest.
OPTIMIZER_OWNED_STOP_FIELDS = frozenset(
    {
        "folder_path",
        "route_id",
        "sequence",
        "demand_weight",
        "service_demand",
        "optimization_role",
    }
)


@dataclass(frozen=True)
class Stop:
    feature_id: str
    stop_id: str
    name: str
    lng: float
    lat: float
    service_demand: float
    location_role: str = ""
    #: A place the vehicles already call at where no stop is recorded — offered
    #: to the solver, never required of it. See `_candidate_stops`.
    candidate: bool = False

    @property
    def demand(self) -> float:
        """Legacy read-only alias retained for stored proposal compatibility."""
        return self.service_demand


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_name(value: Any) -> str:
    table = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    return "".join(char for char in str(value or "").translate(table).lower() if char.isalnum())


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def _demand_weights(path: Path = DEFAULT_DEMAND_CSV) -> dict[str, float]:
    prefix_by_sheet = {
        "kavaklionu": "K",
        "bahcelievler": "B",
        "evka": "E",
        "fatih": "F",
        "toki1": "T",
    }
    scores = {"0": 0.0, "AZ": 1.0, "ORTA": 2.0, "YOGUN": 3.0, "YOĞUN": 3.0}
    values: dict[str, list[float]] = {}
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                prefix = prefix_by_sheet.get(normalize_name(row.get("sheet")))
                stop_number = str(row.get("stop_id") or "").strip()
                label = str(row.get("normalized_value") or "").strip().upper()
                if prefix and stop_number and label in scores:
                    values.setdefault(f"{prefix}{stop_number}", []).append(scores[label])
    except OSError:
        return {}
    return {key: max(0.25, sum(items) / len(items)) for key, items in values.items() if items}


def _default_depot(records: list[Stop]) -> Stop:
    """Resolve the depot by role, then by the legacy name, else fail explicitly."""
    for role in ("depot", "hub"):
        role_matches = {stop.stop_id: stop for stop in records if stop.location_role == role}
        if len(role_matches) > 1:
            raise PlanningValidationError(
                "ambiguous_depot",
                f"Birden fazla {role} rolündeki durak var; depot_id açıkça seçilmeli.",
            )
        if role_matches:
            return next(iter(role_matches.values()))
    legacy = next((stop for stop in records if normalize_name(stop.name) == "merkezdurak"), None)
    if legacy is None:
        raise PlanningValidationError(
            "depot_not_found",
            "Depo belirlenemedi; depot_id, depot/hub rolü veya legacy Merkez Durak gerekli.",
        )
    return legacy


#: Least solver time that lets a candidate be dropped at all. See the note at
#: the call site; measured, not chosen.
CANDIDATE_SOLVER_FLOOR_SECONDS = 20


def _seed_with_candidates(
    seed_routes: list[list[int]],
    customer_count: int,
    candidate_count: int,
    max_stops: int,
    matrix: list[list[float]],
) -> list[list[int]]:
    """Put each candidate into the seed where it is cheapest to serve.

    Where a candidate starts matters more than it looks. Local search moves a
    node between vehicles readily enough when the move is cheap, but a place
    seeded onto a route on the far side of town is a node whose every
    improving move is expensive, and it sits there until the time limit. Round
    robin put them wherever the counter landed: two stopping places eight
    kilometres east, costing 733 m and 1.2 km to serve from the route that
    passes them, went unserved at a tolerance of two and a half kilometres.

    So each candidate is inserted at its own cheapest position — the pair of
    consecutive seed nodes where going via it adds least. That is the position
    local search would have to discover anyway, handed to it instead.

    `max_stops` still bounds a seed route, and a full one is skipped: a seed
    longer than the limit is not a valid starting assignment.
    """
    seeded = [list(route) for route in seed_routes]
    if not seeded:
        return seed_routes
    # A candidate inside a single-route neighbourhood is already in the seed,
    # placed there with its group. Adding it again is the same index twice, and
    # OR-Tools rejects the whole assignment for it.
    already = {node for route in seeded for node in route}
    for offset in range(candidate_count):
        node = 1 + customer_count + offset
        if node in already:
            continue
        best: tuple[float, int, int] | None = None
        for route_index, route in enumerate(seeded):
            if max_stops and len(route) >= max_stops:
                continue
            # The depot bookends every route, so the gaps to consider run from
            # depot→first through last→depot.
            chain = [0, *route, 0]
            for position in range(len(chain) - 1):
                before, after = chain[position], chain[position + 1]
                cost = matrix[before][node] + matrix[node][after] - matrix[before][after]
                if best is None or cost < best[0]:
                    best = (cost, route_index, position)
        if best is not None:
            seeded[best[1]].insert(best[2], node)
    return seeded


def _append_candidates_to_routes(
    routes: list[list[int]], candidate_nodes: list[int], matrix: list[list[float]]
) -> list[list[int]]:
    """Add each candidate to the finished plan at its cheapest position.

    Used where the planner cannot weigh an optional node itself. Greedy and in
    order, which is enough: the places that get here are the ones already
    within the detour budget, so the difference between the best arrangement
    and this one is small, and it costs a scan rather than another search.
    """
    result = [list(route) for route in routes]
    for node in candidate_nodes:
        best: tuple[float, int, int] | None = None
        for index, route in enumerate(result):
            chain = [0, *route, 0]
            for position in range(len(chain) - 1):
                before, after = chain[position], chain[position + 1]
                cost = matrix[before][node] + matrix[node][after] - matrix[before][after]
                if best is None or cost < best[0]:
                    best = (cost, index, position)
        if best is not None:
            result[best[1]].insert(best[2], node)
    return result


def _candidates_after_snap_failure(
    error: PlanningValidationError, candidates: list[Stop]
) -> list[Stop] | None:
    """Candidates minus the unreachable ones, or None if this is not our problem.

    None means re-raise: either the failure was something else, or one of the
    points that cannot reach a road is a real stop, which is a fact about the
    network that a planner needs to see rather than have worked around.
    """
    if error.code != "stop_road_snap_too_far":
        return None
    offenders = {str(item.get("stop_id")) for item in error.diagnostics}
    if not offenders:
        return None
    candidate_ids = {stop.stop_id for stop in candidates}
    if not offenders <= candidate_ids:
        return None
    return [stop for stop in candidates if stop.stop_id not in offenders]


def _candidate_detour_budget(params: dict[str, Any]) -> float:
    """The tolerance in the unit the cost matrix uses: metres, or seconds."""
    detour_m = max(0.0, float(params.get("candidate_detour_m") or 0))
    basis = _cost_basis(params)
    speed_kmh = max(1.0, float(params.get("avg_speed_kmh") or DEFAULT_FALLBACK_ROAD_SPEED_KMH))
    if basis == "travel_time":
        return detour_m / (speed_kmh / 3.6)
    if basis == "energy":
        # Metres of detour, priced as the same metres of level road at the
        # planning speed — the unit the energy matrix is in is Wh.
        profile = _energy_setup(params).profile
        return detour_m * energy_model.flat_energy_j_per_m(speed_kmh / 3.6, profile) / energy_model.J_PER_WH
    return detour_m


def _candidate_is_affordable(
    node: int, stops: list[Stop], matrix: list[list[float]], budget: float
) -> bool:
    """Whether serving this place could cost at most `budget` extra.

    Out and back from the nearest stop the plan has to visit anyway. That is an
    upper bound on what inserting it really costs — a real insertion continues
    to the next stop rather than returning — and on this network it tracks the
    true figure closely: 777 against 733, 1270 against 1225, 12.0 km against
    11.4. Close enough to decide with, and it needs no second solve.

    The tolerance has to act as a filter and not only as a price, because a
    price alone cannot separate them. Scaled high enough to buy a candidate
    worth 733 m against the balance term, it also bought four costing eleven to
    twelve kilometres each, and the plan went from 76 km to 108.
    """
    if budget <= 0:
        return False
    return any(
        matrix[other][node] + matrix[node][other] <= budget
        for other, stop in enumerate(stops)
        if not stop.candidate
    )


def _candidate_penalty(params: dict[str, Any]) -> int:
    """What skipping one candidate costs, in whatever unit the cost basis uses.

    The planner is asked one question — how much extra road a call at an
    unofficial stopping place is worth — and answers it in metres, because that
    is the unit they can picture. The solver works in the cost basis, so when
    that is travel time the answer is converted at the planning speed rather
    than asking the same question twice in two units.
    """
    detour = _candidate_detour_budget(params)
    # Billed against an objective that is not arc cost alone:
    # `SetGlobalSpanCostCoefficient` charges the spread between the longest and
    # shortest route at `distance_balance_weight`, so a detour landing on the
    # longest route is charged that many times over. The penalty is scaled to
    # match, which is what lets a candidate worth taking survive the comparison.
    #
    # This is why the tolerance cannot be left to do the whole job on its own.
    # Scaled far enough to rescue a cheap candidate, it also buys candidates
    # nobody wants: at 1500 m every one of the twenty-eight came in, including
    # four costing eleven to twelve kilometres each, and the plan went from 76
    # to 108 km. `_affordable_candidates` is the half of the answer that says
    # which ones are worth offering at all.
    balance = max(0.0, float(params.get("distance_balance_weight", 20)))
    return int(round(detour * (1.0 + balance)))


def _candidate_stops(params: dict[str, Any], draft: dict[str, Any] | None = None) -> list[Stop]:
    """The unofficial stopping places, as stops the solver may use or ignore.

    These carry **no demand**. Demand is a claim about how many people board
    somewhere, and nothing in the sheet says that — it says a vehicle stops. So
    what makes a candidate worth visiting is not invented demand but the
    disjunction penalty in `_optimize_vrp`: the solver takes one when the detour
    costs less than the planner said a call is worth, and leaves it otherwise.

    Absent file, absent layer, absent option — all three mean no candidates,
    which is the same as today's behaviour.

    A place already in the draft is not offered again. Applying a proposal
    writes the stopping places it used into the draft as ordinary stops, so
    from the next run on they are stops — and offering them a second time put
    the same place in one route twice, under one id. The draft rejected that,
    which is the right answer and an unhelpful place to find out: the proposal
    generated, the apply failed on a unique key.
    """
    if not params.get("include_stopping_places"):
        return []
    try:
        payload = json.loads(STOPPING_PLACES_GEOJSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    existing_ids: set[str] = set()
    existing_points: set[tuple[float, float]] = set()
    for feature in (draft or {}).get("layers", {}).get("stops", {}).get("features", []):
        properties = feature.get("properties") or {}
        existing_ids.add(str(properties.get("stop_id") or feature.get("id") or ""))
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) >= 2:
            # Exact coordinates, because that is what an applied candidate has.
            # A radius would start excluding places that merely stand near a
            # stop, which is most of them.
            existing_points.add((round(float(coords[0]), 9), round(float(coords[1]), 9)))

    records: list[Stop] = []
    for feature in payload.get("features") or []:
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        if geometry.get("type") != "Point" or len(coords) < 2:
            continue
        properties = feature.get("properties") or {}
        place_id = str(properties.get("place_id") or feature.get("id") or uuid.uuid4())
        if place_id in existing_ids:
            continue
        if (round(float(coords[0]), 9), round(float(coords[1]), 9)) in existing_points:
            continue
        records.append(
            Stop(
                feature_id=place_id,
                stop_id=place_id,
                name=str(properties.get("name") or "Duraklama"),
                lng=float(coords[0]),
                lat=float(coords[1]),
                service_demand=0.0,
                candidate=True,
            )
        )
    return records


def _extract_stops(
    draft: dict[str, Any], depot_id: str | None = None
) -> tuple[Stop, list[Stop]]:
    demand_by_name = _demand_weights()
    records: list[Stop] = []
    for feature in draft["layers"]["stops"]["features"]:
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        if geometry.get("type") != "Point" or len(coords) < 2:
            continue
        props = feature.get("properties") or {}
        explicit_demand = props.get("demand_weight")
        stop_name = str(props.get("name") or "Durak")
        demand_key_match = re.fullmatch(r"([KBEFTkbeft])(\d+)", stop_name.strip())
        demand_key = f"{demand_key_match.group(1).upper()}{int(demand_key_match.group(2))}" if demand_key_match else ""
        demand = float(explicit_demand) if explicit_demand not in (None, "") else demand_by_name.get(demand_key, 1.0)
        records.append(
            Stop(
                feature_id=str(feature.get("id") or uuid.uuid4()),
                stop_id=str(props.get("stop_id") or feature.get("id") or uuid.uuid4()),
                name=stop_name,
                lng=float(coords[0]),
                lat=float(coords[1]),
                service_demand=max(0.0, demand),
                location_role=str(props.get("location_role") or "").strip().lower(),
            )
        )
    if len(records) < 3:
        raise ValueError("Optimizasyon için en az üç durak gerekli.")
    explicit_depot_id = str(depot_id or "").strip()
    if explicit_depot_id:
        matches = [
            stop
            for stop in records
            if stop.stop_id == explicit_depot_id or stop.feature_id == explicit_depot_id
        ]
        if not matches:
            raise PlanningValidationError(
                "depot_not_found",
                f"Seçilen depot_id için durak bulunamadı: {explicit_depot_id}",
            )
        depot = matches[0]
    else:
        depot = _default_depot(records)
    customers = [stop for stop in records if stop.stop_id != depot.stop_id]
    return depot, customers


def _matrix(stops: list[Stop], road_factor: float = 1.0) -> list[list[float]]:
    return [
        [0.0 if i == j else haversine_m((a.lng, a.lat), (b.lng, b.lat)) * road_factor for j, b in enumerate(stops)]
        for i, a in enumerate(stops)
    ]


def geojson_road_graph(payload: dict[str, Any]):
    """Build a noded routing graph that honors editable road direction."""
    from numbers import Integral

    import networkx as nx
    from shapely.geometry import LineString, Point, shape
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    line_records = []
    for feature in payload.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "LineString":
            continue
        line = shape(geometry)
        if not line.is_empty and line.length > 0:
            line_records.append((line, feature.get("properties") or {}))
    if not line_records:
        raise ValueError("Düzenlenebilir yol ağında kullanılabilir yol bulunamadı.")
    lines = [record[0] for record in line_records]
    merged = unary_union(lines)
    segments = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    tree = STRtree(lines)
    graph = nx.MultiDiGraph()
    for index, geometry in enumerate(segments):
        if not isinstance(geometry, LineString) or len(geometry.coords) < 2:
            continue
        coordinates = [(float(lng), float(lat)) for lng, lat in geometry.coords]
        nearest = tree.nearest(geometry.interpolate(0.5, normalized=True))
        source_index = int(nearest) if isinstance(nearest, Integral) else lines.index(nearest)
        original, properties = line_records[source_index]
        if original.project(Point(coordinates[0])) > original.project(Point(coordinates[-1])):
            coordinates.reverse()
            geometry = LineString(coordinates)
        source = (round(coordinates[0][0], 7), round(coordinates[0][1], 7))
        target = (round(coordinates[-1][0], 7), round(coordinates[-1][1], 7))
        if source == target:
            continue
        graph.add_node(source, x=source[0], y=source[1])
        graph.add_node(target, x=target[0], y=target[1])
        length = sum(haversine_m(a, b) for a, b in pairwise(coordinates))
        direction = str(properties.get("direction") or ("forward" if properties.get("oneway") else "both"))
        try:
            speed_kmh, speed_source, _speed_confidence = modeled_editable_speed(properties)
        except (TypeError, ValueError) as exc:
            raise PlanningValidationError(
                "invalid_road_speed", f"Yol hızı sayı olmalı: {properties.get('name') or index}"
            ) from exc
        if not math.isfinite(speed_kmh) or speed_kmh <= 0:
            raise PlanningValidationError(
                "invalid_road_speed",
                f"Yol hızı sıfırdan büyük ve sonlu olmalı: {properties.get('name') or index}",
            )
        edge = {
            "length": length,
            "geometry": geometry,
            "name": str(properties.get("name") or "İsimsiz Yol"),
            "highway": str(properties.get("highway") or "residential"),
            "speed_kmh": speed_kmh,
            "speed_source": speed_source,
        }
        if direction in {"both", "forward"}:
            graph.add_edge(source, target, key=f"{index}-f", **edge)
        if direction in {"both", "reverse"}:
            graph.add_edge(target, source, key=f"{index}-r", **edge)
    if graph.number_of_edges() == 0:
        raise ValueError("Düzenlenebilir yol ağı grafiğe dönüştürülemedi.")
    return graph


def _edge_geometry(graph: Any, source: Any, target: Any, data: dict[str, Any]):
    from shapely.geometry import LineString

    geometry = data.get("geometry")
    if isinstance(geometry, str):
        from shapely import wkt

        geometry = wkt.loads(geometry)
    if geometry is not None and hasattr(geometry, "coords"):
        return geometry
    source_data = graph.nodes[source]
    target_data = graph.nodes[target]
    return LineString(
        [
            (float(source_data["x"]), float(source_data["y"])),
            (float(target_data["x"]), float(target_data["y"])),
        ]
    )


def _canonical_line_key(line: Any) -> tuple[tuple[float, float], ...]:
    """Group the two directed copies of one physical road geometry."""
    coordinates = tuple(
        (round(float(coordinate[0]), 10), round(float(coordinate[1]), 10))
        for coordinate in line.coords
    )
    reversed_coordinates = tuple(reversed(coordinates))
    return min(coordinates, reversed_coordinates)


def _snap_stops_to_road_edges(
    stops: list[Stop], graph: Any, *, include_diagnostics: bool = False
) -> list[Any] | tuple[list[Any], list[dict[str, Any]]]:
    """Insert routable virtual nodes at each stop's projection on a road edge.

    Nearest-node attachment can bypass stops located midway along a long edge.
    Splitting both directed copies of that edge makes shortest/ring paths visit
    the physical stop projection while preserving the road's direction rules.
    """
    from numbers import Integral

    from shapely.geometry import LineString, Point
    from shapely.ops import substring
    from shapely.strtree import STRtree

    reference_latitude = sum(
        float(data.get("y", 0.0)) for _node, data in graph.nodes(data=True)
    ) / max(1, graph.number_of_nodes())
    longitude_scale = EARTH_RADIUS_M * math.cos(math.radians(reference_latitude))
    latitude_scale = EARTH_RADIUS_M

    def metric_point(lng: float, lat: float) -> Point:
        return Point(math.radians(lng) * longitude_scale, math.radians(lat) * latitude_scale)

    def metric_line(line: LineString) -> LineString:
        return LineString(
            [
                (math.radians(float(lng)) * longitude_scale, math.radians(float(lat)) * latitude_scale)
                for lng, lat in line.coords
            ]
        )

    def projected_original_point(original: LineString, projected: float, measured: LineString) -> Point:
        remaining = projected
        original_coordinates = list(original.coords)
        measured_coordinates = list(measured.coords)
        for index, (start, end) in enumerate(pairwise(measured_coordinates)):
            segment_length = math.hypot(end[0] - start[0], end[1] - start[1])
            if remaining <= segment_length or index == len(measured_coordinates) - 2:
                ratio = 0.0 if segment_length <= 0 else max(0.0, min(1.0, remaining / segment_length))
                original_start = original_coordinates[index]
                original_end = original_coordinates[index + 1]
                return Point(
                    float(original_start[0]) + (float(original_end[0]) - float(original_start[0])) * ratio,
                    float(original_start[1]) + (float(original_end[1]) - float(original_start[1])) * ratio,
                )
            remaining -= segment_length
        return Point(original_coordinates[-1])

    edge_groups: dict[tuple[tuple[float, float], ...], dict[str, Any]] = {}
    for source, target, key, data in graph.edges(keys=True, data=True):
        line = _edge_geometry(graph, source, target, data)
        if line.is_empty or len(line.coords) < 2:
            continue
        group_key = _canonical_line_key(line)
        group = edge_groups.setdefault(
            group_key,
            {"line": line, "metric_line": metric_line(line), "edges": [], "snap_nodes": {}},
        )
        group["edges"].append((source, target, key, dict(data)))

    if not edge_groups:
        raise PlanningValidationError(
            "road_graph_invalid", "Yol ağında durakların bağlanabileceği kenar bulunamadı."
        )

    assignments: list[Any] = []
    diagnostics: list[dict[str, Any]] = []
    groups = list(edge_groups.values())
    metric_lines = [group["metric_line"] for group in groups]
    metric_tree = STRtree(metric_lines)
    endpoint_tolerance = 1e-10
    for stop in stops:
        measured_point = metric_point(stop.lng, stop.lat)
        nearest = metric_tree.nearest(measured_point)
        group_index = int(nearest) if isinstance(nearest, Integral) else metric_lines.index(nearest)
        group = groups[group_index]
        line = group["line"]
        measured_line = group["metric_line"]
        measured_projection = float(measured_line.project(measured_point))
        snapped = projected_original_point(line, measured_projection, measured_line)

        snapped_node = None
        for source, target, _key, _data in group["edges"]:
            for node in (source, target):
                node_data = graph.nodes[node]
                if (
                    abs(float(node_data["x"]) - float(snapped.x)) <= endpoint_tolerance
                    and abs(float(node_data["y"]) - float(snapped.y)) <= endpoint_tolerance
                ):
                    snapped_node = node
                    break
            if snapped_node is not None:
                break

        if snapped_node is None:
            snapped_node = (
                "stop-snap",
                round(float(snapped.x), 9),
                round(float(snapped.y), 9),
            )
            if snapped_node not in graph:
                graph.add_node(
                    snapped_node,
                    x=float(snapped.x),
                    y=float(snapped.y),
                    virtual_stop_snap=True,
                )
            group["snap_nodes"][snapped_node] = snapped
        assignments.append(snapped_node)
        reference = group["edges"][0] if group["edges"] else None
        snap_distance_m = float(measured_point.distance(measured_line.interpolate(measured_projection)))
        diagnostics.append(
            {
                "stop_id": stop.stop_id,
                "stop_name": stop.name,
                "snapped_node": repr(snapped_node),
                "road_reference": (
                    f"{reference[0]!r}->{reference[1]!r}:{reference[2]!r}"
                    if reference
                    else None
                ),
                "snap_distance_m": round(snap_distance_m, 1),
                "snap_distance_m_raw": snap_distance_m,
                "snap_status": "ok",
            }
        )

    for group in edge_groups.values():
        if not group["snap_nodes"]:
            continue
        for source, target, key, data in group["edges"]:
            if not graph.has_edge(source, target, key):
                continue
            line = _edge_geometry(graph, source, target, data)
            coordinates = list(line.coords)
            source_data = graph.nodes[source]
            source_point = Point(float(source_data["x"]), float(source_data["y"]))
            if source_point.distance(Point(coordinates[-1])) < source_point.distance(Point(coordinates[0])):
                coordinates.reverse()
                line = LineString(coordinates)

            split_points: list[tuple[float, Any]] = []
            for node, snapped in group["snap_nodes"].items():
                distance = float(line.project(snapped))
                if endpoint_tolerance < distance < float(line.length) - endpoint_tolerance:
                    split_points.append((distance, node))
            split_points.sort(key=lambda item: item[0])
            if not split_points:
                continue

            graph.remove_edge(source, target, key)
            distances = [0.0, *(distance for distance, _node in split_points), float(line.length)]
            nodes = [source, *(node for _distance, node in split_points), target]
            for index, (start_node, end_node) in enumerate(pairwise(nodes)):
                start_distance, end_distance = distances[index], distances[index + 1]
                if end_distance - start_distance <= endpoint_tolerance:
                    continue
                segment = substring(line, start_distance, end_distance)
                if not isinstance(segment, LineString):
                    start_data, end_data = graph.nodes[start_node], graph.nodes[end_node]
                    segment = LineString(
                        [
                            (float(start_data["x"]), float(start_data["y"])),
                            (float(end_data["x"]), float(end_data["y"])),
                        ]
                    )
                segment_coordinates = [
                    (float(coordinate[0]), float(coordinate[1]))
                    for coordinate in segment.coords
                ]
                segment_length = sum(
                    haversine_m(first, second)
                    for first, second in pairwise(segment_coordinates)
                )
                attributes = dict(data)
                attributes.pop("travel_time", None)
                attributes.pop("travel_time_seconds", None)
                attributes["geometry"] = segment
                attributes["length"] = segment_length
                graph.add_edge(
                    start_node,
                    end_node,
                    key=f"{key}:stop-snap:{index}",
                    **attributes,
                )

    return (assignments, diagnostics) if include_diagnostics else assignments


def _stop_road_diagnostic(stop: Stop, graph: Any, snapped_node: Any) -> dict[str, Any]:
    """Measure physical stop-to-road distance independently of node spacing."""
    from shapely.geometry import Point

    point = Point(stop.lng, stop.lat)
    best_distance = float("inf")
    best_reference = ""
    for source, target, key, data in graph.edges(keys=True, data=True):
        line = _edge_geometry(graph, source, target, data)
        snapped = line.interpolate(line.project(point))
        distance = haversine_m((stop.lng, stop.lat), (float(snapped.x), float(snapped.y)))
        if distance < best_distance:
            best_distance = distance
            best_reference = f"{source!r}->{target!r}:{key!r}"
    return {
        "stop_id": stop.stop_id,
        "stop_name": stop.name,
        "snapped_node": repr(snapped_node),
        "road_reference": best_reference or None,
        "snap_distance_m": round(best_distance, 1) if math.isfinite(best_distance) else None,
        "snap_status": "ok" if math.isfinite(best_distance) else "road_graph_missing",
    }


def _prepare_graph_travel_times(graph: Any, fallback_speed_kmh: float) -> str:
    """Validate edge speeds and attach travel-time weights in seconds."""
    if not math.isfinite(fallback_speed_kmh) or fallback_speed_kmh <= 0:
        raise PlanningValidationError(
            "invalid_fallback_speed",
            "Fallback yol hızı sıfırdan büyük ve sonlu olmalı.",
        )
    used_specific = False
    used_fallback = False
    for source, target, key, data in graph.edges(keys=True, data=True):
        try:
            length_m = float(data.get("length"))
        except (TypeError, ValueError) as exc:
            raise PlanningValidationError(
                "invalid_road_length",
                f"Yol uzunluğu geçersiz: {source!r}->{target!r}:{key!r}",
            ) from exc
        if not math.isfinite(length_m) or length_m < 0:
            raise PlanningValidationError(
                "invalid_road_length",
                f"Yol uzunluğu negatif veya sonlu değil: {source!r}->{target!r}:{key!r}",
            )
        raw_speed = data.get("speed_kmh", data.get("speed_kph"))
        if raw_speed in (None, ""):
            speed_kmh = fallback_speed_kmh
            speed_source = "fallback_average_speed"
            used_fallback = True
        else:
            try:
                speed_kmh = float(raw_speed)
            except (TypeError, ValueError) as exc:
                raise PlanningValidationError(
                    "invalid_road_speed",
                    f"Yol hızı geçersiz: {source!r}->{target!r}:{key!r}",
                ) from exc
            if not math.isfinite(speed_kmh) or speed_kmh <= 0:
                raise PlanningValidationError(
                    "invalid_road_speed",
                    f"Yol hızı sıfırdan büyük ve sonlu olmalı: {source!r}->{target!r}:{key!r}",
                )
            speed_source = str(data.get("speed_source") or "road_specific_assumed_speed")
            used_specific = True
        data["speed_kmh"] = speed_kmh
        data["speed_source"] = speed_source
        data["travel_time_seconds"] = length_m / (speed_kmh / 3.6)
    if used_specific and used_fallback:
        return "mixed_road_and_fallback_speed"
    if used_specific:
        return "road_specific_assumed_speed"
    return "fallback_average_speed"


def _road_context(
    stops: list[Stop],
    graph_path: Path = DEFAULT_ROAD_GRAPH,
    *,
    max_snap_distance_m: float = DEFAULT_MAX_STOP_SNAP_DISTANCE_M,
    fallback_speed_kmh: float = DEFAULT_FALLBACK_ROAD_SPEED_KMH,
    allow_non_road_fallback: bool = False,
    energy_setup: EnergySetup | None = None,
):
    """Load a road graph and validate stop attachment and directed reachability."""
    # Imported here, not at module scope: stores reaches back into this module
    # for the default paths, and importing it at the top would close the cycle.
    from .stores import editable_road_network_payload

    editable_payload = editable_road_network_payload()
    if not graph_path.exists() and editable_payload is None:
        if allow_non_road_fallback:
            return None
        raise PlanningValidationError(
            "road_graph_missing",
            "Optimizasyon için düzenlenebilir veya önbelleğe alınmış bir yol grafiği bulunamadı.",
        )
    try:
        import networkx as nx
        if editable_payload is not None:
            graph = geojson_road_graph(editable_payload)
            snapshot_kind = "editable_road_network"
            snapshot_digest = road_network_digest(editable_payload)
        else:
            import osmnx as ox

            graph = ox.load_graphml(graph_path)
            snapshot_kind = "cached_osm_drive_graph"
            snapshot_digest = file_digest(graph_path)
        nodes, snap_diagnostics = _snap_stops_to_road_edges(
            stops, graph, include_diagnostics=True
        )
        # Snapping can split one physical edge into several virtual segments.
        # Calculate travel-time weights only after that mutation so every new
        # segment receives a duration derived from its own length and speed.
        time_source = _prepare_graph_travel_times(graph, fallback_speed_kmh)
        if energy_setup is not None:
            energy_setup.graph_summary = _prepare_graph_energy(graph, energy_setup)
        invalid_snaps = [
            diagnostic
            for diagnostic in snap_diagnostics
            if diagnostic["snap_distance_m_raw"] is None
            or float(diagnostic["snap_distance_m_raw"]) > max_snap_distance_m
        ]
        if invalid_snaps:
            for diagnostic in invalid_snaps:
                diagnostic["snap_status"] = "too_far"
            labels = ", ".join(
                f"{item['stop_name']} ({item['snap_distance_m']} m)" for item in invalid_snaps[:8]
            )
            for diagnostic in snap_diagnostics:
                diagnostic.pop("snap_distance_m_raw", None)
            raise PlanningValidationError(
                "stop_road_snap_too_far",
                f"Duraklar izin verilen {max_snap_distance_m:g} m yol-snap mesafesini aşıyor: {labels}",
                invalid_snaps,
            )
        for diagnostic in snap_diagnostics:
            diagnostic.pop("snap_distance_m_raw", None)
        distance_matrix = [[0.0 for _ in stops] for _ in stops]
        travel_time_matrix = [[0.0 for _ in stops] for _ in stops]
        energy_matrix = [[0.0 for _ in stops] for _ in stops] if energy_setup is not None else None
        unreachable: list[dict[str, Any]] = []
        for source_index, source_node in enumerate(nodes):
            lengths = nx.single_source_dijkstra_path_length(graph, source_node, weight="length")
            travel_times = nx.single_source_dijkstra_path_length(
                graph, source_node, weight="travel_time_seconds"
            )
            energy_paths: dict[Any, list[Any]] = {}
            if energy_matrix is not None:
                # Dijkstra on the corrected weights finds the least-energy path;
                # the energy reported is the true sum along that path.
                _adjusted, energy_paths = nx.single_source_dijkstra(
                    graph, source_node, weight=ENERGY_ADJUSTED_KEY
                )
            for target_index, target_node in enumerate(nodes):
                if target_node not in lengths:
                    unreachable.append(
                        {
                            "source_stop_id": stops[source_index].stop_id,
                            "source_stop_name": stops[source_index].name,
                            "target_stop_id": stops[target_index].stop_id,
                            "target_stop_name": stops[target_index].name,
                        }
                    )
                    continue
                distance_matrix[source_index][target_index] = float(lengths[target_node])
                travel_time_matrix[source_index][target_index] = float(travel_times[target_node])
                if energy_matrix is not None:
                    energy_matrix[source_index][target_index] = _path_energy_wh(
                        graph, energy_paths[target_node]
                    )
        if unreachable:
            affected = sorted(
                {
                    (item["source_stop_id"], item["source_stop_name"])
                    for item in unreachable
                }
                | {
                    (item["target_stop_id"], item["target_stop_name"])
                    for item in unreachable
                }
            )
            labels = ", ".join(f"{name} [{stop_id}]" for stop_id, name in affected[:12])
            raise PlanningValidationError(
                "road_graph_unreachable",
                f"Yol grafiğinde yön kurallarına uygun bağlantı bulunmayan duraklar var: {labels}",
                unreachable,
            )
        return {
            "graph": graph,
            "nodes": nodes,
            "matrix": distance_matrix,
            "distance_matrix": distance_matrix,
            "travel_time_matrix": travel_time_matrix,
            "nx": nx,
            "source": snapshot_kind,
            "revision": int(editable_payload.get("revision", 0)) if editable_payload is not None else None,
            "digest": snapshot_digest,
            "snapshot_kind": snapshot_kind,
            "snap_diagnostics": snap_diagnostics,
            "max_snap_distance_m": max_snap_distance_m,
            "time_source": time_source,
            "energy_matrix": energy_matrix,
            "energy_setup": energy_setup,
            "energy_source": (
                f"{energy_model.ENERGY_MODEL_VERSION}_{energy_setup.options.surface}"
                if energy_setup is not None
                else None
            ),
        }
    except PlanningValidationError:
        raise
    except Exception as exc:
        raise PlanningValidationError(
            "road_graph_invalid",
            "Yol ağı yüklenemedi; kuş uçumu rota üretmek yerine optimizasyon durduruldu."
        ) from exc


def _road_context_for_params(stops: list[Stop], params: dict[str, Any]):
    try:
        max_snap_distance_m = float(
            params.get("max_stop_snap_distance_m", DEFAULT_MAX_STOP_SNAP_DISTANCE_M)
        )
    except (TypeError, ValueError) as exc:
        raise PlanningValidationError(
            "invalid_snap_distance",
            "Azami durak-yol snap mesafesi geçerli bir sayı olmalı.",
        ) from exc
    if not math.isfinite(max_snap_distance_m) or max_snap_distance_m <= 0:
        raise PlanningValidationError(
            "invalid_snap_distance",
            "Azami durak-yol snap mesafesi sıfırdan büyük ve sonlu olmalı.",
        )
    routing_mode = str(params.get("routing_mode") or "road").lower()
    if routing_mode not in {"road", "haversine_analysis"}:
        raise PlanningValidationError(
            "invalid_routing_mode",
            "Routing modu road veya haversine_analysis olmalı.",
        )
    try:
        fallback_speed_kmh = float(
            params.get("avg_speed_kmh", DEFAULT_FALLBACK_ROAD_SPEED_KMH)
        )
    except (TypeError, ValueError) as exc:
        raise PlanningValidationError(
            "invalid_fallback_speed", "Fallback yol hızı geçerli bir sayı olmalı."
        ) from exc
    if not math.isfinite(fallback_speed_kmh) or not 5 <= fallback_speed_kmh <= 120:
        raise PlanningValidationError(
            "invalid_fallback_speed", "Ortalama hız 5 ile 120 km/sa arasında olmalı."
        )
    if routing_mode == "haversine_analysis":
        if _cost_basis(params) == "energy":
            raise PlanningValidationError(
                "energy_requires_road_graph",
                "Enerji maliyeti yol geometrisi ve yükseklik gerektirir; kuş uçumu analiz modunda kullanılamaz.",
            )
        route_shape = str(params.get("route_shape") or "shortest_closed").lower()
        if route_shape != "shortest_closed":
            raise PlanningValidationError(
                "analysis_route_shape_unsupported",
                "Kuş uçumu analiz modunda yalnız shortest_closed rota biçimi kullanılabilir.",
            )
        # Analysis mode is an explicit straight-line scenario. It must not
        # silently switch to the road graph merely because one is available.
        return None
    return _road_context(
        stops,
        max_snap_distance_m=max_snap_distance_m,
        fallback_speed_kmh=fallback_speed_kmh,
        allow_non_road_fallback=False,
        energy_setup=_energy_setup(params, stops) if _cost_basis(params) == "energy" else None,
    )


def _undirected_edge_key(source: Any, target: Any) -> tuple[Any, Any]:
    """Return a stable physical-road key while preserving directed graph routing."""
    return tuple(sorted((source, target), key=repr))


def _edge_data_for_weight(
    graph: Any, source: Any, target: Any, weight: str = "length"
) -> dict[str, Any]:
    edge_data = graph.get_edge_data(source, target) or {}
    candidates = (
        list(edge_data.values())
        if edge_data and all(isinstance(value, dict) for value in edge_data.values())
        else [edge_data]
    )
    return min(candidates, key=lambda item: float(item.get(weight, float("inf"))))


def _edge_length(graph: Any, source: Any, target: Any) -> float:
    return float(_edge_data_for_weight(graph, source, target).get("length", float("inf")))


def _ring_path(
    graph: Any,
    source: Any,
    target: Any,
    used_edges: set[tuple[Any, Any]],
    mode: str,
    nx: Any,
    weight: str = "length",
):
    """Find a directed path with soft or hard physical-edge reuse avoidance."""
    if mode == "shortest_closed" or not used_edges:
        return nx.shortest_path(graph, source, target, weight=weight), False

    def penalized_weight(start: Any, end: Any, edge_data: dict[str, Any]) -> float:
        candidates = (
            list(edge_data.values())
            if edge_data and all(isinstance(value, dict) for value in edge_data.values())
            else [edge_data]
        )
        value = min(float(item.get(weight, float("inf"))) for item in candidates)
        penalty = 8.0 if mode == "prefer_ring" else 50.0
        return value * (penalty if _undirected_edge_key(start, end) in used_edges else 1.0)

    if mode == "strict_ring":
        available = graph.copy()
        for start, end, key in list(available.edges(keys=True)):
            if _undirected_edge_key(start, end) in used_edges:
                available.remove_edge(start, end, key)
        try:
            return nx.shortest_path(available, source, target, weight=weight), False
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            # Bridges and cul-de-sacs make a fully edge-disjoint ring impossible.
            return nx.shortest_path(graph, source, target, weight=penalized_weight), True

    return nx.shortest_path(graph, source, target, weight=penalized_weight), False


def _route_geometry(
    order: list[int],
    stops: list[Stop],
    context,
    route_shape: str = "shortest_closed",
    cost_basis: str = "distance",
    fallback_speed_kmh: float = DEFAULT_FALLBACK_ROAD_SPEED_KMH,
) -> dict[str, Any]:
    indexes = [0, *order, 0]
    if context is None:
        coordinates = [[stops[index].lng, stops[index].lat] for index in indexes]
        distance_m = sum(
            haversine_m((a[0], a[1]), (b[0], b[1])) for a, b in pairwise(coordinates)
        )
        return {
            "coordinates": coordinates,
            "distance_m": distance_m,
            "road_travel_time_seconds": distance_m / (fallback_speed_kmh / 3.6),
            "reused_edge_distance_m": 0.0,
            "unavoidable_reuse_legs": 0,
            "edge_lengths": {},
            "roads": [],
            "highway_distance_m": {},
            "edge_speeds": [],
            "edge_records": [],
            "energy_j": None,
        }
    graph, nodes, nx = context["graph"], context["nodes"], context["nx"]
    coordinates = []
    used_edges: set[tuple[Any, Any]] = set()
    # Which physical road pieces the line drives, and how far on each. The
    # first feeds the overlap figures in `_build_proposal`; the other two are
    # what "the streets this route uses" means in the deliverables.
    edge_lengths: dict[tuple[Any, Any], float] = {}
    roads: list[dict[str, Any]] = []
    highway_distance_m: dict[str, float] = {}
    distance_m = 0.0
    reused_distance_m = 0.0
    road_travel_time_seconds = 0.0
    unavoidable_reuse_legs = 0
    # The pieces of road in driving order, each with its speed: `edge_speeds`
    # are index ranges into `coordinates` compact enough to store, and
    # `edge_records` the same thing ready for the energy model to re-drive on
    # any surface. Both exist so a solution can be evaluated later without
    # re-routing it — "same routes, other terrain" is a comparison of geometry.
    edge_speeds: list[list[float]] = []
    edge_records: list[energy_model.DirectedEdge] = []
    energy_j: float | None = 0.0 if cost_basis == "energy" else None
    weight = _weight_attribute(cost_basis)
    try:
        for start, end in pairwise(indexes):
            node_path, unavoidable = _ring_path(
                graph, nodes[start], nodes[end], used_edges, route_shape, nx, weight
            )
            unavoidable_reuse_legs += int(unavoidable)
            if len(node_path) == 1 and not coordinates:
                node = graph.nodes[node_path[0]]
                coordinates.append([float(node["x"]), float(node["y"])])
            for source_node, target_node in pairwise(node_path):
                edge = _edge_data_for_weight(graph, source_node, target_node, weight)
                piece = _oriented_edge_coordinates(graph, source_node, edge)
                if len(piece) < 2:
                    source_data, target_data = graph.nodes[source_node], graph.nodes[target_node]
                    piece = [
                        [float(source_data["x"]), float(source_data["y"])],
                        [float(target_data["x"]), float(target_data["y"])],
                    ]
                if coordinates and coordinates[-1] == piece[0]:
                    start_index = len(coordinates) - 1
                    coordinates.extend(piece[1:])
                else:
                    start_index = len(coordinates)
                    coordinates.extend(piece)
                end_index = len(coordinates) - 1
                length = float(edge["length"])
                edge_speed = float(edge.get("speed_kmh") or fallback_speed_kmh)
                edge_time = edge.get("travel_time_seconds")
                if edge_time is None:
                    edge_time = length / (edge_speed / 3.6)
                road_travel_time_seconds += float(edge_time)
                edge_speeds.append([start_index, end_index, round(edge_speed, 1)])
                edge_records.append(
                    energy_model.DirectedEdge(tuple((p[0], p[1]) for p in piece), edge_speed)
                )
                if energy_j is not None:
                    energy_j += float(edge.get(ENERGY_KEY, 0.0))
                key = _undirected_edge_key(source_node, target_node)
                distance_m += length
                if key in used_edges:
                    reused_distance_m += length
                used_edges.add(key)
                edge_lengths[key] = length
                name = str(edge.get("name") or "İsimsiz Yol")
                highway = str(edge.get("highway") or "residential")
                highway_distance_m[highway] = highway_distance_m.get(highway, 0.0) + length
                if roads and roads[-1]["name"] == name and roads[-1]["highway"] == highway:
                    roads[-1]["distance_m"] += length
                else:
                    roads.append({"name": name, "highway": highway, "distance_m": length})
    except Exception as exc:
        raise ValueError(
            "Yol ağı üzerinde kesintisiz rota kurulamadı; kuş uçumu çizgi üretilmedi."
        ) from exc
    return {
        "coordinates": coordinates,
        "distance_m": distance_m,
        "road_travel_time_seconds": road_travel_time_seconds,
        "reused_edge_distance_m": reused_distance_m,
        "unavoidable_reuse_legs": unavoidable_reuse_legs,
        "edge_lengths": edge_lengths,
        "roads": roads,
        "highway_distance_m": highway_distance_m,
        "edge_speeds": edge_speeds,
        "edge_records": edge_records,
        "energy_j": energy_j,
    }


def _major_roads(roads: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """The named streets a route drives, longest first, each named once.

    A route runs down the same boulevard on the way out and the way back and
    the sequence records it twice; the reader wants the boulevard once with the
    total. Unnamed pieces are left out — "İsimsiz Yol" tells nobody anything.
    """
    totals: dict[str, dict[str, Any]] = {}
    for piece in roads:
        name = piece["name"]
        if name == "İsimsiz Yol":
            continue
        entry = totals.setdefault(name, {"name": name, "highway": piece["highway"], "distance_m": 0.0})
        entry["distance_m"] += float(piece["distance_m"])
    ranked = sorted(totals.values(), key=lambda item: -item["distance_m"])
    return [{**item, "distance_m": round(item["distance_m"], 1)} for item in ranked[:limit]]


def _shared_edge_metrics(
    edge_lengths_by_route: list[dict[tuple[Any, Any], float]],
) -> tuple[list[float], float]:
    """How much road each route shares with another route, and the network total.

    Two lines on the same street is not a fault in itself — every radial line
    has to use the roads into the centre — but it is the thing the planner
    asks about when they say "çakışma", so it is measured rather than argued.
    Per route: metres of its road also driven by some other route. Network:
    metres of road driven by two or more routes, counted once.
    """
    users: dict[tuple[Any, Any], int] = {}
    for edges in edge_lengths_by_route:
        for key in edges:
            users[key] = users.get(key, 0) + 1
    per_route = [
        sum(length for key, length in edges.items() if users[key] > 1)
        for edges in edge_lengths_by_route
    ]
    lengths = {key: length for edges in edge_lengths_by_route for key, length in edges.items()}
    total = sum(length for key, length in lengths.items() if users[key] > 1)
    return per_route, total


def _route_coordinates(order: list[int], stops: list[Stop], context) -> list[list[float]]:
    """Backward-compatible shortest closed-tour geometry helper."""
    return _route_geometry(order, stops, context)["coordinates"]


def _node_path_coordinates(
    graph: Any, node_path: list[Any], weight: str = "length"
) -> list[list[float]]:
    """Expand an OSM node path with each edge's full curved geometry."""
    if not node_path:
        return []
    if len(node_path) == 1:
        node = graph.nodes[node_path[0]]
        return [[float(node["x"]), float(node["y"])]]

    coordinates: list[list[float]] = []
    for source, target in pairwise(node_path):
        edge_data = graph.get_edge_data(source, target) or {}
        parallel_edges = bool(edge_data) and all(isinstance(value, dict) for value in edge_data.values())
        candidates = list(edge_data.values()) if parallel_edges else [edge_data]
        edge = min(candidates, key=lambda item: float(item.get(weight, float("inf"))))
        geometry = edge.get("geometry")
        if isinstance(geometry, str):
            from shapely import wkt

            geometry = wkt.loads(geometry)
        if geometry is not None and hasattr(geometry, "coords"):
            segment = [[float(lng), float(lat)] for lng, lat in geometry.coords]
        else:
            source_node, target_node = graph.nodes[source], graph.nodes[target]
            segment = [
                [float(source_node["x"]), float(source_node["y"])],
                [float(target_node["x"]), float(target_node["y"])],
            ]

        source_node = graph.nodes[source]
        source_point = (float(source_node["x"]), float(source_node["y"]))
        first_distance = (segment[0][0] - source_point[0]) ** 2 + (segment[0][1] - source_point[1]) ** 2
        last_distance = (segment[-1][0] - source_point[0]) ** 2 + (segment[-1][1] - source_point[1]) ** 2
        if last_distance < first_distance:
            segment.reverse()
        if coordinates and segment and coordinates[-1] == segment[0]:
            segment = segment[1:]
        coordinates.extend(segment)
    return coordinates


def _route_distance(route: list[int], matrix: list[list[float]]) -> float:
    if not route:
        return 0.0
    total = matrix[0][route[0]] + matrix[route[-1]][0]
    total += sum(matrix[a][b] for a, b in pairwise(route))
    return total


def _cost_basis(params: dict[str, Any]) -> str:
    basis = str(params.get("cost_basis") or "distance").lower()
    if basis not in {"distance", "travel_time", "energy"}:
        raise PlanningValidationError(
            "invalid_cost_basis", "cost_basis distance, travel_time veya energy olmalı."
        )
    return basis


def _weight_attribute(cost_basis: str) -> str:
    """The edge attribute a shortest path is measured in, per cost basis."""
    return {
        "travel_time": "travel_time_seconds",
        "energy": ENERGY_ADJUSTED_KEY,
    }.get(cost_basis, "length")


@dataclass
class EnergySetup:
    """Everything the energy model needs for one run, resolved once."""

    profile: energy_model.VehicleProfile
    options: energy_model.EnergyOptions
    #: The grid energy is read from — derived (scaled / perturbed) when asked.
    grid: Any
    #: The unmodified grid, for evaluating a solution on the real terrain.
    base_grid: Any
    reference_m: float | None
    graph_summary: dict[str, Any] | None = None

    @property
    def dem_grid(self) -> Any:
        return self.grid

    def elevation_contract(self) -> dict[str, Any]:
        grid = self.base_grid
        if grid is None:
            return {
                "elevation_source": None,
                "elevation_confidence": None,
                "elevation_dataset": None,
                "elevation_sha256": None,
                "grade_uncertainty_percent": None,
            }
        return {
            "elevation_source": grid.source,
            "elevation_confidence": grid.confidence,
            "elevation_dataset": grid.meta.get("dataset"),
            "elevation_sha256": grid.meta.get("sha256"),
            "vertical_accuracy_m": grid.meta.get("vertical_accuracy_m"),
            "grade_uncertainty_percent": round(grid.grade_uncertainty_percent(), 1),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_version": energy_model.ENERGY_MODEL_VERSION,
            "energy_unit": "Wh",
            "surface": self.options.surface,
            "vehicle_profile": self.profile.as_dict(),
            "energy_options": self.options.as_dict(),
            "reference_elevation_m": self.reference_m,
            "sample_step_m": self.options.sample_step_m,
            **self.elevation_contract(),
            **(self.graph_summary or {}),
        }


def _energy_setup(params: dict[str, Any], stops: list[Stop] | None = None) -> EnergySetup:
    """Resolve profile, options and terrain for a run; fail with a planning error."""
    raw = params.get("energy_options") or {}
    if not isinstance(raw, dict):
        raise PlanningValidationError("invalid_energy_options", "energy_options bir nesne olmalı.")
    try:
        profile = energy_model.vehicle_profile(
            params.get("vehicle_profile") or None,
            mass_scenario=str(raw.get("mass_scenario") or "average"),
            eta_regen=raw.get("eta_regen"),
            p_regen_max_kw=raw.get("p_regen_max_kw"),
            path=raw.get("vehicle_profiles_path"),
        )
        options = energy_model.EnergyOptions(
            surface=str(raw.get("surface") or "dem").lower(),
            sample_step_m=float(raw.get("sample_step_m", 25.0)),
            smooth_window_m=float(raw.get("smooth_window_m", 100.0)),
            grade_cap=float(raw.get("grade_cap", 0.20)),
            dem_scale=float(raw.get("dem_scale", 1.0)),
            dem_noise_sigma_m=float(raw.get("dem_noise_sigma_m", 0.0)),
            dem_noise_corr_cells=int(raw.get("dem_noise_corr_cells", 3) or 3),
            seed=(int(raw["seed"]) if raw.get("seed") not in (None, "") else None),
            exact_potentials=raw.get("exact_potentials") in {True, "1", "true", "yes", "on"},
        )
    except (energy_model.EnergyConfigurationError, TypeError, ValueError) as exc:
        raise PlanningValidationError("invalid_energy_options", str(exc)) from exc
    try:
        base_grid = load_grid(raw.get("elevation_dir") or None)
    except ElevationUnavailableError as exc:
        raise PlanningValidationError(
            "elevation_grid_missing",
            f"Enerji maliyeti için yükseklik gridi gerekli: {exc}",
        ) from exc
    reference_m: float | None = None
    if stops:
        depot = stops[0]
        reference_m = base_grid.at(depot.lat, depot.lng)
    grid = base_grid
    if options.dem_scale != 1.0 or options.dem_noise_sigma_m > 0:
        grid = energy_model.derive_grid(
            base_grid,
            dem_scale=options.dem_scale,
            reference_m=reference_m,
            noise_sigma_m=options.dem_noise_sigma_m,
            seed=options.seed,
            noise_corr_cells=options.dem_noise_corr_cells,
        )
    return EnergySetup(profile=profile, options=options, grid=grid, base_grid=base_grid, reference_m=reference_m)


def _oriented_edge_coordinates(graph: Any, source: Any, data: dict[str, Any]) -> list[list[float]]:
    """An edge's polyline in driving order, starting at ``source``."""
    geometry = data.get("geometry")
    if isinstance(geometry, str):
        from shapely import wkt

        geometry = wkt.loads(geometry)
    if geometry is not None and hasattr(geometry, "coords"):
        segment = [[float(lng), float(lat)] for lng, lat in geometry.coords]
    else:
        return []
    source_node = graph.nodes[source]
    source_point = (float(source_node["x"]), float(source_node["y"]))
    first_distance = (segment[0][0] - source_point[0]) ** 2 + (segment[0][1] - source_point[1]) ** 2
    last_distance = (segment[-1][0] - source_point[0]) ** 2 + (segment[-1][1] - source_point[1]) ** 2
    if last_distance < first_distance:
        segment.reverse()
    return segment


def _prepare_graph_energy(graph: Any, setup: EnergySetup) -> dict[str, Any]:
    """
    Attach a directed energy and its potential-corrected weight to every edge.

    Runs after stop snapping, like the travel-time pass, because snapping
    splits edges and a split piece must be costed on its own geometry. The
    correction uses the node's own elevation, so the two ends of every edge are
    the very samples the potential is built from; a corrected weight can still
    come out negative when the grade cap truncates a climb inside the edge,
    and those are counted and clipped to zero rather than hidden. Realised
    energies are always summed from the true per-edge values along the path
    found, so a clipped weight can only make a path slightly suboptimal, never
    misreport its energy.
    """
    profile, options = setup.profile, setup.options
    grid = setup.grid if options.surface == "dem" else None
    potentials: dict[Any, float] = {}
    n_nodata_nodes = 0

    def potential(node: Any) -> float:
        nonlocal n_nodata_nodes
        if node in potentials:
            return potentials[node]
        value = 0.0
        if grid is not None:
            data = graph.nodes[node]
            height = grid.at(float(data["y"]), float(data["x"]))
            if height is None:
                n_nodata_nodes += 1
            else:
                value = energy_model.potential_j(height, profile)
        potentials[node] = value
        return value

    n_edges = n_negative = n_capped = n_nodata = 0
    for source, target, _key, data in graph.edges(keys=True, data=True):
        coordinates = _oriented_edge_coordinates(graph, source, data)
        if len(coordinates) < 2:
            source_node, target_node = graph.nodes[source], graph.nodes[target]
            coordinates = [
                [float(source_node["x"]), float(source_node["y"])],
                [float(target_node["x"]), float(target_node["y"])],
            ]
        speed_mps = float(data.get("speed_kmh") or DEFAULT_FALLBACK_ROAD_SPEED_KMH) / 3.6
        segment = energy_model.segment_energy_j(coordinates, speed_mps, profile, options, grid)
        data[ENERGY_KEY] = segment.total_j
        n_edges += 1
        n_capped += segment.n_capped
        n_nodata += segment.n_nodata
    summary: dict[str, Any] = {"potential_method": "elevation_physics"}
    if options.exact_potentials:
        # Johnson's reweighting proper: h(v) is the least true energy of any
        # path into v from a virtual source joined to every node at cost 0.
        # Then w + h(u) − h(v) ≥ 0 on every edge by the triangle inequality,
        # and nothing has to be clipped. Costs one Bellman-Ford per run.
        exact, passes = _johnson_potentials(graph, ENERGY_KEY)
        potentials.clear()
        potentials.update(exact)
        summary = {"potential_method": "johnson_bellman_ford", "bellman_ford_passes": passes,
                   "negative_cycle": False}
    for source, target, _key, data in graph.edges(keys=True, data=True):
        adjusted = data[ENERGY_KEY] + potential(source) - potential(target)
        if adjusted < -1e-6:
            n_negative += 1
        data[ENERGY_ADJUSTED_KEY] = max(0.0, adjusted)
    return {
        "energy_edges": n_edges,
        "n_negative_adjusted": n_negative,
        "n_capped_subsegments": n_capped,
        "n_nodata_samples": n_nodata,
        "n_nodata_nodes": n_nodata_nodes,
        **summary,
    }


def _johnson_potentials(graph: Any, weight: str) -> tuple[dict[Any, float], int]:
    """
    Bellman-Ford potentials from a virtual source, or a planning error.

    Queue-based (SPFA) relaxation over the true directed energies; a node
    relaxed more times than there are nodes is on a negative cycle, which the
    physics forbids on a closed loop unless the grade cap has truncated a
    climb whose descent it left whole — and that is worth refusing loudly.
    """
    from collections import deque

    potential: dict[Any, float] = {node: 0.0 for node in graph.nodes}
    queue = deque(graph.nodes)
    queued = set(potential)
    relaxations: dict[Any, int] = {}
    limit = graph.number_of_nodes() + 1
    passes = 0
    while queue:
        node = queue.popleft()
        queued.discard(node)
        base = potential[node]
        for _source, target, data in graph.out_edges(node, data=True):
            candidate = base + float(data.get(weight, 0.0))
            if candidate < potential[target] - 1e-9:
                potential[target] = candidate
                relaxations[target] = relaxations.get(target, 0) + 1
                passes = max(passes, relaxations[target])
                if relaxations[target] > limit:
                    raise PlanningValidationError(
                        "energy_negative_cycle",
                        "Enerji grafiğinde negatif çevrim var; kesin potansiyel hesaplanamadı.",
                    )
                if target not in queued:
                    queue.append(target)
                    queued.add(target)
    return potential, passes


def _path_energy_wh(graph: Any, node_path: list[Any]) -> float:
    """True energy along a node path, from the per-edge values, in Wh."""
    total = 0.0
    for source, target in pairwise(node_path):
        edge = _edge_data_for_weight(graph, source, target, ENERGY_ADJUSTED_KEY)
        total += float(edge.get(ENERGY_KEY, 0.0))
    return total / energy_model.J_PER_WH


def _planning_matrices(
    stops: list[Stop], road_context: Any, params: dict[str, Any]
) -> tuple[list[list[float]], list[list[float]], list[list[float]]]:
    distance_matrix = road_context["distance_matrix"] if road_context else _matrix(stops)
    if road_context:
        travel_time_matrix = road_context["travel_time_matrix"]
    else:
        speed_kmh = float(params.get("avg_speed_kmh", DEFAULT_FALLBACK_ROAD_SPEED_KMH))
        travel_time_matrix = [
            [distance / (speed_kmh / 3.6) for distance in row]
            for row in distance_matrix
        ]
    basis = _cost_basis(params)
    if basis == "travel_time":
        selected = travel_time_matrix
    elif basis == "energy":
        if not road_context or road_context.get("energy_matrix") is None:
            raise PlanningValidationError(
                "energy_requires_road_graph",
                "Enerji maliyeti için yol grafiği ve yükseklik gridi gerekli.",
            )
        selected = road_context["energy_matrix"]
    else:
        selected = distance_matrix
    return selected, distance_matrix, travel_time_matrix


def _baseline_distance(draft: dict[str, Any]) -> float:
    total = 0.0
    for feature in draft["layers"]["routes"]["features"]:
        geometry = feature.get("geometry") or {}
        segments = geometry.get("coordinates") or []
        if geometry.get("type") == "LineString":
            segments = [segments]
        elif geometry.get("type") != "MultiLineString":
            continue
        for segment in segments:
            total += sum(haversine_m(tuple(start[:2]), tuple(end[:2])) for start, end in pairwise(segment))
    return total


def exact_tsp(route: list[int], matrix: list[list[float]]) -> list[int]:
    if len(route) < 2:
        return route[:]
    return list(min(itertools.permutations(route), key=lambda order: _route_distance(list(order), matrix)))


def two_opt(route: list[int], matrix: list[list[float]]) -> list[int]:
    best = route[:]
    best_distance = _route_distance(best, matrix)
    improved = True
    while improved:
        improved = False
        for left in range(len(best) - 1):
            for right in range(left + 2, len(best) + 1):
                candidate = best[:left] + list(reversed(best[left:right])) + best[right:]
                distance = _route_distance(candidate, matrix)
                if distance + 0.01 < best_distance:
                    best, best_distance, improved = candidate, distance, True
                    break
            if improved:
                break
    return best


def or_opt(route: list[int], matrix: list[list[float]]) -> list[int]:
    """
    Move blocks of one to three stops elsewhere, either way round.

    2-opt only reverses; on an asymmetric matrix a reversal is rarely the
    improving move, because it changes the direction every arc in the block is
    driven. Relocation keeps direction, which is what an energy matrix — where
    the two directions of a hill differ — rewards. Costs are recomputed from
    the full directed tour, so nothing here assumes symmetry.
    """
    best = route[:]
    best_cost = _route_distance(best, matrix)
    improved = True
    while improved:
        improved = False
        count = len(best)
        for size in (1, 2, 3):
            if size > count - 1:
                break
            for left in range(0, count - size + 1):
                block = best[left:left + size]
                rest = best[:left] + best[left + size:]
                for position in range(0, len(rest) + 1):
                    if position == left:
                        continue
                    for variant in (block, block[::-1]):
                        candidate = rest[:position] + variant + rest[position:]
                        cost = _route_distance(candidate, matrix)
                        if cost + 0.01 < best_cost:
                            best, best_cost, improved = candidate, cost, True
                            break
                    if improved:
                        break
                if improved:
                    break
            if improved:
                break
    return best


def _local_search(route: list[int], matrix: list[list[float]], cost_basis: str) -> list[int]:
    """2-opt for the symmetric bases; 2-opt then or-opt when the matrix is directed."""
    improved = two_opt(route, matrix)
    if cost_basis == "energy":
        improved = or_opt(improved, matrix)
    return improved


def _route_sizes(stop_count: int, route_count: int) -> list[int]:
    base, extra = divmod(stop_count, route_count)
    return [base + (1 if index < extra else 0) for index in range(route_count)]


def _split(permutation: list[int], route_sizes: list[int]) -> list[list[int]]:
    result, cursor = [], 0
    for size in route_sizes:
        result.append(permutation[cursor : cursor + size])
        cursor += size
    return result


def _fitness(
    permutation: list[int],
    route_sizes: list[int],
    matrix: list[list[float]],
    protected_groups: list[list[int]] | None = None,
    *,
    distance_constraint_matrix: list[list[float]] | None = None,
    distance_balance_weight: float = 0.38,
    longest_route_weight: float = 0.28,
    max_route_distance_m: float = 0.0,
    max_stops_per_route: int = 0,
) -> float:
    routes = _split(permutation, route_sizes)
    distances = [_route_distance(route, matrix) for route in routes]
    mean_distance = sum(distances) / max(1, len(distances))
    imbalance = sum(abs(value - mean_distance) for value in distances)
    split_count = 0
    for group in protected_groups or []:
        members = set(group)
        route_hits = sum(bool(members.intersection(route)) for route in routes)
        split_count += max(0, route_hits - 1)
    constraint_penalty = 0.0
    if max_route_distance_m > 0:
        route_distances = [
            _route_distance(route, distance_constraint_matrix or matrix) for route in routes
        ]
        constraint_penalty += sum(
            max(0.0, value - max_route_distance_m) for value in route_distances
        ) * 10_000
    if max_stops_per_route > 0:
        constraint_penalty += sum(max(0, len(route) - max_stops_per_route) for route in routes) * 5_000_000
    return (
        sum(distances)
        + distance_balance_weight * imbalance
        + longest_route_weight * max(distances, default=0)
        + split_count * 5_000_000
        + constraint_penalty
    )


def _fold_small_groups(
    groups: list[list[int]], matrix: list[list[float]], limit: int
) -> list[list[int]]:
    """Fold neighbourhoods with few stops into the nearest larger one.

    Planning only. Nothing is written back: the boundaries, the names and the
    stops stay exactly as they are, and this changes one thing — which stops the
    single-route rule insists must share a vehicle.

    The rule is one line per neighbourhood, so every neighbourhood is a claim on
    a route. With more neighbourhoods than routes that claim has to be given up
    somewhere, and the solver gives it up wherever the arithmetic lands: Ürgüp
    had one route carrying 85 stops across four neighbourhoods while two others
    carried nine each. Folding the small ones in first spends the routes where
    the stops are — the same network came back as 60, 25, 24, 20, 18, and the
    Aksalur line, for 3.4 km more.

    Nearest is measured on the planning matrix rather than in degrees, so it is
    the road between them and not the map distance.
    """
    if limit <= 0:
        return groups
    small = [group for group in groups if len(group) < limit]
    large = [list(group) for group in groups if len(group) >= limit]
    if not small or not large:
        # Nothing to fold into: keeping the small ones separate is better than
        # collapsing the whole town onto one vehicle.
        return groups
    for group in small:
        nearest = min(
            large,
            key=lambda target: min(matrix[a][b] for a in group for b in target),
        )
        nearest.extend(group)
    return large


def _single_route_groups(
    draft: dict[str, Any],
    customers: list[Stop],
    *,
    protect_all: bool = False,
    candidates: list[Stop] | None = None,
) -> list[list[int]]:
    """Return node indexes covered by single-route neighborhoods.

    A stopping place inside a neighbourhood is treated as that neighbourhood's
    stop, by the same boundary test the stops use. The rule says a
    neighbourhood is served by one line; a place the vehicles already call at,
    standing inside it, is part of what that means.

    Thirteen of Ürgüp's twenty-eight are outside every boundary and stay
    outside the rule, exactly as a stop out there would — four of them by
    around four kilometres, on the road to Aksalur. There is no neighbourhood
    they are in, and inventing the nearest one would drag a line out of town
    to reach it.
    """
    from shapely.geometry import Point, shape

    # A stop can be assigned to a neighbourhood it does not stand in. Which line
    # serves a stop is an operating decision; the boundary is a fact about
    # where it is, and outside every boundary there was no way to express the
    # first at all — those stops fell out of the rule entirely.
    assigned: dict[str, str] = {}
    for feature in draft.get("layers", {}).get("stops", {}).get("features", []):
        properties = feature.get("properties") or {}
        override = str(properties.get("mahalle_override") or "").strip()
        if override:
            assigned[str(properties.get("stop_id") or feature.get("id") or "")] = override

    groups: list[list[int]] = []
    for feature in draft.get("layers", {}).get("mahalle", {}).get("features", []):
        properties = feature.get("properties") or {}
        enabled = protect_all or properties.get("single_route_only")
        if enabled not in {True, "1", "true", "yes", "on"}:
            continue
        geometry = feature.get("geometry")
        if not geometry:
            continue
        area = shape(geometry)
        name = str(properties.get("name") or "").strip()
        members = [
            index
            for index, stop in enumerate(customers, start=1)
            if assigned.get(stop.stop_id, "") == name
            or (not assigned.get(stop.stop_id) and area.covers(Point(stop.lng, stop.lat)))
        ]
        # Candidates sit after the customers in the node list, so their indexes
        # carry on from there.
        members.extend(
            1 + len(customers) + offset
            for offset, stop in enumerate(candidates or [])
            if area.covers(Point(stop.lng, stop.lat))
        )
        if members:
            groups.append(members)
    return groups


def _merged_groups(groups: list[list[int]]) -> list[list[int]]:
    """Merge overlapping same-route groups into disjoint hard components."""
    components: list[set[int]] = []
    for group in groups:
        current = set(group)
        overlapping = [component for component in components if component.intersection(current)]
        if overlapping:
            for component in overlapping:
                current.update(component)
                components.remove(component)
        components.append(current)
    return [sorted(component) for component in components]


def _vrp_seed_routes(
    customer_count: int,
    route_count: int,
    groups: list[list[int]],
    matrix: list[list[float]],
    *,
    min_stops: int,
    max_stops: int,
) -> list[list[int]] | None:
    """Build a feasible bin-packed seed for hard same-vehicle groups."""
    if not groups:
        return None
    components = _merged_groups(groups)
    grouped = {node for component in components for node in component}
    items = [*components, *[[node] for node in range(1, customer_count + 1) if node not in grouped]]
    if min_stops > 0 and len(items) < route_count:
        raise ValueError("Mahalle tek-rota grupları asgari sayıda dolu rota oluşturmaya izin vermiyor.")

    for item in items:
        if max_stops and len(item) > max_stops:
            raise ValueError(
                f"Tek rota kuralına bağlı {len(item)} durak, rota başına {max_stops} durak sınırını aşıyor."
            )

    bins: list[list[int]] = [[] for _ in range(route_count)]
    for item in sorted(items, key=lambda value: (len(value), value), reverse=True):
        candidates = [
            index
            for index, route in enumerate(bins)
            if (not max_stops or len(route) + len(item) <= max_stops)
        ]
        if not candidates:
            return None
        target = min(candidates, key=lambda index: (len(bins[index]), index))
        bins[target].extend(item)
    return [two_opt(route, matrix) for route in bins]


def _consolidate_single_route_groups(
    routes: list[list[int]], groups: list[list[int]], matrix: list[list[float]]
) -> list[list[int]]:
    """Hard-enforce each protected neighborhood after GA scoring."""
    result = [route[:] for route in routes]
    for group in groups:
        members = set(group)
        counts = [sum(item in members for item in route) for route in result]
        if sum(count > 0 for count in counts) <= 1:
            continue
        without = [[item for item in route if item not in members] for route in result]
        target = min(
            range(len(result)),
            key=lambda index: (
                -counts[index],
                _route_distance([*without[index], *group], matrix)
                - _route_distance(without[index], matrix),
            ),
        )
        result = without
        result[target].extend(group)

    protected = {item for group in groups for item in group}
    for empty_index, route in enumerate(result):
        if route:
            continue
        donors = [
            index
            for index, candidate in enumerate(result)
            if len(candidate) > 1 and any(item not in protected for item in candidate)
        ]
        if not donors:
            continue
        donor = max(donors, key=lambda index: len(result[index]))
        moved = next(item for item in reversed(result[donor]) if item not in protected)
        result[donor].remove(moved)
        result[empty_index].append(moved)
    return result


def _ordered_crossover(a: list[int], b: list[int], rng: random.Random) -> list[int]:
    left, right = sorted(rng.sample(range(len(a)), 2))
    child: list[int | None] = [None] * len(a)
    child[left:right] = a[left:right]
    remaining = [item for item in b if item not in child]
    cursor = 0
    filled: list[int] = []
    for value in child:
        if value is None:
            filled.append(remaining[cursor])
            cursor += 1
        else:
            filled.append(value)
    return filled


def _mutate(values: list[int], rng: random.Random, mutation_rate: float) -> None:
    if len(values) < 2 or rng.random() >= mutation_rate:
        return
    left, right = sorted(rng.sample(range(len(values)), 2))
    if rng.random() < 0.5:
        values[left], values[right] = values[right], values[left]
    else:
        values[left : right + 1] = reversed(values[left : right + 1])


def _angle_seed(stops: list[Stop], depot: Stop) -> list[int]:
    return sorted(
        range(1, len(stops)),
        key=lambda index: math.atan2(stops[index].lat - depot.lat, stops[index].lng - depot.lng),
    )


def _existing_route_seed(draft: dict[str, Any], customers: list[Stop], route_count: int):
    """Use current route membership/order as a strong GA seed when route counts match."""
    try:
        from shapely.geometry import Point, shape

        route_features = draft["layers"]["routes"]["features"]
        if len(route_features) != route_count:
            return None
        customer_indexes = {stop.stop_id: index for index, stop in enumerate(customers, start=1)}
        route_indexes = {
            str((feature.get("properties") or {}).get("route_id") or feature.get("id")): index
            for index, feature in enumerate(route_features)
        }
        explicit_groups: list[list[tuple[int, int]]] = [[] for _ in route_features]
        for feature in draft.get("layers", {}).get("stops", {}).get("features", []):
            properties = feature.get("properties") or {}
            customer_index = customer_indexes.get(str(properties.get("stop_id") or ""))
            route_index = route_indexes.get(str(properties.get("route_id") or ""))
            if customer_index is None or route_index is None:
                continue
            explicit_groups[route_index].append((int(properties.get("sequence") or 0), customer_index))
        explicit = [[item for _, item in sorted(group)] for group in explicit_groups]
        flattened = [item for group in explicit for item in group]
        if all(explicit) and len(flattened) == len(customers) and len(set(flattened)) == len(customers):
            return flattened, [len(group) for group in explicit]

        geometries = [shape(feature["geometry"]) for feature in route_features]
        if any(geometry.is_empty for geometry in geometries):
            return None
        groups: list[list[tuple[float, int]]] = [[] for _ in geometries]
        for customer_index, stop in enumerate(customers, start=1):
            point = Point(stop.lng, stop.lat)
            route_index = min(range(len(geometries)), key=lambda index: geometries[index].distance(point))
            groups[route_index].append((float(geometries[route_index].project(point)), customer_index))
        if any(not group for group in groups):
            return None
        ordered_groups = [[customer_index for _, customer_index in sorted(group)] for group in groups]
        return [item for group in ordered_groups for item in group], [len(group) for group in ordered_groups]
    except Exception:
        return None


def _route_labels(draft: dict[str, Any], route_count: int) -> list[dict[str, str]]:
    features = draft.get("layers", {}).get("routes", {}).get("features", [])
    labels = []
    for index in range(route_count):
        properties = (features[index].get("properties") or {}) if index < len(features) else {}
        name = str(properties.get("name") or f"Optimize Rota {index + 1}")
        labels.append(
            {
                "name": name,
                "folder_path": str(properties.get("folder_path") or name),
                "color": str(properties.get("color") or ROUTE_COLORS[index % len(ROUTE_COLORS)]),
            }
        )
    return labels


def _baseline_routed_metrics(
    draft: dict[str, Any],
    stops: list[Stop],
    road_context: Any,
    *,
    cost_basis: str,
    fallback_speed_kmh: float,
) -> dict[str, Any]:
    """Route the current stop sequence over the same graph used by the proposal."""
    route_features = draft.get("layers", {}).get("routes", {}).get("features", [])
    stop_features = draft.get("layers", {}).get("stops", {}).get("features", [])
    if not route_features or not stop_features:
        return {"comparison_status": "not_comparable"}
    route_ids = [
        str((feature.get("properties") or {}).get("route_id") or feature.get("id") or "")
        for feature in route_features
    ]
    if not all(route_ids) or len(set(route_ids)) != len(route_ids):
        return {"comparison_status": "not_comparable"}
    index_by_stop_id = {stop.stop_id: index for index, stop in enumerate(stops)}
    customer_ids = set(index_by_stop_id) - {stops[0].stop_id}
    grouped: dict[str, list[tuple[int, int]]] = {route_id: [] for route_id in route_ids}
    seen: list[str] = []
    for feature in stop_features:
        properties = feature.get("properties") or {}
        stop_id = str(properties.get("stop_id") or feature.get("id") or "")
        if stop_id == stops[0].stop_id or stop_id not in customer_ids:
            continue
        route_id = str(properties.get("route_id") or "")
        if route_id not in grouped:
            return {"comparison_status": "not_comparable"}
        try:
            sequence = int(properties.get("sequence") or 0)
        except (TypeError, ValueError):
            return {"comparison_status": "not_comparable"}
        grouped[route_id].append((sequence, index_by_stop_id[stop_id]))
        seen.append(stop_id)
    if set(seen) != customer_ids or len(seen) != len(customer_ids) or any(
        not group for group in grouped.values()
    ):
        return {"comparison_status": "not_comparable"}
    distance_m = 0.0
    travel_time_seconds = 0.0
    for route_id in route_ids:
        order = [index for _sequence, index in sorted(grouped[route_id])]
        realized = _route_geometry(
            order,
            stops,
            road_context,
            "shortest_closed",
            cost_basis,
            fallback_speed_kmh,
        )
        distance_m += float(realized["distance_m"])
        travel_time_seconds += float(realized["road_travel_time_seconds"])
    return {
        "comparison_status": "comparable",
        "baseline_routed_distance_m": distance_m,
        "baseline_routed_travel_time_minutes": travel_time_seconds / 60,
    }


def _route_energy_figures(
    edge_records: list[Any],
    setup: EnergySetup,
    stop_count: int,
    speed_kmh: float,
    dwell_seconds: float,
) -> dict[str, Any]:
    """
    One route driven three ways: forward on the DEM, forward on the plane,
    and backward on the DEM — the same geometry each time.

    The backward figure reverses the polyline as drawn; it ignores one-way
    rules on purpose, because the question it answers is physical ("what does
    the same loop cost the other way round?"), not operational.
    """
    profile, options = setup.profile, setup.options
    dem_options = energy_model.with_surface(options, "dem")
    planar_options = energy_model.with_surface(options, "planar")
    forward_dem = energy_model.path_energy(edge_records, profile, dem_options, setup.grid)
    forward_planar = energy_model.path_energy(edge_records, profile, planar_options, None)
    reverse_dem = energy_model.path_energy(edge_records, profile, dem_options, setup.grid, reverse=True)
    run = forward_dem if options.surface == "dem" else forward_planar
    stop_wh = (
        energy_model.stop_energy_j(speed_kmh / 3.6, dwell_seconds, profile) * (stop_count + 1)
        / energy_model.J_PER_WH
    )
    fwd, rev = forward_dem.total_j, reverse_dem.total_j
    asymmetry = abs(fwd - rev) / max(abs(fwd), abs(rev)) if max(abs(fwd), abs(rev)) > 0 else 0.0
    dem = forward_dem.as_wh_dict()
    return {
        "energy_surface": options.surface,
        "energy_wh": round(run.total_j / energy_model.J_PER_WH, 2),
        "energy_dem_wh": dem["energy_wh"],
        "energy_planar_wh": round(forward_planar.total_j / energy_model.J_PER_WH, 2),
        "energy_reverse_dem_wh": round(reverse_dem.total_j / energy_model.J_PER_WH, 2),
        "energy_reverse_planar_wh": round(forward_planar.total_j / energy_model.J_PER_WH, 2),
        "stop_energy_wh": round(stop_wh, 2),
        "traction_wh": dem["traction_wh"],
        "regen_wh": dem["regen_wh"],
        "aux_wh": dem["aux_wh"],
        "friction_loss_wh": dem["friction_loss_wh"],
        "reverse_friction_loss_wh": round(reverse_dem.friction_loss_j / energy_model.J_PER_WH, 2),
        "reverse_regen_wh": round(reverse_dem.regen_j / energy_model.J_PER_WH, 2),
        "climb_m": dem["climb_m"],
        "descent_m": dem["descent_m"],
        "slope_distance_m": dem["slope_length_m"],
        "direction_asymmetry": round(asymmetry, 4),
        "planar_error_ratio": (
            round(forward_planar.total_j / forward_dem.total_j - 1.0, 4) if forward_dem.total_j else None
        ),
        "energy_n_capped": dem["n_capped"],
        "energy_n_nodata": dem["n_nodata"],
    }


def _network_energy_metrics(route_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [item for item in route_metrics if "energy_dem_wh" in item]
    if not rows:
        return {}
    total_dem = sum(item["energy_dem_wh"] for item in rows)
    total_planar = sum(item["energy_planar_wh"] for item in rows)
    total_reverse = sum(item["energy_reverse_dem_wh"] for item in rows)
    total_stop = sum(item["stop_energy_wh"] for item in rows)
    return {
        "total_energy_wh": round(sum(item["energy_wh"] for item in rows), 1),
        "total_energy_dem_wh": round(total_dem, 1),
        "total_energy_planar_wh": round(total_planar, 1),
        "total_energy_reverse_dem_wh": round(total_reverse, 1),
        "total_stop_energy_wh": round(total_stop, 1),
        "total_climb_m": round(sum(item["climb_m"] for item in rows), 1),
        "total_descent_m": round(sum(item["descent_m"] for item in rows), 1),
        "total_friction_loss_wh": round(sum(item["friction_loss_wh"] for item in rows), 1),
        "total_regen_wh": round(sum(item["regen_wh"] for item in rows), 1),
        "planar_error_ratio": round(total_planar / total_dem - 1.0, 4) if total_dem else None,
        "mean_direction_asymmetry": round(
            sum(item["direction_asymmetry"] for item in rows) / len(rows), 4
        ),
    }


def _build_proposal(
    draft: dict[str, Any],
    params: dict[str, Any],
    stops: list[Stop],
    matrix: list[list[float]],
    road_context: Any,
    routes: list[list[int]],
    *,
    algorithm: str,
    protected_groups: list[list[int]],
    fitness: float | None = None,
    labels: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    speed_kmh = max(5.0, min(float(params.get("avg_speed_kmh", 30)), 120.0))
    dwell_seconds = max(0.0, min(float(params.get("dwell_time_seconds", 30)), 600.0))
    layover_minutes = max(
        0.0, min(float(params.get("layover_minutes", DEFAULT_LAYOVER_MINUTES)), 240.0)
    )
    cost_basis = _cost_basis(params)
    # What the record already says about each stop. The optimizer decides which
    # line calls at a stop and in what order; everything else about it belongs to
    # whoever entered it. Rebuilding the feature from a fixed list of fields
    # silently dropped the rest — a neighbourhood assignment survived being
    # saved and published, and then vanished the next time a proposal was
    # applied, which is the one workflow it exists to influence.
    carried: dict[str, dict[str, Any]] = {}
    for feature in draft.get("layers", {}).get("stops", {}).get("features", []):
        properties = feature.get("properties") or {}
        stop_key = str(properties.get("stop_id") or feature.get("id") or "")
        if stop_key and stop_key not in carried:
            carried[stop_key] = {
                key: value
                for key, value in properties.items()
                if key not in OPTIMIZER_OWNED_STOP_FIELDS
            }
    exact_limit = max(2, min(int(params.get("exact_tsp_limit") or 8), 8))
    route_shape = str(params.get("route_shape") or "shortest_closed")
    if params.get("keep_stop_order"):
        # Evaluate the sequence as given — how a line is operated today, not how
        # it would be re-ordered. Membership and order are both the input.
        optimized_routes = [list(route) for route in routes]
    else:
        optimized_routes = [
            exact_tsp(route, matrix)
            if len(route) <= exact_limit
            else _local_search(route, matrix, cost_basis)
            for route in routes
        ]
    energy_setup: EnergySetup | None = None
    if road_context:
        energy_setup = road_context.get("energy_setup")
        if energy_setup is None and params.get("report_energy"):
            energy_setup = _energy_setup(params, stops)
    labels = labels or _route_labels({"layers": {"routes": {"features": []}}}, len(routes))
    proposal_id = uuid.uuid4().hex
    route_features: list[dict[str, Any]] = []
    stop_features: list[dict[str, Any]] = []
    route_metrics: list[dict[str, Any]] = []
    edge_lengths_by_route: list[dict[tuple[Any, Any], float]] = []
    total_distance = 0.0
    for route_index, order in enumerate(optimized_routes, start=1):
        route_id = f"opt-{proposal_id[:8]}-{route_index}"
        label = labels[route_index - 1] if route_index <= len(labels) else {}
        route_name = str(label.get("name") or f"Optimize Rota {route_index}")
        folder_path = str(label.get("folder_path") or route_name)
        color = str(label.get("color") or ROUTE_COLORS[(route_index - 1) % len(ROUTE_COLORS)])
        geometry_result = _route_geometry(
            order, stops, road_context, route_shape, cost_basis, speed_kmh
        )
        shortest_result = (
            geometry_result
            if route_shape == "shortest_closed"
            else _route_geometry(
                order,
                stops,
                road_context,
                "shortest_closed",
                cost_basis,
                speed_kmh,
            )
        )
        distance_m = float(geometry_result["distance_m"])
        total_distance += distance_m
        road_travel_time_minutes = float(geometry_result["road_travel_time_seconds"]) / 60
        dwell_time_minutes = len(order) * dwell_seconds / 60
        cycle_time_minutes = road_travel_time_minutes + dwell_time_minutes + layover_minutes
        solver_cost = _route_distance(order, matrix)
        if cost_basis == "travel_time":
            realized_cost = float(geometry_result["road_travel_time_seconds"])
        elif cost_basis == "energy":
            realized_cost = float(geometry_result["energy_j"] or 0.0) / energy_model.J_PER_WH
        else:
            realized_cost = distance_m
        coordinates = geometry_result["coordinates"]
        energy_figures = (
            _route_energy_figures(
                geometry_result["edge_records"], energy_setup, len(order), speed_kmh, dwell_seconds
            )
            if energy_setup is not None
            else {}
        )
        route_features.append(
            {
                "type": "Feature",
                "id": route_id,
                "properties": {
                    "name": route_name,
                    "folder_path": folder_path,
                    "route_id": route_id,
                    "color": color,
                    "distance_m": round(distance_m, 1),
                    "road_travel_time_minutes": round(road_travel_time_minutes, 2),
                    "dwell_time_minutes": round(dwell_time_minutes, 2),
                    "layover_minutes": round(layover_minutes, 2),
                    "cycle_time_minutes": round(cycle_time_minutes, 2),
                    "duration_minutes": round(cycle_time_minutes, 1),
                    "route_shape": route_shape,
                    "reused_edge_distance_m": round(geometry_result["reused_edge_distance_m"], 1),
                    "unavoidable_reuse_legs": geometry_result["unavoidable_reuse_legs"],
                    "additional_distance_m": round(
                        distance_m - float(shortest_result["distance_m"]), 1
                    ),
                    "additional_travel_time_minutes": round(
                        (
                            float(geometry_result["road_travel_time_seconds"])
                            - float(shortest_result["road_travel_time_seconds"])
                        )
                        / 60,
                        2,
                    ),
                    "stop_count": len(order) + 1,
                    "optimization_run_id": proposal_id,
                    "highway_distance_m": {
                        key: round(value, 1)
                        for key, value in sorted(geometry_result["highway_distance_m"].items())
                    },
                    "major_roads": _major_roads(geometry_result["roads"]),
                    **energy_figures,
                },
                "geometry": {"type": "LineString", "coordinates": coordinates},
            }
        )
        route_stops = [stops[0], *[stops[index] for index in order]]
        for sequence, stop in enumerate(route_stops):
            stop_features.append(
                {
                    "type": "Feature",
                    "id": f"{route_id}-{stop.stop_id}",
                    "properties": {
                        **carried.get(stop.stop_id, {}),
                        "name": stop.name,
                        "folder_path": folder_path,
                        "route_id": route_id,
                        "stop_id": stop.stop_id,
                        "sequence": sequence,
                        "demand_weight": stop.service_demand,
                        "service_demand": stop.service_demand,
                        "location_role": stop.location_role or None,
                        "optimization_role": "depot" if stop.stop_id == stops[0].stop_id else None,
                        # Present only on the ones that came from the stopping
                        # places, so a plan can be read as "these calls have no
                        # stop at them yet" rather than looking like 155 stops
                        # the town already has.
                        "candidate_stop": True if stop.candidate else None,
                    },
                    "geometry": {"type": "Point", "coordinates": [stop.lng, stop.lat]},
                }
            )
        route_metrics.append(
            {
                "route_id": route_id,
                "name": route_name,
                "distance_m": round(distance_m, 1),
                "road_travel_time_minutes": round(road_travel_time_minutes, 2),
                "dwell_time_minutes": round(dwell_time_minutes, 2),
                "layover_minutes": round(layover_minutes, 2),
                "cycle_time_minutes": round(cycle_time_minutes, 2),
                "duration_minutes": round(cycle_time_minutes, 1),
                "stop_count": len(route_stops),
                "demand_weight": round(sum(stops[index].service_demand for index in order), 2),
                "service_demand_weight": round(
                    sum(stops[index].service_demand for index in order), 2
                ),
                "reused_edge_distance_m": round(geometry_result["reused_edge_distance_m"], 1),
                "unavoidable_reuse_legs": geometry_result["unavoidable_reuse_legs"],
                "additional_distance_m": round(
                    distance_m - float(shortest_result["distance_m"]), 1
                ),
                "additional_travel_time_minutes": round(
                    (
                        float(geometry_result["road_travel_time_seconds"])
                        - float(shortest_result["road_travel_time_seconds"])
                    )
                    / 60,
                    2,
                ),
                "solver_cost": round(solver_cost, 2),
                "realized_cost": round(realized_cost, 2),
                "solver_to_realized_cost_delta": round(realized_cost - solver_cost, 2),
                "highway_distance_m": {
                    key: round(value, 1)
                    for key, value in sorted(geometry_result["highway_distance_m"].items())
                },
                "major_roads": _major_roads(geometry_result["roads"]),
                # Index ranges into the route's own LineString with the speed
                # of each piece, so the stored geometry can be re-driven on
                # another surface without re-routing it. Kept out of the layer
                # feature on purpose: a route edited in the editor would make
                # the ranges stale, and metrics are never applied to the draft.
                "edge_speeds": geometry_result["edge_speeds"],
                **energy_figures,
            }
        )
        edge_lengths_by_route.append(geometry_result["edge_lengths"])

    shared_per_route, shared_total = _shared_edge_metrics(edge_lengths_by_route)
    for feature, item, shared in zip(route_features, route_metrics, shared_per_route, strict=True):
        share = round(shared / float(item["distance_m"]) * 100, 1) if item["distance_m"] else 0.0
        item["shared_edge_distance_m"] = round(shared, 1)
        item["shared_edge_share_percent"] = share
        feature["properties"]["shared_edge_distance_m"] = round(shared, 1)
        feature["properties"]["shared_edge_share_percent"] = share

    distances = [item["distance_m"] for item in route_metrics]
    durations = [item["cycle_time_minutes"] for item in route_metrics]
    road_travel_times = [item["road_travel_time_minutes"] for item in route_metrics]
    demands = [item["demand_weight"] for item in route_metrics]
    mean_distance = sum(distances) / max(1, len(distances))
    mean_travel_time = sum(road_travel_times) / max(1, len(road_travel_times))
    mean_demand = sum(demands) / max(1, len(demands))
    baseline_geometry_distance = _baseline_distance(draft)
    baseline = _baseline_routed_metrics(
        draft,
        stops,
        road_context,
        cost_basis=cost_basis,
        fallback_speed_kmh=speed_kmh,
    )
    raw_baseline_distance = baseline.get("baseline_routed_distance_m")
    raw_baseline_travel_time = baseline.get("baseline_routed_travel_time_minutes")
    # A "comparable" baseline that is missing either figure is not comparable in practice.
    comparable = (
        baseline["comparison_status"] == "comparable"
        and raw_baseline_distance is not None
        and raw_baseline_travel_time is not None
    )
    baseline_routed_distance = float(raw_baseline_distance) if raw_baseline_distance is not None else 0.0
    baseline_routed_travel_time = (
        float(raw_baseline_travel_time) if raw_baseline_travel_time is not None else 0.0
    )
    max_distance_limit = float(params.get("max_route_distance_km") or 0) * 1000
    if max_distance_limit and any(value > max_distance_limit + 0.1 for value in distances):
        raise ValueError("Seçilen rota biçiminin alternatif yolları azami rota mesafesini aşıyor.")
    max_duration_limit = float(params.get("max_route_duration_minutes") or 0)
    if max_duration_limit and any(value > max_duration_limit + 0.1 for value in durations):
        raise ValueError("Seçilen rota biçiminin alternatif yolları azami rota süresini aşıyor.")
    metrics = {
        "total_distance_m": round(total_distance, 1),
        "proposal_routed_distance_m": round(total_distance, 1),
        "proposal_routed_travel_time_minutes": round(sum(road_travel_times), 2),
        "baseline_geometry_distance_m": round(baseline_geometry_distance, 1),
        "baseline_routed_distance_m": (
            round(float(baseline_routed_distance), 1) if comparable else None
        ),
        "baseline_routed_travel_time_minutes": (
            round(float(baseline_routed_travel_time), 2) if comparable else None
        ),
        "baseline_total_distance_m": round(
            float(baseline_routed_distance) if comparable else baseline_geometry_distance, 1
        ),
        "comparison_status": baseline["comparison_status"],
        "distance_change_m": (
            round(total_distance - float(baseline_routed_distance), 1)
            if comparable
            else None
        ),
        "distance_change_percent": (
            round(
                (total_distance - float(baseline_routed_distance))
                / float(baseline_routed_distance)
                * 100,
                1,
            )
            if comparable and baseline_routed_distance
            else None
        ),
        "travel_time_change_percent": (
            round(
                (sum(road_travel_times) - float(baseline_routed_travel_time))
                / float(baseline_routed_travel_time)
                * 100,
                1,
            )
            if comparable and baseline_routed_travel_time
            else None
        ),
        "estimated_duration_minutes": round(sum(durations), 1),
        "total_road_travel_time_minutes": round(sum(road_travel_times), 2),
        "total_cycle_time_minutes": round(sum(durations), 2),
        "route_count": len(routes),
        "served_stop_count": len({index for route in routes for index in route}),
        "candidate_stop_count": sum(1 for stop in stops if stop.candidate),
        "candidate_served_count": len(
            {index for route in routes for index in route}
            & {index for index, stop in enumerate(stops) if stop.candidate}
        ),
        "max_route_distance_m": round(max(distances, default=0), 1),
        "min_route_distance_m": round(min(distances, default=0), 1),
        "distance_imbalance_percent": round(
            (max(distances, default=0) - min(distances, default=0)) / mean_distance * 100, 1
        )
        if mean_distance
        else 0,
        "max_route_duration_minutes": round(max(durations, default=0), 1),
        "max_route_travel_time_minutes": round(max(road_travel_times, default=0), 2),
        "travel_time_imbalance_percent": round(
            (max(road_travel_times, default=0) - min(road_travel_times, default=0))
            / mean_travel_time
            * 100,
            1,
        ) if mean_travel_time else 0,
        "demand_imbalance_percent": round(
            (max(demands, default=0) - min(demands, default=0)) / mean_demand * 100, 1
        )
        if mean_demand
        else 0,
        "service_demand_imbalance_percent": round(
            (max(demands, default=0) - min(demands, default=0)) / mean_demand * 100,
            1,
        ) if mean_demand else 0,
        "single_route_neighborhood_count": len(protected_groups),
        "route_shape": route_shape,
        "reused_edge_distance_m": round(sum(item["reused_edge_distance_m"] for item in route_metrics), 1),
        "additional_ring_distance_m": round(
            sum(item["additional_distance_m"] for item in route_metrics), 1
        ),
        "additional_ring_travel_time_minutes": round(
            sum(item["additional_travel_time_minutes"] for item in route_metrics), 2
        ),
        "unavoidable_reuse_legs": sum(item["unavoidable_reuse_legs"] for item in route_metrics),
        # Road driven by two or more routes, counted once; and what share of
        # the whole network that is.
        "inter_route_shared_distance_m": round(shared_total, 1),
        "inter_route_shared_share_percent": (
            round(shared_total / total_distance * 100, 1) if total_distance else 0.0
        ),
        "highway_distance_m": {
            key: round(sum(item["highway_distance_m"].get(key, 0.0) for item in route_metrics), 1)
            for key in sorted({key for item in route_metrics for key in item["highway_distance_m"]})
        },
        "routes": route_metrics,
    }
    if energy_setup is not None:
        metrics.update(_network_energy_metrics(route_metrics))
    if fitness is not None:
        metrics["fitness"] = round(fitness, 2)
    return {
        "schema_version": 1,
        "proposal_id": proposal_id,
        "input_revision": int(draft.get("revision", 0)),
        "generated_at_utc": utc_now(),
        "algorithm": algorithm,
        "depot_id": stops[0].stop_id,
        "scenario_id": str(params.get("scenario_id") or "current-center"),
        "scenario_type": str(params.get("scenario_type") or "CURRENT_NETWORK"),
        "cost_basis": cost_basis,
        "optimization_cost_basis": f"{'shortest_path' if road_context else 'straight_line'}_{cost_basis}",
        "realized_route_shape": route_shape,
        "distance_source": road_context.get("source") if road_context else "haversine_analysis",
        "time_source": road_context.get("time_source") if road_context else "analysis_average_speed",
        "energy_source": road_context.get("energy_source") if road_context else None,
        "energy": energy_setup.as_dict() if energy_setup is not None else None,
        "road_network_revision": road_context.get("revision") if road_context else None,
        "road_network_digest": road_context.get("digest") if road_context else None,
        "routing_model_version": ROUTING_MODEL_VERSION,
        "speed_profile_version": ROAD_SPEED_PROFILE_VERSION if road_context else None,
        "routing_snapshot": {
            "kind": road_context.get("snapshot_kind") if road_context else "haversine_analysis",
            "revision": road_context.get("revision") if road_context else None,
            "digest": road_context.get("digest") if road_context else None,
            "routing_model_version": ROUTING_MODEL_VERSION,
            "speed_profile_version": ROAD_SPEED_PROFILE_VERSION if road_context else None,
            "elevation_dataset": (
                energy_setup.elevation_contract().get("elevation_dataset") if energy_setup else None
            ),
            "elevation_sha256": (
                energy_setup.elevation_contract().get("elevation_sha256") if energy_setup else None
            ),
            "energy_model_version": energy_model.ENERGY_MODEL_VERSION if energy_setup else None,
        },
        "parameters": {
            **params,
            "depot_id": stops[0].stop_id,
            "cost_basis": cost_basis,
            "layover_minutes": layover_minutes,
            "route_count": len(routes),
            "exact_tsp_limit": exact_limit,
        },
        "road_validation": {
            "max_snap_distance_m": (
                road_context.get("max_snap_distance_m") if road_context else None
            ),
            "stop_snap_diagnostics": (
                road_context.get("snap_diagnostics", []) if road_context else []
            ),
        },
        "deprecated_network_design_parameters": [
            key
            for key in ("vehicle_capacity", "demand_balance_weight")
            if key in params
        ],
        "metrics": metrics,
        "layers": {
            "routes": {"type": "FeatureCollection", "features": route_features},
            "stops": {"type": "FeatureCollection", "features": stop_features},
        },
    }


def _optimize_ga(
    draft: dict[str, Any],
    params: dict[str, Any] | None = None,
    *,
    progress: Callable[[int, int, float], None] | None = None,
) -> dict[str, Any]:
    params = params or {}
    depot, customers = _extract_stops(draft, params.get("depot_id"))
    single_route_per_mahalle = bool(params.get("single_route_per_mahalle"))
    requested_routes = int(params.get("route_count") or len(draft["layers"]["routes"]["features"]) or 5)
    route_count = max(1, min(requested_routes, len(customers)))
    population_size = max(12, min(int(params.get("population_size") or 90), 500))
    generations = max(5, min(int(params.get("generations") or 140), 2000))
    mutation_rate = max(0.0, min(float(params.get("mutation_rate", 0.08)), 1.0))
    tournament_k = max(2, min(int(params.get("tournament_k") or 5), population_size))
    exact_limit = max(2, min(int(params.get("exact_tsp_limit") or 8), 8))
    seed = int(params.get("seed", 42))
    speed_kmh = max(5.0, min(float(params.get("avg_speed_kmh", 30)), 120.0))
    dwell_seconds = max(0.0, min(float(params.get("dwell_time_seconds", 30)), 600.0))
    distance_balance_weight = max(0.0, min(float(params.get("distance_balance_weight", 38)), 100.0)) / 100
    longest_route_weight = max(0.0, min(float(params.get("longest_route_weight", 28)), 100.0)) / 100
    max_route_distance_m = max(0.0, float(params.get("max_route_distance_km") or 0)) * 1000
    max_stops_per_route = max(0, int(params.get("max_stops_per_route") or 0))
    if max_stops_per_route and max_stops_per_route * route_count < len(customers):
        raise ValueError("Azami durak sayısı tüm durakları rotalara dağıtmak için yetersiz.")
    rng = random.Random(seed)
    # A genetic algorithm over a permutation has no notion of an optional node:
    # everything in the sequence is visited. Letting the stopping places compete
    # in it was measurably wrong — twenty-three extra nodes swamped the search,
    # and the plan went from 84 km to 194, then to 269 when given more
    # generations to make it worse with.
    #
    # So they do not compete. The algorithm plans the network it must serve,
    # and the places within budget are added to the finished routes afterwards,
    # each at its cheapest position. They are extras a plan picks up on the way,
    # not forces that should be shaping it.
    offered = _candidate_stops(params, draft)
    stops = [depot, *customers, *offered]
    try:
        road_context = _road_context_for_params(stops, params)
    except PlanningValidationError as error:
        keep = _candidates_after_snap_failure(error, offered)
        if keep is None:
            raise
        offered = keep
        stops = [depot, *customers, *offered]
        road_context = _road_context_for_params(stops, params)
    matrix, distance_matrix, _travel_time_matrix = _planning_matrices(
        stops, road_context, params
    )
    candidate_nodes = [
        1 + len(customers) + offset
        for offset in range(len(offered))
        if _candidate_is_affordable(
            1 + len(customers) + offset, stops, matrix, _candidate_detour_budget(params)
        )
    ]
    protected_groups = _merged_groups(
        _fold_small_groups(
            _single_route_groups(draft, customers, protect_all=single_route_per_mahalle),
            matrix,
            max(0, int(params.get("small_mahalle_stop_limit") or 0)),
        )
    )
    existing_seed = _existing_route_seed(draft, customers, route_count)
    if existing_seed:
        base, route_sizes = existing_seed
    else:
        # Customers only. `stops` carries the candidates so the matrices and
        # the proposal can see them, but a candidate in the permutation is a
        # candidate the algorithm has to serve — which is the thing this mode
        # deliberately does not do.
        base = _angle_seed(stops[: 1 + len(customers)], depot)
        route_sizes = _route_sizes(len(base), route_count)
    population = [base[:]]
    while len(population) < population_size:
        candidate = base[:]
        swaps = max(1, len(candidate) // 8)
        for _ in range(swaps):
            left, right = rng.sample(range(len(candidate)), 2)
            candidate[left], candidate[right] = candidate[right], candidate[left]
        population.append(candidate)

    def score(individual: list[int]) -> float:
        return _fitness(
            individual,
            route_sizes,
            matrix,
            protected_groups,
            distance_constraint_matrix=distance_matrix,
            distance_balance_weight=distance_balance_weight,
            longest_route_weight=longest_route_weight,
            max_route_distance_m=max_route_distance_m,
            max_stops_per_route=max_stops_per_route,
        )

    best = min(population, key=score)[:]
    best_score = score(best)
    for generation in range(generations):
        ranked = sorted(((score(item), item) for item in population), key=lambda pair: pair[0])
        if ranked[0][0] < best_score:
            best_score, best = ranked[0][0], ranked[0][1][:]
        next_population = [ranked[0][1][:], ranked[1][1][:]]
        while len(next_population) < population_size:
            contenders_a = rng.sample(population, tournament_k)
            contenders_b = rng.sample(population, tournament_k)
            parent_a = min(contenders_a, key=score)
            parent_b = min(contenders_b, key=score)
            child = _ordered_crossover(parent_a, parent_b, rng)
            _mutate(child, rng, mutation_rate)
            next_population.append(child)
        population = next_population
        report_every = max(1, generations // 20)
        if progress and (generation in (0, generations - 1) or generation % report_every == 0):
            progress(generation + 1, generations, best_score)

    routes = _consolidate_single_route_groups(_split(best, route_sizes), protected_groups, matrix)
    validated_routes = [
        exact_tsp(route, matrix)
        if len(route) <= exact_limit
        else _local_search(route, matrix, _cost_basis(params))
        for route in routes
    ]
    if max_stops_per_route and any(len(route) > max_stops_per_route for route in validated_routes):
        raise ValueError("GA önerisi mahalle kısıtlarıyla birlikte azami durak sınırını sağlayamadı.")
    if max_route_distance_m and any(
        _route_distance(route, distance_matrix) > max_route_distance_m
        for route in validated_routes
    ):
        raise ValueError("GA önerisi azami rota mesafesi sınırını sağlayamadı.")

    resolved = {
        **params,
        "planning_mode": "ga",
        "route_count": route_count,
        "population_size": population_size,
        "generations": generations,
        "mutation_rate": mutation_rate,
        "tournament_k": tournament_k,
        "exact_tsp_limit": exact_limit,
        "seed": seed,
        "avg_speed_kmh": speed_kmh,
        "dwell_time_seconds": dwell_seconds,
        "single_route_per_mahalle": single_route_per_mahalle,
    }
    if candidate_nodes:
        validated_routes = _append_candidates_to_routes(validated_routes, candidate_nodes, matrix)
    return _build_proposal(
        draft,
        resolved,
        stops,
        matrix,
        road_context,
        validated_routes,
        algorithm="hybrid_ga_tsp_2opt",
        protected_groups=protected_groups,
        fitness=best_score,
    )


def _optimize_preserve(
    draft: dict[str, Any],
    params: dict[str, Any],
    *,
    progress: Callable[[int, int, float], None] | None = None,
) -> dict[str, Any]:
    depot, customers = _extract_stops(draft, params.get("depot_id"))
    route_count = len(draft.get("layers", {}).get("routes", {}).get("features", []))
    if route_count < 1:
        raise ValueError("Mevcut rotaları iyileştirmek için taslakta en az bir rota bulunmalı.")
    stops = [depot, *customers]
    road_context = _road_context_for_params(stops, params)
    matrix, _distance_matrix, _travel_time_matrix = _planning_matrices(
        stops, road_context, params
    )
    existing_seed = _existing_route_seed(draft, customers, route_count)
    if not existing_seed:
        raise ValueError("Mevcut rota üyelikleri güvenilir biçimde belirlenemedi; dengeli ağ modunu kullanın.")
    permutation, sizes = existing_seed
    routes = _split(permutation, sizes)
    protected_groups = _merged_groups(
        _single_route_groups(
            draft,
            customers,
            protect_all=bool(params.get("single_route_per_mahalle")),
        )
    )
    for group in protected_groups:
        if sum(bool(set(group).intersection(route)) for route in routes) > 1:
            raise ValueError(
                "Mevcut rota üyelikleri mahalle tek-rota kuralıyla çelişiyor; dengeli ağ modunu kullanın."
            )
    if progress:
        progress(1, 1, 0.0)
    return _build_proposal(
        draft,
        {**params, "planning_mode": "preserve", "route_count": route_count},
        stops,
        matrix,
        road_context,
        routes,
        algorithm="fixed_membership_tsp_2opt",
        protected_groups=protected_groups,
        labels=_route_labels(draft, route_count),
    )


#: The longest an OR-Tools search may run. The web layer caps requests at 120 s
#: on its own (`sanitize_optimization_params`); this is the ceiling for
#: experiment scripts, which ask for minutes and must get what they asked for.
MAX_SOLVER_SECONDS = 3600


def _solver_seconds(params: dict[str, Any]) -> int:
    """The wall-clock limit the routing search gets, 1 s to MAX_SOLVER_SECONDS."""
    return max(1, min(int(params.get("solver_time_limit_seconds") or 15), MAX_SOLVER_SECONDS))


def _optimize_vrp(
    draft: dict[str, Any],
    params: dict[str, Any],
    *,
    progress: Callable[[int, int, float], None] | None = None,
) -> dict[str, Any]:
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    depot, customers = _extract_stops(draft, params.get("depot_id"))
    # OR-Tools' routing search has no random seed of its own and, under a fixed
    # time limit, returns the same solution every time on this instance. What
    # does move it is the order the nodes are handed over in: the first
    # solution and the local-search trajectory both depend on it. So a
    # `node_order_seed` permutes the customers before the model is built — the
    # only sense in which "another seed" means anything for this solver. The
    # depot stays first and nothing about the problem changes.
    node_order_seed = params.get("node_order_seed")
    if node_order_seed not in (None, ""):
        random.Random(int(node_order_seed)).shuffle(customers)
    # Candidates ride along as extra nodes but are never "customers": every
    # count, ratio and validation below is about the stops the plan must serve,
    # and letting an optional node into those numbers would quietly change what
    # "at least two stops per route" means.
    candidates = _candidate_stops(params, draft)
    stops = [depot, *customers, *candidates]
    route_count = max(1, min(int(params.get("route_count") or 7), len(customers)))
    try:
        road_context = _road_context_for_params(stops, params)
    except PlanningValidationError as error:
        # A stop too far from any road is a data problem somebody has to fix,
        # and refusing to plan is the right answer for one. A *candidate* too
        # far from any road is not: it is optional by definition, and killing
        # the whole optimisation over a point the solver was free to ignore
        # would be the worst possible reading of "you may use these".
        keep = _candidates_after_snap_failure(error, candidates)
        if keep is None:
            raise
        candidates = keep
        stops = [depot, *customers, *candidates]
        road_context = _road_context_for_params(stops, params)
    matrix, distance_matrix, travel_time_matrix = _planning_matrices(
        stops, road_context, params
    )
    protected_groups = _merged_groups(
        _fold_small_groups(
            _single_route_groups(
                draft,
                customers,
                protect_all=bool(params.get("single_route_per_mahalle")),
                candidates=candidates,
            ),
            matrix,
            max(0, int(params.get("small_mahalle_stop_limit") or 0)),
        )
    )
    min_stops = max(0, int(params.get("min_stops_per_route", 1)))
    max_stops = max(0, int(params.get("max_stops_per_route") or 0))
    if min_stops * route_count > len(customers):
        raise ValueError("Asgari durak sayısı × rota sayısı toplam durak sayısını aşıyor.")
    if max_stops and max_stops * route_count < len(customers):
        raise ValueError("Azami durak sayısı tüm durakları rotalara dağıtmak için yetersiz.")
    if max_stops and min_stops > max_stops:
        raise ValueError("Asgari durak sayısı azami durak sayısından büyük olamaz.")

    manager = pywrapcp.RoutingIndexManager(len(stops), route_count, 0)
    routing = pywrapcp.RoutingModel(manager)
    # An energy matrix can be negative (downhill from the depot). Every route
    # visits every mandatory node once, so adding one constant to every arc
    # adds the same constant to every solution and changes nothing but the
    # sign the solver sees.
    lowest = min((value for row in matrix for value in row), default=0.0)
    cost_offset = -lowest if lowest < 0 else 0.0
    cost_values = [[int(round(value + cost_offset)) for value in row] for row in matrix]
    distance_values = [[int(round(value)) for value in row] for row in distance_matrix]
    travel_time_values = [[int(round(value)) for value in row] for row in travel_time_matrix]

    def cost_callback(from_index: int, to_index: int) -> int:
        return cost_values[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    cost_callback_index = routing.RegisterTransitCallback(cost_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(cost_callback_index)

    # What makes a candidate optional. Every other node is mandatory because the
    # model has no disjunction for it; a candidate gets one, so the solver may
    # drop it and pay `penalty` instead. It therefore calls at a candidate
    # exactly when the detour costs less than that — which is the planner's
    # "how far is a call at an unofficial stopping place worth going?", asked
    # once in metres and converted into whatever unit the cost basis uses.
    if candidates:
        penalty = _candidate_penalty(params)
        budget = _candidate_detour_budget(params)
        first_candidate = 1 + len(customers)
        for offset in range(len(candidates)):
            node = first_candidate + offset
            # A place beyond the budget is still offered, at no price: the
            # solver takes it if it happens to be free and leaves it otherwise.
            # Removing it instead would mean rebuilding the matrix, and "free"
            # is a real answer that deserves to stay reachable.
            node_penalty = penalty if _candidate_is_affordable(node, stops, matrix, budget) else 0
            # A zero penalty means "never worth a detour", which is a coherent
            # answer and the one that leaves today's plan untouched. Adding the
            # disjunction anyway keeps the node droppable rather than making it
            # mandatory, which is what omitting it would do.
            routing.AddDisjunction([manager.NodeToIndex(node)], node_penalty)

    def distance_callback(from_index: int, to_index: int) -> int:
        return distance_values[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    distance_callback_index = routing.RegisterTransitCallback(distance_callback)
    total_distance_bound = max(1, sum(max(row) for row in distance_values))
    max_distance_m = int(round(float(params.get("max_route_distance_km") or 0) * 1000))
    routing.AddDimension(
        distance_callback_index,
        0,
        max_distance_m or total_distance_bound,
        True,
        "Distance",
    )
    distance_dimension = routing.GetDimensionOrDie("Distance")
    distance_dimension.SetGlobalSpanCostCoefficient(int(params.get("distance_balance_weight", 20)))

    dwell_seconds = max(0.0, float(params.get("dwell_time_seconds", 30)))

    def time_callback(from_index: int, to_index: int) -> int:
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        travel = travel_time_values[from_node][to_node]
        return int(round(travel + (dwell_seconds if from_node else 0)))

    time_callback_index = routing.RegisterTransitCallback(time_callback)
    max_duration_seconds = int(round(float(params.get("max_route_duration_minutes") or 0) * 60))
    if max_duration_seconds:
        routing.AddDimension(time_callback_index, 0, max_duration_seconds, True, "Time")

    def stop_count_callback(index: int) -> int:
        return 0 if manager.IndexToNode(index) == 0 else 1

    stop_count_index = routing.RegisterUnaryTransitCallback(stop_count_callback)
    # Without an explicit cap the bound has to admit the candidates too, or one
    # vehicle taking several of them hits a limit that was never asked for.
    stop_capacity = max_stops or (len(customers) + len(candidates))
    routing.AddDimensionWithVehicleCapacity(
        stop_count_index,
        0,
        [stop_capacity] * route_count,
        True,
        "Stops",
    )
    stop_dimension = routing.GetDimensionOrDie("Stops")
    for vehicle in range(route_count):
        stop_dimension.CumulVar(routing.End(vehicle)).SetMin(min_stops)

    solver = routing.solver()
    for group in protected_groups:
        indexes = [manager.NodeToIndex(node) for node in group]
        for other in indexes[1:]:
            solver.Add(routing.VehicleVar(indexes[0]) == routing.VehicleVar(other))

    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    solver_seconds = _solver_seconds(params)
    if candidates:
        # Dropping a node is a local-search move, and the first solution comes
        # from PATH_CHEAPEST_ARC, which starts by inserting everything. Below a
        # certain budget the solver simply never gets to the move, and the
        # option fails in the worst way available: silently, by serving every
        # candidate whatever tolerance was asked for.
        #
        # Measured on this network — 121 stops, 28 candidates, zero tolerance,
        # where the right answer is the 13 that cost nothing: 5 s and 12 s both
        # returned all 28, and 15 s and above returned 13. The floor is set
        # above the observed knee rather than on it, because the knee moves with
        # the size of the problem and being slow is the recoverable failure.
        solver_seconds = max(solver_seconds, CANDIDATE_SOLVER_FLOOR_SECONDS)
    search.time_limit.FromSeconds(solver_seconds)
    search.log_search = False
    if progress:
        progress(0, 1, 0.0)
    seed_routes = _vrp_seed_routes(
        len(customers),
        route_count,
        protected_groups,
        matrix,
        min_stops=min_stops,
        max_stops=max_stops,
    )
    # A warm start: routes given as lists of stop ids, typically a solution
    # found under a constraint that is now being relaxed. Local search then
    # begins from a known-good network instead of from a cold construction,
    # so what it changes is what the relaxation made worth changing. Stops it
    # does not name are appended to the shortest route so the start is complete.
    warm = params.get("warm_start_stop_ids")
    if warm:
        index_of = {stop.stop_id: index for index, stop in enumerate(stops)}
        placed: set[int] = set()
        seed_routes = []
        for route in list(warm)[:route_count]:
            nodes = [index_of[str(stop_id)] for stop_id in route if str(stop_id) in index_of and index_of[str(stop_id)] != 0]
            nodes = [node for node in nodes if node not in placed]
            placed.update(nodes)
            seed_routes.append(nodes)
        while len(seed_routes) < route_count:
            seed_routes.append([])
        for node in range(1, len(customers) + 1):
            if node not in placed:
                min(seed_routes, key=len).append(node)
    if seed_routes and candidates:
        # The seed exists to satisfy the same-vehicle groups, and it is built
        # from customers alone — so it starts the search with every candidate
        # inactive. From there, taking one is a move the search has to find,
        # and in the time available it finds almost none: with the neighbourhood
        # rule on, this network went from thirteen candidates to four, and the
        # ones it kept were not the cheapest, just the reachable-first.
        #
        # Seeding them in instead starts where the unseeded search starts —
        # everything offered — and lets local search drop what it does not want,
        # which is the direction it is good at.
        seed_routes = _seed_with_candidates(
            seed_routes, len(customers), len(candidates), max_stops, matrix
        )
    initial_assignment = routing.ReadAssignmentFromRoutes(seed_routes, True) if seed_routes else None
    solution = (
        routing.SolveFromAssignmentWithParameters(initial_assignment, search)
        if initial_assignment is not None
        else routing.SolveWithParameters(search)
    )
    if solution is None:
        raise ValueError("Verilen rota, süre, kapasite ve mahalle kısıtlarıyla uygulanabilir çözüm bulunamadı.")

    routes: list[list[int]] = []
    for vehicle in range(route_count):
        order: list[int] = []
        index = routing.Start(vehicle)
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node:
                order.append(node)
            index = solution.Value(routing.NextVar(index))
        routes.append(order)
    if progress:
        progress(1, 1, float(solution.ObjectiveValue()))
    return _build_proposal(
        draft,
        {**params, "planning_mode": "vrp", "route_count": route_count},
        stops,
        matrix,
        road_context,
        routes,
        algorithm="ortools_vrp_guided_local_search",
        protected_groups=protected_groups,
        fitness=float(solution.ObjectiveValue()),
    )


def optimize(
    draft: dict[str, Any],
    params: dict[str, Any] | None = None,
    *,
    progress: Callable[[int, int, float], None] | None = None,
) -> dict[str, Any]:
    """Dispatch route planning to fixed-membership TSP, OR-Tools VRP, or GA."""
    resolved = params or {}
    mode = str(resolved.get("planning_mode") or "vrp").lower()
    if mode == "preserve":
        return _optimize_preserve(draft, resolved, progress=progress)
    if mode == "vrp":
        return _optimize_vrp(draft, resolved, progress=progress)
    if mode == "ga":
        return _optimize_ga(draft, resolved, progress=progress)
    raise ValueError("Planlama modu preserve, vrp veya ga olmalı.")
