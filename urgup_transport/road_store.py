"""PostGIS-backed editable road network, alongside the GeoJSON store.

Both implementations answer the same calls, so one contract test covers them and
the database one is shown to behave like the file one before anything switches.

What moving it buys is not locking — an ``fcntl`` flock already serializes the
file across processes on one host. It is that a road edit changes what the
optimizer produces, and until now nobody recorded who made it; and that a backup
covering a draft in the database and a road network in a file cannot be restored
to one consistent moment.

``frozen()`` is the piece that matters. Applying an optimization proposal has to
hold the road network steady while it validates the proposal against it and then
writes the draft. The file store does that with a flock; this does it with a
session-level advisory lock, so the guarantee survives the draft write happening
on a different connection. Lock order stays road → draft: road mutations never
touch the draft, so there is no cycle.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import db
from .draft_store import Actor, flatten_to_2d
from .editor_store import DraftValidationError, RevisionConflictError, utc_now
from .routing_contract import road_network_digest

log = logging.getLogger(__name__)

REVISION_KEY = "road_revision"
UPDATED_AT_KEY = "road_updated_at"
SOURCE_KEY = "road_source"

# Same lock space as the mutations below, so a proposal being applied and a road
# edit cannot interleave.
ROAD_ADVISORY_LOCK = 0x55524F41  # "UROA"

GEOJSON_DECIMALS = 15


class DatabaseRoadNetworkStore:
    """The editable road network, stored as rows."""

    def __init__(self, graph_path: Path | None = None):
        # Imported here rather than at module scope: road_network imports the
        # optimizer, which would close an import cycle through this module.
        if graph_path is None:
            from .optimizer import DEFAULT_ROAD_GRAPH

            graph_path = DEFAULT_ROAD_GRAPH
        self.graph_path = graph_path

    # ── reads ────────────────────────────────────────────────────────────
    def read(self) -> dict[str, Any]:
        with db.connection() as conn, conn.cursor() as cursor:
            return self._read(cursor)

    def _read(self, cursor) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT id, properties, ST_AsGeoJSON(geometry, %s)::jsonb
              FROM draft_road
             ORDER BY ordinal, id
            """,
            (GEOJSON_DECIMALS,),
        )
        features = [
            {
                "type": "Feature",
                "id": feature_id,
                "properties": dict(properties or {}),
                "geometry": geometry,
            }
            for feature_id, properties, geometry in cursor.fetchall()
        ]
        meta = self._read_meta(cursor)
        return {
            "type": "FeatureCollection",
            "schema_version": 1,
            "revision": int(meta.get(REVISION_KEY) or 0),
            "updated_at_utc": meta.get(UPDATED_AT_KEY) or utc_now(),
            "source": meta.get(SOURCE_KEY) or "OpenStreetMap + local editor changes",
            "features": features,
        }

    @staticmethod
    def _read_meta(cursor) -> dict[str, Any]:
        cursor.execute(
            "SELECT key, value FROM draft_meta WHERE key = ANY(%s)",
            ([REVISION_KEY, UPDATED_AT_KEY, SOURCE_KEY],),
        )
        return {key: value.get("value") for key, value in cursor.fetchall()}

    def ensure(self, *, actor: Actor | None = None) -> dict[str, Any]:
        """Return the network, seeding it from the cached OSM graph when empty."""
        with db.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM draft_meta WHERE key = %s)", (REVISION_KEY,))
            if not cursor.fetchone()[0]:
                self._seed(cursor, actor or Actor.system("ilk kurulum"))
            return self._read(cursor)

    def is_empty(self) -> bool:
        with db.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM draft_road)")
            return not cursor.fetchone()[0]

    def cache_token(self) -> tuple[str, int]:
        """A cheap change signal: the network revision, read without the rows."""
        with db.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT (value ->> 'value')::bigint FROM draft_meta WHERE key = %s",
                (REVISION_KEY,),
            )
            row = cursor.fetchone()
            return ("database", int(row[0]) if row else 0)

    # ── holding it steady ────────────────────────────────────────────────
    @contextmanager
    def frozen(self):
        """Block road edits and yield the current revision and digest.

        The lock is session level, not transaction level, because the caller
        writes the draft on a separate connection while holding this.
        """
        with db.connection(autocommit=True) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (ROAD_ADVISORY_LOCK,))
            try:
                payload = self._read(cursor)
                yield {
                    "revision": int(payload["revision"]),
                    "digest": road_network_digest(payload),
                }
            finally:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (ROAD_ADVISORY_LOCK,))

    # ── writes ───────────────────────────────────────────────────────────
    def save_feature(
        self,
        feature: dict[str, Any],
        *,
        feature_id: str | None = None,
        expected_revision: int | None = None,
        actor: Actor | None = None,
    ) -> dict[str, Any]:
        from .road_network import validate_road_feature

        who = actor or Actor.system()
        with db.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ROAD_ADVISORY_LOCK,))
            self._check_revision(cursor, expected_revision)
            normalized = validate_road_feature({**feature, "id": feature_id or feature.get("id")})
            before = self._feature_by_id(cursor, normalized["id"])
            if feature_id and before is None:
                raise KeyError(f"Yol bulunamadı: {feature_id}")

            geometry_json = json.dumps(flatten_to_2d(normalized["geometry"]))
            properties_json = json.dumps(normalized.get("properties") or {}, ensure_ascii=False)
            if before is None:
                cursor.execute(
                    """
                    INSERT INTO draft_road (id, ordinal, properties, geometry, updated_by)
                    VALUES (%s, (SELECT coalesce(max(ordinal), 0) + 1 FROM draft_road),
                            %s::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s)
                    """,
                    (normalized["id"], properties_json, geometry_json, who.id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE draft_road
                       SET properties = %s::jsonb,
                           geometry   = ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326),
                           revision   = revision + 1,
                           updated_at = now(),
                           updated_by = %s
                     WHERE id = %s
                    """,
                    (properties_json, geometry_json, who.id, normalized["id"]),
                )
            self._log(cursor, who, "update" if before else "create", normalized["id"], before, normalized)
            revision = self._bump_revision(cursor)
        return {"revision": revision, "feature": normalized}

    def delete_feature(
        self,
        feature_id: str,
        *,
        expected_revision: int | None = None,
        actor: Actor | None = None,
    ) -> dict[str, Any]:
        who = actor or Actor.system()
        with db.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ROAD_ADVISORY_LOCK,))
            self._check_revision(cursor, expected_revision)
            before = self._feature_by_id(cursor, feature_id)
            if before is None:
                raise KeyError(f"Yol bulunamadı: {feature_id}")
            cursor.execute("DELETE FROM draft_road WHERE id = %s", (feature_id,))
            self._log(cursor, who, "delete", feature_id, before, None)
            revision = self._bump_revision(cursor)
        return {"revision": revision, "deleted_id": feature_id}

    def replace_all(self, payload: dict[str, Any], *, actor: Actor | None = None) -> dict[str, Any]:
        """Swap the whole network, for an OSM refresh that already merged locally."""
        from .road_network import validate_road_feature

        who = actor or Actor.system()
        with db.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ROAD_ADVISORY_LOCK,))
            cursor.execute("SELECT count(*) FROM draft_road")
            replaced = cursor.fetchone()[0]
            cursor.execute("DELETE FROM draft_road")
            for ordinal, feature in enumerate(payload.get("features", []), start=1):
                normalized = validate_road_feature(feature)
                cursor.execute(
                    """
                    INSERT INTO draft_road (id, ordinal, properties, geometry, updated_by)
                    VALUES (%s, %s, %s::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s)
                    """,
                    (
                        normalized["id"],
                        ordinal,
                        json.dumps(normalized.get("properties") or {}, ensure_ascii=False),
                        json.dumps(flatten_to_2d(normalized["geometry"])),
                        who.id,
                    ),
                )
            if payload.get("source"):
                self._write_meta(cursor, SOURCE_KEY, payload["source"])
            self._log(
                cursor,
                who,
                "update",
                None,
                {"feature_count": replaced},
                {"feature_count": len(payload.get("features", []))},
            )
            self._bump_revision(cursor)
            return self._read(cursor)

    # ── internals ────────────────────────────────────────────────────────
    @staticmethod
    def _feature_by_id(cursor, feature_id: str) -> dict[str, Any] | None:
        cursor.execute(
            """
            SELECT id, properties, ST_AsGeoJSON(geometry, %s)::jsonb
              FROM draft_road WHERE id = %s
            """,
            (GEOJSON_DECIMALS, feature_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "type": "Feature",
            "id": row[0],
            "properties": dict(row[1] or {}),
            "geometry": row[2],
        }

    def _check_revision(self, cursor, expected_revision: int | None) -> None:
        if expected_revision is None:
            return
        current = int(self._read_meta(cursor).get(REVISION_KEY) or 0)
        if int(expected_revision) != current:
            raise RevisionConflictError("Yol ağı başka bir oturumda değişti. Veriyi yenileyin.")

    def _bump_revision(self, cursor) -> int:
        cursor.execute(
            """
            INSERT INTO draft_meta (key, value) VALUES (%s, jsonb_build_object('value', 1))
            ON CONFLICT (key) DO UPDATE
               SET value = jsonb_build_object('value', (draft_meta.value ->> 'value')::bigint + 1),
                   updated_at = now()
            RETURNING (value ->> 'value')::bigint
            """,
            (REVISION_KEY,),
        )
        revision = cursor.fetchone()[0]
        self._write_meta(cursor, UPDATED_AT_KEY, utc_now())
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

    def _seed(self, cursor, actor: Actor) -> None:
        """Populate an empty network from the cached OSM drive graph, if there is one.

        Without a cached graph the network starts empty at revision 0. That is a
        valid state — the editor can draw roads from scratch — and it means a
        fresh deployment is usable before anyone has run the OSM cache.
        """
        from .road_network import _context, validate_road_feature

        try:
            payload = deepcopy(_context(self.graph_path)[0])
        except (FileNotFoundError, OSError):
            log.info("Önbelleğe alınmış OSM grafiği yok; yol ağı boş başlatılıyor.")
            payload = {"type": "FeatureCollection", "features": []}
        for ordinal, feature in enumerate(payload.get("features", []), start=1):
            normalized = validate_road_feature(feature)
            cursor.execute(
                """
                INSERT INTO draft_road (id, ordinal, properties, geometry, updated_by)
                VALUES (%s, %s, %s::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    normalized["id"],
                    ordinal,
                    json.dumps(normalized.get("properties") or {}, ensure_ascii=False),
                    json.dumps(flatten_to_2d(normalized["geometry"])),
                    actor.id,
                ),
            )
        if payload.get("source"):
            self._write_meta(cursor, SOURCE_KEY, payload["source"])
        self._write_meta(cursor, UPDATED_AT_KEY, utc_now())
        cursor.execute(
            """
            INSERT INTO draft_meta (key, value) VALUES (%s, jsonb_build_object('value', 0))
            ON CONFLICT (key) DO NOTHING
            """,
            (REVISION_KEY,),
        )

    @staticmethod
    def _log(cursor, actor: Actor, action: str, feature_id: str | None, before: Any, after: Any) -> None:
        """Record the edit. A road change alters what the optimizer produces."""
        cursor.execute(
            """
            INSERT INTO change_log (actor_id, actor_label, action, layer, feature_id,
                                    before_state, after_state, request_id)
            VALUES (%s, %s, %s, 'roads', %s, %s::jsonb, %s::jsonb, %s)
            """,
            (
                actor.id,
                actor.label,
                action,
                feature_id,
                json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False) if after is not None else None,
                actor.request_id,
            ),
        )


def road_network_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Revision and digest for a payload, however it was read."""
    if not isinstance(payload, dict):
        raise DraftValidationError("Yol ağı revizyonu geçerli bir nesne değil.")
    return {"revision": int(payload.get("revision", 0)), "digest": road_network_digest(payload)}
