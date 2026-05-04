# FBX Mapper's Toolkit

**Blender 5.1 Extension — UV unwrapping and material management for UE5 map geometry**

Inspired by UnrealEd / UT99 surface texturing. Built for mappers who want to work fast and stay accurate.

---

## What it does

- **UV Unwrap** — wall/floor/ceiling projection by world-space normal, edge-stitched chain strips, MaxRects packer
- **Material System** — procedural checker materials per surface type (Floor, Ceiling, Wall, Trim, Ignore) with configurable colours, checker scale, corner markers and quarter-circle tile indicators
- **Island Chains** — user-defined UV island boundary materials with per-chain colour pickers
- **Import Pipeline** — full prep on FBX import: strip foreign materials, clear UVs, auto-assign by normal, unwrap, generate LightmapUVs
- **Export** — FBX export with LightmapUVs enforcement, optional EMIT bake of all materials to PNG (A1-H8 labelled UV reference sheets), saved to `Textures/` alongside the FBX
- **Startup Template** — bake your preferences into a startup blend so every new file is ready to go

---

## Requirements

- Blender 5.1+
- Cycles (for material baking on export)

---

## Installation

1. Download the latest zip from [Releases](../../releases)
2. In Blender: Edit → Preferences → Add-ons → Install from Disk
3. Select the zip — enable **FBX Mapper's Toolkit**
4. Open the 3D Viewport, press **N**, select the **FBX Toolkit** tab

> **Note:** Preferences are in the N-panel, not Edit > Preferences. The Add-ons preferences entry will show a redirect message explaining this.

---

## File structure

```
fbx_mappers_toolkit/
├── __init__.py          # Registration
├── blender_manifest.toml
├── fbx_import.py        # Import operators and full prep pipeline
├── handlers.py          # Depsgraph handler stub
├── materials.py         # Material operators, chain system, node tree builder
├── op.py                # Export operator, UV order enforcement, material baking
├── panel.py             # All panels and UILists
├── props.py             # FBXMT_GlobalPrefs and FBXMT_Props PropertyGroups
├── template.py          # Startup template generator
├── uv_pack.py           # MaxRects BSSF packer
├── uv_unwrap.py         # Core unwrap logic, projection, stitching
└── readme.txt           # Plain text readme shipped with the addon
```

---

## Where preferences are stored

Preferences live on the Scene as `PointerProperty` groups — not in `AddonPreferences`. This sidesteps Blender 5.x extension package prefix issues and means preferences persist with the blend file and in the startup template.

- `scene.fbxmt_prefs_global` → addon-wide settings (checker appearance, colours, import defaults)
- `scene.fbxmt_props` → per-scene settings (export path, texel density, lightmap options)

See `props.py` for full documentation.

---

## Version history

See [CHANGELOG.md](CHANGELOG.md) for full history.

Current version: **2.5.6**

---

## Licence

GPL v3 — see [LICENCE](LICENCE)
