"""Persistent background jobs for multi-mode route optimization."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from urgup_transport import db, jobs, stores
from urgup_transport.config import DEFAULT_LAYOVER_MINUTES, DEFAULT_MAX_STOP_SNAP_DISTANCE_M
from urgup_transport.editor_store import DraftValidationError, atomic_write_json
from urgup_transport.optimizer import (
    DEFAULT_EDITABLE_ROAD_NETWORK,
    PlanningValidationError,
    optimize,
)
from urgup_transport.routing_contract import ROUTING_MODEL_VERSION

ROOT = Path(__file__).resolve().parents[1]
JOB_DIR = ROOT / "outputs/intermediate/optimization/jobs"
PROPOSAL_DIR = ROOT / "outputs/intermediate/optimization/proposals"

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="optimizer")
_lock = threading.Lock()
_active_job_id: str | None = None

PROFILE_DEFAULTS = {
    "quick": {"population_size": 40, "generations": 60, "mutation_rate": 0.10, "solver_time_limit_seconds": 5},
    "balanced": {"population_size": 90, "generations": 140, "mutation_rate": 0.08, "solver_time_limit_seconds": 15},
    "detailed": {"population_size": 160, "generations": 300, "mutation_rate": 0.06, "solver_time_limit_seconds": 30},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_path(job_id: str) -> Path:
    return JOB_DIR / f"{job_id}.json"


def _proposal_path(proposal_id: str) -> Path:
    return PROPOSAL_DIR / f"{proposal_id}.json"


def _valid_id(value: str) -> bool:
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value.lower())


def _write_job(job_id: str, payload: dict[str, Any]) -> None:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    payload["job_id"] = job_id
    atomic_write_json(_job_path(job_id), payload)


def get_job(job_id: str) -> dict[str, Any] | None:
    """Read a job from wherever it was queued."""
    if db.database_configured():
        return flatten_job(jobs.get(job_id))
    if not _valid_id(job_id):
        return None
    try:
        return json.loads(_job_path(job_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def flatten_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    """Present a queued row in the shape the editor already reads.

    The row keeps the outcome under `result`; the editor reads `job.error`,
    `job.proposal_id` and `job.diagnostics` at the top level. Flattening here
    keeps one client contract across both queues.
    """
    if job is None:
        return None
    result = job.get("result") or {}
    flattened = {**job, **{key: value for key, value in result.items() if key != "success"}}
    if "progress" in result:
        flattened["progress"] = result["progress"]
    return flattened


#: How many past runs the list offers. The directory keeps every proposal ever
#: generated — 143 of them, 41 MB — and reading all of it to answer one request
#: would cost a second for rows nobody scrolls to.
MAX_LISTED_PROPOSALS = 20

#: Header fields worth carrying into the list. `layers` is the other 99% of a
#: proposal file and is never needed to decide whether to open one.
_SUMMARY_FIELDS = (
    "proposal_id", "generated_at_utc", "input_revision", "road_network_revision",
    "road_network_digest", "routing_model_version", "speed_profile_version",
    "scenario_type", "algorithm",
)

_summary_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _proposal_summary(path: Path) -> dict[str, Any] | None:
    """The header of one proposal, cached against the file's mtime."""
    key = str(path)
    stamp = path.stat().st_mtime
    cached = _summary_cache.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    metrics = payload.get("metrics") or {}
    summary = {field: payload.get(field) for field in _SUMMARY_FIELDS}
    summary["total_distance_m"] = metrics.get("total_distance_m")
    summary["total_road_travel_time_minutes"] = metrics.get("total_road_travel_time_minutes")
    summary["route_count"] = len(((payload.get("layers") or {}).get("routes") or {}).get("features") or [])
    _summary_cache[key] = (stamp, summary)
    return summary


def list_proposals(
    *,
    draft_revision: int | None = None,
    road_snapshot: dict[str, Any] | None = None,
    applied_proposal_id: str | None = None,
    limit: int = MAX_LISTED_PROPOSALS,
) -> list[dict[str, Any]]:
    """Recent optimization runs, newest first, each saying what it was computed against.

    A proposal is only as good as the network it was optimized on, and nothing
    on screen used to say which one that was. A run from this morning and one
    from after a road-class change look identical in a list of distances, and
    applying the older one silently publishes a plan built on a network that no
    longer exists. So every row carries the road revision and digest it was
    bound to, and `stale` says plainly when they no longer match.
    """
    if not PROPOSAL_DIR.exists():
        return []
    paths = sorted(PROPOSAL_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    rows: list[dict[str, Any]] = []
    for path in paths[: max(0, limit)]:
        summary = _proposal_summary(path)
        if summary is None:
            continue
        reasons = []
        if road_snapshot is not None and summary.get("road_network_revision") is not None:
            if int(summary["road_network_revision"]) != int(road_snapshot.get("revision", -1)):
                reasons.append("yol_agi_revizyonu")
            elif summary.get("road_network_digest") != road_snapshot.get("digest"):
                reasons.append("yol_agi_icerigi")
        if (
            draft_revision is not None
            and summary.get("input_revision") is not None
            and int(summary["input_revision"]) != int(draft_revision)
        ):
            reasons.append("taslak_revizyonu")
        if summary.get("routing_model_version") != ROUTING_MODEL_VERSION:
            reasons.append("yonlendirme_modeli")
        rows.append({
            **summary,
            "applied": bool(applied_proposal_id) and summary["proposal_id"] == applied_proposal_id,
            "stale": bool(reasons),
            "stale_reasons": reasons,
        })
    return rows


def get_proposal(proposal_id: str) -> dict[str, Any] | None:
    if not _valid_id(proposal_id):
        return None
    try:
        return json.loads(_proposal_path(proposal_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def sanitize_optimization_params(params: dict[str, Any]) -> dict[str, Any]:
    planning_mode = str(params.get("planning_mode") or "vrp").lower()
    if planning_mode not in {"preserve", "vrp", "ga"}:
        raise ValueError("Planlama modu preserve, vrp veya ga olmalı.")
    route_shape = str(params.get("route_shape") or "shortest_closed").lower()
    if route_shape not in {"shortest_closed", "prefer_ring", "strict_ring"}:
        raise ValueError("Rota biçimi shortest_closed, prefer_ring veya strict_ring olmalı.")
    cost_basis = str(params.get("cost_basis") or "distance").lower()
    if cost_basis not in {"distance", "travel_time", "energy"}:
        raise ValueError("Maliyet temeli distance, travel_time veya energy olmalı.")
    routing_mode = str(params.get("routing_mode") or "road").lower()
    if routing_mode not in {"road", "haversine_analysis"}:
        raise ValueError("Routing modu road veya haversine_analysis olmalı.")
    profile = str(params.get("profile") or "balanced").lower()
    if profile not in PROFILE_DEFAULTS:
        raise ValueError("Profil quick, balanced veya detailed olmalı.")
    resolved = {
        **PROFILE_DEFAULTS[profile],
        "profile": profile,
        "planning_mode": planning_mode,
        "route_shape": route_shape,
        "cost_basis": cost_basis,
        "routing_mode": routing_mode,
    }
    numeric = {
        "route_count": (1, 25, int),
        "population_size": (12, 500, int),
        "generations": (5, 2000, int),
        "mutation_rate": (0, 1, float),
        "tournament_k": (2, 20, int),
        "exact_tsp_limit": (2, 8, int),
        "seed": (0, 2_147_483_647, int),
        "avg_speed_kmh": (5, 120, float),
        "dwell_time_seconds": (0, 600, float),
        "layover_minutes": (0, 240, float),
        "max_stop_snap_distance_m": (1, 5_000, float),
        "solver_time_limit_seconds": (1, 120, int),
        "max_route_distance_km": (0, 500, float),
        "max_route_duration_minutes": (0, 1440, float),
        "min_stops_per_route": (0, 1000, int),
        "max_stops_per_route": (0, 1000, int),
        "candidate_detour_m": (0, 20_000, float),
        "small_mahalle_stop_limit": (0, 200, int),
        "vehicle_capacity": (0, 1_000_000, float),
        "distance_balance_weight": (0, 100, int),
        "demand_balance_weight": (0, 100, int),
        "longest_route_weight": (0, 100, int),
    }
    defaults = {
        "route_count": 7,
        "tournament_k": 5,
        "exact_tsp_limit": 8,
        "seed": 42,
        "avg_speed_kmh": 30,
        "dwell_time_seconds": 30,
        "layover_minutes": DEFAULT_LAYOVER_MINUTES,
        "max_stop_snap_distance_m": DEFAULT_MAX_STOP_SNAP_DISTANCE_M,
        "max_route_distance_km": 0,
        "max_route_duration_minutes": 0,
        "min_stops_per_route": 1,
        "max_stops_per_route": 0,
        # Zero means "only where it costs nothing": a stopping place the
        # vehicle already drives past is taken, one needing a detour is not.
        "candidate_detour_m": 0,
        # 0 = off. Every neighbourhood keeps its own claim on a route.
        "small_mahalle_stop_limit": 0,
        "vehicle_capacity": 0,
        "distance_balance_weight": 20,
        "demand_balance_weight": 20,
        "longest_route_weight": 28,
    }
    for key, (lower, upper, cast) in numeric.items():
        if key in {"vehicle_capacity", "demand_balance_weight"} and key not in params:
            # Accepted only as deprecated API compatibility inputs. They no
            # longer participate in stop-to-route assignment.
            continue
        value = params.get(key, resolved.get(key, defaults.get(key)))
        try:
            value = cast(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} geçerli bir sayı olmalı.") from exc
        if not lower <= value <= upper:
            raise ValueError(f"{key} {lower} ile {upper} arasında olmalı.")
        resolved[key] = value
    # Both values were just range-checked above, so the cast is total.
    search_space = float(resolved["population_size"]) * float(resolved["generations"])  # type: ignore[arg-type]
    if search_space > 200_000:
        raise ValueError("Popülasyon × nesil en fazla 200.000 olabilir.")
    for flag in ("single_route_per_mahalle", "include_stopping_places"):
        resolved[flag] = params.get(flag) in {True, "1", "true", "yes", "on"}
    for key, default in (
        ("depot_id", ""),
        ("scenario_id", "current-center"),
        ("scenario_type", "CURRENT_NETWORK"),
    ):
        value = str(params.get(key) or default).strip()
        if len(value) > 160:
            raise ValueError(f"{key} en fazla 160 karakter olabilir.")
        resolved[key] = value
    # Energy: the vehicle and the terrain options ride along explicitly, or the
    # whitelist above would drop them and the run would silently use defaults.
    resolved["report_energy"] = params.get("report_energy") in {True, "1", "true", "yes", "on"}
    resolved["keep_stop_order"] = params.get("keep_stop_order") in {True, "1", "true", "yes", "on"}
    if cost_basis == "energy" or resolved["report_energy"]:
        profile = str(params.get("vehicle_profile") or "").strip()
        if len(profile) > 80:
            raise ValueError("vehicle_profile en fazla 80 karakter olabilir.")
        resolved["vehicle_profile"] = profile or None
        resolved["energy_options"] = sanitize_energy_options(params.get("energy_options") or {})
    return resolved


def sanitize_energy_options(raw: Any) -> dict[str, Any]:
    """The terrain and vehicle sweep options a request may set, range-checked.

    Paths (a different grid directory, a different profile file) are not
    accepted from the web: those are experiment-script inputs.
    """
    if not isinstance(raw, dict):
        raise ValueError("energy_options bir nesne olmalı.")
    surface = str(raw.get("surface") or "dem").lower()
    if surface not in {"dem", "planar"}:
        raise ValueError("energy_options.surface dem veya planar olmalı.")
    mass_scenario = str(raw.get("mass_scenario") or "average").lower()
    if mass_scenario not in {"empty", "average", "full"}:
        raise ValueError("energy_options.mass_scenario empty, average veya full olmalı.")
    resolved: dict[str, Any] = {"surface": surface, "mass_scenario": mass_scenario}
    numeric = {
        "eta_regen": (0.0, 1.0),
        "p_regen_max_kw": (0.0, 1000.0),
        "dem_scale": (0.0, 3.0),
        "dem_noise_sigma_m": (0.0, 20.0),
        "sample_step_m": (5.0, 200.0),
        "smooth_window_m": (0.0, 500.0),
        "grade_cap": (0.01, 1.0),
    }
    for key, (lower, upper) in numeric.items():
        if raw.get(key) in (None, ""):
            continue
        try:
            value = float(raw[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"energy_options.{key} geçerli bir sayı olmalı.") from exc
        if not lower <= value <= upper:
            raise ValueError(f"energy_options.{key} {lower} ile {upper} arasında olmalı.")
        resolved[key] = value
    if raw.get("seed") not in (None, ""):
        try:
            resolved["seed"] = int(raw["seed"])
        except (TypeError, ValueError) as exc:
            raise ValueError("energy_options.seed tam sayı olmalı.") from exc
    if raw.get("dem_noise_corr_cells") not in (None, ""):
        try:
            corr = int(raw["dem_noise_corr_cells"])
        except (TypeError, ValueError) as exc:
            raise ValueError("energy_options.dem_noise_corr_cells tam sayı olmalı.") from exc
        if not 1 <= corr <= 50:
            raise ValueError("energy_options.dem_noise_corr_cells 1 ile 50 arasında olmalı.")
        resolved["dem_noise_corr_cells"] = corr
    if "exact_potentials" in raw:
        resolved["exact_potentials"] = raw.get("exact_potentials") in {True, "1", "true", "yes", "on"}
    return resolved


def optimization_progress(params: dict[str, Any], current_step: int, total: int, fitness: float) -> dict[str, Any]:
    """The progress shape the editor renders, for either execution path."""
    unit = "generation" if params.get("planning_mode") == "ga" else "stage"
    return {
        "current": current_step,
        "total": total,
        "unit": unit,
        "percent": round(current_step / max(1, total) * 100, 1),
        "best_fitness": round(fitness, 2),
    }


def execute_optimization(
    params: dict[str, Any],
    draft: dict[str, Any],
    *,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Run the optimizer and return what the job's result should hold.

    Shared by the in-process runner and the database worker, so the two cannot
    diverge on what a success or a failure looks like. A planning validation
    failure is a result, not an exception: it carries diagnostics the editor
    shows, and the job did finish.
    """

    def report(current_step: int, total: int, fitness: float) -> None:
        if on_progress is not None:
            on_progress(optimization_progress(params, current_step, total, fitness))

    try:
        proposal = optimize(draft, params, progress=report)
    except PlanningValidationError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_code": exc.code,
            "diagnostics": exc.diagnostics,
        }

    PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_proposal_path(proposal["proposal_id"]), proposal)
    total = params["generations"] if params.get("planning_mode") == "ga" else 1
    return {
        "success": True,
        "proposal_id": proposal["proposal_id"],
        "metrics": proposal["metrics"],
        "progress": {
            "current": total,
            "total": total,
            "unit": "generation" if params.get("planning_mode") == "ga" else "stage",
            "percent": 100,
        },
    }


def _run(job_id: str, params: dict[str, Any], draft: dict[str, Any]) -> None:
    """In-process runner, used when no database queue is configured."""
    global _active_job_id
    started = _now()
    _write_job(
        job_id,
        {
            "status": "running",
            "created_at_utc": started,
            "started_at_utc": started,
            "input_revision": int(draft.get("revision", 0)),
            "parameters": params,
            "progress": optimization_progress(
                params, 0, params["generations"] if params["planning_mode"] == "ga" else 1, 0.0
            ),
        },
    )

    def report(progress: dict[str, Any]) -> None:
        current = get_job(job_id) or {}
        current.update({"status": "running", "progress": progress})
        _write_job(job_id, current)

    try:
        result = execute_optimization(params, draft, on_progress=report)
        current = get_job(job_id) or {}
        current.update({"finished_at_utc": _now(), **result})
        current["status"] = "succeeded" if result.get("success") else "failed"
        current.pop("success", None)
        _write_job(job_id, current)
    except Exception as exc:
        logging.getLogger(__name__).exception("Optimization job failed: %s", exc)
        current = get_job(job_id) or {}
        current.update(
            {
                "status": "failed",
                "finished_at_utc": _now(),
                "error": "Optimizasyon beklenmeyen bir nedenle tamamlanamadı.",
            }
        )
        _write_job(job_id, current)
    finally:
        with _lock:
            if _active_job_id == job_id:
                _active_job_id = None


def start_job(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Queue an optimization, in the database where there is one.

    With a database the job is a row a separate worker claims, so the web
    process can be restarted or scaled without losing it. Without one it runs
    here, on the single-slot pool that forces `--workers 1`.
    """
    global _active_job_id
    resolved = sanitize_optimization_params(params or {})
    draft = stores.editor_store().read()

    if db.database_configured():
        try:
            queued = jobs.enqueue("optimization", resolved)
        except jobs.JobAlreadyRunning as exc:
            raise RuntimeError(str(exc)) from exc
        return {
            **queued,
            "input_revision": int(draft.get("revision", 0)),
            "progress": optimization_progress(
                resolved, 0, resolved["generations"] if resolved["planning_mode"] == "ga" else 1, 0.0
            ),
        }

    with _lock:
        if _active_job_id:
            active = get_job(_active_job_id)
            if active and active.get("status") in {"queued", "running"}:
                raise RuntimeError("Başka bir optimizasyon çalışıyor.")
        job_id = uuid.uuid4().hex
        _active_job_id = job_id
        payload = {
            "status": "queued",
            "created_at_utc": _now(),
            "input_revision": int(draft.get("revision", 0)),
            "parameters": resolved,
            "progress": {
                "current": 0,
                "total": resolved["generations"] if resolved["planning_mode"] == "ga" else 1,
                "unit": "generation" if resolved["planning_mode"] == "ga" else "stage",
                "percent": 0,
            },
        }
        _write_job(job_id, payload)
        _executor.submit(_run, job_id, resolved, draft)
    return get_job(job_id) or {**payload, "job_id": job_id}


def _without_candidate_stops(proposal: dict[str, Any]) -> dict[str, Any]:
    """The proposal as the draft should receive it: stops only.

    A stopping place is not a stop. It has no sign and no shelter, and the
    reason it exists as a separate layer is to stay told apart from the ones
    that do — so applying a plan must not quietly turn it into one. Writing
    them in did exactly that: twenty-eight of them entered the stop inventory
    and, on the next apply, lost even the marker saying where they came from.

    The proposal itself is left whole, because the map reads it to show which
    places the plan adopted. Only the draft write is filtered.

    Sequences are renumbered per route afterwards. Leaving the gaps would
    describe a route that calls at 0, 1, 3, 6 — readable, but every later
    reader would have to know why.
    """
    layers = proposal.get("layers") or {}
    stops = (layers.get("stops") or {}).get("features") or []
    if not any((feature.get("properties") or {}).get("candidate_stop") for feature in stops):
        return proposal

    kept = [
        feature for feature in stops
        if not (feature.get("properties") or {}).get("candidate_stop")
    ]
    counters: dict[str, int] = {}
    renumbered = []
    for feature in kept:
        properties = dict(feature.get("properties") or {})
        route = str(properties.get("route_id") or properties.get("folder_path") or "")
        properties["sequence"] = counters.get(route, 0)
        counters[route] = properties["sequence"] + 1
        renumbered.append({**feature, "properties": properties})

    return {
        **proposal,
        "layers": {
            **layers,
            "stops": {**(layers.get("stops") or {}), "features": renumbered},
        },
    }


def apply_proposal(
    proposal_id: str,
    expected_revision: int | None,
    expected_road_revision: int | None = None,
    *,
    actor: Any = None,
) -> dict[str, Any]:
    proposal = get_proposal(proposal_id)
    if proposal is None:
        raise KeyError("Optimizasyon önerisi bulunamadı.")
    snapshot_kind = str((proposal.get("routing_snapshot") or {}).get("kind") or proposal.get("distance_source") or "")
    if snapshot_kind == "cached_osm_drive_graph":
        raise DraftValidationError(
            "Öneri önbellek OSM grafiğiyle üretildi. Uygulamadan önce düzenlenebilir "
            "yol ağını oluşturup optimizasyonu yenileyin."
        )
    road_bound = snapshot_kind == "editable_road_network" or proposal.get("road_network_revision") is not None
    return stores.editor_store().apply_optimization(
        _without_candidate_stops(proposal),
        expected_revision=expected_revision,
        expected_road_revision=expected_road_revision if road_bound else None,
        road_network_path=DEFAULT_EDITABLE_ROAD_NETWORK if road_bound else None,
        actor=actor,
    )
