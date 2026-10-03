"""Optimizasyon senaryolarını çalıştırır, karşılaştırır ve birini seçer.

Bütün koşular aynı taslak ve aynı yol ağı revizyonu üzerinde yapılır; aksi
hâlde karşılaştırma başka ağların sayılarını yan yana koymak olur. Yöntem
mevcut `urgup_transport.optimizer` (OR-Tools VRP, yönlendirilmiş yol grafiği,
mahalle tek-hat kuralı); burada yalnızca parametre kümeleri ve seçim kuralı
vardır.
"""

from __future__ import annotations

import copy
import logging
import time
from pathlib import Path
from typing import Any

from shapely.geometry import shape

from ..config import PROJECT_ROOT
from ..municipal_data import read_kml
from ..optimizer import optimize
from . import common

log = logging.getLogger(__name__)

TERMINAL_KML = PROJECT_ROOT / "veriler/Terminal Mimari Proje-Ulaşıım Planı/yeniterminal.kml"

#: Bütün senaryoların paylaştığı çekirdek. Seyahat süresi maliyeti, yol sınıfı
#: hız varsayımları üzerinden yol hiyerarşisini tercih ettirir; mesafe tabanlı
#: koşu duyarlılık için ayrıca yapılır.
BASE_PARAMS: dict[str, Any] = {
    "planning_mode": "vrp",
    "cost_basis": "travel_time",
    "single_route_per_mahalle": True,
    "small_mahalle_stop_limit": 8,
    "route_shape": "shortest_closed",
    "solver_time_limit_seconds": 45,
    "distance_balance_weight": 20,
    "dwell_time_seconds": 30,
    "avg_speed_kmh": 30,
    "min_stops_per_route": 2,
    "exact_tsp_limit": 8,
    "seed": 42,
    "scenario_type": "CURRENT_NETWORK",
}

ROUTE_COUNT_CANDIDATES = (5, 6, 7, 8)

#: Mahalle tek-hat kuralı her mahalleyi bir hatta bağlar; az duraklı mahalleler
#: en yakın büyük mahalleye katlanır (`small_mahalle_stop_limit`). Katlama
#: sonrası grup sayısı rota sayısından az kalırsa çözücü başlayamaz, o yüzden
#: eşik rota sayısıyla birlikte düşer: 5–6 hat için 8'in altındaki mahalleler
#: (Duayeri 5, Cumhuriyet 4 + depo, Temenni 1) katlanır; 7 hat için Cumhuriyet ve
#: Temenni; 8 hat için yalnız Temenni.
FOLD_LIMIT_BY_ROUTE_COUNT = {5: 8, 6: 8, 7: 5, 8: 4}

#: Kent içi bir hattın kabul edilebilir en uzun çevrim süresi (dk). Belediyenin
#: sefer tablosundaki kent içi turajlar 22–38 dk; 40 dk bu aralığın yuvarlanmış
#: tavanıdır. Kırsal Aksalur hattı bu kuraldan muaf tutulur: uzunluğu rota
#: sayısından bağımsızdır ve bugün de 4 seferlik ayrı bir hattır.
MAX_CYCLE_MINUTES = 40.0
#: Bir hattın durak sayısı tavanı. İşletilen hatlarda 15–27 resmî durak var;
#: 35, en kalabalık hattın çok üstüne çıkmayan bir tavandır. Daha kalabalık bir
#: hat iki mahalleyi tek araca yığar ve yolcu için seferi uzatır.
MAX_STOPS_PER_ROUTE = 35


def _terminal_point() -> tuple[float, float]:
    feature = read_kml(TERMINAL_KML)[0]
    centroid = shape(feature.geometry).centroid
    return float(centroid.x), float(centroid.y)


def _with_terminal(draft: dict[str, Any], role: str) -> dict[str, Any]:
    """Taslağın kopyasına yeni terminali bir durak olarak ekle.

    `role="depot"`: bütün rotalar terminalden başlar ve orada biter; Merkez
    Durak sıradan bir durak olur. `role=""`: terminal zorunlu bir duraktır, hangi
    hattın uğrayacağına çözücü karar verir.
    """
    copied = copy.deepcopy(draft)
    lng, lat = _terminal_point()
    copied["layers"]["stops"]["features"].append(
        {
            "type": "Feature",
            "id": "yeni-terminal",
            "properties": {
                "name": "Yeni Terminal",
                "stop_id": "yeni-terminal",
                "location_role": role or None,
                "mahalle_override": "Kavaklıönü",
            },
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
        }
    )
    return copied


def scenario_definitions(selected_route_count: int | None = None) -> list[dict[str, Any]]:
    """Koşulacak senaryolar. Rota sayısı duyarlılığı önce, türevler seçilen sayı için."""
    rows: list[dict[str, Any]] = []
    for count in ROUTE_COUNT_CANDIDATES:
        rows.append(
            {
                "id": f"S-R{count}",
                "group": "rota_sayisi",
                "title": f"{count} hat, seyahat süresi, mevcut merkez",
                "params": {
                    **BASE_PARAMS,
                    "route_count": count,
                    "small_mahalle_stop_limit": FOLD_LIMIT_BY_ROUTE_COUNT[count],
                },
                "terminal": None,
            }
        )
    if selected_route_count is None:
        return rows
    count = selected_route_count
    base = {**BASE_PARAMS, "small_mahalle_stop_limit": FOLD_LIMIT_BY_ROUTE_COUNT[count]}
    rows.extend(
        [
            {
                "id": f"S-R{count}-mesafe",
                "group": "maliyet",
                "title": f"{count} hat, mesafe maliyeti",
                "params": {**base, "route_count": count, "cost_basis": "distance"},
                "terminal": None,
            },
            {
                "id": f"S-R{count}-ring",
                "group": "bicim",
                "title": f"{count} hat, ring tercihli (yol tekrarı cezalı)",
                "params": {**base, "route_count": count, "route_shape": "prefer_ring"},
                "terminal": None,
            },
            {
                "id": f"S-R{count}-terminal-durak",
                "group": "terminal",
                "title": f"{count} hat, yeni terminal zorunlu durak",
                "params": {**base, "route_count": count, "scenario_type": "TERMINAL_CONNECTION"},
                "terminal": "stop",
            },
            {
                "id": f"S-R{count}-terminal-depo",
                "group": "terminal",
                "title": f"{count} hat, yeni terminal başlangıç/bitiş",
                "params": {**base, "route_count": count, "scenario_type": "TERMINAL_RADIAL"},
                "terminal": "depot",
            },
        ]
    )
    return rows


def _summarize(definition: dict[str, Any], proposal: dict[str, Any], seconds: float) -> dict[str, Any]:
    metrics = proposal["metrics"]
    routes = metrics["routes"]
    cycles = [row["cycle_time_minutes"] for row in routes]
    highway = metrics.get("highway_distance_m", {})
    total = metrics["total_distance_m"] or 1.0
    arterial = sum(highway.get(key, 0.0) for key in ("trunk", "trunk_link", "primary", "secondary", "secondary_link"))
    return {
        "id": definition["id"],
        "group": definition["group"],
        "title": definition["title"],
        "proposal_id": proposal["proposal_id"],
        "route_count": metrics["route_count"],
        "cost_basis": proposal["cost_basis"],
        "route_shape": proposal["realized_route_shape"],
        "terminal": definition.get("terminal"),
        "total_distance_km": round(total / 1000, 2),
        "total_road_time_min": metrics["total_road_travel_time_minutes"],
        "total_cycle_min": metrics["total_cycle_time_minutes"],
        "max_route_km": round(metrics["max_route_distance_m"] / 1000, 2),
        "min_route_km": round(metrics["min_route_distance_m"] / 1000, 2),
        "max_cycle_min": max(cycles),
        "distance_imbalance_percent": metrics["distance_imbalance_percent"],
        "reused_km": round(metrics["reused_edge_distance_m"] / 1000, 2),
        "shared_km": round(metrics["inter_route_shared_distance_m"] / 1000, 2),
        "shared_percent": metrics["inter_route_shared_share_percent"],
        "arterial_percent": round(arterial / total * 100, 1),
        "served_stops": metrics["served_stop_count"],
        "baseline_routed_km": (
            round(metrics["baseline_routed_distance_m"] / 1000, 2)
            if metrics.get("baseline_routed_distance_m") is not None
            else None
        ),
        "distance_change_percent": metrics.get("distance_change_percent"),
        "travel_time_change_percent": metrics.get("travel_time_change_percent"),
        "solver_seconds": round(seconds, 1),
        "algorithm": proposal["algorithm"],
        "road_network_revision": proposal.get("road_network_revision"),
        "input_revision": proposal.get("input_revision"),
    }


def run_scenario(definition: dict[str, Any], draft: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    source = draft
    if definition.get("terminal") == "depot":
        source = _with_terminal(draft, "depot")
    elif definition.get("terminal") == "stop":
        source = _with_terminal(draft, "")
    started = time.time()
    proposal = optimize(source, dict(definition["params"]))
    seconds = time.time() - started
    proposal["scenario"] = {key: value for key, value in definition.items() if key != "params"}
    summary = _summarize(definition, proposal, seconds)
    return proposal, summary


def select_route_count(summaries: list[dict[str, Any]]) -> tuple[int, str]:
    """Rota sayısını seç ve gerekçesini yaz.

    Kural: kırsal Aksalur hattı dışında hiçbir rotanın çevrim süresi
    MAX_CYCLE_MINUTES'i, hiçbir rotanın durak sayısı MAX_STOPS_PER_ROUTE'u
    aşmasın; bu iki koşulu sağlayanlar arasında toplam araç-kilometresi en
    düşük olan seçilir. Toplam km her ek hatla artar (her hat merkeze gidip
    gelir); süre ve durak tavanı ise hattın yolcu ve işletmeci için
    kabul edilebilirliğidir.
    """
    candidates = [row for row in summaries if row["group"] == "rota_sayisi" and not row.get("error")]
    feasible = [
        row
        for row in candidates
        if max(row["_cycles_without_rural"], default=0) <= MAX_CYCLE_MINUTES
        and row["_max_stops"] <= MAX_STOPS_PER_ROUTE
    ]
    pool = feasible or candidates
    chosen = min(pool, key=lambda row: (row["total_distance_km"], row["route_count"]))
    rejected = [
        f"{row['route_count']} hat: en uzun kent içi çevrim {max(row['_cycles_without_rural']):.1f} dk, "
        f"en kalabalık hat {row['_max_stops']} durak"
        for row in candidates
        if row not in feasible
    ]
    reason = (
        f"{chosen['route_count']} hat seçildi: kent içi çevrim süresi ≤ {MAX_CYCLE_MINUTES:g} dk ve hat başına "
        f"≤ {MAX_STOPS_PER_ROUTE} durak koşullarını sağlayan seçenekler arasında toplam araç-km en düşük "
        f"({chosen['total_distance_km']} km). Elenenler — " + ("; ".join(rejected) if rejected else "yok") + "."
    )
    if not feasible:
        reason = (
            f"Hiçbir rota sayısı {MAX_CYCLE_MINUTES:g} dk / {MAX_STOPS_PER_ROUTE} durak koşullarını sağlamadı; "
            f"toplam araç-km en düşük olan {chosen['route_count']} hat seçildi."
        )
    return int(chosen["route_count"]), reason


def _rural_free_cycles(proposal: dict[str, Any], draft: dict[str, Any]) -> list[float]:
    named = common.name_routes(proposal, draft)
    return [
        row["cycle_time_minutes"] for row in named["metrics"]["routes"] if "aksalur" not in common._fold(row["name"])
    ]


def run_all(draft: dict[str, Any] | None = None, *, output_dir: Path | None = None) -> dict[str, Any]:
    """Bütün senaryoları koş, özetle, seçilen öneriyi adlandırıp kaydet."""
    common.ensure_dirs()
    draft = draft or common.read_draft()
    output_dir = output_dir or common.SUBDIRS["senaryolar"]
    output_dir.mkdir(parents=True, exist_ok=True)
    # Önceki koşudan kalan senaryo dosyaları bu koşunun özetinde yer almaz;
    # kalırsa hangi ağa ait oldukları belirsizleşir.
    for stale in output_dir.glob("*.json"):
        stale.unlink()
    summaries: list[dict[str, Any]] = []
    proposals: dict[str, dict[str, Any]] = {}

    def run(definitions: list[dict[str, Any]]) -> None:
        for definition in definitions:
            log.info("Senaryo %s başlıyor: %s", definition["id"], definition["title"])
            try:
                proposal, summary = run_scenario(definition, draft)
            except (ValueError, Exception) as exc:  # bir senaryonun çözümsüz kalması ötekileri durdurmasın
                log.error("Senaryo %s çözülemedi: %s", definition["id"], exc)
                summaries.append(
                    {
                        "id": definition["id"],
                        "group": definition["group"],
                        "title": definition["title"],
                        "error": str(exc),
                        "_cycles_without_rural": [float("inf")],
                    }
                )
                continue
            summary["_cycles_without_rural"] = _rural_free_cycles(proposal, draft)
            summary["_max_stops"] = max(row["stop_count"] - 1 for row in proposal["metrics"]["routes"])
            summaries.append(summary)
            proposals[definition["id"]] = proposal
            common.write_json(output_dir / f"{definition['id']}.json", proposal)
            log.info(
                "Senaryo %s: %s km, %s dk çevrim, %s sn",
                definition["id"],
                summary["total_distance_km"],
                summary["total_cycle_min"],
                summary["solver_seconds"],
            )

    run(scenario_definitions())
    route_count, reason = select_route_count(summaries)
    run(scenario_definitions(route_count)[len(ROUTE_COUNT_CANDIDATES) :])

    selected_id = f"S-R{route_count}"
    selected = common.name_routes(proposals[selected_id], draft)
    selected["selection"] = {
        "scenario_id": selected_id,
        "route_count": route_count,
        "reason": reason,
        "max_cycle_minutes_rule": MAX_CYCLE_MINUTES,
        "max_stops_per_route_rule": MAX_STOPS_PER_ROUTE,
    }
    common.write_json(common.SELECTED_PROPOSAL, selected)
    for row in summaries:
        row["max_in_town_cycle_min"] = round(max(row.pop("_cycles_without_rural", [0])), 2)
        row["max_stops_on_a_route"] = row.pop("_max_stops", None)
    payload = {
        "generated_at_utc": common_now(),
        "draft_revision": draft.get("revision"),
        "road_network_revision": selected.get("road_network_revision"),
        "road_network_digest": selected.get("road_network_digest"),
        "selected_scenario": selected_id,
        "selected_route_count": route_count,
        "selection_reason": reason,
        "base_params": BASE_PARAMS,
        "scenarios": summaries,
    }
    common.write_json(common.SCENARIO_SUMMARY, payload)
    return payload


def common_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
