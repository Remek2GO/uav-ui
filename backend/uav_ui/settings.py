"""Paths and addresses, taken from the environment so that Docker and tests can differ."""

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _path_list(value):
    return [Path(part).expanduser() for part in (value or "").split(":") if part.strip()]


@dataclass
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("UAV_UI_DATA", REPO_ROOT / "data"))
    )
    schema_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get("UAV_UI_SCHEMA", REPO_ROOT / "spec" / "plugin.schema.json")
        )
    )
    static_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("UAV_UI_STATIC", REPO_ROOT / "frontend" / "dist")
        )
    )
    plugin_dirs: list = field(
        default_factory=lambda: _path_list(os.environ.get("UAV_UI_PLUGIN_DIRS"))
    )
    whep_url: str = field(
        default_factory=lambda: os.environ.get("UAV_UI_WHEP_URL", "http://127.0.0.1:8889")
    )
    video_sink_port: int = field(
        default_factory=lambda: int(os.environ.get("UAV_UI_VIDEO_SINK_PORT", "5611"))
    )
    allowed_hosts: set = field(
        default_factory=lambda: set(
            os.environ.get("UAV_UI_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
        )
    )
