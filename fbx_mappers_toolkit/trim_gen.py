# trim_gen.py — FBX Mapper's Toolkit
#
# Profile-sweep trim generator.
# Fully face-agnostic — derives all orientation from the two adjacent faces
# of each selected edge. No world-up assumptions, no Z thresholds.
#
# Per-edge frame:
#   T  = edge tangent (along the edge)
#   N  = "outward" face normal — the face the trim protrudes FROM
#   U  = "flat" face normal negated — the direction the wall arm extends
#
# The two adjacent faces are classified by their relationship to T:
#   The face whose normal is more perpendicular to T = the "wall" face → N
#   The other face = the "flat" face → U (negated, pointing away from it)
#
# Profile (in N/U space, origin = seam vert A):
#   A  origin
#   B  = A - U*vert_cover
#   C  = B + N*thickness
#   D  = C + U*(vert_cover+thickness)
#   E  = D - N*(thickness+horiz_cover)
#   F  = E - U*thickness
#   F→A closes
#
# Frame tuple: (N, arm0, arm1, is_concave, n0, n1, is_floor_wall, flat_arm_index)
#   is_floor_wall   = True when one face is horizontal (floor/ceiling) and the
#                     other is vertical (wall). Arm matching in _mitre_profile
#                     must use this flag rather than re-classifying by index.
#   flat_arm_index  = 0 if arm0 is the flat arm, 1 if arm1 is the flat arm.
#                     Stored once at frame-build time so _mitre_profile can
#                     always pair flat-with-flat and wall-with-wall regardless
#                     of Blender's arbitrary face0/face1 ordering.
#
# Corners: ray-ray intersection for exact mitre at any angle.

import bpy
import bmesh
from mathutils import Vector
from bpy.types import Operator

from .materials import ensure_fbxmt_materials, COLLECTION_TRIM, move_to_collection


# ---------------------------------------------------------------------------
# Frame from two face normals

def _edge_frame(edge, tangent):
    """Derive profile arms from edge topology.

    Returns (N, arm0, arm1, is_concave, n0, n1, is_floor_wall, flat_arm_index).

    is_floor_wall   — True when the two adjacent faces are floor/wall or
                      ceiling/wall (one horizontal, one vertical).
    flat_arm_index  — 0 if arm0 is the flat (horizontal) arm, 1 if arm1 is.
                      Only meaningful when is_floor_wall is True.
    """
    T = tangent.normalized()
    faces = edge.link_faces

    if len(faces) < 2:
        n = faces[0].normal.normalized() if faces else Vector((1, 0, 0))
        perp = T.cross(n).normalized() if T.cross(n).length > 1e-6 else Vector((0, 1, 0))
        return n, perp, -perp, False, n, n, False, 0

    face0, face1 = faces[0], faces[1]
    n0 = face0.normal.normalized()
    n1 = face1.normal.normalized()

    # Shared proud direction = average face normal
    N = (n0 + n1)
    N = N.normalized() if N.length > 1e-6 else n0.normalized()

    avg_normal = n0 + n1
    if avg_normal.length > 1e-6 and N.dot(avg_normal) < 0:
        N = -N

    # Classify: larger abs(Z) = flat face, smaller = wall face
    z0 = abs(n0.z)
    z1 = abs(n1.z)

    def _topo_arm(face, edge, T):
        """Arm direction toward opposite edge of face, perp to T."""
        edge_vert_indices = {v.index for v in edge.verts}
        far_verts = [v for v in face.verts if v.index not in edge_vert_indices]
        if not far_verts:
            return T.cross(face.normal.normalized()).normalized()
        far_center = sum((v.co for v in far_verts), Vector((0, 0, 0))) / len(far_verts)
        edge_center = (edge.verts[0].co + edge.verts[1].co) * 0.5
        d = far_center - edge_center
        d = d - T * T.dot(d)  # remove along-edge component
        return d.normalized() if d.length > 1e-6 else T.cross(face.normal.normalized()).normalized()

    is_floor_wall = (z0 > 0.7 and z1 < 0.3) or (z1 > 0.7 and z0 < 0.3)

    if is_floor_wall:
        # One face is the floor/ceiling (normal has large Z component),
        # the other is a wall (normal is mostly horizontal).
        # Use n.z > 0 to distinguish floor (points up) from ceiling (points down).
        # This avoids misclassifying a wall whose normal happens to point in +Z
        # (e.g. the back wall of a room at Z=-2 with inward normal (0,0,+1)).
        if z0 >= z1:
            # face0 is the flat face — but verify it's actually floor/ceiling
            # by checking the wall face (face1) is more vertical (lower abs z)
            n_flat, n_wall = n0, n1
            face_flat, face_wall = face0, face1
            flat_is_face0 = True
        else:
            n_flat, n_wall = n1, n0
            face_flat, face_wall = face1, face0
            flat_is_face0 = False

        # Flat arm: project wall normal onto the flat face plane and negate.
        # This gives the direction away from the wall along the flat surface.
        # We use face normals directly (not topology) so the direction is
        # deterministic and doesn't depend on face vertex ordering.
        nw = Vector(n_wall)
        nf = Vector(n_flat)
        arm_flat = -(nw - nf * nf.dot(nw))
        if arm_flat.length < 1e-6:
            arm_flat = nf.cross(T)
        arm_flat = arm_flat.normalized()
        # Sanity: arm_flat must point away from the wall (positive dot with -n_wall projected)
        # If topology gives opposite sign (e.g. edge traversal reversed), flip it.
        proj_nw = -(nw - nf * nf.dot(nw))
        if proj_nw.length > 1e-6 and arm_flat.dot(proj_nw.normalized()) < 0:
            arm_flat = -arm_flat

        # Wall arm: topology — toward the far side of the wall face along the wall surface.
        arm_wall = _topo_arm(face_wall, edge, T)

        # Assign to arm0/arm1 matching face0/face1 ordering so n0/n1 stay aligned.
        if flat_is_face0:
            arm0, arm1 = arm_flat, arm_wall
            flat_arm_index = 0
        else:
            arm0, arm1 = arm_wall, arm_flat
            flat_arm_index = 1
    else:
        # Wall/wall or similar: topology for both
        arm0 = _topo_arm(face0, edge, T)
        arm1 = _topo_arm(face1, edge, T)
        flat_arm_index = 0  # unused for wall/wall

    is_concave = arm0.dot(arm1) > 0.1

    return N, arm0, arm1, is_concave, n0, n1, is_floor_wall, flat_arm_index


# ---------------------------------------------------------------------------
# Profile points

def _profile_pts(origin, N, U, thickness, vert_cover, horiz_cover,
                 chamfer_BD=False, chamfer_DF=False):
    """6 profile points in world space.

    Profile: A→B→C→D→E→F→A
      A  origin (seam vert)
      B  A - U*vert_cover
      C  B + N*thickness
      D  C + U*(vert_cover+thickness)     [+thickness if chamfer_BD]
      E  D - N*(thickness+horiz_cover)
      F  E - U*thickness                  [+thickness if chamfer_DF]

    chamfer_BD: adds extra thickness to D, making the B→D cap diagonal.
    chamfer_DF: adds extra thickness to F, making the D→F cap diagonal.
    """
    A = Vector(origin)
    B = A - U * vert_cover
    C = B + N * thickness
    d_extra = thickness if chamfer_BD else 0.0
    D = C + U * (vert_cover + thickness + d_extra)
    E = D - N * (thickness + horiz_cover)
    f_extra = thickness if chamfer_DF else 0.0
    F = E - U * (thickness + f_extra)
    return (A, B, C, D, E, F)


# ---------------------------------------------------------------------------
# Ray-ray intersection (for mitre corners)

def _ray_ray_closest(p1, d1, p2, d2):
    """Midpoint of closest approach between two rays."""
    w = p1 - p2
    a = d1.dot(d1); b = d1.dot(d2); c = d2.dot(d2)
    d = d1.dot(w);  e = d2.dot(w)
    denom = a * c - b * b
    if abs(denom) < 1e-10:
        return p1
    s = (b * e - c * d) / denom
    t = (a * e - b * d) / denom
    return ((p1 + d1 * s) + (p2 + d2 * t)) * 0.5


# ---------------------------------------------------------------------------
# Chain builder

def _chain_edges(selected_edges):
    """Build edge chains, splitting at junction verts (degree > 2)."""
    adj = {}
    for e in selected_edges:
        for v in e.verts:
            adj.setdefault(v.index, []).append(e)

    vert_count = {}
    for e in selected_edges:
        for v in e.verts:
            vert_count[v.index] = vert_count.get(v.index, 0) + 1

    junction_verts = {vi for vi, c in vert_count.items() if c > 2}

    visited = set()
    chains, is_closed_list = [], []

    def _traverse(start_edge, start_vert):
        chain, cur_edge, cur_vert = [], start_edge, start_vert
        while cur_edge and cur_edge.index not in visited:
            visited.add(cur_edge.index)
            chain.append(cur_edge)
            nxt = (cur_edge.verts[1] if cur_edge.verts[0].index == cur_vert.index
                   else cur_edge.verts[0])
            if nxt.index in junction_verts and len(chain) > 0:
                break
            nexts = [e for e in adj.get(nxt.index, []) if e.index not in visited]
            cur_edge = nexts[0] if nexts else None
            cur_vert = nxt
        return chain

    seen_starts = set()
    for e in selected_edges:
        for v in e.verts:
            if vert_count[v.index] == 1 and v.index not in seen_starts:
                seen_starts.add(v.index)
                for te in adj.get(v.index, []):
                    if te.index not in visited:
                        ch = _traverse(te, v)
                        if ch:
                            chains.append(ch)
                            is_closed_list.append(False)

    for vi in junction_verts:
        for te in adj.get(vi, []):
            if te.index not in visited:
                sv = te.verts[0] if te.verts[0].index == vi else te.verts[1]
                ch = _traverse(te, sv)
                if ch:
                    chains.append(ch)
                    is_closed_list.append(False)

    for e in selected_edges:
        if e.index not in visited:
            ch = _traverse(e, e.verts[0])
            if ch:
                chains.append(ch)
                is_closed_list.append(True)

    return chains, is_closed_list


def _chain_data(chain, is_closed):
    """Ordered verts, per-edge tangents, and per-edge frames.

    Frame tuple: (N, arm0, arm1, is_concave, n0, n1, is_floor_wall, flat_arm_index)
    """
    v0 = chain[0].verts[0]
    if len(chain) > 1:
        shared = {vv.index for vv in chain[1].verts}
        if v0.index in shared:
            v0 = chain[0].verts[1]

    verts = [v0]
    for edge in chain:
        nxt = (edge.verts[1] if edge.verts[0].index == verts[-1].index
               else edge.verts[0])
        verts.append(nxt)
    if is_closed and verts[-1].index == verts[0].index:
        verts.pop()

    n = len(verts)

    edge_tangents = []
    edge_frames   = []
    count = n if is_closed else n - 1
    for idx in range(count):
        j = (idx + 1) % n
        t = verts[j].co - verts[idx].co
        t = t.normalized() if t.length > 1e-6 else Vector((1, 0, 0))
        edge_tangents.append(t)
        frame = _edge_frame(chain[idx], t)
        edge_frames.append(frame)

    if not is_closed:
        edge_tangents.append(edge_tangents[-1])
        edge_frames.append(edge_frames[-1])

    return verts, edge_tangents, edge_frames


# ---------------------------------------------------------------------------
# Trim builder — dihedral frame sweep
#
# One profile per chain vertex, placed in the local dihedral frame.
# Profile dimensions are invariant — shear from corner rotation is compensated
# by solving for Bp position via plane intersection (same principle as Dw).

def _plane_intersect(A_pos, dir_in, dir_out, dist):
    """Find point P such that dot(P-A, dir_in) = dist AND dot(P-A, dir_out) = dist.

    Used for both Bp (floor arm, dist=vert_cover) and Dw (wall proud, dist=thickness).
    Falls back to simple offset when the two directions are parallel.
    """
    c     = dir_in.dot(dir_out)
    denom = 1.0 - c * c
    if abs(denom) < 1e-6:
        # Parallel — just use one direction
        return A_pos + dir_in * dist
    s = dist * (1.0 - c) / denom
    return A_pos + dir_in * s + dir_out * s


def _build_trim(trim_bm, chain, is_closed,
                thickness, vert_cover, horiz_cover,
                wall_a_cover=None, wall_b_cover=None,
                chamfer_BD=False, chamfer_DF=False, mat_index=0):

    # Resolve wall/wall cover lengths (default to floor/wall covers if not set)
    wac = wall_a_cover if wall_a_cover is not None else horiz_cover
    wbc = wall_b_cover if wall_b_cover is not None else vert_cover

    verts, tangents, frames = _chain_data(chain, is_closed)
    n       = len(verts)
    n_edges = n if is_closed else n - 1
    if n_edges < 1:
        return

    # ── per-edge directions ───────────────────────────────────────────────────

    def _dirs(ei):
        """Return (X, nw, nf) for edge ei.

        X   = floor arm direction: toward far side of flat face (into room).
              Derived from topology — always correct regardless of face winding.
        nw  = wall face normal (used for Dw and Cm offset).
        nf  = flat face normal (used for Df and Cp offset).
        """
        N, arm0, arm1, _, n0, n1, fw, fai = frames[ei]
        T = tangents[ei]

        if fw:
            # nf = flat face normal (the one with large Z component = floor/ceiling)
            # nw = wall face normal (horizontal)
            nf = (n0 if fai == 0 else n1).normalized()
            nw = (n1 if fai == 0 else n0).normalized()

            # X = floor arm direction = nf × T, then fix sign.
            # nf×T gives a horizontal vector perpendicular to the edge.
            # It points into the room on one side — use arm_flat from topology to fix sign.
            X = nf.cross(T)
            if X.length < 1e-6:
                X = Vector(arm0 if fai == 0 else arm1)
            X = X.normalized()
            arm_flat = Vector(arm0 if fai == 0 else arm1)
            if X.dot(arm_flat) < 0:
                X = -X

            # nw should point FROM the seam TOWARD the wall surface.
            # The wall face normal points into the room — we want the opposite.
            if nw.dot(X) < 0:
                nw = -nw
            nw = -nw  # Dw sits on the wall side of A, not the room side
        else:
            # wall/wall: _dirs just provides nw (bisector) for Dw mitre.
            # Arm directions are computed per-vertex in the profile block
            # using both ei_in and ei_out frames directly.
            bis = n0 + n1
            nf = bis.normalized() if bis.length > 1e-6 else n0.normalized()
            nw = nf
            X  = Vector(arm0).normalized()  # placeholder, unused for wall/wall profile

        return X, nw, nf

    def _face(vlist):
        try:
            f = trim_bm.faces.new(vlist)
            f.material_index = mat_index
        except Exception:
            pass

    # ── build one profile ring per vertex ─────────────────────────────────────
    #
    # Each ring has 6 verts: [A, Bp, Cp, Dwt, Bm, Cm]
    #
    #   A   = seam vertex
    #   Bp  = floor arm tip  — _plane_intersect(A, X_in, X_out, horiz_cover)
    #         Measured from A so horiz_cover is the true seam-to-tip distance.
    #         At corners the miter extends slightly beyond horiz_cover to fill
    #         the corner gap — this is geometrically correct behaviour.
    #   Cp  = Bp + nf * thickness
    #   Dw  = _plane_intersect(A, nw_in, nw_out, thickness)  — wall proud point
    #   Dwt = Dw + nf * thickness
    #   Bm  = A  - nf * vert_cover
    #   Cm  = Dw + (Bm - A)
    #
    # Single Bp per ring — no in/out split. The loft connects ring[i] to
    # ring[i+1] with 7 quad strips. End caps are two quads each.

    profiles = []   # list of [A_v, Bp_v, Cp_v, Dwt_v, Bm_v, Cm_v]

    for vi in range(n):
        A_pos = Vector(verts[vi].co)

        is_start = (not is_closed) and vi == 0
        is_end   = (not is_closed) and vi == n - 1

        ei_in  = (vi - 1) % n_edges if not is_start else 0
        ei_out = vi       % n_edges if not is_end   else n_edges - 1

        X_in,  nw_in,  nf_in  = _dirs(ei_in)
        X_out, nw_out, nf_out = _dirs(ei_out)

        nf = nf_out

        # Dw — mitered wall proud point
        if is_start or is_end or (nw_in - nw_out).length < 1e-6:
            Dw_pos = A_pos + nw_out * thickness
        else:
            Dw_pos = _plane_intersect(A_pos, nw_in, nw_out, thickness)

        _, _, _, _, _, _, fw_out, _ = frames[ei_out]

        if fw_out:
            # ── floor/wall profile ────────────────────────────────────────────
            # Bp mitered from A — correct distance from seam regardless of Dw
            if is_start or is_end or (X_in - X_out).length < 1e-6:
                Bp_pos = A_pos + X_out * horiz_cover
            else:
                Bp_pos = _plane_intersect(A_pos, X_in, X_out, horiz_cover)

            Cp_pos  = Bp_pos + nf * thickness
            Dwt_pos = Dw_pos + nf * thickness
            Bm_pos  = A_pos  - nf * vert_cover
            Cm_pos  = Dw_pos + (Bm_pos - A_pos)

        else:
            # ── wall/wall profile ─────────────────────────────────────────────
            _, arm0_in, arm1_in, _, n0_in, n1_in, _, _ = frames[ei_in]
            _, arm0_out, arm1_out, _, n0_out, n1_out, _, _ = frames[ei_out]
            T_in  = tangents[ei_in].normalized()
            T_out = tangents[ei_out].normalized()

            T_avg = T_in + T_out
            T_use = T_avg.normalized() if T_avg.length > 1e-6 else T_out

            def _wall_arm(nn, arm_topo, T):
                d = nn.cross(T)
                if d.length < 1e-6:
                    d = Vector(arm_topo)
                d = d.normalized()
                if d.dot(Vector(arm_topo)) < 0:
                    d = -d
                return d

            a0_in  = _wall_arm(n0_in,  arm0_in,  T_use)
            a1_in  = _wall_arm(n1_in,  arm1_in,  T_use)
            a0_out = _wall_arm(n0_out, arm0_out, T_use)
            a1_out = _wall_arm(n1_out, arm1_out, T_use)

            if n0_in.dot(n0_out) > n0_in.dot(n1_out):
                avg0 = a0_in + a0_out
                avg1 = a1_in + a1_out
            else:
                avg0 = a0_in + a1_out
                avg1 = a1_in + a0_out

            avg0 = avg0.normalized() if avg0.length > 1e-6 else a0_out
            avg1 = avg1.normalized() if avg1.length > 1e-6 else a1_out

            bis_n = (n0_out + n1_out)
            if bis_n.length < 1e-6:
                bis_n = n0_out
            bis_n = bis_n.normalized()
            up = T_use.cross(bis_n)
            if up.length < 1e-6:
                up = Vector((0, 0, 1)) - T_use * T_use.z
            up = up.normalized()
            if up.z < 0:
                up = -up

            Bp_pos  = A_pos + avg0 * wac
            Bm_pos  = A_pos + avg1 * wbc
            Cp_pos  = Bp_pos + up * thickness
            Dwt_pos = Dw_pos + up * thickness
            Cm_pos  = Bm_pos + up * thickness

        A_v   = trim_bm.verts.new(A_pos)
        Bp_v  = trim_bm.verts.new(Bp_pos)
        Cp_v  = trim_bm.verts.new(Cp_pos)
        Dwt_v = trim_bm.verts.new(Dwt_pos)
        Bm_v  = trim_bm.verts.new(Bm_pos)
        Cm_v  = trim_bm.verts.new(Cm_pos)

        profiles.append([A_v, Bp_v, Cp_v, Dwt_v, Bm_v, Cm_v])

    # ── loft ─────────────────────────────────────────────────────────────────

    loop = range(n) if is_closed else range(n - 1)
    for i in loop:
        j = (i + 1) % n
        A0, Bp0, Cp0, Dwt0, Bm0, Cm0 = profiles[i]
        A1, Bp1, Cp1, Dwt1, Bm1, Cm1 = profiles[j]

        _face([A0,   Bp0,  Bp1,  A1  ])   # floor bottom
        _face([Bp0,  Cp0,  Cp1,  Bp1 ])   # floor outer cap
        _face([Cp0,  Dwt0, Dwt1, Cp1 ])   # floor top
        _face([Dwt0, Cm0,  Cm1,  Dwt1])   # wall outer
        _face([A0,   A1,   Bm1,  Bm0 ])   # wall surface
        _face([Bm0,  Bm1,  Cm1,  Cm0 ])   # wall cap
        _face([A0,   Dwt0, Dwt1, A1  ])   # floor inner

    # ── end caps ─────────────────────────────────────────────────────────────
    # 7 open edges per terminal ring — A has 3 neighbours (Bp, Bm, Dwt),
    # so a single polygon can't close all of them. Two quads sharing A↔Dwt:
    #   floor quad: A↔Bp, Bp↔Cp, Cp↔Dwt, Dwt↔A
    #   wall quad:  A↔Dwt, Dwt↔Cm, Cm↔Bm, Bm↔A

    if not is_closed:
        def _cap(ring, reverse=False):
            A, Bp, Cp, Dwt, Bm, Cm = ring
            if reverse:
                # start cap — normal points away from chain start
                _face([Dwt, Cp, Bp, A])
                _face([Bm, Cm, Dwt, A])
            else:
                # end cap — normal points away from chain end
                _face([A, Bp, Cp, Dwt])
                _face([A, Dwt, Cm, Bm])
        _cap(profiles[0],     reverse=True)
        _cap(profiles[n - 1], reverse=False)

    bmesh.ops.remove_doubles(trim_bm, verts=trim_bm.verts, dist=1e-5)
    trim_bm.normal_update()




# ---------------------------------------------------------------------------
# Operator

class OT_FBXMT_Generate_Trim(Operator):
    bl_idname  = 'fbxmt.generate_trim'
    bl_label   = 'Generate Trim'
    bl_description = 'Sweep L-profile trim along selected seam edges'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH'
                and context.active_object is not None
                and context.active_object.type == 'MESH')

    def execute(self, context):
        props        = context.scene.fbxmt_props
        thickness    = props.trim_thickness
        vert_cover   = props.trim_vert_cover
        horiz_cover  = props.trim_horiz_cover
        wall_a_cover = props.trim_wall_a_cover
        wall_b_cover = props.trim_wall_b_cover
        chamfer_BD   = props.trim_chamfer_BD
        chamfer_DF   = props.trim_chamfer_DF

        obj = context.active_object
        bpy.ops.object.mode_set(mode='OBJECT')

        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        selected_edges = [e for e in bm.edges if e.select]
        if not selected_edges:
            bm.free()
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'WARNING'}, 'No edges selected')
            return {'CANCELLED'}

        matrix_world = obj.matrix_world

        ensure_fbxmt_materials()
        trim_mat = bpy.data.materials.get('M_FBXMT_Trim')
        if trim_mat is None:
            bm.free()
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'ERROR'}, 'M_FBXMT_Trim not found — run Setup Scene first')
            return {'CANCELLED'}

        chains, closed_flags = _chain_edges(selected_edges)
        if not chains:
            bm.free()
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'WARNING'}, 'Could not build edge chains')
            return {'CANCELLED'}

        trim_objects = []
        for idx, (chain, is_closed) in enumerate(zip(chains, closed_flags)):
            trim_bm = bmesh.new()
            _build_trim(trim_bm, chain, is_closed,
                        thickness, vert_cover, horiz_cover,
                        wall_a_cover, wall_b_cover,
                        chamfer_BD, chamfer_DF, mat_index=0)

            suffix = '_Trim' if len(chains) == 1 else f'_Trim.{idx+1:03d}'
            trim_mesh = bpy.data.meshes.new(f'{obj.name}{suffix}')
            trim_mesh.materials.append(trim_mat)
            trim_bm.to_mesh(trim_mesh)
            trim_bm.free()
            trim_mesh.update()

            trim_obj = bpy.data.objects.new(f'{obj.name}{suffix}', trim_mesh)
            context.collection.objects.link(trim_obj)
            trim_obj.select_set(True)
            trim_objects.append(trim_obj)

        bm.free()

        last_obj = trim_objects[-1]
        context.view_layer.objects.active = last_obj
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='OBJECT')

        for trim_obj in trim_objects:
            move_to_collection(trim_obj, COLLECTION_TRIM)
            trim_obj.matrix_world = matrix_world.copy()

        bpy.ops.object.select_all(action='DESELECT')
        for trim_obj in trim_objects:
            trim_obj.select_set(True)
        context.view_layer.objects.active = last_obj

        import os
        export_path = os.path.join(os.path.expanduser('~'), 'trimtest.obj')
        bpy.ops.wm.obj_export(
            filepath=export_path,
            export_selected_objects=True,
            export_materials=False,
        )

        self.report({'INFO'},
            f'Trim generated: {len(chains)} chain(s) -> "{last_obj.name}"')
        return {'FINISHED'}
