FBX Mapper's Toolkit v2.8.0
For Blender 5.1+ - Unreal Engine 5 Map Geometry Workflow

Original exporter by Ja5mine (2021)
Rebuilt and extended by Claude (2026)

------------------------------------------------------------------------
OVERVIEW
------------------------------------------------------------------------

A workflow toolkit for preparing and exporting map geometry from Blender
to Unreal Engine 5. Handles material assignment, UV unwrapping, lightmap
generation, UCX collision and FBX export in a streamlined N-shelf panel.

Designed for map geometry. Props need their own UV love.

THE CORE IDEA

If you used UnrealEd 1/2 (UT99, Quake-era editors) or UE's BSP tools,
the UV approach here will feel immediately familiar. Those editors
assigned textures per-face based on the face's world-space normal, kept
the vertical axis locked to world Z so wall textures always read upright,
and scaled to world units so density was consistent without any manual
work. You just painted surfaces and it looked right.

This toolkit does exactly that - but instead of runtime projection, it
bakes the result into proper UV islands packed into an atlas. The artist
experience is the same: assign a surface type, run the unwrap, done.
The output is a static mesh UV map that UE5's Lumen, Nanite, and
lightmass pipelines can use correctly.

The island marker system is the one addition that has no UnrealEd
equivalent. BSP never needed to think about island boundaries because
it never packed anything. The island marker is how you tell the unwrapper
where one UV island ends and the next begins - for the cases where long
wall runs or curved sections need splitting for packing economy.
Everything else is automated.

See UV UNWRAPPING PHILOSOPHY for the full technical detail.

------------------------------------------------------------------------
INSTALLATION
------------------------------------------------------------------------

Edit > Preferences > Add-ons > Install from Disk -> select the .zip
Find "FBX Mapper's Toolkit" in the list and enable it.
Panel appears in the N-shelf (press N in 3D viewport) under "FBX Toolkit".

------------------------------------------------------------------------
WORKFLOW
------------------------------------------------------------------------

1. SCENE SETUP (first time)
   - Hit "Setup Scene" - creates Geo, Props, Trim collections and adds
     all M_FBXMT materials to the blend
   - Set your Geo Texel Density (default 1024 texels/m)
   - Configure material colours and checker patterns in the Materials
     panel (see MATERIAL COLOURS AND PATTERNS below)

2. IMPORT (optional)
   - Use the Import dropdown to bring in FBX files
   - Import as Geo: adds M_FBXMT material slots, moves to Geo collection
   - Import as Trim: adds slots + assigns M_FBXMT_Trim, moves to Trim
   - Import as Prop: imports as-is, moves to Props collection
   - Set a default Import Folder in the Import panel to pre-fill the
     file browser on Quick Import

3. MATERIAL ASSIGNMENT
   - In Edit mode, select faces you want to exclude -> assign M_FBXMT_Ignore
   - Hit "Auto-Assign to Faces" - floors, ceilings and walls assigned
     automatically by world-space normal direction
   - Ignore and island-marked faces are never overwritten by auto-assign

4. ISLAND MARKING (optional - see UV UNWRAPPING PHILOSOPHY)
   - Only required for meshes with long wall runs or curved sections that
     need manual island splitting
   - Select the faces you want as one island in Edit mode
   - Assign M_FBXMT_Island to those faces
   - Auto-colouring fires immediately on assign - adjacent islands are
     automatically assigned distinct hidden sub-materials so the unwrapper
     can tell them apart. No manual numbering required.
   - Hit "Auto-Colour Islands" in the panel at any time to reprocess

5. UV UNWRAP
   - Select objects in Object mode
   - Hit "Unwrap Selected Objects"
   - Or enter Edit mode, select specific faces, hit "Unwrap Selected Faces"
   - Faces with no M_FBXMT material are skipped (UVs left untouched)
   - Unwrap is disabled in Edit Mode at the object level to prevent
     materials appearing black during the operation

6. PROJECT SETUP (optional)
   - Open Project Setup from the Scene Setup panel
   - Bake All generates 128px preview tiles for all 6 materials
   - Contact Sheet composites all tiles into a single reference image
     and saves it to MaterialCache/ alongside the blend file

7. EXPORT
   - Set export folder in the Scene Setup panel (saved per blend file)
   - Tick UCX Collision if needed
   - Lightmap is guaranteed - created if missing, regenerated if ticked
   - UV channel order is enforced: 0 = diffuse, 1 = LightmapUVs
   - Hit "Export Selected"
   - On export, island sub-materials are replaced by surface-detected
     base materials (Wall/Floor/Ceiling) before the FBX write, then
     stripped. Island markers never ship in the exported file.
   - Full-resolution texture bake to Textures/ runs on export

STARTUP TEMPLATE
   - Set up your scene as desired (colours, patterns, texel density,
     export folder)
   - Hit "Save Startup Template" in the Scene Setup panel
   - Restart Blender
   - "FBX Mapper Toolkit" appears under File > New and in the splash screen

------------------------------------------------------------------------
MATERIALS
------------------------------------------------------------------------

BASE MATERIALS (always present, auto-assigned by normal direction)

  M_FBXMT_Floor    - horizontal upward-facing surfaces
  M_FBXMT_Ceiling  - horizontal downward-facing surfaces
  M_FBXMT_Wall     - vertical surfaces
  M_FBXMT_Trim     - edge detail, wear-stoppers
  M_FBXMT_Ignore   - excluded from unwrap entirely

All base materials use procedural node-based shaders - no texture files.
The checker pattern, corner cross markers, scale, colours and patterns
are all configurable per-material and applied on Rebuild. No PNG assets.

ISLAND MARKER MATERIAL

  M_FBXMT_Island   - visible marker painted by the artist

  M_FBXMT_Island_01 through M_FBXMT_Island_15 - hidden sub-materials
  assigned automatically by the graph colourer. Never visible in the
  panel, never baked, never exported.

Island markers always display with Wall Colour A and incrementally darker
grey B values per island, giving immediate visual feedback on island
boundaries. The Colour A for island materials is always derived from
Wall Colour A - they are wall-type surfaces by definition.

On export, all island sub-material faces are re-detected by face normal
and assigned the appropriate base material before the FBX write.

------------------------------------------------------------------------
MATERIAL COLOURS AND PATTERNS
------------------------------------------------------------------------

Each of the 6 visible materials has its own colour and pattern settings,
accessible by selecting a material in the Materials list in the N-panel.
Settings appear in a box below the list. Hit "Update Material" to
rebuild that material's node tree.

CHECKER PATTERN

  Square    - standard checkerboard (default)
  Diagonal  - each square bisected diagonally, A and B colour the triangles
  Diamond   - each square split into 4 triangles (N/S = A, E/W = B),
              producing a diamond/argyle appearance

All three patterns use the same A/B colour pair and the same corner
marker system. Pattern is set per material giving three independent axes
of visual differentiation: hue, lightness, and pattern shape.

COLOUR A

Always a free colour picker. No constraints.
Exception: Island Marker Colour A always tracks Wall Colour A.

COLOUR B MODE

  Manual         - free colour picker, no processing applied
  Lighter/Darker - 7-position slider. Centre (position 4) = same
                   lightness as A. Positions 1-3 = darker, 5-7 = lighter.
  Greyscale      - 5-position slider: Black / 25% / 50% / 75% / White.
  Inverse        - complementary hue of A, computed, no control needed.

CORNER MARKERS

Cross markers appear at texel tile corners (not checker square corners).

  Corner Mark Length    - arm length as % of tile (4 presets: 12.5-50%)
  Corner Mark Width     - arm width in pixels at 1024tx/m density
  Show Corner Circle    - quarter-circle arc at each corner
  Corner Line Hue Shift - hue rotation on marker colour (default 180 =
                          inverted checker for max contrast)

------------------------------------------------------------------------
UV UNWRAPPING PHILOSOPHY
------------------------------------------------------------------------

The unwrap approach in this toolkit is specifically designed for game-
level architecture and differs significantly from general-purpose UV
unwrapping tools.

THE CORE PROBLEM

A typical game environment wall mesh might contain 200 or more vertical
faces arranged as a continuous perimeter - straight runs, corners, and
curved sections all connected. A naive unwrap produces either one
enormous island or arbitrary cuts that break texture continuity.

THE APPROACH

  Floors and ceilings  - projected from world Z, preserving real-world
                         scale and alignment to world X/Y axes.

  Walls and trim       - projected per face from its own normal, with
                         world Z locked as the UV vertical axis. Wall
                         UVs always read upright regardless of direction.
                         Faces stitched edge-to-edge into strips before
                         packing.

  Island-marked faces  - treated identically to walls in projection,
                         but island sub-material boundaries are hard
                         island boundaries. Two adjacent faces with
                         different sub-materials are always separate UV
                         islands even if they share an edge.

WHY THE ISLAND MARKER INSTEAD OF SEAMS

Blender seams are per-edge, easy to place accidentally, difficult to
audit at scale, and invisible at a glance. For a 200-face wall loop,
managing seams is fragile.

The island marker solves this differently. The material assigned to a
face is immediately visible in the viewport, auditable in the material
list, and persistent across mesh edits. The boundary between islands is
explicit - you are not cutting, you are categorising. Auto-colouring
means you never need to think about which number goes where - paint the
marker, the system handles the rest.

HOW AUTO-COLOURING WORKS

When M_FBXMT_Island is assigned to faces, the system immediately:
1. Finds all connected groups of island-marked faces (components)
2. Builds an adjacency graph between components
3. Assigns hidden sub-materials (Island_01-15) by greedy graph colouring
   so no two adjacent components share the same sub-material
4. Existing island components from previous runs are respected and not
   recoloured - only newly marked faces get assigned

The four colour theorem guarantees four colours suffice for any planar
quad mesh. Fifteen sub-materials provide headroom for complex geometry
and non-planar cases.

WHEN TO USE ISLAND MARKING

Not every mesh needs it. Simple box rooms, flat wall segments, and any
geometry where the auto-unwrap produces acceptable results do not need
island marking.

Island marking is for:

  - A curved wall section that must unroll as a single continuous strip
  - A long straight run needing a deliberate break point for packing
  - Any section where the artistic intent requires a specific island
    boundary that cannot be inferred from geometry alone

------------------------------------------------------------------------
UV PACKING
------------------------------------------------------------------------

Islands are packed using the Maximal Rectangles algorithm (Best Short
Side Fit heuristic). No rotation is ever applied - wall face Z-up
orientation is preserved at all times. UV packing margin is 0.0 for
seamless tiling. UVs may extend beyond 1.0 on either axis.

Algorithm reference:
  Jylänki, J. (2010). "A Thousand Ways to Pack the Bin."
  http://clb.demon.fi/files/RectangleBinPack.pdf

The implementation in uv_pack.py is an original Python implementation.
No code from any existing library was copied or adapted.

------------------------------------------------------------------------
UCX COLLISION
------------------------------------------------------------------------

When "Generate UCX Collision" is ticked, a copy of each exported mesh
is included in the FBX prefixed with UCX_. UE5 detects this
automatically as custom convex collision.

------------------------------------------------------------------------
LIGHTMAP
------------------------------------------------------------------------

Every export guarantees a LightmapUVs channel exists. With the toggle
off, existing lightmaps are preserved. With the toggle on, a fresh
lightmap is generated. UV channel order enforced on export: channel 0 =
diffuse, channel 1 = LightmapUVs.

------------------------------------------------------------------------
PREFERENCES AND PROPERTY STORAGE
------------------------------------------------------------------------

Preferences are in the N-panel, not Edit > Preferences.

One exception: "Show Project Setup on New Project" lives in true
AddonPreferences because it must persist across blend files.

WHERE THE DATA LIVES

  Scene.fbxmt_prefs_global  (FBXMT_GlobalPrefs in props.py)
    Checker appearance, per-material colours, patterns, colour B modes.

  Scene.fbxmt_props  (FBXMT_Props in props.py)
    Export path, texel density, FBX scale options, lightmap behaviour,
    import path, Project Setup state.

------------------------------------------------------------------------
KNOWN LIMITATIONS
------------------------------------------------------------------------

  - "Show Model" button in Project Setup is hidden. The preview render
    operator exists but template_image cannot display outside the Image
    Editor context.

  - The eyedropper in the Project Setup dialog samples tile preview
    icons if they are under the cursor. Use the N-panel colour pickers
    for eyedropper work, or enter values numerically.

  - Contact sheet save requires the blend file to have been saved first.

------------------------------------------------------------------------
THIRD-PARTY ATTRIBUTIONS
------------------------------------------------------------------------

UV Packing Algorithm
  Original implementation of the algorithm described in:
    Jylänki, Jukka (2010). "A Thousand Ways to Pack the Bin."
    Available at: http://clb.demon.fi/files/RectangleBinPack.pdf
  No code from the reference implementation was used.

All other code is original work licensed under GPL v3.

------------------------------------------------------------------------
CHANGELOG
------------------------------------------------------------------------

2.8.0  Feature: Project Setup dialog overhauled. Tile preview uses fast
       pure-Python/numpy renderer — no Cycles, near-instant. Tiles are
       clickable material selectors with highlight on active tile.
       Contact sheet updated to 3x2 layout for 6 visible materials.
       Feature: Floor/Ceiling/Wall tiles show split view — top half
       standard checker, bottom half island B stepping (inverted A colour
       as centre, ±25%/±50% lightness steps).
       Feature: Material preset system — save/load/delete named JSON
       presets, team-shareable via configurable folder path in
       AddonPreferences.
       Feature: Apply B / Apply B to All split button in Project Setup.
       Feature: UV Preview mesh — builds flat mesh from UVMap coordinates
       in UV_Preview collection, enters local view automatically.
       Feature: UVPreview UV channel — scaled-to-fit copy of UVMap,
       created on every unwrap, stripped on export. Open UV Editor and
       select UVPreview to see full layout within 0-1 space.
       Feature: Checker scale now power-of-2 button row (1,2,4,8,16,32).
       Feature: Island auto-colouring fires on assign, deferred via timer
       to avoid edit-mode bmesh conflict.
       Feature: Bare Island Marker faces route to wall unwrap path.
       Fix: Update Tile cache hash skip removed — was silently doing
       nothing after first run.
       Fix: Contact sheet bilinear scale artefact — CELL_SIZE now matches
       PREVIEW_SIZE, no scaling required.
       Fix: Island B step cycling now per-square not per-pixel.

2.7.0  Feature: Island marker system replaces 5 chain materials.
       M_FBXMT_Island is the single visible marker the artist assigns.
       15 hidden sub-materials (M_FBXMT_Island_01-15) are assigned
       automatically by adjacency graph colouring — no manual numbering.
       Auto-colouring fires on assign; existing islands respected on
       re-runs. Island Colour A always tracks Wall Colour A. On export,
       island faces are surface-detected and replaced with base materials
       before FBX write; island slots stripped.
       Feature: Per-material checker patterns (Square / Diagonal /
       Diamond). Diagonal bisects each square into two triangles.
       Diamond produces a diamond/argyle pattern. Per material.
       Feature: Per-material Colour B mode (Manual / Lighter-Darker 7-
       notch / Greyscale 5-notch / Inverse). Derived at node-build time.
       Feature: Project Setup bake is preview-only (128px). Full-res
       bake to Textures/ is export-only.
       Feature: N-panel per-material colour and pattern controls.
       Fix: template_list crash (EXCEPTION_ACCESS_VIOLATION) — prop
       mutation during draw re-enters layout system. Stale index now
       clamped in load_post handler.
       Fix: Contact sheet missing function, double-save, image buffer
       errors on on-the-fly bake.
       Fix: BakeAllModal was stripping all materials from scene.
       Fix: Dead colour callbacks removed.

2.6.x  See previous release notes for 2.6.x history.
