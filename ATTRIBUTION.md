# Data sources, licences and attribution

This package has three licences, because its parts come from different places.

| Part | Licence |
|---|---|
| Code: `urgup_transport/`, `webapp/`, `scripts/`, `tests/` | MIT, see [`LICENSE`](LICENSE) |
| Road geometry: `data/editable/road_network.geojson`, and the files that carry geometry taken from it (`routes.geojson` and `proposals/*.json` of every run) | Open Database License 1.0, see [`LICENSE-ODbL`](LICENSE-ODbL) |
| Every other data file, table and figure | Creative Commons Attribution 4.0, see [`LICENSE-DATA`](LICENSE-DATA) |

## Sources

| File | Content | Source |
|---|---|---|
| `data/editable/road_network.geojson` | Directed road graph, revision 1,584, 1,948 road features | Derived from **OpenStreetMap**. Side streets that OpenStreetMap lacks were added from the street centrelines of the **Municipality of Ürgüp** (MAKS). One-way rules come from the municipality's signage records; part of the road classes come from the municipal transport master plan. |
| `data/editable/network.json` | Stop register, neighbourhood boundaries and published lines at draft revision 135 | Municipality of Ürgüp; stops snapped and curated in the authors' planning platform |
| `data/processed/baseline/baseline_network.json` | The eight lines operated today, frozen | Digitised from the route drawings and stop register of the Municipality of Ürgüp |
| `data/parameters/vehicle_profiles.json` | Vehicle parameters of the energy model | Authors. The values are placeholders within the ranges reported for the vehicle class and are flagged `DOĞRULANACAK` ("to be verified") until sources are added |
| `data/processed/experiments/` | Outputs of the main run and of the revision (R1) runs | Authors |
| `docs/paper/figures/`, `docs/paper/revision_R1/` | Figures and tables drawn from the run outputs | Authors |

The municipal data are open data of the Municipality of Ürgüp and are redistributed here with attribution.

## Required attribution

When you reuse the road geometry, keep the notice:

> Road network © OpenStreetMap contributors, available under the Open Database License (ODbL) 1.0;
> additions from the Municipality of Ürgüp.

When you reuse other data, tables or figures, cite the article (see [`CITATION.cff`](CITATION.cff)) and credit the
Municipality of Ürgüp for the stop register and the operated lines.

## Elevation data (not included)

The elevation grids are not redistributed. `scripts/build_elevation.py` downloads and builds the main grid, and
experiment E7 downloads the second one. Each run manifest records the sha256 of the grid it used.

- **Copernicus DEM GLO-30**, used for every run. © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018,
  provided under COPERNICUS by the European Union and ESA; all rights reserved. Used under the Copernicus DEM licence.
- **ALOS World 3D 30 m (AW3D30) v3.2**, used in experiment E7 only. © JAXA. Obtained through the Microsoft Planetary
  Computer.

The maps in `docs/paper/figures/` show both the road network and Copernicus DEM relief. When you reuse one, give
both notices above.
