# beam_placement.py — FBX Mapper's Toolkit
#
# Beam Placement subsystem — Operator 2a of the Ceiling Deco system.
#
# Places paired empties (beam_NNN_1, beam_NNN_2) at the centroids of two
# selected face groups.  Empties are freely moveable in the viewport after
# placement.  The Generate Beams operator in ceiling_deco.py consumes them.
#
# Panel controls exposed via FBXMT_Props (see props.py):
#   beam_count          — number of beam pairs to place along the span
#   beam_spacing        — distance between beams (overrides count if > 0)
#   beam_offset_h       — horizontal offset from centroid line
#   beam_offset_v       — vertical offset from centroid
#   beam_snap_to_face   — snap each empty to nearest selected face centre
#
# Collision detection:
#   Warn-only — checks for overlap between beam bounding boxes after placement
#   and reports via INFO messages.  Does not block placement.

import bpy
import bmesh
from mathutils import Vector
from bpy.types import Operator


# ---------------------------------------------------------------------------
# Helpers

def _selected_face_groups(obj):
    """Return up to two lists of selected faces from the active mesh.

    Faces are split into two groups by connected component (flood-fill).
    If more than two components are selected, only the two largest are used.
    """
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    selected = [f for f in bm.faces if f.select]
    if not selected:
        bm.free()
        return [], []

    # Flood-fill to find connected components
    remaining = {f.index: f for f in selected}
    components = []

    while remaining:
        seed = next(iter(remaining.values()))
        stack = [seed]
        comp  = []
        while stack:
            face = stack.pop()
            if face.index not in remaining:
                continue
            del remaining[face.index]
            comp.append(face)
            for edge in face.edges:
                for linked_face in edge.link_faces:
                    if linked_face.index in remaining:
                        stack.append(linked_face)
        components.append(comp)

    components.sort(key=len, reverse=True)
    group_a = components[0] if len(components) > 0 else []
    group_b = components[1] if len(components) > 1 else []

    # Convert to world-space centroids
    mat = obj.matrix_world

    def _centroid(faces):
        if not faces:
            return None
        total = Vector()
        for f in faces:
            total += mat @ f.calc_center_median()
        return total / len(faces)

    ca = _centroid(group_a)
    cb = _centroid(group_b)
    bm.free()
    return ca, cb


def _snap_to_face_centre(obj, world_pos):
    """Return the world-space centre of the selected face nearest to world_pos."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    mat = obj.matrix_world

    best_dist = float('inf')
    best_co   = world_pos.copy()
    for face in bm.faces:
        if not face.select:
            continue
        fc = mat @ face.calc_center_median()
        d  = (fc - world_pos).length
        if d < best_dist:
            best_dist = d
            best_co   = fc
    bm.free()
    return best_co


def _next_beam_group_index():
    """Return the next unused NNN for beam_NNN_1/2 naming."""
    idx = 1
    existing = {obj.name for obj in bpy.data.objects if obj.type == 'EMPTY'}
    while f'beam_{idx:03d}_1' in existing or f'beam_{idx:03d}_2' in existing:
        idx += 1
    return idx


def _place_empty(name, location, collection):
    """Create a plain axes empty at location and link to collection."""
    e = bpy.data.objects.new(name, None)
    e.empty_display_type = 'PLAIN_AXES'
    e.empty_display_size = 0.1
    e.location = location
    collection.objects.link(e)
    return e


def _check_collisions(pairs, depth, thickness):
    """Warn-only collision check between beam bounding boxes.

    Each beam is approximated as an axis-aligned box along its axis.
    Returns a list of (group_a_name, group_b_name) collision tuples.
    """
    collisions = []
    beam_boxes = []

    for start_empty, end_empty in pairs:
        s = start_empty.location
        e = end_empty.location
        mn = Vector((min(s.x, e.x) - depth, min(s.y, e.y) - depth, min(s.z, e.z) - thickness))
        mx = Vector((max(s.x, e.x) + depth, max(s.y, e.y) + depth, max(s.z, e.z) + thickness))
        beam_boxes.append((start_empty.name.rsplit('_', 1)[0], mn, mx))

    for i in range(len(beam_boxes)):
        for j in range(i + 1, len(beam_boxes)):
            name_a, mn_a, mx_a = beam_boxes[i]
            name_b, mn_b, mx_b = beam_boxes[j]
            overlap = (mn_a.x < mx_b.x and mx_a.x > mn_b.x and
                       mn_a.y < mx_b.y and mx_a.y > mn_b.y and
                       mn_a.z < mx_b.z and mx_a.z > mn_b.z)
            if overlap:
                collisions.append((name_a, name_b))

    return collisions


# ---------------------------------------------------------------------------
# Operator: Place Beams

class OT_FBXMT_Place_Beams(Operator):
    bl_idname     = 'fbxmt.place_beams'
    bl_label      = 'Place Beams'
    bl_description = ('Place beam_NNN_1 / beam_NNN_2 empties at the centroids '
                       'of two selected face groups. Empties can be moved freely '
                       'before generating beam geometry.')
    bl_options    = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH'
                and context.active_object is not None
                and context.active_object.type == 'MESH')

    def execute(self, context):
        props = context.scene.fbxmt_props

        count      = props.beam_count
        spacing    = props.beam_spacing
        offset_h   = props.beam_offset_h
        offset_v   = props.beam_offset_v
        snap       = props.beam_snap_to_face

        obj = context.active_object

        # Temporarily drop to object mode to query face selection
        bpy.ops.object.mode_set(mode='OBJECT')
        ca, cb = _selected_face_groups(obj)
        bpy.ops.object.mode_set(mode='EDIT')

        if ca is None or cb is None:
            self.report({'WARNING'},
                        'Select faces in exactly two disconnected groups '
                        '(one per beam anchor).')
            return {'CANCELLED'}

        if snap:
            bpy.ops.object.mode_set(mode='OBJECT')
            ca = _snap_to_face_centre(obj, ca)
            cb = _snap_to_face_centre(obj, cb)
            bpy.ops.object.mode_set(mode='EDIT')

        # Apply offsets
        axis = (cb - ca)
        span = axis.length
        if span < 1e-4:
            self.report({'WARNING'}, 'Face group centroids are coincident — select faces further apart.')
            return {'CANCELLED'}

        t = axis.normalized()
        # Perpendicular in XY for horizontal offset
        perp_h = Vector((-t.y, t.x, 0.0))
        if perp_h.length < 1e-6:
            perp_h = Vector((1, 0, 0))
        perp_h = perp_h.normalized()

        ca_final = ca + perp_h * offset_h + Vector((0, 0, offset_v))
        cb_final = cb + perp_h * offset_h + Vector((0, 0, offset_v))

        # Determine beam positions along span
        if spacing > 0.0:
            n_beams = max(1, int(span / spacing))
        else:
            n_beams = max(1, count)

        collection = context.collection
        placed_pairs = []
        start_group_idx = _next_beam_group_index()

        for i in range(n_beams):
            frac_a = i / n_beams
            frac_b = (i + 1) / n_beams if n_beams > 1 else 1.0
            if n_beams == 1:
                pos_a = ca_final
                pos_b = cb_final
            else:
                mid   = (frac_a + frac_b) * 0.5
                pos_a = ca_final.lerp(cb_final, mid)
                pos_b = pos_a + t * (span / n_beams)

            gidx = start_group_idx + i
            name_a = f'beam_{gidx:03d}_1'
            name_b = f'beam_{gidx:03d}_2'
            e_a = _place_empty(name_a, pos_a, collection)
            e_b = _place_empty(name_b, pos_b, collection)
            placed_pairs.append((e_a, e_b))

        # Warn-only collision detection
        depth     = props.coving_depth
        thickness = props.coving_thickness

        # Gather all existing pairs for collision test
        from .ceiling_deco import _get_beam_empties
        all_pairs = _get_beam_empties(context)
        collisions = _check_collisions(all_pairs, depth, thickness)
        for name_a, name_b in collisions:
            self.report({'WARNING'}, f'Beam collision detected: {name_a} ↔ {name_b}')

        self.report({'INFO'}, f'{n_beams} beam pair(s) placed')
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Clear Beam Empties

class OT_FBXMT_Clear_Beams(Operator):
    bl_idname     = 'fbxmt.clear_beams'
    bl_label      = 'Clear Beam Empties'
    bl_description = 'Remove all beam_NNN_1 / beam_NNN_2 empties from the scene'
    bl_options    = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        to_remove = [
            obj for obj in bpy.data.objects
            if obj.type == 'EMPTY' and obj.name.startswith('beam_')
            and obj.name.rsplit('_', 1)[-1] in ('1', '2')
        ]
        for obj in to_remove:
            bpy.data.objects.remove(obj, do_unlink=True)
        self.report({'INFO'}, f'{len(to_remove)} beam empty/empties removed')
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration

classes = (
    OT_FBXMT_Place_Beams,
    OT_FBXMT_Clear_Beams,
)
