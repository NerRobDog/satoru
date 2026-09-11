"""The AoE IV pack's side of the contract.

These shell out to the real setup.sh. They build a directory shaped like an
unpacked release out of artifacts already on this machine, and they never touch
an installed pack home or a CrossOver bottle.

The rule being defended is the one the whole install ordering exists for:
--preflight answers "can this be installed here" and writes nothing into the
home. Today's setup.sh used to die on the sidecar probe after copying 420 MB of
engine. The unpacked pack is a different matter - it lives in the cache, which
the contract declares erasable and which unpack replaces wholesale - so clearing
the quarantine flag from the pack's own sidecar before probing it is allowed, and
is what lets setup.sh answer at all when a person runs it by hand.
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
    def test_preflight_answers_wherever_it_appears_in_the_arguments(self):
        """The flag loop accepted it anywhere; only $1 was ever looked at again.

        `setup.sh --clone --preflight` therefore answered the question by doing
        the whole thing: cloning a bottle, building a prefix, possibly fetching
        Steam - when what was asked was whether it could.
        """
        rc, out, err = self.run_setup("--clone", "--preflight")
        self.assertEqual(rc, 0, err)
        self.assertIn("satoru: mode=", out)
        self.assertEqual(os.listdir(self.home), [],
                         "--preflight built something because it was not first")

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



@unittest.skipUnless(have_artifacts(),
                     "needs the aoe4 submodule and an installed pack to borrow binaries from")
class CloneIsIdempotent(unittest.TestCase):
    """A second install must not clone the bottle over a prefix that has been played in.

    The contract has no update command: "Update = Install, run again", so
    install has to be idempotent. The fresh branch has always guarded its
    prefix; the clone branch rsynced the bottle in unconditionally, and
    MODE=auto finds the same bottle every time. Every update therefore put the
    bottle's month-old user.reg, Steam config and game profile back over the
    ones the player had been using.

    Nothing here launches wine: with the engine already installed and the
    prefix already there, clone mode copies files and nothing else. That is
    why this stubs pgrep rather than skipping on someone else's wineserver.
    """

    PACK_FILES = ("dxmt.conf", "counters.py", "patch-profile.py", "aoe4.sh", "setup.sh")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.pack = os.path.join(self.dir, "pack")
        self.home = os.path.join(self.dir, "home")
        self.bottle = os.path.join(self.dir, "bottle")

        for src, dst in NEEDED:
            full = os.path.join(self.pack, dst)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            shutil.copy2(os.path.join(INSTALLED, src), full)
        os.makedirs(os.path.join(self.pack, "dxmt", "x86_64-windows"))
        open(os.path.join(self.pack, "dxmt", "x86_64-windows", "d3d12.dll"), "wb").close()
        for name in self.PACK_FILES:
            shutil.copy2(os.path.join(PACK_SRC, name), os.path.join(self.pack, name))

        # An engine already installed and identical to the pack's, so setup.sh
        # keeps it instead of copying 420 MB into a temporary directory.
        shutil.copytree(os.path.join(self.pack, "Engine"), os.path.join(self.home, "Engine"))
        with open(os.path.join(self.home, "Engine", ".engine-id"), "w") as fh:
            fh.write(self.engine_id() + "\n")

        # A prefix that has been played in, and a bottle carrying older copies
        # of the very files a player's month lives in.
        self.prefix = os.path.join(self.home, "prefix")
        steamapps = os.path.join("drive_c", "Program Files (x86)", "Steam", "steamapps")
        os.makedirs(os.path.join(self.prefix, steamapps))
        with open(os.path.join(self.prefix, "user.reg"), "w") as fh:
            fh.write("a month of play\n")
        with open(os.path.join(self.prefix, "system.reg"), "w") as fh:
            fh.write("WINE REGISTRY Version 2\n")
        os.makedirs(os.path.join(self.bottle, steamapps, "common"))
        with open(os.path.join(self.bottle, "user.reg"), "w") as fh:
            fh.write("the bottle, as it was in April\n")
        with open(os.path.join(self.bottle, "drive_c", "from-the-bottle.txt"), "w") as fh:
            fh.write("should not arrive\n")

        # pgrep on PATH: this machine's own CrossOver wineserver is not ours to
        # kill, and the path under test never runs wine.
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "pgrep")
        with open(stub, "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(stub, 0o755)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def engine_id(self):
        """setup.sh's own recipe, so the ids agree and the copy is skipped."""
        out = subprocess.check_output(
            ["bash", "-c",
             "find Engine -type f ! -name '.DS_Store' | LC_ALL=C sort "
             "| xargs shasum -a 256 | shasum -a 256 | awk '{print $1}'"],
            cwd=self.pack)
        return out.decode().strip()

    def run_setup(self):
        env = dict(os.environ)
        env["SATORU_GAME_HOME"] = self.home
        env["AOE4_BOTTLE"] = self.bottle
        env["AOE4_MODE"] = "clone"
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        proc = subprocess.Popen(
            ["bash", os.path.join(self.pack, "setup.sh")],
            cwd=self.pack, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=300)
        return (proc.returncode, out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"))

    def test_a_second_install_keeps_the_prefix_that_was_played_in(self):
        rc, out, err = self.run_setup()
        self.assertEqual(rc, 0, err)
        with open(os.path.join(self.prefix, "user.reg")) as fh:
            self.assertEqual(fh.read(), "a month of play\n",
                             "the bottle's registry was cloned back over the player's")

    def test_a_second_install_does_not_bring_the_bottle_in_again(self):
        rc, _, err = self.run_setup()
        self.assertEqual(rc, 0, err)
        self.assertFalse(
            os.path.exists(os.path.join(self.prefix, "drive_c", "from-the-bottle.txt")),
            "clone mode rsynced the bottle into a prefix that already existed")

    def test_a_first_install_still_clones_the_bottle(self):
        """The guard must recognise a played-in prefix, not refuse to ever clone."""
        shutil.rmtree(self.prefix)
        rc, _, err = self.run_setup()
        self.assertEqual(rc, 0, err)
        with open(os.path.join(self.prefix, "user.reg")) as fh:
            self.assertEqual(fh.read(), "the bottle, as it was in April\n")
        self.assertTrue(os.path.islink(os.path.join(
            self.prefix, "drive_c", "Program Files (x86)", "Steam",
            "steamapps", "common")),
            "the game files were not linked in on a first install")

    def test_it_says_the_prefix_was_kept(self):
        # Not "keeping it": the engine says that too, a line earlier.
        _, out, _ = self.run_setup()
        self.assertIn("Prefix " + self.prefix + " already exists", out)


if __name__ == "__main__":
    unittest.main()
