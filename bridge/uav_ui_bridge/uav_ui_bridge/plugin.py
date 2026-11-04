"""Plugin file handling that needs no ROS: loading, argument validation and field templating."""

import hashlib
import math
import os
import re
import time

import yaml

SUPPORTED_API = 1
MAX_POINTS = 500
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


class PluginError(Exception):
    """The plugin file is unusable."""


class ArgError(Exception):
    """A command was called with invalid arguments."""


def parse_vars(text):
    """Parses `a=1;b=2` into a dict. Values may contain `=`."""
    result = {}
    for part in (text or "").split(";"):
        if part.strip():
            key, _, value = part.partition("=")
            result[key.strip()] = value.strip()
    return result


def load_plugin(path, variables=None):
    with open(os.path.expanduser(path), "rb") as handle:
        raw = handle.read()
    plugin = yaml.safe_load(raw)
    if not isinstance(plugin, dict):
        raise PluginError("plugin file is not a mapping")
    if plugin.get("api") != SUPPORTED_API:
        raise PluginError(f"unsupported plugin api: {plugin.get('api')} (expected {SUPPORTED_API})")
    if not plugin.get("id"):
        raise PluginError("plugin has no id")
    plugin["_sha256"] = hashlib.sha256(raw).hexdigest()
    plugin["_vars"] = variables or {}
    return plugin


def resolve_file(value, variables):
    """Resolves `$name` to a bridge variable, and expands `~`."""
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if name not in variables:
            raise PluginError(f"bridge variable '{name}' is not set")
        value = variables[name]
    return os.path.expanduser(value)


def _check_number(name, value, spec, integer):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArgError(f"{name}: expected a number")
    if not math.isfinite(value):
        raise ArgError(f"{name}: must be finite")
    if integer:
        if value != int(value):
            raise ArgError(f"{name}: expected an integer")
        value = int(value)
    if "min" in spec and value < spec["min"]:
        raise ArgError(f"{name}: below minimum {spec['min']}")
    if "max" in spec and value > spec["max"]:
        raise ArgError(f"{name}: above maximum {spec['max']}")
    return value


def _check_coordinates(name, point):
    if not isinstance(point, dict):
        raise ArgError(f"{name}: expected an object with lat and lon")
    cleaned = dict(point)
    cleaned["lat"] = _check_number(f"{name}.lat", point.get("lat"), {"min": -90, "max": 90}, False)
    cleaned["lon"] = _check_number(f"{name}.lon", point.get("lon"), {"min": -180, "max": 180}, False)
    cleaned["alt"] = _check_number(f"{name}.alt", point.get("alt", 0.0), {}, False)
    return cleaned


def validate_args(command, args):
    """Returns cleaned arguments or raises ArgError. Nothing undeclared passes through."""
    specs = command.get("args") or {}
    args = args if args is not None else {}
    if not isinstance(args, dict):
        raise ArgError("args must be an object")
    unknown = set(args) - set(specs)
    if unknown:
        raise ArgError(f"unknown arguments: {', '.join(sorted(unknown))}")

    cleaned = {}
    for name, spec in specs.items():
        if name in args:
            value = args[name]
        else:
            default = spec.get("default")
            # $config.* defaults are filled in by the GUI, the bridge knows nothing about them
            if default is None or (isinstance(default, str) and default.startswith("$")):
                raise ArgError(f"{name}: required")
            value = default

        kind = spec.get("type", "string")
        if kind == "number":
            value = _check_number(name, value, spec, False)
        elif kind == "integer":
            value = _check_number(name, value, spec, True)
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ArgError(f"{name}: expected true or false")
        elif kind == "enum":
            if value not in spec.get("values", []):
                raise ArgError(f"{name}: must be one of {spec.get('values', [])}")
        elif kind == "enums":
            if not isinstance(value, list):
                raise ArgError(f"{name}: expected a list")
            wrong = [item for item in value if item not in spec.get("values", [])]
            if wrong:
                raise ArgError(f"{name}: must be from {spec.get('values', [])}")
        elif kind == "string":
            if not isinstance(value, str):
                raise ArgError(f"{name}: expected text")
            if len(value) > spec.get("max_length", 256):
                raise ArgError(f"{name}: too long")
        elif kind == "point":
            value = _check_coordinates(name, value)
        elif kind == "points":
            if not isinstance(value, list) or not value:
                raise ArgError(f"{name}: expected a non-empty list of points")
            if len(value) > MAX_POINTS:
                raise ArgError(f"{name}: more than {MAX_POINTS} points")
            value = [_check_coordinates(f"{name}[{i}]", p) for i, p in enumerate(value)]
        else:
            raise ArgError(f"{name}: unknown argument type '{kind}'")
        cleaned[name] = value
    return cleaned


def _render_value(template, args):
    if isinstance(template, str) and template.startswith("$"):
        name = template[1:]
        if name == "now":
            now = time.time()
            return {"sec": int(now), "nanosec": int((now % 1) * 1e9)}
        if name not in args:
            raise ArgError(f"template refers to unknown value '{template}'")
        return args[name]
    if isinstance(template, dict):
        if "$each" in template:
            items = _render_value(template["$each"], args)
            return [
                render_fields(template.get("item", {}), {**args, **item}) for item in items
            ]
        return render_fields(template, args)
    if isinstance(template, list):
        return [_render_value(item, args) for item in template]
    return template


def render_fields(fields, args):
    """Turns `{a.b: $x}` into `{a: {b: <value of x>}}`, ready for set_message_fields."""
    result = {}
    for dotted, template in (fields or {}).items():
        target = result
        parts = str(dotted).split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = _render_value(template, args)
    return result


def get_path(obj, path):
    """Reads `a.b[1].c` from a message or a mapping. Raises KeyError when it is not there."""
    current = obj
    for name, index in _PATH_TOKEN.findall(path):
        try:
            if name:
                current = current[name] if isinstance(current, dict) else getattr(current, name)
            else:
                current = current[int(index)]
        except (AttributeError, IndexError, TypeError) as exc:
            raise KeyError(path) from exc
    return current


def to_json_value(value, precision=4):
    """Makes a message field safe for JSON, with fewer digits to send over the radio."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if hasattr(value, "tolist"):
        return to_json_value(value.tolist(), precision)
    if isinstance(value, (list, tuple)):
        return [to_json_value(item, precision) for item in value]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return round(number, precision) if math.isfinite(number) else None
