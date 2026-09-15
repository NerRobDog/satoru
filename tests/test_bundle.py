"""The .app Finder and Spotlight actually launch.

Until this exists, install_game leaves a directory named .app with a home
inside it and no Info.plist. Finder draws that as a broken icon. The bundle
is what turns the home into a thing a person can click.

Re-running must update, not duplicate: a second Install is how an update
arrives, and Spotlight indexes one bundle per game.
"""
import os
import plistlib
import shutil
import stat
import tempfile
import unittest

from support import satoru

MANIFEST_TOML = (
    'contract = 1\n'
    '[game]\nid = "aoe4"\nname = "Age of Empires IV"\nstatus = "rc"\n'
    '[source]\nkind = "release"\n'
    'url = "https://example.invalid/p.tar.gz"\n'
    'sha256 = "abc"\nversion = "v0.2"\n'
    '[commands]\nlaunch = "aoe4.sh"\n'
)


class Bundle(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)
        self.manifest, errors = satoru.parse_manifest(
            satoru._parse_minimal_toml(MANIFEST_TOML))
        self.assertEqual(errors, [])
        satoru.write_shim(self.paths, self.manifest)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_plist_names_the_game_and_the_executable(self):
        path = satoru.write_bundle(self.paths, self.manifest)
        plist_path = os.path.join(path, "Contents", "Info.plist")
        with open(plist_path, "rb") as fh:
            info = plistlib.load(fh)
        self.assertEqual(info["CFBundleIdentifier"], "org.satoru.game.aoe4")
        self.assertEqual(info["CFBundleName"], "Age of Empires IV")
        self.assertEqual(info["CFBundleExecutable"], "launch")
        self.assertEqual(info["CFBundlePackageType"], "APPL")
        self.assertTrue(info["NSHighResolutionCapable"])
        self.assertIn("NSMicrophoneUsageDescription", info)
        self.assertIn("NSCameraUsageDescription", info)

    def test_macos_launch_is_executable_and_execs_the_shim(self):
        bundle = satoru.write_bundle(self.paths, self.manifest)
        launch = os.path.join(bundle, "Contents", "MacOS", "launch")
        self.assertTrue(os.path.isfile(launch))
        self.assertTrue(stat.S_IXUSR & os.stat(launch).st_mode)
        with open(launch) as fh:
            text = fh.read()
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("exec", text)
        self.assertIn("../Resources/home/launch", text)
        self.assertIn("AppTranslocation", text)

    def test_a_second_write_updates_the_same_bundle(self):
        first = satoru.write_bundle(self.paths, self.manifest)
        second = satoru.write_bundle(self.paths, self.manifest)
        self.assertEqual(first, second)
        apps = os.listdir(self.paths.applications)
        self.assertEqual(apps, ["Age of Empires IV.app"])

    def test_bundle_respects_the_games_minimum_macos(self):
        self.manifest["requires"]["macos"] = ">=14"
        self.assertEqual(satoru.bundle_info(self.manifest)["LSMinimumSystemVersion"], "14.0")

    def test_a_thin_wrap_execs_an_existing_launcher_without_moving_it(self):
        existing = os.path.join(self.dir, "already", "run.sh")
        os.makedirs(os.path.dirname(existing))
        with open(existing, "w") as fh:
            fh.write("#!/bin/sh\necho ok\n")
        os.chmod(existing, 0o755)
        bundle = satoru.write_bundle(
            self.paths, self.manifest, exec_path=existing, cwd=os.path.dirname(existing))
        launch = os.path.join(bundle, "Contents", "MacOS", "launch")
        with open(launch) as fh:
            text = fh.read()
        self.assertIn(existing, text)
        self.assertIn("cd ", text)
        self.assertTrue(os.path.isfile(existing), "wrap must not move the launcher")
        home = self.paths.home("Age of Empires IV")
        self.assertFalse(os.path.isfile(os.path.join(home, "run.sh")))


class InstallWritesTheBundle(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)
        toml = (
            'contract = 1\n'
            '[game]\nid = "aoe4"\nname = "Age of Empires IV"\nstatus = "rc"\n'
            '[source]\nkind = "release"\nurl = "https://example.invalid/p.tar.gz"\n'
            'sha256 = "abc"\nversion = "v0.2"\n'
            '[requires]\narch = "arm64"\nmacos = ">=26"\nrosetta = true\n'
            '[commands]\npreflight = "true"\ninstall = "true"\nlaunch = "aoe4.sh"\n'
        )
        self.manifest, errors = satoru.parse_manifest(
            satoru._parse_minimal_toml(toml))
        self.assertEqual(errors, [])

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_successful_install_leaves_a_plist_finder_can_read(self):
        unpacked = os.path.join(self.dir, "unpacked")
        os.makedirs(unpacked)

        class Probe(object):
            def arch(self): return "arm64"
            def macos_version(self): return "26.5.2"
            def has_rosetta(self): return True
            def free_gb(self, path=None): return 200.0
            def which(self, tool): return "/usr/bin/" + tool

        class Runner(object):
            def run(self, command, cwd=None, env=None, on_output=None):
                return 0

        result = satoru.install_game(
            self.manifest, self.paths, probe=Probe(), runner=Runner(),
            fetch=lambda *a, **k: os.path.join(self.dir, "missing.tar.gz"),
            unpack=lambda archive, dest: unpacked,
            unquarantine=lambda path: None)
        self.assertTrue(result["ok"], result.get("message"))
        plist = os.path.join(
            self.paths.bundle("Age of Empires IV"), "Contents", "Info.plist")
        self.assertTrue(os.path.isfile(plist), "Finder still sees a broken .app")
        launch = os.path.join(
            self.paths.bundle("Age of Empires IV"), "Contents", "MacOS", "launch")
        self.assertTrue(os.path.isfile(launch))
        self.assertTrue(
            os.path.isfile(os.path.join(self.paths.home("Age of Empires IV"), "launch")))
