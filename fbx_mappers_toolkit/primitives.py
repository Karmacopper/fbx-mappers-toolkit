"""FBXMT Primitives — parametric mesh generators for level design.

Accessed via the FBXMT Primitives N panel tab.
All primitives auto-assign FBXMT materials to the correct face types.
"""

import bpy
import bmesh
import math
import gpu
from gpu_extras.batch import batch_for_shader
from bpy.types import Operator, Menu
from bpy.props import (
    FloatProperty, IntProperty, BoolProperty, EnumProperty
)


# ─── Material helpers ─────────────────────────────────────────────────────────

def _ensure_material(name):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name=name)
    return mat


def _assign_materials(obj, face_mat_map):
    mat_names = list(face_mat_map.keys())
    mat_index = {n: i for i, n in enumerate(mat_names)}
    obj.data.materials.clear()
    for name in mat_names:
        obj.data.materials.append(_ensure_material(name))
    for name, indices in face_mat_map.items():
        idx = mat_index[name]
        for fi in indices:
            obj.data.polygons[fi].material_index = idx


def _assign_mats_edit(obj, bm, wall_set, floor_set, ceil_set):
    for mat_name in ('M_FBXMT_Wall', 'M_FBXMT_Ceiling', 'M_FBXMT_Floor'):
        mat = _ensure_material(mat_name)
        if mat.name not in [m.name for m in obj.data.materials]:
            obj.data.materials.append(mat)
    slot_map = {m.name: i for i, m in enumerate(obj.data.materials)}
    bm.faces.ensure_lookup_table()
    for fi in wall_set:
        if fi < len(bm.faces):
            bm.faces[fi].material_index = slot_map.get('M_FBXMT_Wall', 0)
    for fi in ceil_set:
        if fi < len(bm.faces):
            bm.faces[fi].material_index = slot_map.get('M_FBXMT_Ceiling', 0)
    for fi in floor_set:
        if fi < len(bm.faces):
            bm.faces[fi].material_index = slot_map.get('M_FBXMT_Floor', 0)


# ─── Cap style enum ───────────────────────────────────────────────────────────

CAP_STYLE_ITEMS = [
    ('NONE',      'None',      'No fill'),
    ('VANILLA',   'Vanilla',   'Triangle fan from centre vertex'),
    ('GRID_FILL', 'Grid Fill', 'Quad grid fill (even sides enforced)'),
]


def _fill_cap_bm(bm, loop_verts, is_floor, style, off=None):
    n      = len(loop_verts)
    result = []
    if style == 'NONE':
        return result
    if style == 'VANILLA':
        if is_floor:
            ctr = bm.verts.new((off.x, off.y, off.z) if off else (0.0, 0.0, 0.0))
        else:
            ctr = bm.verts.new((
                sum(v.co.x for v in loop_verts) / n,
                sum(v.co.y for v in loop_verts) / n,
                sum(v.co.z for v in loop_verts) / n,
            ))
        bm.verts.ensure_lookup_table()
        for i in range(n):
            j = (i + 1) % n
            if is_floor:
                f = bm.faces.new([loop_verts[j], loop_verts[i], ctr])
            else:
                f = bm.faces.new([loop_verts[i], loop_verts[j], ctr])
            result.append(f.index)
    elif style == 'GRID_FILL':
        edges = []
        for i in range(n):
            j = (i + 1) % n
            e = bm.edges.get([loop_verts[i], loop_verts[j]])
            if e is None:
                e = bm.edges.new([loop_verts[i], loop_verts[j]])
            edges.append(e)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        try:
            res = bmesh.ops.grid_fill(bm, edges=edges)
            for f in res.get('faces', []):
                result.append(f.index)
        except Exception:
            pass
    return result


# ─── Partial Cylinder ─────────────────────────────────────────────────────────

class FBXMT_OT_AddPartialCylinder(Operator):
    """Add a parametric partial cylinder (arc sweep) with FBXMT materials."""
    bl_idname  = 'fbxmt.add_partial_cylinder'
    bl_label   = 'Partial Cylinder'
    bl_options = {'REGISTER', 'UNDO'}

    radius: FloatProperty(name='Radius', default=2.0, min=0.01, soft_max=100.0, unit='LENGTH')
    inner_radius: FloatProperty(name='Inner Radius', default=0.0, min=0.0, soft_max=100.0, unit='LENGTH')
    height: FloatProperty(name='Height', default=4.0, min=0.01, soft_max=100.0, unit='LENGTH')
    sides: IntProperty(name='Sides', default=16, min=3, max=256)
    arc_sweep: FloatProperty(name='Arc Sweep', default=math.radians(90.0),
                             min=math.radians(1.0), max=math.radians(360.0), subtype='ANGLE')
    arc_start: FloatProperty(name='Arc Start', default=0.0,
                             min=0.0, max=math.radians(360.0), subtype='ANGLE')
    normal_direction: EnumProperty(name='Normals',
        items=[('OUTWARD', 'Outward', ''), ('INWARD', 'Inward', '')], default='OUTWARD')
    cap_top: BoolProperty(name='Top Cap', default=True)
    cap_bottom: BoolProperty(name='Bottom Cap', default=True)
    cap_ends: BoolProperty(name='Arc End Caps', default=True)

    @classmethod
    def poll(cls, context):
        return context.mode in ('OBJECT', 'EDIT_MESH')

    def execute(self, context):
        edit_mode = context.mode == 'EDIT_MESH'
        hollow  = self.inner_radius > 0.0 and self.inner_radius < self.radius
        partial = self.arc_sweep < math.radians(359.9)
        inward  = self.normal_direction == 'INWARD'
        sweep_r = self.arc_sweep
        start_r = self.arc_start
        n       = self.sides
        r_out   = self.radius
        r_in    = self.inner_radius if hollow else 0.0
        h       = self.height

        if edit_mode:
            obj    = context.active_object
            mesh   = obj.data
            bm     = bmesh.from_edit_mesh(mesh)
            offset = obj.matrix_world.inverted() @ context.scene.cursor.location
        else:
            bm     = bmesh.new()
            offset = None

        def ring_verts(radius, z):
            verts = []
            for i in range(n + 1):
                angle = start_r + (sweep_r / n) * i
                x, y  = radius * math.cos(angle), radius * math.sin(angle)
                co    = (x + offset.x, y + offset.y, z + offset.z) if offset else (x, y, z)
                verts.append(bm.verts.new(co))
            return verts

        out_bot = ring_verts(r_out, 0.0)
        out_top = ring_verts(r_out, h)
        if hollow:
            in_bot = ring_verts(r_in, 0.0)
            in_top = ring_verts(r_in, h)
        bm.verts.ensure_lookup_table()

        wall_idx  = []
        floor_idx = []
        ceil_idx  = []

        def nf(verts):
            f = bm.faces.new(verts)
            return f.index

        for i in range(n):
            if inward:
                wall_idx.append(nf([out_bot[i+1], out_bot[i], out_top[i], out_top[i+1]]))
            else:
                wall_idx.append(nf([out_bot[i], out_bot[i+1], out_top[i+1], out_top[i]]))

        if hollow:
            for i in range(n):
                if inward:
                    wall_idx.append(nf([in_bot[i], in_bot[i+1], in_top[i+1], in_top[i]]))
                else:
                    wall_idx.append(nf([in_bot[i+1], in_bot[i], in_top[i], in_top[i+1]]))

        if self.cap_top:
            if hollow:
                for i in range(n):
                    if inward:
                        ceil_idx.append(nf([out_top[i], out_top[i+1], in_top[i+1], in_top[i]]))
                    else:
                        ceil_idx.append(nf([out_top[i+1], out_top[i], in_top[i], in_top[i+1]]))
            else:
                ox = (offset.x if offset else 0.0)
                oy = (offset.y if offset else 0.0)
                oz = (offset.z if offset else 0.0) + h
                ct = bm.verts.new((ox, oy, oz))
                bm.verts.ensure_lookup_table()
                for i in range(n):
                    if inward:
                        ceil_idx.append(nf([out_top[i+1], out_top[i], ct]))
                    else:
                        ceil_idx.append(nf([out_top[i], out_top[i+1], ct]))

        if self.cap_bottom:
            if hollow:
                for i in range(n):
                    if inward:
                        floor_idx.append(nf([in_bot[i], in_bot[i+1], out_bot[i+1], out_bot[i]]))
                    else:
                        floor_idx.append(nf([out_bot[i+1], out_bot[i], in_bot[i], in_bot[i+1]]))
            else:
                ox = (offset.x if offset else 0.0)
                oy = (offset.y if offset else 0.0)
                oz = (offset.z if offset else 0.0)
                cb = bm.verts.new((ox, oy, oz))
                bm.verts.ensure_lookup_table()
                for i in range(n):
                    if inward:
                        floor_idx.append(nf([out_bot[i], out_bot[i+1], cb]))
                    else:
                        floor_idx.append(nf([out_bot[i+1], out_bot[i], cb]))

        if self.cap_ends and partial:
            if not hollow:
                ox = (offset.x if offset else 0.0)
                oy = (offset.y if offset else 0.0)
                oz = (offset.z if offset else 0.0)
                ctr_bot = bm.verts.new((ox, oy, oz))
                ctr_top = bm.verts.new((ox, oy, oz + h))
                bm.verts.ensure_lookup_table()
            for side in (0, n):
                if hollow:
                    if inward:
                        wall_idx.append(nf([in_bot[side], out_bot[side], out_top[side], in_top[side]]))
                    else:
                        wall_idx.append(nf([out_bot[side], in_bot[side], in_top[side], out_top[side]]))
                else:
                    if inward:
                        wall_idx.append(nf([ctr_bot, out_bot[side], out_top[side], ctr_top]))
                    else:
                        wall_idx.append(nf([out_bot[side], ctr_bot, ctr_top, out_top[side]]))

        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)
        wall_set  = set(wall_idx)
        floor_set = set(floor_idx)
        ceil_set  = set(ceil_idx)

        if edit_mode:
            _assign_mats_edit(obj, bm, wall_set, floor_set, ceil_set)
            bmesh.update_edit_mesh(mesh)
            self.report({'INFO'}, 'FBXMT Partial Cylinder added into active mesh')
        else:
            mesh = bpy.data.meshes.new('FBXMT_PartialCylinder')
            bm.to_mesh(mesh)
            bm.free()
            mesh.update()
            obj = bpy.data.objects.new('FBXMT_PartialCylinder', mesh)
            context.collection.objects.link(obj)
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
            obj.location = context.scene.cursor.location
            face_mat_map = {}
            if wall_set:  face_mat_map['M_FBXMT_Wall']    = wall_set
            if ceil_set:  face_mat_map['M_FBXMT_Ceiling'] = ceil_set
            if floor_set: face_mat_map['M_FBXMT_Floor']   = floor_set
            if face_mat_map:
                _assign_materials(obj, face_mat_map)
            self.report({'INFO'}, f'FBXMT Partial Cylinder added — {len(mesh.polygons)} faces')
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.label(text='Shape')
        layout.prop(self, 'radius')
        layout.prop(self, 'inner_radius')
        layout.prop(self, 'height')
        layout.prop(self, 'sides')
        layout.prop(self, 'arc_sweep')
        layout.prop(self, 'arc_start')
        layout.prop(self, 'normal_direction')
        layout.separator()
        layout.label(text='Caps')
        layout.prop(self, 'cap_top')
        layout.prop(self, 'cap_bottom')
        row = layout.row()
        row.prop(self, 'cap_ends')
        row.enabled = self.arc_sweep < math.radians(359.9)


# ─── Truncated Cone (simple) ──────────────────────────────────────────────────

class FBXMT_OT_AddTruncatedCone(Operator):
    """Add a truncated cone with angled cut plane."""
    bl_idname  = 'fbxmt.add_truncated_cone'
    bl_label   = 'Truncated Cone (Simple)'
    bl_options = {'REGISTER', 'UNDO'}

    radius_x: FloatProperty(name='Radius X', default=2.0, min=0.01, soft_max=100.0, unit='LENGTH')
    radius_y: FloatProperty(name='Radius Y', default=2.0, min=0.01, soft_max=100.0, unit='LENGTH')
    height:   FloatProperty(name='Height',   default=4.0, min=0.01, soft_max=100.0, unit='LENGTH')
    cut_height: FloatProperty(name='Cut Height', default=3.0, min=0.01, soft_max=100.0, unit='LENGTH')
    cut_angle:  FloatProperty(name='Cut Angle',  default=0.0, min=0.0, max=math.radians(89.0), subtype='ANGLE')
    cut_rotation: FloatProperty(name='Cut Rotation', default=0.0, min=0.0, max=math.radians(360.0), subtype='ANGLE')
    sides: IntProperty(name='Sides', default=16, min=3, max=256)
    cap_bottom: BoolProperty(name='Base Cap', default=True)
    cap_style: EnumProperty(name='Cap Style', items=CAP_STYLE_ITEMS, default='VANILLA')

    @classmethod
    def poll(cls, context):
        return context.mode in ('OBJECT', 'EDIT_MESH')

    def execute(self, context):
        edit_mode = context.mode == 'EDIT_MESH'
        style = self.cap_style
        n     = self.sides

        if style == 'GRID_FILL' and n % 2 != 0:
            n = n + 1
            self.report({'INFO'}, f'Grid Fill requires even sides — rounded up to {n}')

        rx    = self.radius_x
        ry    = self.radius_y
        h     = self.height
        cut_h = min(self.cut_height, h)
        cut_a = self.cut_angle
        cut_r = self.cut_rotation

        t      = cut_h / h
        rx_cut = rx * (1.0 - t)
        ry_cut = ry * (1.0 - t)
        min_r  = min(rx_cut, ry_cut)
        max_ca = math.atan2(min_r, h - cut_h) if h > cut_h else math.radians(85.0)
        cut_a  = min(cut_a, max_ca)
        tilt_x = math.cos(cut_r)
        tilt_y = math.sin(cut_r)

        if edit_mode:
            obj  = context.active_object
            mesh = obj.data
            bm   = bmesh.from_edit_mesh(mesh)
            off  = obj.matrix_world.inverted() @ context.scene.cursor.location
        else:
            bm  = bmesh.new()
            off = None

        def vert(x, y, z):
            return bm.verts.new((x + off.x, y + off.y, z + off.z) if off else (x, y, z))

        base_verts = []
        cut_verts  = []
        for i in range(n):
            angle = (2 * math.pi / n) * i
            base_verts.append(vert(rx * math.cos(angle), ry * math.sin(angle), 0.0))
            cx = rx_cut * math.cos(angle)
            cy = ry_cut * math.sin(angle)
            cz = cut_h + math.tan(cut_a) * (cx * tilt_x + cy * tilt_y)
            cut_verts.append(vert(cx, cy, cz))

        bm.verts.ensure_lookup_table()

        wall_idx  = []
        floor_idx = []
        ceil_idx  = []

        for i in range(n):
            j = (i + 1) % n
            f = bm.faces.new([base_verts[i], base_verts[j], cut_verts[j], cut_verts[i]])
            wall_idx.append(f.index)

        if self.cap_bottom:
            floor_idx += _fill_cap_bm(bm, base_verts, is_floor=True, style=style, off=off)

        ceil_idx += _fill_cap_bm(bm, list(reversed(cut_verts)), is_floor=False, style=style, off=off)

        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)
        wall_set  = set(wall_idx)
        floor_set = set(floor_idx)
        ceil_set  = set(ceil_idx)

        if edit_mode:
            _assign_mats_edit(obj, bm, wall_set, floor_set, ceil_set)
            bmesh.update_edit_mesh(mesh)
            self.report({'INFO'}, 'FBXMT Truncated Cone added into active mesh')
        else:
            mesh = bpy.data.meshes.new('FBXMT_TruncatedCone')
            bm.to_mesh(mesh)
            bm.free()
            mesh.update()
            obj = bpy.data.objects.new('FBXMT_TruncatedCone', mesh)
            context.collection.objects.link(obj)
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
            obj.location = context.scene.cursor.location
            face_mat_map = {}
            if wall_set:  face_mat_map['M_FBXMT_Wall']    = wall_set
            if ceil_set:  face_mat_map['M_FBXMT_Ceiling'] = ceil_set
            if floor_set: face_mat_map['M_FBXMT_Floor']   = floor_set
            if face_mat_map:
                _assign_materials(obj, face_mat_map)
            self.report({'INFO'}, f'FBXMT Truncated Cone added — {len(mesh.polygons)} faces')
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.label(text='Base')
        layout.prop(self, 'radius_x')
        layout.prop(self, 'radius_y')
        layout.prop(self, 'height')
        layout.prop(self, 'sides')
        layout.separator()
        layout.label(text='Cut Plane')
        layout.prop(self, 'cut_height')
        layout.prop(self, 'cut_angle')
        layout.prop(self, 'cut_rotation')
        layout.separator()
        layout.prop(self, 'cap_bottom')
        layout.prop(self, 'cap_style')


# ─── GPU preview helpers ───────────────────────────────────────────────────────

def _get_inverted_edge_colour():
    try:
        theme = bpy.context.preferences.themes[0]
        ec = theme.view_3d.edge_facesel
        return (1.0 - ec[0], 1.0 - ec[1], 1.0 - ec[2], 0.4)
    except Exception:
        return (1.0, 0.8, 0.2, 0.4)


def _build_cone_preview(rx, ry, h, cut_h, cut_a, cut_r, n, cap_bottom, cap_style):
    t      = min(cut_h, h) / h
    rx_cut = rx * (1.0 - t)
    ry_cut = ry * (1.0 - t)
    tilt_x = math.cos(cut_r)
    tilt_y = math.sin(cut_r)
    tan_a  = math.tan(cut_a)

    base_pts = []
    cut_pts  = []
    for i in range(n):
        a = (2 * math.pi / n) * i
        base_pts.append((rx * math.cos(a), ry * math.sin(a), 0.0))
        cx = rx_cut * math.cos(a)
        cy = ry_cut * math.sin(a)
        cut_pts.append((cx, cy, cut_h + tan_a * (cx * tilt_x + cy * tilt_y)))

    side_tris = []
    for i in range(n):
        j = (i + 1) % n
        side_tris += [base_pts[i], base_pts[j], cut_pts[j],
                      base_pts[i], cut_pts[j],  cut_pts[i]]

    cx_c = sum(p[0] for p in cut_pts) / n
    cy_c = sum(p[1] for p in cut_pts) / n
    cz_c = sum(p[2] for p in cut_pts) / n
    cut_ctr  = (cx_c, cy_c, cz_c)
    cut_tris = []
    for i in range(n):
        j = (i + 1) % n
        cut_tris += [cut_ctr, cut_pts[i], cut_pts[j]]

    base_tris = []
    if cap_bottom and cap_style != 'NONE':
        bc = (0.0, 0.0, 0.0)
        for i in range(n):
            j = (i + 1) % n
            base_tris += [bc, base_pts[j], base_pts[i]]

    cut_outline = cut_pts + [cut_pts[0]]
    grid_lines  = []
    if cap_style in ('VANILLA', 'GRID_FILL'):
        for pt in cut_pts:
            grid_lines += [cut_ctr, pt]
        if cap_bottom:
            bc = (0.0, 0.0, 0.0)
            for pt in base_pts:
                grid_lines += [bc, pt]
            grid_lines += base_pts + [base_pts[0]]

    return side_tris, base_tris, cut_tris, cut_outline, grid_lines


# ─── Truncated Cone Modal ─────────────────────────────────────────────────────

_active_modal = None


class FBXMT_OT_TruncatedConeModal(Operator):
    """Add a truncated cone with live GPU preview. Adjust in FBXMT Primitives N panel."""
    bl_idname  = 'fbxmt.add_truncated_cone_modal'
    bl_label   = 'Truncated Cone'
    bl_options = {'REGISTER', 'UNDO'}

    radius_x: FloatProperty(name='Radius X', default=2.0, min=0.01, soft_max=100.0, unit='LENGTH')
    radius_y: FloatProperty(name='Radius Y', default=2.0, min=0.01, soft_max=100.0, unit='LENGTH')
    height:   FloatProperty(name='Height',   default=4.0, min=0.01, soft_max=100.0, unit='LENGTH')
    cut_height: FloatProperty(name='Cut Height', default=3.0, min=0.01, soft_max=100.0, unit='LENGTH')
    cut_angle:  FloatProperty(name='Cut Angle',  default=0.0, min=0.0, max=math.radians(89.0), subtype='ANGLE')
    cut_rotation: FloatProperty(name='Cut Rotation', default=0.0, min=0.0, max=math.radians(360.0), subtype='ANGLE')
    sides: IntProperty(name='Sides', default=16, min=3, max=256)
    cap_bottom: BoolProperty(name='Base Cap', default=True)
    cap_style: EnumProperty(name='Cap Style', items=CAP_STYLE_ITEMS, default='VANILLA')

    _handle = None
    _origin = None

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def _clamped_cut_angle(self):
        t     = min(self.cut_height, self.height) / self.height
        rx_c  = self.radius_x * (1.0 - t)
        ry_c  = self.radius_y * (1.0 - t)
        dist  = self.height - min(self.cut_height, self.height)
        max_a = math.atan2(min(rx_c, ry_c), dist) if dist > 0 else math.radians(85.0)
        return min(self.cut_angle, max_a)

    def _draw_callback(self, context):
        if self._origin is None:
            return
        n     = self.sides
        cut_a = self._clamped_cut_angle()
        side_tris, base_tris, cut_tris, cut_outline, grid_lines = _build_cone_preview(
            self.radius_x, self.radius_y, self.height,
            min(self.cut_height, self.height),
            cut_a, self.cut_rotation, n, self.cap_bottom, self.cap_style,
        )
        ox, oy, oz = self._origin

        def off(pts):
            return [(p[0]+ox, p[1]+oy, p[2]+oz) for p in pts]

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.blend_set('ALPHA')

        if side_tris:
            batch = batch_for_shader(shader, 'TRIS', {'pos': off(side_tris)})
            shader.uniform_float('color', (0.5, 0.5, 0.5, 0.25))
            batch.draw(shader)

        if base_tris:
            batch = batch_for_shader(shader, 'TRIS', {'pos': off(base_tris)})
            shader.uniform_float('color', (0.4, 0.7, 0.4, 0.25))
            batch.draw(shader)

        col = _get_inverted_edge_colour()

        if cut_tris:
            batch = batch_for_shader(shader, 'TRIS', {'pos': off(cut_tris)})
            shader.uniform_float('color', col)
            batch.draw(shader)

        if cut_outline:
            batch = batch_for_shader(shader, 'LINE_STRIP', {'pos': off(cut_outline)})
            shader.uniform_float('color', (col[0], col[1], col[2], 0.9))
            gpu.state.line_width_set(2.0)
            batch.draw(shader)

        if grid_lines:
            batch = batch_for_shader(shader, 'LINES', {'pos': off(grid_lines)})
            shader.uniform_float('color', (col[0], col[1], col[2], 0.6))
            gpu.state.line_width_set(1.0)
            batch.draw(shader)

        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('NONE')
        gpu.state.line_width_set(1.0)

    def invoke(self, context, event):
        global _active_modal
        self._origin = tuple(context.scene.cursor.location)
        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_callback, (context,), 'WINDOW', 'POST_VIEW'
        )
        _active_modal = self
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set('FBXMT Truncated Cone — adjust in N panel · Enter confirm · Esc cancel')
        self._tag_ui(context)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        context.area.tag_redraw()
        self._tag_ui(context)
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            return self._confirm(context)
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._cancel(context)
        return {'PASS_THROUGH'}

    def _confirm(self, context):
        global _active_modal
        self._remove_handler()
        context.area.header_text_set(None)
        _active_modal = None
        self._tag_ui(context)
        n = self.sides
        if self.cap_style == 'GRID_FILL' and n % 2 != 0:
            n = n + 1
        bpy.ops.fbxmt.add_truncated_cone(
            radius_x=self.radius_x, radius_y=self.radius_y,
            height=self.height, cut_height=self.cut_height,
            cut_angle=self.cut_angle, cut_rotation=self.cut_rotation,
            sides=n, cap_bottom=self.cap_bottom, cap_style=self.cap_style,
        )
        return {'FINISHED'}

    def _cancel(self, context):
        global _active_modal
        self._remove_handler()
        context.area.header_text_set(None)
        _active_modal = None
        context.area.tag_redraw()
        self._tag_ui(context)
        return {'CANCELLED'}

    def _remove_handler(self):
        if self._handle:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            self._handle = None

    def _tag_ui(self, context):
        for region in context.area.regions:
            if region.type == 'UI':
                region.tag_redraw()

    def draw(self, context):
        pass


# ─── N Panel ──────────────────────────────────────────────────────────────────

class FBXMT_PT_Primitives(bpy.types.Panel):
    bl_label       = 'FBXMT Primitives'
    bl_idname      = 'FBXMT_PT_Primitives'
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = 'FBXMT Primitives'
    bl_order       = 0

    def draw(self, context):
        layout = self.layout
        global _active_modal

        if _active_modal is not None:
            op = _active_modal
            layout.label(text=op.bl_label, icon='MESH_CONE')
            layout.separator()
            col = layout.column(align=True)
            col.use_property_split = True
            col.prop(op, 'radius_x')
            col.prop(op, 'radius_y')
            col.prop(op, 'height')
            col.prop(op, 'sides')
            layout.separator()
            layout.label(text='Cut Plane')
            col2 = layout.column(align=True)
            col2.use_property_split = True
            col2.prop(op, 'cut_height')
            col2.prop(op, 'cut_angle')
            col2.prop(op, 'cut_rotation')
            layout.separator()
            col3 = layout.column(align=True)
            col3.prop(op, 'cap_bottom')
            col3.prop(op, 'cap_style')
            layout.separator()
            row = layout.row(align=True)
            row.scale_y = 1.4
            row.operator('fbxmt.truncated_cone_confirm', text='Confirm', icon='CHECKMARK')
            row.operator('fbxmt.truncated_cone_cancel',  text='Cancel',  icon='X')
        else:
            layout.label(text='Add Primitive', icon='MESH_DATA')
            layout.separator()
            layout.operator('fbxmt.add_partial_cylinder',     icon='MESH_CYLINDER',
                            text='Partial Cylinder')
            layout.operator('fbxmt.add_truncated_cone_modal', icon='MESH_CONE',
                            text='Truncated Cone')
            layout.separator()
            layout.label(text='Simple (no preview)')
            layout.operator('fbxmt.add_truncated_cone', icon='MESH_CONE',
                            text='Truncated Cone (Simple)')


class FBXMT_OT_TruncatedConeConfirm(bpy.types.Operator):
    bl_idname  = 'fbxmt.truncated_cone_confirm'
    bl_label   = 'Confirm'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global _active_modal
        if _active_modal is not None:
            _active_modal._confirm(context)
        return {'FINISHED'}


class FBXMT_OT_TruncatedConeCancel(bpy.types.Operator):
    bl_idname  = 'fbxmt.truncated_cone_cancel'
    bl_label   = 'Cancel'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global _active_modal
        if _active_modal is not None:
            _active_modal._cancel(context)
        return {'FINISHED'}


# ─── Registration ─────────────────────────────────────────────────────────────

CLASSES = (
    FBXMT_OT_AddPartialCylinder,
    FBXMT_OT_AddTruncatedCone,
    FBXMT_OT_TruncatedConeModal,
    FBXMT_OT_TruncatedConeConfirm,
    FBXMT_OT_TruncatedConeCancel,
    FBXMT_PT_Primitives,
)


def register_primitives():
    for c in CLASSES:
        try:
            bpy.utils.register_class(c)
        except Exception:
            try:
                bpy.utils.unregister_class(c)
                bpy.utils.register_class(c)
            except Exception:
                pass


def unregister_primitives():
    for c in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
