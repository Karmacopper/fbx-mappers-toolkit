import bpy
from bpy.types import Panel, AddonPreferences, UIList
from .materials import LIGHTMAP_CHANNEL_NAME, PREVIEW_UV_NAME
from .props import FBXMT_GlobalPrefs, FBXMT_Props, PATTERN_ITEMS, _COLOR_B_MODE_ITEMS

# Slot key for each material in display order — must match props naming convention
_MAT_DISPLAY_ORDER = [
    ('floor',   'M_FBXMT_Floor'),
    ('ceiling', 'M_FBXMT_Ceiling'),
    ('wall',    'M_FBXMT_Wall'),
    ('trim',    'M_FBXMT_Trim'),
    ('ignore',  'M_FBXMT_Ignore'),
    ('island',  'M_FBXMT_Island'),
]


# Name → slot key mapping (inverse of _MAT_DISPLAY_ORDER)
_MAT_NAME_TO_SLOT = {mat_name: slot for slot, mat_name in _MAT_DISPLAY_ORDER}


def _draw_preset_lock_ticker(layout, prefs):
    """Draw the lock ticker. Always visible above material controls.

    Locked with preset name: 'Locked: "Brutalist Grey"'
    Locked manually:         'Settings locked'
    Unlocked:                'Lock settings'
    Operator flips preset_locked and clears active_preset_name on unlock.
    """
    locked = prefs.preset_locked
    name   = prefs.active_preset_name.strip()
    row    = layout.row(align=True)
    if locked:
        label = f'Locked: "{name}"' if name else 'Settings locked'
        icon  = 'LOCKED'
    else:
        label = 'Lock settings'
        icon  = 'UNLOCKED'
    row.operator(
        'fbxmt.toggle_preset_lock',
        text    = label,
        icon    = icon,
        depress = locked,
    )


def _draw_material_colour_controls(layout, prefs, slot):
    """Draw the pattern dropdown, colour B mode dropdown, and conditional B control.

    All controls are disabled when prefs.preset_locked is True.
    Caller should draw _draw_preset_lock_ticker above this.
    """
    locked = prefs.preset_locked

    pattern_prop  = f'checker_pattern_{slot}'
    mode_prop     = f'color_b_mode_{slot}'
    col_a_prop    = f'color_{slot}_a'
    col_b_prop    = f'color_{slot}_b'
    darker_prop   = f'color_b_darker_{slot}'
    grey_prop     = f'color_b_grey_{slot}'

    # ── Two dropdowns side by side ────────────────────────────────────────────
    row = layout.row(align=True)
    row.enabled = not locked
    row.prop(prefs, pattern_prop, text="")
    row.prop(prefs, mode_prop,    text="")

    # ── Box: Colour A always, Colour B conditional ────────────────────────────
    box  = layout.box()
    box.enabled = not locked
    mode = getattr(prefs, mode_prop, 'MANUAL')

    # Colour A — always a free picker, full width
    box.prop(prefs, col_a_prop, text="A")

    # Colour B — depends on mode, below A
    if mode == 'MANUAL':
        box.prop(prefs, col_b_prop, text="B")

    elif mode == 'DARKER':
        box.label(text="B — Lighter / Darker")
        box.prop(prefs, darker_prop, text="", slider=True)

    elif mode == 'GREYSCALE':
        box.label(text="B — Grey Level")
        box.prop(prefs, grey_prop, text="", slider=True)

    elif mode == 'INVERSE':
        box.label(text="B — inverse of A", icon='INFO')



# Blender 5.x extensions use a prefixed package name at runtime.
# We store the resolved ID at register() time via set_addon_id().
ADDON_ID = __package__


def set_addon_id(pkg):
    global ADDON_ID
    ADDON_ID = pkg


class FBXMT_AddonPreferences(AddonPreferences):
    bl_idname = __package__  # patched in register() before register_class

    show_setup_on_new: bpy.props.BoolProperty(
        name="Show Project Setup on New Project",
        description="Automatically open the Project Setup window when creating a new FBXMT project from the template",
        default=True,
    )

    enable_primitives: bpy.props.BoolProperty(
        name="Enable FBXMT Primitives",
        description="Add FBXMT Primitives submenu to Shift+A. Disable to keep the menu clean if you don't need level design primitives",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Preferences are in the FBX Toolkit N-panel.", icon="INFO")
        layout.label(text="Open the 3D Viewport, press N, select the FBX Toolkit tab.")
        layout.prop(self, "show_setup_on_new")
        layout.prop(self, "enable_primitives")



class FBXMT_UL_UVMaps(UIList):
    bl_idname = "FBXMT_UL_UVMaps"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        row         = layout.row(align=True)
        is_lightmap = item.name == LIGHTMAP_CHANNEL_NAME
        icon_name   = "LIGHT_DATA" if is_lightmap else "GROUP_UVS"
        row.prop(item, "name", text="", emboss=False, icon=icon_name)
        if is_lightmap:
            row.label(text="", icon="LOCKED")
        elif item.active_render:
            row.label(text="", icon="RESTRICT_RENDER_OFF")
    # No filter_items override — default behaviour is correct



# ─── Main Panel ───────────────────────────────────────────────────────────────

class FBXMT_PT_Main(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "FBX Mapper's Toolkit"
    bl_category    = "FBX Toolkit"

    def draw(self, context):
        from . import __version__
        self.layout.label(text=f"v{__version__}", icon='INFO')


# ─── Scene Setup ──────────────────────────────────────────────────────────────

class FBXMT_PT_SceneSetup(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "Scene Setup"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"
    bl_order       = 5

    def draw(self, context):
        layout = self.layout
        props  = context.scene.fbxmt_props

        # Collection status indicators
        col_row = layout.row(align=True)
        for col_name in ('Geo', 'Props', 'Trim'):
            exists = col_name in bpy.data.collections
            col_row.label(text=col_name, icon="CHECKMARK" if exists else "X")

        layout.separator()
        row = layout.row()
        row.scale_y = 1.5
        row.operator("fbxmt.project_setup", text="Project Setup…", icon="SETTINGS")
        layout.separator()
        layout.operator("fbxmt.save_template", text="Save Startup Template", icon="LAYERGROUP_COLOR_02")
        layout.label(text="Saves current scene as template — restart Blender after", icon="INFO")
        layout.separator()
        addon_prefs = bpy.context.preferences.addons.get(ADDON_ID)
        if addon_prefs:
            row = layout.row()
            row.label(text='Show Setup on New Project')
            row.prop(addon_prefs.preferences, 'show_setup_on_new', text='')


# ─── Materials ────────────────────────────────────────────────────────────────

class FBXMT_PT_Materials(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "Materials"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"
    bl_order       = 3

    def draw(self, context):
        layout = self.layout
        scene  = context.scene
        obj    = context.active_object
        in_edit = context.mode == 'EDIT_MESH'

        # ── Toolbar ───────────────────────────────────────────────────────────
        row = layout.row(align=True)
        row.scale_y = 1.2
        row.operator("fbxmt.rebuild_materials", text="Rebuild", icon="FILE_REFRESH")

        row = layout.row(align=True)
        row.scale_y = 1.2
        row.operator("fbxmt.assign_materials", text="Auto-Assign by Normal", icon="FACE_MAPS")

        alert_col = layout.column()
        alert_col.alert = True
        alert_col.scale_y = 1.1
        alert_col.menu("FBXMT_MT_Clear_Menu", text="Clear...", icon="TRASH")

        layout.separator()

        # ── Material list ─────────────────────────────────────────────────────
        mat_box = layout.box()
        mat_box.label(text="Materials", icon="MATERIAL")
        mat_box.template_list(
            "FBXMT_UL_all_materials", "",
            bpy.data, "materials",
            scene, "fbxmt_base_list_index",
            rows=6,
        )

        layout.separator()

        # ── Face assignment ───────────────────────────────────────────────────
        face_box = layout.box()
        face_box.label(
            text="Edit Mode - select faces, then:",
            icon="EDITMODE_HLT" if in_edit else "INFO",
        )
        face_row = face_box.row(align=True)
        face_row.scale_y = 1.3
        face_row.operator("fbxmt.assign_to_faces",    text="Assign",   icon="BRUSH_DATA")
        face_row.operator("fbxmt.select_by_material", text="Select",   icon="RESTRICT_SELECT_OFF")

        layout.separator()

        # ── Island colouring ──────────────────────────────────────────────────
        island_row = layout.row()
        island_row.scale_y = 1.2
        island_row.operator("fbxmt.colour_islands", text="Auto-Colour Islands", icon="OUTLINER_OB_LATTICE")


# ─── Import ───────────────────────────────────────────────────────────────────

class FBXMT_PT_Import(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "Import"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"
    bl_order       = 1
    bl_options     = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout  = self.layout
        props   = context.scene.fbxmt_props

        row = layout.row(align=True)
        row.scale_y = 1.3
        row.menu("FBXMT_MT_Import_Dropdown", text="Import...", icon="IMPORT")

        # Quick Import — fires the stored import type in one click
        qt = props.quick_import_type
        op = row.operator("fbxmt.import_fbx", text="Quick Import", icon="INDIRECT_ONLY_ON")
        op.import_type = qt

        layout.prop(props, "quick_import_type", text="")


# ─── UV Maps & Unwrap ─────────────────────────────────────────────────────────

class FBXMT_PT_UVUnwrap(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "UV Maps & Unwrap"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"
    bl_order       = 2

    def draw(self, context):
        layout = self.layout
        props  = context.scene.fbxmt_props
        obj    = context.active_object
        mesh   = obj.data if obj and obj.type == 'MESH' else None

        if mesh:
            row = layout.row()
            row.template_list(
                "FBXMT_UL_UVMaps", "",
                mesh, "uv_layers",
                mesh.uv_layers, "active_index",
                rows=3,
            )
            col = row.column(align=True)
            col.operator("fbxmt.uv_add", text="", icon="ADD")
            remove_row = col.row()
            active_uv  = mesh.uv_layers.active
            remove_row.enabled = (
                active_uv is not None and
                active_uv.name != LIGHTMAP_CHANNEL_NAME and
                active_uv.name != PREVIEW_UV_NAME
            )
            remove_row.operator("fbxmt.uv_remove", text="", icon="REMOVE")
        else:
            layout.label(text="No mesh selected", icon="INFO")

        layout.separator()

        layout.prop(props, "uv_floor_threshold")

        # Disable unwrap if selected object is in Trim or Props collection
        in_trim_or_props = False
        if obj:
            for col in obj.users_collection:
                if col.name in ('Trim', 'Props'):
                    in_trim_or_props = True
                    break

        row = layout.row()
        row.scale_y = 1.3
        row.enabled = not in_trim_or_props
        row.operator(
            "fbxmt.uv_unwrap",
            text="Unwrap Selected Faces" if context.mode == "EDIT_MESH"
                 else "Unwrap Selected Objects",
            icon="UV_DATA",
        )
        if in_trim_or_props:
            layout.label(text="Unwrap disabled for Trim/Props", icon="INFO")

        # Smart Pack — hidden until ready
        # row = layout.row()
        # row.scale_y = 1.2
        # row.operator("fbxmt.smart_pack", text="Smart Pack UVs", icon="SORTSIZE")

        row = layout.row()
        row.scale_y = 1.2
        row.operator("fbxmt.uv_preview", text="Preview UVs as Mesh", icon="MESH_GRID")



# ─── Export ───────────────────────────────────────────────────────────────────

class FBXMT_PT_Export(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "Export"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"
    bl_order       = 4
    bl_options     = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props  = context.scene.fbxmt_props
        prefs  = context.scene.fbxmt_prefs_global

        if not props.export_path:
            layout.label(text="No export folder set - open Preferences panel", icon="ERROR")
        else:
            layout.label(text=props.export_path, icon="FILE_FOLDER")

        layout.separator()

        scale_box = layout.box()
        scale_box.label(text="Scale")
        scale_box.prop(props, "apply_scale")
        row = scale_box.row()
        row.enabled = props.apply_scale
        row.prop(props, "apply_scale_options", text="")

        layout.separator()
        layout.prop(props, "ucx_generate")
        row = layout.row()
        row.prop(props, "bake_textures")

        row = layout.row()
        row.prop(
            props, "lightmap_force_regenerate",
            text  = "Force Regenerate Lightmap" if props.lightmap_force_regenerate
                    else "Ensure Lightmap Exists",
            icon  = "LIGHT" if props.lightmap_force_regenerate else "LIGHT_DATA",
        )

        layout.separator()

        row = layout.row()
        row.scale_y = 1.2
        row.operator("fbxmt.check_mesh", text="Check Mesh", icon="CHECKMARK")

        row = layout.row()
        row.scale_y = 1.4
        row.enabled = bool(props.export_path)
        row.operator("unreal.collision_exporter", text="Export Selected", icon="EXPORT")


# ─── Trim Generation ──────────────────────────────────────────────────────────

class FBXMT_PT_TrimGen(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "Trim Generation"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"
    bl_order       = 6
    bl_options     = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props  = context.scene.fbxmt_props
        in_edit = context.mode == 'EDIT_MESH'

        # Dimensions
        box_fw = layout.box()
        box_fw.label(text="Floor / Wall Trim", icon='EDGESEL')
        col = box_fw.column(align=True)
        col.prop(props, 'trim_thickness')
        col.prop(props, 'trim_horiz_cover', text='Floor Cover Depth')
        col.prop(props, 'trim_vert_cover',  text='Wall Cover Depth')

        box_ww = layout.box()
        box_ww.label(text="Wall Run Trim", icon='EDGESEL')
        col_ww = box_ww.column(align=True)
        col_ww.prop(props, 'trim_wall_a_cover', text='Wall A Depth')
        col_ww.prop(props, 'trim_wall_b_cover', text='Wall B Depth')

        layout.separator()

        # Cap style
        box = layout.box()
        box.label(text="Cap Style", icon='MOD_BEVEL')
        col2 = box.column(align=True)
        row = col2.row(align=True)
        row.label(text="B→D Cap")
        row.prop(props, 'trim_chamfer_BD', text="Chamfer", toggle=True)
        row2 = col2.row(align=True)
        row2.label(text="D→F Cap")
        row2.prop(props, 'trim_chamfer_DF', text="Chamfer", toggle=True)

        layout.separator()

        if not in_edit:
            col3 = layout.column()
            col3.label(text="Enter Edit Mode and", icon='INFO')
            col3.label(text="select seam edges first.")

        row3 = layout.row()
        row3.scale_y = 1.4
        row3.enabled = in_edit
        row3.operator('fbxmt.generate_trim', text='Generate Trim', icon='MOD_SOLIDIFY')
