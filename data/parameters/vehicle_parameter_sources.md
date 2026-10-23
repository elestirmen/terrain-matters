# Vehicle parameters: sources and ranges

This note gives the source and the literature range of every value in
[`vehicle_profiles.json`](vehicle_profiles.json). The values form a **representative parameter set for a 14-seat
battery-electric minibus class**. They are not the datasheet of one specific vehicle. The study is simulation only;
no vehicle was measured.

`vehicle_profiles.json` still carries the labels it had while the model was being built: `"status": "DOĞRULANACAK"`
("to be verified"), the profile name `placeholder_minibus` and the Turkish range notes under `sources`. The file is
kept byte-identical on purpose, because every run manifest records its sha256 and its full content. This note
replaces those labels and notes. Where the two disagree, this note applies. The same holds for the word
"placeholders" in the docstring of `urgup_transport/energy.py` and in the generated R1 report.

## Summary

| Parameter | Symbol | Value used | Range found in the literature | Position | Sources |
|---|---|---|---|---|---|
| Empty mass incl. battery | m<sub>0</sub> | 4,200 kg | 2,060 kg (retrofitted electric minibus taxi, tare) to about 3,800 kg (upper bound for the 5.0–5.2 t GVW of the Karsan e-JEST) | **Above the range**: heavy end of the class, see note 1 | [1], [3], [6] |
| Passenger mass | m<sub>p</sub> | 75 kg | 68 kg (EU type approval, M2/M3 Class I/A) to 75 kg (ISO 2416: 68 kg + 7 kg luggage) | Upper end | [5], [6] |
| Payload scenarios | – | 0 / 7 / 14 passengers, total 4,200 / 4,725 / 5,250 kg | GVW of the class 5,000–5,200 kg | Full load 1–5 % above GVW, see note 1 | [1] |
| Rolling resistance | C<sub>rr</sub> | 0.010 | 0.0055 to ≥ 0.0091 for C2 tyres on the test drum; 0.0111 on-road for large vans (tyres 0.0094 + drivetrain 0.0017); 0.010 in a minibus taxi model; 0.006–0.020 for city buses; 0.013 for an e-bus on asphalt | Middle | [4], [7], [8], [9], [10] |
| Drag area | C<sub>d</sub>A | 3.0 m² | 1.38–1.53 m² for large vans (C<sub>d</sub> 0.34, A 4.06 m²); C<sub>d</sub> up to 0.58 for a boxy delivery van; 4.6 m² for a 10.5 m e-bus (C<sub>d</sub> 0.55, A 8.3 m²) | Upper end for the size, see note 2 | [4], [8], [10], [11] |
| Drive efficiency, battery to wheel | η<sub>drive</sub> | 0.88 | 0.80 (minibus taxi model); 0.82 incl. battery (SUMO model of a BMW i3); 0.866 motor and inverter only (city bus); 0.90 traction system (city bus) | Upper end | [4], [12], [13], [14] |
| Regeneration efficiency, wheel to battery | η<sub>regen</sub> | 0.5 default; swept 0, 0.3, 0.5, 0.7 | 0.5 (minibus taxi model); 0.82 incl. battery (SUMO model of a BMW i3) | Default low to middle; the sweep brackets the range | [4], [12] |
| Regeneration power cap | P<sub>regen,max</sub> | 60 kW | 50 kW cap with the 125 kW BMW i3 motor (40 % of peak); the e-JEST uses the i3 motor family, now rated 135 kW, with 80 kW DC charging | Scaled, see note 3 | [1], [2], [12] |
| Auxiliary power | P<sub>aux</sub> | 2 kW | 0.36 kW (car model); 2–7 kW for 12 m buses without HVAC; HVAC 2 kW at 20 °C to 7 kW at −10 °C for a 12 m bus | Low to middle, mild season, see note 4 | [9], [12], [13], [14] |
| Air density | ρ | 1.15 kg/m³ | 1.105 kg/m³ in the standard atmosphere at the run's reference elevation of 1,064 m; 1.04 at 25 °C | Cold season (about −3 °C), see note 5 | [15] |

## Notes

**1. Mass.** This is the one value outside the range we could verify. A retrofitted electric minibus taxi
has a tare mass of 2,060 kg and weighs 3,090 kg fully loaded [3]. The Karsan e-JEST, the Turkish-built model of
the size class, does not publish its kerb mass. It lists a GVW of 5,000 kg (M2, up to 19 passengers) or 5,200 kg
(M3, up to 20) [1]. With the 68 kg passenger mass of the type-approval rules [6], that rating bounds its mass in
running order at about 3.7–3.8 t (our calculation). The 4,200 kg used here is therefore the heavy end of the class.
The full-load case (5,250 kg) lies 1–5 % above the e-JEST GVW and should be read as an upper bound.

The mass sweep shows how much this matters. Over the 25 % mass range of experiment A, the headline metrics move
little (distance-optimal network, DEM surface; source: `experiments_summary.csv` of the main run):

| η<sub>regen</sub> | Mass scenario | Energy (kWh/km) | Planar estimation error | VoTI vs planar-optimal network |
|---|---|---|---|---|
| 0.0 | empty / average / full | 0.467 / 0.513 / 0.559 | −46.3 % / −47.9 % / −49.3 % | 3.79 % / 3.54 % / 3.12 % |
| 0.3 | empty / average / full | 0.414 / 0.454 / 0.495 | −39.4 % / −41.2 % / −42.7 % | 3.39 % / 3.32 % / 3.05 % |
| 0.5 | empty / average / full | 0.378 / 0.415 / 0.452 | −33.7 % / −35.6 % / −37.3 % | 3.15 % / 3.21 % / 3.04 % |
| 0.7 | empty / average / full | 0.343 / 0.375 / 0.409 | −26.9 % / −28.9 % / −30.7 % | 5.00 % / 5.24 % / 5.27 % |

The planar estimation error grows by about 1.4–2.0 percentage points per 525 kg, and the VoTI shows no consistent
trend with mass. A lighter vehicle of about 3.7 t would therefore show a slightly smaller planar error, with the
same direction and mechanism.

**2. Drag area.** The working note in `vehicle_profiles.json` gives C<sub>d</sub> 0.40–0.50 and A 6–7 m². The
frontal area range is not supported: an e-JEST-sized body (2.06 m wide, 2.80 m high [1]) has a gross frontal box of
5.8 m². Scaling the measured ratio of a Toyota Quantum minibus taxi (4 m² frontal area on a 1.9 m × 2.3 m body [4]) gives
about 5.3 m² (our estimate). On that area, C<sub>d</sub>A = 3.0 m² implies C<sub>d</sub> ≈ 0.57. That is above
modern large vans (0.34 [8]) and at the level of boxy vans (0.58 [11]) and buses (0.55 [10]). The value is an upper
end. Aerodynamic drag acts the same way on the planar and the DEM surface, so a high C<sub>d</sub>A adds the same
energy to both. It lowers relative terrain effects slightly and leaves the absolute planar–DEM gap almost unchanged.

**3. Regeneration power cap.** We found no published cap for buses or minibuses. The BMW i3 caps recuperation at
50 kW with a 125 kW motor [12]. The e-JEST runs on the i3 motor and battery family [2], now rated 135 kW [1]. The
same 40 % ratio gives 54 kW. 60 kW is of the same order and stays below the 80 kW DC charging power of the
vehicle [1]. The cap limits recovery on steep descents only.

**4. Auxiliary power.** A car model uses 0.36 kW of auxiliary power [12]. A 12 m city bus draws 2–7 kW without
HVAC [9], plus 2 kW HVAC at 20 °C and 7 kW at −10 °C [13]. A fast-charging bus study uses 2.25 kW of continuous
auxiliaries plus 6.75 kW of air conditioning [14]. A 14-seat minibus lies between the two classes. 2 kW is a
mild-season value; a winter or summer peak would be higher. Auxiliary energy depends on time, not on terrain, so
it enters both surfaces equally.

**5. Air density.** The standard atmosphere [15] gives 1.105 kg/m³ at 1,064 m, the reference elevation recorded in
the run manifest. 1.15 kg/m³ corresponds to about −3 °C, and 25 °C gives 1.04 kg/m³. The value is a cold-season
choice. At 30–50 km/h, replacing 1.15 by 1.105 changes aerodynamic energy by 1.4–3.9 Wh/km. That is under 1 % of
the modelled 0.34–0.56 kWh/km.

## Direction of the choices

Several values sit at the conservative end for the claim that planar routing misleads:

- A high η<sub>drive</sub> lowers the terrain penalty (1/η<sub>drive</sub> − η<sub>regen</sub>)·m·g·Σclimb.
- A high C<sub>d</sub>A and a cold-season ρ add energy that does not depend on terrain, which dilutes relative
  effects.

The heavy mass works the other way, and the table in note 1 shows its size.

## Plausibility of the resulting consumption

On the DEM surface the model gives 0.34–0.56 kWh/km over all cells of experiment A, and 0.38–0.45 kWh/km at
η<sub>regen</sub> = 0.5. The distance-optimal network climbs about 23 m per km (1,846 m over 81.8 km). Reference
values for the class:

- A Karsan Jest electric (88 kWh) used 0.33 kWh/km in tests on Metrobús Route 4 in Mexico City by UNAM's LETEV
  laboratory [16]. This is a vendor report, not peer-reviewed.
- A retrofitted electric minibus taxi used a measured mean of 0.342 kWh/km over 30 trips (about 1,400 km) [3].
  Its hillier half used 10 % more energy per km, and going from about 2.1 t to 3 t added 13 %.
- The e-JEST's nominal figure is about 0.40 kWh/km (105 kWh over 260 km NEDC [1]; our calculation).

On a flat surface the model gives 0.25–0.28 kWh/km. That is below the measured values, which is consistent with
two simplifications of the model: constant speed between stops, and acceleration modelled only at stops. With
terrain, the model lands above the flat-route measurements and around the nominal figure, as expected for a heavy
vehicle on steep streets.

## References

1. Karsan. *e-JEST technical specifications*. https://www.karsan.com/en/electric-bus/e-jest/e-jest-specs (accessed
   23 October 2026).
2. BMW Group (2018, 8 November). *BMW i also advances electromobility in local public transport* [Press release].
   https://press.bmwgroup.com/global/article/detail/T0286718EN/
3. Abraham, C. J., Lacock, S., du Plessis, A. A., & Booysen, M. J. (2025). Empirically validated method to simulate
   electric minibus taxi efficiency using tracking data. *Energies, 18*(2), 446.
   https://doi.org/10.3390/en18020446
4. Abraham, C. J., Rix, A. J., Ndibatya, I., & Booysen, M. J. (2021). Ray of hope for sub-Saharan Africa's
   paratransit: Solar charging of urban electric minibus taxis in South Africa. *Energy for Sustainable Development,
   64*, 118–127. https://doi.org/10.1016/j.esd.2021.08.003
5. ISO 2416:1992. *Passenger cars — Mass distribution*. International Organization for Standardization.
6. Commission Regulation (EU) No 1230/2012 of 12 December 2012 on type-approval requirements for masses and
   dimensions of motor vehicles and their trailers, Annex I.
7. Regulation (EU) 2020/740 of the European Parliament and of the Council of 25 May 2020 on the labelling of tyres
   with respect to fuel efficiency and other parameters, Annex I.
8. Kühlwein, J. (2016). *Driving resistances of light-duty vehicles in Europe: Present situation, trends, and
   scenarios for 2025* [White paper]. International Council on Clean Transportation.
9. Vepsäläinen, J., Otto, K., Lajunen, A., & Tammi, K. (2019). Computationally efficient model for energy demand
   prediction of electric city bus in varying operating conditions. *Energy, 169*, 433–443.
   https://doi.org/10.1016/j.energy.2018.12.064
10. Arı, O., & Kızılarslan, Y. (2026). Effects of frontal area and road characteristics on the energy efficiency of
    electric buses: A simulation study. *Bitlis Eren Üniversitesi Fen Bilimleri Dergisi, 15*(3), 1263–1281.
    https://doi.org/10.17798/bitlisfen.1886365
11. Kurtz, D. W. (1979). *Aerodynamic characteristics of sixteen electric, hybrid, and subcompact vehicles* (JPL
    Publication 79-59). Jet Propulsion Laboratory.
12. Koch, L., Buse, D. S., Wegener, M., Schoenberg, S., Badalian, K., Dressler, F., & Andert, J. (2021). Accurate
    physics-based modeling of electric vehicle energy consumption in the SUMO traffic microsimulator. In *2021 IEEE
    International Intelligent Transportation Systems Conference (ITSC)* (pp. 1650–1657).
    https://doi.org/10.1109/ITSC48978.2021.9564463
13. Kivekäs, K., Lajunen, A., Baldi, F., Vepsäläinen, J., & Tammi, K. (2019). Reducing the energy consumption of
    electric buses with design choices and predictive driving. *IEEE Transactions on Vehicular Technology, 68*(12),
    11409–11419. https://doi.org/10.1109/TVT.2019.2936772
14. Rogge, M., Wollny, S., & Sauer, D. U. (2015). Fast charging battery buses for the electrification of urban
    public transport: A feasibility study focusing on charging infrastructure and energy storage requirements.
    *Energies, 8*(5), 4587–4606. https://doi.org/10.3390/en8054587
15. ISO 2533:1975. *Standard Atmosphere*. International Organization for Standardization.
16. Dewesoft. *Evaluating energy consumption of electric minibus* [Blog post on tests by UNAM's Vehicle Technology
    Assessment Laboratory, LETEV]. https://dewesoft.com/blog/evaluating-energy-consumption-of-electric-minibus
    (accessed 23 October 2026).
