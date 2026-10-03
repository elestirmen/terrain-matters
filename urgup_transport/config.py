"""Shared configuration for pipeline scripts and the Flask data service."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DEMAND_INPUT = "data/raw/spreadsheets/MAHALLELER ASIL DURAKLAR.xlsx"
DEFAULT_SERVICE_SHEET = "data/raw/spreadsheets/DURAK SERVİS SAATLERİ.xlsx"
DEFAULT_SERVICE_DECLARATION = "data/processed/service/sefer-duzeni.json"
DEFAULT_SERVICE_LINES_CSV = "data/processed/tabular/service_lines.csv"
DEFAULT_SERVICE_ISSUES = "data/processed/validation/service_declaration_issues.json"
DEFAULT_POPULATION_DIR = "data/raw/population"
DEFAULT_POPULATION_CSV = "data/processed/tabular/mahalle_population.csv"
DEFAULT_MAHALLE_BOUNDARY_KML = "data/raw/kml/mahallesınırları.kml"
DEFAULT_STOPS_KML = "data/raw/kml/DURAKLAR.kml"
DEFAULT_BASELINE_ROUTES_KML = "data/raw/kml/GÜZERGAHLAR.kml"
DEFAULT_BASELINE_NETWORK = "data/processed/baseline/baseline_network.json"
DEFAULT_ROUTE_COORDINATES_XLSX = "data/raw/spreadsheets/Güzergah koordinatları.xlsx"
DEFAULT_STOPPING_PLACES = "data/processed/baseline/stopping_places.geojson"
DEFAULT_ONEWAY_KML = "data/raw/kml/tekyön.kml"
DEFAULT_CLOSED_ROAD_KML = "data/raw/kml/kapalıyol.kml"
DEFAULT_MAKS_PACKAGE = "veriler/yol_agi/ulasim.mpk"
DEFAULT_DEMAND_OUTPUT = "data/processed/tabular/demand_cleaned.csv"
DEFAULT_DEMAND_ISSUES = "data/processed/validation/demand_issues.csv"
DEFAULT_DEMAND_SUMMARY = "data/processed/validation/demand_summary.json"
DEFAULT_SLOT_SUMMARY = "data/processed/tabular/demand_slot_summary.csv"
DEFAULT_MAHALLE_SHP = "data/raw/shapefile/mahalle.shp"
DEFAULT_FINAL_KML = "outputs/final/maps/final_routes.kml"
DEFAULT_OUTPUT_GPKG = "data/processed/vector/urgup_transport.gpkg"
DEFAULT_VECTOR_SUMMARY = "data/processed/metadata/vector_build_summary.json"
DEFAULT_RAW_SHP_DIR = "data/raw/shapefile"
DEFAULT_QA_JSON = "qa/qa_report.json"
DEFAULT_QA_MD = "qa/qa_report.md"
DEFAULT_PIPELINE_PARAMS_JSON = "data/processed/metadata/pipeline_parameters.json"
DEFAULT_EDITABLE_NETWORK = "data/editable/network.json"
DEFAULT_ELEVATION_DIR = "data/processed/elevation/glo30-v1"
DEFAULT_VEHICLE_PROFILES = "data/parameters/vehicle_profiles.json"
DEFAULT_PUBLISHED_REVISION = "data/processed/metadata/published_revision.json"
DEFAULT_MAX_MERKEZ_DISTANCE_M = 250.0

# Network-design validation policy. Stops farther from a drivable road are not
# silently attached to the mathematically nearest graph node.
DEFAULT_MAX_STOP_SNAP_DISTANCE_M = 120.0
DEFAULT_FALLBACK_ROAD_SPEED_KMH = 30.0
DEFAULT_LAYOVER_MINUTES = 0.0

# Provisional service-planning policy. Category values are planning proxies,
# not observed passenger counts and never participate in network design.
SERVICE_DEMAND_CATEGORY_MAPPING = {
    "0": 0.0,
    "AZ": 10.0,
    "ORTA": 25.0,
    "YOGUN": 45.0,
    "YOĞUN": 45.0,
}
DEFAULT_SERVICE_VEHICLE_CAPACITY = 14.0
DEFAULT_TARGET_LOAD_FACTOR = 0.85
DEFAULT_MIN_SERVICE_FREQUENCY_PER_HOUR = 0.0
DEFAULT_MAXIMUM_HEADWAY_MINUTES = 0.0
DEFAULT_MINIMUM_FEASIBLE_HEADWAY_MINUTES = 0.0

GPKG_PATH = PROJECT_ROOT / DEFAULT_OUTPUT_GPKG
DEMAND_CSV = PROJECT_ROOT / DEFAULT_DEMAND_OUTPUT
DEMAND_SUMMARY_CSV = PROJECT_ROOT / DEFAULT_SLOT_SUMMARY
QA_JSON = PROJECT_ROOT / DEFAULT_QA_JSON
PIPELINE_PARAMS_JSON = PROJECT_ROOT / DEFAULT_PIPELINE_PARAMS_JSON
EDITABLE_NETWORK = PROJECT_ROOT / DEFAULT_EDITABLE_NETWORK
BASELINE_NETWORK_JSON = PROJECT_ROOT / DEFAULT_BASELINE_NETWORK
STOPPING_PLACES_GEOJSON = PROJECT_ROOT / DEFAULT_STOPPING_PLACES
PUBLISHED_REVISION_JSON = PROJECT_ROOT / DEFAULT_PUBLISHED_REVISION

GPKG_LAYERS = ("mahalle", "route_lines", "route_stops")

TIME_SLOTS = {
    3: "07:30-10:00",
    5: "10:00-15:30",
    7: "15:30-18:00",
    9: "18:00-23:15",
}

PIPELINE_PATH_ARGS = {
    "bootstrap_root": "--bootstrap-root",
    "demand_input": "--demand-input",
    "demand_output": "--demand-output",
    "demand_issues": "--demand-issues",
    "demand_summary": "--demand-summary",
    "slot_summary": "--slot-summary",
    "mahalle_shp": "--mahalle-shp",
    "final_kml": "--final-kml",
    "editable_source": "--editable-source",
    "output_gpkg": "--output-gpkg",
    "vector_summary": "--vector-summary",
    "raw_shp_dir": "--raw-shp-dir",
    "qa_out_json": "--qa-out-json",
    "qa_out_md": "--qa-out-md",
}

PIPELINE_PATH_DEFAULTS = {
    "bootstrap_root": ".",
    "demand_input": DEFAULT_DEMAND_INPUT,
    "demand_output": DEFAULT_DEMAND_OUTPUT,
    "demand_issues": DEFAULT_DEMAND_ISSUES,
    "demand_summary": DEFAULT_DEMAND_SUMMARY,
    "slot_summary": DEFAULT_SLOT_SUMMARY,
    "mahalle_shp": DEFAULT_MAHALLE_SHP,
    "final_kml": DEFAULT_FINAL_KML,
    "editable_source": DEFAULT_EDITABLE_NETWORK,
    "output_gpkg": DEFAULT_OUTPUT_GPKG,
    "vector_summary": DEFAULT_VECTOR_SUMMARY,
    "raw_shp_dir": DEFAULT_RAW_SHP_DIR,
    "qa_out_json": DEFAULT_QA_JSON,
    "qa_out_md": DEFAULT_QA_MD,
}

PIPELINE_PATH_SUFFIXES = {
    "demand_input": {".xlsx"},
    "demand_output": {".csv"},
    "demand_issues": {".csv"},
    "demand_summary": {".json"},
    "slot_summary": {".csv"},
    "mahalle_shp": {".shp"},
    "final_kml": {".kml"},
    "editable_source": {".json"},
    "output_gpkg": {".gpkg"},
    "vector_summary": {".json"},
    "qa_out_json": {".json"},
    "qa_out_md": {".md"},
}

PIPELINE_DEFAULTS = {
    **PIPELINE_PATH_DEFAULTS,
    "max_merkez_distance_m": DEFAULT_MAX_MERKEZ_DISTANCE_M,
    "strict_qa": False,
}

PIPELINE_UI_DEFAULTS = {
    "route_stop_match_max_meters": 220.0,
    "optimizer_cluster_count": 5,
    "optimizer_features": "Koordinat + nufus",
    "optimizer_scaler": "Min-Max",
    "optimizer_validation_metric": "Silhouette score",
    "optimizer_population_size": 5000,
    "optimizer_generations": 1000,
    "optimizer_mutation_rate": 0.01,
    "optimizer_tournament_k": 5,
    "optimizer_elitism": "best_individual",
    "optimizer_mutation_type": "2 durak swap",
    "optimizer_exact_tsp_use": "Kucuk gruplar",
    "optimizer_exact_tsp_limit": 8,
    "optimizer_exact_tsp_method": "Tum permutasyonlar",
    "optimizer_fixed_stops": "Merkez/Gidis/Donus",
    "optimizer_sa_enabled": "report_only",
    "optimizer_sa_initial_temp": "",
    "optimizer_sa_cooling_rate": "",
    "optimizer_sa_stop_criterion": "Iterasyon / sicaklik esigi",
    "optimizer_distance_source": "OSMnx / NetworkX yol agi",
    "optimizer_population_factor": 0.5,
    "optimizer_avg_speed_kmh": 35,
    "optimizer_dwell_time_seconds": 30,
    "optimizer_mahalle_stop_method": "Shapely covers",
    "optimizer_mahalle_route_method": "Shapely intersects",
}

PIPELINE_NUMERIC_UI_PARAMS = {
    "route_stop_match_max_meters",
    "optimizer_cluster_count",
    "optimizer_population_size",
    "optimizer_generations",
    "optimizer_mutation_rate",
    "optimizer_tournament_k",
    "optimizer_exact_tsp_limit",
    "optimizer_population_factor",
    "optimizer_avg_speed_kmh",
    "optimizer_dwell_time_seconds",
}

PIPELINE_OPTIONAL_NUMERIC_UI_PARAMS = {
    "optimizer_sa_initial_temp",
    "optimizer_sa_cooling_rate",
}
