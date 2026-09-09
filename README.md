# satoru

One entry point for running Windows games on Apple Silicon with **open,
reproducible** builds: our fork of [DXMT](https://github.com/NerRobDog/dxmt)
(D3D11/D3D12 → Metal), our own Wine builds where a game needs a patched engine,
and a small launcher that knows how to set up and start each game.

No closed binaries, no app-store wrapper, no subscription. Every DLL and engine
in a release is built from a commit you can check out, and the release notes
carry the hashes.

## Philosophy

- **Open.** Sources for everything that runs: the DXMT fork, the Wine tree and
  the patches on top of it, the helper that hooks Rosetta. If we could not
  publish it, we did not ship it.
- **Reproducible.** A game pack is a tarball with `SHA256SUMS`, a `setup.sh`
  that builds a clean home from nothing, and a `THIRD_PARTY.md` naming each
  upstream and the exact commit. `setup.sh --fresh` needs no CrossOver at all.
- **Frametime over fps.** A flat 16.7 ms line beats a "120 fps average" with a
  300 ms stall every tenth frame. Knobs are accepted by the smoothness of the
  frame graph (p95 / p99, stalls > 200 ms, memory at start and end of a
  20-minute match), not by the mean.

## Games

| Game | Status | Where | Notes |
|---|---|---|---|
| Age of Empires IV | **release candidate v0.1** | [`games/aoe4`](games/aoe4/) → https://github.com/NerRobDog/dxmt-aoe4-pack | own Wine engine (LGPL source + relocator patch) + x87sidecar; flat 60 on M1 Pro 16 GB and M5 Air, memory stays at 4.5–5.5 GB |
| Overwatch 2 | playable | [`games/ow2`](games/ow2/) → _(repo not published yet)_ | DXMT fork `ir-release`: shader-IR release patch takes the game from 13 GB to ~4 GB resident, swap gone |
| Magicka | work in progress | [`games/magicka`](games/magicka/) → _(repo not published yet)_ | FNA port (native, no Wine); tutorial playable, shadows and M5 testing open |
| Prime World | work in progress | [`games/prime-world`](games/prime-world/) → _(repo not published yet)_ | offline 5v5 vs bots plays (first time on a Mac at all); CI Release build, 99 % of time in x87 → SSE2 rebuild pending |

Status vocabulary (also used by `game.toml`): `rc` = release candidate, pack
tested end-to-end on two machines; `playable` = we play it, install is still
hand-driven; `wip` = not for users yet.

## Layout

```
satoru/
  README.md          this file
  DONATE.md          how to support the work
  LICENSE            MIT — for the umbrella and the launcher only (see below)
  games/             one directory per game; each is a git submodule
    aoe4/            dxmt-aoe4-pack (standalone, has its own README/INSTALL)
    ow2/             Overwatch 2 notes and config (stub for now)
    magicka/         Magicka FNA port (stub for now)
    prime-world/     Prime World (stub for now)
  components/        the shared pieces: dxmt, wine-aoe4, x87sidecar
  launcher/          satoru.py — the TUI, Python 3 stdlib only
```

Each `games/<id>/game.toml` tells the launcher what the game is called, how
far along it is, and which commands set it up and start it. Format in
[`games/README.md`](games/README.md).

## Launcher

```
python3 launcher/satoru.py            # TUI (curses)
python3 launcher/satoru.py --list     # plain listing, no terminal UI
python3 launcher/satoru.py --check    # validate every games/*/game.toml
```

Arrow keys or `j`/`k` pick a game, `Enter` opens its actions (Setup, Launch,
Launch --plain, Show profile, Open logs), `q` quits. Actions a game does not
support yet are shown greyed with `· SOON` and do nothing; actions whose script
is not present (submodule not checked out) say so instead of failing quietly.

Requires Python 3.8+ (macOS ships 3.9; `tomllib` is used when the interpreter is
3.11+, otherwise a built-in minimal TOML reader covers the `game.toml` subset).

## Components and licenses

The umbrella repository and the launcher are **MIT** (`LICENSE`). The things
that actually run the games are under their own licenses, unchanged:

| Component | License | Source |
|---|---|---|
| DXMT fork (`components/dxmt`) | LGPL | https://github.com/NerRobDog/dxmt |
| Wine engine for AoE IV (`components/wine-aoe4`) | LGPL 2.1 | CrossOver 26.3 / Wine 11.0 tree with Marc Ibrahim's Rosetta patch + our relocator diff — https://github.com/NerRobDog/wine-aoe4 |
| x87sidecar (`components/x87sidecar`) | MIT | fork of Lifeisawful/rosettax87_jit by athei — https://github.com/NerRobDog/x87sidecar |

Details in [`components/README.md`](components/README.md). No game files, no
shader caches, no Apple D3DMetal are distributed anywhere in this tree.

## Support

Free, open, and staying that way. If it helped: `<DONATE-LINK>` — see
[`DONATE.md`](DONATE.md) for what the money is used for.
