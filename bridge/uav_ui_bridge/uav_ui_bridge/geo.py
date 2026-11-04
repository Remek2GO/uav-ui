"""Turns messages into map objects. Types are recognised by name, so no message package is imported."""

import math

from .plugin import PluginError, get_path

EARTH_RADIUS_M = 6378137.0

_GEO_POINT_FIELDS = {
    "NavSatFix": ("latitude", "longitude", "altitude"),
    "GeoPointStamped": ("position.latitude", "position.longitude", "position.altitude"),
    "GeoPoint": ("latitude", "longitude", "altitude"),
    "GeoPoseStamped": (
        "pose.position.latitude",
        "pose.position.longitude",
        "pose.position.altitude",
    ),
    "GeoPose": ("position.latitude", "position.longitude", "position.altitude"),
}
_LOCAL_POINT_FIELDS = {
    "PoseStamped": "pose.position",
    "PointStamped": "point",
    "Pose": "position",
    "Point": "",
}


def short_type(type_name):
    return str(type_name).rsplit("/", 1)[-1]


def is_local_type(type_name):
    return short_type(type_name) in {*_LOCAL_POINT_FIELDS, "PoseArray", "Path"}


def local_to_geodetic(x, y, z, frame, origin):
    """Small-area conversion of a local position to WGS84. `origin` is (lat, lon, alt)."""
    lat0, lon0, alt0 = origin
    if frame == "ned":
        north, east, up = x, y, -z
    elif frame == "enu":
        east, north, up = x, y, z
    else:
        raise PluginError(f"unknown frame '{frame}', expected ned or enu")
    lat = lat0 + math.degrees(north / EARTH_RADIUS_M)
    lon = lon0 + math.degrees(east / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    return lat, lon, alt0 + up


def valid_coordinates(lat, lon):
    return (
        isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90.0 <= lat <= 90.0
        and -180.0 <= lon <= 180.0
        and not (lat == 0.0 and lon == 0.0)
    )


def _local_point(position, source, origin):
    if origin is None:
        return None
    return local_to_geodetic(
        float(position.x), float(position.y), float(position.z), source.get("frame", "enu"), origin
    )


def extract_points(msg, source, origin=None):
    """Returns a list of (lat, lon, alt) and whether it is a path, or ([], False) if unknown yet.

    Explicit `lat`/`lon` fields in the source win over the message type, which is how any custom
    message can be placed on the map.
    """
    if "lat" in source and "lon" in source:
        lat = float(get_path(msg, source["lat"]))
        lon = float(get_path(msg, source["lon"]))
        alt = float(get_path(msg, source["alt"])) if "alt" in source else 0.0
        return ([(lat, lon, alt)] if valid_coordinates(lat, lon) else []), False

    kind = short_type(source["type"])
    if kind in _GEO_POINT_FIELDS:
        fields = _GEO_POINT_FIELDS[kind]
        lat, lon = float(get_path(msg, fields[0])), float(get_path(msg, fields[1]))
        alt = float(get_path(msg, fields[2]))
        return ([(lat, lon, alt)] if valid_coordinates(lat, lon) else []), False
    if kind == "GeoPath":
        points = []
        for pose in msg.poses:
            position = pose.pose.position
            if valid_coordinates(position.latitude, position.longitude):
                points.append((position.latitude, position.longitude, position.altitude))
        return points, True
    if kind in _LOCAL_POINT_FIELDS:
        path = _LOCAL_POINT_FIELDS[kind]
        position = get_path(msg, path) if path else msg
        point = _local_point(position, source, origin)
        return ([point] if point else []), False
    if kind == "PoseArray":
        points = [_local_point(pose.position, source, origin) for pose in msg.poses]
        return [p for p in points if p], True
    if kind == "Path":
        points = [_local_point(pose.pose.position, source, origin) for pose in msg.poses]
        return [p for p in points if p], True
    raise PluginError(f"type {source['type']} needs explicit lat and lon fields")


def markers_from_file(data, source):
    """Reads static markers from a parsed YAML file. Entries are found by a dotted path."""
    entries = get_path(data, source["path"])
    if isinstance(entries, dict):
        items = list(entries.items())
    elif isinstance(entries, list):
        items = [(str(i), entry) for i, entry in enumerate(entries)]
    else:
        raise PluginError(f"{source['path']} is neither a mapping nor a list")

    markers = []
    for key, entry in items:
        lat = float(get_path(entry, source.get("lat", "lat")))
        lon = float(get_path(entry, source.get("lon", "lon")))
        if not valid_coordinates(lat, lon):
            continue
        if "key" in source:
            key = get_path(entry, source["key"])
        marker = {"key": str(key), "lat": lat, "lon": lon}
        if "label" in source:
            marker["label"] = str(get_path(entry, source["label"]))
        if "color" in source:
            marker["color"] = str(get_path(entry, source["color"]))
        markers.append(marker)
    return markers
