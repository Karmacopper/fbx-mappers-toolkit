# FBX Mapper's Toolkit — v0.2.41

UV unwrap, material management, import/export, dihedral trim generation, ceiling deco (coving), and beam placement for UE5/UT99 mapping workflows in Blender 5.1+.

---

## What's new in v0.2.41

### Ceiling Deco System — coving and beam placement

Two new operators in the **N-panel → FBX Toolkit → Ceiling Deco** section.

#### Generate Coving

Sweeps a 4-vert coving profile (ceiling leg → notch → wall leg → back) along any selected ceiling/wall seam edge run. Supports:

- **Closed loops** — single or multi-mesh selections that form a continuous closed loop
- **Open chains** — partial seam runs with clean start/end caps
- **Bay curves** — concave and convex curved walls with correct dihedral miters at every facet
- **Bay/straight wall junctions** — automatic miter termination where bay curve meets straight wall
- **Multi-mesh selections** — edges from two or more mesh objects are combined into one world edge graph; coincident verts from different meshes (within 1 cm) are merged automatically
- **Cross-mesh T-junctions** — where an InnerWall seam endpoint lies on an OuterWall edge mid-span, the sanitiser splits the host edge and re-chains the combined graph per source mesh
- **Auto-export OBJ** — incremental counter, exports coving mesh + seam wire reference

| Parameter | Description | Default |
|---|---|---|
| Depth | How far the profile extends DOWN the wall from the seam | 0.25 m |
| Thickness | How far the profile extends ALONG the ceiling from the seam | 0.15 m |
| Notch H | Horizontal notch fraction — 0.5 = rectangle, 0 = triangle, 1 = kite | 0.5 |
| Notch V | Vertical notch fraction — 0.5 = rectangle, 0 = triangle, 1 = kite | 0.5 |

**Profile shape** (4 verts per seam vert, wound consistently):

```
ceiling ──── v1 (seam + h_arm × thickness)
              │
             v2 (notch vert — notch_h/notch_v controlled)
              │
wall    ──── v3 (seam + wall_down × depth)
              │
back    ──── sv (seam vert on wall surface)
```

**Bay/straight junction handling:** At the transition from a curved bay wall to a straight return wall, the system detects the junction (c_h < 0.5, edge length ratio > 3) and snaps the adjacent ring's v1 to the junction miter point. This closes the gap between the last bay ceiling arm and the straight wall ceiling arm.

**Known limitations:**
- Cross-mesh T-junctions where InnerWall and OuterWall share a seam but the shared edges are not pre-split by the user — the sanitiser handles this automatically but results may vary on complex geometry. Workaround: manually add a vert at the T-intersection before selecting.

---

#### Generate Beams

Places beam empty pairs across a selected face span for use with the FBXMT beam instancing pipeline.

| Parameter | Description | Default |
|---|---|---|
| Count | Number of beam pairs | 1 |
| Spacing | Place at interval instead of count (0 = use count) | 0 m |
| Horiz Offset | Horizontal offset from centroid line | 0 m |
| Vert Offset | Vertical offset from centroid | 0 m |
| Snap to Face Centre | Snap empties to nearest selected face centre | Off |

---

## What's new in v0.2.40 (previous release)

### Dihedral Trim Generator (trim_gen2) — release milestone

`Generate Trim (Dihedral)` sweeps a 10-vert profile ring along any selected wall/floor, wall/ceiling, wall/wall, or ramp edge run.

#### Profile verts

| Vert | Role |
|---|---|
| v0 | Seam — on the shared edge |
| v1 | Foot A inner — fixed at `depth_a` along face A |
| v2 | Foot A mid — chamfer loop (HALF only) |
| v3 | Foot A outer — stepped back along arm when chamfer active |
| v4 | Chamfer shoulder A |
| v5 | Nose tip — at exactly `thickness` from both face planes |
| v6 | Chamfer shoulder B |
| v7 | Foot B outer |
| v8 | Foot B mid — chamfer loop (HALF only) |
| v9 | Foot B inner — fixed at `depth_b` along face B |

No-chamfer mode collapses to 6 unique positions (v2=v1, v4=v3, v6=v7, v8=v9).

#### Chamfer modes
- **None** — sharp nose, no reinforcement loops
- **Half** — nose flattened, toe chamfer bevel, mid toe loop present
- **Full** — nose flattened, toe chamfer bevel, mid toe loop collapsed

#### Parameters

| Parameter | Description |
|---|---|
| Thickness | Profile height from seam to nose |
| Depth A | Floor/ramp/ceiling arm length |
| Depth B | Wall arm length |
| Chamfer | None / Half / Full |

---

## Installation

1. Download `fbx_mappers_toolkit_v0.2.41.zip`
2. In Blender 5.1+: **Edit → Preferences → Extensions → Install from Disk**
3. Select the zip — do not unzip first
4. Enable the extension if not auto-enabled

---

## Quick start

1. Open the **N-panel → FBX Toolkit** tab
2. Run **Project Setup** to configure materials, UV settings, and anchor colour
3. Import or open your FBX/OBJ geometry
4. Use **Auto-Assign** to classify faces by normal angle
5. Use **UV Unwrap** to apply FBXMT projection per material type
6. Select wall/ceiling seam edges → **Generate Coving** to add coving geometry
7. Select wall/floor seam edges → **Generate Trim (Dihedral)** to add trim geometry
8. Export via **Export** tab for UE5, or use the engine adapter pipeline

---

## Material classification thresholds

| Angle from horizontal | Assigned material |
|---|---|
| ≤ 15° | Floor / Ceiling |
| 15° – 45° | Ramp Floor / Ramp Ceiling |
| > 45° | Wall |

---

## Known issues / outstanding work

- Cross-mesh T-junction coving — sanitiser handles common cases; complex geometry may need manual vert insertion at T-intersection
- 3-edge junction miter faces (trim_gen2) — topology correct, miter quality unresolved
- Ramp switchback — degenerate case, select each ramp separately
- `PROFILE_180_FLAT` 10-vert not yet implemented
- Vertex-level generation, sequential generation, face/vertex select modes
- UV unwrap lockout toggle on trim objects
- Engine adapters: interchange format spec, fbxmt-ue5/unity/godot/flax
- Export: UE5 procedural material pipeline, UT99 Play Volume Generator
- UT99 texture material library generator — planned

---

## Repo

https://github.com/Karmacopper/fbx-mappers-toolkit

Maintainer: Ja5mine  
Licence: GPL-3.0-or-later
