# harry.py — FBX Mapper's Toolkit
#
# "Harry" — 1-arm trim run engine.
#
# Owns the coving sweep geometry (extracted from ceiling_deco.py) and adds
# flip controls so the same profile works on any edge regardless of winding.
#
# Public API
# ----------
#   build_cove_run(world_bm, selected_edges, face_normals_map,
#                  depth, thickness, chamfer,
#                  flip_depth, flip_thickness,
#                  overrun_start, overrun_end)
#       → caller-owned BMesh  (or raises on failure)
#
# The caller is responsible for:
#   - building world_bm + face_normals_map from edit-mode edge data
#   - handling Z-coplanarity checks (via chain_z_ok)
#   - creating the Blender object, assigning material, triggering the popup
#
# Nothing in this file touches bpy.data or bpy.ops — pure geometry.

from __future__ import annotations

import math
from mathutils import Vector
import bmesh

from .spline_utils import catmull_rom_resample

# ---------------------------------------------------------------------------
# Constants

Z_NOISE_EPS = 1e-3   # metres — coplanarity tolerance

# ---------------------------------------------------------------------------
# Low-level helpers  (ported verbatim from ceiling_deco.py)

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


def _plane_intersect(origin, dir_in, dir_out, dist):
    """Miter intersection of two planar offsets."""
    c = dir_in.dot(dir_out)
    denom = 1.0 - c * c
    if abs(denom) < 1e-6:
        return origin + dir_in * dist

    s = dist * (1.0 - c) / denom

    if s >= 0:
        result = origin + dir_in * s + dir_out * s
        if s <= dist * 3.0:
            return result

    bisector = dir_in + dir_out
    if bisector.length > 1e-6:
        return origin + bisector.normalized() * dist
    return origin + dir_in * dist


def sanitise_t_junctions(bm, edges, face_normals_map, snap=1e-3):
    """Resolve T-junctions by splitting host edges at hanging vert positions."""
    split_host_map = {}
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
                nv = bm.verts.new(proj)
                old_norm = face_normals_map.pop(id(e), None)
                bm.edges.remove(e)
                ea = bm.edges.new((va, nv))
                eb = bm.edges.new((nv, vb))
                split_host_map[id(ea)] = id(e)
                split_host_map[id(eb)] = id(e)
                if old_norm is not None:
                    face_normals_map[id(ea)] = old_norm
                    face_normals_map[id(eb)] = old_norm
                hv.co = proj.copy()
                changed = True
                break
    return [e for e in bm.edges if e.is_valid], face_normals_map, split_host_map


def chain_edges(selected_edges):
    """Build ordered edge chains from a list of BMEdges.

    Returns (chains, closed_flags) where each chain is a list of BMEdges
    and closed_flags[i] is True for closed loops.
    """
    edge_verts = {}
    adj        = {}
    vert_obj   = {}

    for e in selected_edges:
        v0, v1 = e.verts[0], e.verts[1]
        edge_verts[id(e)] = (v0, v1)
        for v in (v0, v1):
            vert_obj.setdefault(id(v), v)
            adj.setdefault(id(v), []).append(e)

    valence   = {vid: len(el) for vid, el in adj.items()}
    junctions = {vid for vid, c in valence.items() if c > 2}
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
                if not nexts:
                    break
                if len(nexts) == 1:
                    cur_e, cur_v_id = nexts[0], nxt_id
                else:
                    break
                continue
            nexts = [e for e in adj.get(nxt_id, []) if id(e) not in visited]
            cur_e, cur_v_id = (nexts[0], nxt_id) if nexts else (None, nxt_id)
        return chain

    for vid, v in vert_obj.items():
        if valence.get(vid, 0) == 1:
            for e in adj.get(vid, []):
                if id(e) not in visited:
                    ch = _walk(e, vid)
                    if ch:
                        chains.append(ch)
                        closed_fl.append(False)

    for vid in junctions:
        for e in adj.get(vid, []):
            if id(e) not in visited:
                ch = _walk(e, vid)
                if ch:
                    chains.append(ch)
                    closed_fl.append(False)

    for e in selected_edges:
        if id(e) not in visited:
            v0, v1 = edge_verts.get(id(e), (e.verts[0], e.verts[1]))
            ch = _walk(e, id(v0))
            if ch:
                chains.append(ch)
                closed_fl.append(True)

    return chains, closed_fl


def chain_verts(chain, is_closed):
    """Return ordered list of BMVerts for a chain."""
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


def chain_z_ok(chain, is_closed, eps=Z_NOISE_EPS, matrix_world=None):
    """Return (ok, z_ref, max_deviation) — checks in world-space Z."""
    verts = chain_verts(chain, is_closed)
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
# Catmull-Rom loop smoother

def _smooth_loops(bm, loop_a, loop_b, is_closed, smooth_angle_deg):
    """Resample curved sub-segments of two longitudinal vert loops.

    Only contiguous runs of verts where the local deviation angle exceeds
    smooth_angle_deg are resampled. Straight sections and corner verts are
    left exactly where they are.
    """
    threshold_rad = math.radians(smooth_angle_deg)

    def _local_angle(verts, i, n, closed):
        """Deviation angle at vert i — angle between incoming and outgoing edges."""
        if not closed and (i == 0 or i == n - 1):
            return 0.0
        a = verts[(i - 1) % n].co
        b = verts[i % n].co
        c = verts[(i + 1) % n].co
        ab = b - a
        bc = c - b
        if ab.length < 1e-8 or bc.length < 1e-8:
            return 0.0
        cos_a = max(-1.0, min(1.0, ab.normalized().dot(bc.normalized())))
        return math.acos(cos_a)

    def _resample_segment(verts_seg):
        """Resample a sub-segment in-place, pinning endpoints."""
        n = len(verts_seg)
        if n < 3:
            return
        pts       = [Vector(v.co) for v in verts_seg]
        resampled = catmull_rom_resample(pts, n - 1, closed=False)
        count     = min(len(resampled), n)
        for i in range(count):
            verts_seg[i].co = resampled[i]
        # Always pin endpoints
        verts_seg[0].co  = pts[0]
        verts_seg[-1].co = pts[-1]

    def _smooth_one_loop(verts, closed):
        n = len(verts)
        if n < 3:
            return

        # Compute local angle at every vert
        angles = [_local_angle(verts, i, n, closed) for i in range(n)]


        # Find contiguous runs where angle > threshold
        # A run starts when angle > threshold and ends when it drops back
        # Include one vert either side as anchors (pinned endpoints)
        in_run = [a < threshold_rad for a in angles]

        if not any(in_run):
            return

        # Walk the loop and collect sub-segments to resample
        if closed:
            # For closed loops, find start of first non-run section to anchor
            # Duplicate the loop to handle wrap-around
            indices = list(range(n)) + list(range(n))
            in_run2 = in_run + in_run
            i = 0
            segments_processed = set()
            while i < n:
                if in_run2[i] and i not in segments_processed:
                    # Walk back to find start of this run
                    start = i
                    while in_run2[(start - 1) % n]:
                        start = (start - 1) % n
                        if start == i:  # entire loop is curved
                            break
                    # Walk forward to find end
                    end = i
                    while in_run2[(end + 1) % n]:
                        end = (end + 1) % n
                        if end == i:  # entire loop is curved
                            break
                    # Collect segment including one anchor vert each side
                    seg_start = (start - 1) % n
                    seg_end   = (end   + 1) % n
                    # Build ordered index list
                    seg_indices = []
                    k = seg_start
                    while True:
                        seg_indices.append(k)
                        segments_processed.add(k)
                        if k == seg_end:
                            break
                        k = (k + 1) % n
                    if len(seg_indices) >= 3:
                        _resample_segment([verts[k] for k in seg_indices])
                    i = (end + 2) % n
                    if i in segments_processed and i == 0:
                        break
                else:
                    i += 1
        else:
            # Open loop — simple linear scan
            i = 0
            while i < n:
                if in_run[i]:
                    # Find extent of this run
                    j = i
                    while j < n and in_run[j]:
                        j += 1
                    # Include one anchor vert either side
                    seg_start = max(0, i - 1)
                    seg_end   = min(n - 1, j)
                    seg = [verts[k] for k in range(seg_start, seg_end + 1)]
                    if len(seg) >= 3:
                        _resample_segment(seg)
                    i = j + 1
                else:
                    i += 1

    for loop_verts in (loop_a, loop_b):
        valid = [v for v in loop_verts if v.is_valid]
        _smooth_one_loop(valid, is_closed)

    bm.normal_update()



# ---------------------------------------------------------------------------
# Core profile sweep

def build_cove_run(cov_bm, chain, is_closed,
                   depth, thickness,
                   chamfer='NONE',
                   mat_index=0,
                   face_normals=None,
                   flip_depth=False,
                   flip_thickness=False,
                   overrun_start=0.0,
                   overrun_end=0.0,
                   smooth_angle_deg=6.0):
    """Sweep 4-vert coving profile along one edge chain.

    flip_depth      — negate h_arm    (flips the depth/ceiling leg direction)
    flip_thickness  — negate wall_down (flips the thickness/wall leg direction)
    overrun_start   — extend run past chain start (open loops only, metres)
    overrun_end     — extend run past chain end   (open loops only, metres)
    """
    verts   = chain_verts(chain, is_closed)
    n       = len(verts)
    n_edges = n if is_closed else n - 1
    if n_edges < 1:
        return

    MIN_EDGE   = 0.01
    local_up   = Vector((0, 0, 1))
    local_down = -local_up

    tangents        = []
    edge_h_arms     = []
    edge_wall_downs = []

    for idx in range(n_edges):
        j = (idx + 1) % n
        t = verts[j].co - verts[idx].co
        if t.length < MIN_EDGE:
            t = tangents[-1] if tangents else Vector((1, 0, 0))
        t = t.normalized() if t.length > 1e-6 else Vector((1, 0, 0))
        tangents.append(t)

        t_h = t - local_up * local_up.dot(t)
        if t_h.length < 1e-6:
            t_h = t.copy()
        t_h = t_h.normalized()

        cand_a = local_up.cross(t_h).normalized()
        cand_b = -cand_a

        edge    = chain[idx]
        fn_list = (face_normals or {}).get(id(edge), [])
        if not fn_list and hasattr(edge, 'link_faces') and edge.link_faces:
            fn_list = [f.normal.normalized() for f in edge.link_faces]

        if fn_list:
            avg_n   = sum(fn_list, Vector()).normalized()
            avg_n_h = avg_n - local_up * local_up.dot(avg_n)
            if avg_n_h.length > 1e-6:
                avg_n_h = avg_n_h.normalized()
                h_arm   = cand_a if avg_n_h.dot(cand_a) >= 0 else cand_b
            else:
                h_arm = cand_a
        else:
            h_arm = cand_a

        if fn_list:
            wall_n = min(fn_list, key=lambda nn: abs(nn.dot(local_up)))
            wd     = local_down - wall_n * wall_n.dot(local_down)
            if wd.length < 1e-4:
                wd = local_down.copy()
            wd = wd.normalized()
            if wd.dot(local_up) > 0:
                wd = -wd
            wall_down = wd
        else:
            wall_down = local_down

        # ── Flip controls ────────────────────────────────────────────────────
        # flip_depth    negates h_arm    — mirrors the ceiling/depth leg
        # flip_thickness negates wall_down — mirrors the wall/thickness leg
        if flip_depth:
            h_arm = -h_arm
        if flip_thickness:
            wall_down = -wall_down

        edge_h_arms.append(h_arm)
        edge_wall_downs.append(wall_down)

    if not is_closed:
        tangents.append(tangents[-1])
        edge_h_arms.append(edge_h_arms[-1])
        edge_wall_downs.append(edge_wall_downs[-1])

    v2_run_edges = []   # (va, vb) pairs along the v2 inner-corner line

    def _face(vlist):
        try:
            f = cov_bm.faces.new(vlist)
            f.material_index = mat_index
        except Exception:
            pass

    def _edge_between(va, vb):
        for e in va.link_edges:
            if vb in e.verts:
                return e
        return None

    # ── Shared miter verts ────────────────────────────────────────────────────
    v1_pts       = []
    v3_pts       = []
    vert_ha      = []
    vert_wd      = []
    junction_verts = []

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

        v1_pts.append(Vector((v1r.x, v1r.y, seam_z)))
        v3_pts.append(v3r)
        vert_ha.append(ha if (is_start or is_end) else
                       ((ha_in + ha_out).normalized() if (ha_in + ha_out).length > 1e-6 else ha_out))
        vert_wd.append(wd if (is_start or is_end) else
                       ((wd_in + wd_out).normalized() if (wd_in + wd_out).length > 1e-6 else wd_out))

        if not (is_start or is_end):
            c_h = ha_in.dot(ha_out)
            if c_h < 0.5:
                prev_vi_d = (vi - 1) % n
                len_in  = (Vector(verts[vi].co) - Vector(verts[prev_vi_d].co)).length
                len_out = (Vector(verts[(vi+1)%n].co) - Vector(verts[vi].co)).length
                ratio = max(len_in, len_out) / max(min(len_in, len_out), 1e-6)
                if ratio > 3.0 and len_in < len_out:
                    junction_verts.append(vi)

    # ── Junction snap (closed chains only) ───────────────────────────────────
    if not is_closed:
        junction_verts = []
    for vi in junction_verts:
        prev_vi = (vi - 1) % n
        nxt_vi  = (vi + 1) % n
        len_in  = (Vector(verts[vi].co) - Vector(verts[prev_vi].co)).length
        len_out = (Vector(verts[nxt_vi].co) - Vector(verts[vi].co)).length
        if len_in > len_out:
            v1_pts[nxt_vi] = Vector(v1_pts[vi])

    # ── v2 inner corner ───────────────────────────────────────────────────────
    def _v2(vi):
        wd = vert_wd[vi]
        return v1_pts[vi] + wd * depth

    # ── Overrun: push start/end seam verts along their tangents ──────────────
    # Only for open chains; does not alter v1/v3 miters (start/end rings are
    # already flush, so we just displace the ring origin outward).
    sv_coords = [Vector(verts[vi].co) for vi in range(n)]
    if not is_closed:
        if overrun_start > 0.0 and len(tangents) > 0:
            sv_coords[0] = sv_coords[0] - tangents[0] * overrun_start
        if overrun_end > 0.0 and len(tangents) > 1:
            sv_coords[-1] = sv_coords[-1] + tangents[-2] * overrun_end

    # ── Build BMesh verts ─────────────────────────────────────────────────────
    sv_bm = [cov_bm.verts.new(sv_coords[vi]) for vi in range(n)]

    v1_bm = []
    _v1_pos_to_bv = {}
    for vi in range(n):
        p = v1_pts[vi]
        k = (round(p.x, 4), round(p.y, 4), round(p.z, 4))
        if k in _v1_pos_to_bv:
            v1_bm.append(_v1_pos_to_bv[k])
        else:
            bv = cov_bm.verts.new(p)
            _v1_pos_to_bv[k] = bv
            v1_bm.append(bv)

    v2_bm = []
    _v2_pos_to_bv = {}
    for vi in range(n):
        p = _v2(vi)
        k = (round(p.x, 4), round(p.y, 4), round(p.z, 4))
        if k in _v2_pos_to_bv:
            v2_bm.append(_v2_pos_to_bv[k])
        else:
            bv = cov_bm.verts.new(p)
            _v2_pos_to_bv[k] = bv
            v2_bm.append(bv)

    v3_bm = []
    _v3_pos_to_bv = {}
    for vi in range(n):
        p = v3_pts[vi]
        k = (round(p.x, 4), round(p.y, 4), round(p.z, 4))
        if k in _v3_pos_to_bv:
            v3_bm.append(_v3_pos_to_bv[k])
        else:
            bv = cov_bm.verts.new(p)
            _v3_pos_to_bv[k] = bv
            v3_bm.append(bv)

    loop = range(n) if is_closed else range(n - 1)
    for i in loop:
        j = (i + 1) % n
        v1_same = (v1_pts[i] - v1_pts[j]).length < 1e-4
        v3_same = (v3_pts[i] - v3_pts[j]).length < 1e-4
        sv_same = (sv_coords[i] - sv_coords[j]).length < 1e-4
        if v1_same and (v3_same or sv_same):
            continue
        _face([sv_bm[i],  v1_bm[i],  v1_bm[j],  sv_bm[j]])
        _face([v1_bm[i],  v2_bm[i],  v2_bm[j],  v1_bm[j]])
        _face([v2_bm[i],  v3_bm[i],  v3_bm[j],  v2_bm[j]])
        _face([v3_bm[i],  sv_bm[i],  sv_bm[j],  v3_bm[j]])
        if v2_bm[i] is not v2_bm[j]:
            v2_run_edges.append((v2_bm[i], v2_bm[j]))

    if not is_closed and n >= 2:
        _face([v2_bm[0],  v1_bm[0],  sv_bm[0]])
        _face([v3_bm[0],  v2_bm[0],  sv_bm[0]])
        _face([sv_bm[-1], v1_bm[-1], v2_bm[-1]])
        _face([sv_bm[-1], v2_bm[-1], v3_bm[-1]])

    cov_bm.normal_update()

    # ── Catmull-Rom loop smoothing ────────────────────────────────────────────
    # sv is pinned to selected edges, v3 hugs the wall surface via wall_down.
    # Neither is moved. v1 (ceiling/room-facing edge) and v2 (inner corner)
    # are the free loops that benefit from even Catmull-Rom spacing.
    _smooth_loops(cov_bm,
                  list(_v1_pos_to_bv.values()),
                  list(_v2_pos_to_bv.values()),
                  is_closed, smooth_angle_deg)

    return v2_run_edges   # list of (BMVert, BMVert) pairs
