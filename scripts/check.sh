#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
exec python3 "$project_dir/scripts/check.py" "$@"
