# FBX Mapper's Toolkit

**Blender 5.1 Extension — UV unwrapping and material management for game engine (UE5, Godot, Unity et al) map geometry**

Inspired by UnrealEd / UT99 surface texturing. Built for mappers who want to work fast and stay accurate.

---

## What it does

- **UV Unwrap** — wall/floor/ceiling projection by world-space normal, edge-stitched chain strips, MaxRects packer
- **Material System** — procedural EEVEE checker materials per surface type (Floor, Ceiling, Wall, Trim, Ignore) with a single anchor hue driving all colours, configurable patterns (Square / Diagonal / Diamond / Circle), corner markers and quarter-circle tile indicators
- **Colour Derivation** — single anchor hue (0–360°) drives all A colours via fixed offsets; global B mode (Darker / Lighter / Greyscale / Inverse) + notch
- **Island Chains** — single visible Island Marker material; 15 hidden sub-materials auto-assigned by adjacency graph colouring (Floor / Ceiling / Wall groups). Colouring fires on assign and immediately reflects current prefs — no manual Rebuild required
- **Import Pipeline** — full prep on FBX import: strip foreign materials, clear UVs, auto-assign by normal, unwrap, generate LightmapUVs
- **Export** — FBX export with LightmapUVs enforcement, optional EEVEE tile render of all materials to PNG (A1–H8 labelled UV reference sheets), saved to `Textures/` alongside the FBX. No Cycles required
- **Project Setup Dialog** — anchor hue slider, global B mode/notch, per-material pattern pickers, live EEVEE tile previews, contact sheet, preset save/load
- **Startup Template** — bake your preferences into a startup blend so every new file is ready to go

---

## Requirements

- Blender 5.1+
- EEVEE only — no Cycles dependency

---

## Installation

1. Download the latest zip from [Releases](../../releases)
2. In Blender: Edit → Preferences → Add-ons → Install from Disk
3. Select the zip — enable **FBX Mapper's Toolkit**
4. Open the 3D Viewport, press **N**, select the **FBX Toolkit** tab

> **Note:** Preferences are in the N-panel, not Edit → Preferences. The Add-ons preferences entry shows a redirect message explaining this.

---

## File structure

```
fbx_mappers_toolkit/
├── __init__.py           # Registration
├── blender_manifest.toml
├── fbx_import.py         # Import operators and full prep pipeline
├── handlers.py           # load_post handler — chain material init, template detection
├── materials.py          # Material operators, island system, node tree builder
├── op.py                 # Export operator, UV order enforcement, material tile render
├── panel.py              # All panels and UILists
├── project_setup.py      # Project Setup dialog, EEVEE tile renderer, preset system
├── props.py              # FBXMT_GlobalPrefs and FBXMT_Props PropertyGroups
├── template.py           # Startup template generator
├── uv_pack.py            # MaxRects BSSF packer
└── uv_unwrap.py          # Core unwrap logic, projection, stitching
```

---

## Where preferences are stored

Preferences live on the Scene as `PointerProperty` groups — not in `AddonPreferences`. This sidesteps Blender 5.x extension package prefix issues and means preferences persist with the blend file and in the startup template.

- `scene.fbxmt_prefs_global` → addon-wide settings (checker appearance, colours, patterns, import defaults)
- `scene.fbxmt_props` → per-scene settings (export path, texel density, lightmap options)

Two settings that must persist *across* files live in true `AddonPreferences`:

- `show_setup_on_new` — whether the Project Setup dialog fires on template load
- `presets_path` — shared folder for team preset JSON files

See `props.py` for full documentation.

---

## Version history

See [CHANGELOG.md](CHANGELOG.md) for full history.

Current version: **2.9.0**

---

## Licence

GPL v3 — see [LICENCE](LICENCE)
