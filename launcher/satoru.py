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
import datetime
import os
import platform
import re
import shlex
import shutil
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
# TOML: tomllib on 3.11+, otherwise a minimal reader for the game.toml subset
# ([section], key = "string" | 123 | true | false | """multi-line""", # comments)

def _unescape(text):
    """TOML basic-string escapes, in one pass.

    A chain of .replace() calls cannot do this: unescaping \\" before \\\\ turns
    a literal backslash-then-quote into a quote, and doing it the other way
    round breaks the quote. One left-to-right pass is the only correct order.
    """
    out = []
    i = 0
    simple = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append(simple.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_scalar(val, lineno):
    """A bare value: string, bool or integer. Everything else is an error, loudly."""
    val = val.strip()
    if val.startswith('"') and val.endswith('"') and len(val) >= 2:
        return _unescape(val[1:-1])
    val = val.split("#", 1)[0].strip()
    if val == "true":
        return True
    if val == "false":
        return False
    try:
        return int(val)
    except ValueError:
        raise ValueError("line %d: unsupported value %r" % (lineno, val))


def _parse_array(val, lineno):
    """A single-line array: ["a", "b"]. Enough for [requires] tools, and no more.

    Scanning for the closing bracket has to ignore one inside a string, or a path
    with a bracket in it would end the array early.
    """
    depth, end, in_str = 0, -1, False
    for pos, ch in enumerate(val):
        if ch == '"' and (pos == 0 or val[pos - 1] != "\\"):
            in_str = not in_str
        elif not in_str and ch == "[":
            depth += 1
        elif not in_str and ch == "]":
            depth -= 1
            if depth == 0:
                end = pos
                break
    if end == -1:
        raise ValueError(
            "line %d: unterminated array (multi-line arrays are not supported, "
            "keep it on one line)" % lineno)
    body = val[1:end].strip()
    if not body:
        return []
    items, cur_item, in_str = [], "", False
    for pos, ch in enumerate(body):
        if ch == '"' and (pos == 0 or body[pos - 1] != "\\"):
            in_str = not in_str
            cur_item += ch
        elif ch == "," and not in_str:
            items.append(cur_item)
            cur_item = ""
        else:
            cur_item += ch
    items.append(cur_item)
    return [_parse_scalar(x, lineno) for x in items if x.strip()]


def _parse_minimal_toml(text):
    data = {}
    cur = data
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            cur = data.setdefault(name, {})
            continue
        if "=" not in line:
            raise ValueError("line %d: expected key = value: %r" % (i, raw))
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if val.startswith('"""'):
            body = val[3:]
            if body.endswith('"""') and len(body) >= 3:
                cur[key] = body[:-3]
                continue
            parts = [body] if body else []
            while i < len(lines):
                l2 = lines[i]
                i += 1
                if l2.rstrip().endswith('"""'):
                    parts.append(l2.rstrip()[:-3])
                    break
                parts.append(l2)
            else:
                raise ValueError("unterminated multi-line string for %r" % key)
            cur[key] = "\n".join(parts).lstrip("\n")
            continue
        if val.startswith('"'):
            # Walk it rather than searching: a closing quote is one that is not
            # itself escaped, and counting backslashes backwards gets that wrong
            # for a string ending in a literal backslash.
            end, j = -1, 1
            while j < len(val):
                if val[j] == "\\":
                    j += 2
                    continue
                if val[j] == '"':
                    end = j
                    break
                j += 1
            if end == -1:
                raise ValueError("line %d: unterminated string" % i)
            cur[key] = _unescape(val[1:end])
            continue
        if val.startswith("["):
            cur[key] = _parse_array(val, i)
            continue
        cur[key] = _parse_scalar(val, i)
    return data


def load_toml(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        import tomllib  # 3.11+
    except ImportError:
        tomllib = None
    if tomllib is not None:
        return tomllib.loads(raw.decode("utf-8"))
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
_PATH_KEYS = ("profile", "logs")

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
            if name not in _V1_SECTIONS and name != "contract":
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
# [requires]: what the machine has to be before a pack is worth downloading

class SystemProbe(object):
    """Everything the checks want to know about this Mac, in one injectable place.

    Tests hand in a fake, which is how an Intel Mac with no Rosetta and a full
    disk get tested from an M1 with 200 GB free.
    """

    def arch(self):
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
        st = os.statvfs(path or os.path.expanduser("~"))
        return st.f_bavail * st.f_frsize / float(1024 ** 3)

    def which(self, tool):
        return shutil.which(tool)


# Where a missing tool comes from. Deliberately a hint and not a command we run:
# installing packages on someone's behalf is a bigger promise than this makes.
TOOL_HINTS = {
    "ffmpeg": "brew install ffmpeg",
    "gh": "brew install gh",
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
    def __init__(self, path, data):
        g = data.get("game", {})
        self.path = path
        self.dir = os.path.dirname(path)
        self.name = str(g.get("name", os.path.basename(self.dir)))
        self.id = str(g.get("id", os.path.basename(self.dir)))
        self.status = str(g.get("status", "wip"))
        self.home = str(g.get("home", ""))
        self.notes = str(g.get("notes", "")).strip()
        self.cmds = {k: str(g.get(k, "")).strip() for k, _, _ in ACTIONS}

    def validate(self):
        errs = []
        if "game" not in load_toml(self.path):
            errs.append("no [game] table")
        if self.status not in STATUSES:
            errs.append("status %r not in %s" % (self.status, "/".join(STATUSES)))
        if not self.name:
            errs.append("empty name")
        if self.id != os.path.basename(self.dir):
            errs.append("id %r != directory %r" % (self.id, os.path.basename(self.dir)))
        if self.status != "wip" and not self.cmds["launch"]:
            errs.append("status %s but no launch command" % self.status)
        return errs

    def action_state(self, key):
        """(state, detail): state is 'ok' | 'soon' | 'missing'."""
        cmd = self.cmds.get(key, "")
        if self.status == "wip" or not cmd:
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


def load_games(games_dir=GAMES_DIR):
    games = []
    if not os.path.isdir(games_dir):
        return games
    for entry in sorted(os.listdir(games_dir)):
        toml_path = os.path.join(games_dir, entry, "game.toml")
        if os.path.isfile(toml_path):
            games.append(Game(toml_path, load_toml(toml_path)))
    # rc first, then playable, then wip; stable within a group
    order = {s: i for i, s in enumerate(STATUSES)}
    games.sort(key=lambda g: order.get(g.status, 99))
    return games


# ----------------------------------------------------------------------------
# running things (outside curses)

def run_action(game, key):
    """Returns (returncode, message). Called with the terminal in normal mode."""
    state, detail = game.action_state(key)
    if state == "soon":
        return 0, "%s: SOON — not available for %s yet." % (key, game.name)
    if state == "missing":
        return 1, "%s: missing %s (submodule not checked out?)" % (key, detail)
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

def describe(games, out=sys.stdout):
    if not games:
        out.write(NO_GAMES_HINT % GAMES_DIR)
        return
    absent = unchecked_packs()
    if absent:
        out.write("Not listed: %s — submodule%s not checked out.\n"
                  "Run `git submodule update --init --recursive`, or take the release\n"
                  "tarball, which needs no clone: https://github.com/NerRobDog/satoru/releases\n\n"
                  % (", ".join(absent), "" if len(absent) == 1 else "s"))
    for g in games:
        out.write("%s [%s] — %s\n" % (g.name, g.id, STATUS_LABEL.get(g.status, g.status)))
        for key, label, _ in ACTIONS:
            state, detail = g.action_state(key)
            if state == "ok":
                out.write("    %-32s %s\n" % (label, detail))
            elif state == "soon":
                out.write("    %-32s · SOON\n" % label)
            else:
                out.write("    %-32s · missing: %s\n" % (label, detail))
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
            for i, (key, label, _) in enumerate(ACTIONS):
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
                message = ""
        else:
            if ch in (curses.KEY_DOWN, ord("j")):
                action_sel = (action_sel + 1) % len(ACTIONS)
            elif ch in (curses.KEY_UP, ord("k")):
                action_sel = (action_sel - 1) % len(ACTIONS)
            elif ch in (27, curses.KEY_LEFT, ord("h")):
                mode = "games"
                message = ""
            elif ch in (curses.KEY_ENTER, 10, 13):
                g = games[sel]
                key = ACTIONS[action_sel][0]
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
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        sys.stdout.write("satoru %s\n" % version())
        for g in load_games():
            sys.stdout.write("  %-12s %s\n" % (g.id, STATUS_LABEL.get(g.status, g.status)))
        return 0
    if "--check" in argv:
        return 0 if check() else 1
    games = load_games()
    if "--list" in argv:
        describe(games)
        return 0
    if argv and argv[0] not in ("--list", "--check", "--version"):
        sys.stderr.write(__doc__)
        return 2
    import curses
    curses.wrapper(tui, games)
    return 0


if __name__ == "__main__":
    sys.exit(main())
