#!/bin/bash
exec "$(dirname "$(realpath "$0")")/run.sh" restore "$@"
