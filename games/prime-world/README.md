# games/prime-world — Prime World

Placeholder for the Prime World submodule (`<REPO-LINK>`). Status: **work in
progress**.

A full offline 5v5 match against bots plays on an M1 — the first time the game
has run on a Mac at all. The client is built from source on CI as a single
`PrimeWorld.exe` (two start-up bugs fixed there: static-init order across the
former DLLs, and the community client's launcher check). It runs under Wine
with wined3d (D3D9) plus a prefaulted MoltenVK, and 99 % of the frame time is
x87 code under Rosetta, so the next step is an SSE2 rebuild (or the x87
sidecar). Lobby 90–105 fps, battle 13–20 fps on the debug build.

## Support

`<DONATE-LINK>` — see the umbrella `DONATE.md`.
