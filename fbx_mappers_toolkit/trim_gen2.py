# trim_gen2.py — FBX Mapper's Toolkit
#
# Dihedral-frame trim generator.  Sweeps a 10-vert profile ring along
# selected edges using per-edge local frames derived from adjacent face normals.
#
# Architecture
# ------------
# * DihedralFrame: built per edge from its two linked face normals. Classifies
#   edge type (WF/WC/WR/WW) using world-space Z thresholds. WR edges are
#   overridden to a flat frame (WF/WC) so ramp profiles generate horizontally.
#   WW edges adjacent to a prior floor/ceiling frame inherit that nA.
#
# * _profile_ring: emits 10 verts per chain vertex. Foot positions use
#   _plane_intersect to solve the miter implicitly. Nose is the point at
#   exactly `thickness` from both face planes simultaneously.
#
# * _build_trim: one ring per chain vertex. At surface-type transitions,
#   a split pair (closing + opening ring) is emitted instead. All chains
#   build into a single BMesh; remove_doubles welds coincident split caps.
#
# Profile vertex layout (10 verts, 0-indexed)
# -------------------------------------------
#   v0  seam — on the shared edge, no offset
#   v1  foot A — depth_a along faceA surface from seam
#   v2  foot A half-lift — v1 + nA * thickness/2
#   v3  foot A tip — v1 + nA * thickness
#   v4  chamfer shoulder A — on line v3→v5, thickness from v5
#   v5  nose tip — at thickness from both face planes simultaneously
#   v6  chamfer shoulder B — on line v7→v5, thickness from v5
#   v7  foot B tip — v9 + nB * thickness
#   v8  foot B half-lift — v9 + nB * thickness/2
#   v9  foot B — depth_b along faceB surface from seam
#
# No-chamfer mode: v2=v1, v4=v3, v6=v7, v8=v9 (6 unique positions).

import math
import bpy
import bmesh
from mathutils import Vector
from bpy.types import Operator

from .materials import ensure_fbxmt_materials, COLLECTION_TRIM, move_to_collection


# ---------------------------------------------------------------------------
# Helpers

def _topo_arm(face, edge, T):
    """Direction from edge midpoint toward the far side of face, perp to T."""
    edge_vis = {v.index for v in edge.verts}
    far = [v for v in face.verts if v.index not in edge_vis]
    n = face.normal.normalized()

    def _fallback():
        # T × n gives a direction in the face plane perpendicular to T.
        # If T is nearly parallel to n (degenerate edge), use a world axis instead.
        cross = T.cross(n)
        if cross.length > 0.1:
            return cross.normalized()
        # T ≈ face normal — use any axis perpendicular to n
        for axis in (Vector((1,0,0)), Vector((0,1,0)), Vector((0,0,1))):
            cross = n.cross(axis)
            if cross.length > 0.1:
                return cross.normalized()
        return Vector((1, 0, 0))

    if not far:
        return _fallback()
    fc = sum((v.co for v in far), Vector()) / len(far)
    ec = (edge.verts[0].co + edge.verts[1].co) * 0.5
    d  = fc - ec
    d  = d - T * T.dot(d)
    return d.normalized() if d.length > 1e-6 else _fallback()


def _best_proud_neighbour(edge, linked_face_indices, bisector, current_arm, T):
    """Search faces reachable via edge verts for a better arm donor.

    Used ONLY for single-linked-face edges to find the missing second arm.
    A candidate face must sit in the proud-side halfspace and provide an arm
    meaningfully more perpendicular to the current arm than what we have.

    Score = 1 - |dot(current_arm, candidate_arm)|.
    A score > 0.5 means the candidate is at least 60° away from current_arm.
    """
    ec = (edge.verts[0].co + edge.verts[1].co) * 0.5
    best_score = -1.0
    best_arm   = None

    for vert in edge.verts:
        for linked_edge in vert.link_edges:
            for face in linked_edge.link_faces:
                if face.index in linked_face_indices:
                    continue
                fc = face.calc_center_median()
                if (fc - ec).dot(bisector) < 0.0:
                    continue
                cand_arm = _topo_arm(face, edge, T)
                dot = abs(current_arm.dot(cand_arm))
                score = 1.0 - dot
                if score > best_score:
                    best_score = score
                    best_arm   = cand_arm

    if best_arm is not None and best_score > 0.5:
        return best_arm
    return current_arm


# ---------------------------------------------------------------------------
# Per-edge dihedral frame

class DihedralFrame:
    """Local frame for one edge.

    Attributes
    ----------
    nA, nB      face normals (normalised, pointing toward open space)
    faceA       BMFace — the A face (flat: floor/ceiling/ramp)
    faceB       BMFace — the B face (wall)
    bisector    (nA + nB).normalised — direction the nose protrudes
    T           edge tangent (normalised)
    is_convex   True when the interior angle is convex
    dihedral    interior dihedral angle in radians
    edge_type   'WF', 'WC', 'WR', 'WW'

    Arms are NOT stored — foot positions are computed by projecting onto
    the face planes directly in _profile_ring, which gives correct mitering
    implicitly without any arm direction or miter scale computation.
    """
    __slots__ = ('nA', 'nB', 'faceA', 'faceB', 'bisector', 'T',
                 'is_convex', 'dihedral', 'half_angle', 'edge_type', 'edge', 'nA_world')

    def __init__(self, edge, tangent, face_index_A=None, normal_mat=None, prev_nA=None):
        T = tangent.normalized()
        self.T = T

        faces = edge.link_faces

        if len(faces) < 2:
            # Boundary edge — single face only. Use available face as A,
            # synthesise B normal from proud-side search.
            n = faces[0].normal.normalized() if faces else Vector((0, 0, 1))
            armA = _topo_arm(faces[0], edge, T) if faces else T.cross(n).normalized()
            armB = _best_proud_neighbour(edge, {faces[0].index} if faces else set(),
                                         n, -armA, T)
            nB = T.cross(armB).normalized()
            if nB.dot(n) < 0:
                nB = -nB
            self.faceA = faces[0] if faces else None
            self.faceB = None
            self.nA = n
            self.nB = nB
            self.nA_world = (normal_mat @ n).normalized() if normal_mat else n.copy()
            self.edge_type = 'WF'
            bis = (n + nB)
            self.bisector = bis.normalized() if bis.length > 1e-6 else n
            self.is_convex = armA.dot(armB) < 0.0
            cos_a = max(-1.0, min(1.0, n.dot(nB)))
            self.dihedral = math.acos(cos_a)
            self.half_angle = self.dihedral / 2.0
            return

        face0, face1 = faces[0], faces[1]

        # ── Assign A/B faces ─────────────────────────────────────────────────
        # Classify both faces independently using normal Z and geometric extent.
        def _flat_score(face):
            """Higher score = more likely to be the flat (A) face.
            Uses world-space normal Z first; falls back to face area.
            The flat face (floor/ceiling/ramp) is almost always larger
            than the wall strip — area is robust when normals are ambiguous.
            """
            n_world = (normal_mat @ face.normal.normalized()).normalized() if normal_mat else face.normal.normalized()
            nz = abs(n_world.z)
            if nz > 0.3:
                return nz
            return face.calc_area()

        s0 = _flat_score(face0)
        s1 = _flat_score(face1)

        if face_index_A is not None:
            # Prefer whichever face matches the established A index.
            if face0.index == face_index_A:
                fA, fB = face0, face1
            elif face1.index == face_index_A:
                fA, fB = face1, face0
            else:
                # Neither face matches — wall-corner edge where the floor ngon
                # is not directly linked. Use prev_nA similarity as fallback,
                # or score if prev_nA is unavailable.
                if prev_nA is not None:
                    d0 = abs(face0.normal.normalized().dot(prev_nA))
                    d1 = abs(face1.normal.normalized().dot(prev_nA))
                    fA, fB = (face0, face1) if d0 >= d1 else (face1, face0)
                else:
                    fA, fB = (face0, face1) if s0 >= s1 else (face1, face0)
        else:
            # First edge — classify by score
            fA, fB = (face0, face1) if s0 >= s1 else (face1, face0)

        nA = fA.normal.normalized()
        nB = fB.normal.normalized()

        # Classify etype from raw nA first, then orient nA toward the room.
        # WF/WC: nearly horizontal (abs(z)>0.95). WR: ramp (0.3<abs(z)<=0.95). WW: walls.
        w_nA_raw = (normal_mat @ nA).normalized() if normal_mat else nA
        w_nB_raw = (normal_mat @ nB).normalized() if normal_mat else nB
        nA_z = abs(w_nA_raw.z)
        nB_z = abs(w_nB_raw.z)
        if nA_z > 0.95:
            etype = 'WF' if w_nA_raw.z > 0 else 'WC'
        elif nA_z > 0.3 and nB_z < 0.5:
            etype = 'WR'
        else:
            etype = 'WW' if nB_z < 0.5 else 'WR'

        # Orient nA toward open space (room):
        # WF: room above floor → nA must point +Z.
        # WC: room below ceiling → nA must point -Z.
        # WR: ramp normal should point toward open space — same test as is_convex
        #     below: faceB's far vert sits on the solid side, so if d_to_fb points
        #     the same way as nA the normal is facing into the solid and must flip.
        #     This is world-axis-independent and correct for any ramp orientation.
        if etype == 'WF' and nA.z < 0:
            nA = -nA
        elif etype == 'WC' and nA.z > 0:
            nA = -nA
        elif etype == 'WR':
            # Use faceB's far-vert centroid: if it lies on the same side as nA
            # then nA is pointing into the solid — flip it.
            edge_vis_wr = {v.index for v in edge.verts}
            far_b_wr = [v for v in fB.verts if v.index not in edge_vis_wr]
            if far_b_wr:
                ec_wr  = (edge.verts[0].co + edge.verts[1].co) * 0.5
                fb_co_wr = sum((v.co for v in far_b_wr), Vector()) / len(far_b_wr)
                if (fb_co_wr - ec_wr).dot(nA) > 0:
                    nA = -nA
            else:
                # Degenerate faceB — fall back to nB agreement:
                # ramp nA should point away from the wall, i.e. dot(nA, nB) > 0.
                if nA.dot(nB) < 0:
                    nA = -nA

        # Store world-space nA for context-change detection
        self_nA_world = (normal_mat @ nA).normalized() if normal_mat else nA.copy()

        bis_raw = nA + nB
        if bis_raw.length < 1e-6:
            bis_raw = T.cross(nA)
        bis = bis_raw.normalized()

        cos_a = max(-1.0, min(1.0, nA.dot(nB)))
        dihedral = math.acos(cos_a)

        # is_convex: determined by which side of the shared edge faceA's
        # interior is on relative to faceB's normal.
        # _topo_arm gives direction from edge midpoint toward faceA interior.
        # If that direction aligns with nB (dot > 0): concave (faces open toward room).
        # If opposite (dot < 0): convex (faces open away from room).
        # This is T-direction independent and robust for any face shape.
        # is_convex: use a far vert of faceB to determine which side of
        # faceA's plane it sits on.
        # If faceB's far vert is on the same side as nA points → convex.
        # If on the opposite side → concave.
        # Using faceB (the wall, small quad) avoids large-ngon centroid errors.
        edge_vis = {v.index for v in edge.verts}
        far_b = [v for v in fB.verts if v.index not in edge_vis]
        if far_b:
            ec = (edge.verts[0].co + edge.verts[1].co) * 0.5
            fb_co = sum((v.co for v in far_b), Vector()) / len(far_b)
            d_to_fb = fb_co - ec
            is_convex = d_to_fb.dot(nA) < 0
        else:
            # Degenerate — fall back to topo_arm on faceA
            topo_a = _topo_arm(fA, edge, T)
            is_convex = topo_a.dot(nB) > 0

        self.faceA      = fA
        self.faceB      = fB
        self.nA         = nA
        self.nB         = nB
        self.bisector   = bis
        self.is_convex  = is_convex
        self.dihedral   = dihedral
        self.half_angle = dihedral / 2.0
        self.edge_type  = etype
        self.nA_world   = self_nA_world


# ---------------------------------------------------------------------------
# Profile ring at one chain vertex

def _profile_ring(seam_co, frame_in, frame_out,
                  depth_a, depth_b, thickness, chamfer,
                  is_start, is_end):
    """Return 10 world-space positions: v0..v9.

    Profile layout (0-indexed):
      v0   seam — exactly on the shared edge
      v1   foot A: projected depth_a along faceA surface from seam
      v2   foot A lift½: v1 + nA * thickness/2
      v3   foot A tip:   v1 + nA * thickness
      v4   chamfer A: on line v3→v5, thickness from v5
      v5   nose tip
      v6   chamfer B: on line v7→v5, thickness from v5
      v7   foot B tip:   v9 + nB * thickness
      v8   foot B lift½: v9 + nB * thickness/2
      v9   foot B: projected depth_b along faceB surface from seam

    Foot positions are computed by projecting onto the actual face surface.
    Miter compensation is implicit — at corners the face angle dictates the
    correct foot position automatically. No arm vectors or miter scale needed.
    """
    A = Vector(seam_co)

    def _avg(va, vb):
        s = va + vb
        return s.normalized() if s.length > 1e-6 else va.normalized()

    def _get_arm_a(frame, ref_nB):
        """Floor arm from topo, projected onto faceA plane, oriented by convex/concave."""
        a = _topo_arm(frame.faceA, frame.edge, frame.T)
        nA = frame.nA
        proj = a - nA * nA.dot(a)
        if proj.length > 1e-4:
            a = proj.normalized()
        if frame.is_convex:
            if a.dot(ref_nB) > 0:
                a = -a
        else:
            if a.dot(ref_nB) < 0:
                a = -a
        return a

    def _get_arm_b(frame, ref_nA):
        """Wall arm from topo, projected onto faceB plane, oriented by convex/concave."""
        if frame.faceB is None:
            return -_get_arm_a(frame, ref_nA)
        a = _topo_arm(frame.faceB, frame.edge, frame.T)
        nB = frame.nB
        proj = a - nB * nB.dot(a)
        if proj.length > 1e-4:
            a = proj.normalized()
        if frame.is_convex:
            if a.dot(ref_nA) > 0:
                a = -a
        else:
            if a.dot(ref_nA) < 0:
                a = -a
        return a

    def _plane_intersect(origin, dir_in, dir_out, dist):
        """Point P where dot(P-origin, dir_in)=dist AND dot(P-origin, dir_out)=dist.
        Equivalent to the miter intersection of two planar constraints.
        Falls back to simple offset when directions are parallel, antiparallel,
        or would produce an unreasonably large scale (degenerate short edges).
        """
        c = dir_in.dot(dir_out)
        denom = 1.0 - c * c
        if abs(denom) < 1e-6:
            return origin + dir_in * dist
        s = dist * (1.0 - c) / denom
        # Clamp: if miter scale > 4x, something is degenerate.
        # Use simple offset instead.
        if abs(s) > dist * 4.0:
            return origin + dir_in * dist
        result = origin + dir_in * s + dir_out * s
        # Hard cap: result must be within 10x dist of origin
        from mathutils import Vector as _V
        if (result - origin).length > dist * 10.0:
            return origin + dir_in * dist
        return result

    # Chamfer scale needed in all branches for foot_am/foot_bm
    cham_scale = 0.5 if chamfer == 'HALF' else 1.0

    if is_start:
        nA    = frame_out.nA
        nB    = frame_out.nB
        T     = frame_out.T
        arm_a = _get_arm_a(frame_out, nB)
        arm_b = _get_arm_b(frame_out, nA)
        foot_a  = A + arm_a * depth_a
        foot_b  = A + arm_b * depth_b
        foot_at_raw = foot_a + nA * thickness
        foot_bt_raw = foot_b + nB * thickness
        foot_at = foot_at_raw - arm_a * (cham_scale * thickness) if chamfer != 'NONE' else foot_at_raw
        foot_bt = foot_bt_raw - arm_b * (cham_scale * thickness) if chamfer != 'NONE' else foot_bt_raw
        foot_am = foot_a + nA * (thickness * 0.5)
        foot_bm = foot_b + nB * (thickness * 0.5)
        nose_raw = _plane_intersect(A, nA, nB, thickness)
        nose     = nose_raw
    elif is_end:
        nA    = frame_in.nA
        nB    = frame_in.nB
        T     = frame_in.T
        arm_a = _get_arm_a(frame_in, nB)
        arm_b = _get_arm_b(frame_in, nA)
        foot_a  = A + arm_a * depth_a
        foot_b  = A + arm_b * depth_b
        foot_at_raw = foot_a + nA * thickness
        foot_bt_raw = foot_b + nB * thickness
        foot_at = foot_at_raw - arm_a * (cham_scale * thickness) if chamfer != 'NONE' else foot_at_raw
        foot_bt = foot_bt_raw - arm_b * (cham_scale * thickness) if chamfer != 'NONE' else foot_bt_raw
        foot_am = foot_a + nA * (thickness * 0.5)
        foot_bm = foot_b + nB * (thickness * 0.5)
        nose_raw = _plane_intersect(A, nA, nB, thickness)
        nose     = nose_raw
    else:
        nA = _avg(frame_in.nA, frame_out.nA)
        nB = _avg(frame_in.nB, frame_out.nB)
        arm_a_in  = _get_arm_a(frame_in,  nB)
        arm_a_out = _get_arm_a(frame_out, nB)
        arm_b_in  = _get_arm_b(frame_in,  nA)
        arm_b_out = _get_arm_b(frame_out, nA)
        foot_a  = _plane_intersect(A, arm_a_in, arm_a_out, depth_a)
        foot_b  = _plane_intersect(A, arm_b_in, arm_b_out, depth_b)
        foot_at_raw = _plane_intersect(foot_a, frame_in.nA, frame_out.nA, thickness)
        foot_bt_raw = _plane_intersect(foot_b, frame_in.nB, frame_out.nB, thickness)
        arm_a   = _avg(arm_a_in, arm_a_out)
        arm_b   = _avg(arm_b_in, arm_b_out)
        foot_at = foot_at_raw - arm_a * (cham_scale * thickness) if chamfer != 'NONE' else foot_at_raw
        foot_bt = foot_bt_raw - arm_b * (cham_scale * thickness) if chamfer != 'NONE' else foot_bt_raw
        foot_am = foot_a + nA * (thickness * 0.5)
        foot_bm = foot_b + nB * (thickness * 0.5)
        # Nose: point at exactly thickness from floor plane AND both wall planes.
        # Solve the 3-constraint linear system: dot(P-A, nA)=t, dot(P-A, nB_in)=t,
        # dot(P-A, nB_out)=t. Falls back to 2-constraint when nB_in ≈ nB_out
        # (straight run — matrix would be singular).
        try:
            from mathutils import Matrix, Vector as _V
            _M = Matrix([frame_in.nA, frame_in.nB, frame_out.nB])
            _nose_candidate = A + _M.inverted() @ _V((thickness, thickness, thickness))
            # Guard against near-singular matrix producing a geometrically
            # valid but astronomically displaced result (no ValueError raised).
            if (_nose_candidate - A).length > thickness * 10.0:
                raise ValueError("near-singular")
            nose = _nose_candidate
        except ValueError:
            # Singular or near-singular — straight run, ramp switchback, or
            # other degenerate configuration. Use 2-constraint fallback.
            nose = _plane_intersect(A, nA, nB, thickness)

    # ── Chamfer shoulders ────────────────────────────────────────────────────
    # Shoulders sit exactly cham_scale * thickness from the nose, measured
    # along the foot_at→nose line. Step from foot_at toward nose, stopping
    # cham_scale * thickness short of nose.
    # FULL = 1.0× thickness from nose, HALF = 0.5× thickness from nose.

    v3v5 = nose - foot_at_raw
    d35 = v3v5.length
    cham_a = foot_at_raw + v3v5 * ((d35 - cham_scale * thickness) / d35) if d35 > cham_scale * thickness else (foot_at_raw + nose) * 0.5

    v7v5 = nose - foot_bt_raw
    d75 = v7v5.length
    cham_b = foot_bt_raw + v7v5 * ((d75 - cham_scale * thickness) / d75) if d75 > cham_scale * thickness else (foot_bt_raw + nose) * 0.5

    # ── Assemble ─────────────────────────────────────────────────────────────
    v0 = A.copy()
    v1 = foot_a.copy()                      # inner toe A — fixed
    v2 = foot_am.copy()                     # mid toe A — HALF only
    v3 = foot_at.copy()                     # outer toe A — never moves
    v4 = cham_a.copy()                      # chamfer shoulder A
    v5 = nose.copy()                        # nose tip
    v6 = cham_b.copy()                      # chamfer shoulder B
    v7 = foot_bt.copy()                     # outer toe B — never moves
    v8 = foot_bm.copy()                     # mid toe B — HALF only
    v9 = foot_b.copy()                      # inner toe B — fixed

    if chamfer == 'NONE':
        # No toe loops, no chamfer shoulders, sharp nose
        v2 = v1.copy()
        v4 = v3.copy()
        v6 = v7.copy()
        v8 = v9.copy()
    elif chamfer == 'HALF':
        # Both toe loops present, chamfer shoulders, nose flattened
        v5 = (v4 + v6) * 0.5
    else:  # FULL
        # Outer toe only (mid collapsed), chamfer shoulders, nose flattened
        v2 = v1.copy()
        v8 = v9.copy()
        v5 = (v4 + v6) * 0.5

    return v0, v1, v2, v3, v4, v5, v6, v7, v8, v9


# ---------------------------------------------------------------------------
# Chain utilities

def _chain_edges(selected_edges):
    """Build ordered edge chains, splitting at junction verts (valence > 2)."""
    adj = {}
    for e in selected_edges:
        for v in e.verts:
            adj.setdefault(v.index, []).append(e)

    valence   = {vi: len(el) for vi, el in adj.items()}
    junctions = {vi for vi, c in valence.items() if c > 2}

    visited   = set()
    chains    = []
    closed_fl = []

    def _walk(start_edge, start_vert):
        chain, cur_e, cur_v = [], start_edge, start_vert
        while cur_e and cur_e.index not in visited:
            visited.add(cur_e.index)
            chain.append(cur_e)
            nxt = (cur_e.verts[1] if cur_e.verts[0].index == cur_v.index
                   else cur_e.verts[0])
            if nxt.index in junctions and chain:
                nexts = [e for e in adj.get(nxt.index, []) if e.index not in visited]
                if not nexts:
                    break
                cur_faces = set(f.index for f in cur_e.link_faces)
                same_type = [e for e in nexts
                             if any(f.index in cur_faces for f in e.link_faces)]
                if len(nexts) == 1 or (same_type and len(same_type) == 1):
                    cur_e = same_type[0] if same_type else nexts[0]
                    cur_v = nxt
                else:
                    break
                continue
            nexts = [e for e in adj.get(nxt.index, []) if e.index not in visited]
            cur_e = nexts[0] if nexts else None
            cur_v = nxt
        return chain

    for e in selected_edges:
        for v in e.verts:
            if valence[v.index] == 1 and e.index not in visited:
                ch = _walk(e, v)
                if ch:
                    chains.append(ch)
                    closed_fl.append(False)

    for vi in junctions:
        for e in adj.get(vi, []):
            if e.index not in visited:
                sv = e.verts[0] if e.verts[0].index == vi else e.verts[1]
                ch = _walk(e, sv)
                if ch:
                    chains.append(ch)
                    closed_fl.append(False)

    for e in selected_edges:
        if e.index not in visited:
            ch = _walk(e, e.verts[0])
            if ch:
                chains.append(ch)
                closed_fl.append(True)

    return chains, closed_fl


def _chain_data(chain, is_closed, normal_mat=None):
    """Return (verts, tangents, frames, vert_z_ref) for a chain.

    vert_z_ref[i] is the world-space Z of verts[i] at the time of chain
    construction.  Used by _build_trim to Z-correct rings after flat-frame
    generation so that ramp profiles land at the correct height without
    needing per-etype special casing.

    faceA is assigned from the first edge and kept consistent across all
    subsequent edges — same face index means same A leg throughout the chain.
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

    # Record world-space Z for each chain vert before any geometry is built.
    # normal_mat is only the 3×3 rotation part; vert positions are in local
    # (object) space.  The operator always applies matrix_world to the trim
    # result after generation, so we stay in local space throughout — local Z
    # is the correct reference for the correction pass.
    vert_z_ref = [v.co.z for v in verts]

    n        = len(verts)
    n_edges  = n if is_closed else n - 1
    tangents = []
    frames   = []

    # Establish face_index_A from the first edge that has a clear flat face.
    # The first edge might be at a plan corner with two wall faces — walk
    # forward to find an edge with a proper flat (floor/ceiling/ramp) face.
    face_index_A = None
    for seed_idx in range(n_edges):
        e = chain[seed_idx]
        if len(e.link_faces) < 2:
            continue
        f0, f1 = e.link_faces[0], e.link_faces[1]
        n0 = (normal_mat @ f0.normal.normalized()).normalized() if normal_mat else f0.normal.normalized()
        n1 = (normal_mat @ f1.normal.normalized()).normalized() if normal_mat else f1.normal.normalized()
        s0, s1 = abs(n0.z), abs(n1.z)
        if max(s0, s1) > 0.3:
            face_index_A = (f0 if s0 >= s1 else f1).index
            break
        # Area fallback — flat face is typically larger than wall strip
        a0, a1 = f0.calc_area(), f1.calc_area()
        if max(a0, a1) > 0 and abs(a0 - a1) / max(a0, a1) > 0.1:
            face_index_A = (f0 if a0 >= a1 else f1).index
            break

    MIN_EDGE_LENGTH = 0.01  # skip degenerate near-zero edges

    for idx in range(n_edges):
        j = (idx + 1) % n
        t = verts[j].co - verts[idx].co
        # Skip degenerate very short edges — they produce unstable frames
        # and cause _plane_intersect blow-up in adjacent interior rings.
        if t.length < MIN_EDGE_LENGTH:
            # Use a fallback tangent so frame count stays in sync with chain
            t = tangents[-1] if tangents else Vector((1, 0, 0))
        t = t.normalized() if t.length > 1e-6 else Vector((1, 0, 0))
        tangents.append(t)
        prev_nA = frames[-1].nA if frames else None
        frame = DihedralFrame(chain[idx], t, face_index_A, normal_mat, prev_nA)
        frame.edge = chain[idx]

        # If prev_nA was the floor normal and this frame got classified as
        # WW (both walls), override nA and etype to maintain floor continuity.
        # This handles wall-corner edges where the floor ngon is not a linked face.
        if prev_nA is not None and frame.edge_type == 'WW':
            from mathutils import Vector as _Vec
            w_prev = (normal_mat @ prev_nA).normalized() if normal_mat else prev_nA
            if abs(w_prev.z) > 0.7:  # prev was a floor/ceiling normal
                frame.nA = prev_nA.copy()
                frame.edge_type = 'WF' if w_prev.z > 0 else 'WC'
                w_nA_world = w_prev
                frame.nA_world = w_nA_world

        # WR → WF override: generate ramp edges in a flat frame so that
        # arm directions are horizontal and miter at floor/ramp transitions
        # is clean.  Both nA and T must be flattened — T is the edge tangent
        # and if it has a vertical component the arms computed from T.cross(nA)
        # will also be tilted even with nA overridden.
        # Use prev_nA as the floor normal so orientation is consistent with
        # the preceding flat section.  Fall back to world up if unavailable.
        if frame.edge_type == 'WR':
            from mathutils import Vector as _Vec
            if prev_nA is not None:
                w_prev = (normal_mat @ prev_nA).normalized() if normal_mat else prev_nA
                if abs(w_prev.z) > 0.7:
                    floor_nA = prev_nA.copy()
                    floor_nA_world = w_prev
                else:
                    # consecutive ramp edges — use world up/down
                    floor_nA = (normal_mat.inverted() @ _Vec((0, 0, 1))).normalized() if normal_mat else _Vec((0, 0, 1))
                    floor_nA_world = _Vec((0, 0, 1))
            else:
                floor_nA = (normal_mat.inverted() @ _Vec((0, 0, 1))).normalized() if normal_mat else _Vec((0, 0, 1))
                floor_nA_world = _Vec((0, 0, 1))

            # Flatten T: remove the world-vertical component so the tangent
            # lies in the horizontal plane.  This ensures T.cross(nA) gives
            # a purely horizontal arm direction in _profile_ring.
            world_up = _Vec((0, 0, 1))
            if normal_mat:
                local_up = (normal_mat.inverted() @ world_up).normalized()
            else:
                local_up = world_up
            t_flat = t - local_up * local_up.dot(t)
            if t_flat.length > 1e-6:
                t_flat = t_flat.normalized()
            else:
                t_flat = t  # degenerate — keep original
            # Update tangent in list and on frame
            tangents[-1] = t_flat
            frame.T         = t_flat
            frame.nA        = floor_nA
            frame.edge_type = 'WF' if floor_nA_world.z > 0 else 'WC'
            frame.nA_world  = floor_nA_world
            # Inherit is_convex from the preceding flat frame — the ramp
            # dihedral gives the wrong answer for the WC (underside) case.
            if frames:
                frame.is_convex = frames[-1].is_convex
            else:
                # Solo ramp — no preceding frame to inherit from.
                # is_convex was computed in DihedralFrame.__init__ against the
                # original ramp nA, which has now been replaced with floor_nA.
                # Recompute against floor_nA using the same far-vert test so
                # the value is consistent with the overridden nA.
                edge_vis_solo = {v.index for v in chain[idx].verts}
                far_b_solo = [v for v in frame.faceB.verts if v.index not in edge_vis_solo] if frame.faceB else []
                if far_b_solo:
                    ec_solo   = (chain[idx].verts[0].co + chain[idx].verts[1].co) * 0.5
                    fb_co_solo = sum((v.co for v in far_b_solo), Vector()) / len(far_b_solo)
                    frame.is_convex = (fb_co_solo - ec_solo).dot(floor_nA) < 0
                else:
                    # Degenerate faceB — use nB agreement as fallback
                    frame.is_convex = floor_nA.dot(frame.nB) > 0

        if face_index_A is None and frame.faceA is not None:
            face_index_A = frame.faceA.index
        frames.append(frame)

    if not is_closed:
        tangents.append(tangents[-1])
        frames.append(frames[-1])
        vert_z_ref.append(vert_z_ref[-1])  # phantom end-vert mirrors frame duplication

    return verts, tangents, frames, vert_z_ref


# ---------------------------------------------------------------------------
# Trim mesh builder

def _build_trim(trim_bm, chain, is_closed,
                depth_cfg, thickness, chamfer,
                mat_index=0, normal_mat=None):

    verts, tangents, frames, _ = _chain_data(chain, is_closed, normal_mat)
    n       = len(verts)
    n_edges = n if is_closed else n - 1
    if n_edges < 1:
        return

    def _face(vlist):
        try:
            f = trim_bm.faces.new(vlist)
            f.material_index = mat_index
        except Exception:
            pass

    # Build one ring per chain vertex. At interior verts where the face context
    # changes significantly, emit TWO rings at the same position: one closing
    # the incoming edge (is_end) and one opening the outgoing edge (is_start).
    # This avoids averaging arms across surface-pair transitions.

    def _is_context_change(fi, fo, vi=None):
        """True when frames are different surface types, OR when the flat face
        normals diverge significantly, OR when the chain height changes at this
        vert (floor→ramp or ramp→floor transition after WR→WF override).
        """
        if fi.edge_type != fo.edge_type:
            return True
        # Detect floor→ramp transition: same etype but nA diverges.
        if fi.edge_type in ('WF', 'WC', 'WR'):
            if fi.nA_world.dot(fo.nA_world) < 0.85:
                return True
        # Detect height change at this vert — catches WR→WF overridden ramp
        # edges where both frames look identical but chain Z is changing.
        if vi is not None:
            # Modulo is safe for both open and closed chains:
            # - Open chain: vi is always interior (1..n-2), so vi-1 and vi+1
            #   are always valid without wrapping. Modulo is a no-op here.
            # - Closed chain: duplicate end vert is removed (see _chain_data line ~564),
            #   so verts[0] and verts[n-1] are adjacent chain verts and wrap is correct.
            vi_prev = (vi - 1) % n
            vi_next = (vi + 1) % n
            dz_in  = abs(verts[vi].co.z - verts[vi_prev].co.z)
            dz_out = abs(verts[vi_next].co.z - verts[vi].co.z)
            # Fire if one side is flat and the other is not (threshold 0.05 m)
            if abs(dz_out - dz_in) > 0.05:
                return True
        return False

    rings    = []      # list of (ring_bm_verts, edge_index_after, is_convex, split_open)
    ring_z_ref = []    # parallel list: source vert Z for each emitted ring
    # edge_index_after: which edge's strips use this ring on the LEFT side

    def _emit_ring(pts, ei, frame, split_open=False, src_z=0.0):
        """Create BMVerts from pts. Always seam-first (v0..v9).
        split_open=True marks this ring as the opening half of a context-change
        pair; the strip between it and the preceding closing ring is skipped.
        src_z is the world-space Z of the source chain vert, stored for the
        post-generation Z-correction pass.
        """
        ring_z_ref.append(src_z)
        return ([trim_bm.verts.new(p) for p in pts], ei, frame.is_convex, split_open)

    for vi in range(n):
        is_start = (not is_closed) and vi == 0
        is_end   = (not is_closed) and vi == n - 1

        ei_in  = (vi - 1) % n_edges if not is_start else 0
        ei_out = vi       % n_edges if not is_end   else n_edges - 1

        if is_start or is_end:
            fr_cur = frames[ei_out] if ei_out < len(frames) else frames[-1]
            _da, _db = depth_cfg.get(fr_cur.edge_type, list(depth_cfg.values())[0])
            pts = _profile_ring(
                verts[vi].co,
                frames[ei_in], frames[ei_out],
                _da, _db, thickness, chamfer,
                is_start, is_end,
            )
            rings.append(_emit_ring(pts, ei_out, fr_cur, src_z=verts[vi].co.z))
        else:
            # Interior vertex — check for face-context change
            fi = frames[ei_in]
            fo = frames[ei_out]
            if _is_context_change(fi, fo, vi):
                seam_co = verts[vi].co
                _da_cl, _db_cl = depth_cfg.get(fi.edge_type, list(depth_cfg.values())[0])
                _da_op, _db_op = depth_cfg.get(fo.edge_type, list(depth_cfg.values())[0])

                # Determine if this is a flat→ramp or ramp→flat transition
                # by comparing the two nA world-space Z components.
                nA_in_wz  = abs(fi.nA_world.z)
                nA_out_wz = abs(fo.nA_world.z)
                fi_is_flat = nA_in_wz > 0.95
                fo_is_flat = nA_out_wz > 0.95

                def _arm_a_from_frame(frame):
                    """T × nA gives floor/ramp arm in face plane, perp to edge."""
                    a = frame.T.cross(frame.nA)
                    if a.length < 0.1:
                        a = frame.T.cross(frame.nB)
                    a = a.normalized()
                    # Orient away from wall (concave: toward wall, convex: away)
                    if frame.is_convex:
                        if a.dot(frame.nB) > 0: a = -a
                    else:
                        if a.dot(frame.nB) < 0: a = -a
                    return a

                def _conformance_ring(seam, arm_a, nA_ramp, T_ref, da, t, chamfer_mode):
                    """10-vert ring for floor/ramp junction.
                    Floor cover at full depth, wall cover collapsed to 0 (B-arm
                    is zero length at the junction — no wall cover on the ramp side).
                    Nose sits at thickness along the ramp normal from seam, T-projected
                    so it stays in the cross-section plane.
                    Chamfer shoulders and nose flattening follow chamfer_mode (NONE/HALF/FULL).
                    """
                    v0 = seam.copy()
                    v1 = seam + arm_a * da          # footA
                    v3 = v1   + nA_ramp * t         # footAt (lift by ramp normal)
                    nose_raw = seam + nA_ramp * t   # nose at thickness along ramp normal
                    v5 = nose_raw - T_ref * T_ref.dot(nose_raw - seam)  # T-project
                    v7 = v3.copy()                   # footBt = footAt (wall arm = 0)
                    v9 = v3.copy()                   # footB  = footAt (wall arm = 0)
                    if chamfer_mode != 'NONE':
                        cham_scale = 0.5 if chamfer_mode == 'HALF' else 1.0
                        v3v5 = v5 - v3
                        d35  = v3v5.length
                        v4 = v5 - v3v5 * (cham_scale * t / d35) if d35 > 1e-6 else (v3 + v5) * 0.5
                        v6  = v4.copy()              # B shoulder mirrors A (wall=0)
                        v2  = v1 + (v3 - v1) * 0.5  # foot A half-lift
                        v8  = v9.copy()              # foot B half-lift (wall=0, same as v9)
                        v5  = (v4 + v6) * 0.5        # flatten nose to midpoint of shoulders
                    else:
                        v2, v4, v6, v8 = v1.copy(), v3.copy(), v7.copy(), v9.copy()
                    return v0,v1,v2,v3,v4,v5,v6,v7,v8,v9

                src_z = verts[vi].co.z
                if fi_is_flat and not fo_is_flat:
                    nA_ramp = fo.nA
                    arm_a   = _arm_a_from_frame(fi)
                    pts_close = _conformance_ring(seam_co, arm_a, nA_ramp, fi.T,
                                                  _da_cl, thickness, chamfer)
                    rings.append(_emit_ring(pts_close, ei_in, fi, src_z=src_z))

                elif not fi_is_flat and fo_is_flat:
                    pts_close = _profile_ring(
                        seam_co, fi, fo,
                        _da_cl, _db_cl, thickness, chamfer,
                        is_start=False, is_end=True,
                    )
                    rings.append(_emit_ring(pts_close, ei_in, fi, src_z=src_z))
                    nA_ramp = fi.nA
                    arm_a   = _arm_a_from_frame(fo)
                    pts_open = _conformance_ring(seam_co, arm_a, nA_ramp, fo.T,
                                                 _da_op, thickness, chamfer)
                    rings.append(_emit_ring(pts_open, ei_out, fo, split_open=True, src_z=src_z))

                else:
                    # Other type changes (WF→WC, WW transitions etc):
                    # plain start/end rings.
                    pts_close = _profile_ring(
                        seam_co, fi, fo,
                        _da_cl, _db_cl, thickness, chamfer,
                        is_start=False, is_end=True,
                    )
                    rings.append(_emit_ring(pts_close, ei_in, fi, src_z=src_z))
                    pts_open = _profile_ring(
                        seam_co, fi, fo,
                        _da_op, _db_op, thickness, chamfer,
                        is_start=True, is_end=False,
                    )
                    rings.append(_emit_ring(pts_open, ei_out, fo, split_open=True, src_z=src_z))
            else:
                fr_cur = frames[ei_out] if ei_out < len(frames) else frames[-1]
                _da_i, _db_i = depth_cfg.get(fr_cur.edge_type, list(depth_cfg.values())[0])
                pts = _profile_ring(
                    verts[vi].co,
                    fi, fo,
                    _da_i, _db_i, thickness, chamfer,
                    is_start=False, is_end=False,
                )
                rings.append(_emit_ring(pts, ei_out, fr_cur, src_z=verts[vi].co.z))

    # Always 10 strips — _profile_ring always emits 10 distinct verts.
    # No-chamfer mode has v2≈v1, v4≈v3, v6≈v7, v8≈v9 positionally close
    # but they are separate BMVerts; all 10 strips must be connected.
    STRIPS = [(0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8),(8,9),(9,0)]

    def _face_oriented(vlist):
        """Create a face. Winding correctness comes from ring order."""
        try:
            f = trim_bm.faces.new(vlist)
            f.material_index = mat_index
        except Exception:
            pass

    # Connect strips. Rings are always seam-first (v0..v9), always CW.
    # nA/nB from DihedralFrame already point toward open space for both
    # concave and convex — ring verts are always correctly positioned.
    # Standard winding r0[a], r0[b], r1[b], r1[a] works for all cases.
    # Context-change junctions emit two rings at the same spatial vertex:
    # the closing ring followed by the opening ring (tagged split_open=True).
    # Skip the strip between that pair only.
    nr = len(rings)
    loop = range(nr) if is_closed else range(nr - 1)
    for i in loop:
        j = (i + 1) % nr
        r0, ei0, c0, so0 = rings[i]
        r1, ei1, c1, so1 = rings[j]
        # Connect all pairs including split junctions (miter faces).
        # At split junctions v0 is shared so the seam strip degenerates silently.
        for a, b in STRIPS:
            _face_oriented([r0[a], r0[b], r1[b], r1[a]])

    if not is_closed:
        def _cap_ring(rv, start):
            """Emit cap as two quads split through v0 (seam) and v5 (nose).
            start=True: looking inward (backward along chain) → reversed winding.
            """
            # no-chamfer: v2=v1, v4=v3, v6=v7, v8=v9 — use indices 0,1,3,5,7,9
            v0,v1,v3,v5,v7,v9 = rv[0],rv[1],rv[3],rv[5],rv[7],rv[9]
            if start:
                # reversed so cap faces outward
                qa = [v5, v3, v1, v0]
                qb = [v9, v7, v5, v0]
            else:
                qa = [v0, v1, v3, v5]
                qb = [v0, v5, v7, v9]
            for q in (qa, qb):
                try:
                    f = trim_bm.faces.new(q)
                    f.material_index = mat_index
                except Exception:
                    pass

        r0_verts, _, _, _ = rings[0]
        r1_verts, _, _, _ = rings[-1]
        _cap_ring(r0_verts, start=True)
        _cap_ring(r1_verts, start=False)

    # Note: remove_doubles is called by the operator after all chains are built.
    trim_bm.normal_update()


# ---------------------------------------------------------------------------
# Degenerate chain splitter

def _split_degenerate_chains(chains, closed_flags, normal_mat=None):
    """Pass-through — degenerate chain splitting removed.

    The topo-arm antiparallel check that was here fired incorrectly on curved
    wall/floor runs with large floor ngons, splitting clean chains at the
    straight-to-curve junction and curve midpoint.

    The original target case (collinear ramp ridge — two ramps ascending in
    opposite directions sharing a vert) is handled by selecting each ramp
    section separately. Both generate with matching caps at the shared vert
    and weld cleanly via remove_doubles.
    """
    return chains, closed_flags

# ---------------------------------------------------------------------------
# Operator

class OT_FBXMT_Generate_Trim2(Operator):
    bl_idname     = 'fbxmt.generate_trim2'
    bl_label      = 'Generate Trim (Dihedral)'
    bl_description = ('Sweep dihedral-frame trim along selected edges. '
                      'Works for floor/wall, wall/wall, ramp, and any angle. '
                      'v0.2.40: release — chamfer NONE/HALF/FULL, correct nose position, '
                      'curved runs, ramp, closed loops.')
    bl_options    = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH'
                and context.active_object is not None
                and context.active_object.type == 'MESH')

    def execute(self, context):
        props = context.scene.fbxmt_props

        thickness = props.trim_thickness
        chamfer   = props.trim_corner_chamfer
        # depth_a = A-face (floor/ramp/ceiling) depth
        # depth_b = B-face (wall) depth
        # (depth_a, depth_b) = (flat-face/floor/ceiling/ramp arm, wall arm)
        # DihedralFrame: A = flat face, B = wall face
        depth_cfg = {
            'WF': (props.trim_wf_floor_b,   props.trim_wf_wall_a),
            'WC': (props.trim_wc_ceiling_b,  props.trim_wc_wall_a),
            'WR': (props.trim_wr_ramp_b,     props.trim_wr_wall_a),
            'WW': (props.trim_ww_wall,       props.trim_ww_wall),
        }

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

        # Split at collinear ramp ridge verts before generation
        chains, closed_flags = _split_degenerate_chains(
            chains, closed_flags,
            normal_mat=matrix_world.to_3x3().normalized(),
        )

        # Build all chains into a single BMesh so the result is one object.
        trim_bm = bmesh.new()
        self.report({'INFO'}, f'[FBXMT] {len(chains)} chains to build')
        for chain, is_closed in zip(chains, closed_flags):
            _build_trim(
                trim_bm, chain, is_closed,
                depth_cfg, thickness, chamfer,
                mat_index=0,
                normal_mat=matrix_world.to_3x3().normalized(),
            )

        suffix    = '_Trim2'
        trim_mesh = bpy.data.meshes.new(f'{obj.name}{suffix}')
        trim_mesh.materials.append(trim_mat)
        # Weld any coincident verts from split-ring caps across all chains
        bmesh.ops.remove_doubles(trim_bm, verts=trim_bm.verts, dist=1e-4)
        trim_bm.to_mesh(trim_mesh)
        trim_bm.free()
        trim_mesh.update()

        trim_obj = bpy.data.objects.new(f'{obj.name}{suffix}', trim_mesh)
        context.collection.objects.link(trim_obj)
        trim_obj.select_set(True)
        trim_objects = [trim_obj]

        bm.free()

        last_obj = trim_objects[-1]
        context.view_layer.objects.active = last_obj

        for trim_obj in trim_objects:
            move_to_collection(trim_obj, COLLECTION_TRIM)
            trim_obj.matrix_world = matrix_world.copy()

        bpy.ops.object.select_all(action='DESELECT')
        for trim_obj in trim_objects:
            trim_obj.select_set(True)
        context.view_layer.objects.active = last_obj

        # Clear A/B overlay now that generation is complete
        try:
            from .trim_overlay import clear_overlay
            clear_overlay()
        except Exception:
            pass

        self.report({'INFO'},
            f'Trim2 generated: {len(chains)} chain(s) -> "{last_obj.name}"')
        return {'FINISHED'}
