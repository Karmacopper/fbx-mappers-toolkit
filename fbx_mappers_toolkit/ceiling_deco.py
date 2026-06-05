# ceiling_deco.py — FBX Mapper's Toolkit  [v0.25.1]
import sys as _sys
print("FBXMT ceiling_deco v0.25.1 loaded", file=_sys.stderr)
del _sys


#
# Ceiling Deco System: Generate Coving + Generate Beams.
#
# Architecture
# ------------
# Coving sweeps a 4-vert rectangular profile along any selected edge loop.
# No edge-type classification — any selection is accepted.
#
# Profile (4 verts, 1 quad face per ring):
#
#   v0  seam       — exactly on the selected edge (seam vert position)
#
#   v1  depth leg  — v0 moved `depth` along the HORIZONTAL inward arm, with
#                    Z (world-up) locked to seam Z.  Coplanar with the
#                    ceiling face in height.  At plan corners _plane_intersect
#                    miters in XY only.
#
#   v2  notch      — v1 moved `notch_frac × thickness` in the wall-down
#                    direction (world −Z).  Creates the shadow groove between
#                    the ceiling cover and wall cover.
#
#   v3  wall flush — v0 moved `thickness` in the wall-down direction (world −Z
#                    projected into the wall face plane).  Lies flush on the
#                    wall face.  At plan corners _plane_intersect miters across
#                    the per-edge wall-down vectors.
#
# Key geometry insight
# --------------------
# The seam edge sits at the ceiling/wall junction.  The two arms of the
# cross-section are:
#
#   Horizontal arm (→ v1): _topo_arm on the ceiling face, Z-flattened, XY only.
#   Wall-down arm  (→ v3): _topo_arm on the WALL face but projected onto the
#                           wall face's OWN plane and then pointing DOWNWARD
#                           (away from seam along wall surface = −Z for a
#                           vertical wall).  This is NOT the wall face normal;
#                           it is the in-face direction perpendicular to the
#                           seam edge going downward.
#
# For a perfectly vertical wall this resolves to (0, 0, −1) in local space.
# For a slightly tilted wall it follows the wall surface, staying flush.
#
# Z coplanarity rule
# ------------------
# All seam verts in a chain must share the same Z within Z_NOISE_EPS (1 mm).
# Chains that fail are skipped with a per-chain warning reporting the deviation.
#
# Key reuse from trim_gen2 (verbatim — no import coupling):
#   _topo_arm, _chain_edges, _plane_intersect

import math
import bpy
import bmesh
from mathutils import Vector
from bpy.types import Operator

from .materials import ensure_fbxmt_materials, COLLECTION_TRIM, move_to_collection


# ---------------------------------------------------------------------------
# Noise compensation threshold for Z coplanarity check (metres)
Z_NOISE_EPS = 1e-3


# ---------------------------------------------------------------------------
# Helpers (verbatim from trim_gen2 — no import to keep files decoupled)

def _topo_arm(face, edge, T):
    """Direction from edge midpoint toward the far side of face, perp to T."""
    edge_vis = {v.index for v in edge.verts}
    far = [v for v in face.verts if v.index not in edge_vis]
    n = face.normal.normalized()

    def _fallback():
        cross = T.cross(n)
        if cross.length > 0.1:
            return cross.normalized()
        for axis in (Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))):
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


def _sanitise_t_junctions(bm, edges, face_normals_map, snap=1e-3):
    """
    Resolve T-junctions: a hanging vert (valence-1) that lies on another edge
    is welded to that edge by:
      1. Creating a new vert at the projection point on the host edge
      2. Splitting the host edge through the new vert
      NO connector edge — hv stays valence-1, nv stays valence-2.
    This keeps hv's chain edges separate from the host chain edges so per-obj
    tagging works correctly after the sanitise.
    Returns updated edge list, face_normals_map, and split_host_map.
    {connector_edge_id: hv_own_edge_id} for obj tag propagation.
    """
    split_host_map     = {}  # half-edge id → host edge id
    changed = True
    while changed:
        changed = False
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        hanging = [v for v in bm.verts if v.is_valid and len(v.link_edges) == 1]
        for hv in hanging:
            if not hv.is_valid:
                continue
            hp = hv.co.copy()
            hv_own_edge = hv.link_edges[0] if hv.link_edges else None
            for e in list(bm.edges):
                if not e.is_valid or hv in e.verts:
                    continue
                va, vb = e.verts[0], e.verts[1]
                ab = vb.co - va.co
                ab_len = ab.length
                if ab_len < 1e-8:
                    continue
                t = (hp - va.co).dot(ab) / (ab_len * ab_len)
                if t < 0.01 or t > 0.99:
                    continue
                proj = va.co + ab * t
                if (hp - proj).length > snap:
                    continue
                # Create new vert at projection point on host edge
                nv = bm.verts.new(proj)
                # Split host edge through nv (host chain stays separate)
                old_norm = face_normals_map.pop(id(e), None)
                bm.edges.remove(e)
                ea = bm.edges.new((va, nv))
                eb = bm.edges.new((nv, vb))
                split_host_map[id(ea)] = id(e)
                split_host_map[id(eb)] = id(e)
                if old_norm is not None:
                    face_normals_map[id(ea)] = old_norm
                    face_normals_map[id(eb)] = old_norm
                # Snap hv to the projection point — same position as nv.
                # Do NOT add a connector edge between hv and nv.
                # hv stays valence-1 (InnerWall endpoint at proj position).
                # nv stays valence-2 (OuterWall half-edges only, collinear).
                # Per-obj separation then keeps them in separate chains cleanly.
                hv.co = proj.copy()
                changed = True
                break
    return [e for e in bm.edges if e.is_valid], face_normals_map, split_host_map


def _plane_intersect(origin, dir_in, dir_out, dist):
    """Miter intersection of two planar offsets.

    For convex corners up to ~120° the exact intersection stays within 2× dist
    of the origin — we use it directly. Beyond ~120° the spike grows too large;
    we fall back to the bisector (average of the two arms, normalised to dist).
    The bisector is the correct architectural miter for sharp concave corners —
    it splits the angle equally so neither adjacent face has a disproportionate
    kink. Flushing to dir_in only is wrong because it leaves the other adjacent
    face with the full angular mismatch.
    """
    c = dir_in.dot(dir_out)
    denom = 1.0 - c * c
    if abs(denom) < 1e-6:
        return origin + dir_in * dist

    s = dist * (1.0 - c) / denom

    if s >= 0:
        result = origin + dir_in * s + dir_out * s
        # Clamp based on s/dist ratio (angular measure, thickness-independent).
        # s/dist = (1-c)/(1-c²) = 1/(1+c). At 120°: s/dist=2. At 135°: s/dist=3.4.
        # Allow up to s/dist=3 (covers ~130° miters) regardless of thickness.
        if s <= dist * 3.0:
            return result

    # Sharp corner or blowout — clamp to bisector at dist
    bisector = dir_in + dir_out
    if bisector.length > 1e-6:
        return origin + bisector.normalized() * dist
    return origin + dir_in * dist


# ---------------------------------------------------------------------------
# Chain utilities (verbatim from trim_gen2)

def _chain_edges(selected_edges):
    """Build ordered edge chains. Holds all vert Python objects to keep
    id() values stable for the duration of the function call."""
    # Pre-fetch and hold all vert wrappers. BMesh returns a new wrapper on
    # each .verts[] access; holding the object keeps its id() constant.
    edge_verts = {}   # id(edge) → (v0, v1) — stable vert refs per edge
    adj        = {}   # id(vert) → [edge, ...]
    vert_obj   = {}   # id(vert) → vert

    for e in selected_edges:
        v0, v1 = e.verts[0], e.verts[1]  # hold both refs now
        edge_verts[id(e)] = (v0, v1)
        for v in (v0, v1):
            vert_obj.setdefault(id(v), v)
            adj.setdefault(id(v), []).append(e)

    valence   = {vid: len(el) for vid, el in adj.items()}
    junctions = {vid for vid, c in valence.items() if c > 2}
    from collections import Counter as _Ctr
    _vc = _Ctr(valence.values())
    visited   = set()
    chains    = []
    closed_fl = []

    def _walk(start_edge, start_v_id):
        chain, cur_e, cur_v_id = [], start_edge, start_v_id
        while cur_e and id(cur_e) not in visited:
            visited.add(id(cur_e))
            chain.append(cur_e)
            v0, v1 = edge_verts.get(id(cur_e), cur_e.verts)
            v0id, v1id = id(v0), id(v1)
            nxt_id = v1id if v0id == cur_v_id else v0id
            if nxt_id in junctions and chain:
                nexts = [e for e in adj.get(nxt_id, []) if id(e) not in visited]
                if not nexts: break
                if len(nexts) == 1:
                    cur_e, cur_v_id = nexts[0], nxt_id
                else: break
                continue
            nexts = [e for e in adj.get(nxt_id, []) if id(e) not in visited]
            cur_e, cur_v_id = (nexts[0], nxt_id) if nexts else (None, nxt_id)
        return chain

    # Open chains from endpoints (valence-1)
    for vid, v in vert_obj.items():
        if valence.get(vid, 0) == 1:
            for e in adj.get(vid, []):
                if id(e) not in visited:
                    ch = _walk(e, vid)
                    if ch: chains.append(ch); closed_fl.append(False)

    # Junction-started chains
    for vid in junctions:
        for e in adj.get(vid, []):
            if id(e) not in visited:
                ch = _walk(e, vid)
                if ch: chains.append(ch); closed_fl.append(False)

    # Closed loops — remaining unvisited edges
    for e in selected_edges:
        if id(e) not in visited:
            v0, v1 = edge_verts.get(id(e), (e.verts[0], e.verts[1]))
            ch = _walk(e, id(v0))
            if ch: chains.append(ch); closed_fl.append(True)

    return chains, closed_fl


def _chain_verts(chain, is_closed):
    """Return ordered list of BMVerts for a chain. Holds vert refs for stable id()."""
    # Pre-fetch all vert pairs to avoid re-wrapping id() instability
    ev = [(e.verts[0], e.verts[1]) for e in chain]

    v0 = ev[0][0]
    if len(chain) > 1:
        shared = {id(ev[1][0]), id(ev[1][1])}
        if id(v0) in shared:
            v0 = ev[0][1]

    verts = [v0]
    for v0e, v1e in ev:
        nxt = v1e if id(v0e) == id(verts[-1]) else v0e
        verts.append(nxt)

    if is_closed and id(verts[-1]) == id(verts[0]):
        verts.pop()

    return verts


# ---------------------------------------------------------------------------
# Per-edge arm extraction for coving
#
# For each seam edge we need two directions:
#
#   h_arm      — horizontal inward (→ v1, v2 base)
#                _topo_arm on the most-horizontal linked face, Z zeroed.
#
#   wall_down  — down the wall surface (→ v3, v2 offset)
#                _topo_arm on the most-vertical linked face, projected onto
#                that face's plane, then the Z component is taken and made
#                negative (downward).  For a plumb wall this is simply
#                (0,0,−1) in local space.  For a sloped wall it follows
#                the face, staying flush.
#
# Both are expressed in LOCAL (object) space — the mesh is in object space
# when bmesh reads it, and the output verts go back through matrix_world.

def _edge_arms(edge, T, normal_mat, seam_centroid_local=None, face_normals=None):
    """Return (h_arm, wall_down) unit vectors in local/world space.

    h_arm     : unit horizontal vector pointing INTO THE ROOM from the seam
                edge, derived directly from the wall face normal projected to
                the horizontal plane.  The face normal of a correctly-wound
                mesh always points away from the wall solid toward the room —
                so we use it as-is with no sign flip.

    wall_down : unit vector pointing DOWN the wall face from the seam.
                local_down projected onto the wall face plane — for a plumb
                wall this is exactly (0,0,-1).

    face_normals : dict {edge.index: [Vector, ...]} of pre-computed world-
                space face normals.  Used instead of edge.link_faces so that
                world_bm edges (which have no linked faces) work correctly.
    seam_centroid_local : unused, kept for API compatibility.
    """
    # Resolve face normals from explicit dict or edge.link_faces
    if face_normals is not None and id(edge) in face_normals:
        raw_normals = face_normals[id(edge)]
    elif edge.link_faces:
        if normal_mat is not None:
            raw_normals = [(normal_mat @ f.normal.normalized()).normalized()
                           for f in edge.link_faces]
        else:
            raw_normals = [f.normal.normalized() for f in edge.link_faces]
    else:
        raw_normals = []

    # Up/down in the coordinate space we're working in
    if normal_mat is not None:
        try:
            local_up = normal_mat.inverted() @ Vector((0, 0, 1))
            local_up = local_up.normalized()
        except Exception:
            local_up = Vector((0, 0, 1))
    else:
        local_up = Vector((0, 0, 1))
    local_down = -local_up

    if not raw_normals:
        # Bare edge — no face data.  Fall back to T × up; caller must ensure
        # the seam loop is on a meshed object for correct direction.
        perp = T.cross(local_up)
        if perp.length < 1e-6:
            perp = Vector((1, 0, 0))
        return perp.normalized(), local_down

    # Pick the most vertical face normal (smallest |dot with up|) = wall face
    wall_n = min(raw_normals, key=lambda n: abs(n.dot(local_up)))

    # --- Horizontal arm ---
    # The wall face normal points AWAY from the wall solid into the room.
    # Project it onto the horizontal plane — this IS the room-inward direction.
    # No sign flip needed; face winding handles this already.
    h_arm = wall_n - local_up * local_up.dot(wall_n)
    if h_arm.length < 1e-4:
        h_arm = T.cross(local_up)   # degenerate (horizontal face)
    if h_arm.length < 1e-4:
        h_arm = Vector((1, 0, 0))
    h_arm = h_arm.normalized()

    # --- Wall-down arm ---
    # local_down projected onto the wall face plane.
    wd = local_down - wall_n * wall_n.dot(local_down)
    if wd.length < 1e-4:
        wd = local_down.copy()
    wd = wd.normalized()
    if wd.dot(local_up) > 0:
        wd = -wd

    return h_arm, wd


# ---------------------------------------------------------------------------
# Coving profile ring — 4 verts

def _coving_ring(seam_co, seam_z,
                 h_arm_in, h_arm_out,
                 wall_down_in, wall_down_out,
                 depth, thickness, notch_h, notch_v,
                 is_start, is_end):
    """Return (v0, v1, v2, v3) for one ring of the coving profile.

    v2 is positioned relative to v1 using notch_h and notch_v as signed
    blend controls where 0.5 is the neutral/rectangle position:

      v2 = v1 + h_arm    * (2*notch_h - 1) * depth
              + wall_down * (2*notch_v)     * thickness

    Key values:
      notch_h=0.5, notch_v=0.5  -> rectangle (v2 at far corner) [default]
      notch_h=0,   notch_v=0    -> right-angle triangle
      notch_h=1,   notch_v=1    -> kite

    Miter via _plane_intersect for offset legs at interior verts.
    """
    A = Vector(seam_co)

    if is_start:
        h_arm     = h_arm_out
        wall_down = wall_down_out
        v1_raw = A + h_arm * thickness
        v3_raw = A + wall_down * depth
        v2_raw = v1_raw + h_arm * ((2*notch_h - 1) * thickness) + wall_down * (2*notch_v * depth)
    elif is_end:
        h_arm     = h_arm_in
        wall_down = wall_down_in
        v1_raw = A + h_arm * thickness
        v3_raw = A + wall_down * depth
        v2_raw = v1_raw + h_arm * ((2*notch_h - 1) * thickness) + wall_down * (2*notch_v * depth)
    else:
        v1_raw = _plane_intersect(A, h_arm_in,    h_arm_out,    thickness)
        v3_raw = _plane_intersect(A, wall_down_in, wall_down_out, depth)
        wd_avg = wall_down_in + wall_down_out
        ha_avg = h_arm_in    + h_arm_out
        wd = wd_avg.normalized() if wd_avg.length > 1e-6 else wall_down_in
        ha = ha_avg.normalized() if ha_avg.length > 1e-6 else h_arm_in
        v2_raw = v1_raw + ha * ((2*notch_h - 1) * thickness) + wd * (2*notch_v * depth)
        wall_down = wd
        h_arm     = ha

    # Lock v1 and v2 Z to seam_z — enforces coplanar ceiling leg rule
    v1 = Vector((v1_raw.x, v1_raw.y, seam_z))
    # v2 has moved down by wall_down so its Z is NOT seam_z — keep it as computed
    v2 = v2_raw.copy()

    v0 = A.copy()
    v3 = v3_raw.copy()

    return v0, v1, v2, v3


# ---------------------------------------------------------------------------
# Build coving mesh from one chain

def _build_coving(cov_bm, chain, is_closed, depth, thickness, notch_h, notch_v,
                  mat_index=0, normal_mat=None, face_normals=None):

    """Sweep 4-vert coving profile along one edge chain."""
    verts   = _chain_verts(chain, is_closed)
    n       = len(verts)
    n_edges = n if is_closed else n - 1
    if n_edges < 1:
        return

    MIN_EDGE = 0.01
    tangents        = []
    edge_h_arms     = []
    edge_wall_downs = []

    # local_up in the working coordinate space (world space: normal_mat=None)
    if normal_mat is not None:
        try:
            local_up = normal_mat.inverted() @ Vector((0, 0, 1))
            local_up = local_up.normalized()
        except Exception:
            local_up = Vector((0, 0, 1))
    else:
        local_up = Vector((0, 0, 1))
    local_down = -local_up

    for idx in range(n_edges):
        j = (idx + 1) % n
        t = verts[j].co - verts[idx].co
        if t.length < MIN_EDGE:
            t = tangents[-1] if tangents else Vector((1, 0, 0))
        t = t.normalized() if t.length > 1e-6 else Vector((1, 0, 0))
        tangents.append(t)

        # h_arm: perpendicular to edge tangent in the horizontal plane.
        # Rotate tangent 90° to get the two candidate perpendiculars,
        # then use the face normal to pick the one pointing toward the room.
        # Use local_up.cross(t_h) — this correctly rotates in the
        # horizontal plane regardless of which axes are horizontal.
        # For Blender Z-up: local_up=(0,0,1), T=(1,0,0) → perp=(0,1,0) ✓
        # The face normal dot-product with the correct perp should be positive.
        t_h = t - local_up * local_up.dot(t)   # tangent projected to horizontal
        if t_h.length < 1e-6:
            t_h = t.copy()
        t_h = t_h.normalized()

        # Two candidate h_arms (perpendiculars to t_h in horizontal plane)
        cand_a = local_up.cross(t_h).normalized()
        cand_b = -cand_a                            # rotate CCW

        # Use face normal to choose the inward-pointing perpendicular
        edge = chain[idx]
        fn_list = (face_normals or {}).get(id(edge), [])
        if not fn_list and hasattr(edge, 'link_faces') and edge.link_faces:
            fn_list = [f.normal.normalized() for f in edge.link_faces]

        if fn_list:
            # Average the face normals, project to horizontal, use for sign
            avg_n = sum(fn_list, Vector()).normalized()
            avg_n_h = avg_n - local_up * local_up.dot(avg_n)
            if avg_n_h.length > 1e-6:
                avg_n_h = avg_n_h.normalized()
                h_arm = cand_a if avg_n_h.dot(cand_a) >= 0 else cand_b
            else:
                h_arm = cand_a
        else:
            # No face data — fall back to one of the candidates
            h_arm = cand_a

        # wall_down: local_down projected onto the most vertical face plane
        if fn_list:
            # most vertical face = smallest |dot with local_up|
            wall_n = min(fn_list, key=lambda n: abs(n.dot(local_up)))
            wd = local_down - wall_n * wall_n.dot(local_down)
            if wd.length < 1e-4:
                wd = local_down.copy()
            wd = wd.normalized()
            if wd.dot(local_up) > 0:
                wd = -wd
            wall_down = wd
        else:
            wall_down = local_down



        edge_h_arms.append(h_arm)
        edge_wall_downs.append(wall_down)

    if not is_closed:
        # Phantom end entry — mirrors last edge for start/end ring computation
        tangents.append(tangents[-1])
        edge_h_arms.append(edge_h_arms[-1])
        edge_wall_downs.append(edge_wall_downs[-1])

    STRIPS = [(0, 1), (1, 2), (2, 3), (3, 0)]

    def _face(vlist):
        try:
            f = cov_bm.faces.new(vlist)
            f.material_index = mat_index
        except Exception:
            pass

    # ── Shared miter verts ────────────────────────────────────────────────────
    # Compute ONE v1 position per seam vert using the miter of its two adjacent
    # edge arms. Both adjacent strip faces share this vert, so the ceiling strip
    # is gapless regardless of angle changes between edges.
    # v3 (wall) is also shared for the same reason.

    v1_pts       = []   # world-space ceiling-miter positions, one per seam vert
    v3_pts       = []   # world-space wall positions, one per seam vert
    junction_verts = []  # verts where miter arm > 1.1*thickness (collected first pass)

    for vi in range(n):
        is_start = (not is_closed) and vi == 0
        is_end   = (not is_closed) and vi == n - 1

        ei_out = vi % n_edges
        ei_in  = (vi - 1) % n_edges
        if is_start: ei_in  = ei_out
        if is_end:   ei_out = ei_in

        A      = Vector(verts[vi].co)
        seam_z = verts[vi].co.z
        ha_in  = edge_h_arms[ei_in]
        ha_out = edge_h_arms[ei_out]
        wd_in  = edge_wall_downs[ei_in]
        wd_out = edge_wall_downs[ei_out]

        if is_start or is_end:
            ha  = ha_out if is_start else ha_in
            wd  = wd_out if is_start else wd_in
            v1r = A + ha * thickness
            v3r = A + wd * depth

        else:
            c_h = ha_in.dot(ha_out)
            c_w = wd_in.dot(wd_out)
            if c_h < -0.9:
                v1r = A + ha_out * thickness
            else:
                v1r = _plane_intersect(A, ha_in, ha_out, thickness)
            if c_w < -0.9:
                v3r = A + wd_out * depth
            else:
                v3r = _plane_intersect(A, wd_in, wd_out, depth)

        # Lock v1 Z to seam height (coplanar ceiling rule)
        v1_pts.append(Vector((v1r.x, v1r.y, seam_z)))
        v3_pts.append(v3r)
        if not (is_start or is_end):
            c_h = ha_in.dot(ha_out)
            # Flag as junction when h_arms differ significantly AND one adjacent
            # edge is much longer than the other.
            # Bay/straight junction: c_h≈0, edge ratio ~6 (2m bay vs 12m straight).
            # Normal straight corners: c_h≈0 but similar edge lengths on both sides.
            # Bay micro-steps: c_h=0.995 (filtered by c_h threshold).
            if c_h < 0.5:
                # Measure the two adjacent seam edge lengths
                prev_vi_d = (vi - 1) % n
                len_in  = (Vector(verts[vi].co)      - Vector(verts[prev_vi_d].co)).length
                len_out = (Vector(verts[(vi+1)%n].co) - Vector(verts[vi].co)).length
                ratio = max(len_in, len_out) / max(min(len_in, len_out), 1e-6)
                # Only snap when transitioning short→long (entering straight wall from bay).
                # At long→short transitions (vi=17,33) the miter is already correct;
                # snapping there would corrupt v1[prev] by pulling it to the far end.
                if ratio > 3.0 and len_in < len_out:
                    junction_verts.append(vi)


    # ── Snap v1/v3 at antiparallel junctions ─────────────────────────────────
    # At junctions where the chain folds back (bay→straight), the antiparallel
    # arm condition fires and each side computes its own flush v1. These differ
    # slightly, leaving a crack in the ceiling strip. Snap both to the midpoint
    # of the outgoing arm's v1 — this is the correct shared miter point and
    # eliminates the crack without changing the geometry elsewhere.


    # ── Junction ceiling-line intersection post-pass ──────────────────────────
    # Collect junction verts during the main loop (arm_len > 1.1*thickness),
    # then apply the ceiling-line intersection exactly once per junction.
    # Must not re-read v1_pts (already modified) — use original arm data.
    # ── Junction snap (closed chains only) ────────────────────────────────────
    # Only snap for closed chains — open chains have genuine start/end endpoints
    # that must not be collapsed, and their junction detection misfires at the
    # open-chain endpoints which share positions with the closed OuterWall loop.
    if not is_closed:
        junction_verts = []
    for vi in junction_verts:
        # At a long→short junction (straight wall exits to bay curve),
        # snap the first back-side ring to the junction miter so the
        # straight wall and back-side curve share a clean termination point.
        # The bay-side (short→long) needs no snap — it already miters correctly.
        prev_vi = (vi - 1) % n
        nxt_vi  = (vi + 1) % n
        len_in  = (Vector(verts[vi].co) - Vector(verts[prev_vi].co)).length
        len_out = (Vector(verts[nxt_vi].co) - Vector(verts[vi].co)).length
        if len_in > len_out:
            v1_pts[nxt_vi] = Vector(v1_pts[vi])

    # ── Notch (v2) is computed per-strip-face, not per seam-vert ─────────────
    # v2 sits between v1 and v3 of the same seam vert.
    # We keep it per-ring (same as before) since it doesn't need to be shared.

    # ── Build strip faces using shared v1 / v3 verts ─────────────────────────
    # Each seam vert contributes one v0 (seam) and shared v1, v3.
    # Strip face between verts i and j:
    #   ceiling quad: v0[i], v1[i], v1[j], v0[j]   (top face of ceiling leg)
    #   notch  quad:  v1[i], v2[i], v2[j], v1[j]   (chamfer between legs)
    #   wall   quad:  v2[i], v3[i], v3[j], v2[j]   (wall leg face)
    #   back   quad:  v3[i], v0[i], v0[j], v3[j]   (back/wall face)
    # End caps close the open chain ends.

    def _v2(vi):
        """Notch vert for seam vert vi — between v1 and v3."""
        ha = (edge_h_arms[vi % n_edges] if not ((not is_closed) and vi == n-1)
              else edge_h_arms[n_edges-1])
        wd = (edge_wall_downs[vi % n_edges] if not ((not is_closed) and vi == n-1)
              else edge_wall_downs[n_edges-1])
        return v1_pts[vi] + ha * ((2*notch_h - 1) * thickness) + wd * (2*notch_v * depth)

    # Create seam verts
    sv_bm = [cov_bm.verts.new(Vector(verts[vi].co)) for vi in range(n)]

    # Build v1_bm sharing BMVerts for snapped (coincident) positions
    v1_bm = []
    _v1_pos_to_bv = {}
    for vi in range(n):
        p = v1_pts[vi]
        k = (round(p.x,4), round(p.y,4), round(p.z,4))
        if k in _v1_pos_to_bv:
            v1_bm.append(_v1_pos_to_bv[k])
        else:
            bv = cov_bm.verts.new(p)
            _v1_pos_to_bv[k] = bv
            v1_bm.append(bv)

    # Build v2_bm similarly
    v2_bm = []
    _v2_pos_to_bv = {}
    for vi in range(n):
        p = _v2(vi)
        k = (round(p.x,4), round(p.y,4), round(p.z,4))
        if k in _v2_pos_to_bv:
            v2_bm.append(_v2_pos_to_bv[k])
        else:
            bv = cov_bm.verts.new(p)
            _v2_pos_to_bv[k] = bv
            v2_bm.append(bv)

    # Build v3_bm similarly
    v3_bm = []
    _v3_pos_to_bv = {}
    for vi in range(n):
        p = v3_pts[vi]
        k = (round(p.x,4), round(p.y,4), round(p.z,4))
        if k in _v3_pos_to_bv:
            v3_bm.append(_v3_pos_to_bv[k])
        else:
            bv = cov_bm.verts.new(p)
            _v3_pos_to_bv[k] = bv
            v3_bm.append(bv)

    loop = range(n) if is_closed else range(n - 1)
    for i in loop:
        j = (i + 1) % n
        # When ceiling arm is snapped (v1[i]==v1[j]), the ceiling/notch faces
        # are degenerate — skip them. But wall and back faces are still valid
        # (v3 is not snapped, just v1).
        # Skip completely degenerate strips where profile points coincide.
        # Use position distance checks for all profile levels.
        v1_same = (v1_pts[i] - v1_pts[j]).length < 1e-4
        v3_same = (v3_pts[i] - v3_pts[j]).length < 1e-4
        sv_same = (Vector(verts[i].co) - Vector(verts[j].co)).length < 1e-4
        if v1_same and (v3_same or sv_same):
            continue
        _face([sv_bm[i], v1_bm[i], v1_bm[j], sv_bm[j]])
        _face([v1_bm[i], v2_bm[i], v2_bm[j], v1_bm[j]])
        _face([v2_bm[i], v3_bm[i], v3_bm[j], v2_bm[j]])
        _face([v3_bm[i], sv_bm[i], sv_bm[j], v3_bm[j]])

    if not is_closed and n >= 2:
        # Start cap
        _face([v2_bm[0],  v1_bm[0],  sv_bm[0]])
        _face([v3_bm[0],  v2_bm[0],  sv_bm[0]])
        # End cap
        _face([sv_bm[-1], v1_bm[-1], v2_bm[-1]])
        _face([sv_bm[-1], v2_bm[-1], v3_bm[-1]])

    cov_bm.normal_update()



# ---------------------------------------------------------------------------
# Z coplanarity check

def _chain_z_ok(chain, is_closed, eps=Z_NOISE_EPS, normal_mat=None, matrix_world=None):
    """Return (ok, z_ref, max_deviation).

    Checks in WORLD space Z so that objects with non-identity transforms
    and meshes with verts at genuinely different world heights are caught.
    """
    verts = _chain_verts(chain, is_closed)
    if not verts:
        return True, 0.0, 0.0

    def _world_z(v):
        if matrix_world is not None:
            return (matrix_world @ v.co).z
        return v.co.z

    z_ref   = _world_z(verts[0])
    max_dev = max((abs(_world_z(v) - z_ref) for v in verts[1:]), default=0.0)
    return max_dev <= eps, z_ref, max_dev


# ---------------------------------------------------------------------------
# Beam profile sweep (same cross-section as coving)

def _build_beam(beam_bm, start_co, end_co, depth, thickness, notch_h, notch_v,
                mat_index=0):
    """Sweep coving profile from start_co to end_co, flush end caps."""
    axis = Vector(end_co) - Vector(start_co)
    if axis.length < 1e-4:
        return

    t_dir = axis.normalized()
    world_up = Vector((0, 0, 1))

    # Horizontal arm — perp to t_dir in XY
    h_arm = t_dir.cross(world_up)
    if h_arm.length < 1e-6:
        h_arm = t_dir.cross(Vector((0, 1, 0)))
    h_arm = h_arm.normalized()

    # Wall-down — straight down for a horizontal beam
    wall_down = -world_up

    # Centre offset — shifts the profile so its bounding box midpoint sits on
    # the empty location rather than v0 being at the empty location.
    centre_offset = h_arm * (thickness * 0.5) + wall_down * (depth * 0.5)

    def _ring(co):
        A = Vector(co) - centre_offset
        v0 = A.copy()
        v1 = Vector((A.x + h_arm.x * thickness, A.y + h_arm.y * thickness, A.z))
        v2 = v1 + h_arm * ((2*notch_h - 1) * thickness) + wall_down * (2*notch_v * depth)
        v3 = A + wall_down * depth
        return v0, v1, v2, v3

    ring_s = [beam_bm.verts.new(p) for p in _ring(start_co)]
    ring_e = [beam_bm.verts.new(p) for p in _ring(end_co)]

    STRIPS = [(0, 1), (1, 2), (2, 3), (3, 0)]

    def _face(vlist):
        try:
            f = beam_bm.faces.new(vlist)
            f.material_index = mat_index
        except Exception:
            pass

    for a, b in STRIPS:
        _face([ring_s[a], ring_s[b], ring_e[b], ring_e[a]])

    v0s, v1s, v2s, v3s = ring_s
    _face([v2s, v1s, v0s])
    _face([v3s, v2s, v0s])

    v0e, v1e, v2e, v3e = ring_e
    _face([v0e, v1e, v2e])
    _face([v0e, v2e, v3e])

    beam_bm.normal_update()



def _build_curve_beam(bm, ring_positions, depth, thickness, mat_index=0):
    """Sweep the beam profile along an ordered list of ring positions with
    mitered joins at every interior ring.

    At each ring the local tangent is the bisector of the incoming and
    outgoing segment directions — this rotates h_arm correctly so adjacent
    segments meet at a clean mitered angle rather than a perpendicular cut.
    End rings use the single adjacent segment tangent (flat cap).

    ring_positions: list of Vector, at least 2 entries.
    """
    if len(ring_positions) < 2:
        return

    world_up = Vector((0, 0, 1))
    notch_h  = 0.5
    notch_v  = 0.5

    def _tangent(i):
        """Local tangent at ring i — bisector for interior, segment for ends."""
        n = len(ring_positions)
        if i == 0:
            t = (ring_positions[1] - ring_positions[0])
        elif i == n - 1:
            t = (ring_positions[-1] - ring_positions[-2])
        else:
            t_in  = (ring_positions[i]     - ring_positions[i - 1]).normalized()
            t_out = (ring_positions[i + 1] - ring_positions[i]).normalized()
            t     = t_in + t_out
        if t.length < 1e-6:
            t = Vector((0, 1, 0))
        return t.normalized()

    def _profile(co, tangent):
        """Build 4-vert profile at co oriented to tangent."""
        h_arm = tangent.cross(world_up)
        if h_arm.length < 1e-6:
            h_arm = tangent.cross(Vector((0, 1, 0)))
        h_arm      = h_arm.normalized()
        wall_down  = -world_up
        centre_off = h_arm * (thickness * 0.5) + wall_down * (depth * 0.5)
        A  = Vector(co) - centre_off
        v0 = A.copy()
        v1 = A + h_arm * thickness
        v2 = v1 + h_arm * ((2*notch_h - 1) * thickness) + wall_down * (2*notch_v * depth)
        v3 = A + wall_down * depth
        return v0, v1, v2, v3

    def _face(vlist):
        try:
            f = bm.faces.new(vlist)
            f.material_index = mat_index
        except Exception:
            pass

    # Build all profile rings
    rings = []
    for i, pos in enumerate(ring_positions):
        t    = _tangent(i)
        verts = [bm.verts.new(p) for p in _profile(pos, t)]
        rings.append(verts)

    # Stitch strip faces between adjacent rings
    STRIPS = [(0, 1), (1, 2), (2, 3), (3, 0)]
    for ri in range(len(rings) - 1):
        rs = rings[ri]
        re = rings[ri + 1]
        for a, b in STRIPS:
            _face([rs[a], rs[b], re[b], re[a]])

    # Start cap (flat, perpendicular to first segment)
    v0s, v1s, v2s, v3s = rings[0]
    _face([v2s, v1s, v0s])
    _face([v3s, v2s, v0s])

    # End cap
    v0e, v1e, v2e, v3e = rings[-1]
    _face([v0e, v1e, v2e])
    _face([v0e, v2e, v3e])

    bm.normal_update()

# ---------------------------------------------------------------------------
# Beam empty discovery (used by both Generate Beams and beam_placement.py)

def _get_empties_by_prefix(prefix):
    """Return ordered (start, end) pairs for empties named prefix_NNN_1/2."""
    from collections import defaultdict
    groups = defaultdict(dict)
    for obj in bpy.data.objects:
        if obj.type != 'EMPTY':
            continue
        if not obj.name.startswith(prefix + '_'):
            continue
        parts = obj.name.rsplit('_', 1)
        if len(parts) == 2 and parts[1] in ('1', '2'):
            groups[parts[0]][int(parts[1])] = obj
    pairs = []
    for key in sorted(groups.keys()):
        g = groups[key]
        if 1 in g and 2 in g:
            pairs.append((g[1], g[2]))
    return pairs


def _get_beam_empties(context):
    """Legacy helper — returns beam_NNN pairs. Used by old beam_placement refs."""
    return _get_empties_by_prefix('beam')


# ---------------------------------------------------------------------------
# Operator 1: Generate Coving

class OT_FBXMT_Generate_Coving(Operator):
    bl_idname      = 'fbxmt.generate_coving'
    bl_label       = 'Generate Coving'
    bl_description = ('Sweep rectangular coving profile along any selected edge '
                      'loop. Ceiling leg stays coplanar with seam Z; wall leg '
                      'steps downward flush to wall face. Chains with non-planar '
                      'Z are warned and skipped.')
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH'
                and context.active_object is not None
                and context.active_object.type == 'MESH')

    def execute(self, context):
        props     = context.scene.fbxmt_props
        depth     = props.coving_depth
        thickness = props.coving_thickness
        notch_h   = props.coving_notch_h
        notch_v   = props.coving_notch_v

        # ── Collect edge data from all Edit-Mode objects into plain Python ──
        # We do NOT build topology in bmesh here — vert deduplication across
        # mesh boundaries corrupts the seam_edge_normals↔edge ordering.
        # Instead, store each selected edge as a plain record:
        #   (co_a, co_b, [world_face_normals])
        # Then build world_bm from those records after mode_set, with every
        # edge guaranteed to have its normal stored at the matching index.

        edit_objs = [o for o in context.selected_objects if o.type == 'MESH']
        if context.active_object and context.active_object not in edit_objs:
            edit_objs.insert(0, context.active_object)

        src_name = (context.active_object.name if context.active_object
                    else 'Coving').replace(' ', '_')

        # edge_records: list of (co_a, co_b, normals, obj_tag)
        # obj_tag identifies which source object the edge came from
        edge_records     = []
        seam_edge_coords = []  # for debug export

        for obj_idx, src_obj in enumerate(edit_objs):
            if src_obj is None or src_obj.type != 'MESH':
                continue
            mw = src_obj.matrix_world
            nm = mw.to_3x3().normalized()
            src_bm = bmesh.from_edit_mesh(src_obj.data)
            src_bm.verts.ensure_lookup_table()
            src_bm.edges.ensure_lookup_table()
            src_bm.faces.ensure_lookup_table()
            sel_edges = [e for e in src_bm.edges if e.select]
            if not sel_edges:
                continue
            for e in sel_edges:
                co_a = tuple(mw @ e.verts[0].co)
                co_b = tuple(mw @ e.verts[1].co)
                norms = [(nm @ f.normal.normalized()).normalized()
                         for f in e.link_faces]
                edge_records.append((co_a, co_b, norms, obj_idx))
                seam_edge_coords.append((co_a, co_b))

        # ── Build world_bm from clean records ──────────────────────────────
        # Deduplicate verts by rounded position (1e-4 m tolerance).
        # Two verts from different meshes at the same position become ONE vert
        # in world_bm — no duplicate verts, no failed edges.new() calls.
        # face_normals_map is keyed by id(BMEdge) AFTER ensure_lookup_table.
        SNAP = 1e-4
        def _snap_key(co):
            return (round(co[0]/SNAP), round(co[1]/SNAP), round(co[2]/SNAP))

        world_bm      = bmesh.new()
        pos_to_wv     = {}   # snap_key → BMVert
        edge_normals  = []   # parallel to edge_records, normals per edge
        world_edges   = []   # BMEdge objects in edge_records order
        # Track which snap_keys belong to each obj_tag for boundary detection

        edge_obj_tags = []  # parallel to world_edges — which obj_idx owns each edge
        for co_a, co_b, norms, obj_idx in edge_records:
            ka, kb = _snap_key(co_a), _snap_key(co_b)
            if ka not in pos_to_wv:
                pos_to_wv[ka] = world_bm.verts.new(Vector(co_a))
            if kb not in pos_to_wv:
                pos_to_wv[kb] = world_bm.verts.new(Vector(co_b))
            wva, wvb = pos_to_wv[ka], pos_to_wv[kb]
            try:
                we = world_bm.edges.new((wva, wvb))
                world_edges.append(we)
                edge_normals.append(norms)
                edge_obj_tags.append(obj_idx)
            except Exception:
                # Edge already exists — find it and merge normals so both
                # mesh objects' face normals are available for wall_down calc.
                existing = wva.link_edges and next(
                    (e for e in wva.link_edges if wvb in e.verts), None)
                if existing is not None:
                    # Find the existing edge's norms entry and extend it
                    for i, we_i in enumerate(world_edges):
                        if we_i is not None and id(we_i) == id(existing):
                            edge_normals[i] = edge_normals[i] + norms
                            break
                world_edges.append(None)
                edge_normals.append(norms)
                edge_obj_tags.append(obj_idx)

        # Boundary verts: snap_keys that appear in more than one obj_tag
        # and build face_normals_map BEFORE mode_set (no index invalidation yet)
        # Key by id(BMEdge) — stable Python object identity
        face_normals_pre = {id(we): norms
                            for we, norms in zip(world_edges, edge_normals)
                            if we is not None}
        seam_edges_clean = [we for we in world_edges if we is not None]

        bpy.ops.object.mode_set(mode='OBJECT')
        try:
            world_bm.normal_update()

            # Use the pre-built seam edge list and face_normals_map.
            # These were built from plain Python records before mode_set, so
            # BMEdge id() values are stable — no index invalidation possible.
            selected_edges   = seam_edges_clean
            face_normals_map = face_normals_pre

            # Merge coincident verts from different mesh objects.
            # Verts from separate meshes sharing a position may differ by sub-mm
            # floating point amounts and need an explicit merge pass.
            _vert_norms = {}
            for _e in list(world_bm.edges):
                _en = face_normals_pre.get(id(_e), [])
                for _v in _e.verts:
                    _k = (round(_v.co.x, 3), round(_v.co.y, 3), round(_v.co.z, 3))
                    _vert_norms.setdefault(_k, []).extend(_en)
            bmesh.ops.remove_doubles(world_bm, verts=list(world_bm.verts), dist=0.01)
            # Rebuild selected_edges and face_normals_map after merge
            selected_edges = list(world_bm.edges)
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
                self.report({'WARNING'}, 'No edges selected across any active mesh')
                return {'CANCELLED'}

            ensure_fbxmt_materials()
            trim_mat = bpy.data.materials.get('M_FBXMT_Trim')
            if trim_mat is None:
                world_bm.free()
                self.report({'ERROR'}, 'M_FBXMT_Trim not found — run Setup Scene first')
                return {'CANCELLED'}



            chains, closed_flags = _chain_edges(selected_edges)

            # If the result isn't all closed chains, attempt T-junction
            # sanitisation and then chain each source mesh's edges separately.
            # Cross-mesh selections (InnerWall + OuterWall) produce intersecting
            # loops that must be walked independently, not as one combined graph.
            _needs_sanitise = any(not cf for cf in closed_flags)
            if _needs_sanitise:
                sel_e, face_normals_map, split_host_map =                     _sanitise_t_junctions(world_bm, selected_edges, face_normals_map)
                selected_edges = sel_e
                # Build per-obj edge sets.
                # Original edges → edge_id_to_tag.
                # Split half-edges → split_host_map → host edge → tag.
                n_objs = max(edge_obj_tags) + 1 if edge_obj_tags else 1
                edge_id_to_tag = {}
                for we, tag in zip(world_edges, edge_obj_tags):
                    if we is not None:
                        edge_id_to_tag[id(we)] = tag
                def _get_tag(eid):
                    if eid in split_host_map:
                        return edge_id_to_tag.get(split_host_map[eid], 0)
                    return edge_id_to_tag.get(eid, 0)
                obj_edge_sets = [[] for _ in range(n_objs)]
                for e in selected_edges:
                    tag = _get_tag(id(e))
                    obj_edge_sets[tag].append(e)
                # Chain each obj's edges independently
                chains = []; closed_flags = []
                for obj_edges_i in obj_edge_sets:
                    if not obj_edges_i:
                        continue
                    c, cf = _chain_edges(obj_edges_i)
                    chains.extend(c)
                    closed_flags.extend(cf)

            if not chains:
                world_bm.free()
                self.report({'WARNING'}, 'Could not build edge chains')
                return {'CANCELLED'}

            valid_chains = []
            valid_closed = []
            skipped      = 0
            for chain, is_closed in zip(chains, closed_flags):
                ok, z_ref, max_dev = _chain_z_ok(chain, is_closed, eps=Z_NOISE_EPS)
                if ok:
                    valid_chains.append(chain)
                    valid_closed.append(is_closed)
                else:
                    skipped += 1
                    self.report({'WARNING'},
                                f'Chain skipped — seam verts not coplanar in Z '
                                f'(max deviation {max_dev:.4f} m, ref Z {z_ref:.4f} m).')

            if not valid_chains:
                world_bm.free()
                self.report({'WARNING'}, 'All chains failed the Z coplanarity check.')
                return {'CANCELLED'}

            cov_bm = bmesh.new()
            for chain, is_closed in zip(valid_chains, valid_closed):
                _build_coving(cov_bm, chain, is_closed,
                              depth, thickness, notch_h, notch_v,
                              mat_index=0, normal_mat=None,
                              face_normals=face_normals_map)

            suffix   = '_Coving'
            cov_mesh = bpy.data.meshes.new(f'{src_name}{suffix}')
            cov_mesh.materials.append(trim_mat)
            # remove_doubles intentionally omitted — triangle detection handles snapped verts
            cov_bm.to_mesh(cov_mesh)
            cov_bm.free()
            # Recalculate normals to point outward — generator produces
            # inward-facing normals which break ray-cast based tools
            bpy.ops.object.select_all(action='DESELECT')
            temp_obj = bpy.data.objects.new('_fbxmt_temp_norm', cov_mesh)
            context.collection.objects.link(temp_obj)
            context.view_layer.objects.active = temp_obj
            temp_obj.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.normals_make_consistent(inside=False)
            bpy.ops.object.mode_set(mode='OBJECT')
            context.collection.objects.unlink(temp_obj)
            bpy.data.objects.remove(temp_obj, do_unlink=False)
            cov_mesh.update()

            cov_obj = bpy.data.objects.new(f'{src_name}{suffix}', cov_mesh)
            context.collection.objects.link(cov_obj)
            move_to_collection(cov_obj, COLLECTION_TRIM)
            world_bm.free()

            msg = f'Coving generated ({len(valid_chains)} chain(s))'
            if skipped:
                msg += f', {skipped} chain(s) skipped (non-planar Z)'
            self.report({'INFO'}, msg)
            return {'FINISHED'}

        finally:
            # Always restore Edit Mode — prevents Blender deadlock on any
            # exception between mode_set(OBJECT) and end of operator.
            try:
                if context.mode != 'EDIT_MESH':
                    bpy.ops.object.mode_set(mode='EDIT')
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Smart ray-cast for parallel beam _2 placement

_PARALLEL_THRESHOLD = 0.1   # abs(dot) below this = edge-on / pass-through face
_RAY_MAX_DIST       = 100.0
_RAY_OFFSET         = 0.001 # nudge past current hit to continue casting


def _smart_raycast(obj, ray_origin, ray_dir, depsgraph):
    """Cast ray along ray_dir, passing through edge-on faces.

    A face is edge-on if abs(dot(ray_dir, face_normal)) < _PARALLEL_THRESHOLD —
    the ray sees it from its edge rather than its front.  In that case the ray
    has clipped both leading and trailing edges of a parallel face — jump through
    and continue hunting for a true terminator face.

    Returns world-space hit location, or None if no valid terminator found.
    """
    from mathutils import Vector

    mat_inv  = obj.matrix_world.inverted()
    rot_inv  = mat_inv.to_3x3()

    # Nudge origin slightly along ray direction so we don't immediately
    # hit the face the empty is sitting on
    origin    = Vector(ray_origin) + Vector(ray_dir).normalized() * 0.02
    direction = Vector(ray_dir).normalized()

    max_iter = 32   # guard against infinite loops in degenerate geometry

    for _ in range(max_iter):
        # Ray-cast in object local space
        local_orig = mat_inv @ origin
        local_dir  = (rot_inv @ direction).normalized()

        hit, loc, normal, face_idx = obj.ray_cast(local_orig, local_dir,
                                                   distance=_RAY_MAX_DIST)
        if not hit:
            return None

        # World-space normal of hit face
        world_normal = (obj.matrix_world.to_3x3() @ normal).normalized()
        world_loc    = obj.matrix_world @ loc

        dot = abs(direction.dot(world_normal))

        if dot >= _PARALLEL_THRESHOLD:
            # Non-parallel face — genuine terminator
            return world_loc

        # Edge-on face — jump past it and continue
        origin = world_loc + direction * _RAY_OFFSET

    return None   # ran out of iterations


# ---------------------------------------------------------------------------
# Shared generate helper

def _generate_beams_from_pairs(context, pairs, depth, thickness,
                                export_stem, merge_verts=False):
    """Build beam geometry for each (start, end) empty pair.

    For each pair:
      1. Pull both ends inward by coving_depth along the beam axis so end
         faces sit inside the source mesh volume.
      2. Build the beam mesh.
      3. Add a Boolean Difference modifier using the source mesh stored on
         the empty as fbxmt_source — trims both ends cleanly.
      4. Apply the modifier (destructive, at end of operation stack).

    merge_verts: weld coincident verts (curve beams).
    Returns (generated_objects, export_message).
    """
    import os
    from mathutils import Vector
    props = context.scene.fbxmt_props

    ensure_fbxmt_materials()
    trim_mat = bpy.data.materials.get('M_FBXMT_Trim')
    if trim_mat is None:
        return [], 'M_FBXMT_Trim not found — run Setup Scene first'

    generated    = []
    vert_markers = []
    pullback     = 0.25    # extend each end outward into coving mesh for boolean cut

    for start_empty, end_empty in pairs:
        start_co = Vector(start_empty.matrix_world.translation)
        end_co   = Vector(end_empty.matrix_world.translation)

        # ── Extend both ends outward into coving mesh for boolean cut ─────
        axis       = end_co - start_co
        length     = axis.length
        group_name = start_empty.name.rsplit('_', 1)[0]
        if length > 1e-4:
            t_dir    = axis / length
            start_co = start_co - t_dir * pullback
            end_co   = end_co   + t_dir * pullback

        # ── Build beam mesh ───────────────────────────────────────────────
        beam_bm = bmesh.new()
        _build_beam(beam_bm, start_co, end_co,
                    depth, thickness, 0.5, 0.5, mat_index=0)

        if merge_verts:
            bmesh.ops.remove_doubles(beam_bm,
                                     verts=list(beam_bm.verts), dist=0.001)

        beam_mesh  = bpy.data.meshes.new(f'{group_name}_Beam')
        beam_mesh.materials.append(trim_mat)
        beam_bm.to_mesh(beam_mesh)
        beam_bm.free()
        beam_mesh.update()

        beam_obj = bpy.data.objects.new(f'{group_name}_Beam', beam_mesh)
        context.collection.objects.link(beam_obj)
        move_to_collection(beam_obj, COLLECTION_TRIM)
        generated.append(beam_obj)

        # ── Boolean Difference using source mesh stored on empty ──────────
        source_name = start_empty.get('fbxmt_source', '')
        if not source_name:
            source_name = end_empty.get('fbxmt_source', '')
        source_obj = bpy.data.objects.get(source_name) if source_name else None

        if source_obj and source_obj.type == 'MESH':
            mod = beam_obj.modifiers.new(name='FBXMT_BoolTrim', type='BOOLEAN')
            mod.operation = 'DIFFERENCE'
            mod.object    = source_obj
            mod.solver    = 'FLOAT'

            bpy.ops.object.select_all(action='DESELECT')
            beam_obj.select_set(True)
            context.view_layer.objects.active = beam_obj
            try:
                bpy.ops.object.modifier_apply(modifier='FBXMT_BoolTrim')
            except Exception as e:
                import sys as _sys
                print(f'FBXMT: Boolean apply failed for {group_name}: {e}',
                      file=_sys.stderr)

        # ── Vert markers at original empty positions ──────────────────────
        for empty in (start_empty, end_empty):
            marker_name = f'{empty.name}_marker'
            me = bpy.data.meshes.new(marker_name)
            bm = bmesh.new()
            bm.verts.new(empty.matrix_world.translation.copy())
            bm.to_mesh(me)
            bm.free()
            me.update()
            marker_obj = bpy.data.objects.new(marker_name, me)
            context.collection.objects.link(marker_obj)
            vert_markers.append(marker_obj)

    # ── OBJ export ────────────────────────────────────────────────────────
    export_folder = props.export_path.strip() if props.export_path else ''
    if export_folder and os.path.isdir(export_folder):
        counter = 1
        while os.path.exists(
                os.path.join(export_folder, f'{export_stem}_{counter:03d}.obj')):
            counter += 1
        filepath = os.path.join(export_folder,
                                f'{export_stem}_{counter:03d}.obj')

        bpy.ops.object.select_all(action='DESELECT')
        for obj in generated + vert_markers:
            obj.select_set(True)
        if generated:
            context.view_layer.objects.active = generated[0]

        bpy.ops.wm.obj_export(
            filepath=filepath,
            export_selected_objects=True,
            export_materials=False,
        )
        export_msg = f'exported {os.path.basename(filepath)}'
    elif export_folder:
        export_msg = f'export folder not found: {export_folder}'
    else:
        export_msg = 'set export folder in Project Setup to auto-export'

    # ── Remove vert markers and source empties ───────────────────────────
    for marker_obj in vert_markers:
        bpy.data.objects.remove(marker_obj, do_unlink=True)

    for start_empty, end_empty in pairs:
        for e in (start_empty, end_empty):
            try:
                bpy.data.objects.remove(e, do_unlink=True)
            except Exception:
                pass

    bpy.ops.object.select_all(action='DESELECT')
    return generated, export_msg


# ---------------------------------------------------------------------------
# Operator: Generate Parallel Beams

class OT_FBXMT_Generate_Parallel(Operator):
    bl_idname      = 'fbxmt.generate_parallel'
    bl_label       = 'Generate Parallel Beams'
    bl_description = ('Ray-cast from par_NNN_1 empties along stored face normals '
                      'to find _2 positions using smart edge-on pass-through logic. '
                      'Builds beams, boolean trims, exports OBJ.')
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        import os
        import sys as _sys
        from mathutils import Vector

        props      = context.scene.fbxmt_props
        depsgraph  = context.evaluated_depsgraph_get()

        # Collect _1 empties only
        anchors = [o for o in bpy.data.objects
                   if o.type == 'EMPTY'
                   and o.name.startswith('par_')
                   and o.name.rsplit('_', 1)[-1] == '1']
        anchors.sort(key=lambda o: o.name)

        if not anchors:
            self.report({'WARNING'},
                'No par_NNN_1 empties found — use Place Parallel Beams first.')
            return {'CANCELLED'}

        ensure_fbxmt_materials()
        trim_mat = bpy.data.materials.get('M_FBXMT_Trim')
        if trim_mat is None:
            self.report({'ERROR'}, 'M_FBXMT_Trim not found — run Setup Scene first')
            return {'CANCELLED'}

        generated   = []
        vert_markers = []
        pullback    = 0.25
        skipped     = 0

        for anchor in anchors:
            # Read stored normal
            raw_normal = anchor.get('fbxmt_normal', None)
            if raw_normal is None:
                print(f'FBXMT Parallel: {anchor.name} has no stored normal — skipping',
                      file=_sys.stderr)
                skipped += 1
                continue

            ray_origin = Vector(anchor.matrix_world.translation)
            ray_dir    = Vector(raw_normal).normalized()

            # Get source mesh for ray-cast
            source_name = anchor.get('fbxmt_source', '')
            source_obj  = bpy.data.objects.get(source_name) if source_name else None

            if source_obj is None or source_obj.type != 'MESH':
                print(f'FBXMT Parallel: {anchor.name} source {source_name!r} not found — skipping',
                      file=_sys.stderr)
                skipped += 1
                continue

            # Smart ray-cast to find _2 position
            hit_loc = _smart_raycast(source_obj, ray_origin, ray_dir, depsgraph)

            if hit_loc is None:
                print(f'FBXMT Parallel: {anchor.name} ray found no terminator — skipping',
                      file=_sys.stderr)
                skipped += 1
                continue

            # Extend both ends outward for boolean
            axis   = hit_loc - ray_origin
            length = axis.length
            if length < 1e-4:
                skipped += 1
                continue
            t_dir    = axis / length
            start_co = ray_origin - t_dir * pullback
            end_co   = hit_loc   + t_dir * pullback

            # Build beam
            group_name = anchor.name.rsplit('_', 1)[0]
            beam_bm    = bmesh.new()
            _build_beam(beam_bm, start_co, end_co,
                        props.coving_depth, props.coving_thickness,
                        0.5, 0.5, mat_index=0)

            beam_mesh = bpy.data.meshes.new(f'{group_name}_Beam')
            beam_mesh.materials.append(trim_mat)
            beam_bm.to_mesh(beam_mesh)
            beam_bm.free()
            beam_mesh.update()

            beam_obj = bpy.data.objects.new(f'{group_name}_Beam', beam_mesh)
            context.collection.objects.link(beam_obj)
            move_to_collection(beam_obj, COLLECTION_TRIM)
            generated.append(beam_obj)

            # Vert markers
            for pos in (ray_origin, hit_loc):
                me = bpy.data.meshes.new(f'{group_name}_marker')
                bm = bmesh.new()
                bm.verts.new(pos.copy())
                bm.to_mesh(me)
                bm.free()
                me.update()
                mo = bpy.data.objects.new(f'{group_name}_marker', me)
                context.collection.objects.link(mo)
                vert_markers.append(mo)

            # Boolean trim
            bpy.ops.object.select_all(action='DESELECT')
            beam_obj.select_set(True)
            context.view_layer.objects.active = beam_obj

            mod = beam_obj.modifiers.new(name='FBXMT_BoolTrim', type='BOOLEAN')
            mod.operation = 'DIFFERENCE'
            mod.object    = source_obj
            mod.solver    = 'FLOAT'
            # Modifier left in stack for fine-tuning after generation

            # Remove anchor empty
            try:
                bpy.data.objects.remove(anchor, do_unlink=True)
            except Exception:
                pass

        # OBJ export
        export_folder = props.export_path.strip() if props.export_path else ''
        if export_folder and os.path.isdir(export_folder):
            counter = 1
            while os.path.exists(
                    os.path.join(export_folder, f'beams_parallel_{counter:03d}.obj')):
                counter += 1
            filepath = os.path.join(export_folder,
                                    f'beams_parallel_{counter:03d}.obj')
            bpy.ops.object.select_all(action='DESELECT')
            for obj in generated + vert_markers:
                obj.select_set(True)
            if generated:
                context.view_layer.objects.active = generated[0]
            bpy.ops.wm.obj_export(
                filepath=filepath,
                export_selected_objects=True,
                export_materials=False,
            )
            export_msg = f'exported {os.path.basename(filepath)}'
        elif export_folder:
            export_msg = f'export folder not found: {export_folder}'
        else:
            export_msg = 'set export folder in Project Setup to auto-export'

        for mo in vert_markers:
            bpy.data.objects.remove(mo, do_unlink=True)

        bpy.ops.object.select_all(action='DESELECT')

        msg = f'{len(generated)} parallel beam(s) generated'
        if skipped:
            msg += f' ({skipped} skipped — check console)'
        msg += f' — {export_msg}'
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Generate Spoke Beams

class OT_FBXMT_Generate_Spokes(Operator):
    bl_idname      = 'fbxmt.generate_spokes'
    bl_label       = 'Generate Spoke Beams'
    bl_description = ('Generate beam geometry from spk_NNN_1/2 empties. '
                      'Uses coving Depth (V) / Thickness (H). '
                      'Exports OBJ to export folder if set.')
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        props = context.scene.fbxmt_props
        pairs = _get_empties_by_prefix('spk')
        if not pairs:
            self.report({'WARNING'},
                'No spk_NNN_1/2 empties found — use Place Spoke Beams first.')
            return {'CANCELLED'}

        generated, export_msg = _generate_beams_from_pairs(
            context, pairs,
            depth=props.coving_depth,
            thickness=props.coving_thickness,
            export_stem='beams_spoke',
            merge_verts=False,
        )
        if not generated:
            self.report({'ERROR'}, export_msg)
            return {'CANCELLED'}
        self.report({'INFO'},
                    f'{len(generated)} spoke beam(s) generated — {export_msg}')
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Generate Curve Beams

class OT_FBXMT_Generate_Curve(Operator):
    bl_idname      = 'fbxmt.generate_curve'
    bl_label       = 'Generate Curve Beams'
    bl_description = ('Generate mitered curve beam from crv_NNN_1/2 empties. '
                      'All segments built in one sweep with mitered joins. '
                      'Uses curve-specific Depth (V) / Thickness (H). '
                      'Exports OBJ to export folder if set.')
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        import os
        from mathutils import Vector
        props = context.scene.fbxmt_props

        pairs = _get_empties_by_prefix('crv')
        if not pairs:
            self.report({'WARNING'},
                'No crv_NNN_1/2 empties found — use Place Curve Beams first.')
            return {'CANCELLED'}

        ensure_fbxmt_materials()
        trim_mat = bpy.data.materials.get('M_FBXMT_Trim')
        if trim_mat is None:
            self.report({'ERROR'}, 'M_FBXMT_Trim not found — run Setup Scene first')
            return {'CANCELLED'}

        # Collect ordered ring positions from paired empties.
        # Each pair is (ring_i, ring_i+1) — take _1 from every pair plus the
        # final _2 to reconstruct the full ring sequence without duplicates.
        ring_positions = []
        for i, (e1, e2) in enumerate(pairs):
            ring_positions.append(Vector(e1.matrix_world.translation))
            if i == len(pairs) - 1:
                ring_positions.append(Vector(e2.matrix_world.translation))

        depth     = props.crv_depth
        thickness = props.crv_thickness
        pullback  = 0.25   # extend ends outward for boolean

        # Extend first and last ring outward along the curve axis
        if len(ring_positions) >= 2:
            t_start = (ring_positions[0]  - ring_positions[1]).normalized()
            t_end   = (ring_positions[-1] - ring_positions[-2]).normalized()
            ring_positions[0]  = ring_positions[0]  + t_start * pullback
            ring_positions[-1] = ring_positions[-1] + t_end   * pullback

        # Build in one shot with mitered joins
        curve_bm = bmesh.new()
        _build_curve_beam(curve_bm, ring_positions, depth, thickness, mat_index=0)

        curve_mesh = bpy.data.meshes.new('CurveBeam')
        curve_mesh.materials.append(trim_mat)
        curve_bm.to_mesh(curve_mesh)
        curve_bm.free()
        curve_mesh.update()

        curve_obj = bpy.data.objects.new('CurveBeam', curve_mesh)
        context.collection.objects.link(curve_obj)
        move_to_collection(curve_obj, COLLECTION_TRIM)

        # Boolean trim using source from first empty
        source_name = pairs[0][0].get('fbxmt_source', '') or                       pairs[0][1].get('fbxmt_source', '')
        source_obj  = bpy.data.objects.get(source_name) if source_name else None

        if source_obj and source_obj.type == 'MESH':
            mod = curve_obj.modifiers.new(name='FBXMT_BoolTrim', type='BOOLEAN')
            mod.operation = 'DIFFERENCE'
            mod.object    = source_obj
            mod.solver    = 'FLOAT'
            # Modifier left in stack for fine-tuning after generation

        # Remove empties
        for e1, e2 in pairs:
            for e in (e1, e2):
                try:
                    bpy.data.objects.remove(e, do_unlink=True)
                except Exception:
                    pass

        # Vert markers for OBJ export
        vert_markers = []
        for pos in ring_positions:
            me = bpy.data.meshes.new('crv_marker')
            bm = bmesh.new()
            bm.verts.new(pos)
            bm.to_mesh(me)
            bm.free()
            me.update()
            mo = bpy.data.objects.new('crv_marker', me)
            context.collection.objects.link(mo)
            vert_markers.append(mo)

        # OBJ export
        export_folder = props.export_path.strip() if props.export_path else ''
        if export_folder and os.path.isdir(export_folder):
            counter = 1
            while os.path.exists(
                    os.path.join(export_folder, f'beams_curve_{counter:03d}.obj')):
                counter += 1
            filepath = os.path.join(export_folder,
                                    f'beams_curve_{counter:03d}.obj')
            bpy.ops.object.select_all(action='DESELECT')
            curve_obj.select_set(True)
            for mo in vert_markers:
                mo.select_set(True)
            context.view_layer.objects.active = curve_obj
            bpy.ops.wm.obj_export(
                filepath=filepath,
                export_selected_objects=True,
                export_materials=False,
            )
            export_msg = f'exported {os.path.basename(filepath)}'
        elif export_folder:
            export_msg = f'export folder not found: {export_folder}'
        else:
            export_msg = 'set export folder in Project Setup to auto-export'

        for mo in vert_markers:
            bpy.data.objects.remove(mo, do_unlink=True)

        bpy.ops.object.select_all(action='DESELECT')
        self.report({'INFO'}, f'Curve beam generated — {export_msg}')
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Legacy Generate Beams (beam_NNN prefix) — kept for backwards compat

class OT_FBXMT_Generate_Beams(Operator):
    bl_idname      = 'fbxmt.generate_beams'
    bl_label       = 'Generate Beams (Legacy)'
    bl_description = 'Legacy — generates from beam_NNN_1/2 empties'
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        props = context.scene.fbxmt_props
        pairs = _get_empties_by_prefix('beam')
        if not pairs:
            self.report({'WARNING'}, 'No legacy beam_NNN_1/2 empties found.')
            return {'CANCELLED'}
        generated, export_msg = _generate_beams_from_pairs(
            context, pairs,
            depth=props.coving_depth,
            thickness=props.coving_thickness,
            export_stem='beams_legacy',
            merge_verts=False,
        )
        self.report({'INFO'},
                    f'{len(generated)} legacy beam(s) generated — {export_msg}')
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration

classes = (
    OT_FBXMT_Generate_Coving,
    OT_FBXMT_Generate_Parallel,
    OT_FBXMT_Generate_Spokes,
    OT_FBXMT_Generate_Curve,
    OT_FBXMT_Generate_Beams,
)
