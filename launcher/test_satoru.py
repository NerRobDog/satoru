"""Smoke tests for the launcher: python3 -m unittest launcher/test_satoru.py"""
import os
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


class RepoGames(unittest.TestCase):
    def test_repo_tomls_load(self):
        games = satoru.load_games()
        ids = [g.id for g in games]
        self.assertIn("aoe4", ids)
        self.assertEqual(games[0].id, "aoe4")  # rc sorts first
        aoe4 = games[0]
        self.assertEqual(aoe4.cmds["setup"], "bash games/aoe4/setup.sh")
        self.assertEqual(aoe4.action_state("setup")[0], "missing")  # submodule not checked out
        with open(os.devnull, "w") as sink:
            self.assertTrue(satoru.check(out=sink))


if __name__ == "__main__":
    unittest.main()
