#!/bin/bash
# Plain analysis runs synchronously so the table lands in the web console. But
# APPLY/SAVE send gcode back through Moonraker while RUN_SHELL_COMMAND still holds
# Klipper's gcode queue — a deadlock until the HTTP timeout — so those run detached.
here=$(dirname "$(realpath "$0")")
case " $* " in
    *" APPLY="*|*" SAVE="*|*" --apply"*|*" --save"*)
        exec "$here/run.sh" analyze "$@"
        ;;
    *)
        exec "$here/.venv/bin/chopper-autotune" analyze "$@"
        ;;
esac
