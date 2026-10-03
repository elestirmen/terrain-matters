"""Paylaşılan yollar, dönüşümler ve rota adlandırma kuralı."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

from pyproj import Transformer
from shapely.geometry import Point, shape

from ..config import PROJECT_ROOT

OUTPUT_ROOT = PROJECT_ROOT / "sonuç_raporu"
VERILER = PROJECT_ROOT / "veriler"

SUBDIRS = {
    "kml": OUTPUT_ROOT / "kml",
    "kmz": OUTPUT_ROOT / "kmz",
    "haritalar": OUTPUT_ROOT / "haritalar",
    "rota_gorselleri": OUTPUT_ROOT / "rota_gorselleri",
    "tablolar": OUTPUT_ROOT / "tablolar",
    "veri_kalite": OUTPUT_ROOT / "veri_kalite_kontrolu",
    "teknik": OUTPUT_ROOT / "teknik_ciktilar",
    "senaryolar": OUTPUT_ROOT / "teknik_ciktilar" / "senaryolar",
}

#: Seçilen öneri: adlandırma uygulanmış, teslim edilen ağ.
SELECTED_PROPOSAL = SUBDIRS["teknik"] / "secilen_oneri.json"
#: Senaryo koşularının özeti.
SCENARIO_SUMMARY = SUBDIRS["teknik"] / "senaryo_ozeti.json"

EARTH_RADIUS_M = 6_371_008.8
UTM_EPSG = 32636
WEB_MERCATOR_EPSG = 3857

_to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{UTM_EPSG}", always_xy=True)
_to_wgs = Transformer.from_crs(f"EPSG:{UTM_EPSG}", "EPSG:4326", always_xy=True)
_to_merc = Transformer.from_crs("EPSG:4326", f"EPSG:{WEB_MERCATOR_EPSG}", always_xy=True)


def ensure_dirs() -> None:
    for path in SUBDIRS.values():
        path.mkdir(parents=True, exist_ok=True)


def to_utm(lng: float, lat: float) -> tuple[float, float]:
    return _to_utm.transform(lng, lat)


def to_wgs(x: float, y: float) -> tuple[float, float]:
    return _to_wgs.transform(x, y)


def to_mercator(lng: float, lat: float) -> tuple[float, float]:
    return _to_merc.transform(lng, lat)


def haversine_m(a: Sequence[float], b: Sequence[float]) -> float:
    lon1, lat1 = map(math.radians, a[:2])
    lon2, lat2 = map(math.radians, b[:2])
    value = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def line_length_m(coordinates: list[list[float]]) -> float:
    return sum(haversine_m(a, b) for a, b in pairwise(coordinates))


def geometry_length_m(geometry: dict[str, Any]) -> float:
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "LineString":
        return line_length_m(coords)
    if kind == "MultiLineString":
        return sum(line_length_m(part) for part in coords)
    return 0.0


def utm_geometry(geometry: dict[str, Any]):
    """Shapely geometry in metres (UTM 36N)."""
    from shapely.ops import transform

    return transform(_to_utm.transform, shape(geometry))


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def read_draft() -> dict[str, Any]:
    """Sistem kaydındaki taslak (veritabanı tanımlıysa oradan)."""
    from .. import stores

    return stores.build_store().read()


def read_road_network() -> dict[str, Any]:
    from .. import stores

    payload = stores.editable_road_network_payload()
    if payload is None:
        raise RuntimeError("Düzenlenebilir yol ağı bulunamadı; optimizasyon yol ağı olmadan çalıştırılamaz.")
    return payload


def safe_slug(value: str) -> str:
    table = str.maketrans("çğıöşüÇĞİÖŞÜ ", "cgiosuCGIOSU_")
    text = str(value).translate(table)
    text = re.sub(r"[^A-Za-z0-9_\-]+", "_", text).strip("_")
    return re.sub(r"_+", "_", text)


# ── rota adlandırma ──────────────────────────────────────────────────────
#: Belediyenin araçlardaki ışıklı hat numarası sırası (HAREKET SAATleri.xls,
#: "HAT NO" sayfası): 1 Kavaklıönü, 2 Fatih, 3 Bahçelievler, 4 EVKA, 5 TOKİ.
#: Geri kalan mahalleler o listede yok; sıraya sonradan eklenir.
MUNICIPAL_LINE_ORDER = ["Kavaklıönü", "Fatih", "Bahçelievler", "EVKA", "15 Temmuz", "Aksalur"]


def _fold(name: str) -> str:
    table = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    return re.sub(r"[^a-z0-9]", "", str(name or "").translate(table).lower())


def _order_key(mahalle: str) -> int:
    folded = _fold(mahalle)
    for index, candidate in enumerate(MUNICIPAL_LINE_ORDER):
        if _fold(candidate) in folded or folded in _fold(candidate):
            return index
    return len(MUNICIPAL_LINE_ORDER)


def stop_neighbourhoods(draft: dict[str, Any]) -> dict[str, str]:
    """Her fiziksel durağın sayıldığı mahalle: atama varsa o, yoksa sınır testi."""
    polygons = [
        (str(feature["properties"].get("name") or ""), shape(feature["geometry"]))
        for feature in draft["layers"]["mahalle"]["features"]
        if feature.get("geometry")
    ]
    result: dict[str, str] = {}
    for feature in draft["layers"]["stops"]["features"]:
        properties = feature.get("properties") or {}
        stop_id = str(properties.get("stop_id") or feature.get("id") or "")
        if stop_id in result:
            continue
        override = str(properties.get("mahalle_override") or "").strip()
        if override:
            result[stop_id] = override
            continue
        point = Point(feature["geometry"]["coordinates"][:2])
        hit = next((name for name, polygon in polygons if polygon.covers(point)), "")
        result[stop_id] = hit
    return result


def name_routes(proposal: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """Öneri rotalarına belediye sırasına göre R01… numarası ve mahalle adı ver.

    Ad, rotanın en çok durağa uğradığı mahalledir; ikinci mahalle durakların en
    az %30'unu taşıyorsa ada eklenir. Aynı ada düşen iki rota varsa -1/-2 eki
    alır. Değişiklik kopya üzerinde yapılır; özgün optimizer adları
    `optimizer_name` alanında saklanır.
    """
    import copy

    result = copy.deepcopy(proposal)
    neighbourhood_of = stop_neighbourhoods(draft)
    depot_id = str(result.get("depot_id") or "")
    stops_by_route: dict[str, list[dict[str, Any]]] = {}
    for feature in result["layers"]["stops"]["features"]:
        stops_by_route.setdefault(str(feature["properties"].get("route_id")), []).append(feature)

    descriptors = []
    for feature in result["layers"]["routes"]["features"]:
        route_id = str(feature["properties"].get("route_id") or feature.get("id"))
        counts: dict[str, int] = {}
        for stop in stops_by_route.get(route_id, []):
            stop_id = str(stop["properties"].get("stop_id") or "")
            if stop_id == depot_id:
                continue
            mahalle = neighbourhood_of.get(stop_id) or stop["properties"].get("mahalle_name") or "Mahalle dışı"
            counts[mahalle] = counts.get(mahalle, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], _order_key(item[0])))
        total = sum(counts.values()) or 1
        primary = ranked[0][0] if ranked else "Rota"
        parts = [primary]
        if len(ranked) > 1 and ranked[1][1] / total >= 0.30:
            parts.append(ranked[1][0])
        descriptors.append(
            {"feature": feature, "route_id": route_id, "parts": parts, "primary": primary, "counts": ranked}
        )

    descriptors.sort(key=lambda item: (_order_key(item["primary"]), -(item["counts"][0][1] if item["counts"] else 0)))
    seen: dict[str, int] = {}
    for item in descriptors:
        base = " – ".join(item["parts"])
        seen[base] = seen.get(base, 0) + 1
        item["base"] = base
    duplicates = {base for base, count in seen.items() if count > 1}
    counters: dict[str, int] = {}
    metrics_by_id = {row["route_id"]: row for row in result.get("metrics", {}).get("routes", [])}
    for number, item in enumerate(descriptors, start=1):
        base = item["base"]
        if base in duplicates:
            counters[base] = counters.get(base, 0) + 1
            base = f"{base}-{counters[base]}"
        code = f"R{number:02d}"
        name = f"{code} {base} Hattı"
        properties = item["feature"]["properties"]
        properties["optimizer_name"] = properties.get("name")
        properties["name"] = name
        properties["folder_path"] = name
        properties["route_code"] = code
        properties["route_number"] = number
        properties["primary_mahalle"] = item["primary"]
        properties["mahalle_stop_counts"] = dict(item["counts"])
        for stop in stops_by_route.get(item["route_id"], []):
            stop["properties"]["folder_path"] = name
            stop["properties"]["route_code"] = code
        if item["route_id"] in metrics_by_id:
            metrics_by_id[item["route_id"]]["optimizer_name"] = metrics_by_id[item["route_id"]].get("name")
            metrics_by_id[item["route_id"]]["name"] = name
            metrics_by_id[item["route_id"]]["route_code"] = code
            metrics_by_id[item["route_id"]]["route_number"] = number
    # Rota ve metrik sırası numara sırası olsun.
    order = {item["route_id"]: index for index, item in enumerate(descriptors)}
    result["layers"]["routes"]["features"].sort(key=lambda f: order[str(f["properties"]["route_id"])])
    if "routes" in result.get("metrics", {}):
        result["metrics"]["routes"].sort(key=lambda row: order.get(row["route_id"], 999))
    return result


def routes_with_stops(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    """Numara sırasıyla rota + sıralı durak listesi."""
    stops_by_route: dict[str, list[dict[str, Any]]] = {}
    for feature in proposal["layers"]["stops"]["features"]:
        stops_by_route.setdefault(str(feature["properties"].get("route_id")), []).append(feature)
    rows = []
    for feature in proposal["layers"]["routes"]["features"]:
        route_id = str(feature["properties"].get("route_id") or feature.get("id"))
        stops = sorted(stops_by_route.get(route_id, []), key=lambda f: int(f["properties"].get("sequence") or 0))
        rows.append({"route": feature, "stops": stops, "route_id": route_id})
    rows.sort(key=lambda row: int(row["route"]["properties"].get("route_number") or 0))
    return rows
