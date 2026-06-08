# FBX Mapper's Toolkit — v0.39.7

UV unwrap, material management, import/export, dihedral trim generation, ceiling deco (coving), and beam placement for UE5/UT99 mapping workflows in Blender 5.1+.

---

## What's new in v0.39.x — Gizmo System & Parallel Beam Overhaul

### Clear Empties dropdown
Top of the **Ceiling Deco** panel — dropdown menu with:
- **Clear All** — removes every `par_`, `spk_`, `crv_`, `dh_` empty in one shot
- **Clear Parallel / Spokes / Curve / Dihedral** — per-type removal

---

### Gizmo system — all beam types

All generated beam meshes now carry **interactive viewport gizmos** that appear when the beam is the active object. Boolean modifiers are automatically **disabled during drag** so you see the raw beam geometry while positioning, then re-enabled on release with the rebuilt mesh. Hold **Shift** for ×0.1 fine precision on all drags.

---

#### Quick Beam gizmos (`FBXMT_GGT_QuickBeam`)

| Gizmo | Colour | Function |
|---|---|---|
| End arrows ×2 | 🟠 Orange | `qb_overrun_start` / `qb_overrun_end` — drag outward to extend beam past wall |

Props stored on beam object: `fbxmt_qb_anchors`

---

#### Dihedral Beam gizmos (`FBXMT_GGT_DihedralBeam`)

| Gizmo | Colour | Function |
|---|---|---|
| End arrows ×2 | 🟠 Orange | `dh_overrun_start` / `dh_overrun_end` |
| Mid double-cone | 🟢 Green | `dh_offset` — bisector offset, slides beam laterally across the dihedral |

Mid gizmo points along the edge tangent direction.

---

#### Parallel Beam gizmos

Parallel beams use **two gizmo groups** — one on each beam mesh, one on the group empty.

##### Per-beam gizmos (`FBXMT_GGT_ParallelBeam`)
Active when a `par_NNN_Beam` mesh is selected:

| Gizmo | Colour | Function |
|---|---|---|
| End arrows ×2 | 🟠 Orange | `par_overrun_start` / `par_overrun_end` — extend past walls |
| Wall inset arrows ×2 | 🟣 Purple | `par_inset_start` / `par_inset_end` — how deep beam penetrates wall for boolean clearance |
| Lateral arrows ×2 | 🔵 Blue | `par_offset_lat` — side/side positioning, **grid-snapped** (hidden on first/last beam in group) |

##### Group empty gizmos (`FBXMT_GGT_ParallelGroup`)
Active when the `par_grp_NNN` empty is selected:

| Gizmo | Colour | Function |
|---|---|---|
| Span inset arrows ×2 | ⬜ White | `par_inset_start` / `par_inset_end` — shrink the beam zone from each end of the selection. Grid-snapped. Triggers full group regeneration on release. |
| Vertical arrows ×2 | 🟢 Green | `par_offset_v` — vertical offset for whole group. Rebuilds all child beam meshes in place. |

Grid snap reads `grid_scale / grid_subdivisions` from the active 3D viewport (default ~10 cm with standard Blender grid).

##### Group empty architecture
On **Generate Parallel**, a `par_grp_NNN` empty is created at the face midpoint. All beam meshes are parented to it using `matrix_parent_inverse` to preserve world position. Moving the group empty moves all beams as a unit. The empty stores the full chain/anchor data needed for regeneration.

---

### Parallel beam ray preview — 4-corner profile casting

The ray preview now fires **4 rays per anchor**, one from each corner of the beam profile cross-section (`±coving_depth/2` lateral, `±coving_thickness/2` vertical). This shows:

- **4 red lines** — the actual beam corners travelling wall-to-wall
- **Pink quad outlines** — the beam profile rectangle projected onto both the anchor face and the hit face (end caps)

The end cap rectangles immediately reveal if the beam profile will clip into a corner, pilaster, or adjacent geometry before generation. Preview cache invalidates automatically when `coving_depth` or `coving_thickness` props change.

---

## Previous features

### Quick Beam
Generates a single beam between two clicked points on a mesh surface. Boolean modifier applied against source mesh. Overrun controls how far the beam extends into the wall for a clean cut.

### Dihedral Beam
Generates a beam along a selected edge dihedral (wall/floor, wall/ceiling etc). Ray-cast finds the opposite wall automatically. Bisector offset slides the beam laterally.

### Parallel Beams
Places evenly-spaced beams across a face strip selection. Smart ray-cast navigates concave corners and curved geometry to find the opposite wall. Live update when placement props change.

### Spoke Beams
Beams radiate from hub face group to rim face group. Hub/rim auto-detected by face count.

### Curve Beams
Beams follow the midpoint arc between two face strip selections. Mitered at every interior ring.

---

## Installation

1. Download `fbx_mappers_toolkit_v0.39.7.zip`
2. In Blender 5.1+: **Edit → Preferences → Extensions → Install from Disk**
3. Select the zip — do not unzip first
4. Enable the extension if not auto-enabled

---

## Quick start

1. Open the **N-panel → FBX Toolkit** tab
2. Run **Project Setup** to configure materials, UV settings, and anchor colour
3. Import or open your FBX/OBJ geometry
4. Use **Auto-Assign** to classify faces by normal angle
5. Select wall/ceiling seam edges → **Generate Coving**
6. Select wall/floor seam edges → **Generate Trim (Dihedral)**
7. Select face strips → **Place Parallel** → **Preview Rays** → **Generate Parallel**
8. Export via **Export** tab

---

## Props reference (new in v0.39.x)

| Prop | Default | Description |
|---|---|---|
| `qb_overrun_start` | 0.25 m | Quick Beam start overrun |
| `qb_overrun_end` | 0.25 m | Quick Beam end overrun |
| `dh_overrun_start` | 0.25 m | Dihedral start overrun |
| `dh_overrun_end` | 0.25 m | Dihedral end overrun |
| `dh_offset` | 0.0 m | Dihedral lateral bisector offset |
| `par_overrun_start` | 0.25 m | Parallel beam start overrun |
| `par_overrun_end` | 0.25 m | Parallel beam end overrun |
| `par_inset_start` | 0.0 m | Span inset from selection start |
| `par_inset_end` | 0.0 m | Span inset from selection end |
| `par_offset_v` | 0.0 m | Parallel group vertical offset |
| `par_offset_lat` | 0.0 m | Per-beam lateral offset |

---

## Known issues / outstanding work

- Span inset white gizmos: orientation relative to beam direction may need tuning per scene
- Curve beam STRIP/RADIUS modes less tested than PAIR mode
- Dihedral ray may miss in some extreme convex corner cases
- `PROFILE_180_FLAT` 10-vert not yet implemented
- Engine adapters: UE5, Unity, Godot, Flax interchange format spec pending
- UT99 texture material library generator — planned

---

## Repo

https://github.com/Karmacopper/fbx-mappers-toolkit

Maintainer: Ja5mine
Licence: GPL-3.0-or-later
