"""Where things go.

The rule the whole layout serves: nothing lands in the root of a home directory.
Steam and CrossOver both keep their bodies in ~/Library/Application Support, and
so does this. Logs go where Console.app looks for them, caches go where the
system already knows they are disposable.

`root` in config.toml moves the game bodies (they are the gigabytes, and the
people who move them are moving them to an external disk). It deliberately does
not move logs or caches: a log Console.app cannot find is not a log.
"""
import os
import unittest

from support import satoru


class Defaults(unittest.TestCase):
    def setUp(self):
        self.p = satoru.Paths(home="/Users/tester")

    def test_body_goes_where_steam_and_crossover_put_theirs(self):
        self.assertEqual(
            self.p.game_home("aoe4"),
            "/Users/tester/Library/Application Support/satoru/aoe4")

    def test_logs_go_where_console_app_looks(self):
        self.assertEqual(self.p.game_logs("aoe4"),
                         "/Users/tester/Library/Logs/satoru/aoe4")

    def test_caches_are_declared_disposable(self):
        self.assertEqual(self.p.game_cache("aoe4"),
                         "/Users/tester/Library/Caches/satoru/aoe4")

    def test_bundles_are_findable_by_spotlight(self):
        self.assertEqual(self.p.bundle("Age of Empires IV"),
                         "/Users/tester/Applications/satoru/Age of Empires IV.app")

    def test_state_lives_with_the_bodies(self):
        self.assertEqual(self.p.installed_file,
                         "/Users/tester/Library/Application Support/satoru/installed.toml")

    def test_nothing_lands_in_the_home_root(self):
        for path in (self.p.game_home("aoe4"), self.p.game_logs("aoe4"),
                     self.p.game_cache("aoe4"), self.p.bundle("X"),
                     self.p.installed_file):
            rest = path[len("/Users/tester/"):]
            self.assertIn("/", rest, "%s sits directly in the home directory" % path)


class RootOverride(unittest.TestCase):
    def test_moves_the_bodies(self):
        p = satoru.Paths(home="/Users/tester", root="/Volumes/Games/satoru")
        self.assertEqual(p.game_home("aoe4"), "/Volumes/Games/satoru/aoe4")

    def test_leaves_logs_and_caches_alone(self):
        p = satoru.Paths(home="/Users/tester", root="/Volumes/Games/satoru")
        self.assertTrue(p.game_logs("aoe4").startswith("/Users/tester/Library/Logs"))
        self.assertTrue(p.game_cache("aoe4").startswith("/Users/tester/Library/Caches"))

    def test_tilde_and_vars_are_expanded(self):
        os.environ["SATORU_TEST_DISK"] = "/Volumes/Ext"
        try:
            p = satoru.Paths(home="/Users/tester", root="$SATORU_TEST_DISK/games")
            self.assertEqual(p.game_home("x"), "/Volumes/Ext/games/x")
        finally:
            del os.environ["SATORU_TEST_DISK"]


class Hostile(unittest.TestCase):
    """An id comes out of a manifest, and a manifest comes off the internet."""

    def test_traversal_is_refused(self):
        p = satoru.Paths(home="/Users/tester")
        for bad in ("..", "../etc", "a/b", "", "/abs"):
            with self.subTest(bad=bad):
                self.assertRaises(ValueError, p.game_home, bad)

    def test_bundle_name_cannot_carry_a_separator(self):
        p = satoru.Paths(home="/Users/tester")
        # ":" is a path separator to the classic Mac APIs and shows up as "/" in Finder
        self.assertEqual(p.bundle("Half/Life: Alyx"),
                         "/Users/tester/Applications/satoru/Half-Life- Alyx.app")


class Config(unittest.TestCase):
    def test_missing_file_gives_defaults(self):
        cfg = satoru.load_config("/nonexistent/config.toml")
        self.assertIsNone(cfg["root"])
        self.assertTrue(cfg["check_updates"])

    def test_reads_root_and_switch(self, ):
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
