#!/bin/bash
# --yes: the detached run has stdin at /dev/null and cannot answer the prompt
exec "$(dirname "$(realpath "$0")")/run.sh" envelope --yes "$@"
