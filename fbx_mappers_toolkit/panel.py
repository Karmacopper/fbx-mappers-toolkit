import bpy
from bpy.types import Panel, AddonPreferences, UIList
from .materials import LIGHTMAP_CHANNEL_NAME
from .props import FBXMT_GlobalPrefs, FBXMT_Props


# Blender 5.x extensions use a prefixed package name at runtime.
# We store the resolved ID at register() time via set_addon_id().
ADDON_ID = __package__


def set_addon_id(pkg):
    global ADDON_ID
    ADDON_ID = pkg


class FBXMT_AddonPreferences(AddonPreferences):
    bl_idname = __package__  # patched in register() before register_class

    def draw(self, context):
        layout = self.layout
        layout.label(text="Preferences are in the FBX Toolkit N-panel.", icon="INFO")
        layout.label(text="Open the 3D Viewport, press N, select the FBX Toolkit tab.")



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
        pass


# ─── Scene Setup ──────────────────────────────────────────────────────────────

class FBXMT_PT_SceneSetup(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "Scene Setup"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"

    def draw(self, context):
        layout = self.layout
        props  = context.scene.fbxmt_props

        # Collection status indicators
        col_row = layout.row(align=True)
        for col_name in ('Geo', 'Props', 'Trim'):
            exists = col_name in bpy.data.collections
            col_row.label(text=col_name, icon="CHECKMARK" if exists else "X")

        layout.label(text="Texel Density:", icon="LOCKED")
        btn_row = layout.row(align=True)
        for val in (512, 1024, 2048, 4096, 8192):
            op = btn_row.operator("fbxmt.set_texel_density", text=str(val),
                                  depress=(props.geo_texel_density == val))
            op.value = val
        tile_m = props.geo_texel_density / 1024.0
        layout.label(text=f"Tile: {tile_m:.3g}m  ({props.geo_texel_density}tx/m)", icon="INFO")


        layout.separator()
        layout.operator("fbxmt.save_template", text="Save Startup Template", icon="FILE_BLEND")
        layout.label(text="Saves current scene as template - restart Blender after", icon="INFO")


# ─── Materials ────────────────────────────────────────────────────────────────

class FBXMT_PT_Materials(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "Materials"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"

    def draw(self, context):
        layout = self.layout
        scene  = context.scene
        obj    = context.active_object
        mesh   = obj.data if obj and obj.type == 'MESH' else None
        in_edit = context.mode == 'EDIT_MESH'

        # ── Toolbar ───────────────────────────────────────────────────────────
        row = layout.row(align=True)
        row.scale_y = 1.2
        row.operator("fbxmt.add_materials",     text="Add",     icon="ADD")
        row.operator("fbxmt.rebuild_materials", text="Rebuild", icon="FILE_REFRESH")

        row = layout.row(align=True)
        row.scale_y = 1.2
        row.operator("fbxmt.assign_materials", text="Auto-Assign by Normal", icon="FACE_MAPS")

        row = layout.row()
        row.scale_y = 1.1
        row.alert = True
        row.menu("FBXMT_MT_Clear_Menu", text="Clear...", icon="TRASH")

        if not mesh:
            layout.separator()
            layout.label(text="No mesh selected", icon="INFO")
            return

        layout.separator()

        # ── Base Materials list ───────────────────────────────────────────────
        base_box = layout.box()
        base_box.label(text="Surface Materials", icon="MATERIAL")

        base_row = base_box.row()
        base_row.template_list(
            "FBXMT_UL_base_materials", "",
            mesh, "materials",
            scene, "fbxmt_base_list_index",
            rows=5,
        )

        layout.separator()

        # ── Island Materials list ─────────────────────────────────────────────
        island_box = layout.box()
        island_box.label(text="Island Materials", icon="UGLYPACKAGE")

        island_row = island_box.row()
        island_row.template_list(
            "FBXMT_UL_chain_materials", "",
            mesh, "materials",
            scene, "fbxmt_island_list_index",
            rows=4,
        )
        btn_col = island_row.column(align=True)
        btn_col.operator("fbxmt.add_chain_material", text="", icon="ADD")
        btn_col.operator("fbxmt.delete_chain_material", text="", icon="REMOVE")

        # Colour pickers — shown only when island list has an active selection
        if scene.fbxmt_island_selected:
            all_mats   = list(mesh.materials)
            isl_idx    = scene.fbxmt_island_list_index
            sel_island = all_mats[isl_idx] if 0 <= isl_idx < len(all_mats) else None
            if sel_island and sel_island.name.startswith('M_FBXMT_Chain_'):
                picker_row = island_box.row(align=True)
                picker_row.prop(scene, "fbxmt_chain_color_a", text="A")
                picker_row.prop(scene, "fbxmt_chain_color_b", text="B")

        layout.separator()

        # ── Shared face operators ─────────────────────────────────────────────
        # Both buttons require Edit mode — poll() handles greying out.
        # Track which list was last clicked via fbxmt_active_list so the
        # operators know which selection to act on.
        face_box = layout.box()
        face_box.label(
            text="Edit Mode - select faces, then:",
            icon="EDITMODE_HLT" if in_edit else "INFO",
        )
        face_row = face_box.row(align=True)
        face_row.scale_y = 1.3
        face_row.operator("fbxmt.assign_to_faces",    text="Assign",   icon="BRUSH_DATA")
        face_row.operator("fbxmt.select_by_material", text="Select",   icon="RESTRICT_SELECT_OFF")


# ─── Import ───────────────────────────────────────────────────────────────────

class FBXMT_PT_Import(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "Import"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"
    bl_options     = {'DEFAULT_CLOSED'}

    def draw(self, context):
        row = self.layout.row()
        row.scale_y = 1.3
        row.menu("FBXMT_MT_Import_Dropdown", text="Import...", icon="IMPORT")


# ─── UV Maps & Unwrap ─────────────────────────────────────────────────────────

class FBXMT_PT_UVUnwrap(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "UV Maps & Unwrap"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"

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
                active_uv.name != LIGHTMAP_CHANNEL_NAME
            )
            remove_row.operator("fbxmt.uv_remove", text="", icon="REMOVE")
        else:
            layout.label(text="No mesh selected", icon="INFO")

        layout.separator()

        layout.prop(props, "uv_floor_threshold")

        row = layout.row()
        row.scale_y = 1.3
        row.operator(
            "fbxmt.uv_unwrap",
            text="Unwrap Selected Faces" if context.mode == "EDIT_MESH"
                 else "Unwrap Selected Objects",
            icon="UV_DATA",
        )


# ─── Addon Preferences (N-panel) ─────────────────────────────────────────────

class FBXMT_PT_AddonPrefs(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "Preferences"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"
    bl_options     = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        prefs  = context.scene.fbxmt_prefs_global
        if prefs is None:
            layout.label(text="Preferences unavailable - try reloading the addon", icon="ERROR")
            return

        # Export — stored per blend file on the scene
        box = layout.box()
        box.label(text="Export", icon="EXPORT")
        box.prop(context.scene.fbxmt_props, "export_path", text="Folder")

        # Workflow Defaults
        box = layout.box()
        box.label(text="Workflow Defaults", icon="SETTINGS")
        box.prop(prefs, "prep_on_import")

        # Checker Appearance
        box = layout.box()
        box.label(text="Checker Appearance", icon="MATERIAL")
        box.label(text="Changes apply on next Rebuild", icon="INFO")
        row = box.row(align=True)
        row.label(text="Checker Squares/Tile:")
        btn_row = box.row(align=True)
        for val in (1, 2, 4, 8):
            op = btn_row.operator("fbxmt.set_checker_scale", text=str(val),
                                  depress=(prefs.checker_scale == val))
            op.value = val
        box.label(text="Corner Mark Length:")
        preset_row = box.row(align=True)
        preset_labels = {1: "12.5%", 2: "25%", 3: "37.5%", 4: "50%"}
        for val in (1, 2, 3, 4):
            op = preset_row.operator("fbxmt.set_corner_preset", text=preset_labels[val],
                                     depress=(prefs.corner_mark_preset == val))
            op.value = val
        box.prop(prefs, "corner_mark_width_px")
        box.prop(prefs, "show_corner_circle")
        box.prop(prefs, "bake_labels")
        box.separator(factor=0.5)
        mat_colours = [
            ("Floor",   "color_floor_a",   "color_floor_b"),
            ("Ceiling", "color_ceiling_a", "color_ceiling_b"),
            ("Wall",    "color_wall_a",    "color_wall_b"),
            ("Trim",    "color_trim_a",    "color_trim_b"),
            ("Ignore",  "color_ignore_a",  "color_ignore_b"),
        ]
        for label, prop_a, prop_b in mat_colours:
            row = box.row(align=True)
            row.label(text=label + ":")
            row.prop(prefs, prop_a, text="A")
            row.prop(prefs, prop_b, text="B")
        box.separator(factor=0.5)
        box.label(text="Chain_01 Checkerboard")
        row = box.row(align=True)
        row.prop(prefs, "color_chain01_a", text="A")
        row.prop(prefs, "color_chain01_b", text="B")

        layout.separator()
        row = layout.row()
        row.scale_y = 1.3
        row.operator("fbxmt.rebuild_materials", text="Rebuild Materials", icon="FILE_REFRESH")


# ─── Export ───────────────────────────────────────────────────────────────────

class FBXMT_PT_Export(Panel):
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_label       = "Export"
    bl_category    = "FBX Toolkit"
    bl_parent_id   = "FBXMT_PT_Main"
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
        sub = row.row()
        sub.enabled = props.bake_textures
        sub.prop(context.scene.fbxmt_prefs_global, "bake_labels", text="Label Squares")

        row = layout.row()
        row.prop(
            props, "lightmap_force_regenerate",
            text  = "Force Regenerate Lightmap" if props.lightmap_force_regenerate
                    else "Ensure Lightmap Exists",
            icon  = "LIGHT" if props.lightmap_force_regenerate else "LIGHT_DATA",
        )

        layout.separator()

        row = layout.row()
        row.scale_y = 1.4
        row.enabled = bool(props.export_path)
        row.operator("unreal.collision_exporter", text="Export Selected", icon="EXPORT")
