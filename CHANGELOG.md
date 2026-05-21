# FBX Mapper's Toolkit — v2.9.38

UV unwrap, material management, and import/export for UE5 mapping workflows in Blender 5.1+.

---

## What's new in v2.9.38

### Colour Modifier System — full overhaul

The previous V2 colour system (per-slot DARKER/LIGHTER/GREYSCALE/INVERSE modes with free colour pickers) has been replaced with a unified, notch-driven system that prevents impossible or ugly colour combinations at the source.

**Anchor Colour A** — H/S/V notch controls  
- Hue: 30° steps (12 positions, 0°–330°)  
- Saturation: Low (0.3) / Medium (0.6) / High (0.8) — Full saturation removed (looks bad at checker scale)  
- Value: Darkest (0.25) / Dark (0.35) / Mid (0.50) / Light (0.60) / Lightest (0.80)

**Anchor Colour B** — always derived from A  
- Hue Offset: 0° / 30° / 60° / 90° / 120° / 150° / 180° (replaces fixed INVERSE)  
- Saturation: same notches as A  
- Value: same notches as A — default Darkest (0.35)  
- Removed: MANUAL free picker, DARKER slider, GREYSCALE slider, INVERSE fixed mode

**Island Colour** — independent S/V for A, full H+/S/V for B  
- Island A hue tracks Wall A (unchanged)  
- All modifier controls updated to match new notch system

**B colours are no longer stored** — all `color_{slot}_b` FloatVectorProperties removed. B is always computed fresh from A via `_resolve_color_b`, eliminating the stale 50/50 tile bug on blend file load.

### Ignore material — hardcoded grey
Ignore A is always dark grey `(0.25, 0.25, 0.25)`, Ignore B is always dark grey `(0.10, 0.10, 0.10)`. Cannot accidentally follow the anchor hue. Override via preset JSON only.

### Setup window UI overhaul

- **Left column order:** Anchor Colour A → Anchor Colour B → colour swatches → Island Colour A → Island Colour B → Apply/Reset. Swatches act as a natural visual separator between anchor and island controls.
- **Right column:** Checker Style and all settings now live in their own nested box. Lock Settings is a separate box below it with double vertical spacing — outside the checker box, clearly separated.
- **Checker Style order:** Wall/Floor/Ceiling/Ignore in column 1, Island/Ramp Floor/Ramp Ceiling/Trim in column 2 — each base material pairs with its logical counterpart.
- **Row margins:** leading and trailing `separator()` on every content row for horizontal breathing room.
- **Checker style row spacing:** `separator(factor=0.4)` between each pair of rows.
- **Label consistency:** all colour section labels follow `X Colour — Y:` pattern throughout.
- **Island swatches:** now shown in swatch row 2 (replacing transparent spacers), tracking Wall colours so they always match.

### Tile persistence fix
- `OT_FBXMT_Rebuild_Materials` now fires `bake_all_modal` after rebuilding, so `__tile_*` N-panel UIList thumbnails stay current after OK or manual Rebuild.
- Ramp materials (`M_FBXMT_Ramp_Floor`, `M_FBXMT_Ramp_Ceiling`) added to the bake queue — previously never baked on OK.

### Bug fixes
- Island marker colour derivation: was using `rgb_to_hsv`/`hsv_to_rgb` while all other paths used HLS — now consistent throughout, fixing the hue mismatch between island and wall tiles.
- `_safe_float`: now resets stale EnumProperty values back to a valid string on read, fixing blank dropdowns after loading blend files saved with older prop sets.
- Swatch colour space: corrected linear→sRGB encode so swatches match tile display (within ~2% saturation, visually indistinguishable).

---

## Installation

1. Download `fbx_mappers_toolkit_v2.9.38.zip`
2. In Blender 5.1+: **Edit → Preferences → Extensions → Install from Disk**
3. Select the zip — do not unzip first
4. Enable the extension if not auto-enabled

---

## Quick start

1. Open the **N-panel → FBX Toolkit** tab
2. Run **Project Setup** to configure materials, UV settings, and anchor colour
3. Import or open your FBX geometry
4. Use **Auto-Assign** to classify faces by normal angle (Floor / Ceiling / Wall / Ramp)
5. Use **UV Unwrap** to apply FBXMT projection per material type
6. Export via **Export** tab for UE5, or use the engine adapter pipeline

---

## Colour system reference

| Control | What it does |
|---|---|
| Anchor H | Base hue in 30° steps. Wall=H, Floor=H+120°, Ceiling=H+240°, Trim=H+270° |
| Anchor S | Saturation of all A colours |
| Anchor V | Lightness of all A colours |
| B H+ | Hue rotation from A to derive all B colours |
| B S | Saturation of all B colours |
| B V | Lightness of all B colours (default: Darkest) |
| Island A S/V | Independent sat/val for island marker A (hue tracks Wall) |
| Island B H+/S/V | Full derivation controls for island marker B |

---

## Material classification thresholds

| Angle | Assigned material |
|---|---|
| ≤ 15° from horizontal | Floor / Ceiling |
| 15° – 45° | Ramp Floor / Ramp Ceiling |
| > 45° | Wall |

Thresholds (`floor_ramp_threshold` / `ramp_wall_threshold`) are exposed in Project Setup → Project tab → Classification.

---

## Known issues / outstanding work

- Prop rename: `uv_floor_threshold` → `ramp_wall_threshold`, `ramp_threshold` → `floor_ramp_threshold` (semantically swapped, low risk, pending)
- Wall/wall (wall-run) fix — blocked on `_edge_frame` refactor
- Ramp seams — Z-threshold classification fails for non-axis-aligned faces, same blocker
- `PROFILE_180_FLAT` 10-vert not yet implemented
- Chamfer/Apex nose modifiers — not yet implemented
- Vertex-level generation, sequential generation, face/vertex select modes
- UV unwrap lockout toggle on trim objects
- CORE: FBXMT project guard, version in prefs panel
- Materials: Ramp material NodeFrame labels
- Engine adapters: interchange format spec, fbxmt-ue5/unity/godot/flax
- Export: UE5 procedural material pipeline, UT99 Play Volume Generator

---

## Repo

https://github.com/Karmacopper/fbx-mappers-toolkit

Maintainer: Ja5mine  
Licence: GPL-3.0-or-later
