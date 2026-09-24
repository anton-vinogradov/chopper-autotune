#!/bin/bash
# Shared launcher for the CHOPPER_* macros. RUN_SHELL_COMMAND must return at once:
# it holds Klipper's gcode processor, and the tool both drives the axis and streams
# the accelerometer over that same connection (a synchronous run starves the reactor
# and hangs; save/analyze additionally send gcode back through Moonraker).
# Progress goes to the display (M117) and console; full output to the log.
# `run.sh --sync CMD ...` runs in the foreground instead (status, plain analyze).
# gcode_shell_command ignores exit codes: a failure only reaches the user as a
# printed line, so every failure here prints one.
sync=
if [ "$1" = "--sync" ]; then
    sync=1
    shift
fi
cmd=$1
shift
here=$(dirname "$(realpath "$0")")
bin=$here/.venv/bin/chopper-autotune
if [ ! -x "$bin" ]; then
    echo "ERROR: chopper-autotune is not installed ($bin is missing). Run: bash $here/install.sh"
    exit 1
fi
if [ -n "$sync" ]; then
    # the tool must not report failures through Klipper while this macro holds the
    # gcode queue: the output goes straight to the console instead
    CHOPPER_SYNC=1 exec "$bin" "$cmd" "$@"
fi
log=~/printer_data/config/chopper-autotune/$cmd.log
mkdir -p "$(dirname "$log")"
# -w: $! stays the tool's lifetime even if setsid has to fork
setsid -w "$bin" "$cmd" "$@" > "$log" 2>&1 < /dev/null &
pid=$!
# a start-up failure (bad parameter, broken environment) exits within a second and
# would otherwise be visible only in the log
sleep 1
if kill -0 "$pid" 2>/dev/null; then
    echo "chopper-autotune $cmd started (PID $pid); progress on the display/console, log: $log"
    exit 0
fi
wait "$pid"
status=$?
if [ "$status" -eq 0 ]; then
    echo "chopper-autotune $cmd finished; log: $log"
else
    echo "ERROR: chopper-autotune $cmd exited with status $status; log: $log"
    if grep -qE '^(ModuleNotFoundError|ImportError)' "$log"; then
        echo "The Python environment is broken (e.g. after an OS upgrade). Run: bash $here/install.sh"
    fi
fi
tail -n 5 "$log"
exit "$status"
