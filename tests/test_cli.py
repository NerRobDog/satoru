"""The same install and launch actions are usable without curses."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from support import satoru


class CommandLine(unittest.TestCase):
    def test_preflight_failure_keeps_the_cause_and_fix(self):
        lines = ["no such bottle: Battle.net Desktop App", "", "Pass OW2_BOTTLE with its full path."]
        reason, message = satoru.explain_exit(10, "bash setup.sh --preflight", lines)
        self.assertEqual(reason, "refused")
        self.assertIn("\n".join(lines), message)

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        game = os.path.join(self.directory, "demo")
        os.makedirs(game)
        with open(os.path.join(game, "game.toml"), "w") as stream:
            stream.write('contract = 1\n[game]\nid="demo"\nname="Demo"\n'
                         'status="rc"\n[source]\nkind="release"\n'
                         'url="file:///demo.tar.gz"\nsha256="abc"\n'
                         '[commands]\ninstall="bash setup.sh"\nlaunch="game.sh"\n')

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_catalog_can_be_selected_for_listing(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = satoru.main(["--games-dir", self.directory, "--list"])
        self.assertEqual(code, 0)
        self.assertIn("Demo", output.getvalue())
        self.assertNotIn("submodules not checked out", output.getvalue())

    def test_install_uses_the_existing_action_and_returns_its_failure(self):
        with mock.patch.object(satoru, "run_action", return_value=(10, "missing data")) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                code = satoru.main(["--games-dir", self.directory, "--install", "demo"])
        self.assertEqual(code, 10)
        self.assertEqual(run.call_args[0][0].id, "demo")
        self.assertEqual(run.call_args[0][1], "setup")

    def test_unknown_game_is_a_failure_without_running_an_action(self):
        with mock.patch.object(satoru, "run_action") as run:
            with contextlib.redirect_stderr(io.StringIO()):
                code = satoru.main(["--games-dir", self.directory, "--launch", "absent"])
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_launch_uses_recorded_home_after_display_name_changes(self):
        paths = satoru.Paths(home=os.path.join(self.directory, "user"))
        game = satoru.load_games(self.directory)[0]
        old_home = os.path.join(self.directory, "old home")
        os.makedirs(old_home)
        shim = os.path.join(old_home, "launch")
        with open(shim, "w") as stream:
            stream.write("#!/bin/sh\nexit 0\n")
        satoru.write_installed(paths.installed_file, {"demo": {"id": "demo", "home": old_home}})
        with mock.patch.object(satoru, "current_paths", return_value=paths):
            with mock.patch.object(satoru.subprocess, "call", return_value=0) as call:
                code, _ = satoru.run_action(game, "launch")
        self.assertEqual(code, 0)
        self.assertEqual(call.call_args[0][0], [shim])
