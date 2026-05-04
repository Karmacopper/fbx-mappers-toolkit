FBX Mapper's Toolkit v2.5.6
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

The island chain system is the one addition that has no UnrealEd
equivalent. BSP never needed to think about island boundaries because
it never packed anything. Chains are how you tell the unwrapper where
one UV island ends and the next begins - for the cases where long wall
runs or curved sections need splitting for packing economy. Everything
else is automated.

See UV UNWRAPPING PHILOSOPHY for the full technical detail.

------------------------------------------------------------------------
INSTALLATION
------------------------------------------------------------------------

Edit > Preferences > Add-ons > Install from Disk → select the .zip
Find "FBX Mapper's Toolkit" in the list and enable it.
Panel appears in the N-shelf (press N in 3D viewport) under "FBX Toolkit".

------------------------------------------------------------------------
WORKFLOW
------------------------------------------------------------------------

1. SCENE SETUP (first time)
   - Hit "Setup Scene" - creates Geo, Props, Trim collections and adds
     all M_FBXMT materials to the blend
   - Set your Geo Texel Density (default 1024 texels/m)
   - Enable "Auto-Add Materials to New Objects" if desired

2. IMPORT (optional)
   - Use the Import dropdown to bring in FBX files
   - Import as Geo: adds M_FBXMT material slots, moves to Geo collection
   - Import as Trim: adds slots + assigns M_FBXMT_Trim, moves to Trim
   - Import as Prop: imports as-is, moves to Props collection
   - Import and Ask: per-file dialog for mixed batches

3. MATERIAL ASSIGNMENT
   - In Edit mode, select faces you want to exclude → assign M_FBXMT_Ignore
   - Hit "Auto-Assign to Faces" - floors, ceilings and walls assigned
     automatically by world-space normal direction
   - Ignore and chain-marked faces are never overwritten by auto-assign

4. ISLAND CHAIN MARKING (optional - see UV UNWRAPPING PHILOSOPHY)
   - Only required for meshes with long wall runs or curved sections that
     need manual island splitting
   - With a mesh object active, hit "+" in the Island Chain Materials list
     to generate M_FBXMT_Chain_01, _02, etc.
   - In Edit mode, select the faces you want as one island and assign the
     appropriate chain material
   - Chain_01 is always present and locked - it is the baseline island
     marker. Additional chains split the run into separate UV islands.

5. UV UNWRAP
   - Select objects in Object mode
   - Hit "Unwrap Selected Objects"
   - Or enter Edit mode, select specific faces, hit "Unwrap Selected Faces"
   - Faces with no M_FBXMT material are skipped (UVs left untouched)

6. EXPORT
   - Set export folder in the Scene Setup panel (saved per blend file)
   - Tick UCX Collision if needed
   - Lightmap is guaranteed - created if missing, regenerated if ticked
   - UV channel order is enforced: 0 = diffuse, 1 = LightmapUVs
   - A sanity check runs before export - objects with chain materials
     but missing Chain_01 in their slots will trigger a warning
   - Hit "Export Selected"

STARTUP TEMPLATE
   - Set up your scene as desired (colours, texel density, export folder)
   - Hit "Save Startup Template" in the Scene Setup panel
   - Restart Blender
   - "FBX Mapper Toolkit" appears under File > New and in the splash screen
   - The template captures all per-scene preferences (checker colours,
     workflow defaults) since they are stored on the Scene

------------------------------------------------------------------------
MATERIALS
------------------------------------------------------------------------

BASE MATERIALS (always present, auto-assigned by normal direction)

  M_FBXMT_Floor    - horizontal upward-facing surfaces (green checker)
  M_FBXMT_Ceiling  - horizontal downward-facing surfaces (blue checker)
  M_FBXMT_Wall     - vertical surfaces (amber checker)
  M_FBXMT_Trim     - edge detail, wear-stoppers (lilac checker)
  M_FBXMT_Ignore   - excluded from unwrap entirely (grey checker)

All base materials use procedural node-based checkerboard shaders -
no texture files. The checker pattern, corner cross markers, scale,
and colours are all configurable in the Preferences panel and applied
on Rebuild. There are no PNG assets to replace.

ISLAND CHAIN MATERIALS (user-generated, optional)

  M_FBXMT_Chain_01 - locked baseline island marker (blue+orange checker)
  M_FBXMT_Chain_02
  M_FBXMT_Chain_NN - additional chain differentiators (blue+chosen colour)

Chain materials are procedural checkerboards generated from the Materials
panel. Each uses the same blue tile paired with a user-chosen colour B
(lightness normalised to match the blue tile for visual consistency).
Chain_01 is always present and cannot be deleted - it is the foundation
of the island system. Additional chains are generated on demand and
numbered sequentially, with gaps filled before the sequence extends.

------------------------------------------------------------------------
UV UNWRAPPING PHILOSOPHY
------------------------------------------------------------------------

The unwrap approach in this toolkit is specifically designed for game-
level architecture and differs significantly from general-purpose UV
unwrapping tools. Understanding the intent is essential to using it
correctly.

THE CORE PROBLEM

A typical game environment wall mesh might contain 200 or more vertical
faces arranged as a continuous perimeter - straight runs, corners, and
curved sections all connected. A naive unwrap produces either one
enormous island (wasteful, awkward to texture) or arbitrary cuts that
break texture continuity. Neither is acceptable for a shipping level.

THE APPROACH

The toolkit separates faces into categories by purpose, then applies the
most appropriate projection to each:

  Floors and ceilings  - projected from world Z, preserving real-world
                         scale and alignment to world X/Y axes.

  Walls and trim       - projected per face from its own normal, with
                         world Z locked as the UV vertical axis. This
                         means wall UVs always read upright regardless
                         of which direction the wall faces. Faces are
                         then stitched edge-to-edge into contiguous
                         strips before packing.

  Island chain faces   - treated identically to walls in projection
                         terms, but the chain material number acts as
                         a hard island boundary. Two adjacent faces
                         with different chain numbers are always
                         separate UV islands, even if they share an
                         edge. Within the same chain number,
                         connectivity determines islands - disconnected
                         geometry becomes separate islands naturally.

WHY CHAINS INSTEAD OF SEAMS

Blender seams define where a mesh can be cut. They are a per-edge
property that is easy to place accidentally, difficult to audit at
scale, and invisible at a glance when reviewing many objects. For a
200-face wall loop with 40 curves, managing seams is fragile.

Chain materials solve this differently. The material assigned to a face
is immediately visible in the viewport, auditable in the material list,
and persistent across mesh edits. Selecting all faces with a given
material is a single operation. The boundary between chain numbers is
explicit and intentional - you are not cutting, you are categorising.

WHEN TO USE CHAIN MATERIALS

Not every mesh needs them. Simple box rooms, flat wall segments, and any
geometry where the auto-unwrap produces acceptable results do not require
chain marking. Do not add chain slots to an object unless you intend to
use them - Chain_01 only needs to be in a mesh's material slots if you
are actively marking island chains on that mesh.

Chain marking is for the cases the automation cannot solve:

  - A curved wall section that must unroll as a single continuous strip
    to avoid texture pinching at face boundaries

  - A long straight run that would produce an impractically wide UV
    island if left as one piece, needing a deliberate break point for
    packing economy

  - Any section where the artistic intent requires a specific island
    boundary that cannot be inferred from geometry alone

In all these cases, the artist makes a deliberate decision about where
one island ends and the next begins, marks it with a chain number, and
the unwrapper respects that decision exactly. This is the part of the
UV workflow that cannot be fully automated - the toolkit handles
everything it can automatically and gives you a clean, auditable system
for the rest.

CHAIN MATERIAL DISCIPLINE

Chain_01 is the baseline. It marks faces that belong to the island
chain system but do not need to be separated from their connected
neighbours - they will still split naturally at geometry boundaries.

Chain_02, _03, and so on are break points. A face assigned Chain_02
will never share a UV island with an adjacent face assigned Chain_01,
even if they share an edge. Use additional chain numbers wherever you
need a deliberate island split that geometry alone does not provide.

The number itself carries no semantic meaning beyond ordering. Chain_02
is not "more important" than Chain_01. It is simply "different from
Chain_01 and therefore a separate island."

For a simple linear wall run, two chain numbers are sufficient -
alternate _01 and _02 along the strip and no island touches its
neighbour. For more complex geometry with T-junctions, corners where
multiple runs meet, or faces with more than two chain-marked neighbours,
you need enough distinct numbers that no two adjacent chain faces share
the same one. For quad meshes this is bounded by the four colour theorem
- four chain numbers are sufficient to guarantee no chain material is
adjacent to itself across any junction in any planar quad mesh.
A fifth gives you headroom for deliberate double-breaks where packing
economy demands it rather than adjacency. Beyond five, the geometry
is probably complex enough to warrant splitting into separate objects.

SANITY CHECKING

The export operator checks for the following condition before exporting:
any object that has chain materials assigned to faces but is missing
Chain_01 from its material slots will be flagged with a warning. This
condition produces incorrect UV islands - the unwrapper uses material
boundaries to determine islands, so a missing chain slot means faces
that should be in separate islands may be merged, or faces may be
assigned to the wrong UV region.

If you receive this warning, add Chain_01 to the object's material slots
(use "Add to Active Object" in the Materials panel with Chain_01
selected), then re-run the unwrap.

------------------------------------------------------------------------
UV PACKING
------------------------------------------------------------------------

Islands are packed using the Maximal Rectangles algorithm (Best Short
Side Fit heuristic). The bin width is estimated by trying several
candidate aspect ratios and choosing whichever produces output closest
to square. No rotation is ever applied - wall face Z-up orientation is
preserved at all times.

UVs may extend beyond 1.0 on either axis. UE5 handles this correctly
for tiling textures. Overlaps are never produced within a single object.

Algorithm reference:
  Jylänki, J. (2010). "A Thousand Ways to Pack the Bin - A Practical
  Approach to Two-Dimensional Rectangle Bin Packing."
  http://clb.demon.fi/files/RectangleBinPack.pdf

The implementation in uv_pack.py is an original Python implementation
of the algorithm described in that paper. No code from any existing
library was copied or adapted.

------------------------------------------------------------------------
UCX COLLISION
------------------------------------------------------------------------

When "Generate UCX Collision" is ticked, a copy of each exported mesh
is included in the FBX prefixed with UCX_. UE5 detects this
automatically as custom convex collision - no setup needed in UE.

------------------------------------------------------------------------
LIGHTMAP
------------------------------------------------------------------------

Every export guarantees a LightmapUVs channel exists. With the toggle
off, existing lightmaps are preserved. With the toggle on, a fresh
lightmap is always generated using Blender's built-in lightmap pack.

UV channel order is enforced on export: channel 0 = diffuse, channel 1
= LightmapUVs, as UE5 expects.

------------------------------------------------------------------------
PREFERENCES AND PROPERTY STORAGE
------------------------------------------------------------------------

Preferences are in the N-panel, not Edit > Preferences.

Open the 3D Viewport, press N, select the FBX Toolkit tab, expand
Preferences. Edit > Preferences > Add-ons will show the addon entry
but its panel is empty by design - it just points here.

WHY NOT AddonPreferences?

Blender 5.x extensions load under a prefixed package name at runtime
(e.g. bl_ext.user_default.fbx_mappers_toolkit). AddonPreferences
requires bl_idname to match the package name exactly, and that name
differs between a local source install and a packaged extension. Storing
preferences on the Scene as PointerProperties sidesteps this entirely.

WHERE THE DATA LIVES

  Scene.fbxmt_prefs_global  (FBXMT_GlobalPrefs in props.py)
    Addon-wide settings: checker appearance, material colours, import
    workflow defaults. These are the same across a project - set once
    in your startup template and leave them.

  Scene.fbxmt_props  (FBXMT_Props in props.py)
    Per-scene operational values: export path, texel density, FBX scale
    options, lightmap behaviour. These vary between projects and are
    intentionally per-blend-file.

Both PropertyGroups are defined in props.py, registered on bpy.types.Scene
in __init__.py, and imported by panel.py for display. If you need to access
them in code: context.scene.fbxmt_prefs_global and context.scene.fbxmt_props.

STARTUP TEMPLATE

Use Save Startup Template in the Scene Setup panel to bake the current
preferences into a startup .blend. From that point on, every new file
inherits your preferred checker scale, texel density, colours and
export settings without any manual setup.

------------------------------------------------------------------------
THIRD-PARTY ATTRIBUTIONS
------------------------------------------------------------------------

UV Packing Algorithm
  The MaxRects / Best Short Side Fit bin packing algorithm used in
  uv_pack.py is an original implementation of the algorithm described in:

    Jylänki, Jukka (2010). "A Thousand Ways to Pack the Bin - A
    Practical Approach to Two-Dimensional Rectangle Bin Packing."
    Available at: http://clb.demon.fi/files/RectangleBinPack.pdf

  No code from Jylänki's C++ reference implementation (RectangleBinPack)
  or any derived library was used. The paper is not licensed software;
  citation is provided for academic credit only.

All other code in this addon is original work licensed under GPL v3.

------------------------------------------------------------------------
CHANGELOG
------------------------------------------------------------------------

2.5.6  Blender 5.1 node API fixes: ShaderNodeMixRGB replaced with
       ShaderNodeMix (RGBA), ShaderNodeInvert replaced with DIFFERENCE
       blend against white. math/bmesh/Vector promoted to module-level
       imports in materials.py.

2.4.x  Major refactor and review pass (2.4.0-2.4.4):
       props.py extracted from panel.py - FBXMT_GlobalPrefs and
       FBXMT_Props now have a dedicated file with full documentation.
       Dead imports, stale constants, unnecessary aliases, empty
       AddonPreferences panel, _ask_index reset bug, chain-push-to-all-
       meshes, _enforce_uv_order O(layers) bmesh passes all fixed.
       Em dash encoding fixed for Windows console output.

2.3.x  Checker system rebuilt (2.3.0-2.3.9):
       Two independent mapping paths: checker scale and tile corner
       markers fully decoupled. Tile corners at texel tile boundaries
       (geo_texel_density/1024), not at checker squares. Cross arms use
       colour invert for maximum contrast. A+B colour pickers for all
       base materials. checker_scale changed from EnumProperty to
       IntProperty to fix Blender extension reload bug.

2.2.x  Material panel rebuilt (2.2.0-2.2.9):
       Surface Materials and Island Materials as two object-scoped
       UILists with cross-list deselection. Assign and Select face
       operators. Texel density consolidated to Scene Setup with live
       tile size readout. Dead UV density props removed.

2.1.x  Initial reviewed build (2.1.5-2.1.9):
       PNG files removed, manifest fixed for Blender 5.1, dead imports
       cleaned, no-op depsgraph handler unregistered, LightmapUVs
       protected from removal.
