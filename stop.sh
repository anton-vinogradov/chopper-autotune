#!/bin/bash
# Stop a running chopper-autotune motion job (tune/collect/find-speed/map/belts/demo/current/envelope/extruder).
# SIGTERM so the tool's handler restores registers, spreadCycle, heaters and re-homes
# before exiting. The [c] bracket keeps the pattern from matching this script.
if pkill -TERM -f "[c]hopper-autotune (tune|collect|find-speed|map|belts|demo|current|envelope|extruder)"; then
    echo "chopper-autotune: stop signal sent"
else
    echo "chopper-autotune: nothing to stop"
fi
