"""
trim_overlay.py — Real-time A/B face overlay for trim_gen2.

When the user selects edges in edit mode on a mesh, this module:
  - Reads the first edge's two linked faces to determine A (floor) and B (wall)
  - Collects all A and B faces across the whole selected edge run
  - Draws a translucent GPU overlay: BLUE for A faces, YELLOW for B faces
  - Clears automatically when selection changes or is empty

Blue  = A face (floor/flat)
Yellow = B face (wall)
"""

import bpy
import bmesh
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

# ── Module-level overlay state ───────────────────────────────────────────────
_draw_handle   = None   # SpaceView3D draw handler
_overlay_tris  = []     # list of (color, [Vector, Vector, Vector]) triangles
_overlay_obj   = None   # object name the overlay was built for

COLOUR_A = (0.15, 0.50, 1.00, 0.30)   # blue,   A face (floor)
COLOUR_B = (1.00, 0.85, 0.00, 0.30)   # yellow, B face (wall)


# ── GPU draw callback ─────────────────────────────────────────────────────────
def _draw_overlay():
    if not _overlay_tris:
        return

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.face_culling_set('NONE')

    # Group tris by colour to minimise shader calls
    groups = {}
    for colour, tri in _overlay_tris:
        groups.setdefault(colour, []).extend(tri)

    for colour, coords in groups.items():
        batch = batch_for_shader(shader, 'TRIS', {'pos': coords})
        shader.bind()
        shader.uniform_float('color', colour)
        batch.draw(shader)

    gpu.state.blend_set('NONE')
    gpu.state.depth_test_set('NONE')
    gpu.state.face_culling_set('NONE')


# ── Overlay data builder ──────────────────────────────────────────────────────
def _face_to_tris(face, matrix_world):
    """Tessellate a BMFace correctly (handles concave ngons).
    Offsets verts slightly along face normal to avoid z-fighting.
    Uses mathutils.geometry.tessellate_polygon which is robust in all contexts.
    """
    from mathutils.geometry import tessellate_polygon
    normal = face.normal.normalized()
    offset = 0.005  # 5mm push along normal to avoid z-fighting
    # Collect local coords for tessellator (needs flat list of 2D or 3D verts)
    local_verts = [v.co + normal * offset for v in face.verts]
    # tessellate_polygon takes a list of vert lists (one per contour)
    indices = tessellate_polygon([local_verts])
    tris = []
    for tri_idx in indices:
        tri = tuple(matrix_world @ local_verts[i] for i in tri_idx)
        tris.append(tri)
    return tris


def _classify_face_a(face, normal_mat):
    """True if face is the A (flat/floor) face."""
    n = face.normal.normalized()
    wn = (normal_mat @ n).normalized() if normal_mat else n
    # Primary: abs(z) of world normal
    if abs(wn.z) > 0.7:
        return True
    if abs(wn.z) < 0.3:
        return False
    # Ambiguous: use Z geometric extent
    z_ext = max(v.co.z for v in face.verts) - min(v.co.z for v in face.verts)
    return z_ext < 0.5


def build_overlay(obj, selected_edges):
    """Rebuild _overlay_tris from selected edges on obj."""
    global _overlay_tris, _overlay_obj

    _overlay_tris = []
    _overlay_obj  = obj.name if obj else None

    if not selected_edges or not obj:
        return

    mw  = obj.matrix_world
    nm  = mw.to_3x3().normalized()

    seen_a = set()
    seen_b = set()

    # Determine A/B from the first selected edge's faces
    first_edge   = selected_edges[0]
    first_faces  = first_edge.link_faces
    if len(first_faces) < 2:
        return

    f0, f1 = first_faces[0], first_faces[1]
    face_index_A = f0.index if _classify_face_a(f0, nm) else f1.index

    # Walk all selected edges, collect A and B faces
    for edge in selected_edges:
        for face in edge.link_faces:
            fi = face.index
            if fi == face_index_A or _classify_face_a(face, nm):
                if fi not in seen_a:
                    seen_a.add(fi)
                    for tri in _face_to_tris(face, mw):
                        _overlay_tris.append((COLOUR_A, tri))
            else:
                if fi not in seen_b:
                    seen_b.add(fi)
                    for tri in _face_to_tris(face, mw):
                        _overlay_tris.append((COLOUR_B, tri))


def clear_overlay():
    global _overlay_tris, _overlay_obj
    _overlay_tris = []
    _overlay_obj  = None


# ── Handler registration ──────────────────────────────────────────────────────
def register_overlay():
    global _draw_handle
    if _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_overlay, (), 'WINDOW', 'POST_VIEW'
        )


def unregister_overlay():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    clear_overlay()
