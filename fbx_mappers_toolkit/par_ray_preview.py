# par_ray_preview.py — FBX Mapper's Toolkit  [v0.25.0]
#
# Viewport overlay that draws parallel beam ray previews.
#
# For each par_NNN_1 empty in the scene:
#   - Reads stored fbxmt_normal and fbxmt_source
#   - Runs _smart_raycast to find the hit path (including pass-through segments)
#   - Draws the ray path in red with a dot at the terminus
#
# Results are cached per-empty based on world position + stored normal.
# Cache is invalidated only when a position or normal changes — cheap per-frame.
#
# Registration:
#   call register_par_preview()  / unregister_par_preview()
#   call invalidate_par_cache()  when empties are added/removed

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

# ---------------------------------------------------------------------------
# Cache

_ray_cache       = {}   # empty_name → {'pos': Vector, 'normal': tuple,
                         #               'segments': [(start,end), ...],
                         #               'terminus': Vector or None}
_draw_handle     = None
_NUDGE           = 0.02
_PARALLEL_THRESH = 0.1
_MAX_ITER        = 32
_RAY_MAX         = 100.0


def invalidate_par_cache():
    """Clear all cached ray results — call after adding/removing empties."""
    _ray_cache.clear()


def _collect_ray_path(source_obj, ray_origin, ray_dir):
    """Run smart ray-cast and return (segments, terminus).

    segments: list of (Vector, Vector) world-space start/end pairs
    terminus: Vector of final hit, or None
    """
    from mathutils import Vector

    mat_inv = source_obj.matrix_world.inverted()
    rot_inv = mat_inv.to_3x3()

    origin    = Vector(ray_origin) + Vector(ray_dir) * _NUDGE
    direction = Vector(ray_dir).normalized()

    segments = []
    prev     = Vector(ray_origin)

    for _ in range(_MAX_ITER):
        local_orig = mat_inv @ origin
        local_dir  = (rot_inv @ direction).normalized()

        hit, loc, normal, face_idx = source_obj.ray_cast(
            local_orig, local_dir, distance=_RAY_MAX)

        if not hit:
            return segments, None

        world_normal = (source_obj.matrix_world.to_3x3() @ normal).normalized()
        world_loc    = source_obj.matrix_world @ loc

        dot = abs(direction.dot(world_normal))

        segments.append((prev.copy(), world_loc.copy()))

        if dot >= _PARALLEL_THRESH:
            # Terminator hit
            return segments, world_loc.copy()

        # Pass-through — continue from here
        prev   = world_loc.copy()
        origin = world_loc + direction * _NUDGE

    return segments, None


def _get_par_anchors():
    """Return sorted list of par_NNN_1 empties."""
    return sorted(
        [o for o in bpy.data.objects
         if o.type == 'EMPTY'
         and o.name.startswith('par_')
         and o.name.rsplit('_', 1)[-1] == '1'],
        key=lambda o: o.name
    )


def _update_cache(depsgraph=None):
    """Recompute ray paths for any anchor whose position/normal has changed."""
    anchors = _get_par_anchors()
    if not anchors:
        _ray_cache.clear()
        return

    current_names = {a.name for a in anchors}

    # Remove stale entries
    for name in list(_ray_cache.keys()):
        if name not in current_names:
            del _ray_cache[name]

    for anchor in anchors:
        pos    = anchor.matrix_world.translation.copy()
        raw_n  = anchor.get('fbxmt_normal', None)
        if raw_n is None:
            continue
        normal = tuple(raw_n)

        cached = _ray_cache.get(anchor.name)
        if (cached is not None
                and (cached['pos'] - pos).length < 1e-5
                and cached['normal'] == normal):
            continue   # no change — keep cached result

        # Recompute
        source_name = anchor.get('fbxmt_source', '')
        source_obj  = bpy.data.objects.get(source_name) if source_name else None

        if source_obj is None or source_obj.type != 'MESH':
            _ray_cache[anchor.name] = {
                'pos': pos, 'normal': normal,
                'segments': [], 'terminus': None,
            }
            continue

        import sys as _sys
        ray_dir = Vector(normal).normalized()
        print(f'FBXMT cache: {anchor.name} source={source_obj.name} '
              f'pos=({pos.x:.2f},{pos.y:.2f},{pos.z:.2f}) '
              f'dir=({ray_dir.x:.2f},{ray_dir.y:.2f},{ray_dir.z:.2f})',
              file=_sys.stderr)

        # Use evaluated object so ray_cast works against final mesh geometry
        try:
            dg       = depsgraph or bpy.context.evaluated_depsgraph_get()
            eval_obj = source_obj.evaluated_get(dg)
        except Exception:
            eval_obj = source_obj

        segments, terminus = _collect_ray_path(source_obj, pos, ray_dir)

        # Only cache successful results — failed ones retry next frame
        if segments or terminus is not None:
            _ray_cache[anchor.name] = {
                'pos':      pos,
                'normal':   normal,
                'segments': segments,
                'terminus': terminus,
            }
        else:
            # Store failed attempt with pos/normal so we don't spam retries
            # on every frame — retry only when position changes
            _ray_cache[anchor.name] = {
                'pos':      pos,
                'normal':   normal,
                'segments': [],
                'terminus': None,
            }


# ---------------------------------------------------------------------------
# Draw callback

def _draw_par_rays():
    """GPU draw callback — draws ray paths and terminus dots."""
    try:
        _update_cache()
        if not _ray_cache:
            return

        # Collect line verts and point verts
        line_verts   = []
        point_verts  = []

        for name, data in _ray_cache.items():
            for seg_start, seg_end in data['segments']:
                line_verts.append(seg_start)
                line_verts.append(seg_end)
            if data['terminus'] is not None:
                point_verts.append(data['terminus'])

        if not line_verts and not point_verts:
            return

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(2.0)
        gpu.state.point_size_set(8.0)

        shader.bind()
        shader.uniform_float('color', (1.0, 0.1, 0.1, 0.9))

        if line_verts:
            batch = batch_for_shader(
                shader, 'LINES',
                {'pos': [v[:] for v in line_verts]}
            )
            batch.draw(shader)

        if point_verts:
            batch = batch_for_shader(
                shader, 'POINTS',
                {'pos': [v[:] for v in point_verts]}
            )
            batch.draw(shader)

        gpu.state.blend_set('NONE')
        gpu.state.line_width_set(1.0)

    except Exception as e:
        import sys
        print(f'FBXMT par_ray_preview draw error: {e}', file=sys.stderr)


# ---------------------------------------------------------------------------
# Registration

def register_par_preview():
    global _draw_handle
    if _draw_handle is not None:
        return
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
        _draw_par_rays, (), 'WINDOW', 'POST_VIEW'
    )
    import sys
    print('FBXMT: par_ray_preview draw handler registered', file=sys.stderr)


def unregister_par_preview():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    _ray_cache.clear()
