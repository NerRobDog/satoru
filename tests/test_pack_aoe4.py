"""The AoE IV pack's side of the contract.

These shell out to the real setup.sh. They build a directory shaped like an
unpacked release out of artifacts already on this machine, and they never touch
an installed pack home or a CrossOver bottle.

The rule being defended is the one the whole install ordering exists for:
--preflight answers "can this be installed here" and writes nothing. Today's
setup.sh used to die on the sidecar probe after copying 420 MB of engine.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

from support import satoru  # noqa: F401  (kept for consistency with the suite)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACK_SRC = os.path.join(ROOT, "games", "aoe4")
INSTALLED = os.path.expanduser("~/aoe4-pack")

NEEDED = [
    ("deps/Frameworks/libgnutls.30.dylib", "deps/libgnutls.30.dylib"),
    ("deps/Frameworks/libinotify.dylib", "deps/libinotify.dylib"),
    ("Helpers/x87sidecar", "Helpers/x87sidecar"),
    ("Engine/bin/wine", "Engine/bin/wine"),
    ("Engine/lib/wine/x86_64-unix/ntdll.so", "Engine/lib/wine/x86_64-unix/ntdll.so"),
]


def have_artifacts():
    if not os.path.isfile(os.path.join(PACK_SRC, "setup.sh")):
        return False
    return all(os.path.isfile(os.path.join(INSTALLED, src)) for src, _ in NEEDED)


def wineserver_running():
    return subprocess.call(["pgrep", "-q", "-f", "wineserver"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


@unittest.skipUnless(have_artifacts(),
                     "needs the aoe4 submodule and an installed pack to borrow binaries from")
class Preflight(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.pack = os.path.join(self.dir, "pack")
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        for src, dst in NEEDED:
            full = os.path.join(self.pack, dst)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            shutil.copy2(os.path.join(INSTALLED, src), full)
        shutil.copy2(os.path.join(PACK_SRC, "setup.sh"), os.path.join(self.pack, "setup.sh"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_setup(self, *args, **env_extra):
        env = dict(os.environ)
        env["SATORU_GAME_HOME"] = self.home
        env.update(env_extra)
        proc = subprocess.Popen(
            ["bash", os.path.join(self.pack, "setup.sh")] + list(args),
            cwd=self.pack, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=180)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def test_preflight_writes_nothing_into_the_home(self):
        # Whatever it decides, it decides it before touching the destination.
        self.run_setup("--preflight")
        self.assertEqual(os.listdir(self.home), [],
                         "--preflight must answer without building anything")

    def test_an_incomplete_pack_is_exit_12_not_a_generic_failure(self):
        os.remove(os.path.join(self.pack, "Helpers", "x87sidecar"))
        rc, _, err = self.run_setup("--preflight")
        self.assertEqual(rc, 12, err)
        self.assertIn("pack is incomplete", err)

    def test_an_unknown_flag_is_refused(self):
        rc, _, _ = self.run_setup("--wat")
        self.assertEqual(rc, 2)

    @unittest.skipIf(wineserver_running(),
                     "a wineserver is running, so preflight legitimately stops earlier")
    def test_preflight_reports_the_facts_the_umbrella_shows(self):
        rc, out, err = self.run_setup("--preflight")
        self.assertEqual(rc, 0, err)
        self.assertIn("satoru: mode=", out)
        self.assertIn("satoru: home=" + self.home, out)

    @unittest.skipIf(wineserver_running(),
                     "a wineserver is running, so preflight legitimately stops earlier")
    def test_the_old_variable_still_works(self):
        env = dict(os.environ)
        env.pop("SATORU_GAME_HOME", None)
        legacy = os.path.join(self.dir, "legacy")
        env["AOE4_PACK_HOME"] = legacy
        proc = subprocess.Popen(
            ["bash", os.path.join(self.pack, "setup.sh"), "--preflight"],
            cwd=self.pack, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = proc.communicate(timeout=180)
        self.assertIn("satoru: home=" + legacy, out.decode("utf-8", "replace"))


@unittest.skipUnless(os.path.isfile(os.path.join(PACK_SRC, "uninstall.sh")),
                     "needs the aoe4 submodule")
class Uninstall(unittest.TestCase):
    """Removing the pack must not remove the game.

    The prefix reaches the user's Steam library through a symlink. Deleting a
    symlink deletes the link; following it would delete 45 GB the pack never
    owned. This builds exactly that shape and checks the target survives.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.home = os.path.join(self.dir, "home")
        self.precious = os.path.join(self.dir, "steam-library")
        os.makedirs(os.path.join(self.precious, "Age of Empires IV"))
        with open(os.path.join(self.precious, "Age of Empires IV", "game.dat"), "w") as fh:
            fh.write("45 GB, pretend")
        steamapps = os.path.join(self.home, "prefix", "drive_c", "steamapps")
        os.makedirs(steamapps)
        os.symlink(self.precious, os.path.join(steamapps, "common"))
        os.makedirs(os.path.join(self.home, "Engine"))
        with open(os.path.join(self.home, "aoe4.conf"), "w") as fh:
            fh.write("pace = 60\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_uninstall(self, *args):
        env = dict(os.environ)
        env["SATORU_GAME_HOME"] = self.home
        proc = subprocess.Popen(
            ["bash", os.path.join(PACK_SRC, "uninstall.sh")] + list(args),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=60)
        return (proc.returncode, out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"))

    def test_dry_run_lists_sizes_and_removes_nothing(self):
        rc, out, err = self.run_uninstall("--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("aoe4.conf", out)
        self.assertIn("(total)", out)
        self.assertTrue(os.path.isdir(self.home), "--dry-run must not remove anything")

    def test_dry_run_names_what_it_will_not_touch(self):
        _, out, _ = self.run_uninstall("--dry-run")
        self.assertIn(self.precious, out,
                      "a linked library should be named as not-ours, not silently skipped")

    def test_it_refuses_without_being_told_to(self):
        rc, _, err = self.run_uninstall()
        self.assertEqual(rc, 2)
        self.assertIn("refusing", err)
        self.assertTrue(os.path.isdir(self.home))

    def test_removing_the_pack_leaves_the_game_alone(self):
        rc, _, err = self.run_uninstall("--yes")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(self.home))
        self.assertTrue(os.path.isfile(
            os.path.join(self.precious, "Age of Empires IV", "game.dat")),
            "the user's Steam library was followed and deleted")

    def test_nothing_to_remove_is_exit_11_not_a_failure(self):
        shutil.rmtree(self.home)
        rc, out, _ = self.run_uninstall("--yes")
        self.assertEqual(rc, 11)
        self.assertIn("nothing to remove", out)


if __name__ == "__main__":
    unittest.main()
