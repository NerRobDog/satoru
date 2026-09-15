"""ow2-a92: preflight touches nothing outside the pack.

docs/pack-contract.md ("Rule"): "preflight writes nothing into
SATORU_GAME_HOME, and touches nothing outside the unpacked pack." The only
existing coverage, tests/test_pack_aoe4.py::Preflight::
test_preflight_writes_nothing_into_the_home, checks just the first half
(`os.listdir(self.home) == []`) -- an empty-directory check that would not
notice a write to an existing file, and says nothing about anywhere outside
SATORU_GAME_HOME (a CrossOver bottle, $HOME, /tmp, ...), nor about the
refusal path, which the contract calls out as "most tempted to tidy up".

Two things exercise the fuller rule:

  * AoE4PreflightTouchesNothingOutsideThePack -- the real pack, gated exactly
    like test_pack_aoe4.py's own Preflight class (skipped when the aoe4
    submodule / a locally installed pack aren't available -- true on this
    worktree, where games/aoe4 is not checked out).
  * FixturePreflightTouchesNothingOutsideThePack -- a tiny preflight built
    fresh in a temp dir, so the property has unconditional coverage even
    when no real pack is on the machine. Its "cache/" subdirectory stands in
    for the "pack's own cache dir" the contract allows a pack to use freely
    (SATORU_CACHE is documented as erasable); everything else, in the pack
    and under $HOME, must come back byte-identical.

No CrossOver bottle fixture here: neither preflight (the real AoE4 one,
called with plain --preflight/no --clone, or the fixture one) reads a
bottle, and CrossOver has been uninstalled from this Mac -- a fixture
nothing under test reads would just be clutter.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import test_pack_aoe4 as aoe4  # reuses its artifact-staging + skip logic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def snapshot(root, exclude_dirnames=()):
    """{relpath: (mode, size, mtime_ns)} for every file under root.

    Anything under a directory named in exclude_dirnames is left out --
    used for the one subtree a pack is allowed to touch (its own cache).
    """
    out = {}
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirnames]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            st = os.lstat(full)
            out[rel] = (stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns)
    return out


@unittest.skipUnless(aoe4.have_artifacts(),
                     "needs the aoe4 submodule and an installed pack to borrow binaries from")
class AoE4PreflightTouchesNothingOutsideThePack(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.pack = os.path.join(self.dir, "pack")
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        for src, dst in aoe4.NEEDED:
            full = os.path.join(self.pack, dst)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            shutil.copy2(os.path.join(aoe4.INSTALLED, src), full)
        shutil.copy2(os.path.join(aoe4.PACK_SRC, "setup.sh"), os.path.join(self.pack, "setup.sh"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_setup(self, *args):
        env = dict(os.environ)
        env["SATORU_GAME_HOME"] = self.home
        proc = subprocess.Popen(
            ["bash", os.path.join(self.pack, "setup.sh")] + list(args),
            cwd=self.pack, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=180)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def assert_touches_only_the_pack(self, args, expect_rc):
        before_home = snapshot(self.home)
        before_pack = snapshot(self.pack)
        rc, _out, err = self.run_setup(*args)
        self.assertEqual(rc, expect_rc, err)
        self.assertEqual(snapshot(self.home), before_home,
                         "--preflight wrote into SATORU_GAME_HOME")
        self.assertEqual(snapshot(self.pack), before_pack,
                         "--preflight touched files in the unpacked pack")

    def test_preflight_pass_path_touches_only_the_pack(self):
        self.assert_touches_only_the_pack(["--preflight"], 0)

    def test_preflight_refusal_path_touches_only_the_pack(self):
        # Same incomplete-pack refusal test_pack_aoe4.py uses to get exit 12 --
        # the contract calls the refusal path the one "most tempted to tidy up".
        os.remove(os.path.join(self.pack, "Helpers", "x87sidecar"))
        self.assert_touches_only_the_pack(["--preflight"], 12)


class FixturePreflightTouchesNothingOutsideThePack(unittest.TestCase):
    GOOD_PREFLIGHT = """#!/bin/bash
set -euo pipefail
mkdir -p cache
touch cache/probe.tmp   # allowed: the pack's own cache dir (SATORU_CACHE is erasable)
echo "satoru: mode=fixture"
if [ "${SATORU_PREFLIGHT_REFUSE:-0}" = "1" ]; then
    echo "refusing on purpose" >&2
    exit 10
fi
exit 0
"""

    # The mutation ow2-a92 calls for: a preflight that writes to $HOME.
    BAD_PREFLIGHT = GOOD_PREFLIGHT.replace(
        'echo "satoru: mode=fixture"',
        'touch "$HOME/x"\necho "satoru: mode=fixture"',
    )

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fake_home = os.path.join(self.dir, "home")
        self.game_home = os.path.join(self.fake_home, "game-home")
        self.pack = os.path.join(self.dir, "pack")
        os.makedirs(self.game_home)
        os.makedirs(self.pack)
        with open(os.path.join(self.fake_home, "preexisting"), "w") as fh:
            fh.write("something that lived in $HOME before satoru ever ran\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_preflight(self, script):
        path = os.path.join(self.pack, "preflight.sh")
        with open(path, "w") as fh:
            fh.write(script)
        os.chmod(path, 0o755)
        return path

    def run_preflight(self, refuse=False):
        env = dict(os.environ)
        env["HOME"] = self.fake_home
        env["SATORU_GAME_HOME"] = self.game_home
        if refuse:
            env["SATORU_PREFLIGHT_REFUSE"] = "1"
        proc = subprocess.Popen(
            ["bash", "preflight.sh"], cwd=self.pack, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=30)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def assert_writes_nothing_outside_the_pack(self, refuse):
        before_home = snapshot(self.fake_home)
        before_pack = snapshot(self.pack, exclude_dirnames=("cache",))
        rc, _out, err = self.run_preflight(refuse=refuse)
        self.assertEqual(rc, 10 if refuse else 0, err)
        self.assertEqual(snapshot(self.fake_home), before_home,
                         "preflight wrote somewhere under $HOME")
        self.assertEqual(snapshot(self.pack, exclude_dirnames=("cache",)), before_pack,
                         "preflight touched the pack outside its own cache dir")

    def test_pass_path_writes_nothing_outside_the_pack(self):
        self.write_preflight(self.GOOD_PREFLIGHT)
        self.assert_writes_nothing_outside_the_pack(refuse=False)

    def test_refusal_path_writes_nothing_outside_the_pack(self):
        self.write_preflight(self.GOOD_PREFLIGHT)
        self.assert_writes_nothing_outside_the_pack(refuse=True)

    def test_the_check_catches_a_write_outside_the_pack(self):
        """Mutation check (ow2-a92): a preflight that does `touch "$HOME/x"`
        must fail the snapshot comparison above, or it isn't proving anything."""
        self.write_preflight(self.BAD_PREFLIGHT)
        with self.assertRaises(AssertionError):
            self.assert_writes_nothing_outside_the_pack(refuse=False)
        self.assertTrue(os.path.exists(os.path.join(self.fake_home, "x")),
                        "sanity: the mutated preflight should have actually written the file")


if __name__ == "__main__":
    unittest.main()
