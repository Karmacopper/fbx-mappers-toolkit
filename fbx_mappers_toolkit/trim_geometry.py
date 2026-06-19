# trim_geometry.py — FBX Mapper's Toolkit
#
# Pure geometry builders for every beam type.
# Each function accepts a source object + settings dataclass and returns a
# *caller-owned* bmesh.  No bpy.data mutations happen here.
#
# Coordinate convention
# ─────────────────────
#  For every beam the sweep runs along a *run axis* (the long axis of the beam).
#  The cross-section is always built in the plane PERPENDICULAR to that axis:
#
#      run_axis  — direction the beam travels (v0 → v1)
#      normal    — face normal = depth direction (beam protrudes into the room)
#      across    — run_axis × normal, normalised = left/right across the face
#
#  Width  expands along ±across  (left/right as you look along the beam)
#  Height expands along ±normal  (in/out of the wall surface)
#  Offset slides the whole profile along ±across
#  Inset  pushes the profile along +normal (recesses the beam into the surface)
#
#  This means the cross-section quad lives in the (across, normal) plane and
#  is swept along run_axis — which is how a real beam works.

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Tuple

import bpy
import bmesh
from mathutils import Vector, Matrix

from .spline_utils import catmull_rom_resample


# ──────────────────────────────────────────────────────────────────────────────
# Settings dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BeamSettings:
    # Cross-section — shared by all beam types
    width:          float = 0.10
    height:         float = 0.10
    cap_ends:       bool  = True
    profile:        int   = 0      # reserved — unused

    # Parallel-beam specific
    par_count:       int   = 2      # number of beams
    par_spacing:     float = 0.30   # centre-to-centre distance between beams
    par_placement:   str  = 'DEFAULT'  # 'DEFAULT' or 'CENTRED'
    par_first_beam:  float = 0.05   # lateral position of beam 1 from face start edge
    par_start_inset: float = 0.02   # beam end caps must not be closer than this to face start
    par_end_inset:   float = 0.02   # beam end caps must not be closer than this to face end
    par_end_clamp:   float = 0.0    # no beam lateral centre past (face_across_max - this); 0 = off

    # Quick beam — face mode raycast
    quick_raycast_iters:  int   = 1    # how many surfaces to pierce before stopping
    quick_overrun_start:  float = 0.02 # extend back past selection start
    quick_overrun_end:    float = 0.02 # extend past raycast hit point

    # Quick beam — edge mode dihedral
    dihedral_angle_offset: float = 0.0  # radians — rotation of bisector around run axis

    # Spoke-beam specific
    spoke_count:    int   = 4
    spoke_spacing_mode: str = 'VISUAL'

    # Curve-beam specific
    curve_segments: int   = 8


# ──────────────────────────────────────────────────────────────────────────────
# Low-level helpers
# ──────────────────────────────────────────────────────────────────────────────

def _world_normal(face, world_matrix: Matrix) -> Vector:
    return (world_matrix.to_3x3().normalized() @ face.normal).normalized()


def _face_centre(face, world_matrix: Matrix) -> Vector:
    return world_matrix @ face.calc_center_median()


def _across_from_run(run_axis: Vector, normal: Vector) -> Vector:
    """Left/right axis perpendicular to both run and normal.

    If run and normal are parallel (degenerate face) fall back to world X.
    Result is always oriented so its Z component >= 0 (consistent winding
    across direction changes that would otherwise flip the sign).
    """
    c = run_axis.cross(normal)
    if c.length < 1e-4:
        c = run_axis.cross(Vector((1.0, 0.0, 0.0)))
    c = c.normalized()
    if c.z < 0:
        c = -c
    return c


def _make_profile(
    anchor:   Vector,   # world-space point on the face surface at beam centre
    normal:   Vector,   # depth axis — beam protrudes along this (face normal)
    across:   Vector,   # width axis — perpendicular to run and normal
    width:    float,
    height:   float,
) -> List[Vector]:
    """Four corners of the beam cross-section in world space.

    The quad lives in the (across, normal) plane centred on *anchor*.
    The beam sits ON the face surface: anchor is on the face, and the profile
    extends outward (away from the wall) along +normal by the full height.

    Corner winding (looking in +run_axis direction, i.e. down the barrel):
        0 = surface-left    1 = surface-right
        2 = proud-right     3 = proud-left
    """
    hw = width  * 0.5
    # Profile sits proud of the surface: bottom edge flush with face, top edge
    # at +height along normal.  The surface-flush bottom keeps the beam
    # anchored to the geometry rather than floating or sinking arbitrarily.
    c0 = anchor - across * hw
    c1 = anchor + across * hw
    c2 = anchor + across * hw + normal * height
    c3 = anchor - across * hw + normal * height
    return [c0, c1, c2, c3]


def _sweep(
    bm:       bmesh.types.BMesh,
    profile:  List[Vector],   # 4 corners at the start end
    run_axis: Vector,          # unit vector along the beam
    start:    Vector,          # world-space start point
    end:      Vector,          # world-space end point
    cap_ends: bool,
) -> None:
    """Sweep *profile* from *start* to *end*, adding geometry to *bm*.

    The profile is treated as already positioned at its correct location
    relative to the beam (it was built at the start anchor).  We just
    translate a copy of it to the end position by moving each vert along
    run_axis by the beam length.
    """
    length = (end - start).length
    if length < 1e-6:
        return

    sv = [bm.verts.new(p)            for p in profile]
    ev = [bm.verts.new(p + run_axis * length) for p in profile]

    n = len(sv)
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new([sv[i], sv[j], ev[j], ev[i]])

    if cap_ends:
        bm.faces.new(list(reversed(sv)))
        bm.faces.new(ev)


def _bmesh_from_obj(obj: bpy.types.Object) -> Tuple[bmesh.types.BMesh, bool]:
    """Return (bm, is_edit_mesh).

    When *is_edit_mesh* is True the bmesh is the live edit-mode mesh and must
    NOT be freed by the caller — it is owned by Blender.  When False the caller
    is responsible for calling bm.free().
    """
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        return bm, True
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    return bm, False


def _selected_faces(obj: bpy.types.Object) -> List[int]:
    """Face indices that are selected, works in both Object and Edit mode."""
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        return [f.index for f in bm.faces if f.select]
    return [p.index for p in obj.data.polygons if p.select]


def get_selected_edge_indices(obj: bpy.types.Object) -> List[int]:
    """Edge indices that are selected — Edit mode only."""
    bm = bmesh.from_edit_mesh(obj.data)
    bm.edges.ensure_lookup_table()
    return [e.index for e in bm.edges if e.select]


def get_selected_vert_indices(obj: bpy.types.Object) -> List[int]:
    """Vert indices that are selected — Edit mode only."""
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    return [v.index for v in bm.verts if v.select]


def _longest_edge_run(face, wm: Matrix) -> Tuple[Vector, Vector, Vector]:
    """Return (run_axis, v0_world, v1_world) for the longest edge of *face*."""
    longest = max(face.edges, key=lambda e: e.calc_length())
    v0 = wm @ longest.verts[0].co
    v1 = wm @ longest.verts[1].co
    run = (v1 - v0)
    return run.normalized(), v0, v1


def _shortest_edge_run(face, wm: Matrix) -> Tuple[Vector, Vector, Vector]:
    """Return (run_axis, v0_world, v1_world) for the shortest edge of *face*."""
    shortest = min(face.edges, key=lambda e: e.calc_length())
    v0 = wm @ shortest.verts[0].co
    v1 = wm @ shortest.verts[1].co
    run = (v1 - v0)
    return run.normalized(), v0, v1


def _face_run_extents(
    face,
    wm:       Matrix,
    run_axis: Vector,
    origin:   Vector,
) -> Tuple[float, float]:
    """Project every vert of *face* onto *run_axis* and return (t_min, t_max).

    *origin* is the reference point (usually one face vert) so the scalars are
    relative distances along the beam axis.  t_min..t_max is the full span of
    the face in the run direction.
    """
    ts = [(wm @ v.co - origin).dot(run_axis) for v in face.verts]
    return min(ts), max(ts)


# ──────────────────────────────────────────────────────────────────────────────
# Quick Beam
# ──────────────────────────────────────────────────────────────────────────────

def build_quick_beam(
    source_obj:   bpy.types.Object,
    settings:     BeamSettings,
    context=None,
) -> bmesh.types.BMesh:
    """Unified quick beam — behaviour driven by edit-mode selection type and count."""
    import bpy as _bpy
    ctx = context or _bpy.context

    out = bmesh.new()
    wm  = source_obj.matrix_world
    src, is_edit = _bmesh_from_obj(source_obj)
    normal_mat   = wm.to_3x3().normalized()

    # Auto-computed overrun (single-edge mode only) — None means "not computed"
    computed_overrun = {'start': None, 'end': None}

    ts   = ctx.tool_settings
    sm   = ts.mesh_select_mode
    is_vert_mode  = sm[0] and not sm[1] and not sm[2]
    is_edge_mode  = sm[1] and not sm[0] and not sm[2]
    is_face_mode  = sm[2] and not sm[0] and not sm[1]
    if not (is_vert_mode or is_edge_mode or is_face_mode):
        is_face_mode = True


    src.verts.ensure_lookup_table()
    src.edges.ensure_lookup_table()
    src.faces.ensure_lookup_table()

    # ── VERT MODE ─────────────────────────────────────────────────────────────
    if is_vert_mode:
        sel_verts = [v for v in src.verts if v.select]
        if len(sel_verts) == 2:
            va, vb   = sel_verts
            p0       = wm @ va.co
            p1       = wm @ vb.co
            na_list  = [(normal_mat @ lf.normal).normalized() for lf in va.link_faces]
            nb_list  = [(normal_mat @ lf.normal).normalized() for lf in vb.link_faces]
            n        = _avg_normal(na_list + nb_list)
            _quick_span(out, p0, p1, n, settings)
        else:

            pass  # fewer than 2 verts — no-op
    # ── EDGE MODE ─────────────────────────────────────────────────────────────
    elif is_edge_mode:
        sel_edges = [e for e in src.edges if e.select]

        if len(sel_edges) == 1:
            edge   = sel_edges[0]
            linked = edge.link_faces[:]

            if len(linked) == 2:
                fa, fb = linked
                na     = (normal_mat @ fa.normal).normalized()
                nb     = (normal_mat @ fb.normal).normalized()
                sv0    = wm @ edge.verts[0].co
                sv1    = wm @ edge.verts[1].co
                run    = (sv1 - sv0).normalized()


                dot        = max(-1.0, min(1.0, na.dot(nb)))
                half_angle = math.acos(dot) * 0.5

                bisector_raw = (na + nb)
                bisector     = na.copy() if bisector_raw.length < 1e-4 else bisector_raw.normalized()
                edge_mid     = (sv0 + sv1) * 0.5

                # na/nb already point outward (away from solid mass) by
                # convention, so their sum/average also points outward — no
                # flip needed. (A face-centre-based flip heuristic was tried
                # here previously but is unreliable for concave edges.)

                offset = max(-half_angle + 0.009,
                             min(half_angle - 0.009, settings.dihedral_angle_offset))
                if abs(offset) > 1e-6:
                    bisector = (Matrix.Rotation(offset, 4, run) @ bisector).normalized()

                # 'across' for the profile is the edge direction itself —
                # this becomes the WIDTH axis (beam spans along the edge)
                across = run

                # Raycast from edge midpoint along bisector to find beam length —
                # the beam travels along the bisector, not along the edge.
                depsgraph    = ctx.evaluated_depsgraph_get()
                max_dist     = _scene_diagonal(ctx)
                iters        = max(1, settings.quick_raycast_iters)
                ray_origin   = edge_mid + bisector * 0.001
                hit_loc      = None
                hit_normal   = None
                surfaces_hit = 0
                travelled    = 0.0
                while surfaces_hit < iters and travelled < max_dist:
                    hit, loc, hn, _, hit_obj, _ = ctx.scene.ray_cast(
                        depsgraph, ray_origin, bisector, distance=max_dist - travelled)
                    if not hit:
                        break
                    hit_loc       = loc
                    hit_normal    = hn
                    surfaces_hit += 1
                    travelled    += (loc - ray_origin).length + 0.002
                    ray_origin    = loc + bisector * 0.002

                if hit_loc is None:
                    pass  # no hit — no geometry generated
                else:
                    # height_axis ⊥ bisector and edge-direction — this is the
                    # WIDTH axis (across the corner, e.g. wall-to-wall span).
                    # 'across' (=run, edge direction) is the HEIGHT axis —
                    # along the edge itself (e.g. vertical for a vertical edge).
                    width_axis  = _across_from_run(bisector, across)
                    height_axis = across

                    # Overrun is manual — panel values used directly.
                    overrun_start = settings.quick_overrun_end
                    overrun_end   = settings.quick_overrun_start

                    start_co = edge_mid - bisector * overrun_start
                    end_co   = hit_loc  + bisector * overrun_end

                    anchor  = start_co - height_axis * (settings.height * 0.5)
                    profile = _make_profile(anchor, height_axis, width_axis,
                                             settings.width, settings.height)
                    _sweep(out, profile, bisector, start_co, end_co, settings.cap_ends)
            else:
                pass

        elif len(sel_edges) == 2:
            ea, eb  = sel_edges
            p0      = wm @ ((ea.verts[0].co + ea.verts[1].co) * 0.5)
            p1      = wm @ ((eb.verts[0].co + eb.verts[1].co) * 0.5)
            na_list = [(normal_mat @ lf.normal).normalized() for lf in ea.link_faces]
            nb_list = [(normal_mat @ lf.normal).normalized() for lf in eb.link_faces]
            n       = _avg_normal(na_list + nb_list)
            _quick_span(out, p0, p1, n, settings)
        else:
            pass

    # ── FACE MODE ─────────────────────────────────────────────────────────────
    else:
        sel_faces = [f for f in src.faces if f.select]

        if len(sel_faces) == 1:
            face   = sel_faces[0]
            n      = (normal_mat @ face.normal).normalized()
            origin = wm @ face.calc_center_median()

            depsgraph    = ctx.evaluated_depsgraph_get()
            max_dist     = _scene_diagonal(ctx)
            iters        = max(1, settings.quick_raycast_iters)

            ray_origin   = origin + n * 0.001
            hit_loc      = None
            surfaces_hit = 0
            travelled    = 0.0
            while surfaces_hit < iters and travelled < max_dist:
                hit, loc, _, _, _, _ = ctx.scene.ray_cast(
                    depsgraph, ray_origin, n, distance=max_dist - travelled)
                if not hit:
                    break
                hit_loc       = loc
                surfaces_hit += 1
                travelled    += (loc - ray_origin).length + 0.002
                ray_origin    = loc + n * 0.002

            if hit_loc is not None:
                beam_length = (hit_loc - origin).length
                up      = _stable_up(n)
                across  = _across_from_run(n, up)
                depth   = _across_from_run(n, across)
                start_co = origin - n * settings.quick_overrun_end
                end_co   = hit_loc + n * settings.quick_overrun_start
                anchor   = start_co - depth * (settings.height * 0.5)
                profile = _make_profile(anchor, depth, across, settings.width, settings.height)
                _sweep(out, profile, n, start_co, end_co, settings.cap_ends)

        elif len(sel_faces) == 2:
            fa, fb = sel_faces
            p0     = wm @ fa.calc_center_median()
            p1     = wm @ fb.calc_center_median()
            na     = (normal_mat @ fa.normal).normalized()
            nb     = (normal_mat @ fb.normal).normalized()
            n      = _avg_normal([na, nb])
            _quick_span(out, p0, p1, n, settings)
        else:
            pass

    if not is_edit:
        src.free()
    bmesh.ops.remove_doubles(out, verts=out.verts, dist=1e-5)
    bmesh.ops.recalc_face_normals(out, faces=out.faces)
    return out, computed_overrun

def _avg_normal(normals: List[Vector]) -> Vector:
    """Average a list of unit normals, fall back to world Z if degenerate."""
    s = Vector((0.0, 0.0, 0.0))
    for n in normals:
        s += n
    return s.normalized() if s.length > 1e-4 else Vector((0.0, 0.0, 1.0))


def _stable_up(run: Vector) -> Vector:
    """A world-up vector that isn't parallel to run."""
    up = Vector((0.0, 0.0, 1.0))
    if abs(run.dot(up)) > 0.99:
        up = Vector((1.0, 0.0, 0.0))
    return up


def _scene_diagonal(ctx) -> float:
    all_pts = [obj.matrix_world @ Vector(c)
               for obj in ctx.scene.objects if obj.type == 'MESH'
               for c in obj.bound_box]
    if not all_pts:
        return 1000.0
    lo = Vector((min(p.x for p in all_pts), min(p.y for p in all_pts), min(p.z for p in all_pts)))
    hi = Vector((max(p.x for p in all_pts), max(p.y for p in all_pts), max(p.z for p in all_pts)))
    return (hi - lo).length


def _quick_span(
    out:      bmesh.types.BMesh,
    p0:       Vector,
    p1:       Vector,
    normal:   Vector,
    settings: 'BeamSettings',
) -> None:
    """Hard-span beam from p0 to p1 with cross-section oriented by normal.

    Profile is centred on p0 (and by extension p1) in both the across
    (width) and normal (depth/height) axes — matching the single-edge/face
    quick beam convention where the cross-section is always centroid-
    positioned on the source selection, not flush-anchored.
    """
    run    = (p1 - p0)
    if run.length < 1e-6:
        return
    run    = run.normalized()
    across = _across_from_run(run, normal)
    anchor  = p0 - normal * (settings.height * 0.5)
    profile = _make_profile(anchor, normal, across, settings.width, settings.height)
    _sweep(out, profile, run, p0, p1, settings.cap_ends)


# ──────────────────────────────────────────────────────────────────────────────
# Parallel Beam
# ──────────────────────────────────────────────────────────────────────────────

def build_parallel_beam(
    source_obj:      bpy.types.Object,
    face_indices:    List[int],
    settings:        BeamSettings,
    context=None,
    drive_through:   bool = False,
    source_obj_name: str  = '',
) -> bmesh.types.BMesh:
    """Generate parallel beams by raycasting from anchor points on selected faces."""
    import bpy as _bpy
    ctx = context or _bpy.context

    out  = bmesh.new()
    wm   = source_obj.matrix_world
    src, is_edit = _bmesh_from_obj(source_obj)
    count = max(settings.par_count, 1)

    depsgraph = ctx.evaluated_depsgraph_get()

    # Clear any previous drive-through hit data
    ctx.scene.pop('_fbxmt_drivethru_hits', None)

    from .ceiling_deco import _build_beam_per_vert

    # Scene bounding box diagonal — hard cap for drive-through raycast
    all_pts = [obj.matrix_world @ Vector(c)
               for obj in ctx.scene.objects if obj.type == 'MESH'
               for c in obj.bound_box]
    if all_pts:
        scene_min = Vector((min(p.x for p in all_pts), min(p.y for p in all_pts), min(p.z for p in all_pts)))
        scene_max = Vector((max(p.x for p in all_pts), max(p.y for p in all_pts), max(p.z for p in all_pts)))
        max_dist  = (scene_max - scene_min).length
    else:
        max_dist = 1000.0

    guide_obj = ctx.scene.objects.get(source_obj_name) if source_obj_name else source_obj

    for fi in face_indices:
        face   = src.faces[fi]
        n      = _world_normal(face, wm)
        run, v0, v1 = _longest_edge_run(face, wm)  # longest edge = horizontal spacing axis
        across = run                                # space beams along longest edge

        # Face centre — use as vertical anchor (mid-height of face)
        face_centre = sum((wm @ v.co for v in face.verts), Vector()) / len(face.verts)

        # Lateral extents along the longest edge
        across_ts = [(wm @ v.co - v0).dot(across) for v in face.verts]
        lat_min   = min(across_ts)
        lat_max   = max(across_ts)
        face_span = lat_max - lat_min
        first_off = max(0.0, min(settings.par_first_beam, face_span))
        last_off  = max(0.0, min(settings.par_end_clamp,  face_span))
        span      = face_span - first_off - last_off

        if settings.par_placement == 'CENTRED':
            n_beams   = max(settings.par_count, 1)
            step      = face_span / (n_beams + 1)
            positions = [lat_min + step * (i + 1) for i in range(n_beams)]
        elif settings.par_spacing == 0.0:
            # Count mode — distribute count beams evenly between FB and LB offsets
            n_beams = max(settings.par_count, 1)
            if n_beams == 1:
                positions = [lat_min + first_off]
            else:
                step = span / (n_beams - 1)
                positions = [lat_min + first_off + i * step for i in range(n_beams)]
        else:
            # Spacing mode — fill span at spacing intervals
            if settings.par_count > 0:
                # Count limits maximum beams
                max_beams = settings.par_count
            else:
                max_beams = 10000  # effectively unlimited
            pos = lat_min + first_off
            end = lat_max - last_off
            positions = []
            while pos <= end + 1e-5 and len(positions) < max_beams:
                positions.append(pos)
                pos += settings.par_spacing

        for lat in positions:

            # Horizontal position from edge, vertical position at face mid-height
            h_pos  = v0 + across * lat
            anchor = Vector((h_pos.x, h_pos.y, face_centre.z))

            # Raycast to find opposite surface
            intermediate_hits = []   # list of obj names hit before guide
            if drive_through and guide_obj:
                ray_origin = anchor + n * 0.001
                hit_loc    = None
                travelled  = 0.0
                while travelled < max_dist:
                    hit, loc, norm, _, hit_obj, _ = ctx.scene.ray_cast(
                        depsgraph, ray_origin, n, distance=max_dist - travelled)
                    if not hit:
                        break
                    travelled += (loc - ray_origin).length + 0.002
                    ray_origin = loc + n * 0.002
                    if hit_obj and hit_obj.name == guide_obj.name:
                        hit_loc = loc
                        break
                    elif hit_obj and hit_obj.type == 'MESH':
                        front_face = norm.dot(n) < 0
                        is_fbxmt   = (hit_obj.get('fbxmt_beam_w') is not None or
                                      hit_obj.get('fbxmt_beam_h') is not None or
                                      hit_obj.get('fbxmt_is_drivethru') is not None)
                        is_source  = hit_obj.name == source_obj.name
                        is_guide   = hit_obj.name == guide_obj.name
                        is_seen    = hit_obj.name in intermediate_hits
                        if front_face and not is_fbxmt and not is_source and not is_guide and not is_seen:
                            intermediate_hits.append(hit_obj.name)
                if hit_loc is None:
                    continue
            else:
                hit, hit_loc, hit_norm, _, hit_obj, _ = ctx.scene.ray_cast(
                    depsgraph, anchor + n * 0.001, n, distance=max_dist)
                if not hit:
                    hit, hit_loc, hit_norm, _, hit_obj, _ = ctx.scene.ray_cast(
                        depsgraph, anchor - n * 0.001, -n, distance=max_dist)
                    if not hit:
                        continue

            # Store intermediate hits + beam normal for commit-time boolean resolution
            if intermediate_hits:
                existing = list(ctx.scene.get('_fbxmt_drivethru_hits', []))
                existing.append({
                    'hits': intermediate_hits,
                    'n':    [n.x, n.y, n.z],
                    'w':    settings.width,
                    'h':    settings.height,
                })
                ctx.scene['_fbxmt_drivethru_hits'] = existing

            # Apply overrun
            start_co = anchor  - n * settings.par_start_inset
            end_co   = hit_loc + n * settings.par_end_inset

            # Build beam from anchor to hit point using _build_beam_per_vert
            beam_bm = bmesh.new()
            _build_beam_per_vert(
                beam_bm, start_co, end_co,
                settings.height, settings.width,
                mat_index=0,
            )
            # Join beam_bm into out via mesh intermediate
            tmp_mesh = bpy.data.meshes.new('_fbxmt_tmp')
            beam_bm.to_mesh(tmp_mesh)
            beam_bm.free()
            out.from_mesh(tmp_mesh)
            bpy.data.meshes.remove(tmp_mesh)

    if not is_edit:
        src.free()
    bmesh.ops.remove_doubles(out, verts=out.verts, dist=1e-5)
    bmesh.ops.recalc_face_normals(out, faces=out.faces)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Dihedral Beam
# ──────────────────────────────────────────────────────────────────────────────

def build_dihedral_beam(
    source_obj:   bpy.types.Object,
    edge_indices: List[int],
    settings:     BeamSettings,
) -> 'Tuple[bmesh.types.BMesh, List[float]]':
    """Corner beam sitting in the dihedral of each selected edge.

    Select one or more edges.  Each edge must have exactly two linked faces
    (manifold).  The beam sits on the bisector of the two face normals and
    runs the full length of the edge.

    Returns (out_bm, half_angles) where half_angles[i] is the half-dihedral
    in radians for edge_indices[i] — used by the panel to clamp the offset
    slider to a physically meaningful range.
    """
    out = bmesh.new()
    wm  = source_obj.matrix_world
    src, is_edit = _bmesh_from_obj(source_obj)
    src.edges.ensure_lookup_table()

    half_angles: List[float] = []
    normal_mat = wm.to_3x3().normalized()

    for ei in edge_indices:
        edge = src.edges[ei]

        linked = edge.link_faces[:]
        if len(linked) != 2:
            # Boundary or non-manifold — skip silently
            continue

        fa, fb = linked
        na = (normal_mat @ fa.normal).normalized()
        nb = (normal_mat @ fb.normal).normalized()

        sv0 = wm @ edge.verts[0].co
        sv1 = wm @ edge.verts[1].co
        run = (sv1 - sv0).normalized()

        # Raw angle between the two face planes.
        # dot of inward normals: 0 = perpendicular faces, -1 = flat (antiparallel)
        dot = max(-1.0, min(1.0, na.dot(nb)))
        raw_dihedral = math.acos(dot)          # 0..π
        half_angle   = raw_dihedral * 0.5      # clamp limit for offset slider
        half_angles.append(half_angle)

        # Bisector of the two normals — points into the concave side (room interior
        # for a wall/ceiling join).  If the two normals are nearly parallel the
        # bisector degenerates; fall back to one of them.
        bisector = (na + nb)
        if bisector.length < 1e-4:
            bisector = na.copy()
        else:
            bisector = bisector.normalized()

        # Face centres sit on the interior side — bisector must point away from them.
        # If to_faces and bisector point the same way, we're going inward — flip.
        edge_mid = (sv0 + sv1) * 0.5
        ca       = wm @ fa.calc_center_median()
        cb       = wm @ fb.calc_center_median()
        to_faces = ((ca + cb) * 0.5 - edge_mid).normalized()
        if to_faces.dot(bisector) > 0.0:
            bisector = -bisector

        # Apply user angle offset — rotate bisector around run axis
        offset = max(-half_angle + 0.009, min(half_angle - 0.009,
                                               settings.dihedral_angle_offset))
        if abs(offset) > 1e-6:
            rot      = Matrix.Rotation(offset, 4, run)
            bisector = (rot @ bisector).normalized()

        across  = _across_from_run(run, bisector)
        profile = _make_profile(sv0, bisector, across,
                                 settings.width, settings.height)
        _sweep(out, profile, run, sv0, sv1, settings.cap_ends)

    if not is_edit:
        src.free()
    bmesh.ops.remove_doubles(out, verts=out.verts, dist=1e-5)
    bmesh.ops.recalc_face_normals(out, faces=out.faces)
    return out, half_angles


# ──────────────────────────────────────────────────────────────────────────────
# Spoke Beam
# ──────────────────────────────────────────────────────────────────────────────

def build_spoke_beam(
    source_obj:   bpy.types.Object,
    face_indices: List[int],
    settings:     BeamSettings,
) -> bmesh.types.BMesh:
    """Radial spokes running from an inner (hub) arc to an outer (rim) arc."""

    out          = bmesh.new()
    wm           = source_obj.matrix_world
    normal_mat   = wm.to_3x3().normalized()
    src, is_edit = _bmesh_from_obj(source_obj)
    chains       = _sort_face_chain(src, face_indices)


    if len(chains) != 2 or len(chains[0]) != len(chains[1]):
        if not is_edit:
            src.free()
        return out

    def _terminal_edge_mid(face_idx, next_face_idx):
        face       = src.faces[face_idx]
        next_verts = {v.index for v in src.faces[next_face_idx].verts}
        for edge in face.edges:
            if {v.index for v in edge.verts}.isdisjoint(next_verts):
                return wm @ ((edge.verts[0].co + edge.verts[1].co) * 0.5)
        return wm @ face.calc_center_median()

    def _pinned_polyline(chain):
        cents     = [_face_centre(src.faces[fi], wm) for fi in chain]
        cents[0]  = _terminal_edge_mid(chain[0],  chain[1])
        cents[-1] = _terminal_edge_mid(chain[-1], chain[-2])
        return cents

    def _normals_for_chain(chain):
        return [_world_normal(src.faces[fi], wm) for fi in chain]

    def _interp_at(polyline, t):
        n = len(polyline)
        if n < 2:
            return Vector(polyline[0]) if polyline else Vector()
        arc = [0.0]
        for i in range(1, n):
            arc.append(arc[-1] + (polyline[i] - polyline[i-1]).length)
        total = arc[-1]
        if total < 1e-8:
            return Vector(polyline[0])
        target = total * t
        for i in range(n - 1):
            if arc[i] <= target <= arc[i+1]:
                seg_len = arc[i+1] - arc[i]
                f = (target - arc[i]) / seg_len if seg_len > 1e-8 else 0.0
                return polyline[i].lerp(polyline[i+1], f)
        return Vector(polyline[-1])

    hub_poly = _pinned_polyline(chains[0])
    rim_poly = _pinned_polyline(chains[1])
    hub_norm = _normals_for_chain(chains[0])
    rim_norm = _normals_for_chain(chains[1])


    dist_same = (hub_poly[0]  - rim_poly[0]).length  + (hub_poly[-1] - rim_poly[-1]).length
    dist_rev  = (hub_poly[0]  - rim_poly[-1]).length + (hub_poly[-1] - rim_poly[0]).length
    if dist_rev < dist_same:
        rim_poly = list(reversed(rim_poly))
        rim_norm = list(reversed(rim_norm))

    n_spokes = max(settings.spoke_count, 1)

    # Compute total hub-arc length for width-aware visual spacing
    def _polyline_length(poly):
        return sum((poly[i] - poly[i-1]).length for i in range(1, len(poly)))

    hub_arc_len = _polyline_length(hub_poly)

    if n_spokes == 1:
        fractions = [0.5]
    elif settings.spoke_spacing_mode == 'EXACT':
        fractions = [(i + 1) / (n_spokes + 1) for i in range(n_spokes)]
    else:  # VISUAL — width-aware: N beams + (N+1) equal gaps along the arc
        w = settings.width
        if hub_arc_len > w * n_spokes:
            gap = (hub_arc_len - w * n_spokes) / (n_spokes + 1)
            fractions = [(gap * (i + 1) + w * i + w * 0.5) / hub_arc_len
                        for i in range(n_spokes)]
        else:
            # Beams wider than available arc — fall back to exact spacing
            fractions = [(i + 1) / (n_spokes + 1) for i in range(n_spokes)]


    overrun_s = settings.quick_overrun_start
    overrun_e = settings.quick_overrun_end

    for idx, t in enumerate(fractions):
        hub = _interp_at(hub_poly, t)
        rim = _interp_at(rim_poly, t)

        run = (rim - hub)
        if run.length < 1e-6:
            continue
        run_dir = run.normalized()

        hub = hub - run_dir * overrun_s
        rim = rim + run_dir * overrun_e

        na = _interp_at(hub_norm, t) if len(hub_norm) > 1 else hub_norm[0]
        nb = _interp_at(rim_norm, t) if len(rim_norm) > 1 else rim_norm[0]
        if na.length > 1e-6:
            na = na.normalized()
        if nb.length > 1e-6:
            nb = nb.normalized()
        normal = _avg_normal([na, nb])


        vb, fb = len(out.verts), len(out.faces)
        _quick_span(out, hub, rim, normal, settings)

    if not is_edit:
        src.free()
    bmesh.ops.remove_doubles(out, verts=out.verts, dist=1e-5)
    bmesh.ops.recalc_face_normals(out, faces=out.faces)
    return out

# ──────────────────────────────────────────────────────────────────────────────
# Curve Beam
# ──────────────────────────────────────────────────────────────────────────────

def build_curve_beam(
    source_obj:   bpy.types.Object,
    face_indices: List[int],
    settings:     BeamSettings,
) -> bmesh.types.BMesh:
    """Beam swept along a Catmull-Rom spline midway between two parallel face chains.

    Endpoints are pinned exactly to the terminal edge midpoints of the selection.
    Interior points are resampled evenly along the spline arc.
    curve_segments controls interior subdivisions (total rings = segments + 1).
    """

    import math as _math

    out          = bmesh.new()
    wm           = source_obj.matrix_world
    src, is_edit = _bmesh_from_obj(source_obj)
    chains       = _sort_face_chain(src, face_indices)
    world_up     = Vector((0.0, 0.0, 1.0))


    depth     = settings.height
    thickness = settings.width
    n_segs    = max(2, settings.curve_segments)

    def _terminal_edge_mid(face_idx, next_face_idx):
        """World midpoint of the terminal edge of face_idx —
        the edge that shares NO vertices with next_face_idx."""
        face       = src.faces[face_idx]
        next_verts = {v.index for v in src.faces[next_face_idx].verts}
        # First pass: edge sharing no verts with next face (the far end)
        for edge in face.edges:
            ev = {v.index for v in edge.verts}
            if ev.isdisjoint(next_verts):
                mid = (edge.verts[0].co + edge.verts[1].co) * 0.5
                return wm @ mid
        # Fallback: face centre
        mid = face.calc_center_median()
        return wm @ mid

    def _tangent(rp, i):
        n = len(rp)
        if i == 0:
            t = rp[1] - rp[0]
        elif i == n - 1:
            t = rp[-1] - rp[-2]
        else:
            t_in  = (rp[i]     - rp[i - 1]).normalized()
            t_out = (rp[i + 1] - rp[i]).normalized()
            t     = t_in + t_out
        return t.normalized() if t.length > 1e-6 else Vector((0.0, 1.0, 0.0))

    def _miter_scale(rp, i):
        n = len(rp)
        if i == 0 or i == n - 1:
            return 1.0
        t_in  = (rp[i]     - rp[i - 1]).normalized()
        t_out = (rp[i + 1] - rp[i]).normalized()
        cos_a = max(-1.0, min(1.0, t_in.dot(t_out)))
        half  = (1.0 + cos_a) * 0.5
        return 1.0 / _math.sqrt(half) if half > 1e-6 else 1.0

    def _profile(co, tangent, scale=1.0):
        h_arm = tangent.cross(world_up)
        if h_arm.length < 1e-6:
            h_arm = tangent.cross(Vector((0.0, 1.0, 0.0)))
        h_arm      = h_arm.normalized()
        wall_down  = -world_up
        t_thick    = thickness * scale
        centre_off = h_arm * (t_thick * 0.5) + wall_down * (depth * 0.5)
        A = Vector(co) - centre_off
        return A.copy(), A + h_arm * t_thick, A + h_arm * t_thick + wall_down * depth, A + wall_down * depth

    def _face_fn(vlist):
        try:
            out.faces.new(vlist)
        except Exception:
            pass

    def _build_run(ring_positions):
        rings = []
        for i, pos in enumerate(ring_positions):
            t     = _tangent(ring_positions, i)
            scale = _miter_scale(ring_positions, i)
            verts = [out.verts.new(p) for p in _profile(pos, t, scale)]
            rings.append(verts)
        STRIPS = [(0, 1), (1, 2), (2, 3), (3, 0)]
        for ri in range(len(rings) - 1):
            rs, re = rings[ri], rings[ri + 1]
            for a, b in STRIPS:
                _face_fn([rs[a], rs[b], re[b], re[a]])
        if settings.cap_ends:
            v0s, v1s, v2s, v3s = rings[0]
            _face_fn([v2s, v1s, v0s])
            _face_fn([v3s, v2s, v0s])
            v0e, v1e, v2e, v3e = rings[-1]
            _face_fn([v0e, v1e, v2e])
            _face_fn([v0e, v2e, v3e])

    def _pinned_polyline(chain):
        cents     = [_face_centre(src.faces[fi], wm) for fi in chain]
        cents[0]  = _terminal_edge_mid(chain[0],  chain[1])
        cents[-1] = _terminal_edge_mid(chain[-1], chain[-2])
        return cents

    def _apply_overrun(polyline):
        """Displace endpoints along outward tangents by overrun values."""
        if len(polyline) < 2:
            return polyline
        overrun_s = settings.quick_overrun_start
        overrun_e = settings.quick_overrun_end
        if overrun_s != 0.0:
            t_start = (polyline[0] - polyline[1]).normalized()
            polyline[0] = polyline[0] + t_start * overrun_s
        if overrun_e != 0.0:
            t_end = (polyline[-1] - polyline[-2]).normalized()
            polyline[-1] = polyline[-1] + t_end * overrun_e
        return polyline

    if len(chains) == 2 and len(chains[0]) == len(chains[1]):
        mp0 = _pinned_polyline(chains[0])
        mp1 = _pinned_polyline(chains[1])
        dist_same = (mp0[0]  - mp1[0]).length  + (mp0[-1] - mp1[-1]).length
        dist_rev  = (mp0[0]  - mp1[-1]).length + (mp0[-1] - mp1[0]).length
        if dist_rev < dist_same:
            mp1 = list(reversed(mp1))
        midpoints      = _apply_overrun([(a + b) * 0.5 for a, b in zip(mp0, mp1)])
        ring_positions = catmull_rom_resample(midpoints, n_segs)
        # Pin endpoints exactly
        ring_positions[0]  = midpoints[0]
        ring_positions[-1] = midpoints[-1]
        _build_run(ring_positions)
    else:
        for ci, chain in enumerate(chains):
            if len(chain) < 2:
                continue
            raw            = _apply_overrun(_pinned_polyline(chain))
            ring_positions = catmull_rom_resample(raw, n_segs)
            ring_positions[0]  = raw[0]
            ring_positions[-1] = raw[-1]
            _build_run(ring_positions)

    if not is_edit:
        src.free()

    out.normal_update()
    return out

def _sort_face_chain(bm, face_indices):
    """Sort face indices into one or more connected chains.

    Returns a list of chains (each chain is a list of face indices).
    Handles disconnected selections by returning each connected run separately.
    """
    if len(face_indices) <= 1:
        return [face_indices]

    index_set = set(face_indices)
    adjacency = {fi: [] for fi in face_indices}
    for fi in face_indices:
        for edge in bm.faces[fi].edges:
            for linked in edge.link_faces:
                li = linked.index
                if li != fi and li in index_set and li not in adjacency[fi]:
                    adjacency[fi].append(li)

    visited = set()
    chains  = []

    def _walk(start):
        chain = [start]
        visited.add(start)
        prev, current = None, start
        while True:
            nbrs = [n for n in adjacency[current] if n != prev and n not in visited]
            if not nbrs:
                break
            nxt = nbrs[0]
            visited.add(nxt)
            chain.append(nxt)
            prev, current = current, nxt
        return chain

    ends = [fi for fi, nbrs in adjacency.items() if len(nbrs) == 1]
    starts = ends if ends else list(face_indices)

    for s in starts:
        if s not in visited:
            chains.append(_walk(s))

    for fi in face_indices:
        if fi not in visited:
            chains.append([fi])
            visited.add(fi)

    return chains


# ──────────────────────────────────────────────────────────────────────────────
# Public dispatch table
# ──────────────────────────────────────────────────────────────────────────────

BEAM_BUILDERS = {
    'QUICK':    build_quick_beam,
    'DIHEDRAL': build_dihedral_beam,
    'PARALLEL': build_parallel_beam,
    'SPOKE':    build_spoke_beam,
    'CURVE':    build_curve_beam,
}

# Expose selection helpers so trim2.py can use them without re-importing bmesh
get_selected_face_indices = _selected_faces
