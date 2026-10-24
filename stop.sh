#!/bin/bash
# Stop the running job that moves the printer. Before it exits, the job undoes its
# changes to the driver registers and printer settings. CHOPPER_EXTRUDER also switches
# the hotend heater off, even if it was on before the run. A job that moved X and Y
# then homes them again. CHOPPER_BELTS without SWEEP=1 switches the X and Y motors off
# instead of homing: you move the head by hand next.
#
# SIGTERM: the tool's handler turns it into an exit that runs these steps. The [c]
# bracket keeps the pattern from matching this script.
if pkill -TERM -f "[c]hopper-autotune (tune|collect|find-speed|map|belts|demo|current|envelope|extruder)"; then
    echo "chopper-autotune: stop signal sent"
else
    echo "chopper-autotune: nothing to stop"
fi
