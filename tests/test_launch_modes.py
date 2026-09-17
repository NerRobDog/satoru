"""Launch modes: the optional [modes] part of contract 1.

A pack declares ways to start its game and the values each way needs; satoru
asks, remembers and validates, and hands the answer over as SATORU_MODE and
SATORU_SETTING_<NAME>. The dialog lives in the shim and talks to osascript, so
the tests put a fake osascript first on PATH: it answers from a list and writes
down every call. No real dialog is ever shown.
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest

from support import ROOT, satoru

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
FIXTURE = os.path.join(FIXTURES, "modes-pack", "game.toml")

OFFLINE = "Оффлайн против ботов"
CLIENT = "Подключиться к серверу"
HOST = "Создать сервер и играть"

FAKE_OSASCRIPT = r"""#!/bin/sh
d=${FAKE_OSA_DIR:?}
kind=other
for a in "$@"; do
  case "$a" in
    *"choose from list"*) kind=choose ;;
    *"display dialog"*) kind=ask ;;
    *"display alert"*) kind=alert ;;
  esac
done
found=0
{
  printf '%s' "$kind"
  for a in "$@"; do
    [ "$found" = 1 ] && printf '\t%s' "$a"
    [ "$a" = "end run" ] && found=1
  done
  echo
} >> "$d/calls"
[ "$kind" = alert ] && exit 0
n=$(cat "$d/counter" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$d/counter"
line=$(sed -n "${n}p" "$d/answers")
[ "$line" = "!fail" ] && exit 1
printf '%s\n' "$line"
"""

FAKE_PACK = """#!/bin/sh
{
  echo "args=$*"
  env | grep '^SATORU_MODE=\\|^SATORU_SETTING_' | sort
} > "$SATORU_GAME_HOME/../ran"
[ -z "${PACK_FAILS:-}" ] || { echo "$PACK_FAILS" >&2; exit 2; }
exit 0
"""


def manifest_of(text):
    return satoru.parse_manifest(satoru._parse_minimal_toml(text))


def fixture_manifest():
    return satoru.parse_manifest(satoru.load_toml(FIXTURE))


BASE = ('contract = 1\n[game]\nid = "x"\nname = "X"\nstatus = "rc"\n'
        '[commands]\nlaunch = "run.sh"\n')


class TheManifest(unittest.TestCase):
    def test_the_fixture_is_clean(self):
        manifest, errors = fixture_manifest()
        self.assertEqual(errors, [])

    def test_modes_keep_the_order_they_were_written_in(self):
        manifest, _ = fixture_manifest()
        self.assertEqual([m["id"] for m in manifest["modes"]], ["offline", "client", "host"])
        self.assertEqual(manifest["modes"][0]["label"], OFFLINE)

    def test_settings_belong_to_modes(self):
        manifest, _ = fixture_manifest()
        names = lambda mode: [s["name"] for s in satoru.mode_settings(manifest, mode)]
        self.assertEqual(names("offline"), [])
        self.assertEqual(names("client"), ["nick", "server"])
        self.assertEqual(names("host"), ["nick"])

    def test_a_pack_without_modes_has_none(self):
        manifest, errors = manifest_of(BASE)
        self.assertEqual(errors, [])
        self.assertEqual(manifest["modes"], [])
        self.assertEqual(manifest["settings"], [])

    def refused(self, extra, fragment):
        _, errors = manifest_of(BASE + extra)
        self.assertTrue(any(fragment in e for e in errors),
                        "expected an error containing %r, got %r" % (fragment, errors))

    def test_a_setting_without_modes_is_refused(self):
        self.refused('[setting_nick]\nlabel = "Nick"\nmodes = ["a"]\n', "needs a [modes]")

    def test_a_setting_for_an_undeclared_mode_is_refused(self):
        self.refused('[modes]\na = "A"\n[setting_nick]\nlabel = "N"\nmodes = ["b"]\n',
                     "does not declare")

    def test_a_setting_for_no_mode_is_refused(self):
        self.refused('[modes]\na = "A"\n[setting_nick]\nlabel = "N"\nmodes = []\n',
                     "modes must list")

    def test_an_unknown_kind_is_refused(self):
        self.refused('[modes]\na = "A"\n[setting_n]\nlabel = "N"\nmodes = ["a"]\n'
                     'kind = "email"\n', "kind")

    def test_a_pattern_with_a_backslash_is_refused(self):
        # Python and grep -E disagree about escapes; only what both read alike.
        self.refused('[modes]\na = "A"\n[setting_n]\nlabel = "N"\nmodes = ["a"]\n'
                     "pattern = '\\d+'\n", "without backslashes")

    def test_a_class_name_is_refused(self):
        # grep -E knows [[:alpha:]], Python reads it as a set of characters.
        self.refused('[modes]\na = "A"\n[setting_n]\nlabel = "N"\nmodes = ["a"]\n'
                     'pattern = "[[:alpha:]]+"\n', "[:class:]")

    def test_a_pattern_that_does_not_compile_is_refused(self):
        self.refused('[modes]\na = "A"\n[setting_n]\nlabel = "N"\nmodes = ["a"]\n'
                     'pattern = "[a-"\n', "does not compile")

    def test_a_pattern_on_an_address_is_refused(self):
        self.refused('[modes]\na = "A"\n[setting_n]\nlabel = "N"\nmodes = ["a"]\n'
                     'kind = "ipv4"\npattern = "[0-9]+"\n', "only applies")

    def test_two_modes_with_one_label_are_refused(self):
        # The dialog answers with the label, so two alike could not be told apart.
        self.refused('[modes]\na = "Same"\nb = "Same"\n', "used twice")

    def test_a_bad_mode_id_is_refused(self):
        self.refused('[modes]\nBig = "A"\n', "mode id")

    def test_an_unknown_key_in_a_setting_is_a_typo(self):
        self.refused('[modes]\na = "A"\n[setting_n]\nlabel = "N"\nmodes = ["a"]\n'
                     'lable = "x"\n', "unknown key")

    def test_the_older_manifest_shape_does_not_get_modes(self):
        _, errors = satoru.parse_manifest(satoru._parse_minimal_toml(
            '[game]\nid = "x"\nname = "X"\nstatus = "rc"\n[modes]\na = "A"\n'))
        self.assertIn("unknown section [modes]", errors)

    def test_check_accepts_a_catalogue_with_the_fixture(self):
        out = io.StringIO()
        self.assertTrue(satoru.check(FIXTURES, out=out), out.getvalue())
        self.assertIn("modes-pack/game.toml: ok", out.getvalue())


class Validation(unittest.TestCase):
    def setUp(self):
        manifest, _ = fixture_manifest()
        self.nick, self.server = manifest["settings"]

    def test_nick(self):
        self.assertIsNone(satoru.setting_problem(self.nick, "Player_1"))
        for bad in ("", "bad.nick", "a" * 17, "$(touch x)", "ник"):
            self.assertEqual(satoru.setting_problem(self.nick, bad), self.nick["error"], bad)

    def test_server(self):
        for good in ("192.168.0.116", "0.0.0.0", "255.255.255.255"):
            self.assertIsNone(satoru.setting_problem(self.server, good), good)
        for bad in ("", "1.2.3", "1.2.3.256", "1.2.3.4.5", "a.b.c.d", "1..2.3", "1.2.3.4 "):
            self.assertIsNotNone(satoru.setting_problem(self.server, bad), bad)

    def test_the_default_message_names_the_setting(self):
        self.assertIn("IPv4", satoru.setting_problem(self.server, "nope"))


class Remembering(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.manifest, _ = fixture_manifest()
        self.asked = []
        self.said = []

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def answers(self, *values):
        queue = list(values)

        def ask(setting, current):
            self.asked.append((setting["name"], current))
            return queue.pop(0)
        return ask

    def test_no_state_means_the_first_mode(self):
        state = satoru.read_launch_state(self.home)
        self.assertEqual(satoru.launch_mode_default(self.manifest, state), "offline")

    def test_the_first_time_asks_and_the_second_does_not(self):
        env = satoru.resolve_launch_mode(self.manifest, self.home, "client",
                                         self.answers("Player_1", "10.0.0.2"), self.said.append)
        self.assertEqual(env, {"SATORU_MODE": "client", "SATORU_SETTING_NICK": "Player_1",
                               "SATORU_SETTING_SERVER": "10.0.0.2"})
        self.assertEqual(len(self.asked), 2)
        env = satoru.resolve_launch_mode(self.manifest, self.home, "client",
                                         self.answers(), self.said.append)
        self.assertEqual(env["SATORU_SETTING_SERVER"], "10.0.0.2")
        self.assertEqual(len(self.asked), 2, "a saved, valid value is not asked again")
        state = satoru.read_launch_state(self.home)
        self.assertEqual(satoru.launch_mode_default(self.manifest, state), "client")

    def test_a_refused_answer_is_explained_and_asked_again(self):
        env = satoru.resolve_launch_mode(self.manifest, self.home, "host",
                                         self.answers("bad.nick", " Good "), self.said.append)
        self.assertEqual(env, {"SATORU_MODE": "host", "SATORU_SETTING_NICK": "Good"})
        self.assertEqual(self.said, [self.manifest["settings"][0]["error"]])
        self.assertEqual(self.asked, [("nick", ""), ("nick", "bad.nick")])

    def test_cancel_saves_nothing(self):
        env = satoru.resolve_launch_mode(self.manifest, self.home, "client",
                                         self.answers(None), self.said.append)
        self.assertIsNone(env)
        self.assertFalse(os.path.exists(os.path.join(self.home, satoru.LAUNCH_STATE_FILE)))

    def test_offline_asks_nothing_and_exports_only_the_mode(self):
        env = satoru.resolve_launch_mode(self.manifest, self.home, "offline",
                                         self.answers(), self.said.append)
        self.assertEqual(env, {"SATORU_MODE": "offline"})

    def test_change_settings_asks_everything_with_the_current_values(self):
        satoru.write_launch_state(self.home, "host", {"nick": "Old", "server": "1.1.1.1"})
        env = satoru.resolve_launch_mode(self.manifest, self.home, "host",
                                         self.answers("Old", "2.2.2.2"), self.said.append,
                                         force=True)
        self.assertEqual(self.asked, [("nick", "Old"), ("server", "1.1.1.1")])
        self.assertEqual(env, {"SATORU_MODE": "host", "SATORU_SETTING_NICK": "Old"})
        self.assertEqual(satoru.read_launch_state(self.home)["settings"]["server"], "2.2.2.2")


class ShimHarness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=os.path.join(self.dir, "user"))
        self.manifest, _ = fixture_manifest()
        self.shim = satoru.write_shim(self.paths, self.manifest, check_updates=False)
        self.home = os.path.dirname(self.shim)
        with open(os.path.join(self.home, "run.sh"), "w") as fh:
            fh.write(FAKE_PACK)
        os.chmod(os.path.join(self.home, "run.sh"), 0o755)
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "osascript"), "w") as fh:
            fh.write(FAKE_OSASCRIPT)
        os.chmod(os.path.join(self.bin, "osascript"), 0o755)
        self.osa = os.path.join(self.dir, "osa")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def launch(self, answers=(), args=(), env=None):
        shutil.rmtree(self.osa, ignore_errors=True)
        os.makedirs(self.osa)
        with open(os.path.join(self.osa, "answers"), "w", encoding="utf-8") as fh:
            fh.write("".join(a + "\n" for a in answers))
        _remove(os.path.join(self.home, "..", "ran"))
        full = {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.dir,
                "FAKE_OSA_DIR": self.osa, "LANG": "en_US.UTF-8"}
        full.update(env or {})
        proc = subprocess.Popen([self.shim] + list(args), env=full, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        out, err = proc.communicate(timeout=60)
        return proc.returncode, err.decode("utf-8")

    def calls(self):
        try:
            with open(os.path.join(self.osa, "calls"), encoding="utf-8") as fh:
                return [line.split("\t") for line in fh.read().splitlines()]
        except OSError:
            return []

    def ran(self):
        try:
            with open(os.path.join(self.home, "..", "ran"), encoding="utf-8") as fh:
                return fh.read().splitlines()
        except OSError:
            return None


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class TheShimWithoutModes(unittest.TestCase):
    def test_a_pack_without_modes_gets_no_dialog_code(self):
        manifest, _ = manifest_of(BASE)
        text = satoru.shim_text(satoru.Paths(home="/nonexistent"), manifest)
        for word in ("SATORU_MODE", "choose from list", satoru.LAUNCH_STATE_FILE):
            self.assertNotIn(word, text)


class TheDialog(ShimHarness):
    def test_sh_can_parse_it(self):
        self.assertEqual(subprocess.call(["/bin/sh", "-n", self.shim]), 0)

    def test_first_launch_asks_and_the_pack_gets_the_answers(self):
        rc, err = self.launch(["ok:" + CLIENT, "ok:Player_1", "ok:192.168.0.116"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ran(), ["args=", "SATORU_MODE=client",
                                      "SATORU_SETTING_NICK=Player_1",
                                      "SATORU_SETTING_SERVER=192.168.0.116"])
        kinds = [c[0] for c in self.calls()]
        self.assertEqual(kinds, ["choose", "ask", "ask"])
        choose = self.calls()[0]
        self.assertEqual(choose[2], OFFLINE, "with nothing saved, the first mode is the default")
        self.assertEqual(choose[5:], [OFFLINE, CLIENT, HOST, satoru.CHANGE_SETTINGS_LABEL])

    def test_the_second_launch_highlights_the_last_choice_and_asks_nothing_else(self):
        self.launch(["ok:" + CLIENT, "ok:Player_1", "ok:192.168.0.116"])
        rc, err = self.launch(["ok:" + CLIENT])
        self.assertEqual(rc, 0, err)
        self.assertEqual([c[0] for c in self.calls()], ["choose"])
        self.assertEqual(self.calls()[0][2], CLIENT)
        self.assertIn("SATORU_SETTING_SERVER=192.168.0.116", self.ran())

    def test_a_refused_value_is_explained_and_asked_again_with_what_was_typed(self):
        rc, err = self.launch(["ok:" + HOST, "ok:bad.nick", "ok:Good"])
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        self.assertEqual([c[0] for c in calls], ["choose", "ask", "alert", "ask"])
        self.assertEqual(calls[2][3], self.manifest["settings"][0]["error"])
        self.assertEqual(calls[3][2], "bad.nick")
        self.assertEqual(self.ran(), ["args=", "SATORU_MODE=host", "SATORU_SETTING_NICK=Good"])

    def test_cancel_in_the_list_starts_nothing_and_exits_twenty(self):
        rc, _ = self.launch(["cancel:"])
        self.assertEqual(rc, 20)
        self.assertIsNone(self.ran())

    def test_cancel_in_a_field_starts_nothing_and_saves_nothing(self):
        rc, _ = self.launch(["ok:" + CLIENT, "cancel:"])
        self.assertEqual(rc, 20)
        self.assertIsNone(self.ran())
        self.assertFalse(os.path.exists(os.path.join(self.home, satoru.LAUNCH_STATE_FILE)))

    def test_change_settings_prefills_and_returns_to_the_list(self):
        satoru.write_launch_state(self.home, "host", {"nick": "Old", "server": "1.1.1.1"})
        rc, err = self.launch(["ok:" + satoru.CHANGE_SETTINGS_LABEL, "ok:New", "ok:2.2.2.2",
                               "ok:" + CLIENT])
        self.assertEqual(rc, 0, err)
        calls = self.calls()
        self.assertEqual([c[0] for c in calls], ["choose", "ask", "ask", "choose"])
        self.assertEqual((calls[1][2], calls[2][2]), ("Old", "1.1.1.1"))
        self.assertIn("SATORU_SETTING_SERVER=2.2.2.2", self.ran())

    def test_no_dialog_possible_is_a_precondition_not_a_cancel(self):
        rc, err = self.launch(["!fail"])
        self.assertEqual(rc, 10)
        self.assertIn("SATORU_MODE", err)

    def test_plain_reaches_the_pack(self):
        rc, _ = self.launch(["ok:" + OFFLINE], args=["--plain"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.ran()[0], "args=--plain")

    def test_a_refusal_from_the_pack_is_shown_when_there_is_no_terminal(self):
        rc, err = self.launch(["ok:" + OFFLINE], env={"PACK_FAILS": "Invalid server"})
        self.assertEqual(rc, 2)
        self.assertEqual(self.calls()[-1][0], "alert")
        self.assertIn("Invalid server", self.calls()[-1][3])
        self.assertIn("Invalid server", err, "stderr is still passed on")

    def test_a_terminal_gets_no_alert(self):
        rc, _ = self.launch(["ok:" + OFFLINE], env={"PACK_FAILS": "x", "TERM": "xterm"})
        self.assertEqual(rc, 2)
        self.assertNotIn("alert", [c[0] for c in self.calls()])

    def test_what_the_shim_saves_python_reads(self):
        self.launch(["ok:" + CLIENT, "ok:Player_1", "ok:10.1.2.3"])
        state = satoru.read_launch_state(self.home)
        self.assertEqual(state, {"mode": "client",
                                 "settings": {"nick": "Player_1", "server": "10.1.2.3"}})


class TheModeFromTheCaller(ShimHarness):
    """SATORU_MODE set means the choice was made elsewhere: no dialog at all."""

    def test_no_osascript_is_called(self):
        satoru.write_launch_state(self.home, "offline", {"nick": "Saved"})
        rc, err = self.launch(env={"SATORU_MODE": "host"})
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.ran(), ["args=", "SATORU_MODE=host", "SATORU_SETTING_NICK=Saved"])

    def test_the_environment_beats_the_saved_value(self):
        satoru.write_launch_state(self.home, "client", {"nick": "Saved", "server": "1.1.1.1"})
        rc, _ = self.launch(env={"SATORU_MODE": "client", "SATORU_SETTING_SERVER": "9.9.9.9"})
        self.assertEqual(rc, 0)
        self.assertIn("SATORU_SETTING_SERVER=9.9.9.9", self.ran())

    def test_an_unknown_mode_is_refused(self):
        rc, err = self.launch(env={"SATORU_MODE": "lan"})
        self.assertEqual(rc, 10)
        self.assertIn("offline client host", err)
        self.assertIsNone(self.ran())

    def test_a_missing_value_is_refused_by_name(self):
        rc, err = self.launch(env={"SATORU_MODE": "client", "SATORU_SETTING_NICK": "P"})
        self.assertEqual(rc, 10)
        self.assertIn("SATORU_SETTING_SERVER", err)

    def test_shell_and_python_agree_on_every_value(self):
        nick, server = self.manifest["settings"]
        values = ["Player_1", "a" * 16, "a" * 17, "bad.nick", "ник", "$(id)", "x y",
                  "192.168.0.1", "256.1.1.1", "1.2.3", "01.2.3.4", "0001.2.3.4", "1.2.3.4."]
        for value in values:
            for setting, env in ((nick, {"SATORU_MODE": "host", "SATORU_SETTING_NICK": value}),
                                 (server, {"SATORU_MODE": "client", "SATORU_SETTING_NICK": "P",
                                           "SATORU_SETTING_SERVER": value})):
                _remove(os.path.join(self.home, satoru.LAUNCH_STATE_FILE))
                rc, _ = self.launch(env=env)
                python_ok = satoru.setting_problem(setting, value) is None
                self.assertEqual(rc == 0, python_ok, "%s=%r: shell rc %d" % (
                    setting["name"], value, rc))


class LabelsAreData(ShimHarness):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=os.path.join(self.dir, "user"))
        evil = "$(touch %s/pwned) \"q' `id`" % self.dir
        self.label = evil
        self.manifest, errors = manifest_of(
            BASE + "[modes]\na = %s\n[setting_n]\nlabel = %s\nmodes = [\"a\"]\n"
            % (satoru._toml_string(evil), satoru._toml_string(evil)))
        self.assertEqual(errors, [])
        self.shim = satoru.write_shim(self.paths, self.manifest, check_updates=False)
        self.home = os.path.dirname(self.shim)
        with open(os.path.join(self.home, "run.sh"), "w") as fh:
            fh.write(FAKE_PACK)
        os.chmod(os.path.join(self.home, "run.sh"), 0o755)
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "osascript"), "w") as fh:
            fh.write(FAKE_OSASCRIPT)
        os.chmod(os.path.join(self.bin, "osascript"), 0o755)
        self.osa = os.path.join(self.dir, "osa")

    def test_nothing_in_a_label_is_executed(self):
        rc, err = self.launch(["ok:" + self.label, "ok:$(touch pwned2)"])
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "pwned")))
        self.assertEqual(self.calls()[0][5], self.label)
        self.assertIn("SATORU_SETTING_N=$(touch pwned2)", self.ran())


class TheLauncher(unittest.TestCase):
    """The TUI side: the same modes as menu entries, asked in the terminal."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = satoru.Paths(home=self.dir)
        self.game = satoru.Game(FIXTURE, satoru.load_toml(FIXTURE))
        satoru.write_shim(self.paths, self.game.manifest, check_updates=False)
        self.home = self.paths.home(self.game.name)
        self.called = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def call(self, argv, env=None):
        self.called.append((argv, dict((k, v) for k, v in env.items()
                                       if k.startswith("SATORU_"))))
        return 0

    def test_the_menu_lists_the_modes_in_place_of_launch(self):
        keys = [k for k, _, _ in self.game.actions()]
        self.assertEqual(keys, ["setup", "mode:offline", "mode:client", "mode:host",
                                "settings", "launch_plain", "profile", "logs"])
        labels = dict((k, l) for k, l, _ in self.game.actions())
        self.assertEqual(labels["mode:client"], "Launch: " + CLIENT)

    def test_a_pack_without_modes_keeps_the_old_menu(self):
        manifest_game = satoru.Game("/x/x/game.toml", satoru._parse_minimal_toml(BASE))
        self.assertEqual(manifest_game.actions(), satoru.ACTIONS)

    def test_the_last_choice_is_marked(self):
        satoru.write_launch_state(self.home, "host", {})
        self.assertEqual(self.game.last_mode(self.paths), "host")
        state, detail = self.game.action_state("mode:host", self.paths)
        self.assertEqual(state, "ok")
        self.assertIn("(last)", detail)
        self.assertNotIn("(last)", self.game.action_state("mode:client", self.paths)[1])

    def test_launching_a_mode_asks_then_runs_the_shim_with_the_mode_set(self):
        answers = ["Player_1", "10.0.0.5"]
        rc, message = satoru.launch_with_mode(
            self.game, self.paths, "mode:client", ask=lambda s, c: answers.pop(0),
            say=lambda t: None, call=self.call)
        self.assertEqual(rc, 0, message)
        argv, env = self.called[0]
        self.assertEqual(argv, [os.path.join(self.home, "launch")])
        self.assertEqual(env["SATORU_MODE"], "client")
        self.assertEqual(env["SATORU_SETTING_SERVER"], "10.0.0.5")

    def test_plain_uses_the_last_mode(self):
        satoru.write_launch_state(self.home, "host", {"nick": "Saved"})
        satoru.launch_with_mode(self.game, self.paths, "launch_plain",
                                ask=lambda s, c: None, say=lambda t: None, call=self.call)
        argv, env = self.called[0]
        self.assertEqual(argv[1:], ["--plain"])
        self.assertEqual(env["SATORU_MODE"], "host")

    def test_cancel_runs_nothing(self):
        rc, _ = satoru.launch_with_mode(self.game, self.paths, "mode:host",
                                        ask=lambda s, c: None, say=lambda t: None,
                                        call=self.call)
        self.assertEqual(rc, 20)
        self.assertEqual(self.called, [])

    def test_listing_shows_the_modes(self):
        out = io.StringIO()
        old = satoru.current_paths
        satoru.current_paths = lambda: self.paths
        try:
            satoru.describe([self.game], out=out, paths=self.paths)
        finally:
            satoru.current_paths = old
        self.assertIn("Launch: " + HOST, out.getvalue())


if __name__ == "__main__":
    unittest.main()
