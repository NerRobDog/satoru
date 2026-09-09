# games/ow2 — Overwatch 2

Placeholder for the Overwatch 2 submodule (repo not published yet). Status:
**playable**, install still hand-driven — the launcher shows it as WIP until a
`setup.sh` exists.

What is known to work (both an M1 Pro and an M5 Air, daily play):

- DXMT from our fork, `main` at the `ir-release` merge. The patch that matters
  is `d3d11.releaseShaderIR`: stock DXMT kept ~7.7 GB of parsed shader IR
  forever, so the game sat at 13 GB resident and swapped. With the release the
  footprint is ~4 GB and swap is gone. Validated by full matches on both
  machines.
- Reference config (M1 Pro): `preferredMaxFrameRate = 60`,
  `ignoreMapFlagNoWait = False`, `metalSpatialUpscaleFactor = 1.33` with
  `DXMT_METALFX_SPATIAL_SWAPCHAIN=1`. MetalFX here is an output upscale for
  sharpness, not a speed-up.
- Today the DLLs are dropped into a CrossOver bottle by hand
  (`lib/dxmt/{x86_64-windows,x86_64-unix}`, md5-checked). The pack will do the
  same as AoE IV: a home directory with our engine, no CrossOver at runtime.

Not in the tree and never will be: the shader cache (derived from game content)
and any game files.

## Support

https://github.com/NerRobDog/satoru/blob/main/DONATE.md — see the umbrella `DONATE.md`.
