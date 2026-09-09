#!/bin/bash
# Build the satoru release tarball: the launcher and the packs' metadata, and nothing else.
#
# A clone of this repository with every submodule is ~410 MB, of which 332 MB is the Wine
# source tree in components/wine-aoe4 — none of which the launcher reads. The release is
# what a person actually needs to pick a game and run its setup: ~130 KB.
#
#   bash tools/make-release.sh [version]        default: the VERSION file
#
# The result lands in dist/. Each game.toml is copied out of its submodule, so the
# submodules must be checked out: what ships is exactly what this checkout would run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="${1:-$(cat VERSION 2>/dev/null || echo "")}"
[ -n "$VERSION" ] || { echo "no version: pass one, or write a VERSION file" >&2; exit 2; }

STAGE="dist/satoru-$VERSION"
rm -rf "$STAGE"; mkdir -p "$STAGE/launcher" "$STAGE/games"

cp launcher/satoru.py "$STAGE/launcher/"
cp satoru.command "$STAGE/"
cp LICENSE DONATE.md "$STAGE/"
printf '%s\n' "$VERSION" > "$STAGE/VERSION"

missing=0
for d in games/*/; do
    id=$(basename "$d")
    if [ ! -f "$d/game.toml" ]; then
        echo "games/$id: no game.toml — submodule not checked out" >&2
        missing=1; continue
    fi
    mkdir -p "$STAGE/games/$id"
    cp "$d/game.toml" "$STAGE/games/$id/"
    # A pack's bootstrap is the one script the umbrella runs before the pack exists on
    # disk; everything else a pack owns lives in the pack's own release.
    [ -f "$d/bootstrap.sh" ] && cp "$d/bootstrap.sh" "$STAGE/games/$id/"
done
[ "$missing" -eq 0 ] || { echo "refusing to build a release that is missing packs" >&2; exit 1; }

cat > "$STAGE/README.txt" <<TXT
satoru $VERSION — Windows games on Apple Silicon, open builds
https://github.com/NerRobDog/satoru

Run it:

    ./satoru.command

(or  python3 launcher/satoru.py  — same thing. In Finder, a downloaded .command needs
right-click -> Open the first time; from Terminal it just runs. If macOS offers to
install the Command Line Tools, accept: that is where python3 comes from.)

Arrow keys or j/k pick a game, Enter opens its actions, q quits.
    python3 launcher/satoru.py --list      plain listing, no terminal UI
    python3 launcher/satoru.py --version   this build, and the packs it knows

This archive is the launcher and the packs' metadata only — about 130 KB. Nothing here
is a game, an engine or a DLL. Choosing Setup downloads the pack that game needs, checks
its hashes, and runs the pack's own installer.

What each game needs today:

  Age of Empires IV   Works end to end. Setup downloads the pack (133 MB), which brings
                      its own Wine engine and DXMT; CrossOver is not needed at runtime,
                      though an existing CrossOver bottle with the game is used as the
                      source of the files if you have one. macOS 26+, Rosetta, ~4 GB free.

  Overwatch 2         Shown as SOON here. It plays, but installing it is still by hand
                      and needs a DXMT you build yourself:
                      https://github.com/NerRobDog/dxmt-ow2-pack

  Magicka             Shown as SOON here. A native FNA port, work in progress; building
                      it needs the .NET 8 SDK and your own copy of the game:
                      https://github.com/NerRobDog/magicka-fna

  Prime World         Nothing to run yet.

Everything is free and open source. If it helped, see DONATE.md.
TXT

( cd "$STAGE" && find . -type f ! -name SHA256SUMS -print0 \
    | sort -z | xargs -0 shasum -a 256 > SHA256SUMS )

TARBALL="dist/satoru-$VERSION.tar.gz"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C dist "satoru-$VERSION"

echo
echo "built $TARBALL"
du -h "$TARBALL" | awk '{print "  size:   " $1}'
shasum -a 256 "$TARBALL" | awk '{print "  sha256: " $1}'
echo "  files:  $(cd "$STAGE" && find . -type f | wc -l | tr -d ' ')"
