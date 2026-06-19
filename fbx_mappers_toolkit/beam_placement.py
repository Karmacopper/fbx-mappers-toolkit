# beam_placement.py — FBX Mapper's Toolkit  [v0.29.4]
#
# Three independent beam placement operators:
#
#   Parallel  — par_NNN_1 / par_NNN_2
#   Spoke     — spk_NNN_1 / spk_NNN_2
#   Curve     — crv_NNN_1 / crv_NNN_2
#
# Arc measurement strategy:
#   Each face group's perimeter is walked as a continuous vertex chain along
#   its boundary edges (edges with only one face in the group).  The boundary
#   with two chains (inner + outer perimeter of the face strip) — the chain
#   facing the other group is selected by finding which chain's centroid is
#   closer to the other group's centroid.
#
#   Sampling walks the vert chain v0→vlast accumulating real vert-to-vert
#   distances.  Each sample finds the precise interpolated position on the
#   chain at (i+1)/(n+1) fraction, then snaps to the nearest face centre
#   so the empty lands flush on actual geometry.
#
# Group orientation:
#   Both chains are angle-sorted around their own centroid (XY) for
#   consistent sweep direction, then aligned to each other.

import sys
import math as _math
import bpy
import bmesh
from mathutils import Vector
from bpy.types import Operator

# ---------------------------------------------------------------------------
# Boundary edge chain extraction

def _split_components(selected_faces):
    """Split a list of BMFace into connected components.
    Returns list of lists, largest first.
    """
    unvisited  = {f.index: f for f in selected_faces}
    components = []
    while unvisited:
        seed  = next(iter(unvisited.values()))
        stack = [seed]
        comp  = []
        while stack:
            face = stack.pop()
            if face.index not in unvisited:
                continue
            del unvisited[face.index]
            comp.append(face)
            for edge in face.edges:
                for lf in edge.link_faces:
                    if lf.index in unvisited:
                        stack.append(lf)
        components.append(comp)
    components.sort(key=len, reverse=True)
    return components

def _boundary_edges(face_list):
    """Return edges that belong to exactly one face in face_list (perimeter)."""
    face_set   = {f.index for f in face_list}
    edge_count = {}
    for f in face_list:
        for e in f.edges:
            edge_count[e.index] = edge_count.get(e.index, 0) + 1
    boundary = []
    for f in face_list:
        for e in f.edges:
            if edge_count[e.index] == 1:
                boundary.append(e)
    # Deduplicate
    seen = set()
    result = []
    for e in boundary:
        if e.index not in seen:
            seen.add(e.index)
            result.append(e)
    return result

def _walk_edge_chain(edges, mat):
    """Walk a set of connected edges into an ordered vert chain.

    Returns list of world-space Vectors.  Handles open chains (arc perimeters).
    If edges form multiple disconnected chains, returns the longest.
    """
    if not edges:
        return []

    # Build adjacency: vert_index → list of (other_vert, edge)
    adj = {}
    for e in edges:
        a, b = e.verts[0].index, e.verts[1].index
        adj.setdefault(a, []).append((b, e))
        adj.setdefault(b, []).append((a, e))

    # Find endpoints (verts with degree 1 in this edge set) — start of open chain
    endpoints = [v for v, neighbours in adj.items() if len(neighbours) == 1]

    def _walk_from(start_idx):
        chain    = [start_idx]
        visited  = {start_idx}
        current  = start_idx
        while True:
            nexts = [nb for nb, _ in adj.get(current, []) if nb not in visited]
            if not nexts:
                break
            current = nexts[0]
            visited.add(current)
            chain.append(current)
        return chain

    # Build vert index → BMVert lookup from edges
    vert_map = {}
    for e in edges:
        for v in e.verts:
            vert_map[v.index] = v

    if endpoints:
        # Open chain — walk from one endpoint
        # Try both endpoints, return longer chain
        chains = [_walk_from(ep) for ep in endpoints[:2]]
        chain  = max(chains, key=len)
    else:
        # Closed loop — walk from arbitrary start
        chain = _walk_from(next(iter(adj)))

    return [mat @ vert_map[vi].co for vi in chain if vi in vert_map]

def _chain_centroid(chain):
    """XY centroid of a vert chain."""
    cx = sum(v.x for v in chain) / len(chain)
    cy = sum(v.y for v in chain) / len(chain)
    return cx, cy

def _split_boundary_chains(edges, mat):
    """Split boundary edges into separate chains (inner/outer perimeter).

    Returns list of world-space vert chains, sorted longest first.
    """
    if not edges:
        return []

    # Find connected sub-graphs of edges
    edge_adj = {}
    for e in edges:
        a, b = e.verts[0].index, e.verts[1].index
        edge_adj.setdefault(a, set()).add(e.index)
        edge_adj.setdefault(b, set()).add(e.index)

    edge_by_idx = {e.index: e for e in edges}
    visited_edges = set()
    sub_groups    = []

    for e in edges:
        if e.index in visited_edges:
            continue
        # BFS over edges
        group   = []
        eq      = [e]
        while eq:
            ce = eq.pop()
            if ce.index in visited_edges:
                continue
            visited_edges.add(ce.index)
            group.append(ce)
            for v in ce.verts:
                for nei_idx in edge_adj.get(v.index, []):
                    if nei_idx not in visited_edges:
                        eq.append(edge_by_idx[nei_idx])
        sub_groups.append(group)

    chains = [_walk_edge_chain(g, mat) for g in sub_groups]
    chains.sort(key=len, reverse=True)
    return chains

def _get_face_groups_as_chains(obj):
    """Return two boundary vert chains — one per face group — in world space.

    For each face group, extracts the boundary edge chains (inner + outer
    perimeter of the face strip) and selects the chain that faces the other
    group.  Returns (chain_large, chain_small), larger group first.

    All BMesh access completed before bm.free().
    Returns ([], []) if fewer than two components found.
    """
    mat = obj.matrix_world
    bm  = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.verts.ensure_lookup_table()

    selected = [f for f in bm.faces if f.select]
    if not selected:
        bm.free()
        return [], []

    components = _split_components(selected)
    if len(components) < 2:
        bm.free()
        return [], []

    # Extract boundary chains for the two largest components
    results = []
    centroids = []
    for comp in components[:2]:
        boundary = _boundary_edges(comp)
        chains   = _split_boundary_chains(boundary, mat)
        # Compute centroid of this component's faces for chain selection
        fc = [mat @ f.calc_center_median() for f in comp]
        cx = sum(v.x for v in fc) / len(fc)
        cy = sum(v.y for v in fc) / len(fc)
        results.append(chains)
        centroids.append((cx, cy))

    bm.free()

    def _pick_chain(chains, other_cx, other_cy):
        """Pick the chain whose centroid is closest to the other group."""
        if not chains:
            return []
        if len(chains) == 1:
            return chains[0]
        best     = None
        best_d   = float('inf')
        for ch in chains:
            if not ch:
                continue
            cx, cy = _chain_centroid(ch)
            d = _math.hypot(cx - other_cx, cy - other_cy)
            if d < best_d:
                best_d = d
                best   = ch
        return best or chains[0]

    cx_a, cy_a = centroids[0]
    cx_b, cy_b = centroids[1]

    chain_a = _pick_chain(results[0], cx_b, cy_b)
    chain_b = _pick_chain(results[1], cx_a, cy_a)

    # Angle-sort each chain around its own centroid for consistent sweep
    chain_a = _angle_sort_chain(chain_a)
    chain_b = _angle_sort_chain(chain_b)

    # Align sweep directions
    chain_a, chain_b = _align_directions(chain_a, chain_b)

    return chain_a, chain_b

# ---------------------------------------------------------------------------
# Chain ordering helpers

def _flatten_chain_z(chain):
    """Deduplicate and flatten a boundary vert chain to the ceiling plane.

    Face-strip boundary chains contain paired verts at the same XY but
    different Z (top and bottom edges of the face strip, e.g. Z=26/Z=28).
    This causes doubled arc-length measurements and incorrect interpolated Z.

    Fix:
      1. Group verts by XY position (within 1e-4 tolerance).
      2. Replace each group with a single vert at mean XY and mean Z.
      3. Re-sort the deduplicated verts by angle around their centroid.

    Result: one vert per unique arc position, at the correct midplane Z.
    """
    if not chain:
        return chain

    # Group by XY proximity
    XY_TOL  = 1e-3
    groups  = []   # list of lists of Vectors
    for v in chain:
        placed = False
        for g in groups:
            ref = g[0]
            if abs(v.x - ref.x) < XY_TOL and abs(v.y - ref.y) < XY_TOL:
                g.append(v)
                placed = True
                break
        if not placed:
            groups.append([v])

    # Z midpoint — midpoint between the northmost and southmost edge of the
    # face strip.  Correct for any uniform extrusion regardless of vert count.
    from mathutils import Vector
    all_z = [v.z for v in chain]
    z_mid = (max(all_z) + min(all_z)) / 2.0

    deduped = []
    for g in groups:
        mx = sum(v.x for v in g) / len(g)
        my = sum(v.y for v in g) / len(g)
        deduped.append(Vector((mx, my, z_mid)))

    return deduped

def _angle_sort_chain(chain):
    """Sort a vert chain by angle around its own XY centroid."""
    if len(chain) < 2:
        return chain
    cx, cy = _chain_centroid(chain)
    return sorted(chain, key=lambda v: _math.atan2(v.y - cy, v.x - cx))

def _align_directions(chain_a, chain_b):
    """Ensure both chains sweep in the same rotational direction."""
    if len(chain_a) < 2 or len(chain_b) < 2:
        return chain_a, chain_b
    da = chain_a[-1] - chain_a[0]; da.z = 0.0
    db = chain_b[-1] - chain_b[0]; db.z = 0.0
    if da.dot(db) < 0.0:
        chain_b = list(reversed(chain_b))
    return chain_a, chain_b

# ---------------------------------------------------------------------------
# Arc sampling

def _build_cum(chain):
    """Build cumulative vert-to-vert distance table."""
    cum = [0.0]
    for i in range(len(chain) - 1):
        cum.append(cum[-1] + (chain[i+1] - chain[i]).length)
    return cum

def _pos_on_chain(chain, cum, t):
    """Exact interpolated world position at arc distance t along chain."""
    if t <= 0.0:
        return chain[0].copy()
    if t >= cum[-1]:
        return chain[-1].copy()
    for i in range(len(cum) - 1):
        if cum[i] <= t <= cum[i+1]:
            seg = cum[i+1] - cum[i]
            f   = (t - cum[i]) / seg if seg > 1e-8 else 0.0
            return chain[i].lerp(chain[i+1], f)
    return chain[-1].copy()

def _snap_to_nearest_face_centre(pos, face_centres):
    """Return the face centre closest to pos."""
    best   = None
    best_d = float('inf')
    for fc in face_centres:
        d = (fc - pos).length
        if d < best_d:
            best_d = d
            best   = fc
    return best if best is not None else pos

def _sample_chain(chain, face_centres, n):
    """Sample n positions along chain at equal intervals with margins.

    Finds the precise interpolated position on the vert chain at each
    (i+1)/(n+1) fraction of the total arc length.  Position is exact —
    no face-centre snapping.  face_centres is accepted but unused (kept
    for call-site compatibility).

    Returns list of n Vectors.
    """
    if not chain or n <= 0:
        return []
    if len(chain) == 1:
        return [chain[0].copy() for _ in range(n)]

    cum   = _build_cum(chain)
    total = cum[-1]
    if total < 1e-8:
        return [chain[0].copy() for _ in range(n)]

    return [_pos_on_chain(chain, cum, (i + 1) / (n + 1) * total)
            for i in range(n)]

# ---------------------------------------------------------------------------
# Face centre extraction (for snap targets)

def _get_face_centres_for_component(comp_faces, mat):
    """Return world-space face centres for a component's faces."""
    return [mat @ f.calc_center_median() for f in comp_faces]

# ---------------------------------------------------------------------------
# Full extraction — chains + face centres

def _get_groups(obj):
    """Return ((chain_a, faces_a), (chain_b, faces_b)) for the two largest
    selected face components.  chain_* are angle-sorted, direction-aligned
    boundary vert chains.  faces_* are world-space face centre lists for
    snapping.  Larger group is index 0.

    All BMesh access before bm.free().  Returns (None, None) on failure.
    """
    mat = obj.matrix_world
    bm  = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.verts.ensure_lookup_table()

    selected = [f for f in bm.faces if f.select]
    if not selected:
        bm.free()
        return None, None

    components = _split_components(selected)
    if len(components) < 2:
        bm.free()
        return None, None

    comp_a = components[0]
    comp_b = components[1]

    # Face centres for snapping
    face_centres_a = [mat @ f.calc_center_median() for f in comp_a]
    face_centres_b = [mat @ f.calc_center_median() for f in comp_b]

    # Component centroids for chain selection
    def _comp_centroid(fc_list):
        return (sum(v.x for v in fc_list) / len(fc_list),
                sum(v.y for v in fc_list) / len(fc_list))

    cx_a, cy_a = _comp_centroid(face_centres_a)
    cx_b, cy_b = _comp_centroid(face_centres_b)

    # Boundary chains
    def _best_chain(comp, other_cx, other_cy):
        boundary = _boundary_edges(comp)
        chains   = _split_boundary_chains(boundary, mat)
        if not chains:
            return []
        if len(chains) == 1:
            return chains[0]
        best_d = float('inf')
        best   = chains[0]
        for ch in chains:
            if not ch:
                continue
            ccx, ccy = _chain_centroid(ch)
            d = _math.hypot(ccx - other_cx, ccy - other_cy)
            if d < best_d:
                best_d = d
                best   = ch
        return best

    chain_a = _best_chain(comp_a, cx_b, cy_b)
    chain_b = _best_chain(comp_b, cx_a, cy_a)

    bm.free()

    chain_a = _flatten_chain_z(_angle_sort_chain(chain_a))
    chain_b = _flatten_chain_z(_angle_sort_chain(chain_b))
    chain_a, chain_b = _align_directions(chain_a, chain_b)

    return (chain_a, face_centres_a), (chain_b, face_centres_b)

# ---------------------------------------------------------------------------
# Empty placement helpers

def _next_index(prefix):
    """Return next unused NNN for prefix_NNN_1/2 naming."""
    idx      = 1
    existing = {o.name for o in bpy.data.objects if o.type == 'EMPTY'}
    while (f'{prefix}_{idx:03d}_1' in existing or
           f'{prefix}_{idx:03d}_2' in existing):
        idx += 1
    return idx

def _place_empty(name, location, collection, source_name=''):
    """Create a SPHERE empty at location linked to collection.
    source_name is stored as a custom property for boolean target lookup
    at Generate time.
    """
    e = bpy.data.objects.new(name, None)
    e.empty_display_type  = 'SPHERE'
    e.empty_display_size  = 0.1
    e.location            = location.copy()
    e.show_name           = True
    e.color               = (1.0, 0.0, 0.0, 1.0)
    if source_name:
        e['fbxmt_source'] = source_name
    collection.objects.link(e)
    return e

def _label_empty(empty, index):
    empty['fbxmt_idx'] = index

def _replace_parallel_empties(context):
    """Re-place par_NNN_1 empties using stored chain data and current props.

    Called by prop update callbacks — reads chain/normals from the first
    existing par_NNN_1 empty, clears all par empties, re-samples with
    current settings and places fresh empties.
    """
    import json as _json

    # Find first par_NNN_1 empty with stored chain data
    anchors = sorted(
        [o for o in bpy.data.objects
         if o.type == 'EMPTY' and o.name.startswith('par_')
         and o.name.rsplit('_', 1)[-1] == '1'],
        key=lambda o: o.name
    )
    if not anchors:
        return

    seed = anchors[0]
    chain_json  = seed.get('fbxmt_chain', None)
    faces_json  = seed.get('fbxmt_face_centres', None)
    normals_json= seed.get('fbxmt_face_normals', None)
    source_name = seed.get('fbxmt_source', '')

    if not chain_json or not faces_json or not normals_json:
        return   # old empties without stored data — can't auto-replace

    chain        = [Vector(v) for v in _json.loads(chain_json)]
    face_centres = [Vector(v) for v in _json.loads(faces_json)]
    face_normals = [Vector(v) for v in _json.loads(normals_json)]

    if not chain:
        return

    props   = context.scene.fbxmt_props
    offset_v = props.par_offset_v

    cum_c   = _build_cum(chain)
    total_c = cum_c[-1]
    inset_s = props.par_inset_start
    inset_e = props.par_inset_end
    t_start = max(0.0,  inset_s / total_c) if total_c > 1e-8 else 0.0
    t_end   = min(1.0, 1.0 - inset_e / total_c) if total_c > 1e-8 else 1.0
    t_start = max(0.0, min(t_start, 0.99))
    t_end   = max(t_start + 0.01, min(t_end, 1.0))

    usable = (t_end - t_start) * total_c
    if props.par_spacing > 0.0:
        n = max(1, round(usable / props.par_spacing))
    else:
        n = max(1, props.par_count)

    if n == 1:
        pos_list = [_pos_on_chain(chain, cum_c,
                                  (t_start + t_end) * 0.5 * total_c)]
    else:
        pos_list = [_pos_on_chain(chain, cum_c,
                                  (t_start + i / (n - 1) * (t_end - t_start))
                                  * total_c)
                    for i in range(n)]

    # Remove all existing par empties
    to_remove = [o for o in bpy.data.objects
                 if o.type == 'EMPTY' and o.name.startswith('par_')
                 and o.name.rsplit('_', 1)[-1] in ('1', '2')]
    for o in to_remove:
        bpy.data.objects.remove(o, do_unlink=True)
    try:
        from .par_ray_preview import invalidate_par_cache
        invalidate_par_cache()
    except Exception:
        pass

    # Re-place
    v_shift = Vector((0.0, 0.0, offset_v))
    coll    = context.collection if context.collection else bpy.context.collection
    start   = _next_index('par')

    for i in range(n):
        pos    = pos_list[i] + v_shift
        normal = _nearest_face_normal(pos_list[i], face_centres, face_normals)
        ea     = _place_empty(f'par_{start + i:03d}_1', pos, coll, source_name)
        ea['fbxmt_normal']       = (normal.x, normal.y, normal.z)
        ea['fbxmt_chain']        = _json.dumps([[v.x, v.y, v.z] for v in chain])
        ea['fbxmt_face_centres'] = _json.dumps([[v.x, v.y, v.z] for v in face_centres])
        ea['fbxmt_face_normals'] = _json.dumps([[v.x, v.y, v.z] for v in face_normals])

def _par_update_cb(self, context):
    """Prop update callback — defer auto re-place via timer to avoid
    calling bpy.data.objects.remove during a prop update evaluation."""
    import bpy as _bpy

    def _deferred():
        _replace_parallel_empties(_bpy.context)
        return None   # don't repeat

    _bpy.app.timers.register(_deferred, first_interval=0.0)

# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
# Operator: Place Parallel Beams

class OT_FBXMT_Place_Parallel(Operator):
    bl_idname      = 'fbxmt.place_parallel'
    bl_label       = 'Place Parallel Beams'
    bl_description = ('Select one face strip. Places par_NNN_1 empties along '
                      'it, storing the face normal as the ray direction. '
                      'par_NNN_2 empties are derived at Generate time by '
                      'smart ray-cast along the stored normal.')
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode == 'EDIT_MESH'
                and obj is not None and obj.type == 'MESH')

    def execute(self, context):
        props    = context.scene.fbxmt_props
        offset_v = props.par_offset_v
        debug    = props.beam_debug
        obj      = context.active_object

        bpy.ops.object.mode_set(mode='OBJECT')
        chain, face_centres, face_normals = _get_single_group(obj)
        bpy.ops.object.mode_set(mode='EDIT')

        if not chain:
            self.report({'WARNING'}, 'No selected faces found.')
            return {'CANCELLED'}

        # Apply independent start/end inset along the chain arc
        cum_c   = _build_cum(chain)
        total_c = cum_c[-1]
        inset_s = props.par_inset_start
        inset_e = props.par_inset_end
        t_start = max(0.0,  inset_s / total_c) if total_c > 1e-8 else 0.0
        t_end   = min(1.0, 1.0 - inset_e / total_c) if total_c > 1e-8 else 1.0
        t_start = max(0.0, min(t_start, 0.99))
        t_end   = max(t_start + 0.01, min(t_end, 1.0))

        # Count vs spacing — mutually exclusive; spacing > 0 overrides count
        usable = (t_end - t_start) * total_c
        if props.par_spacing > 0.0:
            n = max(1, round(usable / props.par_spacing))
        else:
            n = max(1, props.par_count)

        if n == 1:
            pos_list = [_pos_on_chain(chain, cum_c,
                                      (t_start + t_end) * 0.5 * total_c)]
        else:
            pos_list = [_pos_on_chain(chain, cum_c,
                                      (t_start + i / (n - 1) * (t_end - t_start))
                                      * total_c)
                        for i in range(n)]
        v_shift  = Vector((0.0, 0.0, offset_v))
        coll     = context.collection
        start    = _next_index('par')
        source_name = obj.name

        import json as _json
        chain_json   = _json.dumps([[v.x, v.y, v.z] for v in chain])
        centres_json = _json.dumps([[v.x, v.y, v.z] for v in face_centres])
        normals_json = _json.dumps([[v.x, v.y, v.z] for v in face_normals])

        for i in range(n):
            gidx   = start + i
            pos    = pos_list[i] + v_shift
            normal = _nearest_face_normal(pos_list[i], face_centres, face_normals)
            ea     = _place_empty(f'par_{gidx:03d}_1', pos, coll, source_name)
            # Store data for ray-cast and auto-replace
            ea['fbxmt_normal']       = (normal.x, normal.y, normal.z)
            ea['fbxmt_chain']        = chain_json
            ea['fbxmt_face_centres'] = centres_json
            ea['fbxmt_face_normals'] = normals_json
        try:
            from .par_ray_preview import invalidate_par_cache
            invalidate_par_cache()
        except Exception:
            pass

        self.report({'INFO'}, f'{n} parallel anchor(s) placed — '
                              f'normals stored, Generate will ray-cast _2 positions')
        return {'FINISHED'}

class OT_FBXMT_Clear_Parallel(Operator):
    bl_idname  = 'fbxmt.clear_parallel'
    bl_label   = 'Clear Parallel Empties'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        to_remove = [o for o in bpy.data.objects
                     if o.type == 'EMPTY' and o.name.startswith('par_')
                     and o.name.rsplit('_', 1)[-1] in ('1', '2')]
        for o in to_remove:
            bpy.data.objects.remove(o, do_unlink=True)
        try:
            from .par_ray_preview import invalidate_par_cache
            invalidate_par_cache()
        except Exception:
            pass
        self.report({'INFO'}, f'{len(to_remove)} parallel empty/empties removed')
        return {'FINISHED'}

# ---------------------------------------------------------------------------
# Operator: Place Spoke Beams

class OT_FBXMT_Place_Spokes(Operator):
    bl_idname      = 'fbxmt.place_spokes'
    bl_label       = 'Place + Generate Spoke Beams'
    bl_description = ('Select two disconnected face groups (hub + rim). '
                      'Silently flips to Object Mode for BMesh read and generate, '
                      'then returns to Edit Mode. One-shot.')
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode == 'EDIT_MESH'
                and obj is not None and obj.type == 'MESH')

    def execute(self, context):
        props    = context.scene.fbxmt_props
        n        = max(1, props.spk_count)
        offset_v = props.spk_offset_v
        length   = props.spk_length
        both     = props.spk_both_ends
        debug    = props.beam_debug
        obj      = context.active_object

        # ── Read BMesh ────────────────────────────────────────────────────
        bpy.ops.object.mode_set(mode='OBJECT')
        group_rim, group_hub = _get_groups(obj)

        if group_rim is None or group_hub is None:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'WARNING'},
                'Select faces in exactly two disconnected groups.')
            return {'CANCELLED'}

        chain_hub, faces_hub = group_hub
        chain_rim, faces_rim = group_rim

        hub_pos = _sample_chain(chain_hub, faces_hub, n)
        rim_pos = _sample_chain(chain_rim, faces_rim, n)
        v_shift     = Vector((0.0, 0.0, offset_v))
        coll        = context.collection
        source_name = obj.name
        raw_pairs   = []  # list of (p1, p2) Vector positions
        source_name = obj.name

        start = _next_index('spk')

        for i in range(n):
            h    = hub_pos[i]
            r    = rim_pos[i]
            gidx = start + i

            if length > 0.0:
                axis = r - h
                dist = axis.length
                t    = axis / dist if dist > 1e-6 else Vector((0, 1, 0))
                p1   = h + t * length
                p2   = r - t * length if both else h + t * length
            else:
                p1 = h
                p2 = r

            raw_pairs.append((p1 + v_shift, p2 + v_shift))

        # ── Build beams directly from sampled positions ──────────────────
        from .ceiling_deco import (ensure_fbxmt_materials, _build_beam,
                                   move_to_collection, COLLECTION_TRIM)
        import os, sys as _sys
        ensure_fbxmt_materials()
        trim_mat = bpy.data.materials.get('M_FBXMT_Trim')
        if trim_mat is None:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'ERROR'}, 'M_FBXMT_Trim not found — run Setup Scene first')
            return {'CANCELLED'}

        pullback  = 0.25
        depth     = props.coving_depth
        thickness = props.coving_thickness
        generated = []
        vert_markers = []

        for i, (p1, p2) in enumerate(raw_pairs):
            gidx  = start + i
            gname = f'spk_{gidx:03d}'

            axis   = p2 - p1
            length = axis.length
            if length < 1e-4:
                        continue
            t_dir    = axis / length
            start_co = p1 - t_dir * pullback
            end_co   = p2 + t_dir * pullback

                    
            beam_bm = bmesh.new()
            _build_beam(beam_bm, start_co, end_co, depth, thickness, 0.5, 0.5)
            beam_bm.verts.ensure_lookup_table()
    
            beam_mesh = bpy.data.meshes.new(f'{gname}_Beam')
            beam_mesh.materials.append(trim_mat)
            beam_bm.to_mesh(beam_mesh)
            beam_bm.free()
            beam_mesh.update()

            beam_obj = bpy.data.objects.new(f'{gname}_Beam', beam_mesh)
            context.scene.collection.objects.link(beam_obj)
            generated.append(beam_obj)

            if source_name:
                src_obj = bpy.data.objects.get(source_name)
                if src_obj and src_obj.type == 'MESH':
                    mod           = beam_obj.modifiers.new(name='FBXMT_BoolTrim', type='BOOLEAN')
                    mod.operation = 'DIFFERENCE'
                    mod.object    = src_obj
                    mod.solver    = 'FLOAT'

            # Vert markers
            for co, mname in ((p1, f'{gname}_1_marker'), (p2, f'{gname}_2_marker')):
                me = bpy.data.meshes.new(mname)
                bm2 = bmesh.new(); bm2.verts.new(co); bm2.to_mesh(me); bm2.free(); me.update()
                mo = bpy.data.objects.new(mname, me)
                context.scene.collection.objects.link(mo)
                vert_markers.append(mo)

        # ── OBJ export ────────────────────────────────────────────────────
        export_msg = ''
        export_folder = props.export_path.strip() if props.export_path else ''
        if export_folder and os.path.isdir(export_folder):
            counter = 1
            while os.path.exists(os.path.join(export_folder, f'beams_spoke_{counter:03d}.obj')):
                counter += 1
            filepath = os.path.join(export_folder, f'beams_spoke_{counter:03d}.obj')
            bpy.ops.object.select_all(action='DESELECT')
            for o in generated + vert_markers:
                o.select_set(True)
            if generated:
                context.view_layer.objects.active = generated[0]
            bpy.ops.wm.obj_export(filepath=filepath, export_selected_objects=True,
                                   export_materials=False)
            export_msg = f'exported {os.path.basename(filepath)}'

        # ── Move to Trim collection ────────────────────────────────────────
        for o in generated:
            move_to_collection(o, COLLECTION_TRIM)
        for o in vert_markers:
            bpy.data.objects.remove(o, do_unlink=True)

        bpy.ops.object.mode_set(mode='OBJECT')

        msg = f'{len(generated)} spoke beam(s) generated'
        if export_msg:
            msg += f' — {export_msg}'
        self.report({'INFO'}, msg)
        return {'FINISHED'}

class OT_FBXMT_Clear_Spokes(Operator):
    bl_idname  = 'fbxmt.clear_spokes'
    bl_label   = 'Clear Spoke Empties'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        to_remove = [o for o in bpy.data.objects
                     if o.type == 'EMPTY' and o.name.startswith('spk_')
                     and o.name.rsplit('_', 1)[-1] in ('1', '2')]
        for o in to_remove:
            bpy.data.objects.remove(o, do_unlink=True)
        self.report({'INFO'}, f'{len(to_remove)} spoke empty/empties removed')
        return {'FINISHED'}

# ---------------------------------------------------------------------------
# Operator: Place Curve Beams

class OT_FBXMT_Place_Curve(Operator):
    bl_idname      = 'fbxmt.place_curve'
    bl_label       = 'Place Curve Beam'
    bl_description = (
        'Three selection modes (auto-detected):\n'
        '  STRIP   — two long boundary-chain strips; beam arcs along midline.\n'
        '  PAIR    — two equal-count face groups; each A face pairs with nearest\n'
        '            B face; ring = midpoint. Best for U-shapes with matching\n'
        '            inner/outer faces.\n'
        '  RADIUS  — strip + 1 face; single face is an anchor point only.\n'
        'Empties placed for inspection. Then Generate Curve to build beam.'
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode == 'EDIT_MESH'
                and obj is not None and obj.type == 'MESH')

    def execute(self, context):
        from mathutils import Vector
        props       = context.scene.fbxmt_props
        offset_v    = props.crv_offset_v
        debug       = props.beam_debug
        obj         = context.active_object
        source_name = obj.name
        mat         = obj.matrix_world

        # ── Read BMesh + detect mode ────────────────────────────────────
        bpy.ops.object.mode_set(mode='OBJECT')

        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        sel_faces = [f for f in bm.faces if f.select]
        comps     = _split_components(sel_faces)

        if len(comps) < 2:
            bm.free()
            self.report({'WARNING'},
                f'Select at least 2 disconnected face groups ({len(comps)} found).')
            return {'CANCELLED'}

        comps.sort(key=len, reverse=True)

        # Split: smallest comp(s) → group B, rest → group A
        sizes    = [len(c) for c in comps]
        min_size = min(sizes)
        max_size = max(sizes)

        if min_size == max_size:
            # All same size — equal split
            half        = len(comps) // 2
            comp_a_list = comps[:half]
            comp_b_list = comps[half:]
        else:
            comp_b_list = [c for c in comps if len(c) == min_size]
            comp_a_list = [c for c in comps if len(c) != min_size]

        comp_a = [f for c in comp_a_list for f in c]
        comp_b = [f for c in comp_b_list for f in c]
        na, nb = len(comp_a), len(comp_b)

        # Mode detection:
        # PAIR   — 2 components, equal small face count (≤5 each) = individual face selections
        # RADIUS — smallest component is exactly 1 face
        # STRIP  — large multi-face boundary-chain strips
        n_comps  = len(comps)
        max_comp = max(len(c) for c in comps)

        if nb == 1 and na > 1:
            mode = 'RADIUS'
        elif n_comps == 2 and na == nb and max_comp <= 5:
            mode = 'PAIR'
        else:
            mode = 'STRIP'


        # ── Extract geometry per mode ───────────────────────────────────
        if mode == 'PAIR':
            # Face centroids sorted by arc order, paired nearest-neighbour.
            # Sort A centroids using the inner boundary chain as arc reference.
            # This gives the correct ring count (one per face) and correct order.
            cents_a = [mat @ f.calc_center_median() for f in comp_a]
            cents_b = [mat @ f.calc_center_median() for f in comp_b]

            # Get arc-ordered boundary chain for comp_a to sort its centroids
            boundary_a = _boundary_edges(comp_a)
            chains_a   = _split_boundary_chains(boundary_a, mat)
            # Pick the shortest chain (inner boundary, closest to comp_b)
            ref_chain  = min(chains_a, key=lambda c: len(c)) if chains_a else []
            bm.free()

            # Sort A centroids by nearest-neighbour chain traversal —
            # start from the centroid furthest from the group centre,
            # then greedily pick the nearest unvisited centroid.
            # This gives correct arc order for U/L shapes without assuming
            # circular topology.
            if len(cents_a) > 1:
                gc = Vector(sum(cents_a, Vector()) / len(cents_a))
                start = max(range(len(cents_a)),
                            key=lambda i: (cents_a[i] - gc).length)
                ordered = [cents_a[start]]
                remaining = [c for j, c in enumerate(cents_a) if j != start]
                while remaining:
                    last = ordered[-1]
                    nearest = min(remaining, key=lambda c: (c - last).length)
                    ordered.append(nearest)
                    remaining.remove(nearest)
                cents_a = ordered

            # Pair each sorted A centroid with nearest unused B centroid
            used_b = set()
            pairs  = []
            for ca in cents_a:
                candidates = [(i, (cents_b[i] - ca).length)
                              for i in range(len(cents_b)) if i not in used_b]
                if not candidates:
                    break
                best_i = min(candidates, key=lambda x: x[1])[0]
                used_b.add(best_i)
                pairs.append((ca, cents_b[best_i]))

            # 4-point ring sequence matching expected geometry:
            # end_0 → corner_0 → corner_1 → end_1
            # Formula (verified against expected OBJ):
            #   end_x    = 2*arm_A.x - centre_A.x  (reflect arm centroid past its boundary)
            #   corner   = (centre_mid.x, arm_mid.y, z)
            #   end      = (end_x,        arm_mid.y, z)
            mids = [(a + b) * 0.5 for a, b in pairs]
            ring_positions = []

            if len(pairs) < 2:
                ring_positions = list(mids)
            else:
                # Find centre pair: the one whose A centroid is furthest from
                # the mean A centroid in any direction — the "turning" face.
                from mathutils import Vector as _V
                centre_idx = max(range(len(pairs)),
                                 key=lambda i: (pairs[i][1] - pairs[i][0]).length)
                centre_mid = mids[centre_idx]
                centre_a   = pairs[centre_idx][0]
                arm_indices = [i for i in range(len(pairs)) if i != centre_idx]

                end_offset = props.crv_end_offset

                # Offset: only corners move (in X, along arm axis).
                # Ends are fully fixed. Corners slide toward/away from centre,
                # changing the width of the mid section.
                if abs(end_offset) > 1e-6:
                    arm0_a    = pairs[arm_indices[0]][0]
                    to_centre = centre_a - arm0_a
                    if abs(to_centre.x) >= abs(to_centre.y):
                        arm_ax = _V((1 if to_centre.x > 0 else -1, 0, 0))
                    else:
                        arm_ax = _V((0, 1 if to_centre.y > 0 else -1, 0))
                    corner_shift = arm_ax * end_offset
                else:
                    corner_shift = _V((0, 0, 0))

                for j, ai in enumerate(arm_indices):
                    arm_a   = pairs[ai][0]
                    arm_mid = mids[ai]
                    # End: fully fixed, no offset
                    end_x = arm_a.x - (centre_a.x - arm_a.x)
                    end   = _V((end_x, arm_mid.y, arm_mid.z))
                    # Corner: slides along arm axis only
                    corner = _V((centre_mid.x, arm_mid.y, arm_mid.z)) + corner_shift

                    if j == 0:
                        ring_positions.append(end)
                        ring_positions.append(corner)
                    else:
                        ring_positions.append(corner)
                        ring_positions.append(end)

        elif mode == 'RADIUS':
            # comp_b is 1 face — its centroid is a reference anchor only
            radius_target = mat @ comp_b[0].calc_center_median()
            # comp_a boundary chain drives ring positions
            boundary = _boundary_edges(comp_a)
            chains   = _split_boundary_chains(boundary, mat)
            chain_a  = _flatten_chain_z(_angle_sort_chain(chains[0] if chains else []))
            bm.free()

            # Segment midpoints
            ring_positions = []
            for j in range(len(chain_a) - 1):
                ring_positions.append((chain_a[j] + chain_a[j + 1]) * 0.5)
            ring_positions = [chain_a[0]] + ring_positions + [chain_a[-1]]

        else:  # STRIP
            bm.free()
            group_a, group_b = _get_groups(obj)
            if group_a is None or group_b is None:
                self.report({'WARNING'},
                    'Select faces in exactly two disconnected groups.')
                return {'CANCELLED'}
            chain_a, faces_a = group_a
            chain_b, faces_b = group_b

            cum_a   = _build_cum(chain_a)
            cum_b   = _build_cum(chain_b)
            total_a = cum_a[-1]
            total_b = cum_b[-1]
            inset_s = props.crv_inset_start
            inset_e = props.crv_inset_end
            t_start = max(0.0, min((inset_s / total_a) if total_a > 1e-8 else 0.0, 0.99))
            t_end   = max(t_start + 0.01,
                          min(1.0 - (inset_e / total_a) if total_a > 1e-8 else 1.0, 1.0))
            n_rings = min(len(chain_a), len(chain_b))
            ring_positions = []
            for j in range(n_rings):
                t     = t_start + j / max(n_rings - 1, 1) * (t_end - t_start)
                pos_a = _pos_on_chain(chain_a, cum_a, t * total_a)
                pos_b = _pos_on_chain(chain_b, cum_b, t * total_b)
                ring_positions.append(pos_a.lerp(pos_b, 0.5))

        if len(ring_positions) < 2:
            self.report({'WARNING'}, 'Need at least 2 ring positions.')
            return {'CANCELLED'}

        v_shift = Vector((0.0, 0.0, offset_v))
        ring_positions = [p + v_shift for p in ring_positions]

        # ── Place empties ───────────────────────────────────────────────
        stale = [o for o in bpy.data.objects
                 if o.type == 'EMPTY' and o.name.startswith('crv_')
                 and o.name.rsplit('_', 1)[-1] in ('1', '2')]
        for o in stale:
            bpy.data.objects.remove(o, do_unlink=True)

        coll  = context.collection
        start = _next_index('crv')

        for i, pos in enumerate(ring_positions):
            gidx = start + i
            e = _place_empty(f'crv_{gidx:03d}_1', pos, coll, source_name)
        self.report({'INFO'},
            f'inspect, then Generate Curve')
        return {'FINISHED'}


class OT_FBXMT_Clear_Curve(Operator):
    bl_idname  = 'fbxmt.clear_curve'
    bl_label   = 'Clear Curve Empties'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        to_remove = [o for o in bpy.data.objects
                     if o.type == 'EMPTY' and o.name.startswith('crv_')
                     and o.name.rsplit('_', 1)[-1] in ('1', '2')]
        for o in to_remove:
            bpy.data.objects.remove(o, do_unlink=True)
        self.report({'INFO'}, f'{len(to_remove)} curve empty/empties removed')
        return {'FINISHED'}

# ---------------------------------------------------------------------------
# Quick Beam helpers

def _qb_build_mesh(beam_obj, props):
    """Rebuild the QuickBeam mesh in-place from the raw anchor points stored
    as custom properties on the object.  Reads qb_overrun and qb_offset_v
    from props.  Returns False if the stored anchors are missing/invalid."""
    import json
    raw = beam_obj.get('fbxmt_qb_anchors')
    if raw is None:
        return False
    try:
        a, b = json.loads(raw)
        anchor_start = Vector(a)
        anchor_end   = Vector(b)
    except Exception:
        return False

    axis   = anchor_end - anchor_start
    length = axis.length
    if length < 1e-4:
        return False

    t_dir     = axis / length
    overrun_s = getattr(props, 'qb_overrun_start', props.qb_overrun)
    overrun_e = getattr(props, 'qb_overrun_end',   props.qb_overrun)
    start_co  = anchor_start - t_dir * overrun_s
    end_co    = anchor_end   + t_dir * overrun_e

    # Apply vertical offset along world Z
    v_shift  = Vector((0, 0, props.qb_offset_v))
    start_co = start_co + v_shift
    end_co   = end_co   + v_shift

    from .ceiling_deco import _build_beam
    beam_bm = bmesh.new()
    _build_beam(beam_bm, start_co, end_co,
                props.coving_depth, props.coving_thickness,
                0.5, 0.5, mat_index=0)

    # Replace mesh data in-place so modifiers stay intact
    beam_bm.to_mesh(beam_obj.data)
    beam_bm.free()
    beam_obj.data.update()
    return True

# ---------------------------------------------------------------------------
# Operator: Quick Beam

class OT_FBXMT_Quick_Beam(bpy.types.Operator):
    bl_idname      = 'fbxmt.quick_beam'
    bl_label       = 'Quick Beam'
    bl_description = ('Select exactly 2 verts, edges, or faces (any mix). '
                      'Places a beam immediately — no empties. Select the '
                      'generated beam and use the gizmo arrows to adjust '
                      'overrun and vertical offset non-destructively.')
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode == 'EDIT_MESH'
                and obj is not None and obj.type == 'MESH')

    def execute(self, context):
        import json
        source_obj = context.active_object

        # ── Read selected elements from all meshes in Edit Mode ──────────
        # Switch to Object Mode to flush BMesh data to obj.data, then
        # read selection flags directly — no duplicate/separate needed.
        original_active = context.active_object
        all_edit_objs   = list(context.objects_in_mode)  # capture before mode switch

        bpy.ops.object.mode_set(mode='OBJECT')

        elements = []
        for obj in all_edit_objs:
            if obj.type != 'MESH':
                continue
            mat = obj.matrix_world
            bm  = bmesh.new()
            bm.from_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

            sel_faces = [f for f in bm.faces if f.select]
            face_edge_indices = {e.index for f in sel_faces for e in f.edges}
            sel_edges = [e for e in bm.edges
                         if e.select and e.index not in face_edge_indices]
            face_vert_indices = {v.index for f in sel_faces for v in f.verts}
            edge_vert_indices = {v.index for e in sel_edges for v in e.verts}
            sel_verts = [v for v in bm.verts
                         if v.select
                         and v.index not in face_vert_indices
                         and v.index not in edge_vert_indices]

            for f in sel_faces:
                elements.append(mat @ f.calc_center_median())
            for e in sel_edges:
                elements.append(mat @ ((e.verts[0].co + e.verts[1].co) / 2))
            for v in sel_verts:
                elements.append(mat @ v.co)

            bm.free()

        context.view_layer.objects.active = original_active

        if len(elements) < 2:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'WARNING'},
                'Select exactly 2 elements (verts, edges, or faces).')
            return {'CANCELLED'}

        if len(elements) > 2:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'WARNING'},
                f'Select exactly 2 elements ({len(elements)} selected).')
            return {'CANCELLED'}

        anchor_start = Vector(elements[0])
        anchor_end   = Vector(elements[1])

        axis   = anchor_end - anchor_start
        length = axis.length
        if length < 1e-4:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'WARNING'}, 'Selected elements are coincident.')
            return {'CANCELLED'}


        props          = context.scene.fbxmt_props
        t_dir          = axis / length
        overrun_s      = getattr(props, 'qb_overrun_start', props.qb_overrun)
        overrun_e      = getattr(props, 'qb_overrun_end',   props.qb_overrun)
        v_shift        = Vector((0, 0, props.qb_offset_v))
        start_co       = anchor_start - t_dir * overrun_s + v_shift
        end_co         = anchor_end   + t_dir * overrun_e + v_shift

        from .ceiling_deco import (ensure_fbxmt_materials, _build_beam,
                                   move_to_collection, COLLECTION_TRIM)
        ensure_fbxmt_materials()
        trim_mat = bpy.data.materials.get('M_FBXMT_Trim')
        if trim_mat is None:
            self.report({'ERROR'}, 'M_FBXMT_Trim not found — run Setup Scene first')
            return {'CANCELLED'}

        beam_bm = bmesh.new()
        _build_beam(beam_bm, start_co, end_co,
                    props.coving_depth, props.coving_thickness,
                    0.5, 0.5, mat_index=0)
        beam_mesh = bpy.data.meshes.new('QuickBeam')
        beam_mesh.materials.append(trim_mat)
        beam_bm.to_mesh(beam_mesh)
        beam_bm.free()
        beam_mesh.update()

        beam_obj = bpy.data.objects.new('QuickBeam', beam_mesh)
        context.scene.collection.objects.link(beam_obj)

        # Store anchors for gizmo/rebuild
        beam_obj['fbxmt_qb_anchors'] = json.dumps(
            [list(anchor_start), list(anchor_end)]
        )
        beam_obj['fbxmt_qb_source'] = source_obj.name

        # Boolean stack: source mesh + scene raycast hit mesh
        depsgraph = context.evaluated_depsgraph_get()
        if source_obj and source_obj.type == 'MESH':
            mod           = beam_obj.modifiers.new(name='FBXMT_BoolTrim_Source', type='BOOLEAN')
            mod.operation = 'DIFFERENCE'
            mod.object    = source_obj
            mod.solver    = 'FLOAT'
        result = context.scene.ray_cast(depsgraph, anchor_start + t_dir * 0.05,
                                        t_dir, distance=200.0)
        if result[0] and result[4] and result[4] != source_obj:
            hit_obj       = result[4]
            mod           = beam_obj.modifiers.new(name='FBXMT_BoolTrim_Hit', type='BOOLEAN')
            mod.operation = 'DIFFERENCE'
            mod.object    = hit_obj
            mod.solver    = 'FLOAT'
            beam_obj['fbxmt_qb_hit_obj'] = hit_obj.name

        # Select beam so gizmos activate
        bpy.ops.object.select_all(action='DESELECT')
        beam_obj.select_set(True)
        context.view_layer.objects.active = beam_obj
        # Beam stays in context.collection so gizmos remain accessible

        self.report({'INFO'}, 'Quick Beam generated — gizmos active')
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Quick Beam Refresh

class OT_FBXMT_Quick_Beam_Refresh(bpy.types.Operator):
    """Rebuild the selected QuickBeam mesh in-place using current props.
    Safe to run repeatedly — modifiers are untouched."""
    bl_idname  = 'fbxmt.quick_beam_refresh'
    bl_label   = 'Refresh Quick Beam'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None
                and obj.type == 'MESH'
                and obj.get('fbxmt_qb_anchors') is not None)

    def execute(self, context):
        obj   = context.active_object
        props = context.scene.fbxmt_props
        ok    = _qb_build_mesh(obj, props)
        if not ok:
            self.report({'WARNING'}, 'Quick Beam: anchor data missing — regenerate')
            return {'CANCELLED'}
        self.report({'INFO'}, 'Quick Beam refreshed')
        return {'FINISHED'}

# ---------------------------------------------------------------------------
# Gizmo group: Quick Beam handles

class FBXMT_GGT_QuickBeam(bpy.types.GizmoGroup):
    """Three arrow gizmos on a selected QuickBeam object."""
    bl_idname      = 'FBXMT_GGT_QuickBeam'
    bl_label       = 'Quick Beam Gizmos'
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options     = {'3D', 'PERSISTENT', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None
                and obj.type == 'MESH'
                and obj.get('fbxmt_qb_anchors') is not None)

    @staticmethod
    def _get_frame(obj, props):
        """Return (anchor_start, anchor_end, t_dir, mid) or None."""
        import json
        raw = obj.get('fbxmt_qb_anchors')
        if raw is None:
            return None
        try:
            a, b = json.loads(raw)
            s = Vector(a)
            e = Vector(b)
        except Exception:
            return None
        axis = e - s
        if axis.length < 1e-4:
            return None
        t_dir = axis.normalized()
        offset_v = props.qb_offset_v
        overrun  = props.qb_overrun
        v_shift  = Vector((0, 0, offset_v))
        start_tip = s - t_dir * overrun + v_shift
        end_tip   = e + t_dir * overrun + v_shift
        mid       = (s + e) * 0.5 + v_shift
        return start_tip, end_tip, t_dir, mid

    @staticmethod
    def _mat_from_z(direction, location):
        """Build a 4×4 matrix with local Z along direction, placed at location."""
        from mathutils import Matrix
        d  = direction.normalized()
        ref = Vector((0, 1, 0)) if abs(d.dot(Vector((0, 0, 1)))) > 0.99               else Vector((0, 0, 1))
        x  = ref.cross(d).normalized()
        if x.length < 1e-6:
            x = Vector((1, 0, 0))
        y  = d.cross(x).normalized()
        m  = Matrix((
            (x.x, y.x, d.x, location.x),
            (x.y, y.y, d.y, location.y),
            (x.z, y.z, d.z, location.z),
            (0,   0,   0,   1          ),
        ))
        return m

    def setup(self, context):
        def _arrow(color):
            gz = self.gizmos.new('GIZMO_GT_arrow_3d')
            gz.draw_style        = 'NORMAL'
            gz.length            = 1.0
            gz.color             = color
            gz.color_highlight   = (1.0, 1.0, 0.2)
            gz.alpha             = 0.4
            gz.alpha_highlight   = 1.0
            gz.scale_basis       = 0.6
            gz.use_draw_modal    = True
            return gz

        self.gz_start = _arrow((0.2, 0.55, 1.0))   # blue — overrun start
        self.gz_end   = _arrow((0.2, 0.55, 1.0))   # blue — overrun end

        op = self.gz_start.target_set_operator('fbxmt.quick_beam_gizmo_drag')
        op.mode = 'OVERRUN_START'
        op = self.gz_end.target_set_operator('fbxmt.quick_beam_gizmo_drag')
        op.mode = 'OVERRUN_END'

    def draw_prepare(self, context):
        if not hasattr(self, 'gz_start'):
            return
        obj   = context.active_object
        props = context.scene.fbxmt_props
        frame = FBXMT_GGT_QuickBeam._get_frame(obj, props)
        if frame is None:
            return
        start_tip, end_tip, t_dir, mid = frame
        mf = FBXMT_GGT_QuickBeam._mat_from_z

        self.gz_start.matrix_basis = mf(-t_dir, start_tip)
        self.gz_end.matrix_basis   = mf( t_dir, end_tip)

# ---------------------------------------------------------------------------
# Operator: Quick Beam Gizmo Drag  (modal, invoked by gizmo arrows)

class OT_FBXMT_Quick_Beam_Gizmo_Drag(bpy.types.Operator):
    """Modal drag operator invoked by the Quick Beam gizmo arrows.
    Drag right to increase, left to decrease.  LMB release commits."""
    bl_idname  = 'fbxmt.quick_beam_gizmo_drag'
    bl_label   = 'Quick Beam Drag'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: bpy.props.StringProperty(default='OVERRUN_START')

    def invoke(self, context, event):
        obj = context.active_object
        if obj is None or obj.get('fbxmt_qb_anchors') is None:
            return {'CANCELLED'}
        props = context.scene.fbxmt_props
        self._start_x        = event.mouse_x
        self._orig_overrun_s = getattr(props, 'qb_overrun_start', props.qb_overrun)
        self._orig_overrun_e = getattr(props, 'qb_overrun_end',   props.qb_overrun)
        self._orig_offset_v  = props.qb_offset_v
        self._obj_name       = obj.name
        # Disable boolean modifiers for clean viewport feedback during drag
        self._bool_states = {}
        for mod in obj.modifiers:
            if mod.type == 'BOOLEAN':
                self._bool_states[mod.name] = mod.show_viewport
                mod.show_viewport = False
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            delta_px = event.mouse_x - self._start_x
            # 1 px ≈ 0.001 m  (fine control; hold Shift for ×0.1)
            scale = 0.001 if not event.shift else 0.0001
            props = context.scene.fbxmt_props
            if self.mode == 'OVERRUN_START':
                props.qb_overrun_start = max(0.0, self._orig_overrun_s + delta_px * scale)
            elif self.mode == 'OVERRUN_END':
                props.qb_overrun_end = max(0.0, self._orig_overrun_e - delta_px * scale)
            elif self.mode == 'OFFSET_V':
                props.qb_offset_v = self._orig_offset_v + delta_px * scale
            obj = context.active_object
            if obj and obj.get('fbxmt_qb_anchors'):
                _qb_build_mesh(obj, props)
                for area in context.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
            return {'RUNNING_MODAL'}

        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            # Rebuild then re-enable booleans
            obj = bpy.data.objects.get(self._obj_name)
            if obj:
                props = context.scene.fbxmt_props
                _qb_build_mesh(obj, props)
                for mod in obj.modifiers:
                    if mod.name in self._bool_states:
                        mod.show_viewport = self._bool_states[mod.name]
            return {'FINISHED'}

        elif event.type in ('RIGHTMOUSE', 'ESC'):
            props = context.scene.fbxmt_props
            if hasattr(props, 'qb_overrun_start'):
                props.qb_overrun_start = self._orig_overrun_s
                props.qb_overrun_end   = self._orig_overrun_e
            props.qb_offset_v = self._orig_offset_v
            obj = bpy.data.objects.get(self._obj_name)
            if obj:
                _qb_build_mesh(obj, props)
                for mod in obj.modifiers:
                    if mod.name in self._bool_states:
                        mod.show_viewport = self._bool_states[mod.name]
            return {'CANCELLED'}

        return {'PASS_THROUGH'}

# ---------------------------------------------------------------------------
# Dihedral Beam helpers

def _dihedral_bisector(edge, obj_matrix):
    """Return (bisector, tangent, edge_mid) in world space for a BMEdge.

    bisector — unit vector pointing AWAY from the dihedral interior, i.e. the
               average of the two outward face normals projected perpendicular
               to the edge tangent.  This is the direction the beam profile
               'faces' into.
    tangent  — unit vector along the edge (v0 → v1).
    edge_mid — world-space midpoint of the edge.

    Returns None if the edge has < 2 linked faces or is degenerate.
    """
    import sys as _sys
    if len(edge.link_faces) < 2:
        print('FBXMT DH: edge has fewer than 2 linked faces', file=_sys.stderr)
        return None

    mat = obj_matrix
    rot = mat.to_3x3()

    # World-space edge tangent
    v0 = mat @ edge.verts[0].co
    v1 = mat @ edge.verts[1].co
    tangent = v1 - v0
    if tangent.length < 1e-6:
        print('FBXMT DH: edge verts are coincident', file=_sys.stderr)
        return None
    tangent = tangent.normalized()

    # Face normals in world space — OUTWARD (BMesh normals are outward by convention)
    n0 = (rot @ edge.link_faces[0].normal).normalized()
    n1 = (rot @ edge.link_faces[1].normal).normalized()


    # Average outward normals, project out the tangent component so the
    # bisector is perpendicular to the edge.
    avg  = n0 + n1
    perp = avg - tangent * avg.dot(tangent)
    if perp.length < 1e-6:
        # Faces are coplanar — bisector is ambiguous, use one face normal
        perp = n0 - tangent * n0.dot(tangent)
    if perp.length < 1e-6:
        return None

    bisector = perp.normalized()
    edge_mid = (v0 + v1) * 0.5

    # Convex vs concave: dot(face_normal, face_centroid - edge_mid)
    # For convex: centroid is on the same side as the outward normal → dot > 0
    # For concave: centroid is on the opposite side → dot < 0
    # Average both faces for robustness.

    return bisector, tangent, edge_mid

def _dh_profile_axes(tangent, bisector):
    """Return (h_arm, wall_down) for a dihedral bridging beam.
    h_arm = bisector × world_up (horizontal width)
    wall_down = -world_up (always hangs down, same as all beams)
    """
    world_up  = Vector((0, 0, 1))
    h_arm     = bisector.cross(world_up)
    if h_arm.length < 1e-6:
        h_arm = tangent.cross(world_up)
    if h_arm.length < 1e-6:
        h_arm = Vector((1, 0, 0))
    h_arm     = h_arm.normalized()
    wall_down = -world_up
    return h_arm, wall_down

def _dh_build_mesh(beam_obj, props):
    """Rebuild a DihedralBeam mesh in-place from stored custom props.

    Reads from beam_obj custom properties:
        fbxmt_dh_v0       — JSON [x,y,z]  world-space edge vert 0
        fbxmt_dh_v1       — JSON [x,y,z]  world-space edge vert 1
        fbxmt_dh_bisector — JSON [x,y,z]  unit bisector (away from corner)
        fbxmt_dh_source   — str           name of source object

    The beam sweeps along the edge (v0→v1), overrun extended at both ends.
    Profile orientation: h_arm = tangent × bisector, wall_down = -bisector.
    dh_offset slides the whole beam along the bisector (useful when the
    ray-cast origin needs to clear the surface).

    Returns False if anchor data is missing or degenerate.
    """
    import json, sys as _sys

    def _vec(raw):
        if raw is None:
            return None
        if isinstance(raw, str):
            return Vector(json.loads(raw))
        return Vector(raw)  # IDPropertyArray or list

    try:
        v0  = _vec(beam_obj.get('fbxmt_dh_v0'))
        v1  = _vec(beam_obj.get('fbxmt_dh_v1'))
        bis = _vec(beam_obj.get('fbxmt_dh_bisector'))
        if v0 is None or v1 is None or bis is None:
            raise KeyError('missing v0/v1/bisector')
        bis = bis.normalized()
    except (KeyError, Exception) as e:
        print(f'FBXMT DH rebuild: missing anchor data — {e}', file=_sys.stderr)
        return False

    tangent = (v1 - v0)
    if tangent.length < 1e-6:
        print('FBXMT DH rebuild: v0/v1 coincident', file=_sys.stderr)
        return False
    tangent = tangent.normalized()

    overrun_s  = getattr(props, 'dh_overrun_start', props.dh_overrun)
    overrun_e  = getattr(props, 'dh_overrun_end',   props.dh_overrun)
    offset     = props.dh_offset
    bis_n      = bis.normalized()
    v_shift    = bis_n * offset
    edge_mid   = (v0 + v1) * 0.5

    # Use stored hit_loc if available — avoids re-raycasting wrong source
    raw_hit = beam_obj.get('fbxmt_dh_hit')
    if raw_hit and len(raw_hit) == 3:
        hit_loc = Vector(raw_hit)
    else:
        hit_loc = None

    if hit_loc is not None:
        t_dir    = (hit_loc - edge_mid).normalized()
        start_co = edge_mid - t_dir * overrun_s + v_shift
        end_co   = hit_loc  + t_dir * overrun_e + v_shift
    else:
        start_co = edge_mid + v_shift
        end_co   = edge_mid + v_shift + bis_n * props.coving_depth

    # Update stored tips for gizmo (deduplicated)
    beam_obj['fbxmt_dh_start'] = list(start_co)
    beam_obj['fbxmt_dh_end']   = list(end_co)

    h_arm, wall_down = _dh_profile_axes(tangent, bis)


    from .ceiling_deco import _build_beam_oriented
    beam_bm = bmesh.new()
    _build_beam_oriented(beam_bm, start_co, end_co,
                         h_arm, wall_down,
                         props.coving_depth, props.coving_thickness,
                         0.5, 0.5, mat_index=0)
    beam_bm.to_mesh(beam_obj.data)
    beam_bm.free()
    beam_obj.data.update()
    beam_obj.update_tag()
    return True

# ---------------------------------------------------------------------------
# Operator: Place Dihedral Beam  (Edit Mode — places dh_NNN_1 empty only)

class OT_FBXMT_Place_Dihedral(bpy.types.Operator):
    """Select exactly 1 edge in Edit Mode. Places a dh_NNN_1 empty at the
    edge midpoint storing v0/v1/bisector. Generate Dihedral then raycasts
    to find the far surface and builds the beam."""
    bl_idname      = 'fbxmt.place_dihedral'
    bl_label       = 'Place Dihedral'
    bl_description = ('Edge Select Mode: select 1 edge with 2 adjacent faces. '
                      'Places dh_NNN_1 anchor empty. '
                      'Then use Generate Dihedral to build the beam.')
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode == 'EDIT_MESH'
                and obj is not None and obj.type == 'MESH')

    def execute(self, context):
        obj        = context.active_object
        source_obj = obj
        mat        = obj.matrix_world

        bpy.ops.object.mode_set(mode='OBJECT')

        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        sel_edges = [e for e in bm.edges if e.select]

        if len(sel_edges) != 1:
            bm.free()
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'WARNING'},
                f'Select exactly 1 edge ({len(sel_edges)} selected).')
            return {'CANCELLED'}

        edge   = sel_edges[0]
        result = _dihedral_bisector(edge, mat)

        if result is None:
            bm.free()
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'WARNING'},
                'Edge needs exactly 2 adjacent faces.')
            return {'CANCELLED'}

        bisector, tangent, edge_mid = result
        v0_world = mat @ edge.verts[0].co
        v1_world = mat @ edge.verts[1].co
        bm.free()

        bpy.ops.object.mode_set(mode='EDIT')

        props   = context.scene.fbxmt_props
        offset  = props.dh_offset
        v_shift = bisector * offset

        gidx   = _next_index('dh')
        anchor = _place_empty(f'dh_{gidx:03d}_1', edge_mid + v_shift,
                              context.collection, source_obj.name)
        anchor['fbxmt_dh_v0']       = list(v0_world)
        anchor['fbxmt_dh_v1']       = list(v1_world)
        anchor['fbxmt_dh_bisector']  = list(bisector)   # un-flipped; Generate picks direction
        anchor['fbxmt_dh_tangent']   = list(tangent)


        try:
            from .par_ray_preview import invalidate_dh_cache
            invalidate_dh_cache()
        except Exception:
            pass

        self.report({'INFO'}, f'{anchor.name} placed — use Generate Dihedral to build beam')
        return {'FINISHED'}

# ---------------------------------------------------------------------------
# Operator: Generate Dihedral Beams  (Object Mode — mirrors Generate Parallel)

class OT_FBXMT_Generate_Dihedral(bpy.types.Operator):
    """Raycast from each dh_NNN_1 empty along ±bisector to find the far
    surface, then builds a beam from edge to hit. Leaves Boolean modifier
    in stack unapplied."""
    bl_idname      = 'fbxmt.generate_dihedral'
    bl_label       = 'Generate Dihedral'
    bl_description = ('Ray-cast from dh_NNN_1 empties along the stored bisector '
                      '(both directions tried) to find the opposite surface. '
                      'Builds beams, boolean trims left unapplied.')
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        props     = context.scene.fbxmt_props
        depsgraph = context.evaluated_depsgraph_get()

        anchors = sorted(
            [o for o in bpy.data.objects
             if o.type == 'EMPTY'
             and o.name.startswith('dh_')
             and o.name.rsplit('_', 1)[-1] == '1'],
            key=lambda o: o.name,
        )

        if not anchors:
            self.report({'WARNING'}, 'No dh_NNN_1 empties — use Place Dihedral first.')
            return {'CANCELLED'}

        from .ceiling_deco import (ensure_fbxmt_materials, _build_beam_oriented,
                                   _smart_raycast, move_to_collection,
                                   COLLECTION_TRIM)
        ensure_fbxmt_materials()
        trim_mat = bpy.data.materials.get('M_FBXMT_Trim')
        if trim_mat is None:
            self.report({'ERROR'}, 'M_FBXMT_Trim not found — run Setup Scene first')
            return {'CANCELLED'}

        overrun   = props.dh_overrun
        generated = 0
        skipped   = 0

        for anchor in anchors:
            raw_v0  = anchor.get('fbxmt_dh_v0')
            raw_v1  = anchor.get('fbxmt_dh_v1')
            raw_bis = anchor.get('fbxmt_dh_bisector')
            raw_tan = anchor.get('fbxmt_dh_tangent')

            if any(r is None for r in (raw_v0, raw_v1, raw_bis, raw_tan)):
                print(f'FBXMT DH generate: {anchor.name} missing data — skipping')
                skipped += 1
                continue

            v0       = Vector(raw_v0)
            v1       = Vector(raw_v1)
            bisector = Vector(raw_bis).normalized()
            tangent  = Vector(raw_tan).normalized()
            edge_mid = (v0 + v1) * 0.5

            source_name = anchor.get('fbxmt_source', '')
            source_obj  = bpy.data.objects.get(source_name) if source_name else None
            if source_obj is None or source_obj.type != 'MESH':
                print(f'FBXMT DH generate: {anchor.name} source {source_name!r} not found')
                skipped += 1
                continue

            # Trim outputs always live in +bisector (outward normal) space.
            # Cast along +bisector only — the opposite face is always in that direction.
            # Debug: print local-space ray to diagnose transform issues
            mat_inv    = source_obj.matrix_world.inverted()
            rot_inv    = mat_inv.to_3x3()
            local_orig = mat_inv @ edge_mid
            local_dir  = (rot_inv @ bisector).normalized()
            mw = source_obj.matrix_world

            # Cast against the whole scene — agnostic of which mesh is the source.
            # Nudge origin 0.1m along bisector to clear the corner geometry.
            ray_origin = Vector(edge_mid) + Vector(bisector) * 0.1
            ray_dir    = bisector

            result = context.scene.ray_cast(
                depsgraph,
                ray_origin,
                Vector(bisector).normalized(),
                distance=200.0,
            )
            hit_loc      = Vector(result[1]) if result[0] else None
            hit_obj_name = result[4].name if result[0] and result[4] else ""

            if hit_loc is not None:
                t_dir    = (hit_loc - edge_mid).normalized()
                start_co = edge_mid - t_dir * props.dh_overrun_start
                end_co   = hit_loc  + t_dir * props.dh_overrun_end
            else:
                # No opposite face — protrude by coving_depth
                start_co = edge_mid
                end_co   = edge_mid + bisector * props.coving_depth

            h_arm, wall_down = _dh_profile_axes(tangent, bisector)

            span = (end_co - start_co).length

            beam_bm = bmesh.new()
            _build_beam_oriented(beam_bm, start_co, end_co,
                                 h_arm, wall_down,
                                 props.coving_depth, props.coving_thickness,
                                 0.5, 0.5, mat_index=0)
            beam_mesh = bpy.data.meshes.new(f'{anchor.name[:-2]}_Beam')
            beam_mesh.materials.append(trim_mat)
            beam_bm.to_mesh(beam_mesh)
            beam_bm.free()
            beam_mesh.update()

            beam_obj = bpy.data.objects.new(f'{anchor.name[:-2]}_Beam', beam_mesh)
            context.scene.collection.objects.link(beam_obj)

            beam_obj['fbxmt_dh_anchor']   = anchor.name
            beam_obj['fbxmt_dh_start']    = list(start_co)
            beam_obj['fbxmt_dh_end']      = list(end_co)
            beam_obj['fbxmt_dh_hit']      = list(hit_loc) if hit_loc is not None else []
            beam_obj['fbxmt_dh_hit_obj']  = hit_obj_name
            # Copy anchor data so gizmo poll works on beam object
            beam_obj['fbxmt_dh_v0']       = list(v0)
            beam_obj['fbxmt_dh_v1']       = list(v1)
            beam_obj['fbxmt_dh_bisector'] = list(bisector)
            beam_obj['fbxmt_dh_source']   = source_name

            # Boolean stack: source vert mesh + raycast hit mesh
            # Add source first (the wall the edge belongs to)
            if source_obj and source_obj.type == 'MESH':
                mod           = beam_obj.modifiers.new(name='FBXMT_BoolTrim_Source', type='BOOLEAN')
                mod.operation = 'DIFFERENCE'
                mod.object    = source_obj
                mod.solver    = 'FLOAT'
            # Add hit mesh if different from source
            hit_obj = bpy.data.objects.get(hit_obj_name) if hit_obj_name else None
            if hit_obj and hit_obj.type == 'MESH' and hit_obj != source_obj:
                mod           = beam_obj.modifiers.new(name='FBXMT_BoolTrim_Hit', type='BOOLEAN')
                mod.operation = 'DIFFERENCE'
                mod.object    = hit_obj
                mod.solver    = 'FLOAT'

            # Remove the anchor empty — beam is built, empty no longer needed
            try:
                bpy.data.objects.remove(anchor, do_unlink=True)
            except Exception:
                pass

            bpy.ops.object.select_all(action='DESELECT')
            beam_obj.select_set(True)
            context.view_layer.objects.active = beam_obj
            move_to_collection(beam_obj, COLLECTION_TRIM)

            generated += 1

        try:
            from .par_ray_preview import invalidate_dh_cache
            invalidate_dh_cache()
        except Exception:
            pass

        msg = f'{generated} dihedral beam(s) generated'
        if skipped:
            msg += f', {skipped} skipped'
        self.report({'INFO'}, msg)
        return {'FINISHED'}

# ---------------------------------------------------------------------------
# Operator: Clear Dihedral

class OT_FBXMT_Clear_Dihedral(bpy.types.Operator):
    bl_idname  = 'fbxmt.clear_dihedral'
    bl_label   = 'Clear Dihedral Empties'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        to_remove = [o for o in bpy.data.objects
                     if o.type == 'EMPTY' and o.name.startswith('dh_')
                     and o.name.rsplit('_', 1)[-1] in ('1', '2')]
        for o in to_remove:
            bpy.data.objects.remove(o, do_unlink=True)
        try:
            from .par_ray_preview import invalidate_dh_cache
            invalidate_dh_cache()
        except Exception:
            pass
        self.report({'INFO'}, f'{len(to_remove)} dihedral empty/empties removed')
        return {'FINISHED'}

# ---------------------------------------------------------------------------
# Operator: Dihedral Beam Refresh

class OT_FBXMT_Dihedral_Beam_Refresh(bpy.types.Operator):
    """Rebuild the selected DihedralBeam mesh in-place using current props."""
    bl_idname  = 'fbxmt.dihedral_beam_refresh'
    bl_label   = 'Refresh Dihedral Beam'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None
                and obj.type == 'MESH'
                and obj.get('fbxmt_dh_v0') is not None)

    def execute(self, context):
        obj   = context.active_object
        props = context.scene.fbxmt_props
        ok    = _dh_build_mesh(obj, props)
        if not ok:
            self.report({'WARNING'}, 'Dihedral Beam: missing anchor data — regenerate')
            return {'CANCELLED'}
        self.report({'INFO'}, 'Dihedral Beam refreshed')
        return {'FINISHED'}

# ---------------------------------------------------------------------------
# Operator: Dihedral Beam Gizmo Drag

class OT_FBXMT_Dihedral_Beam_Gizmo_Drag(bpy.types.Operator):
    """Modal drag for Dihedral Beam gizmo arrows.
    mode: OVERRUN | OFFSET"""
    bl_idname  = 'fbxmt.dihedral_beam_gizmo_drag'
    bl_label   = 'Dihedral Beam Drag'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    mode: bpy.props.StringProperty(default='OVERRUN')

    def invoke(self, context, event):
        obj = context.active_object
        if obj is None or obj.get('fbxmt_dh_v0') is None:
            return {'CANCELLED'}
        props = context.scene.fbxmt_props
        self._start_x      = event.mouse_x
        self._orig_overrun   = getattr(props, 'dh_overrun_start', props.dh_overrun)
        self._orig_overrun_e = getattr(props, 'dh_overrun_end', props.dh_overrun)
        self._orig_offset    = props.dh_offset
        self._obj_name       = obj.name
        # Disable booleans for clean drag feedback
        self._bool_states = {}
        for mod in obj.modifiers:
            if mod.type == 'BOOLEAN':
                self._bool_states[mod.name] = mod.show_viewport
                mod.show_viewport = False
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            delta_px = event.mouse_x - self._start_x
            scale    = 0.005 if not event.shift else 0.0005
            props    = context.scene.fbxmt_props
            if self.mode == 'OVERRUN_START':
                props.dh_overrun_start = max(0.0, self._orig_overrun + delta_px * scale)
            elif self.mode in ('OVERRUN', 'OVERRUN_END'):
                props.dh_overrun_end = max(0.0, self._orig_overrun + delta_px * scale)
            elif self.mode == 'OFFSET':
                props.dh_offset  = self._orig_offset + delta_px * scale
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
            return {'RUNNING_MODAL'}

        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            obj = bpy.data.objects.get(self._obj_name)
            if obj and obj.get('fbxmt_dh_v0') is not None:
                props = context.scene.fbxmt_props
                _dh_build_mesh(obj, props)
                for mod in obj.modifiers:
                    if mod.name in self._bool_states:
                        mod.show_viewport = self._bool_states[mod.name]
            return {'FINISHED'}

        elif event.type in ('RIGHTMOUSE', 'ESC'):
            props = context.scene.fbxmt_props
            if hasattr(props, 'dh_overrun_start'):
                props.dh_overrun_start = self._orig_overrun
                props.dh_overrun_end   = self._orig_overrun_e
            props.dh_offset = self._orig_offset
            obj = bpy.data.objects.get(self._obj_name)
            if obj and obj.get('fbxmt_dh_v0') is not None:
                _dh_build_mesh(obj, props)
                for mod in obj.modifiers:
                    if mod.name in self._bool_states:
                        mod.show_viewport = self._bool_states[mod.name]
            return {'CANCELLED'}

        return {'PASS_THROUGH'}

class FBXMT_GGT_DihedralBeam(bpy.types.GizmoGroup):
    """Two arrow gizmos on a selected DihedralBeam object."""
    bl_idname      = 'FBXMT_GGT_DihedralBeam'
    bl_label       = 'Dihedral Beam Gizmos'
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options     = {'3D', 'PERSISTENT', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        result = (obj is not None
                  and obj.type == 'MESH'
                  and obj.get('fbxmt_dh_v0') is not None)
        return result

    @staticmethod
    def _get_frame(obj, props):
        import json, sys as _sys
        def _vec(raw):
            """Accept IDPropertyArray, list, or JSON string."""
            if raw is None:
                return None
            if isinstance(raw, str):
                return Vector(json.loads(raw))
            return Vector(raw)  # IDPropertyArray or list

        try:
            v0  = _vec(obj.get('fbxmt_dh_v0'))
            v1  = _vec(obj.get('fbxmt_dh_v1'))
            bis = _vec(obj.get('fbxmt_dh_bisector'))
            if v0 is None or v1 is None or bis is None:
                return None
            bis = bis.normalized()
        except Exception as e:
            return None

        tangent = (v1 - v0)
        if tangent.length < 1e-6:
            return None
        tangent = tangent.normalized()

        raw_start = obj.get('fbxmt_dh_start')
        raw_end   = obj.get('fbxmt_dh_end')
        if raw_start is not None and raw_end is not None:
            start_tip = _vec(raw_start)
            end_tip   = _vec(raw_end)
        else:
            overrun   = props.dh_overrun
            offset    = props.dh_offset
            v_shift   = bis * offset
            start_tip = v0 - tangent * overrun + v_shift
            end_tip   = v1 + tangent * overrun + v_shift

        mid = (start_tip + end_tip) * 0.5
        return start_tip, end_tip, tangent, bis, mid

    @staticmethod
    def _mat_from_z(direction, location):
        from mathutils import Matrix
        d  = direction.normalized()
        ref = Vector((0, 1, 0)) if abs(d.dot(Vector((0, 0, 1)))) > 0.99               else Vector((0, 0, 1))
        x  = ref.cross(d).normalized()
        if x.length < 1e-6:
            x = Vector((1, 0, 0))
        y  = d.cross(x).normalized()
        m  = Matrix((
            (x.x, y.x, d.x, location.x),
            (x.y, y.y, d.y, location.y),
            (x.z, y.z, d.z, location.z),
            (0,   0,   0,   1          ),
        ))
        return m

    def setup(self, context):
        def _arrow(color, scale=1.0):
            gz = self.gizmos.new('GIZMO_GT_arrow_3d')
            gz.draw_style      = 'NORMAL'
            gz.length          = 1.0
            gz.color           = color
            gz.color_highlight = (1.0, 1.0, 0.2)
            gz.alpha           = 0.4
            gz.alpha_highlight = 1.0
            gz.scale_basis     = 0.6 * scale
            gz.use_draw_modal  = True
            return gz

        self.gz_start = _arrow((1.0, 0.45, 0.1))   # orange — overrun start
        self.gz_end   = _arrow((1.0, 0.45, 0.1))   # orange — overrun end
        self.gz_bis      = _arrow((0.2, 0.85, 0.35), scale=0.7)
        self.gz_bis_back = _arrow((0.2, 0.85, 0.35), scale=0.7)

        op = self.gz_start.target_set_operator('fbxmt.dihedral_beam_gizmo_drag')
        op.mode = 'OVERRUN_START'
        op = self.gz_end.target_set_operator('fbxmt.dihedral_beam_gizmo_drag')
        op.mode = 'OVERRUN_END'
        op = self.gz_bis.target_set_operator('fbxmt.dihedral_beam_gizmo_drag')
        op.mode = 'OFFSET'
        op = self.gz_bis_back.target_set_operator('fbxmt.dihedral_beam_gizmo_drag')
        op.mode = 'OFFSET'

    def draw_prepare(self, context):
        # Guard: setup may not have completed (e.g. raised an exception)
        if not hasattr(self, 'gz_start'):
            return
        obj   = context.active_object
        props = context.scene.fbxmt_props
        frame = FBXMT_GGT_DihedralBeam._get_frame(obj, props)
        if frame is None:
            return
        start_tip, end_tip, tangent, bis, mid = frame
        mf = FBXMT_GGT_DihedralBeam._mat_from_z

        self.gz_start.matrix_basis = mf(-bis, start_tip)
        self.gz_end.matrix_basis   = mf( bis, end_tip)
        self.gz_bis.matrix_basis      = mf( tangent, mid)
        self.gz_bis_back.matrix_basis = mf(-tangent, mid)

# ---------------------------------------------------------------------------
# Legacy clear

class OT_FBXMT_Clear_Beams(Operator):
    bl_idname  = 'fbxmt.clear_beams'
    bl_label   = 'Clear Legacy Beam Empties'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        to_remove = [o for o in bpy.data.objects
                     if o.type == 'EMPTY' and o.name.startswith('beam_')
                     and o.name.rsplit('_', 1)[-1] in ('1', '2')]
        for o in to_remove:
            bpy.data.objects.remove(o, do_unlink=True)
        self.report({'INFO'}, f'{len(to_remove)} legacy empty/empties removed')
        return {'FINISHED'}

# ---------------------------------------------------------------------------
# Operator: Preview Parallel Rays

def _force_viewport_redraw(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
                for region in area.regions:
                    if region.type == 'WINDOW':
                        region.tag_redraw()

def _ensure_object_mode_for_raycast(context):
    """Switch to Object mode if needed, return whether we switched."""
    was_edit = context.mode == 'EDIT_MESH'
    if was_edit:
        bpy.ops.object.mode_set(mode='OBJECT')
    return was_edit

class OT_FBXMT_Preview_Parallel_Rays(bpy.types.Operator):
    bl_idname      = 'fbxmt.preview_parallel_rays'
    bl_label       = 'Preview Rays'
    bl_description = 'Recompute and display ray-cast preview for all par_NNN_1 empties'
    bl_options     = {'REGISTER'}

    def execute(self, context):
        try:
            from .par_ray_preview import invalidate_par_cache, _update_par_cache
            invalidate_par_cache()
            was_edit = _ensure_object_mode_for_raycast(context)
            context.view_layer.update()
            _update_par_cache(context.evaluated_depsgraph_get())
            if was_edit:
                bpy.ops.object.mode_set(mode='EDIT')
        except Exception as e:
            self.report({'WARNING'}, f'Ray preview failed: {e}')
            return {'CANCELLED'}
        _force_viewport_redraw(context)
        self.report({'INFO'}, 'Parallel ray preview updated')
        return {'FINISHED'}

class OT_FBXMT_Preview_Dihedral_Ray(bpy.types.Operator):
    bl_idname      = 'fbxmt.preview_dihedral_ray'
    bl_label       = 'Preview Ray'
    bl_description = ('Recompute and display the dihedral ray-cast preview for all '
                      'DihedralBeam meshes in the scene')
    bl_options     = {'REGISTER'}

    def execute(self, context):
        try:
            from .par_ray_preview import invalidate_dh_cache, _update_dh_cache

            # ray_cast on mesh objects only works in Object Mode — the BMesh
            # is not flushed to mesh data while the object is in Edit Mode.
            # Switch unconditionally and restore afterward.
            was_edit = context.mode == 'EDIT_MESH'
            if was_edit:
                bpy.ops.object.mode_set(mode='OBJECT')

            context.view_layer.update()
            dg = context.evaluated_depsgraph_get()

            invalidate_dh_cache()
            _update_dh_cache(dg)

            if was_edit:
                bpy.ops.object.mode_set(mode='EDIT')

        except Exception as e:
            self.report({'WARNING'}, f'Dihedral ray preview failed: {e}')
            return {'CANCELLED'}

        # Immediate redraw + deferred second redraw in case the first fires
        # before the draw handler has picked up the new cache entries.
        _force_viewport_redraw(context)
        def _deferred_redraw():
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
            return None  # don't repeat
        bpy.app.timers.register(_deferred_redraw, first_interval=0.05)

        from .par_ray_preview import _ray_cache as _rc, _get_dh_anchors
        import sys as _sys2
        all_anchors = _get_dh_anchors()
        all_empties = [o.name for o in bpy.data.objects if o.type == 'EMPTY']
        dh_empties  = [o.name for o in bpy.data.objects
                       if o.type == 'EMPTY' and o.name.startswith('dh_')]
        dh_entries = {k: v for k, v in _rc.items() if k.startswith('dh:')}
        n_anchors  = len(all_anchors)
        self.report({'INFO'},
            f'{sum(len(v["segments"]) for v in dh_entries.values())} segment(s)')
        return {'FINISHED'}

# ---------------------------------------------------------------------------
# Parallel Beam rebuild function

def _par_build_mesh(beam_obj, props):
    """Rebuild a Parallel Beam mesh in-place from stored anchor data + props."""

    def _vec(raw):
        if raw is None: return None
        from mathutils import Vector
        return Vector(raw)

    raw_start = beam_obj.get('fbxmt_par_start')
    raw_end   = beam_obj.get('fbxmt_par_end')
    t_raw     = beam_obj.get('fbxmt_par_t_dir')
    lat_raw   = beam_obj.get('fbxmt_par_lat_dir')

    if not all([raw_start, raw_end, t_raw, lat_raw]):
        return False

    start  = _vec(raw_start)
    end    = _vec(raw_end)
    t_dir  = _vec(t_raw).normalized()
    lat_dir = _vec(lat_raw).normalized()

    overrun_s  = getattr(props, 'par_overrun_start',        0.25)
    overrun_e  = getattr(props, 'par_overrun_end',          0.25)
    ep_inset_s = getattr(props, 'par_endpoint_inset_start', 0.0)
    ep_inset_e = getattr(props, 'par_endpoint_inset_end',   0.0)
    offset_v   = getattr(props, 'par_offset_v',             0.0)
    offset_lat = getattr(props, 'par_offset_lat',           0.0)

    v_shift   = Vector((0, 0, offset_v))
    lat_shift = lat_dir * offset_lat

    # overrun extends past the anchor (away from wall); ep_inset pulls back
    # toward the anchor (shortens the beam end).  Both along t_dir.
    # start_co moves in -t_dir by overrun, then +t_dir by ep_inset (net shorter)
    # end_co   moves in +t_dir by overrun, then -t_dir by ep_inset (net shorter)
    start_co = start - t_dir * overrun_s + t_dir * ep_inset_s + v_shift + lat_shift
    end_co   = end   + t_dir * overrun_e - t_dir * ep_inset_e + v_shift + lat_shift

    from .ceiling_deco import _build_beam_per_vert
    beam_bm  = bmesh.new()
    s_offsets = [beam_obj.get(f'fbxmt_par_vs{i}', 0.0) for i in range(4)]
    e_offsets = [beam_obj.get(f'fbxmt_par_ve{i}', 0.0) for i in range(4)]
    _build_beam_per_vert(beam_bm, start_co, end_co,
                         props.coving_depth, props.coving_thickness,
                         0.5, 0.5,
                         s_offsets=s_offsets,
                         e_offsets=e_offsets,
                         mat_index=0)
    beam_bm.to_mesh(beam_obj.data)
    beam_bm.free()
    beam_obj.data.update()
    beam_obj.update_tag()
    return True


# ---------------------------------------------------------------------------
# Parallel Beam group regeneration helper

def _par_regen_group(context, grp_empty, props):
    """Rebuild beam meshes for a group using stored anchor positions.
    Deletes existing child beams, ray-casts new ones, re-parents to same empty."""
    import json as _json
    grp_name = grp_empty.name

    anchors_json = grp_empty.get('fbxmt_par_anchors', '[]')
    source_name  = grp_empty.get('fbxmt_par_source',  '')
    t_dir_raw    = grp_empty.get('fbxmt_par_t_dir',   [1,0,0])

    try:
        anchor_positions = [Vector(v) for v in _json.loads(anchors_json)]
    except Exception:
        return
    if not anchor_positions:
        return

    source_obj = bpy.data.objects.get(source_name) if source_name else None
    if source_obj is None:
        return

    inset_s  = getattr(props, 'par_inset_start', 0.0)
    inset_e  = getattr(props, 'par_inset_end',   0.0)
    offset_v = getattr(props, 'par_offset_v',    0.0)
    t_dir    = Vector(t_dir_raw).normalized()
    v_shift  = Vector((0, 0, offset_v))

    # Delete existing child beams
    children = [o for o in bpy.data.objects
                if o.get('fbxmt_par_group_empty') == grp_name]
    for c in children:
        bpy.data.objects.remove(c, do_unlink=True)

    # Trim anchor list based on inset values
    span_start = _vec_or_none(grp_empty.get('fbxmt_par_span_start'))
    span_end   = _vec_or_none(grp_empty.get('fbxmt_par_span_end'))
    span_len   = grp_empty.get('fbxmt_par_span_len', 0.0)

    if span_start and span_end and span_len > 0:
        span_dir = (span_end - span_start).normalized()
        filtered = []
        for pos in anchor_positions:
            along = (pos - span_start).dot(span_dir)
            if along >= inset_s and along <= (span_len - inset_e):
                filtered.append(pos)
        if not filtered:
            return
        anchor_positions = filtered

    # Rebuild each beam by ray-casting from stored anchor position
    from .ceiling_deco import _build_beam
    ensure_fbxmt_materials = None
    try:
        from .ceiling_deco import ensure_fbxmt_materials as _efm
        ensure_fbxmt_materials = _efm
    except ImportError:
        pass

    if ensure_fbxmt_materials:
        ensure_fbxmt_materials()
    trim_mat = bpy.data.materials.get('M_FBXMT_Trim')

    depsgraph  = context.evaluated_depsgraph_get()
    n_anchors  = len(anchor_positions)
    generated  = []

    for anchor_idx, raw_pos in enumerate(anchor_positions):
        pos = raw_pos + v_shift
        normal_raw = grp_empty.get('fbxmt_par_t_dir', [1,0,0])
        normal = Vector(normal_raw).normalized()

        # Ray-cast to find opposite wall
        origin = pos
        hit, hit_loc, hit_norm, _, hit_obj, _ = context.scene.ray_cast(
            depsgraph, origin + normal * 0.01, normal, distance=200.0)
        if not hit or hit_obj is None:
            # Try opposite direction
            hit, hit_loc, hit_norm, _, hit_obj, _ = context.scene.ray_cast(
                depsgraph, origin - normal * 0.01, -normal, distance=200.0)
        if not hit:
            continue

        lat_dir = Vector((-t_dir.y, t_dir.x, 0.0)).normalized()

        inset_s_wall = getattr(props, 'par_inset_start', 0.25)
        inset_e_wall = getattr(props, 'par_inset_end',   0.25)

        start_co = pos      - normal * inset_s_wall
        end_co   = hit_loc  + normal * inset_e_wall

        beam_bm = bmesh.new()
        _build_beam(beam_bm, start_co, end_co,
                    props.coving_depth, props.coving_thickness,
                    0.5, 0.5, mat_index=0)
        beam_mesh = bpy.data.meshes.new(f'{grp_name}_Beam')
        if trim_mat:
            beam_mesh.materials.append(trim_mat)
        beam_bm.to_mesh(beam_mesh)
        beam_bm.free()
        beam_mesh.update()

        beam_obj = bpy.data.objects.new(f'{grp_name}_Beam', beam_mesh)
        context.scene.collection.objects.link(beam_obj)

        # Boolean
        mod           = beam_obj.modifiers.new('FBXMT_BoolTrim_Source', 'BOOLEAN')
        mod.operation = 'DIFFERENCE'
        mod.object    = source_obj
        mod.solver    = 'FLOAT'

        # Store gizmo data
        beam_obj['fbxmt_par_start']       = list(pos)
        beam_obj['fbxmt_par_end']         = list(hit_loc)
        beam_obj['fbxmt_par_wall_start']  = list(pos)       # immutable wall-surface anchor
        beam_obj['fbxmt_par_wall_end']    = list(hit_loc)   # immutable wall-surface anchor
        beam_obj['fbxmt_par_source']      = source_name
        beam_obj['fbxmt_par_t_dir']       = list(t_dir)
        beam_obj['fbxmt_par_lat_dir']     = list(lat_dir)
        beam_obj['fbxmt_par_group_empty'] = grp_name
        beam_obj['fbxmt_par_group_idx']   = anchor_idx
        beam_obj['fbxmt_par_group_count'] = n_anchors

        # Parent to existing group empty
        context.view_layer.update()
        world_mat = beam_obj.matrix_world.copy()
        beam_obj.parent = grp_empty
        beam_obj.matrix_parent_inverse = grp_empty.matrix_world.inverted()
        beam_obj.matrix_world = world_mat

        generated.append(beam_obj)

    # Select group empty
    bpy.ops.object.select_all(action='DESELECT')
    grp_empty.select_set(True)
    context.view_layer.objects.active = grp_empty


def _vec_or_none(raw):
    if raw is None:
        return None
    return Vector(raw)



# ---------------------------------------------------------------------------
# Parallel Beam Gizmo Drag — one concrete class per mode
#
# Blender 5.x resets StringProperty defaults on gizmo operator properties
# between setup() and invocation, so a single "mode" string prop is
# unreliable.  Each mode gets its own bl_idname so the mode is baked in
# at the class level and can never be reset.

class _ParBeamDragBase:
    """Mixin — shared invoke / modal / cancel logic for all parallel drag ops."""
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    # Subclasses set _MODE = 'OVERRUN_START' etc.
    _MODE: str = ''

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _accept(obj):
        if obj is None:
            return False
        if obj.type == 'EMPTY' and obj.get('fbxmt_par_group'):
            return True
        return obj.get('fbxmt_par_start') is not None

    def _rebuild(self, context, obj, props):
        mode = self._MODE
        if mode in ('SPAN_INSET_START', 'SPAN_INSET_END'):
            # Lateral offset of first/last beam — shift their stored anchor
            # along span_dir by the inset value, then rebuild mesh in-place.
            if obj.type == 'EMPTY' and obj.get('fbxmt_par_group'):
                span_dir_raw = obj.get('fbxmt_par_span_dir')
                print(f'[DBG] _rebuild  mode={mode}  obj={obj.name}  span_dir_raw={span_dir_raw}')
                if span_dir_raw is None:
                    print('[DBG] _rebuild  ABORT — no fbxmt_par_span_dir')
                    return
                span_dir = Vector(span_dir_raw).normalized()
                span_start_raw = obj.get('fbxmt_par_span_start')
                print(f'[DBG] _rebuild  span_start_raw={span_start_raw}')
                if span_start_raw is None:
                    print('[DBG] _rebuild  ABORT — no fbxmt_par_span_start')
                    return
                span_start = Vector(span_start_raw)
                inset_s = getattr(props, 'par_inset_start', 0.0)
                inset_e = getattr(props, 'par_inset_end',   0.0)
                print(f'[DBG] _rebuild  inset_s={inset_s:.3f}  inset_e={inset_e:.3f}')
                children = [o for o in bpy.data.objects
                            if o.get('fbxmt_par_group_empty') == obj.name]
                n = len(children)
                print(f'[DBG] _rebuild  children={[c.name for c in children]}  n={n}')
                for child in children:
                    idx = child.get('fbxmt_par_group_idx', -1)
                    orig_raw = child.get('fbxmt_par_start')
                    print(f'[DBG] _rebuild  child={child.name}  idx={idx}  orig_start={orig_raw}  fbxmt_par_end={child.get("fbxmt_par_end")}')
                    if orig_raw is None:
                        continue
                    if idx == 0:
                        new_pos = span_start + span_dir * inset_s
                        delta = new_pos - Vector(orig_raw)
                        child['fbxmt_par_start'] = list(new_pos)
                        end_raw = child.get('fbxmt_par_end')
                        if end_raw:
                            child['fbxmt_par_end'] = list(Vector(end_raw) + delta)
                        print(f'[DBG] _rebuild  idx=0  new_start={list(new_pos)}  delta={list(delta)}')
                    elif idx == n - 1:
                        span_end_raw = obj.get('fbxmt_par_span_end')
                        if span_end_raw:
                            span_end = Vector(span_end_raw)
                            new_pos = span_end - span_dir * inset_e
                            delta = new_pos - Vector(orig_raw)
                            child['fbxmt_par_start'] = list(new_pos)
                            end_raw = child.get('fbxmt_par_end')
                            if end_raw:
                                child['fbxmt_par_end'] = list(Vector(end_raw) + delta)
                            print(f'[DBG] _rebuild  idx={idx}(last)  new_start={list(new_pos)}  delta={list(delta)}')
                    result = _par_build_mesh(child, props)
                    print(f'[DBG] _rebuild  _par_build_mesh returned {result}')
                    for mod in child.modifiers:
                        if mod.type == 'BOOLEAN':
                            mod.show_viewport = True
        elif mode in ('OFFSET_V_UP', 'OFFSET_V_DOWN'):
            if obj.type == 'EMPTY' and obj.get('fbxmt_par_group'):
                for child in [o for o in bpy.data.objects
                               if o.get('fbxmt_par_group_empty') == obj.name]:
                    _par_build_mesh(child, props)
                    for mod in child.modifiers:
                        if mod.type == 'BOOLEAN':
                            mod.show_viewport = True
            else:
                _par_build_mesh(obj, props)
                self._restore_bools(obj)
        else:
            _par_build_mesh(obj, props)
            self._restore_bools(obj)

        # VERT modes always rebuild the individual beam in place
        if mode in ('VERT_S0', 'VERT_S1', 'VERT_S2', 'VERT_S3',
                    'VERT_E0', 'VERT_E1', 'VERT_E2', 'VERT_E3'):
            if obj.get('fbxmt_par_start') is not None:
                _par_build_mesh(obj, props)
                self._restore_bools(obj)

    def _restore_bools(self, obj):
        for mod in obj.modifiers:
            if mod.name in self._bool_states:
                mod.show_viewport = self._bool_states[mod.name]

    # ── Blender operator methods ──────────────────────────────────────────────
    def invoke(self, context, event):
        obj = context.active_object
        if not self._accept(obj):
            return {'CANCELLED'}

        import time
        now  = time.time()
        mode = self._MODE

        # Double-click detection via timestamp stored on the object
        # Orange overrun gizmos: double-click toggles vert mode
        if mode in ('OVERRUN_START', 'OVERRUN_END'):
            last_key = '_fbxmt_last_overrun_click'
            last = obj.get(last_key, 0.0)
            obj[last_key] = now
            if now - last < 0.4:
                # Double-click — toggle vert mode
                obj['fbxmt_par_vert_mode'] = not bool(obj.get('fbxmt_par_vert_mode', False))
                for area in context.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
                return {'FINISHED'}

        # Per-vert gizmos: double-click resets that vert to 0
        if mode.startswith(('VERT_S', 'VERT_E')):
            end = 's' if mode.startswith('VERT_S') else 'e'
            idx = int(mode[-1])
            key = f'fbxmt_par_v{end}{idx}'
            last_key = f'_fbxmt_last_vert_click_{key}'
            last = obj.get(last_key, 0.0)
            obj[last_key] = now
            if now - last < 0.4:
                # Double-click — reset this vert
                obj[key] = 0.0
                props = context.scene.fbxmt_props
                _par_build_mesh(obj, props)
                for area in context.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
                return {'FINISHED'}
        props              = context.scene.fbxmt_props
        self._start_x      = event.mouse_x
        self._start_y      = event.mouse_y
        self._last_x       = event.mouse_x
        self._last_y       = event.mouse_y
        self._accum        = 0.0   # single accumulator for all modes
        self._obj_name     = obj.name
        self._orig_os      = getattr(props, 'par_overrun_start',        0.25)
        self._orig_oe      = getattr(props, 'par_overrun_end',          0.25)
        self._orig_is      = getattr(props, 'par_inset_start',          0.0)
        self._orig_ie      = getattr(props, 'par_inset_end',            0.0)
        self._orig_ov      = getattr(props, 'par_offset_v',             0.0)
        self._orig_ol      = getattr(props, 'par_offset_lat',           0.0)
        self._orig_verts   = {}
        for i in range(4):
            self._orig_verts[f'fbxmt_par_vs{i}'] = obj.get(f'fbxmt_par_vs{i}', 0.0)
            self._orig_verts[f'fbxmt_par_ve{i}'] = obj.get(f'fbxmt_par_ve{i}', 0.0)
        self._bool_states  = {}

        # Determine world-space arrow direction for this mode, then project to screen
        from mathutils import Vector
        import bpy_extras.view3d_utils as v3u

        def _vec(raw):
            return Vector(raw).normalized() if raw else None

        # Arrow world directions per mode (sign matches gizmo arrow direction in draw_prepare)
        mode = self._MODE
        arrow_world = None
        if mode == 'OVERRUN_START':
            arrow_world = _vec(obj.get('fbxmt_par_t_dir'))
            if arrow_world: arrow_world = -arrow_world   # gz_os points -t_dir
        elif mode == 'OVERRUN_END':
            arrow_world = _vec(obj.get('fbxmt_par_t_dir'))  # gz_oe points +t_dir
        elif mode == 'INSET_START':
            arrow_world = _vec(obj.get('fbxmt_par_t_dir'))  # gz_is points +t_dir
        elif mode == 'INSET_END':
            arrow_world = _vec(obj.get('fbxmt_par_t_dir'))
            if arrow_world: arrow_world = -arrow_world   # gz_ie points -t_dir
        elif mode == 'SPAN_INSET_START':
            arrow_world = _vec(obj.get('fbxmt_par_span_dir'))  # gz_ss points +span_dir
        elif mode == 'SPAN_INSET_END':
            arrow_world = _vec(obj.get('fbxmt_par_span_dir'))
            if arrow_world: arrow_world = -arrow_world   # gz_se points -span_dir
        elif mode in ('OFFSET_V_UP', 'OFFSET_V_DOWN'):
            arrow_world = Vector((0, 0, 1))              # gz_vu points +Z
        elif mode in ('OFFSET_LAT_A', 'OFFSET_LAT_B'):
            arrow_world = _vec(obj.get('fbxmt_par_lat_dir'))  # gz_la points +lat_dir
        elif mode in ('VERT_S0', 'VERT_S1', 'VERT_S2', 'VERT_S3'):
            arrow_world = _vec(obj.get('fbxmt_par_t_dir'))
            if arrow_world: arrow_world = -arrow_world   # start verts point -t_dir (into wall)
        elif mode in ('VERT_E0', 'VERT_E1', 'VERT_E2', 'VERT_E3'):
            arrow_world = _vec(obj.get('fbxmt_par_t_dir'))   # end verts point +t_dir (into wall)

        # Project arrow into screen space
        self._screen_dir = (1.0, 0.0)  # fallback
        if arrow_world:
            region, rv3d = None, None
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    for r in area.regions:
                        if r.type == 'WINDOW':
                            region = r
                    rv3d = area.spaces.active.region_3d
                    break
            if region and rv3d:
                origin_3d = Vector(obj.matrix_world.translation)
                tip_3d    = origin_3d + arrow_world
                p0 = v3u.location_3d_to_region_2d(region, rv3d, origin_3d)
                p1 = v3u.location_3d_to_region_2d(region, rv3d, tip_3d)
                if p0 and p1:
                    sc = p1 - p0
                    ln = sc.length
                    if ln > 1e-4:
                        self._screen_dir = (sc.x / ln, sc.y / ln)

        print(f'[DBG] invoke  _MODE={self._MODE!r}  obj={obj.name!r}  screen_dir={self._screen_dir}')

        for mod in obj.modifiers:
            if mod.type == 'BOOLEAN':
                self._bool_states[mod.name] = mod.show_viewport
                mod.show_viewport = False
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('SCROLL_X')
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            win    = context.window
            margin = 20
            wx, wy = event.mouse_x, event.mouse_y

            # Project mouse delta onto screen-space arrow direction
            raw_dx   = wx - self._last_x
            raw_dy   = wy - self._last_y
            sd       = self._screen_dir
            frame_d  = raw_dx * sd[0] + raw_dy * sd[1]
            # Modifier keys control both drag sensitivity and snap grid:
            #   Ctrl  = 0.25 m steps   Alt   = 0.5 m steps
            #   none  = 0.1 m steps    Shift = 0.01 m steps
            if event.ctrl:
                snap = 0.25;  scale = 0.005
            elif event.alt:
                snap = 0.5;   scale = 0.005
            elif event.shift:
                snap = 0.01;  scale = 0.0005
            else:
                snap = 0.1;   scale = 0.005
            props    = context.scene.fbxmt_props
            mode     = self._MODE
            grid     = _par_grid_snap(context)

            self._accum += frame_d * scale
            delta = round(self._accum / snap) * snap   # snapped delta for display + value
            hint  = f'Ctrl=0.25m  Alt=0.5m  none=0.1m  Shift=0.01m  |  Esc=cancel'

            if mode == 'OVERRUN_START':
                v = max(0.0, self._orig_os + delta)
                props.par_overrun_start = v
                context.workspace.status_text_set(f'Overrun Start  Δ {delta:+.3f} m    |  {hint}')
            elif mode == 'OVERRUN_END':
                v = max(0.0, self._orig_oe + delta)
                props.par_overrun_end = v
                context.workspace.status_text_set(f'Overrun End  Δ {delta:+.3f} m    |  {hint}')
            elif mode == 'INSET_START':
                v = max(0.0, self._orig_is + delta)
                props.par_endpoint_inset_start = v
                context.workspace.status_text_set(f'Wall Inset Start  Δ {delta:+.3f} m    |  {hint}')
            elif mode == 'INSET_END':
                v = max(0.0, self._orig_ie + delta)
                props.par_endpoint_inset_end = v
                context.workspace.status_text_set(f'Wall Inset End  Δ {delta:+.3f} m    |  {hint}')
            elif mode == 'SPAN_INSET_START':
                v = self._orig_is + delta
                props.par_inset_start = v
                context.workspace.status_text_set(f'First Beam Offset  Δ {delta:+.3f} m    |  {hint}')
                print(f'[DBG] SPAN_INSET_START  frame_d={frame_d:.2f}  accum={self._accum:.4f}  delta={delta:.3f}  prop={v:.3f}')
            elif mode == 'SPAN_INSET_END':
                v = self._orig_ie + delta
                props.par_inset_end = v
                context.workspace.status_text_set(f'Last Beam Offset  Δ {delta:+.3f} m    |  {hint}')
                print(f'[DBG] SPAN_INSET_END    frame_d={frame_d:.2f}  accum={self._accum:.4f}  delta={delta:.3f}  prop={v:.3f}')
            elif mode in ('OFFSET_V_UP', 'OFFSET_V_DOWN'):
                v = self._orig_ov + delta
                props.par_offset_v = v
                context.workspace.status_text_set(f'Vertical Offset  Δ {delta:+.3f} m    |  {hint}')
            elif mode in ('OFFSET_LAT_A', 'OFFSET_LAT_B'):
                v = self._orig_ol + delta
                props.par_offset_lat = v
                context.workspace.status_text_set(f'Lateral Offset  Δ {delta:+.3f} m    |  {hint}')
            elif mode in ('VERT_S0', 'VERT_S1', 'VERT_S2', 'VERT_S3'):
                idx = int(mode[-1])
                key = f'fbxmt_par_vs{idx}'
                orig = self._orig_verts.get(key, 0.0)
                obj  = bpy.data.objects.get(self._obj_name)
                if obj:
                    obj[key] = orig + delta
                context.workspace.status_text_set(f'Start Vert {idx}  Δ {delta:+.3f} m    |  {hint}')
            elif mode in ('VERT_E0', 'VERT_E1', 'VERT_E2', 'VERT_E3'):
                idx = int(mode[-1])
                key = f'fbxmt_par_ve{idx}'
                orig = self._orig_verts.get(key, 0.0)
                obj  = bpy.data.objects.get(self._obj_name)
                if obj:
                    obj[key] = orig + delta
                context.workspace.status_text_set(f'End Vert {idx}  Δ {delta:+.3f} m    |  {hint}')

            # Cursor wrap
            if wx < margin:
                new_x = win.width - margin - 1
                context.window.cursor_warp(new_x, wy)
                self._last_x, self._last_y = new_x, wy
            elif wx > win.width - margin:
                new_x = margin + 1
                context.window.cursor_warp(new_x, wy)
                self._last_x, self._last_y = new_x, wy
            else:
                self._last_x, self._last_y = wx, wy

            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
            return {'RUNNING_MODAL'}

        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            obj = bpy.data.objects.get(self._obj_name)
            if obj:
                props = context.scene.fbxmt_props
                self._rebuild(context, obj, props)
            context.workspace.status_text_set(None)
            context.window.cursor_modal_restore()
            return {'FINISHED'}

        elif event.type in ('RIGHTMOUSE', 'ESC'):
            props = context.scene.fbxmt_props
            props.par_overrun_start             = self._orig_os
            props.par_overrun_end               = self._orig_oe
            props.par_inset_start               = self._orig_is
            props.par_inset_end                 = self._orig_ie
            props.par_endpoint_inset_start      = getattr(self, '_orig_ep_is', props.par_endpoint_inset_start)
            props.par_endpoint_inset_end        = getattr(self, '_orig_ep_ie', props.par_endpoint_inset_end)
            props.par_offset_v                  = self._orig_ov
            props.par_offset_lat                = self._orig_ol
            # Restore per-vert offsets
            obj = bpy.data.objects.get(self._obj_name)
            if obj:
                for k, v in self._orig_verts.items():
                    obj[k] = v
            obj = bpy.data.objects.get(self._obj_name)
            if obj:
                _par_build_mesh(obj, context.scene.fbxmt_props)
                self._restore_bools(obj)
            context.workspace.status_text_set(None)
            context.window.cursor_modal_restore()
            return {'CANCELLED'}

        return {'PASS_THROUGH'}


# One concrete class per gizmo mode — bl_idname is the mode baked in.
class OT_FBXMT_Par_Drag_OverrunStart(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_overrun_start'
    bl_label      = 'Par Beam Overrun Start'
    bl_description = 'Drag to adjust how far this beam overruns its start wall'
    _MODE         = 'OVERRUN_START'

class OT_FBXMT_Par_Drag_OverrunEnd(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_overrun_end'
    bl_label      = 'Par Beam Overrun End'
    bl_description = 'Drag to adjust how far this beam overruns its end wall'
    _MODE         = 'OVERRUN_END'

class OT_FBXMT_Par_Drag_InsetStart(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_inset_start'
    bl_label      = 'Par Beam Inset Start'
    bl_description = 'Drag to adjust how far this beam penetrates its start wall'
    _MODE         = 'INSET_START'

class OT_FBXMT_Par_Drag_InsetEnd(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_inset_end'
    bl_label      = 'Par Beam Inset End'
    bl_description = 'Drag to adjust how far this beam penetrates its end wall'
    _MODE         = 'INSET_END'

class OT_FBXMT_Par_Drag_SpanInsetStart(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_span_inset_start'
    bl_label      = 'Par Beam Span Inset Start'
    bl_description = 'Drag to offset the first beam laterally from the span start edge'
    _MODE         = 'SPAN_INSET_START'

class OT_FBXMT_Par_Drag_SpanInsetEnd(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_span_inset_end'
    bl_label      = 'Par Beam Span Inset End'
    bl_description = 'Drag to offset the last beam laterally from the span end edge'
    _MODE         = 'SPAN_INSET_END'

class OT_FBXMT_Par_Drag_OffsetV(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_offset_v'
    bl_label      = 'Par Beam Vertical Offset'
    bl_description = 'Drag to shift all beams in this group vertically'
    _MODE         = 'OFFSET_V_UP'

class OT_FBXMT_Par_Drag_OffsetLat(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_offset_lat'
    bl_label      = 'Par Beam Lateral Offset'
    bl_description = 'Drag to shift this beam laterally within its span'
    _MODE         = 'OFFSET_LAT_A'

# Per-vert depth gizmo operators (start end v0..v3, end end v0..v3)
class OT_FBXMT_Par_Drag_VertS0(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_vert_s0'
    bl_label      = 'Vert Start 0'
    bl_description = 'Drag to adjust depth of start-end corner v0.  Double-click to reset to 0'
    _MODE         = 'VERT_S0'

class OT_FBXMT_Par_Drag_VertS1(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_vert_s1'
    bl_label      = 'Vert Start 1'
    bl_description = 'Drag to adjust depth of start-end corner v1.  Double-click to reset to 0'
    _MODE         = 'VERT_S1'

class OT_FBXMT_Par_Drag_VertS2(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_vert_s2'
    bl_label      = 'Vert Start 2'
    bl_description = 'Drag to adjust depth of start-end corner v2.  Double-click to reset to 0'
    _MODE         = 'VERT_S2'

class OT_FBXMT_Par_Drag_VertS3(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_vert_s3'
    bl_label      = 'Vert Start 3'
    bl_description = 'Drag to adjust depth of start-end corner v3.  Double-click to reset to 0'
    _MODE         = 'VERT_S3'

class OT_FBXMT_Par_Drag_VertE0(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_vert_e0'
    bl_label      = 'Vert End 0'
    bl_description = 'Drag to adjust depth of end-face corner v0.  Double-click to reset to 0'
    _MODE         = 'VERT_E0'

class OT_FBXMT_Par_Drag_VertE1(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_vert_e1'
    bl_label      = 'Vert End 1'
    bl_description = 'Drag to adjust depth of end-face corner v1.  Double-click to reset to 0'
    _MODE         = 'VERT_E1'

class OT_FBXMT_Par_Drag_VertE2(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_vert_e2'
    bl_label      = 'Vert End 2'
    bl_description = 'Drag to adjust depth of end-face corner v2.  Double-click to reset to 0'
    _MODE         = 'VERT_E2'

class OT_FBXMT_Par_Drag_VertE3(_ParBeamDragBase, bpy.types.Operator):
    bl_idname     = 'fbxmt.par_drag_vert_e3'
    bl_label      = 'Vert End 3'
    bl_description = 'Drag to adjust depth of end-face corner v3.  Double-click to reset to 0'
    _MODE         = 'VERT_E3'


class OT_FBXMT_Par_VertMode_Toggle(bpy.types.Operator):
    """Double-click an orange overrun gizmo to reveal per-vert depth gizmos."""
    bl_idname     = 'fbxmt.par_vert_mode_toggle'
    bl_label      = 'Toggle Per-Vert Gizmos'
    bl_description = 'Double-click to show/hide per-vertex depth gizmos on this beam'
    bl_options    = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.get('fbxmt_par_start') is not None

    def invoke(self, context, event):
        obj = context.active_object
        current = obj.get('fbxmt_par_vert_mode', False)
        obj['fbxmt_par_vert_mode'] = not current
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}



def _par_grid_snap(context):
    """Return grid snap increment — grid_scale divided by subdivisions."""
    try:
        for space in context.area.spaces:
            if space.type == 'VIEW_3D':
                scale = space.overlay.grid_scale or 1.0
                subs  = max(1, getattr(space.overlay, 'grid_subdivisions', 10))
                return scale / subs
    except Exception:
        pass
    return 0.1  # fallback: 10cm


# ---------------------------------------------------------------------------
# Parallel Beam GizmoGroup

class FBXMT_GGT_ParallelBeam(bpy.types.GizmoGroup):
    """Six arrow gizmos on a selected Parallel Beam:
    Orange ×2 — end overrun (along beam axis)
    Blue   ×2 — lateral offset (perp to beam in XY, grid-snapped)
    Green  ×2 — vertical offset (world Z, grid-snapped)
    """
    bl_idname      = 'FBXMT_GGT_ParallelBeam'
    bl_label       = 'Parallel Beam Gizmos'
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options     = {'3D', 'PERSISTENT', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None
                and obj.type == 'MESH'
                and obj.get('fbxmt_par_start') is not None)

    @staticmethod
    def _mat_from_z(direction, location):
        """Build a 4x4 matrix with *direction* as the local +Z axis (arrow axis).

        We want the gizmo to appear upright in the viewport, so we use world-Z
        as the cross-product reference when the arrow direction is horizontal
        (the common case for span/run arrows).  When direction is near world-Z
        (vertical arrows) we fall back to world-Y.
        """
        from mathutils import Matrix, Vector
        d   = Vector(direction).normalized()
        ref = Vector((0, 1, 0)) if abs(d.dot(Vector((0, 0, 1)))) > 0.99               else Vector((0, 0, 1))
        y = d.cross(ref).normalized()
        x = y.cross(d).normalized()
        return Matrix((
            (x.x, y.x, d.x, location.x),
            (x.y, y.y, d.y, location.y),
            (x.z, y.z, d.z, location.z),
            (0,   0,   0,   1),
        ))

    def setup(self, context):
        def _arrow(color, scale=1.0):
            gz = self.gizmos.new('GIZMO_GT_arrow_3d')
            gz.draw_style      = 'NORMAL'
            gz.length          = 1.0
            gz.color           = color
            gz.color_highlight = (1.0, 1.0, 0.2)
            gz.alpha           = 0.4
            gz.alpha_highlight = 1.0
            gz.scale_basis     = 0.5 * scale
            gz.use_draw_modal  = True
            return gz

        # Orange — overrun
        self.gz_os = _arrow((1.0, 0.45, 0.1))
        self.gz_oe = _arrow((1.0, 0.45, 0.1))
        # Purple — wall inset (beam end penetration depth)
        self.gz_is = _arrow((0.8, 0.2, 0.9))
        self.gz_ie = _arrow((0.8, 0.2, 0.9))
        # Blue — lateral
        self.gz_la = _arrow((0.2, 0.55, 1.0), 0.8)
        self.gz_lb = _arrow((0.2, 0.55, 1.0), 0.8)
        # Per-vert depth gizmos — smaller, brighter orange, hidden until vert mode
        self.gz_vs = [_arrow((1.0, 0.65, 0.1), 0.55) for _ in range(4)]
        self.gz_ve = [_arrow((1.0, 0.65, 0.1), 0.55) for _ in range(4)]

        self.gz_os.target_set_operator('fbxmt.par_drag_overrun_start')
        self.gz_oe.target_set_operator('fbxmt.par_drag_overrun_end')
        self.gz_is.target_set_operator('fbxmt.par_drag_inset_start')
        self.gz_ie.target_set_operator('fbxmt.par_drag_inset_end')
        self.gz_la.target_set_operator('fbxmt.par_drag_offset_lat')
        self.gz_lb.target_set_operator('fbxmt.par_drag_offset_lat')
        for i, gz in enumerate(self.gz_vs):
            gz.target_set_operator(f'fbxmt.par_drag_vert_s{i}')
        for i, gz in enumerate(self.gz_ve):
            gz.target_set_operator(f'fbxmt.par_drag_vert_e{i}')

    def draw_prepare(self, context):
        if not hasattr(self, 'gz_os'):
            return
        obj = context.active_object
        if obj is None or obj.get('fbxmt_par_start') is None:
            return

        def _vec(raw):
            from mathutils import Vector
            return Vector(raw) if raw else None

        props   = context.scene.fbxmt_props
        # Use immutable wall anchors for gizmo positioning so they always
        # return to the correct resting spot after a drag.
        start   = _vec(obj.get('fbxmt_par_wall_start') or obj.get('fbxmt_par_start'))
        end     = _vec(obj.get('fbxmt_par_wall_end')   or obj.get('fbxmt_par_end'))
        t_dir   = _vec(obj.get('fbxmt_par_t_dir'))
        lat_dir = _vec(obj.get('fbxmt_par_lat_dir'))
        if not all([start, end, t_dir, lat_dir]):
            return

        t_dir   = t_dir.normalized()
        lat_dir = lat_dir.normalized()
        up      = Vector((0, 0, 1))

        os_val  = getattr(props, 'par_overrun_start', 0.25)
        oe_val  = getattr(props, 'par_overrun_end',   0.25)
        ov_val  = getattr(props, 'par_offset_v',   0.0)
        ol_val  = getattr(props, 'par_offset_lat', 0.0)

        v_shift  = Vector((0, 0, ov_val))
        lat_shift = lat_dir * ol_val

        start_tip = start - t_dir * os_val + v_shift + lat_shift
        end_tip   = end   + t_dir * oe_val + v_shift + lat_shift
        mid       = (start_tip + end_tip) * 0.5

        mf = FBXMT_GGT_ParallelBeam._mat_from_z

        start_face = start + v_shift + lat_shift
        end_face   = end   + v_shift + lat_shift

        # Span inset gizmos — sit at chain extremities offset inward by inset amount
        self.gz_os.matrix_basis = mf(-t_dir,   start_face)
        self.gz_oe.matrix_basis = mf( t_dir,   end_face)
        self.gz_is.matrix_basis = mf( t_dir,   start_face)
        self.gz_ie.matrix_basis = mf(-t_dir,   end_face)

        # Lateral gizmos only on non-extremity beams
        group_idx   = obj.get('fbxmt_par_group_idx',   0)
        group_count = obj.get('fbxmt_par_group_count', 1)
        is_extremity = (group_idx == 0 or group_idx == group_count - 1)
        show_lat = not is_extremity or group_count <= 2
        self.gz_la.hide = not show_lat
        self.gz_lb.hide = not show_lat
        if show_lat:
            self.gz_la.matrix_basis = mf( lat_dir, mid)
            self.gz_lb.matrix_basis = mf(-lat_dir, mid)

        # Per-vert gizmos — shown only when fbxmt_par_vert_mode is True
        vert_mode = bool(obj.get('fbxmt_par_vert_mode', False))
        world_up  = Vector((0, 0, 1))
        h_arm     = t_dir.cross(world_up)
        if h_arm.length < 1e-6:
            h_arm = t_dir.cross(Vector((0, 1, 0)))
        h_arm      = h_arm.normalized()
        wall_down  = -world_up
        depth      = getattr(props, 'coving_depth',     0.1)
        thickness  = getattr(props, 'coving_thickness', 0.1)
        c_off      = h_arm * (thickness * 0.5) + wall_down * (depth * 0.5)

        def _vert_pos(base, offsets, axis_dir, idx):
            A  = base - c_off
            ring = [
                A.copy(),
                A + h_arm * thickness,
                A + h_arm * thickness + wall_down * depth,
                A + wall_down * depth,
            ]
            return ring[idx] + axis_dir * offsets[idx]

        s_offsets = [obj.get(f'fbxmt_par_vs{i}', 0.0) for i in range(4)]
        e_offsets = [obj.get(f'fbxmt_par_ve{i}', 0.0) for i in range(4)]

        for i, gz in enumerate(self.gz_vs):
            gz.hide = not vert_mode
            if vert_mode:
                pos = _vert_pos(start_face, s_offsets, -t_dir, i)
                gz.matrix_basis = mf(-t_dir, pos)
        for i, gz in enumerate(self.gz_ve):
            gz.hide = not vert_mode
            if vert_mode:
                pos = _vert_pos(end_face, e_offsets, t_dir, i)
                gz.matrix_basis = mf(t_dir, pos)


# ---------------------------------------------------------------------------
# Parallel Beam GROUP GizmoGroup (on the par_grp empty)

class FBXMT_GGT_ParallelGroup(bpy.types.GizmoGroup):
    """Gizmos on the par_grp_NNN empty — span inset and vertical offset
    for the whole beam group."""
    bl_idname      = 'FBXMT_GGT_ParallelGroup'
    bl_label       = 'Parallel Beam Group Gizmos'
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options     = {'3D', 'PERSISTENT', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None
                and obj.type == 'EMPTY'
                and obj.get('fbxmt_par_group') is not None)

    @staticmethod
    def _mat_from_z(direction, location):
        """Build a 4x4 matrix with *direction* as the local +Z axis (arrow axis).

        Uses world-Z as the up reference for horizontal arrows (span gizmos)
        and world-Y for vertical arrows, matching QuickBeam/DihedralBeam.
        """
        from mathutils import Matrix, Vector
        d   = Vector(direction).normalized()
        ref = Vector((0, 1, 0)) if abs(d.dot(Vector((0, 0, 1)))) > 0.99               else Vector((0, 0, 1))
        y = d.cross(ref).normalized()
        x = y.cross(d).normalized()
        return Matrix((
            (x.x, y.x, d.x, location.x),
            (x.y, y.y, d.y, location.y),
            (x.z, y.z, d.z, location.z),
            (0,   0,   0,   1),
        ))

    def setup(self, context):
        def _arrow(color, scale=1.0):
            gz = self.gizmos.new('GIZMO_GT_arrow_3d')
            gz.draw_style      = 'NORMAL'
            gz.length          = 1.0
            gz.color           = color
            gz.color_highlight = (1.0, 1.0, 0.2)
            gz.alpha           = 0.4
            gz.alpha_highlight = 1.0
            gz.scale_basis     = 0.6 * scale
            gz.use_draw_modal  = True
            return gz

        # White — span inset at chain extremities
        self.gz_ss = _arrow((0.9, 0.9, 0.9))
        self.gz_se = _arrow((0.9, 0.9, 0.9))
        # Green — vertical offset (taller arrows, offset from span gizmos)
        self.gz_vu = _arrow((0.2, 0.85, 0.35), 1.1)
        self.gz_vd = _arrow((0.2, 0.85, 0.35), 1.1)

        self.gz_ss.target_set_operator('fbxmt.par_drag_span_inset_start')
        self.gz_se.target_set_operator('fbxmt.par_drag_span_inset_end')
        self.gz_vu.target_set_operator('fbxmt.par_drag_offset_v')
        self.gz_vd.target_set_operator('fbxmt.par_drag_offset_v')


    def draw_prepare(self, context):
        if not hasattr(self, 'gz_ss'):
            return
        obj = context.active_object
        if obj is None or not obj.get('fbxmt_par_group'):
            return

        def _vec(raw):
            from mathutils import Vector
            return Vector(raw) if raw else None

        props    = context.scene.fbxmt_props
        span_dir = _vec(obj.get('fbxmt_par_span_dir'))
        raw_ss   = _vec(obj.get('fbxmt_par_span_start'))
        raw_se   = _vec(obj.get('fbxmt_par_span_end'))
        up       = Vector((0, 0, 1))
        mf       = FBXMT_GGT_ParallelGroup._mat_from_z

        ov_val   = getattr(props, 'par_offset_v', 0.0)
        v_shift  = Vector((0, 0, ov_val))

        if span_dir and raw_ss and raw_se:
            span_dir = span_dir.normalized()
            is_val   = getattr(props, 'par_inset_start', 0.0)
            ie_val   = getattr(props, 'par_inset_end',   0.0)
            ss_pos   = raw_ss + span_dir * is_val + v_shift
            se_pos   = raw_se - span_dir * ie_val + v_shift
            face_mid = (raw_ss + raw_se) * 0.5 + v_shift
        else:
            # Fallback — use group empty world position
            loc      = Vector(obj.matrix_world.translation)
            ss_pos   = loc
            se_pos   = loc
            face_mid = loc
            span_dir = Vector((1, 0, 0))

        # White span inset gizmos point along span_dir (beam-spacing direction)
        self.gz_ss.matrix_basis = mf( span_dir, ss_pos)
        self.gz_se.matrix_basis = mf(-span_dir, se_pos)
        # Green vertical gizmos — offset perpendicular to span_dir (along t_dir if
        # available, otherwise a fixed world offset) so they never colocate with the
        # span gizmos when face_mid == ss_pos == se_pos (degenerate span case).
        t_dir_raw = obj.get('fbxmt_par_t_dir')
        if t_dir_raw:
            from mathutils import Vector as _V
            t_off = _V(t_dir_raw).normalized() * 0.18
        else:
            t_off = Vector((0.0, 0.0, 0.0))
        v_mid = face_mid + t_off
        self.gz_vu.matrix_basis = mf( up, v_mid)
        self.gz_vd.matrix_basis = mf(-up, v_mid)


# ---------------------------------------------------------------------------
# Clear All Empties operator

class OT_FBXMT_Clear_All_Empties(bpy.types.Operator):
    """Remove ALL beam anchor empties from the scene (par, spk, crv, dh)."""
    bl_idname  = 'fbxmt.clear_all_empties'
    bl_label   = 'Clear All Beam Empties'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        prefixes = ('par_', 'spk_', 'crv_', 'dh_')
        suffixes = ('1', '2')
        to_remove = [
            o for o in bpy.data.objects
            if o.type == 'EMPTY'
            and any(o.name.startswith(p) for p in prefixes)
            and o.name.rsplit('_', 1)[-1] in suffixes
        ]
        for o in to_remove:
            bpy.data.objects.remove(o, do_unlink=True)
        self.report({'INFO'}, f'{len(to_remove)} beam empty/empties removed')
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Clear Empties Menu

class FBXMT_MT_ClearEmpties(bpy.types.Menu):
    bl_label   = 'Clear Beam Empties'
    bl_idname  = 'FBXMT_MT_ClearEmpties'

    def draw(self, context):
        layout = self.layout
        layout.operator('fbxmt.clear_all_empties',
                        text='Clear All', icon='TRASH')
        layout.separator()
        layout.operator('fbxmt.clear_parallel',
                        text='Clear Parallel (par_)', icon='X')
        layout.operator('fbxmt.clear_spokes',
                        text='Clear Spokes (spk_)', icon='X')
        layout.operator('fbxmt.clear_curve',
                        text='Clear Curve (crv_)', icon='X')
        layout.operator('fbxmt.clear_dihedral',
                        text='Clear Dihedral (dh_)', icon='X')


# ---------------------------------------------------------------------------
# Registration

classes = (
    OT_FBXMT_Quick_Beam,
    OT_FBXMT_Quick_Beam_Refresh,
    OT_FBXMT_Quick_Beam_Gizmo_Drag,
    FBXMT_GGT_QuickBeam,
    OT_FBXMT_Place_Dihedral,
    OT_FBXMT_Generate_Dihedral,
    OT_FBXMT_Clear_Dihedral,
    OT_FBXMT_Dihedral_Beam_Refresh,
    OT_FBXMT_Dihedral_Beam_Gizmo_Drag,
    FBXMT_GGT_DihedralBeam,
    OT_FBXMT_Preview_Dihedral_Ray,
    OT_FBXMT_Place_Parallel,
    OT_FBXMT_Preview_Parallel_Rays,
    OT_FBXMT_Clear_Parallel,
    OT_FBXMT_Place_Spokes,
    OT_FBXMT_Clear_Spokes,
    OT_FBXMT_Place_Curve,
    OT_FBXMT_Clear_Curve,
    OT_FBXMT_Clear_Beams,
)

# ---------------------------------------------------------------------------
# Single-group extraction for parallel beam placement

def _get_single_group(obj):
    """Return (chain, face_centres, face_normals) for the largest selected
    face component.  chain is a flattened angle-sorted vert chain.
    face_centres and face_normals are world-space lists, one per face,
    in the same order as the component faces.

    All BMesh access before bm.free().  Returns ([], [], []) on failure.
    """
    mat     = obj.matrix_world
    mat_inv = mat.inverted_safe().transposed()   # for normal transform
    bm      = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.verts.ensure_lookup_table()

    selected = [f for f in bm.faces if f.select]
    if not selected:
        bm.free()
        return [], [], []

    components = _split_components(selected)
    comp       = components[0]   # largest

    face_centres = [mat @ f.calc_center_median() for f in comp]
    # World-space normals — transform normal by inverse-transpose of mat
    face_normals = [(mat_inv @ f.normal).normalized() for f in comp]

    # Boundary chain facing inward (we only have one group so just take longest)
    boundary = _boundary_edges(comp)
    chains   = _split_boundary_chains(boundary, mat)
    chain    = chains[0] if chains else []

    bm.free()

    chain = _flatten_chain_z(_angle_sort_chain(chain))
    return chain, face_centres, face_normals

def _nearest_face_normal(pos, face_centres, face_normals):
    """Return the world-space normal of the face whose centre is nearest pos."""
    best_d = float('inf')
    best_n = Vector((0, 0, 1))
    for fc, fn in zip(face_centres, face_normals):
        d = (fc - pos).length
        if d < best_d:
            best_d = d
            best_n = fn
    return best_n

