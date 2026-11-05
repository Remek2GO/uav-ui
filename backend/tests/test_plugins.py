import io
import stat
import zipfile

import pytest

from uav_ui.plugins import PluginValidationError

MINIMAL = b"api: 1\nid: demo\nname: Demo\n"


def make_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_example_plugin_is_valid(store, example_dir):
    plugin = store.validate((example_dir / "plugin.yaml").read_bytes(), example_dir)
    assert plugin["id"] == "example"


def test_install_and_remove(store):
    assert store.install_zip(make_zip({"plugin.yaml": MINIMAL}))["id"] == "demo"
    assert store.get("demo")["_origin"] == "uploaded"
    assert store.remove("demo") is True
    assert store.get("demo") is None


def test_data_folder_cleared_while_running(store):
    import shutil

    store.install_zip(make_zip({"plugin.yaml": MINIMAL}))
    shutil.rmtree(store.install_dir)
    shutil.rmtree(store.config_dir)
    assert store.list() == ({}, [])
    assert store.install_zip(make_zip({"plugin.yaml": MINIMAL}))["id"] == "demo"
    plugin = store.get("demo")
    assert store.config_values(plugin) == {}
    assert store.save_config(plugin, {}) == {}


def test_plain_yaml_is_installed_without_a_zip(store):
    assert store.install(MINIMAL)["id"] == "demo"
    assert store.get("demo")["_origin"] == "uploaded"
    assert store.install(MINIMAL + b"version: '2'\n")["id"] == "demo"
    assert store.get("demo")["version"] == "2"


def test_zip_still_works_through_the_same_call(store):
    assert store.install(make_zip({"plugin.yaml": MINIMAL}))["id"] == "demo"


@pytest.mark.parametrize("data", [b"not: [valid", b"api: 1\nid: demo\n", b"\x00\x01binary"])
def test_bad_yaml_is_rejected_and_keeps_the_previous_version(store, data):
    store.install(MINIMAL + b"version: '1'\n")
    with pytest.raises(PluginValidationError):
        store.install(data)
    assert store.get("demo")["version"] == "1"


def test_yaml_plugin_with_a_custom_panel_needs_a_zip(store):
    text = MINIMAL + b"ui: {panels: [{id: p, title: P, type: custom, entry: x.html}]}\n"
    with pytest.raises(PluginValidationError, match="missing"):
        store.install(text)


def test_zip_with_one_wrapping_folder(store):
    assert store.install_zip(make_zip({"demo-1.0/plugin.yaml": MINIMAL}))["id"] == "demo"


@pytest.mark.parametrize(
    "name", ["../evil.yaml", "/abs/evil.yaml", "a/../../evil.yaml", "a\\..\\evil.yaml"]
)
def test_path_traversal_is_rejected(store, name):
    with pytest.raises(PluginValidationError, match="unsafe"):
        store.install_zip(make_zip({"plugin.yaml": MINIMAL, name: b"x"}))


def test_executable_file_types_are_rejected(store):
    with pytest.raises(PluginValidationError, match="not allowed"):
        store.install_zip(make_zip({"plugin.yaml": MINIMAL, "run.py": b"x"}))


def test_symlink_is_rejected(store):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.yaml", MINIMAL)
        link = zipfile.ZipInfo("link.yaml")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    with pytest.raises(PluginValidationError, match="symbolic link"):
        store.install_zip(buffer.getvalue())


def test_too_many_files_are_rejected(store):
    files = {"plugin.yaml": MINIMAL, **{f"{i}.txt": b"x" for i in range(70)}}
    with pytest.raises(PluginValidationError, match="more than"):
        store.install_zip(make_zip(files))


def test_oversized_unpacked_content_is_rejected(store):
    files = {"plugin.yaml": MINIMAL, "big.txt": b"0" * (21 * 1024 * 1024)}
    with pytest.raises(PluginValidationError, match="too large"):
        store.install_zip(make_zip(files))


def test_not_a_zip(store):
    with pytest.raises(PluginValidationError, match="not a zip"):
        store.install_zip(b"plain text")


def test_missing_manifest(store):
    with pytest.raises(PluginValidationError, match="plugin.yaml is missing"):
        store.install_zip(make_zip({"readme.txt": b"x"}))


def test_failed_install_keeps_previous_version(store):
    store.install_zip(make_zip({"plugin.yaml": MINIMAL + b"version: '1'\n"}))
    with pytest.raises(PluginValidationError):
        store.install_zip(make_zip({"plugin.yaml": MINIMAL + b"unknown_field: 1\n"}))
    assert store.get("demo")["version"] == "1"


@pytest.mark.parametrize(
    "text, message",
    [
        ("api: 2\nid: a\nname: A\n", "api"),
        ("api: 1\nid: 'Bad/Id'\nname: A\n", "id"),
        ("api: 1\nid: a\nname: A\nui: {panels: [{id: p, title: P, widgets: [{type: button, command: nope}]}]}\n", "unknown command"),
        ("api: 1\nid: a\nname: A\nui: {panels: [{id: p, title: P, widgets: [{type: value, state: nope}]}]}\n", "unknown state"),
        ("api: 1\nid: a\nname: A\nui: {panels: [{id: p, title: P, widgets: [{type: image, image: nope}]}]}\n", "unknown image"),
        ("api: 1\nid: a\nname: A\nui: {map_tools: [{id: t, kind: point, label: T, command: nope}]}\n", "unknown command"),
        ("api: 1\nid: a\nname: A\ncommands: {c: {label: C, args: {x: {type: number, default: $config.nope}}, ros: {topic: /t, type: std_msgs/Empty}}}\n", "unknown"),
        ("api: 1\nid: a\nname: A\ncommands: {c: {label: C}}\n", "commands"),
        ("api: 1\nid: a\nname: A\ncommands: {c: {label: C, ros: {topic: /t, service: /s, type: x/Y}}}\n", "commands"),
    ],
)
def test_invalid_plugins_are_rejected(store, text, message):
    with pytest.raises(PluginValidationError, match=message):
        store.validate(text.encode())


@pytest.mark.parametrize("command", [400, 176, 22, 300, 185, 192, 246])
def test_flight_takeover_mavlink_commands_are_forbidden(store, command):
    text = f"api: 1\nid: a\nname: A\ncommands: {{c: {{label: C, mavlink: {{command: {command}}}}}}}\n"
    with pytest.raises(PluginValidationError, match="not allowed"):
        store.validate(text.encode())


def test_missing_custom_panel_file_is_rejected(store, tmp_path):
    text = b"api: 1\nid: a\nname: A\nui: {panels: [{id: p, title: P, type: custom, entry: x.html}]}\n"
    with pytest.raises(PluginValidationError, match="missing"):
        store.validate(text, tmp_path)


def test_invalid_plugin_in_a_directory_is_reported_not_hidden(tmp_path, example_dir):
    from uav_ui.plugins import PluginStore
    from pathlib import Path

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "plugin.yaml").write_text("api: 99\n")
    repo = Path(__file__).resolve().parents[2]
    store = PluginStore(tmp_path / "data", repo / "spec" / "plugin.schema.json", [bad, example_dir])
    plugins, problems = store.list()
    assert "example" in plugins and plugins["example"]["_origin"] == "directory"
    assert len(problems) == 1 and "bad" in problems[0]["path"]


def test_config_defaults_and_saving(store):
    text = b"api: 1\nid: demo\nname: D\nconfig: {ml: {type: number, default: 5, min: 1, max: 9}, c: {type: enum, values: [a, b], default: a}}\n"
    store.install_zip(make_zip({"plugin.yaml": text}))
    plugin = store.get("demo")
    assert store.config_values(plugin) == {"ml": 5, "c": "a"}
    store.save_config(plugin, {"ml": 7})
    assert store.config_values(plugin) == {"ml": 7, "c": "a"}
    for bad in ({"ml": 10}, {"ml": "x"}, {"c": "z"}, {"nope": 1}):
        with pytest.raises(PluginValidationError):
            store.save_config(plugin, bad)
    assert store.remove("demo")
    store.install_zip(make_zip({"plugin.yaml": text}))
    assert store.config_values(store.get("demo"))["ml"] == 5


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden", "", "x" * 200])
def test_asset_names_are_checked(store, name):
    store.install_zip(make_zip({"plugin.yaml": MINIMAL, "a.txt": b"x"}))
    assert store.asset("demo", name) is None
    assert store.asset("demo", "a.txt") is not None
