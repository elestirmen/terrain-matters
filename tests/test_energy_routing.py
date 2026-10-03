from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.energy_fixture import write_hill_grid
from tests.synthetic_fixture import synthetic_draft, synthetic_road_network
from urgup_transport import optimizer
from urgup_transport.editor_store import atomic_write_json
from webapp.optimization_service import sanitize_optimization_params


class RoutingFixture(unittest.TestCase):
    """A synthetic town on a synthetic hill, for every energy-routing case."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.roads = self.root / "roads.geojson"
        atomic_write_json(self.roads, synthetic_road_network())
        self.grid_dir = write_hill_grid(self.root / "grid")
        self.energy_options = {
            "surface": "dem",
            "eta_regen": 0.5,
            "mass_scenario": "average",
            "elevation_dir": str(self.grid_dir),
        }

    def run_optimizer(self, **params):
        with patch.object(optimizer, "DEFAULT_EDITABLE_ROAD_NETWORK", self.roads):
            return optimizer.optimize(synthetic_draft(), {"planning_mode": "preserve", **params})

    def context(self, **params):
        depot, customers = optimizer._extract_stops(synthetic_draft())
        stops = [depot, *customers]
        with patch.object(optimizer, "DEFAULT_EDITABLE_ROAD_NETWORK", self.roads):
            return stops, optimizer._road_context_for_params(stops, {"cost_basis": "energy", "energy_options": self.energy_options, **params})



class EnergyRoutingTests(RoutingFixture):
    def test_energy_matrix_matches_bellman_ford_on_true_energies(self):
        stops, context = self.context()
        graph, nodes, nx = context["graph"], context["nodes"], context["nx"]
        summary = context["energy_setup"].graph_summary
        self.assertEqual(summary["n_negative_adjusted"], 0)
        self.assertGreater(summary["energy_edges"], 0)
        energy = context["energy_matrix"]
        self.assertIsNotNone(energy)
        for i, source in enumerate(nodes):
            exact = nx.single_source_bellman_ford_path_length(graph, source, weight=optimizer.ENERGY_KEY)
            for j, target in enumerate(nodes):
                self.assertAlmostEqual(
                    energy[i][j], exact[target] / 3600.0, delta=1e-6,
                    msg=f"{stops[i].name} -> {stops[j].name}",
                )
        # Uphill costs more than downhill between the same two stops.
        west, east = 1, 6  # Batı 1 (lower) and Doğu 3 (higher on the hill)
        self.assertGreater(energy[west][east], energy[east][west])
        self.assertEqual(context["distance_matrix"][west][east], context["distance_matrix"][east][west])

    def test_energy_run_reports_the_contract_and_realises_its_own_cost(self):
        proposal = self.run_optimizer(cost_basis="energy", energy_options=self.energy_options)
        self.assertEqual(proposal["cost_basis"], "energy")
        self.assertEqual(proposal["optimization_cost_basis"], "shortest_path_energy")
        self.assertEqual(proposal["energy_source"], "segment_model_v1_dem")
        block = proposal["energy"]
        self.assertEqual(block["surface"], "dem")
        self.assertEqual(block["elevation_dataset"], "test-hill")
        self.assertEqual(block["elevation_confidence"], "modelled_dem30")
        self.assertIn("grade_uncertainty_percent", block)
        self.assertEqual(block["vehicle_profile"]["eta_regen"], 0.5)
        self.assertEqual(proposal["routing_snapshot"]["elevation_dataset"], "test-hill")
        for route in proposal["metrics"]["routes"]:
            self.assertAlmostEqual(route["solver_cost"], route["realized_cost"], delta=0.05)
            self.assertAlmostEqual(route["realized_cost"], route["energy_dem_wh"], delta=0.05)
            self.assertGreater(route["energy_dem_wh"], 0)
            self.assertNotEqual(route["energy_dem_wh"], route["energy_planar_wh"])
            self.assertNotEqual(route["energy_dem_wh"], route["energy_reverse_dem_wh"])
            self.assertGreater(route["climb_m"], 0)
            self.assertTrue(route["edge_speeds"])
            self.assertGreater(route["stop_energy_wh"], 0)
        metrics = proposal["metrics"]
        self.assertIn("total_energy_dem_wh", metrics)
        self.assertIn("mean_direction_asymmetry", metrics)
        # The layer feature carries no derived per-edge data.
        feature = proposal["layers"]["routes"]["features"][0]
        self.assertNotIn("edge_speeds", feature["properties"])
        self.assertIn("energy_dem_wh", feature["properties"])

    def test_planar_surface_with_energy_report_leaves_distance_results_unchanged(self):
        plain = self.run_optimizer(cost_basis="distance")
        reported = self.run_optimizer(
            cost_basis="distance",
            report_energy=True,
            energy_options={**self.energy_options, "surface": "planar"},
        )
        for a, b in zip(plain["metrics"]["routes"], reported["metrics"]["routes"], strict=True):
            self.assertEqual(a["distance_m"], b["distance_m"])
            self.assertEqual(a["road_travel_time_minutes"], b["road_travel_time_minutes"])
            self.assertEqual(a["solver_cost"], b["solver_cost"])
            self.assertEqual(b["energy_surface"], "planar")
            self.assertEqual(b["energy_wh"], b["energy_planar_wh"])
        self.assertEqual(
            [f["geometry"] for f in plain["layers"]["routes"]["features"]],
            [f["geometry"] for f in reported["layers"]["routes"]["features"]],
        )
        self.assertEqual(reported["metrics"]["total_distance_m"], plain["metrics"]["total_distance_m"])
        self.assertIsNone(plain["energy"])

    def test_keep_stop_order_evaluates_the_sequence_as_given(self):
        kept = self.run_optimizer(cost_basis="energy", energy_options=self.energy_options, keep_stop_order=True)
        names = [
            [f["properties"]["name"] for f in kept["layers"]["stops"]["features"] if f["properties"]["route_id"] == route["route_id"]]
            for route in kept["metrics"]["routes"]
        ]
        self.assertEqual(names[0], ["Merkez Durak", "Batı 1", "Batı 2", "Batı 3"])
        self.assertEqual(names[1], ["Merkez Durak", "Doğu 1", "Doğu 2", "Doğu 3"])

    def test_terrain_scaling_changes_energy_but_not_distance(self):
        flat = self.run_optimizer(cost_basis="energy", energy_options={**self.energy_options, "dem_scale": 0.0})
        steep = self.run_optimizer(cost_basis="energy", energy_options={**self.energy_options, "dem_scale": 2.0})
        for a, b in zip(flat["metrics"]["routes"], steep["metrics"]["routes"], strict=True):
            self.assertAlmostEqual(a["energy_dem_wh"], a["energy_planar_wh"], delta=0.05)
            self.assertGreater(b["climb_m"], 0)
        self.assertEqual(flat["energy"]["energy_options"]["dem_scale"], 0.0)

    def test_energy_needs_a_road_graph_and_a_grid(self):
        with self.assertRaises(optimizer.PlanningValidationError) as raised:
            self.run_optimizer(cost_basis="energy", routing_mode="haversine_analysis", energy_options=self.energy_options)
        self.assertEqual(raised.exception.code, "energy_requires_road_graph")
        with self.assertRaises(optimizer.PlanningValidationError) as raised:
            self.run_optimizer(cost_basis="energy", energy_options={**self.energy_options, "elevation_dir": str(self.root / "nowhere")})
        self.assertEqual(raised.exception.code, "elevation_grid_missing")
        with self.assertRaises(optimizer.PlanningValidationError) as raised:
            self.run_optimizer(cost_basis="energy", energy_options={**self.energy_options, "surface": "moon"})
        self.assertEqual(raised.exception.code, "invalid_energy_options")

    def test_or_opt_never_worsens_a_directed_tour(self):
        matrix = [
            [0, 5, 9, 4],
            [6, 0, 2, 8],
            [3, 7, 0, 1],
            [9, 2, 6, 0],
        ]
        route = [3, 2, 1]
        improved = optimizer.or_opt(route, matrix)
        self.assertLessEqual(optimizer._route_distance(improved, matrix), optimizer._route_distance(route, matrix))
        self.assertEqual(sorted(improved), [1, 2, 3])


class EnergyParameterTests(unittest.TestCase):
    def test_energy_basis_carries_its_options_through_the_whitelist(self):
        resolved = sanitize_optimization_params({
            "cost_basis": "energy",
            "vehicle_profile": "placeholder_minibus",
            "energy_options": {"surface": "planar", "eta_regen": "0.3", "mass_scenario": "full", "dem_scale": 1.5, "seed": "7"},
        })
        self.assertEqual(resolved["cost_basis"], "energy")
        self.assertEqual(resolved["vehicle_profile"], "placeholder_minibus")
        self.assertEqual(resolved["energy_options"]["surface"], "planar")
        self.assertEqual(resolved["energy_options"]["eta_regen"], 0.3)
        self.assertEqual(resolved["energy_options"]["mass_scenario"], "full")
        self.assertEqual(resolved["energy_options"]["seed"], 7)
        self.assertFalse(resolved["report_energy"])
        self.assertNotIn("energy_options", sanitize_optimization_params({"cost_basis": "distance"}))

    def test_energy_options_are_range_checked(self):
        with self.assertRaises(ValueError):
            sanitize_optimization_params({"cost_basis": "energy", "energy_options": {"eta_regen": 1.5}})
        with self.assertRaises(ValueError):
            sanitize_optimization_params({"cost_basis": "energy", "energy_options": {"surface": "moon"}})
        with self.assertRaises(ValueError):
            sanitize_optimization_params({"cost_basis": "energy", "energy_options": "dem"})


if __name__ == "__main__":
    unittest.main()


class ExactPotentialTests(RoutingFixture):
    """Johnson potentials: the corrected weights are non-negative by construction."""

    def test_exact_potentials_reproduce_bellman_ford_and_leave_nothing_to_clip(self):
        stops, context = self.context(energy_options={**self.energy_options, "exact_potentials": True})
        graph, nodes, nx = context["graph"], context["nodes"], context["nx"]
        summary = context["energy_setup"].graph_summary
        self.assertEqual(summary["potential_method"], "johnson_bellman_ford")
        self.assertFalse(summary["negative_cycle"])
        self.assertEqual(summary["n_negative_adjusted"], 0)
        for _u, _v, data in graph.edges(data=True):
            self.assertGreaterEqual(data[optimizer.ENERGY_ADJUSTED_KEY], 0.0)
        energy = context["energy_matrix"]
        for i, source in enumerate(nodes):
            exact = nx.single_source_bellman_ford_path_length(graph, source, weight=optimizer.ENERGY_KEY)
            for j, target in enumerate(nodes):
                self.assertAlmostEqual(energy[i][j], exact[target] / 3600.0, delta=1e-6)
        # The physics potential gives the same matrix on this graph (nothing was clipped there either).
        _stops, physics = self.context()
        self.assertEqual(physics["energy_setup"].graph_summary["potential_method"], "elevation_physics")
        for i in range(len(nodes)):
            for j in range(len(nodes)):
                self.assertAlmostEqual(energy[i][j], physics["energy_matrix"][i][j], delta=1e-6)

    def test_johnson_potentials_refuse_a_negative_cycle(self):
        import networkx as nx

        graph = nx.MultiDiGraph()
        graph.add_edge("a", "b", **{optimizer.ENERGY_KEY: -5.0})
        graph.add_edge("b", "a", **{optimizer.ENERGY_KEY: 2.0})
        with self.assertRaises(optimizer.PlanningValidationError) as caught:
            optimizer._johnson_potentials(graph, optimizer.ENERGY_KEY)
        self.assertEqual(caught.exception.code, "energy_negative_cycle")
        graph = nx.MultiDiGraph()
        graph.add_edge("a", "b", **{optimizer.ENERGY_KEY: -5.0})
        graph.add_edge("b", "a", **{optimizer.ENERGY_KEY: 7.0})
        potential, _passes = optimizer._johnson_potentials(graph, optimizer.ENERGY_KEY)
        self.assertEqual(potential["b"], -5.0)
        self.assertEqual(potential["a"], 0.0)

    def test_exact_potentials_run_end_to_end(self):
        proposal = self.run_optimizer(cost_basis="energy", energy_options={**self.energy_options, "exact_potentials": True})
        self.assertEqual(proposal["energy"]["potential_method"], "johnson_bellman_ford")
        self.assertTrue(proposal["energy"]["energy_options"]["exact_potentials"])
        for route in proposal["metrics"]["routes"]:
            self.assertAlmostEqual(route["solver_cost"], route["realized_cost"], delta=0.05)


class NodeOrderSeedTests(RoutingFixture):
    def vrp(self, **params):
        with patch.object(optimizer, "DEFAULT_EDITABLE_ROAD_NETWORK", self.roads):
            return optimizer.optimize(synthetic_draft(), {
                "planning_mode": "vrp", "route_count": 2, "solver_time_limit_seconds": 1,
                "single_route_per_mahalle": False, **params,
            })

    @staticmethod
    def served(proposal):
        return sorted(f["properties"]["stop_id"] for f in proposal["layers"]["stops"]["features"])

    def test_node_order_seed_permutes_the_search_but_serves_the_same_stops(self):
        plain = self.vrp()
        again = self.vrp()
        seeded = self.vrp(node_order_seed=3)
        seeded_again = self.vrp(node_order_seed=3)
        self.assertEqual(self.served(plain), self.served(seeded))
        self.assertEqual(self.served(plain), self.served(again))
        self.assertEqual(plain["metrics"]["total_distance_m"], again["metrics"]["total_distance_m"])
        self.assertEqual(seeded["metrics"]["total_distance_m"], seeded_again["metrics"]["total_distance_m"])
        self.assertEqual(seeded["parameters"]["node_order_seed"], 3)
        self.assertEqual(len(seeded["layers"]["routes"]["features"]), 2)
        self.assertNotIn("node_order_seed", plain["parameters"])
        # The depot stays the first node whatever the permutation.
        self.assertEqual(seeded["depot_id"], plain["depot_id"])

    def test_energy_options_whitelist_carries_the_new_switches(self):
        resolved = sanitize_optimization_params({
            "cost_basis": "energy",
            "energy_options": {"dem_noise_sigma_m": 2, "dem_noise_corr_cells": "10", "exact_potentials": "true"},
        })
        self.assertEqual(resolved["energy_options"]["dem_noise_corr_cells"], 10)
        self.assertTrue(resolved["energy_options"]["exact_potentials"])
        with self.assertRaises(ValueError):
            sanitize_optimization_params({"cost_basis": "energy", "energy_options": {"dem_noise_corr_cells": 0}})


class WarmStartTests(RoutingFixture):
    def vrp(self, **params):
        with patch.object(optimizer, "DEFAULT_EDITABLE_ROAD_NETWORK", self.roads):
            return optimizer.optimize(synthetic_draft(), {
                "planning_mode": "vrp", "route_count": 2, "solver_time_limit_seconds": 1, **params,
            })

    def test_warm_start_from_a_constrained_solution_is_never_worse_than_it(self):
        constrained = self.vrp(single_route_per_mahalle=True)
        routes = [[f["properties"]["stop_id"] for f in sorted(
            (s for s in constrained["layers"]["stops"]["features"] if s["properties"]["route_id"] == r["properties"]["route_id"]),
            key=lambda s: s["properties"]["sequence"])] for r in constrained["layers"]["routes"]["features"]]
        relaxed = self.vrp(single_route_per_mahalle=False, warm_start_stop_ids=routes)
        self.assertLessEqual(relaxed["metrics"]["fitness"], constrained["metrics"]["fitness"] + 0.5)
        self.assertEqual(relaxed["parameters"]["warm_start_stop_ids"], routes)
        served = sorted(f["properties"]["stop_id"] for f in relaxed["layers"]["stops"]["features"])
        self.assertEqual(served, sorted(f["properties"]["stop_id"] for f in constrained["layers"]["stops"]["features"]))

    def test_warm_start_tolerates_unknown_and_missing_stops(self):
        proposal = self.vrp(single_route_per_mahalle=False, warm_start_stop_ids=[["no-such-stop", "1"], []])
        self.assertEqual(len(proposal["layers"]["routes"]["features"]), 2)
        served = {f["properties"]["stop_id"] for f in proposal["layers"]["stops"]["features"]}
        self.assertEqual(served, {f["properties"]["stop_id"] for f in synthetic_draft()["layers"]["stops"]["features"]})


class SolverSecondsTests(unittest.TestCase):
    def test_experiment_scripts_get_the_minutes_they_ask_for(self):
        self.assertEqual(optimizer._solver_seconds({"solver_time_limit_seconds": 300}), 300)
        self.assertEqual(optimizer._solver_seconds({"solver_time_limit_seconds": 0}), 15)
        self.assertEqual(optimizer._solver_seconds({"solver_time_limit_seconds": -5}), 1)
        self.assertEqual(optimizer._solver_seconds({}), 15)
        self.assertEqual(optimizer._solver_seconds({"solver_time_limit_seconds": 10_000}), optimizer.MAX_SOLVER_SECONDS)
        # The web layer keeps its own, tighter ceiling.
        with self.assertRaises(ValueError):
            sanitize_optimization_params({"solver_time_limit_seconds": 300})
