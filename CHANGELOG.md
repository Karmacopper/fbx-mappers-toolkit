# Changelog

All notable changes to FBX Mapper's Toolkit are documented here.

---

## [2.9.0] — Current

- Fix: Material bake on export now uses the EEVEE tile renderer (`_render_tile`) instead of Cycles. No Cycles dependency anywhere in the codebase. `_bake_material_emit` gutted and replaced; temporary bake quad, bake nodes, and all `scene.cycles.*` access removed from `op.py`
- Fix: `_render_preview` (3D mesh preview scene) switched from `CYCLES` to `BLENDER_EEVEE_NEXT`
- Fix: Auto-Colour Islands now calls `rebuild_fbxmt_materials()` after assignment — island sub-materials immediately reflect current pattern, colours, and corner mark settings without requiring a manual Rebuild
- Version strings corrected and unified: `__init__.py`, `blender_manifest.toml` all read `2.9.0`

## [2.8.0]

- Feature: Project Setup dialog overhauled. Tile preview uses fast EEVEE renderer — near-instant. Tiles are clickable material selectors with highlight on active tile
- Feature: Contact sheet updated to 3×2 layout for 6 visible materials
- Feature: Floor/Ceiling/Wall tiles show split view — top half standard checker, bottom half island B stepping
- Feature: Material preset system — save/load/delete named JSON presets, team-shareable via configurable folder path in AddonPreferences
- Feature: Apply B / Apply B to All split button in Project Setup
- Feature: UV Preview mesh — builds flat mesh from UVMap coordinates in UV_Preview collection, enters local view automatically
- Feature: UVPreview UV channel — scaled-to-fit copy of UVMap, created on every unwrap, stripped on export
- Feature: Checker scale now power-of-2 button row (1, 2, 4, 8, 16, 32)
- Feature: Island auto-colouring fires on assign, deferred via timer to avoid edit-mode bmesh conflict
- Feature: Bare Island Marker faces route to wall unwrap path
- Fix: Update Tile cache hash skip removed — was silently doing nothing after first run
- Fix: Contact sheet bilinear scale artefact — `CELL_SIZE` now matches `PREVIEW_SIZE`
- Fix: Island B step cycling now per-square not per-pixel

## [2.7.0]

- Feature: Island marker system replaces 5 chain materials. `M_FBXMT_Island` is the single visible marker. 15 hidden sub-materials (`M_FBXMT_Island_Floor/Ceil/Wall_01–05`) assigned automatically by adjacency graph colouring — no manual numbering
- Feature: Auto-colouring fires on assign; existing islands respected on re-runs. Island Colour A tracks Wall Colour A
- Feature: On export, island faces are surface-detected and replaced with base materials before FBX write; island slots stripped
- Feature: Per-material checker patterns — Square / Diagonal / Diamond / Circle. Circle radius = √(1/2π) ≈ 0.3989 (equal-area)
- Feature: Checkerception — island sub-materials XOR pattern with checker phase via `checker_invert=True`
- Feature: Setup V2 colour system — single anchor hue (0–360°) drives all A colours: Wall=H, Floor=H+120°, Ceiling=H+240°, Trim=H+270°. Global B mode (Darker/Lighter/Greyscale/Inverse) + notch (1–3 = 25%/50%/75%)
- Feature: Corner reticle — `show_corner_lines` toggle: off=short arms (preset 2), on=full tile-edge lines (preset 4). Gamma-correct invert for cross marker colour

## [2.6.7]

- Fix: Preferences panel now correctly appears last in the N-panel — registration order in `classes` tuple corrected

## [2.6.6]

- QoL: Rebuild Materials button added to the bottom of the Preferences panel

## [2.6.5] — Housekeeping pass

- Duplicate `bake_labels` property removed from `FBXMT_GlobalPrefs`
- Duplicate `OT_FBXMT_Set_Corner_Preset` import removed from `__init__.py`
- Stale `_get_prefs()` docstring corrected
- Backslash line continuations in `fbx_import.py` replaced with parenthesised expressions
- Dead `_collect_materials` static method removed from `OT_FBXMT_Export`
- Scene Setup "Reset Scene to Defaults" button removed
- Version strings corrected across all files

## [2.5.6]

- Corner marker system rebuilt: `corner_mark_length` slider replaced with 4 preset buttons (12.5 / 25 / 37.5 / 50% of texel tile)
- Quarter-circle SDF markers added — radius = half arm length, toggle in Preferences
- `OT_FBXMT_Set_Corner_Preset` operator added, rebuilds immediately on click

## [2.5.5]

- `bake_labels` toggle added — overlay A1–H8 grid coordinate labels on baked PNGs
- 5×7 bitmap font embedded in `op.py`, drawn directly into pixel array post-bake

## [2.5.4]

- `bake_textures` toggle added to Export panel (default on)

## [2.5.3]

- Material bake now uses a temporary 1×1m quad per material instead of the export mesh
- Fixes black bake caused by meshes having no faces assigned to certain material slots

## [2.5.2]

- Cycles sample count explicitly set to minimum 1 before bake

## [2.5.1]

- Bake now explicitly sets UVMap as active UV layer before baking
- Fixes "No active UV layer" error on export

## [2.5.0] — Material baking on export

- EMIT bake of all unique materials to PNG on export
- Output to `{export_path}/Textures/{material_name}.png`
- Bake failures reported as warnings, export continues

## [2.4.9] — Panel order, texel density preset

- Import panel moved above UV Maps & Unwrap
- `geo_texel_density` changed from FloatProperty to IntProperty with 5 preset buttons
- `OT_FBXMT_Set_Texel_Density` operator added, rebuilds materials immediately

## [2.4.6] — Housekeeping

- Dead imports removed from `materials.py` and `panel.py`
- Changelog rewritten with accurate history

## [2.4.5] — Blender 5.1 node API fixes

- `ShaderNodeMixRGB` replaced with `ShaderNodeMix` (data_type='RGBA')
- `ShaderNodeInvert`/`ShaderNodeInvertColor` replaced with `ShaderNodeMix` DIFFERENCE blend against white

## [2.4.3]

- `math`, `bmesh`, `Vector` promoted to module-level imports in `materials.py`

## [2.4.0] — Structural refactor

- `props.py` created — `FBXMT_GlobalPrefs` and `FBXMT_Props` moved out of `panel.py`
- `README.md` and `CHANGELOG.md` added for GitHub

## [2.3.9] — Clarity pass

- Dead constants, parameters, and aliases removed and renamed throughout

## [2.3.8] — Bug fixes

- `_ask_index` reset to 0 on new import batch
- `rebuild_fbxmt_materials` no longer pushes chain materials to all scene meshes
- `_enforce_uv_order` write path uses single bmesh pass
- `FBXMT_AddonPreferences.draw` shows redirect message

## [2.3.7]

- Em dashes replaced with `-` in all user-visible strings (Windows console encoding fix)

## [2.3.5] — Dead props removed

- `uv_density_preset`, `uv_density_unit`, `uv_texel_density`, `default_texel_density` deleted

## [2.3.4] — A/B colours + invert cross

- A and B colour pickers exposed for all 5 base materials
- Cross arms use `ShaderNodeMix` DIFFERENCE invert

## [2.3.3] — Checker system architecture fix

- Two independent mapping paths: `mapping_checker` (checker scale) and `mapping_tile` (corner markers)
- Corner crosses land at texel tile boundaries, not checker square corners

## [2.3.2]

- `BORDER_W` modulo fixed from 1.0 to 0.5

## [2.3.1]

- `BORDER_W` formula fixed: `px * scale / 1024`

## [2.3.0] — Checker scale working

- `checker_scale` changed from `EnumProperty` to `IntProperty`
- `OT_FBXMT_Set_Checker_Scale` operator added

## [2.2.x] — Material panel rebuilt

- Surface Materials and Island Materials as two object-scoped UILists
- Assign and Select face operators
- Texel density consolidated to Scene Setup

## [2.1.x] — Initial reviewed build

- PNG files removed from zip
- `blender_manifest.toml` fixed: `blender_version_min = "5.1.0"`, `category = "Import-Export"`
- `LightmapUVs` protected from removal in `OT_FBXMT_UV_Remove`

---

## Versioning

`MAJOR.MINOR.PATCH` — patch increments on every build, minor on significant feature additions, major reserved for architectural changes or the Bridge project.
