"""The pack manifest, contract v1.

Two shapes have to parse: what the packs ship today (a flat [game] table) and
the v1 contract. Legacy is not a courtesy — all four packs are legacy right now,
and they keep working while the contract lands.
"""
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
profile      = "~/aoe4-pack/README-local.txt"
logs         = "~/aoe4-pack/logs"

notes = """
Flat 60 on M1 Pro.
"""
'''

V1_TOML = '''
contract = 1

[game]
id      = "aoe4"
name    = "Age of Empires IV"
status  = "rc"
summary = "Flat 60, open engine, no CrossOver at runtime"
notes   = """
Longer text.
"""

[source]
kind    = "release"
url     = "https://example.invalid/pack-v0.1.tar.gz"
sha256  = "0540919b44aaa83ab33ef7e16b6ed2da665f3b0a588bf5e1a82b1cdb5c715b4d"
size    = 133000000
version = "v0.1"
check   = "github-release"

[requires]
arch    = "arm64"
macos   = ">=26"
rosetta = true
disk_gb = 4
tools   = ["python3"]

[install]
home_authoritative = true
foreign_note = ""

[commands]
preflight    = "bash setup.sh --preflight"
install      = "bash setup.sh"
launch       = "aoe4.sh"
launch_plain = "aoe4.sh --plain"
uninstall    = "bash uninstall.sh"
'''


def parse(text):
    return satoru.parse_manifest(satoru._parse_minimal_toml(text))


class Legacy(unittest.TestCase):
    def test_parses_with_no_errors(self):
        m, errors = parse(LEGACY)
        self.assertEqual(errors, [])
        self.assertEqual(m["contract"], 0)

    def test_setup_becomes_install(self):
        m, _ = parse(LEGACY)
        self.assertEqual(m["commands"]["install"], "bash games/aoe4/bootstrap.sh")

    def test_home_and_paths_survive(self):
        m, _ = parse(LEGACY)
        self.assertEqual(m["install"]["home"], "~/aoe4-pack")
        self.assertEqual(m["paths"]["logs"], "~/aoe4-pack/logs")

    def test_declares_no_source_so_cannot_be_downloaded(self):
        m, _ = parse(LEGACY)
        self.assertIsNone(m["source"])


class V1(unittest.TestCase):
    def test_parses_with_no_errors(self):
        m, errors = parse(V1_TOML)
        self.assertEqual(errors, [])
        self.assertEqual(m["contract"], 1)

    def test_sections(self):
        m, _ = parse(V1_TOML)
        self.assertEqual(m["game"]["summary"][:7], "Flat 60")
        self.assertEqual(m["source"]["size"], 133000000)
        self.assertEqual(m["requires"]["tools"], ["python3"])
        self.assertTrue(m["install"]["home_authoritative"])
        self.assertEqual(m["commands"]["preflight"], "bash setup.sh --preflight")

    def test_requires_defaults_when_section_absent(self):
        m, errors = parse('contract = 1\n[game]\nid = "x"\nname = "X"\nstatus = "wip"\n')
        self.assertEqual(errors, [])
        self.assertEqual(m["requires"]["tools"], [])
        self.assertFalse(m["requires"]["rosetta"])


class Refusals(unittest.TestCase):
    def test_contract_from_the_future(self):
        _, errors = parse('contract = 2\n[game]\nid = "x"\nname = "X"\nstatus = "wip"\n')
        self.assertTrue(any("contract 2" in e for e in errors), errors)

    def test_missing_id(self):
        _, errors = parse('contract = 1\n[game]\nname = "X"\nstatus = "wip"\n')
        self.assertTrue(any("id" in e for e in errors), errors)

    def test_bad_id(self):
        _, errors = parse('contract = 1\n[game]\nid = "Age IV"\nname = "X"\nstatus = "wip"\n')
        self.assertTrue(any("id" in e for e in errors), errors)

    def test_bad_status(self):
        _, errors = parse('contract = 1\n[game]\nid = "x"\nname = "X"\nstatus = "soon"\n')
        self.assertTrue(any("status" in e for e in errors), errors)

    def test_source_without_hash_is_refused(self):
        _, errors = parse('contract = 1\n[game]\nid = "x"\nname = "X"\nstatus = "wip"\n'
                          '[source]\nkind = "release"\nurl = "https://e.invalid/p.tar.gz"\n')
        self.assertTrue(any("sha256" in e for e in errors), errors)

    def test_unknown_key_is_a_typo_not_a_feature(self):
        _, errors = parse('contract = 1\n[game]\nid = "x"\nname = "X"\nstatus = "wip"\n'
                          'nots = "typo"\n')
        self.assertTrue(any("nots" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()


class TheRealOnes(unittest.TestCase):
    """The four game.toml files this repository actually ships.

    Fixtures agree with whoever wrote them. These do not.
    """

    def test_every_shipped_manifest_parses_clean(self):
        import glob
        import os
        paths = sorted(glob.glob(os.path.join(satoru.GAMES_DIR, "*", "game.toml")))
        if not paths:
            self.skipTest("submodules are not checked out")
        for path in paths:
            with self.subTest(game=os.path.basename(os.path.dirname(path))):
                m, errors = satoru.parse_manifest(satoru.load_toml(path))
                self.assertEqual(errors, [], "%s: %s" % (path, errors))
                self.assertTrue(m["game"]["id"])
