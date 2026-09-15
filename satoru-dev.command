#!/bin/sh
# Local dev catalogue; build it with tools/dev-catalog.py first.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
if [ -f "$HERE/dist/dev-env.sh" ]; then
    . "$HERE/dist/dev-env.sh"
fi
exec /bin/sh "$HERE/satoru.command" --games-dir "$HERE/dist/dev-games" "$@"
