# games/

One directory per game. Each is (or will be) a **git submodule** pointing at
that game's own repository, so a pack can be cloned and used on its own without
the umbrella. The umbrella only adds a `game.toml` per game so the launcher can
find it.

| Directory | Submodule | State |
|---|---|---|
| `aoe4/` | dxmt-aoe4-pack — `<REPO-LINK>` | release candidate v0.1; the pack has its own `README.md`, `INSTALL.md`, `setup.sh`, `aoe4.sh` |
| `ow2/` | `<REPO-LINK>` | stub: notes + reference `dxmt.conf`, no setup script yet |
| `magicka/` | `<REPO-LINK>` | stub: FNA port in progress |
| `prime-world/` | `<REPO-LINK>` | stub: CI build, offline matches |

Until the submodules are wired (`git submodule add <url> games/aoe4`), the
directories hold only a `README.md` and a `game.toml`; the launcher reports the
setup script as missing rather than pretending.

## `game.toml`

```toml
[game]
name   = "Age of Empires IV"       # shown in the launcher
id     = "aoe4"                    # short id, same as the directory name
status = "rc"                      # rc | playable | wip
home   = "~/aoe4-pack"             # where setup installs to; ~ and $VARS expand

# Commands. Relative paths resolve from the satoru root; ~ and $VARS expand.
# Leave a key out (or empty) and the launcher greys that action out.
setup        = "bash games/aoe4/setup.sh"
launch       = "~/aoe4-pack/aoe4.sh"
launch_plain = "~/aoe4-pack/aoe4.sh --plain"   # A/B: same engine, patches off
profile      = "~/aoe4-pack/README-local.txt"  # text file, opened in a pager
logs         = "~/aoe4-pack/logs"              # directory, opened with `open`

notes = """
Free text shown under the game in the launcher: what works, what to keep in
mind (display mode, memory), which machines were tested.
"""
```

Rules the launcher applies:

- `status = "wip"` → every action is shown as `· SOON` and disabled, whatever
  the command keys say. No live-looking dead buttons.
- `status` is `rc` or `playable` → an action is enabled only if its key is set
  **and** the first word of the command (after `bash`/`sh`) exists on disk.
  Otherwise it is shown as `· missing: <path>`.
- `setup` runs with the satoru root as working directory; `launch*` run with
  `home` as working directory when it exists.

## Support

Every game here is free and open. `<DONATE-LINK>` — see `../DONATE.md`.
