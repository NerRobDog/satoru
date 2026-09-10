"""The hand-rolled TOML reader.

It is not a fallback: `python3` on this project's own Macs is 3.8, and a clean
macOS ships 3.9 with the Command Line Tools. `tomllib` arrives in 3.11, so for
essentially every user this reader IS the parser. It gets tested accordingly,
including against tomllib where that exists.
"""
import unittest

from support import satoru, has_tomllib

parse = satoru._parse_minimal_toml


class Scalars(unittest.TestCase):
    def test_sections_and_scalars(self):
        self.assertEqual(
            parse('contract = 1\n[game]\nid = "aoe4"\nrc = true\nn = 42\n'),
            {"contract": 1, "game": {"id": "aoe4", "rc": True, "n": 42}},
        )

    def test_comment_and_blank_lines(self):
        self.assertEqual(parse('# c\n\n[a]\n# c\nk = "v"  # trailing\n'),
                         {"a": {"k": "v"}})

    def test_multiline_string(self):
        got = parse('[game]\nnotes = """\nline one\nline two\n"""\n')
        self.assertEqual(got["game"]["notes"], "line one\nline two\n")


class Arrays(unittest.TestCase):
    """[requires] tools = [...] — the v1 manifest needs these."""

    def test_array_of_strings(self):
        self.assertEqual(parse('[r]\ntools = ["dotnet", "ffmpeg"]\n'),
                         {"r": {"tools": ["dotnet", "ffmpeg"]}})

    def test_empty_array(self):
        self.assertEqual(parse("[r]\ntools = []\n"), {"r": {"tools": []}})

    def test_array_with_trailing_comma_and_comment(self):
        self.assertEqual(parse('[r]\ntools = ["gh",]  # one\n'),
                         {"r": {"tools": ["gh"]}})


class SameAsTomllib(unittest.TestCase):
    """Differential: whatever tomllib says on 3.11+ is the definition of right."""

    FIXTURES = [
        'contract = 1\n[game]\nid = "aoe4"\nname = "Age of Empires IV"\n',
        '[requires]\narch = "arm64"\nrosetta = true\ndisk_gb = 4\ntools = ["dotnet"]\n',
        '[game]\nnotes = """\na\nb\n"""\nstatus = "rc"\n',
        '[install]\nhome_authoritative = false\nforeign_note = "touches your bottle"\n',
        # A home path is a filename, and a filename can hold quotes and backslashes.
        # Unescaping these with a chain of .replace() gets them wrong in either order.
        r'[g]' + '\n' + r'home = "/x/A \"quoted\" \\ path"' + '\n',
        r'[g]' + '\n' + r'trailing = "ends with a backslash \\"' + '\n',
    ]

    @unittest.skipUnless(has_tomllib(), "tomllib needs Python 3.11+")
    def test_agrees_with_tomllib(self):
        import tomllib
        for text in self.FIXTURES:
            with self.subTest(text=text[:40]):
                self.assertEqual(parse(text), tomllib.loads(text))


if __name__ == "__main__":
    unittest.main()
