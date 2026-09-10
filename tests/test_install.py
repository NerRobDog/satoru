"""The install pipeline.

The order is the design, not an implementation detail:

  requirements -> fetch -> unpack -> unquarantine -> preflight -> install -> record -> shim

Requirements come before the download so that a machine which cannot run the
game never spends 133 MB finding that out. Unquarantining comes before preflight
because the pack's own preflight runs an ad-hoc-signed binary, and a
browser-downloaded archive would get it SIGKILLed. Preflight comes before the
first write so that a refusal leaves no half-built home behind — which is
exactly what today's setup.sh does, dying on a probe after copying 420 MB.

Everything here runs against fakes: no network, no game, no pack.
"""
import os
import shutil
import tempfile
import unittest

from support import satoru

MANIFEST_TOML = (
    'contract = 1\n'
    '[game]\nid = "aoe4"\nname = "Age of Empires IV"\nstatus = "rc"\n'
    '[source]\nkind = "release"\n'
    'url = "https://github.com/NerRobDog/dxmt-aoe4-pack/releases/download/v0.1/p.tar.gz"\n'
    'sha256 = "abc"\nversion = "v0.1"\n'
    '[requires]\narch = "arm64"\nmacos = ">=26"\nrosetta = true\ndisk_gb = 4\n'
    '[commands]\npreflight = "bash setup.sh --preflight"\ninstall = "bash setup.sh"\n'
    'launch = "aoe4.sh"\n'
)


class FakeProbe(object):
    def __init__(self, arch="arm64", macos="26.5.2", rosetta=True, free_gb=200.0):
        self._a, self._m, self._r, self._f = arch, macos, rosetta, free_gb

    def arch(self): return self._a
    def macos_version(self): return self._m
    def has_rosetta(self): return self._r
    def free_gb(self, path=None): return self._f
    def which(self, tool): return "/usr/bin/" + tool


class FakeRunner(object):
    """Records what would have been run, and answers with canned exit codes."""

    def __init__(self, codes=None):
        self.calls = []
        self.codes = codes or {}

    def run(self, command, cwd=None, env=None, on_output=None):
        self.calls.append({"command": command, "cwd": cwd, "env": dict(env or {})})
        # Longest needle wins: "bash setup.sh" is a prefix of
        # "bash setup.sh --preflight", and a test that means one must not hit both.
        for needle in sorted(self.codes, key=len, reverse=True):
            if needle in command:
                return self.codes[needle]
        return 0


class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)
        self.manifest, errors = satoru.parse_manifest(
            satoru._parse_minimal_toml(MANIFEST_TOML))
        self.assertEqual(errors, [])
        self.unpacked = os.path.join(self.dir, "unpacked", "pack")
        os.makedirs(self.unpacked)
        with open(os.path.join(self.unpacked, "setup.sh"), "w") as fh:
            fh.write("#!/bin/sh\n")
        self.steps = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fetcher(self, source, cache_dir, reporter=None):
        self.steps.append("fetch")
        path = os.path.join(cache_dir, "pack.tar.gz")
        if not os.path.isdir(cache_dir):
            os.makedirs(cache_dir)
        open(path, "wb").close()
        return path

    def unpacker(self, archive, dest):
        self.steps.append("unpack")
        return self.unpacked

    def unquarantine(self, path):
        self.steps.append("unquarantine")

    def install(self, **kw):
        runner = kw.pop("runner", None) or FakeRunner()
        return satoru.install_game(
            self.manifest, self.paths,
            probe=kw.pop("probe", None) or FakeProbe(),
            runner=runner,
            fetch=self.fetcher, unpack=self.unpacker, unquarantine=self.unquarantine,
            **kw), runner


class Order(Harness):
    def test_the_steps_happen_in_the_order_the_design_requires(self):
        result, runner = self.install()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.steps, ["fetch", "unpack", "unquarantine"])
        ran = [c["command"] for c in runner.calls]
        self.assertEqual(ran, ["bash setup.sh --preflight", "bash setup.sh"])

    def test_the_pack_is_told_where_to_install_itself(self):
        _, runner = self.install()
        env = runner.calls[-1]["env"]
        self.assertEqual(env["SATORU_GAME_HOME"], self.paths.home("Age of Empires IV"))
        self.assertEqual(env["SATORU_GAME_ID"], "aoe4")
        self.assertEqual(env["SATORU_CONTRACT"], str(satoru.CONTRACT))
        self.assertIn("SATORU_LIBRARY", env)

    def test_commands_run_from_the_unpacked_pack(self):
        _, runner = self.install()
        for call in runner.calls:
            self.assertEqual(call["cwd"], self.unpacked)

    def test_it_records_state_and_writes_a_shim(self):
        result, _ = self.install()
        entry = satoru.installed_entry(self.paths, "aoe4")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["version"], "v0.1")
        self.assertTrue(os.path.isfile(
            os.path.join(self.paths.home("Age of Empires IV"), "launch")))


class WhichVolumeIsMeasured(Harness):
    def test_the_disk_check_asks_about_the_volume_the_bytes_land_on(self):
        """Per ADR-0001 the pack installs into the bundle, not into the library.

        With a library moved to an external disk, measuring the library reports
        six terabytes free while the engine goes onto a full internal one.
        """
        asked = []

        class RecordingProbe(FakeProbe):
            def free_gb(self, path=None):
                asked.append(path)
                return FakeProbe.free_gb(self, path)

        self.install(probe=RecordingProbe())
        self.assertTrue(asked, "nothing asked about free space at all")
        self.assertTrue(asked[0].startswith(self.paths.applications),
                        "measured %r, but the pack writes under %r"
                        % (asked[0], self.paths.applications))


class Refusals(Harness):
    def test_an_unmet_requirement_stops_before_a_single_byte_is_downloaded(self):
        result, runner = self.install(probe=FakeProbe(rosetta=False))
        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "requirements")
        self.assertEqual(self.steps, [], "nothing may be fetched for a machine that cannot run it")
        self.assertEqual(runner.calls, [])
        unmet = [r for r in result["requirements"] if not r["ok"]]
        self.assertEqual(unmet[0]["id"], "rosetta")
        self.assertEqual(unmet[0]["fix"]["kind"], "command")

    def test_a_preflight_refusal_leaves_no_home_behind(self):
        runner = FakeRunner({"--preflight": 10})
        result, runner = self.install(runner=runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "preflight")
        self.assertEqual(result["code"], 10)
        self.assertFalse(os.path.exists(self.paths.home("Age of Empires IV")),
                         "a refusal must not leave a half-built home")
        self.assertIsNone(satoru.installed_entry(self.paths, "aoe4"))

    def test_the_pack_is_unquarantined_before_its_preflight_runs(self):
        # The pack's preflight probes an ad-hoc-signed binary; a browser-downloaded
        # archive would have it SIGKILLed by Gatekeeper.
        self.install()
        self.assertIn("unquarantine", self.steps)
        self.assertLess(self.steps.index("unquarantine"), len(self.steps))

    def test_a_failed_install_is_not_recorded_as_installed(self):
        # preflight says yes, the install itself falls over
        runner = FakeRunner({"bash setup.sh --preflight": 0, "bash setup.sh": 1})
        result, _ = self.install(runner=runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "install")
        self.assertIsNone(satoru.installed_entry(self.paths, "aoe4"))

    def test_a_pack_with_no_source_cannot_be_installed_automatically(self):
        self.manifest["source"] = None
        result, _ = self.install()
        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "source")


class TalkingRunner(FakeRunner):
    """A pack that says something before it exits, the way a real one does."""

    def __init__(self, codes=None, says=None):
        FakeRunner.__init__(self, codes)
        self.says = says or []

    def run(self, command, cwd=None, env=None, on_output=None):
        for line in self.says:
            if on_output:
                on_output(line)
        return FakeRunner.run(self, command, cwd=cwd, env=env, on_output=on_output)


class WhatTheExitCodeMeans(Harness):
    """Contract v1 gives five exit codes their own meaning, and everything else
    one meaning: this pack is not speaking the contract.

    Until now every non-zero code became the same sentence, which is how the real
    v0.1 release — whose setup.sh predates --preflight and answers
    `unknown flag --preflight` with exit 2 — was reported as a pack refusing the
    machine, sending the reader to hunt for a fault that is not there.
    """

    def test_a_stated_refusal_is_told_apart_from_a_pack_that_cannot_answer(self):
        refused, _ = self.install(runner=FakeRunner({"--preflight": 10}))
        self.assertEqual(refused["reason"], "refused")
        confused, _ = self.install(runner=FakeRunner({"--preflight": 2}))
        self.assertEqual(confused["reason"], "not-contract")

    def test_an_unknown_code_names_the_code_and_blames_the_version(self):
        result, _ = self.install(runner=FakeRunner({"--preflight": 2}))
        self.assertIn("2", result["message"])
        self.assertIn("older", result["message"])
        self.assertNotIn("refused to install here", result["message"])

    def test_every_contract_code_gets_its_own_words(self):
        said = {}
        for code in (10, 11, 12, 20, 1):
            result, _ = self.install(runner=FakeRunner({"--preflight": code}))
            said[code] = result["message"]
        self.assertEqual(len(set(said.values())), len(said), said)

    def test_the_packs_own_words_are_what_the_reader_gets(self):
        # The spec is explicit for code 10: the pack's own message is shown as is.
        runner = TalkingRunner({"--preflight": 10},
                               says=["checking CrossOver...",
                                     "CrossOver 25.7 or newer is required"])
        result, _ = self.install(runner=runner)
        self.assertIn("CrossOver 25.7 or newer is required", result["message"])
        self.assertIn("CrossOver 25.7 or newer is required", result["output"])

    def test_an_install_that_cannot_answer_is_told_apart_too(self):
        runner = FakeRunner({"bash setup.sh --preflight": 0, "bash setup.sh": 127})
        result, _ = self.install(runner=runner)
        self.assertEqual(result["step"], "install")
        self.assertEqual(result["reason"], "not-contract")

    def test_a_caller_watching_the_output_still_sees_every_line(self):
        seen = []
        runner = TalkingRunner({"--preflight": 10}, says=["one", "two"])
        self.install(runner=runner, on_output=seen.append)
        self.assertEqual(seen, ["one", "two"])


if __name__ == "__main__":
    unittest.main()
