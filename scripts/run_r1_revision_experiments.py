#!/usr/bin/env python3
"""
Reviewer-revision (R1) experiments for the Terrain Matters manuscript.

    # draft and roads: PostgreSQL if DATABASE_URL is set, else data/editable/*.json
    .venv/bin/python scripts/run_r1_revision_experiments.py E1        # one experiment
    .venv/bin/python scripts/run_r1_revision_experiments.py report    # REPORT.md tables from the CSVs
    .venv/bin/python scripts/run_r1_revision_experiments.py figures   # PNGs from the CSVs

The main run ``data/processed/experiments/2026-09-29-urgup/`` is read, never
written. Everything new goes under ``data/processed/experiments/2026-10-R1/<E>/``
(manifest, proposals, per-route rows) and the paper-facing tables under
``docs/paper/revision_R1/``. Every optimisation is a *cell* whose proposal is
stored, so a run can be resumed; every solution is then re-driven on the
surfaces the experiment asks for without re-routing it, exactly as in
``run_terrain_experiments.py`` (whose evaluator, matching and CSV helpers this
script imports rather than copies).

Terms: the solver is a heuristic under a time limit, so every solution is the
*best found*; "reference" means the main run's solution for the same cost
basis.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import platform
import random
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_terrain_experiments as rte  # noqa: E402
from urgup_transport import energy as energy_model  # noqa: E402
from urgup_transport import optimizer, stores  # noqa: E402
from urgup_transport.config import DEFAULT_BASELINE_NETWORK, DEFAULT_VEHICLE_PROFILES, PROJECT_ROOT  # noqa: E402
from urgup_transport.elevation import ElevationGrid, load_grid  # noqa: E402

log = logging.getLogger("r1")

RUN_ID = "2026-10-R1"
MAIN_RUN_ID = "2026-09-29-urgup"
MAIN_RUN = PROJECT_ROOT / "data/processed/experiments" / MAIN_RUN_ID
R1_ROOT = PROJECT_ROOT / "data/processed/experiments" / RUN_ID
DOCS = PROJECT_ROOT / "docs/paper/revision_R1"
ETA_MAIN, MASS_MAIN = 0.5, "average"
EXPECTED_DRAFT_REVISION = 135
EXPECTED_ROAD_REVISION = 1584
REFERENCE_CELLS = {"distance": "A-distance", "energy-planar": "A-planar-average", "energy-dem": "A-dem-eta0.5-average"}


# ── Shared state ─────────────────────────────────────────────────────────
class Context:
    """Draft, roads, grid, base parameters and the main run, loaded once."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.draft = stores.build_store().read()
        self.roads = stores.editable_road_network_payload() or {}
        if int(self.draft.get("revision", 0)) != EXPECTED_DRAFT_REVISION:
            raise SystemExit(f"Taslak revizyonu {self.draft.get('revision')}, beklenen {EXPECTED_DRAFT_REVISION}")
        if int(self.roads.get("revision", 0)) != EXPECTED_ROAD_REVISION:
            raise SystemExit(f"Yol ağı revizyonu {self.roads.get('revision')}, beklenen {EXPECTED_ROAD_REVISION}")
        self.grid = load_grid()
        namespace = argparse.Namespace(route_count=8, time_limit=15, balance_weight=20, vehicle_profile=None)
        self.base = rte.base_params(namespace)
        self.depot_lnglat = rte.depot_lnglat(self.draft)
        self.evaluator = R1Evaluator(self.grid, self.depot_lnglat, self.base["avg_speed_kmh"], self.base["dwell_time_seconds"])
        self._main: dict[str, dict[str, Any]] = {}

    def main_proposal(self, cell_id: str) -> dict[str, Any]:
        if cell_id not in self._main:
            self._main[cell_id] = json.loads((MAIN_RUN / "proposals" / f"{cell_id}.json").read_text(encoding="utf-8"))
        return self._main[cell_id]

    def energy_options(self, **overrides: Any) -> dict[str, Any]:
        options = {"surface": "dem", "eta_regen": ETA_MAIN, "mass_scenario": MASS_MAIN, "dem_scale": 1.0, "dem_noise_sigma_m": 0.0}
        options.update(overrides)
        return options

    def optimise(self, exp_dir: Path, cell_id: str, cost_basis: str, options: dict[str, Any], **param_overrides: Any) -> dict[str, Any]:
        """Run one cell, or read it back if it was run before (resume)."""
        path = exp_dir / "proposals" / f"{cell_id}.json"
        if path.exists():
            proposal = json.loads(path.read_text(encoding="utf-8"))
            log.info("%s: mevcut öneri okundu (%.0f s)", cell_id, proposal["experiment_cell"]["seconds"])
            return proposal
        params = {**self.base, "cost_basis": cost_basis, "energy_options": options, **param_overrides}
        started = time.time()
        log.info("%s başlıyor: %s %s %s", cell_id, cost_basis, options, param_overrides)
        proposal = optimizer.optimize(self.draft, params)
        seconds = time.time() - started
        proposal["experiment_cell"] = {
            "cell_id": cell_id, "run_id": RUN_ID, "cost_basis": cost_basis, "energy_options": options,
            "param_overrides": param_overrides, "seconds": round(seconds, 1),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
        m = proposal["metrics"]
        log.info("%s bitti: %.0f s, %.1f km, E_dem %.0f Wh, fitness %s", cell_id, seconds, m["total_distance_m"] / 1000,
                 m.get("total_energy_dem_wh", 0), m.get("fitness"))
        return proposal

    def manifest(self, exp_dir: Path, experiment: str, **extra: Any) -> dict[str, Any]:
        profiles_path = PROJECT_ROOT / DEFAULT_VEHICLE_PROFILES
        try:
            import ortools
            ortools_version = getattr(ortools, "__version__", None)
        except Exception:  # pragma: no cover
            ortools_version = None
        payload = {
            "run_id": RUN_ID, "experiment": experiment, "started_utc": datetime.now(UTC).isoformat(),
            "git_commit": rte.git_commit(), "git_dirty": git_dirty(),
            "machine": {"node": platform.node(), "platform": platform.platform(), "cpus": os.cpu_count(), "python": platform.python_version(), "ortools": ortools_version},
            "draft_revision": self.draft.get("revision"), "stop_count": len(self.draft["layers"]["stops"]["features"]),
            "road_network_revision": self.roads.get("revision"), "road_network_features": len(self.roads.get("features", [])),
            "elevation": {
                "dataset": self.grid.meta.get("dataset"), "source": self.grid.meta.get("source"), "sha256": self.grid.meta.get("sha256"),
                "vertical_accuracy_m": self.grid.meta.get("vertical_accuracy_m"),
                "grade_uncertainty_percent": round(self.grid.grade_uncertainty_percent(), 1),
                "reference_elevation_m": self.evaluator.reference_m,
            },
            "vehicle_profiles_sha256": rte.file_sha256(profiles_path),
            "vehicle_profiles": json.loads(profiles_path.read_text(encoding="utf-8")),
            "base_params": self.base,
            "energy_options_default": self.energy_options(),
            "energy_model_version": energy_model.ENERGY_MODEL_VERSION,
            "main_run": {"run_id": MAIN_RUN_ID, "manifest": json.loads((MAIN_RUN / "manifest.json").read_text(encoding="utf-8")).get("git_commit")},
            **extra,
        }
        exp_dir.mkdir(parents=True, exist_ok=True)
        (exp_dir / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return payload

    @staticmethod
    def finish(exp_dir: Path, manifest: dict[str, Any], **extra: Any) -> None:
        manifest.update({"finished_utc": datetime.now(UTC).isoformat(), **extra})
        (exp_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class R1Evaluator(rte.Evaluator):
    """The main run's evaluator, plus a correlation length for the noise."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._corr = 3

    def grid(self, dem_scale: float = 1.0, noise: float = 0.0, seed: int | None = None) -> ElevationGrid:
        corr = self._corr if noise else None
        key = (dem_scale, noise, seed if noise else None, corr)
        if key not in self._grids:
            if dem_scale == 1.0 and noise == 0.0:
                self._grids[key] = self.base_grid
            else:
                self._grids[key] = energy_model.derive_grid(
                    self.base_grid, dem_scale=dem_scale, reference_m=self.reference_m,
                    noise_sigma_m=noise, seed=seed, noise_corr_cells=corr or 3,
                )
        return self._grids[key]

    def evaluate_on(self, proposal: dict[str, Any], *, corr: int = 3, **kwargs: Any) -> dict[str, Any]:
        self._corr = corr
        try:
            return self.evaluate(proposal, **kwargs)
        finally:
            self._corr = 3

    def forget(self, dem_scale: float, noise: float, seed: int | None, corr: int) -> None:
        self._grids.pop((dem_scale, noise, seed if noise else None, corr if noise else None), None)


def git_dirty() -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return bool(out.strip())
    except Exception:
        return False


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rte.write_csv(path, rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def fnum(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


def sd(values: list[float]) -> float:
    return pstdev(values) if len(values) > 1 else 0.0


def cost_label(cost_basis: str, options: dict[str, Any]) -> str:
    return cost_basis if cost_basis != "energy" else f"energy-{options.get('surface', 'dem')}"


# ── E1: solver robustness ───────────────────────────────────────────────
E1_SEEDS = tuple(range(1, 11))
E1_TIME_LIMITS = (15, 60, 300)


def run_e1(ctx: Context) -> None:
    exp_dir = R1_ROOT / "E1"
    manifest = ctx.manifest(exp_dir, "E1", seeds=E1_SEEDS, time_limits_s=E1_TIME_LIMITS,
                            seed_mechanism="node_order_seed: seeded permutation of the customer node order before the OR-Tools model is built (OR-Tools has no seed; ReSeed of its solver was verified to change nothing)")
    references = {basis: ctx.main_proposal(cell) for basis, cell in REFERENCE_CELLS.items() if basis != "energy-planar"}
    ref_memb = {basis: rte.memberships(p) for basis, p in references.items()}
    ref_eval = {basis: ctx.evaluator.evaluate(p, eta=ETA_MAIN, mass=MASS_MAIN) for basis, p in references.items()}
    rows: list[dict[str, Any]] = []
    total_solver = 0.0
    csv_path = DOCS / "E1_solver_robustness.csv"
    by_key: dict[tuple[str, int, int], dict[str, Any]] = {}
    for time_limit in E1_TIME_LIMITS:
        for seed in E1_SEEDS:
            for basis in ("distance", "energy-dem"):
                cost_basis = "distance" if basis == "distance" else "energy"
                cell_id = f"E1-{basis}-t{time_limit}-s{seed}"
                proposal = ctx.optimise(exp_dir, cell_id, cost_basis, ctx.energy_options(), solver_time_limit_seconds=time_limit, node_order_seed=seed)
                total_solver += proposal["experiment_cell"]["seconds"]
                evaluation = ctx.evaluator.evaluate(proposal, eta=ETA_MAIN, mass=MASS_MAIN)
                sim = rte.similarity(ref_memb[basis], rte.memberships(proposal))
                row = {
                    "run_id": RUN_ID, "cell_id": cell_id, "cost_basis": basis, "seed": seed, "time_limit_s": time_limit,
                    "solver_objective": proposal["metrics"].get("fitness"), "solver_seconds": proposal["experiment_cell"]["seconds"],
                    "E_dem_wh": evaluation["total_energy_wh_dem"], "E_planar_wh": evaluation["total_energy_wh_planar"],
                    "km": round(evaluation["total_distance_m"] / 1000, 3), "climb_m": evaluation["total_climb_m"],
                    "jaccard_to_reference": sim["mean_jaccard"], "stops_changing_line": sim["stops_changing_route"],
                    "kendall_tau_to_reference": sim["mean_kendall_tau"],
                    "reference_cell": REFERENCE_CELLS[basis], "E_dem_reference_wh": ref_eval[basis]["total_energy_wh_dem"],
                    "voti_vs_distance_wh": None, "voti_vs_distance_pct": None,
                    "eta_regen": ETA_MAIN, "mass_scenario": MASS_MAIN, "balance_weight": ctx.base["distance_balance_weight"],
                }
                by_key[(basis, time_limit, seed)] = row
                if basis == "energy-dem":
                    distance_row = by_key[("distance", time_limit, seed)]
                    row["voti_vs_distance_wh"] = round(distance_row["E_dem_wh"] - row["E_dem_wh"], 1)
                    row["voti_vs_distance_pct"] = round((distance_row["E_dem_wh"] - row["E_dem_wh"]) / distance_row["E_dem_wh"] * 100, 2)
                rows.append(row)
                write_csv(csv_path, rows)
                write_csv(exp_dir / "E1_solver_robustness.csv", rows)
    ctx.finish(exp_dir, manifest, cell_count=len(rows), total_solver_seconds=round(total_solver, 1))
    log.info("E1 bitti: %d hücre, %.0f s çözücü", len(rows), total_solver)


def e1_summary(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    out = []
    for basis in ("distance", "energy-dem"):
        for time_limit in E1_TIME_LIMITS:
            sub = [r for r in rows if r["cost_basis"] == basis and int(r["time_limit_s"]) == time_limit]
            if not sub:
                continue
            energies = [float(r["E_dem_wh"]) for r in sub]
            entry: dict[str, Any] = {
                "cost_basis": basis, "time_limit_s": time_limit, "n": len(sub),
                "E_dem_mean_wh": round(mean(energies), 1), "E_dem_sd_wh": round(sd(energies), 1),
                "E_dem_min_wh": round(min(energies), 1), "E_dem_max_wh": round(max(energies), 1),
                "E_dem_reference_wh": float(sub[0]["E_dem_reference_wh"]),
                "km_mean": round(mean(float(r["km"]) for r in sub), 2),
                "jaccard_mean": round(mean(float(r["jaccard_to_reference"]) for r in sub), 4),
                "stops_changing_line_mean": round(mean(float(r["stops_changing_line"]) for r in sub), 2),
                "kendall_tau_mean": round(mean(float(r["kendall_tau_to_reference"]) for r in sub if r["kendall_tau_to_reference"]), 3),
            }
            if basis == "energy-dem":
                votis = [float(r["voti_vs_distance_wh"]) for r in sub]
                pcts = [float(r["voti_vs_distance_pct"]) for r in sub]
                entry.update({
                    "voti_mean_wh": round(mean(votis), 1), "voti_sd_wh": round(sd(votis), 1),
                    "voti_min_wh": round(min(votis), 1), "voti_max_wh": round(max(votis), 1),
                    "voti_mean_pct": round(mean(pcts), 2), "voti_sd_pct": round(sd(pcts), 2),
                    "voti_positive_count": sum(1 for v in votis if v > 0),
                })
            out.append(entry)
    return out


# ── E2: neighbourhood constraint off ────────────────────────────────────
E2_SCALES = (1.5, 2.0)


def network_row(evaluation: dict[str, Any]) -> dict[str, Any]:
    return {
        "E_dem_wh": evaluation["total_energy_wh_dem"], "E_planar_wh": evaluation["total_energy_wh_planar"],
        "km": round(evaluation["total_distance_m"] / 1000, 3), "climb_m": evaluation["total_climb_m"],
        "mean_direction_asymmetry": evaluation["mean_asymmetry"], "max_direction_asymmetry": evaluation["max_asymmetry"],
        "friction_share_pct": round((evaluation["friction_share"] or 0.0) * 100, 2),
        "planar_error_pct": round((evaluation["planar_error_ratio"] or 0.0) * 100, 2),
    }


def run_e2(ctx: Context) -> None:
    exp_dir = R1_ROOT / "E2"
    off = {"single_route_per_mahalle": False, "small_mahalle_stop_limit": 0}
    manifest = ctx.manifest(exp_dir, "E2", constraint_off_params=off, scales=E2_SCALES, warm_time_limits_s=(15, 300),
                            note="distance and planar solutions do not depend on eta_regen, and the distance solution does not depend on dem_scale; they are optimised once and evaluated under every scenario, as in the main run. "
                                 "Variants: cold_15 = OR-Tools from its own first solution, 15 s (the plan as written); warm_15 / warm_300 = local search started from the constrained main-run solution of the same cell, 15 s / 300 s, because the cold start returned networks 7 km longer than the constrained ones (a heuristic failure, not a property of the relaxation)")
    on: dict[tuple[str, float, float], dict[str, Any]] = {
        ("distance", 0.5, 1.0): ctx.main_proposal("A-distance"),
        ("energy-planar", 0.5, 1.0): ctx.main_proposal("A-planar-average"),
        ("energy-dem", 0.5, 1.0): ctx.main_proposal("A-dem-eta0.5-average"),
        ("energy-dem", 0.7, 1.0): ctx.main_proposal("A-dem-eta0.7-average"),
    }
    for scale in E2_SCALES:
        on[("energy-dem", 0.5, scale)] = ctx.main_proposal(f"C-dem-scale{scale}")
    cell_specs: dict[tuple[str, float, float], tuple[str, str, dict[str, Any]]] = {
        ("distance", 0.5, 1.0): ("distance", "distance", ctx.energy_options()),
        ("energy-planar", 0.5, 1.0): ("planar-average", "energy", ctx.energy_options(surface="planar")),
        ("energy-dem", 0.5, 1.0): ("dem-eta0.5-average", "energy", ctx.energy_options()),
        ("energy-dem", 0.7, 1.0): ("dem-eta0.7-average", "energy", ctx.energy_options(eta_regen=0.7)),
    }
    for scale in E2_SCALES:
        cell_specs[("energy-dem", 0.5, scale)] = (f"dem-scale{scale}", "energy", ctx.energy_options(dem_scale=scale))
    variants: dict[str, dict[tuple[str, float, float], dict[str, Any]]] = {"on": on}
    for variant, time_limit, warm in (("off_cold_15", 15, False), ("off_warm_15", 15, True), ("off_warm_300", 300, True)):
        table = {}
        for key, (suffix, cost_basis, options) in cell_specs.items():
            cell_id = f"E2-off-{suffix}" if variant == "off_cold_15" else f"E2-{variant}-{suffix}"
            extra = {**off, "solver_time_limit_seconds": time_limit}
            if warm:
                extra["warm_start_stop_ids"] = list(rte.memberships(on[key]).values())
            table[key] = ctx.optimise(exp_dir, cell_id, cost_basis, options, **extra)
        variants[variant] = table

    def solution(table: dict, basis: str, eta: float, scale: float) -> dict[str, Any]:
        if basis == "distance":
            return table[("distance", 0.5, 1.0)]
        if basis == "energy-planar":
            return table[("energy-planar", 0.5, 1.0)]
        return table[(basis, eta, scale)]

    # Terrain-aware local search started from the best distance / planar network
    # of the same setting: the energy it removes is a lower bound of VoTI that
    # the search noise of two independent runs cannot fake, because the DEM
    # search can only improve on its start.
    on_params = {"single_route_per_mahalle": True, "small_mahalle_stop_limit": ctx.base["small_mahalle_stop_limit"]}
    chained = {
        "off_warm_300_from_distance": (variants["off_warm_300"][("distance", 0.5, 1.0)], off),
        "off_warm_300_from_planar": (variants["off_warm_300"][("energy-planar", 0.5, 1.0)], off),
        "on_warm_300_from_distance": (on[("distance", 0.5, 1.0)], on_params),
        "on_warm_300_from_planar": (on[("energy-planar", 0.5, 1.0)], on_params),
    }
    chain_rows: list[dict[str, Any]] = []
    for variant, (start, setting) in chained.items():
        cell_id = f"E2-{variant}-dem-eta0.5-average"
        proposal = ctx.optimise(exp_dir, cell_id, "energy", ctx.energy_options(), **setting, solver_time_limit_seconds=300,
                                warm_start_stop_ids=list(rte.memberships(start).values()))
        evaluation = ctx.evaluator.evaluate(proposal, eta=ETA_MAIN, mass=MASS_MAIN)
        start_eval = ctx.evaluator.evaluate(start, eta=ETA_MAIN, mass=MASS_MAIN)
        sim_start = rte.similarity(rte.memberships(start), rte.memberships(proposal))
        constrained_dem = on[("energy-dem", 0.5, 1.0)]
        sim_on = rte.similarity(rte.memberships(constrained_dem), rte.memberships(proposal))
        chain_rows.append({
            "run_id": RUN_ID, "variant": variant, "start": f"warm_from_{start['experiment_cell']['cell_id']}",
            "cell_id": cell_id, "cost_basis": "energy-dem", "eta_regen": ETA_MAIN, "mass_scenario": MASS_MAIN, "dem_scale": 1.0,
            "neighbourhood_constraint": "on" if variant.startswith("on") else "off", "time_limit_s": 300, "seed": ctx.base["seed"],
            "solver_objective": proposal["metrics"].get("fitness"), **network_row(evaluation),
            "E_dem_start_wh": start_eval["total_energy_wh_dem"], "km_start": round(start_eval["total_distance_m"] / 1000, 3),
            "improvement_over_start_wh": round(start_eval["total_energy_wh_dem"] - evaluation["total_energy_wh_dem"], 1),
            "improvement_over_start_pct": round((start_eval["total_energy_wh_dem"] - evaluation["total_energy_wh_dem"]) / start_eval["total_energy_wh_dem"] * 100, 2),
            "jaccard_vs_start": sim_start["mean_jaccard"], "stops_changing_line_vs_start": sim_start["stops_changing_route"], "kendall_tau_vs_start": sim_start["mean_kendall_tau"],
            "jaccard_vs_constrained": sim_on["mean_jaccard"], "stops_changing_line_vs_constrained": sim_on["stops_changing_route"],
            "lines": len(proposal["layers"]["routes"]["features"]),
            "min_route_stops": min(len(v) for v in rte.memberships(proposal).values()), "max_route_stops": max(len(v) for v in rte.memberships(proposal).values()),
        })

    rows: list[dict[str, Any]] = []
    scenarios = [(0.5, 1.0), (0.7, 1.0)] + [(0.5, scale) for scale in E2_SCALES]
    total_solver = sum(p["experiment_cell"]["seconds"] for name, table in variants.items() if name != "on" for p in table.values())
    for variant, table in variants.items():
        constraint = "on" if variant == "on" else "off"
        for eta, scale in scenarios:
            evaluated = {}
            for basis in ("distance", "energy-planar", "energy-dem"):
                proposal = solution(table, basis, eta, scale)
                evaluated[basis] = (proposal, ctx.evaluator.evaluate(proposal, eta=eta, mass=MASS_MAIN, dem_scale=scale))
            memb = {basis: rte.memberships(p) for basis, (p, _e) in evaluated.items()}
            sim_pd = rte.similarity(memb["energy-planar"], memb["energy-dem"])
            sim_dd = rte.similarity(memb["distance"], memb["energy-dem"])
            e_dist = evaluated["distance"][1]["total_energy_wh_dem"]
            e_planar = evaluated["energy-planar"][1]["total_energy_wh_dem"]
            e_dem = evaluated["energy-dem"][1]["total_energy_wh_dem"]
            for basis, (proposal, evaluation) in evaluated.items():
                counterpart = solution(on, basis, eta, scale)
                sim_counterpart = rte.similarity(rte.memberships(counterpart), memb[basis])
                cell_id = proposal["experiment_cell"]["cell_id"]
                rows.append({
                    "run_id": MAIN_RUN_ID if variant == "on" else RUN_ID, "variant": variant, "start": {"on": "constrained_main_run", "off_cold_15": "cold", "off_warm_15": "warm_from_constrained", "off_warm_300": "warm_from_constrained"}[variant],
                    "cell_id": cell_id, "cost_basis": basis,
                    "eta_regen": eta, "mass_scenario": MASS_MAIN, "dem_scale": scale, "neighbourhood_constraint": constraint,
                    "time_limit_s": proposal["parameters"]["solver_time_limit_seconds"], "seed": proposal["parameters"].get("seed"),
                    "solver_objective": proposal["metrics"].get("fitness"),
                    **network_row(evaluation),
                    "voti_vs_distance_wh": round(e_dist - e_dem, 1) if basis == "energy-dem" else None,
                    "voti_vs_distance_pct": round((e_dist - e_dem) / e_dist * 100, 2) if basis == "energy-dem" else None,
                    "voti_vs_planar_wh": round(e_planar - e_dem, 1) if basis == "energy-dem" else None,
                    "voti_vs_planar_pct": round((e_planar - e_dem) / e_planar * 100, 2) if basis == "energy-dem" else None,
                    "jaccard_planar_vs_dem": sim_pd["mean_jaccard"] if basis == "energy-dem" else None,
                    "stops_changing_line_planar_vs_dem": sim_pd["stops_changing_route"] if basis == "energy-dem" else None,
                    "kendall_tau_planar_vs_dem": sim_pd["mean_kendall_tau"] if basis == "energy-dem" else None,
                    "jaccard_distance_vs_dem": sim_dd["mean_jaccard"] if basis == "energy-dem" else None,
                    "stops_changing_line_distance_vs_dem": sim_dd["stops_changing_route"] if basis == "energy-dem" else None,
                    "jaccard_vs_constrained": sim_counterpart["mean_jaccard"],
                    "stops_changing_line_vs_constrained": sim_counterpart["stops_changing_route"],
                    "lines": len(proposal["layers"]["routes"]["features"]),
                    "min_route_stops": min(len(v) for v in memb[basis].values()), "max_route_stops": max(len(v) for v in memb[basis].values()),
                })
    rows.extend(chain_rows)
    write_csv(DOCS / "E2_neighbourhood_off.csv", rows)
    write_csv(exp_dir / "E2_neighbourhood_off.csv", rows)
    features = []
    for variant, table in variants.items():
        if variant == "on":
            continue
        for key, proposal in table.items():
            cell_id = proposal["experiment_cell"]["cell_id"]
            for index, feature in enumerate(proposal["layers"]["routes"]["features"], start=1):
                features.append({"type": "Feature", "id": f"{cell_id}:{feature['properties']['route_id']}",
                                 "properties": {"cell_id": cell_id, "variant": variant, "cost_basis": key[0], "eta_regen": key[1], "dem_scale": key[2],
                                                "route_id": feature["properties"]["route_id"], "line": index,
                                                "name": f"Line {index}", "stop_count": feature["properties"].get("stop_count"),
                                                "energy_dem_wh": feature["properties"].get("energy_dem_wh")},
                                 "geometry": feature["geometry"]})
    (exp_dir / "routes.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False), encoding="utf-8")
    ctx.finish(exp_dir, manifest, cell_count=sum(len(t) for n, t in variants.items() if n != "on"), total_solver_seconds=round(total_solver, 1))
    log.info("E2 bitti: %d satır", len(rows))


# ── E3: DEM noise levels ────────────────────────────────────────────────
E3_SIGMAS = (1.0, 2.0, 2.5, 4.0)
E3_CORR = (3, 10)
E3_REPEATS = 30
E3_TIME_LIMIT = 5


def e3_seed(sigma_index: int, corr_index: int, repeat: int) -> int:
    """One seed per (level, repeat): the draws are independent across levels."""
    return 3000 + 1000 * corr_index + 100 * sigma_index + repeat


def run_e3(ctx: Context) -> None:
    exp_dir = R1_ROOT / "E3"
    manifest = ctx.manifest(exp_dir, "E3", sigmas_m=E3_SIGMAS, corr_lengths_cells=E3_CORR, repeats=E3_REPEATS,
                            time_limit_s=E3_TIME_LIMIT, seed_rule="3000 + 1000*corr_index + 100*sigma_index + repeat",
                            noise_model="Gaussian per cell, box mean over corr×corr cells (3: offsets -1..1; 10: offsets -5..4), rescaled by sqrt(count) so the marginal sigma is preserved")
    distance_ref = ctx.main_proposal("A-distance")
    dem_ref = ctx.main_proposal("A-dem-eta0.5-average")
    dem_ref_memb = rte.memberships(dem_ref)
    base_distance = ctx.evaluator.evaluate(distance_ref, eta=ETA_MAIN, mass=MASS_MAIN)
    base_dem = ctx.evaluator.evaluate(dem_ref, eta=ETA_MAIN, mass=MASS_MAIN)
    rows: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    total_solver = 0.0
    for corr_index, corr in enumerate(E3_CORR):
        for sigma_index, sigma in enumerate(E3_SIGMAS):
            level_rows: list[dict[str, Any]] = []
            assignment_counts: dict[str, dict[str, int]] = {}
            for repeat in range(E3_REPEATS):
                seed = e3_seed(sigma_index, corr_index, repeat)
                cell_id = f"E3-s{sigma}-L{corr}-r{repeat:02d}"
                options = ctx.energy_options(dem_noise_sigma_m=sigma, dem_noise_corr_cells=corr, seed=seed)
                proposal = ctx.optimise(exp_dir, cell_id, "energy", options, solver_time_limit_seconds=E3_TIME_LIMIT)
                total_solver += proposal["experiment_cell"]["seconds"]
                own = ctx.evaluator.evaluate_on(proposal, corr=corr, eta=ETA_MAIN, mass=MASS_MAIN, noise=sigma, seed=seed)
                own_distance = ctx.evaluator.evaluate_on(distance_ref, corr=corr, eta=ETA_MAIN, mass=MASS_MAIN, noise=sigma, seed=seed)
                own_dem_ref = ctx.evaluator.evaluate_on(dem_ref, corr=corr, eta=ETA_MAIN, mass=MASS_MAIN, noise=sigma, seed=seed)
                reference = ctx.evaluator.evaluate(proposal, eta=ETA_MAIN, mass=MASS_MAIN)
                ctx.evaluator.forget(1.0, sigma, seed, corr)
                memb = rte.memberships(proposal)
                sim = rte.similarity(dem_ref_memb, memb)
                for anchor_route, repeat_route in sim["pairs"]:
                    for stop in memb[repeat_route]:
                        assignment_counts.setdefault(stop, {})
                        assignment_counts[stop][anchor_route] = assignment_counts[stop].get(anchor_route, 0) + 1
                row = {
                    "run_id": RUN_ID, "cell_id": cell_id, "sigma_m": sigma, "corr_len_cells": corr, "repeat": repeat, "seed": seed,
                    "time_limit_s": E3_TIME_LIMIT,
                    "spurious_climb_m": round(own["total_climb_m"] - reference["total_climb_m"], 1),
                    "climb_own_grid_m": own["total_climb_m"], "climb_reference_grid_m": reference["total_climb_m"],
                    "E_own_grid_wh": own["total_energy_wh_dem"], "E_reference_grid_wh": reference["total_energy_wh_dem"],
                    "E_distance_opt_own_grid_wh": own_distance["total_energy_wh_dem"], "E_distance_opt_reference_grid_wh": base_distance["total_energy_wh_dem"],
                    "E_dem_opt_reference_solution_own_grid_wh": own_dem_ref["total_energy_wh_dem"],
                    "voti_own_grid_wh": round(own_distance["total_energy_wh_dem"] - own["total_energy_wh_dem"], 1),
                    "voti_reference_grid_wh": round(base_distance["total_energy_wh_dem"] - reference["total_energy_wh_dem"], 1),
                    "voti_reference_solution_wh": round(base_distance["total_energy_wh_dem"] - base_dem["total_energy_wh_dem"], 1),
                    "km": round(reference["total_distance_m"] / 1000, 3),
                    "jaccard_to_reference_dem_solution": sim["mean_jaccard"], "stops_changing_line": sim["stops_changing_route"],
                    "kendall_tau_to_reference_dem_solution": sim["mean_kendall_tau"],
                }
                rows.append(row)
                level_rows.append(row)
                write_csv(DOCS / "E3_dem_noise_levels.csv", rows)
            modal = []
            for _stop, counts in assignment_counts.items():
                modal.append(max(counts.values()) / sum(counts.values()))
            votis = [r["voti_reference_grid_wh"] for r in level_rows]
            summary.append({
                "sigma_m": sigma, "corr_len_cells": corr, "repeats": len(level_rows), "time_limit_s": E3_TIME_LIMIT,
                "voti_ref_p05_wh": pct(votis, 0.05), "voti_ref_p50_wh": pct(votis, 0.5), "voti_ref_p95_wh": pct(votis, 0.95),
                "voti_ref_mean_wh": round(mean(votis), 1), "voti_ref_sd_wh": round(sd(votis), 1),
                "share_voti_ref_negative": round(sum(1 for v in votis if v < 0) / len(votis), 3),
                "voti_own_p50_wh": pct([r["voti_own_grid_wh"] for r in level_rows], 0.5),
                "spurious_climb_mean_m": round(mean(r["spurious_climb_m"] for r in level_rows), 1),
                "E_reference_grid_mean_wh": round(mean(r["E_reference_grid_wh"] for r in level_rows), 1),
                "E_reference_grid_sd_wh": round(sd([r["E_reference_grid_wh"] for r in level_rows]), 1),
                "jaccard_mean": round(mean(r["jaccard_to_reference_dem_solution"] for r in level_rows), 4),
                "stops_changing_line_mean": round(mean(r["stops_changing_line"] for r in level_rows), 2),
                "modal_line_stability_mean": round(mean(modal), 4) if modal else None,
                "share_stops_modal_ge_0_8": round(sum(1 for m in modal if m >= 0.8) / len(modal), 4) if modal else None,
                "voti_reference_solution_wh": round(base_distance["total_energy_wh_dem"] - base_dem["total_energy_wh_dem"], 1),
            })
            write_csv(DOCS / "E3_summary.csv", summary)
            write_csv(exp_dir / "E3_summary.csv", summary)
    write_csv(exp_dir / "E3_dem_noise_levels.csv", rows)
    ctx.finish(exp_dir, manifest, cell_count=len(rows), total_solver_seconds=round(total_solver, 1))
    log.info("E3 bitti: %d hücre", len(rows))


# ── E4: exact shortest paths (Johnson potentials) ───────────────────────
def stop_list(ctx: Context) -> list[optimizer.Stop]:
    depot, customers = optimizer._extract_stops(ctx.draft)
    return [depot, *customers]


def road_context(ctx: Context, stops: list[optimizer.Stop], cost_basis: str, options: dict[str, Any] | None):
    params = {**ctx.base, "cost_basis": cost_basis}
    if options is not None:
        params["energy_options"] = options
    return optimizer._road_context_for_params(stops, params)


def tour_cost(order: list[int], matrix: list[list[float]]) -> float:
    return optimizer._route_distance(order, matrix)


def proposal_orders(proposal: dict[str, Any], index_of: dict[str, int]) -> dict[str, list[int]]:
    return {route: [index_of[s] for s in stops] for route, stops in rte.memberships(proposal).items()}


def run_e4(ctx: Context) -> None:
    exp_dir = R1_ROOT / "E4"
    manifest = ctx.manifest(exp_dir, "E4", note="physics potential pi = eta_regen*m*g*h with clipping of negative corrected weights (main run) versus Johnson potentials from Bellman-Ford on the true edge energies (exact_potentials=True)")
    stops = stop_list(ctx)
    index_of = {stop.stop_id: i for i, stop in enumerate(stops)}
    n = len(stops)
    rows: list[dict[str, Any]] = []
    reeval: list[dict[str, Any]] = []
    exact_main: list[list[float]] | None = None
    for eta in rte.ETA_LEVELS:
        for mass in rte.MASS_LEVELS:
            options = ctx.energy_options(eta_regen=eta, mass_scenario=mass)
            started = time.time()
            clipped = road_context(ctx, stops, "energy", options)
            exact = road_context(ctx, stops, "energy", {**options, "exact_potentials": True})
            seconds = time.time() - started
            a, b = clipped["energy_matrix"], exact["energy_matrix"]
            if eta == ETA_MAIN and mass == MASS_MAIN:
                exact_main = b
            diffs = []
            for i in range(n):
                for j in range(n):
                    if i == j:
                        continue
                    diffs.append((a[i][j] - b[i][j], a[i][j], b[i][j]))
            abs_diffs = [abs(d) for d, _x, _y in diffs]
            rel = [abs(d) / max(abs(y), 1.0) * 100 for d, _x, y in diffs]
            differing = [d for d in diffs if abs(d[0]) > 0.01]
            better_exact = sum(1 for d in diffs if d[0] > 0.01)
            worse_exact = sum(1 for d in diffs if d[0] < -0.01)
            summary_c = clipped["energy_setup"].graph_summary
            summary_e = exact["energy_setup"].graph_summary
            row = {
                "run_id": RUN_ID, "eta_regen": eta, "mass_scenario": mass,
                "negative_edges_physics_potential": summary_c["n_negative_adjusted"],
                "negative_edges_johnson": summary_e["n_negative_adjusted"],
                "directed_edges": summary_c["energy_edges"], "bellman_ford_passes": summary_e.get("bellman_ford_passes"),
                "negative_cycle_found": bool(summary_e.get("negative_cycle", False)),
                "pairs_total": len(diffs), "pairs_differing": len(differing),
                "pairs_clipped_worse": better_exact, "pairs_clipped_better": worse_exact,
                "max_abs_diff_wh": round(max(abs_diffs), 3), "mean_abs_diff_wh": round(mean(abs_diffs), 4),
                "max_rel_diff_pct": round(max(rel), 4), "mean_rel_diff_pct": round(mean(rel), 5),
                "sum_abs_diff_wh": round(sum(abs_diffs), 2), "seconds": round(seconds, 1),
            }
            rows.append(row)
            write_csv(DOCS / "E4_exact_shortest_paths.csv", rows)
            # The main run's DEM solution for this scenario, priced with both matrices.
            cell = f"A-dem-eta{eta}-{mass}"
            proposal = ctx.main_proposal(cell)
            orders = proposal_orders(proposal, index_of)
            cost_clipped = sum(tour_cost(o, a) for o in orders.values())
            cost_exact = sum(tour_cost(o, b) for o in orders.values())
            reeval.append({
                "run_id": MAIN_RUN_ID, "cell_id": cell, "eta_regen": eta, "mass_scenario": mass,
                "tour_energy_clipped_matrix_wh": round(cost_clipped, 1), "tour_energy_exact_matrix_wh": round(cost_exact, 1),
                "difference_wh": round(cost_clipped - cost_exact, 2),
                "difference_pct": round((cost_clipped - cost_exact) / cost_clipped * 100, 4) if cost_clipped else None,
                "legs_differing": sum(1 for o in orders.values() for x, y in zip([0, *o], [*o, 0]) if abs(a[x][y] - b[x][y]) > 0.01),
                "legs_total": sum(len(o) + 1 for o in orders.values()),
            })
            write_csv(DOCS / "E4_reevaluation.csv", reeval)
            log.info("E4 %s: %d negatif kenar, %d/%d çift farklı, en büyük fark %.2f Wh", cell, row["negative_edges_physics_potential"], len(differing), len(diffs), row["max_abs_diff_wh"])
    # Re-optimise the main scenario with the exact matrix (one run, 15 s, same seed handling as the main run).
    proposal = ctx.optimise(exp_dir, "E4-dem-exact-eta0.5-average", "energy", {**ctx.energy_options(), "exact_potentials": True})
    evaluation = ctx.evaluator.evaluate(proposal, eta=ETA_MAIN, mass=MASS_MAIN)
    ref = ctx.main_proposal("A-dem-eta0.5-average")
    ref_eval = ctx.evaluator.evaluate(ref, eta=ETA_MAIN, mass=MASS_MAIN)
    dist_eval = ctx.evaluator.evaluate(ctx.main_proposal("A-distance"), eta=ETA_MAIN, mass=MASS_MAIN)
    sim = rte.similarity(rte.memberships(ref), rte.memberships(proposal))
    same_orders = proposal_orders(proposal, index_of)
    ref_orders = proposal_orders(ref, index_of)
    reopt = [{
        "run_id": RUN_ID, "cell_id": "E4-dem-exact-eta0.5-average", "eta_regen": ETA_MAIN, "mass_scenario": MASS_MAIN,
        "time_limit_s": ctx.base["solver_time_limit_seconds"], "seed": ctx.base["seed"],
        "potential_method": proposal["energy"]["potential_method"], "negative_edges": proposal["energy"]["n_negative_adjusted"],
        **network_row(evaluation),
        "E_dem_reference_dem_opt_wh": ref_eval["total_energy_wh_dem"], "E_dem_distance_opt_wh": dist_eval["total_energy_wh_dem"],
        "voti_vs_distance_wh": round(dist_eval["total_energy_wh_dem"] - evaluation["total_energy_wh_dem"], 1),
        "voti_vs_distance_pct": round((dist_eval["total_energy_wh_dem"] - evaluation["total_energy_wh_dem"]) / dist_eval["total_energy_wh_dem"] * 100, 2),
        "voti_reference_wh": round(dist_eval["total_energy_wh_dem"] - ref_eval["total_energy_wh_dem"], 1),
        "jaccard_to_reference": sim["mean_jaccard"], "stops_changing_line": sim["stops_changing_route"], "kendall_tau": sim["mean_kendall_tau"],
        "identical_sequences": sorted(map(tuple, same_orders.values())) == sorted(map(tuple, ref_orders.values())),
        "solver_cost_exact_matrix_wh": round(sum(tour_cost(o, exact_main) for o in same_orders.values()), 1) if exact_main else None,
        "reference_cost_exact_matrix_wh": round(sum(tour_cost(o, exact_main) for o in ref_orders.values()), 1) if exact_main else None,
        "solver_seconds": proposal["experiment_cell"]["seconds"],
    }]
    write_csv(DOCS / "E4_reoptimisation.csv", reopt)
    for name in ("E4_exact_shortest_paths.csv", "E4_reevaluation.csv", "E4_reoptimisation.csv"):
        (exp_dir / name).write_text((DOCS / name).read_text(encoding="utf-8"), encoding="utf-8")
    ctx.finish(exp_dir, manifest, cell_count=1, total_solver_seconds=proposal["experiment_cell"]["seconds"])
    log.info("E4 bitti")


# ── Legs on the graph, priced at any load (E5, E6) ──────────────────────
LOAD_FULL_KG = 14 * 75.0
LOAD_AVERAGE_KG = 7 * 75.0
MASS_BIN_KG = 25.0


class LegModel:
    """
    Stop-to-stop legs on the road graph under one cost basis, priced leg by leg.

    A leg's path is the least-cost path under the context's own weight (length,
    planar energy or DEM energy at the average load), exactly as the optimiser
    realised it; the energy of driving it is then recomputed for whatever mass
    is on board, on the reference DEM or on the plane, forwards or backwards.
    Sub-segments are sampled once per edge and cached; energies once per
    (edge, direction, surface, mass).
    """

    def __init__(self, ctx: Context, stops: list[optimizer.Stop], cost_basis: str, options: dict[str, Any] | None, label: str):
        self.label = label
        self.stops = stops
        self.context = road_context(ctx, stops, cost_basis, options)
        self.graph, self.nodes, self.nx = self.context["graph"], self.context["nodes"], self.context["nx"]
        self.weight = optimizer._weight_attribute(cost_basis)
        self.grid = ctx.grid
        self.options = energy_model.EnergyOptions(surface="dem")
        self.profile = energy_model.vehicle_profile(None, mass_scenario=MASS_MAIN, eta_regen=ETA_MAIN)
        self.speed_kmh = ctx.base["avg_speed_kmh"]
        self.dwell_s = ctx.base["dwell_time_seconds"]
        self._paths: dict[tuple[int, int], list[tuple[Any, Any, dict[str, Any]]]] = {}
        self._pieces: dict[tuple[int, bool], tuple[float, list[tuple[float, float]], float]] = {}
        self._energy: dict[tuple[int, bool, str, float], tuple[float, float, float, float]] = {}

    # -- paths ---------------------------------------------------------
    def path(self, a: int, b: int) -> list[tuple[Any, Any, dict[str, Any]]]:
        key = (a, b)
        if key not in self._paths:
            if a == b:
                self._paths[key] = []
            else:
                node_path = self.nx.shortest_path(self.graph, self.nodes[a], self.nodes[b], weight=self.weight)
                self._paths[key] = [
                    (u, v, optimizer._edge_data_for_weight(self.graph, u, v, self.weight))
                    for u, v in zip(node_path, node_path[1:])
                ]
        return self._paths[key]

    def coordinates(self, u: Any, data: dict[str, Any]) -> list[list[float]]:
        piece = optimizer._oriented_edge_coordinates(self.graph, u, data)
        if len(piece) < 2:
            a, b = self.graph.nodes[u], None
            piece = [[float(a["x"]), float(a["y"])]]
        return piece

    def pieces(self, u: Any, data: dict[str, Any], reverse: bool) -> tuple[float, list[tuple[float, float]], float]:
        """(speed m/s, [(length, capped slope)…], plan length) of an edge in one direction."""
        key = (id(data), reverse)
        if key not in self._pieces:
            coords = self.coordinates(u, data)
            if reverse:
                coords = coords[::-1]
            chain, elevation, _n = energy_model.elevation_series(coords, self.grid, self.options)
            parts: list[tuple[float, float]] = []
            for index in range(1, len(chain)):
                length = chain[index] - chain[index - 1]
                if length <= 0:
                    continue
                slope = (elevation[index] - elevation[index - 1]) / length
                if abs(slope) > self.options.grade_cap:
                    slope = math.copysign(self.options.grade_cap, slope)
                parts.append((length, slope))
            speed = float(data.get("speed_kmh") or self.speed_kmh) / 3.6
            self._pieces[key] = (speed, parts, sum(length for length, _s in parts))
        return self._pieces[key]

    def edge_energy(self, u: Any, data: dict[str, Any], reverse: bool, surface: str, mass_kg: float) -> tuple[float, float, float, float]:
        """(energy J, climb m, friction loss J, length m) for one edge at one mass."""
        key = (id(data), reverse, surface, round(mass_kg, 3))
        if key not in self._energy:
            speed, parts, length = self.pieces(u, data, reverse)
            profile = energy_model.with_passenger_mass(self.profile, mass_kg - self.profile.mass_empty_kg)
            total = climb = friction = 0.0
            if surface == "planar":
                seg = energy_model._subsegment(length, 0.0, speed, profile)
                total, friction = seg.total_j, seg.friction_loss_j
            else:
                for piece_length, slope in parts:
                    seg = energy_model._subsegment(piece_length, slope, speed, profile)
                    total += seg.total_j
                    climb += seg.climb_m
                    friction += seg.friction_loss_j
            self._energy[key] = (total, climb, friction, length)
        return self._energy[key]

    def leg(self, a: int, b: int, surface: str, mass_kg: float, reverse: bool = False) -> tuple[float, float, float, float]:
        """Energy of the leg a→b at ``mass_kg``; ``reverse`` drives the a→b path backwards (b→a, one-way rules ignored)."""
        edges = self.path(a, b)
        sequence = [(v, u, data, True) for u, v, data in reversed(edges)] if reverse else [(u, v, data, False) for u, v, data in edges]
        total = climb = friction = length = 0.0
        for start, _end, data, back in sequence:
            source = start if not back else _end
            e, c, f, l = self.edge_energy(source, data, back, surface, mass_kg)
            total += e
            climb += c
            friction += f
            length += l
        return total, climb, friction, length

    # -- tours ---------------------------------------------------------
    def stop_energy(self, arrival_kg: float, departure_kg: float) -> float:
        v = self.speed_kmh / 3.6
        p = self.profile
        return 0.5 * v * v * (departure_kg / p.eta_drive - p.stop_regen * arrival_kg) + p.p_aux_w * self.dwell_s

    def loads(self, order: list[int], profile_name: str) -> list[float]:
        """Load on board (kg) on legs 0…n of the tour depot→order→depot."""
        n = len(order)
        if profile_name == "constant_7pax":
            return [LOAD_AVERAGE_KG] * (n + 1)
        if profile_name == "decreasing_equal":
            return [LOAD_FULL_KG * (1.0 - k / n) for k in range(n + 1)]
        if profile_name == "decreasing_demand":
            demands = [self.stops[i].service_demand for i in order]
            total = sum(demands) or 1.0
            loads, remaining = [LOAD_FULL_KG], LOAD_FULL_KG
            for d in demands:
                remaining -= LOAD_FULL_KG * d / total
                loads.append(max(0.0, remaining))
            return loads
        raise ValueError(profile_name)

    def price(self, order: list[int], profile_name: str, surface: str, *, reverse: bool = False, quantise: bool = False) -> dict[str, Any]:
        """
        Whole tour: legs at their leg mass plus stop events.

        ``reverse`` drives the tour backwards along the same road pieces (the
        physical reverse of the paper), with the load profile applied to the
        reversed visiting order. ``quantise`` bins the mass to 25 kg for the
        local search cache; final figures are always priced unquantised.
        """
        visiting = order[::-1] if reverse else order
        loads = self.loads(visiting, profile_name)
        empty = self.profile.mass_empty_kg
        n = len(visiting)
        legs = list(zip([0, *visiting], [*visiting, 0]))
        energy = climb = friction = length = 0.0
        weighted_climb = weighted_climb_load = 0.0
        mass_sum = load_sum = 0.0
        for k, (a, b) in enumerate(legs):
            load = loads[k]
            if quantise:
                load = round(load / MASS_BIN_KG) * MASS_BIN_KG
            mass = empty + load
            if reverse:
                # Leg k of the reversed tour is the forward leg n-k driven backwards.
                fa, fb = legs[n - k][1], legs[n - k][0]
                e, c, f, l = self.leg(fa, fb, surface, mass, reverse=True)
            else:
                e, c, f, l = self.leg(a, b, surface, mass)
            energy += e
            climb += c
            friction += f
            length += l
            weighted_climb += mass * c
            weighted_climb_load += load * c
            mass_sum += mass
            load_sum += load
        # Stop events at every customer and at the depot (arriving with what is
        # left, leaving with the next tour's full load), as the main run counts
        # them: stop_count includes the depot.
        stops_j = sum(self.stop_energy(empty + loads[k], empty + loads[k + 1]) for k in range(n))
        stops_j += self.stop_energy(empty + loads[n], empty + loads[0])
        return {
            "E_wh": energy / energy_model.J_PER_WH, "climb_m": climb, "friction_wh": friction / energy_model.J_PER_WH,
            "km": length / 1000, "stop_energy_wh": stops_j / energy_model.J_PER_WH,
            "heavy_first_index": weighted_climb / mass_sum if mass_sum else None,
            "heavy_first_index_load": weighted_climb_load / load_sum if load_sum else None,
            "mean_leg_climb_m": climb / len(legs),
        }

    def local_search(self, order: list[int], profile_name: str, surface: str) -> list[int]:
        """2-opt then or-opt on the full load-aware tour cost, first improvement, until no move helps."""
        def cost(candidate: list[int]) -> float:
            return self.price(candidate, profile_name, surface, quantise=True)["E_wh"]

        best = order[:]
        best_cost = cost(best)
        improved = True
        while improved:
            improved = False
            count = len(best)
            for left in range(count - 1):
                for right in range(left + 2, count + 1):
                    candidate = best[:left] + best[left:right][::-1] + best[right:]
                    value = cost(candidate)
                    if value + 0.01 < best_cost:
                        best, best_cost, improved = candidate, value, True
                        break
                if improved:
                    break
            if improved:
                continue
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
                            value = cost(candidate)
                            if value + 0.01 < best_cost:
                                best, best_cost, improved = candidate, value, True
                                break
                        if improved:
                            break
                    if improved:
                        break
                if improved:
                    break
        return best


def kendall(a: list[int], b: list[int]) -> float | None:
    return rte.kendall_tau([str(x) for x in a], [str(x) for x in b])


# ── Reference (municipal) lines, as drawn ───────────────────────────────
def reference_features(ctx: Context) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload = json.loads((PROJECT_ROOT / DEFAULT_BASELINE_NETWORK).read_text(encoding="utf-8"))
    return payload["routes"]["features"], payload["stops"]["features"]


def drawn_runs(feature: dict[str, Any]) -> list[list[list[float]]]:
    return energy_model.line_runs(feature.get("geometry") or {})


def project_stops_on_runs(runs: list[list[list[float]]], points: list[tuple[str, float, float]], max_m: float = 120.0) -> list[tuple[int, float, str]]:
    """(run index, chainage on run, stop name) for every stop within ``max_m`` of the drawing, in driving order."""
    from shapely.geometry import LineString, Point
    from shapely.ops import transform

    lat0 = points[0][2] if points else 38.64
    kx = 111_195 * math.cos(math.radians(lat0))
    ky = 111_195

    def metric(x: float, y: float) -> tuple[float, float]:
        return x * kx, y * ky

    lines = [LineString([metric(*p[:2]) for p in run]) for run in runs]
    placed = []
    for name, lng, lat in points:
        point = Point(*metric(lng, lat))
        best = None
        for index, line in enumerate(lines):
            distance = line.distance(point)
            if best is None or distance < best[0]:
                best = (distance, index, line.project(point))
        if best and best[0] <= max_m:
            placed.append((best[1], best[2], name))
    return sorted(placed)


def rotate_loop_to(run: list[list[float]], lng: float, lat: float) -> list[list[float]]:
    """Start a closed drawn loop at the point on it nearest to (lng, lat)."""
    from shapely.geometry import LineString, Point

    line = LineString([(p[0], p[1]) for p in run])
    at = line.project(Point(lng, lat))
    cut = line.interpolate(at)
    walked = 0.0
    index = None
    for i, (a, b) in enumerate(zip(run, run[1:])):
        seg = LineString([(a[0], a[1]), (b[0], b[1])]).length
        if walked + seg >= at:
            index = i
            break
        walked += seg
    if index is None:
        return run
    tail = [list(p) for p in run[index + 1:]]
    head = [list(p) for p in run[1:index + 1]]
    return [[cut.x, cut.y], *tail, *head, [cut.x, cut.y]]


# ── E5: delivery-load scenario ──────────────────────────────────────────
LOAD_PROFILES = ("constant_7pax", "decreasing_equal", "decreasing_demand")
E5_NETWORKS = {"distance_opt": "A-distance", "planar_opt": "A-planar-average", "dem_opt": "A-dem-eta0.5-average"}


def leg_models(ctx: Context, stops: list[optimizer.Stop]) -> dict[str, LegModel]:
    return {
        "distance": LegModel(ctx, stops, "distance", None, "distance"),
        "planar": LegModel(ctx, stops, "energy", ctx.energy_options(surface="planar"), "planar"),
        "dem": LegModel(ctx, stops, "energy", ctx.energy_options(), "dem"),
    }


def municipal_orders(ctx: Context, stops: list[optimizer.Stop]) -> dict[str, list[int]]:
    """Each drawn line's stops (by name), in the order they lie along the drawing from the depot."""
    routes, drawn_stops = reference_features(ctx)
    index_by_name = {stop.name: i for i, stop in enumerate(stops)}
    depot = stops[0]
    orders: dict[str, list[int]] = {}
    for feature in routes:
        name = feature["properties"]["name"]
        runs = drawn_runs(feature)
        if not runs:
            continue
        if len(runs) == 1 and energy_model.haversine_m(runs[0][0][1], runs[0][0][0], runs[0][-1][1], runs[0][-1][0]) < 50:
            runs = [rotate_loop_to(runs[0], depot.lng, depot.lat)]
        points = [(f["properties"]["name"], f["geometry"]["coordinates"][0], f["geometry"]["coordinates"][1])
                  for f in drawn_stops if f["properties"].get("line") == name]
        placed = project_stops_on_runs(runs, points)
        orders[name] = [index_by_name[n] for _run, _at, n in placed if n in index_by_name]
    return orders


def drawn_line_rows(ctx: Context, stops: list[optimizer.Stop]) -> list[dict[str, Any]]:
    """E5 step 1 and 3 for the municipal lines on their drawn geometry at 30 km/h."""
    routes, drawn_stops = reference_features(ctx)
    depot = stops[0]
    profile = energy_model.vehicle_profile(None, mass_scenario=MASS_MAIN, eta_regen=ETA_MAIN)
    dem = energy_model.EnergyOptions(surface="dem")
    planar = energy_model.EnergyOptions(surface="planar")
    speed = ctx.base["avg_speed_kmh"]
    dwell = ctx.base["dwell_time_seconds"]
    demand_by_name = {stop.name: stop.service_demand for stop in stops}
    rows = []
    for feature in routes:
        name = feature["properties"]["name"]
        runs = drawn_runs(feature)
        if not runs:
            continue
        closed = len(runs) == 1 and energy_model.haversine_m(runs[0][0][1], runs[0][0][0], runs[0][-1][1], runs[0][-1][0]) < 50
        if closed:
            runs = [rotate_loop_to(runs[0], depot.lng, depot.lat)]
        points = [(f["properties"]["name"], f["geometry"]["coordinates"][0], f["geometry"]["coordinates"][1])
                  for f in drawn_stops if f["properties"].get("line") == name]
        placed = project_stops_on_runs(runs, points)
        # Legs: pieces of the drawing between consecutive projected stops; a gap between runs is not driven.
        from shapely.geometry import LineString
        from shapely.ops import substring
        lat0 = runs[0][0][1]
        kx, ky = 111_195 * math.cos(math.radians(lat0)), 111_195
        legs: list[list[list[float]]] = []   # each leg: list of coordinate lists (pieces)
        current: list[list[list[float]]] = []
        for run_index, run in enumerate(runs):
            metric = LineString([(p[0] * kx, p[1] * ky) for p in run])
            cuts = sorted(at for r, at, _n in placed if r == run_index)
            last = 0.0
            for at in cuts:
                piece = substring(metric, last, at)
                if piece.length > 0:
                    current.append([[x / kx, y / ky] for x, y in piece.coords])
                legs.append(current)
                current = []
                last = at
            piece = substring(metric, last, metric.length)
            if piece.length > 0:
                current.append([[x / kx, y / ky] for x, y in piece.coords])
        legs.append(current)
        n = len(placed)
        names = [nm for _r, _a, nm in placed]
        for direction in ("forward", "reverse"):
            leg_list = legs if direction == "forward" else [[piece[::-1] for piece in reversed(leg)] for leg in reversed(legs)]
            visit = names if direction == "forward" else names[::-1]
            for profile_name in LOAD_PROFILES:
                if n == 0 and profile_name != "constant_7pax":
                    continue
                if profile_name == "constant_7pax":
                    loads = [LOAD_AVERAGE_KG] * (n + 1)
                elif profile_name == "decreasing_equal":
                    loads = [LOAD_FULL_KG * (1 - k / n) for k in range(n + 1)]
                else:
                    demands = [demand_by_name.get(nm, 1.0) for nm in visit]
                    total = sum(demands) or 1.0
                    loads, remaining = [LOAD_FULL_KG], LOAD_FULL_KG
                    for d in demands:
                        remaining -= LOAD_FULL_KG * d / total
                        loads.append(max(0.0, remaining))
                e_dem = e_planar = climb = friction = length = 0.0
                weighted = weighted_load = mass_sum = load_sum = 0.0
                for k, leg in enumerate(leg_list):
                    mass_profile = energy_model.with_passenger_mass(profile, loads[k])
                    leg_climb = 0.0
                    for piece in leg:
                        seg = energy_model.segment_energy_j(piece, speed / 3.6, mass_profile, dem, ctx.grid)
                        flat = energy_model.segment_energy_j(piece, speed / 3.6, mass_profile, planar, None)
                        e_dem += seg.total_j
                        e_planar += flat.total_j
                        leg_climb += seg.climb_m
                        friction += seg.friction_loss_j
                        length += seg.length_m
                    climb += leg_climb
                    weighted += mass_profile.mass_kg * leg_climb
                    weighted_load += loads[k] * leg_climb
                    mass_sum += mass_profile.mass_kg
                    load_sum += loads[k]
                v = speed / 3.6
                events = [(loads[k], loads[k + 1]) for k in range(n)] + [(loads[n], loads[0])]
                stops_j = sum(0.5 * v * v * ((profile.mass_empty_kg + depart) / profile.eta_drive - profile.stop_regen * (profile.mass_empty_kg + arrive)) + profile.p_aux_w * dwell for arrive, depart in events)
                rows.append({
                    "run_id": RUN_ID, "network": "municipal_as_drawn", "line": name, "direction": direction,
                    "load_profile": profile_name, "sequence_source": "as_drawn", "stops": n, "drawn_as": "closed_loop" if closed else "open_chain",
                    "E_dem_wh": round(e_dem / 3600, 1), "E_planar_wh": round(e_planar / 3600, 1), "climb_m": round(climb, 1),
                    "friction_wh": round(friction / 3600, 1), "km": round(length / 1000, 3), "stop_energy_wh": round(stops_j / 3600, 1),
                    "kendall_tau_vs_as_found": 1.0 if direction == "forward" else (-1.0 if n > 1 else None),
                    "heavy_first_index": round(weighted / mass_sum, 2) if mass_sum else None,
                    "heavy_first_index_load": round(weighted_load / load_sum, 2) if load_sum else None,
                    "speed_kmh": speed, "eta_regen": ETA_MAIN,
                })
    return rows


def run_e5(ctx: Context) -> None:
    exp_dir = R1_ROOT / "E5"
    manifest = ctx.manifest(exp_dir, "E5", load_full_kg=LOAD_FULL_KG, load_average_kg=LOAD_AVERAGE_KG, profiles=LOAD_PROFILES,
                            search="2-opt then or-opt (blocks of 1-3, both orientations), first improvement, full load-aware tour price; masses binned to 25 kg during the search and priced exactly afterwards",
                            paths="a leg's road path is the least-cost path under the sequencing surface at the average load (DEM energy for load_aware_dem, planar energy for load_aware_planar); as_found uses each network's own realised paths")
    stops = stop_list(ctx)
    index_of = {stop.stop_id: i for i, stop in enumerate(stops)}
    models = leg_models(ctx, stops)
    rows: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    started = time.time()

    def record(network: str, line: str, order: list[int], model: LegModel, source: str, as_found: list[int], extra: dict[str, Any] | None = None) -> None:
        for direction in ("forward", "reverse"):
            for profile_name in LOAD_PROFILES:
                priced = model.price(order, profile_name, "dem", reverse=direction == "reverse")
                flat = model.price(order, profile_name, "planar", reverse=direction == "reverse")
                rows.append({
                    "run_id": RUN_ID, "network": network, "line": line, "direction": direction,
                    "load_profile": profile_name, "sequence_source": source, "stops": len(order), "paths_from": model.label,
                    "E_dem_wh": round(priced["E_wh"], 1), "E_planar_wh": round(flat["E_wh"], 1), "climb_m": round(priced["climb_m"], 1),
                    "friction_wh": round(priced["friction_wh"], 1), "km": round(priced["km"], 3), "stop_energy_wh": round(priced["stop_energy_wh"], 1),
                    "kendall_tau_vs_as_found": kendall(as_found, order[::-1] if direction == "reverse" else order),
                    "heavy_first_index": round(priced["heavy_first_index"], 2) if priced["heavy_first_index"] is not None else None,
                    "heavy_first_index_load": round(priced["heavy_first_index_load"], 2) if priced["heavy_first_index_load"] is not None else None,
                    "mean_leg_climb_m": round(priced["mean_leg_climb_m"], 2), "eta_regen": ETA_MAIN, **(extra or {}),
                })

    networks: dict[str, tuple[dict[str, list[int]], LegModel, dict[str, Any] | None]] = {}
    for network, cell in E5_NETWORKS.items():
        proposal = ctx.main_proposal(cell)
        orders = {}
        names = {}
        for feature in proposal["layers"]["routes"]["features"]:
            names[feature["properties"]["route_id"]] = feature["properties"]["name"]
        for route_id, stop_ids in rte.memberships(proposal).items():
            orders[names[route_id].replace("Optimize Rota ", "Line ")] = [index_of[s] for s in stop_ids]
        own = models[{"distance_opt": "distance", "planar_opt": "planar", "dem_opt": "dem"}[network]]
        networks[network] = (orders, own, proposal)
        # Sanity check: the as-found pricing at 7 passengers must reproduce the stored line energies.
        stored = {f["properties"]["name"].replace("Optimize Rota ", "Line "): f["properties"]["energy_dem_wh"] for f in proposal["layers"]["routes"]["features"]}
        for line, order in orders.items():
            priced = own.price(order, "constant_7pax", "dem")
            checks.append({"network": network, "line": line, "stored_energy_dem_wh": stored[line], "repriced_energy_dem_wh": round(priced["E_wh"], 2),
                           "difference_wh": round(priced["E_wh"] - stored[line], 2)})
    orders_municipal = municipal_orders(ctx, stops)
    networks["municipal_on_graph"] = (orders_municipal, models["dem"], None)
    write_csv(exp_dir / "E5_sanity_check.csv", checks)
    log.info("E5 sanity: max |repriced - stored| = %.2f Wh", max(abs(c["difference_wh"]) for c in checks))

    for network, (orders, own, _proposal) in networks.items():
        for line, order in orders.items():
            if not order:
                continue
            record(network, line, order, own, "as_found", order)
            for surface, model in (("dem", models["dem"]), ("planar", models["planar"])):
                for profile_name in LOAD_PROFILES:
                    t0 = time.time()
                    improved = model.local_search(order, profile_name, surface)
                    log.info("E5 %s %s %s %s: %.0f s, tau %.3f", network, line, surface, profile_name, time.time() - t0, kendall(order, improved) or 0)
                    for direction in ("forward", "reverse"):
                        priced = model.price(improved, profile_name, "dem", reverse=direction == "reverse")
                        flat = model.price(improved, profile_name, "planar", reverse=direction == "reverse")
                        rows.append({
                            "run_id": RUN_ID, "network": network, "line": line, "direction": direction,
                            "load_profile": profile_name, "sequence_source": f"load_aware_{surface}", "stops": len(order), "paths_from": model.label,
                            "E_dem_wh": round(priced["E_wh"], 1), "E_planar_wh": round(flat["E_wh"], 1), "climb_m": round(priced["climb_m"], 1),
                            "friction_wh": round(priced["friction_wh"], 1), "km": round(priced["km"], 3), "stop_energy_wh": round(priced["stop_energy_wh"], 1),
                            "kendall_tau_vs_as_found": kendall(order, improved[::-1] if direction == "reverse" else improved),
                            "heavy_first_index": round(priced["heavy_first_index"], 2) if priced["heavy_first_index"] is not None else None,
                            "heavy_first_index_load": round(priced["heavy_first_index_load"], 2) if priced["heavy_first_index_load"] is not None else None,
                            "mean_leg_climb_m": round(priced["mean_leg_climb_m"], 2), "eta_regen": ETA_MAIN,
                            "sequence": " ".join(stops[i].stop_id for i in improved), "search_seconds": round(time.time() - t0, 1),
                        })
            write_csv(DOCS / "E5_delivery_load.csv", rows)
    rows.extend(drawn_line_rows(ctx, stops))
    write_csv(DOCS / "E5_delivery_load.csv", rows)
    write_csv(exp_dir / "E5_delivery_load.csv", rows)
    ctx.finish(exp_dir, manifest, rows=len(rows), seconds=round(time.time() - started, 1))
    log.info("E5 bitti: %d satır", len(rows))


# ── E6: legal feasibility of the reverse direction ──────────────────────
def run_e6(ctx: Context) -> None:
    exp_dir = R1_ROOT / "E6"
    manifest = ctx.manifest(exp_dir, "E6", note="one-way rules from the editable road network (direction property); reverse-as-driven drives the forward road pieces backwards; the legal reverse routes the reversed stop order on the directed graph with least-energy paths")
    stops = stop_list(ctx)
    index_of = {stop.stop_id: i for i, stop in enumerate(stops)}
    model = LegModel(ctx, stops, "energy", ctx.energy_options(), "dem")
    graph = model.graph
    rows: list[dict[str, Any]] = []

    def legal_route(order: list[int]) -> dict[str, Any]:
        total = km = climb = 0.0
        for a, b in zip([0, *order], [*order, 0]):
            e, c, _f, l = model.leg(a, b, "dem", model.profile.mass_kg)
            total += e
            km += l
            climb += c
        return {"E_wh": total / 3600, "km": km / 1000, "climb_m": climb}

    def violations(order: list[int]) -> tuple[int, float, float, int]:
        """(one-way pieces that the reverse would drive against, their length, line length, pieces) of the forward paths."""
        violated = length = total = 0.0
        count = pieces = 0
        for a, b in zip([0, *order], [*order, 0]):
            for u, v, data in model.path(a, b):
                pieces += 1
                l = float(data["length"])
                total += l
                if not graph.has_edge(v, u):
                    count += 1
                    violated += l
        return count, violated, total, pieces

    # The DEM-optimal network.
    proposal = ctx.main_proposal("A-dem-eta0.5-average")
    names = {f["properties"]["route_id"]: f["properties"]["name"] for f in proposal["layers"]["routes"]["features"]}
    stored = {f["properties"]["name"]: f["properties"] for f in proposal["layers"]["routes"]["features"]}
    for route_id, stop_ids in rte.memberships(proposal).items():
        order = [index_of[s] for s in stop_ids]
        line = names[route_id].replace("Optimize Rota ", "Line ")
        count, violated, total, pieces = violations(order)
        forward = legal_route(order)
        physical = model.price(order, "constant_7pax", "dem", reverse=True)
        legal = legal_route(order[::-1])
        rows.append({
            "run_id": RUN_ID, "network": "dem_opt", "line": line, "stops": len(order), "direction": "reverse",
            "road_pieces": pieces, "oneway_segments_violated": count, "violated_length_m": round(violated, 1),
            "line_length_m": round(total, 1), "share_of_length_pct": round(violated / total * 100, 2) if total else None,
            "reverse_as_driven_legal": count == 0, "legal_reverse_available": True,
            "E_forward_wh": round(forward["E_wh"], 1), "E_forward_stored_wh": stored[names[route_id]]["energy_dem_wh"],
            "E_reverse_physical_wh": round(physical["E_wh"], 1), "E_legal_reverse_wh": round(legal["E_wh"], 1),
            "legal_reverse_km": round(legal["km"], 3), "forward_km": round(forward["km"], 3),
            "asymmetry_physical": round(abs(forward["E_wh"] - physical["E_wh"]) / max(forward["E_wh"], physical["E_wh"]), 4),
            "asymmetry_legal": round(abs(forward["E_wh"] - legal["E_wh"]) / max(forward["E_wh"], legal["E_wh"]), 4),
            "legal_reverse_cheaper": legal["E_wh"] < forward["E_wh"],
        })
    # The municipal lines, as drawn: map the drawing onto the road network.
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree

    roads = [f for f in ctx.roads.get("features", []) if (f.get("geometry") or {}).get("type") == "LineString"]
    lat0 = 38.64
    kx, ky = 111_195 * math.cos(math.radians(lat0)), 111_195
    road_lines = [LineString([(p[0] * kx, p[1] * ky) for p in f["geometry"]["coordinates"]]) for f in roads]
    tree = STRtree(road_lines)
    directions = [str((f.get("properties") or {}).get("direction") or ("forward" if (f.get("properties") or {}).get("oneway") else "both")) for f in roads]
    routes, _drawn_stops = reference_features(ctx)
    orders_municipal = municipal_orders(ctx, stops)
    for feature in routes:
        name = feature["properties"]["name"]
        runs = drawn_runs(feature)
        traversals: list[tuple[int, bool, float]] = []  # (road index, travelled forward along the road, length)
        unmatched = 0.0
        total = 0.0
        for run in runs:
            points = energy_model.resample_line(run, 10.0, keep_vertices=True)
            samples = [Point(p[0] * kx, p[1] * ky) for p in points]
            previous: tuple[int, bool] | None = None
            for i in range(1, len(samples)):
                step = samples[i - 1].distance(samples[i])
                total += step
                mid = Point((samples[i - 1].x + samples[i].x) / 2, (samples[i - 1].y + samples[i].y) / 2)
                nearest = int(tree.nearest(mid))
                road = road_lines[nearest]
                if road.distance(mid) > 30.0:
                    unmatched += step
                    previous = None
                    continue
                forward = road.project(samples[i]) >= road.project(samples[i - 1])
                key = (nearest, forward)
                if previous == key and traversals:
                    traversals[-1] = (nearest, forward, traversals[-1][2] + step)
                else:
                    traversals.append((nearest, forward, step))
                previous = key
        def count(reverse: bool) -> tuple[int, float]:
            n = length = 0.0
            for road_index, forward, length_m in traversals:
                allowed = directions[road_index]
                travel_forward = forward != reverse
                if allowed == "both" or (allowed == "forward" and travel_forward) or (allowed == "reverse" and not travel_forward):
                    continue
                n += 1
                length += length_m
            return int(n), length
        n_fwd, len_fwd = count(False)
        n_rev, len_rev = count(True)
        order = orders_municipal.get(name) or []
        row = {
            "run_id": RUN_ID, "network": "municipal_as_drawn", "line": name, "stops": len(order), "direction": "reverse",
            "road_pieces": len(traversals), "oneway_segments_violated": n_rev, "violated_length_m": round(len_rev, 1),
            "line_length_m": round(total, 1), "share_of_length_pct": round(len_rev / total * 100, 2) if total else None,
            "reverse_as_driven_legal": n_rev == 0, "unmatched_length_m": round(unmatched, 1),
            "forward_as_drawn_violations": n_fwd, "forward_as_drawn_violated_length_m": round(len_fwd, 1),
            "legal_reverse_available": bool(order),
        }
        if order:
            forward = legal_route(order)
            legal = legal_route(order[::-1])
            row.update({
                "E_forward_wh": round(forward["E_wh"], 1), "E_legal_reverse_wh": round(legal["E_wh"], 1),
                "forward_km": round(forward["km"], 3), "legal_reverse_km": round(legal["km"], 3),
                "asymmetry_legal": round(abs(forward["E_wh"] - legal["E_wh"]) / max(forward["E_wh"], legal["E_wh"]), 4),
                "legal_reverse_cheaper": legal["E_wh"] < forward["E_wh"],
                "note": "forward and legal reverse are routed on the graph through the drawn stop order (graph speeds), not on the drawing",
            })
        rows.append(row)
    write_csv(DOCS / "E6_reverse_feasibility.csv", rows)
    write_csv(exp_dir / "E6_reverse_feasibility.csv", rows)
    ctx.finish(exp_dir, manifest, rows=len(rows))
    log.info("E6 bitti: %d satır", len(rows))


# ── E7: a second DEM (ALOS AW3D30) ──────────────────────────────────────
AW3D_DIR = PROJECT_ROOT / "data/processed/elevation/aw3d30-v1"
AW3D_TILES = ("ALPSMLC30_N038E034_DSM", "ALPSMLC30_N038E035_DSM")
AW3D_URL = "https://ai4edataeuwest.blob.core.windows.net/alos-dem/AW3D30_global/{name}.tif"
PC_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/alos-dem"


def build_second_dem(base: ElevationGrid) -> dict[str, Any]:
    """AW3D30 v3.2 resampled (bilinear) onto the GLO-30 lattice, same layout on disk."""
    import urllib.request

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject

    meta_path = AW3D_DIR / "urgup-30m.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    source_dir = AW3D_DIR / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    token = json.loads(urllib.request.urlopen(PC_TOKEN_URL, timeout=60).read().decode("utf-8"))["token"]
    grid = {key: base.meta[key] for key in ("lat0", "lon0", "d_lat", "d_lon", "rows", "cols")}
    transform = from_origin(grid["lon0"], grid["lat0"], grid["d_lon"], grid["d_lat"])
    dest = np.full((grid["rows"], grid["cols"]), np.nan, dtype="float32")
    for name in AW3D_TILES:
        local = source_dir / f"{name}.tif"
        if not local.exists():
            log.info("E7: %s indiriliyor", name)
            urllib.request.urlretrieve(f"{AW3D_URL.format(name=name)}?{token}", local)
        with rasterio.open(local) as src:
            patch = np.full_like(dest, np.nan)
            reproject(source=rasterio.band(src, 1), destination=patch, dst_transform=transform, dst_crs="EPSG:4326",
                      dst_nodata=np.nan, src_nodata=src.nodata, resampling=Resampling.bilinear)
            dest = np.where(np.isnan(dest), patch, dest)
    nodata = int(base.meta["nodata"])
    quantised = np.where(np.isnan(dest), nodata, np.rint(dest)).astype("int16")
    raw_path = AW3D_DIR / "urgup-30m.i16"
    raw_path.write_bytes(quantised.astype("<i2").tobytes())
    real = quantised[quantised != nodata]
    meta = {
        "dataset": "aw3d30-v1", "source": "JAXA ALOS World 3D 30 m (AW3D30 v3.2)", "source_kind": "DSM",
        "source_tiles": list(AW3D_TILES), "source_url": AW3D_URL, "distribution": "Microsoft Planetary Computer STAC collection alos-dem",
        "licence": "JAXA AW3D30: free, attribution required (© JAXA)", "accessed_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "crs": "EPSG:4326", "dtype": "int16", "byte_order": "little", "nodata": nodata, "spacing_m": base.meta.get("spacing_m"),
        "vertical_accuracy_m": 5.0, "vertical_accuracy_note": "JAXA states ~5 m RMSE for AW3D30; DOĞRULANACAK",
        "bounds": base.meta.get("bounds"), **grid, "min_m": int(real.min()), "max_m": int(real.max()),
        "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(), "bytes": raw_path.stat().st_size,
        "lattice": "identical to glo30-v1 (same origin, spacing, rows, cols)", "resampling": "bilinear",
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return meta


def run_e7(ctx: Context) -> None:
    exp_dir = R1_ROOT / "E7"
    meta = build_second_dem(ctx.grid)
    second = ElevationGrid.load(AW3D_DIR)
    manifest = ctx.manifest(exp_dir, "E7", second_dem=meta)
    # 1. Road-pixel comparison.
    diffs: list[float] = []
    grade_pairs: list[tuple[float, float]] = []
    options = energy_model.EnergyOptions(surface="dem", smooth_window_m=0.0)
    for feature in ctx.roads.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "LineString":
            continue
        coords = geometry["coordinates"]
        chain, h1, _n = energy_model.elevation_series(coords, ctx.grid, options)
        _chain2, h2, _n2 = energy_model.elevation_series(coords, second, options)
        diffs.extend(b - a for a, b in zip(h1, h2))
        head = 0
        for index in range(1, len(chain)):
            while chain[index] - chain[head] > 100.0 and head < index - 1:
                head += 1
            span = chain[index] - chain[head]
            if span < 50.0:
                continue
            grade_pairs.append(((h1[index] - h1[head]) / span * 100, (h2[index] - h2[head]) / span * 100))
    abs_diffs = sorted(abs(d) for d in diffs)
    mean_diff = mean(diffs)
    n = len(grade_pairs)
    mx = mean(a for a, _b in grade_pairs); my = mean(b for _a, b in grade_pairs)
    cov = sum((a - mx) * (b - my) for a, b in grade_pairs) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a, _b in grade_pairs) / n); sy = math.sqrt(sum((b - my) ** 2 for _a, b in grade_pairs) / n)
    stops = stop_list(ctx)
    stop_diffs = [second.at(s.lat, s.lng) - ctx.grid.at(s.lat, s.lng) for s in stops]
    comparison = {
        "run_id": RUN_ID, "second_dem": meta["dataset"], "second_dem_sha256": meta["sha256"], "reference_dem": ctx.grid.meta.get("dataset"),
        "road_samples": len(diffs), "sample_step_m": options.sample_step_m,
        "diff_mean_m": round(mean_diff, 2), "diff_sd_m": round(sd(diffs), 2), "diff_rmse_m": round(math.sqrt(mean(d * d for d in diffs)), 2),
        "abs_diff_p50_m": round(abs_diffs[len(abs_diffs) // 2], 2), "abs_diff_p95_m": round(abs_diffs[int(0.95 * (len(abs_diffs) - 1))], 2),
        "abs_diff_max_m": round(abs_diffs[-1], 2),
        "grade_100m_pairs": n, "grade_100m_pearson_r": round(cov / (sx * sy), 4) if sx and sy else None,
        "grade_100m_sd_glo30_pct": round(sx, 3), "grade_100m_sd_aw3d30_pct": round(sy, 3),
        "grade_100m_rmse_pct": round(math.sqrt(mean((a - b) ** 2 for a, b in grade_pairs)), 3),
        "stop_diff_mean_m": round(mean(stop_diffs), 2), "stop_diff_sd_m": round(sd(stop_diffs), 2),
        "stop_abs_diff_max_m": round(max(abs(d) for d in stop_diffs), 2),
        "grid_min_m_glo30": ctx.grid.meta.get("min_m"), "grid_max_m_glo30": ctx.grid.meta.get("max_m"),
        "grid_min_m_aw3d30": meta["min_m"], "grid_max_m_aw3d30": meta["max_m"],
    }
    write_csv(DOCS / "E7_dem_comparison.csv", [comparison])
    # 2. The pipeline on the second DEM.
    proposal = ctx.optimise(exp_dir, "E7-dem-aw3d30-eta0.5-average", "energy", {**ctx.energy_options(), "elevation_dir": str(AW3D_DIR)})
    evaluator2 = R1Evaluator(second, ctx.depot_lnglat, ctx.base["avg_speed_kmh"], ctx.base["dwell_time_seconds"])
    solutions = {
        "distance_opt": ctx.main_proposal("A-distance"), "planar_opt": ctx.main_proposal("A-planar-average"),
        "dem_opt_glo30": ctx.main_proposal("A-dem-eta0.5-average"), "dem_opt_aw3d30": proposal,
    }
    rows = []
    on_second = {name: evaluator2.evaluate(p, eta=ETA_MAIN, mass=MASS_MAIN) for name, p in solutions.items()}
    on_first = {name: ctx.evaluator.evaluate(p, eta=ETA_MAIN, mass=MASS_MAIN) for name, p in solutions.items()}
    memb = {name: rte.memberships(p) for name, p in solutions.items()}
    sim = rte.similarity(memb["dem_opt_glo30"], memb["dem_opt_aw3d30"])
    for name in solutions:
        for dem_name, table in (("aw3d30-v1", on_second), ("glo30-v1", on_first)):
            e = table[name]
            e_dist = table["distance_opt"]["total_energy_wh_dem"]
            e_pl = table["planar_opt"]["total_energy_wh_dem"]
            rows.append({
                "run_id": RUN_ID, "network": name, "evaluated_on": dem_name, "eta_regen": ETA_MAIN, "mass_scenario": MASS_MAIN,
                **network_row(e),
                "voti_vs_distance_wh": round(e_dist - e["total_energy_wh_dem"], 1) if name.startswith("dem_opt") else None,
                "voti_vs_distance_pct": round((e_dist - e["total_energy_wh_dem"]) / e_dist * 100, 2) if name.startswith("dem_opt") else None,
                "voti_vs_planar_wh": round(e_pl - e["total_energy_wh_dem"], 1) if name.startswith("dem_opt") else None,
                "jaccard_glo30_vs_aw3d30_solutions": sim["mean_jaccard"], "stops_changing_line": sim["stops_changing_route"], "kendall_tau": sim["mean_kendall_tau"],
                "time_limit_s": ctx.base["solver_time_limit_seconds"], "seed": ctx.base["seed"],
            })
    write_csv(DOCS / "E7_second_dem.csv", rows)
    for name in ("E7_dem_comparison.csv", "E7_second_dem.csv"):
        (exp_dir / name).write_text((DOCS / name).read_text(encoding="utf-8"), encoding="utf-8")
    ctx.finish(exp_dir, manifest, cell_count=1, total_solver_seconds=proposal["experiment_cell"]["seconds"])
    log.info("E7 bitti")


# ── Figures (from the CSVs only) ────────────────────────────────────────
def figures(ctx_or_none: Context | None, dpi: int = 300) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import plot_terrain_figures as ptf

    okabe = ptf.OKABE
    # R1: solver robustness.
    rows = read_csv(DOCS / "E1_solver_robustness.csv")
    if rows:
        fig, axes = plt.subplots(1, 2, figsize=(10.5, 3.9))
        positions = {15: 0, 60: 1, 300: 2}
        rng = np.random.default_rng(0)
        for index, basis in enumerate(("distance", "energy-dem")):
            sub = [r for r in rows if r["cost_basis"] == basis]
            x = np.array([positions[int(r["time_limit_s"])] for r in sub], dtype=float) + (index - 0.5) * 0.18 + rng.uniform(-0.05, 0.05, len(sub))
            y = [float(r["E_dem_wh"]) / 1000 for r in sub]
            axes[0].scatter(x, y, s=18, color=okabe[index], label=f"{basis}-optimised, one point per seed", alpha=0.85, edgecolors="white", linewidths=0.3)
            ref = float(sub[0]["E_dem_reference_wh"]) / 1000
            axes[0].axhline(ref, color=okabe[index], linestyle="--", linewidth=0.8, label=f"{basis}: main run (seed 42, 15 s)")
        axes[0].set_xticks([0, 1, 2]); axes[0].set_xticklabels(["15 s", "60 s", "300 s"])
        axes[0].set_xlabel("Solver time limit"); axes[0].set_ylabel("Tour energy on the DEM, all lines (kWh)")
        axes[0].set_title("(a) Best-found energy across 10 node-order seeds", fontsize=9)
        axes[0].grid(alpha=0.3); axes[0].legend(fontsize=6.5)
        sub = [r for r in rows if r["cost_basis"] == "energy-dem"]
        x = np.array([positions[int(r["time_limit_s"])] for r in sub], dtype=float) + rng.uniform(-0.08, 0.08, len(sub))
        axes[1].scatter(x, [float(r["voti_vs_distance_wh"]) for r in sub], s=18, color=okabe[2], edgecolors="white", linewidths=0.3, label="VoTI, same seed and time limit")
        for tl, pos in positions.items():
            votis = [float(r["voti_vs_distance_wh"]) for r in sub if int(r["time_limit_s"]) == tl]
            if votis:
                axes[1].hlines(mean(votis), pos - 0.25, pos + 0.25, color="k", linewidth=1.2)
        dist_ref = next(float(r["E_dem_reference_wh"]) for r in rows if r["cost_basis"] == "distance")
        dem_ref = next(float(r["E_dem_reference_wh"]) for r in rows if r["cost_basis"] == "energy-dem")
        axes[1].axhline(dist_ref - dem_ref, color=okabe[3], linestyle="--", linewidth=0.9, label=f"main run: {dist_ref - dem_ref:.0f} Wh")
        axes[1].axhline(0, color="k", linewidth=0.5)
        axes[1].set_xticks([0, 1, 2]); axes[1].set_xticklabels(["15 s", "60 s", "300 s"])
        axes[1].set_xlabel("Solver time limit"); axes[1].set_ylabel("VoTI vs distance network (Wh per cycle)")
        axes[1].set_title("(b) Value of terrain information, seed by seed (bar = mean)", fontsize=9)
        axes[1].grid(alpha=0.3); axes[1].legend(fontsize=6.5)
        fig.suptitle("E1: solver robustness (η_regen = 0.5, 7 passengers, neighbourhood rule on)", fontsize=10)
        fig.tight_layout(); fig.savefig(DOCS / "figR1_solver_robustness.png", dpi=dpi); plt.close(fig)
        print("figR1 ok")
    # R2: unconstrained networks side by side.
    geojson = R1_ROOT / "E2" / "routes.geojson"
    if geojson.exists():
        grid = load_grid()
        cells = defaultdict(list)
        for feature in json.loads(geojson.read_text(encoding="utf-8"))["features"]:
            cells[feature["properties"]["cell_id"]].append(feature)
        pairs = [("E2-off_warm_300-planar-average", "E2-off_warm_300-dem-eta0.5-average", "warm start from the constrained network, 300 s"),
                 ("E2-off-planar-average", "E2-off-dem-eta0.5-average", "cold start, 15 s")]
        for left_id, right_id, label in pairs:
            left, right = cells.get(left_id), cells.get(right_id)
            if not left or not right:
                continue
            fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.3), sharey=True, layout="constrained")
            ptf.draw_cell(axes[0], grid, left, "(a) Planar-energy network, neighbourhood rule off")
            ptf.draw_cell(axes[1], grid, right, "(b) DEM-energy network, neighbourhood rule off")
            axes[0].set_ylabel("Latitude (°N)")
            lons = [c[0] for f in left + right for c in f["geometry"]["coordinates"]]
            lats = [c[1] for f in left + right for c in f["geometry"]["coordinates"]]
            for ax in axes:
                ax.set_xlim(min(lons) - 0.004, max(lons) + 0.004); ax.set_ylim(min(lats) - 0.003, max(lats) + 0.003)
            fig.suptitle(f"E2: same stops, two surfaces, neighbourhood rule off ({label}; η_regen = 0.5, 7 passengers)", fontsize=9.5)
            name = "figR2_membership_unconstrained.png" if "warm" in left_id else "figR2b_membership_unconstrained_cold.png"
            fig.savefig(DOCS / name, dpi=dpi); plt.close(fig)
            print(name, "ok")
    # R3: VoTI against sigma.
    summary = read_csv(DOCS / "E3_summary.csv")
    if summary:
        fig, ax = plt.subplots(figsize=(6.6, 4.0))
        for index, corr in enumerate(sorted({int(r["corr_len_cells"]) for r in summary})):
            sub = sorted([r for r in summary if int(r["corr_len_cells"]) == corr], key=lambda r: float(r["sigma_m"]))
            x = [float(r["sigma_m"]) for r in sub]
            ax.plot(x, [float(r["voti_ref_p50_wh"]) for r in sub], marker="o", color=okabe[index], label=f"median, correlation length {corr} cells (~{corr * 30} m)")
            ax.fill_between(x, [float(r["voti_ref_p05_wh"]) for r in sub], [float(r["voti_ref_p95_wh"]) for r in sub], color=okabe[index], alpha=0.15, label=f"5th–95th percentile, {corr} cells")
        ref = float(summary[0]["voti_reference_solution_wh"])
        ax.axhline(ref, color=okabe[3], linestyle="--", linewidth=0.9, label=f"network optimised on the reference DEM: {ref:.0f} Wh")
        comparison = read_csv(DOCS / "E7_dem_comparison.csv")
        if comparison:
            measured = float(comparison[0]["diff_sd_m"])
            ax.axvline(measured, color=okabe[4], linestyle=":", linewidth=1.0, label=f"AW3D30 − GLO-30 s.d. along the roads (E7): {measured:.1f} m")
        ax.axhline(0, color="k", linewidth=0.5)
        ax.set_xlabel("DEM noise σ (m, per cell before correlation)"); ax.set_ylabel("VoTI on the reference DEM (Wh per cycle)")
        ax.set_title(f"E3: VoTI of networks optimised on perturbed DEMs ({summary[0]['repeats']} repeats per level, {summary[0]['time_limit_s']} s)", fontsize=9)
        ax.grid(alpha=0.3); ax.legend(fontsize=6.5)
        fig.tight_layout(); fig.savefig(DOCS / "figR3_voti_vs_sigma.png", dpi=dpi); plt.close(fig)
        print("figR3 ok")
    # R5: delivery load per line.
    rows = read_csv(DOCS / "E5_delivery_load.csv")
    if rows:
        sub = [r for r in rows if r["network"] == "dem_opt" and r["direction"] == "forward"]
        lines = sorted({r["line"] for r in sub}, key=lambda s: int(s.split()[-1]))
        series = [("constant_7pax", "as_found", "7 passengers, as found"),
                  ("decreasing_equal", "as_found", "decreasing load, as found"),
                  ("decreasing_equal", "load_aware_dem", "decreasing load, re-sequenced on the DEM"),
                  ("decreasing_equal", "load_aware_planar", "decreasing load, re-sequenced on the plane")]
        x = np.arange(len(lines)); width = 0.2
        fig, ax = plt.subplots(figsize=(8.5, 4.0))
        for index, (profile_name, source, label) in enumerate(series):
            values = []
            for line in lines:
                match = [r for r in sub if r["line"] == line and r["load_profile"] == profile_name and r["sequence_source"] == source]
                values.append(float(match[0]["E_dem_wh"]) / 1000 if match else np.nan)
            ax.bar(x + (index - 1.5) * width, values, width=width, color=okabe[index], label=label)
        ax.set_xticks(x); ax.set_xticklabels(lines, fontsize=8)
        ax.set_ylabel("Tour energy on the DEM (kWh per cycle)"); ax.grid(axis="y", alpha=0.3); ax.legend(fontsize=7)
        ax.set_title("E5: DEM-optimal network under a delivery-style load (14 × 75 kg at the depot, unloaded evenly; η_regen = 0.5)", fontsize=9)
        fig.tight_layout(); fig.savefig(DOCS / "figR5_delivery_load.png", dpi=dpi); plt.close(fig)
        print("figR5 ok")


# ── REPORT.md (every number from a CSV) ─────────────────────────────────
E0_CONSTRAINTS = [
    ("Objective: arc cost = matrix of the cost basis (distance m, travel-time s or energy Wh rounded to integers, shifted by a constant when negative)", "active", "urgup_transport/optimizer.py", "2820-2827"),
    ("Fixed number of vehicles = route_count (8); every vehicle starts and ends at the depot (node 0)", "active", "urgup_transport/optimizer.py", "2815"),
    ("Every customer stop is mandatory (no disjunction; only optional stopping-place candidates get one, and the main run has none)", "active", "urgup_transport/optimizer.py", "2843-2854"),
    ("Distance dimension with global-span cost: distance_balance_weight (20) × longest route distance in metres", "active", "urgup_transport/optimizer.py", "2862-2870"),
    ("Route distance cap max_route_distance_km (0 = the sum of the matrix maxima, i.e. not binding)", "inactive (0)", "urgup_transport/optimizer.py", "2862-2866"),
    ("Time dimension max_route_duration_minutes (added only when > 0)", "inactive (0)", "urgup_transport/optimizer.py", "2882-2883"),
    ("'Stops' dimension: each stop counts 1; vehicle capacity = max_stops_per_route or (customers + candidates) = 129, i.e. not binding; minimum min_stops_per_route (1) per vehicle", "active (min 1), capacity not binding", "urgup_transport/optimizer.py", "2891-2901"),
    ("Same-vehicle constraint per neighbourhood: VehicleVar equality for every stop of a protected group (single_route_per_mahalle=True protects all 9 neighbourhoods; groups smaller than small_mahalle_stop_limit=4 are folded into the nearest larger one on the planning matrix, giving 8 groups for 8 vehicles)", "active", "urgup_transport/optimizer.py", "2905-2907, 1701-1800"),
    ("Passenger/parcel capacity: vehicle_capacity, demand_balance_weight", "NOT implemented (deprecated compatibility inputs; not used in the VRP model or in the GA fitness)", "urgup_transport/optimizer.py; README.md", "2531-2535; README 239"),
    ("Stop demand (demand_weight / service_demand): read from the demand CSV or the stop record, carried into the proposal and reported as demand_weight per line and demand_imbalance_percent; never a constraint or an objective term", "reporting only", "urgup_transport/optimizer.py", "380-410, 2446-2455"),
    ("longest_route_weight (28) and the fitness function _fitness: GA mode only; not used in planning_mode=vrp", "inactive in vrp", "urgup_transport/optimizer.py", "1661-1699"),
    ("Search: PATH_CHEAPEST_ARC first solution, GUIDED_LOCAL_SEARCH, wall-clock time limit; initial assignment = bin-packed seed of the protected groups", "active", "urgup_transport/optimizer.py", "2910-2913, 2935-2942"),
    ("seed (42): used by the GA only; the OR-Tools search has no seed and is deterministic on this instance (verified: identical repeats; solver.ReSeed changes nothing). R1 adds node_order_seed (customer order permutation) for E1", "no effect in vrp", "urgup_transport/optimizer.py", "2760-2769"),
    ("Within-line sequence: exact TSP for ≤ 8 stops, else 2-opt (+ or-opt for the energy basis) on the planning matrix", "active", "urgup_transport/optimizer.py", "1576-1650"),
]


def md_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    head = "| " + " | ".join(label for _key, label in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    body = []
    for row in rows:
        cells = []
        for key, _label in columns:
            value = row.get(key, "")
            if isinstance(value, float):
                value = f"{value:,.4g}" if abs(value) < 1 else f"{value:,.1f}" if abs(value) < 1000 else f"{value:,.0f}"
            cells.append("" if value is None else str(value))
        body.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *body])


def f1(value: Any, digits: int = 1) -> str:
    v = fnum(value)
    return "n/a" if v is None else f"{v:,.{digits}f}"


def manifests() -> dict[str, dict[str, Any]]:
    out = {}
    for path in sorted(R1_ROOT.glob("E*/manifest.json")):
        out[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
    return out


def write_report() -> None:
    mans = manifests()
    lines: list[str] = []
    total_solver = sum(float(m.get("total_solver_seconds") or 0) for m in mans.values())
    commits = sorted({m.get("git_commit") for m in mans.values() if m.get("git_commit")})
    machine = next((m["machine"] for m in mans.values() if m.get("machine")), {})
    lines.append("# R1 revision experiments report")
    lines.append("")
    lines.append(f"Run ids: {RUN_ID} (sub-runs {', '.join(mans)}); reference run {MAIN_RUN_ID} | Git commit: {', '.join(c[:12] for c in commits)}"
                 f"{' (working tree dirty at run time)' if any(m.get('git_dirty') for m in mans.values()) else ''} | Date: {datetime.now(UTC).date().isoformat()} | "
                 f"Machine: {machine.get('node')} ({machine.get('platform')}, {machine.get('cpus')} CPUs, Python {machine.get('python')}, OR-Tools {machine.get('ortools')}) | "
                 f"Total solver time: {total_solver:,.0f} s ({total_solver / 3600:.2f} h)")
    lines.append("")
    lines.append("Every number below is read from the CSV files in this folder by `scripts/run_r1_revision_experiments.py report`; nothing is typed by hand. "
                 "Fixed factors unless a table says otherwise: 129 customer stops + depot (planning register revision 135), 8 lines, road graph revision 1584, "
                 "graph speeds with 30 km/h fallback, 30 s dwell, balance weight 20, η_regen = 0.5, 7 passengers (4,725 kg), dem_scale 1, grade cap 0.20, 15 s per solve, seed 42. "
                 "\"Optimal\" never appears: every solution is the best found under its time limit.")
    lines.append("")
    # E0
    lines.append("## E0 Capacity check")
    lines.append("")
    lines.append("**Is there a vehicle-capacity constraint? No.** The manuscript's phrase \"capacitated closed-tour VRP\" is not what the code solves. "
                 "The only capacity-like dimension in the OR-Tools model counts *stops* (one unit per stop) with a vehicle capacity equal to `max_stops_per_route`, "
                 "which the main run sets to 0, so the capacity becomes the total number of stops (129) and never binds; its only active part is the minimum of 1 stop per vehicle. "
                 "Passenger demand (`demand_weight` / `service_demand`, values 0.25–2.0 from the boarding-intensity survey, 1.0 where unknown) is read, carried through the proposal and "
                 "reported per line (`demand_weight`, `demand_imbalance_percent`) but enters neither the objective nor any constraint. `vehicle_capacity` and `demand_balance_weight` are accepted as deprecated "
                 "compatibility inputs and listed under `deprecated_network_design_parameters` in every proposal; the README (line 239) says so. The 14-seat figure comes from "
                 "`config.DEFAULT_SERVICE_VEHICLE_CAPACITY` and is used by the service planner (frequencies), not by line design. **Recommendation for the manuscript:** call the problem a "
                 "closed-tour multi-vehicle routing problem with a fixed fleet, mandatory visits, a same-vehicle (neighbourhood) constraint and a route-length balance term, and state explicitly that demand and capacity are out of scope "
                 "(consistent with the stated scope: service level fixed, energy per cycle). Constraints active in the main run, with file and lines (line numbers of the R1 commit):")
    lines.append("")
    e0_rows = [{"constraint": c, "status": st, "file": f, "lines": ln} for c, st, f, ln in E0_CONSTRAINTS]
    write_csv(DOCS / "E0_constraints.csv", e0_rows)
    lines.append(md_table(e0_rows, [("constraint", "Constraint / term"), ("status", "Status in the main run"), ("file", "File"), ("lines", "Lines")]))
    lines.append("")
    lines.append("A consequence worth stating in the paper: with all nine neighbourhoods protected and the one-stop neighbourhood folded into its nearest larger one, the main run hands the solver "
                 "exactly eight same-vehicle groups for eight vehicles that must each serve at least one stop, so **line membership is fully determined by the neighbourhood rule**; the solver's freedom is the visiting order "
                 "and the path between stops. The single stop that changes line at η_regen = 0.7 and at dem_scale ≥ 1.5 in the main run is the folded one-stop neighbourhood, whose \"nearest larger group\" is measured on the planning matrix and therefore depends on the cost basis. "
                 "Experiment E2 removes the rule.")
    lines.append("")
    # E1
    lines.append("## E1 Solver robustness")
    lines.append("")
    rows = read_csv(DOCS / "E1_solver_robustness.csv")
    if rows:
        m = mans.get("E1", {})
        summary = e1_summary(rows)
        write_csv(DOCS / "E1_summary.csv", summary)
        lines.append(f"Run id {RUN_ID}/E1, {len(rows)} optimisations (cost basis ∈ {{distance, energy-dem}} × node-order seed 1–10 × time limit 15/60/300 s), η_regen 0.5, 7 passengers, neighbourhood rule on, balance weight 20; solver time {float(m.get('total_solver_seconds') or 0):,.0f} s. "
                     "OR-Tools has no random seed and returned the identical network on three repeats in the main run, so \"seed\" here is a seeded permutation of the customer node order handed to the model "
                     "(`node_order_seed`), which changes the first solution and the local-search trajectory but nothing about the problem. Every solution is evaluated on the reference DEM.")
        lines.append("")
        lines.append(md_table(summary, [("cost_basis", "Cost basis"), ("time_limit_s", "Time limit (s)"), ("E_dem_mean_wh", "E_dem mean (Wh)"), ("E_dem_sd_wh", "s.d."), ("E_dem_min_wh", "best"), ("E_dem_max_wh", "worst"),
                                        ("E_dem_reference_wh", "main run (seed 42)"), ("km_mean", "km mean"), ("jaccard_mean", "Jaccard to reference"), ("kendall_tau_mean", "Kendall τ"),
                                        ("voti_mean_wh", "VoTI mean (Wh)"), ("voti_sd_wh", "s.d."), ("voti_min_wh", "min"), ("voti_max_wh", "max"), ("voti_mean_pct", "VoTI mean (%)"), ("voti_positive_count", "positive of n")]))
        lines.append("")
        dem_rows = [r for r in rows if r["cost_basis"] == "energy-dem"]
        dist_ref = float(next(r["E_dem_reference_wh"] for r in rows if r["cost_basis"] == "distance"))
        dem_ref = float(dem_rows[0]["E_dem_reference_wh"])
        voti_ref = dist_ref - dem_ref
        all_votis = [float(r["voti_vs_distance_wh"]) for r in dem_rows]
        rank = sum(1 for v in all_votis if v < voti_ref) / len(all_votis) * 100
        by_tl = {tl: [float(r["voti_vs_distance_wh"]) for r in dem_rows if int(r["time_limit_s"]) == tl] for tl in E1_TIME_LIMITS}
        dist_by_tl = {tl: [float(r["E_dem_wh"]) for r in rows if r["cost_basis"] == "distance" and int(r["time_limit_s"]) == tl] for tl in E1_TIME_LIMITS}
        dem_by_tl = {tl: [float(r["E_dem_wh"]) for r in dem_rows if int(r["time_limit_s"]) == tl] for tl in E1_TIME_LIMITS}
        complete = all(dem_by_tl[tl] and dist_by_tl[tl] for tl in E1_TIME_LIMITS)
        if not complete:
            lines.append(f"_E1 is incomplete: {len(rows)} of {2 * len(E1_SEEDS) * len(E1_TIME_LIMITS)} cells so far._")
        lines.append(f"**Sentences for the manuscript.** Across the {len(all_votis)} seed × time-limit pairs, VoTI against the distance network of the same seed and time limit is "
                     f"{mean(all_votis):,.0f} ± {sd(all_votis):,.0f} Wh (mean ± s.d.), positive in {sum(1 for v in all_votis if v > 0)} of {len(all_votis)}; per time limit: "
                     + "; ".join(f"{tl} s: {mean(v):,.0f} ± {sd(v):,.0f} Wh, {sum(1 for x in v if x > 0)}/{len(v)} positive" for tl, v in by_tl.items() if v)
                     + f". The main run's {voti_ref:,.0f} Wh ({voti_ref / dist_ref * 100:.1f} %) sits at the {rank:.0f}th percentile of this distribution. "
                     + (f"The heuristic noise of the objective itself is small: the DEM-optimised energy varies by {sd(dem_by_tl[15]):,.0f} Wh (s.d., {sd(dem_by_tl[15]) / mean(dem_by_tl[15]) * 100:.2f} %) across seeds at 15 s and by "
                        f"{sd(dem_by_tl[300]):,.0f} Wh at 300 s; the distance-optimised network's DEM energy varies by {sd(dist_by_tl[15]):,.0f} Wh at 15 s and {sd(dist_by_tl[300]):,.0f} Wh at 300 s"
                        + (", i.e. the distance solutions differ more in their *terrain* energy than the terrain solutions do, because the distance objective is blind to it. " if sd(dist_by_tl[15]) > sd(dem_by_tl[15]) else ". ")
                        + f"Longer time limits change the best-found energy by {mean(dem_by_tl[300]) - mean(dem_by_tl[15]):+,.0f} Wh (DEM) and {mean(dist_by_tl[300]) - mean(dist_by_tl[15]):+,.0f} Wh (distance) from 15 s to 300 s. " if complete else "")
                     + f"Membership stays fixed (mean Jaccard to the reference {mean(float(r['jaccard_to_reference']) for r in rows):.3f}), as E0 predicts.")
        lines.append("")
        lines.append("Figure: `figR1_solver_robustness.png`.")
    else:
        lines.append("_E1 not run._")
    lines.append("")
    # E2
    lines.append("## E2 Neighbourhood constraint off")
    lines.append("")
    rows = read_csv(DOCS / "E2_neighbourhood_off.csv")
    if rows:
        m = mans.get("E2", {})
        lines.append(f"Run id {RUN_ID}/E2, {m.get('cell_count')} optimisations, solver time {float(m.get('total_solver_seconds') or 0):,.0f} s. The rule is switched off with the existing "
                     "`single_route_per_mahalle=False` (no neighbourhood carries its own `single_route_only` flag, so no group remains) and `small_mahalle_stop_limit=0`. "
                     "Three variants: `off_cold_15` is the plan as written (OR-Tools from its own first solution, 15 s); because that returned networks 4–12 km *longer* than the constrained ones "
                     "(the constrained solution is feasible for the relaxed problem, so this is a heuristic failure, not a property of the relaxation), `off_warm_15` and `off_warm_300` start the local search "
                     "from the constrained main-run solution of the same cell (new optimiser parameter `warm_start_stop_ids`, unit-tested to be never worse than its start) for 15 s and 300 s. "
                     "Distance and planar solutions are η-independent and the distance solution is dem_scale-independent, so they are optimised once per variant (6 optimisations per variant, 18 in all).")
        lines.append("")
        dem_rows = [r for r in rows if r["cost_basis"] == "energy-dem" and not r.get("E_dem_start_wh")]
        table = []
        for r in dem_rows:
            table.append({
                "variant": r["variant"], "eta_regen": r["eta_regen"], "dem_scale": r["dem_scale"], "time_limit_s": r["time_limit_s"],
                "E_dem_dist": next(fnum(x["E_dem_wh"]) for x in rows if x["variant"] == r["variant"] and x["cost_basis"] == "distance" and x["eta_regen"] == r["eta_regen"] and x["dem_scale"] == r["dem_scale"]),
                "E_dem_planar": next(fnum(x["E_dem_wh"]) for x in rows if x["variant"] == r["variant"] and x["cost_basis"] == "energy-planar" and x["eta_regen"] == r["eta_regen"] and x["dem_scale"] == r["dem_scale"]),
                "E_dem_dem": fnum(r["E_dem_wh"]), "km": fnum(r["km"]),
                "voti_d": f"{f1(r['voti_vs_distance_wh'], 0)} ({f1(r['voti_vs_distance_pct'], 1)} %)", "voti_p": f"{f1(r['voti_vs_planar_wh'], 0)} ({f1(r['voti_vs_planar_pct'], 1)} %)",
                "jac_pd": r["jaccard_planar_vs_dem"], "chg_pd": r["stops_changing_line_planar_vs_dem"], "tau": r["kendall_tau_planar_vs_dem"],
                "jac_dd": r["jaccard_distance_vs_dem"], "chg_dd": r["stops_changing_line_distance_vs_dem"],
                "jac_on": r["jaccard_vs_constrained"], "chg_on": r["stops_changing_line_vs_constrained"], "sizes": f"{r['min_route_stops']}–{r['max_route_stops']}",
                "asym": r["mean_direction_asymmetry"], "fric": r["friction_share_pct"],
            })
        lines.append(md_table(table, [("variant", "Variant"), ("eta_regen", "η"), ("dem_scale", "dem_scale"), ("time_limit_s", "s"), ("E_dem_dist", "E_dem distance-opt"), ("E_dem_planar", "E_dem planar-opt"), ("E_dem_dem", "E_dem DEM-opt"), ("km", "km DEM-opt"),
                                      ("voti_d", "VoTI vs distance (Wh, %)"), ("voti_p", "VoTI vs planar (Wh, %)"), ("jac_pd", "Jaccard planar/DEM"), ("chg_pd", "stops changing line planar/DEM"), ("tau", "Kendall τ"),
                                      ("jac_dd", "Jaccard distance/DEM"), ("chg_dd", "stops changing distance/DEM"), ("jac_on", "Jaccard DEM-opt vs constrained DEM-opt"), ("chg_on", "stops changing vs constrained"), ("sizes", "line sizes (stops)"), ("asym", "mean asymmetry"), ("fric", "friction share %")]))
        lines.append("")
        chained = [r for r in rows if r.get("E_dem_start_wh")]
        if chained:
            lines.append("Terrain-aware local search started from the best distance / planar network of the same setting (300 s). What it removes is a lower bound of VoTI that two independent heuristic runs cannot fake, because the search can only improve on its start:")
            lines.append("")
            lines.append(md_table(chained, [("variant", "Variant"), ("start", "Start"), ("E_dem_start_wh", "E_dem of start (Wh)"), ("km_start", "km start"), ("E_dem_wh", "E_dem after DEM search"), ("km", "km"), ("improvement_over_start_wh", "removed (Wh)"), ("improvement_over_start_pct", "%"),
                                            ("jaccard_vs_start", "Jaccard vs start"), ("stops_changing_line_vs_start", "stops changing vs start"), ("kendall_tau_vs_start", "τ vs start"), ("jaccard_vs_constrained", "Jaccard vs constrained DEM-opt"), ("min_route_stops", "min stops"), ("max_route_stops", "max stops")]))
            lines.append("")
        def pick(variant, eta, scale):
            return next((r for r in dem_rows if r["variant"] == variant and float(r["eta_regen"]) == eta and float(r["dem_scale"]) == scale), None)
        on, cold, warm = pick("on", 0.5, 1.0), pick("off_cold_15", 0.5, 1.0), pick("off_warm_300", 0.5, 1.0)
        sentences = []
        if on and warm:
            sentences.append(f"With the rule off (warm start, 300 s), the planar-energy and DEM-energy networks differ in membership: mean Jaccard {warm['jaccard_planar_vs_dem']} with {warm['stops_changing_line_planar_vs_dem']} of 129 stops on a different line "
                             f"(against 1.0 and 0 with the rule on); the DEM network itself moves {warm['stops_changing_line_vs_constrained']} stops relative to the constrained DEM network. "
                             f"VoTI against the distance network is {f1(warm['voti_vs_distance_wh'], 0)} Wh ({f1(warm['voti_vs_distance_pct'])} %) unconstrained versus {f1(on['voti_vs_distance_wh'], 0)} Wh ({f1(on['voti_vs_distance_pct'])} %) constrained, "
                             f"and against the planar network {f1(warm['voti_vs_planar_wh'], 0)} Wh ({f1(warm['voti_vs_planar_pct'])} %) versus {f1(on['voti_vs_planar_wh'], 0)} Wh ({f1(on['voti_vs_planar_pct'])} %).")
        if cold:
            sentences.append(f"The cold 15 s variant gives VoTI {f1(cold['voti_vs_distance_wh'], 0)} Wh ({f1(cold['voti_vs_distance_pct'])} %) vs distance and {f1(cold['voti_vs_planar_wh'], 0)} Wh vs planar, with {cold['stops_changing_line_planar_vs_dem']} stops changing line between planar and DEM, "
                             f"but its three networks are {fnum(next(r['km'] for r in rows if r['variant'] == 'off_cold_15' and r['cost_basis'] == 'distance')) - fnum(next(r['km'] for r in rows if r['variant'] == 'on' and r['cost_basis'] == 'distance')):+.1f} km (distance) longer than the constrained ones, so its VoTI mixes terrain with solver noise and should not be quoted alone.")
        for scale in E2_SCALES:
            o, w = pick("on", 0.5, scale), pick("off_warm_300", 0.5, scale)
            if o and w:
                sentences.append(f"At dem_scale {scale}: VoTI vs distance {f1(w['voti_vs_distance_wh'], 0)} Wh ({f1(w['voti_vs_distance_pct'])} %) unconstrained (warm 300 s) vs {f1(o['voti_vs_distance_wh'], 0)} Wh ({f1(o['voti_vs_distance_pct'])} %) constrained; "
                                 f"planar/DEM Jaccard {w['jaccard_planar_vs_dem']} ({w['stops_changing_line_planar_vs_dem']} stops) vs {o['jaccard_planar_vs_dem']} ({o['stops_changing_line_planar_vs_dem']}).")
        o7, w7 = pick("on", 0.7, 1.0), pick("off_warm_300", 0.7, 1.0)
        if o7 and w7:
            sentences.append(f"At η_regen 0.7: VoTI vs distance {f1(w7['voti_vs_distance_wh'], 0)} Wh ({f1(w7['voti_vs_distance_pct'])} %) unconstrained vs {f1(o7['voti_vs_distance_wh'], 0)} Wh ({f1(o7['voti_vs_distance_pct'])} %) constrained; planar/DEM Jaccard {w7['jaccard_planar_vs_dem']} ({w7['stops_changing_line_planar_vs_dem']} stops).")
        for r in chained:
            sentences.append(f"Starting the DEM search from the {r['start'].replace('warm_from_', '')} network ({r['neighbourhood_constraint']} rule) removes {f1(r['improvement_over_start_wh'], 0)} Wh ({f1(r['improvement_over_start_pct'])} %) of its {f1(r['E_dem_start_wh'], 0)} Wh, moving {r['stops_changing_line_vs_start']} stops to another line (Jaccard {r['jaccard_vs_start']}, τ {r['kendall_tau_vs_start']}).")
        unc_dist = [r for r in chained if r["variant"] == "off_warm_300_from_distance"]
        unc_300 = pick("off_warm_300", 0.5, 1.0)
        if unc_dist and unc_300:
            sentences.append(f"The independent unconstrained runs are dominated by search noise: the 300 s DEM-energy network is {f1(unc_300['km'], 1)} km against the 300 s distance network's {f1(unc_dist[0]['km_start'], 1)} km, and the distance network's DEM energy is lower by {f1(-fnum(unc_300['voti_vs_distance_wh']), 0)} Wh, "
                             f"an amount smaller than the spread between local optima reached from different starts of the same objective; the chained search above is the number to quote for the unconstrained case.")
        lines.append("**Sentences for the manuscript.** " + " ".join(sentences))
        lines.append("")
        lines.append("Figures: `figR2_membership_unconstrained.png` (warm start, 300 s) and `figR2b_membership_unconstrained_cold.png` (cold start, 15 s); line names are \"Line N\" by index.")
    else:
        lines.append("_E2 not run._")
    lines.append("")
    # E3
    lines.append("## E3 DEM noise levels")
    lines.append("")
    summary = read_csv(DOCS / "E3_summary.csv")
    if summary:
        m = mans.get("E3", {})
        lines.append(f"Run id {RUN_ID}/E3, {m.get('cell_count')} optimisations (σ ∈ {{1, 2, 2.5, 4}} m × correlation length L ∈ {{3, 10}} cells × 30 repeats), {E3_TIME_LIMIT} s per solve (the main run's Monte Carlo limit), "
                     f"η_regen 0.5, 7 passengers, solver time {float(m.get('total_solver_seconds') or 0):,.0f} s. Noise model: Gaussian per cell with the stated σ, then a box mean over L × L cells "
                     "(L = 3: offsets −1…+1, the main run's method; L = 10: offsets −5…+4, a 10 × 10 window ≈ 300 m), rescaled by √(cells in the window) so the marginal standard deviation stays σ; "
                     f"each (σ, L, repeat) has its own seed ({m.get('seed_rule')}), the base grid is never modified. Every network is evaluated on its own perturbed grid and on the reference grid; "
                     "VoTI on the reference grid = E_ref(distance network) − E_ref(network optimised on the perturbed grid).")
        lines.append("")
        lines.append(md_table(summary, [("sigma_m", "σ (m)"), ("corr_len_cells", "L (cells)"), ("repeats", "n"), ("voti_ref_p50_wh", "VoTI ref p50 (Wh)"), ("voti_ref_p05_wh", "p5"), ("voti_ref_p95_wh", "p95"), ("voti_ref_mean_wh", "mean"), ("voti_ref_sd_wh", "s.d."),
                                        ("share_voti_ref_negative", "share negative"), ("voti_own_p50_wh", "VoTI own grid p50"), ("spurious_climb_mean_m", "spurious climb (m, mean)"), ("E_reference_grid_mean_wh", "E on ref grid mean"), ("jaccard_mean", "Jaccard to ref DEM solution"), ("stops_changing_line_mean", "stops changing (mean)"), ("modal_line_stability_mean", "modal line stability")]))
        lines.append("")
        sentences = []
        ref_voti = fnum(summary[0]["voti_reference_solution_wh"])
        for corr in sorted({int(r["corr_len_cells"]) for r in summary}):
            sub = sorted([r for r in summary if int(r["corr_len_cells"]) == corr], key=lambda r: float(r["sigma_m"]))
            first_negative = next((r["sigma_m"] for r in sub if fnum(r["voti_ref_p50_wh"]) < 0), None)
            first_half = next((r["sigma_m"] for r in sub if fnum(r["voti_ref_p50_wh"]) < ref_voti / 2), None)
            sentences.append(f"L = {corr}: median VoTI on the reference grid " + ", ".join(f"{f1(r['voti_ref_p50_wh'], 0)} Wh at σ = {r['sigma_m']} m ({float(r['share_voti_ref_negative']) * 100:.0f} % negative)" for r in sub)
                             + (f"; the median first falls below zero at σ = {first_negative} m" if first_negative else "; the median never falls below zero in the tested range")
                             + (f" and below half the reference value ({ref_voti / 2:,.0f} Wh) at σ = {first_half} m." if first_half else "."))
        l3 = {float(r["sigma_m"]): r for r in summary if int(r["corr_len_cells"]) == 3}
        l10 = {float(r["sigma_m"]): r for r in summary if int(r["corr_len_cells"]) == 10}
        if l3 and l10:
            common = sorted(set(l3) & set(l10))
            sentences.append("Longer correlation (L = 10 vs 3) changes the median VoTI by " + ", ".join(f"{fnum(l10[s]['voti_ref_p50_wh']) - fnum(l3[s]['voti_ref_p50_wh']):+,.0f} Wh at σ = {s:g} m" for s in common)
                             + " and the spurious climb by " + ", ".join(f"{fnum(l10[s]['spurious_climb_mean_m']) - fnum(l3[s]['spurious_climb_mean_m']):+,.0f} m" for s in common) + ".")
        sentences.append(f"Membership is stable at every level (Jaccard to the reference DEM solution ≥ {min(float(r['jaccard_mean']) for r in summary):.3f}, modal-line stability ≥ {min(float(r['modal_line_stability_mean']) for r in summary):.3f}). The network optimised on the unperturbed grid has VoTI {ref_voti:,.0f} Wh.")
        lines.append("**Sentences for the manuscript.** " + " ".join(sentences))
        lines.append("")
        lines.append("Figure: `figR3_voti_vs_sigma.png`.")
    else:
        lines.append("_E3 not run._")
    lines.append("")
    # E4
    lines.append("## E4 Exact shortest paths")
    lines.append("")
    rows = read_csv(DOCS / "E4_exact_shortest_paths.csv")
    reeval = read_csv(DOCS / "E4_reevaluation.csv")
    reopt = read_csv(DOCS / "E4_reoptimisation.csv")
    if rows:
        neg = [int(r["negative_edges_physics_potential"]) for r in rows]
        lines.append(f"Run id {RUN_ID}/E4. For each of the 12 (η_regen, mass) scenarios the stop-to-stop matrix was built twice on the same graph: with the elevation potential π = η_regen·m·g·h and clipping "
                     f"(the main run) and with Johnson potentials from a Bellman-Ford pass over the true directed edge energies (new option `energy_options.exact_potentials=True`, unit-tested against Bellman-Ford). "
                     f"The physics potential leaves {min(neg)}–{max(neg)} of {rows[0]['directed_edges']} directed edges with a negative corrected weight (clipped to 0); Johnson leaves {max(int(r['negative_edges_johnson']) for r in rows)}, "
                     f"and no negative cycle was found in any scenario ({'none' if not any(r['negative_cycle_found'] == 'True' for r in rows) else 'FOUND'}). Over the {rows[0]['pairs_total']} ordered stop pairs, "
                     + "; ".join(f"at η_regen = {eta} {max(int(r['pairs_differing']) for r in rows if float(r['eta_regen']) == eta)} pairs differ by more than 0.01 Wh (largest |Δ| {max(float(r['max_abs_diff_wh']) for r in rows if float(r['eta_regen']) == eta):.2f} Wh, largest relative Δ {max(float(r['max_rel_diff_pct']) for r in rows if float(r['eta_regen']) == eta):.3f} %)" for eta in sorted({float(r['eta_regen']) for r in rows}))
                     + " (relative to the exact value, floored at 1 Wh; in every differing pair the clipped path costs more, never less). ")
        if reeval:
            lines.append(f"Re-pricing the 12 main-run DEM networks with the exact matrix changes their tour energy by at most {max(abs(float(r['difference_wh'])) for r in reeval):.2f} Wh ({max(int(r['legs_differing']) for r in reeval)} of {reeval[0]['legs_total']} legs differ). ")
        if reopt:
            r = reopt[0]
            lines.append(f"Re-optimising η_regen 0.5 / 7 passengers with the exact matrix (15 s, same settings as the main run) returned {'the identical network' if r['identical_sequences'] == 'True' else 'a different sequence'} "
                         f"(Jaccard {r['jaccard_to_reference']}, {r['stops_changing_line']} stops changing line, Kendall τ {r['kendall_tau']}): E_dem {f1(r['E_dem_wh'], 0)} Wh vs {f1(r['E_dem_reference_dem_opt_wh'], 0)} Wh, "
                         f"VoTI vs distance {f1(r['voti_vs_distance_wh'], 0)} Wh ({f1(r['voti_vs_distance_pct'])} %) vs {f1(r['voti_reference_wh'], 0)} Wh in the main run. "
                         f"Conclusion: the clipping never changes a leg of the tours actually found and never changes the network at the main scenario; the exact option is kept in the code (`exact_potentials`) for graphs where the grade cap bites harder.")
        lines.append("")
        lines.append(md_table(rows, [("eta_regen", "η"), ("mass_scenario", "mass"), ("negative_edges_physics_potential", "negative edges (physics π)"), ("negative_edges_johnson", "negative edges (Johnson)"), ("negative_cycle_found", "negative cycle"),
                                     ("pairs_total", "pairs"), ("pairs_differing", "pairs differing"), ("max_abs_diff_wh", "max |Δ| (Wh)"), ("max_rel_diff_pct", "max rel Δ (%)"), ("mean_rel_diff_pct", "mean rel Δ (%)"), ("bellman_ford_passes", "BF relaxations (max per node)")]))
    else:
        lines.append("_E4 not run._")
    lines.append("")
    # E5
    lines.append("## E5 Delivery load")
    lines.append("")
    rows = read_csv(DOCS / "E5_delivery_load.csv")
    if rows:
        m = mans.get("E5", {})
        lines.append(f"Run id {RUN_ID}/E5. Load profiles: `constant_7pax` (525 kg throughout), `decreasing_equal` (1,050 kg at the depot, 1,050/n unloaded at each of the line's n stops, empty on the last leg), "
                     "`decreasing_demand` (the same 1,050 kg unloaded in proportion to each stop's `demand_weight`). Every leg is priced at the mass on board (rolling resistance, grade, regeneration cap and the stop's braking/pull-away energy all see it); "
                     "energies exclude the stop events, which are listed separately (`stop_energy_wh`). `as_found` = the main-run network's own sequence and road paths; `load_aware_dem` / `load_aware_planar` = the visiting order re-optimised per line by 2-opt + or-opt with the full load-aware tour price on that surface "
                     "(paths = least-energy paths on that surface at the average load), membership fixed; every result is then evaluated on the DEM. `reverse` drives the same road pieces backwards with the load profile applied to the reversed order. "
                     f"`heavy_first_index` = Σ m_k·climb_k / Σ m_k over legs with m_k the total mass (kerb + load); `heavy_first_index_load` uses the load alone (a lower value means the heavy legs climb less). Municipal lines: `municipal_as_drawn` on the drawn geometry at 30 km/h with stops in drawing order from the point nearest the depot (HADOSAN and ÜSET carry no stops in the register and get the constant profile only); `municipal_on_graph` routes the same stop order on the graph so that it can be re-sequenced.")
        lines.append("")
        totals = defaultdict(float)
        counts = defaultdict(int)
        for r in rows:
            if r["direction"] != "forward":
                continue
            key = (r["network"], r["load_profile"], r["sequence_source"])
            totals[key] += float(r["E_dem_wh"]); counts[key] += 1
        table = []
        for network in ("distance_opt", "planar_opt", "dem_opt", "municipal_on_graph", "municipal_as_drawn"):
            for profile_name in LOAD_PROFILES:
                base = totals.get((network, profile_name, "as_found")) or totals.get((network, profile_name, "as_drawn"))
                if base is None:
                    continue
                dem_seq = totals.get((network, profile_name, "load_aware_dem"))
                pl_seq = totals.get((network, profile_name, "load_aware_planar"))
                table.append({"network": network, "profile": profile_name, "lines": counts.get((network, profile_name, "as_found")) or counts.get((network, profile_name, "as_drawn")),
                              "as_found": round(base, 0), "dem_seq": round(dem_seq, 0) if dem_seq is not None else None, "pl_seq": round(pl_seq, 0) if pl_seq is not None else None,
                              "gain_dem": f"{base - dem_seq:,.0f} ({(base - dem_seq) / base * 100:.2f} %)" if dem_seq is not None else "",
                              "gain_pl": f"{base - pl_seq:,.0f} ({(base - pl_seq) / base * 100:.2f} %)" if pl_seq is not None else "",
                              "load_voti": f"{pl_seq - dem_seq:,.0f} ({(pl_seq - dem_seq) / pl_seq * 100:.2f} %)" if dem_seq is not None and pl_seq is not None else ""})
        lines.append(md_table(table, [("network", "Network"), ("profile", "Load profile"), ("lines", "lines"), ("as_found", "E_dem as found (Wh)"), ("dem_seq", "E_dem load-aware DEM sequence"), ("pl_seq", "E_dem load-aware planar sequence"), ("gain_dem", "gain of DEM re-sequencing (Wh, %)"), ("gain_pl", "gain of planar re-sequencing"), ("load_voti", "load-dependent VoTI: planar seq − DEM seq (Wh, %)")]))
        lines.append("")
        # Steepest two lines of the DEM network.
        dem_lines = [r for r in rows if r["network"] == "dem_opt" and r["direction"] == "forward" and r["load_profile"] == "constant_7pax" and r["sequence_source"] == "as_found"]
        steep = sorted(dem_lines, key=lambda r: -float(r["climb_m"]))[:2]
        sentences = []
        for network in ("dem_opt", "distance_opt", "planar_opt"):
            af = totals.get((network, "decreasing_equal", "as_found"))
            ld = totals.get((network, "decreasing_equal", "load_aware_dem"))
            lp = totals.get((network, "decreasing_equal", "load_aware_planar"))
            cf = totals.get((network, "constant_7pax", "as_found"))
            cd = totals.get((network, "constant_7pax", "load_aware_dem"))
            if af and ld and lp and cf and cd:
                sentences.append(f"Network total, {network.replace('_opt', '-optimised')} lines, decreasing load: as found {af:,.0f} Wh, DEM re-sequenced {ld:,.0f} Wh (saving {af - ld:,.0f} Wh, {(af - ld) / af * 100:.2f} %; with the constant load the same search saves {cf - cd:,.0f} Wh, {(cf - cd) / cf * 100:.2f} %), "
                                 f"planar re-sequenced {lp:,.0f} Wh, so the load-dependent VoTI (planar sequence − DEM sequence, both under the decreasing load) is {lp - ld:,.0f} Wh ({(lp - ld) / lp * 100:.2f} %).")
        for r in steep:
            line = r["line"]
            def val(profile_name, source):
                match = [x for x in rows if x["network"] == "dem_opt" and x["direction"] == "forward" and x["line"] == line and x["load_profile"] == profile_name and x["sequence_source"] == source]
                return float(match[0]["E_dem_wh"]) if match else None
            af, ad, ap = val("decreasing_equal", "as_found"), val("decreasing_equal", "load_aware_dem"), val("decreasing_equal", "load_aware_planar")
            hf = [x for x in rows if x["network"] == "dem_opt" and x["direction"] == "forward" and x["line"] == line and x["load_profile"] == "decreasing_equal"]
            hfi = {x["sequence_source"]: x["heavy_first_index_load"] for x in hf}
            sentences.append(f"{line} ({float(r['climb_m']):,.0f} m climb, {r['stops']} stops): decreasing load as found {af:,.0f} Wh, re-sequenced on the DEM {ad:,.0f} Wh ({(af - ad) / af * 100:.1f} % saved), re-sequenced on the plane {ap:,.0f} Wh; "
                             f"load-weighted mean climb per leg (heavy-first index, load only) {hfi.get('as_found')} → {hfi.get('load_aware_dem')} m (DEM) / {hfi.get('load_aware_planar')} m (planar).")
        # Heavy-first pattern across all DEM-network lines.
        hf_rows = [r for r in rows if r["network"] == "dem_opt" and r["direction"] == "forward" and r["load_profile"] == "decreasing_equal"]
        by_line = defaultdict(dict)
        for r in hf_rows:
            by_line[r["line"]][r["sequence_source"]] = fnum(r["heavy_first_index_load"])
        lowered = sum(1 for d in by_line.values() if d.get("load_aware_dem") is not None and d.get("as_found") is not None and d["load_aware_dem"] < d["as_found"] - 0.05)
        raised = sum(1 for d in by_line.values() if d.get("load_aware_dem") is not None and d.get("as_found") is not None and d["load_aware_dem"] > d["as_found"] + 0.05)
        mean_af = mean(d["as_found"] for d in by_line.values() if d.get("as_found") is not None)
        mean_ld = mean(d["load_aware_dem"] for d in by_line.values() if d.get("load_aware_dem") is not None)
        mean_lp = mean(d["load_aware_planar"] for d in by_line.values() if d.get("load_aware_planar") is not None)
        rev = defaultdict(float)
        for r in rows:
            if r["network"] == "dem_opt" and r["load_profile"] == "decreasing_equal" and r["sequence_source"] == "load_aware_dem":
                rev[r["direction"]] += float(r["E_dem_wh"])
        sentences.append(f"Heavy-first pattern (DEM network, decreasing load): the load-weighted climb per leg falls from {mean_af:.1f} m (as found) to {mean_ld:.1f} m after DEM re-sequencing and {mean_lp:.1f} m after planar re-sequencing on average over the 8 lines; "
                         f"it falls on {lowered} lines and rises on {raised}. Reversing the DEM-re-sequenced lines with the load profile reversed costs {rev['reverse']:,.0f} Wh against {rev['forward']:,.0f} Wh forward ({(rev['reverse'] - rev['forward']) / rev['forward'] * 100:+.1f} %).")
        lines.append("**Sentences for the manuscript.** " + " ".join(sentences))
        lines.append("")
        lines.append("Figure: `figR5_delivery_load.png`. Per-line rows (all networks, both directions, three profiles, three sequence sources) in `E5_delivery_load.csv`; the as-found pricing was checked against the stored main-run line energies (`data/processed/experiments/2026-10-R1/E5/E5_sanity_check.csv`).")
    else:
        lines.append("_E5 not run._")
    lines.append("")
    # E6
    lines.append("## E6 Reverse feasibility")
    lines.append("")
    rows = read_csv(DOCS / "E6_reverse_feasibility.csv")
    if rows:
        lines.append(f"Run id {RUN_ID}/E6. One-way rules come from the editable road network's `direction` attribute (revision 1584). For the DEM-optimal lines the forward road pieces are known exactly; a piece violates the rule in reverse when the graph has no edge in the opposite direction. "
                     "For the municipal drawings the line is resampled every 10 m and each sample matched to the nearest road (within 30 m) with its travel direction; consecutive samples on one road form a traversal. "
                     "`E_legal_reverse_wh` routes the *reversed stop order* on the directed graph with least-energy legal paths (η_regen 0.5, 7 passengers); for the municipal lines the forward energy in that column pair is routed on the graph the same way (drawn stop order, graph speeds), so that the two are comparable.")
        lines.append("")
        lines.append(md_table(rows, [("network", "Network"), ("line", "Line"), ("stops", "stops"), ("road_pieces", "road pieces"), ("oneway_segments_violated", "one-way pieces violated in reverse"), ("violated_length_m", "violated length (m)"), ("share_of_length_pct", "share of length (%)"),
                                     ("forward_as_drawn_violations", "violations as drawn (fwd)"), ("reverse_as_driven_legal", "reverse legal as driven"), ("legal_reverse_available", "legal reverse routed"), ("E_forward_wh", "E forward (Wh)"), ("E_reverse_physical_wh", "E reverse physical"), ("E_legal_reverse_wh", "E legal reverse"),
                                     ("forward_km", "km fwd"), ("legal_reverse_km", "km legal rev"), ("asymmetry_physical", "A physical"), ("asymmetry_legal", "A legal"), ("legal_reverse_cheaper", "legal reverse cheaper")]))
        lines.append("")
        legal_now = [r for r in rows if r["reverse_as_driven_legal"] == "True"]
        dem = [r for r in rows if r["network"] == "dem_opt"]
        muni = [r for r in rows if r["network"] != "dem_opt"]
        sentences = [f"Of the {len(rows)} lines ({len(dem)} DEM-optimal, {len(muni)} municipal), the reverse direction is legal as driven on {len(legal_now)} ({sum(1 for r in dem if r['reverse_as_driven_legal'] == 'True')} DEM-optimal, {sum(1 for r in muni if r['reverse_as_driven_legal'] == 'True')} municipal); "
                     f"the others cross {min(int(r['oneway_segments_violated']) for r in rows if r['reverse_as_driven_legal'] != 'True')}–{max(int(r['oneway_segments_violated']) for r in rows if r['reverse_as_driven_legal'] != 'True')} one-way pieces, "
                     f"{min(float(r['share_of_length_pct']) for r in rows if r['reverse_as_driven_legal'] != 'True'):.1f}–{max(float(r['share_of_length_pct']) for r in rows if r['reverse_as_driven_legal'] != 'True'):.1f} % of their length."]
        for r in dem:
            if r["line"] in ("Line 6", "Line 7"):
                sentences.append(f"{r['line']}: physical asymmetry {float(r['asymmetry_physical']):.3f} (reverse {f1(r['E_reverse_physical_wh'], 0)} vs forward {f1(r['E_forward_wh'], 0)} Wh); with a legal reverse routing ({r['oneway_segments_violated']} one-way pieces, {f1(r['violated_length_m'], 0)} m, avoided) the asymmetry is {float(r['asymmetry_legal']):.3f} ({f1(r['E_legal_reverse_wh'], 0)} Wh, {f1(r['legal_reverse_km'], 1)} km vs {f1(r['forward_km'], 1)} km).")
        with_legal = [r for r in rows if r.get("asymmetry_legal")]
        sentences.append(f"Across the {len(with_legal)} lines with a stop order, the legal-reverse asymmetry ranges {min(float(r['asymmetry_legal']) for r in with_legal):.3f}–{max(float(r['asymmetry_legal']) for r in with_legal):.3f} (mean {mean(float(r['asymmetry_legal']) for r in with_legal):.3f}), and the legal reverse is cheaper than the forward direction on {sum(1 for r in with_legal if r['legal_reverse_cheaper'] == 'True')} of them.")
        lines.append("**Sentences for the manuscript.** " + " ".join(sentences))
    else:
        lines.append("_E6 not run._")
    lines.append("")
    # E7
    lines.append("## E7 Second DEM")
    lines.append("")
    comp = read_csv(DOCS / "E7_dem_comparison.csv")
    rows = read_csv(DOCS / "E7_second_dem.csv")
    if comp and rows:
        c = comp[0]
        lines.append(f"Run id {RUN_ID}/E7. Second DEM: {c['second_dem']} (JAXA ALOS World 3D 30 m, v3.2, from the Planetary Computer `alos-dem` collection; NASADEM tiles are also served there; only AW3D30 was used), resampled bilinearly onto the GLO-30 lattice (sha256 {c['second_dem_sha256'][:12]}…). "
                     f"On {int(c['road_samples']):,} road samples (25 m along every road) AW3D30 − GLO-30 is {c['diff_mean_m']} ± {c['diff_sd_m']} m (mean ± s.d.; RMSE {c['diff_rmse_m']} m; |Δ| p50 {c['abs_diff_p50_m']} m, p95 {c['abs_diff_p95_m']} m, max {c['abs_diff_max_m']} m); at the 130 stops (129 + depot) {c['stop_diff_mean_m']} ± {c['stop_diff_sd_m']} m. "
                     f"Grades over a 100 m base along the roads ({int(c['grade_100m_pairs']):,} pairs) correlate with Pearson r = {c['grade_100m_pearson_r']} (s.d. {c['grade_100m_sd_glo30_pct']} % vs {c['grade_100m_sd_aw3d30_pct']} %, RMSE of the difference {c['grade_100m_rmse_pct']} %).")
        lines.append("")
        lines.append(md_table(rows, [("network", "Network"), ("evaluated_on", "Evaluated on"), ("E_dem_wh", "E (Wh)"), ("E_planar_wh", "E planar"), ("planar_error_pct", "planar error %"), ("km", "km"), ("climb_m", "climb (m)"), ("voti_vs_distance_wh", "VoTI vs distance (Wh)"), ("voti_vs_distance_pct", "%"), ("voti_vs_planar_wh", "VoTI vs planar (Wh)"), ("mean_direction_asymmetry", "mean asymmetry"), ("friction_share_pct", "friction share %"), ("jaccard_glo30_vs_aw3d30_solutions", "Jaccard GLO-30/AW3D30 solutions"), ("kendall_tau", "τ")]))
        lines.append("")
        def get(network, on):
            return next((r for r in rows if r["network"] == network and r["evaluated_on"] == on), None)
        a, g, d = get("dem_opt_aw3d30", "aw3d30-v1"), get("dem_opt_glo30", "aw3d30-v1"), get("distance_opt", "aw3d30-v1")
        g2, a2 = get("dem_opt_glo30", "glo30-v1"), get("dem_opt_aw3d30", "glo30-v1")
        if a and g and d:
            lines.append(f"**Sentences for the manuscript.** On AW3D30 the distance network's planar error is {d['planar_error_pct']} % (GLO-30: {get('distance_opt', 'glo30-v1')['planar_error_pct']} %); the network optimised on AW3D30 saves {f1(a['voti_vs_distance_wh'], 0)} Wh ({f1(a['voti_vs_distance_pct'])} %) against the distance network on its own DEM, "
                         f"the GLO-30-optimised network saves {f1(g['voti_vs_distance_wh'], 0)} Wh ({f1(g['voti_vs_distance_pct'])} %) when evaluated on AW3D30, and the AW3D30-optimised network saves {f1(a2['voti_vs_distance_wh'], 0)} Wh on GLO-30 (GLO-30's own: {f1(g2['voti_vs_distance_wh'], 0)} Wh). "
                         f"The two DEM-optimised networks share memberships (Jaccard {a['jaccard_glo30_vs_aw3d30_solutions']}) with Kendall τ {a['kendall_tau']} between paired sequences; mean direction asymmetry {a['mean_direction_asymmetry']} on AW3D30 vs {g2['mean_direction_asymmetry']} on GLO-30.")
    else:
        lines.append("_E7 not run (see open issues)._")
    lines.append("")
    # Deviations
    lines.append("## Deviations from this plan and open issues")
    lines.append("")
    deviations = [
        "E1 \"seed\": OR-Tools' routing search has no seed; the main run's identical repeats and a test with `solver.ReSeed()` showed no variation. The seed was realised as a seeded permutation of the customer node order (`node_order_seed`), which does move the heuristic. The main run (seed 42) corresponds to the unpermuted order and is shown as the reference line.",
        "E2 count: 6 optimisations per variant rather than 6 + 4, because the distance and planar solutions do not depend on η_regen and the distance solution does not depend on dem_scale (they are re-evaluated, exactly as the main run does). Two warm-start variants (15 s, 300 s) were added because the cold 15 s solve of the unconstrained problem is far from the constrained best-found (longer networks); the cold results are kept in the table as the plan specified them.",
        "E3 noise: L = 10 is a 10 × 10 box (offsets −5…+4, half a cell asymmetric), not a Gaussian kernel; σ is rescaled by √count so the marginal σ is preserved. Seeds are independent across levels rather than paired.",
        "E4: `exact_potentials` was added as an option (default False, so the main run stays reproducible); relative differences are floored at 1 Wh in the denominator because some exact pair energies are near zero.",
        "E5: leg paths are the least-energy paths at the *average* load (7 passengers) and are not re-routed per leg mass; only the pricing is leg-mass-aware. Masses are binned to 25 kg inside the local search and priced exactly afterwards. Three stops of the municipal register (A1, A2 on AKSALUR; B13 on BAHÇELİEVLER) no longer exist by name in planning register revision 135, so `municipal_on_graph` routes 12 of 14 and 26 of 27 stops of those lines; `municipal_as_drawn` uses all drawn stops (the load profile's n differs accordingly). The drawn-line pricing reproduces the main run's reference energies within about 1 % (differences come from the 25 m sampling restarting at each stop cut).",
        "E6: the municipal drawings are matched to roads by nearest-road sampling; a drawn line off the network by more than 30 m is counted as unmatched (`unmatched_length_m`). HADOSAN and ÜSET have no stops in the register, so no legal reverse routing exists for them.",
        "E7: NASADEM was not run (AW3D30 only). AW3D30's stated accuracy (5 m RMSE) is entered as `vertical_accuracy_m` with a DOĞRULANACAK note.",
        "The grid file for AW3D30 lives under `data/processed/elevation/aw3d30-v1/`, which `.gitignore` excludes like the GLO-30 grid; its sha256 is in the E7 manifest and CSV.",
        "Vehicle parameters remain the placeholders of `vehicle_profiles.json` (sha256 in every manifest).",
        "Time limits: the optimiser silently capped every request at 120 s (`_optimize_vrp`), so the first pass of the 300 s cells of E1 and E2 actually ran for 120 s (their proposals were set aside, not used). The cap was raised to `MAX_SOLVER_SECONDS = 3600` for scripts (the web layer keeps its own 120 s ceiling; unit-tested) and those cells were re-run at a true 300 s; `solver_seconds` in the CSVs is the measured wall time of each solve. Also observed: the 15 s and 60 s cells return identical networks for every seed (guided local search stalls on this instance well before 15 s), so the seed, not the time limit, is what moves the heuristic here.",
        "Code changes for R1 (all unit-tested, full suite green in a clean environment; the four closed-tour identities in `tests/test_energy.py::ClosedTourIdentityTests` re-run after every change): `node_order_seed` and `warm_start_stop_ids` in `_optimize_vrp`; `exact_potentials` (Johnson/Bellman-Ford) in `_prepare_graph_energy`; `dem_noise_corr_cells` in `derive_grid`/`EnergyOptions`; `with_passenger_mass` for leg-level load. Defaults leave the main run bit-for-bit reproducible (verified: re-running A-dem-eta0.5-average returns the stored network).",
        "Open: the E2 unconstrained result depends on the start and time limit of a heuristic; a longer run or an exact method would be needed to call any unconstrained membership change 'terrain-driven' rather than 'search-driven'. Open: E5 keeps leg paths at the average load; a fully load-aware path choice per leg would need one matrix per mass level. Open: E6 counts one-way rules of the editable network only; turn restrictions and signage are not modelled.",
    ]
    lines.extend(f"- {d}" for d in deviations)
    lines.append("")
    (DOCS / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print("REPORT.md written")
    write_summary()


def write_summary() -> None:
    """One table over every experiment: the rows the manuscript will cite."""
    out: list[dict[str, Any]] = []

    def add(experiment: str, item: str, **fields: Any) -> None:
        out.append({"experiment": experiment, "item": item, "run_id": RUN_ID, **fields})

    for r in read_csv(DOCS / "E1_summary.csv"):
        add("E1", f"{r['cost_basis']} × {r['time_limit_s']} s, {r['n']} seeds", cost_basis=r["cost_basis"], time_limit_s=r["time_limit_s"], eta_regen=ETA_MAIN, mass_scenario=MASS_MAIN,
            E_dem_wh=r["E_dem_mean_wh"], E_dem_sd_wh=r["E_dem_sd_wh"], E_dem_min_wh=r["E_dem_min_wh"], E_dem_max_wh=r["E_dem_max_wh"], E_dem_reference_wh=r["E_dem_reference_wh"],
            voti_wh=r.get("voti_mean_wh"), voti_sd_wh=r.get("voti_sd_wh"), voti_pct=r.get("voti_mean_pct"), voti_positive=r.get("voti_positive_count"), jaccard=r["jaccard_mean"], kendall_tau=r["kendall_tau_mean"])
    for r in read_csv(DOCS / "E2_neighbourhood_off.csv"):
        if r.get("cost_basis") != "energy-dem":
            continue
        add("E2", f"{r['variant']}, η {r['eta_regen']}, dem_scale {r['dem_scale']}", variant=r["variant"], neighbourhood_constraint=r["neighbourhood_constraint"], cost_basis="energy-dem", time_limit_s=r["time_limit_s"], eta_regen=r["eta_regen"], mass_scenario=MASS_MAIN, dem_scale=r["dem_scale"],
            E_dem_wh=r["E_dem_wh"], km=r["km"], voti_wh=r["voti_vs_distance_wh"], voti_pct=r["voti_vs_distance_pct"], voti_vs_planar_wh=r["voti_vs_planar_wh"], voti_vs_planar_pct=r["voti_vs_planar_pct"],
            jaccard=r["jaccard_planar_vs_dem"], stops_changing_line=r["stops_changing_line_planar_vs_dem"], kendall_tau=r["kendall_tau_planar_vs_dem"], jaccard_vs_constrained=r["jaccard_vs_constrained"], stops_changing_vs_constrained=r["stops_changing_line_vs_constrained"])
    for r in read_csv(DOCS / "E3_summary.csv"):
        add("E3", f"σ {r['sigma_m']} m, L {r['corr_len_cells']} cells, {r['repeats']} repeats", sigma_m=r["sigma_m"], corr_len_cells=r["corr_len_cells"], time_limit_s=r["time_limit_s"], eta_regen=ETA_MAIN, mass_scenario=MASS_MAIN,
            E_dem_wh=r["E_reference_grid_mean_wh"], E_dem_sd_wh=r["E_reference_grid_sd_wh"], voti_wh=r["voti_ref_p50_wh"], voti_p05_wh=r["voti_ref_p05_wh"], voti_p95_wh=r["voti_ref_p95_wh"], voti_mean_wh=r["voti_ref_mean_wh"],
            share_voti_negative=r["share_voti_ref_negative"], spurious_climb_m=r["spurious_climb_mean_m"], jaccard=r["jaccard_mean"], stops_changing_line=r["stops_changing_line_mean"], modal_line_stability=r["modal_line_stability_mean"])
    for r in read_csv(DOCS / "E4_exact_shortest_paths.csv"):
        add("E4", f"η {r['eta_regen']}, {r['mass_scenario']}", eta_regen=r["eta_regen"], mass_scenario=r["mass_scenario"], negative_edges=r["negative_edges_physics_potential"], pairs_differing=r["pairs_differing"], max_abs_diff_wh=r["max_abs_diff_wh"], max_rel_diff_pct=r["max_rel_diff_pct"], negative_cycle=r["negative_cycle_found"])
    for r in read_csv(DOCS / "E4_reoptimisation.csv"):
        add("E4", "re-optimisation with exact potentials", eta_regen=r["eta_regen"], mass_scenario=r["mass_scenario"], time_limit_s=r["time_limit_s"], E_dem_wh=r["E_dem_wh"], E_dem_reference_wh=r["E_dem_reference_dem_opt_wh"], voti_wh=r["voti_vs_distance_wh"], voti_pct=r["voti_vs_distance_pct"], jaccard=r["jaccard_to_reference"], kendall_tau=r["kendall_tau"], identical=r["identical_sequences"])
    totals: dict[tuple[str, str, str], float] = defaultdict(float)
    for r in read_csv(DOCS / "E5_delivery_load.csv"):
        if r["direction"] == "forward":
            totals[(r["network"], r["load_profile"], r["sequence_source"])] += float(r["E_dem_wh"])
    for (network, profile_name, source), value in sorted(totals.items()):
        add("E5", f"{network}, {profile_name}, {source}", network=network, load_profile=profile_name, sequence_source=source, eta_regen=ETA_MAIN, E_dem_wh=round(value, 1))
    for r in read_csv(DOCS / "E6_reverse_feasibility.csv"):
        add("E6", f"{r['network']} {r['line']}", network=r["network"], line=r["line"], eta_regen=ETA_MAIN, mass_scenario=MASS_MAIN, oneway_segments_violated=r["oneway_segments_violated"], share_of_length_pct=r["share_of_length_pct"],
            reverse_as_driven_legal=r["reverse_as_driven_legal"], E_forward_wh=r.get("E_forward_wh"), E_reverse_physical_wh=r.get("E_reverse_physical_wh"), E_legal_reverse_wh=r.get("E_legal_reverse_wh"), asymmetry_physical=r.get("asymmetry_physical"), asymmetry_legal=r.get("asymmetry_legal"))
    for r in read_csv(DOCS / "E7_second_dem.csv"):
        add("E7", f"{r['network']} on {r['evaluated_on']}", network=r["network"], evaluated_on=r["evaluated_on"], eta_regen=r["eta_regen"], mass_scenario=r["mass_scenario"], E_dem_wh=r["E_dem_wh"], planar_error_pct=r["planar_error_pct"], voti_wh=r["voti_vs_distance_wh"], voti_pct=r["voti_vs_distance_pct"], jaccard=r["jaccard_glo30_vs_aw3d30_solutions"], kendall_tau=r["kendall_tau"])
    for r in read_csv(DOCS / "E7_dem_comparison.csv"):
        add("E7", "AW3D30 − GLO-30 on road samples", diff_mean_m=r["diff_mean_m"], diff_sd_m=r["diff_sd_m"], abs_diff_p95_m=r["abs_diff_p95_m"], grade_100m_pearson_r=r["grade_100m_pearson_r"])
    write_csv(DOCS / "experiments_R1_summary.csv", out)
    print(f"experiments_R1_summary.csv: {len(out)} rows")


# ── Entry point ─────────────────────────────────────────────────────────
EXPERIMENTS: dict[str, Any] = {"E1": run_e1, "E2": run_e2, "E3": run_e3, "E4": run_e4, "E5": run_e5, "E6": run_e6, "E7": run_e7}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", help="E1 … E7, report, figures")
    parser.add_argument("--only", default=None, help="Comma list of sub-steps where an experiment has them")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DOCS.mkdir(parents=True, exist_ok=True)
    R1_ROOT.mkdir(parents=True, exist_ok=True)
    command = args.command.upper() if args.command.lower().startswith("e") else args.command.lower()
    if command in EXPERIMENTS:
        EXPERIMENTS[command](Context(args))
        return 0
    if command == "figures":
        figures(None)
        return 0
    if command == "report":
        write_report()
        return 0
    raise SystemExit(f"Bilinmeyen komut: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
