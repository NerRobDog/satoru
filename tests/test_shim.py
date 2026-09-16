"""The launch shim.

The bundle calls exactly one thing, and this is it. Everything the umbrella
needs to happen at launch time lives here rather than in the bundle, so that
updating a pack never means rewriting the .app — the icon stays put in the Dock
and Spotlight has nothing to reindex.

Three things it must get right, in this order:

  1. Refuse to start under App Translocation. macOS mounts a quarantined bundle
     read-only at a random path, and a Wine prefix that cannot be written to
     fails in confusing ways rather than obvious ones. Apple provides no
     supported way to detect this; the executable's own path is the only signal.
  2. Point DXMT at a shader cache inside the home, so the system stops being
     allowed to reclaim 434 MB of warmed pipelines.
  3. Check for updates without making anyone wait for it. The game starts now;
     the answer is read on some later run.
"""
import os
import shlex
import shutil
import stat
import tempfile
import unittest

from support import satoru

MANIFEST_TOML = (
    'contract = 1\n'
    '[game]\nid = "aoe4"\nname = "Age of Empires IV"\nstatus = "rc"\n'
    '[source]\nkind = "release"\n'
    'url = "https://github.com/NerRobDog/dxmt-aoe4-pack/releases/download/v0.1/p.tar.gz"\n'
    'sha256 = "abc"\nversion = "v0.1"\ncheck = "github-release"\n'
    '[commands]\nlaunch = "aoe4.sh"\nlaunch_plain = "aoe4.sh --plain"\n'
)


def lines_of(text):
    return [l.strip() for l in text.splitlines()]


def index_of(text, needle):
    for i, line in enumerate(text.splitlines()):
        if needle in line:
            return i
    raise AssertionError("no line containing %r in:\n%s" % (needle, text))


class Shape(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)
        self.manifest, errors = satoru.parse_manifest(
            satoru._parse_minimal_toml(MANIFEST_TOML))
        self.assertEqual(errors, [])
        self.text = satoru.write_shim(self.paths, self.manifest, write=False)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_it_is_a_shell_script(self):
        self.assertTrue(self.text.startswith("#!/bin/sh"))

    def test_the_translocation_guard_comes_before_anything_else(self):
        guard = index_of(self.text, "AppTranslocation")
        for later in ("DXMT_SHADER_CACHE_PATH", "exec "):
            self.assertLess(guard, index_of(self.text, later),
                            "%s must not run before the translocation guard" % later)

    def test_the_guard_explains_rather_than_just_failing(self):
        self.assertIn("Applications", self.text)
        self.assertIn("read-only", self.text)

    def test_the_shader_cache_points_inside_the_home(self):
        self.assertIn(self.paths.shader_cache("Age of Empires IV"), self.text)

    def test_the_shader_cache_directory_is_made_before_the_game_is_told_about_it(self):
        """Nothing else creates it. DXMT is handed an absolute path and opens a
        file in it; a missing directory costs the warmed pipelines silently,
        which is the one failure this cache exists to prevent.
        """
        made = index_of(self.text, "mkdir -p " + shlex.quote(
            self.paths.shader_cache("Age of Empires IV")))
        self.assertLess(made, index_of(self.text, "exec "))

    def test_the_update_check_is_detached_and_not_waited_for(self):
        check = index_of(self.text, "update-check")
        self.assertLess(check, index_of(self.text, "exec "),
                        "the check has to be started before the game, not after it")
        window = "\n".join(self.text.splitlines()[check:check + 12])
        self.assertIn("&", window, "the check must be detached, not awaited")

    def test_the_check_is_throttled(self):
        self.assertIn("21600", self.text)  # six hours, in seconds

    def test_it_execs_what_the_pack_declared(self):
        exec_line = self.text.splitlines()[index_of(self.text, "exec ")]
        self.assertIn("aoe4.sh", exec_line)
        self.assertIn('"$@"', exec_line, "flags like --plain have to reach the pack")

    def test_it_resolves_its_own_location_rather_than_hardcoding_one(self):
        # The bundle can be moved to another disk; a hardcoded home would not survive it.
        self.assertIn('dirname "$0"', self.text)


class Written(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)
        self.manifest, _ = satoru.parse_manifest(satoru._parse_minimal_toml(MANIFEST_TOML))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_it_lands_in_the_home_and_is_executable(self):
        path = satoru.write_shim(self.paths, self.manifest)
        self.assertEqual(path, os.path.join(self.paths.home("Age of Empires IV"), "launch"))
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR)

    def test_rewriting_it_does_not_append(self):
        satoru.write_shim(self.paths, self.manifest)
        path = satoru.write_shim(self.paths, self.manifest)
        with open(path) as fh:
            body = fh.read()
        self.assertEqual(body.count("#!/bin/sh"), 1)

    def test_a_pack_with_no_launch_command_gets_no_shim(self):
        manifest, _ = satoru.parse_manifest(satoru._parse_minimal_toml(
            'contract = 1\n[game]\nid = "x"\nname = "X"\nstatus = "wip"\n'))
        self.assertIsNone(satoru.write_shim(self.paths, manifest))


class ItActuallyRuns(unittest.TestCase):
    """A generated script that shell cannot parse is worse than no script."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)
        self.manifest, _ = satoru.parse_manifest(satoru._parse_minimal_toml(MANIFEST_TOML))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_sh_can_parse_it(self):
        import subprocess
        path = satoru.write_shim(self.paths, self.manifest)
        rc = subprocess.call(["/bin/sh", "-n", path])
        self.assertEqual(rc, 0, "the generated shim is not valid shell")


class SourcesWeCannotCheck(unittest.TestCase):
    """We only know how to ask GitHub. Anything else gets no check rather than a
    broken one - a shim that runs a curl against a URL it cannot parse would burn
    a network round trip on every launch and never learn anything."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_non_github_source_gets_no_update_check(self):
        manifest, _ = satoru.parse_manifest(satoru._parse_minimal_toml(
            'contract = 1\n[game]\nid = "x"\nname = "X"\nstatus = "rc"\n'
            '[source]\nkind = "release"\nurl = "https://example.invalid/x.tar.gz"\n'
            'sha256 = "abc"\nversion = "v1"\n'
            '[commands]\nlaunch = "run.sh"\n'))
        text = satoru.write_shim(self.paths, manifest, write=False)
        self.assertNotIn("update-check", text)
        self.assertIn("exec ", text, "it still has to be able to launch the game")


class TheGuardActuallyFires(unittest.TestCase):
    """Containing the right text is not the same as refusing to run.

    The shim is written under a path shaped like the one macOS uses for a
    translocated bundle, then run for real. It has to stop with exit 10 and say
    why, rather than start a Wine prefix it cannot write to.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        translocated = os.path.join(self.dir, "var", "folders", "T",
                                    "AppTranslocation", "UUID", "d")
        self.paths = satoru.Paths(home=translocated)
        self.manifest, _ = satoru.parse_manifest(satoru._parse_minimal_toml(MANIFEST_TOML))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, env_extra):
        import subprocess
        path = satoru.write_shim(self.paths, self.manifest)
        env = dict(os.environ)
        env.pop("TERM", None)
        env.update(env_extra)
        proc = subprocess.Popen([path], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env)
        out, err = proc.communicate(timeout=30)
        return proc.returncode, err.decode("utf-8", "replace")

    def test_it_refuses_to_launch_and_says_why(self):
        rc, err = self._run({"TERM": "dumb"})
        self.assertEqual(rc, 10)
        self.assertIn("read-only", err)
        self.assertIn("Applications", err)

    def test_a_terminal_run_gets_text_and_no_dialog(self):
        # A suite that pops a GUI alert on every run is a suite nobody runs.
        text = satoru.write_shim(self.paths, self.manifest, write=False)
        alert = index_of(text, "osascript")
        gate = index_of(text, 'if [ -z "${TERM:-}" ]')
        self.assertLess(gate, alert, "the alert must be behind the no-terminal check")


if __name__ == "__main__":
    unittest.main()
