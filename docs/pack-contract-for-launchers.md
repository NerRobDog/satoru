# Using satoru packs from another launcher (contract 1)

This is the launcher-side view of [`pack-contract.md`](pack-contract.md) (the pack-author
side), written for people who build a launcher and want to offer a satoru pack in it.
Sources: `docs/pack-contract.md`, `docs/launch-modes-design.md` and `launcher/satoru.py`
(`main()`, `shim_text`, `run_action`). Markers: `[gap]` is something the contract or satoru
does not do yet; `[planned]` is decided but not built; `(T)` after a promise means a test in
`tests/` checks it. Everything else is what the code does today.

## 1. The idea in one paragraph

A pack is a tarball with a `game.toml`. It is **self-contained**: it creates and owns its own
Wine prefix and its own Steam or Battle.net client, ships its Wine engine, and needs no
help from the launcher but a way to run four commands. (Where the player's login files are
kept is the host's business, see §4a.) A launcher that wants to offer the game therefore does
not need store clients, bottle management or a graphics stack. It needs to (1) read the
manifest, (2) run `install` once, (3) run `launch` on demand, and (4) show what the pack
says. This thin shape is the contract for third-party launchers.

## 2. Two ways to integrate

**Way 1 — call satoru's headless CLI.** `python3 launcher/satoru.py` (standard-library
Python 3; macOS ships 3.9, which is why the manifest has its own reader, see §3):

```
satoru.py --list                 human-readable catalogue          exit 0
satoru.py --check                validate every game.toml          exit 0, or 1 on any error
satoru.py --install GAME         preflight + install               exit = the pack's code (§6)
satoru.py --launch GAME          run the launch shim               exit = the pack's code
satoru.py --launch-plain GAME    run launch_plain                  exit = the pack's code
```

Unknown game: exit 2. Invalid manifest: exit 2. Action unavailable (for example a pack
with `kind = "none"` asked to install): exit 1 with the reason on stderr.
`[gap]` there is no machine-readable `--list` (no JSON), no `--status`, no `--stop`.

**Way 2 — implement the host side yourself.** The sequence is in §5. It is about 150 lines
of work: download, SHA-256, untar, strip quarantine, run two commands with a given
environment, write a shim. Choose this when you cannot depend on Python or want your own
state store.

## 3. Reading `game.toml`

The manifest is a **strict subset of TOML v1.0.0**, read by satoru's own reader and
never by `tomllib` (so it means the same on every Mac). Do not use a general TOML library
as the only reader: a file it accepts may be refused by ours, and a file that is legal TOML
but outside the subset (floats, dates, inline tables, dotted keys, arrays of tables,
nested or mixed arrays) is an error with the line named. One-level `[section]` headers,
plain keys, basic and literal strings (also multi-line), booleans, decimal integers
(`_` allowed) and same-type arrays are all there is. Full list: `pack-contract.md`,
"The manifest language".

What a launcher reads before downloading anything:

| Section | Keys | Use |
|---|---|---|
| top | `contract = 1` | required; a manifest without it is the older shape |
| `[game]` | `id`, `name`, `status` (`rc`/`playable`/`wip`), `summary`, `notes` | list entry and details |
| `[source]` | `kind` (`release`/`none`), `url`, `sha256`, `size`, `version` | what to download and verify; `version` is what an update check compares against |
| `[requires]` | `arch`, `macos`, `rosetta`, `disk_gb`, `tools` | refuse **before** downloading; `disk_gb` is the pack's own footprint, not the game's |
| `[install]` | `foreign_note`, `manual_url`, `home_authoritative` | show `foreign_note` before the person commits; `manual_url` is where `kind = "none"` packs send people |

Show `status` honestly: `wip` means "not for users yet". A launcher that lists a `wip` pack
as a normal game is misrepresenting it.

## 4. The environment a pack's commands get

```
SATORU_GAME_HOME     where to install, and where everything the pack keeps lives
SATORU_GAME_ID       the game id
SATORU_LIBRARY       root of the game-files library (a game's own files, e.g. a Steam library)
SATORU_CACHE         ~/Library/Caches/satoru/<id>      may be erased at any time
SATORU_LOGS          ~/Library/Logs/satoru/<id>        created before the command runs
SATORU_CONTRACT      1
```

`SATORU_GAME_HOME` is **a directory, and nothing more is promised about where it
physically is**. In satoru's own target layout it is the mount point of one disk image per
game, `/Volumes/satoru-<game>`, holding the game, the Wine prefix and the saves; the Wine
engine lives outside the image; the `.app` is a thin launcher. A host that does not use
images can pass an ordinary folder. Either way the pack must keep **all** of its state inside
`SATORU_GAME_HOME` and use no absolute path outside it, which is also what lets a host move
or image the folder. The home may be an ordinary folder, a mount point, or (satoru's older
layout) a folder inside the `.app`.

A third-party host may choose this path itself, provided it is writable, persistent and on a
local volume, and `SATORU_CACHE` is not required to survive.

Whether a pack tolerates a home at another path: the AoE4 pack did not (its `aoe4.sh` had
the absolute path written into it by `setup.sh`); that is fixed on its `main` branch
(commit `666ecb4` of `NerRobDog/dxmt-aoe4-pack`) and ships with its next release. The Wine
prefix itself held no absolute path to the home. The other packs have not been checked, so a
pack that bakes its home path into a script is a bug against this contract.

The same environment is set at install and at launch. `preflight`, `install` and `uninstall`
run in the root of the unpacked pack; `launch` and `launch_plain` run in `SATORU_GAME_HOME`.
Commands are never interactive: they may not ask the person anything.

## 4a. Login material and the engine

Two things satoru's own layout keeps outside the game's image, and how a pack lets a host
do the same without making it a requirement:

- **Login.** `[planned]` A pack may list, in `game.toml`, the paths inside its prefix that
  carry the player's login (Steam tokens, Steam Guard data): `[paths] login = ["..."]`,
  relative to `SATORU_GAME_HOME`. A host that supports it stores those files per machine,
  outside the home, **copies them in at mount or start**, and **moves them out and deletes
  them at unmount**; it must also remove stale copies at the next start after a crash. Do
  not use a symlink to a place outside the home: Steam writes a file by writing a temporary
  file and renaming it over the old one, which replaces the symlink with a real file inside
  the home (tested, see §10). A host that does not support this leaves the files where they
  are, and must tell the person that the folder or image then carries the login and must not
  be handed to anyone. In the AoE4 pack the login files are `config/loginusers.vdf` and
  `config/config.vdf` under the Steam folder of the prefix; this list is not claimed to be
  complete, and no pack declares `login` yet.
- **Engine.** The pack ships its engine inside the tarball. A host may store engines once,
  by content hash, outside every home and hand each game its pinned version (a hard link
  or clone), so that two games on the same engine build occupy the space once and a
  change to one game's engine never touches another. A pack's engine is always pinned
  by its manifest, never "latest".

## 5. The host sequence

```
Install   [requires] check  ->  download  ->  verify SHA-256 (size is informational)
          ->  unpack (tar, with COPYFILE_DISABLE-built archives, so no ._ entries)
          ->  strip com.apple.quarantine from the whole unpacked pack
          ->  preflight      (the pack writes NOTHING into SATORU_GAME_HOME)
          ->  install        (skipped if preflight answered 11)
          ->  write the launch shim, then record your own state
Launch    run the shim (or the manifest's `launch`) in SATORU_GAME_HOME, with the §4 env
Update    run Install again; `install` is idempotent; `preflight` may answer 11
```

Two rules a host must keep: **never update a pack while its game runs**, and never
between the moment someone presses launch and the moment the game is up. Do the quarantine
strip *before* `preflight`, or a pack's own ad-hoc-signed helper is killed by Gatekeeper
instead of answering.

The shim satoru writes (`SATORU_GAME_HOME/launch`, a `/bin/sh` script) refuses to run from
an App Translocation path, exports the environment, runs the update check in the background
(at most once per six hours, off when `check_updates = false`) and then `exec`s the game.
A host that writes its own shim should keep the translocation refusal: a read-only prefix
fails in confusing ways.

## 6. Exit codes (pack commands)

```
0    done
10   a precondition is not met — the pack's stderr is the message for the person, show it as is
11   nothing to do (already installed / nothing to remove) — this is SUCCESS
12   the pack's own files are not the ones it expects — fetch it again
20   the person said no (a dialog was cancelled) — start nothing, not an error
1    an unexpected break
```

Any other code means "this pack does not speak contract 1": say so, naming the code and the
last line of output; it usually means a release older than the manifest pointing at it.

## 7. Launch modes (optional)

A pack may declare `[modes]` and `[setting_<name>]` sections (for example offline / join /
host with a nick and a server address). The host chooses the mode, collects and validates
the values (`kind`: `text` with a POSIX ERE `pattern`, or `ipv4`) and passes them to
`launch`/`launch_plain` only through the environment:

```
SATORU_MODE              set whenever the pack declares [modes]
SATORU_SETTING_<NAME>    only the chosen mode's values, name upper-cased
```

A host with no UI for this can set `SATORU_MODE` and `SATORU_SETTING_*` itself; anything
missing or refused is exit 10 naming the missing variable. Packs validate again, because
they can be run without any launcher. Satoru stores the choice in
`SATORU_GAME_HOME/satoru-launch.conf`; that file belongs to satoru, a third-party host
should keep its own and not write it.

## 8. What a pack promises, and what it does not

Promises. Those marked (T) have tests in `satoru/tests` (`test_contract_promises.py`,
`test_preflight_writes_nothing_outside_pack.py`); the others are stated by the contract
and not mechanically checked:

- `preflight` writes nothing into `SATORU_GAME_HOME`, and nothing outside the unpacked pack. (T)
- The environment of §4 is set at install and at launch, and `SATORU_LOGS` exists before the
  command runs. (T)
- Commands are non-interactive and use only the exit codes above (11 counts as success). (T for 11)
- A pack states in `foreign_note` anything it writes outside its own home. (shown by the host: T)
- The tarball carries `SHA256SUMS` and a `THIRD_PARTY.md` naming each upstream and its
  commit; the sources of everything that runs are published (Wine under LGPL, DXMT under MIT).
- A distributed pack does not patch a game's executable, in memory or on disk. Changes to
  Wine and DXMT are fine; changes to the game's own code are not.

Does not promise:

- That the game works for you. `status` says what was tested and where.
- That a game's publisher or anti-cheat accepts it. A pack states its own position in
  `notes`. The Overwatch 2 pack says it has not been tried against the anti-cheat; a host
  should not offer a pack whose anti-cheat status is "not cleared" to the public.
- Game files. Packs never contain them; they come from the person's own account.
- Apple's Game Porting Toolkit. No pack contains or needs it. A host must not add it.

## 9. State and updates for a third-party host

Satoru's own `installed.toml` and `satoru-launch.conf` belong to satoru. A third-party
launcher keeps its own record of what it installed (game id, version, source SHA-256,
home) and compares `source.version` with the manifest's to offer an update. Updating is
never automatic: a pack is 100+ MB.

## 10. Gaps to close before offering this to others `[gap]`

1. No machine-readable catalogue: `--list` is for humans. Needed: `--list --json` with id,
   name, status, summary, version, requires, install state.
2. No `--status GAME` (is it installed / which version / is it running) and no `--stop`.
   Running-detection today is the launcher's own business. One candidate technique (not
   tested by us): read `WINEPREFIX` from a process's arguments with `KERN_PROCARGS2` and
   match it to the game home.
3. `uninstall` is `[planned]`: satoru does not call it yet, so a host has no supported
   removal path except deleting `SATORU_GAME_HOME` and the cache.
4. A manifest can only be read by satoru's reader; a reference reader as a tiny
   dependency-free module (or a published JSON schema of the subset) would let hosts in
   other languages stay in step.
5. Versioning: `contract = 1` has no minor version. Optional sections (`[modes]`) are
   refused by an older satoru as unknown sections; third-party hosts need a rule: a host
   that does not know a section must refuse the pack, not guess.
6. The Python 3 requirement: a launcher that is not willing to depend on Python 3.9+ has to
   take way 2.
7. Signing: packs are ad-hoc signed and fetched over HTTPS with a pinned SHA-256. Nothing
   in the contract yet lets a host verify who published a pack. A key or a pinned publisher
   URL list would.

### Image mode: rules and what has been measured

Rules for a host that mounts the home as a disk image: mount without a Finder window
(`hdiutil attach -nobrowse -noautoopen`), at a fixed mount point so that the prefix's paths
never change; unmount when the last user of the volume exits; clean up a volume left mounted
by a crash at the next start; copy saves beside the image (`<game>.saves-backup/`, last three
copies). **A host must not mount an image from a folder that a cloud service is syncing**
(iCloud Drive and similar), or must warn the person first: a sparsebundle is many band
files, and a sync during play can corrupt it. A host can only recognise the folders it knows
about; it cannot be exhaustive. Do not put the mount point under `~/Library`.

What was measured on 2026-10-06 (one Mac, M1 Pro, APFS, warm file cache unless said):

- **Mounting without admin rights.** An account outside the admin group, run with `sudo -u`
  (so without a desktop login), created and mounted a sparsebundle at
  `/Volumes/satoru-<name>` with the command above and wrote to it; no extra rights were
  needed. Not tested: a standard user logged in at the desktop.
- **Speed.** Synthetic (5000 small files plus one 1 GiB file): write 883 MB/s from the image
  against 668 MB/s from a folder, small files 0.51 s against 0.62 s. A native game
  (Magicka, 2.3 GB): entering a level by a scripted route took 12.8 s from the image and
  12.6 s from a folder (three alternating pairs, run one at a time); the first run after a
  re-mount took about 2 s longer. Within noise. Not measured: a cold file cache for the
  folder, and any Wine game with real game data.
- **Login does not stay out by symlink.** Wine reads a login file through a symlink to a
  place outside the image, but an atomic "write a temporary file, then rename over it"
  (what Steam does) replaces the symlink with a real file inside the image. See §4a for what
  a host does instead.
- **Unmount is refused while Wine runs.** `hdiutil detach` fails with "Resource busy" while
  any Wine process holds a file in the image (a running wineserver brings tens). A host asks
  Wine to stop (`wineserver -k`), lists the holders (`lsof +D <mount point>`), kills what is
  left **by pid** (matching by name missed `winedevice` and `cmd`), then detaches. After a
  `kill -9` of everything the image was intact and Wine started again from it.
- **Growth and compaction.** The image grows with what is written (1.5 GB written, 1.5 GB
  more on disk) and does not shrink by itself. `hdiutil compact` reclaims the space on APFS
  and HFS+ images, but on battery it refuses without `-batteryallowed` and prints a
  misleading "Function not implemented". A host passes the flag or tells the person.
- **Not tested:** whether the login files leave anything behind in the image after a forced
  kill with a real Steam session, and how the image behaves across Steam game updates.

## 11. A minimal host, step by step

1. Read `game.toml` with a strict reader; refuse on any error, naming the line.
2. Check `[requires]`. Show `foreign_note`. Ask for consent.
3. Download `source.url`; verify `source.sha256`; unpack into a cache directory; run
   `xattr -dr com.apple.quarantine` on it.
4. Run `preflight` with the §4 environment. 10: show stderr and stop. 11: skip install.
   Anything else outside the table: refuse.
5. Run `install`. Record id, version, SHA-256 and home in your own state.
6. To play: run `launch` in `SATORU_GAME_HOME` with the same environment; add `SATORU_MODE`
   and `SATORU_SETTING_*` for packs that declare modes.
7. To update: step 3 onward, only while the game is not running.
