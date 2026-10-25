#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
PYTHONPATH=. exec python3 -m pytest -q tests "$@"
