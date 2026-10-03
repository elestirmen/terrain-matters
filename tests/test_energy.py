from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from tests.energy_fixture import hill_grid, write_hill_grid
from urgup_transport import energy
from urgup_transport.elevation import ElevationGrid, haversine_m

# A closed loop over the hill: east along one row, north, west, south.
LOOP = [
    [34.9000, 38.6300],
    [34.9020, 38.6300],
    [34.9030, 38.6300],
    [34.9030, 38.6310],
    [34.9010, 38.6310],
    [34.9000, 38.6310],
    [34.9000, 38.6300],
]
SPEED = 30.0 / 3.6


def profile(**overrides) -> energy.VehicleProfile:
    base = dict(
        name="test",
        mass_empty_kg=4000.0,
        passenger_mass_kg=500.0,
        c_rr=0.01,
        cd_a_m2=3.0,
        eta_drive=0.9,
        eta_regen=0.5,
        p_regen_max_w=60_000.0,
        p_aux_w=2000.0,
        air_density=1.15,
    )
    base.update(overrides)
    return energy.VehicleProfile(**base)


def loop_energy(prof: energy.VehicleProfile, grid: ElevationGrid | None, surface: str = "dem", **options):
    opts = energy.EnergyOptions(surface=surface, **options)
    return energy.segment_energy_j(LOOP, SPEED, prof, opts, grid)


class ClosedTourIdentityTests(unittest.TestCase):
    """The four identities of docs/paper/01 §1.6, as code."""

    def test_flat_terrain_makes_dem_equal_planar_exactly(self):
        flat = hill_grid(flat=True)
        dem = loop_energy(profile(), flat, "dem")
        planar = loop_energy(profile(), None, "planar")
        self.assertAlmostEqual(dem.total_j, planar.total_j, delta=1e-3)
        self.assertEqual(dem.climb_m, 0.0)
        self.assertEqual(dem.descent_m, 0.0)

    def test_lossless_vehicle_spends_nothing_on_a_closed_tour(self):
        lossless = profile(
            eta_drive=1.0, eta_regen=1.0, p_regen_max_w=float("inf"), p_aux_w=0.0, c_rr=0.0, cd_a_m2=0.0
        )
        result = loop_energy(lossless, hill_grid(), "dem")
        # Climb equals descent on a closed tour, so the potential energy nets out.
        self.assertAlmostEqual(result.climb_m, result.descent_m, places=6)
        self.assertLess(abs(result.total_j), 1e-6 * lossless.mass_kg * energy.G * result.climb_m + 1e-6)

    def test_the_two_directions_of_a_loop_are_equal_in_distance_but_not_in_energy(self):
        grid = hill_grid()
        edge = energy.DirectedEdge(tuple((p[0], p[1]) for p in LOOP), 30.0)
        opts = energy.EnergyOptions(surface="dem")
        forward = energy.path_energy([edge], profile(), opts, grid)
        backward = energy.path_energy([edge], profile(), opts, grid, reverse=True)
        self.assertAlmostEqual(forward.length_m, backward.length_m, places=6)
        self.assertAlmostEqual(forward.climb_m, backward.descent_m, places=6)
        # A lossy regeneration path or a power cap makes the direction matter…
        self.assertNotAlmostEqual(forward.total_j, backward.total_j, places=3)
        # …and only a lossless drivetrain makes it not matter.
        lossless = profile(eta_drive=1.0, eta_regen=1.0, p_regen_max_w=float("inf"))
        # (Up to the slope length, which the two directions sample at slightly
        # different points.)
        f2 = energy.path_energy([edge], lossless, opts, grid)
        b2 = energy.path_energy([edge], lossless, opts, grid, reverse=True)
        self.assertAlmostEqual(f2.total_j, b2.total_j, delta=1e-4 * f2.total_j)

    def test_symmetric_hill_costs_the_lossy_share_of_its_potential(self):
        # Up the hill and back down the same way: one climb H, one descent H.
        # Gravity only: with rolling resistance a gentle descent near the
        # smoothed summit is still a traction piece, and the identity is then
        # only approximate — which is exactly the non-linearity H2 relies on.
        out_and_back = [[34.9000, 38.6300], [34.9030, 38.6300], [34.9000, 38.6300]]
        prof = profile(c_rr=0.0, cd_a_m2=0.0, p_aux_w=0.0, p_regen_max_w=float("inf"))
        opts = energy.EnergyOptions(surface="dem")
        dem = energy.segment_energy_j(out_and_back, SPEED, prof, opts, hill_grid())
        planar = energy.segment_energy_j(out_and_back, SPEED, prof, energy.EnergyOptions(surface="planar"))
        height = dem.climb_m
        expected = (1.0 / prof.eta_drive - prof.eta_regen) * prof.mass_kg * energy.G * height
        self.assertGreater(height, 5.0)
        self.assertAlmostEqual(dem.total_j - planar.total_j, expected, delta=expected * 1e-6 + 1e-6)


class SegmentModelTests(unittest.TestCase):
    def test_power_cap_sends_the_excess_to_the_brakes(self):
        # A very steep descent at speed: more braking power than the cap.
        steep = [[34.9030, 38.6300], [34.9000, 38.6300]]
        capped = profile(p_regen_max_w=5_000.0)
        uncapped = profile(p_regen_max_w=float("inf"))
        opts = energy.EnergyOptions(surface="dem")
        a = energy.segment_energy_j(steep, SPEED, capped, opts, hill_grid(east_rise_per_cell=6.0))
        b = energy.segment_energy_j(steep, SPEED, uncapped, opts, hill_grid(east_rise_per_cell=6.0))
        self.assertGreater(a.friction_loss_j, 0.0)
        self.assertEqual(b.friction_loss_j, 0.0)
        self.assertLess(a.regen_j, b.regen_j)
        self.assertGreater(a.total_j, b.total_j)

    def test_planar_rate_is_positive_and_speed_dependent(self):
        slow = energy.flat_energy_j_per_m(20 / 3.6, profile())
        fast = energy.flat_energy_j_per_m(60 / 3.6, profile())
        self.assertGreater(slow, 0.0)
        self.assertNotEqual(slow, fast)

    def test_grade_cap_is_counted(self):
        cliff = hill_grid(east_rise_per_cell=30.0)  # ~70 % grade
        result = loop_energy(profile(), cliff, "dem", grade_cap=0.2)
        self.assertGreater(result.n_capped, 0)

    def test_nodata_is_carried_and_counted(self):
        outside = [[34.9000, 38.6300], [34.9200, 38.6300]]  # runs off the grid to the east
        result = energy.segment_energy_j(outside, SPEED, profile(), energy.EnergyOptions(), hill_grid())
        self.assertGreater(result.n_nodata, 0)
        self.assertGreater(result.total_j, 0.0)

    def test_stop_energy_and_potential(self):
        prof = profile()
        stop = energy.stop_energy_j(SPEED, 30.0, prof)
        self.assertGreater(stop, prof.p_aux_w * 30.0)
        self.assertAlmostEqual(
            energy.potential_j(100.0, prof), prof.eta_regen * prof.mass_kg * energy.G * 100.0
        )

    def test_potential_correction_is_non_negative_on_every_directed_piece(self):
        grid = hill_grid()
        prof = profile()
        opts = energy.EnergyOptions(surface="dem")
        for a, b in zip(LOOP, LOOP[1:]):
            for start, end in ((a, b), (b, a)):
                seg = energy.segment_energy_j([start, end], SPEED, prof, opts, grid)
                h_start = grid.at(start[1], start[0])
                h_end = grid.at(end[1], end[0])
                corrected = seg.total_j + energy.potential_j(h_start, prof) - energy.potential_j(h_end, prof)
                self.assertGreaterEqual(corrected, -1e-6)


class TerrainVariantTests(unittest.TestCase):
    def test_scaling_flattens_and_exaggerates_around_the_reference(self):
        base = hill_grid()
        flat = energy.derive_grid(base, dem_scale=0.0, reference_m=100.0)
        double = energy.derive_grid(base, dem_scale=2.0, reference_m=100.0)
        self.assertEqual(flat.at(38.6300, 34.9030), 100.0)
        self.assertAlmostEqual(double.at(38.6300, 34.9030), 100.0 + 2 * (base.at(38.6300, 34.9030) - 100.0))
        self.assertEqual(double.meta["derivation"]["dem_scale"], 2.0)
        # The base is untouched.
        self.assertEqual(base.at(38.6300, 34.9030), 100.0 + 3 * 8)

    def test_noise_is_reproducible_and_keeps_holes(self):
        base = hill_grid()
        a = energy.derive_grid(base, noise_sigma_m=4.0, seed=7)
        b = energy.derive_grid(base, noise_sigma_m=4.0, seed=7)
        c = energy.derive_grid(base, noise_sigma_m=4.0, seed=8)
        self.assertEqual(a.at(38.6300, 34.9010), b.at(38.6300, 34.9010))
        self.assertNotEqual(a.at(38.6300, 34.9010), c.at(38.6300, 34.9010))
        self.assertNotEqual(a.at(38.6300, 34.9010), base.at(38.6300, 34.9010))


class ProfileAndFeatureTests(unittest.TestCase):
    def test_profiles_load_with_mass_scenarios_and_overrides(self):
        empty = energy.vehicle_profile(mass_scenario="empty")
        full = energy.vehicle_profile(mass_scenario="full", eta_regen=0.7, p_regen_max_kw=30)
        self.assertEqual(empty.passenger_mass_kg, 0.0)
        self.assertEqual(full.passenger_mass_kg, 14 * 75.0)
        self.assertEqual(full.eta_regen, 0.7)
        self.assertEqual(full.p_regen_max_w, 30_000.0)
        with self.assertRaises(energy.EnergyConfigurationError):
            energy.vehicle_profile("no-such-vehicle")
        with self.assertRaises(energy.EnergyConfigurationError):
            energy.vehicle_profile(mass_scenario="overloaded")

    def test_feature_edges_honour_edge_speed_ranges(self):
        feature = {
            "properties": {"edge_speeds": [[0, 2, 20.0], [2, 3, 50.0]]},
            "geometry": {"type": "LineString", "coordinates": LOOP[:4]},
        }
        edges = energy.feature_edges(feature, 30.0)
        self.assertEqual([edge.speed_kmh for edge in edges], [20.0, 50.0])
        self.assertEqual(len(edges[0].coordinates), 3)
        self.assertEqual(edges[0].coordinates[-1], edges[1].coordinates[0])
        drawn = energy.feature_edges({"geometry": {"type": "MultiLineString", "coordinates": [LOOP[:3], LOOP[2:5]]}}, 30.0)
        self.assertEqual(len(drawn), 1)
        self.assertEqual(drawn[0].speed_kmh, 30.0)
        self.assertEqual(len(drawn[0].coordinates), 5)

    def test_a_gap_between_drawn_parts_is_not_driven(self):
        # Two parts that do not touch: the straight line between them is a
        # valley crossing nobody drives, so it must not add length or climb.
        apart = {"geometry": {"type": "MultiLineString", "coordinates": [LOOP[:3], LOOP[4:7]]}}
        edges = energy.feature_edges(apart, 30.0)
        self.assertEqual(len(edges), 2)
        total = energy.path_energy(edges, profile(), energy.EnergyOptions(surface="planar"))
        parts = sum(
            energy.segment_energy_j(part, SPEED, profile(), energy.EnergyOptions(surface="planar")).length_m
            for part in (LOOP[:3], LOOP[4:7])
        )
        self.assertAlmostEqual(total.length_m, parts, places=6)
        self.assertEqual(len(energy.line_runs(apart["geometry"])), 2)

    def test_grid_written_to_disk_reads_back(self):
        with tempfile.TemporaryDirectory() as directory:
            written = write_hill_grid(Path(directory))
            grid = ElevationGrid.load(written)
            self.assertEqual(grid.at(38.6300, 34.9000), hill_grid().at(38.6300, 34.9000))


if __name__ == "__main__":
    unittest.main()


class ResampleTests(unittest.TestCase):
    def test_keeping_vertices_preserves_length_in_both_directions(self):
        from urgup_transport.elevation import resample_line

        def length(points):
            return sum(haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(points, points[1:]))

        corner = [[34.9000, 38.6300], [34.9030, 38.6300], [34.9030, 38.6310]]
        plain = resample_line(corner, 25.0)
        kept = resample_line(corner, 25.0, keep_vertices=True)
        kept_back = resample_line(corner[::-1], 25.0, keep_vertices=True)
        self.assertIn([34.9030, 38.6300], kept)
        self.assertNotIn([34.9030, 38.6300], plain)
        self.assertAlmostEqual(length(kept), length(corner), places=6)
        self.assertAlmostEqual(length(kept_back), length(corner), places=6)
        self.assertLess(length(plain), length(corner))


class CorrelatedNoiseTests(unittest.TestCase):
    """The Monte Carlo perturbation, with its correlation length as a parameter."""

    def test_window_three_is_the_direct_neighbourhood_mean(self):
        import random

        rows, cols, sigma, seed = 6, 12, 4.0, 7
        separable = energy.correlated_noise(rows, cols, sigma, seed, 3)
        rng = random.Random(seed)
        white = [rng.gauss(0.0, sigma) for _ in range(rows * cols)]
        for r in range(rows):
            for c in range(cols):
                total, count = 0.0, 0
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        rr, cc = r + dr, c + dc
                        if 0 <= rr < rows and 0 <= cc < cols:
                            total += white[rr * cols + cc]
                            count += 1
                self.assertAlmostEqual(separable[r * cols + c], total / count * math.sqrt(count), places=9)

    def test_wider_window_keeps_sigma_and_raises_the_neighbour_correlation(self):
        rows = cols = 60
        sigma = 4.0

        def stats(window):
            field = energy.correlated_noise(rows, cols, sigma, 11, window)
            inner = [field[r * cols + c] for r in range(10, 50) for c in range(10, 50)]
            mean = sum(inner) / len(inner)
            var = sum((v - mean) ** 2 for v in inner) / len(inner)
            pairs = [(field[r * cols + c], field[r * cols + c + 1]) for r in range(10, 50) for c in range(10, 49)]
            cov = sum((a - mean) * (b - mean) for a, b in pairs) / len(pairs)
            return math.sqrt(var), cov / var

        sd3, rho3 = stats(3)
        sd10, rho10 = stats(10)
        self.assertAlmostEqual(sd3, sigma, delta=0.8)
        self.assertAlmostEqual(sd10, sigma, delta=1.2)
        # A 3×3 box shares 6 of 9 cells with its neighbour, a 10×10 box 90 of 100.
        self.assertGreater(rho3, 0.5)
        self.assertGreater(rho10, rho3 + 0.1)

    def test_derive_grid_records_the_correlation_length(self):
        base = hill_grid()
        wide = energy.derive_grid(base, noise_sigma_m=4.0, seed=7, noise_corr_cells=5)
        narrow = energy.derive_grid(base, noise_sigma_m=4.0, seed=7)
        self.assertEqual(wide.meta["derivation"]["noise_corr_cells"], 5)
        self.assertEqual(narrow.meta["derivation"]["noise_corr_cells"], 3)
        self.assertNotEqual(wide.at(38.6300, 34.9010), narrow.at(38.6300, 34.9010))
        self.assertIsNone(energy.derive_grid(base, dem_scale=2.0).meta["derivation"]["noise_corr_cells"])


class LegLoadTests(unittest.TestCase):
    def test_with_passenger_mass_changes_only_the_load(self):
        base = profile()
        heavy = energy.with_passenger_mass(base, 1050.0)
        empty = energy.with_passenger_mass(base, 0.0)
        self.assertEqual(heavy.mass_kg, base.mass_empty_kg + 1050.0)
        self.assertEqual(empty.mass_kg, base.mass_empty_kg)
        self.assertEqual(heavy.eta_regen, base.eta_regen)
        self.assertEqual(heavy.p_regen_max_w, base.p_regen_max_w)
        grid = hill_grid()
        e_heavy = loop_energy(heavy, grid).total_j
        e_empty = loop_energy(empty, grid).total_j
        self.assertGreater(e_heavy, e_empty)
        # Stop energy scales with the mass on board too.
        self.assertGreater(energy.stop_energy_j(SPEED, 30.0, heavy), energy.stop_energy_j(SPEED, 30.0, empty))

    def test_options_validate_the_correlation_length(self):
        with self.assertRaises(energy.EnergyConfigurationError):
            energy.EnergyOptions(dem_noise_corr_cells=0)
        self.assertTrue(energy.EnergyOptions(exact_potentials=True).as_dict()["exact_potentials"])
