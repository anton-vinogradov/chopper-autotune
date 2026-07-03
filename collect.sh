#!/bin/bash
here=$(dirname "$(realpath "$0")")
exec "$here/.venv/bin/chopper-autotune" collect --yes "$@"
