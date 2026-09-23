#!/usr/bin/env python3
"""satoru — one entry point for the game packs in games/*.

Python 3 standard library only (curses, subprocess). Reads games/*/game.toml,
shows a list, and runs the pack's own scripts: setup, launch, launch --plain,
show profile, open logs. Actions a game does not support are shown as SOON and
do nothing; actions whose script is not on disk say "missing".

    python3 launcher/satoru.py            # TUI
    python3 launcher/satoru.py --list     # plain listing (no curses)
    python3 launcher/satoru.py --check    # validate every game.toml, exit 1 on error
    python3 launcher/satoru.py --version  # what this build is, and which packs it knows
"""
import contextlib
import argparse
import datetime
import hashlib
import os
import platform
import plistlib
import re
import shlex
import shutil
import struct
import tarfile
import tempfile
import urllib.request
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMES_DIR = os.path.join(ROOT, "games")


def version():
    """Release tarballs carry a VERSION file; a git checkout usually does not."""
    try:
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "unreleased"
    except OSError:
        return "git checkout"


# What an empty games/ means, and both ways out of it. A plain `git clone` leaves the
# submodules as empty directories, and the launcher used to answer that by listing
# nothing at all -- which reads as "this project is empty", not as "you are missing a step".
def unchecked_packs(games_dir=GAMES_DIR):
    """games/<id>/ directories that are empty: submodules nobody checked out."""
    out = []
    if not os.path.isdir(games_dir):
        return out
    for entry in sorted(os.listdir(games_dir)):
        d = os.path.join(games_dir, entry)
        if os.path.isdir(d) and not os.listdir(d):
            out.append(entry)
    return out


NO_GAMES_HINT = (
    "No games found under %s.\n"
    "\n"
    "If you cloned the repository, the packs are submodules and are not there yet:\n"
    "    git submodule update --init --recursive\n"
    "(that pulls ~400 MB, most of it the Wine source tree in components/wine-aoe4)\n"
    "\n"
    "To only run games, the release tarball carries the launcher and the packs'\n"
    "metadata and weighs ~130 KB:\n"
    "    https://github.com/NerRobDog/satoru/releases\n"
)

STATUSES = ("rc", "playable", "wip")
STATUS_LABEL = {"rc": "release candidate", "playable": "playable", "wip": "work in progress"}

# (key in game.toml, label, kind)  kind: cmd | file | dir
ACTIONS = (
    ("setup", "Setup", "cmd"),
    ("launch", "Launch", "cmd"),
    ("launch_plain", "Launch --plain", "cmd"),
    ("profile", "Show profile (README-local.txt)", "file"),
    ("logs", "Open logs", "dir"),
)


# ----------------------------------------------------------------------------
# The manifest language.
#
# One reader, on every interpreter, on purpose. tomllib arrived in 3.11 and the
# Python that ships with macOS is 3.9, so choosing a reader per interpreter meant
# a manifest could mean one thing to the person who wrote it and another to the
# person who runs it - an accent spelled é in a path, an escape inside a
# multi-line note - with nothing raised on either side. An audit found 58 such
# differences. Reading the same way everywhere closes that class by construction;
# tomllib stays, as the oracle the tests check this reader against.
#
# Supported, and this list is the whole of it: [section] headers one level deep;
# bare keys of letters, digits, underscore and dash; basic strings with the
# v1.0.0 escapes; multi-line basic strings with escapes and line continuation;
# literal strings, single- and multi-line; true and false; decimal integers with
# optional underscores; arrays of one of those types, over as many lines as you
# like; # comments.
#
# Refused by name: floats, dates and times, non-decimal integers, inline tables,
# dotted or quoted keys, arrays of tables, nested arrays, arrays of mixed types.
# These are legal TOML and a subset that guessed at them would be a trap; a
# subset that says "not supported here" is a contract.

_BARE_KEY = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_HEX = frozenset("0123456789abcdefABCDEF")
_ESCAPES = {'"': '"', "\\": "\\", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
            "t": "\t"}


class _Toml(object):
    """A cursor over the whole document.

    Line by line is what the previous reader did, and it is why a value could
    not span lines, why a comment after a closing delimiter swallowed the rest
    of the file, and why junk after a value went unnoticed. A cursor makes each
    of those the same question: what comes next.
    """

    def __init__(self, text):
        if text.startswith("﻿"):
            # A browser, a copy-paste or a Windows editor adds this. It is not
            # part of the first key's name.
            text = text[1:]
        self.s = text.replace("\r\n", "\n")
        self.i = 0
        self.n = len(self.s)

    # -- where we are ---------------------------------------------------------

    def line(self):
        return self.s.count("\n", 0, self.i) + 1

    def fail(self, message):
        raise ValueError("line %d: %s" % (self.line(), message))

    def fail_at(self, line, message):
        """For a value that spans lines, the opening is the useful place to point."""
        raise ValueError("line %d: %s" % (line, message))

    def peek(self, ahead=0):
        j = self.i + ahead
        return self.s[j] if j < self.n else ""

    def starts(self, text):
        return self.s.startswith(text, self.i)

    # -- whitespace -----------------------------------------------------------

    def skip_inline(self):
        while self.i < self.n and self.s[self.i] in " \t":
            self.i += 1

    def skip_comment(self):
        if self.peek() == "#":
            while self.i < self.n and self.s[self.i] != "\n":
                self.i += 1

    def skip_blanks(self):
        """Between statements: spaces, comments and line endings."""
        while self.i < self.n:
            ch = self.s[self.i]
            if ch in " \t\n":
                self.i += 1
            elif ch == "#":
                self.skip_comment()
            elif ch == "\r":
                self.fail("a bare carriage return is not a line ending")
            else:
                return

    def end_of_statement(self):
        """A statement owns the rest of its line, and nothing else may be on it."""
        self.skip_inline()
        self.skip_comment()
        if self.i >= self.n:
            return
        if self.s[self.i] == "\n":
            self.i += 1
            return
        rest = self.s[self.i:].split("\n", 1)[0].strip()
        self.fail("junk after the value: %r" % rest)

    # -- the document ---------------------------------------------------------

    def parse(self):
        data = {}
        table, table_name = data, None
        seen = {None: set()}
        while True:
            self.skip_blanks()
            if self.i >= self.n:
                return data
            if self.s[self.i] == "[":
                table_name = self.table_header(data)
                table = data[table_name]
                seen[table_name] = set()
                continue
            key = self.bare_key()
            self.skip_inline()
            if self.peek() != "=":
                self.fail("expected = after %r" % key)
            self.i += 1
            self.skip_inline()
            value = self.value()
            self.end_of_statement()
            if key in seen[table_name]:
                self.fail("%r is given twice" % key)
            seen[table_name].add(key)
            table[key] = value

    def table_header(self, data):
        line = self.line()
        self.i += 1
        if self.peek() == "[":
            self.fail("an array of tables is not supported here")
        self.skip_inline()
        name = self.bare_key("table name")
        self.skip_inline()
        if self.peek() != "]":
            self.fail_at(line, "expected ] to close [%s" % name)
        self.i += 1
        self.end_of_statement()
        if name in data:
            # Covers a repeated [section] and a bare key of the same name; TOML
            # forbids declaring a table twice either way.
            self.fail_at(line, "%r is declared twice" % name)
        data[name] = {}
        return name

    def bare_key(self, what="key"):
        if self.peek() in ('"', "'"):
            self.fail("a quoted %s is not supported here" % what)
        start = self.i
        while self.i < self.n and self.s[self.i] in _BARE_KEY:
            self.i += 1
        if self.i == start:
            self.fail("expected a %s" % what)
        key = self.s[start:self.i]
        if self.peek() == ".":
            self.fail("a dotted %s is not supported here" % what)
        return key

    # -- values ---------------------------------------------------------------

    def value(self):
        ch = self.peek()
        if ch == "" or ch == "\n":
            self.fail("expected a value")
        if self.starts('"""'):
            return self.multiline_basic()
        if ch == '"':
            return self.basic_string()
        if self.starts("'''"):
            return self.multiline_literal()
        if ch == "'":
            return self.literal_string()
        if ch == "[":
            return self.array()
        if ch == "{":
            self.fail("an inline table is not supported here")
        return self.atom()

    def atom(self):
        start = self.i
        while self.i < self.n and self.s[self.i] not in " \t\n,]#":
            self.i += 1
        raw = self.s[start:self.i]
        if not raw:
            self.fail("expected a value")
        if raw == "true":
            return True
        if raw == "false":
            return False
        return self.integer(raw)

    def integer(self, raw):
        body = raw[1:] if raw[:1] in "+-" else raw
        low = body.lower()
        if low[:2] in ("0x", "0o", "0b"):
            self.fail("hexadecimal, octal and binary integers are not supported "
                      "here; write it in decimal")
        if "." in body or "e" in low or low in ("inf", "nan"):
            self.fail("floats are not supported here")
        if "-" in body[1:] or ":" in body:
            self.fail("dates and times are not supported here")
        if body.startswith("_") or body.endswith("_") or "__" in body:
            self.fail("unsupported value %r" % raw)
        digits = body.replace("_", "")
        if not digits or not digits.isdigit() or not digits.isascii():
            self.fail("unsupported value %r" % raw)
        if len(digits) > 1 and digits[0] == "0":
            self.fail("unsupported value %r: an integer may not have a leading zero"
                      % raw)
        return int(raw.replace("_", ""))

    def forbid_control(self, ch, allow_newline=False):
        o = ord(ch)
        if ch == "\t" or (allow_newline and ch == "\n"):
            return
        if o < 0x20 or o == 0x7F:
            self.fail("a control character (U+%04X) has to be escaped in a string"
                      % o)

    def escape(self, multiline):
        ch = self.peek()
        if ch == "":
            self.fail("a string may not end in a lone backslash")
        if ch in _ESCAPES:
            self.i += 1
            return _ESCAPES[ch]
        if ch in ("u", "U"):
            width = 4 if ch == "u" else 8
            self.i += 1
            digits = self.s[self.i:self.i + width]
            if len(digits) != width or any(c not in _HEX for c in digits):
                self.fail("\\%s needs %d hex digits" % (ch, width))
            self.i += width
            point = int(digits, 16)
            if point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
                self.fail("\\%s%s is not a character" % (ch, digits))
            return chr(point)
        if multiline:
            # A backslash at the end of a line joins it to the next, swallowing
            # the line ending and the indent that follows.
            j = self.i
            while j < self.n and self.s[j] in " \t":
                j += 1
            if j < self.n and self.s[j] == "\n":
                j += 1
                while j < self.n and self.s[j] in " \t\n":
                    j += 1
                self.i = j
                return ""
        self.fail("unknown escape \\%s" % ch)

    def basic_string(self):
        line = self.line()
        self.i += 1
        out = []
        while True:
            if self.i >= self.n or self.s[self.i] == "\n":
                self.fail_at(line, "unterminated string")
            ch = self.s[self.i]
            if ch == '"':
                self.i += 1
                return "".join(out)
            if ch == "\\":
                self.i += 1
                out.append(self.escape(False))
                continue
            self.forbid_control(ch)
            out.append(ch)
            self.i += 1

    def multiline_basic(self):
        line = self.line()
        self.i += 3
        if self.peek() == "\n":
            # Exactly one, and only where it follows the opening delimiter.
            self.i += 1
        out = []
        while True:
            if self.i >= self.n:
                self.fail_at(line, "unterminated multi-line string")
            if self.starts('"""'):
                self.i += 3
                if self.peek() == '"':
                    self.fail_at(line, "a multi-line string ending in a quote is "
                                       "not supported here; escape it as \\\"")
                return "".join(out)
            ch = self.s[self.i]
            if ch == "\\":
                self.i += 1
                out.append(self.escape(True))
                continue
            self.forbid_control(ch, allow_newline=True)
            out.append(ch)
            self.i += 1

    def literal_string(self):
        line = self.line()
        self.i += 1
        out = []
        while True:
            if self.i >= self.n or self.s[self.i] == "\n":
                self.fail_at(line, "unterminated string")
            ch = self.s[self.i]
            if ch == "'":
                self.i += 1
                return "".join(out)
            self.forbid_control(ch)
            out.append(ch)
            self.i += 1

    def multiline_literal(self):
        line = self.line()
        self.i += 3
        if self.peek() == "\n":
            self.i += 1
        out = []
        while True:
            if self.i >= self.n:
                self.fail_at(line, "unterminated multi-line string")
            if self.starts("'''"):
                self.i += 3
                if self.peek() == "'":
                    self.fail_at(line, "a multi-line string ending in a quote is "
                                       "not supported here")
                return "".join(out)
            ch = self.s[self.i]
            self.forbid_control(ch, allow_newline=True)
            out.append(ch)
            self.i += 1

    def skip_array_space(self):
        while self.i < self.n:
            ch = self.s[self.i]
            if ch in " \t\n":
                self.i += 1
            elif ch == "#":
                self.skip_comment()
            else:
                return

    def array(self):
        line = self.line()
        self.i += 1
        items = []
        after_comma = True
        while True:
            self.skip_array_space()
            if self.i >= self.n:
                self.fail_at(line, "unterminated array")
            if self.peek() == "]":
                self.i += 1
                break
            if not after_comma:
                self.fail("expected , between array items")
            if self.peek() == "[":
                self.fail("a nested array is not supported here")
            items.append(self.value())
            self.skip_array_space()
            after_comma = self.peek() == ","
            if after_comma:
                self.i += 1
        kinds = set(("bool" if v is True or v is False else type(v).__name__)
                    for v in items)
        if len(kinds) > 1:
            self.fail_at(line, "an array of mixed types is not supported here")
        return items


def _parse_minimal_toml(text):
    return _Toml(text).parse()


def load_toml(path):
    """The one reader, whatever the interpreter. See the note above."""
    with open(path, "rb") as f:
        raw = f.read()
    return _parse_minimal_toml(raw.decode("utf-8"))


# ----------------------------------------------------------------------------
# the pack manifest (contract v1)

CONTRACT = 1

_V1_SECTIONS = ("game", "source", "requires", "install", "commands", "paths")
_GAME_KEYS = ("id", "name", "status", "summary", "notes")
_SOURCE_KEYS = ("kind", "url", "sha256", "size", "version", "check")
_REQUIRES_KEYS = ("arch", "macos", "rosetta", "disk_gb", "tools")
_INSTALL_KEYS = ("home_authoritative", "foreign_note", "manual_url", "home")
_COMMAND_KEYS = ("preflight", "install", "launch", "launch_plain", "uninstall", "update")
_PATH_KEYS = ("profile", "logs", "icon_exe")

# What the packs ship today: one flat [game] table with the commands inside it.
# All four are this shape, so it stays supported rather than being a migration.
_LEGACY_KEYS = ("id", "name", "status", "home", "notes", "summary",
                "setup", "launch", "launch_plain", "profile", "logs")

_ID_OK = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _blank_manifest():
    return {
        "contract": 0,
        "game": {"id": "", "name": "", "status": "wip", "summary": "", "notes": ""},
        "source": None,
        "requires": {"arch": None, "macos": None, "rosetta": False,
                     "disk_gb": 0, "tools": []},
        "install": {"home_authoritative": True, "foreign_note": "",
                    "manual_url": "", "home": ""},
        "commands": dict((k, "") for k in _COMMAND_KEYS),
        "paths": dict((k, "") for k in _PATH_KEYS),
        "modes": [],
        "settings": [],
    }


def _take(section, keys, errors, where):
    """Copy the keys we know about; anything else is a typo, and says so."""
    out = {}
    for k, v in section.items():
        if k in keys:
            out[k] = v
        else:
            errors.append("%s: unknown key %r" % (where, k))
    return out


def parse_manifest(data):
    """(manifest, errors). Errors are for humans; the manifest is always usable."""
    m = _blank_manifest()
    errors = []

    contract = data.get("contract", 0)
    if not isinstance(contract, int):
        errors.append("contract must be a number, got %r" % (contract,))
        contract = 0
    elif contract > CONTRACT:
        errors.append(
            "the pack needs contract %d, this satoru speaks %d - update satoru"
            % (contract, CONTRACT))
    m["contract"] = contract

    if contract == 0 and any(k in data for k in ("source", "requires", "install",
                                                 "commands", "paths")):
        # One forgotten line otherwise turns a correct v1 manifest into a handful
        # of "unknown section" errors that accuse the very sections the author
        # copied out of the contract.
        errors.append(
            "this looks like a contract 1 manifest but has no `contract = 1` "
            "line, so it is being read as the older shape")

    if contract == 0:
        game = data.get("game", {})
        known = _take(game, _LEGACY_KEYS, errors, "[game]")
        for k in ("id", "name", "status", "notes", "summary"):
            if k in known:
                m["game"][k] = known[k]
        m["install"]["home"] = known.get("home", "")
        # setup was the old name for install; the rest kept theirs
        m["commands"]["install"] = known.get("setup", "")
        for k in ("launch", "launch_plain"):
            m["commands"][k] = known.get(k, "")
        for k in _PATH_KEYS:
            m["paths"][k] = known.get(k, "")
        for name in data:
            if name not in ("game", "contract"):
                errors.append("unknown section [%s]" % name)
    else:
        for name in data:
            if (name not in _V1_SECTIONS and name not in ("contract", "modes")
                    and not name.startswith(SETTING_SECTION)):
                errors.append("unknown section [%s]" % name)
        m["game"].update(_take(data.get("game", {}), _GAME_KEYS, errors, "[game]"))
        m["requires"].update(
            _take(data.get("requires", {}), _REQUIRES_KEYS, errors, "[requires]"))
        m["install"].update(
            _take(data.get("install", {}), _INSTALL_KEYS, errors, "[install]"))
        m["commands"].update(
            _take(data.get("commands", {}), _COMMAND_KEYS, errors, "[commands]"))
        m["paths"].update(_take(data.get("paths", {}), _PATH_KEYS, errors, "[paths]"))
        if "source" in data:
            src = dict((k, None) for k in _SOURCE_KEYS)
            src.update(_take(data["source"], _SOURCE_KEYS, errors, "[source]"))
            m["source"] = src
        parse_modes(data, m, errors)

    # --- what has to be true whichever shape it came in ---
    gid = m["game"]["id"]
    if not gid:
        errors.append("[game] id is required")
    elif not _ID_OK.match(str(gid)):
        errors.append("[game] id %r must be lower-case letters, digits and dashes" % gid)
    if not m["game"]["name"]:
        errors.append("[game] name is required")
    if m["game"]["status"] not in STATUSES:
        errors.append("[game] status %r must be one of %s"
                      % (m["game"]["status"], ", ".join(STATUSES)))

    req = m["requires"]
    if not isinstance(req["tools"], list):
        errors.append("[requires] tools must be a list")
        req["tools"] = []
    if not isinstance(req["rosetta"], bool):
        errors.append("[requires] rosetta must be true or false")
        req["rosetta"] = bool(req["rosetta"])
    if not isinstance(req["disk_gb"], int):
        errors.append("[requires] disk_gb must be a number")
        req["disk_gb"] = 0

    src = m["source"]
    if src is not None:
        # A source without a hash is a download nobody can check. Refuse it here
        # rather than discovering it after 133 MB have arrived.
        if not src.get("kind"):
            errors.append("[source] kind is required")
        if src.get("kind") == "release":
            for k in ("url", "sha256"):
                if not src.get(k):
                    errors.append("[source] %s is required for kind = \"release\"" % k)

    return m, errors


# ----------------------------------------------------------------------------
# launch modes: an optional part of contract 1 (docs/launch-modes-design.md)
#
# A pack may offer several ways to start the same game - offline, join a server,
# host one - and the values each way needs. satoru asks, remembers, validates and
# hands the answer over in the environment; the pack's own commands stay
# non-interactive. A pack without [modes] never sees any of it.

SETTING_SECTION = "setting_"
SETTING_KINDS = ("text", "ipv4")
_SETTING_KEYS = ("label", "modes", "kind", "pattern", "error")
_MODE_ID_OK = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SETTING_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9_]*$")
LAUNCH_STATE_FILE = "satoru-launch.conf"
CHANGE_SETTINGS_LABEL = "Change settings\u2026"


def _one_line_text(value):
    return isinstance(value, str) and value.strip() != "" and not any(
        ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def parse_modes(data, m, errors):
    """[modes] and [setting_<name>] into m["modes"] and m["settings"].

    The reader keeps keys in the order they were written, so one table gives
    both the list and its labels without the arrays of tables the subset refuses.
    """
    setting_sections = [n for n in data if n.startswith(SETTING_SECTION)]
    modes = data.get("modes")
    if modes is None:
        for name in setting_sections:
            errors.append("[%s] needs a [modes] section to belong to" % name)
        return
    if not isinstance(modes, dict) or not modes:
        errors.append("[modes] must name at least one mode: id = \"label\"")
        return
    labels = set()
    for mode_id, label in modes.items():
        if not _MODE_ID_OK.match(mode_id):
            errors.append("[modes] %r: a mode id is lower-case letters, digits, _ and -"
                          % mode_id)
            continue
        if not _one_line_text(label):
            errors.append("[modes] %s: the label must be a non-empty one-line string"
                          % mode_id)
            continue
        if label in labels or label == CHANGE_SETTINGS_LABEL:
            errors.append("[modes] %s: the label %r is used twice" % (mode_id, label))
            continue
        labels.add(label)
        m["modes"].append({"id": mode_id, "label": label})
    ids = [mode["id"] for mode in m["modes"]]

    for section in setting_sections:
        where = "[%s]" % section
        name = section[len(SETTING_SECTION):]
        if not _SETTING_NAME_OK.match(name):
            errors.append("%s: a setting name is lower-case letters, digits and _" % where)
            continue
        if not isinstance(data[section], dict):
            errors.append("%s must be a section" % where)
            continue
        setting = {"name": name, "label": "", "modes": [], "kind": "text",
                   "pattern": "", "error": ""}
        setting.update(_take(data[section], _SETTING_KEYS, errors, where))
        ok = True
        if not _one_line_text(setting["label"]):
            errors.append("%s: label is required, one line" % where)
            ok = False
        wanted = setting["modes"]
        if (not isinstance(wanted, list) or not wanted
                or not all(isinstance(x, str) for x in wanted)):
            errors.append("%s: modes must list the modes that need it" % where)
            ok = False
        else:
            for mode_id in wanted:
                if mode_id not in ids:
                    errors.append("%s: modes names %r, which [modes] does not declare"
                                  % (where, mode_id))
                    ok = False
        if setting["kind"] not in SETTING_KINDS:
            errors.append("%s: kind %r must be one of %s"
                          % (where, setting["kind"], ", ".join(SETTING_KINDS)))
            ok = False
        pattern = setting["pattern"]
        if pattern:
            # Checked twice, by Python here and by grep -E in the shim, so only
            # what means the same to both is allowed: no escapes, no Python-only
            # groups. A literal dot is [.], a digit is [0-9].
            if not isinstance(pattern, str) or "\\" in pattern or "(?" in pattern \
                    or "[:" in pattern or "[=" in pattern or "[." in pattern \
                    or not _one_line_text(pattern):
                errors.append("%s: pattern must be a one-line POSIX ERE without "
                              "backslashes or [:class:] names" % where)
                ok = False
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append("%s: pattern does not compile: %s" % (where, exc))
                    ok = False
            if setting["kind"] != "text":
                errors.append("%s: pattern only applies to kind = \"text\"" % where)
                ok = False
        if setting["error"] and not _one_line_text(setting["error"]):
            errors.append("%s: error must be one line" % where)
            ok = False
        if ok:
            m["settings"].append(setting)


def mode_settings(manifest, mode_id):
    return [s for s in manifest.get("settings") or [] if mode_id in s["modes"]]


def valid_ipv4(value):
    parts = value.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if (not part or len(part) > 3 or not part.isdigit() or not part.isascii()
                or int(part) > 255):
            return False
    return True


def setting_problem(setting, value):
    """None when the value is usable, otherwise what to tell the person.

    The shim asks the same questions in shell; the pack asks them a third time,
    because it can be run without satoru at all.
    """
    label = setting["label"]
    custom = setting.get("error") or ""
    if not value:
        return custom or "%s is required" % label
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return custom or "%s must be one line of text" % label
    if setting["kind"] == "ipv4" and not valid_ipv4(value):
        return custom or "%s must be an IPv4 address, like 192.168.0.10" % label
    if setting.get("pattern") and not re.fullmatch(setting["pattern"], value):
        return custom or "%s is not in the expected form" % label
    return None


def read_launch_state(home):
    """{"mode": id or "", "settings": {name: value}}. Never raises."""
    state = {"mode": "", "settings": {}}
    try:
        with open(os.path.join(home, LAUNCH_STATE_FILE), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return state
    for line in lines:
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(" \t"), value.strip(" \t")
        if key == "mode":
            state["mode"] = value
        elif key.startswith(SETTING_SECTION):
            state["settings"][key[len(SETTING_SECTION):]] = value
    return state


def write_launch_state(home, mode_id, values):
    """Atomically; key = value lines, the same shape the shim writes and reads."""
    lines = ["mode = %s" % mode_id]
    for name in sorted(values):
        value = values[name]
        if value and "\n" not in value and "\r" not in value:
            lines.append("%s%s = %s" % (SETTING_SECTION, name, value))
    path = os.path.join(home, LAUNCH_STATE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def launch_mode_default(manifest, state):
    ids = [mode["id"] for mode in manifest.get("modes") or []]
    if not ids:
        return ""
    return state["mode"] if state["mode"] in ids else ids[0]


def setting_env_name(name):
    return "SATORU_SETTING_" + name.upper()


def resolve_launch_mode(manifest, home, mode_id, ask, say, force=False):
    """Fill in what `mode_id` needs, save the choice, return the environment.

    `ask(setting, current)` returns the answer or None for "cancel"; `say(text)`
    tells the person why an answer was refused. Values already saved and still
    valid are not asked again unless `force`. Returns None when cancelled.
    """
    state = read_launch_state(home)
    values = dict(state["settings"])
    wanted = (manifest.get("settings") or []) if force else mode_settings(manifest, mode_id)
    for setting in wanted:
        value = values.get(setting["name"], "")
        problem = setting_problem(setting, value)
        asked = False
        while force and not asked or problem:
            if problem and asked:
                say(problem)
            answer = ask(setting, value)
            asked = True
            if answer is None:
                return None
            value = answer.strip(" \t")
            problem = setting_problem(setting, value)
        values[setting["name"]] = value
    write_launch_state(home, mode_id, values)
    env = {"SATORU_MODE": mode_id}
    for setting in mode_settings(manifest, mode_id):
        env[setting_env_name(setting["name"])] = values[setting["name"]]
    return env


# ----------------------------------------------------------------------------
# [requires]: what the machine has to be before a pack is worth downloading


def _nearest_existing(path):
    """The closest ancestor of `path` that is actually there, `/` at worst."""
    path = os.path.abspath(path)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


class SystemProbe(object):
    """Everything the checks want to know about this Mac, in one injectable place.

    Tests hand in a fake, which is how an Intel Mac with no Rosetta and a full
    disk get tested from an M1 with 200 GB free.
    """

    def arch(self):
        """The machine's architecture, not this process's.

        platform.machine() answers about the running process, and a process can
        be translated: python3.11 from an Intel Homebrew prefix reports x86_64
        on an M1 Pro, and every child it spawns inherits that. Asking it would
        tell a perfectly good Apple Silicon Mac that it needs an Apple Silicon
        Mac. hw.optional.arm64 is a property of the hardware and does not lie.
        """
        try:
            with open(os.devnull, "wb") as null:
                out = subprocess.check_output(
                    ["sysctl", "-n", "hw.optional.arm64"], stderr=null)
            if out.strip() == b"1":
                return "arm64"
            return "x86_64"
        except (OSError, subprocess.CalledProcessError):
            return platform.machine()

    def macos_version(self):
        return platform.mac_ver()[0]

    def has_rosetta(self):
        # The probe setup.sh already uses: if x86_64 code cannot run, Rosetta is absent.
        try:
            with open(os.devnull, "wb") as null:
                return subprocess.call(["/usr/bin/arch", "-x86_64", "/usr/bin/true"],
                                       stdout=null, stderr=null) == 0
        except OSError:
            return False

    def free_gb(self, path=None):
        """Free space on the volume that would hold `path`.

        The first install asks about directories nothing has created yet, and
        statvfs raises on a path that is not there. The question is about a
        volume, and the volume exists whether or not the directory does, so walk
        up until something answers.
        """
        st = os.statvfs(_nearest_existing(path or os.path.expanduser("~")))
        return st.f_bavail * st.f_frsize / float(1024 ** 3)

    def which(self, tool):
        return shutil.which(tool)


# Where a missing tool comes from. Deliberately a hint and not a command we run:
# installing packages on someone's behalf is a bigger promise than this makes.
TOOL_HINTS = {
    "ffmpeg": "brew install ffmpeg",
    "gh": "brew install gh",
    "git-lfs": "brew install git-lfs",
    "dotnet": "install the .NET 8 SDK (arm64) from dotnet.microsoft.com",
    "python3": "xcode-select --install",
}


def _version_tuple(text):
    return tuple(int(x) for x in str(text).strip().split("."))


def _version_at_least(have, want):
    """`want` is ">=26", or a bare "26" which means the same thing."""
    want = str(want).strip()
    if want.startswith(">="):
        want = want[2:].strip()
    elif want.startswith(">"):
        want = want[1:].strip()
    a, b = _version_tuple(have), _version_tuple(want)
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)) >= b + (0,) * (size - len(b))


def _result(rid, label, ok, detail="", fix=None):
    return {"id": rid, "label": label, "ok": ok, "detail": detail, "fix": fix}


def check_requirements(requires, probe=None, root=None):
    """One result per declared requirement, in the order the TUI shows them.

    `fix` is the whole point: {"kind": "command"} we can run, {"kind": "manual"}
    only they can, None nobody can. The screen must never offer an action that
    does not exist.
    """
    probe = probe or SystemProbe()
    out = []

    want_arch = requires.get("arch")
    if want_arch:
        have = probe.arch()
        ok = have == want_arch
        out.append(_result(
            "arch", "Apple Silicon" if want_arch == "arm64" else want_arch, ok,
            "" if ok else "this Mac is %s, the pack needs %s" % (have, want_arch),
            None))  # nothing turns an Intel Mac into an M-series one

    want_macos = requires.get("macos")
    if want_macos:
        have = probe.macos_version()
        try:
            ok = _version_at_least(have, want_macos)
            detail = "" if ok else "macOS %s, the pack needs %s" % (have, want_macos)
            fix = None if ok else {"kind": "manual",
                                   "hint": "System Settings -> General -> Software Update"}
        except ValueError:
            ok, fix = False, None
            detail = "cannot read the version requirement %r" % (want_macos,)
        out.append(_result("macos", "macOS %s" % want_macos, ok, detail, fix))

    if requires.get("rosetta"):
        ok = probe.has_rosetta()
        out.append(_result(
            "rosetta", "Rosetta", ok, "" if ok else "not installed",
            None if ok else {
                "kind": "command",
                # No --agree-to-license: the licence is theirs to read, not ours to accept.
                "run": "softwareupdate --install-rosetta",
                "label": "Install Rosetta"}))

    want_gb = requires.get("disk_gb") or 0
    if want_gb:
        free = probe.free_gb(root)
        ok = free >= want_gb
        out.append(_result(
            "disk", "%d GB free" % want_gb, ok,
            "" if ok else "%.1f GB free, the pack needs %d GB" % (free, want_gb),
            None if ok else {"kind": "manual",
                             "hint": "free up space, or point root= at another disk"}))

    for tool in requires.get("tools") or []:
        found = probe.which(tool)
        out.append(_result(
            "tool:%s" % tool, tool, bool(found), found or "not on PATH",
            None if found else {
                "kind": "manual",
                "hint": TOOL_HINTS.get(tool, "install %s and put it on PATH" % tool)}))

    return out


def all_met(results):
    return all(r["ok"] for r in results)


# ----------------------------------------------------------------------------
# installed.toml: what is installed, where, and of what version

INSTALLED_KEYS = ("name", "version", "source_sha256", "installed_at", "home", "bundle")


def _toml_string(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_installed(path, entries):
    """Rewrite the file from scratch, atomically.

    Atomically because the alternative is a half-written state file, and a
    launcher that cannot read its own state is worse than one with none.
    """
    lines = ["# satoru: what is installed. Written by satoru, not by packs.", ""]
    for game_id in sorted(entries):
        lines.append("[%s]" % game_id)
        entry = entries[game_id]
        for key in INSTALLED_KEYS:
            if key in entry and entry[key] not in (None, ""):
                lines.append("%s = %s" % (key, _toml_string(entry[key])))
        lines.append("")
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.replace(tmp, path)


def read_installed(path):
    """Never raises. A missing or broken file means "nothing is installed"."""
    try:
        data = load_toml(path)
    except (IOError, OSError):
        return {}
    except ValueError:
        # Including tomllib's decode error, which is a ValueError. A state file
        # someone hand-edited into nonsense should not take the launcher down.
        return {}
    return dict((k, v) for k, v in data.items() if isinstance(v, dict))


def record_install(paths, manifest, source_sha256):
    name = manifest["game"]["name"]
    game_id = manifest["game"]["id"]
    source = manifest.get("source") or {}
    entries = read_installed(paths.installed_file)
    entries[game_id] = {
        "name": name,
        "version": source.get("version") or "",
        "source_sha256": source_sha256 or "",
        "installed_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "home": paths.home(name),
        "bundle": paths.bundle(name),
    }
    write_installed(paths.installed_file, entries)
    return entries[game_id]


def forget_install(paths, game_id):
    entries = read_installed(paths.installed_file)
    if entries.pop(game_id, None) is None:
        return False
    write_installed(paths.installed_file, entries)
    return True


def installed_entry(paths, game_id):
    """The entry, or None if the home it names is gone.

    installed.toml is a claim, not proof. Dragging a bundle to the Trash is a
    normal thing for a person to do, and afterwards the launcher has to say the
    game is not installed rather than offer to launch what is not there.
    """
    entry = read_installed(paths.installed_file).get(game_id)
    if not entry:
        return None
    home = entry.get("home")
    if not home or not os.path.isdir(home):
        return None
    return entry


def installed_games(paths):
    entries = read_installed(paths.installed_file)
    return dict((gid, e) for gid, e in entries.items()
                if e.get("home") and os.path.isdir(e["home"]))


# ----------------------------------------------------------------------------
# getting a pack onto the disk, and being sure it is the pack


class PackError(Exception):
    """The pack is not what it claims to be. Never a reason to keep going."""


def verify_sha256(path, expected):
    if not expected:
        return False
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest() == str(expected).strip().lower()


def fetch_pack(source, cache_dir, reporter=None):
    """Return a local path to the pack archive, downloading it if needed.

    A file already in the cache is used only if it still matches the hash: 133 MB
    is not a thing to fetch twice because the launcher restarted, and a truncated
    one is not a thing to trust because it has the right name.
    """
    url = (source or {}).get("url")
    want = (source or {}).get("sha256")
    if not url or not want:
        raise PackError("the manifest has no source url and sha256 to fetch")

    if not os.path.isdir(cache_dir):
        os.makedirs(cache_dir)
    target = os.path.join(cache_dir, os.path.basename(url.split("?", 1)[0]) or "pack.tar.gz")

    if os.path.isfile(target) and verify_sha256(target, want):
        return target

    part = target + ".part"
    try:
        with contextlib.closing(urllib.request.urlopen(url)) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as fh:
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if reporter:
                        reporter(done, total)
    except PackError:
        raise
    except Exception as exc:
        _remove(part)
        raise PackError("could not download %s: %s" % (url, exc))

    if not verify_sha256(part, want):
        # Leave nothing a later run could mistake for a good file.
        _remove(part)
        _remove(target)
        raise PackError(
            "sha256 of the downloaded archive does not match the manifest - "
            "the download was corrupted, or the release was replaced")
    os.replace(part, target)
    return target


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _inside(root, path):
    return path == root or path.startswith(root + os.sep)


def _member_stays_inside(member, dest, root):
    """Every path an archive gives us is data from the internet.

    Two things here are not obvious, and both were wrong when this was a pass
    over the member list before anything was written.

    A member's own name is resolved against the destination *as it stands*, so a
    symlink an earlier member planted is followed the way tar itself will follow
    it. Reading the whole list first cannot see that: `up -> .` looks harmless,
    and `up/../../evil` then resolves through it to the destination's parent.

    A symlink's target is resolved against the link's own directory, because
    that is where the system will resolve it. Joining it to the destination root
    instead calls every ../.. inside a Wine tree or a dylib layout an attack. A
    hardlink is the other way round: its target names another member, so it is
    relative to the root.
    """
    if os.path.isabs(member.name) or os.path.isabs(member.linkname or ""):
        return False
    if not _inside(root, os.path.realpath(os.path.join(dest, member.name))):
        return False
    if member.issym():
        base = os.path.dirname(os.path.join(dest, member.name))
        return _inside(root, os.path.realpath(os.path.join(base, member.linkname)))
    if member.islnk():
        return _inside(root, os.path.realpath(os.path.join(dest, member.linkname)))
    return True


def unpack(archive, dest):
    """Replace `dest` with the archive's contents, and return its root directory.

    Replace rather than merge: an older pack's leftovers inside a new one is a
    debugging session nobody should have to have.
    """
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    root = os.path.realpath(dest)
    try:
        with contextlib.closing(tarfile.open(archive, "r:*")) as tf:
            members = tf.getmembers()
            # Python's filter quietly makes an absolute name relative, the way
            # GNU tar does. Safe, but a pack that ships one is broken and should
            # hear about it rather than be silently rewritten.
            for member in members:
                if os.path.isabs(member.name) or os.path.isabs(member.linkname or ""):
                    raise PackError(
                        "the archive contains %r, an absolute path, which no pack "
                        "has any reason to ship" % (member.name,))
            if hasattr(tarfile, "data_filter"):
                # Python's own check, and it looks at the disk rather than at the
                # member list, which is what makes it see a symlink planted by an
                # earlier member. It also strips setuid bits and refuses device
                # nodes - neither of which an archive off the internet has any
                # business carrying. Present from 3.12, and backported to
                # 3.8.17, 3.9.17, 3.10.12 and 3.11.4.
                tf.extractall(dest, members=members, filter="data")
            else:
                # The 3.9.6 that ships with the command line tools has no filter,
                # so each member is checked against the destination at the moment
                # it is written, not against a list read beforehand.
                for member in members:
                    if member.isdev():
                        raise PackError(
                            "the archive contains a device node, %r, which no "
                            "pack has any reason to ship" % (member.name,))
                    if not _member_stays_inside(member, dest, root):
                        raise PackError(
                            "the archive contains %r, which points outside the "
                            "directory it is being unpacked into" % (member.name,))
                    tf.extract(member, dest)
    except PackError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except tarfile.TarError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise PackError("could not unpack %s: %s" % (archive, exc))

    # A tarball built on macOS carries an AppleDouble `._name` beside every entry
    # whose file had extended attributes, and our own releases are built on macOS.
    # Counting one of those as a second root left the pack's commands running a
    # directory too high, where `bash setup.sh` is "No such file or directory".
    entries = [e for e in os.listdir(dest)
               if not e.startswith("._") and e != ".DS_Store"]
    if len(entries) == 1 and os.path.isdir(os.path.join(dest, entries[0])):
        return os.path.join(dest, entries[0])
    return dest


# ----------------------------------------------------------------------------
# the launch shim: the one thing a bundle calls

# Interpreters a launch command may legitimately start with; anything else that
# has no slash in it is a script sitting in the home and needs a ./ to be found.
_INTERPRETERS = ("sh", "bash", "zsh", "python3", "env", "/usr/bin/env")

UPDATE_CHECK_SECONDS = 21600  # six hours


def _launch_command_in_home(command):
    parts = shlex.split(command)
    if not parts:
        return ""
    head = parts[0]
    if head not in _INTERPRETERS and "/" not in head:
        parts[0] = "./" + head
    return " ".join(shlex.quote(p) if " " in p else p for p in parts)


def _github_api_url(source):
    """https://github.com/OWNER/REPO/releases/download/... -> the releases API."""
    url = (source or {}).get("url") or ""
    marker = "https://github.com/"
    if not url.startswith(marker):
        return ""
    rest = url[len(marker):].split("/")
    if len(rest) < 2:
        return ""
    return "https://api.github.com/repos/%s/%s/releases/latest" % (rest[0], rest[1])


def shim_text(paths, manifest, check_updates=True):
    """The script, as text. Returns "" for a pack that declares no way to launch.

    check_updates is the config switch. It was documented, parsed and ignored,
    which is the worst shape a setting can take: the person who turns it off for
    privacy, for an offline machine or for GitHub's rate limit is told it worked.
    """
    game = manifest["game"]
    name, game_id = game["name"], game["id"]
    launch = (manifest["commands"] or {}).get("launch") or ""
    if not launch:
        return ""

    api = _github_api_url(manifest.get("source"))
    version = ((manifest.get("source") or {}).get("version")) or ""
    marker = os.path.join(paths.game_cache(game_id), "update-check")

    out = []
    add = out.append
    add("#!/bin/sh")
    add("# satoru launch shim for %s. Generated by satoru - edits are lost on update." % name)
    add("# The bundle calls this and nothing else, so updating the pack never rewrites")
    add("# the .app: the icon stays put in the Dock and Spotlight has nothing to reindex.")
    add("")
    add('HERE=$(cd "$(dirname "$0")" && pwd -P)')
    add("")
    add("# App Translocation. A quarantined bundle is mounted read-only at a random path,")
    add("# and a Wine prefix that cannot be written to fails in confusing ways rather than")
    add("# obvious ones. Apple provides no supported way to detect this, so the executable's")
    add("# own path is the only signal there is.")
    add('case "$HERE" in')
    add("  */AppTranslocation/*)")
    add('    msg="macOS started this game from a read-only copy, so nothing it saves would be kept."')
    add('    fix="Move the app into Applications in Finder - one at a time, not several at once - then open it again."')
    add('    printf \'%s\\n%s\\n\' "$msg" "$fix" >&2')
    add("    # Finder gives a launched app no terminal, so stderr goes nowhere and the")
    add("    # alert is the only way to be heard. From a terminal the text above is")
    add("    # already visible, and a dialog would just be in the way.")
    add('    # Not `[ -z "$TERM" ]`: /bin/sh sets TERM=dumb when it is unset, so under')
    add("    # Finder that test is never true. No terminal on stderr and no real TERM")
    add("    # is a double click.")
    add('    if [ ! -t 2 ] && [ "${TERM:-dumb}" = dumb ]; then')
    add('      osascript -e "display alert \\"$msg\\" message \\"$fix\\"" >/dev/null 2>&1 || true')
    add("    fi")
    add("    exit 10")
    add("    ;;")
    add("esac")
    add("")
    add("# Warmed shader pipelines live in the home, where the system is not allowed to")
    add("# reclaim them. Overwatch alone keeps 434 MB of them. Nothing else makes this")
    add("# directory: DXMT is handed an absolute path and opens a file inside it, so a")
    add("# missing one loses the pipelines quietly, which is the whole thing this avoids.")
    add("mkdir -p %s 2>/dev/null || true" % shlex.quote(paths.shader_cache(name)))
    add('export DXMT_SHADER_CACHE_PATH=%s' % shlex.quote(paths.shader_cache(name) + "/"))
    add('export SATORU_GAME_HOME="$HERE"')
    add("export SATORU_GAME_ID=%s" % shlex.quote(game_id))
    # The contract names one environment, not one for installing and a thinner
    # one for launching. An author who writes "$SATORU_LOGS/game.log" in a launch
    # command had it work at install time and vanish here, which shows up as an
    # empty log and no explanation.
    add("export SATORU_LIBRARY=%s" % shlex.quote(paths.library))
    add("export SATORU_CACHE=%s" % shlex.quote(paths.game_cache(game_id)))
    add("mkdir -p %s 2>/dev/null || true" % shlex.quote(paths.game_logs(game_id)))
    add("export SATORU_LOGS=%s" % shlex.quote(paths.game_logs(game_id)))
    add("export SATORU_CONTRACT=%s" % shlex.quote(str(CONTRACT)))
    add("")
    if api and check_updates:
        add("# Update check. Started detached and never waited for: the game starts now and")
        add("# the answer is read by the launcher on some later run. Offline, a rate-limited")
        add("# API or a broken marker all cost exactly nothing here.")
        add("MARK=%s" % shlex.quote(marker))
        add('NOW=$(date +%s)')
        add('THEN=$(stat -f %m "$MARK" 2>/dev/null || echo 0)')
        add('if [ "$(( NOW - THEN ))" -gt %d ]; then' % UPDATE_CHECK_SECONDS)
        add('  mkdir -p "$(dirname "$MARK")"')
        add("  (")
        add("    latest=$(curl -fsSL --max-time 20 %s 2>/dev/null \\" % shlex.quote(api))
        add("      | sed -n 's/.*\"tag_name\"[^\"]*\"\\([^\"]*\\)\".*/\\1/p' | head -1)")
        add('    if [ -n "$latest" ] && [ "$latest" != %s ]; then' % shlex.quote(version))
        add('      printf \'%s\\n\' "$latest" > "$MARK"')
        add("    else")
        add('      : > "$MARK"')
        add("    fi")
        add("  ) >/dev/null 2>&1 </dev/null &")
        add("fi")
        add("")
    plain = (manifest["commands"] or {}).get("launch_plain") or ""
    if manifest.get("modes"):
        out.extend(_mode_shim_lines(manifest, launch, plain))
        return "\n".join(out)
    add('cd "$HERE" || exit 1')
    if plain and plain.split() != (launch + " --plain").split():
        # launch_plain names a command. Treating it as a flag - any value meaning
        # "run launch with --plain" - silently ran the wrong thing for a pack
        # whose plain mode is a different script.
        add('if [ "${1:-}" = "--plain" ]; then')
        add("  shift")
        add("  exec sh -c %s satoru-launch \"$@\""
            % shlex.quote(_launch_command_in_home(plain) + ' "$@"'))
        add("fi")
    add("exec sh -c %s satoru-launch \"$@\"" % shlex.quote(_launch_command_in_home(launch) + ' "$@"'))
    add("")
    return "\n".join(out)


# The part of the shim a pack with [modes] gets. Static shell with the manifest's
# data written into case statements: no python at launch time (a clean macOS may
# not have one), and nothing the manifest says is ever evaluated as code. Every
# string that reaches osascript goes in through argv, never into the script text.
_MODE_SHIM = r"""# Launch modes (docs/launch-modes-design.md). SATORU_MODE set by the caller -
# satoru's TUI, a script - means no dialog; otherwise the person is asked here,
# which is what a double click on the bundle does.
STATE="$HERE/@STATE@"
GAME_NAME=@GAME_NAME@
MODE_IDS=@MODE_IDS@
ALL_SETTINGS=@ALL_SETTINGS@
CHANGE=@CHANGE@
PROMPT=@PROMPT@

mode_label() {
  case "$1" in
@MODE_LABEL_CASES@
  esac
}
mode_by_label() {
  case "$1" in
@MODE_BY_LABEL_CASES@
  esac
  return 1
}
mode_settings() {
  case "$1" in
@MODE_SETTINGS_CASES@
  esac
}
setting_field() {
  case "$1:$2" in
@SETTING_FIELD_CASES@
  esac
}

trim() {
  v=$1
  v=${v#"${v%%[![:space:]]*}"}
  v=${v%"${v##*[![:space:]]}"}
  printf '%s' "$v"
}
state_get() {
  [ -f "$STATE" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in \#*) continue ;; *=*) ;; *) continue ;; esac
    if [ "$(trim "${line%%=*}")" = "$1" ]; then
      trim "${line#*=}"
      return 0
    fi
  done < "$STATE"
}
state_save() {
  tmp="$STATE.tmp.$$"
  {
    printf 'mode = %s\n' "$MODE"
    for name in $ALL_SETTINGS; do
      eval "value=\${VAL_$name:-}"
      [ -z "$value" ] || printf 'setting_%s = %s\n' "$name" "$value"
    done
  } > "$tmp" 2>/dev/null && mv -f "$tmp" "$STATE" 2>/dev/null || rm -f "$tmp" 2>/dev/null
}
valid_ipv4() {
  rest=$1 count=0
  case "$rest" in ''|*[!0-9.]*|.*|*.|*..*) return 1 ;; esac
  while [ -n "$rest" ]; do
    case "$rest" in
      *.*) part=${rest%%.*}; rest=${rest#*.} ;;
      *) part=$rest; rest= ;;
    esac
    [ "${#part}" -le 3 ] && [ "$part" -le 255 ] 2>/dev/null || return 1
    count=$((count + 1))
  done
  [ "$count" = 4 ]
}
# Prints why a value is refused and returns 1; silent and 0 when it is usable.
setting_problem() {
  label=$(setting_field "$1" label)
  custom=$(setting_field "$1" error)
  if [ -z "$2" ]; then
    printf '%s' "${custom:-$label is required}"; return 1
  fi
  case "$2" in *[[:cntrl:]]*)
    printf '%s' "${custom:-$label must be one line of text}"; return 1 ;;
  esac
  if [ "$(setting_field "$1" kind)" = ipv4 ] && ! valid_ipv4 "$2"; then
    printf '%s' "${custom:-$label must be an IPv4 address, like 192.168.0.10}"; return 1
  fi
  pattern=$(setting_field "$1" pattern)
  if [ -n "$pattern" ] && ! printf '%s\n' "$2" | /usr/bin/grep -Eqx -- "$pattern"; then
    printf '%s' "${custom:-$label is not in the expected form}"; return 1
  fi
  return 0
}

osa_choose() {
  osascript -e 'on run argv' -e 'activate' \
    -e 'set r to choose from list (items 5 thru -1 of argv) with title (item 3 of argv) with prompt (item 4 of argv) default items {item 2 of argv} OK button name "Play" cancel button name "Cancel"' \
    -e 'if r is false then return "cancel:"' -e 'return "ok:" & (item 1 of r)' \
    -e 'end run' satoru "$@" 2>/dev/null
}
osa_ask() {
  osascript -e 'on run argv' -e 'activate' -e 'try' \
    -e 'set r to display dialog (item 4 of argv) default answer (item 2 of argv) with title (item 3 of argv) buttons {"Cancel", "OK"} default button "OK" cancel button "Cancel"' \
    -e 'return "ok:" & (text returned of r)' -e 'on error number -128' -e 'return "cancel:"' -e 'end try' \
    -e 'end run' satoru "$@" 2>/dev/null
}
osa_alert() {
  osascript -e 'on run argv' -e 'activate' \
    -e 'display alert (item 2 of argv) message (item 3 of argv) as critical' \
    -e 'end run' satoru "$@" >/dev/null 2>&1 || true
}
no_dialog() {
  printf '%s\n' "$GAME_NAME could not show its launch dialog." \
    "Choose a mode in satoru, or set SATORU_MODE to one of: $MODE_IDS" >&2
  exit 10
}

# ask_settings FORCE NAME... : ask for each value that is missing or refused (all
# of them when FORCE is 1). Returns 20 when the person cancels.
ask_settings() {
  force=$1; shift
  for name in "$@"; do
    eval "value=\${VAL_$name:-}"
    asked=0
    while :; do
      problem=$(setting_problem "$name" "$value") && [ "$force$asked" != 10 ] && break
      [ "$asked" = 0 ] || [ -z "$problem" ] || osa_alert "$GAME_NAME" "$problem"
      answer=$(osa_ask "$value" "$GAME_NAME" "$(setting_field "$name" label)") || no_dialog
      case "$answer" in
        cancel:*) return 20 ;;
        ok:*) value=$(trim "${answer#ok:}") ;;
        *) no_dialog ;;
      esac
      asked=1
    done
    eval "VAL_$name=\$value"
  done
  return 0
}

load_state() {
  for name in $ALL_SETTINGS; do
    eval "VAL_$name=\$(state_get setting_$name)"
  done
}
load_state

if [ -n "${SATORU_MODE:-}" ]; then
  MODE=$SATORU_MODE
  case " $MODE_IDS " in *" $MODE "*) ;; *)
    printf '%s\n' "SATORU_MODE=$MODE is not a mode of $GAME_NAME: use one of $MODE_IDS" >&2
    exit 10 ;;
  esac
  for name in $(mode_settings "$MODE"); do
    eval "given=\${SATORU_SETTING_$(printf '%s' "$name" | tr a-z A-Z):-}"
    [ -z "$given" ] || eval "VAL_$name=\$given"
    eval "value=\${VAL_$name:-}"
    if ! problem=$(setting_problem "$name" "$value"); then
      printf '%s\n' "$problem" \
        "Set SATORU_SETTING_$(printf '%s' "$name" | tr a-z A-Z), or choose in satoru." >&2
      exit 10
    fi
  done
else
  command -v osascript >/dev/null 2>&1 || no_dialog
  MODE=$(state_get mode)
  case " $MODE_IDS " in *" $MODE "*) [ -n "$MODE" ] ;; *) false ;; esac || MODE=${MODE_IDS%% *}
  while :; do
    answer=$(osa_choose "$(mode_label "$MODE")" "$GAME_NAME" "$PROMPT" @CHOOSE_ITEMS@) || no_dialog
    case "$answer" in
      cancel:*) exit 20 ;;
      ok:*) picked=${answer#ok:} ;;
      *) no_dialog ;;
    esac
    if [ "$picked" = "$CHANGE" ]; then
      # Cancelling a change goes back to the list with nothing changed.
      if ask_settings 1 $ALL_SETTINGS; then state_save; else load_state; fi
      continue
    fi
    MODE=$(mode_by_label "$picked") || no_dialog
    ask_settings 0 $(mode_settings "$MODE") || exit 20
    break
  done
fi

state_save
export SATORU_MODE="$MODE"
for name in $(mode_settings "$MODE"); do
  eval "export SATORU_SETTING_$(printf '%s' "$name" | tr a-z A-Z)=\"\$VAL_$name\""
done

cd "$HERE" || exit 1
@PICK_COMMAND@
# Not `[ -z "$TERM" ]`: /bin/sh sets TERM=dumb when it is unset, so under Finder
# that test is never true. No terminal on stderr and no real TERM is a double click.
if [ ! -t 2 ] && [ "${TERM:-dumb}" = dumb ]; then
  # A double click gives the game no terminal, so a refusal written to stderr -
  # a bad address, a missing file - would reach nobody. Keep it and show it.
  ERR=$(mktemp -t satoru-launch 2>/dev/null) || ERR=/dev/null
  sh -c "$COMMAND" satoru-launch "$@" 2>"$ERR"
  rc=$?
  if [ "$rc" != 0 ] && [ "$rc" != 20 ] && [ "$ERR" != /dev/null ]; then
    osa_alert "$GAME_NAME" "$(tail -n 8 "$ERR")"
  fi
  [ "$ERR" = /dev/null ] || { cat "$ERR" >&2; rm -f "$ERR"; }
  exit "$rc"
fi
exec sh -c "$COMMAND" satoru-launch "$@"
"""


def _mode_shim_lines(manifest, launch, plain):
    q = shlex.quote
    modes = [m for m in manifest["modes"] if _MODE_ID_OK.match(m["id"])]
    settings = [s for s in manifest.get("settings") or []
                if _SETTING_NAME_OK.match(s["name"])]
    ids = [m["id"] for m in modes]
    items = [m["label"] for m in modes] + ([CHANGE_SETTINGS_LABEL] if settings else [])
    fields = []
    for s in settings:
        for field in ("label", "kind", "pattern", "error"):
            if s.get(field):
                fields.append("    %s) printf '%%s' %s ;;" % (q(s["name"] + ":" + field),
                                                             q(str(s[field]))))
    run = _launch_command_in_home(launch) + ' "$@"'
    if plain and plain.split() != (launch + " --plain").split():
        pick = ('if [ "${1:-}" = "--plain" ]; then\n  shift\n  COMMAND=%s\nelse\n'
                '  COMMAND=%s\nfi' % (q(_launch_command_in_home(plain) + ' "$@"'), q(run)))
    else:
        pick = "COMMAND=%s" % q(run)
    values = {
        "@STATE@": LAUNCH_STATE_FILE,
        "@GAME_NAME@": q(manifest["game"]["name"]),
        "@MODE_IDS@": q(" ".join(ids)),
        "@ALL_SETTINGS@": q(" ".join(s["name"] for s in settings)),
        "@CHANGE@": q(CHANGE_SETTINGS_LABEL),
        "@PROMPT@": q("How do you want to play %s?" % manifest["game"]["name"]),
        "@MODE_LABEL_CASES@": "\n".join(
            "    %s) printf '%%s' %s ;;" % (m["id"], q(m["label"])) for m in modes),
        "@MODE_BY_LABEL_CASES@": "\n".join(
            "    %s) printf '%%s' %s; return 0 ;;" % (q(m["label"]), m["id"]) for m in modes),
        "@MODE_SETTINGS_CASES@": "\n".join(
            "    %s) printf '%%s' %s ;;" % (m["id"], q(" ".join(
                s["name"] for s in settings if m["id"] in s["modes"])))
            for m in modes),
        "@SETTING_FIELD_CASES@": "\n".join(fields) or "    *) ;;",
        "@CHOOSE_ITEMS@": " ".join(q(i) for i in items),
        "@PICK_COMMAND@": pick,
    }
    # One pass, so a label that happens to contain @SOMETHING@ is left alone.
    text = re.sub(r"@[A-Z_]+@", lambda m: values.get(m.group(0), m.group(0)), _MODE_SHIM)
    return text.split("\n")


def newer_version(paths, game_id):
    """The tag the shim's update check found, or None.

    The check has been running since the shim was written, spending one of
    GitHub's sixty calls an hour to leave its answer in a file. Nothing read it,
    which made the whole feature a cost with no benefit, and made the line the
    contract promises - "there is a v0.2" - something no user could ever see.
    """
    marker = os.path.join(paths.game_cache(game_id), "update-check")
    try:
        with open(marker, encoding="utf-8") as fh:
            tag = fh.read().strip()
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return tag or None


def write_shim(paths, manifest, write=True, check_updates=True):
    """Write the shim into the game's home. Returns its path, or None if the pack
    declares no launch command (a work-in-progress pack, honestly)."""
    text = shim_text(paths, manifest, check_updates=check_updates)
    if not text:
        return None
    if not write:
        return text
    home = paths.home(manifest["game"]["name"])
    if not os.path.isdir(home):
        os.makedirs(home)
    path = os.path.join(home, "launch")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o755)
    return path


def bundle_info(manifest, icon_filename=None):
    """Keys Finder, Spotlight and TCC read. One identifier per game, so a
    microphone prompt is attributed to the game and not to Terminal."""
    game = manifest["game"]
    name, gid = game["name"], game["id"]
    version = str(((manifest.get("source") or {}).get("version")) or "0")
    if version.startswith("v"):
        version = version[1:]
    why = "%s needs this to play." % name
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": name,
        "CFBundleExecutable": "launch",
        "CFBundleIdentifier": "org.satoru.game." + gid,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleSignature": "????",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
        "NSCameraUsageDescription": why,
        "NSMicrophoneUsageDescription": why,
        "NSLocalNetworkUsageDescription": why,
    }
    if icon_filename:
        info["CFBundleIconFile"] = icon_filename
    minimum = (manifest.get("requires") or {}).get("macos") or ""
    match = re.fullmatch(r"\s*>=\s*(\d+(?:\.\d+)*)\s*", minimum)
    if match:
        value = match.group(1)
        info["LSMinimumSystemVersion"] = value if "." in value else value + ".0"
    return info


def bundle_launch_text(exec_path=None, cwd=None, extra_args=None):
    """Contents/MacOS/launch. The .app calls this and nothing else.

    Production (exec_path is None) hands off to the shim in the home, so an
    update rewrites the shim and leaves the bundle — and Spotlight's icon —
    untouched. A thin wrap (exec_path set) is for try-dev: an existing
    launcher stays where it is.
    """
    out = []
    add = out.append
    add("#!/bin/sh")
    add("# satoru bundle launcher. Generated by satoru - edits are lost on update.")
    add('HERE=$(cd "$(dirname "$0")" && pwd -P)')
    add("")
    add('case "$0$HERE" in')
    add("  */AppTranslocation/*)")
    add('    msg="macOS started this game from a read-only copy, so nothing it saves would be kept."')
    add('    fix="Move the app into Applications in Finder - one at a time, not several at once - then open it again."')
    add('    printf \'%s\\n%s\\n\' "$msg" "$fix" >&2')
    add('    # /bin/sh sets TERM=dumb when it is unset, so an empty-TERM test never')
    add("    # sees Finder. No terminal on stderr and no real TERM is a double click.")
    add('    if [ ! -t 2 ] && [ "${TERM:-dumb}" = dumb ]; then')
    add('      osascript -e "display alert \\"$msg\\" message \\"$fix\\"" >/dev/null 2>&1 || true')
    add("    fi")
    add("    exit 10")
    add("    ;;")
    add("esac")
    add("")
    if exec_path:
        if cwd:
            add("cd %s || exit 1" % shlex.quote(cwd))
        parts = [shlex.quote(exec_path)]
        for arg in extra_args or []:
            parts.append(shlex.quote(arg))
        add("exec %s \"$@\"" % " ".join(parts))
    else:
        add('exec "$HERE/../Resources/home/launch" "$@"')
    add("")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# the bundle's icon
#
# Two sources, tried in order, and neither is allowed to fail the install:
# the game's own exe, when a pack names one (`[paths] icon_exe`), and
# otherwise a mark this project drew for itself
# (launcher/assets/satoru-default.icns). A pack is not required to name an
# exe; every bundle still gets an icon rather than Finder's generic one.

RT_ICON = 3
RT_GROUP_ICON = 14
DEFAULT_ICON_ASSET = os.path.join(ROOT, "launcher", "assets", "satoru-default.icns")


def _read_struct(fh, fmt, offset):
    fh.seek(offset)
    size = struct.calcsize(fmt)
    data = fh.read(size)
    if len(data) != size:
        raise ValueError("truncated at offset %d reading %r" % (offset, fmt))
    return struct.unpack(fmt, data)


def _pe_sections(fh):
    """(sections, resource_dir_rva). sections is [(va, size_of_raw, ptr_to_raw), ...].

    Reads only what it needs — DOS header, PE + optional header, the section
    table — never the whole file: a game's own exe can be very large, and
    everything this cares about lives in the first few kilobytes.
    """
    fh.seek(0)
    if fh.read(2) != b"MZ":
        raise ValueError("not a PE file (no MZ signature)")
    (e_lfanew,) = _read_struct(fh, "<I", 0x3C)
    fh.seek(e_lfanew)
    if fh.read(4) != b"PE\0\0":
        raise ValueError("no PE header at e_lfanew")
    file_header_off = e_lfanew + 4
    _, num_sections = _read_struct(fh, "<HH", file_header_off)
    (size_opt,) = _read_struct(fh, "<H", file_header_off + 16)
    opt_off = file_header_off + 20
    (magic,) = _read_struct(fh, "<H", opt_off)
    if magic == 0x10B:      # PE32
        dd_off = opt_off + 96
    elif magic == 0x20B:    # PE32+
        dd_off = opt_off + 112
    else:
        raise ValueError("unknown optional header magic 0x%x" % magic)
    rsrc_rva, rsrc_size = _read_struct(fh, "<II", dd_off + 2 * 8)
    if not rsrc_rva or not rsrc_size:
        raise ValueError("this exe has no resource directory")
    sec_off = opt_off + size_opt
    sections = []
    for i in range(num_sections):
        _, vsize, va, sraw, praw = _read_struct(fh, "<8sIIII", sec_off + i * 40)
        sections.append((va, sraw, praw))
    return sections, rsrc_rva


def _rva_to_offset(sections, rva):
    for va, sraw, praw in sections:
        if va <= rva < va + sraw:
            return praw + (rva - va)
    raise ValueError("rva 0x%x is outside every section" % rva)


def _read_resource_dir(fh, sections, dir_rva):
    off = _rva_to_offset(sections, dir_rva)
    _, _, _, _, named, idc = _read_struct(fh, "<IIHHHH", off)
    fh.seek(off + 16)
    entries = []
    for _ in range(named + idc):
        id_or_name, offset_to_data = struct.unpack("<II", fh.read(8))
        entries.append((id_or_name, offset_to_data))
    return entries


def _find_id(entries, wanted_id):
    for id_or_name, offset in entries:
        if not (id_or_name & 0x80000000) and id_or_name == wanted_id:
            return offset
    return None


def _resource_data(fh, sections, rsrc_rva, dir_offset):
    """Follow a Type-level offset (a subdirectory) down through the one
    name and the one language it has, to the raw bytes of the resource."""
    names = _read_resource_dir(fh, sections, rsrc_rva + (dir_offset & 0x7FFFFFFF))
    if not names:
        return None
    _, lang_off = names[0]
    if lang_off & 0x80000000:
        langs = _read_resource_dir(fh, sections, rsrc_rva + (lang_off & 0x7FFFFFFF))
        if not langs:
            return None
        _, data_off = langs[0]
    else:
        data_off = lang_off
    data_entry_off = _rva_to_offset(sections, rsrc_rva + data_off)
    rva, size, _cp, _res = _read_struct(fh, "<IIII", data_entry_off)
    fh.seek(_rva_to_offset(sections, rva))
    return fh.read(size)


def pe_best_icon_ico(exe_path):
    """The exe's own icon, repacked as a single-image .ico, or None.

    A Windows exe carries a whole family of sizes under RT_GROUP_ICON /
    RT_ICON. Only the largest (by area, then by bit depth) is kept: it is
    the one `sips` actually uses when given an .ico with several images,
    so shipping the rest would only make the file bigger, not the result
    better. Any malformed input — not a PE, no resources, a truncated
    section — is a None, never an exception: the caller falls back.
    """
    try:
        with open(exe_path, "rb") as fh:
            sections, rsrc_rva = _pe_sections(fh)
            root = _read_resource_dir(fh, sections, rsrc_rva)
            group_type_off = _find_id(root, RT_GROUP_ICON)
            icon_type_off = _find_id(root, RT_ICON)
            if group_type_off is None or icon_type_off is None:
                return None
            group_bytes = _resource_data(fh, sections, rsrc_rva, group_type_off)
            if not group_bytes:
                return None
            icon_names = _read_resource_dir(
                fh, sections, rsrc_rva + (icon_type_off & 0x7FFFFFFF))

            _, _, count = struct.unpack_from("<HHH", group_bytes, 0)
            best = None
            for i in range(count):
                (bw, bh, colors, _res, planes, bitcount, byte_count, icon_id
                 ) = struct.unpack_from("<BBBBHHIH", group_bytes, 6 + i * 14)
                width, height = (bw or 256), (bh or 256)
                score = (width * height, bitcount)
                if best is not None and score <= best[0]:
                    continue
                icon_data_off = _find_id(icon_names, icon_id)
                if icon_data_off is None:
                    continue
                img = _resource_data(fh, sections, rsrc_rva, icon_data_off)
                if not img:
                    continue
                best = (score, bw, bh, colors, planes, bitcount, img)
            if best is None:
                return None
            _, bw, bh, colors, planes, bitcount, img = best
            header = struct.pack("<HHH", 0, 1, 1)
            entry = struct.pack("<BBBBHHII", bw, bh, colors, 0, planes, bitcount,
                                 len(img), 6 + 16)
            return header + entry + img
    except (OSError, ValueError, struct.error, IndexError):
        return None


def _icon_source_path(paths, manifest):
    """Where the manifest's own `[paths] icon_exe` points, resolved against
    the game's home - or None when the key is absent, or when it would step
    outside that home. The manifest arrives off the internet; `..` and an
    absolute path are refused the same way a bad game id already is."""
    rel = ((manifest.get("paths") or {}).get("icon_exe") or "").strip()
    if not rel:
        return None
    if os.path.isabs(rel) or rel.startswith("~"):
        return None
    home = os.path.abspath(paths.home(manifest["game"]["name"]))
    candidate = os.path.abspath(os.path.join(home, rel))
    if candidate != home and not candidate.startswith(home + os.sep):
        return None
    return candidate


def _sips_to_icns(src_path, dest_path):
    """The one external tool the icon feature needs. Any way it can fail —
    the binary missing, an image it cannot read, a full disk — is the
    caller's cue to fall back, not an exception to propagate."""
    try:
        with open(os.devnull, "wb") as null:
            code = subprocess.call(
                ["sips", "-s", "format", "icns", src_path, "--out", dest_path],
                stdout=null, stderr=null)
    except OSError:
        return False
    return code == 0 and os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0


def install_bundle_icon(paths, manifest, resources_dir, convert=_sips_to_icns):
    """Best effort, in this order: the game's own icon, the icon already
    sitting there from a previous install, satoru's own mark. Returns the
    filename (relative to Contents/Resources) for CFBundleIconFile, or None
    when even the fallback asset is missing. Never raises: a bundle must
    still be written when every part of this fails.
    """
    try:
        icns_name = "%s.icns" % Paths._checked(manifest["game"]["id"])
    except Exception:
        # A game id this broken never gets this far in practice - install_game
        # rejects it long before a bundle is written - but this function's own
        # promise is to never raise, so a filename that is not safe to use
        # means no icon rather than a write outside Contents/Resources.
        return None
    dest = os.path.join(resources_dir, icns_name)
    try:
        exe = _icon_source_path(paths, manifest)
        if exe and os.path.isfile(exe):
            ico_bytes = pe_best_icon_ico(exe)
            if ico_bytes:
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=".ico")
                try:
                    with os.fdopen(tmp_fd, "wb") as fh:
                        fh.write(ico_bytes)
                    if convert(tmp_path, dest):
                        return icns_name
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        # Extraction unavailable or failed this time: an icon a previous,
        # successful install already left behind is still better than
        # downgrading a working bundle to the generic mark.
        if os.path.isfile(dest):
            return icns_name
    except Exception:
        pass
    try:
        if os.path.isfile(DEFAULT_ICON_ASSET):
            shutil.copyfile(DEFAULT_ICON_ASSET, dest)
            return icns_name
    except Exception:
        pass
    return None


def write_bundle(paths, manifest, exec_path=None, cwd=None, extra_args=None,
                  icon_convert=None):
    """Create or refresh the .app. Returns its path. Idempotent: a second
    Install updates the plist and launcher in place, it does not grow a twin."""
    name = manifest["game"]["name"]
    bundle = paths.bundle(name)
    macos = os.path.join(bundle, "Contents", "MacOS")
    resources = os.path.join(bundle, "Contents", "Resources")
    if not os.path.isdir(macos):
        os.makedirs(macos)
    if not os.path.isdir(resources):
        os.makedirs(resources)
    if icon_convert is not None:
        icon_filename = install_bundle_icon(paths, manifest, resources,
                                             convert=icon_convert)
    else:
        icon_filename = install_bundle_icon(paths, manifest, resources)
    with open(os.path.join(bundle, "Contents", "Info.plist"), "wb") as fh:
        plistlib.dump(bundle_info(manifest, icon_filename=icon_filename), fh)
    launch = os.path.join(macos, "launch")
    with open(launch, "w", encoding="utf-8") as fh:
        fh.write(bundle_launch_text(
            exec_path=exec_path, cwd=cwd, extra_args=extra_args))
    os.chmod(launch, 0o755)
    # Finder caches an app's icon on the bundle itself; touch it so a changed
    # icon is picked up instead of the one it saw the last time it looked.
    try:
        os.utime(bundle, None)
    except OSError:
        pass
    return bundle


# ----------------------------------------------------------------------------
# the install pipeline


class SubprocessRunner(object):
    """Runs a pack's command and streams its output, line by line, to a callback."""

    def run(self, command, cwd=None, env=None, on_output=None):
        proc = subprocess.Popen(
            ["bash", "-c", command], cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if on_output:
                    on_output(line)
        finally:
            proc.stdout.close()
        return proc.wait()


def strip_quarantine(path):
    """A browser-downloaded archive carries com.apple.quarantine, and the pack's
    own preflight runs an ad-hoc-signed binary that Gatekeeper would then kill.
    We downloaded it, so removing the flag is ours to do."""
    with open(os.devnull, "wb") as null:
        subprocess.call(["xattr", "-dr", "com.apple.quarantine", path],
                        stdout=null, stderr=null)


def _install_result(ok, step, message="", code=0, **extra):
    out = {"ok": ok, "step": step, "message": message, "code": code}
    out.update(extra)
    return out


# Contract v1 gives these five codes a meaning of their own, and everything else
# one meaning: whatever ran is not a contract-1 pack. The difference matters to
# the reader - "this machine is missing something" sends them to fix it, while
# "the pack cannot answer" sends them to update the pack - and it is the whole
# reason the contract numbers its exits at all.
CONTRACT_EXITS = {
    10: "the pack says this machine is not ready for it",
    11: "there is nothing here to do",
    12: "the pack's own files are not the ones it expects - fetch it again",
    20: "cancelled",
    1: "the pack broke in a way it did not expect",
}

_EXIT_REASON = {10: "refused", 20: "refused", 11: "nothing-to-do",
                12: "stale-files", 1: "failed"}


def explain_exit(code, command, output=None):
    """(reason, message) for the exit code a pack answered with.

    The pack's own last words are appended where it has any: for code 10 the
    contract says its message is shown as it wrote it, and for a pack that does
    not speak the contract at all its complaint - `unknown flag --preflight` -
    is the single most useful thing on the screen.
    """
    said = [line for line in (output or []) if line and line.strip()]
    tail = said[-1].strip() if said else ""
    if code == 10 and said:
        return _EXIT_REASON[code], CONTRACT_EXITS[code] + ":\n" + "\n".join(output)
    if code in CONTRACT_EXITS:
        message = CONTRACT_EXITS[code]
        return _EXIT_REASON[code], (message + ": " + tail) if tail else message
    message = ("this pack does not understand `%s` (exit %d) - it is probably "
               "older than the manifest that points at it" % (command, code))
    return "not-contract", (message + ": " + tail) if tail else message


def _recorder(sink, on_output, keep=40):
    """Feed the caller every line, and keep the tail for the failure message."""
    def watcher(line):
        sink.append(line)
        del sink[:-keep]
        if on_output:
            on_output(line)
    return watcher


def _clean_up_after(paths, name):
    """Remove the empty shell of a failed install; return what was left behind.

    An install that got nowhere leaves directories we made and the pack did not
    fill: Finder renders a .app with nothing in it as broken, and nothing records
    it, so nobody would ever find it again. What the pack did write is a different
    matter - that is its work, not ours to delete - so it stays, and its path is
    returned to be named.
    """
    home, bundle = paths.home(name), paths.bundle(name)
    if os.path.isdir(home) and os.listdir(home):
        return home
    for directory in (home, os.path.join(bundle, "Contents", "Resources"),
                      os.path.join(bundle, "Contents"), bundle):
        try:
            os.rmdir(directory)
        except OSError:
            break
    return ""


def install_game(manifest, paths, probe=None, runner=None, fetch=None, unpack=None,
                 unquarantine=None, on_output=None, check_updates=True):
    """Install a game, in the one order that makes each failure cheap.

    Requirements first, so a machine that cannot run the game never spends
    133 MB finding out. Unquarantine before preflight, or the pack's own probe
    is SIGKILLed. Preflight before the first write, so a refusal leaves nothing
    half-built behind.
    """
    fetch = fetch or fetch_pack
    unpack = unpack or globals()["unpack"]
    unquarantine = unquarantine or strip_quarantine
    runner = runner or SubprocessRunner()

    game = manifest["game"]
    name, game_id = game["name"], game["id"]
    commands = manifest["commands"] or {}

    # The volume the bytes land on is the bundle's, not the library's: per
    # ADR-0001 the pack installs into the home inside the .app, and the library
    # is only where a game's own files go. Measuring the library reports a
    # roomy external disk while the engine fills the internal one.
    requirements = check_requirements(manifest["requires"], probe=probe,
                                      root=paths.home(name))
    if not all_met(requirements):
        unmet = [r for r in requirements if not r["ok"]]
        return _install_result(False, "requirements", reason="requirements",
                       requirements=requirements,
                       message="; ".join(r["label"] + ": " + r["detail"] for r in unmet))

    source = manifest.get("source")
    if not source or not source.get("url"):
        return _install_result(False, "source", reason="no-source",
                       requirements=requirements,
                       message="this pack declares no release to download - "
                               "see its own instructions for installing it by hand")

    cache = paths.game_cache(game_id)
    try:
        archive = fetch(source, cache, None)
        unpacked = unpack(archive, os.path.join(cache, "unpacked"))
    except PackError as exc:
        return _install_result(False, "fetch", code=12, reason="stale-files",
                       requirements=requirements, message=str(exc))

    unquarantine(unpacked)

    # The contract names these in the command environment, so they have to be
    # there. A pack that believes the contract and writes "$SATORU_LOGS/install.log"
    # would otherwise be the one to discover the promise was empty.
    logs = paths.game_logs(game_id)
    for d in (cache, logs):
        if not os.path.isdir(d):
            os.makedirs(d)

    env = dict(os.environ)
    env.update({
        "SATORU_GAME_HOME": paths.home(name),
        "SATORU_GAME_ID": game_id,
        "SATORU_LIBRARY": paths.library,
        "SATORU_CACHE": cache,
        "SATORU_LOGS": logs,
        "SATORU_CONTRACT": str(CONTRACT),
    })

    # 11 is the contract's word for "there is nothing here to do", and an install
    # asked to run twice is the case it exists for. Treating it as a failure
    # punishes the one author who read the exit codes and believed them.
    nothing_to_do = False

    preflight = commands.get("preflight")
    if preflight:
        said = []
        code = runner.run(preflight, cwd=unpacked, env=env,
                          on_output=_recorder(said, on_output))
        if code == 11:
            nothing_to_do = True
        elif code != 0:
            # Nothing has been written yet, and nothing should be: leaving a
            # half-built home is what this whole ordering exists to prevent.
            reason, message = explain_exit(code, preflight, said)
            return _install_result(False, "preflight", code=code, reason=reason,
                           requirements=requirements, message=message,
                           output="\n".join(said))

    install = commands.get("install")
    if not install:
        return _install_result(False, "install", reason="no-command",
                       requirements=requirements,
                       message="this pack declares no install command")

    home = paths.home(name)
    if not os.path.isdir(home):
        os.makedirs(home)
    said = []
    code = 11 if nothing_to_do else runner.run(
        install, cwd=unpacked, env=env, on_output=_recorder(said, on_output))
    if code == 11:
        nothing_to_do = True
    elif code != 0:
        reason, message = explain_exit(code, install, said)
        leftovers = _clean_up_after(paths, name)
        if leftovers:
            # Half an engine is not ours to throw away, and nothing records it -
            # the install did not finish - so the message is the only place it
            # can be named.
            message += " (what it wrote is still in %s)" % leftovers
        return _install_result(False, "install", code=code, reason=reason,
                       requirements=requirements, message=message,
                       leftovers=leftovers, output="\n".join(said))

    # The shim first: installed.toml is a claim, and the shim is what makes it
    # true. Recorded first, a shim that fails to write - a full disk, a read-only
    # volume - leaves the state file saying installed while the launcher looks for
    # a launch script that is not there and says the opposite.
    write_shim(paths, manifest, check_updates=check_updates)
    # The shim is the game; the bundle is how Finder finds it. Written before
    # installed.toml so a claim of "installed" is never a broken icon.
    write_bundle(paths, manifest)
    entry = record_install(paths, manifest, (source or {}).get("sha256"))
    if nothing_to_do:
        return _install_result(True, "done", reason="nothing-to-do",
                       requirements=requirements, entry=entry,
                       message="%s was already installed; nothing needed doing"
                               % name)
    return _install_result(True, "done", reason="done", requirements=requirements,
                   entry=entry)


# ----------------------------------------------------------------------------
# model


def expand(p):
    return os.path.expandvars(os.path.expanduser(p))


def resolve(p):
    """~ and $VARS expanded; relative paths are relative to the satoru root."""
    p = expand(p)
    if not os.path.isabs(p):
        p = os.path.join(ROOT, p)
    return p


def command_target(cmd):
    """The path an action's command points at (first word after bash/sh)."""
    words = shlex.split(cmd)
    if not words:
        return None
    if words[0] in ("bash", "sh", "zsh") and len(words) > 1:
        return resolve(words[1])
    return resolve(words[0])


# ----------------------------------------------------------------------------
# where things go

class Paths(object):
    """The layout, in one place, so that no other code has to guess.

    An installed game is a bundle in ~/Applications with its home *inside* it
    (ADR-0001), so that it is one object a person can move, back up and throw
    away. What is not inside it is the game's own files: 2.4 GB of ours against
    45 GB of theirs. Those live in a library, and the library is what `root`
    moves — that frees 96% of the space without taking the icon out of Launchpad.

    Logs go where Console.app looks for them; the download cache goes where the
    system already knows it is disposable. Neither follows `root`.
    """

    def __init__(self, home=None, root=None):
        self.home_dir = home or os.path.expanduser("~")
        lib = os.path.join(self.home_dir, "Library")
        self.support = os.path.join(lib, "Application Support", "satoru")
        self.caches = os.path.join(lib, "Caches", "satoru")
        self.logs = os.path.join(lib, "Logs", "satoru")
        self.applications = os.path.join(self.home_dir, "Applications", "satoru")
        self.config_file = os.path.join(self.support, "config.toml")
        self.installed_file = os.path.join(self.support, "installed.toml")
        self.library = expand(root) if root else os.path.join(self.support, "library")

    @staticmethod
    def _checked(game_id):
        """An id arrives in a manifest, and a manifest arrives off the internet."""
        if not game_id or not _ID_OK.match(str(game_id)):
            raise ValueError(
                "%r is not a usable game id (lower-case letters, digits and dashes)"
                % (game_id,))
        return game_id

    def bundle(self, name):
        return os.path.join(self.applications, _bundle_filename(name))

    def home(self, name):
        """The game's home, inside its own bundle. Passed to the pack as
        SATORU_GAME_HOME; everything the pack installs goes here and nowhere else."""
        return os.path.join(self.bundle(name), "Contents", "Resources", "home")

    def shader_cache(self, name):
        """Inside the home, so that the system cannot reclaim it.

        Warmed pipelines are the difference between a first match and a stutter
        festival - 434 MB of them for Overwatch - and until now they sat in the
        user cache directory, which macOS may purge whenever it likes.
        """
        return os.path.join(self.home(name), "shader-cache")

    def game_cache(self, game_id):
        return os.path.join(self.caches, self._checked(game_id))

    def game_logs(self, game_id):
        return os.path.join(self.logs, self._checked(game_id))


def _bundle_filename(name):
    """A display name is not a filename.

    "/" cannot appear in one at all, and ":" is a separator to the classic Mac
    APIs - Finder renders it back as "/", which is how a game called "A:B" ends
    up looking like a directory.
    """
    clean = str(name).replace("/", "-").replace(":", "-").strip().lstrip(".")
    clean = "".join(ch for ch in clean if ch >= " ")
    return (clean or "untitled") + ".app"


CONFIG_KEYS = ("root", "check_updates")


def load_config(path):
    """Never fails: a missing or broken config gives defaults and says what it saw."""
    cfg = {"root": None, "check_updates": True, "warnings": []}
    try:
        data = load_toml(path)
    except (IOError, OSError):
        return cfg
    except ValueError as exc:
        cfg["warnings"].append("%s: %s" % (path, exc))
        return cfg
    for key, value in data.items():
        if key not in CONFIG_KEYS:
            cfg["warnings"].append("%s: unknown key %r" % (path, key))
            continue
        cfg[key] = value
    if not isinstance(cfg["check_updates"], bool):
        cfg["warnings"].append("%s: check_updates must be true or false" % path)
        cfg["check_updates"] = True
    return cfg


class Game(object):
    """One game as the launcher sees it, from either manifest shape.

    A contract-1 manifest changes what Setup means. The pack stops naming a
    script for the umbrella to shell out to and starts naming a release for it
    to install, so the action is offered on the strength of [source] rather than
    on a file existing at a path.
    """

    def __init__(self, path, data):
        self.path = path
        self.dir = os.path.dirname(path)
        self.manifest, self.errors = parse_manifest(data)
        game = self.manifest["game"]
        self.contract = self.manifest["contract"]
        self.name = game["name"] or os.path.basename(self.dir)
        self.id = game["id"] or os.path.basename(self.dir)
        self.status = game["status"]
        self.summary = str(game["summary"]).strip()
        self.notes = str(game["notes"]).strip()
        self.source = self.manifest["source"]
        install = self.manifest["install"]
        self.home = install["home"]
        self.manual_url = install["manual_url"]
        self.foreign_note = install["foreign_note"]
        commands, paths_ = self.manifest["commands"], self.manifest["paths"]
        self.cmds = {
            "setup": commands["install"],
            "launch": commands["launch"],
            "launch_plain": commands["launch_plain"],
            "profile": paths_["profile"],
            "logs": paths_["logs"],
        }

    def validate(self):
        errs = list(self.errors)
        if self.id != os.path.basename(self.dir):
            errs.append("id %r does not match its directory %r"
                        % (self.id, os.path.basename(self.dir)))
        if self.contract == 0 and self.status != "wip" and not self.cmds["launch"]:
            errs.append("status %s but no launch command" % self.status)
        return errs

    def actions(self):
        """ACTIONS, with Launch replaced by one entry per launch mode, if any."""
        modes = self.manifest.get("modes") or []
        if not modes or self.contract < 1:
            return ACTIONS
        out = [ACTIONS[0]]
        for mode in modes:
            out.append(("mode:" + mode["id"], "Launch: " + mode["label"], "cmd"))
        if self.manifest.get("settings"):
            out.append(("settings", CHANGE_SETTINGS_LABEL, "cmd"))
        out.extend(ACTIONS[2:])
        return out

    def game_home(self, paths=None):
        paths = paths or Paths()
        entry = installed_entry(paths, self.id)
        return (entry or {}).get("home") or paths.home(self.name)

    def last_mode(self, paths=None):
        """The mode the person chose last time, or the first one."""
        return launch_mode_default(self.manifest, read_launch_state(self.game_home(paths)))

    def installed_home(self, paths=None):
        """Where this game would be, once installed. Only meaningful for v1."""
        return (paths or Paths()).home(self.name)

    def action_state(self, key, paths=None):
        """(state, detail): state is 'ok' | 'soon' | 'missing'."""
        if key.startswith("mode:") or key == "settings":
            state, detail = self.action_state("launch", paths)
            if state != "ok":
                return state, detail
            if key == "settings":
                return "ok", ", ".join(s["label"] for s in self.manifest["settings"])
            mode_id = key[len("mode:"):]
            names = [s["label"] for s in mode_settings(self.manifest, mode_id)]
            last = " (last)" if mode_id == self.last_mode(paths) else ""
            return "ok", "SATORU_MODE=%s%s%s" % (
                mode_id, " + " + ", ".join(names) if names else "", last)
        if self.status == "wip":
            return "soon", ""
        if self.contract >= 1:
            return self._v1_action_state(key, paths)
        cmd = self.cmds.get(key, "")
        if not cmd:
            return "soon", ""
        kind = dict((k, kind) for k, _, kind in ACTIONS)[key]
        if kind == "cmd":
            target = command_target(cmd)
            if target is None or not os.path.exists(target):
                return "missing", os.path.relpath(target, ROOT) if target and target.startswith(ROOT) else (target or cmd)
            return "ok", cmd
        target = resolve(cmd)
        if not os.path.exists(target):
            return "missing", target
        return "ok", target

    def _v1_action_state(self, key, paths=None):
        paths = paths or Paths()
        # The home that was recorded, not one recomputed from today's display
        # name: renaming a game in its manifest is a normal thing for an author
        # to do, and it used to leave the install on disk with the launcher
        # looking somewhere else and reporting it missing.
        entry = installed_entry(paths, self.id)
        home = (entry or {}).get("home") or paths.home(self.name)
        if key == "setup":
            if not self.source or not self.source.get("url"):
                # An honest "there is no automatic install", not a broken manifest.
                return "soon", ""
            return "ok", "install %s (%s)" % (
                self.name, self.source.get("version") or "latest")
        if key in ("launch", "launch_plain"):
            if not self.cmds.get(key):
                return "soon", ""
            shim = os.path.join(home, "launch")
            if not os.path.isfile(shim):
                # Nothing is missing: it has simply not been installed yet, and
                # a path in the listing would read like something went wrong.
                return "missing", "not installed yet"
            return "ok", shim + (" --plain" if key == "launch_plain" else "")
        target = self.cmds.get(key) or ""
        if not target:
            return "soon", ""
        full = target if os.path.isabs(target) else os.path.join(home, target)
        if not os.path.exists(full):
            # Same reasoning as launch: before an install there is nothing to be
            # missing, and a path here reads as a fault rather than a state.
            return "missing", full if os.path.isdir(home) else "not installed yet"
        return "ok", full


def load_games(games_dir=GAMES_DIR):
    games = []
    if not os.path.isdir(games_dir):
        return games
    for entry in sorted(os.listdir(games_dir)):
        toml_path = os.path.join(games_dir, entry, "game.toml")
        if not os.path.isfile(toml_path):
            continue
        try:
            data = load_toml(toml_path)
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            # One pack's manifest is one pack's problem. Taking the catalogue
            # down with it hides every other game behind a traceback, and the
            # reader cannot even see which manifest was at fault.
            game = Game(toml_path, {"game": {"id": entry, "name": entry,
                                             "status": "wip"}})
            game.errors = ["this manifest could not be read: %s" % exc]
            games.append(game)
            continue
        games.append(Game(toml_path, data))
    # rc first, then playable, then wip; stable within a group
    order = {s: i for i, s in enumerate(STATUSES)}
    games.sort(key=lambda g: order.get(g.status, 99))
    return games


# ----------------------------------------------------------------------------
# running things (outside curses)

def current_paths():
    """Paths as this machine's config says they are."""
    base = Paths()
    cfg = load_config(base.config_file)
    return Paths(root=cfg["root"]) if cfg["root"] else base


def _install_via_umbrella(game, paths):
    """Contract v1: the umbrella installs the pack rather than shelling out to it."""
    sys.stdout.write("Installing %s ...\n" % game.name)
    sys.stdout.flush()

    def echo(line):
        sys.stdout.write("  " + line + "\n")
        sys.stdout.flush()

    result = install_game(game.manifest, paths, on_output=echo,
                          check_updates=load_config(paths.config_file)["check_updates"])
    if result["ok"]:
        return 0, "%s installed into %s. Spotlight and Launchpad can open it." % (
            game.name, paths.bundle(game.name))
    if result["step"] == "requirements":
        unmet = [r for r in result["requirements"] if not r["ok"]]
        lines = []
        for req in unmet:
            fix = req["fix"]
            if fix and fix["kind"] == "command":
                lines.append("%s: %s - run: %s" % (req["label"], req["detail"], fix["run"]))
            elif fix:
                lines.append("%s: %s - %s" % (req["label"], req["detail"], fix["hint"]))
            else:
                lines.append("%s: %s (nothing can change this)" % (req["label"], req["detail"]))
        return 1, "cannot install here. " + "; ".join(lines)
    return 1, "%s failed at %s: %s" % (game.name, result["step"], result["message"])


def _terminal_ask(setting, current):
    """Enter keeps the current value. EOF or Ctrl-C is "cancel"."""
    prompt = "%s [%s]: " % (setting["label"], current) if current else "%s: " % setting["label"]
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\n")
        return None
    return answer if answer.strip() else current


def _terminal_say(text):
    sys.stdout.write("  %s\n" % text)
    sys.stdout.flush()


def launch_with_mode(game, paths, key, ask=None, say=None, call=None):
    """TUI side of launch modes: ask here, in the terminal, then run the shim with
    SATORU_MODE set so that it shows no dialog of its own. Returns (rc, message)."""
    ask, say = ask or _terminal_ask, say or _terminal_say
    call = call or subprocess.call
    home = game.game_home(paths)
    last = game.last_mode(paths)
    if key == "settings":
        env = resolve_launch_mode(game.manifest, home, last, ask, say, force=True)
        if env is None:
            return 20, "settings unchanged."
        return 0, "settings saved for %s." % game.name
    mode_id = key[len("mode:"):] if key.startswith("mode:") else last
    env = resolve_launch_mode(game.manifest, home, mode_id, ask, say)
    if env is None:
        return 20, "cancelled; %s was not started." % game.name
    full = dict(os.environ)
    full.update(env)
    args = ["--plain"] if key == "launch_plain" else []
    rc = call([os.path.join(home, "launch")] + args, env=full)
    return rc, "%s (%s) exited with %d." % (key, mode_id, rc)


def run_action(game, key):
    """Returns (returncode, message). Called with the terminal in normal mode."""
    paths = current_paths()
    state, detail = game.action_state(key, paths)
    if state == "soon":
        if key == "setup" and game.manual_url:
            # Not a dead button: there is a way to install this, it is just not ours.
            return 0, "%s has no automatic install yet. Instructions: %s" % (
                game.name, game.manual_url)
        return 0, "%s: SOON — not available for %s yet." % (key, game.name)
    if state == "missing":
        if game.contract >= 1 and (key in ("launch", "launch_plain", "settings")
                                   or key.startswith("mode:")):
            return 1, "%s is not installed yet - run Setup first." % game.name
        return 1, "%s: missing %s (submodule not checked out?)" % (key, detail)
    if game.contract >= 1 and key == "setup":
        return _install_via_umbrella(game, paths)
    if game.contract >= 1 and game.manifest.get("modes") and (
            key in ("launch", "launch_plain", "settings") or key.startswith("mode:")):
        return launch_with_mode(game, paths, key)
    if game.contract >= 1 and key in ("launch", "launch_plain"):
        args = ["--plain"] if key == "launch_plain" else []
        entry = installed_entry(paths, game.id)
        shim = os.path.join((entry or {}).get("home") or paths.home(game.name), "launch")
        rc = subprocess.call([shim] + args)
        return rc, "%s exited with %d." % (key, rc)
    kind = dict((k, kind) for k, _, kind in ACTIONS)[key]
    if kind == "file":
        pager = os.environ.get("PAGER", "less")
        try:
            rc = subprocess.call([pager, detail])
        except OSError:
            with open(detail) as f:
                sys.stdout.write(f.read())
            rc = 0
        return rc, "%s closed." % os.path.basename(detail)
    if kind == "dir":
        if sys.platform == "darwin":
            rc = subprocess.call(["open", detail])
            return rc, "opened %s in Finder." % detail
        sys.stdout.write("\n".join(sorted(os.listdir(detail))) + "\n")
        return 0, detail
    cmd = game.cmds[key]
    cwd = ROOT
    if key != "setup" and game.home:
        home = expand(game.home)
        if os.path.isdir(home):
            cwd = home
    sys.stdout.write("$ %s   (cwd %s)\n" % (cmd, cwd))
    sys.stdout.flush()
    rc = subprocess.call(["bash", "-c", cmd], cwd=cwd)
    return rc, "%s exited with %d." % (key, rc)


# ----------------------------------------------------------------------------
# plain mode

def describe(games, out=sys.stdout, paths=None, games_dir=GAMES_DIR):
    if not games:
        out.write(NO_GAMES_HINT % games_dir)
        return
    absent = unchecked_packs(games_dir)
    if absent:
        out.write("Not listed: %s — submodule%s not checked out.\n"
                  "Run `git submodule update --init --recursive`, or take the release\n"
                  "tarball, which needs no clone: https://github.com/NerRobDog/satoru/releases\n\n"
                  % (", ".join(absent), "" if len(absent) == 1 else "s"))
    for g in games:
        out.write("%s [%s] — %s\n" % (g.name, g.id, STATUS_LABEL.get(g.status, g.status)))
        # Every manifest writes one, and until now no reader had ever seen it.
        if g.summary:
            out.write("    %s\n" % g.summary)
        newer = newer_version(paths or current_paths(), g.id)
        if newer:
            out.write("    %-32s %s\n" % ("Update available", newer))
        for key, label, _ in g.actions():
            state, detail = g.action_state(key)
            if state == "ok":
                out.write("    %-32s %s\n" % (label, detail))
            elif state == "soon":
                out.write("    %-32s · SOON\n" % label)
            else:
                out.write("    %-32s · missing: %s\n" % (label, detail))
        if g.manual_url:
            out.write("    %-32s %s\n" % ("Instructions", g.manual_url))
        if g.foreign_note:
            out.write("    %-32s %s\n" % ("Note", g.foreign_note))
        if g.notes:
            for line in g.notes.splitlines():
                out.write("      %s\n" % line)
        out.write("\n")


def check(games_dir=GAMES_DIR, out=sys.stdout):
    ok = True
    if not os.path.isdir(games_dir):
        out.write("no games dir: %s\n" % games_dir)
        return False
    for entry in sorted(os.listdir(games_dir)):
        d = os.path.join(games_dir, entry)
        if not os.path.isdir(d):
            continue
        p = os.path.join(d, "game.toml")
        if not os.path.isfile(p):
            empty = not os.listdir(d)
            out.write("games/%s: no game.toml — %s\n" % (
                entry,
                "submodule not checked out (git submodule update --init)" if empty
                else "the pack does not declare one"))
            ok = False if empty else ok
            continue
        try:
            g = Game(p, load_toml(p))
            errs = g.validate()
        except Exception as e:  # parse error
            errs = ["parse error: %s" % e]
        if errs:
            ok = False
            out.write("games/%s/game.toml: %s\n" % (entry, "; ".join(errs)))
        else:
            out.write("games/%s/game.toml: ok (%s, %s)\n" % (entry, g.name, g.status))
    return ok


# ----------------------------------------------------------------------------
# curses TUI

def tui(stdscr, games):
    import curses

    curses.curs_set(0)
    curses.use_default_colors()
    has_color = curses.has_colors()
    if has_color:
        curses.init_pair(1, curses.COLOR_GREEN, -1)   # rc
        curses.init_pair(2, curses.COLOR_CYAN, -1)    # playable
        curses.init_pair(3, curses.COLOR_YELLOW, -1)  # wip
        curses.init_pair(4, curses.COLOR_BLACK, -1)   # disabled (dim)
    dim = curses.A_DIM
    status_attr = {"rc": curses.color_pair(1) if has_color else 0,
                   "playable": curses.color_pair(2) if has_color else 0,
                   "wip": (curses.color_pair(3) if has_color else 0) | dim}

    sel = 0
    action_sel = 0
    mode = "games"  # games | actions
    message = ""

    def put(y, x, s, attr=0):
        h, w = stdscr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                stdscr.addnstr(y, x, s, max(0, w - x - 1), attr)
            except curses.error:
                pass

    def draw():
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        put(0, 1, "satoru %s — Windows games on Apple Silicon, open builds" % version(),
            curses.A_BOLD)
        put(1, 1, "↑/↓ or j/k move · Enter actions · Esc back · q quit")
        absent = unchecked_packs()
        if absent and games:
            put(2, 1, "not listed: %s — submodules not checked out (git submodule update --init)"
                % ", ".join(absent), curses.A_DIM)
        y = 3
        if not games:
            for line in (NO_GAMES_HINT % GAMES_DIR).splitlines():
                put(y, 3, line)
                y += 1
        for i, g in enumerate(games):
            attr = curses.A_REVERSE if (i == sel and mode == "games") else 0
            put(y, 1, "%s %-24s" % (">" if i == sel else " ", g.name), attr)
            put(y, 28, STATUS_LABEL.get(g.status, g.status), status_attr.get(g.status, 0))
            y += 1
        y += 1
        if games:
            g = games[sel]
            put(y, 1, "Actions — %s" % g.name, curses.A_BOLD)
            y += 1
            for i, (key, label, _) in enumerate(g.actions()):
                state, detail = g.action_state(key)
                cur = (mode == "actions" and i == action_sel)
                attr = curses.A_REVERSE if cur else 0
                if state == "ok":
                    put(y, 3, "%-32s" % label, attr)
                    put(y, 36, detail, dim)
                elif state == "soon":
                    put(y, 3, "%-32s · SOON" % label, attr | dim)
                else:
                    put(y, 3, "%-32s · missing: %s" % (label, detail), attr | dim)
                y += 1
            y += 1
            if g.manual_url:
                put(y, 3, "%-32s %s" % ("Instructions", g.manual_url), dim)
                y += 1
            if g.foreign_note:
                # The one place the contract lets a pack say it writes outside
                # its own home. It reached only the reader who ran --plain, which
                # is the wrong half of the audience for a warning.
                put(y, 3, "%-32s %s" % ("Note", g.foreign_note), curses.A_BOLD)
                y += 1
            if g.manual_url or g.foreign_note:
                y += 1
            if g.notes:
                put(y, 1, "Notes", curses.A_BOLD)
                y += 1
                for line in g.notes.splitlines():
                    if y >= h - 2:
                        break
                    put(y, 3, line, dim)
                    y += 1
        if message:
            put(h - 1, 1, message, curses.A_BOLD)
        stdscr.refresh()

    while True:
        draw()
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            return
        if ch in (curses.KEY_RESIZE,):
            continue
        if mode == "games":
            if ch in (curses.KEY_DOWN, ord("j")) and games:
                sel = (sel + 1) % len(games)
            elif ch in (curses.KEY_UP, ord("k")) and games:
                sel = (sel - 1) % len(games)
            elif ch in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT, ord("l")) and games:
                mode = "actions"
                action_sel = 0
                g = games[sel]
                if g.manifest.get("modes") and g.contract >= 1:
                    # The last choice is where the cursor starts, as in the dialog.
                    keys = [k for k, _, _ in g.actions()]
                    last = "mode:" + g.last_mode()
                    action_sel = keys.index(last) if last in keys else 0
                message = ""
        else:
            if ch in (curses.KEY_DOWN, ord("j")):
                action_sel = (action_sel + 1) % len(games[sel].actions())
            elif ch in (curses.KEY_UP, ord("k")):
                action_sel = (action_sel - 1) % len(games[sel].actions())
            elif ch in (27, curses.KEY_LEFT, ord("h")):
                mode = "games"
                message = ""
            elif ch in (curses.KEY_ENTER, 10, 13):
                g = games[sel]
                key = g.actions()[action_sel][0]
                state, _ = g.action_state(key)
                if state != "ok":
                    _, message = run_action(g, key)
                    continue
                curses.endwin()
                try:
                    rc, message = run_action(g, key)
                    sys.stdout.write("\n[%s] press Enter to return to satoru " % message)
                    sys.stdout.flush()
                    try:
                        sys.stdin.readline()
                    except KeyboardInterrupt:
                        pass
                finally:
                    stdscr.refresh()
                    curses.curs_set(0)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install and launch satoru game packs.")
    parser.add_argument("--games-dir", default=GAMES_DIR, help="game manifest catalogue")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--version", action="store_true")
    action.add_argument("--check", action="store_true")
    action.add_argument("--list", action="store_true")
    action.add_argument("--install", metavar="GAME")
    action.add_argument("--launch", metavar="GAME")
    action.add_argument("--launch-plain", metavar="GAME")
    options = parser.parse_args(argv)
    games = load_games(options.games_dir)
    if options.version:
        sys.stdout.write("satoru %s\n" % version())
        for g in games:
            sys.stdout.write("  %-12s %s\n" % (g.id, STATUS_LABEL.get(g.status, g.status)))
        return 0
    if options.check:
        return 0 if check(options.games_dir, out=sys.stdout) else 1
    if options.list:
        describe(games, out=sys.stdout, games_dir=options.games_dir)
        return 0
    for game_id, key in ((options.install, "setup"), (options.launch, "launch"),
                         (options.launch_plain, "launch_plain")):
        if game_id is not None:
            game = next((g for g in games if g.id == game_id), None)
            if game is None:
                sys.stderr.write("Unknown game %r. Available: %s\n" % (
                    game_id, ", ".join(g.id for g in games)))
                return 2
            if game.validate():
                sys.stderr.write("Invalid manifest: %s\n" % "; ".join(game.validate()))
                return 2
            state, detail = game.action_state(key, current_paths())
            if state != "ok":
                sys.stderr.write("%s: %s\n" % (game_id, detail or "action unavailable"))
                return 1
            code, message = run_action(game, key)
            sys.stdout.write(message + "\n")
            return code
    import curses
    curses.wrapper(tui, games)
    return 0


if __name__ == "__main__":
    sys.exit(main())
