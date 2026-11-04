import pytest

from uav_ui_bridge import geo
from uav_ui_bridge.plugin import (
    ArgError,
    get_path,
    parse_vars,
    render_fields,
    to_json_value,
    validate_args,
)


def command(**args):
    return {"args": args}


def test_unknown_argument_is_rejected():
    with pytest.raises(ArgError, match="unknown"):
        validate_args(command(ml={"type": "number"}), {"ml": 1, "extra": 2})


def test_number_range_is_enforced():
    spec = command(ml={"type": "number", "min": 1, "max": 2000})
    assert validate_args(spec, {"ml": 500}) == {"ml": 500}
    for bad in (0, 2001, "500", True, float("nan"), float("inf")):
        with pytest.raises(ArgError):
            validate_args(spec, {"ml": bad})


def test_enum_rejects_values_outside_the_list():
    spec = command(color={"type": "enum", "values": ["red", "green"]})
    assert validate_args(spec, {"color": "red"}) == {"color": "red"}
    with pytest.raises(ArgError):
        validate_args(spec, {"color": "purple"})


def test_literal_default_is_used_and_config_default_is_not():
    assert validate_args(command(a={"type": "integer", "default": 3}), {}) == {"a": 3}
    with pytest.raises(ArgError, match="required"):
        validate_args(command(a={"type": "number", "default": "$config.x"}), {})


def test_points_are_validated_and_altitude_defaults_to_zero():
    spec = command(points={"type": "points"})
    assert validate_args(spec, {"points": [{"lat": 50, "lon": 19}]})["points"][0]["alt"] == 0.0
    for bad in ([], [{"lat": 91, "lon": 19}], [{"lat": 50}], "x", [{"lat": 50, "lon": 19}] * 501):
        with pytest.raises(ArgError):
            validate_args(spec, {"points": bad})


def test_enums_take_a_list_of_allowed_values():
    spec = command(colors={"type": "enums", "values": ["red", "blue"], "default": []})
    assert validate_args(spec, {}) == {"colors": []}
    assert validate_args(spec, {"colors": ["red", "blue"]}) == {"colors": ["red", "blue"]}
    for bad in (["green"], "red", ["red", 1]):
        with pytest.raises(ArgError):
            validate_args(spec, {"colors": bad})


def test_path_from_file_makes_a_route():
    data = {"waypoints": [{"lat": 54.0, "lon": 22.0, "alt": 10.0}, {"lat": 54.1, "lon": 22.1, "alt": 5}]}
    source = {"path": "waypoints", "lat": "lat", "lon": "lon", "alt": "alt"}
    assert geo.path_from_file(data, source) == [[54.0, 22.0, 10.0], [54.1, 22.1, 5.0]]
    assert geo.path_from_file({"waypoints": []}, source) == []


def test_render_fields_nests_dotted_names():
    rendered = render_fields({"header.frame_id": "map", "pose.x": "$a", "n": 2}, {"a": 1.5})
    assert rendered == {"header": {"frame_id": "map"}, "pose": {"x": 1.5}, "n": 2}


def test_render_fields_each_builds_a_list():
    fields = {"poses": {"$each": "$points", "item": {"pose.position.latitude": "$lat"}}}
    args = {"points": [{"lat": 1.0}, {"lat": 2.0}]}
    assert render_fields(fields, args) == {
        "poses": [
            {"pose": {"position": {"latitude": 1.0}}},
            {"pose": {"position": {"latitude": 2.0}}},
        ]
    }


def test_render_fields_stamp_now():
    stamp = render_fields({"stamp": "$now"}, {})["stamp"]
    assert stamp["sec"] > 0 and 0 <= stamp["nanosec"] < 1_000_000_000


def test_render_fields_unknown_value_fails():
    with pytest.raises(ArgError):
        render_fields({"a": "$missing"}, {})


def test_get_path():
    data = {"a": {"b": [10, {"c": 5}]}}
    assert get_path(data, "a.b[0]") == 10
    assert get_path(data, "a.b[1].c") == 5
    with pytest.raises(KeyError):
        get_path(data, "a.b[9]")
    with pytest.raises(KeyError):
        get_path(data, "a.missing")


def test_json_value_drops_non_finite_numbers_and_rounds():
    assert to_json_value(float("nan")) is None
    assert to_json_value(1.23456789, 3) == 1.235
    assert to_json_value([1.0, float("inf")]) == [1.0, None]


def test_parse_vars_keeps_equals_in_value():
    assert parse_vars("a=1; b=x=y") == {"a": "1", "b": "x=y"}
    assert parse_vars("") == {}


def test_local_to_geodetic_ned_and_enu_agree():
    origin = (50.0, 19.0, 200.0)
    ned = geo.local_to_geodetic(100.0, 50.0, -10.0, "ned", origin)
    enu = geo.local_to_geodetic(50.0, 100.0, 10.0, "enu", origin)
    assert ned == pytest.approx(enu)
    assert ned[0] == pytest.approx(50.0 + 100.0 / 111319.5, abs=1e-5)
    assert ned[2] == pytest.approx(210.0)


def test_valid_coordinates_rejects_px4_null_island_and_nan():
    assert geo.valid_coordinates(50.0, 19.0)
    assert not geo.valid_coordinates(0.0, 0.0)
    assert not geo.valid_coordinates(float("nan"), 19.0)
    assert not geo.valid_coordinates(91.0, 19.0)


def test_markers_from_file_like_hydrolab_navigation():
    data = {
        "UAV_control_BT": {
            "ros__parameters": {
                "pools": {
                    "red": {"x": 50.05, "y": 19.91, "color": "red"},
                    "bad": {"x": 0.0, "y": 0.0, "color": "green"},
                }
            }
        }
    }
    source = {
        "path": "UAV_control_BT.ros__parameters.pools",
        "lat": "x",
        "lon": "y",
        "color": "color",
        "label": "color",
    }
    assert geo.markers_from_file(data, source) == [
        {"key": "red", "lat": 50.05, "lon": 19.91, "label": "red", "color": "red"}
    ]


def test_markers_from_list_use_the_key_field():
    data = {"places": [{"name": "hala", "lat": 50.0, "lon": 19.0}]}
    source = {"path": "places", "key": "name"}
    assert geo.markers_from_file(data, source)[0]["key"] == "hala"


def test_explicit_fields_place_any_message_on_the_map():
    points, is_path = geo.extract_points(
        {"position": {"la": 50.0, "lo": 19.0}},
        {"type": "my_msgs/Thing", "lat": "position.la", "lon": "position.lo"},
    )
    assert points == [(50.0, 19.0, 0.0)] and is_path is False


def test_unknown_type_without_fields_fails_clearly():
    with pytest.raises(Exception, match="explicit lat and lon"):
        geo.extract_points({}, {"type": "my_msgs/Thing"})
