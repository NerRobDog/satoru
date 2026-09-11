"""The manifest language, decided rather than inherited.

A pack manifest is written by someone who is not us and read on a Mac whose
Python we do not choose. Until now that meant two readers - tomllib where the
interpreter had it, a hand-rolled one everywhere else - and they did not agree.
An audit of 58 differences found the same shape over and over: the author writes
legal TOML on 3.13, the user runs the 3.9 that ships with macOS, and the value
silently arrives different. A launch command with an accent in a path is the
clearest case: correct for the author, a directory that does not exist for the
user, and no error anywhere.

So there is one reader now, on every interpreter, and this file is its
specification. Three tables:

  SUBSET      what a manifest may contain. Read identically everywhere, and -
              where the interpreter has tomllib - identical to what tomllib says.
  NOT_TOML    input that is not TOML at all. Refused, with the line named.
  OUTSIDE     input that IS legal TOML and that we deliberately do not accept.
              Refused loudly rather than guessed at. tomllib takes these; we do
              not, and the test says so out loud so that nobody mistakes this
              reader for a complete one.

The third table is the honest part. A subset that refuses what it cannot do is a
contract; a subset that guesses is a trap.
"""
import unittest

from support import satoru


def has_tomllib():
    try:
        import tomllib  # noqa: F401
        return True
    except ImportError:
        return False


# (name, text, expected)
SUBSET = [
    ("empty file", "", {}),
    ("comment only", "# nothing here\n", {}),
    ("bare key and string", 'a = "x"\n', {"a": "x"}),
    ("no trailing newline", 'a = "x"', {"a": "x"}),
    ("crlf", 'a = "x"\r\n[s]\r\nb = 1\r\n', {"a": "x", "s": {"b": 1}}),
    ("bom", '\ufeffcontract = 1\n', {"contract": 1}),
    ("comment after value", 'a = "x"  # why\n', {"a": "x"}),
    ("comment after header", '[s] # why\nb = 1\n', {"s": {"b": 1}}),
    ("hash inside a string", 'a = "no # comment"\n', {"a": "no # comment"}),
    ("escaped quote", 'a = "say \\"hi\\""\n', {"a": 'say "hi"'}),
    ("escaped backslash", 'a = "C:\\\\dir"\n', {"a": "C:\\dir"}),
    ("string ending in a backslash", 'a = "back\\\\"\n', {"a": "back\\"}),
    ("named escapes", 'a = "x\\ty\\nz\\r\\b\\f"\n', {"a": "x\ty\nz\r\b\f"}),
    ("bmp escape", 'a = "caf\\u00e9"\n', {"a": "caf\u00e9"}),
    ("astral escape", 'a = "hi \\U0001F600"\n', {"a": "hi \U0001F600"}),
    ("true and false", "a = true\nb = false\n", {"a": True, "b": False}),
    ("integers", "a = 0\nb = -7\nc = 1_000\n", {"a": 0, "b": -7, "c": 1000}),
    ("section", '[game]\nid = "aoe4"\n', {"game": {"id": "aoe4"}}),
    ("two sections", '[a]\nx = 1\n[b]\ny = 2\n', {"a": {"x": 1}, "b": {"y": 2}}),
    ("literal string", "a = 'C:\\dir'\n", {"a": "C:\\dir"}),
    ("literal with a quote", "a = 'say \"hi\"'\n", {"a": 'say "hi"'}),
    ("empty array", "a = []\n", {"a": []}),
    ("array of strings", 'a = ["x", "y"]\n', {"a": ["x", "y"]}),
    ("array over several lines",
     'a = [\n  "x",\n  "y",\n]\n', {"a": ["x", "y"]}),
    ("array with a comment inside",
     'a = [\n  "x",  # first\n  "y",\n]\n', {"a": ["x", "y"]}),
    ("array item with a comma and a bracket",
     'a = ["x,y", "z]"]\n', {"a": ["x,y", "z]"]}),
    ("array item ending in a backslash",
     'a = ["back\\\\", "next"]\n', {"a": ["back\\", "next"]}),
    ("array of integers", "a = [1, 2, 3]\n", {"a": [1, 2, 3]}),
    ("multi-line string",
     'a = """one\ntwo"""\n', {"a": "one\ntwo"}),
    ("multi-line strips exactly one leading newline",
     'a = """\none"""\n', {"a": "one"}),
    ("multi-line keeps the second leading newline",
     'a = """\n\none"""\n', {"a": "\none"}),
    ("multi-line keeps a trailing newline",
     'a = """one\n"""\n', {"a": "one\n"}),
    ("multi-line processes escapes",
     'a = """say \\"hi\\" \\u00e9"""\n', {"a": 'say "hi" \u00e9'}),
    ("multi-line line continuation",
     'a = """one \\\n    two"""\n', {"a": "one two"}),
    ("multi-line containing a hash and a bracket",
     'a = """# not a comment\n[not a section]"""\n',
     {"a": "# not a comment\n[not a section]"}),
    ("multi-line with a lone quote inside",
     'a = """say "hi" here"""\n', {"a": 'say "hi" here'}),
    ("comment after the closing delimiter",
     'a = """x"""  # why\nb = 1\n', {"a": "x", "b": 1}),
    ("multi-line literal",
     "a = '''one\\two'''\n", {"a": "one\\two"}),
    ("multi-line literal strips one leading newline",
     "a = '''\none'''\n", {"a": "one"}),
]

# (name, text, what the message should mention)
NOT_TOML = [
    ("missing equals", "a\n", "="),
    ("empty key", '= "x"\n', "key"),
    ("missing value", "a =\n", "value"),
    ("unterminated string", 'a = "x\n', "unterminated"),
    ("unterminated multi-line", 'a = """x\ny\n', "unterminated"),
    ("unterminated array", 'a = ["x",\n', "unterminated"),
    ("unterminated header", "[game\nid = 1\n", "["),
    ("empty header", "[]\n", "name"),
    ("trailing junk after a string", 'a = "x" junk\n', "junk"),
    ("trailing junk after an array", 'a = ["x"] junk\n', "junk"),
    ("trailing junk after an integer", "a = 1 junk\n", "junk"),
    ("missing comma in an array", 'a = ["x" "y"]\n', ","),
    ("duplicate key", 'a = "x"\na = "y"\n', "twice"),
    ("duplicate section", "[s]\na = 1\n[s]\nb = 2\n", "twice"),
    ("key then section of the same name", 'game = 1\n[game]\na = 1\n', "twice"),
    ("unknown escape", 'a = "\\q"\n', "escape"),
    ("truncated unicode escape", 'a = "\\u00"\n', "hex"),
    ("lone backslash at the end of a string", 'a = "x\\"\n', "unterminated"),
    ("leading zero", "a = 01\n", "value"),
    ("raw control character in a string", 'a = "x\x07y"\n', "control"),
]

# (name, text) - legal TOML that this reader refuses on purpose
OUTSIDE = [
    ("float", "a = 1.5\n"),
    ("hexadecimal integer", "a = 0xff\n"),
    ("octal integer", "a = 0o755\n"),
    ("offset date-time", "a = 1979-05-27T07:32:00Z\n"),
    ("local date", "a = 1979-05-27\n"),
    ("inline table", "a = { b = 1 }\n"),
    ("dotted key", "a.b = 1\n"),
    ("quoted key", '"a" = 1\n'),
    ("dotted table header", "[a.b]\nc = 1\n"),
    ("array of tables", "[[a]]\nb = 1\n"),
    ("nested array", "a = [[1, 2], [3]]\n"),
    ("array of mixed types", 'a = [1, "x"]\n'),
]


class Subset(unittest.TestCase):
    """What a manifest may contain, read the same way everywhere."""

    def test_each_case_reads_as_specified(self):
        for name, text, expected in SUBSET:
            self.assertEqual(satoru._parse_minimal_toml(text), expected, name)

    @unittest.skipUnless(has_tomllib(), "tomllib needs Python 3.11+")
    def test_each_case_agrees_with_tomllib(self):
        import tomllib
        for name, text, _ in SUBSET:
            # tomllib.loads is given text, so a byte-order mark reaches it as a
            # character and it refuses. load_toml decodes the bytes itself and
            # drops it, which is the behaviour a manifest saved by an editor
            # needs; there is nothing to compare for that one case.
            self.assertEqual(satoru._parse_minimal_toml(text),
                             tomllib.loads(text.lstrip("﻿")), name)

    def test_one_reader_is_used_on_every_interpreter(self):
        """The divergence class is closed by construction, not by patching.

        load_toml choosing tomllib where it exists is what made a manifest mean
        one thing for its author and another for its reader.
        """
        import inspect
        source = inspect.getsource(satoru.load_toml)
        self.assertNotIn("tomllib", source)


class NotToml(unittest.TestCase):
    """Input that is not TOML at all is refused, and says where."""

    def test_each_case_is_refused_with_a_useful_message(self):
        for name, text, mentions in NOT_TOML:
            try:
                got = satoru._parse_minimal_toml(text)
            except ValueError as exc:
                self.assertIn(mentions, str(exc).lower(), "%s: %s" % (name, exc))
            else:
                self.fail("%s was accepted as %r" % (name, got))

    @unittest.skipUnless(has_tomllib(), "tomllib needs Python 3.11+")
    def test_tomllib_refuses_these_too(self):
        import tomllib
        for name, text, _ in NOT_TOML:
            self.assertRaises(tomllib.TOMLDecodeError, tomllib.loads, text)


class OutsideTheSubset(unittest.TestCase):
    """Legal TOML we do not accept - refused out loud, never guessed at."""

    def test_each_case_is_refused_and_says_it_is_unsupported(self):
        for name, text in OUTSIDE:
            try:
                got = satoru._parse_minimal_toml(text)
            except ValueError as exc:
                self.assertIn("not supported", str(exc).lower(),
                              "%s: %s" % (name, exc))
            else:
                self.fail("%s was accepted as %r" % (name, got))

    @unittest.skipUnless(has_tomllib(), "tomllib needs Python 3.11+")
    def test_these_really_are_legal_toml(self):
        """The gap is deliberate, so it has to be visible in the tests too."""
        import tomllib
        for name, text in OUTSIDE:
            tomllib.loads(text)


class NoCrash(unittest.TestCase):
    """A bad manifest is one game's problem, not the launcher's."""

    def test_a_broken_manifest_does_not_take_the_catalogue_down(self):
        import os
        import shutil
        import tempfile
        root = tempfile.mkdtemp()
        try:
            for name, body in (("good", 'contract = 1\n[game]\nid = "good"\n'
                                        'name = "Good"\n'),
                               ("broken", "[game\nid = ")):
                os.makedirs(os.path.join(root, name))
                with open(os.path.join(root, name, "game.toml"), "w") as fh:
                    fh.write(body)
            games = satoru.load_games(root)
            by_id = dict((g.id, g) for g in games)
            self.assertIn("good", by_id)
            self.assertIn("broken", by_id, "a broken manifest must still be listed")
            self.assertTrue(by_id["broken"].errors,
                            "and it must say what is wrong with it")
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
