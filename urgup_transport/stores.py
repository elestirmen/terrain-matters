"""One system of record for the whole process.

The web layer, the optimization worker and the publishing pipeline all read or
write the draft. If each picked its own store, a deployment with a database
configured would edit rows in one place and publish from a file in another, and
the two would diverge silently — the second one quietly discarding the edits.
They share this instance instead.

It lives here rather than under `webapp/` because the pipeline scripts need it
too, and they must not import the web layer to get it.
"""

from __future__ import annotations

import logging
import os

from . import db
from .draft_store import DatabaseDraftStore
from .editor_store import EditableNetworkStore

log = logging.getLogger(__name__)

STORE_ENV = "URGUP_STORE"


class StoreConfigurationError(RuntimeError):
    """Raised when the configured system of record cannot be served."""


def selected_backend() -> str:
    """Return `database` or `json`, defaulting to whichever is configured.

    Setting DATABASE_URL is the signal to use it, because a deployment that has
    a database and still writes files would be keeping two records. The choice
    can be forced with URGUP_STORE for a staged rollout or a fallback.
    """
    forced = os.environ.get(STORE_ENV, "").strip().lower()
    if forced in {"json", "database"}:
        return forced
    if forced:
        raise StoreConfigurationError(f"{STORE_ENV} yalnızca 'json' veya 'database' olabilir.")
    return "database" if db.database_configured() else "json"


def build_store():
    """Build the store, and never silently fall back to the wrong one.

    A database whose schema is behind is an operator error with a clear fix, so
    the process refuses to start rather than writing to the file store. A
    database that is merely unreachable is transient: the store is still the
    database one, so requests fail loudly instead of writing somewhere else.
    """
    backend = selected_backend()
    if backend == "json":
        log.info("Sistem kaydı: JSON taslağı.")
        return EditableNetworkStore()

    if not db.database_configured():
        raise StoreConfigurationError(
            f"{STORE_ENV}=database seçildi ama {db.DATABASE_URL_ENV} tanımlı değil."
        )
    try:
        pending = db.pending_migrations()
    except Exception as exc:
        log.error("Veritabanına ulaşılamadı; şema sürümü doğrulanamadı: %s", exc)
        return DatabaseDraftStore()
    if pending:
        raise StoreConfigurationError(
            "Veritabanı şeması güncel değil; bekleyen migration: "
            + ", ".join(pending)
            + '. Çalıştırın: python -c "from urgup_transport import db; db.migrate()"'
        )
    log.info("Sistem kaydı: PostgreSQL.")
    return DatabaseDraftStore()


def build_road_store():
    """The road network's store, chosen the same way as the draft's."""
    backend = selected_backend()
    if backend == "json":
        from .optimizer import DEFAULT_EDITABLE_ROAD_NETWORK
        from .road_network import EditableRoadNetworkStore

        # Read the module attribute now rather than at import: the planning tests
        # point it at a fixture, and a store built once would not see that.
        return EditableRoadNetworkStore(path=DEFAULT_EDITABLE_ROAD_NETWORK)

    if not db.database_configured():
        raise StoreConfigurationError(
            f"{STORE_ENV}=database seçildi ama {db.DATABASE_URL_ENV} tanımlı değil."
        )
    from .road_store import DatabaseRoadNetworkStore

    return DatabaseRoadNetworkStore()


_store = None
_road_store = None


def editor_store():
    """The process-wide draft store, built on first use."""
    global _store
    if _store is None:
        _store = build_store()
    return _store


def road_store():
    """The road network store.

    An explicitly injected store always wins. Otherwise the database one is
    cached, and the file one is rebuilt each call so a test repointing the
    default path is honoured.
    """
    global _road_store
    if _road_store is not None:
        return _road_store
    if selected_backend() == "json":
        return build_road_store()
    _road_store = build_road_store()
    return _road_store


def road_store_for(path=None):
    """The road store, optionally pinned to a named GeoJSON file.

    In file mode the caller may say which file: applying an optimization
    proposal names the network the proposal was generated against, and the
    planning tests point it at a fixture. In database mode the network has one
    home, so the path is only a signal that the proposal is road-bound.
    """
    if _road_store is not None:
        return _road_store
    if selected_backend() == "database":
        return road_store()
    if path is None:
        return build_road_store()
    from .road_network import EditableRoadNetworkStore

    return EditableRoadNetworkStore(path=path)


def editable_road_network_payload():
    """The editable road network, or None when there is not one yet.

    The optimizer prefers this over the cached OSM graph. In file mode a missing
    file means "not set up"; in database mode an empty table means the same.
    """
    if selected_backend() == "json":
        from .optimizer import DEFAULT_EDITABLE_ROAD_NETWORK

        if not DEFAULT_EDITABLE_ROAD_NETWORK.exists():
            return None
        import json

        return json.loads(DEFAULT_EDITABLE_ROAD_NETWORK.read_text(encoding="utf-8"))

    store = road_store()
    if store.is_empty():
        return None
    return store.read()


def reset_for_tests(store=None) -> None:
    """Replace the shared draft store. Only tests should call this."""
    global _store
    _store = store


def reset_road_store_for_tests(store=None) -> None:
    """Replace the shared road store. Only tests should call this."""
    global _road_store
    _road_store = store
