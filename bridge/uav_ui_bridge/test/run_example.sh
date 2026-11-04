#!/bin/bash
# Runs the bridge with plugins/example and a node that feeds it, without a drone.
# Usage (inside a ROS 2 environment with this package built):
#   run_example.sh <path to plugins/example> [bind address] [port]
# Stop with Ctrl-C.
set -eu

PLUGIN_DIR=$(realpath "$1")
ADDRESS=${2:-127.0.0.1}
PORT=${3:-8765}
HERE=$(dirname "$(realpath "$0")")
MARKERS=$(mktemp --suffix=.yaml)

cat > "$MARKERS" <<'EOF'
places:
  - {name: hala, lat: 50.0604, lon: 19.9163, color: orange}
  - {name: start, lat: 50.0592, lon: 19.9181, color: cyan}
EOF

python3 "$HERE/example_world.py" &
WORLD=$!
trap 'kill $WORLD 2>/dev/null || true; rm -f "$MARKERS"' EXIT

ros2 run uav_ui_bridge bridge --ros-args \
  -p plugin_file:="$PLUGIN_DIR/plugin.yaml" \
  -p bind_address:="$ADDRESS" \
  -p port:="$PORT" \
  -p plugin_vars:="markers_file=$MARKERS"
