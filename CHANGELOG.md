# Changelog & Migration Notes

## v0.40.0.0 — "Unity"

The convergence release. Coving, Curve Beam, and Spoke Beam join Quick Beam
and Parallel Beam under one Trim 2 panel and one shared props model.

### Coving migrated to Trim 2

- Gained the standard live preview/commit workflow (ghost preview, prop-
  driven re-run, Commit/Cancel) that Quick Beam already had.
- New **Flip Width** / **Flip Height** toggles so the same profile runs
  correctly on an edge regardless of source winding — this was the
  headline new feature for the migration ("the cunning plan").
- New **overrun** support for open chains, using the same shared Start/End
  props as every other type, including unclamped negative values (pull the
  end back) and an **Ext** toggle per end for extruding extra geometry
  rather than just repositioning the end ring.
- New **Smooth Angle** control — automatic Catmull-Rom resampling of
  curved coving sections (see [Coving](Coving.md) for the full mechanics).
  Always active; the angle only tunes sensitivity.
- The underlying geometry algorithm itself (`_build_coving`) was preserved
  verbatim during extraction into the new shared module — flips and
  overrun were additive, not a rewrite of the core sweep.

### Spoke Beam rebuilt

- Old version generated radial spokes from a single face's centre outward
  to a fixed radius — disconnected from any real selection geometry and
  producing broken results on curved surfaces.
- Rebuilt around an **inner/outer arc selection** (two matched face
  chains), with each spoke a straight span crossing the gap between
  corresponding hub and rim positions — using the same builder as Quick
  Beam's 2-face mode.
- New **Visual** vs **Exact** spacing modes, with Visual using a width-
  aware formula so beam edges (not just centres) are evenly spaced
  including the gaps at the arc ends.

### Curve Beam rebuilt

- Old version swept a profile directly along raw face centroids, which
  produced uneven segment spacing on unevenly-subdivided source geometry,
  and (in an earlier broken state during development) could connect
  unrelated face groups with a single rogue long span.
- Rebuilt to: split disconnected selections into independent chains;
  support both single-chain and matched two-chain (midpoint) modes; pin
  start/end points to the actual terminal edge of the source mesh rather
  than a face centroid, so endpoints land exactly coplanar with the
  selection; and resample the path with Catmull-Rom for even segment
  spacing regardless of source topology.
- Correct axis convention settled on: `h_arm = tangent × world_up` (width
  runs tangentially along the surface), `wall_down = −world_up` (height
  drops straight down) — matching the original `ceiling_deco.py` beam
  convention rather than a face-normal-relative one, which was tried and
  found incorrect for curved wall surfaces during development.

### New: Curve Cleaner

- Standalone Catmull-Rom resampling tool, usable on any edge/vert chain
  independent of trim generation. See [Curve Cleaner](Curve-Cleaner.md).
- Shares its resampling engine (`spline_utils.catmull_rom_resample`) with
  Curve Beam and Coving — a fix or improvement there benefits all three.

### Common settings panel unification

- Width, Height, Start, End now live in one common box at the top of Trim
  2, used by every type — previously Coving had its own Depth/Thickness
  naming and overrun props, Parallel had a separately (and confusingly)
  named overrun pair, and Quick Beam's overrun was its own thing again.
- A context-aware hint box explains what Width/Height/Start/End mean for
  the currently selected type, word-wrapped to the N-panel width
  (toggleable via a preference).
- Buttons cleaned up: Preview / Commit / red ✕-only Cancel, no more verbose
  "Preview Trim" / "Commit Trim" labels or oversized rows.

### Legacy Ceiling Deco panel removed

- The old empty-placement-based panel (`FBXMT_PT_CeilingDeco`) is removed
  from the FBXMT Trim tab.
- The underlying `ceiling_deco.py` module is **not** removed — Trim 2's
  commit flow still depends on its room-assignment popup operators, and
  Trim 2's Parallel Beam still calls into one of its beam-builder helpers
  internally. Full retirement is tracked as follow-up work, not done in
  this release.
- The old `OT_FBXMT_Generate_*` one-shot operators remain registered
  (scriptable via `bpy.ops`) but have no panel entry.

## Migration notes for existing scenes/scripts

- If you have scripts or macros calling `bpy.ops.fbxmt.generate_coving`,
  `generate_parallel`, `generate_spokes`, or `generate_curve` directly,
  they still work — those operators weren't removed, only their panel
  buttons.
- Old `coving_depth` / `coving_thickness` props (on `FBXMT_Props`, used by
  the legacy panel) are untouched and still exist — they're simply no
  longer surfaced in any panel. Trim 2's Coving type uses its own
  `width`/`height` (shared) props instead, not these.
- Spoke Beam's old `spoke_radius` prop has been **removed** and replaced
  with `spoke_spacing_mode` (Visual/Exact enum). Any saved `.blend` with
  the old prop value will simply not carry it forward — Spoke Beam's
  selection model changed too fundamentally for a value migration to make
  sense.
- Parallel Beam's previously-misnamed overrun props (`par_end_inset`,
  which actually drove what the panel labelled "Overrun Start") have been
  removed in favour of the shared `overrun_start`/`overrun_end` props.
  `par_first_beam` and `par_start_inset` keep their original meaning
  (lateral first/last beam offset) — only the overrun pair was
  consolidated.

## Known issues carried into this release

- `ceiling_deco.py` dependency not yet extracted (see above).
- `beam_placement.py`'s older gizmo-based direct-placement system is
  untouched and still installed alongside Trim 2.
- UE5/Unity(engine)/Godot/Flax interchange format spec — pending.
- UT99 texture material library generator — planned.
