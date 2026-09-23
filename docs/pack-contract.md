# The pack contract, version 1

This is the document you write a pack against. Everything in it works today.
Anything that does not work yet is marked `[planned]` and is not described in the
present tense, because a contract that promises what it does not do is worse than
one with a visible gap.

A pack is a tarball with a `game.toml` in it. satoru does everything general —
choosing and creating the home, downloading and verifying the archive, keeping
state, writing the launch shim, streaming your output to the screen — and your
pack does only what is specific to the game. A new game is a manifest and four
commands.

## The manifest language

`game.toml` is read by one reader on every machine: satoru's own, never
`tomllib`. That is a decision rather than a limitation. `tomllib` arrived in
Python 3.11 and the Python that ships with macOS is 3.9, so choosing a reader per
interpreter meant a manifest could mean one thing to the person who wrote it and
another to the person who ran it. An audit found 58 such differences, thirteen of
which could break a launch. One reader cannot disagree with itself.

What the reader accepts is a subset of TOML v1.0.0, and this is all of it:

- `[section]` headers, **one level deep**, no dots;
- keys of letters, digits, `_` and `-`, with no dots and no quotes;
- basic strings `"…"` with the escapes `\" \\ \b \f \n \r \t \uXXXX \UXXXXXXXX`;
- multi-line basic strings `"""…"""`, with the same escapes and with a trailing
  backslash joining a line to the next;
- literal strings `'…'` and `'''…'''`, in which nothing is an escape;
- `true` and `false`;
- decimal integers, `_` allowed as a separator (`133_000_000`);
- arrays of those, **all of one type**, over as many lines as you like;
- `#` comments, including after a value and after a section header.

Everything else is **refused, with the line named** — even where it is legal
TOML: floats, dates and times, hexadecimal and octal integers, inline tables
`{…}`, dotted keys and headers, arrays of tables `[[…]]`, nested arrays, arrays
of mixed types. A subset that guesses is a trap. A subset that says "not
supported here" is something you can write against.

Also refused, because TOML forbids them: a duplicate key, a section declared
twice, junk after a value, an unterminated string or array, an unknown escape, a
leading zero in a number, an unescaped control character.

A manifest that cannot be read is one game's problem. It stays in the list
carrying its own error; the catalogue does not fall over.

## The manifest

One file, two audiences. `[game]`, `[source]`, `[requires]` and `[install]` are
copied into satoru's own release and read **before** anything is downloaded.
`[commands]` and `[paths]` matter only once the pack is on disk.

```toml
contract = 1                        # required; without it the file is read as the older shape

[game]
id      = "aoe4"                    # also the directory name, [a-z0-9-]
name    = "Age of Empires IV"       # the bundle's name and the heading in the list
status  = "rc"                      # rc | playable | wip
summary = "one line, shown under the heading"
notes   = """several lines, shown under the game"""

[source]
kind    = "release"                 # release | none (installed by hand)
url     = "https://.../pack-v0.1.tar.gz"     # required when kind = "release"
sha256  = "0540919b..."                      # required when kind = "release"
size    = 133000000                 # informational, for whoever reads the manifest
version = "v0.1"                    # what an update check compares against

[requires]                          # satoru checks these, generically, BEFORE downloading
arch    = "arm64"
macos   = ">=26"
rosetta = true
disk_gb = 4                         # your own footprint, on the volume the home lands on
tools   = []                        # magicka: ["dotnet", "ffmpeg", "gh"]

[install]
home_authoritative = true           # informational: everything lives under SATORU_GAME_HOME
foreign_note = ""                   # ow2: "modifies your CrossOver bottle (cxbottle.conf is backed up)"
manual_url   = ""                   # REQUIRED when kind = "none": where to go to install by hand

[commands]                          # paths relative to the root of the unpacked pack
preflight    = "bash setup.sh --preflight"
install      = "bash setup.sh"
launch       = "aoe4.sh"
launch_plain = "aoe4.sh --plain"    # a command, not a flag: it may be a different script
uninstall    = "bash uninstall.sh"  # [planned]: satoru does not call this yet

[paths]                             # paths relative to SATORU_GAME_HOME
profile  = "README-local.txt"       # what "Show profile" opens
logs     = "logs"                   # what "Open logs" opens
icon_exe = "drive_c/Program Files/Game/Game.exe"   # optional, see below
```

`foreign_note` is the one place the contract lets a pack admit that it writes
somewhere other than its own home. Use it. It is shown before anyone commits to
installing.

`icon_exe` is optional, and a pack that omits it behaves exactly as one always
has. When present, it names the Windows `.exe` — relative to
`SATORU_GAME_HOME`, so typically somewhere under a `drive_c` your own
`install` command creates — whose own icon satoru gives to the `.app`. It is
read once `install` has finished (the exe has to exist by then), by parsing
the exe's `RT_GROUP_ICON` / `RT_ICON` resources directly — no dependency
beyond the Python standard library and macOS's own `sips`, and nothing here
ever holds up an install: a missing exe, a `sips` this Mac does not have, or
an exe with no icon resource all fall back the same way, silently, to a mark
satoru drew for itself. `icon_exe` must resolve inside the game's own home;
`..` or an absolute path is refused the same way a bad game id already is.

`disk_gb` is your own footprint and nothing else. Space for the game's own files
belongs to whoever brings them: a pack that installs through a store leaves that
to the store, which already refuses when the disk is full and says so. Only a
pack that fetches the game itself owns that check, and it belongs in `preflight`
with exit 10 — there is no manifest key for it, because a key that is empty for
every pack but one makes the contract worse rather than better. If you write that
check, its message must name the numbers: how much is free, how much is needed,
and on which volume. "Not enough space" without them is a dead end rather than an
instruction.

If `kind = "none"` and `manual_url` is empty, your game appears as five greyed-out
actions and no explanation, which reads as abandoned. That is why `manual_url` is
required in that case.

Things the contract used to offer and no longer does: `kind = "git"` (nothing
clones, and a checkout has no sha256 to be verified against), `[source] check`
(the repository is taken from `url` itself), `[install] home` (satoru chooses the
home), and `[commands] update` — see the lifecycle below.

## The environment your commands get

```
SATORU_GAME_HOME     where to install, and where to keep everything
SATORU_GAME_ID       the game's id
SATORU_LIBRARY       the root of the game-files library
SATORU_CACHE         ~/Library/Caches/satoru/<id>       may be erased at any time
SATORU_LOGS          ~/Library/Logs/satoru/<id>         created before your command runs
SATORU_CONTRACT      1
```

The same environment at install and at launch — the shim exports all of it.
Otherwise `"$SATORU_LOGS/game.log"` works for the author while installing and is
empty for the user while playing.

**Working directory**: `preflight`, `install` and `uninstall` run in the root of
the unpacked pack. `launch` and `launch_plain` run in `SATORU_GAME_HOME`, because
the cache is documented as erasable and a launch anchored to the unpacked pack
would break the first time it was cleaned.

There is no interactivity. A command may not ask the person anything.

## Launch modes (contract 1, optional)

A pack whose game starts in more than one way — offline, joining a server,
hosting one — declares those ways instead of asking. satoru shows them, asks for
the values each one needs, remembers both, and hands the answer to `launch` and
`launch_plain` in the environment. A pack without `[modes]` gets none of this,
and its shim is byte for byte what it was.

```toml
[modes]                              # id = label; the order here is the order shown
offline = "Offline against bots"
client  = "Join a server"
host    = "Host a server and play"

[setting_nick]                       # one section per value: setting_<name>
label   = "Nick"
modes   = ["client", "host"]         # which modes need it; required
kind    = "text"                     # text (default) | ipv4
pattern = "[A-Za-z0-9_-]{1,16}"      # text only: POSIX ERE, whole value, no backslashes
error   = "Latin letters, digits, _ or -, up to 16"   # shown when a value is refused

[setting_server]
label = "Server IPv4 address"
kind  = "ipv4"
modes = ["client"]
```

Mode ids are lower-case letters, digits, `_` and `-`; setting names are
lower-case letters, digits and `_`. Labels are one line and unique. `--check`
refuses a setting that names an undeclared mode, an unknown `kind`, a `pattern`
with a backslash or a `[:class:]` name (Python and `grep -E` read those
differently, and both check it), a `pattern` on an `ipv4`, and `[setting_*]` without `[modes]`. A pattern is
matched byte-wise when the game is started from Finder, so keep it to ASCII
classes.

What your commands get, in addition to the environment above:

```
SATORU_MODE              the chosen mode id — set whenever the pack declares [modes]
SATORU_SETTING_<NAME>    each value the chosen mode needs, <NAME> upper-cased
```

Only the chosen mode's values are exported. They have already passed the
declared checks; check them again anyway, because your launcher can be run
without satoru, and refuse the way the exit codes say.

How the choice is made:

- **The bundle, or `home/launch` run by hand**: a dialog (`osascript`, nothing
  else) lists the modes with the last choice selected and a `Change settings…`
  item. A value that is missing, or that the current manifest refuses, is asked
  for; a refused one is explained and asked again. Cancel is exit 20 and starts
  nothing. When no dialog can be shown, exit 10 names `SATORU_MODE`.
- **satoru's TUI**: one `Launch: <label>` entry per mode, the cursor on the last
  one; values are asked in the terminal. It then runs the shim with `SATORU_MODE`
  set.
- **Anyone who sets `SATORU_MODE`** (and `SATORU_SETTING_*`): no dialog; saved
  values fill the gaps; anything still missing or refused is exit 10.

The choice is kept in `SATORU_GAME_HOME/satoru-launch.conf` (`mode = …`,
`setting_<name> = …`, one per line). It belongs to satoru; do not write it. It
goes when the home goes and survives a reinstall.

A pack with `[modes]` is refused as `unknown section [modes]` by a satoru older
than this section, so the umbrella that understands it has to ship before a
manifest that uses it.

## Exit codes

```
0    done
10   a precondition is not met — your stderr is shown to the person as you wrote it
11   there is nothing to do (already installed / nothing to remove) — this is SUCCESS
12   the pack's own files are not the ones it expects — fetch it again
20   the person said no
1    an unexpected break
```

`11` from `preflight` means the home is already in the state you want: satoru
skips `install` and still writes the shim and records the state. `11` from
`install` means the same. Any code outside this list is read as "this pack does
not speak contract 1", and satoru says so, naming the code and your last line of
output — usually it means a release older than the manifest pointing at it.

## The lifecycle

```
Install
  [requires] → download + verify → unpack into the cache → strip quarantine
            → preflight (WRITES NOTHING INTO THE HOME) → install → shim
            → record installed.toml
            → bundle [planned]

Launch
  the shim, in the game's home
            ├─ update check older than 6 h? detached, in the background, writes to the cache
            └─ exec the game IMMEDIATELY

Update
  Install, run again. There is no separate command: `install` has to be
  idempotent, and `preflight` is free to see a finished home and answer 11.

Uninstall [planned]
  uninstall --dry-run → show the list and the sizes → confirm
                     → uninstall → remove the home, the bundle, the logs, the
                       cache and the line in installed.toml
```

The shim is written before the state, which is not decoration: `installed.toml`
is a claim, and the shim is what makes it true.

**Rule:** `preflight` writes nothing into `SATORU_GAME_HOME`, and touches nothing
outside the unpacked pack. Splitting your checks from your work is the main
change a pack needs. AoE4's `setup.sh` used to die on a probe after copying
420 MB, leaving half a home behind — the refusal has to cost nothing, so that
what a refusal leaves behind is nothing.

The unpacked pack itself is yours. It sits in `SATORU_CACHE`, which this contract
declares erasable, and `unpack` replaces it wholesale on every install, so
nothing you do there survives to confuse the next run. Use that: AoE4's preflight
clears the quarantine flag from its own `Helpers/x87sidecar` before probing it,
because an ad-hoc-signed binary that still carries the flag is killed by
Gatekeeper rather than answering. satoru already strips quarantine from the whole
pack before calling you, so that line does nothing under the umbrella — it is
there for the person who downloaded the tarball in a browser and ran `setup.sh`
by hand, who has nobody to do it for them.

**Rule:** never update a pack while its game is running, and never between the
moment someone presses launch and the moment the game is up.

## Releasing

A manifest describes an artefact, not an intention:

1. First cut the release that implements every command in `[commands]`.
2. Then point `url`, `sha256`, `size` and `version` at that release.

The other order has exactly one symptom: the person downloads a hundred megabytes
and is refused by a command the release does not have. satoru reports that
honestly and still cannot install the game. It cost us 139 MB and an
`unknown flag --preflight` to learn.

Build the tarball with `COPYFILE_DISABLE=1`. Without it macOS `tar` writes an
AppleDouble `._name` beside every entry whose file has extended attributes, and
those extra top-level entries hide the pack's root directory.

Two habits are worth copying rather than reinventing; the AoE IV pack has both,
in `tools/`.

The first keeps the release version in one place. Before it existed, that version
lived in four, nothing compared them, and a bootstrap script that shipped inside
the tarball would have carried a v0.2 release that downloaded v0.1. A check that
the manifest is the only place the version appears costs a few lines and removes
a whole shape of mistake.

The second makes the rule at the top of this section mechanical instead of
remembered: before building the tarball, run the staged pack's own `preflight`,
taken from the manifest, and refuse to build when it answers with a code that
says the pack does not understand its own commands. One detail matters when you
port it — the gate may run **only** `preflight`, because that is the one command
this contract promises writes nothing. A gate that runs `install` is not a gate;
it is an installation on the build machine.

## State

`~/Library/Application Support/satoru/installed.toml` belongs to satoru, not to
the pack: `id, version, source_sha256, installed_at, home, bundle`. The recorded
`home` is where the game is; satoru does not recompute it from the display name,
so renaming a game in its manifest does not lose the install. If the home is
gone, the game counts as not installed. A pack does not have to keep state of its
own.

## Update checks

The launch shim checks for a newer release in the background, throttled to once
every six hours — GitHub allows sixty API calls an hour per address, and people
launch games often. The answer is read back and shown in the list as
`Update available v0.2`; installing it is a button, never automatic: a pack is
133 MB and more.

A person can turn this off with `check_updates = false` in `config.toml`. Then
the check is not written into the shim at all, rather than being ignored more
quietly.
