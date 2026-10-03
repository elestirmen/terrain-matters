#!/usr/bin/env python3
"""
Draw the Terrain Matters figures from one experiment run.

    # draft and roads: PostgreSQL if DATABASE_URL is set, else data/editable/*.json
    .venv/bin/python scripts/plot_terrain_figures.py --run-id 2026-09-29-urgup [--dpi 600]

Every figure is drawn from `experiments_summary.csv` / `network_metrics.csv` /
`route_metrics.csv` / `routes.geojson` of the run, plus the elevation grid and
the road network for the maps — nothing is typed in by hand, so a figure can
always be traced back to a row. Labels are in English for the journal;
colours are colour-blind safe (Okabe–Ito for lines, viridis/cividis for maps).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from urgup_transport import energy as energy_model  # noqa: E402
from urgup_transport import optimizer, stores  # noqa: E402
from urgup_transport.config import DEFAULT_BASELINE_NETWORK, PROJECT_ROOT  # noqa: E402
from urgup_transport.elevation import load_grid  # noqa: E402

OKABE = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9", "#F0E442", "#000000"]
MASS_LABEL = {"empty": "empty", "average": "7 passengers", "full": "14 passengers"}


def read_csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def fnum(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def grid_array(grid):
    values = np.array(grid._values, dtype=float).reshape(grid.rows, grid.cols)  # noqa: SLF001
    values[values == grid.nodata] = np.nan
    extent = [grid.lon0, grid.lon0 + grid.cols * grid.d_lon, grid.lat0 - grid.rows * grid.d_lat, grid.lat0]
    return values, extent


def hillshade(values, d_lon_m, d_lat_m, azimuth=315.0, altitude=45.0):
    dz_dy, dz_dx = np.gradient(np.nan_to_num(values, nan=np.nanmean(values)), d_lat_m, d_lon_m)
    slope = np.arctan(np.hypot(dz_dx, dz_dy))
    aspect = np.arctan2(-dz_dx, dz_dy)
    az = np.radians(azimuth)
    alt = np.radians(altitude)
    shade = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(shade, 0, 1)


def draw_terrain(ax, grid, *, contours=True, alpha=0.85):
    values, extent = grid_array(grid)
    d_lat_m = grid.d_lat * 111_195
    d_lon_m = grid.d_lon * 111_195 * np.cos(np.radians(grid.lat0 - grid.rows * grid.d_lat / 2))
    ax.imshow(hillshade(values, d_lon_m, d_lat_m), cmap="gray", extent=extent, origin="upper", alpha=1.0, aspect="auto")
    image = ax.imshow(values, cmap="terrain", extent=extent, origin="upper", alpha=alpha * 0.6, aspect="auto")
    if contours:
        lons = np.linspace(extent[0], extent[1], grid.cols)
        lats = np.linspace(extent[3], extent[2], grid.rows)
        ax.contour(lons, lats, values, levels=np.arange(1000, 1600, 50), colors="k", linewidths=0.25, alpha=0.5)
    ax.set_aspect(1 / np.cos(np.radians(38.64)))
    ax.set_xlabel("Longitude (°E)")
    ax.set_ylabel("Latitude (°N)")
    return image


def road_lines():
    payload = stores.editable_road_network_payload() or {}
    return [f["geometry"]["coordinates"] for f in payload.get("features", []) if f.get("geometry", {}).get("type") == "LineString"]


def fig01_terrain_network(run: Path, out: Path, grid, dpi: int):
    fig, ax = plt.subplots(figsize=(7.2, 5.4))
    image = draw_terrain(ax, grid)
    ax.add_collection(LineCollection(road_lines(), colors="#333333", linewidths=0.35, alpha=0.8))
    draft = stores.build_store().read()
    stops = [f["geometry"]["coordinates"] for f in draft["layers"]["stops"]["features"]]
    depot = [f["geometry"]["coordinates"] for f in draft["layers"]["stops"]["features"] if (f["properties"].get("location_role") == "depot")]
    ax.scatter([s[0] for s in stops], [s[1] for s in stops], s=6, c="#D55E00", edgecolors="white", linewidths=0.3, zorder=5, label="Stops")
    if depot:
        ax.scatter([depot[0][0]], [depot[0][1]], s=60, marker="*", c="#000000", zorder=6, label="Depot")
    values, extent = grid_array(grid)
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    cbar = fig.colorbar(image, ax=ax, shrink=0.8, pad=0.02)
    cbar.set_label("Elevation (m, Copernicus GLO-30)")
    ax.legend(loc="lower left", fontsize=8, frameon=True)
    ax.set_title("Ürgüp: road network and stops over the elevation model")
    fig.tight_layout()
    fig.savefig(out / "fig01_terrain_network.png", dpi=dpi)
    plt.close(fig)


def fig02_profile(run: Path, out: Path, grid, dpi: int):
    """The DEM-optimal network's steepest line: elevation and 100 m-base grade."""
    cells = routes_by_cell(run)
    features = cells.get("A-dem-eta0.5-average") or next(iter(cells.values()))
    best = None
    opts = energy_model.EnergyOptions(surface="dem")
    for feature in features:
        coords = [(c[0], c[1]) for c in feature["geometry"]["coordinates"]]
        chain, elev, _n = energy_model.elevation_series(coords, grid, opts)
        relief = max(elev) - min(elev)
        if best is None or relief > best[0]:
            best = (relief, feature["properties"]["name"], chain, elev, coords)
    relief, name, chain, elev, coords = best
    raw_opts = energy_model.EnergyOptions(surface="dem", smooth_window_m=0.0)
    chain_raw, elev_raw, _ = energy_model.elevation_series(coords, grid, raw_opts)
    chain = np.array(chain)
    elev = np.array(elev)
    # Grade over a 100 m base, as the platform reports it (never between two
    # adjacent samples, which can be a metre apart where a vertex was kept).
    base = 100.0
    mid, grade = [], []
    head = 0
    for index in range(1, len(chain)):
        while chain[index] - chain[head] > base and head < index - 1:
            head += 1
        span = chain[index] - chain[head]
        if span < base * 0.5:
            continue
        mid.append((chain[index] + chain[head]) / 2000.0)
        grade.append((elev[index] - elev[head]) / span * 100.0)
    mid, grade = np.array(mid), np.array(grade)
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(7.2, 5.0), sharex=True, gridspec_kw={"height_ratios": [3, 1.2]})
    ax.plot(np.array(chain_raw) / 1000, elev_raw, color="#999999", linewidth=0.6, label="raw 25 m samples")
    ax.plot(chain / 1000, elev, color="#0072B2", linewidth=1.3, label="100 m moving average")
    ax.fill_between(chain / 1000, elev.min() - 20, elev, color="#0072B2", alpha=0.08)
    ax.set_ylabel("Elevation (m)")
    ax.set_title(f"DEM-optimal line “{name}”: relief {relief:.0f} m over a {chain[-1] / 1000:.1f} km cycle", fontsize=10)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)
    colours = np.where(np.abs(grade) > 6, "#D55E00", np.where(np.abs(grade) > 3, "#E69F00", "#009E73"))
    ax2.scatter(mid, grade, s=2, c=colours, linewidths=0)
    ax2.axhline(0, color="k", linewidth=0.5)
    ax2.set_ylim(-20, 20)
    ax2.set_ylabel("Grade over 100 m (%)")
    ax2.set_xlabel("Distance along line (km)")
    ax2.grid(alpha=0.3)
    ax2.text(0.99, 0.95, "green < 3 %, amber 3–6 %, red > 6 %; ±5.7 pp uncertainty", transform=ax2.transAxes, ha="right", va="top", fontsize=7)
    fig.tight_layout()
    fig.savefig(out / "fig02_route_profile.png", dpi=dpi)
    plt.close(fig)


def fig03_segment_energy(run: Path, out: Path, grid, dpi: int):
    draft = stores.build_store().read()
    depot, customers = optimizer._extract_stops(draft)
    stops = [depot, *customers]
    params = {"cost_basis": "energy", "energy_options": {"surface": "dem", "eta_regen": 0.5, "mass_scenario": "average"}}
    context = optimizer._road_context_for_params(stops, params)
    graph = context["graph"]
    forward, asym_lines, asym_values, rates = [], [], [], []
    seen = set()
    for u, v, k, data in graph.edges(keys=True, data=True):
        coords = optimizer._oriented_edge_coordinates(graph, u, data)
        if len(coords) < 2 or data["length"] <= 0:
            continue
        rate = data[optimizer.ENERGY_KEY] / 3600 / (data["length"] / 1000)  # Wh/km
        forward.append(coords)
        rates.append(rate)
        key = tuple(sorted((repr(u), repr(v))))
        if key in seen:
            continue
        seen.add(key)
        back = graph.get_edge_data(v, u)
        if back:
            other = min(back.values(), key=lambda d: d.get(optimizer.ENERGY_ADJUSTED_KEY, 0))
            e1, e2 = data[optimizer.ENERGY_KEY], other[optimizer.ENERGY_KEY]
            top = max(abs(e1), abs(e2))
            asym_lines.append(coords)
            asym_values.append(abs(e1 - e2) / top if top else 0.0)
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.6))
    values, extent = grid_array(grid)
    for ax in axes:
        ax.imshow(hillshade(values, 30, 30), cmap="gray", extent=extent, origin="upper", alpha=0.35, aspect="auto")
        ax.set_aspect(1 / np.cos(np.radians(38.64)))
        ax.set_xlabel("Longitude (°E)")
    lc = LineCollection(forward, cmap="cividis", linewidths=0.8, norm=matplotlib.colors.Normalize(vmin=-200, vmax=1200))
    lc.set_array(np.array(rates))
    axes[0].add_collection(lc)
    axes[0].set_xlim(extent[0], extent[1]); axes[0].set_ylim(extent[2], extent[3])
    axes[0].set_ylabel("Latitude (°N)")
    cb = fig.colorbar(lc, ax=axes[0], shrink=0.85, pad=0.02); cb.set_label("Directed segment energy (Wh/km)")
    axes[0].set_title("(a) Energy per directed road piece")
    lc2 = LineCollection(asym_lines, cmap="magma_r", linewidths=0.8, norm=matplotlib.colors.Normalize(vmin=0, vmax=1))
    lc2.set_array(np.array(asym_values))
    axes[1].add_collection(lc2)
    axes[1].set_xlim(extent[0], extent[1]); axes[1].set_ylim(extent[2], extent[3])
    cb2 = fig.colorbar(lc2, ax=axes[1], shrink=0.85, pad=0.02); cb2.set_label("|E→ − E←| / max(E→, E←)")
    axes[1].set_title("(b) Direction asymmetry of two-way pieces")
    fig.suptitle("Segment energy on the road graph (η_regen = 0.5, 7 passengers)", fontsize=10)
    fig.tight_layout()
    fig.savefig(out / "fig03_segment_energy.png", dpi=dpi)
    plt.close(fig)
    return {"edges": len(forward), "two_way": len(asym_lines), "median_rate_wh_km": float(np.median(rates)),
            "share_asym_gt_0_2": float(np.mean(np.array(asym_values) > 0.2)) if asym_values else None}


def routes_by_cell(run: Path):
    payload = json.loads((run / "routes.geojson").read_text(encoding="utf-8"))
    cells = defaultdict(list)
    for feature in payload["features"]:
        cells[feature["properties"]["cell_id"]].append(feature)
    return cells


def draw_cell(ax, grid, features, title):
    values, extent = grid_array(grid)
    ax.imshow(hillshade(values, 30, 30), cmap="gray", extent=extent, origin="upper", alpha=0.35, aspect="auto")
    ax.contour(np.linspace(extent[0], extent[1], grid.cols), np.linspace(extent[3], extent[2], grid.rows), values,
               levels=np.arange(1000, 1600, 50), colors="k", linewidths=0.2, alpha=0.4)
    for index, feature in enumerate(sorted(features, key=lambda f: f["properties"]["name"])):
        coords = feature["geometry"]["coordinates"]
        ax.plot([c[0] for c in coords], [c[1] for c in coords], color=OKABE[index % len(OKABE)], linewidth=1.0,
                label=f"{feature['properties']['name']} ({feature['properties'].get('energy_dem_wh', 0) / 1000:.1f} kWh)")
    ax.set_aspect(1 / np.cos(np.radians(38.64)))
    ax.set_title(title, fontsize=9)
    ax.set_xlabel("Longitude (°E)")
    ax.legend(fontsize=5.5, loc="lower left", ncol=2, frameon=True)


def fig04_networks(run: Path, out: Path, grid, dpi: int):
    cells = routes_by_cell(run)
    left, right = cells.get("A-planar-average"), cells.get("A-dem-eta0.5-average")
    if not left or not right:
        return
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.8), sharey=True)
    draw_cell(axes[0], grid, left, "(a) Planar-energy network")
    draw_cell(axes[1], grid, right, "(b) Terrain-energy (DEM) network")
    axes[0].set_ylabel("Latitude (°N)")
    lons = [c[0] for f in left + right for c in f["geometry"]["coordinates"]]
    lats = [c[1] for f in left + right for c in f["geometry"]["coordinates"]]
    for ax in axes:
        ax.set_xlim(min(lons) - 0.004, max(lons) + 0.004)
        ax.set_ylim(min(lats) - 0.003, max(lats) + 0.003)
    fig.suptitle("Same stops, same solver, two surfaces (η_regen = 0.5, 7 passengers)", fontsize=10)
    fig.tight_layout()
    fig.savefig(out / "fig07_networks.png", dpi=dpi)
    plt.close(fig)


def fig05_penalty(run: Path, out: Path, dpi: int):
    rows = [r for r in read_csv(run / "experiments_summary.csv") if r.get("experiment") == "A"]
    if not rows:
        return
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.6))
    for index, mass in enumerate(("empty", "average", "full")):
        sub = sorted([r for r in rows if r["mass_scenario"] == mass], key=lambda r: float(r["eta_regen"]))
        etas = [float(r["eta_regen"]) for r in sub]
        axes[0].plot(etas, [-100 * float(r["planar_error_dem_opt"]) for r in sub], marker="o", color=OKABE[index], label=MASS_LABEL[mass])
        axes[1].plot(etas, [float(r["terrain_penalty_dem_opt_wh"]) / 1000 for r in sub], marker="o", color=OKABE[index], label=f"{MASS_LABEL[mass]}: model")
        axes[1].plot(etas, [float(r["analytic_penalty_dem_opt_wh"]) / 1000 for r in sub], linestyle="--", color=OKABE[index], alpha=0.7, label=f"{MASS_LABEL[mass]}: (1/η_d − η_r)·m·g·Σclimb")
        axes[2].plot(etas, [float(r["voti_vs_planar_pct"]) for r in sub], marker="o", color=OKABE[index], label=f"{MASS_LABEL[mass]} vs planar-energy")
        axes[2].plot(etas, [float(r["voti_vs_distance_pct"]) for r in sub], marker="s", linestyle=":", color=OKABE[index], alpha=0.8, label=f"{MASS_LABEL[mass]} vs distance")
    axes[0].set_title("(a) Planar under-estimate of tour energy", fontsize=9)
    axes[0].set_ylabel("(E_DEM − E_planar) / E_DEM (%)")
    axes[1].set_title("(b) Terrain penalty of the DEM-optimal network", fontsize=9)
    axes[1].set_ylabel("kWh per cycle of all lines")
    axes[2].set_title("(c) Value of terrain information (VoTI)", fontsize=9)
    axes[2].set_ylabel("Energy saved on the DEM (%)")
    axes[2].axhline(0, color="k", linewidth=0.5)
    for ax in axes:
        ax.set_xlabel("Regeneration efficiency η_regen")
        ax.grid(alpha=0.3)
        ax.set_xticks([0, 0.3, 0.5, 0.7])
    axes[0].legend(fontsize=7)
    axes[1].legend(fontsize=5.5)
    axes[2].legend(fontsize=5.5)
    fig.tight_layout()
    fig.savefig(out / "fig04_penalty_vs_eta.png", dpi=dpi)
    plt.close(fig)


def fig06_scale(run: Path, out: Path, dpi: int):
    rows = sorted([r for r in read_csv(run / "experiments_summary.csv") if r.get("experiment") == "C"], key=lambda r: float(r["dem_scale"]))
    if not rows:
        return
    scales = [float(r["dem_scale"]) for r in rows]
    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    ax.plot(scales, [float(r["voti_vs_distance_pct"]) for r in rows], marker="o", color=OKABE[0], label="VoTI vs distance-optimal (%)")
    ax.plot(scales, [float(r["voti_vs_planar_pct"]) for r in rows if r.get("voti_vs_planar_pct")], marker="s", color=OKABE[1], label="VoTI vs planar-energy-optimal (%)")
    ax.plot(scales, [-100 * float(r["planar_error_distance_opt"]) for r in rows], marker="^", color=OKABE[3], linestyle="--", label="Planar under-estimate, distance network (%)")
    ax.set_xlabel("Terrain intensity (DEM scale factor)")
    ax.set_ylabel("Percent of tour energy")
    ax.axhline(0, color="k", linewidth=0.5)
    ax.grid(alpha=0.3)
    ax2 = ax.twinx()
    ax2.plot(scales, [float(r["jaccard_distance_dem"]) for r in rows], marker="D", color=OKABE[2], label="Membership similarity (Jaccard) vs distance network")
    ax2.plot(scales, [float(r["stops_changed_distance_dem"]) for r in rows], marker="x", color=OKABE[4], linestyle=":", label="Stops changing line vs distance network")
    ax2.set_ylabel("Jaccard (0–1) / stops changing line")
    handles = ax.get_legend_handles_labels()[0] + ax2.get_legend_handles_labels()[0]
    labels = ax.get_legend_handles_labels()[1] + ax2.get_legend_handles_labels()[1]
    ax.legend(handles, labels, fontsize=6.5, loc="center left")
    ax.set_title("Terrain-intensity sweep (η_regen = 0.5, 7 passengers)", fontsize=9)
    fig.tight_layout()
    fig.savefig(out / "fig08_voti_vs_scale.png", dpi=dpi)
    plt.close(fig)


def fig07_asymmetry(run: Path, out: Path, grid, dpi: int):
    rows = [r for r in read_csv(run / "route_metrics.csv")
            if r["cell_id"] == "A-dem-eta0.5-average" and r["eta_regen_eval"] == "0.5" and r["mass_eval"] == "average"]
    cells = routes_by_cell(run)
    features = cells.get("A-dem-eta0.5-average")
    if not rows or not features:
        return
    fwd = {r["route_id"]: r for r in rows if r["direction"] == "forward"}
    rev = {r["route_id"]: r for r in rows if r["direction"] == "reverse"}
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 4.6), gridspec_kw={"width_ratios": [1.3, 1]})
    values, extent = grid_array(grid)
    ax.imshow(hillshade(values, 30, 30), cmap="gray", extent=extent, origin="upper", alpha=0.35, aspect="auto")
    norm = matplotlib.colors.Normalize(vmin=0, vmax=0.4)
    cmap = plt.get_cmap("magma_r")
    names = []
    for feature in sorted(features, key=lambda f: f["properties"]["name"]):
        rid = feature["properties"]["route_id"]
        a = abs(float(fwd[rid]["energy_wh_dem"]) - float(rev[rid]["energy_wh_dem"])) / max(float(fwd[rid]["energy_wh_dem"]), float(rev[rid]["energy_wh_dem"]))
        coords = feature["geometry"]["coordinates"]
        ax.plot([c[0] for c in coords], [c[1] for c in coords], color=cmap(norm(a)), linewidth=1.4)
        names.append((feature["properties"]["name"], float(fwd[rid]["energy_wh_dem"]) / 1000, float(rev[rid]["energy_wh_dem"]) / 1000, a,
                      float(fwd[rid]["lost_friction_wh"]) / 1000, float(rev[rid]["lost_friction_wh"]) / 1000))
    lons = [c[0] for f in features for c in f["geometry"]["coordinates"]]
    lats = [c[1] for f in features for c in f["geometry"]["coordinates"]]
    ax.set_xlim(min(lons) - 0.004, max(lons) + 0.004); ax.set_ylim(min(lats) - 0.003, max(lats) + 0.003)
    ax.set_aspect(1 / np.cos(np.radians(38.64)))
    sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap); sm.set_array([])
    fig.colorbar(sm, ax=ax, shrink=0.85, pad=0.02).set_label("Direction asymmetry A = |E→ − E←| / max")
    ax.set_title("(a) DEM-optimal lines coloured by direction asymmetry", fontsize=9)
    ax.set_xlabel("Longitude (°E)"); ax.set_ylabel("Latitude (°N)")
    x = np.arange(len(names))
    ax2.bar(x - 0.2, [n[1] for n in names], width=0.4, color=OKABE[0], label="as planned (forward)")
    ax2.bar(x + 0.2, [n[2] for n in names], width=0.4, color=OKABE[1], label="reversed")
    ax2.bar(x - 0.2, [n[4] for n in names], width=0.4, color="none", edgecolor="k", hatch="///", linewidth=0.4, label="of which friction-brake loss")
    ax2.bar(x + 0.2, [n[5] for n in names], width=0.4, color="none", edgecolor="k", hatch="///", linewidth=0.4)
    ax2.set_xticks(x); ax2.set_xticklabels([n[0] for n in names], rotation=45, ha="right", fontsize=7)
    ax2.set_ylabel("kWh per cycle"); ax2.legend(fontsize=7); ax2.grid(axis="y", alpha=0.3)
    ax2.set_title("(b) Same loop, two directions (η_regen = 0.5)", fontsize=9)
    fig.tight_layout()
    fig.savefig(out / "fig05_asymmetry.png", dpi=dpi)
    plt.close(fig)


def fig08_montecarlo(run: Path, out: Path, dpi: int):
    mc = read_csv(run / "monte_carlo.csv")
    stability = read_csv(run / "stop_stability.csv")
    if not mc:
        return
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.6))
    axes[0].boxplot([[float(r["voti_perturbed_wh"]) / 1000 for r in mc], [float(r["voti_base_wh"]) / 1000 for r in mc]],
                    tick_labels=["perturbed\nDEM", "reference\nDEM"])
    axes[0].set_ylabel("VoTI vs distance network (kWh)"); axes[0].set_title("(a) VoTI of noise-optimised networks", fontsize=9)
    axes[0].axhline(0, color="k", linewidth=0.5); axes[0].grid(axis="y", alpha=0.3)
    axes[1].boxplot([[float(r["E_perturbed_dem_opt"]) / 1000 for r in mc], [float(r["E_base_dem_opt"]) / 1000 for r in mc]],
                    tick_labels=["perturbed\nDEM", "reference\nDEM"])
    axes[1].set_ylabel("Energy of the noise-optimised network (kWh)"); axes[1].set_title("(b) Tour energy, evaluated on each DEM", fontsize=9)
    axes[1].grid(axis="y", alpha=0.3)
    if stability:
        shares = [float(r["modal_share"]) for r in stability]
        axes[2].hist(shares, bins=np.linspace(0, 1, 11), color=OKABE[2], edgecolor="white")
        axes[2].set_xlabel("Share of repeats a stop stays on its modal line"); axes[2].set_ylabel("Stops")
        axes[2].set_title(f"(c) Membership stability ({len(mc)} repeats)", fontsize=9)
        axes[2].grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "fig09_montecarlo.png", dpi=dpi)
    plt.close(fig)


def fig09_reference(run: Path, out: Path, dpi: int):
    rows = [r for r in read_csv(run / "reference_metrics.csv") if r["eta_regen"] == "0.5" and r["mass_scenario"] == "average"]
    if not rows:
        return
    names = sorted({r["route_name"] for r in rows})
    fwd = {r["route_name"]: r for r in rows if r["direction"] == "forward"}
    rev = {r["route_name"]: r for r in rows if r["direction"] == "reverse"}
    x = np.arange(len(names))
    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    ax.bar(x - 0.27, [float(fwd[n]["energy_wh_planar"]) / 1000 for n in names], width=0.27, color="#999999", label="planar model")
    ax.bar(x, [float(fwd[n]["energy_wh_dem"]) / 1000 for n in names], width=0.27, color=OKABE[0], label="DEM, as drawn")
    ax.bar(x + 0.27, [float(rev[n]["energy_wh_dem"]) / 1000 for n in names], width=0.27, color=OKABE[1], label="DEM, reversed")
    ax.set_xticks(x); ax.set_xticklabels(names, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("kWh per cycle"); ax.grid(axis="y", alpha=0.3); ax.legend(fontsize=8)
    ax.set_title("Today's eight lines (municipal reference layer, η_regen = 0.5, 7 passengers, 30 km/h)", fontsize=9)
    fig.tight_layout()
    fig.savefig(out / "fig06_reference_lines.png", dpi=dpi)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--experiments-dir", default=str(PROJECT_ROOT / "data/processed/experiments"))
    parser.add_argument("--out", default=str(PROJECT_ROOT / "docs/paper/figures"))
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--only", default=None, help="comma list of figure numbers, e.g. 5,6")
    args = parser.parse_args()
    run = Path(args.experiments_dir) / args.run_id
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    grid = load_grid()
    only = {int(x) for x in args.only.split(",")} if args.only else None
    notes = {}
    # Numbered as the manuscript cites them: 1 terrain, 2 profile, 3 segment
    # energy, 4 penalty vs eta, 5 asymmetry, 6 reference lines, 7 networks,
    # 8 terrain sweep, 9 Monte Carlo.
    steps = {
        1: lambda: fig01_terrain_network(run, out, grid, args.dpi),
        2: lambda: fig02_profile(run, out, grid, args.dpi),
        3: lambda: notes.update(fig03_segment_energy(run, out, grid, args.dpi) or {}),
        4: lambda: fig05_penalty(run, out, args.dpi),
        5: lambda: fig07_asymmetry(run, out, grid, args.dpi),
        6: lambda: fig09_reference(run, out, args.dpi),
        7: lambda: fig04_networks(run, out, grid, args.dpi),
        8: lambda: fig06_scale(run, out, args.dpi),
        9: lambda: fig08_montecarlo(run, out, args.dpi),
    }
    for number, step in steps.items():
        if only and number not in only:
            continue
        step()
        print(f"fig{number:02d} ok")
    if notes:
        print(json.dumps(notes, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
