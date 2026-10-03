"""A hill under the synthetic road network, for energy tests.

The synthetic network (`tests/synthetic_fixture.py`) sits at 34.900–34.903°E,
38.630–38.631°N. This grid covers it with 0.0005° cells and rises to the east
at a constant 3 m per cell — roughly a 7 % grade, gentle enough that the grade
cap never binds and every expected number can be reasoned about by hand.
"""

from __future__ import annotations

import array
import hashlib
import json
import struct
from pathlib import Path
from typing import Any

from urgup_transport.elevation import ElevationGrid

ROWS, COLS = 6, 12
LAT0, LON0 = 38.6320, 34.8990
D_LAT = D_LON = 0.0005
NODATA = -32768


def hill_values(*, east_rise_per_cell: float = 3.0, flat: bool = False) -> list[int]:
    values: list[int] = []
    for _row in range(ROWS):
        for col in range(COLS):
            values.append(100 if flat else int(round(100 + east_rise_per_cell * col)))
    return values


def hill_meta(dataset: str = "test-hill") -> dict[str, Any]:
    return {
        "dataset": dataset,
        "source": "synthetic hill",
        "source_kind": "test",
        "rows": ROWS,
        "cols": COLS,
        "lat0": LAT0,
        "lon0": LON0,
        "d_lat": D_LAT,
        "d_lon": D_LON,
        "nodata": NODATA,
        "dtype": "int16",
        "byte_order": "little",
        "spacing_m": 45.0,
        "vertical_accuracy_m": 4.0,
    }


def hill_grid(**kwargs: Any) -> ElevationGrid:
    return ElevationGrid(array.array("h", hill_values(**kwargs)), hill_meta())


def write_hill_grid(directory: Path, **kwargs: Any) -> Path:
    """Write the hill in the on-disk layout `ElevationGrid.load` reads."""
    directory.mkdir(parents=True, exist_ok=True)
    values = hill_values(**kwargs)
    raw = struct.pack(f"<{len(values)}h", *values)
    meta = hill_meta()
    meta["sha256"] = hashlib.sha256(raw).hexdigest()
    (directory / "urgup-30m.i16").write_bytes(raw)
    (directory / "urgup-30m.json").write_text(json.dumps(meta), encoding="utf-8")
    return directory
