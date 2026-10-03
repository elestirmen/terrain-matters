#!/usr/bin/env python3
"""
Run the Terrain Matters experiments (manuscript Section 5) end to end.

    # draft and roads: PostgreSQL if DATABASE_URL is set, else data/editable/*.json
    .venv/bin/python scripts/run_terrain_experiments.py --run-id 2026-09-29-a --experiments A,B,C,D,E

Every optimisation is one *cell*: a cost basis, a surface, a regeneration
efficiency, a mass scenario, a terrain scale and a DEM noise seed. Every cell's
solution is then *evaluated* — the geometry it produced is re-driven, without
re-routing, under every evaluation scenario the experiment asks for, on the
real terrain and on a plane, forwards and backwards. Optimising and evaluating
are kept apart on purpose: "the same routes on two surfaces" is a statement
about geometry, and it must not depend on the solver being run twice.

Outputs, under ``data/processed/experiments/<run_id>/``:

- ``manifest.json``        run id, git commit, DEM version, vehicle profile, parameters, timings
- ``proposals/<cell>.json`` the full optimiser proposal of each cell
- ``routes.geojson``       every cell's line geometry, tagged by cell
- ``route_metrics.csv``    one row per (cell, evaluation scenario, route, direction)
- ``network_metrics.csv``  one row per (cell, evaluation scenario): totals, VoTI, Jaccard, tau
- ``reference_metrics.csv`` Experiment E: the municipality's lines as drawn
- ``monte_carlo.csv``      Experiment D: per-repeat energies and VoTI
- ``stop_stability.csv``   Experiment D: per-stop modal-assignment share
- ``experiments_summary.csv`` everything the figures are drawn from

The solver is a heuristic under a time limit, so a solution is "the best found",
never "the optimum"; the time limit, seed and profile are in the manifest.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import logging
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from urgup_transport import energy as energy_model  # noqa: E402
from urgup_transport import optimizer, stores  # noqa: E402
from urgup_transport.config import DEFAULT_BASELINE_NETWORK, DEFAULT_VEHICLE_PROFILES, PROJECT_ROOT  # noqa: E402
from urgup_transport.elevation import ElevationGrid, load_grid  # noqa: E402
from urgup_transport.final_report.plan_runs import FOLD_LIMIT_BY_ROUTE_COUNT  # noqa: E402

log = logging.getLogger("terrain_experiments")

ETA_LEVELS = (0.0, 0.3, 0.5, 0.7)
MASS_LEVELS = ("empty", "average", "full")
SCALE_LEVELS = (0.0, 0.5, 1.0, 1.5, 2.0)
ETA_MAIN, MASS_MAIN = 0.5, "average"
NOISE_SIGMA_M = 4.0


# ── Cells ────────────────────────────────────────────────────────────────
def base_params(args: argparse.Namespace) -> dict[str, Any]:
    route_count = args.route_count
    return {
        "planning_mode": "vrp",
        "route_count": route_count,
        "single_route_per_mahalle": True,
        "small_mahalle_stop_limit": FOLD_LIMIT_BY_ROUTE_COUNT.get(route_count, 0),
        "route_shape": "shortest_closed",
        "solver_time_limit_seconds": args.time_limit,
        "distance_balance_weight": args.balance_weight,
        "longest_route_weight": 28,
        "dwell_time_seconds": 30,
        "avg_speed_kmh": 30,
        "min_stops_per_route": 1,
        "max_stops_per_route": 0,
        "max_route_distance_km": 0,
        "max_route_duration_minutes": 0,
        "exact_tsp_limit": 8,
        "seed": 42,
        "scenario_id": "terrain-matters",
        "scenario_type": "CURRENT_NETWORK",
        "vehicle_profile": args.vehicle_profile,
        "report_energy": True,
    }


def energy_options(
    *, surface: str = "dem", eta: float = ETA_MAIN, mass: str = MASS_MAIN,
    dem_scale: float = 1.0, noise: float = 0.0, seed: int | None = None, elevation_dir: str | None,
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "surface": surface, "eta_regen": eta, "mass_scenario": mass,
        "dem_scale": dem_scale, "dem_noise_sigma_m": noise,
    }
    if seed is not None:
        options["seed"] = seed
    if elevation_dir:
        options["elevation_dir"] = elevation_dir
    return options


def cell(cell_id: str, experiment: str, cost_basis: str, options: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"cell_id": cell_id, "experiment": experiment, "cost_basis": cost_basis, "energy_options": options, **extra}


def plan_cells(args: argparse.Namespace) -> list[dict[str, Any]]:
    wanted = set(args.experiments)
    elev = args.elevation_dir
    cells: list[dict[str, Any]] = []
    # The distance-optimal network is one solution whatever η or mass is; the
    # planar-energy network depends on mass (rolling resistance) but not on η
    # (no descent, so nothing to regenerate); the DEM network depends on both.
    if wanted & {"A", "B", "C", "D"}:
        cells.append(cell("A-distance", "A", "distance", energy_options(elevation_dir=elev)))
    if "A" in wanted or "B" in wanted:
        for mass in MASS_LEVELS:
            cells.append(cell(f"A-planar-{mass}", "A", "energy", energy_options(surface="planar", mass=mass, elevation_dir=elev)))
        for eta in ETA_LEVELS:
            for mass in MASS_LEVELS:
                cells.append(cell(f"A-dem-eta{eta}-{mass}", "A", "energy", energy_options(eta=eta, mass=mass, elevation_dir=elev)))
    if "C" in wanted:
        for scale in SCALE_LEVELS:
            if scale == 1.0 and "A" in wanted:
                continue  # identical to A-dem-eta0.5-average
            cells.append(cell(f"C-dem-scale{scale}", "C", "energy", energy_options(dem_scale=scale, elevation_dir=elev)))
    if "D" in wanted:
        for repeat in range(args.mc_repeats):
            cells.append(cell(
                f"D-noise-{repeat:03d}", "D", "energy",
                energy_options(noise=NOISE_SIGMA_M, seed=1000 + repeat, elevation_dir=elev),
                solver_time_limit_seconds=args.mc_time_limit,
            ))
    if "R" in wanted:
        for repeat in range(1, 4):
            cells.append(cell(f"R-dem-rep{repeat}", "R", "energy", energy_options(elevation_dir=elev)))
        cells.append(cell("R-balance0-distance", "R", "distance", energy_options(elevation_dir=elev), distance_balance_weight=0))
        cells.append(cell("R-balance0-dem", "R", "energy", energy_options(elevation_dir=elev), distance_balance_weight=0))
        for cap in (0.15, 0.30):
            cells.append(cell(f"R-cap{cap}-dem", "R", "energy", {**energy_options(elevation_dir=elev), "grade_cap": cap}, grade_cap=cap))
    return cells


# ── Evaluation ───────────────────────────────────────────────────────────
class Evaluator:
    """Re-drive stored proposals on any terrain, without re-routing them."""

    def __init__(self, base_grid: ElevationGrid, depot_lnglat: tuple[float, float], speed_kmh: float, dwell_s: float):
        self.base_grid = base_grid
        self.reference_m = base_grid.at(depot_lnglat[1], depot_lnglat[0])
        self.speed_kmh = speed_kmh
        self.dwell_s = dwell_s
        self._grids: dict[tuple[float, float, int | None], ElevationGrid] = {}

    def grid(self, dem_scale: float = 1.0, noise: float = 0.0, seed: int | None = None) -> ElevationGrid:
        key = (dem_scale, noise, seed if noise else None)
        if key not in self._grids:
            if dem_scale == 1.0 and noise == 0.0:
                self._grids[key] = self.base_grid
            else:
                self._grids[key] = energy_model.derive_grid(
                    self.base_grid, dem_scale=dem_scale, reference_m=self.reference_m,
                    noise_sigma_m=noise, seed=seed,
                )
        return self._grids[key]

    @staticmethod
    def route_features(proposal: dict[str, Any]) -> list[dict[str, Any]]:
        """Layer geometry joined with the metrics' edge speeds, per route."""
        by_id = {row["route_id"]: row for row in proposal["metrics"]["routes"]}
        features = []
        for feature in proposal["layers"]["routes"]["features"]:
            route_id = feature["properties"]["route_id"]
            metrics = by_id.get(route_id, {})
            features.append({
                "id": route_id,
                "properties": {**feature["properties"], "edge_speeds": metrics.get("edge_speeds") or []},
                "geometry": feature["geometry"],
            })
        return features

    def evaluate(
        self, proposal: dict[str, Any], *, eta: float, mass: str,
        dem_scale: float = 1.0, noise: float = 0.0, seed: int | None = None,
        vehicle_profile: str | None = None, grade_cap: float = 0.20,
    ) -> dict[str, Any]:
        profile = energy_model.vehicle_profile(vehicle_profile, mass_scenario=mass, eta_regen=eta)
        grid = self.grid(dem_scale, noise, seed)
        dem = energy_model.EnergyOptions(surface="dem", grade_cap=grade_cap)
        planar = energy_model.EnergyOptions(surface="planar", grade_cap=grade_cap)
        stop_j = energy_model.stop_energy_j(self.speed_kmh / 3.6, self.dwell_s, profile)
        rows = []
        for feature in self.route_features(proposal):
            edges = energy_model.feature_edges(feature, self.speed_kmh)
            stops = int(feature["properties"].get("stop_count") or 0)
            fwd = energy_model.path_energy(edges, profile, dem, grid)
            rev = energy_model.path_energy(edges, profile, dem, grid, reverse=True)
            flat = energy_model.path_energy(edges, profile, planar, None)
            rev_flat = energy_model.path_energy(edges, profile, planar, None, reverse=True)
            for direction, on_dem, on_plane in (("forward", fwd, flat), ("reverse", rev, rev_flat)):
                rows.append({
                    "route_id": feature["id"],
                    "route_name": feature["properties"].get("name"),
                    "direction": direction,
                    "distance_m": round(on_dem.length_m, 1),
                    "slope_distance_m": round(on_dem.slope_length_m, 1),
                    "climb_m": round(on_dem.climb_m, 1),
                    "descent_m": round(on_dem.descent_m, 1),
                    "energy_wh_planar": round(on_plane.total_j / 3600, 1),
                    "energy_wh_dem": round(on_dem.total_j / 3600, 1),
                    "traction_wh": round(on_dem.traction_j / 3600, 1),
                    "regen_wh": round(on_dem.regen_j / 3600, 1),
                    "aux_wh": round(on_dem.aux_j / 3600, 1),
                    "lost_friction_wh": round(on_dem.friction_loss_j / 3600, 1),
                    "stop_energy_wh": round(stop_j * stops / 3600, 1),
                    "stops": stops,
                    "n_capped": on_dem.n_capped,
                    "n_nodata": on_dem.n_nodata,
                })
        forward = [row for row in rows if row["direction"] == "forward"]
        reverse = [row for row in rows if row["direction"] == "reverse"]
        total_dem = sum(row["energy_wh_dem"] for row in forward)
        total_planar = sum(row["energy_wh_planar"] for row in forward)
        total_rev = sum(row["energy_wh_dem"] for row in reverse)
        asym = [
            abs(f["energy_wh_dem"] - r["energy_wh_dem"]) / max(abs(f["energy_wh_dem"]), abs(r["energy_wh_dem"]), 1e-9)
            for f, r in zip(forward, reverse, strict=True)
        ]
        climb = sum(row["climb_m"] for row in forward)
        analytic = (1.0 / profile.eta_drive - profile.eta_regen) * profile.mass_kg * energy_model.G * climb / 3600.0
        return {
            "routes": rows,
            "total_energy_wh_dem": round(total_dem, 1),
            "total_energy_wh_planar": round(total_planar, 1),
            "total_energy_wh_reverse_dem": round(total_rev, 1),
            "total_distance_m": round(sum(row["distance_m"] for row in forward), 1),
            "total_slope_distance_m": round(sum(row["slope_distance_m"] for row in forward), 1),
            "total_climb_m": round(climb, 1),
            "total_lost_friction_wh": round(sum(row["lost_friction_wh"] for row in forward), 1),
            "total_regen_wh": round(sum(row["regen_wh"] for row in forward), 1),
            "total_stop_energy_wh": round(sum(row["stop_energy_wh"] for row in forward), 1),
            "planar_error_ratio": round(total_planar / total_dem - 1.0, 4) if total_dem else None,
            "terrain_penalty_wh": round(total_dem - total_planar, 1),
            "analytic_penalty_wh": round(analytic, 1),
            "mean_asymmetry": round(sum(asym) / len(asym), 4) if asym else None,
            "max_asymmetry": round(max(asym), 4) if asym else None,
            "friction_share": round(sum(row["lost_friction_wh"] for row in forward) / total_dem, 4) if total_dem else None,
            "slope_distance_ratio": round(
                sum(row["slope_distance_m"] for row in forward) / sum(row["distance_m"] for row in forward) - 1.0, 5
            ) if forward else None,
        }


# ── Membership similarity ────────────────────────────────────────────────
def memberships(proposal: dict[str, Any]) -> dict[str, list[str]]:
    """route_id → ordered customer stop ids (depot left out)."""
    depot = proposal["depot_id"]
    out: dict[str, list[tuple[int, str]]] = {}
    for feature in proposal["layers"]["stops"]["features"]:
        props = feature["properties"]
        if props["stop_id"] == depot:
            continue
        out.setdefault(props["route_id"], []).append((int(props.get("sequence") or 0), props["stop_id"]))
    return {route: [stop for _seq, stop in sorted(items)] for route, items in out.items()}


def best_matching(a: dict[str, list[str]], b: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Pair routes across two networks to maximise shared stops (exact, n ≤ 8)."""
    keys_a, keys_b = list(a), list(b)
    if len(keys_a) > 9 or len(keys_b) > 9:
        raise ValueError("Eşleştirme sekiz hattan fazlası için kaba kuvvetle yapılmaz.")
    sets_a = {k: set(v) for k, v in a.items()}
    sets_b = {k: set(v) for k, v in b.items()}
    short, long_, flip = (keys_a, keys_b, False) if len(keys_a) <= len(keys_b) else (keys_b, keys_a, True)
    best_score, best_pairs = -1, []
    for permutation in itertools.permutations(long_, len(short)):
        pairs = list(zip(short, permutation))
        score = sum(
            len((sets_b if flip else sets_a)[x] & (sets_a if flip else sets_b)[y]) for x, y in pairs
        )
        if score > best_score:
            best_score, best_pairs = score, pairs
    return [(y, x) if flip else (x, y) for x, y in best_pairs]


def kendall_tau(order_a: list[str], order_b: list[str]) -> float | None:
    common = [s for s in order_a if s in set(order_b)]
    if len(common) < 2:
        return None
    rank_b = {s: i for i, s in enumerate(order_b)}
    concordant = discordant = 0
    for i in range(len(common)):
        for j in range(i + 1, len(common)):
            if rank_b[common[i]] < rank_b[common[j]]:
                concordant += 1
            else:
                discordant += 1
    return (concordant - discordant) / (concordant + discordant)


def similarity(a: dict[str, list[str]], b: dict[str, list[str]]) -> dict[str, Any]:
    pairs = best_matching(a, b)
    jaccards, taus, shared = [], [], 0
    for x, y in pairs:
        sa, sb = set(a[x]), set(b[y])
        jaccards.append(len(sa & sb) / len(sa | sb) if sa | sb else 1.0)
        shared += len(sa & sb)
        tau = kendall_tau(a[x], b[y])
        if tau is not None:
            taus.append(tau)
    total_stops = sum(len(v) for v in a.values())
    return {
        "mean_jaccard": round(sum(jaccards) / len(jaccards), 4) if jaccards else None,
        "stops_changing_route": total_stops - shared,
        "stops_total": total_stops,
        "mean_kendall_tau": round(sum(taus) / len(taus), 4) if taus else None,
        "pairs": pairs,
    }


# ── Reference layer (Experiment E) ───────────────────────────────────────
def reference_rows(features: list[dict[str, Any]], grid: ElevationGrid, speed_kmh: float, dwell_s: float, vehicle_profile: str | None) -> list[dict[str, Any]]:
    rows = []
    dem = energy_model.EnergyOptions(surface="dem")
    planar = energy_model.EnergyOptions(surface="planar")
    for eta in ETA_LEVELS:
        for mass in MASS_LEVELS:
            profile = energy_model.vehicle_profile(vehicle_profile, mass_scenario=mass, eta_regen=eta)
            stop_j = energy_model.stop_energy_j(speed_kmh / 3.6, dwell_s, profile)
            for feature in features:
                edges = energy_model.feature_edges(feature, speed_kmh)
                if not edges:
                    continue
                props = feature.get("properties") or {}
                stops = int(props.get("stop_count") or 0)
                fwd = energy_model.path_energy(edges, profile, dem, grid)
                rev = energy_model.path_energy(edges, profile, dem, grid, reverse=True)
                flat = energy_model.path_energy(edges, profile, planar, None)
                # How the line was drawn: one closed loop, or open runs with
                # gaps between them (which are not driven).
                first, last = edges[0].coordinates[0], edges[-1].coordinates[-1]
                closing_gap = energy_model.haversine_m(first[1], first[0], last[1], last[0])
                for direction, seg in (("forward", fwd), ("reverse", rev)):
                    rows.append({
                        "route_name": props.get("name"),
                        "direction": direction,
                        "drawn_runs": len(edges),
                        "closing_gap_m": round(closing_gap, 1),
                        "drawn_as": "closed_loop" if len(edges) == 1 and closing_gap < 50 else "open_chain",
                        "eta_regen": eta,
                        "mass_scenario": mass,
                        "speed_kmh": speed_kmh,
                        "distance_m": round(seg.length_m, 1),
                        "slope_distance_m": round(seg.slope_length_m, 1),
                        "climb_m": round(seg.climb_m, 1),
                        "descent_m": round(seg.descent_m, 1),
                        "energy_wh_planar": round(flat.total_j / 3600, 1),
                        "energy_wh_dem": round(seg.total_j / 3600, 1),
                        "traction_wh": round(seg.traction_j / 3600, 1),
                        "regen_wh": round(seg.regen_j / 3600, 1),
                        "aux_wh": round(seg.aux_j / 3600, 1),
                        "lost_friction_wh": round(seg.friction_loss_j / 3600, 1),
                        "stop_energy_wh": round(stop_j * stops / 3600, 1),
                        "stops": stops,
                        "asymmetry": round(abs(fwd.total_j - rev.total_j) / max(abs(fwd.total_j), abs(rev.total_j), 1e-9), 4),
                        "planar_error_ratio": round(flat.total_j / fwd.total_j - 1.0, 4) if fwd.total_j else None,
                        "n_capped": seg.n_capped,
                        "n_nodata": seg.n_nodata,
                    })
    return rows


# ── I/O helpers ──────────────────────────────────────────────────────────
def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return None


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def load_draft(args: argparse.Namespace) -> dict[str, Any]:
    if args.draft:
        return json.loads(Path(args.draft).read_text(encoding="utf-8"))
    return stores.build_store().read()


def load_reference(args: argparse.Namespace) -> list[dict[str, Any]]:
    path = Path(args.reference) if args.reference else PROJECT_ROOT / DEFAULT_BASELINE_NETWORK
    if not path.exists():
        log.warning("Referans ağ yok: %s", path)
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ((payload.get("routes") or {}).get("features")) or []


def depot_lnglat(draft: dict[str, Any]) -> tuple[float, float]:
    depot, _customers = optimizer._extract_stops(draft)
    return depot.lng, depot.lat


# ── Main ─────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--experiments", default="A,B,C,D,E,R", help="Comma list of A,B,C,D,E,R (R = repeats and sensitivity)")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "data/processed/experiments"))
    parser.add_argument("--route-count", type=int, default=8)
    parser.add_argument("--time-limit", type=int, default=15, help="OR-Tools seconds per cell (balanced profile)")
    parser.add_argument("--mc-time-limit", type=int, default=5, help="OR-Tools seconds per Monte Carlo repeat")
    parser.add_argument("--mc-repeats", type=int, default=30)
    parser.add_argument("--balance-weight", type=int, default=20)
    parser.add_argument("--vehicle-profile", default=None)
    parser.add_argument("--draft", default=None, help="Draft JSON instead of the store (tests)")
    parser.add_argument("--roads", default=None, help="Editable road GeoJSON instead of the store (tests)")
    parser.add_argument("--elevation-dir", default=None, help="Grid directory instead of the default (tests)")
    parser.add_argument("--reference", default=None, help="Reference network JSON (Experiment E)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.experiments = [item.strip().upper() for item in args.experiments.split(",") if item.strip()]

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.roads:
        optimizer.DEFAULT_EDITABLE_ROAD_NETWORK = Path(args.roads)
    out = Path(args.out) / args.run_id
    cells = plan_cells(args)
    if args.dry_run:
        for item in cells:
            print(item["cell_id"], item["cost_basis"], item["energy_options"])
        print(len(cells), "cells")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    (out / "proposals").mkdir(exist_ok=True)

    draft = load_draft(args)
    base = base_params(args)
    grid = load_grid(args.elevation_dir)
    evaluator = Evaluator(grid, depot_lnglat(draft), base["avg_speed_kmh"], base["dwell_time_seconds"])
    profiles_path = PROJECT_ROOT / DEFAULT_VEHICLE_PROFILES
    manifest: dict[str, Any] = {
        "run_id": args.run_id,
        "started_utc": datetime.now(UTC).isoformat(),
        "git_commit": git_commit(),
        "python": platform.python_version(),
        "draft_revision": draft.get("revision"),
        "stop_count": len(draft["layers"]["stops"]["features"]),
        "elevation": {
            "dataset": grid.meta.get("dataset"), "source": grid.meta.get("source"),
            "sha256": grid.meta.get("sha256"), "vertical_accuracy_m": grid.meta.get("vertical_accuracy_m"),
            "grade_uncertainty_percent": round(grid.grade_uncertainty_percent(), 1),
            "reference_elevation_m": evaluator.reference_m,
        },
        "vehicle_profiles_sha256": file_sha256(profiles_path),
        "vehicle_profiles": json.loads(profiles_path.read_text(encoding="utf-8")),
        "base_params": base,
        "energy_model_version": energy_model.ENERGY_MODEL_VERSION,
        "experiments": args.experiments,
        "eta_levels": ETA_LEVELS, "mass_levels": MASS_LEVELS, "scale_levels": SCALE_LEVELS,
        "noise_sigma_m": NOISE_SIGMA_M, "mc_repeats": args.mc_repeats,
        "cells": [],
    }
    try:
        import ortools
        manifest["ortools"] = getattr(ortools, "__version__", None)
    except Exception:
        pass

    # 1. Optimise every cell.
    proposals: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(cells, start=1):
        cell_id = item["cell_id"]
        path = out / "proposals" / f"{cell_id}.json"
        if path.exists():
            proposals[cell_id] = json.loads(path.read_text(encoding="utf-8"))
            log.info("[%d/%d] %s: mevcut öneri okundu", index, len(cells), cell_id)
            continue
        params = {**base, "cost_basis": item["cost_basis"], "energy_options": item["energy_options"]}
        for key in ("solver_time_limit_seconds", "distance_balance_weight"):
            if key in item:
                params[key] = item[key]
        started = time.time()
        log.info("[%d/%d] %s başlıyor: %s %s", index, len(cells), cell_id, item["cost_basis"], item["energy_options"])
        proposal = optimizer.optimize(draft, params)
        seconds = time.time() - started
        proposal["experiment_cell"] = {**item, "seconds": round(seconds, 1)}
        path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
        proposals[cell_id] = proposal
        m = proposal["metrics"]
        log.info(
            "[%d/%d] %s bitti: %.0f s, %.1f km, E_dem %.0f Wh, E_planar %.0f Wh, negatif ağırlık %s",
            index, len(cells), cell_id, seconds, m["total_distance_m"] / 1000,
            m.get("total_energy_dem_wh", 0), m.get("total_energy_planar_wh", 0),
            (proposal.get("energy") or {}).get("n_negative_adjusted"),
        )
        manifest["cells"].append({**item, "seconds": round(seconds, 1), "proposal_id": proposal["proposal_id"],
                                  "algorithm": proposal["algorithm"], "fitness": m.get("fitness")})

    # 2. Evaluate.
    route_rows: list[dict[str, Any]] = []
    network_rows: list[dict[str, Any]] = []
    features_out: list[dict[str, Any]] = []

    def record(cell_id: str, proposal: dict[str, Any], *, eta: float, mass: str, scale: float = 1.0,
               noise: float = 0.0, seed: int | None = None, extra: dict[str, Any] | None = None,
               grade_cap: float = 0.20) -> dict[str, Any]:
        item = proposal["experiment_cell"]
        opts = item["energy_options"]
        result = evaluator.evaluate(proposal, eta=eta, mass=mass, dem_scale=scale, noise=noise, seed=seed,
                                    vehicle_profile=args.vehicle_profile, grade_cap=grade_cap)
        head = {
            "run_id": args.run_id, "cell_id": cell_id, "experiment": item["experiment"],
            "cost_basis": item["cost_basis"], "surface_opt": opts.get("surface", "dem") if item["cost_basis"] == "energy" else "none",
            "eta_regen_opt": opts.get("eta_regen"), "mass_opt": opts.get("mass_scenario"),
            "dem_scale_opt": opts.get("dem_scale", 1.0), "noise_seed_opt": opts.get("seed"),
            "eta_regen_eval": eta, "mass_eval": mass, "dem_scale_eval": scale, "noise_seed_eval": seed if noise else None,
        }
        for row in result["routes"]:
            route_rows.append({**head, **row})
        summary = {**head, **{k: v for k, v in result.items() if k != "routes"}, **(extra or {})}
        network_rows.append(summary)
        return summary

    def add_features(cell_id: str, proposal: dict[str, Any]) -> None:
        for feature in proposal["layers"]["routes"]["features"]:
            features_out.append({
                "type": "Feature",
                "id": f"{cell_id}:{feature['properties']['route_id']}",
                "properties": {
                    "cell_id": cell_id, "route_id": feature["properties"]["route_id"],
                    "name": feature["properties"]["name"], "color": feature["properties"].get("color"),
                    "distance_m": feature["properties"].get("distance_m"),
                    "energy_dem_wh": feature["properties"].get("energy_dem_wh"),
                    "stop_count": feature["properties"].get("stop_count"),
                },
                "geometry": feature["geometry"],
            })

    for cell_id, proposal in proposals.items():
        add_features(cell_id, proposal)

    summary_rows: list[dict[str, Any]] = []
    distance_cell = proposals.get("A-distance")
    memb = {cid: memberships(p) for cid, p in proposals.items()}

    if "A" in args.experiments or "B" in args.experiments:
        for eta in ETA_LEVELS:
            for mass in MASS_LEVELS:
                d = record("A-distance", distance_cell, eta=eta, mass=mass) if distance_cell else None
                p_cell = f"A-planar-{mass}"
                e_cell = f"A-dem-eta{eta}-{mass}"
                p = record(p_cell, proposals[p_cell], eta=eta, mass=mass) if p_cell in proposals else None
                e = record(e_cell, proposals[e_cell], eta=eta, mass=mass) if e_cell in proposals else None
                if d and p and e:
                    sim_dp = similarity(memb["A-distance"], memb[p_cell])
                    sim_de = similarity(memb["A-distance"], memb[e_cell])
                    sim_pe = similarity(memb[p_cell], memb[e_cell])
                    summary_rows.append({
                        "experiment": "A", "eta_regen": eta, "mass_scenario": mass,
                        "E_dem_distance_opt": d["total_energy_wh_dem"], "E_dem_planar_opt": p["total_energy_wh_dem"],
                        "E_dem_dem_opt": e["total_energy_wh_dem"],
                        "E_planar_distance_opt": d["total_energy_wh_planar"], "E_planar_planar_opt": p["total_energy_wh_planar"],
                        "E_planar_dem_opt": e["total_energy_wh_planar"],
                        "km_distance_opt": d["total_distance_m"] / 1000, "km_planar_opt": p["total_distance_m"] / 1000,
                        "km_dem_opt": e["total_distance_m"] / 1000,
                        "climb_distance_opt": d["total_climb_m"], "climb_planar_opt": p["total_climb_m"], "climb_dem_opt": e["total_climb_m"],
                        "voti_vs_distance_wh": round(d["total_energy_wh_dem"] - e["total_energy_wh_dem"], 1),
                        "voti_vs_distance_pct": round((d["total_energy_wh_dem"] - e["total_energy_wh_dem"]) / d["total_energy_wh_dem"] * 100, 2),
                        "voti_vs_planar_wh": round(p["total_energy_wh_dem"] - e["total_energy_wh_dem"], 1),
                        "voti_vs_planar_pct": round((p["total_energy_wh_dem"] - e["total_energy_wh_dem"]) / p["total_energy_wh_dem"] * 100, 2),
                        "planar_error_distance_opt": d["planar_error_ratio"], "planar_error_planar_opt": p["planar_error_ratio"],
                        "planar_error_dem_opt": e["planar_error_ratio"],
                        "terrain_penalty_dem_opt_wh": e["terrain_penalty_wh"], "analytic_penalty_dem_opt_wh": e["analytic_penalty_wh"],
                        "friction_share_dem_opt": e["friction_share"], "friction_share_distance_opt": d["friction_share"],
                        "asym_distance_opt": d["mean_asymmetry"], "asym_planar_opt": p["mean_asymmetry"], "asym_dem_opt": e["mean_asymmetry"],
                        "jaccard_distance_planar": sim_dp["mean_jaccard"], "jaccard_distance_dem": sim_de["mean_jaccard"],
                        "jaccard_planar_dem": sim_pe["mean_jaccard"],
                        "stops_changed_distance_dem": sim_de["stops_changing_route"], "stops_changed_planar_dem": sim_pe["stops_changing_route"],
                        "tau_planar_dem": sim_pe["mean_kendall_tau"], "tau_distance_dem": sim_de["mean_kendall_tau"],
                        "slope_distance_ratio": e["slope_distance_ratio"],
                    })

    if "C" in args.experiments and distance_cell:
        for scale in SCALE_LEVELS:
            c_cell = "A-dem-eta0.5-average" if (scale == 1.0 and "A-dem-eta0.5-average" in proposals) else f"C-dem-scale{scale}"
            if c_cell not in proposals:
                continue
            d = record("A-distance", distance_cell, eta=ETA_MAIN, mass=MASS_MAIN, scale=scale)
            e = record(c_cell, proposals[c_cell], eta=ETA_MAIN, mass=MASS_MAIN, scale=scale)
            sim = similarity(memb["A-distance"], memb[c_cell])
            p_cell = f"A-planar-{MASS_MAIN}"
            p = record(p_cell, proposals[p_cell], eta=ETA_MAIN, mass=MASS_MAIN, scale=scale) if p_cell in proposals else None
            sim_p = similarity(memb[p_cell], memb[c_cell]) if p else None
            summary_rows.append({
                "experiment": "C", "dem_scale": scale, "eta_regen": ETA_MAIN, "mass_scenario": MASS_MAIN,
                "E_scaled_distance_opt": d["total_energy_wh_dem"], "E_scaled_dem_opt": e["total_energy_wh_dem"],
                "E_scaled_planar_opt": p["total_energy_wh_dem"] if p else None,
                "voti_vs_distance_wh": round(d["total_energy_wh_dem"] - e["total_energy_wh_dem"], 1),
                "voti_vs_distance_pct": round((d["total_energy_wh_dem"] - e["total_energy_wh_dem"]) / d["total_energy_wh_dem"] * 100, 2),
                "voti_vs_planar_wh": round(p["total_energy_wh_dem"] - e["total_energy_wh_dem"], 1) if p else None,
                "voti_vs_planar_pct": round((p["total_energy_wh_dem"] - e["total_energy_wh_dem"]) / p["total_energy_wh_dem"] * 100, 2) if p else None,
                "km_distance_opt": d["total_distance_m"] / 1000, "km_dem_opt": e["total_distance_m"] / 1000,
                "climb_dem_opt": e["total_climb_m"], "climb_distance_opt": d["total_climb_m"],
                "planar_error_distance_opt": d["planar_error_ratio"],
                "jaccard_distance_dem": sim["mean_jaccard"], "stops_changed_distance_dem": sim["stops_changing_route"],
                "tau_distance_dem": sim["mean_kendall_tau"],
                "jaccard_planar_dem": sim_p["mean_jaccard"] if sim_p else None,
                "stops_changed_planar_dem": sim_p["stops_changing_route"] if sim_p else None,
                "asym_dem_opt": e["mean_asymmetry"], "friction_share_dem_opt": e["friction_share"],
            })

    mc_rows: list[dict[str, Any]] = []
    stability_rows: list[dict[str, Any]] = []
    if "D" in args.experiments and distance_cell:
        anchor_id = "A-dem-eta0.5-average" if "A-dem-eta0.5-average" in proposals else None
        assignment_counts: dict[str, dict[str, int]] = {}
        repeats = [cid for cid in proposals if cid.startswith("D-noise-")]
        for cid in repeats:
            seed = proposals[cid]["experiment_cell"]["energy_options"]["seed"]
            d_own = record("A-distance", distance_cell, eta=ETA_MAIN, mass=MASS_MAIN, noise=NOISE_SIGMA_M, seed=seed)
            e_own = record(cid, proposals[cid], eta=ETA_MAIN, mass=MASS_MAIN, noise=NOISE_SIGMA_M, seed=seed)
            e_base = record(cid, proposals[cid], eta=ETA_MAIN, mass=MASS_MAIN)
            d_base = network_rows_lookup = None
            row = {
                "cell_id": cid, "seed": seed,
                "E_perturbed_distance_opt": d_own["total_energy_wh_dem"], "E_perturbed_dem_opt": e_own["total_energy_wh_dem"],
                "voti_perturbed_wh": round(d_own["total_energy_wh_dem"] - e_own["total_energy_wh_dem"], 1),
                "E_base_dem_opt": e_base["total_energy_wh_dem"],
                "climb_perturbed": e_own["total_climb_m"], "climb_base": e_base["total_climb_m"],
                "km_dem_opt": e_base["total_distance_m"] / 1000,
            }
            if anchor_id:
                sim = similarity(memb[anchor_id], memb[cid])
                row["jaccard_vs_anchor"] = sim["mean_jaccard"]
                row["stops_changed_vs_anchor"] = sim["stops_changing_route"]
                for anchor_route, repeat_route in sim["pairs"]:
                    for stop in memb[cid][repeat_route]:
                        assignment_counts.setdefault(stop, {})
                        assignment_counts[stop][anchor_route] = assignment_counts[stop].get(anchor_route, 0) + 1
            mc_rows.append(row)
        base_eval = evaluator.evaluate(distance_cell, eta=ETA_MAIN, mass=MASS_MAIN, vehicle_profile=args.vehicle_profile)
        for row in mc_rows:
            row["E_base_distance_opt"] = base_eval["total_energy_wh_dem"]
            row["voti_base_wh"] = round(base_eval["total_energy_wh_dem"] - row["E_base_dem_opt"], 1)
        for stop, counts in sorted(assignment_counts.items()):
            total = sum(counts.values())
            modal_route, modal = max(counts.items(), key=lambda kv: kv[1])
            stability_rows.append({"stop_id": stop, "repeats": total, "modal_route": modal_route,
                                   "modal_share": round(modal / total, 4)})
        if mc_rows:
            votis = sorted(r["voti_perturbed_wh"] for r in mc_rows)
            energies = sorted(r["E_perturbed_dem_opt"] for r in mc_rows)
            def pct(values, q):
                if not values:
                    return None
                k = (len(values) - 1) * q
                lo, hi = int(k), min(int(k) + 1, len(values) - 1)
                return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 1)
            summary_rows.append({
                "experiment": "D", "eta_regen": ETA_MAIN, "mass_scenario": MASS_MAIN, "noise_sigma_m": NOISE_SIGMA_M,
                "repeats": len(mc_rows),
                "voti_p05": pct(votis, 0.05), "voti_p50": pct(votis, 0.5), "voti_p95": pct(votis, 0.95),
                "E_dem_opt_p05": pct(energies, 0.05), "E_dem_opt_p50": pct(energies, 0.5), "E_dem_opt_p95": pct(energies, 0.95),
                "mean_modal_share": round(sum(r["modal_share"] for r in stability_rows) / len(stability_rows), 4) if stability_rows else None,
                "share_stops_modal_ge_0_8": round(sum(1 for r in stability_rows if r["modal_share"] >= 0.8) / len(stability_rows), 4) if stability_rows else None,
                "mean_jaccard_vs_anchor": round(sum(r.get("jaccard_vs_anchor") or 0 for r in mc_rows) / len(mc_rows), 4) if anchor_id else None,
            })

    if "R" in args.experiments:
        for cid in [c for c in proposals if c.startswith("R-")]:
            cap = float(proposals[cid]["experiment_cell"].get("grade_cap", 0.20))
            e = record(cid, proposals[cid], eta=ETA_MAIN, mass=MASS_MAIN, grade_cap=cap)
            row = {"experiment": "R", "cell_id": cid, "grade_cap": cap, "E_dem": e["total_energy_wh_dem"],
                   "E_planar": e["total_energy_wh_planar"], "km": e["total_distance_m"] / 1000, "climb": e["total_climb_m"],
                   "planar_error": e["planar_error_ratio"], "asym": e["mean_asymmetry"]}
            if distance_cell:
                d = record("A-distance", distance_cell, eta=ETA_MAIN, mass=MASS_MAIN, grade_cap=cap)
                row["E_dem_distance_opt_same_cap"] = d["total_energy_wh_dem"]
                row["voti_vs_distance_wh"] = round(d["total_energy_wh_dem"] - e["total_energy_wh_dem"], 1)
                row["voti_vs_distance_pct"] = round((d["total_energy_wh_dem"] - e["total_energy_wh_dem"]) / d["total_energy_wh_dem"] * 100, 2)
            if "A-dem-eta0.5-average" in proposals:
                sim = similarity(memb["A-dem-eta0.5-average"], memb[cid])
                row["jaccard_vs_A_dem"] = sim["mean_jaccard"]
                row["stops_changed_vs_A_dem"] = sim["stops_changing_route"]
            if distance_cell:
                sim = similarity(memb["A-distance"], memb[cid])
                row["jaccard_vs_A_distance"] = sim["mean_jaccard"]
            summary_rows.append(row)

    ref_rows: list[dict[str, Any]] = []
    if "E" in args.experiments:
        features = load_reference(args)
        ref_rows = reference_rows(features, grid, base["avg_speed_kmh"], base["dwell_time_seconds"], args.vehicle_profile)
        for eta in ETA_LEVELS:
            for mass in MASS_LEVELS:
                fwd = [r for r in ref_rows if r["direction"] == "forward" and r["eta_regen"] == eta and r["mass_scenario"] == mass]
                if not fwd:
                    continue
                e_dem = sum(r["energy_wh_dem"] for r in fwd)
                e_pl = sum(r["energy_wh_planar"] for r in fwd)
                summary_rows.append({
                    "experiment": "E", "eta_regen": eta, "mass_scenario": mass, "routes": len(fwd),
                    "E_dem_reference": round(e_dem, 1), "E_planar_reference": round(e_pl, 1),
                    "km_reference": round(sum(r["distance_m"] for r in fwd) / 1000, 2),
                    "climb_reference": round(sum(r["climb_m"] for r in fwd), 1),
                    "planar_error_reference": round(e_pl / e_dem - 1.0, 4) if e_dem else None,
                    "mean_asymmetry_reference": round(sum(r["asymmetry"] for r in fwd) / len(fwd), 4),
                    "max_asymmetry_reference": round(max(r["asymmetry"] for r in fwd), 4),
                    "friction_share_reference": round(sum(r["lost_friction_wh"] for r in fwd) / e_dem, 4) if e_dem else None,
                })

    # 3. Write.
    write_csv(out / "route_metrics.csv", route_rows)
    write_csv(out / "network_metrics.csv", network_rows)
    write_csv(out / "experiments_summary.csv", summary_rows)
    write_csv(out / "reference_metrics.csv", ref_rows)
    write_csv(out / "monte_carlo.csv", mc_rows)
    write_csv(out / "stop_stability.csv", stability_rows)
    (out / "routes.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features_out}, ensure_ascii=False), encoding="utf-8"
    )
    manifest["finished_utc"] = datetime.now(UTC).isoformat()
    manifest["cell_count"] = len(proposals)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Bitti: %s (%d hücre, %d özet satırı)", out, len(proposals), len(summary_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
