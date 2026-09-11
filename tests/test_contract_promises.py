"""What the contract promises a pack author, checked against what happens.

An audit asked one question of the published contract: could someone who is not
us write a working pack from the spec alone? The answers collected here are the
places where the honest answer was no - a documented exit code treated as a
failure, an environment promised at install and absent at launch, a switch the
user is told to flip that changes nothing, a state file written and never read.

Each test is one promise.
"""
import os
import shutil
import tempfile
import unittest

from support import satoru

MANIFEST = (
    'contract = 1\n'
    '[game]\nid = "aoe4"\nname = "Age of Empires IV"\nstatus = "rc"\n'
    'summary = "Flat 60 on an M1 Pro"\n'
    '[source]\nkind = "release"\n'
    'url = "https://github.com/NerRobDog/dxmt-aoe4-pack/releases/download/v0.1/p.tar.gz"\n'
    'sha256 = "abc"\nversion = "v0.1"\n'
    '[requires]\narch = "arm64"\n'
    '[install]\nforeign_note = "modifies your CrossOver bottle"\n'
    '[commands]\npreflight = "bash setup.sh --preflight"\ninstall = "bash setup.sh"\n'
    'launch = "aoe4.sh"\n'
)


class Probe(object):
    def arch(self): return "arm64"
    def macos_version(self): return "26.5.2"
    def has_rosetta(self): return True
    def free_gb(self, path=None): return 500.0
    def which(self, tool): return "/usr/bin/" + tool


class Runner(object):
    def __init__(self, codes=None):
        self.codes = codes or {}
        self.calls = []

    def run(self, command, cwd=None, env=None, on_output=None):
        self.calls.append(command)
        for needle in sorted(self.codes, key=len, reverse=True):
            if needle in command:
                return self.codes[needle]
        return 0


class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)
        self.manifest, errors = satoru.parse_manifest(
            satoru._parse_minimal_toml(MANIFEST))
        self.assertEqual(errors, [])
        self.unpacked = os.path.join(self.dir, "pack")
        os.makedirs(self.unpacked)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fetcher(self, source, cache_dir, reporter=None):
        if not os.path.isdir(cache_dir):
            os.makedirs(cache_dir)
        path = os.path.join(cache_dir, "p.tar.gz")
        open(path, "wb").close()
        return path

    def install(self, runner=None):
        runner = runner or Runner()
        return satoru.install_game(
            self.manifest, self.paths, probe=Probe(), runner=runner,
            fetch=self.fetcher, unpack=lambda a, d: self.unpacked,
            unquarantine=lambda p: None), runner


class ExitElevenMeansNothingToDo(Harness):
    """The contract gives 11 a meaning, and it is not failure.

    A pack that is asked to install itself twice and answers "there is nothing
    to do" is doing exactly what the contract asks. Reporting that as a failed
    install - with a warning about leftovers on top - punishes the one author who
    read the exit codes and believed them.
    """

    def test_eleven_from_the_install_is_a_finished_install(self):
        result, _ = self.install(Runner({"bash setup.sh": 11}))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "nothing-to-do")
        self.assertIn("already", result["message"].lower())
        self.assertIsNotNone(satoru.installed_entry(self.paths, "aoe4"))
        self.assertTrue(os.path.isfile(
            os.path.join(self.paths.home("Age of Empires IV"), "launch")))

    def test_eleven_from_the_preflight_skips_the_install_and_still_finishes(self):
        result, runner = self.install(Runner({"--preflight": 11}))
        self.assertTrue(result["ok"], result)
        self.assertEqual(runner.calls, ["bash setup.sh --preflight"],
                         "there was nothing to do, so the install must not run")
        self.assertIsNotNone(satoru.installed_entry(self.paths, "aoe4"))

    def test_eleven_does_not_warn_about_leftovers(self):
        result, _ = self.install(Runner({"bash setup.sh": 11}))
        self.assertNotIn("still in", result["message"])


class TheLaunchEnvironment(Harness):
    """The contract names an environment; it cannot be true only at install.

    An author writes "$SATORU_LOGS/game.log" in their launch command because the
    contract says that variable exists. It did at install time and not at launch,
    which is the kind of difference that shows up as an empty log nobody can
    explain.
    """

    def test_the_shim_exports_everything_the_contract_names(self):
        text = satoru.shim_text(self.paths, self.manifest)
        for var in ("SATORU_GAME_HOME", "SATORU_GAME_ID", "SATORU_LIBRARY",
                    "SATORU_CACHE", "SATORU_LOGS", "SATORU_CONTRACT"):
            self.assertIn(var, text, "%s is promised and not exported" % var)

    def test_the_log_directory_exists_before_the_game_is_told_about_it(self):
        text = satoru.shim_text(self.paths, self.manifest)
        self.assertIn("mkdir -p " + _quoted(self.paths.game_logs("aoe4")), text)


def _quoted(path):
    import shlex
    return shlex.quote(path)


class TheUpdateSwitch(Harness):
    """A switch the user is told to flip has to change something."""

    def test_turning_update_checks_off_takes_them_out_of_the_shim(self):
        on = satoru.shim_text(self.paths, self.manifest, check_updates=True)
        off = satoru.shim_text(self.paths, self.manifest, check_updates=False)
        self.assertIn("curl", on)
        self.assertNotIn("curl", off)

    def test_the_answer_the_check_writes_is_read_back(self):
        """Every launch may spend one of sixty API calls an hour on this."""
        marker = os.path.join(self.paths.game_cache("aoe4"), "update-check")
        os.makedirs(os.path.dirname(marker))
        with open(marker, "w") as fh:
            fh.write("v0.2\n")
        self.assertEqual(satoru.newer_version(self.paths, "aoe4"), "v0.2")

    def test_an_empty_marker_means_there_is_nothing_newer(self):
        marker = os.path.join(self.paths.game_cache("aoe4"), "update-check")
        os.makedirs(os.path.dirname(marker))
        open(marker, "w").close()
        self.assertIsNone(satoru.newer_version(self.paths, "aoe4"))

    def test_no_marker_at_all_is_not_an_error(self):
        self.assertIsNone(satoru.newer_version(self.paths, "aoe4"))


class StateIsReadBack(Harness):
    """installed.toml is written by satoru and has to be believed by satoru.

    Every path was recomputed from the game's display name, so renaming a game in
    its manifest - a normal thing for an author to do - left the install on disk
    and the launcher looking somewhere else, reporting not installed.
    """

    def test_a_renamed_game_is_still_the_game_that_was_installed(self):
        self.install()
        game = satoru.Game(os.path.join(self.dir, "game.toml"),
                           satoru._parse_minimal_toml(
                               MANIFEST.replace('name = "Age of Empires IV"',
                                                'name = "Age of Empires 4"')))
        state, detail = game.action_state("launch", self.paths)
        self.assertEqual(state, "ok", detail)
        self.assertTrue(detail.startswith(self.paths.home("Age of Empires IV")),
                        "the launcher must use the home it recorded, not a new guess")


class AMissingContractLine(unittest.TestCase):
    """One forgotten line must not silently mean a different language."""

    def test_a_v1_manifest_without_the_contract_line_is_told_so(self):
        data = satoru._parse_minimal_toml(
            MANIFEST.replace("contract = 1\n", ""))
        _, errors = satoru.parse_manifest(data)
        self.assertTrue(errors)
        self.assertTrue(any("contract" in e for e in errors),
                        "the errors blamed the sections instead: %r" % (errors,))


class WhatTheReaderSees(Harness):
    """Two things written in every manifest that nobody has ever seen."""

    def _described(self):
        import io
        game = satoru.Game(os.path.join(self.dir, "game.toml"),
                           satoru._parse_minimal_toml(MANIFEST))
        out = io.StringIO()
        satoru.describe([game], out=out, paths=self.paths)
        return out.getvalue()

    def test_a_newer_release_is_named_where_the_game_is_listed(self):
        marker = os.path.join(self.paths.game_cache("aoe4"), "update-check")
        os.makedirs(os.path.dirname(marker))
        with open(marker, "w") as fh:
            fh.write("v0.2\n")
        self.assertIn("v0.2", self._described())

    def test_the_one_line_summary_is_shown(self):
        self.assertIn("Flat 60 on an M1 Pro", self._described())

    def test_the_warning_that_a_pack_touches_something_else_is_shown(self):
        self.assertIn("modifies your CrossOver bottle", self._described())


if __name__ == "__main__":
    unittest.main()
