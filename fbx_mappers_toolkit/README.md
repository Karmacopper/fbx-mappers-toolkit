# FBX Mapper's Toolkit — v0.2.40

UV unwrap, material management, import/export, and dihedral trim generation for UE5/UT99 mapping workflows in Blender 5.1+.

---

## What's new in v0.2.40

### Dihedral Trim Generator (trim_gen2) — release milestone

`Generate Trim (Dihedral)` sweeps a 10-vert profile ring along any selected wall/floor, wall/ceiling, wall/wall, or ramp edge run. This release marks the first stable milestone of the system.

#### Profile
10 verts wound clockwise from the seam vert (v0):

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

#### Working cases
- Wall/floor — straight, curved, closed loop
- Wall/ceiling — straight and curved
- Wall/wall — convex and concave 90° corners
- Ramp — floor→ramp→floor runs (any length, any combination)
- Solo ramp edge — correct orientation on any wall axis
- Multi-chain selection — merged into one object, caps welded
- Curved wall/floor runs with large floor ngons — correct continuous mitering
- Auto-export OBJ with incremental counter

#### Known limitations
- 3-edge junction miter faces — topology correct, miter quality not resolved
- Ramp switchback (two ramps ascending in opposite directions sharing a vert) — degenerate, select each ramp separately and they weld at the junction
- Closed platform loop — 3 split-ring gaps at 4-face concave corner and arc transition points (not a typical workflow — individual seam runs are the normal use)

#### Parameters
| Parameter | Description |
|---|---|
| Thickness | Profile height from seam to nose |
| Depth A | Floor/ramp/ceiling arm length |
| Depth B | Wall arm length |
| Chamfer | None / Half / Full |

---

## What's new in v0.2.39 (previous release)

### Colour Modifier System — full overhaul

The V2 colour system has been replaced with a unified notch-driven system.

**Anchor Colour A** — H/S/V notch controls
- Hue: 30° steps (12 positions)
- Saturation: Low / Medium / High
- Value: Darkest / Dark / Mid / Light / Lightest

**Anchor Colour B** — always derived from A
- Hue Offset, Saturation, Value notches
- B colours no longer stored — always computed fresh, eliminating stale tile bug on load

**Ignore material** — hardcoded grey, cannot follow anchor hue.

---

## Installation

1. Download `fbx_mappers_toolkit_v0.2.40.zip`
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
6. Select wall/floor seam edges → **Generate Trim (Dihedral)** to add trim geometry
7. Export via **Export** tab for UE5, or use the engine adapter pipeline

---

## Material classification thresholds

| Angle from horizontal | Assigned material |
|---|---|
| ≤ 15° | Floor / Ceiling |
| 15° – 45° | Ramp Floor / Ramp Ceiling |
| > 45° | Wall |

---

## Known issues / outstanding work

- 3-edge junction miter faces — topology correct, miter quality unresolved
- Closed loop trim — gaps at 4-face concave corner and arc transition verts
- Ramp switchback — degenerate case, select each ramp separately
- Prop rename: `uv_floor_threshold` → `ramp_wall_threshold` (pending)
- `PROFILE_180_FLAT` 10-vert not yet implemented
- Vertex-level generation, sequential generation, face/vertex select modes
- UV unwrap lockout toggle on trim objects
- Engine adapters: interchange format spec, fbxmt-ue5/unity/godot/flax
- Export: UE5 procedural material pipeline, UT99 Play Volume Generator
- Ceiling deco / coving operator — planned
- Beam placement system — planned
- UT99 texture material library generator — planned

---

## Repo

https://github.com/Karmacopper/fbx-mappers-toolkit

Maintainer: Ja5mine  
Licence: GPL-3.0-or-later
