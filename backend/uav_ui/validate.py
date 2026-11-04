"""Checks a plugin without a drone: python -m uav_ui.validate <directory with plugin.yaml>..."""

import sys
import tempfile
from pathlib import Path

from .plugins import PluginStore, PluginValidationError
from .settings import Settings


def main(paths):
    if not paths:
        print(__doc__)
        return 2
    failed = False
    with tempfile.TemporaryDirectory() as temp:
        store = PluginStore(Path(temp), Settings().schema_file)
        for path in paths:
            directory = Path(path)
            try:
                plugin = store.validate((directory / "plugin.yaml").read_bytes(), directory)
            except (OSError, PluginValidationError) as exc:
                print(f"{path}: BAD: {exc}")
                failed = True
                continue
            commands = len(plugin.get("commands") or {})
            print(f"{path}: OK: {plugin['id']} {plugin.get('version', '')}, {commands} commands")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
