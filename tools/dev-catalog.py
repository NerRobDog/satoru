#!/usr/bin/env python3
"""Build a local dev catalogue from existing pack manifests and archives.

python3 tools/dev-catalog.py --pack /path/game.toml /path/pack.tar.gz
Then: ./satoru.command --games-dir dist/dev-games --install GAME
Archives stay where they are. Their hashes are checked again by the installer.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "launcher"))
import satoru


def add_pack(manifest_path, archive_path, output):
    archive = Path(archive_path).resolve(strict=True)
    if not archive.is_file():
        raise ValueError("not an archive file: %s" % archive)
    with open(manifest_path, encoding="utf-8") as stream:
        text = stream.read()
    data = satoru._parse_minimal_toml(text)
    gid = data.get("game", {}).get("id", "")
    if not re.fullmatch(r"[a-z0-9-]+", gid):
        raise ValueError("manifest needs a valid game id")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    version = data.get("source", {}).get("version") or "local-dev"
    source = ('[source]\nkind = "release"\nurl = %s\nsha256 = "%s"\n'
              'size = %d\nversion = %s\n\n') % (
                  satoru._toml_string(archive.as_uri()), digest.hexdigest(),
                  archive.stat().st_size, satoru._toml_string(version))
    # Only the source is changed. Requirements, status and pack commands stay
    # exactly as the pack author supplied them.
    text, count = re.subn(r"(?ms)^\[source\][^\[]*(?=^\[|\Z)",
                          lambda match: source, text)
    if count != 1:
        raise ValueError("manifest needs one [source] section")
    manifest, errors = satoru.parse_manifest(satoru._parse_minimal_toml(text))
    if errors:
        raise ValueError("invalid manifest: %s" % "; ".join(errors))
    target = Path(output) / gid / "game.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", nargs=2, action="append", required=True,
                        metavar=("MANIFEST", "ARCHIVE"))
    parser.add_argument("--output", default=os.path.join(ROOT, "dist", "dev-games"))
    args = parser.parse_args(argv)
    for manifest, archive in args.pack:
        try:
            print(add_pack(manifest, archive, args.output))
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
