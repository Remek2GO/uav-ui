"""Plugin store: validates plugin files, installs zips, keeps per-plugin configuration."""

import hashlib
import io
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import jsonschema
import yaml

ALLOWED_SUFFIXES = {".yaml", ".yml", ".html", ".md", ".txt", ".png", ".svg", ".json"}
MAX_ZIP_FILES = 64
MAX_UNPACKED_BYTES = 20 * 1024 * 1024

# Commands that start or take over a flight stay with the RC and QGC. Plugins cannot declare them:
# MAV_CMD_COMPONENT_ARM_DISARM, DO_SET_MODE, NAV_TAKEOFF, MISSION_START, DO_FLIGHTTERMINATION,
# DO_REPOSITION, PREFLIGHT_REBOOT_SHUTDOWN.
FORBIDDEN_MAVLINK_COMMANDS = {400, 176, 22, 300, 185, 192, 246}


class PluginValidationError(Exception):
    pass


def _reference_errors(plugin):
    """What the JSON schema cannot see: references between sections of the file."""
    errors = []
    commands = plugin.get("commands") or {}
    states = plugin.get("state") or {}
    images = plugin.get("images") or {}

    for name, command in commands.items():
        mavlink = command.get("mavlink")
        if mavlink and mavlink["command"] in FORBIDDEN_MAVLINK_COMMANDS:
            errors.append(f"commands.{name}: MAVLink command {mavlink['command']} is not allowed")
        for arg, spec in (command.get("args") or {}).items():
            default = spec.get("default")
            if isinstance(default, str) and default.startswith("$config."):
                if default[len("$config.") :] not in (plugin.get("config") or {}):
                    errors.append(f"commands.{name}.args.{arg}: unknown {default}")

    ui = plugin.get("ui") or {}
    panel_ids = set()
    for panel in ui.get("panels") or []:
        where = f"ui.panel {panel['id']}"
        if panel["id"] in panel_ids:
            errors.append(f"{where}: duplicate id")
        panel_ids.add(panel["id"])
        if panel.get("type") == "custom":
            if not panel.get("entry"):
                errors.append(f"{where}: a custom panel needs entry")
            elif not str(panel["entry"]).endswith(".html"):
                errors.append(f"{where}: entry must be an .html file")
        for widget in panel.get("widgets") or []:
            kind = widget["type"]
            if kind in ("button", "form"):
                if widget.get("command") not in commands:
                    errors.append(f"{where}: unknown command '{widget.get('command')}'")
            elif kind in ("value", "gauge", "status"):
                if widget.get("state") not in states:
                    errors.append(f"{where}: unknown state '{widget.get('state')}'")
            elif kind in ("chart", "table"):
                for key in widget.get("states") or []:
                    if key not in states:
                        errors.append(f"{where}: unknown state '{key}'")
                if not widget.get("states"):
                    errors.append(f"{where}: {kind} needs states")
            elif kind == "image" and widget.get("image") not in images:
                errors.append(f"{where}: unknown image '{widget.get('image')}'")

    for tool in ui.get("map_tools") or []:
        if tool.get("command") and tool["command"] not in commands:
            errors.append(f"ui.map_tools {tool['id']}: unknown command '{tool['command']}'")

    layer_ids = [layer["id"] for layer in (plugin.get("map") or {}).get("layers") or []]
    if len(layer_ids) != len(set(layer_ids)):
        errors.append("map.layers: duplicate id")
    return errors


class PluginStore:
    def __init__(self, data_dir: Path, schema_file: Path, extra_dirs=()):
        self.install_dir = data_dir / "plugins"
        self.config_dir = data_dir / "plugin-config"
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.schema = json.loads(schema_file.read_text(encoding="utf-8"))
        self.extra_dirs = [Path(path) for path in extra_dirs]

    def validate(self, raw: bytes, directory: Path | None = None) -> dict:
        try:
            plugin = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise PluginValidationError(f"plugin.yaml is not valid YAML: {exc}") from exc
        try:
            jsonschema.validate(plugin, self.schema)
        except jsonschema.ValidationError as exc:
            location = "/".join(str(part) for part in exc.absolute_path) or "(root)"
            raise PluginValidationError(f"{location}: {exc.message}") from exc
        errors = _reference_errors(plugin)
        if directory is not None:
            for panel in (plugin.get("ui") or {}).get("panels") or []:
                if panel.get("type") == "custom" and panel.get("entry"):
                    if not _is_safe_name(panel["entry"]) or not (directory / panel["entry"]).is_file():
                        errors.append(f"ui.panel {panel['id']}: file '{panel['entry']}' is missing")
        if errors:
            raise PluginValidationError("; ".join(errors))
        return plugin

    def _directories(self):
        # Someone may clear the data folder while the app runs.
        self.install_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.install_dir.iterdir()):
            if path.is_dir():
                yield path, "uploaded"
        for path in self.extra_dirs:
            yield path, "directory"

    def list(self):
        """Valid plugins plus the errors of invalid ones: a bad file must be visible, not silent."""
        plugins, problems = {}, []
        for directory, origin in self._directories():
            manifest = directory / "plugin.yaml"
            try:
                raw = manifest.read_bytes()
                plugin = self.validate(raw, directory)
            except (OSError, PluginValidationError) as exc:
                problems.append({"path": str(directory), "error": str(exc)})
                continue
            plugin["_sha256"] = hashlib.sha256(raw).hexdigest()
            plugin["_origin"] = origin
            plugin["_dir"] = str(directory)
            plugins[plugin["id"]] = plugin
        return plugins, problems

    def get(self, plugin_id):
        return self.list()[0].get(plugin_id)

    def install_zip(self, data: bytes) -> dict:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise PluginValidationError("this is not a zip file") from exc

        with archive, tempfile.TemporaryDirectory() as temp:
            members = [m for m in archive.infolist() if not m.is_dir()]
            if len(members) > MAX_ZIP_FILES:
                raise PluginValidationError(f"more than {MAX_ZIP_FILES} files")
            if sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
                raise PluginValidationError("unpacked plugin is too large")

            names = [_checked_member(m) for m in members]
            prefix = _common_prefix(names)
            root = Path(temp) / "plugin"
            root.mkdir()
            for member, name in zip(members, names):
                relative = PurePosixPath(*name.parts[prefix:])
                target = root.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, open(target, "wb") as sink:
                    shutil.copyfileobj(source, sink, 64 * 1024)
                if target.stat().st_size != member.file_size:
                    raise PluginValidationError(f"{name}: size differs from the zip header")

            manifest = root / "plugin.yaml"
            if not manifest.is_file():
                raise PluginValidationError("plugin.yaml is missing from the zip")
            return self._install(root)

    def install_yaml(self, data: bytes) -> dict:
        """A plugin that is a single plugin.yaml, the usual case: no zip needed."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugin"
            root.mkdir()
            (root / "plugin.yaml").write_bytes(data)
            return self._install(root)

    def install(self, data: bytes) -> dict:
        """A zip, or the plugin.yaml itself."""
        if data[:4] == b"PK\x03\x04":
            return self.install_zip(data)
        return self.install_yaml(data)

    def _install(self, root: Path) -> dict:
        plugin = self.validate((root / "plugin.yaml").read_bytes(), root)
        destination = self.install_dir / plugin["id"]
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(root, destination)
        return plugin

    def remove(self, plugin_id) -> bool:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", plugin_id):
            return False
        destination = self.install_dir / plugin_id
        if not destination.is_dir():
            return False
        shutil.rmtree(destination)
        config = self.config_dir / f"{plugin_id}.json"
        config.unlink(missing_ok=True)
        return True

    def asset(self, plugin_id, name) -> Path | None:
        plugin = self.get(plugin_id)
        if plugin is None or not _is_safe_name(name):
            return None
        path = Path(plugin["_dir"]) / name
        return path if path.is_file() else None

    # ------------------------------------------------------------ configuration

    def config_values(self, plugin) -> dict:
        values = {key: spec.get("default") for key, spec in (plugin.get("config") or {}).items()}
        path = self.config_dir / f"{plugin['id']}.json"
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        values.update({key: value for key, value in saved.items() if key in values})
        return values

    def save_config(self, plugin, values: dict) -> dict:
        specs = plugin.get("config") or {}
        unknown = set(values) - set(specs)
        if unknown:
            raise PluginValidationError(f"unknown settings: {', '.join(sorted(unknown))}")
        for key, value in values.items():
            _check_config_value(key, value, specs[key])
        merged = {**self.config_values(plugin), **values}
        self.config_dir.mkdir(parents=True, exist_ok=True)
        path = self.config_dir / f"{plugin['id']}.json"
        path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        return merged


def _check_config_value(key, value, spec):
    kind = spec.get("type", "string")
    if kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PluginValidationError(f"{key}: expected a number")
        if kind == "integer" and value != int(value):
            raise PluginValidationError(f"{key}: expected an integer")
        if "min" in spec and value < spec["min"]:
            raise PluginValidationError(f"{key}: below minimum {spec['min']}")
        if "max" in spec and value > spec["max"]:
            raise PluginValidationError(f"{key}: above maximum {spec['max']}")
    elif kind == "boolean" and not isinstance(value, bool):
        raise PluginValidationError(f"{key}: expected true or false")
    elif kind == "enum" and value not in spec.get("values", []):
        raise PluginValidationError(f"{key}: must be one of {spec.get('values', [])}")
    elif kind == "string" and not isinstance(value, str):
        raise PluginValidationError(f"{key}: expected text")


def _is_safe_name(name) -> bool:
    """Plain file name in the plugin directory: no separators, no hidden files."""
    return (
        isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is not None
        and ".." not in name
    )


def _checked_member(member: zipfile.ZipInfo) -> PurePosixPath:
    name = PurePosixPath(member.filename)
    if name.is_absolute() or ".." in name.parts or "\\" in member.filename:
        raise PluginValidationError(f"unsafe path in zip: {member.filename}")
    # The upper bits of external_attr hold the Unix mode; 0o120000 is a symbolic link.
    if (member.external_attr >> 16) & 0o170000 == 0o120000:
        raise PluginValidationError(f"symbolic link in zip: {member.filename}")
    if member.flag_bits & 0x1:
        raise PluginValidationError(f"encrypted file in zip: {member.filename}")
    if name.suffix.lower() not in ALLOWED_SUFFIXES:
        raise PluginValidationError(f"file type not allowed in a plugin: {member.filename}")
    return name


def _common_prefix(names) -> int:
    """Zips usually wrap everything in one folder. Returns how many leading parts to drop."""
    if names and all(len(name.parts) > 1 for name in names):
        first = names[0].parts[0]
        if all(name.parts[0] == first for name in names):
            return 1
    return 0
