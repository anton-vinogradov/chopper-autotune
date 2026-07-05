#!/bin/bash
# Shared detached launcher for the CHOPPER_* macros. RUN_SHELL_COMMAND must return
# at once: it holds Klipper's gcode processor, and the tool both drives the axis and
# streams the accelerometer over that same connection (a synchronous run starves the
# reactor and hangs; save/analyze additionally send gcode back through Moonraker).
# Progress goes to the display (M117) and console; full output to the log.
cmd=$1
shift
here=$(dirname "$(realpath "$0")")
log=~/printer_data/config/chopper-autotune/$cmd.log
mkdir -p "$(dirname "$log")"
setsid "$here/.venv/bin/chopper-autotune" "$cmd" "$@" > "$log" 2>&1 < /dev/null &
echo "chopper-autotune $cmd started (PID $!); progress on the display/console, log: $log"
