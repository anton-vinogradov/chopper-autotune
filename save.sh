#!/bin/bash
# Launched by a gcode_shell_command macro. Detach so RUN_SHELL_COMMAND returns at
# once: this writes the config and restarts Klipper (via Moonraker), which would
# otherwise fight the macro's own gcode processor. Full output to the log.
here=$(dirname "$(realpath "$0")")
log=~/printer_data/config/chopper-autotune/save.log
setsid "$here/.venv/bin/chopper-autotune" save "$@" > "$log" 2>&1 < /dev/null &
echo "chopper-autotune save started (PID $!); it writes the config and restarts Klipper, log: $log"
