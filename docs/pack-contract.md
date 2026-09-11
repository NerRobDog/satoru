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
disk_gb = 4                         # measured on the volume the home will land on
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
profile = "README-local.txt"        # what "Show profile" opens
logs    = "logs"                    # what "Open logs" opens
```

`foreign_note` is the one place the contract lets a pack admit that it writes
somewhere other than its own home. Use it. It is shown before anyone commits to
installing.

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
            → preflight (WRITES NOTHING) → install → shim → record installed.toml
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

**Rule:** `preflight` writes nothing. Splitting your checks from your work is the
main change a pack needs. AoE4's `setup.sh` used to die on a probe after copying
420 MB, leaving half a home behind.

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
