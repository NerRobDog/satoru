"""Smoke tests for the launcher: python3 -m unittest launcher/test_satoru.py"""
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import satoru  # noqa: E402

SAMPLE = '''
# comment
[game]
name   = "Test Game"
id     = "tg"
status = "rc"
home   = "~/tg-home"
pace   = 60
hud    = true
launch = "~/tg-home/run.sh --flag \\"x\\""   # trailing comment
notes  = """
line one
line two
"""
'''


class MinimalToml(unittest.TestCase):
    def test_subset(self):
        d = satoru._parse_minimal_toml(SAMPLE)
        g = d["game"]
        self.assertEqual(g["name"], "Test Game")
        self.assertEqual(g["status"], "rc")
        self.assertEqual(g["pace"], 60)
        self.assertIs(g["hud"], True)
        self.assertEqual(g["launch"], '~/tg-home/run.sh --flag "x"')
        self.assertEqual(g["notes"].strip(), "line one\nline two")

    def test_matches_tomllib_when_available(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("no tomllib on %s" % sys.version.split()[0])
        self.assertEqual(satoru._parse_minimal_toml(SAMPLE), tomllib.loads(SAMPLE))

    def test_bad_line(self):
        with self.assertRaises(ValueError):
            satoru._parse_minimal_toml("[game]\nnonsense\n")


class ActionState(unittest.TestCase):
    def make(self, body):
        d = tempfile.mkdtemp()
        gd = os.path.join(d, "x")
        os.mkdir(gd)
        p = os.path.join(gd, "game.toml")
        with open(p, "w") as f:
            f.write(body)
        return d, satoru.Game(p, satoru.load_toml(p))

    def test_wip_everything_soon(self):
        _, g = self.make('[game]\nname="W"\nid="x"\nstatus="wip"\nlaunch="/bin/ls"\n')
        for key, _, _ in satoru.ACTIONS:
            self.assertEqual(g.action_state(key)[0], "soon")
        self.assertEqual(g.validate(), [])

    def test_rc_missing_vs_ok(self):
        logs = tempfile.mkdtemp()
        _, g = self.make('[game]\nname="R"\nid="x"\nstatus="rc"\n'
                         'setup="bash /nonexistent/setup.sh"\nlaunch="/bin/ls"\nlogs="%s"\n' % logs)
        self.assertEqual(g.action_state("setup")[0], "missing")
        self.assertEqual(g.action_state("launch")[0], "ok")
        self.assertEqual(g.action_state("launch_plain")[0], "soon")
        self.assertEqual(g.action_state("logs"), ("ok", logs))
        self.assertEqual(g.validate(), [])

    def test_validate(self):
        _, g = self.make('[game]\nname="B"\nid="nope"\nstatus="weird"\n')
        errs = g.validate()
        self.assertTrue(any("status" in e for e in errs))
        self.assertTrue(any("id" in e for e in errs))


def _submodule_pin(repo_root, rel_path):
    """The commit the umbrella repo's HEAD pins for the submodule at rel_path
    (the gitlink entry), or None if rel_path isn't a submodule there."""
    out = subprocess.run(
        ["git", "ls-tree", "HEAD", "--", rel_path],
        cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode().strip()
    if not out:
        return None
    fields = out.split()
    if len(fields) < 3 or fields[0] != "160000":
        return None
    return fields[2]


def _submodule_git_source(repo_root, rel_path):
    """Where to read the submodule's objects from: a checked-out working tree
    (use `git -C <dir>`), or the superproject's cached `.git/modules/<path>`
    (use `git --git-dir=<dir>`) when the working tree is empty but the objects
    were still fetched. Returns (dir, is_git_dir) or (None, None) if neither
    exists -- i.e. the submodule was never initialized at all."""
    checkout = os.path.join(repo_root, rel_path)
    if os.path.exists(os.path.join(checkout, ".git")):
        return checkout, False
    modules_dir = os.path.join(repo_root, ".git", "modules", rel_path)
    if os.path.isdir(modules_dir):
        return modules_dir, True
    return None, None


def _blob_mode_at(git_source, is_git_dir, sha, rel_path):
    """git's tree mode for rel_path at sha ('100755', '100644', ...), or None
    if rel_path doesn't exist at that commit."""
    prefix = ["git", "--git-dir", git_source] if is_git_dir else ["git", "-C", git_source]
    out = subprocess.run(
        prefix + ["ls-tree", sha, "--", rel_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode().strip()
    if not out:
        return None
    return out.split()[0]


class RepoGames(unittest.TestCase):
    def test_repo_tomls_load(self):
        games = satoru.load_games()
        ids = [g.id for g in games]
        self.assertIn("aoe4", ids)
        self.assertEqual(games[0].id, "aoe4")  # rc sorts first
        with open(os.devnull, "w") as sink:
            self.assertTrue(satoru.check(out=sink))

    def test_repo_tomls_reads_the_command_from_the_manifest(self):
        """The property that matters isn't any particular script name -- it's
        that whatever a manifest's [commands] key names actually exists in
        that game's submodule at the commit the umbrella repo pins, and is
        executable unless it's invoked through an interpreter (`bash foo.sh`
        doesn't need +x on foo.sh; a bare `foo.sh` does). A pack renaming its
        entry point (games/aoe4/game.toml: bootstrap.sh replacing setup.sh)
        must not silently pass a test that only checked a literal string.
        """
        games = satoru.load_games()
        self.assertTrue(games, "no games found under %s" % satoru.GAMES_DIR)
        checked_any = False
        skipped = []
        for g in games:
            rel = os.path.relpath(g.dir, satoru.ROOT)
            sha = _submodule_pin(satoru.ROOT, rel)
            if sha is None:
                continue  # games/<id> isn't a submodule (or untracked) here
            git_source, is_git_dir = _submodule_git_source(satoru.ROOT, rel)
            if git_source is None:
                skipped.append(g.id)
                continue  # never initialized: no objects anywhere to check against
            for key, cmd in sorted(g.cmds.items()):
                if not cmd:
                    continue  # "soon"/unset actions name nothing to verify
                words = shlex.split(cmd)
                via_interpreter = words[0] in ("bash", "sh", "zsh") and len(words) > 1
                script = words[1] if via_interpreter else words[0]
                script_abs = satoru.resolve(script)
                game_root = os.path.join(satoru.ROOT, rel)
                if os.path.commonpath([script_abs, game_root]) != game_root:
                    continue  # this command doesn't point inside the submodule
                script_rel = os.path.relpath(script_abs, game_root)
                mode = _blob_mode_at(git_source, is_git_dir, sha, script_rel)
                checked_any = True
                self.assertIsNotNone(
                    mode,
                    "games/%s game.toml: %s command %r names %s, which does not "
                    "exist in games/%s at the pinned commit %s"
                    % (g.id, key, cmd, script_rel, g.id, sha[:12]))
                if not via_interpreter:
                    self.assertEqual(
                        mode, "100755",
                        "games/%s game.toml: %s command %r runs %s directly, "
                        "but it is not executable in the pinned submodule (mode %s)"
                        % (g.id, key, cmd, script_rel, mode))
        if not checked_any:
            self.skipTest(
                "no submodule objects available to check against (skipped: %s); "
                "run `git submodule update --init --recursive`" % ", ".join(skipped or ["-"]))


if __name__ == "__main__":
    unittest.main()
