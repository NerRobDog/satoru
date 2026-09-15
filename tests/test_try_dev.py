"""Dev wrappers must preserve real installations for every game."""
import importlib.util
import os
import subprocess
import tempfile
import unittest

from support import ROOT, satoru

spec = importlib.util.spec_from_file_location(
    "try_dev", os.path.join(ROOT, "tools", "try-dev.py"))
try_dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(try_dev)


class PreserveInstalledHome(unittest.TestCase):
    def test_existing_home_wins_over_legacy_launcher_for_each_game(self):
        for gid, name in (("aoe4", "Age of Empires IV"),
                          ("ow2", "Overwatch 2"),
                          ("magicka", "Magicka"),
                          ("prime-world", "Prime World")):
            with self.subTest(game=gid), tempfile.TemporaryDirectory() as directory:
                paths = satoru.Paths(home=directory)
                home = paths.home(name)
                os.makedirs(home)
                shim = os.path.join(home, "launch")
                with open(shim, "w") as stream:
                    stream.write('#!/bin/sh\nprintf "installed:%s\\n" "$1"\n')
                os.chmod(shim, 0o755)
                legacy = os.path.join(directory, "legacy.sh")
                with open(legacy, "w") as stream:
                    stream.write('#!/bin/sh\nprintf "legacy\\n"\n')
                os.chmod(legacy, 0o755)
                game = {"id": gid, "name": name, "status": "rc",
                        "execs": [legacy]}
                for _ in range(2):
                    bundle, detail = try_dev.wrap_one(paths, game)
                    launch = os.path.join(bundle, "Contents", "MacOS", "launch")
                    result = subprocess.check_output([launch, "--plain"], text=True)
                    self.assertEqual(result, "installed:--plain\n")
                    self.assertEqual(detail, "repaired in-bundle home")
