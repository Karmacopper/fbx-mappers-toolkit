# Changelog

All notable changes to FBX Mapper's Toolkit are documented here.

---

## [2.6.7] — Current

- Fix: Preferences panel now correctly appears last in the N-panel — registration order in `classes` tuple corrected in `__init__.py`

## [2.6.6]

- QoL: Rebuild Materials button added to the bottom of the Preferences panel — no more scrolling back up to the Materials panel after tweaking colours or checker settings

## [2.6.5] — Housekeeping pass

- Duplicate `bake_labels` property removed from `FBXMT_GlobalPrefs` in `props.py` (was silently clobbering the first definition)
- Duplicate `OT_FBXMT_Set_Corner_Preset` import removed from `__init__.py`
- Stale `_get_prefs()` docstring corrected ("from WindowManager" → "from the active scene")
- Backslash line continuations in `fbx_import.py` replaced with parenthesised expressions
- Dead `_collect_materials` static method removed from `OT_FBXMT_Export` in `op.py`
- Scene Setup panel "Reset Scene to Defaults" button removed — redundant now that the startup template is the established first-run workflow
- Version strings corrected across `__init__.py`, `blender_manifest.toml`, and `readme.txt` (were stuck at 2.5.6)

## [2.5.6]

- Corner marker system rebuilt: `corner_mark_length` slider replaced with 4 preset buttons (12.5 / 25 / 37.5 / 50% of texel tile)
- Quarter-circle SDF markers added — radius = half arm length, toggle in Preferences, drawn in shader alongside cross arms
- `OT_FBXMT_Set_Corner_Preset` operator added, rebuilds immediately on click

## [2.5.5]

- `bake_labels` toggle added to Preferences — overlay A1-H8 grid coordinate labels on baked PNGs
- 5×7 bitmap font embedded in `op.py`, drawn directly into pixel array post-bake
- Label colour inverted from checker square colour for guaranteed contrast
- Label scale adapts to square size

## [2.5.4]

- `bake_textures` toggle added to Export panel (default on)
- When off: no bake runs, no Textures/ folder created

## [2.5.3]

- Material bake now uses a temporary 1×1m quad per material instead of the export mesh
- Fixes black bake caused by meshes having no faces assigned to certain material slots

## [2.5.2]

- Cycles sample count explicitly set to minimum 1 before bake to prevent black output
- Debug pixel value print added (removed in 2.5.3)

## [2.5.1]

- Bake now explicitly sets UVMap as active UV layer before baking
- Renderer switched to Cycles for bake, restored after
- Fixes "No active UV layer" error on export

## [2.5.0] — Material baking on export

- EMIT bake of all unique materials to PNG on export
- Output to `{export_path}/Textures/{material_name}.png`
- Textures/ subfolder created automatically
- Bake failures reported as warnings, export continues
- `_collect_materials` and `_bake_material_emit` static methods added to `OT_FBXMT_Export`

## [2.4.9] — Panel order, texel density preset

- Import panel moved above UV Maps & Unwrap
- `geo_texel_density` changed from FloatProperty to IntProperty with 5 preset buttons (512 / 1024 / 2048 / 4096 / 8192)
- `OT_FBXMT_Set_Texel_Density` operator added, rebuilds materials immediately

## [2.4.6] — Final housekeeping

- Dead `import os` removed from `materials.py` and `panel.py`
- Changelog rewritten with accurate history
- Remaining em dashes fixed in readme.txt

## [2.4.5] — Blender 5.1 node API fixes

- `ShaderNodeMixRGB` replaced with `ShaderNodeMix` (data_type='RGBA')
- `ShaderNodeInvert`/`ShaderNodeInvertColor` replaced with `ShaderNodeMix` DIFFERENCE blend against white
- `math`, `bmesh`, `Vector` promoted to module-level imports in `materials.py`

## [2.4.3]

- `math`, `bmesh`, `Vector` added to module-level imports (were local imports inside operators, broken after `_execute` inline)

## [2.4.2]

- Syntax fix: missing newline after docstring in `_shelf_estimate`

## [2.4.1] — Remaining issue list cleared

- `_suppress_handler` documented as live infrastructure
- Export path removed from Scene Setup panel (canonical location: Preferences + Export)
- `ADN_OT_Duplicate` alias replaced with `OT_FBXMT_Export` everywhere
- `_shelf_estimate` dead `max_x` computation removed, return simplified to single height float
- `bpy.data.materials.keys()` to list conversion removed (already cleaned in earlier pass)

## [2.4.0] — Structural refactor

- `props.py` created — `FBXMT_GlobalPrefs` and `FBXMT_Props` moved out of `panel.py`
- Full property storage model documented in `props.py` and `readme.txt`
- `README.md` and `CHANGELOG.md` added for GitHub

## [2.3.9] — Clarity pass

- Dead `CHECKER_SCALE = 2` constant deleted
- `PRESET_LIGHTNESS` renamed to `CHAIN_COLOR_LIGHTNESS`
- `get_addon_dir()` deleted (never called)
- `img=None` dead parameter removed from `setup_material_nodes`
- `import bpy as _bpy` alias removed from `_build_checker_node_tree`
- `n(t, x, y)` renamed to `new_node(node_type, x, y)`
- `_c = lambda` removed, inlined as `tuple(p[:3])`
- `_execute` inlined into `execute` on `OT_FBXMT_Assign_Materials` with correct try/finally

## [2.3.8] — Bug fixes

- `_ask_index` now reset to 0 when a new import batch starts
- `rebuild_fbxmt_materials` no longer pushes chain materials to all meshes in scene
- `_enforce_uv_order` write path now uses a single bmesh pass across all layers
- `FBXMT_AddonPreferences.draw` now shows a redirect message instead of blank panel
- `app_templates/` and `templates/` stub directories removed from zip

## [2.3.7]

- Em dashes replaced with `-` in all user-visible strings across all files (Windows console encoding fix)

## [2.3.5] — Dead props removed

- `uv_density_preset`, `uv_density_unit`, `uv_texel_density`, `default_texel_density` all deleted
- Dead seed lines removed from `OT_FBXMT_Scene_Setup.execute`

## [2.3.4] — A/B colours + invert cross

- A and B colour pickers exposed for all 5 base materials in Preferences
- Cross arms now use `ShaderNodeMix` DIFFERENCE invert instead of second checker node
- Maximum contrast at any colour combination

## [2.3.3] — Checker system architecture fix

- Two independent mapping paths: `mapping_checker` (checker scale) and `mapping_tile` (corner markers)
- Corner crosses now land at texel tile boundaries (geo_texel_density/1024), not checker square corners
- `BORDER_W` and `BORDER_L` correctly expressed as fractions of one texel tile

## [2.3.2]

- `BORDER_W` modulo fixed from 1.0 to 0.5 (ShaderNodeTexChecker Scale=1.0 produces 0.5-wide squares)

## [2.3.1]

- `BORDER_W` formula fixed: `px * scale / 1024` (was `px / 1024`, making arms invisible at scale > 1)

## [2.3.0] — Checker scale working

- `checker_scale` changed from `EnumProperty` to `IntProperty` — fixes Blender extension reload bug where saved value `'8'` failed enum lookup
- 1/2/4/8 button row in panel, clicking rebuilds immediately
- `OT_FBXMT_Set_Checker_Scale` operator added

## [2.2.x] — Material panel rebuilt

- Surface Materials and Island Materials as two object-scoped UILists
- Cross-list deselection via `fbxmt_base_selected` / `fbxmt_island_selected` bool props
- Assign and Select face operators
- Texel density consolidated to Scene Setup with live tile size readout
- `OT_FBXMT_Add_Chain_To_Object` removed

## [2.1.x] — Initial reviewed build

- PNG files removed from zip (spec violation)
- `blender_manifest.toml` fixed: `blender_version_min = "5.1.0"`, `category = "Import-Export"`
- Dead `_suppress_handler` value-copy import removed from `fbx_import.py`
- No-op `@persistent` depsgraph handler unregistered
- `LightmapUVs` protected from removal in `OT_FBXMT_UV_Remove`
- Dead `CHAIN_LOCKED_NAME` import removed from `fbx_import.py`

---

## Versioning

`MAJOR.MINOR.PATCH` — patch increments on every build, minor on significant feature additions, major reserved for architectural changes or the Bridge project.
