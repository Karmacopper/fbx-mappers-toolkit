# grid.py — FBX Mapper's Toolkit
#
# UT99-style power-of-2 snap grid.
#
# Scale: 1 BU = 16 UU (48 UU ≈ 1m at standard UT99 scale)
# Grid sizes (UU): 1, 2, 4, 8, 16, 32, 64, 128, 256, 512
#
# NOTE: Blender 5.1 does not expose snap_increment or allow visual grid
# scale to be set from Python. This module controls snap on/off and
# tracks the active UU grid size for display purposes only.
# Visual grid manipulation is not possible — blocked by Blender 5.1 API.

import bpy
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import BoolProperty, IntProperty, PointerProperty

# ─── Constants ────────────────────────────────────────────────────────────────

# Grid step sizes in UU — powers of 2
GRID_STEPS_UU = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]

DEFAULT_GRID_INDEX = 4  # 16 UU


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _apply_snap(context):
    """Apply snap settings for UU grid mode."""
    props = context.scene.fbxmt_grid
    ts    = context.scene.tool_settings
    try:
        ts.use_snap = props.snap_enabled
    except Exception:
        pass
    try:
        ts.snap_elements = {'GRID'}
    except Exception:
        pass
    try:
        ts.use_snap_grid_absolute = True
    except Exception:
        pass


def _snapshot_snap(context):
    """Save current snap state."""
    snap = context.scene.fbxmt_grid.blender_snapshot
    ts   = context.scene.tool_settings
    try:
        snap.use_snap = ts.use_snap
    except Exception:
        pass
    try:
        snap.snap_elements = ' '.join(ts.snap_elements)
    except Exception:
        pass
    try:
        snap.use_snap_grid_absolute = getattr(ts, 'use_snap_grid_absolute', False)
    except Exception:
        pass
    snap.captured = True


def _restore_snap(context):
    """Restore saved snap state."""
    snap = context.scene.fbxmt_grid.blender_snapshot
    ts   = context.scene.tool_settings
    if not snap.captured:
        return
    try:
        ts.use_snap = snap.use_snap
    except Exception:
        pass
    if snap.snap_elements:
        try:
            ts.snap_elements = set(snap.snap_elements.split())
        except Exception:
            pass
    try:
        ts.use_snap_grid_absolute = snap.use_snap_grid_absolute
    except Exception:
        pass


# ─── Property groups ──────────────────────────────────────────────────────────

class FBXMT_GridSnapshot(PropertyGroup):
    captured:               BoolProperty(default=False)
    use_snap:               BoolProperty(default=False)
    snap_elements:          bpy.props.StringProperty(default='GRID')
    use_snap_grid_absolute: BoolProperty(default=False)


class FBXMT_GridProps(PropertyGroup):

    uu_grid_active: BoolProperty(
        name        = 'UU Grid Active',
        description = 'Enable UT99-style power-of-2 snap grid',
        default     = False,
    )

    grid_step_index: IntProperty(
        name    = 'Grid Step Index',
        default = DEFAULT_GRID_INDEX,
        min     = 0,
        max     = len(GRID_STEPS_UU) - 1,
    )

    snap_enabled: BoolProperty(
        name        = 'Snap',
        description = 'Enable grid snapping',
        default     = True,
    )

    blender_snapshot: PointerProperty(type=FBXMT_GridSnapshot)


# ─── Operators ────────────────────────────────────────────────────────────────

class FBXMT_OT_GridToggle(Operator):
    """Toggle UT99-style UU snap grid on/off."""
    bl_idname  = 'fbxmt.grid_toggle'
    bl_label   = 'Toggle UU Grid'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.fbxmt_grid
        props.uu_grid_active = not props.uu_grid_active
        if props.uu_grid_active:
            _snapshot_snap(context)
            _apply_snap(context)
        else:
            _restore_snap(context)
        return {'FINISHED'}


class FBXMT_OT_GridStepUp(Operator):
    """Increase UU grid size to next power-of-2 step  ]"""
    bl_idname  = 'fbxmt.grid_step_up'
    bl_label   = 'Grid Step Up'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.fbxmt_grid
        if not props.uu_grid_active:
            return {'CANCELLED'}
        if props.grid_step_index >= len(GRID_STEPS_UU) - 1:
            return {'CANCELLED'}
        props.grid_step_index += 1
        return {'FINISHED'}


class FBXMT_OT_GridStepDown(Operator):
    """Decrease UU grid size to previous power-of-2 step  ["""
    bl_idname  = 'fbxmt.grid_step_down'
    bl_label   = 'Grid Step Down'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.fbxmt_grid
        if not props.uu_grid_active:
            return {'CANCELLED'}
        if props.grid_step_index <= 0:
            return {'CANCELLED'}
        props.grid_step_index -= 1
        return {'FINISHED'}


class FBXMT_OT_GridSnapToggle(Operator):
    """Toggle snap on/off."""
    bl_idname  = 'fbxmt.grid_snap_toggle'
    bl_label   = 'Toggle Grid Snap'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.fbxmt_grid
        if not props.uu_grid_active:
            return {'CANCELLED'}
        props.snap_enabled = not props.snap_enabled
        try:
            context.scene.tool_settings.use_snap = props.snap_enabled
        except Exception:
            pass
        return {'FINISHED'}


# ─── Panel ────────────────────────────────────────────────────────────────────

class FBXMT_PT_Grid(Panel):
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_label       = 'Grid'
    bl_category    = 'FBX Toolkit'
    bl_parent_id   = 'FBXMT_PT_Main'
    bl_order       = 4

    def draw(self, context):
        layout = self.layout
        props  = context.scene.fbxmt_grid
        active = props.uu_grid_active

        # ── Toggle ────────────────────────────────────────────────────────────
        row = layout.row(align=True)
        row.scale_y = 1.2
        row.operator('fbxmt.grid_toggle', text='Blender Grid', depress=not active)
        row.operator('fbxmt.grid_toggle', text='^2 Grid',      depress=active)

        if not active:
            return

        layout.separator(factor=0.5)

        # ── UU size ───────────────────────────────────────────────────────────
        uu_val = GRID_STEPS_UU[props.grid_step_index]
        size_row = layout.row(align=True)
        size_row.operator('fbxmt.grid_step_down', text='', icon='TRIA_LEFT')
        col = size_row.column()
        col.alignment = 'CENTER'
        col.label(text=f'{uu_val} UU')
        size_row.operator('fbxmt.grid_step_up', text='', icon='TRIA_RIGHT')

        layout.separator(factor=0.5)

        # ── Snap toggle ───────────────────────────────────────────────────────
        snap_row = layout.row()
        snap_row.operator(
            'fbxmt.grid_snap_toggle',
            text    = 'Snap  ON'  if props.snap_enabled else 'Snap  OFF',
            icon    = 'SNAP_ON'   if props.snap_enabled else 'SNAP_OFF',
            depress = props.snap_enabled,
        )


# ─── Registration ─────────────────────────────────────────────────────────────

_keymap_items = []

GRID_CLASSES = (
    FBXMT_GridSnapshot,
    FBXMT_GridProps,
    FBXMT_OT_GridToggle,
    FBXMT_OT_GridStepUp,
    FBXMT_OT_GridStepDown,
    FBXMT_OT_GridSnapToggle,
    FBXMT_PT_Grid,
)


def register():
    for cls in GRID_CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.fbxmt_grid = PointerProperty(type=FBXMT_GridProps)

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        for mode in ('Object Mode', 'Mesh'):
            km = kc.keymaps.new(name=mode, space_type='VIEW_3D')
            kmi_down = km.keymap_items.new('fbxmt.grid_step_down', type='LEFT_BRACKET',  value='PRESS')
            kmi_up   = km.keymap_items.new('fbxmt.grid_step_up',   type='RIGHT_BRACKET', value='PRESS')
            _keymap_items.append((km, kmi_down))
            _keymap_items.append((km, kmi_up))


def unregister():
    for km, kmi in _keymap_items:
        km.keymap_items.remove(kmi)
    _keymap_items.clear()

    try:
        del bpy.types.Scene.fbxmt_grid
    except Exception:
        pass

    for cls in reversed(GRID_CLASSES):
        bpy.utils.unregister_class(cls)
