"""Revisioned editable network storage used by the editor and pipeline."""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.geometry import shape

from .config import GPKG_PATH, PROJECT_ROOT
from .routing_contract import (
    ROAD_SPEED_PROFILE_VERSION,
    ROUTING_MODEL_VERSION,
)

DEFAULT_EDITABLE_NETWORK = PROJECT_ROOT / "data/editable/network.json"
DEFAULT_PUBLISHED_REVISION = PROJECT_ROOT / "data/processed/metadata/published_revision.json"

LAYER_CONFIG: dict[str, dict[str, Any]] = {
    "routes": {"gpkg": "route_lines", "geometry": {"LineString", "MultiLineString"}},
    "stops": {"gpkg": "route_stops", "geometry": {"Point"}},
    "mahalle": {"gpkg": "mahalle", "geometry": {"Polygon", "MultiPolygon"}},
}

_LOCK = threading.RLock()


class DraftValidationError(ValueError):
    """Raised when an editable feature is malformed."""


class RevisionConflictError(RuntimeError):
    """Raised when a client attempts to overwrite a newer draft."""


class FeatureRevisionConflictError(RevisionConflictError):
    """Raised when the record itself moved, rather than the draft around it.

    Kept distinct because the two need different answers: a draft conflict means
    reload everything, a record conflict means reload one record — and the
    editor can say who changed it.
    """

    def __init__(self, message: str, *, layer: str | None = None, feature_id: str | None = None):
        super().__init__(message)
        self.layer = layer
        self.feature_id = feature_id


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        return _json_value(value.item())
    return str(value)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def exclusive_store_lock(path: Path):
    """Serialize draft mutations across threads and server processes."""
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _empty_draft() -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": 2,
        "revision": 0,
        "created_at_utc": now,
        "updated_at_utc": now,
        "layers": {key: {"type": "FeatureCollection", "features": []} for key in LAYER_CONFIG},
    }


def _feature_id(layer: str, index: int, feature: dict[str, Any]) -> str:
    props = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}
    fingerprint = json.dumps([layer, index, props, geometry], ensure_ascii=False, sort_keys=True, default=str)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint))


def seed_from_geopackage(gpkg_path: Path = GPKG_PATH) -> dict[str, Any]:
    """Build an editable draft from the currently published GeoPackage."""
    draft = _empty_draft()
    if not gpkg_path.exists():
        return draft

    for editable_layer, config in LAYER_CONFIG.items():
        gdf = gpd.read_file(gpkg_path, layer=config["gpkg"])
        if gdf.crs is None:
            raise DraftValidationError(f"{config['gpkg']} katmanının CRS bilgisi yok.")
        if gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        collection = json.loads(gdf.to_json(na="drop"))
        for index, feature in enumerate(collection.get("features", [])):
            existing_id = (feature.get("properties") or {}).pop("feature_id", None)
            feature["id"] = str(existing_id or feature.get("id") or _feature_id(editable_layer, index, feature))
            feature["properties"] = {
                str(key): _json_value(value) for key, value in (feature.get("properties") or {}).items()
            }
        draft["layers"][editable_layer] = collection

    draft["source"] = {"kind": "published_geopackage", "path": str(gpkg_path)}
    return draft


def validate_feature(layer: str, feature: dict[str, Any]) -> dict[str, Any]:
    if layer not in LAYER_CONFIG:
        raise DraftValidationError(f"Bilinmeyen katman: {layer}")
    if not isinstance(feature, dict):
        raise DraftValidationError("Feature nesne olmalı.")

    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        raise DraftValidationError("Geometri zorunludur.")
    geometry_type = geometry.get("type")
    if geometry_type not in LAYER_CONFIG[layer]["geometry"]:
        allowed = ", ".join(sorted(LAYER_CONFIG[layer]["geometry"]))
        raise DraftValidationError(f"{layer} geometrisi {allowed} olmalı.")
    try:
        geom = shape(geometry)
    except Exception as exc:
        raise DraftValidationError(f"Geometri okunamadı: {exc}") from exc
    if geom.is_empty:
        raise DraftValidationError("Geometri boş olamaz.")
    if not geom.is_valid:
        raise DraftValidationError("Geometri geçerli değil.")
    minx, miny, maxx, maxy = geom.bounds
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90):
        raise DraftValidationError("Koordinatlar EPSG:4326 sınırları dışında.")

    props = feature.get("properties") or {}
    if not isinstance(props, dict):
        raise DraftValidationError("properties nesne olmalı.")
    clean_props = {str(key): _json_value(value) for key, value in props.items()}
    feature_id = str(feature.get("id") or uuid.uuid4())
    name = str(clean_props.get("name") or "").strip()
    if not name:
        raise DraftValidationError("Ad alanı zorunludur.")
    clean_props["name"] = name
    if layer in {"routes", "stops"}:
        clean_props["folder_path"] = str(clean_props.get("folder_path") or name).strip()
    if layer == "routes":
        clean_props["route_id"] = str(clean_props.get("route_id") or feature_id)
    if layer == "stops":
        clean_props["stop_id"] = str(clean_props.get("stop_id") or feature_id)
        if clean_props.get("location_role") not in (None, ""):
            location_role = str(clean_props["location_role"]).strip().lower()
            if location_role not in {"stop", "depot", "terminal", "hub"}:
                raise DraftValidationError(
                    "Konum rolü stop, depot, terminal veya hub olmalı."
                )
            clean_props["location_role"] = location_role
        if clean_props.get("route_id") not in (None, ""):
            clean_props["route_id"] = str(clean_props["route_id"])
        if clean_props.get("sequence") not in (None, ""):
            try:
                clean_props["sequence"] = max(0, int(float(clean_props["sequence"])))
            except (TypeError, ValueError) as exc:
                raise DraftValidationError("Durak sırası tam sayı olmalı.") from exc
        # A stop the municipality counts as part of a neighbourhood it does not
        # stand in. The boundary decides walking distance and always will; this
        # decides which line serves it, which is an operating decision and was
        # not expressible at all — a stop outside every boundary fell outside
        # the single-route rule with it.
        if clean_props.get("mahalle_override") not in (None, ""):
            override = str(clean_props["mahalle_override"]).strip()
            if len(override) > 160:
                raise DraftValidationError("Mahalle ataması en çok 160 karakter olabilir.")
            clean_props["mahalle_override"] = override
        else:
            clean_props.pop("mahalle_override", None)
        if clean_props.get("demand_weight") not in (None, ""):
            try:
                demand_weight = float(clean_props["demand_weight"])
            except (TypeError, ValueError) as exc:
                raise DraftValidationError("Talep ağırlığı sayı olmalı.") from exc
            if not math.isfinite(demand_weight) or demand_weight < 0:
                raise DraftValidationError("Talep ağırlığı negatif olamaz.")
            clean_props["demand_weight"] = demand_weight
    if layer == "mahalle" and clean_props.get("population") not in (None, ""):
        try:
            population = int(float(clean_props["population"]))
        except (TypeError, ValueError) as exc:
            raise DraftValidationError("Nüfus tam sayı olmalı.") from exc
        if population < 0:
            raise DraftValidationError("Nüfus negatif olamaz.")
        clean_props["population"] = population

    return {
        "type": "Feature",
        "id": feature_id,
        "properties": clean_props,
        "geometry": geometry,
    }


def validate_draft(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("layers"), dict):
        raise DraftValidationError("Taslak katmanları bulunamadı.")
    clean = deepcopy(payload)
    clean["schema_version"] = 2
    for layer in LAYER_CONFIG:
        collection = payload["layers"].get(layer)
        if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
            raise DraftValidationError(f"{layer} FeatureCollection olmalı.")
        features = collection.get("features")
        if not isinstance(features, list):
            raise DraftValidationError(f"{layer}.features liste olmalı.")
        clean["layers"][layer] = {
            "type": "FeatureCollection",
            "features": [validate_feature(layer, feature) for feature in features],
        }
    return clean


def check_optimization_model(proposal: dict[str, Any], road_network_path: Path | None) -> bool:
    """Validate the proposal's model versions. Returns whether it is road-bound."""
    proposal_road_revision = proposal.get("road_network_revision")
    snapshot = proposal.get("routing_snapshot") or {}
    snapshot_kind = str(snapshot.get("kind") or proposal.get("distance_source") or "")
    road_bound = proposal_road_revision is not None or snapshot_kind in {
        "editable_road_network",
        "cached_osm_drive_graph",
    }
    if proposal.get("routing_model_version") != ROUTING_MODEL_VERSION:
        raise RevisionConflictError(
            "Optimizasyon önerisi eski yönlendirme modeline ait. Yeniden çalıştırın."
        )
    if road_bound and road_network_path is None:
        raise DraftValidationError(
            "Yol tabanlı optimizasyon önerisi yol ağı snapshot'ı olmadan uygulanamaz."
        )
    return road_bound


@contextmanager
def optimization_road_snapshot(
    proposal: dict[str, Any],
    *,
    expected_road_revision: int | None,
    road_network_path: Path | None,
):
    """Hold the road network steady and check every road-side precondition.

    Shared by both draft stores so the rules cannot drift apart. The freezing is
    delegated to whichever road store is active: a file lock for the GeoJSON one,
    a session-level advisory lock for the database one. Lock order is always road
    → draft, and road edits never take the draft lock, so there is no cycle.

    In file mode `road_network_path` still names the file to freeze, which is
    what the pipeline and the planning fixtures rely on. In database mode the
    network has one home and the path only signals that the proposal is
    road-bound.
    """
    check_optimization_model(proposal, road_network_path)
    proposal_road_revision = proposal.get("road_network_revision")

    if road_network_path is None:
        yield None
        return

    from .stores import road_store_for

    with road_store_for(road_network_path).frozen() as current:
        if expected_road_revision is not None and int(expected_road_revision) != current["revision"]:
            raise RevisionConflictError(
                "Yol ağı başka bir oturumda değişti. Optimizasyon sonucunu yeniden kontrol edin."
            )
        if proposal_road_revision is not None:
            if int(proposal_road_revision) != current["revision"]:
                raise RevisionConflictError(
                    "Optimizasyon önerisi eski yol ağına ait. Güncel yol ağıyla yeniden çalıştırın."
                )
            if proposal.get("speed_profile_version") != ROAD_SPEED_PROFILE_VERSION:
                raise RevisionConflictError(
                    "Optimizasyon önerisi eski hız profiline ait. Yeniden çalıştırın."
                )
            if proposal.get("road_network_digest") != current["digest"]:
                raise RevisionConflictError(
                    "Yol ağı içeriği öneri üretildikten sonra değişti. Optimizasyonu yeniden çalıştırın."
                )
        yield current


def check_optimization_input_revision(proposal: dict[str, Any], draft_revision: int) -> None:
    input_revision = proposal.get("input_revision")
    if input_revision is not None and int(input_revision) != int(draft_revision):
        raise RevisionConflictError(
            "Optimizasyon önerisi eski bir taslağa ait. Güncel taslakla yeniden çalıştırın."
        )


def optimization_layers(proposal: dict[str, Any]) -> dict[str, Any]:
    layers = proposal.get("layers") or {}
    if not isinstance(layers, dict) or "routes" not in layers or "stops" not in layers:
        raise DraftValidationError("Optimizasyon önerisinde rota veya durak katmanı eksik.")
    return layers


def optimization_record(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": str(proposal.get("proposal_id") or ""),
        "applied_at_utc": utc_now(),
        "parameters": deepcopy(proposal.get("parameters") or {}),
        "metrics": deepcopy(proposal.get("metrics") or {}),
    }


class EditableNetworkStore:
    def __init__(self, path: Path = DEFAULT_EDITABLE_NETWORK, gpkg_path: Path = GPKG_PATH):
        self.path = path
        self.gpkg_path = gpkg_path

    def ensure(self) -> dict[str, Any]:
        with _LOCK:
            with exclusive_store_lock(self.path):
                if not self.path.exists():
                    atomic_write_json(self.path, seed_from_geopackage(self.gpkg_path))
            return self.read()

    def read(self) -> dict[str, Any]:
        with _LOCK:
            if not self.path.exists():
                return self.ensure()
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DraftValidationError(f"Taslak okunamadı: {exc}") from exc
            return validate_draft(payload)

    @staticmethod
    def _check_revision(draft: dict[str, Any], expected_revision: int | None) -> None:
        if expected_revision is not None and int(expected_revision) != int(draft.get("revision", 0)):
            raise RevisionConflictError("Taslak başka bir oturumda değişti. Veriyi yenileyip tekrar deneyin.")

    def save_feature(
        self,
        layer: str,
        feature: dict[str, Any],
        *,
        feature_id: str | None = None,
        expected_revision: int | None = None,
        expected_feature_revision: int | None = None,
        actor: Any = None,
    ) -> dict[str, Any]:
        # actor and expected_feature_revision are accepted so both stores share a
        # single call shape. This store cannot record who changed what, and keeps
        # one revision for the whole draft — the two reasons the database store
        # exists.
        del actor, expected_feature_revision
        with _LOCK, exclusive_store_lock(self.path):
            draft = self.read()
            self._check_revision(draft, expected_revision)
            normalized = validate_feature(layer, {**feature, "id": feature_id or feature.get("id")})
            features = draft["layers"][layer]["features"]
            match = next(
                (index for index, item in enumerate(features) if str(item.get("id")) == normalized["id"]),
                None,
            )
            if feature_id and match is None:
                raise KeyError(f"Kayıt bulunamadı: {feature_id}")
            if match is None:
                features.append(normalized)
            else:
                features[match] = normalized
            draft["revision"] = int(draft.get("revision", 0)) + 1
            draft["updated_at_utc"] = utc_now()
            atomic_write_json(self.path, draft)
            return {"revision": draft["revision"], "feature": normalized}

    def delete_feature(
        self,
        layer: str,
        feature_id: str,
        *,
        expected_revision: int | None = None,
        expected_feature_revision: int | None = None,
        actor: Any = None,
    ) -> dict[str, Any]:
        del actor, expected_feature_revision
        with _LOCK, exclusive_store_lock(self.path):
            if layer not in LAYER_CONFIG:
                raise DraftValidationError(f"Bilinmeyen katman: {layer}")
            draft = self.read()
            self._check_revision(draft, expected_revision)
            features = draft["layers"][layer]["features"]
            remaining = [item for item in features if str(item.get("id")) != feature_id]
            if len(remaining) == len(features):
                raise KeyError(f"Kayıt bulunamadı: {feature_id}")
            draft["layers"][layer]["features"] = remaining
            draft["revision"] = int(draft.get("revision", 0)) + 1
            draft["updated_at_utc"] = utc_now()
            atomic_write_json(self.path, draft)
            return {"revision": draft["revision"], "deleted_id": feature_id}

    def reset_from_published(
        self, *, expected_revision: int | None = None, actor: Any = None
    ) -> dict[str, Any]:
        del actor
        with _LOCK, exclusive_store_lock(self.path):
            current = self.read()
            self._check_revision(current, expected_revision)
            draft = seed_from_geopackage(self.gpkg_path)
            draft["revision"] = int(current.get("revision", 0)) + 1
            draft["updated_at_utc"] = utc_now()
            atomic_write_json(self.path, draft)
            return draft

    def apply_optimization(
        self,
        proposal: dict[str, Any],
        *,
        expected_revision: int | None = None,
        expected_road_revision: int | None = None,
        road_network_path: Path | None = None,
        actor: Any = None,
    ) -> dict[str, Any]:
        """Replace route/stop layers after validating the full planning snapshot."""
        del actor
        with optimization_road_snapshot(
            proposal,
            expected_road_revision=expected_road_revision,
            road_network_path=road_network_path,
        ), _LOCK, exclusive_store_lock(self.path):
            draft = self.read()
            self._check_revision(draft, expected_revision)
            check_optimization_input_revision(proposal, int(draft.get("revision", 0)))
            layers = optimization_layers(proposal)

            candidate = deepcopy(draft)
            candidate["layers"]["routes"] = layers["routes"]
            candidate["layers"]["stops"] = layers["stops"]
            candidate = validate_draft(candidate)
            candidate["revision"] = int(draft.get("revision", 0)) + 1
            candidate["updated_at_utc"] = utc_now()
            candidate["last_optimization"] = optimization_record(proposal)
            atomic_write_json(self.path, candidate)
            return candidate


def draft_to_layers(payload: dict[str, Any]) -> dict[str, gpd.GeoDataFrame]:
    """Convert a draft into the GeoDataFrames the GeoPackage builder writes.

    Split from reading a file so the publishing pipeline can take its input from
    whichever store is authoritative, rather than always from disk.
    """
    payload = validate_draft(payload)
    result: dict[str, gpd.GeoDataFrame] = {}
    for editable_layer, config in LAYER_CONFIG.items():
        features = deepcopy(payload["layers"][editable_layer]["features"])
        for feature in features:
            feature.setdefault("properties", {})["feature_id"] = str(feature.get("id"))
        if features:
            gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
        else:
            gdf = gpd.GeoDataFrame({"name": []}, geometry=[], crs="EPSG:4326")
        result[config["gpkg"]] = gdf
    return result


def load_editable_layers(path: Path) -> dict[str, gpd.GeoDataFrame]:
    return draft_to_layers(json.loads(path.read_text(encoding="utf-8")))


def published_revision(path: Path = DEFAULT_PUBLISHED_REVISION) -> int | None:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("revision"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
