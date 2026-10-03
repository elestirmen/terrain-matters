"""
Reading elevation for a geometry, without storing it on the geometry.

`draft_store.flatten_to_2d` refuses a third ordinate and that decision stands.
Its docstring packs two things into one sentence — "a 3D schema *and* a planner
that honours grade" — and they come apart: honouring grade needs no schema at
all, because elevation is a pure function of (position, terrain grid). Derive it
at read time and the editor cannot desynchronise it; store it and dragging one
vertex quietly falsifies every number computed from the old shape.

Deliberately stdlib-only. The grid is a build artefact produced by
`scripts/build_elevation.py`; nothing here needs numpy, so nothing here forces a
new runtime dependency on the service.

The same bilinear read is implemented in `webapp/static/js/lib/elevation.js`,
and `tests/test_elevation.py` and `tests/js/elevation.test.js` check the two
against one shared fixture — a grid whose values are chosen so the right answer
can be worked out by hand.
"""

from __future__ import annotations

import array
import json
import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_ELEVATION_DIR, PROJECT_ROOT

EARTH_RADIUS_M = 6371000.0

# Above this the road is doing something a minibus timetable should mention.
STEEP_GRADE_PERCENT = 6.0

# Grade is read over a fixed ground base, never between adjacent samples. With
# GLO-30's ~4 m vertical error, a 30 m base would report ±19 percentage points
# of noise; 100 m brings that to roughly ±6, which is still wide and is why
# `grade_percent` is reported with its uncertainty rather than on its own.
GRADE_BASE_M = 100.0


class ElevationUnavailableError(RuntimeError):
    """Raised when the grid has not been built."""


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle metres. Matches `geodesy.js`, including the 6 371 000 m radius."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    sin_dphi = math.sin(math.radians(lat2 - lat1) / 2.0)
    sin_dlmb = math.sin(math.radians(lon2 - lon1) / 2.0)
    h = sin_dphi * sin_dphi + math.cos(phi1) * math.cos(phi2) * sin_dlmb * sin_dlmb
    return EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(h), math.sqrt(1.0 - h))


def resample_line(
    coordinates: Sequence[Sequence[float]], step_m: float, *, keep_vertices: bool = False
) -> list[list[float]]:
    """
    Even ``[lng, lat]`` positions along a line, roughly ``step_m`` apart.

    Sampling every vertex would read the grid at whatever spacing the geometry
    happens to have — dense on a curve, sparse on a straight — and let vertex
    density leak into every figure derived from it. Resampling to a fixed step
    makes a climb or an energy a property of the route, not of how it was
    drawn. Both ends are always kept.

    With ``keep_vertices`` every corner of the line is kept too and the step
    restarts at it, so the chords between samples never cut a corner and the
    sampled length equals the line's length whichever way it is driven. The
    profile endpoint keeps the plain sampling it has always reported.
    """
    points = [list(point) for point in coordinates if point is not None and len(point) >= 2]
    if len(points) < 2:
        return points
    if step_m <= 0:
        return points

    out: list[list[float]] = [list(points[0])]
    carry = 0.0
    for index in range(1, len(points)):
        start = points[index - 1]
        end = points[index]
        span = haversine_m(start[1], start[0], end[1], end[0])
        if span <= 0:
            continue
        travelled = step_m - carry
        while travelled < span:
            ratio = travelled / span
            out.append([
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
            ])
            travelled += step_m
        if keep_vertices:
            if index < len(points) - 1:
                out.append(list(end))
            carry = 0.0
        else:
            carry = (carry + span) % step_m
    out.append(list(points[-1]))
    return out


@dataclass(frozen=True)
class RouteProfile:
    """What a line's shape does vertically, and how much of that to believe."""

    chainage_m: list[float]
    elevation_m: list[float]
    length_m: float
    min_m: float
    max_m: float
    climb_m: float
    descent_m: float
    max_grade_percent: float
    steep_length_m: float
    steep_share: float
    source: str
    confidence: str
    grade_uncertainty_percent: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "length_m": round(self.length_m, 1),
            "min_m": round(self.min_m, 1),
            "max_m": round(self.max_m, 1),
            "relief_m": round(self.max_m - self.min_m, 1),
            "climb_m": round(self.climb_m, 1),
            "descent_m": round(self.descent_m, 1),
            "max_grade_percent": round(self.max_grade_percent, 1),
            "steep_length_m": round(self.steep_length_m, 1),
            "steep_share": round(self.steep_share, 4),
            "steep_threshold_percent": STEEP_GRADE_PERCENT,
            "grade_base_m": GRADE_BASE_M,
            "grade_uncertainty_percent": round(self.grade_uncertainty_percent, 1),
            "elevation_source": self.source,
            "elevation_confidence": self.confidence,
        }


class ElevationGrid:
    """A north-west-origin, row-major grid of int16 metres."""

    def __init__(self, values: array.array, meta: dict[str, Any]) -> None:
        self._values = values
        self.meta = meta
        self.rows = int(meta["rows"])
        self.cols = int(meta["cols"])
        self.lat0 = float(meta["lat0"])
        self.lon0 = float(meta["lon0"])
        self.d_lat = float(meta["d_lat"])
        self.d_lon = float(meta["d_lon"])
        self.nodata = int(meta["nodata"])
        expected = self.rows * self.cols
        if len(values) != expected:
            raise ValueError(
                f"Yükseklik gridi {expected} değer bekliyordu, {len(values)} buldu."
            )

    # -- construction ----------------------------------------------------

    @classmethod
    def load(cls, directory: str | Path | None = None) -> ElevationGrid:
        base = Path(directory) if directory else PROJECT_ROOT / DEFAULT_ELEVATION_DIR
        meta_path = base / "urgup-30m.json"
        raw_path = base / "urgup-30m.i16"
        if not meta_path.exists() or not raw_path.exists():
            raise ElevationUnavailableError(
                f"Yükseklik gridi bulunamadı: {base}. "
                "Önce `python scripts/build_elevation.py` çalıştırın."
            )
        return cls.from_files(raw_path, meta_path)

    @classmethod
    def from_files(cls, raw_path: Path, meta_path: Path) -> ElevationGrid:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        values = array.array("h")
        values.frombytes(raw_path.read_bytes())
        # The file is little-endian by construction, so a big-endian host is the
        # one that has to adapt.
        if sys.byteorder != "little":
            values.byteswap()
        return cls(values, meta)

    # -- sampling --------------------------------------------------------

    def _cell(self, row: int, col: int) -> float | None:
        if row < 0 or col < 0 or row >= self.rows or col >= self.cols:
            return None
        value = self._values[row * self.cols + col]
        return None if value == self.nodata else float(value)

    @staticmethod
    def _snap(value: float) -> float:
        """
        Pull a cell coordinate onto a grid node when it is one to within noise.

        ``(34.04 - 34.00) / 0.01`` is 3.999999999999915 in binary floating
        point, so a read that lands exactly on a node otherwise asks for a
        neighbour one cell further out — off the edge of the grid, at the last
        row or column. Snapping first makes a node read need no interpolation,
        which is what it is. The tolerance is 1e-9 of a cell: 3×10⁻⁸ m here.
        """
        nearest = round(value)
        return float(nearest) if abs(value - nearest) < 1e-9 else value

    def at(self, lat: float, lng: float) -> float | None:
        """
        Bilinear metres at a point, or ``None`` outside the grid.

        A hole is not interpolated across: if any neighbour the read actually
        needs is nodata the answer is ``None``, because a smooth-looking number
        invented over missing terrain is worse than an admitted gap.
        """
        col = self._snap((lng - self.lon0) / self.d_lon)
        row = self._snap((self.lat0 - lat) / self.d_lat)
        col0 = math.floor(col)
        row0 = math.floor(row)
        fx = col - col0
        fy = row - row0
        # A read sitting on a node or an edge needs no second neighbour, and
        # must not be refused because one would have been out of range.
        col1 = col0 if fx == 0.0 else col0 + 1
        row1 = row0 if fy == 0.0 else row0 + 1

        v00 = self._cell(row0, col0)
        v01 = self._cell(row0, col1)
        v10 = self._cell(row1, col0)
        v11 = self._cell(row1, col1)
        if v00 is None or v01 is None or v10 is None or v11 is None:
            return None

        top = v00 + (v01 - v00) * fx
        bottom = v10 + (v11 - v10) * fx
        return top + (bottom - top) * fy

    def sample(self, coordinates: Iterable[Sequence[float]]) -> list[float | None]:
        """Sample GeoJSON ``[lng, lat]`` positions in order."""
        return [self.at(float(point[1]), float(point[0])) for point in coordinates]

    # -- derived ---------------------------------------------------------

    @property
    def source(self) -> str:
        return str(self.meta.get("source", "unknown"))

    @property
    def confidence(self) -> str:
        """
        Mirrors the `speed_source` / `speed_confidence` contract already in the
        road network: a modelled value says so rather than passing as observed.
        """
        return "modelled_dem30"

    def grade_uncertainty_percent(self, base_m: float = GRADE_BASE_M) -> float:
        """
        How wrong a grade read off this grid can be, over a given base.

        Two independent endpoint errors of σ each combine to σ√2; expressed as a
        percentage of the horizontal base that is the number a planner needs to
        see next to any grade this module reports.
        """
        sigma = float(self.meta.get("vertical_accuracy_m", 4.0))
        return 100.0 * sigma * math.sqrt(2.0) / base_m

    def profile(
        self,
        coordinates: Sequence[Sequence[float]],
        *,
        base_m: float = GRADE_BASE_M,
    ) -> RouteProfile | None:
        """
        Chainage and elevation along a GeoJSON ``[lng, lat]`` line.

        Grade is measured over ``base_m`` of ground rather than between adjacent
        vertices, so a dense stretch of geometry does not manufacture slope out
        of vertical noise.
        """
        points = [p for p in coordinates if p is not None and len(p) >= 2]
        if len(points) < 2:
            return None

        chainage = [0.0]
        for index in range(1, len(points)):
            previous, current = points[index - 1], points[index]
            chainage.append(
                chainage[-1]
                + haversine_m(
                    float(previous[1]), float(previous[0]),
                    float(current[1]), float(current[0]),
                )
            )

        sampled = self.sample(points)
        kept = [(c, e) for c, e in zip(chainage, sampled, strict=True) if e is not None]
        if len(kept) < 2:
            return None

        chain = [c for c, _ in kept]
        elevation = [e for _, e in kept]

        climb = descent = 0.0
        for index in range(1, len(elevation)):
            delta = elevation[index] - elevation[index - 1]
            if delta > 0:
                climb += delta
            else:
                descent -= delta

        max_grade = 0.0
        steep_length = 0.0
        head = 0
        for index in range(1, len(chain)):
            while chain[index] - chain[head] > base_m and head < index - 1:
                head += 1
            span = chain[index] - chain[head]
            if span <= 0:
                continue
            grade = abs(elevation[index] - elevation[head]) / span * 100.0
            max_grade = max(max_grade, grade)
            if grade > STEEP_GRADE_PERCENT:
                steep_length += chain[index] - chain[index - 1]

        length = chain[-1]
        return RouteProfile(
            chainage_m=chain,
            elevation_m=elevation,
            length_m=length,
            min_m=min(elevation),
            max_m=max(elevation),
            climb_m=climb,
            descent_m=descent,
            max_grade_percent=max_grade,
            steep_length_m=steep_length,
            steep_share=(steep_length / length) if length else 0.0,
            source=self.source,
            confidence=self.confidence,
            grade_uncertainty_percent=self.grade_uncertainty_percent(base_m),
        )


_CACHED: ElevationGrid | None = None


def load_grid(directory: str | Path | None = None) -> ElevationGrid:
    """Process-wide grid. 272 KiB, read once, shared by every request."""
    global _CACHED
    if directory is not None:
        return ElevationGrid.load(directory)
    if _CACHED is None:
        _CACHED = ElevationGrid.load()
    return _CACHED


def reset_cache() -> None:
    """Drop the shared grid. Exists so tests do not leak one into the next."""
    global _CACHED
    _CACHED = None
