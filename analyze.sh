#!/bin/bash
# Plain analysis runs synchronously so the table lands in the web console. But
# APPLY/SAVE send gcode back through Moonraker while RUN_SHELL_COMMAND still holds
# Klipper's gcode queue — a deadlock until the HTTP timeout — so those run detached.
here=$(dirname "$(realpath "$0")")
# the values the CLI takes for yes (cli.TRUE_VALUES), keys case-insensitive: APPLY=0
# stays synchronous, its table in the console
shopt -s nocasematch
for arg in "$@"; do
    case $arg in
        --apply|--save|APPLY=1|APPLY=true|APPLY=yes|APPLY=on|APPLY=y|SAVE=1|SAVE=true|SAVE=yes|SAVE=on|SAVE=y)
            exec "$here/run.sh" analyze "$@"
            ;;
    esac
done
exec "$here/run.sh" --sync analyze "$@"
