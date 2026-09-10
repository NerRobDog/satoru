"""installed.toml — what is installed, where, and of what version.

The authority is satoru, not the pack: a pack that lies or breaks cannot make
the umbrella believe a game is installed. And the file is only ever a claim —
if the home it names is gone, the game is not installed, whatever the file says.
Someone dragging a bundle to the Trash in Finder is a normal thing to do, and
it must leave the launcher telling the truth rather than offering to launch
something that is not there.
"""
import os
import shutil
import tempfile
import unittest

from support import satoru


class RoundTrip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "installed.toml")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_what_goes_in_comes_out(self):
        satoru.write_installed(self.path, {"aoe4": {
            "version": "v0.1", "source_sha256": "0540919b", "name": "Age of Empires IV",
            "installed_at": "2026-09-10T02:00:00", "home": "/x/home", "bundle": "/y/A.app"}})
        got = satoru.read_installed(self.path)
        self.assertEqual(got["aoe4"]["version"], "v0.1")
        self.assertEqual(got["aoe4"]["bundle"], "/y/A.app")

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(satoru.read_installed(os.path.join(self.dir, "nope.toml")), {})

    def test_a_broken_file_does_not_take_the_launcher_down(self):
        with open(self.path, "w") as fh:
            fh.write("this is not toml at all\n")
        self.assertEqual(satoru.read_installed(self.path), {})

    def test_paths_with_quotes_and_backslashes_survive(self):
        # A display name is a filename, and a filename can contain almost anything.
        nasty = '/x/A "quoted" \\ path/home'
        satoru.write_installed(self.path, {"g": {"home": nasty, "version": "v1"}})
        self.assertEqual(satoru.read_installed(self.path)["g"]["home"], nasty)

    def test_writing_is_atomic_enough_to_survive_itself(self):
        satoru.write_installed(self.path, {"a": {"version": "1"}})
        satoru.write_installed(self.path, {"b": {"version": "2"}})
        got = satoru.read_installed(self.path)
        self.assertNotIn("a", got)
        self.assertIn("b", got)


class RecordAndForget(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)
        os.makedirs(self.paths.support)
        self.manifest, errors = satoru.parse_manifest(satoru._parse_minimal_toml(
            'contract = 1\n[game]\nid = "aoe4"\nname = "Age of Empires IV"\nstatus = "rc"\n'
            '[source]\nkind = "release"\nurl = "https://e.invalid/p.tar.gz"\n'
            'sha256 = "abc"\nversion = "v0.1"\n'))
        self.assertEqual(errors, [])

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_record_writes_what_the_tui_needs_to_draw_a_row(self):
        satoru.record_install(self.paths, self.manifest, "0540919b")
        entry = satoru.read_installed(self.paths.installed_file)["aoe4"]
        self.assertEqual(entry["version"], "v0.1")
        self.assertEqual(entry["source_sha256"], "0540919b")
        self.assertEqual(entry["name"], "Age of Empires IV")
        self.assertEqual(entry["home"], self.paths.home("Age of Empires IV"))
        self.assertTrue(entry["installed_at"])

    def test_forget_removes_only_that_game(self):
        satoru.record_install(self.paths, self.manifest, "abc")
        satoru.write_installed(self.paths.installed_file, dict(
            satoru.read_installed(self.paths.installed_file), other={"version": "9"}))
        satoru.forget_install(self.paths, "aoe4")
        left = satoru.read_installed(self.paths.installed_file)
        self.assertNotIn("aoe4", left)
        self.assertIn("other", left)

    def test_forgetting_something_that_is_not_there_is_not_an_error(self):
        satoru.forget_install(self.paths, "aoe4")  # must not raise


class TheFileIsOnlyAClaim(unittest.TestCase):
    """A home that vanished means not installed, whatever installed.toml says."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)
        os.makedirs(self.paths.support)
        self.manifest, _ = satoru.parse_manifest(satoru._parse_minimal_toml(
            'contract = 1\n[game]\nid = "aoe4"\nname = "Age of Empires IV"\nstatus = "rc"\n'))
        satoru.record_install(self.paths, self.manifest, "abc")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_home_on_disk_counts_as_installed(self):
        os.makedirs(self.paths.home("Age of Empires IV"))
        self.assertIsNotNone(satoru.installed_entry(self.paths, "aoe4"))

    def test_a_home_dragged_to_the_trash_does_not(self):
        # never created, i.e. the same state as "the user deleted the bundle"
        self.assertIsNone(satoru.installed_entry(self.paths, "aoe4"))

    def test_and_the_stale_row_is_not_silently_kept(self):
        self.assertNotIn("aoe4", satoru.installed_games(self.paths))


if __name__ == "__main__":
    unittest.main()
