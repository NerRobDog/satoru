# components/

The pieces every game pack is assembled from. Each will be a git submodule at
the commit the current packs are built from; a pack's `THIRD_PARTY.md` names the
exact commit and the hashes of what shipped.

| Directory | What | License | Source |
|---|---|---|---|
| `dxmt/` | Our fork of DXMT: D3D11 and the experimental D3D12 layer → Metal. Branches: `main` (OW2, `ir-release` merged — shader-IR release), `aoe4-d3d12` (the D3D12 fixes for AoE IV, CPU-side pacing, per-frame CSV log, disk shader cache). The fork keeps the upstream name; nothing goes to upstream from here. | LGPL | https://github.com/NerRobDog/dxmt |
| `wine-aoe4/` | Wine engine for Age of Empires IV: CrossOver 26.3 / Wine 11.0 source carrying Marc Ibrahim's Rosetta patch (in-process delivery of the anti-tamper's invalid-opcode exceptions and a fixed cache for the code fragments it continues into), plus our relocator extension (near conditional branches `0F 8x rel32` leaving a fragment are relocated too). Built by us, from source, with the x86_64 deps it needs (freetype, gnutls, inotify, …). | LGPL 2.1 | https://github.com/NerRobDog/wine-aoe4; our diff: `wine-aoe-patch-relocator.diff` in the AoE IV pack |
| `x87sidecar/` | Helper that attaches to the game process and hooks Rosetta's decoder there (softfault decoder hook). Per-process only: no SIP change, no root, nothing system-wide. Fork of Lifeisawful/rosettax87_jit by athei, built by us from the MIT source. | MIT | https://github.com/NerRobDog/x87sidecar |

Build consistency rules we hold ourselves to (from the project's working
agreement):

- All DXMT DLLs in one pack come from **one** commit and one build directory;
  the version stamp inside the DLL (`strings d3d12.dll | grep v0.80…`) must
  match the tagged commit.
- A config option that the shipped DLL does not know is a packaging error, not
  a harmless extra.
- Release tags are set after acceptance (clean `setup.sh` from the tarball, a
  ≥ 20-minute match with frametime p50/p95/p99 and memory at start and end,
  `lsof` showing no library from `/Applications/CrossOver` or `/usr/local`).
