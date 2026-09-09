#!/usr/bin/env python3
"""satoru — one entry point for the game packs in games/*.

Python 3 standard library only (curses, subprocess). Reads games/*/game.toml,
shows a list, and runs the pack's own scripts: setup, launch, launch --plain,
show profile, open logs. Actions a game does not support are shown as SOON and
do nothing; actions whose script is not on disk say "missing".

    python3 launcher/satoru.py            # TUI
    python3 launcher/satoru.py --list     # plain listing (no curses)
    python3 launcher/satoru.py --check    # validate every game.toml, exit 1 on error
"""
import os
import shlex
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMES_DIR = os.path.join(ROOT, "games")

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
            end = val.find('"', 1)
            while end != -1 and val[end - 1] == "\\":
                end = val.find('"', end + 1)
            if end == -1:
                raise ValueError("line %d: unterminated string" % i)
            cur[key] = val[1:end].replace('\\"', '"').replace("\\n", "\n")
            continue
        val = val.split("#", 1)[0].strip()
        if val == "true":
            cur[key] = True
        elif val == "false":
            cur[key] = False
        else:
            try:
                cur[key] = int(val)
            except ValueError:
                raise ValueError("line %d: unsupported value %r" % (i, val))
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
            out.write("games/%s: no game.toml (skipped)\n" % entry)
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
        put(0, 1, "satoru — Windows games on Apple Silicon, open builds", curses.A_BOLD)
        put(1, 1, "↑/↓ or j/k move · Enter actions · Esc back · q quit")
        y = 3
        if not games:
            put(y, 3, "no games/*/game.toml found under %s" % GAMES_DIR)
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
    if "--check" in argv:
        return 0 if check() else 1
    games = load_games()
    if "--list" in argv:
        describe(games)
        return 0
    if argv and argv[0] not in ("--list", "--check"):
        sys.stderr.write(__doc__)
        return 2
    import curses
    curses.wrapper(tui, games)
    return 0


if __name__ == "__main__":
    sys.exit(main())
