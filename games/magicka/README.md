# games/magicka — Magicka

Placeholder for the Magicka submodule (repo not published yet). Status: **work in
progress**.

Magicka 1 is XNA 3.1 / D3D9. Under Wine that means wined3d → OpenGL (Apple's
D3DMetal has no d3d9; CrossOver's DXVK backend renders a purple screen; wined3d
on Vulkan crashes creating the depth buffer). Rather than patch around three
dead render paths we are porting the game to **FNA** — native on macOS, no
Wine, Metal through FNA3D.

Where it stands: the tutorial plays with VFX, lighting and voices; shield,
challenges, shadow maps and `segment.dat` loading are done. Open: the binary
shadow format and testing on an M5. Separately, the Windows `Magicka.exe`
patches (IL, Mono.Cecil) that fix the network-join crash (`ReadMessage`
catch-all, v8) live here too.

## Support

`<DONATE-LINK>` — see the umbrella `DONATE.md`.
