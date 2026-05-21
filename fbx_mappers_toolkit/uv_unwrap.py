import bpy
import bmesh
from bpy.types import Operator
from mathutils import Vector
import math
from .materials import (
    FBXMT_FLOOR_MATERIALS,
    FBXMT_RAMP_MATERIALS,
    FBXMT_WALL_MATERIALS,
    FBXMT_IGNORE_MATERIAL,
    ISLAND_SUB_PREFIX,
    ISLAND_MARKER_NAME,
    _island_sub_index,
    LIGHTMAP_CHANNEL_NAME,
    PREVIEW_UV_NAME,
    # Legacy chain imports kept for old blend files
    CHAIN_PREFIX,
    _chain_index,
)
from .uv_pack import pack_islands, smart_pack_islands


def _build_preview_uv(mesh):
    """Copy UVMap into UVPreview, scaled to fit inside 0-1 space.

    Reads all UV coordinates from UVMap, finds the bounding box of the
    full island layout, then scales and translates everything uniformly
    so the tightest fit sits within 0-1 with a small margin. Creates
    UVPreview if missing. Always overwrites existing UVPreview data.
    """
    src = mesh.uv_layers.get('UVMap')
    if not src:
        return

    # Ensure UVPreview channel exists
    if PREVIEW_UV_NAME not in mesh.uv_layers:
        mesh.uv_layers.new(name=PREVIEW_UV_NAME)
    dst = mesh.uv_layers[PREVIEW_UV_NAME]

    # Find bounding box of all UVs in UVMap
    min_u = min_v =  1e9
    max_u = max_v = -1e9
    for loop_uv in src.data:
        u, v   = loop_uv.uv
        min_u  = min(min_u, u)
        min_v  = min(min_v, v)
        max_u  = max(max_u, u)
        max_v  = max(max_v, v)

    span_u = max_u - min_u
    span_v = max_v - min_v
    if span_u < 1e-9 or span_v < 1e-9:
        return  # degenerate — nothing to preview

    # Uniform scale to fit within [margin, 1-margin]
    margin = 0.02
    scale  = (1.0 - 2 * margin) / max(span_u, span_v)

    # Centre the layout in 0-1
    offset_u = margin + (1.0 - 2 * margin - span_u * scale) * 0.5 - min_u * scale
    offset_v = margin + (1.0 - 2 * margin - span_v * scale) * 0.5 - min_v * scale

    for i, loop_uv in enumerate(src.data):
        u, v = loop_uv.uv
        dst.data[i].uv = (u * scale + offset_u, v * scale + offset_v)


# ─── Material helpers ─────────────────────────────────────────────────────────

def get_face_material_name(face, mesh):
    if face.material_index < len(mesh.materials):
        mat = mesh.materials[face.material_index]
        if mat:
            return mat.name
    return None


def is_island_sub_name(name):
    """True for M_FBXMT_Island_NN hidden sub-materials."""
    return name is not None and name.startswith(ISLAND_SUB_PREFIX) and _island_sub_index(name) is not None


def get_island_sub_names_on_mesh(mesh):
    """Sorted list of island sub-material names present in this mesh's slots."""
    names = [m.name for m in mesh.materials if m and is_island_sub_name(m.name)]
    names.sort(key=lambda n: _island_sub_index(n))
    return names


# Legacy shims
def is_chain_mat_name(name):
    return name is not None and name.startswith(CHAIN_PREFIX) and _chain_index(name) is not None


# ─── Connectivity ─────────────────────────────────────────────────────────────

def find_connected_groups(face_list):
    """
    Walk connectivity within face_list.
    Material boundary is a hard stop: neighbours must share the same
    material_index. Returns list of lists of BMFaces.

    Note: bm and mesh are NOT parameters — we only need the face list itself.
    """
    face_index_set = set(f.index for f in face_list)
    visited        = set()
    groups         = []

    for start_face in face_list:
        if start_face.index in visited:
            continue
        group = []
        queue = [start_face]
        visited.add(start_face.index)
        while queue:
            current = queue.pop()
            group.append(current)
            for edge in current.edges:
                for linked in edge.link_faces:
                    if (linked.index not in visited
                            and linked.index in face_index_set
                            and linked.material_index == current.material_index):
                        visited.add(linked.index)
                        queue.append(linked)
        groups.append(group)

    return groups


# ─── Chain sorting ────────────────────────────────────────────────────────────

def sort_face_chain(group):
    """
    Sort a connected group into a linear chain by walking shared edges.
    Falls back to unsorted on closed loops or branching geometry.
    """
    if len(group) <= 1:
        return group

    index_set    = set(f.index for f in group)
    face_by_idx  = {f.index: f for f in group}
    adjacency    = {f.index: [] for f in group}

    for face in group:
        for edge in face.edges:
            for linked in edge.link_faces:
                if linked.index != face.index and linked.index in index_set:
                    if linked.index not in adjacency[face.index]:
                        adjacency[face.index].append(linked.index)

    ends = [idx for idx, nbrs in adjacency.items() if len(nbrs) == 1]

    if len(ends) == 0 or len(ends) > 2:
        return group  # closed loop or branching — return unsorted

    start   = ends[0]
    chain   = [face_by_idx[start]]
    prev    = None
    current = start

    while True:
        nbrs = [n for n in adjacency[current] if n != prev]
        if not nbrs:
            break
        next_idx = nbrs[0]
        chain.append(face_by_idx[next_idx])
        prev    = current
        current = next_idx

    return chain if len(chain) == len(group) else group


# ─── Projection ───────────────────────────────────────────────────────────────

def get_face_axes(face, world_matrix):
    """
    Return (u_axis, v_axis) for a wall/chain face.
    V = world Z projected onto the face plane — keeps vertical consistent.
    U = perpendicular to normal and V.
    """
    z_axis       = Vector((0.0, 0.0, 1.0))
    world_normal = (world_matrix.to_3x3() @ face.normal).normalized()
    v_axis       = (z_axis - z_axis.dot(world_normal) * world_normal).normalized()
    if v_axis.length < 0.001:
        v_axis = Vector((0.0, 1.0, 0.0))
    u_axis = -(world_normal.cross(v_axis).normalized())
    return u_axis, v_axis


def get_shared_edge_loops(face_a, face_b):
    """Return the two loops on face_a that form the shared edge with face_b, or None."""
    verts_b      = {v.index for v in face_b.verts}
    shared_loops = [loop for loop in face_a.loops if loop.vert.index in verts_b]
    return shared_loops if len(shared_loops) == 2 else None


def _is_linear_chain(group):
    """Return True if group forms a linear strip (each face has at most 2
    neighbours within the group). Triangulated meshes fail this — branching
    or closed loops mean edge-stitching is unreliable."""
    if len(group) <= 1:
        return True
    index_set = set(f.index for f in group)
    for face in group:
        nbr_count = sum(
            1 for edge in face.edges
            for linked in edge.link_faces
            if linked.index != face.index and linked.index in index_set
        )
        if nbr_count > 2:
            return False
    ends = [
        face for face in group
        if sum(
            1 for edge in face.edges
            for linked in edge.link_faces
            if linked.index != face.index and linked.index in index_set
        ) == 1
    ]
    # Closed loop (no ends) or branching (handled above) → not linear
    return len(ends) >= 1


def project_face_independent(face, uv_layer, world_matrix):
    """Project a single face using its own normal-derived axes, centred on
    its own bounding box. Used as fallback for triangulated geometry where
    edge-stitching is unreliable."""
    u_axis, v_axis = get_face_axes(face, world_matrix)
    loops          = list(face.loops)
    world_verts    = {loop.vert.index: world_matrix @ loop.vert.co for loop in loops}
    raw = {
        loop.vert.index: (
            world_verts[loop.vert.index].dot(u_axis),
            world_verts[loop.vert.index].dot(v_axis),
        )
        for loop in loops
    }
    u_vals   = [uv[0] for uv in raw.values()]
    v_vals   = [uv[1] for uv in raw.values()]
    u_centre = (min(u_vals) + max(u_vals)) / 2.0
    v_centre = (min(v_vals) + max(v_vals)) / 2.0
    for loop in loops:
        u, v = raw[loop.vert.index]
        loop[uv_layer].uv = (u - u_centre, v - v_centre)


def project_wall_group(group, uv_layer, world_matrix):
    """
    Unroll a connected wall or chain strip by stitching faces edge-to-edge.
    Each face uses its own normal-derived axes — no distortion on curves.
    V is always world Z for consistency. Orientation never changes.
    """
    chain     = sort_face_chain(group)
    prev_face = None
    prev_uv   = {}   # vert index → UV from the previous face's shared edge

    for i, face in enumerate(chain):
        u_axis, v_axis = get_face_axes(face, world_matrix)
        loops          = list(face.loops)
        world_verts    = {loop.vert.index: world_matrix @ loop.vert.co for loop in loops}

        raw = {
            loop.vert.index: (
                world_verts[loop.vert.index].dot(u_axis),
                world_verts[loop.vert.index].dot(v_axis),
            )
            for loop in loops
        }

        if i == 0:
            v_vals   = [uv[1] for uv in raw.values()]
            v_centre = (min(v_vals) + max(v_vals)) / 2.0
            u_min    = min(uv[0] for uv in raw.values())
            for loop in loops:
                u, v = raw[loop.vert.index]
                loop[uv_layer].uv = (u - u_min, v - v_centre)
                prev_uv[loop.vert.index] = loop[uv_layer].uv.copy()
        else:
            shared = get_shared_edge_loops(face, prev_face)
            if shared and all(loop.vert.index in prev_uv for loop in shared):
                offsets_u = []
                offsets_v = []
                for loop in shared:
                    vid = loop.vert.index
                    tu, tv = prev_uv[vid]
                    ru, rv = raw[vid]
                    offsets_u.append(tu - ru)
                    offsets_v.append(tv - rv)
                du = sum(offsets_u) / len(offsets_u)
                dv = sum(offsets_v) / len(offsets_v)
            else:
                prev_u_max = max(uv[0] for uv in prev_uv.values())
                u_min      = min(uv[0] for uv in raw.values())
                du = prev_u_max - u_min
                dv = 0.0

            for loop in loops:
                u, v = raw[loop.vert.index]
                loop[uv_layer].uv = (u + du, v + dv)
                prev_uv[loop.vert.index] = loop[uv_layer].uv.copy()

        prev_face = face


def project_floor_group(group, uv_layer, world_matrix):
    """Project a floor/ceiling group using world X/Y axes, centred on its bounding box."""
    u_axis  = Vector((1.0, 0.0, 0.0))
    v_axis  = Vector((0.0, 1.0, 0.0))
    all_wco = [world_matrix @ loop.vert.co for face in group for loop in face.loops]
    all_u   = [v.dot(u_axis) for v in all_wco]
    all_v   = [v.dot(v_axis) for v in all_wco]
    cu      = (min(all_u) + max(all_u)) / 2.0
    cv      = (min(all_v) + max(all_v)) / 2.0
    for face in group:
        for loop in face.loops:
            wco = world_matrix @ loop.vert.co
            loop[uv_layer].uv = (wco.dot(u_axis) - cu, wco.dot(v_axis) - cv)


# ─── Lightmap ─────────────────────────────────────────────────────────────────

def ensure_lightmap_channel(mesh, force_regenerate, obj=None):
    """Create or regenerate LightmapUVs channel.
    
    obj: the bpy.types.Object owner of the mesh — required to run the
    lightmap_pack operator which needs an active object in Edit mode.
    If obj is None and the operator context is unavailable, a blank UV
    layer is created without packing (still valid for UE5).
    """
    existing = mesh.uv_layers.get(LIGHTMAP_CHANNEL_NAME)
    if existing and not force_regenerate:
        return False
    if not existing:
        mesh.uv_layers.new(name=LIGHTMAP_CHANNEL_NAME)

    # lightmap_pack requires Edit mode on the active object
    if obj is None:
        # No object context — layer created but not packed, acceptable fallback
        return True

    prev_active = mesh.uv_layers.active
    mesh.uv_layers.active = mesh.uv_layers[LIGHTMAP_CHANNEL_NAME]

    prev_mode = obj.mode
    try:
        bpy.context.view_layer.objects.active = obj
        if prev_mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.uv.lightmap_pack(
            'EXEC_DEFAULT',
            PREF_CONTEXT='ALL_FACES',
            PREF_PACK_IN_ONE=True,
            PREF_NEW_UVLAYER=False,
            PREF_MARGIN_DIV=0.1,
        )
    finally:
        if prev_mode != 'EDIT':
            bpy.ops.object.mode_set(mode='OBJECT')
        if prev_active:
            mesh.uv_layers.active = prev_active

    return True


# ─── UV colour attribute ───────────────────────────────────────────────────────

def _write_uv_colours(mesh):
    """Write a FBXMT_Colours face-corner colour attribute to mesh.

    Each face gets the colour of its FBXMT material so islands are visually
    distinct in the UV editor. Uses CORNER domain so it aligns with UV loops.

    To see the result: in the UV editor, open Overlays and set the
    colour attribute to 'FBXMT_Colours'.
    """
    from .materials import FBXMT_MATERIALS, ISLAND_SUB_PREFIX

    ATTR_NAME    = 'FBXMT_Colours'
    _ISLAND_COL  = (0.2, 0.8, 0.9, 1.0)  # cyan — distinct from all base materials
    _FALLBACK    = (0.5, 0.5, 0.5, 1.0)

    # Build material index -> colour lookup
    mat_colours = {}
    for i, mat in enumerate(mesh.materials):
        if mat is None:
            mat_colours[i] = _FALLBACK
            continue
        if mat.name in FBXMT_MATERIALS:
            mat_colours[i] = FBXMT_MATERIALS[mat.name]
        elif mat.name.startswith(ISLAND_SUB_PREFIX):
            mat_colours[i] = _ISLAND_COL
        else:
            mat_colours[i] = _FALLBACK

    # Remove and recreate attribute so it's always fresh
    existing = mesh.color_attributes.get(ATTR_NAME)
    if existing:
        mesh.color_attributes.remove(existing)
    attr = mesh.color_attributes.new(name=ATTR_NAME, type='FLOAT_COLOR', domain='CORNER')

    data     = attr.data
    loop_idx = 0
    for poly in mesh.polygons:
        col = mat_colours.get(poly.material_index, _FALLBACK)
        for _ in poly.loop_indices:
            data[loop_idx].color = col
            loop_idx += 1

    mesh.update()


# ─── Core unwrap ──────────────────────────────────────────────────────────────

def unwrap_mesh(mesh, world_matrix, floor_threshold_dot, selected_only=False):
    """
    Unwrap one mesh object.

    Face routing:
      Floor/Ceiling  → world Z projection
      Ramp           → world Z projection (same as floor, own islands)
      Wall/Trim      → per-face normal, edge-stitched strip
      Chain_NN       → per-face normal, edge-stitched strip, grouped by chain
                       number first then by connectivity — each connected
                       component within a chain number is its own UV island.
                       Material boundary = hard island boundary even if
                       geometry is physically connected.
      Ignore         → skipped
      No M_FBXMT mat → skipped
    """
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    bm.verts.ensure_lookup_table()

    # Always write to the named diffuse UV layer, not whatever happens to be
    # active — active could be LightmapUVs or any other layer.
    uv_layer_name = "UVMap"
    uv_layer = bm.loops.layers.uv.get(uv_layer_name)
    if uv_layer is None:
        uv_layer = bm.loops.layers.uv.new(uv_layer_name)

    floor_faces  = []
    ramp_faces   = []
    wall_faces   = []
    # island_faces: dict of sub-material name → [BMFace, ...]
    island_faces = {}

    # Which island sub-material names are slotted on this mesh
    island_sub_on_mesh = {
        m.name for m in mesh.materials
        if m and is_island_sub_name(m.name)
    }
    # Legacy chain support — treat old chain mats the same as island subs
    chain_names_on_mesh = {
        m.name for m in mesh.materials
        if m and is_chain_mat_name(m.name)
    }

    for face in bm.faces:
        if selected_only and not face.select:
            continue
        mat_name = get_face_material_name(face, mesh)
        if mat_name in FBXMT_FLOOR_MATERIALS:
            floor_faces.append(face)
        elif mat_name in FBXMT_RAMP_MATERIALS:
            ramp_faces.append(face)
        elif mat_name in FBXMT_WALL_MATERIALS:
            wall_faces.append(face)
        elif mat_name == ISLAND_MARKER_NAME:
            # Bare marker (not yet graph-coloured) — treat as wall
            wall_faces.append(face)
        elif mat_name and is_island_sub_name(mat_name) and mat_name in island_sub_on_mesh:
            island_faces.setdefault(mat_name, []).append(face)
        elif mat_name and is_chain_mat_name(mat_name) and mat_name in chain_names_on_mesh:
            # Legacy chain materials — treat as island subs
            island_faces.setdefault(mat_name, []).append(face)
        # FBXMT_IGNORE_MATERIAL, M_FBXMT_Island marker, unrecognised → skip

    all_groups = []

    # Floors / ceilings
    for group in find_connected_groups(floor_faces):
        project_floor_group(group, uv_layer, world_matrix)
        all_groups.append(group)

    # Ramps — floor projection (world X/Y), each connected group its own island
    for group in find_connected_groups(ramp_faces):
        project_floor_group(group, uv_layer, world_matrix)
        all_groups.append(group)

    # Standard walls — linear strips get edge-stitched as one island.
    # Non-linear (triangulated) groups get per-face independent projection.
    for group in find_connected_groups(wall_faces):
        if _is_linear_chain(group):
            project_wall_group(group, uv_layer, world_matrix)
            all_groups.append(group)
        else:
            for face in group:
                project_face_independent(face, uv_layer, world_matrix)
                all_groups.append([face])

    # Island faces — each sub-material processed separately so the
    # auto-coloured boundaries are respected as hard island boundaries.
    for sub_name in sorted(island_faces.keys(), key=lambda n: (_island_sub_index(n) or _chain_index(n) or 0)):
        faces = island_faces[sub_name]
        for group in find_connected_groups(faces):
            project_wall_group(group, uv_layer, world_matrix)
            all_groups.append(group)

    pack_islands(bm, uv_layer, all_groups)

    # Zero out UV loops on skipped faces (Ignore, unrecognised, bare marker).
    packed_faces = {face.index for group in all_groups for face in group}
    for face in bm.faces:
        if face.index not in packed_faces:
            for loop in face.loops:
                loop[uv_layer].uv = (0.0, 0.0)

    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    _write_uv_colours(mesh)

    total_island = sum(len(v) for v in island_faces.values())
    return len(floor_faces) + len(ramp_faces) + len(wall_faces) + total_island


# ─── UV Map List Operators ────────────────────────────────────────────────────

class OT_FBXMT_UV_Add(Operator):
    bl_idname = "fbxmt.uv_add"
    bl_label = "Add UV Map"
    bl_description = "Add a new UV map to the active object"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        context.active_object.data.uv_layers.new(name="UVMap")
        return {'FINISHED'}


class OT_FBXMT_UV_Remove(Operator):
    bl_idname = "fbxmt.uv_remove"
    bl_label = "Remove UV Map"
    bl_description = "Remove the active UV map from the active object"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and len(obj.data.uv_layers) > 0

    def execute(self, context):
        mesh   = context.active_object.data
        active = mesh.uv_layers.active
        if active and active.name == LIGHTMAP_CHANNEL_NAME:
            self.report({'WARNING'}, f'{LIGHTMAP_CHANNEL_NAME} is protected - remove via Clear UV Maps if intentional')
            return {'CANCELLED'}
        if active:
            mesh.uv_layers.remove(active)
        return {'FINISHED'}


# ─── Main Unwrap Operator ─────────────────────────────────────────────────────

class OT_FBXMT_UV_Unwrap(Operator):
    bl_idname = "fbxmt.uv_unwrap"
    bl_label = "Mapper UV Unwrap"
    bl_description = (
        "Object mode: unwrap all M_FBXMT-material faces on selected objects. "
        "Edit mode: unwrap selected M_FBXMT-material faces only. "
        "Faces with no M_FBXMT material are skipped."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode == 'EDIT_MESH':
            return False  # disabled in edit mode — causes materials to go black
        if context.mode == 'OBJECT':
            return any(obj.type == 'MESH' for obj in context.selected_objects)
        return False

    def execute(self, context):
        props               = context.scene.fbxmt_props
        floor_threshold_dot = math.cos(math.radians(props.uv_floor_threshold))
        edit_mode           = context.mode == 'EDIT_MESH'

        if edit_mode:
            obj = context.active_object
            bpy.ops.object.mode_set(mode='OBJECT')
            count = unwrap_mesh(
                obj.data, obj.matrix_world, floor_threshold_dot, selected_only=True
            )
            _build_preview_uv(obj.data)
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'INFO'}, f"Unwrapped {count} selected face(s)")
        else:
            mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
            total = 0
            for obj in mesh_objects:
                total += unwrap_mesh(
                    obj.data, obj.matrix_world, floor_threshold_dot, selected_only=False
                )
                _build_preview_uv(obj.data)
            self.report({'INFO'}, f"Unwrapped {total} face(s) across {len(mesh_objects)} object(s)")

        return {'FINISHED'}


# ─── UV Preview Mesh Operator ─────────────────────────────────────────────────

class OT_FBXMT_UV_Preview(Operator):
    """Build a flat mesh from UVMap coordinates and show it in a UV_Preview
    collection. Each selected object gets its own mesh, coloured by material.
    The result looks identical to the UV Editor but lives in the 3D viewport
    so Numpad-dot frames it correctly regardless of island extents.
    """
    bl_idname  = 'fbxmt.uv_preview'
    bl_label   = 'Preview UVs as Mesh'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == 'OBJECT' and
            any(obj.type == 'MESH' for obj in context.selected_objects)
        )

    def execute(self, context):
        import bmesh as _bmesh

        PREVIEW_COLLECTION = 'UV_Preview'
        UV_LAYER_NAME      = 'UVMap'

        # ── Clear existing preview collection ─────────────────────────────────
        if PREVIEW_COLLECTION in bpy.data.collections:
            col = bpy.data.collections[PREVIEW_COLLECTION]
            for obj in list(col.objects):
                bpy.data.meshes.remove(obj.data, do_unlink=True)
            bpy.data.collections.remove(col)

        preview_col = bpy.data.collections.new(PREVIEW_COLLECTION)
        context.scene.collection.children.link(preview_col)

        # ── Texel tile scale: 1 UV unit = tile_size world units ───────────────
        props     = context.scene.fbxmt_props
        tile_size = 1024.0 / props.geo_texel_density  # metres per UV unit

        mesh_objects = [o for o in context.selected_objects if o.type == 'MESH']
        created      = 0

        for src_obj in mesh_objects:
            src_mesh = src_obj.data
            uv_layer = src_mesh.uv_layers.get(UV_LAYER_NAME)
            if not uv_layer:
                continue

            # ── Build flat mesh from UV coordinates ───────────────────────────
            new_mesh = bpy.data.meshes.new(f'UV_{src_obj.name}')
            bm       = _bmesh.new()

            uv_data = uv_layer.data  # one entry per loop

            # Collect per-polygon UV loops and build verts
            for poly in src_mesh.polygons:
                face_verts = []
                for loop_idx in poly.loop_indices:
                    uv  = uv_data[loop_idx].uv
                    vert = bm.verts.new((uv.x * tile_size, uv.y * tile_size, 0.0))
                    face_verts.append(vert)
                try:
                    bm.faces.new(face_verts)
                except Exception:
                    pass  # degenerate face — skip

            bm.to_mesh(new_mesh)
            bm.free()

            # ── Assign material colours matching source mesh ───────────────────
            # One material slot per source material, same colour
            slot_mats = list(src_mesh.materials)
            for mat in slot_mats:
                if mat:
                    new_mesh.materials.append(mat)

            # Assign material_index per face to match source
            for i, poly in enumerate(src_mesh.polygons):
                if i < len(new_mesh.polygons):
                    new_mesh.polygons[i].material_index = poly.material_index

            new_mesh.update()

            new_obj      = bpy.data.objects.new(f'UV_{src_obj.name}', new_mesh)
            new_obj.location = (0, 0, 0)
            preview_col.objects.link(new_obj)
            created += 1

        # ── Add a tile grid overlay at 0,0 ────────────────────────────────────
        # Draw a simple 4x4 grid of tile outlines so you can read UV placement
        grid_mesh = bpy.data.meshes.new('UV_Grid')
        bm        = _bmesh.new()
        grid_range = range(-2, 5)  # -2 to +4 tiles
        for gx in grid_range:
            for gy in grid_range:
                x0, y0 = gx * tile_size, gy * tile_size
                x1, y1 = x0 + tile_size, y0 + tile_size
                v0 = bm.verts.new((x0, y0, -0.001))
                v1 = bm.verts.new((x1, y0, -0.001))
                v2 = bm.verts.new((x1, y1, -0.001))
                v3 = bm.verts.new((x0, y1, -0.001))
                bm.edges.new((v0, v1))
                bm.edges.new((v1, v2))
                bm.edges.new((v2, v3))
                bm.edges.new((v3, v0))
        bm.to_mesh(grid_mesh)
        bm.free()
        grid_obj          = bpy.data.objects.new('UV_Grid', grid_mesh)
        grid_obj.location = (0, 0, 0)
        preview_col.objects.link(grid_obj)

        self.report({'INFO'}, f'UV preview built for {created} object(s) — delete UV_Preview collection to return to scene')

        # Select only the preview objects and enter local view
        # so the workspace is isolated to the UV mesh — delete collection to exit
        bpy.ops.object.select_all(action='DESELECT')
        for obj in preview_col.objects:
            obj.select_set(True)
        if preview_col.objects:
            context.view_layer.objects.active = list(preview_col.objects)[0]
        bpy.ops.view3d.localview()

        return {'FINISHED'}


# ─── Smart Pack Operator ──────────────────────────────────────────────────────

class OT_FBXMT_SmartPack(Operator):
    """Repack existing UV islands using the 3-pass informed shelf algorithm.
    Reads current UV coordinates, translates islands only (no scale/rotate/flip).
    Run after Unwrap to optimise packing toward the most square bounding box.
    """
    bl_idname  = 'fbxmt.smart_pack'
    bl_label   = 'Smart Pack UVs'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT' and
                any(obj.type == 'MESH' for obj in context.selected_objects))

    def execute(self, context):
        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not mesh_objects:
            self.report({'WARNING'}, 'No mesh objects selected')
            return {'CANCELLED'}

        total_islands = 0
        for obj in mesh_objects:
            mesh = obj.data
            bm   = bmesh.new()
            bm.from_mesh(mesh)
            bm.faces.ensure_lookup_table()

            uv_layer = bm.loops.layers.uv.get('UVMap')
            if uv_layer is None:
                bm.free()
                self.report({'WARNING'}, f'{obj.name}: no UVMap channel found — skipped')
                continue

            mat_names = [m.name if m else None for m in mesh.materials]
            result = smart_pack_islands(bm, uv_layer, mat_names)

            if 'error' in result:
                bm.free()
                self.report({'WARNING'}, f'{obj.name}: {result["error"]}')
                continue

            bm.to_mesh(mesh)
            bm.free()
            mesh.update()
            _write_uv_colours(mesh)
            _build_preview_uv(mesh)
            total_islands += result['islands']

        if total_islands:
            self.report({'INFO'}, f'Smart Pack complete — {total_islands} island(s) across {len(mesh_objects)} object(s). Set UV editor overlay colour attribute to FBXMT_Colours to see island colours.')
        return {'FINISHED'}
