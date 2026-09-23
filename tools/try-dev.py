#!/usr/bin/env python3
"""Wrap whatever is already playable here as ~/Applications/satoru/*.app.

Does not download. Does not move game files. Re-run refreshes Info.plist and
Contents/MacOS/launch in place. Games with no local launcher are skipped.

    python3 tools/try-dev.py
"""
from __future__ import print_function

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "launcher"))

import satoru  # noqa: E402


def _toml(gid, name, status):
    return (
        'contract = 1\n'
        '[game]\nid = "%s"\nname = "%s"\nstatus = "%s"\n'
        '[source]\nkind = "none"\n'
        '[commands]\nlaunch = "launch"\n'
    ) % (gid, name, status)


def _manifest(gid, name, status):
    manifest, errors = satoru.parse_manifest(
        satoru._parse_minimal_toml(_toml(gid, name, status)))
    if errors:
        raise SystemExit("%s: %s" % (gid, "; ".join(errors)))
    return manifest


def _first_existing(paths):
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return None


def candidates():
    """What this machine can wrap. Paths are looked up, not required."""
    home = os.path.expanduser("~")
    apps = os.path.join(home, "Applications", "satoru")
    lab = os.path.join(os.path.dirname(ROOT), "lab")
    return [
        {
            "id": "aoe4",
            "name": "Age of Empires IV",
            "status": "rc",
            "in_bundle_shim": os.path.join(
                apps, "Age of Empires IV.app", "Contents", "Resources", "home", "launch"),
            "execs": [os.path.join(home, "aoe4-pack", "aoe4.sh")],
        },
        {
            "id": "ow2",
            "name": "Overwatch 2",
            "status": "playable",
            "execs": [os.path.join(home, "ow2-pack", "ow2.sh")],
        },
        {
            "id": "magicka",
            "name": "Magicka",
            "status": "wip",
            "execs": [os.path.join(home, "Games", "magicka-fna-play", "run.sh")],
            "cwd": os.path.join(home, "Games", "magicka-fna-play"),
            "extra_args": ["--play"],
        },
        {
            "id": "prime-world",
            "name": "Prime World",
            "status": "wip",
            "execs": [
                os.path.join(lab, "pw-sidecar", "pw.sh"),
                os.path.join(lab, "pw-pack", "pw.sh"),
            ],
        },
    ]


def wrap_one(paths, game):
    manifest = _manifest(game["id"], game["name"], game["status"])
    shim = os.path.join(paths.home(game["name"]), "launch")
    if shim and os.path.isfile(shim):
        bundle = satoru.write_bundle(paths, manifest)
        return bundle, "repaired in-bundle home"
    exec_path = _first_existing(game.get("execs") or [])
    if not exec_path:
        return None, "no local launcher"
    bundle = satoru.write_bundle(
        paths, manifest,
        exec_path=exec_path,
        cwd=game.get("cwd") or os.path.dirname(exec_path),
        extra_args=game.get("extra_args"),
    )
    return bundle, exec_path


def main(argv=None):
    paths = satoru.Paths()
    if not os.path.isdir(paths.applications):
        os.makedirs(paths.applications)
    wrote = []
    skipped = []
    for game in candidates():
        bundle, detail = wrap_one(paths, game)
        if bundle is None:
            skipped.append("%s: %s" % (game["name"], detail))
            continue
        wrote.append("%s -> %s (%s)" % (game["name"], bundle, detail))
    for line in wrote:
        sys.stdout.write(line + "\n")
    for line in skipped:
        sys.stdout.write("skip " + line + "\n")
    sys.stdout.write("%d wrapped, %d skipped\n" % (len(wrote), len(skipped)))
    return 0 if wrote else 1


if __name__ == "__main__":
    sys.exit(main())
