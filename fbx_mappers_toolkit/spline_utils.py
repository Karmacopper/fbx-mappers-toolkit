# spline_utils.py — FBX Mappers Toolkit
#
# Self-contained Catmull-Rom spline utilities.
# No dependency on external addons — safe to use anywhere in the toolkit.
#
# Public API
# ----------
#   catmull_rom_resample(points, n_segments, closed=False, alpha=0.5)
#       -> list[Vector]
#
#   Operator: FBXMT_OT_CurveCleaner  (bl_idname: 'fbxmt.curve_cleaner')

from __future__ import annotations

import math
from mathutils import Vector
from typing import List

# ---------------------------------------------------------------------------
# Catmull-Rom spline

def _cr_segment(p0: Vector, p1: Vector, p2: Vector, p3: Vector,
                t0: float, t1: float, t2: float, t3: float,
                n_samples: int) -> List[Vector]:
    """Sample one Catmull-Rom segment from p1 to p2 (centripetal parameterisation).

    Returns n_samples points NOT including the endpoint p2,
    so segments can be concatenated without duplicates.
    """
    pts = []
    for i in range(n_samples):
        t = t1 + (t2 - t1) * i / n_samples

        # Barry-Goldman algorithm
        def _lerp(a, b, ta, tb):
            if abs(tb - ta) < 1e-10:
                return Vector(a)
            return Vector(a) + (Vector(b) - Vector(a)) * ((t - ta) / (tb - ta))

        a1 = _lerp(p0, p1, t0, t1)
        a2 = _lerp(p1, p2, t1, t2)
        a3 = _lerp(p2, p3, t2, t3)
        b1 = _lerp(a1, a2, t0, t2)
        b2 = _lerp(a2, a3, t1, t3)
        pts.append(_lerp(b1, b2, t1, t2))
    return pts


def _knot(t_prev: float, p_prev: Vector, p_next: Vector, alpha: float) -> float:
    """Centripetal knot interval."""
    d = (p_next - p_prev).length
    return t_prev + (d ** alpha if d > 1e-10 else 1e-10)


def catmull_rom_resample(points: List[Vector],
                          n_segments: int,
                          closed: bool = False,
                          alpha: float = 0.5) -> List[Vector]:
    """Resample a polyline through n_segments+1 evenly-spaced points.

    Uses centripetal Catmull-Rom parameterisation (alpha=0.5 by default)
    which avoids cusps on unevenly-spaced control points.

    Endpoints are always preserved exactly.
    Requires at least 2 control points; returns input unchanged if < 2.

    Args:
        points:     Ordered list of mathutils.Vector control points.
        n_segments: Number of output segments (output has n_segments+1 points).
        closed:     If True, treat the polyline as a closed loop.
        alpha:      0.0 = uniform, 0.5 = centripetal (default), 1.0 = chordal.

    Returns:
        List of n_segments+1 Vectors evenly spaced along the spline arc.
    """
    n = len(points)
    if n < 2:
        return list(points)
    if n == 2:
        # Degenerate — just lerp
        return [points[0].lerp(points[1], k / n_segments)
                for k in range(n_segments + 1)]

    # Build extended control point list with phantom endpoints
    if closed:
        ext = [points[-1]] + list(points) + [points[0], points[1]]
    else:
        # Phantom points: mirror second and penultimate
        p_start = points[0] + (points[0] - points[1])
        p_end   = points[-1] + (points[-1] - points[-2])
        ext = [p_start] + list(points) + [p_end]

    # Compute knots for extended list
    knots = [0.0]
    for i in range(1, len(ext)):
        knots.append(_knot(knots[-1], ext[i-1], ext[i], alpha))

    # Dense sample count per segment (higher = more accurate arc-length integration)
    _DENSE = 64

    # Sample all segments densely to build arc-length table
    dense_pts  = []
    dense_arcs = [0.0]

    n_segs_ext = len(ext) - 3  # number of CR segments (p1..p_{n-2})
    for si in range(n_segs_ext):
        i = si + 1  # index of p1 in ext
        seg_pts = _cr_segment(
            ext[i-1], ext[i], ext[i+1], ext[i+2],
            knots[i-1], knots[i], knots[i+1], knots[i+2],
            _DENSE,
        )
        for pt in seg_pts:
            if dense_pts:
                dense_arcs.append(dense_arcs[-1] + (pt - dense_pts[-1]).length)
            else:
                dense_arcs.append(0.0)
            dense_pts.append(pt)

    # Append true last point
    last = ext[-2]
    dense_arcs.append(dense_arcs[-1] + (last - dense_pts[-1]).length if dense_pts else 0.0)
    dense_pts.append(last)

    total_arc = dense_arcs[-1]
    if total_arc < 1e-8:
        return [points[0]] * (n_segments + 1)

    # Resample at even arc-length intervals, pinning endpoints
    result = [Vector(points[0])]
    j = 0
    for k in range(1, n_segments):
        target = total_arc * k / n_segments
        while j < len(dense_arcs) - 2 and dense_arcs[j+1] < target:
            j += 1
        seg_len = dense_arcs[j+1] - dense_arcs[j]
        f = (target - dense_arcs[j]) / seg_len if seg_len > 1e-10 else 0.0
        result.append(dense_pts[j].lerp(dense_pts[j+1], f))
    result.append(Vector(points[-1]))

    return result


# ---------------------------------------------------------------------------
# Curve Cleaner operator

import bpy
import bmesh as _bmesh

def _chain_from_edges(bm) -> List[Vector] | None:
    """Order selected edges into a continuous chain, return world-space verts."""
    sel_edges = [e for e in bm.edges if e.select]
    if not sel_edges:
        return None

    # Build adjacency
    adj: dict[int, list] = {}
    for e in sel_edges:
        for v in e.verts:
            adj.setdefault(v.index, []).append(e)

    # Find an endpoint (valence 1) or start anywhere for a loop
    start_v = next((v for v in bm.verts if v.select and len(adj.get(v.index, [])) == 1),
                   sel_edges[0].verts[0])

    visited_e: set[int] = set()
    chain_v: list = [start_v]
    cur_v = start_v
    while True:
        nexts = [e for e in adj.get(cur_v.index, []) if e.index not in visited_e]
        if not nexts:
            break
        e = nexts[0]
        visited_e.add(e.index)
        cur_v = e.other_vert(cur_v)
        chain_v.append(cur_v)

    return chain_v  # list of BMVerts


def _chain_from_verts(bm) -> List | None:
    """Order selected verts into a chain by nearest-neighbour."""
    sel_verts = [v for v in bm.verts if v.select]
    if len(sel_verts) < 2:
        return None

    # Use edge connectivity if available, else nearest-neighbour
    sel_set = {v.index for v in sel_verts}
    adj: dict[int, list] = {}
    for e in bm.edges:
        if e.verts[0].index in sel_set and e.verts[1].index in sel_set:
            adj.setdefault(e.verts[0].index, []).append(e.verts[1])
            adj.setdefault(e.verts[1].index, []).append(e.verts[0])

    if adj:
        # Walk connected
        start = next((v for v in sel_verts if len(adj.get(v.index, [])) == 1),
                     sel_verts[0])
        visited: set[int] = {start.index}
        chain = [start]
        cur = start
        while True:
            nexts = [v for v in adj.get(cur.index, []) if v.index not in visited]
            if not nexts:
                break
            cur = nexts[0]
            visited.add(cur.index)
            chain.append(cur)
        return chain
    else:
        # Nearest-neighbour fallback
        remaining = list(sel_verts)
        chain = [remaining.pop(0)]
        while remaining:
            last = chain[-1].co
            nearest = min(remaining, key=lambda v: (v.co - last).length)
            chain.append(nearest)
            remaining.remove(nearest)
        return chain


class FBXMT_OT_CurveCleaner(bpy.types.Operator):
    bl_idname      = 'fbxmt.curve_cleaner'
    bl_label       = 'Clean Curve'
    bl_description = ('Resamples the selected edge/vert chain to evenly-spaced '
                      'positions along a Catmull-Rom spline fit. '
                      'Works in Edge select or Vert select mode')
    bl_options     = {'REGISTER', 'UNDO'}

    segments: bpy.props.IntProperty(
        name        = 'Segments',
        description = 'Number of output segments (0 = match source vert count)',
        default     = 0,
        min         = 0,
        max         = 512,
    )

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or context.mode != 'EDIT_MESH':
            self.report({'WARNING'}, 'Curve Cleaner: need a mesh object in Edit Mode')
            return {'CANCELLED'}

        bm = _bmesh.from_edit_mesh(obj.data)
        sm = context.tool_settings.mesh_select_mode

        in_edge_mode = sm[1] and not sm[0] and not sm[2]

        chain_verts = (_chain_from_edges(bm) if in_edge_mode
                       else _chain_from_verts(bm))

        if not chain_verts or len(chain_verts) < 2:
            self.report({'WARNING'}, 'Curve Cleaner: select at least 2 connected edges/verts')
            return {'CANCELLED'}

        wm   = obj.matrix_world
        wm_i = wm.inverted()

        world_pts = [wm @ v.co for v in chain_verts]
        n_segs    = self.segments if self.segments > 0 else len(chain_verts) - 1

        resampled = catmull_rom_resample(world_pts, n_segs)

        if len(resampled) != len(chain_verts):
            self.report({'WARNING'},
                        f'Curve Cleaner: resampled count ({len(resampled)}) differs '
                        f'from source ({len(chain_verts)}). '
                        f'Set Segments to {len(chain_verts)-1} to match, '
                        f'or use a different segment count.')
            return {'CANCELLED'}

        for v, new_world in zip(chain_verts, resampled):
            v.co = wm_i @ new_world

        _bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, f'Curve Cleaner: resampled {len(chain_verts)} verts '
                              f'over {n_segs} segments')
        return {'FINISHED'}

    def invoke(self, context, event):
        # Auto-set segments to match source vert count on first run
        bm = _bmesh.from_edit_mesh(context.active_object.data)
        sm = context.tool_settings.mesh_select_mode
        if sm[1] and not sm[0] and not sm[2]:
            chain = _chain_from_edges(bm)
        else:
            chain = _chain_from_verts(bm)
        if chain:
            self.segments = len(chain) - 1
        return self.execute(context)


def register():
    pass

def unregister():
    pass
