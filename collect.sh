#!/bin/bash
# Launched by a gcode_shell_command macro. Detach so RUN_SHELL_COMMAND returns
# at once and does not hold Klipper's gcode processor while we drive the axis and
# stream the accelerometer over the same connection (that starves and hangs the
# run). Progress goes to the display (M117) and console (M118); full output to the log.
here=$(dirname "$(realpath "$0")")
log=~/printer_data/config/chopper-autotune/collect.log
setsid "$here/.venv/bin/chopper-autotune" collect --yes "$@" > "$log" 2>&1 < /dev/null &
echo "chopper-autotune collect started (PID $!); progress on the display/console, log: $log"
