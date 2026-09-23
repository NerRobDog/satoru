"""A real local archive reaches a runnable bundle without test runners/fetchers."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from support import satoru, ROOT

spec = importlib.util.spec_from_file_location("dev_catalog", os.path.join(ROOT, "tools", "dev-catalog.py"))
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


class LocalInstall(unittest.TestCase):
    def test_archive_installs_and_bundle_runs_with_finder_environment(self):
        with tempfile.TemporaryDirectory(prefix="satoru space '") as directory:
            pack = Path(directory) / "pack"
            pack.mkdir()
            manifest = pack / "game.toml"
            manifest.write_text('contract=1\n[game]\nid="demo"\nname="Demo Game"\n'
                                'status="rc"\n[source]\nkind="none"\n'
                                '[install]\nmanual_url="https://example.invalid"\n'
                                '[commands]\npreflight="true"\ninstall="bash setup.sh"\n'
                                'launch="game.sh"\n')
            (pack / "setup.sh").write_text(
                '#!/bin/sh\nset -e\ncp game.sh "$SATORU_GAME_HOME/game.sh"\n'
                'chmod +x "$SATORU_GAME_HOME/game.sh"\n')
            (pack / "game.sh").write_text(
                '#!/bin/sh\nprintf "%s\\n" "$SATORU_GAME_ID:$SATORU_CONTRACT:$1"\n')
            archive = Path(directory) / "local pack.tar.gz"
            with tarfile.open(str(archive), "w:gz") as stream:
                stream.add(str(pack), arcname="pack")
            dest = catalog.add_pack(str(manifest), str(archive), Path(directory) / "catalog")
            game = satoru.load_games(str(dest.parent.parent))[0]
            self.assertEqual(game.validate(), [])
            paths = satoru.Paths(home=os.path.join(directory, "user"))
            result = satoru.install_game(game.manifest, paths, unquarantine=lambda p: None)
            self.assertTrue(result["ok"], result)
            # Launch must survive removal of the documented disposable cache.
            shutil.rmtree(paths.caches)
            bundle_launch = os.path.join(paths.bundle(game.name), "Contents", "MacOS", "launch")
            output = subprocess.check_output([bundle_launch, "hello world"], cwd="/",
                env={"HOME": paths.home_dir, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
            self.assertEqual(output.decode().strip(), "demo:1:hello world")
