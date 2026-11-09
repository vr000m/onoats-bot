#!/usr/bin/env bash
set -euo pipefail

clean_tmp="${TMPDIR:-/tmp}"
exec env -i HOME="$HOME" PATH="$PATH" TMPDIR="$clean_tmp" \
    npm ci --ignore-scripts
