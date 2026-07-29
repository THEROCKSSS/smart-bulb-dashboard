#!/usr/bin/env bash
# Sunrise-style wake-up using only bulbctl, driven by cron.
#
# Ramps the bedroom bulb from very dim warm white up to full bright
# daylight white over ~10 minutes, then leaves it there. Simpler than the
# backend's built-in /timers/wake endpoint (which does this server-side
# with proper fades) -- this is here to show bulbctl is fully scriptable
# for anyone who wants the logic client-side instead (e.g. driving several
# independent dashboards from one cron host).
#
# Install (crontab -e), 7:00am on weekdays:
#   0 7 * * 1-5 /path/to/cli/examples/sunrise_cron.sh bulb-1 >> /tmp/bulbctl-sunrise.log 2>&1
#
# Usage: sunrise_cron.sh <device-id> [host] [port]
set -euo pipefail

DEVICE="${1:?usage: sunrise_cron.sh <device-id> [host] [port]}"
HOST="${2:-127.0.0.1}"
PORT="${3:-8500}"
BULBCTL="$(dirname "$0")/../bulbctl.py"

STEPS=10
SLEEP_SECONDS=60  # 10 steps * 60s = ~10 minute ramp

echo "[$(date)] starting sunrise ramp on ${DEVICE} via ${HOST}:${PORT}"

python3 "${BULBCTL}" --host "${HOST}" --port "${PORT}" on "${DEVICE}"
python3 "${BULBCTL}" --host "${HOST}" --port "${PORT}" color "${DEVICE}" ffb060  # warm amber start
python3 "${BULBCTL}" --host "${HOST}" --port "${PORT}" brightness "${DEVICE}" 2

for step in $(seq 1 "${STEPS}"); do
    brightness=$(( step * 100 / STEPS ))
    python3 "${BULBCTL}" --host "${HOST}" --port "${PORT}" brightness "${DEVICE}" "${brightness}"
    if [ "$step" -eq "$STEPS" ]; then
        python3 "${BULBCTL}" --host "${HOST}" --port "${PORT}" color "${DEVICE}" ffffff  # daylight white
    fi
    sleep "${SLEEP_SECONDS}"
done

echo "[$(date)] sunrise ramp complete on ${DEVICE}"
