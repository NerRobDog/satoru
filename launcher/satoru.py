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
import datetime
import hashlib
import os
import platform
import re
import shlex
import shutil
import tarfile
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


def _member_stays_inside(member, dest):
    """Every path an archive gives us is data from the internet.

    A member called ../../etc/passwd, an absolute name, or a symlink pointing out
    of the tree are all attacks rather than files, and tarfile will happily follow
    them if nobody checks.
    """
    root = os.path.realpath(dest)
    for name in (member.name, member.linkname or ""):
        if not name:
            continue
        if os.path.isabs(name):
            return False
        full = os.path.realpath(os.path.join(root, name))
        if full != root and not full.startswith(root + os.sep):
            return False
    return True


def unpack(archive, dest):
    """Replace `dest` with the archive's contents, and return its root directory.

    Replace rather than merge: an older pack's leftovers inside a new one is a
    debugging session nobody should have to have.
    """
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    try:
        with contextlib.closing(tarfile.open(archive, "r:*")) as tf:
            members = tf.getmembers()
            for member in members:
                if not _member_stays_inside(member, dest):
                    raise PackError(
                        "the archive contains %r, which points outside the "
                        "directory it is being unpacked into" % (member.name,))
            if sys.version_info >= (3, 12):
                tf.extractall(dest, filter="tar")
            else:
                tf.extractall(dest)
    except PackError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except tarfile.TarError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise PackError("could not unpack %s: %s" % (archive, exc))

    entries = os.listdir(dest)
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


def shim_text(paths, manifest):
    """The script, as text. Returns "" for a pack that declares no way to launch."""
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
    add('    if [ -z "${TERM:-}" ]; then')
    add('      osascript -e "display alert \\"$msg\\" message \\"$fix\\"" >/dev/null 2>&1 || true')
    add("    fi")
    add("    exit 10")
    add("    ;;")
    add("esac")
    add("")
    add("# Warmed shader pipelines live in the home, where the system is not allowed to")
    add("# reclaim them. Overwatch alone keeps 434 MB of them.")
    add('export DXMT_SHADER_CACHE_PATH=%s' % shlex.quote(paths.shader_cache(name) + "/"))
    add('export SATORU_GAME_HOME="$HERE"')
    add("export SATORU_GAME_ID=%s" % shlex.quote(game_id))
    add("")
    if api:
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
    add('cd "$HERE" || exit 1')
    add("exec sh -c %s satoru-launch \"$@\"" % shlex.quote(_launch_command_in_home(launch) + ' "$@"'))
    add("")
    return "\n".join(out)


def write_shim(paths, manifest, write=True):
    """Write the shim into the game's home. Returns its path, or None if the pack
    declares no launch command (a work-in-progress pack, honestly)."""
    text = shim_text(paths, manifest)
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


def install_game(manifest, paths, probe=None, runner=None, fetch=None, unpack=None,
                 unquarantine=None, on_output=None):
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

    requirements = check_requirements(manifest["requires"], probe=probe,
                                      root=paths.library)
    if not all_met(requirements):
        unmet = [r for r in requirements if not r["ok"]]
        return _install_result(False, "requirements", requirements=requirements,
                       message="; ".join(r["label"] + ": " + r["detail"] for r in unmet))

    source = manifest.get("source")
    if not source or not source.get("url"):
        return _install_result(False, "source", requirements=requirements,
                       message="this pack declares no release to download - "
                               "see its own instructions for installing it by hand")

    cache = paths.game_cache(game_id)
    try:
        archive = fetch(source, cache, None)
        unpacked = unpack(archive, os.path.join(cache, "unpacked"))
    except PackError as exc:
        return _install_result(False, "fetch", code=12, requirements=requirements, message=str(exc))

    unquarantine(unpacked)

    env = dict(os.environ)
    env.update({
        "SATORU_GAME_HOME": paths.home(name),
        "SATORU_GAME_ID": game_id,
        "SATORU_LIBRARY": paths.library,
        "SATORU_CACHE": cache,
        "SATORU_LOGS": paths.game_logs(game_id),
        "SATORU_CONTRACT": str(CONTRACT),
    })

    preflight = commands.get("preflight")
    if preflight:
        code = runner.run(preflight, cwd=unpacked, env=env, on_output=on_output)
        if code != 0:
            # Nothing has been written yet, and nothing should be: leaving a
            # half-built home is what this whole ordering exists to prevent.
            return _install_result(False, "preflight", code=code, requirements=requirements,
                           message="the pack refused to install here")

    install = commands.get("install")
    if not install:
        return _install_result(False, "install", requirements=requirements,
                       message="this pack declares no install command")

    home = paths.home(name)
    if not os.path.isdir(home):
        os.makedirs(home)
    code = runner.run(install, cwd=unpacked, env=env, on_output=on_output)
    if code != 0:
        return _install_result(False, "install", code=code, requirements=requirements,
                       message="installation failed - see the log")

    entry = record_install(paths, manifest, (source or {}).get("sha256"))
    write_shim(paths, manifest)
    return _install_result(True, "done", requirements=requirements, entry=entry)


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
