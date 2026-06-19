# trim2.py — FBX Mapper's Toolkit
#
# "FBXMT Trim 2" — two-pass beam generation with live ghost preview.
#
# Architecture recap
# ──────────────────
#  1. The user selects faces in Object mode, tweaks settings in the panel,
#     and clicks "Preview".
#  2. FBXMT_OT_Trim2_Preview builds a bmesh via trim_geometry, converts it to
#     a real mesh object called FBXMT_Preview, assigns a transparent ghost
#     material, and links it into a dedicated FBXMT_Preview collection.
#  3. Every BeamSettings property carries an update= callback.  When a value
#     changes while preview_active is True, the callback schedules a timer
#     (0 s delay) which re-runs the Preview operator in EXEC_DEFAULT — no
#     manual re-click needed.
#  4. "Commit" renames the preview object to its final name, swaps the ghost
#     material for M_FBXMT_Trim, moves it to the Trim collection, and runs
#     the boolean modifier pipeline.
#  5. "Cancel" removes the FBXMT_Preview object/collection and resets state.
#
# File structure
# ──────────────
#  FBXMT_Trim2Props        — PropertyGroup registered on bpy.types.Scene
#  FBXMT_OT_Trim2_Preview  — builds / refreshes ghost mesh
#  FBXMT_OT_Trim2_Commit   — promotes ghost to final real object
#  FBXMT_OT_Trim2_Cancel   — tears down preview
#  FBXMT_PT_Trim2          — N-panel panel
#  register() / unregister()

from __future__ import annotations

import math

import bpy
import bmesh
from mathutils import Vector
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import (
    BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty,
)

from .trim_geometry import BeamSettings, BEAM_BUILDERS, get_selected_edge_indices, get_selected_vert_indices
from . import harry as _harry


# ──────────────────────────────────────────────────────────────────────────────
# Internal constants
# ──────────────────────────────────────────────────────────────────────────────

_PREVIEW_OBJ_NAME  = 'FBXMT_Preview'
_PREVIEW_COL_NAME  = 'FBXMT_Preview'
_GHOST_MAT_NAME    = 'M_FBXMT_Preview_Ghost'
_TRIM_COL_NAME     = 'Trim'

# Guard: prevents update callbacks re-entering themselves
_refreshing = False


# ──────────────────────────────────────────────────────────────────────────────
# Ghost material
# ──────────────────────────────────────────────────────────────────────────────

def _ensure_ghost_material() -> bpy.types.Material:
    """Return (creating if needed) the ghost preview material."""
    # Remove stale version to pick up any changes
    old = bpy.data.materials.get(_GHOST_MAT_NAME)
    if old and old.use_nodes:
        nt = old.node_tree
        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        # If already correct (no blend_method set), reuse it
        if bsdf and not getattr(old, 'blend_method', None):
            return old
        bpy.data.materials.remove(old)

    mat = bpy.data.materials.new(_GHOST_MAT_NAME)
    mat.use_nodes            = True
    mat.use_backface_culling = False

    nt = mat.node_tree
    nt.nodes.clear()

    out  = nt.nodes.new('ShaderNodeOutputMaterial')
    bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
    out.location  = (300, 0)
    bsdf.location = (0, 0)

    bsdf.inputs['Base Color'].default_value = (0.0, 0.82, 1.0, 1.0)  # cyan
    bsdf.inputs['Alpha'].default_value      = 0.5
    bsdf.inputs['Roughness'].default_value  = 0.6

    nt.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    return mat


# ──────────────────────────────────────────────────────────────────────────────
# Preview collection / object helpers
# ──────────────────────────────────────────────────────────────────────────────

def _ensure_preview_collection() -> bpy.types.Collection:
    col = bpy.data.collections.get(_PREVIEW_COL_NAME)
    if col is None:
        col = bpy.data.collections.new(_PREVIEW_COL_NAME)
        bpy.context.scene.collection.children.link(col)
    return col


def _update_preview_object(bm: bmesh.types.BMesh) -> bpy.types.Object:
    """Replace the mesh data on the shared FBXMT_Preview object in-place.

    In-place swap avoids flickering and keeps the object's transform/parent
    intact across refreshes.  If the object does not exist yet it is created.
    """
    ghost_mat = _ensure_ghost_material()

    prev_obj = bpy.data.objects.get(_PREVIEW_OBJ_NAME)
    if prev_obj is None:
        new_mesh         = bpy.data.meshes.new(_PREVIEW_OBJ_NAME)
        prev_obj         = bpy.data.objects.new(_PREVIEW_OBJ_NAME, new_mesh)
        col              = _ensure_preview_collection()
        col.objects.link(prev_obj)
        # Make it non-selectable so accidental clicks do not break the workflow
        prev_obj.hide_select = True

    # Replace mesh data without removing the object
    old_mesh = prev_obj.data
    tmp_mesh = bpy.data.meshes.new(_PREVIEW_OBJ_NAME + '_tmp')
    bm.to_mesh(tmp_mesh)
    tmp_mesh.update()
    prev_obj.data = tmp_mesh

    # Remove old mesh only if nothing else references it
    if old_mesh and old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)
    tmp_mesh.name = _PREVIEW_OBJ_NAME

    # Ensure ghost material is assigned (slot 0 only)
    if len(tmp_mesh.materials) == 0 or tmp_mesh.materials[0] != ghost_mat:
        tmp_mesh.materials.clear()
        tmp_mesh.materials.append(ghost_mat)

    # Viewport display: show in front so it is visible through solid geometry
    prev_obj.show_in_front   = False
    prev_obj.color           = (0.0, 0.82, 1.0, 1.0)  # solid cyan in solid mode
    prev_obj.display_type    = 'SOLID'

    return prev_obj


def _destroy_preview() -> None:
    """Remove the FBXMT_Preview object, its mesh data, and the preview collection."""
    prev_obj = bpy.data.objects.get(_PREVIEW_OBJ_NAME)
    if prev_obj is not None:
        mesh = prev_obj.data
        bpy.data.objects.remove(prev_obj, do_unlink=True)
        if mesh and mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    col = bpy.data.collections.get(_PREVIEW_COL_NAME)
    if col is not None and len(col.objects) == 0:
        bpy.data.collections.remove(col)


def _tag_redraw() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ──────────────────────────────────────────────────────────────────────────────
# Auto-refresh on property change
# ──────────────────────────────────────────────────────────────────────────────

def _wrap_hint_text(text, context, padding_px=42, glyph_px=8.0):
    """Word-wrap text to fit the current N-panel width.

    Deliberately conservative: Blender's label() will silently truncate
    with an ellipsis if a line is even slightly too wide for its column,
    which looks worse than wrapping a touch early. So this undershoots
    the estimated character budget rather than risk native truncation.

    padding_px / glyph_px tuned empirically against Blender's default UI
    font at common N-panel widths (padding_px=42, glyph_px=8.0).
    """
    if not text:
        return []

    region_w  = getattr(context.region, 'width', 0) or 300
    usable_px = max(region_w - padding_px, 60)
    max_chars = max(int(usable_px / max(glyph_px, 0.1)), 8)

    words = text.split(' ')
    lines = []
    current = ''
    for word in words:
        candidate = f'{current} {word}'.strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _preview_prop_update(self, context):
    """update= callback wired to every BeamSettings-derived property.

    When preview_active is True, schedule an immediate timer to re-run the
    Preview operator.  The timer defers execution to a safe Blender context
    (outside the draw / property-write stack) to avoid re-entrant depsgraph
    issues.
    """
    global _refreshing
    if _refreshing:
        return
    if not context.scene.fbxmt_trim2.preview_active:
        return
    if bpy.data.objects.get(_PREVIEW_OBJ_NAME) is None:
        return
    bpy.app.timers.register(_scheduled_refresh, first_interval=0.0)


def _scheduled_refresh():
    """Timer callback — runs outside the property-write stack."""
    global _refreshing
    if _refreshing:
        return None  # already running

    ctx_override = _find_view3d_context()
    if ctx_override is None:
        return None

    _refreshing = True
    try:
        with ctx_override:
            bpy.ops.fbxmt.trim2_preview('EXEC_DEFAULT')
    except Exception as e:
        print(f'[FBXMT Trim2] Auto-refresh failed: {e}')
    finally:
        _refreshing = False

    return None  # do not repeat


def _find_view3d_context():
    """Return a temp_override context suitable for VIEW_3D operators, or None."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        return bpy.context.temp_override(
                            window=window, area=area, region=region,
                        )
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Settings helpers
# ──────────────────────────────────────────────────────────────────────────────

def _props_to_settings(props) -> BeamSettings:
    """Convert the scene PropertyGroup into a BeamSettings dataclass."""
    return BeamSettings(
        width             = props.width,
        height            = props.height,
        cap_ends          = props.cap_ends,
        par_count         = props.par_count,
        par_spacing       = props.par_spacing,
        par_placement     = props.par_placement,
        par_first_beam    = props.par_start_inset,   # lateral: first beam offset
        par_start_inset   = props.overrun_start,     # beam end overrun — start
        par_end_inset     = props.overrun_end,       # beam end overrun — end
        par_end_clamp     = props.par_end_clamp,     # lateral: last beam offset
        dihedral_angle_offset = props.dihedral_angle_offset,
        quick_raycast_iters   = props.quick_raycast_iters,
        quick_overrun_start   = props.overrun_start,
        quick_overrun_end     = props.overrun_end,
        spoke_count       = props.spoke_count,
        spoke_spacing_mode = props.spoke_spacing_mode,
        curve_segments    = props.curve_segments,
    )


def _get_selected_face_indices(obj: bpy.types.Object):
    """Return face indices currently selected on obj — works in Object and Edit mode."""
    from .trim_geometry import get_selected_face_indices
    return get_selected_face_indices(obj)


# ──────────────────────────────────────────────────────────────────────────────
# PropertyGroup
# ──────────────────────────────────────────────────────────────────────────────

class FBXMT_Trim2Props(PropertyGroup):
    """All Trim 2 settings.  Registered on bpy.types.Scene as fbxmt_trim2."""

    # ── Workflow state ────────────────────────────────────────────────────────
    preview_active: BoolProperty(
        name        = "Preview Active",
        description = "True while a ghost preview object is live",
        default     = False,
    )
    source_obj_name: StringProperty(
        name        = "Source Object",
        description = "Name of the object that was active when Preview was run",
        default     = "",
    )

    # ── Beam type ─────────────────────────────────────────────────────────────
    beam_type: EnumProperty(
        name  = "Beam Type",
        items = [
            ('QUICK',    "Quick Beam",    "Edge: dihedral bisector  |  Face: normal raycast  |  Pair: hard span", 'EDGESEL', 0),
            ('PARALLEL', "Parallel Beam", "Twin beams, one each side of centre-line",  'ALIGN_FLUSH', 2),
            ('SPOKE',    "Spoke Beam",    "Radial spokes from face centre",            'ORIENTATION_NORMAL', 3),
            ('CURVE',    "Curve Beam",    "Beam swept along a face-chain arc",         'CURVE_PATH', 4),
            ('COVE',     "Cove Run",      "Sweep coving profile along selected edges", 'MOD_SMOOTH', 5),
        ],
        default = 'QUICK',
        update  = _preview_prop_update,
    )

    # ── Cross-section ─────────────────────────────────────────────────────────
    width: FloatProperty(
        name        = "Width",
        description = "Beam cross-section width (metres)",
        default     = 0.10,
        min         = 0.001,
        max         = 2.0,
        unit        = 'LENGTH',
        step        = 1,
        precision   = 3,
        update      = _preview_prop_update,
    )
    height: FloatProperty(
        name        = "Height",
        description = "How far the beam profile stands proud of the surface (metres). "
                      "This is the cross-section height — the dimension perpendicular "
                      "to both the beam's run axis and its width",
        default     = 0.10,
        min         = 0.001,
        max         = 2.0,
        unit        = 'LENGTH',
        step        = 1,
        precision   = 3,
        update      = _preview_prop_update,
    )
    cap_ends: BoolProperty(
        name        = "Cap Ends",
        description = "Close the start and end faces of the beam",
        default     = True,
        update      = _preview_prop_update,
    )
    drive_through: BoolProperty(
        name        = 'Drive Through',
        description = 'ON — raycast travels until it finds the guide object, '
                      'ignoring obstacles in between (capped at scene bounds). '
                      'OFF — raycast stops at first hit (default)',
        default     = False,
        update      = _preview_prop_update,
    )

    # ── Quick beam — face mode raycast ────────────────────────────────────────
    quick_raycast_iters: IntProperty(
        name        = 'Surfaces',
        description = 'How many surfaces the raycast pierces before stopping. '
                      '1 = stops at first hit, 2 = drives through one surface, etc.',
        default     = 1,
        min         = 1,
        max         = 8,
        update      = _preview_prop_update,
    )

    # ── Shared overrun (all trim types) ───────────────────────────────────────
    overrun_start: FloatProperty(
        name        = 'Start',
        description = 'How far the trim extends past the run start. '
                      'Positive extends outward, negative pulls back',
        default     = 0.02,
        unit        = 'LENGTH',
        step        = 1,
        precision   = 3,
        update      = _preview_prop_update,
    )
    overrun_end: FloatProperty(
        name        = 'End',
        description = 'How far the trim extends past the run end. '
                      'Positive extends outward, negative pulls back',
        default     = 0.02,
        unit        = 'LENGTH',
        step        = 1,
        precision   = 3,
        update      = _preview_prop_update,
    )
    extrude_start: BoolProperty(
        name        = 'Ext',
        description = 'Extrude the start end ring outward rather than dragging verts — '
                      'gives extra geometry to hand-finish awkward junctions. '
                      'Only meaningful when Start overrun > 0',
        default     = False,
        update      = _preview_prop_update,
    )
    extrude_end: BoolProperty(
        name        = 'Ext',
        description = 'Extrude the end ring outward rather than dragging verts — '
                      'gives extra geometry to hand-finish awkward junctions. '
                      'Only meaningful when End overrun > 0',
        default     = False,
        update      = _preview_prop_update,
    )
    dihedral_angle_offset: FloatProperty(
        name        = 'Angle Offset',
        description = 'Rotate the beam away from the corner bisector. '
                      'Clamped to keep the beam between the two faces',
        default     = 0.0,
        min         = -math.pi,
        max         =  math.pi,
        subtype     = 'ANGLE',
        update      = _preview_prop_update,
    )
    dihedral_half_angle: FloatProperty(
        name    = 'Half Angle',
        default = math.pi / 4,
        options = {'HIDDEN'},
    )

    # ── Parallel ──────────────────────────────────────────────────────────────
    par_start_inset: FloatProperty(
        name        = "First Beam Offset",
        description = "Lateral offset of the first beam from the span start anchor edge. "
                      "Positive moves it toward the other beams",
        default     = 0.05,
        min         = -10.0,
        max         = 10.0,
        unit        = 'LENGTH',
        step        = 1,
        precision   = 3,
        update      = _preview_prop_update,
    )
    par_first_beam: FloatProperty(
        name        = "Last Beam Offset",
        description = "Lateral offset of the last beam from the span end anchor edge. "
                      "Positive moves it toward the other beams",
        default     = 0.05,
        min         = -10.0,
        max         = 10.0,
        unit        = 'LENGTH',
        step        = 1,
        precision   = 3,
        update      = _preview_prop_update,
    )
    par_count: IntProperty(
        name        = "Count",
        description = "Maximum number of beams (beams past End Clamp are dropped)",
        default     = 2,
        min         = 1,
        max         = 32,
        update      = _preview_prop_update,
    )
    par_spacing: FloatProperty(
        name        = "Spacing",
        description = "Centre-to-centre distance between beams. 0 = distribute Count beams evenly",
        default     = 0.30,
        min         = 0.0,
        max         = 5.0,
        unit        = 'LENGTH',
        step        = 1,
        precision   = 3,
        update      = _preview_prop_update,
    )
    par_end_clamp: FloatProperty(
        name        = "Last Beam Offset",
        description = "Offset of the last beam from the span end edge — mirrors First Beam Offset",
        default     = 0.05,
        min         = 0.0,
        max         = 10.0,
        unit        = 'LENGTH',
        step        = 1,
        precision   = 3,
        update      = _preview_prop_update,
    )
    par_placement: bpy.props.EnumProperty(
        name        = 'Placement Method',
        description = 'How beams are distributed across the span',
        items       = [
            ('DEFAULT',  'Default',  'Use First/Last Beam Offset and Spacing/Count settings'),
            ('CENTRED',  'Centred',  'Ignore spacing — distribute Count beams evenly across the full span'),
        ],
        default     = 'DEFAULT',
        update      = _preview_prop_update,
    )

    # ── Cove run — type-specific only (W/H/overrun come from common props) ────
    cove_chamfer: EnumProperty(
        name        = "Chamfer",
        description = "Bevel the inner corner of the coving profile",
        items       = [
            ('NONE', "None", "Sharp inner corner"),
            ('HALF', "Half", "Subtle chamfer — 25% of smallest dimension"),
            ('FULL', "Full", "Prominent chamfer — 50% of smallest dimension"),
        ],
        default     = 'NONE',
        update      = _preview_prop_update,
    )
    cove_flip_width: BoolProperty(
        name        = "Flip",
        description = "Mirror the ceiling/width leg to the opposite side of the seam edge",
        default     = False,
        update      = _preview_prop_update,
    )
    cove_flip_height: BoolProperty(
        name        = "Flip",
        description = "Mirror the wall/height leg to the opposite direction",
        default     = False,
        update      = _preview_prop_update,
    )
    cove_smooth_angle: FloatProperty(
        name        = "Smooth Angle",
        description = "Edge loops on curved coving sections are resampled with "
                      "Catmull-Rom spacing. Sections where adjacent edges deviate "
                      "by LESS than this angle are treated as curves and resampled; "
                      "sharp corners above this angle are used as anchors and left alone",
        default     = 6.0,
        min         = 0.0,
        max         = 180.0,
        step        = 10,
        precision   = 1,
        update      = _preview_prop_update,
    )

    # ── Spoke ─────────────────────────────────────────────────────────────────
    spoke_count: IntProperty(
        name        = "Spoke Count",
        description = "Number of radial spokes running from the inner (hub) "
                      "selection to the outer (rim) selection",
        default     = 4,
        min         = 1,
        max         = 32,
        update      = _preview_prop_update,
    )
    spoke_spacing_mode: EnumProperty(
        name        = "Spacing",
        description = "How spokes are distributed along the hub arc",
        items       = [
            ('VISUAL', "Visual", "Width-aware spacing — treats the arc as N beams "
                        "plus N+1 equal gaps, so end gaps match inter-beam gaps"),
            ('EXACT',  "Exact",  "Spokes at mathematically even fractions "
                        "of the arc (1/(N+1), 2/(N+1), ...), ignoring beam width"),
        ],
        default     = 'VISUAL',
        update      = _preview_prop_update,
    )

    # ── Curve ─────────────────────────────────────────────────────────────────
    curve_segments: IntProperty(
        name        = "Segments",
        description = "Subdivisions per span along the curve beam",
        default     = 8,
        min         = 1,
        max         = 32,
        update      = _preview_prop_update,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Cove preview builder — collects edit-mode edge selection, builds world_bm,
# runs harry.build_cove_run, returns a caller-owned BMesh (or None on failure).
# ──────────────────────────────────────────────────────────────────────────────

def _build_cove_preview(obj, props, op):
    """Build cove geometry from current edit-mode edge selection.

    Returns (bmesh, sel_label) on success or (None, '') on failure.
    op is the calling Operator (for self.report).
    """
    import bpy as _bpy
    import bmesh as _bmesh
    from mathutils import Vector as _V

    if _bpy.context.mode != 'EDIT_MESH':
        op.report({'WARNING'}, 'Cove: switch to Edit Mode and select edges first')
        return None, ''

    # Collect selected edges across all edit-mode objects
    edit_objs = list(getattr(_bpy.context, 'objects_in_mode', None) or
                     [o for o in _bpy.context.selected_objects if o.type == 'MESH'])
    if obj and obj not in edit_objs:
        edit_objs.insert(0, obj)

    SNAP = 1e-4
    def _snap_key(co):
        return (round(co[0] / SNAP), round(co[1] / SNAP), round(co[2] / SNAP))

    world_bm     = _bmesh.new()
    pos_to_wv    = {}
    world_edges  = []
    edge_normals = []
    edge_obj_tags = []
    total_sel    = 0

    for obj_idx, src_obj in enumerate(edit_objs):
        if src_obj is None or src_obj.type != 'MESH':
            continue
        mw  = src_obj.matrix_world
        nm  = mw.to_3x3().normalized()
        src_bm = _bmesh.from_edit_mesh(src_obj.data)
        src_bm.verts.ensure_lookup_table()
        src_bm.edges.ensure_lookup_table()
        sel_edges = [e for e in src_bm.edges if e.select]
        if not sel_edges:
            continue
        total_sel += len(sel_edges)
        for e in sel_edges:
            co_a = tuple(mw @ e.verts[0].co)
            co_b = tuple(mw @ e.verts[1].co)
            norms = [(nm @ f.normal.normalized()).normalized() for f in e.link_faces]
            ka, kb = _snap_key(co_a), _snap_key(co_b)
            if ka not in pos_to_wv:
                pos_to_wv[ka] = world_bm.verts.new(_V(co_a))
            if kb not in pos_to_wv:
                pos_to_wv[kb] = world_bm.verts.new(_V(co_b))
            wva, wvb = pos_to_wv[ka], pos_to_wv[kb]
            try:
                we = world_bm.edges.new((wva, wvb))
                world_edges.append(we)
                edge_normals.append(norms)
                edge_obj_tags.append(obj_idx)
            except Exception:
                existing = next(
                    (ex for ex in wva.link_edges if wvb in ex.verts), None)
                if existing is not None:
                    for i, we_i in enumerate(world_edges):
                        if we_i is not None and id(we_i) == id(existing):
                            edge_normals[i] = edge_normals[i] + norms
                            break
                world_edges.append(None)
                edge_normals.append(norms)
                edge_obj_tags.append(obj_idx)

    if total_sel == 0:
        world_bm.free()
        op.report({'WARNING'}, 'Cove: no edges selected — select edges in Edit Mode first')
        return None, ''

    face_normals_pre = {id(we): norms
                        for we, norms in zip(world_edges, edge_normals)
                        if we is not None}
    seam_edges_clean = [we for we in world_edges if we is not None]

    world_bm.normal_update()

    # Merge coincident verts
    _vert_norms = {}
    for _e in list(world_bm.edges):
        _en = face_normals_pre.get(id(_e), [])
        for _v in _e.verts:
            _k = (round(_v.co.x, 3), round(_v.co.y, 3), round(_v.co.z, 3))
            _vert_norms.setdefault(_k, []).extend(_en)
    _bmesh.ops.remove_doubles(world_bm, verts=list(world_bm.verts), dist=0.01)

    selected_edges   = list(world_bm.edges)
    face_normals_map = {}
    for e in selected_edges:
        norms = face_normals_pre.get(id(e))
        if norms:
            face_normals_map[id(e)] = norms
        else:
            combined = []
            for _v in e.verts:
                _k = (round(_v.co.x, 3), round(_v.co.y, 3), round(_v.co.z, 3))
                combined.extend(_vert_norms.get(_k, []))
            face_normals_map[id(e)] = combined

    if not selected_edges:
        world_bm.free()
        op.report({'WARNING'}, 'Cove: no valid edges after merge')
        return None, ''

    chains, closed_flags = _harry.chain_edges(selected_edges)

    # If any open chains exist, attempt T-junction sanitisation per-object
    _needs_sanitise = any(not cf for cf in closed_flags)
    if _needs_sanitise:
        sel_e, face_normals_map, split_host_map = \
            _harry.sanitise_t_junctions(world_bm, selected_edges, face_normals_map)
        selected_edges = sel_e
        n_objs = max(edge_obj_tags) + 1 if edge_obj_tags else 1
        edge_id_to_tag = {id(we): tag for we, tag in zip(world_edges, edge_obj_tags)
                          if we is not None}
        def _get_tag(eid):
            if eid in split_host_map:
                return edge_id_to_tag.get(split_host_map[eid], 0)
            return edge_id_to_tag.get(eid, 0)
        obj_edge_sets = [[] for _ in range(n_objs)]
        for e in selected_edges:
            obj_edge_sets[_get_tag(id(e))].append(e)
        chains = []; closed_flags = []
        for obj_edges_i in obj_edge_sets:
            if not obj_edges_i:
                continue
            c, cf = _harry.chain_edges(obj_edges_i)
            chains.extend(c)
            closed_flags.extend(cf)

    if not chains:
        world_bm.free()
        op.report({'WARNING'}, 'Cove: could not build edge chains')
        return None, ''

    # Z-coplanarity check
    valid_chains  = []
    valid_closed  = []
    skipped       = 0
    for chain, is_closed in zip(chains, closed_flags):
        ok, z_ref, max_dev = _harry.chain_z_ok(chain, is_closed)
        if ok:
            valid_chains.append(chain)
            valid_closed.append(is_closed)
        else:
            skipped += 1
            op.report({'WARNING'},
                       f'Cove: chain skipped — seam verts not coplanar in Z '
                       f'(max deviation {max_dev:.4f} m, ref Z {z_ref:.4f} m).')

    if not valid_chains:
        world_bm.free()
        op.report({'WARNING'}, 'Cove: all chains failed Z coplanarity check')
        return None, ''

    cov_bm = _bmesh.new()
    all_v2_pairs = []
    for chain, is_closed in zip(valid_chains, valid_closed):
        v2_pairs = _harry.build_cove_run(
            cov_bm, chain, is_closed,
            depth            = props.height,
            thickness        = props.width,
            chamfer          = props.cove_chamfer,
            face_normals     = face_normals_map,
            flip_depth       = props.cove_flip_height,
            flip_thickness   = props.cove_flip_width,
            overrun_start    = props.overrun_start if not is_closed else 0.0,
            overrun_end      = props.overrun_end   if not is_closed else 0.0,
            smooth_angle_deg = props.cove_smooth_angle,
        )
        if v2_pairs:
            all_v2_pairs.extend(v2_pairs)

    world_bm.free()

    # Apply chamfer bevel on v2 inner-corner edges only, using bmesh.ops.bevel
    # with a custom point profile (.5, .5 vector handles) and 1 segment.
    if props.cove_chamfer != 'NONE' and all_v2_pairs:
        def _edge_between_bm(va, vb):
            for e in va.link_edges:
                if vb in e.verts:
                    return e
            return None

        bevel_edges = []
        for va, vb in all_v2_pairs:
            if va.is_valid and vb.is_valid:
                e = _edge_between_bm(va, vb)
                if e is not None and e.is_valid:
                    bevel_edges.append(e)

        if bevel_edges:
            bevel_width = min(props.width, props.height) * \
                          (0.25 if props.cove_chamfer == 'HALF' else 0.50)
            _bmesh.ops.bevel(
                cov_bm,
                geom            = bevel_edges,
                offset          = bevel_width,
                offset_type     = 'OFFSET',
                segments        = 1,
                profile         = 0.5,
                affect          = 'EDGES',
            )
            cov_bm.normal_update()

    n_chains = len(valid_chains)
    suffix   = f', {skipped} skipped' if skipped else ''
    return cov_bm, f'{n_chains} chain(s){suffix}'


# ──────────────────────────────────────────────────────────────────────────────
# Operator: Preview
# ──────────────────────────────────────────────────────────────────────────────

class FBXMT_OT_Trim2_Reset_Parallel(bpy.types.Operator):
    """Reset parallel beam settings to sensible defaults."""
    bl_idname  = 'fbxmt.trim2_reset_parallel'
    bl_label   = 'Reset Parallel Defaults'
    bl_options = {'INTERNAL', 'UNDO'}

    def execute(self, context):
        props = context.scene.fbxmt_trim2
        props.par_count       = 2
        props.par_spacing     = 0.0
        props.par_start_inset = 0.05
        props.par_end_clamp   = 0.05
        props.par_end_inset   = 0.02
        props.par_first_beam  = 0.02
        props.par_placement   = 'DEFAULT'
        return {'FINISHED'}


class FBXMT_OT_Trim2_Preview(Operator):
    bl_idname   = 'fbxmt.trim2_preview'
    bl_label    = 'Preview Beam'
    bl_options  = {'REGISTER', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False
        if context.mode not in ('OBJECT', 'EDIT_MESH'):
            return False
        # Disable entirely in vert select mode — no consistent cast axis possible
        if context.mode == 'EDIT_MESH':
            props = context.scene.fbxmt_trim2
            if props.beam_type == 'QUICK':
                sm = context.tool_settings.mesh_select_mode
                if sm[0] and not sm[1] and not sm[2]:
                    return False
        return True

    def execute(self, context):
        obj   = context.active_object
        props = context.scene.fbxmt_trim2

        # ── Cove run — own geometry path via harry.py ─────────────────────────
        if props.beam_type == 'COVE':
            bm, sel_label = _build_cove_preview(obj, props, self)
            if bm is None:
                return {'CANCELLED'}
        else:
            settings = _props_to_settings(props)
            builder  = BEAM_BUILDERS.get(props.beam_type)
            if builder is None:
                self.report({'ERROR'}, f'Unknown beam type: {props.beam_type}')
                return {'CANCELLED'}

            try:
                if props.beam_type == 'QUICK':
                    bm, _computed_overrun = builder(obj, settings, context=context)
                    sel_label = 'selection'

                elif props.beam_type == 'DIHEDRAL':
                    # Dihedral is now merged into QUICK — shouldn't be reached
                    self.report({'ERROR'}, 'DIHEDRAL type retired — use QUICK')
                    return {'CANCELLED'}

                elif props.beam_type == 'PARALLEL':
                    face_indices = _get_selected_face_indices(obj)
                    if not face_indices:
                        self.report({'WARNING'}, 'No faces selected — select faces first (Object or Edit mode)')
                        return {'CANCELLED'}
                    bm = builder(obj, face_indices, settings, context=context,
                                 drive_through=props.drive_through,
                                 source_obj_name=props.source_obj_name)
                    sel_count = len(face_indices)
                    sel_label = f'{sel_count} face(s)'

                else:
                    face_indices = _get_selected_face_indices(obj)
                    if not face_indices:
                        self.report({'WARNING'}, 'No faces selected — select faces first (Object or Edit mode)')
                        return {'CANCELLED'}
                    bm = builder(obj, face_indices, settings)
                    sel_count = len(face_indices)
                    sel_label = f'{sel_count} face(s)'

            except Exception as e:
                self.report({'ERROR'}, f'Geometry build failed: {e}')
                import traceback
                traceback.print_exc()
                return {'CANCELLED'}

        if len(bm.faces) == 0:
            bm.free()
            self.report({'WARNING'}, 'No geometry was generated — check edge/face selection')
            return {'CANCELLED'}

        _update_preview_object(bm)
        bm.free()

        # Store beam dimensions on preview object for accurate commit-time comparison
        prev_obj = bpy.data.objects.get(_PREVIEW_OBJ_NAME)
        if prev_obj:
            prev_obj['fbxmt_beam_w'] = props.width
            prev_obj['fbxmt_beam_h'] = props.height

        # Store which object the preview was built from
        props.source_obj_name = obj.name
        props.preview_active  = True

        _tag_redraw()
        self.report({'INFO'}, f'Preview: {props.beam_type} on {sel_label}')
        return {'FINISHED'}


# ──────────────────────────────────────────────────────────────────────────────
# Operator: Commit
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Cove commit — mirrors OT_FBXMT_Generate_Coving's finalization exactly:
# single merged object, _Coving suffix, M_FBXMT_Trim material, optional bevel,
# then room-assignment popup.
# ──────────────────────────────────────────────────────────────────────────────

def _commit_cove(context, props, prev_obj, entry_mode, op):
    from .materials import ensure_fbxmt_materials, get_room_collection, COLLECTION_TRIM
    from .ceiling_deco import _fbxmt_register_popup_handler, _rename_beam_objects

    src_name = (props.source_obj_name or 'Coving').replace(' ', '_')
    suffix   = '_Coving'

    ensure_fbxmt_materials()
    trim_mat = bpy.data.materials.get('M_FBXMT_Trim')
    if trim_mat is None:
        op.report({'ERROR'}, 'M_FBXMT_Trim not found — run Setup Scene first')
        _destroy_preview()
        props.preview_active  = False
        props.source_obj_name = ''
        return {'CANCELLED'}

    # Promote preview mesh to real named mesh
    cov_mesh = prev_obj.data
    cov_mesh.materials.clear()
    cov_mesh.materials.append(trim_mat)

    preview_col = bpy.data.collections.get(_PREVIEW_COL_NAME)
    if preview_col and prev_obj.name in [o.name for o in preview_col.objects]:
        preview_col.objects.unlink(prev_obj)

    cov_obj      = prev_obj
    cov_obj.name = f'{src_name}{suffix}'
    cov_obj.data.name = cov_obj.name
    cov_obj.hide_select   = False
    cov_obj.show_in_front = False
    cov_obj.color         = (1.0, 1.0, 1.0, 1.0)
    # Ghost material is now gone — remove it from the slot
    if cov_mesh.materials and cov_mesh.materials[0] != trim_mat:
        cov_mesh.materials[0] = trim_mat

    # Recalculate normals
    bpy.ops.object.select_all(action='DESELECT')
    temp_obj = bpy.data.objects.new('_fbxmt_temp_norm', cov_mesh)
    context.scene.collection.objects.link(temp_obj)
    context.view_layer.objects.active = temp_obj
    temp_obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode='OBJECT')
    context.scene.collection.objects.unlink(temp_obj)
    bpy.data.objects.remove(temp_obj, do_unlink=False)
    cov_mesh.update()

    # Link cov_obj into scene root (it was already in preview col — now moves here)
    context.scene.collection.objects.link(cov_obj)

    # Note: chamfer bevel is already applied to the geometry during preview
    # (via bmesh.ops.bevel on the v2 inner-corner edges before ghost push).
    # No second modifier needed at commit time.

    # Remove now-empty preview collection
    if preview_col and len(preview_col.objects) == 0:
        bpy.data.collections.remove(preview_col)

    # ── Reset state ───────────────────────────────────────────────────────────
    props.preview_active  = False
    props.source_obj_name = ''
    _tag_redraw()

    # ── Room assignment popup — mirrors the coving operator's flow ────────────
    context.scene['_fbxmt_pending_cov_obj'] = cov_obj.name
    context.scene['_fbxmt_pending_cov_src'] = src_name

    from .materials import get_trim_room_names
    src_obj  = bpy.data.objects.get(src_name) or bpy.data.objects.get(
        props.source_obj_name)
    trim_col  = bpy.data.collections.get(COLLECTION_TRIM)
    room_found = None
    if src_obj and trim_col:
        for room_col in trim_col.children:
            if src_obj.name in [o.name for o in room_col.all_objects]:
                room_found = room_col.name
                break

    if room_found:
        cat    = context.scene.fbxmt_props.trim_collection_mode == 'CATEGORISED'
        target = get_room_collection(room_found, categorised=cat, category='Coving')
        for col in list(cov_obj.users_collection):
            col.objects.unlink(cov_obj)
        target.objects.link(cov_obj)
        context.scene.pop('_fbxmt_pending_cov_obj', None)
        context.scene.pop('_fbxmt_pending_cov_src',  None)
        op.report({'INFO'}, f'Coving committed to {room_found}')
    else:
        _fbxmt_register_popup_handler('fbxmt.assign_room_popup')
        op.report({'INFO'}, 'Coving committed — assign to room')

    return {'FINISHED'}


class FBXMT_OT_Trim2_Commit(Operator):
    bl_idname   = 'fbxmt.trim2_commit'
    bl_label    = 'Commit Beam'
    bl_options  = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.fbxmt_trim2
        return (
            props.preview_active
            and bpy.data.objects.get(_PREVIEW_OBJ_NAME) is not None
        )

    def execute(self, context):
        props    = context.scene.fbxmt_trim2
        prev_obj = bpy.data.objects.get(_PREVIEW_OBJ_NAME)

        if prev_obj is None:
            self.report({'ERROR'}, 'No preview object found')
            props.preview_active = False
            return {'CANCELLED'}

        # modifier_apply is forbidden in Edit mode — switch to Object mode now,
        # restore at the end.
        entry_mode = context.mode
        if entry_mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')

        type_label = props.beam_type.title().replace('_', '')

        # ── Cove run — own commit path ────────────────────────────────────────
        if props.beam_type == 'COVE':
            result = _commit_cove(context, props, prev_obj, entry_mode, self)
            if entry_mode == 'EDIT_MESH' and context.mode != 'EDIT_MESH':
                bpy.ops.object.mode_set(mode='EDIT')
            return result

        # ── 1. Separate preview mesh into individual beam objects ─────────────
        # ── 1. Separate preview mesh into individual beam objects via bmesh ────
        import bmesh as _bmesh
        from mathutils import Vector

        src_mesh = prev_obj.data
        bm = _bmesh.new()
        bm.from_mesh(src_mesh)

        # Find connected components (loose parts)
        bm.verts.ensure_lookup_table()
        visited  = set()
        parts    = []

        for start_v in bm.verts:
            if start_v.index in visited:
                continue
            # BFS flood fill
            component = set()
            queue     = [start_v]
            while queue:
                v = queue.pop()
                if v.index in visited:
                    continue
                visited.add(v.index)
                component.add(v.index)
                for edge in v.link_edges:
                    other = edge.other_vert(v)
                    if other.index not in visited:
                        queue.append(other)
            parts.append(component)

        bm.free()

        beam_objs = []
        settings       = _props_to_settings(props)
        source_obj_name = props.source_obj_name
        trim_mat       = bpy.data.materials.get('M_FBXMT_Trim')
        preview_col = bpy.data.collections.get(_PREVIEW_COL_NAME)

        for part_verts in parts:
            # Build a new mesh for this part
            part_bm = _bmesh.new()
            part_bm.from_mesh(src_mesh)
            part_bm.verts.ensure_lookup_table()

            # Delete verts not in this part
            to_delete = [v for v in part_bm.verts if v.index not in part_verts]
            _bmesh.ops.delete(part_bm, geom=to_delete, context='VERTS')

            part_mesh = bpy.data.meshes.new('_fbxmt_part')
            part_bm.to_mesh(part_mesh)
            part_bm.free()

            part_obj = bpy.data.objects.new('_fbxmt_part', part_mesh)
            part_obj.matrix_world = prev_obj.matrix_world.copy()
            context.scene.collection.objects.link(part_obj)

            if trim_mat:
                part_mesh.materials.clear()
                part_mesh.materials.append(trim_mat)

            part_obj.show_in_front = False
            part_obj.color         = (1.0, 1.0, 1.0, 1.0)
            part_obj.hide_select   = False

            beam_objs.append(part_obj)

        # Delete the original preview object
        if preview_col and prev_obj.name in [o.name for o in preview_col.objects]:
            preview_col.objects.unlink(prev_obj)
        bpy.data.objects.remove(prev_obj, do_unlink=True)

        trim_mat = bpy.data.materials.get('M_FBXMT_Trim')

        context.scene['_fbxmt_processed_pairs'] = []
        pending_names = []
        drivethru_hits = context.scene.get('_fbxmt_drivethru_hits', [])
        drivethru_idx  = 0
        for i, obj in enumerate(beam_objs):
            obj.name      = f'FBXMT_{type_label}Beam.{i+1:03d}'
            obj.data.name = obj.name
            obj['fbxmt_beam_w'] = settings.width
            obj['fbxmt_beam_h'] = settings.height
            if props.drive_through:
                obj['fbxmt_is_drivethru'] = True
            # Beams don't get wall booleans — only coving/trim needs that
            # _apply_trim_booleans(context, obj)

            # Drive-through boolean resolution — always run for drive-through beams
            if props.drive_through:
                hit_data = {}
                if drivethru_idx < len(drivethru_hits):
                    hit_data = drivethru_hits[drivethru_idx]
                    drivethru_idx += 1
                _apply_drivethru_booleans(context, obj, hit_data)

            # Apply boolean to guide mesh — beam cuts into the wall surface
            guide_obj = bpy.data.objects.get(source_obj_name)
            if guide_obj and guide_obj.type == 'MESH':
                mod           = obj.modifiers.new(name=f'Bool_{guide_obj.name}', type='BOOLEAN')
                mod.operation = 'DIFFERENCE'
                mod.solver    = 'EXACT'
                mod.object    = guide_obj
                bpy.ops.object.modifier_apply(modifier=mod.name)
            for col in list(obj.users_collection):
                col.objects.unlink(obj)
            context.scene.collection.objects.link(obj)
            pending_names.append(obj.name)

        # Remove now-empty preview collection
        preview_col = bpy.data.collections.get(_PREVIEW_COL_NAME)
        if preview_col and len(preview_col.objects) == 0:
            bpy.data.collections.remove(preview_col)

        # ── 2. Reset state ─────────────────────────────────────────────────────
        props.preview_active  = False
        props.source_obj_name = ''
        context.scene.pop('_fbxmt_drivethru_hits', None)
        context.scene.pop('_fbxmt_processed_pairs', None)
        _tag_redraw()

        # ── 3. Room assignment popup ───────────────────────────────────────────
        context.scene['_fbxmt_pending_beam_objs'] = pending_names
        context.scene['_fbxmt_pending_beam_src']  = props.source_obj_name

        from .ceiling_deco import _fbxmt_register_popup_handler, _rename_beam_objects

        # Check if source mesh is already in a room — auto-assign if so
        from .materials import get_trim_room_names, get_room_collection, COLLECTION_TRIM
        src_obj   = bpy.data.objects.get(props.source_obj_name)
        trim_col  = bpy.data.collections.get(COLLECTION_TRIM)
        room_found = None
        if src_obj and trim_col:
            for room_col in trim_col.children:
                all_names = [o.name for o in room_col.all_objects]
                if src_obj.name in all_names:
                    room_found = room_col.name
                    break

        if room_found:
            cat    = context.scene.fbxmt_props.trim_collection_mode == 'CATEGORISED'
            target = get_room_collection(room_found, categorised=cat, category='Beams')
            for obj_name in pending_names:
                obj = bpy.data.objects.get(obj_name)
                if obj:
                    for col in list(obj.users_collection):
                        col.objects.unlink(obj)
                    target.objects.link(obj)
            _rename_beam_objects(room_found, pending_names)
            context.scene.pop('_fbxmt_pending_beam_objs', None)
            context.scene.pop('_fbxmt_pending_beam_src',  None)
            self.report({'INFO'}, f'Committed {len(beam_objs)} beam(s) to {room_found}')
        else:
            _fbxmt_register_popup_handler('fbxmt.assign_room_beam_popup')
            self.report({'INFO'}, f'Committed {len(beam_objs)} beam(s) — assign to room')

        if entry_mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')

        return {'FINISHED'}


def _apply_drivethru_booleans(context, beam_obj, hit_data) -> None:
    """Drive-through beam cleaves crossing beams based on height comparison."""
    from mathutils import Vector

    hit_obj_names = hit_data.get('hits', []) if hit_data else []
    n_raw         = hit_data.get('n', [0, 0, 1]) if hit_data else [0, 0, 1]
    # Read exact dims from beam_obj custom props (set at commit time)
    beam_w        = float(beam_obj.get('fbxmt_beam_w') or hit_data.get('w', 0.1))
    beam_h        = float(beam_obj.get('fbxmt_beam_h') or hit_data.get('h', 0.1))
    beam_n        = Vector(n_raw).normalized()
    beam_area     = beam_w * beam_h
    beam_height   = beam_h


    def _height_along_axis(obj, axis):
        """Extent of obj along the given axis — used to compare beam heights at crossing."""
        bb  = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        ts  = [v.dot(axis) for v in bb]
        return max(ts) - min(ts)

    prev_active = context.view_layer.objects.active
    seen = set()

    # Find FBXMT beams that this beam's bbox overlaps — handle beam-to-beam cleaving
    from mathutils import Vector as _V
    bb_self = [beam_obj.matrix_world @ _V(c) for c in beam_obj.bound_box]
    bmin = _V((min(v.x for v in bb_self), min(v.y for v in bb_self), min(v.z for v in bb_self)))
    bmax = _V((max(v.x for v in bb_self), max(v.y for v in bb_self), max(v.z for v in bb_self)))

    crossing_beams = []
    processed_pairs = set(tuple(p) for p in context.scene.get('_fbxmt_processed_pairs', []))

    for obj in bpy.context.scene.objects:
        if obj is beam_obj or obj.type != 'MESH':
            continue
        # Find FBXMT beams with or without custom props
        is_fbxmt_beam = (obj.get('fbxmt_beam_w') is not None or
                         obj.get('fbxmt_beam_h') is not None or
                         obj.get('fbxmt_is_drivethru') is not None)
        if not is_fbxmt_beam:
            continue
        # Don't carve other drive-through beams from this same batch
        if obj.get('fbxmt_is_drivethru') and obj.get('fbxmt_beam_w') == beam_w:
            continue
        pair_key = tuple(sorted([beam_obj.name, obj.name]))
        if pair_key in processed_pairs:
            continue
        bb_o = [obj.matrix_world @ _V(c) for c in obj.bound_box]
        omin = _V((min(v.x for v in bb_o), min(v.y for v in bb_o), min(v.z for v in bb_o)))
        omax = _V((max(v.x for v in bb_o), max(v.y for v in bb_o), max(v.z for v in bb_o)))
        if (bmin.x <= omax.x and bmax.x >= omin.x and
            bmin.y <= omax.y and bmax.y >= omin.y and
            bmin.z <= omax.z and bmax.z >= omin.z):
            crossing_beams.append(obj)

    for hit_obj in crossing_beams:
        if hit_obj.name in seen:
            continue
        seen.add(hit_obj.name)
        pair_key = tuple(sorted([beam_obj.name, hit_obj.name]))
        processed_pairs.add(pair_key)
        stored_h = hit_obj.get('fbxmt_beam_h')
        if stored_h is None:
            # Fallback: estimate height from bbox perpendicular to beam normal
            bb   = [hit_obj.matrix_world @ _V(c) for c in hit_obj.bound_box]
            up   = Vector((0, 0, 1))
            if abs(beam_n.dot(up)) > 0.9:
                up = Vector((1, 0, 0))
            perp1 = beam_n.cross(up).normalized()
            perp2 = beam_n.cross(perp1).normalized()
            e1    = max(v.dot(perp1) for v in bb) - min(v.dot(perp1) for v in bb)
            e2    = max(v.dot(perp2) for v in bb) - min(v.dot(perp2) for v in bb)
            stored_h = max(e1, e2)
        stored_h = float(stored_h)
        if beam_height >= stored_h:
            mod           = hit_obj.modifiers.new(name=f'Bool_{beam_obj.name}', type='BOOLEAN')
            mod.operation = 'DIFFERENCE'
            mod.solver    = 'EXACT'
            mod.object    = beam_obj
            context.view_layer.objects.active = hit_obj
            hit_obj.select_set(True)
            try:
                bpy.ops.object.modifier_apply(modifier=mod.name)
            except Exception as e:
                print(f'[FBXMT] bool failed: {e}')
        else:
            mod           = beam_obj.modifiers.new(name=f'Bool_{hit_obj.name}', type='BOOLEAN')
            mod.operation = 'DIFFERENCE'
            mod.solver    = 'EXACT'
            mod.object    = hit_obj
            context.view_layer.objects.active = beam_obj
            beam_obj.select_set(True)
            try:
                bpy.ops.object.modifier_apply(modifier=mod.name)
            except Exception as e:
                print(f'[FBXMT] bool failed: {e}')

    context.scene['_fbxmt_processed_pairs'] = list(processed_pairs)
    context.view_layer.objects.active = prev_active


def _apply_trim_booleans(context, trim_obj: bpy.types.Object) -> None:
    """Add Boolean Difference modifiers to the beam (trim_obj).

    For each mesh object in the scene that the beam's bounding box overlaps,
    add a Boolean modifier on the beam using that mesh as the cutter,
    operation=DIFFERENCE, solver=EXACT.
    """
    from mathutils import Vector

    depsgraph = context.evaluated_depsgraph_get()

    # Get beam world-space bbox corners
    bbox_ws = [trim_obj.matrix_world @ Vector(c) for c in trim_obj.bound_box]
    bmin = Vector((min(v.x for v in bbox_ws), min(v.y for v in bbox_ws), min(v.z for v in bbox_ws)))
    bmax = Vector((max(v.x for v in bbox_ws), max(v.y for v in bbox_ws), max(v.z for v in bbox_ws)))

    cutters = []
    for obj in context.scene.objects:
        if obj is trim_obj:
            continue
        if obj.type != 'MESH':
            continue
        if obj.name.startswith('FBXMT_'):
            continue
        if obj.name.startswith('_fbxmt'):
            continue
        # Cheap bbox overlap test
        ob_ws = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        omin = Vector((min(v.x for v in ob_ws), min(v.y for v in ob_ws), min(v.z for v in ob_ws)))
        omax = Vector((max(v.x for v in ob_ws), max(v.y for v in ob_ws), max(v.z for v in ob_ws)))
        if (bmin.x <= omax.x and bmax.x >= omin.x and
            bmin.y <= omax.y and bmax.y >= omin.y and
            bmin.z <= omax.z and bmax.z >= omin.z):
            cutters.append(obj)

    if not cutters:
        return

    prev_active = context.view_layer.objects.active
    context.view_layer.objects.active = trim_obj
    trim_obj.select_set(True)

    for cutter in cutters:
        mod           = trim_obj.modifiers.new(name=f'Bool_{cutter.name}', type='BOOLEAN')
        mod.operation = 'DIFFERENCE'
        mod.solver    = 'EXACT'
        mod.object    = cutter
        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception as e:
            print(f'[FBXMT Trim2] Boolean apply failed for {cutter.name}: {e}')

    context.view_layer.objects.active = prev_active


# ──────────────────────────────────────────────────────────────────────────────
# Operator: Cancel
# ──────────────────────────────────────────────────────────────────────────────

class FBXMT_OT_Trim2_Cancel(Operator):
    bl_idname  = 'fbxmt.trim2_cancel'
    bl_label   = 'Cancel Preview'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.fbxmt_trim2.preview_active

    def execute(self, context):
        _destroy_preview()
        props                 = context.scene.fbxmt_trim2
        props.preview_active  = False
        props.source_obj_name = ''
        _tag_redraw()
        self.report({'INFO'}, 'Trim 2 preview cancelled')
        return {'FINISHED'}


# ──────────────────────────────────────────────────────────────────────────────
# Panel
# ──────────────────────────────────────────────────────────────────────────────

class FBXMT_PT_Trim2(Panel):
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_label       = 'Trim 2'
    bl_category    = 'FBXMT Trim'
    bl_parent_id   = 'FBXMT_PT_TrimMain'
    bl_order       = 10
    bl_options     = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props  = context.scene.fbxmt_trim2
        prefs  = context.preferences.addons.get('fbx_mappers_toolkit')
        prefs  = prefs.preferences if prefs else None
        bt     = props.beam_type

        # -- Beam type picker --------------------------------------------------
        layout.prop(props, 'beam_type', text='')
        layout.separator(factor=0.5)

        # -- Common settings ---------------------------------------------------
        common = layout.box()
        row_w = common.row(align=True)
        row_w.prop(props, 'width')
        if bt == 'COVE':
            row_w.prop(props, 'cove_flip_width', text='Flip', toggle=True)
        row_h = common.row(align=True)
        row_h.prop(props, 'height')
        if bt == 'COVE':
            row_h.prop(props, 'cove_flip_height', text='Flip', toggle=True)

        common.separator(factor=0.5)

        row_s = common.row(align=True)
        row_s.prop(props, 'overrun_start', text='Start')
        ext_s = row_s.row(align=True)
        ext_s.enabled = props.overrun_start > 0.0
        ext_s.prop(props, 'extrude_start', text='Ext', toggle=True)

        row_e = common.row(align=True)
        row_e.prop(props, 'overrun_end', text='End')
        ext_e = row_e.row(align=True)
        ext_e.enabled = props.overrun_end > 0.0
        ext_e.prop(props, 'extrude_end', text='Ext', toggle=True)

        # -- Hint box ----------------------------------------------------------
        _HINTS = {
            'QUICK':    "Width and Height set the beam cross-section. "
                        "Start/End extend beam ends past the selection.",
            'PARALLEL': "Width and Height set each beam cross-section. "
                        "Start/End extend beam ends along the run direction.",
            'SPOKE':    "Width and Height set each spoke cross-section.",
            'CURVE':    "Width and Height set the cross-section swept along the arc.",
            'COVE':     "Width runs along the ceiling from the seam. "
                        "Height drops down the wall.",
        }
        show_hint = (prefs is None) or getattr(prefs, 'show_trim2_hint', True)
        if show_hint:
            hint_box = common.box()
            hint_box.scale_y = 0.75
            for line in _wrap_hint_text(_HINTS.get(bt, ''), context):
                hint_box.label(text=line)

        # -- Type-specific settings --------------------------------------------
        if bt == 'QUICK':
            ts = context.tool_settings
            sm = ts.mesh_select_mode
            in_vert_mode = sm[0] and not sm[1] and not sm[2]
            in_edge_mode = sm[1] and not sm[0] and not sm[2]
            sub = layout.box()
            if in_vert_mode:
                sub.label(text='Vert mode not supported', icon='ERROR')
                sub.label(text='Switch to Edge or Face select mode')
            else:
                sub.label(text='Quick Beam', icon='SNAP_EDGE')
                row_ang = sub.row()
                row_ang.enabled = in_edge_mode
                row_ang.prop(props, 'dihedral_angle_offset', text='Angle Offset')
                half_deg = math.degrees(props.dihedral_half_angle)
                sub.label(text=f'Range  ±{half_deg:.1f}°', icon='INFO')
                sub.separator(factor=0.5)
                row_surf = sub.row(align=True)
                row_surf.prop(props, 'drive_through', toggle=True, text='Drive Through')
                col_surf = row_surf.column()
                col_surf.enabled = not props.drive_through
                col_surf.prop(props, 'quick_raycast_iters', text='Surfaces')
                sub.prop(props, 'cap_ends')

        elif bt == 'PARALLEL':
            sub = layout.box()
            row = sub.row()
            row.label(text="Parallel", icon='ALIGN_FLUSH')
            row.operator('fbxmt.trim2_reset_parallel', text='', icon='LOOP_BACK')
            sub.prop(props, 'par_start_inset', text='First Beam Offset')
            sub.prop(props, 'par_end_clamp',   text='Last Beam Offset')
            row_cs = sub.row(align=True)
            row_cs.prop(props, 'par_count',   text='Count')
            row_cs.prop(props, 'par_spacing', text='Spacing')
            sub.prop(props, 'par_placement',  text='')

        elif bt == 'SPOKE':
            sub = layout.box()
            sub.label(text="Spoke", icon='ORIENTATION_NORMAL')
            col2 = sub.column(align=True)
            col2.prop(props, 'spoke_count')
            col2.prop(props, 'spoke_spacing_mode')

        elif bt == 'CURVE':
            sub = layout.box()
            sub.label(text="Curve", icon='CURVE_PATH')
            sub.prop(props, 'curve_segments')

        elif bt == 'COVE':
            sub = layout.box()
            sub.label(text="Cove Run", icon='MOD_SMOOTH')
            sub.prop(props, 'cove_chamfer', text='Chamfer')
            sub.prop(props, 'cove_smooth_angle', text='Smooth Angle')

        layout.separator()

        # -- Action buttons ----------------------------------------------------
        if not props.preview_active:
            row = layout.row(align=True)
            row.operator('fbxmt.trim2_preview', text='Preview', icon='HIDE_OFF')
        else:
            col_info = layout.column()
            col_info.alert = False
            src = props.source_obj_name or '(unknown)'
            col_info.label(text=f'Preview active  ·  {src}', icon='INFO')
            row = layout.row(align=True)
            row.scale_x = 1.0
            row.operator('fbxmt.trim2_commit', text='Commit', icon='CHECKMARK')
            cancel_sub = row.row(align=True)
            cancel_sub.alert = True
            cancel_sub.operator('fbxmt.trim2_cancel', text='', icon='X')

        # -- Selection hint ----------------------------------------------------
        obj = context.active_object
        if obj and obj.type == 'MESH' and context.mode in ('OBJECT', 'EDIT_MESH'):
            if bt in ('QUICK', 'COVE') and context.mode == 'EDIT_MESH':
                import bmesh as _bm
                _tmp = _bm.from_edit_mesh(obj.data)
                if bt == 'QUICK':
                    sm = context.tool_settings.mesh_select_mode
                    if sm[0] and not sm[1] and not sm[2]:
                        n_sel = sum(1 for v in _tmp.verts if v.select)
                        shint = 'Select 2 vertices'
                    elif sm[1] and not sm[0] and not sm[2]:
                        n_sel = sum(1 for e in _tmp.edges if e.select)
                        shint = 'Select 1 or 2 edges'
                    else:
                        n_sel = sum(1 for f in _tmp.faces if f.select)
                        shint = 'Select 1 or 2 faces'
                else:  # COVE
                    n_sel = sum(1 for e in _tmp.edges if e.select)
                    shint = 'Select edges first'
                if n_sel == 0:
                    row2 = layout.row()
                    row2.alert = True
                    row2.label(text=shint, icon='ERROR')
            else:
                if context.mode == 'OBJECT':
                    n_sel = sum(1 for p in obj.data.polygons if p.select)
                else:
                    import bmesh as _bm
                    _tmp = _bm.from_edit_mesh(obj.data)
                    n_sel = sum(1 for f in _tmp.faces if f.select)
                if n_sel == 0:
                    row2 = layout.row()
                    row2.alert = True
                    row2.label(text='Select faces first', icon='ERROR')

# Registration
# ──────────────────────────────────────────────────────────────────────────────

# Classes exported for __init__.py to register in the main classes tuple /
# _trim_classes tuple.  trim2.register() only needs to attach the scene prop.
_CLASSES = (
    FBXMT_Trim2Props,
    FBXMT_OT_Trim2_Preview,
    FBXMT_OT_Trim2_Commit,
    FBXMT_OT_Trim2_Cancel,
    FBXMT_PT_Trim2,
)


def register():
    """Attach the scene PointerProperty.  Class registration is handled by __init__.py."""
    bpy.types.Scene.fbxmt_trim2 = bpy.props.PointerProperty(type=FBXMT_Trim2Props)


def unregister():
    # Remove the preview object if Blender is shutting the addon down mid-session
    try:
        _destroy_preview()
    except Exception:
        pass

    if hasattr(bpy.types.Scene, 'fbxmt_trim2'):
        del bpy.types.Scene.fbxmt_trim2
