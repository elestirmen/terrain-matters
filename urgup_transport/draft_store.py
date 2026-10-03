"""PostGIS-backed editable draft, alongside the JSON store it will replace.

Both implementations answer the same calls and return the same shapes, so they
can be checked against one shared contract test. That is what makes swapping the
system of record safe: the database store is proven to behave like the file
store before anything is switched over.

What it adds beyond the file store:

- a revision per feature, so editing two unrelated stops no longer conflicts;
- an append-only ``change_log`` entry for every mutation;
- geometry in PostGIS, so neighborhood queries stop being Python loops.

The draft-wide revision is kept as well. The current API and editor exchange it
on every write, so it stays authoritative until the clients learn to send a
per-feature revision instead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db
from .config import GPKG_PATH
from .editor_store import (
    LAYER_CONFIG,
    DraftValidationError,
    FeatureRevisionConflictError,
    RevisionConflictError,
    check_optimization_input_revision,
    optimization_layers,
    optimization_record,
    optimization_road_snapshot,
    seed_from_geopackage,
    utc_now,
    validate_feature,
)

log = logging.getLogger(__name__)

REVISION_KEY = "revision"
LAST_OPTIMIZATION_KEY = "last_optimization"
CREATED_AT_KEY = "created_at_utc"

# ST_AsGeoJSON truncates to 9 decimals by default — about 0.1 mm, but enough to
# make a round-trip differ from its source and so make any comparison against the
# original vacuous. PostGIS stores double precision, so reading at full precision
# is lossless and lets the migration actually prove it copied the data.
GEOJSON_DECIMALS = 15


class AltitudeNotSupportedError(DraftValidationError):
    """Raised when a geometry carries an altitude the 2D planner cannot honour."""


def _coordinate_altitudes(coordinates: Any) -> list[float]:
    """Collect every third ordinate, at any nesting depth."""
    if not isinstance(coordinates, (list, tuple)) or not coordinates:
        return []
    if isinstance(coordinates[0], (int, float)):
        return [float(coordinates[2])] if len(coordinates) > 2 else []
    found: list[float] = []
    for item in coordinates:
        found.extend(_coordinate_altitudes(item))
    return found


def _strip_altitude(coordinates: Any) -> Any:
    if not isinstance(coordinates, (list, tuple)) or not coordinates:
        return coordinates
    if isinstance(coordinates[0], (int, float)):
        return list(coordinates[:2])
    return [_strip_altitude(item) for item in coordinates]


def flatten_to_2d(geometry: dict[str, Any]) -> dict[str, Any]:
    """Drop a zero altitude, refuse a real one.

    KML writes every vertex as ``lng,lat,0``, so the published network arrives
    with an altitude on some features and not others — seven of eight
    neighbourhood polygons carry one, all of them exactly zero. Nothing in the
    planner reads it: distances are haversine or projected, and the road graph
    is flat. Storing it would preserve a serialization artefact as if it were
    data, and would make two identical shapes compare unequal.

    A non-zero altitude would be information, so it is rejected rather than
    silently discarded. Supporting it means a 3D schema and a planner that
    honours grade — a decision, not a migration side effect.
    """
    altitudes = _coordinate_altitudes(geometry.get("coordinates"))
    if not altitudes:
        return geometry
    meaningful = [value for value in altitudes if value != 0.0]
    if meaningful:
        raise AltitudeNotSupportedError(
            f"Geometri sıfırdan farklı yükseklik içeriyor (örn. {meaningful[0]}). "
            "Planlayıcı iki boyutlu çalışır; yükseklik desteği ayrı bir karardır."
        )
    return {**geometry, "coordinates": _strip_altitude(geometry["coordinates"])}


@dataclass(frozen=True)
class Actor:
    """Who performed a change. ``label`` is stored so history survives deletion."""

    label: str
    id: int | None = None
    request_id: str | None = None

    @staticmethod
    def system(label: str = "sistem") -> Actor:
        return Actor(label=label)


def _feature_row_to_geojson(row) -> dict[str, Any]:
    feature_id, properties, geometry, revision = row
    return {
        "type": "Feature",
        "id": feature_id,
        "properties": dict(properties or {}),
        "geometry": geometry,
        "revision": revision,
    }


class DatabaseDraftStore:
    """The editable draft, stored as rows."""

    def __init__(self, gpkg_path: Path = GPKG_PATH):
        self.gpkg_path = gpkg_path

    # ── reads ────────────────────────────────────────────────────────────
    def read(self) -> dict[str, Any]:
        with db.connection() as conn, conn.cursor() as cursor:
            return self._read(cursor)

    def _read(self, cursor) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT id, properties, ST_AsGeoJSON(geometry, %s)::jsonb, revision, layer
            FROM draft_feature
            ORDER BY layer, ordinal, id
            """,
            (GEOJSON_DECIMALS,),
        )
        layers: dict[str, dict[str, Any]] = {
            layer: {"type": "FeatureCollection", "features": []} for layer in LAYER_CONFIG
        }
        for feature_id, properties, geometry, revision, layer in cursor.fetchall():
            if layer not in layers:
                continue
            layers[layer]["features"].append(
                _feature_row_to_geojson((feature_id, properties, geometry, revision))
            )

        meta = self._read_meta(cursor)
        draft = {
            "schema_version": 2,
            "revision": meta.get(REVISION_KEY, 0),
            "created_at_utc": meta.get(CREATED_AT_KEY) or utc_now(),
            "updated_at_utc": meta.get("updated_at_utc") or utc_now(),
            "layers": layers,
        }
        if meta.get(LAST_OPTIMIZATION_KEY):
            draft["last_optimization"] = meta[LAST_OPTIMIZATION_KEY]
        return draft

    @staticmethod
    def _read_meta(cursor) -> dict[str, Any]:
        cursor.execute("SELECT key, value FROM draft_meta")
        return {key: value.get("value") for key, value in cursor.fetchall()}

    def is_empty(self) -> bool:
        with db.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM draft_feature)")
            return not cursor.fetchone()[0]

    def ensure(self, *, actor: Actor | None = None) -> dict[str, Any]:
        """Return the draft, seeding it from the published GeoPackage when empty."""
        with db.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM draft_meta WHERE key = %s)", (REVISION_KEY,))
            if not cursor.fetchone()[0]:
                self._seed(cursor, actor or Actor.system("ilk kurulum"))
            return self._read(cursor)

    # ── writes ───────────────────────────────────────────────────────────
    def save_feature(
        self,
        layer: str,
        feature: dict[str, Any],
        *,
        feature_id: str | None = None,
        expected_revision: int | None = None,
        expected_feature_revision: int | None = None,
        actor: Actor | None = None,
    ) -> dict[str, Any]:
        who = actor or Actor.system()
        with db.connection() as conn, conn.cursor() as cursor:
            self._check_draft_revision(cursor, expected_revision)
            normalized = validate_feature(layer, {**feature, "id": feature_id or feature.get("id")})
            before = self._feature_by_id(cursor, normalized["id"])

            if feature_id and before is None:
                raise KeyError(f"Kayıt bulunamadı: {feature_id}")
            if before is not None:
                if before["layer"] != layer:
                    raise DraftValidationError(
                        f"{normalized['id']} kaydı {before['layer']} katmanında; katman değiştirilemez."
                    )
                self._check_feature_revision(
                    before["revision"], expected_feature_revision,
                    layer=layer, feature_id=normalized["id"],
                )

            geometry_json = json.dumps(flatten_to_2d(normalized["geometry"]))
            properties_json = json.dumps(normalized.get("properties") or {}, ensure_ascii=False)
            if before is None:
                cursor.execute("SELECT coalesce(max(ordinal), 0) + 1 FROM draft_feature WHERE layer = %s", (layer,))
                ordinal = cursor.fetchone()[0]
                cursor.execute(
                    """
                    INSERT INTO draft_feature (id, layer, ordinal, properties, geometry, updated_by)
                    VALUES (%s, %s, %s, %s::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s)
                    RETURNING revision
                    """,
                    (normalized["id"], layer, ordinal, properties_json, geometry_json, who.id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE draft_feature
                       SET properties = %s::jsonb,
                           geometry   = ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326),
                           revision   = revision + 1,
                           updated_at = now(),
                           updated_by = %s
                     WHERE id = %s
                    RETURNING revision
                    """,
                    (properties_json, geometry_json, who.id, normalized["id"]),
                )
            feature_revision = cursor.fetchone()[0]

            self._log(
                cursor,
                who,
                action="update" if before else "create",
                layer=layer,
                feature_id=normalized["id"],
                before_state=before["feature"] if before else None,
                after_state=normalized,
            )
            revision = self._bump_draft_revision(cursor)

        result = dict(normalized)
        result["revision"] = feature_revision
        return {"revision": revision, "feature": result}

    def delete_feature(
        self,
        layer: str,
        feature_id: str,
        *,
        expected_revision: int | None = None,
        expected_feature_revision: int | None = None,
        actor: Actor | None = None,
    ) -> dict[str, Any]:
        who = actor or Actor.system()
        if layer not in LAYER_CONFIG:
            raise DraftValidationError(f"Bilinmeyen katman: {layer}")
        with db.connection() as conn, conn.cursor() as cursor:
            self._check_draft_revision(cursor, expected_revision)
            before = self._feature_by_id(cursor, feature_id)
            if before is None or before["layer"] != layer:
                raise KeyError(f"Kayıt bulunamadı: {feature_id}")
            self._check_feature_revision(
                before["revision"], expected_feature_revision, layer=layer, feature_id=feature_id
            )

            cursor.execute("DELETE FROM draft_feature WHERE id = %s", (feature_id,))
            self._log(
                cursor,
                who,
                action="delete",
                layer=layer,
                feature_id=feature_id,
                before_state=before["feature"],
                after_state=None,
            )
            revision = self._bump_draft_revision(cursor)
        return {"revision": revision, "deleted_id": feature_id}

    def reset_from_published(
        self, *, expected_revision: int | None = None, actor: Actor | None = None
    ) -> dict[str, Any]:
        """Discard the draft and reseed it from the published GeoPackage."""
        who = actor or Actor.system()
        with db.connection() as conn, conn.cursor() as cursor:
            self._check_draft_revision(cursor, expected_revision)
            cursor.execute("SELECT count(*) FROM draft_feature")
            discarded = cursor.fetchone()[0]
            cursor.execute("DELETE FROM draft_feature")
            cursor.execute("DELETE FROM draft_meta WHERE key = %s", (LAST_OPTIMIZATION_KEY,))
            self._seed(cursor, who, keep_revision=True)
            self._log(
                cursor,
                who,
                action="reset",
                layer=None,
                feature_id=None,
                before_state={"feature_count": discarded},
                after_state={"source": "published_geopackage"},
            )
            self._bump_draft_revision(cursor)
            return self._read(cursor)

    def apply_optimization(
        self,
        proposal: dict[str, Any],
        *,
        expected_revision: int | None = None,
        expected_road_revision: int | None = None,
        road_network_path: Path | None = None,
        actor: Actor | None = None,
    ) -> dict[str, Any]:
        """Replace the route and stop layers after validating the planning snapshot.

        The validation is the same code the file store runs, so the two cannot
        drift apart. Only the write differs: rows in one transaction, with the
        change attributed to whoever approved the proposal.
        """
        who = actor or Actor.system()
        with optimization_road_snapshot(
            proposal,
            expected_road_revision=expected_road_revision,
            road_network_path=road_network_path,
        ), db.connection() as conn, conn.cursor() as cursor:
            self._check_draft_revision(cursor, expected_revision)
            current = self._read_meta(cursor).get(REVISION_KEY, 0)
            check_optimization_input_revision(proposal, int(current))
            layers = optimization_layers(proposal)

            replaced = {}
            for layer in ("routes", "stops"):
                cursor.execute("SELECT count(*) FROM draft_feature WHERE layer = %s", (layer,))
                replaced[layer] = cursor.fetchone()[0]
                cursor.execute("DELETE FROM draft_feature WHERE layer = %s", (layer,))
                for ordinal, feature in enumerate(
                    (layers[layer] or {}).get("features", []), start=1
                ):
                    normalized = validate_feature(layer, feature)
                    cursor.execute(
                        """
                        INSERT INTO draft_feature (id, layer, ordinal, properties, geometry, updated_by)
                        VALUES (%s, %s, %s, %s::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s)
                        """,
                        (
                            normalized["id"],
                            layer,
                            ordinal,
                            json.dumps(normalized.get("properties") or {}, ensure_ascii=False),
                            json.dumps(flatten_to_2d(normalized["geometry"])),
                            who.id,
                        ),
                    )

            record = optimization_record(proposal)
            self._write_meta(cursor, LAST_OPTIMIZATION_KEY, record)
            self._log(
                cursor,
                who,
                action="apply_proposal",
                layer=None,
                feature_id=None,
                before_state={"replaced": replaced},
                after_state={
                    "proposal_id": record["proposal_id"],
                    "routes": len((layers["routes"] or {}).get("features", [])),
                    "stops": len((layers["stops"] or {}).get("features", [])),
                },
            )
            self._bump_draft_revision(cursor)
            return self._read(cursor)

    def set_last_optimization(self, payload: dict[str, Any], *, actor: Actor | None = None) -> None:
        with db.connection() as conn, conn.cursor() as cursor:
            self._write_meta(cursor, LAST_OPTIMIZATION_KEY, payload)
            self._log(
                cursor,
                actor or Actor.system(),
                action="apply_proposal",
                layer=None,
                feature_id=None,
                before_state=None,
                after_state={"proposal_id": payload.get("proposal_id")},
            )

    # ── history ──────────────────────────────────────────────────────────
    def history(
        self, *, layer: str | None = None, feature_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return the audit trail, newest first, optionally scoped to one feature."""
        limit = max(1, min(int(limit), 500))
        clauses, params = [], []
        if layer:
            clauses.append("layer = %s")
            params.append(layer)
        if feature_id:
            clauses.append("feature_id = %s")
            params.append(feature_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with db.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, occurred_at, actor_label, action, layer, feature_id, request_id
                FROM change_log {where}
                ORDER BY occurred_at DESC, id DESC
                LIMIT %s
                """,
                (*params, limit),
            )
            return [
                {
                    "id": row[0],
                    "occurred_at_utc": row[1].isoformat(),
                    "actor": row[2],
                    "action": row[3],
                    "layer": row[4],
                    "feature_id": row[5],
                    "request_id": row[6],
                }
                for row in cursor.fetchall()
            ]

    # ── internals ────────────────────────────────────────────────────────
    @staticmethod
    def _feature_by_id(cursor, feature_id: str) -> dict[str, Any] | None:
        cursor.execute(
            """
            SELECT id, properties, ST_AsGeoJSON(geometry, %s)::jsonb, revision, layer
            FROM draft_feature WHERE id = %s
            """,
            (GEOJSON_DECIMALS, feature_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "layer": row[4],
            "revision": row[3],
            "feature": _feature_row_to_geojson(row[:4]),
        }

    def _check_draft_revision(self, cursor, expected_revision: int | None) -> None:
        if expected_revision is None:
            return
        current = self._read_meta(cursor).get(REVISION_KEY, 0)
        if int(expected_revision) != int(current):
            raise RevisionConflictError(
                "Taslak başka bir oturumda değişti. Veriyi yenileyip tekrar deneyin."
            )

    @staticmethod
    def _check_feature_revision(
        current: int, expected: int | None, *, layer: str | None = None, feature_id: str | None = None
    ) -> None:
        if expected is not None and int(expected) != int(current):
            raise FeatureRevisionConflictError(
                "Bu kayıt başka bir oturumda değişti. Kaydı yenileyip tekrar deneyin.",
                layer=layer,
                feature_id=feature_id,
            )

    def _bump_draft_revision(self, cursor) -> int:
        cursor.execute(
            """
            INSERT INTO draft_meta (key, value) VALUES (%s, jsonb_build_object('value', 1))
            ON CONFLICT (key) DO UPDATE
               SET value = jsonb_build_object(
                       'value', (draft_meta.value ->> 'value')::bigint + 1
                   ),
                   updated_at = now()
            RETURNING (value ->> 'value')::bigint
            """,
            (REVISION_KEY,),
        )
        revision = cursor.fetchone()[0]
        self._write_meta(cursor, "updated_at_utc", utc_now())
        return revision

    @staticmethod
    def _write_meta(cursor, key: str, value: Any) -> None:
        cursor.execute(
            """
            INSERT INTO draft_meta (key, value) VALUES (%s, jsonb_build_object('value', %s::jsonb))
            ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()
            """,
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def _seed(self, cursor, actor: Actor, *, keep_revision: bool = False) -> None:
        """Populate an empty draft from the published GeoPackage."""
        draft = seed_from_geopackage(self.gpkg_path)
        for layer, collection in draft["layers"].items():
            for ordinal, feature in enumerate(collection.get("features", []), start=1):
                normalized = validate_feature(layer, feature)
                cursor.execute(
                    """
                    INSERT INTO draft_feature (id, layer, ordinal, properties, geometry, updated_by)
                    VALUES (%s, %s, %s, %s::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        normalized["id"],
                        layer,
                        ordinal,
                        json.dumps(normalized.get("properties") or {}, ensure_ascii=False),
                        json.dumps(flatten_to_2d(normalized["geometry"])),
                        actor.id,
                    ),
                )
        if not keep_revision:
            self._write_meta(cursor, CREATED_AT_KEY, utc_now())
            cursor.execute(
                """
                INSERT INTO draft_meta (key, value) VALUES (%s, jsonb_build_object('value', 0))
                ON CONFLICT (key) DO NOTHING
                """,
                (REVISION_KEY,),
            )

    @staticmethod
    def _log(
        cursor,
        actor: Actor,
        *,
        action: str,
        layer: str | None,
        feature_id: str | None,
        before_state: Any,
        after_state: Any,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO change_log (actor_id, actor_label, action, layer, feature_id,
                                    before_state, after_state, request_id)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            """,
            (
                actor.id,
                actor.label,
                action,
                layer,
                feature_id,
                json.dumps(before_state, ensure_ascii=False) if before_state is not None else None,
                json.dumps(after_state, ensure_ascii=False) if after_state is not None else None,
                actor.request_id,
            ),
        )
