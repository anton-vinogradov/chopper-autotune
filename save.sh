#!/bin/bash
# A dry run runs in the foreground, its lines straight in the console: it sends no G-code
# back through Moonraker, which would wait for this macro to end. A save restarts Klipper
# through Moonraker, so it runs detached.
here=$(dirname "$(realpath "$0")")
# the values the CLI takes for yes (cli.TRUE_VALUES), keys case-insensitive
shopt -s nocasematch
for arg in "$@"; do
    case $arg in
        --dry-run|DRY_RUN=1|DRY_RUN=true|DRY_RUN=yes|DRY_RUN=on|DRY_RUN=y)
            exec "$here/run.sh" --sync save "$@"
            ;;
    esac
done
exec "$here/run.sh" save "$@"
