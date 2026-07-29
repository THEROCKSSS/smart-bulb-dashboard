#!/usr/bin/env bash
# One-liner "movie night" alias: dims the living-room bulb and applies the
# dashboard's built-in movie_night scene (see backend/scenes_presets.py).
#
# Install as a shell alias/function, e.g. in ~/.bashrc:
#   alias movie-night='/path/to/cli/examples/movie_night.sh bulb-1'
#
# Usage: movie_night.sh <device-id> [host] [port]
set -euo pipefail

DEVICE="${1:?usage: movie_night.sh <device-id> [host] [port]}"
HOST="${2:-127.0.0.1}"
PORT="${3:-8500}"
BULBCTL="$(dirname "$0")/../bulbctl.py"

python3 "${BULBCTL}" --host "${HOST}" --port "${PORT}" scene "${DEVICE}" movie_night
