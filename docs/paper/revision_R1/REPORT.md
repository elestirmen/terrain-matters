# R1 revision experiments report

Run ids: 2026-10-R1 (sub-runs E1, E2, E3, E4, E5, E6, E7); reference run 2026-09-29-urgup | Git commit: 40b7e70675b3 (working tree dirty at run time) | Date: 2026-09-29 | Machine: ubuntu (Linux-7.0.0-34-generic-x86_64-with-glibc2.43, 4 CPUs, Python 3.14.4, OR-Tools 9.15.6755) | Total solver time: 12,716 s (3.53 h)

Every number below is read from the CSV files in this folder by `scripts/run_r1_revision_experiments.py report`; nothing is typed by hand. Fixed factors unless a table says otherwise: 129 customer stops + depot (planning register revision 135), 8 lines, road graph revision 1584, graph speeds with 30 km/h fallback, 30 s dwell, balance weight 20, η_regen = 0.5, 7 passengers (4,725 kg), dem_scale 1, grade cap 0.20, 15 s per solve, seed 42. "Optimal" never appears: every solution is the best found under its time limit.

## E0 Capacity check

**Is there a vehicle-capacity constraint? No.** The manuscript's phrase "capacitated closed-tour VRP" is not what the code solves. The only capacity-like dimension in the OR-Tools model counts *stops* (one unit per stop) with a vehicle capacity equal to `max_stops_per_route`, which the main run sets to 0, so the capacity becomes the total number of stops (129) and never binds; its only active part is the minimum of 1 stop per vehicle. Passenger demand (`demand_weight` / `service_demand`, values 0.25–2.0 from the boarding-intensity survey, 1.0 where unknown) is read, carried through the proposal and reported per line (`demand_weight`, `demand_imbalance_percent`) but enters neither the objective nor any constraint. `vehicle_capacity` and `demand_balance_weight` are accepted as deprecated compatibility inputs and listed under `deprecated_network_design_parameters` in every proposal; the README (line 239) says so. The 14-seat figure comes from `config.DEFAULT_SERVICE_VEHICLE_CAPACITY` and is used by the service planner (frequencies), not by line design. **Recommendation for the manuscript:** call the problem a closed-tour multi-vehicle routing problem with a fixed fleet, mandatory visits, a same-vehicle (neighbourhood) constraint and a route-length balance term, and state explicitly that demand and capacity are out of scope (consistent with the stated scope: service level fixed, energy per cycle). Constraints active in the main run, with file and lines (line numbers of the R1 commit):

| Constraint / term | Status in the main run | File | Lines |
|---|---|---|---|
| Objective: arc cost = matrix of the cost basis (distance m, travel-time s or energy Wh rounded to integers, shifted by a constant when negative) | active | urgup_transport/optimizer.py | 2820-2827 |
| Fixed number of vehicles = route_count (8); every vehicle starts and ends at the depot (node 0) | active | urgup_transport/optimizer.py | 2815 |
| Every customer stop is mandatory (no disjunction; only optional stopping-place candidates get one, and the main run has none) | active | urgup_transport/optimizer.py | 2843-2854 |
| Distance dimension with global-span cost: distance_balance_weight (20) × longest route distance in metres | active | urgup_transport/optimizer.py | 2862-2870 |
| Route distance cap max_route_distance_km (0 = the sum of the matrix maxima, i.e. not binding) | inactive (0) | urgup_transport/optimizer.py | 2862-2866 |
| Time dimension max_route_duration_minutes (added only when > 0) | inactive (0) | urgup_transport/optimizer.py | 2882-2883 |
| 'Stops' dimension: each stop counts 1; vehicle capacity = max_stops_per_route or (customers + candidates) = 129, i.e. not binding; minimum min_stops_per_route (1) per vehicle | active (min 1), capacity not binding | urgup_transport/optimizer.py | 2891-2901 |
| Same-vehicle constraint per neighbourhood: VehicleVar equality for every stop of a protected group (single_route_per_mahalle=True protects all 9 neighbourhoods; groups smaller than small_mahalle_stop_limit=4 are folded into the nearest larger one on the planning matrix, giving 8 groups for 8 vehicles) | active | urgup_transport/optimizer.py | 2905-2907, 1701-1800 |
| Passenger/parcel capacity: vehicle_capacity, demand_balance_weight | NOT implemented (deprecated compatibility inputs; not used in the VRP model or in the GA fitness) | urgup_transport/optimizer.py; README.md | 2531-2535; README 239 |
| Stop demand (demand_weight / service_demand): read from the demand CSV or the stop record, carried into the proposal and reported as demand_weight per line and demand_imbalance_percent; never a constraint or an objective term | reporting only | urgup_transport/optimizer.py | 380-410, 2446-2455 |
| longest_route_weight (28) and the fitness function _fitness: GA mode only; not used in planning_mode=vrp | inactive in vrp | urgup_transport/optimizer.py | 1661-1699 |
| Search: PATH_CHEAPEST_ARC first solution, GUIDED_LOCAL_SEARCH, wall-clock time limit; initial assignment = bin-packed seed of the protected groups | active | urgup_transport/optimizer.py | 2910-2913, 2935-2942 |
| seed (42): used by the GA only; the OR-Tools search has no seed and is deterministic on this instance (verified: identical repeats; solver.ReSeed changes nothing). R1 adds node_order_seed (customer order permutation) for E1 | no effect in vrp | urgup_transport/optimizer.py | 2760-2769 |
| Within-line sequence: exact TSP for ≤ 8 stops, else 2-opt (+ or-opt for the energy basis) on the planning matrix | active | urgup_transport/optimizer.py | 1576-1650 |

A consequence worth stating in the paper: with all nine neighbourhoods protected and the one-stop neighbourhood folded into its nearest larger one, the main run hands the solver exactly eight same-vehicle groups for eight vehicles that must each serve at least one stop, so **line membership is fully determined by the neighbourhood rule**; the solver's freedom is the visiting order and the path between stops. The single stop that changes line at η_regen = 0.7 and at dem_scale ≥ 1.5 in the main run is the folded one-stop neighbourhood, whose "nearest larger group" is measured on the planning matrix and therefore depends on the cost basis. Experiment E2 removes the rule.

## E1 Solver robustness

Run id 2026-10-R1/E1, 60 optimisations (cost basis ∈ {distance, energy-dem} × node-order seed 1–10 × time limit 15/60/300 s), η_regen 0.5, 7 passengers, neighbourhood rule on, balance weight 20; solver time 7,773 s. OR-Tools has no random seed and returned the identical network on three repeats in the main run, so "seed" here is a seeded permutation of the customer node order handed to the model (`node_order_seed`), which changes the first solution and the local-search trajectory but nothing about the problem. Every solution is evaluated on the reference DEM.

| Cost basis | Time limit (s) | E_dem mean (Wh) | s.d. | best | worst | main run (seed 42) | km mean | Jaccard to reference | Kendall τ | VoTI mean (Wh) | s.d. | min | max | VoTI mean (%) | positive of n |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| distance | 15 | 34,093 | 94.6 | 33,988 | 34,313 | 33,940 | 82.2 | 1.0 | 0.68 |  |  |  |  |  |  |
| distance | 60 | 34,093 | 94.6 | 33,988 | 34,313 | 33,940 | 82.2 | 1.0 | 0.68 |  |  |  |  |  |  |
| distance | 300 | 34,069 | 95.8 | 33,978 | 34,278 | 33,940 | 82.2 | 1.0 | 0.754 |  |  |  |  |  |  |
| energy-dem | 15 | 33,142 | 117.7 | 32,996 | 33,391 | 33,063 | 84.7 | 1.0 | 0.459 | 951.6 | 136.1 | 597.1 | 1,113 | 2.8 | 10 |
| energy-dem | 60 | 33,142 | 117.7 | 32,996 | 33,391 | 33,063 | 84.7 | 1.0 | 0.455 | 951.6 | 136.1 | 597.1 | 1,113 | 2.8 | 10 |
| energy-dem | 300 | 33,092 | 94.5 | 32,996 | 33,264 | 33,063 | 84.5 | 1.0 | 0.496 | 977.0 | 51.9 | 874.9 | 1,078 | 2.9 | 10 |

**Sentences for the manuscript.** Across the 30 seed × time-limit pairs, VoTI against the distance network of the same seed and time limit is 960 ± 116 Wh (mean ± s.d.), positive in 30 of 30; per time limit: 15 s: 952 ± 136 Wh, 10/10 positive; 60 s: 952 ± 136 Wh, 10/10 positive; 300 s: 977 ± 52 Wh, 10/10 positive. The main run's 877 Wh (2.6 %) sits at the 17th percentile of this distribution. The heuristic noise of the objective itself is small: the DEM-optimised energy varies by 118 Wh (s.d., 0.36 %) across seeds at 15 s and by 94 Wh at 300 s; the distance-optimised network's DEM energy varies by 95 Wh at 15 s and 96 Wh at 300 s. Longer time limits change the best-found energy by -50 Wh (DEM) and -24 Wh (distance) from 15 s to 300 s. Membership stays fixed (mean Jaccard to the reference 1.000), as E0 predicts.

Figure: `figR1_solver_robustness.png`.

## E2 Neighbourhood constraint off

Run id 2026-10-R1/E2, 18 optimisations, solver time 2,091 s. The rule is switched off with the existing `single_route_per_mahalle=False` (no neighbourhood carries its own `single_route_only` flag, so no group remains) and `small_mahalle_stop_limit=0`. Three variants: `off_cold_15` is the plan as written (OR-Tools from its own first solution, 15 s); because that returned networks 4–12 km *longer* than the constrained ones (the constrained solution is feasible for the relaxed problem, so this is a heuristic failure, not a property of the relaxation), `off_warm_15` and `off_warm_300` start the local search from the constrained main-run solution of the same cell (new optimiser parameter `warm_start_stop_ids`, unit-tested to be never worse than its start) for 15 s and 300 s. Distance and planar solutions are η-independent and the distance solution is dem_scale-independent, so they are optimised once per variant (6 optimisations per variant, 18 in all).

| Variant | η | dem_scale | s | E_dem distance-opt | E_dem planar-opt | E_dem DEM-opt | km DEM-opt | VoTI vs distance (Wh, %) | VoTI vs planar (Wh, %) | Jaccard planar/DEM | stops changing line planar/DEM | Kendall τ | Jaccard distance/DEM | stops changing distance/DEM | Jaccard DEM-opt vs constrained DEM-opt | stops changing vs constrained | line sizes (stops) | mean asymmetry | friction share % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| on | 0.5 | 1.0 | 15 | 33,940 | 34,159 | 33,063 | 83.8 | 877 (2.6 %) | 1,096 (3.2 %) | 1.0 | 0 | 0.598 | 1.0 | 0 | 1.0 | 0 | 5–25 | 0.0569 | 2.64 |
| on | 0.7 | 1.0 | 15 | 30,730 | 30,843 | 29,228 | 81.3 | 1,502 (4.9 %) | 1,615 (5.2 %) | 0.969 | 1 | 0.8137 | 0.969 | 1 | 1.0 | 0 | 4–25 | 0.0329 | 2.54 |
| on | 0.5 | 1.5 | 15 | 41,679 | 41,976 | 38,441 | 83.0 | 3,238 (7.8 %) | 3,535 (8.4 %) | 0.969 | 1 | 0.5222 | 0.969 | 1 | 1.0 | 0 | 4–25 | 0.0625 | 6.04 |
| on | 0.5 | 2.0 | 15 | 48,597 | 48,940 | 43,262 | 84.2 | 5,336 (11.0 %) | 5,678 (11.6 %) | 0.969 | 1 | 0.5038 | 0.969 | 1 | 1.0 | 0 | 4–25 | 0.1001 | 9.16 |
| off_cold_15 | 0.5 | 1.0 | 15 | 37,075 | 39,206 | 34,724 | 85.1 | 2,351 (6.3 %) | 4,483 (11.4 %) | 0.3621 | 62 | 0.1513 | 0.3826 | 61 | 0.3516 | 60 | 1–54 | 0.0204 | 3.23 |
| off_cold_15 | 0.7 | 1.0 | 15 | 33,672 | 35,316 | 33,828 | 96.3 | -156 (-0.5 %) | 1,487 (4.2 %) | 0.1686 | 71 | 0.6124 | 0.3091 | 66 | 0.1594 | 82 | 1–55 | 0.0264 | 2.08 |
| off_cold_15 | 0.5 | 1.5 | 15 | 45,144 | 48,035 | 39,076 | 85.8 | 6,068 (13.4 %) | 8,960 (18.6 %) | 0.4361 | 53 | -0.2348 | 0.4545 | 51 | 0.2866 | 64 | 1–46 | 0.0476 | 5.78 |
| off_cold_15 | 0.5 | 2.0 | 15 | 52,034 | 55,538 | 46,979 | 93.3 | 5,055 (9.7 %) | 8,559 (15.4 %) | 0.1796 | 71 | 0.3567 | 0.3436 | 66 | 0.2356 | 73 | 1–54 | 0.0712 | 8.56 |
| off_warm_15 | 0.5 | 1.0 | 15 | 31,686 | 31,735 | 31,455 | 75.9 | 231 (0.7 %) | 280 (0.9 %) | 0.5821 | 35 | 0.3339 | 0.5159 | 42 | 0.2629 | 68 | 1–63 | 0.0208 | 3.39 |
| off_warm_15 | 0.7 | 1.0 | 15 | 28,471 | 28,441 | 28,260 | 75.2 | 211 (0.7 %) | 181 (0.6 %) | 0.4957 | 39 | 0.3318 | 0.4264 | 36 | 0.2963 | 72 | 1–59 | 0.0226 | 3.9 |
| off_warm_15 | 0.5 | 1.5 | 15 | 38,770 | 38,994 | 37,654 | 79.6 | 1,115 (2.9 %) | 1,339 (3.4 %) | 0.5372 | 50 | 0.0511 | 0.503 | 47 | 0.3244 | 69 | 1–59 | 0.0454 | 5.98 |
| off_warm_15 | 0.5 | 2.0 | 15 | 44,808 | 44,997 | 42,054 | 80.2 | 2,754 (6.2 %) | 2,942 (6.5 %) | 0.6494 | 25 | 0.5027 | 0.6067 | 30 | 0.3724 | 59 | 1–50 | 0.0753 | 9.06 |
| off_warm_300 | 0.5 | 1.0 | 300 | 30,468 | 31,604 | 31,124 | 74.9 | -656 (-2.1 %) | 480 (1.5 %) | 0.5764 | 34 | 0.3292 | 0.6912 | 17 | 0.2574 | 68 | 1–63 | 0.0213 | 3.44 |
| off_warm_300 | 0.7 | 1.0 | 300 | 27,320 | 28,298 | 27,884 | 74.9 | -564 (-2.1 %) | 414 (1.5 %) | 0.4979 | 29 | 0.5602 | 0.5799 | 23 | 0.2405 | 72 | 1–64 | 0.0248 | 3.94 |
| off_warm_300 | 0.5 | 1.5 | 300 | 37,319 | 38,848 | 37,202 | 79.1 | 117 (0.3 %) | 1,646 (4.2 %) | 0.5092 | 54 | 0.1762 | 0.6333 | 26 | 0.3197 | 69 | 1–59 | 0.0465 | 5.96 |
| off_warm_300 | 0.5 | 2.0 | 300 | 43,295 | 44,884 | 41,995 | 80.0 | 1,299 (3.0 %) | 2,888 (6.4 %) | 0.6983 | 21 | 0.2147 | 0.5425 | 37 | 0.4104 | 53 | 1–54 | 0.0612 | 8.78 |

Terrain-aware local search started from the best distance / planar network of the same setting (300 s). What it removes is a lower bound of VoTI that two independent heuristic runs cannot fake, because the search can only improve on its start:

| Variant | Start | E_dem of start (Wh) | km start | E_dem after DEM search | km | removed (Wh) | % | Jaccard vs start | stops changing vs start | τ vs start | Jaccard vs constrained DEM-opt | min stops | max stops |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| off_warm_300_from_distance | warm_from_E2-off_warm_300-distance | 30468.3 | 71.037 | 30462.0 | 72.331 | 6.3 | 0.02 | 0.9729 | 3 | 0.9233 | 0.2015 | 1 | 57 |
| off_warm_300_from_planar | warm_from_E2-off_warm_300-planar-average | 31604.2 | 73.373 | 30609.1 | 73.708 | 995.1 | 3.15 | 0.7644 | 11 | 0.912 | 0.3803 | 1 | 45 |
| on_warm_300_from_distance | warm_from_A-distance | 33940.0 | 81.838 | 33008.7 | 84.009 | 931.3 | 2.74 | 1.0 | 0 | 0.3724 | 1.0 | 5 | 25 |
| on_warm_300_from_planar | warm_from_A-planar-average | 34158.9 | 81.975 | 33010.0 | 84.252 | 1148.9 | 3.36 | 1.0 | 0 | 0.5301 | 1.0 | 5 | 25 |

**Sentences for the manuscript.** With the rule off (warm start, 300 s), the planar-energy and DEM-energy networks differ in membership: mean Jaccard 0.5764 with 34 of 129 stops on a different line (against 1.0 and 0 with the rule on); the DEM network itself moves 68 stops relative to the constrained DEM network. VoTI against the distance network is -656 Wh (-2.1 %) unconstrained versus 877 Wh (2.6 %) constrained, and against the planar network 480 Wh (1.5 %) versus 1,096 Wh (3.2 %). The cold 15 s variant gives VoTI 2,351 Wh (6.3 %) vs distance and 4,483 Wh vs planar, with 62 stops changing line between planar and DEM, but its three networks are +7.1 km (distance) longer than the constrained ones, so its VoTI mixes terrain with solver noise and should not be quoted alone. At dem_scale 1.5: VoTI vs distance 117 Wh (0.3 %) unconstrained (warm 300 s) vs 3,238 Wh (7.8 %) constrained; planar/DEM Jaccard 0.5092 (54 stops) vs 0.969 (1). At dem_scale 2.0: VoTI vs distance 1,299 Wh (3.0 %) unconstrained (warm 300 s) vs 5,336 Wh (11.0 %) constrained; planar/DEM Jaccard 0.6983 (21 stops) vs 0.969 (1). At η_regen 0.7: VoTI vs distance -564 Wh (-2.1 %) unconstrained vs 1,502 Wh (4.9 %) constrained; planar/DEM Jaccard 0.4979 (29 stops). Starting the DEM search from the E2-off_warm_300-distance network (off rule) removes 6 Wh (0.0 %) of its 30,468 Wh, moving 3 stops to another line (Jaccard 0.9729, τ 0.9233). Starting the DEM search from the E2-off_warm_300-planar-average network (off rule) removes 995 Wh (3.1 %) of its 31,604 Wh, moving 11 stops to another line (Jaccard 0.7644, τ 0.912). Starting the DEM search from the A-distance network (on rule) removes 931 Wh (2.7 %) of its 33,940 Wh, moving 0 stops to another line (Jaccard 1.0, τ 0.3724). Starting the DEM search from the A-planar-average network (on rule) removes 1,149 Wh (3.4 %) of its 34,159 Wh, moving 0 stops to another line (Jaccard 1.0, τ 0.5301). The independent unconstrained runs are dominated by search noise: the 300 s DEM-energy network is 74.9 km against the 300 s distance network's 71.0 km, and the distance network's DEM energy is lower by 656 Wh, an amount smaller than the spread between local optima reached from different starts of the same objective; the chained search above is the number to quote for the unconstrained case.

Figures: `figR2_membership_unconstrained.png` (warm start, 300 s) and `figR2b_membership_unconstrained_cold.png` (cold start, 15 s); line names are "Line N" by index.

## E3 DEM noise levels

Run id 2026-10-R1/E3, 240 optimisations (σ ∈ {1, 2, 2.5, 4} m × correlation length L ∈ {3, 10} cells × 30 repeats), 5 s per solve (the main run's Monte Carlo limit), η_regen 0.5, 7 passengers, solver time 2,805 s. Noise model: Gaussian per cell with the stated σ, then a box mean over L × L cells (L = 3: offsets −1…+1, the main run's method; L = 10: offsets −5…+4, a 10 × 10 window ≈ 300 m), rescaled by √(cells in the window) so the marginal standard deviation stays σ; each (σ, L, repeat) has its own seed (3000 + 1000*corr_index + 100*sigma_index + repeat), the base grid is never modified. Every network is evaluated on its own perturbed grid and on the reference grid; VoTI on the reference grid = E_ref(distance network) − E_ref(network optimised on the perturbed grid).

| σ (m) | L (cells) | n | VoTI ref p50 (Wh) | p5 | p95 | mean | s.d. | share negative | VoTI own grid p50 | spurious climb (m, mean) | E on ref grid mean | Jaccard to ref DEM solution | stops changing (mean) | modal line stability |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.0 | 3 | 30 | 752.9 | 638.3 | 843.4 | 768.1 | 143.6 | 0.0 | 886.8 | 49.1 | 33171.9 | 0.999 | 0.03 | 0.9997 |
| 2.0 | 3 | 30 | 466.6 | 145.8 | 814.1 | 489.7 | 249.6 | 0.0 | 1061.1 | 185.3 | 33450.3 | 0.9934 | 0.17 | 0.9987 |
| 2.5 | 3 | 30 | 237.4 | -83.3 | 479.9 | 247.5 | 254.6 | 0.1 | 1232.3 | 281.3 | 33692.5 | 0.9944 | 0.13 | 0.999 |
| 4.0 | 3 | 30 | -277.6 | -616.2 | 504.9 | -177.6 | 347.0 | 0.767 | 1945.8 | 609.9 | 34117.6 | 0.9893 | 0.27 | 0.9979 |
| 1.0 | 10 | 30 | 826.2 | 773.6 | 1552.9 | 911.5 | 249.8 | 0.0 | 900.0 | 15.4 | 33028.5 | 0.9959 | 0.13 | 0.999 |
| 2.0 | 10 | 30 | 715.0 | 578.8 | 1520.8 | 823.4 | 317.6 | 0.0 | 1009.4 | 66.9 | 33116.6 | 0.9872 | 0.33 | 0.9974 |
| 2.5 | 10 | 30 | 654.1 | 478.7 | 1269.3 | 708.4 | 236.9 | 0.0 | 992.7 | 111.2 | 33231.6 | 0.9898 | 0.27 | 0.9979 |
| 4.0 | 10 | 30 | 411.4 | 209.3 | 1151.9 | 474.0 | 282.3 | 0.033 | 1066.2 | 273.0 | 33466.0 | 0.9847 | 0.37 | 0.9972 |

**Sentences for the manuscript.** L = 3: median VoTI on the reference grid 753 Wh at σ = 1.0 m (0 % negative), 467 Wh at σ = 2.0 m (0 % negative), 237 Wh at σ = 2.5 m (10 % negative), -278 Wh at σ = 4.0 m (77 % negative); the median first falls below zero at σ = 4.0 m and below half the reference value (438 Wh) at σ = 2.5 m. L = 10: median VoTI on the reference grid 826 Wh at σ = 1.0 m (0 % negative), 715 Wh at σ = 2.0 m (0 % negative), 654 Wh at σ = 2.5 m (0 % negative), 411 Wh at σ = 4.0 m (3 % negative); the median never falls below zero in the tested range and below half the reference value (438 Wh) at σ = 4.0 m. Longer correlation (L = 10 vs 3) changes the median VoTI by +73 Wh at σ = 1 m, +248 Wh at σ = 2 m, +417 Wh at σ = 2.5 m, +689 Wh at σ = 4 m and the spurious climb by -34 m, -118 m, -170 m, -337 m. Membership is stable at every level (Jaccard to the reference DEM solution ≥ 0.985, modal-line stability ≥ 0.997). The network optimised on the unperturbed grid has VoTI 877 Wh.

Figure: `figR3_voti_vs_sigma.png`.

## E4 Exact shortest paths

Run id 2026-10-R1/E4. For each of the 12 (η_regen, mass) scenarios the stop-to-stop matrix was built twice on the same graph: with the elevation potential π = η_regen·m·g·h and clipping (the main run) and with Johnson potentials from a Bellman-Ford pass over the true directed edge energies (new option `energy_options.exact_potentials=True`, unit-tested against Bellman-Ford). The physics potential leaves 0–14 of 4321 directed edges with a negative corrected weight (clipped to 0); Johnson leaves 0, and no negative cycle was found in any scenario (none). Over the 16770 ordered stop pairs, at η_regen = 0.0 0 pairs differ by more than 0.01 Wh (largest |Δ| 0.00 Wh, largest relative Δ 0.000 %); at η_regen = 0.3 0 pairs differ by more than 0.01 Wh (largest |Δ| 0.00 Wh, largest relative Δ 0.000 %); at η_regen = 0.5 0 pairs differ by more than 0.01 Wh (largest |Δ| 0.00 Wh, largest relative Δ 0.000 %); at η_regen = 0.7 125 pairs differ by more than 0.01 Wh (largest |Δ| 33.31 Wh, largest relative Δ 2.883 %) (relative to the exact value, floored at 1 Wh; in every differing pair the clipped path costs more, never less). 
Re-pricing the 12 main-run DEM networks with the exact matrix changes their tour energy by at most 0.00 Wh (0 of 137 legs differ). 
Re-optimising η_regen 0.5 / 7 passengers with the exact matrix (15 s, same settings as the main run) returned the identical network (Jaccard 1.0, 0 stops changing line, Kendall τ 1.0): E_dem 33,063 Wh vs 33,063 Wh, VoTI vs distance 877 Wh (2.6 %) vs 877 Wh in the main run. Conclusion: the clipping never changes a leg of the tours actually found and never changes the network at the main scenario; the exact option is kept in the code (`exact_potentials`) for graphs where the grade cap bites harder.

| η | mass | negative edges (physics π) | negative edges (Johnson) | negative cycle | pairs | pairs differing | max |Δ| (Wh) | max rel Δ (%) | mean rel Δ (%) | BF relaxations (max per node) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.0 | empty | 0 | 0 | False | 16770 | 0 | 0.0 | 0.0 | 0.0 | 0 |
| 0.0 | average | 0 | 0 | False | 16770 | 0 | 0.0 | 0.0 | 0.0 | 0 |
| 0.0 | full | 0 | 0 | False | 16770 | 0 | 0.0 | 0.0 | 0.0 | 0 |
| 0.3 | empty | 1 | 0 | False | 16770 | 0 | 0.0 | 0.0 | 0.0 | 17 |
| 0.3 | average | 1 | 0 | False | 16770 | 0 | 0.0 | 0.0 | 0.0 | 19 |
| 0.3 | full | 1 | 0 | False | 16770 | 0 | 0.0 | 0.0 | 0.0 | 23 |
| 0.5 | empty | 5 | 0 | False | 16770 | 0 | 0.0 | 0.0 | 0.0 | 28 |
| 0.5 | average | 6 | 0 | False | 16770 | 0 | 0.0 | 0.0 | 0.0 | 30 |
| 0.5 | full | 6 | 0 | False | 16770 | 0 | 0.0 | 0.0 | 0.0 | 31 |
| 0.7 | empty | 13 | 0 | False | 16770 | 125 | 29.89 | 2.8827 | 0.003 | 40 |
| 0.7 | average | 14 | 0 | False | 16770 | 125 | 31.601 | 2.7429 | 0.00274 | 38 |
| 0.7 | full | 14 | 0 | False | 16770 | 125 | 33.313 | 2.6304 | 0.00254 | 37 |

## E5 Delivery load

Run id 2026-10-R1/E5. Load profiles: `constant_7pax` (525 kg throughout), `decreasing_equal` (1,050 kg at the depot, 1,050/n unloaded at each of the line's n stops, empty on the last leg), `decreasing_demand` (the same 1,050 kg unloaded in proportion to each stop's `demand_weight`). Every leg is priced at the mass on board (rolling resistance, grade, regeneration cap and the stop's braking/pull-away energy all see it); energies exclude the stop events, which are listed separately (`stop_energy_wh`). `as_found` = the main-run network's own sequence and road paths; `load_aware_dem` / `load_aware_planar` = the visiting order re-optimised per line by 2-opt + or-opt with the full load-aware tour price on that surface (paths = least-energy paths on that surface at the average load), membership fixed; every result is then evaluated on the DEM. `reverse` drives the same road pieces backwards with the load profile applied to the reversed order. `heavy_first_index` = Σ m_k·climb_k / Σ m_k over legs with m_k the total mass (kerb + load); `heavy_first_index_load` uses the load alone (a lower value means the heavy legs climb less). Municipal lines: `municipal_as_drawn` on the drawn geometry at 30 km/h with stops in drawing order from the point nearest the depot (HADOSAN and ÜSET carry no stops in the register and get the constant profile only); `municipal_on_graph` routes the same stop order on the graph so that it can be re-sequenced.

| Network | Load profile | lines | E_dem as found (Wh) | E_dem load-aware DEM sequence | E_dem load-aware planar sequence | gain of DEM re-sequencing (Wh, %) | gain of planar re-sequencing | load-dependent VoTI: planar seq − DEM seq (Wh, %) |
|---|---|---|---|---|---|---|---|---|
| distance_opt | constant_7pax | 8 | 33,940 | 33,063 | 34,159 | 877 (2.58 %) | -219 (-0.64 %) | 1,096 (3.21 %) |
| distance_opt | decreasing_equal | 8 | 34,305 | 33,589 | 34,303 | 717 (2.09 %) | 2 (0.01 %) | 715 (2.08 %) |
| distance_opt | decreasing_demand | 8 | 34,300 | 33,568 | 34,324 | 732 (2.14 %) | -24 (-0.07 %) | 756 (2.20 %) |
| planar_opt | constant_7pax | 8 | 34,159 | 33,063 | 34,159 | 1,096 (3.21 %) | 0 (0.00 %) | 1,096 (3.21 %) |
| planar_opt | decreasing_equal | 8 | 34,445 | 33,589 | 34,303 | 856 (2.49 %) | 142 (0.41 %) | 715 (2.08 %) |
| planar_opt | decreasing_demand | 8 | 34,440 | 33,568 | 34,324 | 872 (2.53 %) | 116 (0.34 %) | 756 (2.20 %) |
| dem_opt | constant_7pax | 8 | 33,063 | 33,063 | 34,198 | 0 (0.00 %) | -1,135 (-3.43 %) | 1,135 (3.32 %) |
| dem_opt | decreasing_equal | 8 | 33,625 | 33,600 | 34,303 | 25 (0.07 %) | -678 (-2.02 %) | 703 (2.05 %) |
| dem_opt | decreasing_demand | 8 | 33,603 | 33,578 | 34,324 | 25 (0.07 %) | -721 (-2.15 %) | 746 (2.17 %) |
| municipal_on_graph | constant_7pax | 6 | 35,273 | 29,676 | 30,789 | 5,598 (15.87 %) | 4,484 (12.71 %) | 1,113 (3.62 %) |
| municipal_on_graph | decreasing_equal | 6 | 35,651 | 30,223 | 30,672 | 5,428 (15.22 %) | 4,979 (13.97 %) | 449 (1.46 %) |
| municipal_on_graph | decreasing_demand | 6 | 35,649 | 30,215 | 30,619 | 5,434 (15.24 %) | 5,030 (14.11 %) | 404 (1.32 %) |
| municipal_as_drawn | constant_7pax | 8 | 36,100 |  |  |  |  |  |
| municipal_as_drawn | decreasing_equal | 6 | 31,254 |  |  |  |  |  |
| municipal_as_drawn | decreasing_demand | 6 | 31,256 |  |  |  |  |  |

**Sentences for the manuscript.** Network total, dem-optimised lines, decreasing load: as found 33,625 Wh, DEM re-sequenced 33,600 Wh (saving 25 Wh, 0.07 %; with the constant load the same search saves 0 Wh, 0.00 %), planar re-sequenced 34,303 Wh, so the load-dependent VoTI (planar sequence − DEM sequence, both under the decreasing load) is 703 Wh (2.05 %). Network total, distance-optimised lines, decreasing load: as found 34,305 Wh, DEM re-sequenced 33,589 Wh (saving 717 Wh, 2.09 %; with the constant load the same search saves 877 Wh, 2.58 %), planar re-sequenced 34,303 Wh, so the load-dependent VoTI (planar sequence − DEM sequence, both under the decreasing load) is 715 Wh (2.08 %). Network total, planar-optimised lines, decreasing load: as found 34,445 Wh, DEM re-sequenced 33,589 Wh (saving 856 Wh, 2.49 %; with the constant load the same search saves 1,096 Wh, 3.21 %), planar re-sequenced 34,303 Wh, so the load-dependent VoTI (planar sequence − DEM sequence, both under the decreasing load) is 715 Wh (2.08 %). Line 6 (720 m climb, 13 stops): decreasing load as found 12,168 Wh, re-sequenced on the DEM 12,144 Wh (0.2 % saved), re-sequenced on the plane 12,423 Wh; load-weighted mean climb per leg (heavy-first index, load only) 75.84 → 74.66 m (DEM) / 61.71 m (planar). Line 3 (221 m climb, 20 stops): decreasing load as found 4,338 Wh, re-sequenced on the DEM 4,338 Wh (0.0 % saved), re-sequenced on the plane 4,398 Wh; load-weighted mean climb per leg (heavy-first index, load only) 11.71 → 11.71 m (DEM) / 11.78 m (planar). Heavy-first pattern (DEM network, decreasing load): the load-weighted climb per leg falls from 18.1 m (as found) to 18.0 m after DEM re-sequencing and 17.0 m after planar re-sequencing on average over the 8 lines; it falls on 2 lines and rises on 0. Reversing the DEM-re-sequenced lines with the load profile reversed costs 33,764 Wh against 33,600 Wh forward (+0.5 %).

Figure: `figR5_delivery_load.png`. Per-line rows (all networks, both directions, three profiles, three sequence sources) in `E5_delivery_load.csv`; the as-found pricing was checked against the stored main-run line energies (`data/processed/experiments/2026-10-R1/E5/E5_sanity_check.csv`).

## E6 Reverse feasibility

Run id 2026-10-R1/E6. One-way rules come from the editable road network's `direction` attribute (revision 1584). For the DEM-optimal lines the forward road pieces are known exactly; a piece violates the rule in reverse when the graph has no edge in the opposite direction. For the municipal drawings the line is resampled every 10 m and each sample matched to the nearest road (within 30 m) with its travel direction; consecutive samples on one road form a traversal. `E_legal_reverse_wh` routes the *reversed stop order* on the directed graph with least-energy legal paths (η_regen 0.5, 7 passengers); for the municipal lines the forward energy in that column pair is routed on the graph the same way (drawn stop order, graph speeds), so that the two are comparable.

| Network | Line | stops | road pieces | one-way pieces violated in reverse | violated length (m) | share of length (%) | violations as drawn (fwd) | reverse legal as driven | legal reverse routed | E forward (Wh) | E reverse physical | E legal reverse | km fwd | km legal rev | A physical | A legal | legal reverse cheaper |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| dem_opt | Line 1 | 25 | 139 | 7 | 289.1 | 3.39 |  | False | True | 2906.0 | 2946.9 | 2926.3 | 8.52 | 8.787 | 0.0139 | 0.0069 | False |
| dem_opt | Line 2 | 22 | 115 | 21 | 1730.0 | 19.65 |  | False | True | 3336.6 | 3363.1 | 3785.7 | 8.804 | 9.969 | 0.0079 | 0.1186 | False |
| dem_opt | Line 3 | 20 | 157 | 10 | 485.5 | 4.55 |  | False | True | 4255.4 | 4201.8 | 4463.0 | 10.674 | 11.416 | 0.0126 | 0.0465 | False |
| dem_opt | Line 4 | 20 | 126 | 31 | 3862.2 | 38.41 |  | False | True | 4081.8 | 3922.6 | 8202.5 | 10.055 | 19.531 | 0.039 | 0.5024 | False |
| dem_opt | Line 5 | 19 | 161 | 32 | 2324.6 | 18.01 |  | False | True | 4578.3 | 4837.5 | 6050.8 | 12.906 | 15.873 | 0.0536 | 0.2434 | False |
| dem_opt | Line 6 | 13 | 167 | 18 | 1489.2 | 5.7 |  | False | True | 11506.1 | 12289.8 | 11982.9 | 26.14 | 25.778 | 0.0638 | 0.0398 | False |
| dem_opt | Line 7 | 5 | 73 | 22 | 1329.6 | 31.72 |  | False | True | 1531.7 | 2018.4 | 2171.7 | 4.192 | 5.403 | 0.2412 | 0.2947 | False |
| dem_opt | Line 8 | 5 | 42 | 37 | 2366.0 | 94.28 |  | False | True | 867.5 | 847.2 | 1503.9 | 2.51 | 4.42 | 0.0234 | 0.4231 | False |
| municipal_as_drawn | AKSALUR | 12 | 161 | 20 | 1431.0 | 5.09 | 1 | False | True | 11498.8 |  | 11967.5 | 26.218 | 25.859 |  | 0.0392 | False |
| municipal_as_drawn | BAHÇELİEVLER | 26 | 170 | 49 | 3578.1 | 35.59 | 5 | False | True | 4269.0 |  | 4220.2 | 12.939 | 12.638 |  | 0.0114 | True |
| municipal_as_drawn | EVKA | 19 | 176 | 77 | 6636.5 | 58.36 | 6 | False | True | 5648.7 |  | 6439.0 | 14.589 | 16.506 |  | 0.1227 | False |
| municipal_as_drawn | FATİH | 20 | 169 | 43 | 3327.7 | 32.41 | 4 | False | True | 3835.1 |  | 3682.6 | 10.556 | 10.534 |  | 0.0398 | True |
| municipal_as_drawn | HADOSAN | 0 | 117 | 43 | 3642.2 | 53.53 | 6 | False | False |  |  |  |  |  |  |  |  |
| municipal_as_drawn | KAVAKLIÖNÜ | 15 | 151 | 65 | 6109.8 | 60.98 | 7 | False | True | 3879.3 |  | 5287.3 | 9.324 | 13.429 |  | 0.2663 | False |
| municipal_as_drawn | TOKİ | 22 | 187 | 82 | 6330.1 | 45.49 | 11 | False | True | 6142.4 |  | 6489.8 | 16.84 | 17.089 |  | 0.0535 | False |
| municipal_as_drawn | ÜSET | 0 | 80 | 31 | 2492.6 | 39.39 | 4 | False | False |  |  |  |  |  |  |  |  |

**Sentences for the manuscript.** Of the 16 lines (8 DEM-optimal, 8 municipal), the reverse direction is legal as driven on 0 (0 DEM-optimal, 0 municipal); the others cross 7–82 one-way pieces, 3.4–94.3 % of their length. Line 6: physical asymmetry 0.064 (reverse 12,290 vs forward 11,506 Wh); with a legal reverse routing (18 one-way pieces, 1,489 m, avoided) the asymmetry is 0.040 (11,983 Wh, 25.8 km vs 26.1 km). Line 7: physical asymmetry 0.241 (reverse 2,018 vs forward 1,532 Wh); with a legal reverse routing (22 one-way pieces, 1,330 m, avoided) the asymmetry is 0.295 (2,172 Wh, 5.4 km vs 4.2 km). Across the 14 lines with a stop order, the legal-reverse asymmetry ranges 0.007–0.502 (mean 0.158), and the legal reverse is cheaper than the forward direction on 2 of them.

## E7 Second DEM

Run id 2026-10-R1/E7. Second DEM: aw3d30-v1 (JAXA ALOS World 3D 30 m, v3.2, from the Planetary Computer `alos-dem` collection; NASADEM tiles are also served there; only AW3D30 was used), resampled bilinearly onto the GLO-30 lattice (sha256 4d4f7f38a8e5…). On 15,164 road samples (25 m along every road) AW3D30 − GLO-30 is -0.64 ± 1.11 m (mean ± s.d.; RMSE 1.28 m; |Δ| p50 0.95 m, p95 2.47 m, max 6.52 m); at the 130 stops (129 + depot) -0.86 ± 0.98 m. Grades over a 100 m base along the roads (9,212 pairs) correlate with Pearson r = 0.9786 (s.d. 6.301 % vs 6.471 %, RMSE of the difference 1.331 %).

| Network | Evaluated on | E (Wh) | E planar | planar error % | km | climb (m) | VoTI vs distance (Wh) | % | VoTI vs planar (Wh) | mean asymmetry | friction share % | Jaccard GLO-30/AW3D30 solutions | τ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| distance_opt | aw3d30-v1 | 34299.9 | 21862.5 | -36.26 | 81.838 | 1867.0 |  |  |  | 0.0223 | 3.78 | 1.0 | 0.9931 |
| distance_opt | glo30-v1 | 33940.0 | 21862.5 | -35.58 | 81.838 | 1845.9 |  |  |  | 0.027 | 3.7 | 1.0 | 0.9931 |
| planar_opt | aw3d30-v1 | 34598.8 | 21851.7 | -36.84 | 81.975 | 1906.4 |  |  |  | 0.0229 | 3.72 | 1.0 | 0.9931 |
| planar_opt | glo30-v1 | 34158.9 | 21851.7 | -36.03 | 81.975 | 1871.3 |  |  |  | 0.0249 | 3.62 | 1.0 | 0.9931 |
| dem_opt_glo30 | aw3d30-v1 | 33404.5 | 22375.0 | -33.02 | 83.8 | 1809.0 | 895.4 | 2.61 | 1194.3 | 0.0596 | 2.79 | 1.0 | 0.9931 |
| dem_opt_glo30 | glo30-v1 | 33063.4 | 22375.0 | -32.33 | 83.8 | 1783.9 | 876.6 | 2.58 | 1095.5 | 0.0569 | 2.64 | 1.0 | 0.9931 |
| dem_opt_aw3d30 | aw3d30-v1 | 33355.8 | 22392.8 | -32.87 | 83.793 | 1801.3 | 944.1 | 2.75 | 1243.0 | 0.0574 | 2.8 | 1.0 | 0.9931 |
| dem_opt_aw3d30 | glo30-v1 | 33119.0 | 22392.8 | -32.39 | 83.793 | 1785.9 | 821.0 | 2.42 | 1039.9 | 0.0528 | 2.67 | 1.0 | 0.9931 |

**Sentences for the manuscript.** On AW3D30 the distance network's planar error is -36.26 % (GLO-30: -35.58 %); the network optimised on AW3D30 saves 944 Wh (2.8 %) against the distance network on its own DEM, the GLO-30-optimised network saves 895 Wh (2.6 %) when evaluated on AW3D30, and the AW3D30-optimised network saves 821 Wh on GLO-30 (GLO-30's own: 877 Wh). The two DEM-optimised networks share memberships (Jaccard 1.0) with Kendall τ 0.9931 between paired sequences; mean direction asymmetry 0.0574 on AW3D30 vs 0.0569 on GLO-30.

## Deviations from this plan and open issues

- E1 "seed": OR-Tools' routing search has no seed; the main run's identical repeats and a test with `solver.ReSeed()` showed no variation. The seed was realised as a seeded permutation of the customer node order (`node_order_seed`), which does move the heuristic. The main run (seed 42) corresponds to the unpermuted order and is shown as the reference line.
- E2 count: 6 optimisations per variant rather than 6 + 4, because the distance and planar solutions do not depend on η_regen and the distance solution does not depend on dem_scale (they are re-evaluated, exactly as the main run does). Two warm-start variants (15 s, 300 s) were added because the cold 15 s solve of the unconstrained problem is far from the constrained best-found (longer networks); the cold results are kept in the table as the plan specified them.
- E3 noise: L = 10 is a 10 × 10 box (offsets −5…+4, half a cell asymmetric), not a Gaussian kernel; σ is rescaled by √count so the marginal σ is preserved. Seeds are independent across levels rather than paired.
- E4: `exact_potentials` was added as an option (default False, so the main run stays reproducible); relative differences are floored at 1 Wh in the denominator because some exact pair energies are near zero.
- E5: leg paths are the least-energy paths at the *average* load (7 passengers) and are not re-routed per leg mass; only the pricing is leg-mass-aware. Masses are binned to 25 kg inside the local search and priced exactly afterwards. Three stops of the municipal register (A1, A2 on AKSALUR; B13 on BAHÇELİEVLER) no longer exist by name in planning register revision 135, so `municipal_on_graph` routes 12 of 14 and 26 of 27 stops of those lines; `municipal_as_drawn` uses all drawn stops (the load profile's n differs accordingly). The drawn-line pricing reproduces the main run's reference energies within about 1 % (differences come from the 25 m sampling restarting at each stop cut).
- E6: the municipal drawings are matched to roads by nearest-road sampling; a drawn line off the network by more than 30 m is counted as unmatched (`unmatched_length_m`). HADOSAN and ÜSET have no stops in the register, so no legal reverse routing exists for them.
- E7: NASADEM was not run (AW3D30 only). AW3D30's stated accuracy (5 m RMSE) is entered as `vertical_accuracy_m` with a DOĞRULANACAK note.
- The grid file for AW3D30 lives under `data/processed/elevation/aw3d30-v1/`, which `.gitignore` excludes like the GLO-30 grid; its sha256 is in the E7 manifest and CSV.
- Vehicle parameters remain the placeholders of `vehicle_profiles.json` (sha256 in every manifest).
- Time limits: the optimiser silently capped every request at 120 s (`_optimize_vrp`), so the first pass of the 300 s cells of E1 and E2 actually ran for 120 s (their proposals were set aside, not used). The cap was raised to `MAX_SOLVER_SECONDS = 3600` for scripts (the web layer keeps its own 120 s ceiling; unit-tested) and those cells were re-run at a true 300 s; `solver_seconds` in the CSVs is the measured wall time of each solve. Also observed: the 15 s and 60 s cells return identical networks for every seed (guided local search stalls on this instance well before 15 s), so the seed, not the time limit, is what moves the heuristic here.
- Code changes for R1 (all unit-tested, full suite green in a clean environment; the four closed-tour identities in `tests/test_energy.py::ClosedTourIdentityTests` re-run after every change): `node_order_seed` and `warm_start_stop_ids` in `_optimize_vrp`; `exact_potentials` (Johnson/Bellman-Ford) in `_prepare_graph_energy`; `dem_noise_corr_cells` in `derive_grid`/`EnergyOptions`; `with_passenger_mass` for leg-level load. Defaults leave the main run bit-for-bit reproducible (verified: re-running A-dem-eta0.5-average returns the stored network).
- Open: the E2 unconstrained result depends on the start and time limit of a heuristic; a longer run or an exact method would be needed to call any unconstrained membership change 'terrain-driven' rather than 'search-driven'. Open: E5 keeps leg paths at the average load; a fully load-aware path choice per leg would need one matrix per mass level. Open: E6 counts one-way rules of the editable network only; turn restrictions and signage are not modelled.
