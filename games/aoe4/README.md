# games/aoe4 — Age of Empires IV

Submodule placeholder for **dxmt-aoe4-pack** (https://github.com/NerRobDog/dxmt-aoe4-pack), the standalone
pack that runs Age of Empires IV (D3D12-only, Arxan-protected) on Apple
Silicon. Status: **release candidate v0.1**.

What is in the pack (all with source, ~450 MB unpacked):

1. **DXMT with the D3D12 layer** — our fork, branch `aoe4-d3d12`: tiled-resource
   probing survives, rect-bounded clears, pass-through geometry shaders folded
   into the VS, disk shader cache, Metal encoding off the game thread, per-frame
   CSV log, CPU-side frame pacing (`d3d12.cpuPacing`).
2. **Our own Wine engine** (`Engine/`, LGPL 2.1) — built from the CrossOver
   26.3 / Wine 11.0 source with Marc Ibrahim's Rosetta patch (the invalid-opcode
   exceptions the anti-tamper uses as control flow are handled in-process, and
   the code fragments are cached so Rosetta translates each once) plus our
   relocator extension for near conditional branches.
3. **x87sidecar** (`Helpers/`, MIT) — attaches to the game process only; no SIP
   changes, no root.

Numbers (1v1 skirmish vs AI): flat 16.7 ms at pace 60 on an M5 Air (High,
resolution scale 80 %) and on an M1 Pro 16 GB (p50 16.7 / p95 18.1 / p99 18.7
ms); memory 4.5–5.5 GB over a 30-minute match instead of +600 MB/min.

## Using it through satoru

```
python3 launcher/satoru.py          # pick "Age of Empires IV" → Setup
```

which runs `bash games/aoe4/setup.sh` (modes: `--clone` from an existing
CrossOver bottle, `--fresh` with no CrossOver at all — Steam is installed into a
new prefix) and installs to `~/aoe4-pack` (`AOE4_PACK_HOME` to change). Launch
is `~/aoe4-pack/aoe4.sh`; `--plain` starts the same engine with the patches off
for an A/B. The per-machine profile setup chose (pace, image quality) is in
`~/aoe4-pack/README-local.txt`; DXMT logs in `~/aoe4-pack/logs`, frame logs in
`~/aoe4-pack/telemetry/`.

Until the submodule is added, `setup.sh` is not here and the launcher says
`missing: games/aoe4/setup.sh`. Get the pack from https://github.com/NerRobDog/dxmt-aoe4-pack in the
meantime; its own `INSTALL.md` is the full guide.

## Known limits (short)

Tied to one game build (the Wine patch hard-codes addresses; `setup.sh` refuses
other `RelicCardinal.exe` hashes). Keep the display mode at Fullscreen Desktop.
On 16 GB machines close the browser. Plug in. Tested vs AI only.

## Support

`<DONATE-LINK>` — see the umbrella `DONATE.md`.
