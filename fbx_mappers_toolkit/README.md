# FBX Mapper's Toolkit — v0.25.1

UV unwrap, material management, import/export, dihedral trim generation, ceiling deco (coving), and beam placement for UE5/UT99 mapping workflows in Blender 5.1+.

---

## What's new in v0.25.1

### Ceiling Deco — Beam Generation system (complete)

Three independent beam generation workflows, each with Place → Generate pipeline:

#### Spoke Beams (`spk_NNN_1/2`)
Beams radiate from the smaller (hub) face group toward the larger (rim) face group. Hub/rim auto-detected by face count. Optional fixed spoke length and grow-from-both-ends mode.

| Prop | Description |
|---|---|
| Count | Number of spoke pairs |
| Spacing | Arc-length interval instead of count (0 = use count) |
| Vert Offset | Vertical shift on all empties |
| Spoke Length | Fixed length (0 = face-to-face distance) |
| Grow From Both Ends | When length set, empties grow inward from both ends |

#### Parallel Beams (`par_NNN_1`)
Single face strip selection. Places `_1` empties along the strip storing the face normal. At Generate time a smart ray-cast fires along each stored normal to find `_2` — the ray passes through edge-on faces (parallel to the ray direction) and terminates at the first genuinely perpendicular face, correctly navigating concave corners, convex steps, and curved geometry.

**Ray preview** — Preview Rays button fires the ray-cast and draws the path (including pass-through segments) and terminus dot as a red overlay in all 3D viewports. Works in both Edit and Object Mode. Auto-updates when placement settings change.

| Prop | Description |
|---|---|
| Count | Number of anchor empties |
| Spacing | Arc-length interval instead of count |
| Inset Start | Offset first empty from start edge (negative = outward) |
| Inset End | Offset last empty from end edge |
| Vert Offset | Vertical shift |

**Live update** — changing any parallel prop while `par_NNN_1` empties exist in the scene automatically clears and re-places them using the stored chain data. No need to re-enter Edit Mode.

#### Curve Beams (`crv_NNN_1/2`)
Beams follow the midpoint arc between two face strip selections. Ring count derived from geometry (one ring per vert pair) — no manual count needed. Segments are mitered at every interior ring for clean joins.

| Prop | Description |
|---|---|
| Inset Start | Arc-length offset from start end (0 = full selection) |
| Inset End | Arc-length offset from end |
| Vert Offset | Vertical shift |
| Depth (V) | Profile drop |
| Thickness (H) | Profile width |

#### Shared generation features
- **Boolean trim** — each generated beam gets an `FBXMT_BoolTrim` Boolean Difference modifier referencing the source mesh (stored at Place time). Left unapplied for fine-tuning — apply manually when satisfied.
- **Overrun** — beam ends extend 0.25m beyond the empty position so the boolean has geometry to cut cleanly.
- **Source mesh stored per-empty** — supports multi-mesh scenes; each empty carries `fbxmt_source` so different beams can boolean against different objects.
- **OBJ export** — geometry + orphaned vert markers exported to the main export folder on Generate.
- **Coving normals** — generated coving now has correctly outward-facing normals (required for ray-cast and boolean operations).
- **Empty display** — all placement empties are red SPHERE type with names shown in viewport.

#### Arc measurement
All three systems measure arc length by walking the actual boundary vert chain (not face centroids), deduplicated to one vert per unique XY position at the strip midplane Z. Equal-interval sampling uses `(i+1)/(n+1)` fractions for consistent margins.

---

## What's new in v0.2.43 (previous release)

### Beam Placement — face-anchored algorithm rewrite

The beam placement operator has been rewritten to produce correct results on both straight and curved geometry.

**Old behaviour:** Beams were distributed along the vector between the two face group centroids, producing a daisy-chain of sub-spans that piled up in the middle on any non-trivial count.

**New behaviour:**

- Each face group is walked in adjacency order (flood-fill), giving a topologically consistent ordering along arc strips or straight runs.
- N sample positions are drawn evenly along each group's ordered face-centre sequence using fractional interpolation — smooth even when face count is coarse.
- Sample `i` from group A → `_1` empty; sample `i` from group B → `_2` empty. Pairs are anchored independently to their respective face groups.
- On **straight geometry** beams run parallel (each pair spans A→B at the same angle).
- On **curved geometry** beams spoke naturally — no explicit mode detection needed, the face-centre sampling handles it.
- **Spacing mode** now measures arc length along group A's face-centre sequence and derives count from that, rather than from the straight-line centroid distance.
- **Collision check** now tests newly placed pairs against pre-existing pairs only (not new pairs against each other), eliminating false positives on dense curved placements.
- `beam_offset_h` (horizontal centroid offset) removed from the panel — superseded by the face-anchored approach. The prop remains registered so existing `.blend` files are not broken.
- Live pair count readout added to the Beam Placement panel box.

| Parameter | Description | Default |
|---|---|---|
| Count | Number of beam pairs to place | 1 |
| Spacing | Arc-length interval along face group A instead of count (0 = use count) | 0 m |
| Vert Offset | Vertical shift applied to all empties | 0 m |

After placement, if an export folder is set in Project Setup, a `beam_empties_NNN.obj` is written automatically to that folder. Each beam pair is an `o` object entry containing two point vertices (one per empty) in OBJ coordinate space. NNN auto-increments so repeated placements never overwrite a previous export.

---

## What's new in v0.2.41 (previous release)

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

1. Download `fbx_mappers_toolkit_v0.25.1.zip`
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
