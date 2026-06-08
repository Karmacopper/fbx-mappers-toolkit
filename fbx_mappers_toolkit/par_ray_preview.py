# par_ray_preview.py — FBX Mapper's Toolkit  [v0.26.6]
#
# Viewport overlay drawing ray-cast previews for:
#
#   Parallel beams  — one ray per par_NNN_1 empty (red)
#   Dihedral beams  — one ray per DihedralBeam mesh with fbxmt_dh_v0 (orange)
#
# Both types share a single draw handler and a single cache dict keyed by a
# unique string ("par:<name>" or "dh:<name>").
#
# Cache invalidation:
#   Parallel — position + stored normal hash
#   Dihedral — v0 + v1 + bisector hash (all stored as custom props)
#
# Registration:
#   register_par_preview()   / unregister_par_preview()
#   invalidate_par_cache()   — clears all entries (par + dihedral)
#   invalidate_dh_cache()    — clears dihedral entries only

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

# ---------------------------------------------------------------------------
# Shared constants

_ray_cache       = {}   # key → {'hash': str, 'segments': [...], 'terminus': V|None}
_draw_handle     = None
_NUDGE           = 0.02
_PARALLEL_THRESH = 0.1
_MAX_ITER        = 32
_RAY_MAX         = 100.0


# ---------------------------------------------------------------------------
# Cache invalidation

def invalidate_par_cache():
    """Clear all cached ray results (parallel + dihedral)."""
    _ray_cache.clear()


def invalidate_dh_cache():
    """Clear only dihedral beam cache entries."""
    for k in list(_ray_cache.keys()):
        if k.startswith('dh:'):
            del _ray_cache[k]


# ---------------------------------------------------------------------------
# Core ray-path collector (shared)

def _collect_ray_path(source_obj, ray_origin, ray_dir):
    """Smart ray-cast returning (segments, terminus).

    segments : list of (Vector, Vector) world-space pairs
    terminus : Vector of final perpendicular hit, or None
    """
    mat_inv   = source_obj.matrix_world.inverted()
    rot_inv   = mat_inv.to_3x3()
    origin    = Vector(ray_origin) + Vector(ray_dir) * _NUDGE
    direction = Vector(ray_dir).normalized()
    segments  = []
    prev      = Vector(ray_origin)

    for _ in range(_MAX_ITER):
        local_orig = mat_inv @ origin
        local_dir  = (rot_inv @ direction).normalized()
        hit, loc, normal, _ = source_obj.ray_cast(
            local_orig, local_dir, distance=_RAY_MAX)
        if not hit:
            return segments, None
        world_normal = (source_obj.matrix_world.to_3x3() @ normal).normalized()
        world_loc    = source_obj.matrix_world @ loc
        dot          = abs(direction.dot(world_normal))
        segments.append((prev.copy(), world_loc.copy()))
        if dot >= _PARALLEL_THRESH:
            return segments, world_loc.copy()
        prev   = world_loc.copy()
        origin = world_loc + direction * _NUDGE

    return segments, None


# ---------------------------------------------------------------------------
# Parallel beam cache update

def _get_par_anchors():
    return sorted(
        [o for o in bpy.data.objects
         if o.type == 'EMPTY'
         and o.name.startswith('par_')
         and o.name.rsplit('_', 1)[-1] == '1'],
        key=lambda o: o.name,
    )


def _update_par_cache(depsgraph):
    anchors = _get_par_anchors()

    # Remove stale par entries
    current_keys = {'par:' + a.name for a in anchors}
    for k in list(_ray_cache.keys()):
        if k.startswith('par:') and k not in current_keys:
            del _ray_cache[k]

    for anchor in anchors:
        key    = 'par:' + anchor.name
        pos    = anchor.matrix_world.translation.copy()
        raw_n  = anchor.get('fbxmt_normal')
        if raw_n is None:
            continue
        normal = tuple(raw_n)
        props_  = bpy.context.scene.fbxmt_props
        depth_  = getattr(props_, 'coving_depth',     0.1)
        thick_  = getattr(props_, 'coving_thickness', 0.1)
        h      = f'{pos.x:.4f},{pos.y:.4f},{pos.z:.4f}|{normal}|{depth_:.4f},{thick_:.4f}'

        cached = _ray_cache.get(key)
        if cached and cached.get('hash') == h:
            continue

        source_name = anchor.get('fbxmt_source', '')
        source_obj  = bpy.data.objects.get(source_name) if source_name else None
        if source_obj is None or source_obj.type != 'MESH':
            _ray_cache[key] = {'hash': h, 'segments': [], 'terminus': None}
            continue

        # 4-corner profile rays
        depth      = depth_
        thickness  = thick_
        ray_dir    = Vector(normal).normalized()

        # Lateral direction: perpendicular to ray_dir in XY plane
        lat = Vector((-ray_dir.y, ray_dir.x, 0.0))
        if lat.length < 1e-6:
            lat = Vector((0.0, 1.0, 0.0))
        lat.normalize()
        up = Vector((0.0, 0.0, 1.0))

        half_d = depth     * 0.5
        half_t = thickness * 0.5

        corners = [
            pos + lat *  half_d + up *  half_t,
            pos + lat *  half_d + up * -half_t,
            pos + lat * -half_d + up *  half_t,
            pos + lat * -half_d + up * -half_t,
        ]

        all_segs  = []
        start_pts = []
        end_pts   = []

        for corner in corners:
            segs, term = _collect_ray_path(source_obj, corner, ray_dir)
            all_segs.extend(segs)
            start_pts.append(corner)
            if term is not None:
                end_pts.append(term)

        # Build quad outlines at start and end faces
        quad_lines = []
        def _quad_edges(pts):
            if len(pts) == 4:
                order = [0, 1, 3, 2]  # corners in order: TL TR BR BL
                for i in range(4):
                    quad_lines.append(pts[order[i]])
                    quad_lines.append(pts[order[(i+1) % 4]])

        _quad_edges(start_pts)
        _quad_edges(end_pts)

        _ray_cache[key] = {
            'hash':      h,
            'segments':  all_segs,
            'terminus':  end_pts[0] if end_pts else None,
            'quads':     quad_lines,
        }


# ---------------------------------------------------------------------------
# Dihedral beam cache update

def _get_dh_anchors():
    """Return sorted list of dh_NNN_1 empties — mirrors _get_par_anchors."""
    return sorted(
        [o for o in bpy.data.objects
         if o.type == 'EMPTY'
         and o.name.startswith('dh_')
         and o.name.rsplit('_', 1)[-1] == '1'],
        key=lambda o: o.name,
    )


def _update_dh_cache(depsgraph):
    anchors = _get_dh_anchors()

    # Remove stale dh entries
    current_keys = {'dh:' + a.name for a in anchors}
    for k in list(_ray_cache.keys()):
        if k.startswith('dh:') and k not in current_keys:
            del _ray_cache[k]

    for anchor in anchors:
        key = 'dh:' + anchor.name
        pos = anchor.matrix_world.translation.copy()

        # anchor stores v0/v1/bisector as IDPropertyArray (list of floats)
        raw_v0  = anchor.get('fbxmt_dh_v0')
        raw_v1  = anchor.get('fbxmt_dh_v1')
        raw_bis = anchor.get('fbxmt_dh_bisector')
        if raw_v0 is None or raw_v1 is None or raw_bis is None:
            continue

        h = f'{list(raw_v0)}|{list(raw_v1)}|{list(raw_bis)}'
        cached = _ray_cache.get(key)
        if cached and cached.get('hash') == h:
            continue

        try:
            v0  = Vector(raw_v0)
            v1  = Vector(raw_v1)
            bis = Vector(raw_bis).normalized()
        except Exception:
            _ray_cache[key] = {'hash': h, 'segments': [], 'terminus': None}
            continue

        source_name = anchor.get('fbxmt_source', '')
        source_obj  = bpy.data.objects.get(source_name) if source_name else None
        if source_obj is None or source_obj.type != 'MESH':
            _ray_cache[key] = {'hash': h, 'segments': [], 'terminus': None}
            continue

        edge_mid   = (v0 + v1) * 0.5
        is_concave = bool(anchor.get('fbxmt_dh_concave', 1))

        # Trim outputs always in +bisector (outward normal) space.
        segs, term = _collect_ray_path(source_obj, edge_mid, bis)
        if not segs:
            # No opposite surface — draw 0.5m outward indicator
            tip  = edge_mid + bis * 0.5
            segs = [(edge_mid.copy(), tip)]
            term = tip

        _ray_cache[key] = {'hash': h, 'segments': segs, 'terminus': term}


# ---------------------------------------------------------------------------
# Combined cache update
#
# IMPORTANT: _update_dh_cache() uses ray_cast on mesh objects.  ray_cast
# returns no results when the source object is in Edit Mode (BMesh not flushed).
# Therefore _update_dh_cache() must ONLY be called from an operator that has
# already switched to Object Mode — never from the draw handler.
#
# The draw handler calls _update_par_cache() (empties are always in Object
# data, unaffected by mode) and draws dh entries from whatever the operator
# last computed and stored in the cache.

def _update_cache(depsgraph=None):
    """Update par and dh caches — both safe to call from draw handler.
    Both read from empties (always in Object data regardless of mode).
    ray_cast on the SOURCE mesh is the only mode-sensitive op, but empties
    store enough data to compute the ray without touching the source in Edit Mode
    — _collect_ray_path uses the source object's evaluated mesh which Blender
    keeps available even when that object is active in Edit Mode.
    """
    try:
        dg = depsgraph or bpy.context.evaluated_depsgraph_get()
    except Exception:
        dg = None
    _update_par_cache(dg)
    _update_dh_cache(dg)


# ---------------------------------------------------------------------------
# Draw callback

def _draw_par_rays():
    try:
        _update_cache()
        if not _ray_cache:
            return

        par_lines  = []
        dh_lines   = []
        dh_pts     = []

        par_quads = []
        for key, data in _ray_cache.items():
            is_dh = key.startswith('dh:')
            for seg_s, seg_e in data['segments']:
                if is_dh:
                    dh_lines.append(seg_s)
                    dh_lines.append(seg_e)
                else:
                    par_lines.append(seg_s)
                    par_lines.append(seg_e)
            if data['terminus'] is not None:
                if is_dh and data['terminus'] is not None:
                    dh_pts.append(data['terminus'])
            if not is_dh and data.get('quads'):
                par_quads.extend(data['quads'])

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(2.0)
        gpu.state.point_size_set(8.0)
        shader.bind()

        # Parallel rays — red
        if par_lines:
            shader.uniform_float('color', (1.0, 0.15, 0.15, 0.9))
            batch_for_shader(shader, 'LINES',
                {'pos': [v[:] for v in par_lines]}).draw(shader)

        # Parallel profile quads — brighter red outline
        if par_quads:
            shader.uniform_float('color', (1.0, 0.45, 0.45, 0.7))
            batch_for_shader(shader, 'LINES',
                {'pos': [v[:] for v in par_quads]}).draw(shader)

        # Dihedral rays — orange
        if dh_lines or dh_pts:
            shader.uniform_float('color', (1.0, 0.55, 0.1, 0.9))
            if dh_lines:
                batch_for_shader(shader, 'LINES',
                    {'pos': [v[:] for v in dh_lines]}).draw(shader)
            if dh_pts:
                batch_for_shader(shader, 'POINTS',
                    {'pos': [v[:] for v in dh_pts]}).draw(shader)

        gpu.state.blend_set('NONE')
        gpu.state.line_width_set(1.0)

    except Exception as e:
        import sys
        print(f'FBXMT ray_preview draw error: {e}', file=sys.stderr)


# ---------------------------------------------------------------------------
# Registration

def register_par_preview():
    global _draw_handle
    if _draw_handle is not None:
        return
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
        _draw_par_rays, (), 'WINDOW', 'POST_VIEW'
    )


def unregister_par_preview():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    _ray_cache.clear()
