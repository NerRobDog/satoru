#!/bin/sh
# satoru — entry point. Run from Terminal, or double-click in Finder.
#
# A .command downloaded from the internet carries a quarantine flag, and Finder will
# refuse to open it ("unidentified developer"). Right-click -> Open once, or run it
# from Terminal, where the flag does not apply.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 was not found." >&2
    echo "macOS ships it with the Command Line Tools:  xcode-select --install" >&2
    exit 1
fi
exec python3 "$HERE/launcher/satoru.py" "$@"
