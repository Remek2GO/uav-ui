"""Points and paths drawn on the map, saved as GeoJSON files. Works with no drone connected."""

import json
import math
import re
from pathlib import Path

_SAFE_NAME = re.compile(r"[\w ._-]{1,80}", re.UNICODE)
_SAFE_SCOPE = re.compile(r"[a-z0-9_][a-z0-9_-]{0,63}")
GLOBAL_SCOPE = "_global"
MAX_POINTS = 500


class PlanError(Exception):
    pass


def _check_position(position):
    if not isinstance(position, list) or len(position) not in (2, 3):
        raise PlanError("a position is [lon, lat] or [lon, lat, alt]")
    for value in position:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise PlanError("coordinates must be finite numbers")
    if not -180 <= position[0] <= 180 or not -90 <= position[1] <= 90:
        raise PlanError("coordinates out of range")


def validate_plan(plan):
    """A plan is a GeoJSON Feature with a Point or a LineString."""
    if not isinstance(plan, dict) or plan.get("type") != "Feature":
        raise PlanError("a plan must be a GeoJSON Feature")
    geometry = plan.get("geometry")
    if not isinstance(geometry, dict):
        raise PlanError("a plan needs a geometry")
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "Point":
        _check_position(coordinates)
    elif kind == "LineString":
        if not isinstance(coordinates, list) or not 1 <= len(coordinates) <= MAX_POINTS:
            raise PlanError(f"a path has 1 to {MAX_POINTS} points")
        for position in coordinates:
            _check_position(position)
    else:
        raise PlanError("only Point and LineString are supported")
    return plan


class PlanStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "plans"
        self.root.mkdir(parents=True, exist_ok=True)

    def _scope_dir(self, scope):
        if not _SAFE_SCOPE.fullmatch(scope):
            raise PlanError("invalid scope")
        return self.root / scope

    def _file(self, scope, name):
        if not _SAFE_NAME.fullmatch(name) or name.startswith(".") or ".." in name:
            raise PlanError("invalid name: use letters, digits, spaces, dots, dashes")
        return self._scope_dir(scope) / f"{name}.geojson"

    def list(self, scope):
        directory = self._scope_dir(scope)
        plans = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.geojson")):
                try:
                    plans.append({"name": path.stem, "plan": json.loads(path.read_text("utf-8"))})
                except ValueError:
                    continue  # a damaged file must not hide the others
        return plans

    def save(self, scope, name, plan):
        path = self._file(scope, name)
        validate_plan(plan)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    def delete(self, scope, name):
        path = self._file(scope, name)
        if not path.is_file():
            return False
        path.unlink()
        return True
