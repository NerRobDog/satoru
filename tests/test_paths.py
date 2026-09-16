"""Where things go.

Two rules the layout serves. Nothing lands in the root of a home directory:
Steam and CrossOver both keep their bodies under ~/Library, and so does this.
And the game's home lives *inside* its bundle (ADR-0001), so that an installed
game is one object a person can move, back up and throw away.

What does not live inside the bundle is the game's own files: 2.4 GB of ours
against 45 GB of theirs. Those sit in a library, and the library is what `root`
moves — moving it frees 96% of the space without taking the icon out of
Launchpad.
"""
import os
import unittest

from support import satoru

HOME = "/Users/tester"
APPS = HOME + "/Applications/satoru"
SUPPORT = HOME + "/Library/Application Support/satoru"


class Defaults(unittest.TestCase):
    def setUp(self):
        self.p = satoru.Paths(home=HOME)

    def test_bundle_is_findable_by_spotlight(self):
        self.assertEqual(self.p.bundle("Age of Empires IV"),
                         APPS + "/Age of Empires IV.app")

    def test_the_home_lives_inside_the_bundle(self):
        self.assertEqual(self.p.home("Age of Empires IV"),
                         APPS + "/Age of Empires IV.app/Contents/Resources/home")

    def test_shader_cache_lives_in_the_home_where_nothing_purges_it(self):
        self.assertEqual(self.p.shader_cache("Age of Empires IV"),
                         self.p.home("Age of Empires IV") + "/shader-cache")

    def test_library_is_outside_the_bundle(self):
        self.assertEqual(self.p.library, SUPPORT + "/library")
        self.assertNotIn(".app", self.p.library)

    def test_logs_go_where_console_app_looks(self):
        self.assertEqual(self.p.game_logs("aoe4"), HOME + "/Library/Logs/satoru/aoe4")

    def test_download_cache_is_declared_disposable(self):
        self.assertEqual(self.p.game_cache("aoe4"), HOME + "/Library/Caches/satoru/aoe4")

    def test_state_lives_with_the_umbrella(self):
        self.assertEqual(self.p.installed_file, SUPPORT + "/installed.toml")

    def test_nothing_lands_in_the_home_root(self):
        for path in (self.p.home("X"), self.p.library, self.p.game_logs("aoe4"),
                     self.p.game_cache("aoe4"), self.p.bundle("X"), self.p.installed_file):
            rest = path[len(HOME + "/"):]
            self.assertIn("/", rest, "%s sits directly in the home directory" % path)


class RootMovesTheGigabytes(unittest.TestCase):
    """`root` moves the library. It does not move the bundle, the logs or the cache."""

    def setUp(self):
        self.p = satoru.Paths(home=HOME, root="/Volumes/Games8TB/satoru/library")

    def test_library_follows_root(self):
        self.assertEqual(self.p.library, "/Volumes/Games8TB/satoru/library")

    def test_the_bundle_stays_in_applications(self):
        self.assertTrue(self.p.bundle("X").startswith(APPS),
                        "moving the library must not take the icon out of Launchpad")

    def test_logs_and_cache_stay_put(self):
        self.assertTrue(self.p.game_logs("aoe4").startswith(HOME + "/Library/Logs"))
        self.assertTrue(self.p.game_cache("aoe4").startswith(HOME + "/Library/Caches"))

    def test_tilde_and_vars_are_expanded(self):
        os.environ["SATORU_TEST_DISK"] = "/Volumes/Ext"
        try:
            p = satoru.Paths(home=HOME, root="$SATORU_TEST_DISK/lib")
            self.assertEqual(p.library, "/Volumes/Ext/lib")
        finally:
            del os.environ["SATORU_TEST_DISK"]


class Hostile(unittest.TestCase):
    """An id comes out of a manifest, and a manifest comes off the internet."""

    def setUp(self):
        self.p = satoru.Paths(home=HOME)

    def test_traversal_is_refused(self):
        for bad in ("..", "../etc", "a/b", "", "/abs"):
            with self.subTest(bad=bad):
                self.assertRaises(ValueError, self.p.game_logs, bad)
                self.assertRaises(ValueError, self.p.game_cache, bad)

    def test_bundle_name_cannot_carry_a_separator(self):
        # ":" is a path separator to the classic Mac APIs and Finder renders it as "/"
        self.assertEqual(self.p.bundle("Half/Life: Alyx"),
                         APPS + "/Half-Life- Alyx.app")

    def test_a_hostile_name_cannot_escape_the_bundle_either(self):
        self.assertTrue(self.p.home("../../etc").startswith(APPS))


class Config(unittest.TestCase):
    def test_missing_file_gives_defaults(self):
        cfg = satoru.load_config("/nonexistent/config.toml")
        self.assertIsNone(cfg["root"])
        self.assertTrue(cfg["check_updates"])

    def test_reads_root_and_switch(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write('root = "/Volumes/Games"\ncheck_updates = false\n')
            path = fh.name
        try:
            cfg = satoru.load_config(path)
            self.assertEqual(cfg["root"], "/Volumes/Games")
            self.assertFalse(cfg["check_updates"])
        finally:
            os.unlink(path)

    def test_unknown_key_is_reported(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write('rooot = "/typo"\n')
            path = fh.name
        try:
            cfg = satoru.load_config(path)
            self.assertTrue(any("rooot" in w for w in cfg["warnings"]), cfg)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
