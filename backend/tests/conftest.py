import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uav_ui.plugins import PluginStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path):
    return PluginStore(tmp_path, REPO / "spec" / "plugin.schema.json")


@pytest.fixture
def example_dir():
    return REPO / "plugins" / "example"
