#!/usr/bin/env python3
"""
Build the elevation grid the network is read against.

The planner is two-dimensional and stays that way: `draft_store.flatten_to_2d`
refuses a geometry that carries a third ordinate, and nothing here changes that.
Elevation is not a property of a route or a stop — it is a pure function of
(position, terrain), so it lives in one file next to the network rather than
inside it. Drag a vertex in the editor and a stored `grade_percent` would
silently become a lie; a value derived at read time cannot.

The whole of Ürgüp fits in one 272 KiB array, and everything that reads
elevation as a number reads that. The renderer is the exception: MapLibre wants
terrain as tiles, so the same array is also cut into a small Terrain-RGB
pyramid. It stops at z12 because at 38.64°N a z12 tile is already 29.9 m per
pixel — GLO-30's own spacing — so every zoom above that would be upsampling.
Cutting it to z16 instead would turn 272 KiB into roughly 14 MB of the same
information.

Source is Copernicus DEM GLO-30, which is open data and needs no key. Note it
is a *surface* model — it includes buildings and vegetation. Along a road
centreline that is in practice the ground, which is what the route profiles
sample, but a value read over a building is a roof.

    .venv/bin/python scripts/build_elevation.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# numpy is a runtime dependency of the project; rasterio and pillow are not —
# they live in requirements-dev.txt. Imported inside the functions that need
# them so `--if-missing` can confirm a built artefact on a production machine
# that never installed them, instead of dying on the import and looking like a
# failure when there is nothing to do.
import numpy as np

# Dataset version rides in the directory name, never in a query string. A newer
# source (HGM SYM5, a municipal photogrammetric DSM) lands beside this one as
# `sym5-v1/` and nothing that reads it has to learn a new path shape.
DATASET = "glo30-v1"

# The network's own extent, plus ~300 m so a bilinear read at the edge still has
# four neighbours. Values are the measured bounds of road_network.geojson,
# rounded outward.
WEST, EAST = 34.8860, 35.0250
SOUTH, NORTH = 38.5965, 38.6895

# 30 m on the ground in both directions at this latitude, so a cell is square
# where it is read. Latitude degrees are constant; longitude degrees are not,
# hence the two different steps.
EARTH_RADIUS_M = 6371000.0
METRES_PER_DEGREE_LAT = EARTH_RADIUS_M * np.pi / 180.0
TARGET_SPACING_M = 30.0

NODATA = -32768

TILES = (
    "Copernicus_DSM_COG_10_N38_00_E034_00_DEM",
    "Copernicus_DSM_COG_10_N38_00_E035_00_DEM",
)
TILE_URL = (
    "/vsicurl/https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/{name}/{name}.tif"
)

LICENCE = (
    "Copernicus DEM — free, full and open access "
    "(ESA/Airbus, COPERNICUS DEM Licence). Attribution required."
)


def _grid() -> dict[str, Any]:
    """The destination grid: north-west origin, row-major, south-eastward."""
    mid_lat = (SOUTH + NORTH) / 2.0
    d_lat = TARGET_SPACING_M / METRES_PER_DEGREE_LAT
    d_lon = TARGET_SPACING_M / (METRES_PER_DEGREE_LAT * np.cos(np.radians(mid_lat)))
    rows = int(np.ceil((NORTH - SOUTH) / d_lat))
    cols = int(np.ceil((EAST - WEST) / d_lon))
    return {
        "lat0": NORTH,
        "lon0": WEST,
        "d_lat": float(d_lat),
        "d_lon": float(d_lon),
        "rows": rows,
        "cols": cols,
    }


def _read(grid: dict[str, Any]) -> np.ndarray:
    """Reproject every source tile into one destination array."""
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject

    transform = from_origin(grid["lon0"], grid["lat0"], grid["d_lon"], grid["d_lat"])
    dest = np.full((grid["rows"], grid["cols"]), np.nan, dtype="float32")

    for name in TILES:
        url = TILE_URL.format(name=name)
        print(f"  okunuyor: {name}", flush=True)
        with rasterio.open(url) as src:
            patch = np.full_like(dest, np.nan)
            reproject(
                source=rasterio.band(src, 1),
                destination=patch,
                dst_transform=transform,
                dst_crs="EPSG:4326",
                dst_nodata=np.nan,
                src_nodata=src.nodata,
                resampling=Resampling.bilinear,
            )
            # Tiles abut rather than overlap; the first one to supply a cell wins.
            dest = np.where(np.isnan(dest), patch, dest)

    missing = int(np.isnan(dest).sum())
    if missing:
        print(f"  uyarı: {missing} hücre kaynaksız, {NODATA} yazılıyor", flush=True)
    return dest


def build(out_dir: Path) -> dict[str, Any]:
    grid = _grid()
    print(f"grid {grid['rows']}×{grid['cols']} @ {TARGET_SPACING_M:.0f} m", flush=True)

    values = _read(grid)
    quantised = np.where(np.isnan(values), NODATA, np.rint(values)).astype("int16")

    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "urgup-30m.i16"
    # Little-endian throughout, so the Python and JS readers agree without
    # either of them having to ask the platform.
    raw_path.write_bytes(quantised.astype("<i2").tobytes())

    real = quantised[quantised != NODATA]
    meta = {
        "dataset": DATASET,
        "source": "Copernicus DEM GLO-30",
        "source_kind": "DSM",
        "source_tiles": list(TILES),
        "licence": LICENCE,
        "accessed_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "crs": "EPSG:4326",
        "dtype": "int16",
        "byte_order": "little",
        "nodata": NODATA,
        "spacing_m": TARGET_SPACING_M,
        "vertical_accuracy_m": 4.0,
        "bounds": {"west": WEST, "east": EAST, "south": SOUTH, "north": NORTH},
        **grid,
        "min_m": int(real.min()),
        "max_m": int(real.max()),
        "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "bytes": raw_path.stat().st_size,
    }
    (out_dir / "urgup-30m.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return meta


# MapLibre reads terrain as tiles, so the one array has to be cut up for it —
# but only as far as the data actually goes. At 38.64°N a z12 tile is 29.9 m per
# pixel, which is exactly GLO-30's spacing: every zoom above this is upsampling,
# so the source declares maxzoom 12 and lets the renderer overzoom rather than
# shipping invented detail. With the margin below, z10–z12 is ~110 tiles and
# a little over 300 KiB.
TERRAIN_MAX_ZOOM = 12
TERRAIN_MIN_ZOOM = 10
TILE_MARGIN = 2
TILE_SIZE = 256


def _sample_bilinear(
    values: np.ndarray, grid: dict[str, Any], lat: np.ndarray, lon: np.ndarray
) -> np.ndarray:
    """
    Bilinear read of the grid at arbitrary positions, clamped at the edges.

    Clamping rather than masking: beyond our extent the terrain simply keeps the
    last known height instead of dropping to a cliff at the boundary, which is
    what a renderer needs at the edge of a tile that runs off the data.
    """
    col = np.clip((lon - grid["lon0"]) / grid["d_lon"], 0, grid["cols"] - 1)
    row = np.clip((grid["lat0"] - lat) / grid["d_lat"], 0, grid["rows"] - 1)
    col0 = np.floor(col).astype(int)
    row0 = np.floor(row).astype(int)
    col1 = np.minimum(col0 + 1, grid["cols"] - 1)
    row1 = np.minimum(row0 + 1, grid["rows"] - 1)
    fx = col - col0
    fy = row - row0

    top = values[row0, col0] * (1 - fx) + values[row0, col1] * fx
    bottom = values[row1, col0] * (1 - fx) + values[row1, col1] * fx
    return top * (1 - fy) + bottom * fy


def _terrain_rgb(heights: np.ndarray) -> np.ndarray:
    """Mapbox Terrain-RGB: height = -10000 + (R·65536 + G·256 + B) · 0.1"""
    packed = np.clip(np.rint((heights + 10000.0) * 10.0), 0, 256**3 - 1).astype(np.uint32)
    return np.dstack(
        [(packed >> 16) & 0xFF, (packed >> 8) & 0xFF, packed & 0xFF]
    ).astype(np.uint8)


def build_terrain_tiles(out_dir: Path, grid: dict[str, Any]) -> dict[str, Any]:
    """Cut the array into Terrain-RGB tiles for MapLibre's `raster-dem` source."""
    from PIL import Image

    raw = np.frombuffer((out_dir / "urgup-30m.i16").read_bytes(), dtype="<i2")
    values = raw.reshape(grid["rows"], grid["cols"]).astype("float32")
    values[values == NODATA] = np.nan
    if np.isnan(values).any():
        values = np.nan_to_num(values, nan=float(np.nanmean(values)))

    tile_root = out_dir / "terrain"
    written = 0
    total_bytes = 0
    for zoom in range(TERRAIN_MIN_ZOOM, TERRAIN_MAX_ZOOM + 1):
        n = 2**zoom
        # Margin on every side: a tilted viewport reaches well past the data
        # extent towards the horizon, and a tile that 404s leaves a hard edge
        # where the terrain mesh simply stops. Margin tiles cost a few KiB and
        # carry the clamped edge height, so the ground runs off-screen instead
        # of ending in a cliff.
        x_min = int((WEST + 180.0) / 360.0 * n) - TILE_MARGIN
        x_max = int((EAST + 180.0) / 360.0 * n) + TILE_MARGIN
        y_min = int((1 - math.asinh(math.tan(math.radians(NORTH))) / math.pi) / 2 * n) - TILE_MARGIN
        y_max = int((1 - math.asinh(math.tan(math.radians(SOUTH))) / math.pi) / 2 * n) + TILE_MARGIN
        x_min = max(0, x_min)
        y_min = max(0, y_min)
        x_max = min(n - 1, x_max)
        y_max = min(n - 1, y_max)

        # Pixel centres, so a tile samples the middle of each texel rather than
        # its corner and neighbouring tiles line up without a seam.
        offset = (np.arange(TILE_SIZE) + 0.5) / TILE_SIZE
        for tile_x in range(x_min, x_max + 1):
            lon = (tile_x + offset) / n * 360.0 - 180.0
            for tile_y in range(y_min, y_max + 1):
                merc = np.pi * (1 - 2 * (tile_y + offset) / n)
                lat = np.degrees(np.arctan(np.sinh(merc)))
                lat_grid, lon_grid = np.meshgrid(lat, lon, indexing="ij")

                heights = _sample_bilinear(values, grid, lat_grid, lon_grid)
                path = tile_root / str(zoom) / str(tile_x) / f"{tile_y}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.fromarray(_terrain_rgb(heights), mode="RGB").save(
                    path, format="PNG", optimize=True
                )
                written += 1
                total_bytes += path.stat().st_size

    summary = {
        "tiles": written,
        "bytes": total_bytes,
        "minzoom": TERRAIN_MIN_ZOOM,
        "maxzoom": TERRAIN_MAX_ZOOM,
        "tile_size": TILE_SIZE,
        "encoding": "mapbox",
    }
    (tile_root / "terrain.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def write_fixture(out_dir: Path, meta: dict[str, Any]) -> Path:
    """
    A tiny grid both samplers are tested against.

    The point of the fixture is that it is not the real terrain: the values are
    a plane plus a bump, so a bilinear reader's answer can be checked by hand.
    """
    rows, cols = 4, 5
    values = [
        [100, 110, 120, 130, 140],
        [200, 210, 220, 230, 240],
        [300, 310, 320, 330, 340],
        [400, 410, 420, 430, NODATA],
    ]
    payload = b"".join(
        struct.pack("<h", value) for row in values for value in row
    )
    fixture_dir = out_dir
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / "fixture.i16").write_bytes(payload)
    (fixture_dir / "fixture.json").write_text(
        json.dumps(
            {
                "dataset": "fixture",
                "dtype": "int16",
                "byte_order": "little",
                "nodata": NODATA,
                "lat0": 39.0,
                "lon0": 34.0,
                "d_lat": 0.01,
                "d_lon": 0.01,
                "rows": rows,
                "cols": cols,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return fixture_dir / "fixture.i16"


def _artefact_is_current(out_dir: Path) -> bool:
    """
    Is the built grid already there and intact?

    Checked by the digest the build recorded, not by the file merely existing:
    a truncated download leaves a file behind too, and a deploy that skipped on
    that would hand the site a grid it cannot read.
    """
    raw_path = out_dir / "urgup-30m.i16"
    meta_path = out_dir / "urgup-30m.json"
    if not raw_path.is_file() or not meta_path.is_file():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("dataset") != DATASET:
            return False
        if raw_path.stat().st_size != meta.get("bytes"):
            return False
        digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        if digest != meta.get("sha256"):
            return False
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    # The terrain tiles are part of the same artefact; without them the 3D page
    # renders a flat world while every number still reads correctly, which is
    # exactly the kind of half-working state a digest check exists to catch.
    return (out_dir / "terrain" / "terrain.json").is_file()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data" / "processed" / "elevation" / DATASET,
        help="çıktı dizini",
    )
    parser.add_argument(
        "--fixture-only",
        action="store_true",
        help="ağa çıkmadan yalnızca test fixture'ını yaz",
    )
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="varlık zaten doğruysa hiçbir şey yapma (dağıtımda tekrar tekrar çağrılır)",
    )
    args = parser.parse_args()

    if args.if_missing and _artefact_is_current(args.out):
        print(f"yükseklik verisi yerinde, atlanıyor: {args.out}")
        return 0

    fixture_dir = ROOT / "tests" / "fixtures" / "elevation"
    if args.fixture_only:
        path = write_fixture(fixture_dir, {})
        print(f"fixture yazıldı: {path}")
        return 0

    meta = build(args.out)
    write_fixture(fixture_dir, meta)
    print(
        f"yazıldı: {args.out / 'urgup-30m.i16'} "
        f"({meta['bytes'] / 1024:.1f} KiB, {meta['min_m']}–{meta['max_m']} m)"
    )

    tiles = build_terrain_tiles(args.out, _grid())
    print(
        f"arazi karoları: {tiles['tiles']} adet, "
        f"{tiles['bytes'] / 1024:.1f} KiB, z{tiles['minzoom']}–z{tiles['maxzoom']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
