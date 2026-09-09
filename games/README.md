# games/

One directory per game. Each is (or will be) a **git submodule** pointing at
that game's own repository, so a pack can be cloned and used on its own without
the umbrella. The umbrella only adds a `game.toml` per game so the launcher can
find it.

| Directory | Submodule | State |
|---|---|---|
| `aoe4/` | dxmt-aoe4-pack — https://github.com/NerRobDog/dxmt-aoe4-pack | release candidate v0.1; the pack has its own `README.md`, `INSTALL.md`, `setup.sh`, `aoe4.sh` |
| `ow2/` | dxmt-ow2-pack — https://github.com/NerRobDog/dxmt-ow2-pack | reference `dxmt.conf`, `install-dxmt.sh` into a CrossOver bottle, measurement scripts. No release and no engine of its own yet, so the launcher keeps it at SOON |
| `magicka/` | magicka-fna — https://github.com/NerRobDog/magicka-fna | the FNA port itself: XNA 3.1 bridge, twins, relinker, launcher. Setup is still by hand (`tools/prepare.sh` against your own Steam install) |
| `prime-world/` | _(repo not published yet)_ | stub: CI build, offline matches |

`aoe4/`, `ow2/` and `magicka/` are submodules, and each carries its own `game.toml` — the
umbrella adds no file inside them. `aoe4`'s `Setup` runs its `bootstrap.sh`, which downloads
and verifies the release archive; `ow2` and `magicka` declare no commands yet and the launcher
marks their actions SOON rather than pretending. `prime-world/` still holds only a `README.md`
and a `game.toml` until its repository exists.

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

Every game here is free and open. https://github.com/NerRobDog/satoru/blob/main/DONATE.md — see `../DONATE.md`.
