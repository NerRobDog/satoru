"""What the launcher shows for a game, from either manifest shape.

A contract-1 manifest moves the commands out of [game] and changes what Setup
means: the pack no longer names a script the umbrella shells out to, it names a
release the umbrella installs. Reading a v1 manifest with the old flat rules
would leave every action greyed out and say nothing about why, which is exactly
the dead end this contract exists to remove.
"""
import os
import shutil
import tempfile
import unittest

from support import satoru

LEGACY = '''
[game]
name   = "Age of Empires IV"
id     = "aoe4"
status = "rc"
home   = "~/aoe4-pack"
setup        = "bash games/aoe4/bootstrap.sh"
launch       = "~/aoe4-pack/aoe4.sh"
launch_plain = "~/aoe4-pack/aoe4.sh --plain"
logs         = "~/aoe4-pack/logs"
'''

V1_INSTALLABLE = '''
contract = 1
[game]
id = "aoe4"
name = "Age of Empires IV"
status = "rc"
summary = "Flat 60, open engine"
[source]
kind = "release"
url = "https://github.com/NerRobDog/dxmt-aoe4-pack/releases/download/v0.1/p.tar.gz"
sha256 = "abc"
version = "v0.1"
[commands]
install = "bash setup.sh"
launch = "aoe4.sh"
'''

V1_MANUAL = '''
contract = 1
[game]
id = "ow2"
name = "Overwatch 2"
status = "playable"
[install]
manual_url = "https://github.com/NerRobDog/dxmt-ow2-pack#install"
foreign_note = "modifies your CrossOver bottle"
'''


def game_from(text, directory):
    path = os.path.join(directory, "game.toml")
    with open(path, "w") as fh:
        fh.write(text)
    return satoru.Game(path, satoru.load_toml(path))


class Legacy(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.gamedir = os.path.join(self.dir, "aoe4")
        os.makedirs(self.gamedir)
        self.game = game_from(LEGACY, self.gamedir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_it_still_reads_the_shape_every_pack_ships_today(self):
        self.assertEqual(self.game.name, "Age of Empires IV")
        self.assertEqual(self.game.status, "rc")
        self.assertEqual(self.game.contract, 0)
        self.assertEqual(self.game.cmds["setup"], "bash games/aoe4/bootstrap.sh")
        self.assertEqual(self.game.cmds["logs"], "~/aoe4-pack/logs")

    def test_it_has_no_manual_url_to_offer(self):
        self.assertEqual(self.game.manual_url, "")


class V1Installable(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.gamedir = os.path.join(self.dir, "aoe4")
        os.makedirs(self.gamedir)
        self.game = game_from(V1_INSTALLABLE, self.gamedir)
        # Its own empty world. Without this the answer comes from whatever the
        # machine running the tests happens to have installed, and the suite
        # passes or fails on that — which it did, the first time a real install
        # landed on the developer's own disk.
        self.paths = satoru.Paths(home=os.path.join(self.dir, "home"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_setup_is_offered_because_the_umbrella_can_install_it(self):
        state, detail = self.game.action_state("setup", self.paths)
        self.assertEqual(state, "ok")
        self.assertIn("v0.1", detail)

    def test_launch_waits_until_something_is_installed(self):
        state, _ = self.game.action_state("launch", self.paths)
        self.assertEqual(state, "missing",
                         "nothing is installed yet, so Launch cannot claim to work")

    def test_it_validates_clean(self):
        self.assertEqual(self.game.validate(), [])


class V1Manual(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.gamedir = os.path.join(self.dir, "ow2")
        os.makedirs(self.gamedir)
        self.game = game_from(V1_MANUAL, self.gamedir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_pack_with_no_release_says_where_the_instructions_are(self):
        self.assertEqual(self.game.manual_url,
                         "https://github.com/NerRobDog/dxmt-ow2-pack#install")

    def test_setup_is_not_offered_and_that_is_not_an_error(self):
        state, _ = self.game.action_state("setup", satoru.Paths(
            home=os.path.join(self.dir, "home")))
        self.assertEqual(state, "soon")
        self.assertEqual(self.game.validate(), [],
                         "declaring an honest manual install is not a broken manifest")

    def test_it_declares_that_it_touches_something_of_yours(self):
        self.assertIn("CrossOver", self.game.foreign_note)

    def test_the_listing_points_at_the_instructions(self):
        import io
        out = io.StringIO()
        satoru.describe([self.game], out=out)
        self.assertIn("dxmt-ow2-pack#install", out.getvalue())


class Broken(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.gamedir = os.path.join(self.dir, "aoe4")
        os.makedirs(self.gamedir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_manifest_errors_surface_through_validate(self):
        game = game_from('contract = 1\n[game]\nid = "aoe4"\nstatus = "soon"\n',
                         self.gamedir)
        errs = game.validate()
        self.assertTrue(any("status" in e for e in errs), errs)
        self.assertTrue(any("name" in e for e in errs), errs)

    def test_an_id_that_disagrees_with_its_directory_is_reported(self):
        game = game_from('contract = 1\n[game]\nid = "other"\nname = "X"\nstatus = "wip"\n',
                         self.gamedir)
        self.assertTrue(any("directory" in e for e in game.validate()), game.validate())


if __name__ == "__main__":
    unittest.main()
