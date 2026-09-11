"""The OW2 pack's side of the contract.

These shell out to the real setup.sh, uninstall.sh and ow2.sh against a fixture
that looks like a CrossOver bottle: a directory with a cxbottle.conf and a
drive_c. No CrossOver bottle of yours is touched, the game is never started, and
the DXMT payload is faked — what is under test is the pack's behaviour, not the
translation layer.

Two rules are defended here. The first is the contract's: preflight answers "can
this be installed" and writes nothing into the home. The second is this pack's
own, because it is the pack that edits something outside its home: an install
changes exactly one file in the bottle, cxbottle.conf, and an uninstall gives it
back byte for byte.

Point OW2_PACK_SRC at a working copy to test one before it is committed; by
default the submodule at games/ow2 is what gets tested.
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from support import satoru  # noqa: F401  (kept for consistency with the suite)


def fingerprint(root):
    """Every path under `root` with its mode, size, mtime and contents.

    Reading a file moves atime, not mtime, so a command that only looks at a
    bottle leaves this identical. A write, a new file and a removed one all show
    up as a difference. Extended attributes do not: st_mode covers permissions,
    not xattrs, so a stripped quarantine flag passes unnoticed here.
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACK_SRC = os.environ.get("OW2_PACK_SRC") or os.path.join(ROOT, "games", "ow2")
STAMP = "v0.80-ow2-0.3"

PAYLOAD = [
    "dxmt/x86_64-windows/d3d11.dll",
    "dxmt/x86_64-windows/dxgi.dll",
    "dxmt/x86_64-windows/d3d10core.dll",
    "dxmt/x86_64-windows/winemetal.dll",
    "dxmt/x86_64-unix/winemetal.so",
]

CONF = '''[Bottle]
"Template" = "win10"

[EnvironmentVariables]
"CX_BOTTLE_CREATOR_APPID" = "com.codeweavers.c4.11995"
"WINEMSYNC" = "1"
"CX_GRAPHICS_BACKEND" = "d3dmetal"

[AppPaths]
"Battle.net" = "c:\\\\Program Files (x86)\\\\Battle.net"
'''


def have_pack():
    return os.path.isfile(os.path.join(PACK_SRC, "setup.sh"))


def is_apple_silicon():
    out = subprocess.run(["sysctl", "-n", "hw.optional.arm64"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return out.stdout.strip() == b"1"


@unittest.skipUnless(have_pack(), "games/ow2 is not checked out")
@unittest.skipUnless(is_apple_silicon(), "the pack refuses anything else, by design")
class PackCase(unittest.TestCase):
    """An unpacked pack, a fixture bottle and a home, all in a temp directory."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pack = os.path.join(self.dir, "pack")
        self.home = os.path.join(self.dir, "home")
        self.bottle = os.path.join(self.dir, "Battle.net Desktop App")

        # The pack as it ships: tracked files plus the payload that is not in git.
        shutil.copytree(PACK_SRC, self.pack,
                        ignore=shutil.ignore_patterns(".git", "dxmt", "dist"))
        for rel in PAYLOAD:
            path = os.path.join(self.pack, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Only d3d11.dll carries a stamp, and the pack reads it with `strings`.
            body = ("MZ fake dll for tests " + STAMP) if rel.endswith("d3d11.dll") \
                else "MZ fake dll for tests"
            with open(path, "w") as fh:
                fh.write(body + "\n")
        sums = subprocess.run(["shasum", "-a", "256"] + PAYLOAD,
                              cwd=self.pack, stdout=subprocess.PIPE)
        with open(os.path.join(self.pack, "SHA256SUMS"), "wb") as fh:
            fh.write(sums.stdout)

        desktop = os.path.join(self.bottle, "drive_c", "users", "Public", "Desktop")
        os.makedirs(desktop)
        for lnk in ("Overwatch.lnk", "Battle.net.lnk"):
            with open(os.path.join(desktop, lnk), "w") as fh:
                fh.write("fixture shortcut\n")
        with open(os.path.join(self.bottle, "cxbottle.conf"), "w") as fh:
            fh.write(CONF)

    # -- helpers ---------------------------------------------------------------
    def env(self):
        # Start from a clean environment, not the developer's: OW2_TARGET, CX_ROOT,
        # OW2_PACK_HOME or CX_BOTTLE_PATH left over in a shell would quietly change
        # what is under test, and the failure would only show up on someone else's
        # machine.
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "USER")}
        # satoru's logs live outside the home (~/Library/Logs/satoru/<id>), so the
        # fixture keeps them apart too: with both pointing at the same directory,
        # a pack that logged to the wrong one would look correct here.
        env.update(SATORU_GAME_HOME=self.home, SATORU_CONTRACT="1",
                   SATORU_GAME_ID="ow2",
                   SATORU_LOGS=os.path.join(self.dir, "satoru-logs"),
                   OW2_BOTTLE=self.bottle)
        return env

    def run_pack(self, *argv, **kw):
        cwd = kw.pop("cwd", self.pack)
        proc = subprocess.Popen(list(argv), cwd=cwd, env=self.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=120)
        return proc.returncode, out.decode("utf-8", "replace")

    def conf_text(self):
        with open(os.path.join(self.bottle, "cxbottle.conf")) as fh:
            return fh.read()

    # -- the contract's rule ---------------------------------------------------
    def test_preflight_writes_nothing_into_the_home_or_the_bottle(self):
        os.makedirs(self.home)
        before_home = fingerprint(self.home)
        before_bottle = fingerprint(self.bottle)
        code, out = self.run_pack("bash", "setup.sh", "--preflight")
        self.assertEqual(code, 0, out)
        self.assertEqual(fingerprint(self.home), before_home, "preflight wrote into the home")
        self.assertEqual(fingerprint(self.bottle), before_bottle, "preflight wrote into the bottle")

    def test_preflight_is_accepted_in_any_position(self):
        code, out = self.run_pack("bash", "setup.sh", "--preflight")
        self.assertEqual(code, 0, out)

    def test_a_missing_bottle_is_ten_and_names_what_it_looked_for(self):
        env_bottle = os.path.join(self.dir, "no-such-bottle")
        proc = subprocess.Popen(["bash", "setup.sh", "--preflight"], cwd=self.pack,
                                env=dict(self.env(), OW2_BOTTLE=env_bottle),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, 10, out)
        self.assertIn("no-such-bottle", out.decode())

    def test_a_flipped_byte_in_the_payload_is_twelve(self):
        path = os.path.join(self.pack, "dxmt/x86_64-windows/dxgi.dll")
        with open(path, "a") as fh:
            fh.write("tampered\n")
        code, out = self.run_pack("bash", "setup.sh", "--preflight")
        self.assertEqual(code, 12, out)

    # -- install ---------------------------------------------------------------
    def test_install_fills_the_home_and_edits_one_file_in_the_bottle(self):
        before = fingerprint(self.bottle)
        code, out = self.run_pack("bash", "setup.sh")
        self.assertEqual(code, 0, out)

        for rel in PAYLOAD + ["dxmt.conf", "BUILD-ID", "ow2.sh", "README-local.txt"]:
            self.assertTrue(os.path.exists(os.path.join(self.home, rel)), rel)
        with open(os.path.join(self.home, "BUILD-ID")) as fh:
            self.assertEqual(fh.read().strip(), STAMP)

        after = fingerprint(self.bottle)
        changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
        self.assertEqual(changed, {"cxbottle.conf", "cxbottle.conf.dxmt-ow2-pack.bak"},
                         "install touched more of the bottle than its config")

    def test_the_dll_path_names_the_parent_of_both_architectures(self):
        # Wine searches each WINEDLLPATH entry for x86_64-windows/ and x86_64-unix/.
        # Naming the windows directory itself sends it looking for
        # x86_64-windows/x86_64-windows, which is how this pack's bottle-side path
        # spent a release doing nothing.
        code, out = self.run_pack("bash", "setup.sh")
        self.assertEqual(code, 0, out)
        self.assertIn('"WINEDLLPATH" = "%s/dxmt"' % self.home, self.conf_text())

    def test_the_logs_directory_the_manifest_promises_actually_exists(self):
        # game.toml says [paths] logs = "logs", which satoru resolves inside the
        # home. If the layer logged somewhere else, the launcher's "Open logs"
        # would open nothing.
        code, out = self.run_pack("bash", "setup.sh")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(os.path.join(self.home, "logs")))
        self.assertIn('"DXMT_LOG_PATH" = "%s/logs"' % self.home, self.conf_text())

    def test_install_removes_the_backend_that_would_inject_crossovers_own_dxmt(self):
        self.run_pack("bash", "setup.sh")
        self.assertNotIn("CX_GRAPHICS_BACKEND", self.conf_text())
        self.assertIn('"WINEMSYNC" = "1"', self.conf_text())   # and leaves the rest alone

    def test_a_second_install_says_there_is_nothing_to_do(self):
        self.assertEqual(self.run_pack("bash", "setup.sh")[0], 0)
        code, out = self.run_pack("bash", "setup.sh", "--preflight")
        self.assertEqual(code, 11, out)
        code, out = self.run_pack("bash", "setup.sh")
        self.assertEqual(code, 11, out)

    # -- uninstall -------------------------------------------------------------
    def test_uninstall_gives_the_config_back_byte_for_byte(self):
        original = self.conf_text()
        self.run_pack("bash", "setup.sh")
        self.assertNotEqual(self.conf_text(), original)
        code, out = self.run_pack("bash", "uninstall.sh")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.conf_text(), original)
        self.assertFalse(os.path.exists(self.home))

    def test_dry_run_lists_and_removes_nothing(self):
        self.run_pack("bash", "setup.sh")
        before = fingerprint(self.bottle)
        code, out = self.run_pack("bash", "uninstall.sh", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn(self.home, out)
        self.assertTrue(os.path.isdir(self.home))
        self.assertEqual(fingerprint(self.bottle), before)

    def test_uninstall_finds_a_bottle_a_killed_plain_run_left_stripped(self):
        self.run_pack("bash", "setup.sh")
        original_backend = "d3dmetal"
        conf = os.path.join(self.bottle, "cxbottle.conf")
        stale = conf + ".dxmt-ow2-pack.plain"
        shutil.copyfile(conf, stale)
        subprocess.call(["python3", os.path.join(self.home, "cxenv.py"), "unset", conf,
                         "WINEDLLPATH", "WINEDLLOVERRIDES", "DXMT_CONFIG_FILE",
                         "DXMT_LOG_PATH", "DXMT_USE_DEFAULT_METAL_CACHE",
                         "DXMT_METALFX_SPATIAL_SWAPCHAIN", "DXMT_LOG_LEVEL"])
        subprocess.call(["python3", os.path.join(self.home, "cxenv.py"), "set", conf,
                         "CX_GRAPHICS_BACKEND=" + original_backend])

        code, out = self.run_pack("bash", "uninstall.sh")
        self.assertEqual(code, 0, out)
        # The bottle started out on d3dmetal, so that is what it gets back — from the
        # install-time backup, not from whatever --plain happened to inject.
        self.assertIn('"CX_GRAPHICS_BACKEND" = "d3dmetal"', self.conf_text())
        self.assertNotIn("WINEDLLPATH", self.conf_text())
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(conf + ".dxmt-ow2-pack.bak"))

    def test_uninstall_keeps_what_the_person_changed_since_installing(self):
        self.run_pack("bash", "setup.sh")
        conf = os.path.join(self.bottle, "cxbottle.conf")
        subprocess.call(["python3", os.path.join(self.home, "cxenv.py"), "set", conf,
                         "MTL_HUD_ENABLED=1"])
        code, out = self.run_pack("bash", "uninstall.sh")
        self.assertEqual(code, 0, out)
        self.assertIn('"MTL_HUD_ENABLED" = "1"', self.conf_text(),
                      "uninstall threw away a change that was not ours")
        self.assertIn('"WINEMSYNC" = "1"', self.conf_text())
        self.assertNotIn("WINEDLLPATH", self.conf_text())

    def test_uninstall_refuses_a_home_that_is_not_a_pack_home(self):
        # SATORU_GAME_HOME comes from outside; a typo in it must not cost someone a
        # directory of their own.
        stranger = os.path.join(self.dir, "not-ours")
        os.makedirs(stranger)
        with open(os.path.join(stranger, "thesis.txt"), "w") as fh:
            fh.write("years of work\n")
        env = dict(self.env(), SATORU_GAME_HOME=stranger)
        proc = subprocess.Popen(["bash", "uninstall.sh"], cwd=self.pack, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=120)
        self.assertIn(proc.returncode, (10, 11), out.decode())
        self.assertTrue(os.path.exists(os.path.join(stranger, "thesis.txt")))

    def test_uninstalling_nothing_is_eleven(self):
        code, out = self.run_pack("bash", "uninstall.sh")
        self.assertEqual(code, 11, out)

    # -- the launcher ----------------------------------------------------------
    def test_dry_run_says_which_backend_and_touches_nothing(self):
        self.run_pack("bash", "setup.sh")
        before = fingerprint(self.bottle)
        code, out = self.run_pack("bash", os.path.join(self.home, "ow2.sh"),
                                  "--dry-run", cwd=self.home)
        self.assertEqual(code, 0, out)
        self.assertIn("backend: dxmt", out)
        self.assertIn("cxstart", out)
        self.assertEqual(fingerprint(self.bottle), before)

    def test_a_bottle_with_no_shortcut_is_refused_before_anything_is_started(self):
        self.run_pack("bash", "setup.sh")
        for lnk in ("Overwatch.lnk", "Battle.net.lnk"):
            os.remove(os.path.join(self.bottle, "drive_c", "users", "Public",
                                   "Desktop", lnk))
        code, out = self.run_pack("bash", os.path.join(self.home, "ow2.sh"),
                                  cwd=self.home)
        self.assertEqual(code, 10, out)
        self.assertIn("OW2_TARGET", out)

    def test_the_launcher_prefers_the_games_own_shortcut(self):
        self.run_pack("bash", "setup.sh")
        code, out = self.run_pack("bash", os.path.join(self.home, "ow2.sh"),
                                  "--dry-run", cwd=self.home)
        self.assertEqual(code, 0, out)
        self.assertIn("Overwatch.lnk", out)

    def stub_crossover(self):
        """A CrossOver that records what the config said when it was launched.

        --plain is the one path that takes this pack's settings out of someone's
        bottle, so the test has to see both halves: what the launcher hands to
        CrossOver, and that the bottle is whole again afterwards. A stub is the
        only way to watch that without starting a game.
        """
        cx = os.path.join(self.dir, "cx", "bin")
        os.makedirs(cx)
        seen = os.path.join(self.dir, "conf-at-launch")
        with open(os.path.join(cx, "cxstart"), "w") as fh:
            fh.write('#!/bin/sh\ncp "%s/cxbottle.conf" "%s"\nexit 3\n'
                     % (self.bottle, seen))
        for name in ("wine", "wineserver"):
            with open(os.path.join(cx, name), "w") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
        for name in ("cxstart", "wine", "wineserver"):
            os.chmod(os.path.join(cx, name), 0o755)
        return os.path.dirname(cx), seen

    def test_plain_really_hands_crossover_the_other_backend(self):
        self.run_pack("bash", "setup.sh")
        cx_root, seen = self.stub_crossover()
        env = dict(self.env(), CX_ROOT=cx_root)
        proc = subprocess.Popen(["bash", os.path.join(self.home, "ow2.sh"), "--plain"],
                                cwd=self.home, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=120)
        with open(seen) as fh:
            at_launch = fh.read()
        self.assertIn('"CX_GRAPHICS_BACKEND" = "d3dmetal"', at_launch, out.decode())
        self.assertNotIn("WINEDLLPATH", at_launch,
                         "plain left our DLL overrides in place, so it was not plain")

    def test_a_failed_launch_still_gives_the_bottle_back(self):
        # The stub exits 3. Someone's bottle must not be left stripped because a
        # launch went wrong, or Ctrl-C landed in the wrong second.
        self.run_pack("bash", "setup.sh")
        wired = self.conf_text()
        cx_root, _ = self.stub_crossover()
        env = dict(self.env(), CX_ROOT=cx_root)
        subprocess.Popen(["bash", os.path.join(self.home, "ow2.sh"), "--plain"],
                         cwd=self.home, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT).communicate(timeout=120)
        self.assertEqual(self.conf_text(), wired)
        self.assertFalse(os.path.exists(
            os.path.join(self.bottle, "cxbottle.conf.dxmt-ow2-pack.plain")))

    def test_the_launcher_outlives_the_unpacked_pack(self):
        # satoru unpacks into SATORU_CACHE, which the contract calls erasable and
        # which the next install replaces wholesale. A launcher that reached back
        # into it would work until the first time anything was cleaned.
        self.run_pack("bash", "setup.sh")
        shutil.rmtree(self.pack)
        code, out = self.run_pack("bash", os.path.join(self.home, "ow2.sh"),
                                  "--dry-run", cwd=self.home)
        self.assertEqual(code, 0, out)
        self.assertIn("backend: dxmt", out)
        self.assertIn(os.path.join(self.home, "dxmt"), out,
                      "the launcher could not read the config it wired up")

    def test_a_killed_plain_run_is_undone_by_the_next_launch(self):
        # A --plain run that dies before its trap leaves the bottle stripped and the
        # good config in a .plain file. Taking a fresh backup over that file would
        # save the stripped config as the thing to restore, and this pack's settings
        # would be gone for good.
        self.run_pack("bash", "setup.sh")
        wired = self.conf_text()
        stale = os.path.join(self.bottle, "cxbottle.conf.dxmt-ow2-pack.plain")
        shutil.copyfile(os.path.join(self.bottle, "cxbottle.conf"), stale)
        subprocess.call(["python3", os.path.join(self.home, "cxenv.py"), "unset",
                         os.path.join(self.bottle, "cxbottle.conf"), "WINEDLLPATH",
                         "WINEDLLOVERRIDES", "DXMT_CONFIG_FILE", "DXMT_LOG_PATH"])
        subprocess.call(["python3", os.path.join(self.home, "cxenv.py"), "set",
                         os.path.join(self.bottle, "cxbottle.conf"),
                         "CX_GRAPHICS_BACKEND=d3dmetal"])
        self.assertNotEqual(self.conf_text(), wired)

        code, out = self.run_pack("bash", os.path.join(self.home, "ow2.sh"),
                                  "--dry-run", cwd=self.home)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.conf_text(), wired, "the stripped config was not undone")
        self.assertFalse(os.path.exists(stale))

    def test_a_busy_bottle_is_refused_and_asked_about_correctly(self):
        """The guard has to ask about the prefix root, by fd, tolerating truncation.

        Everything about this check is counter-intuitive, and each part was wrong
        once. wineserver holds the prefix ROOT open, not anything under drive_c.
        lsof truncates COMMAND to nine characters, so "wineserver" prints as
        "wineserve" and a grep for the full name never matches. And `lsof +D` on a
        bottle with a 77 GB game in it walks the whole tree for minutes.

        A stub lsof lets the test assert all three: that the answer is believed,
        that the bottle root is what was asked about, and that +D was not used.
        """
        self.run_pack("bash", "setup.sh")
        cx_root, _ = self.stub_crossover()
        bindir = os.path.join(self.dir, "stub-bin")
        os.makedirs(bindir)
        argfile = os.path.join(self.dir, "lsof-args")
        with open(os.path.join(bindir, "lsof"), "w") as fh:
            fh.write('#!/bin/sh\n'
                     'printf "%s\\n" "$*" >> "%s"\n'
                     'echo "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME"\n'
                     'echo "wineserve 999 nik 4r DIR 1,13 96 1 %s"\n' % ("%s", argfile, self.bottle))
        os.chmod(os.path.join(bindir, "lsof"), 0o755)
        env = dict(self.env(), CX_ROOT=cx_root,
                   PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        proc = subprocess.Popen(["bash", os.path.join(self.home, "ow2.sh")],
                                cwd=self.home, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 10, out.decode())
        self.assertIn("already running", out.decode())

        with open(argfile) as fh:
            asked = fh.read()
        self.assertIn(self.bottle, asked)
        self.assertNotIn("+D", asked, "the guard walks the bottle's whole tree")
        self.assertNotIn("drive_c", asked, "the guard asks about drive_c, where "
                                           "wineserver holds nothing")

    @unittest.skipUnless(os.path.isfile(
        "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wineserver"),
        "CrossOver is not installed")
    def test_the_guard_sees_a_real_wineserver(self):
        """The same check, against the thing itself rather than a stub.

        A wineserver started on the fixture prefix and killed afterwards. This is
        the test that would have caught the original guard, which reported "free"
        with a live session holding the bottle.
        """
        self.run_pack("bash", "setup.sh")
        cx = "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver"
        server = subprocess.Popen([os.path.join(cx, "bin", "wineserver"), "-p", "-f"],
                                  env=dict(os.environ, WINEPREFIX=self.bottle),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(server.kill)
        try:
            deadline = time.time() + 20
            seen = False
            while time.time() < deadline and not seen:
                out = subprocess.run(["lsof", "--", self.bottle],
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL).stdout
                seen = b"wine" in out
            if not seen:
                self.skipTest("wineserver did not take hold of the fixture prefix")
            cx_root, _ = self.stub_crossover()
            proc = subprocess.Popen(["bash", os.path.join(self.home, "ow2.sh")],
                                    cwd=self.home,
                                    env=dict(self.env(), CX_ROOT=cx_root),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out, _ = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 10, out.decode())
        finally:
            server.kill()
            server.wait()

    def test_plain_names_the_other_backend(self):
        self.run_pack("bash", "setup.sh")
        code, out = self.run_pack("bash", os.path.join(self.home, "ow2.sh"),
                                  "--plain", "--dry-run", cwd=self.home)
        self.assertEqual(code, 0, out)
        self.assertIn("backend: d3dmetal", out)


class Manifest(unittest.TestCase):
    """The manifest is read by satoru's own reader, on every interpreter."""

    @unittest.skipUnless(os.path.isfile(os.path.join(PACK_SRC, "game.toml")),
                         "games/ow2 is not checked out")
    def test_the_pack_declares_every_command_it_now_implements(self):
        data = satoru.load_toml(os.path.join(PACK_SRC, "game.toml"))
        manifest, errors = satoru.parse_manifest(data)
        self.assertEqual(errors, [])
        self.assertEqual(manifest["contract"], 1)
        for key in ("preflight", "install", "launch", "launch_plain", "uninstall"):
            self.assertTrue(manifest["commands"][key], key)
        for key in ("preflight", "install", "uninstall"):
            script = manifest["commands"][key].split()[1]
            self.assertTrue(os.path.isfile(os.path.join(PACK_SRC, script)),
                            "%s names %s, which is not in the pack" % (key, script))

    @unittest.skipUnless(os.path.isfile(os.path.join(PACK_SRC, "game.toml")),
                         "games/ow2 is not checked out")
    def test_a_kind_that_is_none_still_says_where_to_go(self):
        # Five greyed-out actions and no explanation reads as abandoned. Until the
        # release exists, the manifest owes the reader a link.
        data = satoru.load_toml(os.path.join(PACK_SRC, "game.toml"))
        manifest, _ = satoru.parse_manifest(data)
        if manifest["source"]["kind"] == "none":
            self.assertTrue(manifest["install"]["manual_url"])
        else:
            self.assertTrue(manifest["source"]["url"])
            self.assertTrue(manifest["source"]["sha256"])
            self.assertIsInstance(manifest["source"]["size"], int)

    def test_a_fraction_is_refused_with_the_line_it_is_on(self):
        # The subset refuses rather than guesses, and says where. A size written as
        # 11.5 MB worth of float would otherwise reach a downloader as something.
        path = os.path.join(tempfile.mkdtemp(), "game.toml")
        with open(path, "w") as fh:
            fh.write('contract = 1\n[source]\nsize = 1.5\n')
        with self.assertRaises(ValueError) as caught:
            satoru.load_toml(path)
        self.assertIn("line 3", str(caught.exception))

    def test_a_dotted_key_is_refused_with_the_line_it_is_on(self):
        path = os.path.join(tempfile.mkdtemp(), "game.toml")
        with open(path, "w") as fh:
            fh.write('contract = 1\n[game]\nid.name = "ow2"\n')
        with self.assertRaises(ValueError) as caught:
            satoru.load_toml(path)
        self.assertIn("line 3", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
