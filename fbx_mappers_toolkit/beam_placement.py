# beam_placement.py — FBX Mapper's Toolkit  [v0.25.1]
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

def _debug_chains(label, chain_a, face_a, chain_b, face_b, n):
    print(f'\n=== FBXMT Beam Debug: {label} ===', file=sys.stderr)
    print(f'Chain A ({len(chain_a)} verts, {len(face_a)} faces):', file=sys.stderr)
    for i, v in enumerate(chain_a):
        print(f'  [{i:02d}] ({v.x:.3f}, {v.y:.3f}, {v.z:.3f})', file=sys.stderr)
    print(f'Chain B ({len(chain_b)} verts, {len(face_b)} faces):', file=sys.stderr)
    for i, v in enumerate(chain_b):
        print(f'  [{i:02d}] ({v.x:.3f}, {v.y:.3f}, {v.z:.3f})', file=sys.stderr)
    pos_a = _sample_chain(chain_a, face_a, n)
    pos_b = _sample_chain(chain_b, face_b, n)
    print(f'Sampled pairs (n={n}):', file=sys.stderr)
    for i, (a, b) in enumerate(zip(pos_a, pos_b)):
        print(f'  pair {i+1}: A=({a.x:.3f},{a.y:.3f},{a.z:.3f})'
              f'  B=({b.x:.3f},{b.y:.3f},{b.z:.3f})', file=sys.stderr)
    print('===', file=sys.stderr)


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

        if debug:
            print(f'\n=== FBXMT Parallel Debug: Place ({n} empties) ===',
                  file=sys.stderr)
            print(f'Chain ({len(chain)} verts):', file=sys.stderr)
            for i, v in enumerate(chain):
                print(f'  [{i:02d}] ({v.x:.3f},{v.y:.3f},{v.z:.3f})',
                      file=sys.stderr)

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
            if debug:
                print(f'  [{i+1}] pos=({pos.x:.3f},{pos.y:.3f},{pos.z:.3f})'
                      f'  normal=({normal.x:.3f},{normal.y:.3f},{normal.z:.3f})',
                      file=sys.stderr)
                _label_empty(ea, i + 1)

        if debug:
            print('===', file=sys.stderr)

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
    bl_label       = 'Place Spoke Beams'
    bl_description = ('Place spk_NNN_1/2 pairs. Hub = smaller group. '
                      'Boundary vert-chain measurement with face-centre snapping.')
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

        bpy.ops.object.mode_set(mode='OBJECT')
        # _get_groups returns larger first — hub is group_b (smaller)
        group_rim, group_hub = _get_groups(obj)
        bpy.ops.object.mode_set(mode='EDIT')

        if group_rim is None or group_hub is None:
            self.report({'WARNING'},
                'Select faces in exactly two disconnected groups.')
            return {'CANCELLED'}

        chain_hub, faces_hub = group_hub
        chain_rim, faces_rim = group_rim

        if debug:
            _debug_chains('Spoke (hub=small, rim=large)',
                          chain_hub, faces_hub, chain_rim, faces_rim, n)

        hub_pos = _sample_chain(chain_hub, faces_hub, n)
        rim_pos = _sample_chain(chain_rim, faces_rim, n)
        v_shift = Vector((0.0, 0.0, offset_v))
        coll    = context.collection
        start   = _next_index('spk')

        source_name = obj.name
        for i in range(n):
            h    = hub_pos[i]
            r    = rim_pos[i]
            gidx = start + i

            if length > 0.0:
                axis = r - h
                dist = axis.length
                t    = axis / dist if dist > 1e-6 else Vector((0, 1, 0))
                p1   = h + t * length if not both else h + t * length
                p2   = r - t * length if both else h + t * length
            else:
                p1 = h
                p2 = r

            ea = _place_empty(f'spk_{gidx:03d}_1', p1 + v_shift, coll, source_name)
            eb = _place_empty(f'spk_{gidx:03d}_2', p2 + v_shift, coll, source_name)
            if debug:
                _label_empty(ea, i + 1)
                _label_empty(eb, i + 1)

        self.report({'INFO'}, f'{n} spoke beam pair(s) placed')
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
    bl_label       = 'Place Curve Beams'
    bl_description = ('Place crv_NNN_1/2 pairs along the midpoint arc. '
                      'Boundary vert-chain measurement, face-centre snapping.')
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode == 'EDIT_MESH'
                and obj is not None and obj.type == 'MESH')

    def execute(self, context):
        props    = context.scene.fbxmt_props
        offset_v = props.crv_offset_v
        debug    = props.beam_debug
        obj      = context.active_object

        bpy.ops.object.mode_set(mode='OBJECT')
        group_a, group_b = _get_groups(obj)
        bpy.ops.object.mode_set(mode='EDIT')

        if group_a is None or group_b is None:
            self.report({'WARNING'},
                'Select faces in exactly two disconnected groups.')
            return {'CANCELLED'}

        chain_a, faces_a = group_a
        chain_b, faces_b = group_b

        # Build n+1 ring positions by lerping directly between the two chains.
        # Chains are sampled at evenly-spaced fractions from t_start to t_end
        # (0.0 = first vert, 1.0 = last vert) with independent inset at each end.
        cum_a   = _build_cum(chain_a)
        cum_b   = _build_cum(chain_b)
        total_a = cum_a[-1]
        total_b = cum_b[-1]

        inset_s = props.crv_inset_start
        inset_e = props.crv_inset_end
        t_start = (inset_s / total_a) if total_a > 1e-8 else 0.0
        t_end   = 1.0 - (inset_e / total_a) if total_a > 1e-8 else 1.0
        t_start = max(0.0, min(t_start, 0.99))
        t_end   = max(t_start + 0.01, min(t_end, 1.0))

        # Ring count derived from face/vert geometry — one ring per vert position
        # on the shorter chain.  No manual count needed.
        n_rings = min(len(chain_a), len(chain_b))
        ring_positions = []
        for j in range(n_rings):
            t     = t_start + j / (n_rings - 1) * (t_end - t_start)
            pos_a = _pos_on_chain(chain_a, cum_a, t * total_a)
            pos_b = _pos_on_chain(chain_b, cum_b, t * total_b)
            ring_positions.append(pos_a.lerp(pos_b, 0.5))

        if len(ring_positions) < 2:
            self.report({'WARNING'}, 'Need at least 2 ring positions.')
            return {'CANCELLED'}

        if debug:
            print(f'\n=== FBXMT Curve Debug: Place Curve (n_rings={n_rings}) ===',
                  file=sys.stderr)
            print(f'Chain A ({len(chain_a)} verts) arc={total_a:.4f}m:',
                  file=sys.stderr)
            for i, v in enumerate(chain_a):
                print(f'  A[{i:02d}] ({v.x:.3f},{v.y:.3f},{v.z:.3f})',
                      file=sys.stderr)
            print(f'Chain B ({len(chain_b)} verts) arc={total_b:.4f}m:',
                  file=sys.stderr)
            for i, v in enumerate(chain_b):
                print(f'  B[{i:02d}] ({v.x:.3f},{v.y:.3f},{v.z:.3f})',
                      file=sys.stderr)
            print(f'Inset: start={inset_s:.3f}m end={inset_e:.3f}m '
                  f't_start={t_start:.4f} t_end={t_end:.4f}',
                  file=sys.stderr)
            print(f'Ring positions ({len(ring_positions)}):',  file=sys.stderr)
            for i, p in enumerate(ring_positions):
                print(f'  ring[{i:02d}] ({p.x:.3f},{p.y:.3f},{p.z:.3f})',
                      file=sys.stderr)
            print(f'Placed pairs:', file=sys.stderr)

        v_shift = Vector((0.0, 0.0, offset_v))
        coll    = context.collection
        start   = _next_index('crv')

        source_name = obj.name
        for i in range(len(ring_positions) - 1):
            gidx = start + i
            p1   = ring_positions[i]     + v_shift
            p2   = ring_positions[i + 1] + v_shift
            ea   = _place_empty(f'crv_{gidx:03d}_1', p1, coll, source_name)
            eb   = _place_empty(f'crv_{gidx:03d}_2', p2, coll, source_name)
            if debug:
                print(f'  seg[{i+1:02d}]: _1=({p1.x:.3f},{p1.y:.3f},{p1.z:.3f})'
                      f'  _2=({p2.x:.3f},{p2.y:.3f},{p2.z:.3f})',
                      file=sys.stderr)
                _label_empty(ea, i + 1)
                _label_empty(eb, i + 1)

        if debug:
            print('===', file=sys.stderr)

        self.report({'INFO'}, f'{len(ring_positions)-1} curve segment(s) placed')
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
# Operator: Quick Beam

class OT_FBXMT_Quick_Beam(bpy.types.Operator):
    bl_idname      = 'fbxmt.quick_beam'
    bl_label       = 'Quick Beam'
    bl_description = ('Select exactly 2 verts, edges, or faces (any mix). '
                      'Places a beam between their centres immediately — '
                      'no empties. Boolean trim modifier added against the '
                      'active object. Selection cleared after generation.')
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
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        # Determine the selection mode from actual selected element counts,
        # ignoring the cascading selection that Blender applies (selected face
        # marks all its edges/verts as selected too).
        sel_faces = [f for f in bm.faces if f.select]
        # Edges truly selected by the user = selected but not a boundary of a selected face
        face_edge_indices = {e.index for f in sel_faces for e in f.edges}
        sel_edges = [e for e in bm.edges
                     if e.select and e.index not in face_edge_indices]
        # Verts truly selected = selected but not part of any selected face or edge
        face_vert_indices = {v.index for f in sel_faces for v in f.verts}
        edge_vert_indices = {v.index for e in sel_edges for v in e.verts}
        sel_verts = [v for v in bm.verts
                     if v.select
                     and v.index not in face_vert_indices
                     and v.index not in edge_vert_indices]

        elements = []
        for f in sel_faces:
            elements.append(mat @ f.calc_center_median())
        for e in sel_edges:
            elements.append(mat @ ((e.verts[0].co + e.verts[1].co) / 2))
        for v in sel_verts:
            elements.append(mat @ v.co)

        bm.free()

        if len(elements) < 2:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'WARNING'},
                'Select exactly 2 elements (verts, edges, or faces).')
            return {'CANCELLED'}

        start_co = Vector(elements[0])
        end_co   = Vector(elements[1])

        # Overrun — extend both ends outward for boolean
        pullback = 0.25
        axis     = end_co - start_co
        length   = axis.length
        if length < 1e-4:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'WARNING'}, 'Selected elements are coincident.')
            return {'CANCELLED'}
        t_dir    = axis / length
        start_co = start_co - t_dir * pullback
        end_co   = end_co   + t_dir * pullback

        # Build beam mesh
        props     = context.scene.fbxmt_props
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
        context.collection.objects.link(beam_obj)
        move_to_collection(beam_obj, COLLECTION_TRIM)

        # Boolean trim — source is the mesh being edited
        if source_obj and source_obj.type == 'MESH':
            mod = beam_obj.modifiers.new(name='FBXMT_BoolTrim', type='BOOLEAN')
            mod.operation = 'DIFFERENCE'
            mod.object    = source_obj
            mod.solver    = 'FLOAT'
            # Modifier left in stack for fine-tuning

        # Clear selection to prevent accidental multi-click
        bpy.ops.object.select_all(action='DESELECT')

        self.report({'INFO'}, 'Quick beam generated')
        return {'FINISHED'}


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

class OT_FBXMT_Preview_Parallel_Rays(bpy.types.Operator):
    bl_idname      = 'fbxmt.preview_parallel_rays'
    bl_label       = 'Preview Rays'
    bl_description = 'Recompute and display ray-cast preview for all par_NNN_1 empties'
    bl_options     = {'REGISTER'}

    def execute(self, context):
        try:
            from .par_ray_preview import invalidate_par_cache, _update_cache
            invalidate_par_cache()

            # ray_cast requires Object Mode — temporarily switch if needed
            was_edit = context.mode == 'EDIT_MESH'
            if was_edit:
                bpy.ops.object.mode_set(mode='OBJECT')

            context.view_layer.update()
            _update_cache(context.evaluated_depsgraph_get())

            if was_edit:
                bpy.ops.object.mode_set(mode='EDIT')

        except Exception as e:
            self.report({'WARNING'}, f'Ray preview failed: {e}')
            return {'CANCELLED'}

        # Force all 3D viewports to redraw
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
                    for region in area.regions:
                        if region.type == 'WINDOW':
                            region.tag_redraw()

        self.report({'INFO'}, 'Ray preview updated')
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration

classes = (
    OT_FBXMT_Quick_Beam,
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


