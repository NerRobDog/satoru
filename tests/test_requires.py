"""[requires] evaluation.

Every check goes through a probe object, so the whole thing is testable without
the machine it describes: an Intel Mac, a full disk and a missing Rosetta all
exist here without any of them existing here.

The shape of a result is not incidental. The TUI has to distinguish three cases
and must never offer an action that does not exist:
  fix = {"kind": "command"} we can run it for you
  fix = {"kind": "manual"}  only you can, here is how
  fix = None                nobody can
"""
import unittest

from support import satoru


class FakeProbe(object):
    def __init__(self, arch="arm64", macos="26.5.2", rosetta=True,
                 free_gb=200.0, tools=("python3", "ffmpeg")):
        self._arch, self._macos, self._rosetta = arch, macos, rosetta
        self._free_gb, self._tools = free_gb, set(tools)

    def arch(self):
        return self._arch

    def macos_version(self):
        return self._macos

    def has_rosetta(self):
        return self._rosetta

    def free_gb(self, path=None):
        return self._free_gb

    def which(self, tool):
        return "/usr/bin/" + tool if tool in self._tools else None


FULL = {"arch": "arm64", "macos": ">=26", "rosetta": True,
        "disk_gb": 4, "tools": ["python3"]}


def check(requires, **probe_kw):
    return satoru.check_requirements(requires, probe=FakeProbe(**probe_kw))


def by_id(results, rid):
    for r in results:
        if r["id"] == rid:
            return r
    raise AssertionError("no result %r in %r" % (rid, [r["id"] for r in results]))


class Satisfied(unittest.TestCase):
    def test_all_met(self):
        results = check(FULL)
        self.assertTrue(satoru.all_met(results), results)
        self.assertTrue(all(r["ok"] for r in results))

    def test_nothing_required_is_met(self):
        self.assertEqual(check({}), [])
        self.assertTrue(satoru.all_met([]))


class Unfixable(unittest.TestCase):
    def test_intel_mac_offers_no_fix(self):
        r = by_id(check(FULL, arch="x86_64"), "arch")
        self.assertFalse(r["ok"])
        self.assertIsNone(r["fix"], "nothing can turn an Intel Mac into an M-series one")
        self.assertIn("x86_64", r["detail"])


class FixableByUs(unittest.TestCase):
    def test_missing_rosetta_is_a_command_we_can_run(self):
        r = by_id(check(FULL, rosetta=False), "rosetta")
        self.assertFalse(r["ok"])
        self.assertEqual(r["fix"]["kind"], "command")
        self.assertIn("--install-rosetta", r["fix"]["run"])

    def test_we_do_not_agree_to_a_licence_on_their_behalf(self):
        r = by_id(check(FULL, rosetta=False), "rosetta")
        self.assertNotIn("--agree-to-license", r["fix"]["run"])


class FixableByThem(unittest.TestCase):
    def test_old_macos_is_manual(self):
        r = by_id(check(FULL, macos="15.6"), "macos")
        self.assertFalse(r["ok"])
        self.assertEqual(r["fix"]["kind"], "manual")
        self.assertIn("15.6", r["detail"])

    def test_not_enough_disk_names_both_numbers(self):
        r = by_id(check(FULL, free_gb=1.2), "disk")
        self.assertFalse(r["ok"])
        self.assertIn("1.2", r["detail"])
        self.assertIn("4", r["detail"])

    def test_missing_tool_says_how_to_get_it(self):
        r = by_id(check({"tools": ["ffmpeg"]}, tools=()), "tool:ffmpeg")
        self.assertFalse(r["ok"])
        self.assertEqual(r["fix"]["kind"], "manual")
        self.assertIn("ffmpeg", r["fix"]["hint"])


class Versions(unittest.TestCase):
    def test_same_major_is_enough(self):
        self.assertTrue(by_id(check(FULL, macos="26.0"), "macos")["ok"])

    def test_newer_major_passes(self):
        self.assertTrue(by_id(check(FULL, macos="27.1"), "macos")["ok"])

    def test_bare_version_means_at_least(self):
        self.assertTrue(by_id(check({"macos": "26"}, macos="26.5.2"), "macos")["ok"])

    def test_nonsense_spec_is_reported_not_silently_passed(self):
        r = by_id(check({"macos": "banana"}), "macos")
        self.assertFalse(r["ok"])


class RealMachine(unittest.TestCase):
    """The default probe has to answer about the Mac it is running on."""

    def test_arch_describes_the_machine_and_not_the_process(self):
        """A translated interpreter must not make an M-series Mac look like Intel.

        tests/run.sh runs the suite on every interpreter this Mac has, and some
        of them are x86_64 binaries under Rosetta - which is exactly the case
        this guards. On a native interpreter there is nothing to prove.
        """
        import platform
        import subprocess
        translated = subprocess.check_output(
            ["sysctl", "-n", "sysctl.proc_translated"]).strip() == b"1"
        if not translated:
            self.skipTest("this interpreter is native, so the two answers agree anyway")
        self.assertNotEqual(satoru.SystemProbe().arch(), platform.machine(),
                            "arch() is reporting the process, not the machine")
        self.assertEqual(satoru.SystemProbe().arch(), "arm64")

    def test_system_probe_answers(self):
        p = satoru.SystemProbe()
        self.assertTrue(p.arch())
        self.assertRegex(p.macos_version(), r"^\d+\.")
        self.assertIsInstance(p.has_rosetta(), bool)
        self.assertGreater(p.free_gb("/"), 0)
        self.assertTrue(p.which("sh"))
        self.assertIsNone(p.which("definitely-not-a-real-binary-xyzzy"))


if __name__ == "__main__":
    unittest.main()
