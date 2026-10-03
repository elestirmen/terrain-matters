"""
Energy of driving a road segment, one direction at a time.

The planner is two-dimensional and stays that way: nothing here writes a third
ordinate or a grade onto any stored geometry (`draft_store.flatten_to_2d` still
refuses one). Energy is derived at read time from (geometry, terrain grid,
vehicle profile) exactly as `elevation.py` derives a climb, and it is labelled
with the same `elevation_source` / `elevation_confidence` contract.

Deliberately stdlib-only, like `elevation.py`: the production service must not
grow a runtime dependency for a research module.

The model is longitudinal vehicle dynamics at the segment's assumed constant
speed: rolling resistance, aerodynamic drag, grade, an auxiliary load, and a
regenerative-braking path with its own efficiency and a power cap. The power
cap is what makes the two directions of a loop differ — a gentle descent only
reduces traction, a steep one exceeds what the drivetrain can absorb and the
rest is heat in the friction brakes. Design note: docs/paper/01-enerji-modeli-
ve-kod-tasarimi.md; the numbers in `vehicle_profiles.json` are placeholders
until each has a source.
"""

from __future__ import annotations

import array
import json
import math
import random
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from .config import DEFAULT_VEHICLE_PROFILES, PROJECT_ROOT
from .elevation import ElevationGrid, haversine_m, resample_line

G = 9.81
J_PER_WH = 3600.0
ENERGY_MODEL_VERSION = "segment_model_v1"
SURFACES = ("dem", "planar")
MASS_SCENARIOS = ("empty", "average", "full")


class EnergyConfigurationError(ValueError):
    """A vehicle profile or option that cannot be used as given."""


# ── Parameters ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class VehicleProfile:
    name: str
    mass_empty_kg: float
    passenger_mass_kg: float
    c_rr: float
    cd_a_m2: float
    eta_drive: float
    eta_regen: float
    p_regen_max_w: float
    p_aux_w: float
    air_density: float = 1.15
    #: Recovery in a stop braking event; ``None`` means "same as eta_regen".
    eta_regen_stop: float | None = None
    mass_scenario: str = "average"

    def __post_init__(self) -> None:
        if self.mass_empty_kg <= 0 or self.passenger_mass_kg < 0:
            raise EnergyConfigurationError("Kütle pozitif olmalı.")
        if not 0 < self.eta_drive <= 1:
            raise EnergyConfigurationError("eta_drive (0, 1] aralığında olmalı.")
        if not 0 <= self.eta_regen <= 1:
            raise EnergyConfigurationError("eta_regen [0, 1] aralığında olmalı.")
        if self.p_regen_max_w < 0 or self.p_aux_w < 0:
            raise EnergyConfigurationError("Güç değerleri negatif olamaz.")
        if self.c_rr < 0 or self.cd_a_m2 < 0 or self.air_density <= 0:
            raise EnergyConfigurationError("Direnç katsayıları negatif olamaz.")

    @property
    def mass_kg(self) -> float:
        return self.mass_empty_kg + self.passenger_mass_kg

    @property
    def stop_regen(self) -> float:
        return self.eta_regen if self.eta_regen_stop is None else self.eta_regen_stop

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mass_scenario": self.mass_scenario,
            "mass_empty_kg": self.mass_empty_kg,
            "passenger_mass_kg": self.passenger_mass_kg,
            "mass_kg": self.mass_kg,
            "c_rr": self.c_rr,
            "cd_a_m2": self.cd_a_m2,
            "eta_drive": self.eta_drive,
            "eta_regen": self.eta_regen,
            "eta_regen_stop": self.stop_regen,
            "p_regen_max_w": self.p_regen_max_w,
            "p_aux_w": self.p_aux_w,
            "air_density": self.air_density,
        }


@dataclass(frozen=True)
class EnergyOptions:
    surface: str = "dem"
    sample_step_m: float = 25.0
    smooth_window_m: float = 100.0
    grade_cap: float = 0.20
    #: Terrain-intensity sweep: h' = h_ref + dem_scale · (h − h_ref).
    dem_scale: float = 1.0
    #: Monte Carlo: spatially smoothed Gaussian noise added to the grid.
    dem_noise_sigma_m: float = 0.0
    #: Width, in cells, of the box mean that correlates the noise (3 = the 3×3
    #: neighbourhood of the first runs; 10 = a 10×10 window, ~300 m).
    dem_noise_corr_cells: int = 3
    seed: int | None = None
    #: Shortest-path reweighting: ``False`` uses the elevation potential
    #: π = η_regen·m·g·h and clips the few corrected weights the grade cap
    #: leaves negative; ``True`` computes Johnson potentials by Bellman-Ford on
    #: the true energies, so every corrected weight is non-negative and the
    #: Dijkstra matrix is exact.
    exact_potentials: bool = False

    def __post_init__(self) -> None:
        if self.surface not in SURFACES:
            raise EnergyConfigurationError("surface dem veya planar olmalı.")
        if self.sample_step_m <= 0:
            raise EnergyConfigurationError("sample_step_m pozitif olmalı.")
        if self.smooth_window_m < 0 or self.grade_cap <= 0:
            raise EnergyConfigurationError("Yumuşatma penceresi ve eğim sınırı geçersiz.")
        if self.dem_scale < 0 or self.dem_noise_sigma_m < 0:
            raise EnergyConfigurationError("dem_scale ve gürültü negatif olamaz.")
        if int(self.dem_noise_corr_cells) < 1:
            raise EnergyConfigurationError("dem_noise_corr_cells en az 1 olmalı.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "sample_step_m": self.sample_step_m,
            "smooth_window_m": self.smooth_window_m,
            "grade_cap": self.grade_cap,
            "dem_scale": self.dem_scale,
            "dem_noise_sigma_m": self.dem_noise_sigma_m,
            "dem_noise_corr_cells": self.dem_noise_corr_cells,
            "seed": self.seed,
            "exact_potentials": self.exact_potentials,
        }


# ── Result ───────────────────────────────────────────────────────────────
@dataclass
class SegmentEnergy:
    """Energy of one directed traversal, with where it went."""

    total_j: float = 0.0
    traction_j: float = 0.0
    regen_j: float = 0.0
    aux_j: float = 0.0
    #: Wheel braking energy above the regeneration cap: heat in the brakes.
    friction_loss_j: float = 0.0
    climb_m: float = 0.0
    descent_m: float = 0.0
    length_m: float = 0.0
    slope_length_m: float = 0.0
    seconds: float = 0.0
    n_capped: int = 0
    n_nodata: int = 0
    n_subsegments: int = 0

    def add(self, other: "SegmentEnergy") -> "SegmentEnergy":
        for name in (
            "total_j", "traction_j", "regen_j", "aux_j", "friction_loss_j",
            "climb_m", "descent_m", "length_m", "slope_length_m", "seconds",
            "n_capped", "n_nodata", "n_subsegments",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        return self

    def as_wh_dict(self, digits: int = 2) -> dict[str, Any]:
        return {
            "energy_wh": round(self.total_j / J_PER_WH, digits),
            "traction_wh": round(self.traction_j / J_PER_WH, digits),
            "regen_wh": round(self.regen_j / J_PER_WH, digits),
            "aux_wh": round(self.aux_j / J_PER_WH, digits),
            "friction_loss_wh": round(self.friction_loss_j / J_PER_WH, digits),
            "climb_m": round(self.climb_m, 1),
            "descent_m": round(self.descent_m, 1),
            "length_m": round(self.length_m, 1),
            "slope_length_m": round(self.slope_length_m, 1),
            "seconds": round(self.seconds, 1),
            "n_capped": self.n_capped,
            "n_nodata": self.n_nodata,
        }


# ── Core physics ─────────────────────────────────────────────────────────
def _subsegment(length_m: float, slope: float, speed_mps: float, profile: VehicleProfile) -> SegmentEnergy:
    """Energy of one straight piece at constant speed on constant grade."""
    theta = math.atan(slope)
    slope_length = length_m * math.sqrt(1.0 + slope * slope)
    seconds = slope_length / speed_mps
    mass = profile.mass_kg
    force = (
        mass * G * profile.c_rr * math.cos(theta)
        + 0.5 * profile.air_density * profile.cd_a_m2 * speed_mps * speed_mps
        + mass * G * math.sin(theta)
    )
    power = force * speed_mps
    result = SegmentEnergy(length_m=length_m, slope_length_m=slope_length, seconds=seconds, n_subsegments=1)
    if power >= 0:
        result.traction_j = power * seconds / profile.eta_drive
    else:
        captured = min(-power, profile.p_regen_max_w)
        result.regen_j = profile.eta_regen * captured * seconds
        result.friction_loss_j = (-power - captured) * seconds
    result.aux_j = profile.p_aux_w * seconds
    delta_h = length_m * slope
    if delta_h > 0:
        result.climb_m = delta_h
    elif delta_h < 0:
        result.descent_m = -delta_h
    result.total_j = result.traction_j - result.regen_j + result.aux_j
    return result


def elevation_series(
    coordinates: Sequence[Sequence[float]], grid: ElevationGrid, options: EnergyOptions
) -> tuple[list[float], list[float], int]:
    """
    Chainage and elevation along a line, sampled at ``sample_step_m``.

    A sample the grid cannot speak for (outside it, or over a hole) is carried
    from its neighbour, which makes that piece flat rather than invented; the
    count is returned so a caller can say how much of the line that was.
    Smoothing is a moving average over ``smooth_window_m`` of ground applied to
    the interior only. The two ends stay at the raw node elevation on purpose:
    the potential used for shortest paths is a per-node quantity, and keeping
    the ends fixed is what keeps the sum of sub-segment rises equal to the
    end-to-end rise.
    """
    points = resample_line(coordinates, options.sample_step_m, keep_vertices=True)
    chain = [0.0]
    for index in range(1, len(points)):
        a, b = points[index - 1], points[index]
        chain.append(chain[-1] + haversine_m(a[1], a[0], b[1], b[0]))
    raw = grid.sample(points)
    n_nodata = sum(1 for value in raw if value is None)
    filled: list[float] = []
    last: float | None = None
    for value in raw:
        if value is not None:
            last = value
        filled.append(last)  # type: ignore[arg-type]
    # Leading gaps take the first known value; a line with no known value is flat.
    first_known = next((value for value in filled if value is not None), 0.0)
    filled = [first_known if value is None else value for value in filled]
    if options.smooth_window_m > 0 and len(filled) > 2:
        half = options.smooth_window_m / 2.0
        smoothed = list(filled)
        for index in range(1, len(filled) - 1):
            centre = chain[index]
            window = [
                filled[other]
                for other in range(len(filled))
                if abs(chain[other] - centre) <= half
            ]
            smoothed[index] = sum(window) / len(window)
        filled = smoothed
    return chain, filled, n_nodata


def segment_energy_j(
    coordinates: Sequence[Sequence[float]],
    speed_mps: float,
    profile: VehicleProfile,
    options: EnergyOptions,
    grid: ElevationGrid | None = None,
) -> SegmentEnergy:
    """
    Energy of driving ``coordinates`` (GeoJSON ``[lng, lat]``) in their order.

    Pure: the same geometry, speed, profile, options and grid give the same
    answer. On the planar surface every grade is zero and the slope length is
    the plan length; everything else is identical, so the difference between
    the two surfaces is terrain and nothing else.
    """
    points = [p for p in coordinates if p is not None and len(p) >= 2]
    if len(points) < 2 or speed_mps <= 0:
        return SegmentEnergy()
    if options.surface == "planar" or grid is None:
        length = sum(
            haversine_m(points[i - 1][1], points[i - 1][0], points[i][1], points[i][0])
            for i in range(1, len(points))
        )
        if length <= 0:
            return SegmentEnergy()
        return _subsegment(length, 0.0, speed_mps, profile)

    chain, elevation, n_nodata = elevation_series(points, grid, options)
    total = SegmentEnergy(n_nodata=n_nodata)
    for index in range(1, len(chain)):
        length = chain[index] - chain[index - 1]
        if length <= 0:
            continue
        slope = (elevation[index] - elevation[index - 1]) / length
        if abs(slope) > options.grade_cap:
            slope = math.copysign(options.grade_cap, slope)
            total.n_capped += 1
        total.add(_subsegment(length, slope, speed_mps, profile))
    return total


def flat_energy_j_per_m(speed_mps: float, profile: VehicleProfile) -> float:
    """Joules per metre of level road at ``speed_mps``: the planar rate."""
    if speed_mps <= 0:
        return 0.0
    return _subsegment(1000.0, 0.0, speed_mps, profile).total_j / 1000.0


def stop_energy_j(speed_mps: float, dwell_s: float, profile: VehicleProfile) -> float:
    """
    Braking to a halt and pulling away again, plus the auxiliaries while waiting.

    The same on both surfaces, so it never changes a routing decision; it is
    here so that absolute kWh/km figures are honest.
    """
    kinetic = 0.5 * profile.mass_kg * speed_mps * speed_mps
    return kinetic * (1.0 / profile.eta_drive - profile.stop_regen) + profile.p_aux_w * max(0.0, dwell_s)


def potential_j(elevation_m: float, profile: VehicleProfile) -> float:
    """
    π(n) = η_regen · m · g · h(n): the most a descent to sea level could return.

    Johnson-style reweighting w'(i, j) = w(i, j) + π(i) − π(j) is non-negative
    whenever w(i, j) ≥ −η_regen·m·g·(h_i − h_j), which the physics above
    guarantees when the sampled rises along the edge sum to h_j − h_i. Along any
    path the corrections telescope to π(source) − π(target), so Dijkstra on w'
    ranks paths exactly as the true energy does.
    """
    return profile.eta_regen * profile.mass_kg * G * elevation_m


# ── Paths and route features ─────────────────────────────────────────────
@dataclass(frozen=True)
class DirectedEdge:
    """One piece of road as driven: its geometry in driving order and its speed."""

    coordinates: tuple[tuple[float, float], ...]
    speed_kmh: float

    def reversed(self) -> "DirectedEdge":
        return DirectedEdge(tuple(reversed(self.coordinates)), self.speed_kmh)


def path_energy(
    edges: Sequence[DirectedEdge],
    profile: VehicleProfile,
    options: EnergyOptions,
    grid: ElevationGrid | None = None,
    *,
    reverse: bool = False,
) -> SegmentEnergy:
    """Sum of segment energies along a path; ``reverse`` drives it backwards."""
    total = SegmentEnergy()
    sequence = [edge.reversed() for edge in reversed(edges)] if reverse else list(edges)
    for edge in sequence:
        total.add(segment_energy_j(edge.coordinates, edge.speed_kmh / 3.6, profile, options, grid))
    return total


#: Two consecutive drawn parts closer than this are one line; farther apart,
#: the vehicle is not assumed to drive the straight line between them.
JOIN_TOLERANCE_M = 0.5


def line_runs(geometry: dict[str, Any]) -> list[list[list[float]]]:
    """
    A LineString as one run, or a MultiLineString as its contiguous runs.

    Consecutive parts whose ends coincide are joined into one run; a part that
    starts somewhere else begins a new run. Joining every part regardless would
    invent a straight segment across the gap — over a valley, a climb and a
    descent that no vehicle drives — which is exactly what happened to four of
    the municipality's drawn lines before this rule existed.
    """
    kind = (geometry or {}).get("type")
    coordinates = (geometry or {}).get("coordinates") or []
    if kind == "LineString":
        run = [list(point[:2]) for point in coordinates]
        return [run] if len(run) >= 2 else []
    if kind != "MultiLineString":
        return []
    runs: list[list[list[float]]] = []
    for part in coordinates:
        points = [list(point[:2]) for point in part]
        if len(points) < 2:
            continue
        if runs:
            tail = runs[-1][-1]
            if haversine_m(tail[1], tail[0], points[0][1], points[0][0]) <= JOIN_TOLERANCE_M:
                runs[-1].extend(points[1:] if points[0] == tail else points)
                continue
        runs.append(points)
    return runs


def line_coordinates(geometry: dict[str, Any]) -> list[list[float]]:
    """A LineString's positions, or a MultiLineString's contiguous runs laid end to end."""
    return [point for run in line_runs(geometry) for point in run]


def feature_edges(feature: dict[str, Any], fallback_speed_kmh: float) -> list[DirectedEdge]:
    """
    The directed edges a route feature was driven on.

    A proposal carries ``edge_speeds`` — ``[start, end, speed_kmh]`` index
    ranges into its own LineString — so the speed of each piece of road
    survives into the stored geometry. A drawn line (the frozen reference
    layer) carries none, and is driven at the fallback speed throughout; the
    caller is told which case it got via ``speed_source``.
    """
    ranges = (feature.get("properties") or {}).get("edge_speeds")
    if not ranges:
        # A drawn line: one edge per contiguous run, so a gap between runs is
        # never driven.
        return [
            DirectedEdge(tuple((float(p[0]), float(p[1])) for p in run), fallback_speed_kmh)
            for run in line_runs(feature.get("geometry") or {})
        ]
    coordinates = line_coordinates(feature.get("geometry") or {})
    if len(coordinates) < 2:
        return []
    edges: list[DirectedEdge] = []
    for start, end, speed in ranges:
        piece = coordinates[int(start): int(end) + 1]
        if len(piece) >= 2:
            edges.append(DirectedEdge(tuple((float(p[0]), float(p[1])) for p in piece), float(speed)))
    return edges


# ── Terrain variants ─────────────────────────────────────────────────────
def correlated_noise(
    rows: int, cols: int, sigma_m: float, seed: int | None, window_cells: int = 3
) -> list[float]:
    """
    A Gaussian field with the requested marginal σ, correlated over a box.

    White noise N(0, σ²) per cell is averaged over a ``window_cells`` ×
    ``window_cells`` box (offsets −⌊w/2⌋ … w−⌊w/2⌋−1, so 3 → −1…1 and
    10 → −5…4) and rescaled by √count, the number of in-grid cells the box
    covered, so the marginal standard deviation stays σ while neighbouring
    cells share most of their error. The box is applied as two one-dimensional
    passes, which is the same mean in a different summation order.
    """
    window = max(1, int(window_cells))
    lower = window // 2
    offsets = range(-lower, window - lower)
    rng = random.Random(seed)
    white = [rng.gauss(0.0, sigma_m) for _ in range(rows * cols)]
    row_sum = [0.0] * (rows * cols)
    row_count = [0] * (rows * cols)
    for r in range(rows):
        base_index = r * cols
        for c in range(cols):
            total = 0.0
            count = 0
            for dc in offsets:
                cc = c + dc
                if 0 <= cc < cols:
                    total += white[base_index + cc]
                    count += 1
            row_sum[base_index + c] = total
            row_count[base_index + c] = count
    noise = [0.0] * (rows * cols)
    for r in range(rows):
        for c in range(cols):
            total = 0.0
            count = 0
            for dr in offsets:
                rr = r + dr
                if 0 <= rr < rows:
                    total += row_sum[rr * cols + c]
                    count += row_count[rr * cols + c]
            noise[r * cols + c] = total / count * math.sqrt(count)
    return noise


def derive_grid(
    base: ElevationGrid,
    *,
    dem_scale: float = 1.0,
    reference_m: float | None = None,
    noise_sigma_m: float = 0.0,
    seed: int | None = None,
    noise_corr_cells: int = 3,
) -> ElevationGrid:
    """
    A grid with the terrain exaggerated or flattened, and/or perturbed.

    ``h' = h_ref + dem_scale · (h − h_ref)``, with ``h_ref`` the depot's
    elevation (or the grid mean). Noise is Gaussian per cell, then averaged
    over a ``noise_corr_cells`` × ``noise_corr_cells`` box (3×3 by default)
    and rescaled so that the field keeps the requested standard deviation
    while being spatially correlated — closer to how a DEM is actually wrong
    than independent cell noise. Holes stay holes. The base grid is never
    modified and the process-wide cache never sees the result.
    """
    values = base._values  # noqa: SLF001 — same package, read only
    nodata = base.nodata
    rows, cols = base.rows, base.cols
    if reference_m is None:
        known = [float(v) for v in values if v != nodata]
        reference_m = sum(known) / len(known) if known else 0.0
    noise: list[float] | None = None
    if noise_sigma_m > 0:
        noise = correlated_noise(rows, cols, noise_sigma_m, seed, noise_corr_cells)
    derived = array.array("d")
    for index, raw in enumerate(values):
        if raw == nodata:
            derived.append(float(nodata))
            continue
        value = reference_m + dem_scale * (float(raw) - reference_m)
        if noise is not None:
            value += noise[index]
        derived.append(value)
    meta = dict(base.meta)
    meta["derivation"] = {
        "dem_scale": dem_scale,
        "reference_m": reference_m,
        "noise_sigma_m": noise_sigma_m,
        "noise_corr_cells": int(noise_corr_cells) if noise_sigma_m > 0 else None,
        "seed": seed,
        "base_sha256": base.meta.get("sha256"),
    }
    return ElevationGrid(derived, meta)


# ── Profiles on disk ─────────────────────────────────────────────────────
def load_vehicle_profiles(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else PROJECT_ROOT / DEFAULT_VEHICLE_PROFILES
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EnergyConfigurationError(f"Araç profili dosyası yok: {target}") from exc


def vehicle_profile(
    name: str | None = None,
    *,
    mass_scenario: str = "average",
    eta_regen: float | None = None,
    p_regen_max_kw: float | None = None,
    path: str | Path | None = None,
) -> VehicleProfile:
    """A profile from ``vehicle_profiles.json`` with the sweep overrides applied."""
    payload = load_vehicle_profiles(path)
    profiles = payload.get("profiles") or {}
    key = str(name or payload.get("default_profile") or "")
    if key not in profiles:
        raise EnergyConfigurationError(f"Araç profili bulunamadı: {key or '(boş)'}")
    if mass_scenario not in MASS_SCENARIOS:
        raise EnergyConfigurationError("mass_scenario empty, average veya full olmalı.")
    raw = profiles[key]
    passengers = int((payload.get("mass_scenarios") or {}).get(mass_scenario, 0))
    each = float(payload.get("passenger_mass_kg_each", 75.0))
    regen = float(raw["eta_regen"]) if eta_regen is None else float(eta_regen)
    cap_kw = float(raw["p_regen_max_kw"]) if p_regen_max_kw is None else float(p_regen_max_kw)
    return VehicleProfile(
        name=key,
        mass_empty_kg=float(raw["mass_empty_kg"]),
        passenger_mass_kg=passengers * each,
        c_rr=float(raw["c_rr"]),
        cd_a_m2=float(raw["cd_a_m2"]),
        eta_drive=float(raw["eta_drive"]),
        eta_regen=regen,
        p_regen_max_w=cap_kw * 1000.0,
        p_aux_w=float(raw["p_aux_kw"]) * 1000.0,
        air_density=float(raw.get("air_density_kg_m3", 1.15)),
        mass_scenario=mass_scenario,
    )


def with_surface(options: EnergyOptions, surface: str) -> EnergyOptions:
    return replace(options, surface=surface)


def with_passenger_mass(profile: VehicleProfile, passenger_mass_kg: float) -> VehicleProfile:
    """
    The same vehicle carrying a different load.

    A load that changes along a tour — passengers boarding, parcels dropped —
    is priced leg by leg with this: the regeneration cap, the resistances and
    the stop energy all see the mass on board for that leg.
    """
    return replace(profile, passenger_mass_kg=float(passenger_mass_kg), mass_scenario="custom")


__all__ = [
    "DirectedEdge",
    "ENERGY_MODEL_VERSION",
    "EnergyConfigurationError",
    "EnergyOptions",
    "G",
    "J_PER_WH",
    "MASS_SCENARIOS",
    "SURFACES",
    "SegmentEnergy",
    "VehicleProfile",
    "correlated_noise",
    "derive_grid",
    "elevation_series",
    "feature_edges",
    "flat_energy_j_per_m",
    "line_coordinates",
    "line_runs",
    "load_vehicle_profiles",
    "path_energy",
    "potential_j",
    "segment_energy_j",
    "stop_energy_j",
    "vehicle_profile",
    "with_passenger_mass",
    "with_surface",
]
