# FBX Mapper's Toolkit — v0.40.0.0 "Unity"

UV unwrap, material management, import/export, and a unified trim-generation
system (beams, coving, curve runs) for UE5/UT99-style mapping workflows in
Blender 5.1+.

---

## What's new — Unity

Unity is the convergence release. Every trim-generation type — Quick Beam,
Parallel Beam, Spoke Beam, Curve Beam, and Coving — now runs through a single
**Trim 2** preview/commit workflow with shared Width/Height/Overrun controls,
a context-aware hint panel, and one consistent Preview → Commit → Room-assign
flow.

- **One panel, one mental model.** Pick a beam type from the dropdown, set
  Width/Height/Start/End, hit Preview, adjust, Commit.
- **Coving migrated to Trim 2**, gaining live preview, Catmull-Rom curve
  smoothing, profile flip controls, and overrun/extrude for open runs.
- **Spoke Beam rebuilt** around an inner/outer arc selection with width-aware
  "Visual" spacing alongside mathematically-"Exact" spacing.
- **Curve Beam rebuilt** with pinned, coplanar endpoints and Catmull-Rom
  resampling so beam segments are evenly spaced regardless of how the source
  faces were subdivided.
- **New: Curve Cleaner.** A standalone Catmull-Rom resampler for any messy
  edge/vert chain — also the engine behind Coving's curve smoothing and Curve
  Beam's even spacing.
- **Legacy Ceiling Deco panel retired** from the UI. The underlying module
  stays installed (Trim 2 still depends on a couple of its internals) but its
  one-shot empty-based workflow is no longer the front door.

See the [wiki](#documentation) for full details on each generator.

---

## Installation

1. Download `fbx_mappers_toolkit_v0.40.0.zip`
2. In Blender 5.1+: **Edit → Preferences → Extensions → Install from Disk**
3. Select the zip — do not unzip first
4. Enable the extension if not auto-enabled

> Blender's extension manifest requires 3-part semver, so the packaged
> manifest version reads `0.40.0`. The addon's own internal version string
> (shown in every panel header and in Preferences) is the full
> `0.40.0.0` — that's the one that tracks actual releases.

---

## Quick start

1. Open the **N-panel → FBX Toolkit** tab
2. Run **Project Setup** to configure materials, UV settings, and anchor colour
3. Import or open your FBX/OBJ geometry
4. Switch to the **FBXMT Trim** tab → **Trim 2** panel
5. Pick a beam type, select your geometry per that type's selection rules
   (see the wiki page for each type), set Width/Height/Start/End
6. **Preview** → adjust any prop live → **Commit**
7. Assign the result to a room when prompted
8. Export via the **Export** tab

---

## Documentation

Full per-feature documentation lives in the project wiki:

- [Trim 2 — Unified Workflow](wiki/Trim-2-Unified-Workflow.md)
- [Quick Beam](wiki/Quick-Beam.md)
- [Parallel Beam](wiki/Parallel-Beam.md)
- [Spoke Beam](wiki/Spoke-Beam.md)
- [Curve Beam](wiki/Curve-Beam.md)
- [Coving](wiki/Coving.md)
- [Curve Cleaner](wiki/Curve-Cleaner.md)
- [Changelog & Migration Notes](wiki/Changelog.md)

---

## Known issues / outstanding work

- `ceiling_deco.py` still backs the room-assignment popup flow and supplies
  one beam-builder helper to Trim 2's Parallel type — full retirement needs
  those extracted into a shared module first.
- The legacy `OT_FBXMT_Generate_*` operators remain registered (scriptable
  via `bpy.ops`) but have no panel entry now that Ceiling Deco is removed.
- `beam_placement.py`'s older direct-placement/gizmo system is untouched and
  still installed alongside Trim 2 — not yet migrated.
- UE5, Unity (engine), Godot, Flax interchange format spec — pending.
- UT99 texture material library generator — planned.

---

## Repo

https://github.com/Karmacopper/fbx-mappers-toolkit

Maintainer: Ja5mine
Licence: GPL-3.0-or-later
