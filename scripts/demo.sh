#!/bin/sh
cd "$(dirname "$0")/.." || exit 1
exec uv run --extra desktop --with-editable . python scripts/demo.py "$@"
