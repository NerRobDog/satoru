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

from support import satoru, build_pe_with_icon

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


ICON_MANIFEST_TOML = (
    'contract = 1\n'
    '[game]\nid = "aoe4"\nname = "Age of Empires IV"\nstatus = "rc"\n'
    '[source]\nkind = "release"\n'
    'url = "https://example.invalid/p.tar.gz"\n'
    'sha256 = "abc"\nversion = "v0.2"\n'
    '[commands]\nlaunch = "aoe4.sh"\n'
    '[paths]\nicon_exe = "drive_c/Program Files/AoE4/AoE4.exe"\n'
)


def _stub_convert(marker):
    """A fake `sips`: writes `marker` to dest and reports success, without
    touching the filesystem beyond that - tests stay independent of whether
    this machine actually has `sips` and of what it does with a fake .ico."""
    def convert(src_path, dest_path):
        with open(dest_path, "wb") as fh:
            fh.write(marker)
        return True
    return convert


def _failing_convert(src_path, dest_path):
    return False


class BundleIcon(unittest.TestCase):
    """Every bundle satoru writes gets an icon now - the game's own, when a
    pack names its exe, or satoru's own mark otherwise. Nothing here talks
    to the real `sips`: `icon_convert` is the same kind of seam
    `install_game` already gives `runner` and `fetch`.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _manifest(self, toml=ICON_MANIFEST_TOML):
        manifest, errors = satoru.parse_manifest(satoru._parse_minimal_toml(toml))
        self.assertEqual(errors, [])
        return manifest

    def _icon_filename(self, bundle):
        plist_path = os.path.join(bundle, "Contents", "Info.plist")
        with open(plist_path, "rb") as fh:
            info = plistlib.load(fh)
        return info.get("CFBundleIconFile")

    def test_no_icon_exe_falls_back_to_the_satoru_default(self):
        manifest = self._manifest(
            'contract = 1\n'
            '[game]\nid = "aoe4"\nname = "Age of Empires IV"\nstatus = "rc"\n'
            '[commands]\nlaunch = "aoe4.sh"\n')
        bundle = satoru.write_bundle(self.paths, manifest)
        icon_name = self._icon_filename(bundle)
        self.assertEqual(icon_name, "aoe4.icns")
        dest = os.path.join(bundle, "Contents", "Resources", icon_name)
        with open(dest, "rb") as fh:
            got = fh.read()
        with open(satoru.DEFAULT_ICON_ASSET, "rb") as fh:
            want = fh.read()
        self.assertEqual(got, want)

    def test_icon_exe_present_uses_the_games_own_icon(self):
        manifest = self._manifest()
        home = self.paths.home(manifest["game"]["name"])
        exe_dir = os.path.join(home, "drive_c", "Program Files", "AoE4")
        os.makedirs(exe_dir)
        pe_bytes, _ = build_pe_with_icon(bits=32)
        with open(os.path.join(exe_dir, "AoE4.exe"), "wb") as fh:
            fh.write(pe_bytes)

        marker = b"an .icns sips would have produced"
        bundle = satoru.write_bundle(self.paths, manifest,
                                      icon_convert=_stub_convert(marker))
        icon_name = self._icon_filename(bundle)
        self.assertEqual(icon_name, "aoe4.icns")
        dest = os.path.join(bundle, "Contents", "Resources", icon_name)
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), marker)

    def test_icon_exe_outside_the_home_is_refused(self):
        for traversal in ("../../etc/passwd", "/etc/passwd", "~/secrets"):
            manifest = self._manifest(
                'contract = 1\n'
                '[game]\nid = "aoe4"\nname = "Age of Empires IV"\nstatus = "rc"\n'
                '[commands]\nlaunch = "aoe4.sh"\n'
                '[paths]\nicon_exe = "%s"\n' % traversal)
            self.assertIsNone(
                satoru._icon_source_path(self.paths, manifest),
                "must refuse %r" % traversal)
            # And the bundle must still get an icon - the safe default -
            # rather than fail or leave the generic one.
            bundle = satoru.write_bundle(
                self.paths, manifest,
                icon_convert=_stub_convert(b"should never be called"))
            self.assertEqual(self._icon_filename(bundle), "aoe4.icns")

    def test_a_failed_extraction_keeps_a_previously_installed_icon(self):
        manifest = self._manifest()
        home = self.paths.home(manifest["game"]["name"])
        exe_dir = os.path.join(home, "drive_c", "Program Files", "AoE4")
        os.makedirs(exe_dir)
        pe_bytes, _ = build_pe_with_icon(bits=32)
        with open(os.path.join(exe_dir, "AoE4.exe"), "wb") as fh:
            fh.write(pe_bytes)

        good = b"the real icon, extracted once"
        bundle = satoru.write_bundle(self.paths, manifest,
                                      icon_convert=_stub_convert(good))
        dest = os.path.join(bundle, "Contents", "Resources", "aoe4.icns")
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), good)

        # A later re-install where conversion breaks (sips missing, a bad
        # exe) must not downgrade a working icon to the generic default.
        satoru.write_bundle(self.paths, manifest, icon_convert=_failing_convert)
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), good, "must keep the icon it already had")

    def test_write_bundle_never_raises_when_sips_is_missing(self):
        manifest = self._manifest()
        home = self.paths.home(manifest["game"]["name"])
        exe_dir = os.path.join(home, "drive_c", "Program Files", "AoE4")
        os.makedirs(exe_dir)
        pe_bytes, _ = build_pe_with_icon(bits=32)
        with open(os.path.join(exe_dir, "AoE4.exe"), "wb") as fh:
            fh.write(pe_bytes)

        def convert_raises(src_path, dest_path):
            raise OSError("[Errno 2] No such file or directory: 'sips'")

        bundle = satoru.write_bundle(self.paths, manifest, icon_convert=convert_raises)
        self.assertTrue(os.path.isdir(bundle))
        # sips is "missing": no game icon, but still the satoru default.
        self.assertEqual(self._icon_filename(bundle), "aoe4.icns")

    def test_an_unchecked_game_id_never_raises_out_of_install_bundle_icon(self):
        # parse_manifest keeps a manifest usable even with a bad id (it just
        # records an error); install_bundle_icon must not turn that into a
        # write outside Contents/Resources, or into an exception either.
        manifest, _ = satoru.parse_manifest(satoru._parse_minimal_toml(
            'contract = 1\n'
            '[game]\nid = "../escaped"\nname = "Bad Id"\nstatus = "rc"\n'
            '[commands]\nlaunch = "bad.sh"\n'))
        resources = os.path.join(self.dir, "Resources")
        os.makedirs(resources)
        result = satoru.install_bundle_icon(self.paths, manifest, resources)
        self.assertIsNone(result)
        self.assertEqual(os.listdir(self.dir), ["Resources"])

    def test_a_second_write_touches_the_bundle_so_finder_refreshes(self):
        manifest = self._manifest(
            'contract = 1\n'
            '[game]\nid = "aoe4"\nname = "Age of Empires IV"\nstatus = "rc"\n'
            '[commands]\nlaunch = "aoe4.sh"\n')
        bundle = satoru.write_bundle(self.paths, manifest)
        os.utime(bundle, (0, 0))  # pretend Finder cached this a long time ago
        satoru.write_bundle(self.paths, manifest)
        self.assertGreater(os.stat(bundle).st_mtime, 0)
