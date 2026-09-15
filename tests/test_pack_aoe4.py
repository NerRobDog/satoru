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
    ("deps/Frameworks/libgnutls.30.dylib", "deps/Frameworks/libgnutls.30.dylib"),
    ("deps/Frameworks/libinotify.dylib", "deps/Frameworks/libinotify.dylib"),
    ("Helpers/x87sidecar", "Helpers/x87sidecar"),
    ("Engine/bin/wine", "Engine/bin/wine"),
    ("Engine/lib/wine/x86_64-unix/ntdll.so", "Engine/lib/wine/x86_64-unix/ntdll.so"),
]


def have_artifacts():
    if not os.path.isfile(os.path.join(PACK_SRC, "setup.sh")):
        return False
    return all(os.path.isfile(os.path.join(INSTALLED, src)) for src, _ in NEEDED)


def fingerprint(root):
    """Every path under `root` with its mode, size, mtime and contents.

    Reading a file moves atime, not mtime, so a preflight that only looks at a
    bottle leaves this identical. A write, a new file, a removed one or a
    stripped attribute all show up as a difference.
    """
    out = {}
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(dirs) + sorted(files):
            full = os.path.join(base, name)
            st = os.lstat(full)
            body = b""
            if os.path.isfile(full) and not os.path.islink(full):
                with open(full, "rb") as fh:
                    body = fh.read()
            out[os.path.relpath(full, root)] = (st.st_mode, st.st_size,
                                                st.st_mtime_ns, body)
    return out


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
        # A $HOME of its own: setup.sh looks for game files under ~/Games, and
        # this machine's real library is neither ours to hash nor a fixture.
        self.user = os.path.join(self.dir, "user")
        os.makedirs(self.user)
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
        env["HOME"] = self.user
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

    def _pgrep_that_finds_nothing(self):
        """So preflight runs past the wineserver check to the sidecar probe.

        This machine's own CrossOver wineserver is not ours to quit, and that
        check sits before the probe - the one part of preflight that runs a
        binary rather than reading a file.
        """
        binned = os.path.join(self.dir, "bin")
        if not os.path.isdir(binned):
            os.makedirs(binned)
            stub = os.path.join(binned, "pgrep")
            with open(stub, "w") as fh:
                fh.write("#!/bin/sh\nexit 1\n")
            os.chmod(stub, 0o755)
        return binned + os.pathsep + os.environ.get("PATH", "")

    def a_bottle(self, with_game=False):
        """A prefix shaped like a CrossOver bottle - the kind AOE4_BOTTLE names."""
        bottle = os.path.join(self.dir, "bottle")
        steam = os.path.join(bottle, "drive_c", "Program Files (x86)", "Steam",
                             "steamapps", "common", "Age of Empires IV")
        os.makedirs(steam)
        os.makedirs(os.path.join(bottle, "drive_c", "users", "crossover", "Documents"))
        if with_game:
            with open(os.path.join(steam, "RelicCardinal.exe"), "wb") as fh:
                fh.write(b"MZ, but not the build the Wine patch is wired to")
        with open(os.path.join(bottle, "system.reg"), "w") as fh:
            fh.write("WINE REGISTRY Version 2\n")
        with open(os.path.join(bottle, "cxbottle.conf"), "w") as fh:
            fh.write("[Bottle]\n")
        return bottle

    def test_preflight_leaves_the_crossover_bottle_alone(self):
        """The other half of the rule: nothing outside the unpacked pack.

        Choosing between clone and fresh means reading someone's bottle -
        walking its users' folders, hashing the game exe in it. A bottle is not
        ours and is not backed up, and at this point the person has only asked
        whether the pack could be installed, not agreed to install it.

        No game exe here, so the build-hash check has nothing to refuse and
        preflight runs all the way through, sidecar probe included.
        """
        bottle = self.a_bottle()
        before = fingerprint(bottle)
        rc, out, err = self.run_setup("--preflight", AOE4_BOTTLE=bottle,
                                      PATH=self._pgrep_that_finds_nothing())
        self.assertEqual(rc, 0, err)
        self.assertIn("satoru: mode=", out, "preflight stopped early, so this proved little")
        self.assertEqual(fingerprint(bottle), before,
                         "preflight modified the bottle it was only asked to read")

    def test_refusing_does_not_tidy_the_bottle_on_the_way_out(self):
        """The refusal path is where a script is most tempted to fix things."""
        bottle = self.a_bottle(with_game=True)
        before = fingerprint(bottle)
        rc, _, err = self.run_setup("--preflight", AOE4_BOTTLE=bottle,
                                    PATH=self._pgrep_that_finds_nothing())
        self.assertEqual(rc, 10, err)
        self.assertIn("RelicCardinal.exe sha256", err)
        self.assertEqual(fingerprint(bottle), before,
                         "preflight wrote to the bottle while refusing")

    def a_games_library(self, bottle_name="Age of Empires IV Anniversary Edition"):
        """A Steam library as moving a CrossOver bottle out left it: ~/Games/<bottle>/steamapps."""
        steamapps = os.path.join(self.user, "Games", bottle_name, "steamapps")
        game = os.path.join(steamapps, "common", "Age of Empires IV")
        os.makedirs(game)
        with open(os.path.join(game, "RelicCardinal.exe"), "wb") as fh:
            fh.write(b"MZ, but not the build the Wine patch is wired to")
        return steamapps

    def test_game_files_under_games_are_found_without_being_told(self):
        """No AOE4_STEAMAPPS, no bottle: the library under ~/Games is the one used.

        The exe is found, so the build-hash check runs on it and refuses the
        fake - which is how we know it was found, not skipped.
        """
        steamapps = self.a_games_library()
        rc, out, err = self.run_setup("--preflight", PATH=self._pgrep_that_finds_nothing())
        self.assertIn("Mode: fresh", out)
        self.assertIn("reusing the Steam library at " + steamapps, out)
        self.assertEqual(rc, 10, err)
        self.assertIn("RelicCardinal.exe sha256", err)

    def test_a_crossover_bottle_is_no_longer_looked_for(self):
        """CrossOver is gone; nothing of this pack goes looking in its Bottles folder."""
        bottles = os.path.join(self.user, "Library", "Application Support", "CrossOver", "Bottles")
        os.makedirs(bottles)
        shutil.move(self.a_bottle(with_game=True), os.path.join(bottles, "AoE"))
        rc, out, err = self.run_setup("--preflight", PATH=self._pgrep_that_finds_nothing())
        self.assertEqual(rc, 0, err)
        self.assertIn("satoru: mode=fresh", out)
        self.assertIn("satoru: game_files=download", out)
        self.assertNotIn("satoru: bottle=", out)

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
        rc, out, err = self.run_setup("--clone", "--preflight",
                                      AOE4_BOTTLE=self.a_bottle())
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
        env["HOME"] = self.user
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



@unittest.skipUnless(os.path.isfile(os.path.join(PACK_SRC, "tools", "make-pack.sh")),
                     "needs the aoe4 submodule")
class VersionIsInOnePlace(unittest.TestCase):
    """The pack's version may be written down exactly once.

    It used to live in four places - game.toml, bootstrap.sh, README.md and
    INSTALL.md - and nothing compared them. Cutting v0.2 would have shipped a
    bootstrap that downloads v0.1, and docs that name a file nobody published.
    The checker defends the invariant that makes that impossible: only
    game.toml may name a release.
    """

    CHECK = os.path.join(PACK_SRC, "tools", "check-version.sh")

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        full = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)

    def check(self, version="v0.2"):
        proc = subprocess.Popen(["bash", self.CHECK, version, self.dir],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=60)
        return (proc.returncode, out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"))

    def test_a_pack_that_names_no_release_outside_the_manifest_passes(self):
        self.write("game.toml", 'version = "v0.1"\n'
                   'url = "https://example.invalid/releases/download/v0.1/p.tar.gz"\n')
        self.write("README.md", "# dxmt-aoe4-pack - Age of Empires IV\n")
        rc, _, err = self.check()
        self.assertEqual(rc, 0, err)

    def test_the_manifest_may_still_point_at_the_previous_release(self):
        """Rule one: the release is cut first, the manifest follows it.

        So while v0.2 is being built, game.toml legitimately still says v0.1.
        A checker that refused that would forbid the correct order.
        """
        self.write("game.toml", 'version = "v0.1"\nsha256 = "0540919b"\n')
        rc, _, err = self.check(version="v0.2")
        self.assertEqual(rc, 0, err)

    def test_a_stale_tarball_name_in_the_docs_refuses_the_build(self):
        self.write("game.toml", 'version = "v0.1"\n')
        self.write("INSTALL.md", "Download `dxmt-aoe4-pack-v0.1.tar.gz` from Releases.\n")
        rc, _, err = self.check(version="v0.2")
        self.assertNotEqual(rc, 0)
        self.assertIn("INSTALL.md", err)

    def test_a_second_download_url_outside_the_manifest_refuses_the_build(self):
        self.write("game.toml", 'version = "v0.1"\n')
        self.write("bootstrap.sh",
                   'URL="https://example.invalid/releases/download/v0.1/p.tar.gz"\n')
        rc, _, err = self.check(version="v0.2")
        self.assertNotEqual(rc, 0)
        self.assertIn("bootstrap.sh", err)

    def build_id(self, sha):
        os.makedirs(os.path.join(self.dir, "Engine"), exist_ok=True)
        with open(os.path.join(self.dir, "Engine", ".build-id"), "w") as fh:
            fh.write(sha + "\n")

    def test_attribution_must_name_the_engine_revision_that_was_built(self):
        """LGPL: the source offer has to point at what was actually shipped.

        The upper tree is pinned by the sha256 of the source tarball, which is
        stronger than a commit. Our delta on top of it was pinned by nothing:
        the attribution named a repository and no revision, and the binary
        carries no stamp - neither bin/wine, a 27 KB loader, nor ntdll.so, where
        the code actually is. .build-id is that stamp, and this keeps it honest.
        """
        self.write("game.toml", 'version = "v0.1"\n')
        self.write("THIRD_PARTY.md",
                   "our build: `https://github.com/NerRobDog/wine-aoe4`, commit "
                   "`cf465d4bbe0fc0bdeeb1023a38bb98810901b547` |\n")
        self.build_id("cf465d4bbe0fc0bdeeb1023a38bb98810901b547")
        rc, _, err = self.check()
        self.assertEqual(rc, 0, err)

    def test_a_short_commit_in_the_attribution_is_enough(self):
        self.write("game.toml", 'version = "v0.1"\n')
        self.write("THIRD_PARTY.md", "our build: wine-aoe4, commit `cf465d4` |\n")
        self.build_id("cf465d4bbe0fc0bdeeb1023a38bb98810901b547")
        rc, _, err = self.check()
        self.assertEqual(rc, 0, err)

    def test_an_attribution_naming_a_different_revision_refuses_the_build(self):
        """The failure this exists for: the engine moves, the credit does not."""
        self.write("game.toml", 'version = "v0.1"\n')
        self.write("THIRD_PARTY.md", "our build: wine-aoe4, commit `438b37c` |\n")
        self.build_id("cf465d4bbe0fc0bdeeb1023a38bb98810901b547")
        rc, _, err = self.check()
        self.assertNotEqual(rc, 0)
        self.assertIn("THIRD_PARTY.md", err)

    def test_a_sha256_in_the_attribution_is_not_mistaken_for_a_revision(self):
        """THIRD_PARTY.md is full of sha256 sums; none of them is a commit."""
        self.write("game.toml", 'version = "v0.1"\n')
        self.write("THIRD_PARTY.md",
                   "source tarball sha256 "
                   "`7be5819017b34f09670293f2be7ed9f4476734b8f42dab121a8b74e6619c92a8`, "
                   "our build commit `cf465d4` |\n")
        self.build_id("cf465d4bbe0fc0bdeeb1023a38bb98810901b547")
        rc, _, err = self.check()
        self.assertEqual(rc, 0, err)

    def test_a_pack_without_a_build_id_is_not_refused(self):
        """v0.1's payload carries none, and a checker that blocked on that would
        make every older engine unbuildable. The build warns instead."""
        self.write("game.toml", 'version = "v0.1"\n')
        self.write("THIRD_PARTY.md", "our build: wine-aoe4 |\n")
        rc, _, err = self.check()
        self.assertEqual(rc, 0, err)

    def test_a_version_belonging_to_something_else_is_not_the_pack_version(self):
        """setup.sh greps the DXMT build stamp `v0.80-`; that is not a release."""
        self.write("game.toml", 'version = "v0.1"\n')
        self.write("setup.sh", "strings d3d12.dll | grep -m1 'v0.80-'\n")
        self.write("THIRD_PARTY.md", "CrossOver 26.3 / Wine 11.0\n")
        rc, _, err = self.check(version="v0.2")
        self.assertEqual(rc, 0, err)


@unittest.skipUnless(os.path.isfile(os.path.join(PACK_SRC, "tools", "make-pack.sh")),
                     "needs the aoe4 submodule")
class WhatTheTarballCarries(unittest.TestCase):
    """Two things the release must not carry, checked on a real build.

    The payload here is a handful of stand-in files, not the 401 MB engine: what
    is under test is which paths survive staging, and that does not depend on
    the bytes inside them.
    """

    MAKE = os.path.join(PACK_SRC, "tools", "make-pack.sh")
    EXEC = "#!/bin/sh\necho supported\n"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.artifacts = os.path.join(self.dir, "artifacts")
        for rel, body, mode in (
                ("Engine/bin/wine", self.EXEC, 0o755),
                ("Engine/lib/wine/x86_64-unix/ntdll.so", "", 0o644),
                # The ballast: 19 MB of these ride along in the real engine.
                ("Engine/lib/wine/x86_64-windows/libkernel32.a", "", 0o644),
                ("Helpers/x87sidecar", self.EXEC, 0o755),
                ("deps/Frameworks/libgnutls.30.dylib", "", 0o644),
                ("deps/Frameworks/libinotify.dylib", "", 0o644),
                ("dxmt/x86_64-windows/d3d12.dll", "", 0o644)):
            full = os.path.join(self.artifacts, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(body)
            os.chmod(full, mode)
        self.out = os.path.join(self.dir, "dist")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def build(self, version="v0.2", engine_commit=None):
        env = dict(os.environ)
        env.pop("AOE4_ENGINE_COMMIT", None)
        if engine_commit:
            env["AOE4_ENGINE_COMMIT"] = engine_commit
        # Point the build's own preflight at a bottle that is not one, so it
        # answers from the checks instead of walking this machine's CrossOver
        # bottles and hashing a real game exe. Any answer but 2 or 127 is fine
        # by the gate, which is what the build is asking about.
        env["AOE4_BOTTLE"] = os.path.join(self.dir, "not-a-bottle")
        proc = subprocess.Popen(["bash", self.MAKE, self.artifacts, version, self.out],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=300)
        return (proc.returncode, out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"))

    def entries(self, version="v0.2", engine_commit=None):
        rc, out, err = self.build(version, engine_commit)
        self.assertEqual(rc, 0, err or out)
        tarball = os.path.join(self.out, "dxmt-aoe4-pack-%s.tar.gz" % version)
        self.assertTrue(os.path.isfile(tarball), out)
        listing = subprocess.check_output(["tar", "tzf", tarball])
        return listing.decode("utf-8", "replace").splitlines()

    def test_no_static_libraries_ride_along(self):
        """503 of these, 19 MB, and nothing at runtime opens one."""
        names = self.entries()
        self.assertFalse([n for n in names if n.endswith(".a")],
                         "build-time import libraries shipped to users")

    def test_the_downloader_does_not_ship_inside_what_it_downloads(self):
        names = self.entries()
        self.assertNotIn("dxmt-aoe4-pack/bootstrap.sh", names,
                         "a bootstrap inside the tarball has nothing to fetch and goes stale")

    def test_the_build_tooling_does_not_ship_either(self):
        """What builds a release is not part of it."""
        names = self.entries()
        self.assertFalse([n for n in names if "/tools/" in n],
                         "the pack's build scripts shipped to users")

    def test_the_engine_revision_travels_with_the_pack(self):
        """So the tarball can answer what built it, and the attribution is checkable."""
        env_sha = "cf465d4bbe0fc0bdeeb1023a38bb98810901b547"
        names = self.entries(engine_commit=env_sha)
        self.assertIn("dxmt-aoe4-pack/Engine/.build-id", names)

    def test_a_build_with_no_recorded_revision_still_ships(self):
        """The v0.1 engine predates the idea; refusing to package it helps nobody."""
        rc, out, err = self.build()
        self.assertEqual(rc, 0, err or out)
        self.assertIn("no engine revision recorded", err)

    def test_the_pack_itself_is_still_there(self):
        """The prune must take the ballast and nothing else."""
        names = self.entries()
        for needed in ("dxmt-aoe4-pack/setup.sh",
                       "dxmt-aoe4-pack/game.toml",
                       "dxmt-aoe4-pack/Engine/bin/wine",
                       "dxmt-aoe4-pack/Helpers/x87sidecar"):
            self.assertIn(needed, names)

if __name__ == "__main__":
    unittest.main()
