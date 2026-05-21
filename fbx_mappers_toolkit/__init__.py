# GPL v3 — see https://www.gnu.org/licenses/gpl-3.0.en.html

__version__ = "2.9.38"

import bpy
from .op import OT_FBXMT_Export
from .handlers import register_handlers, unregister_handlers
from .template import OT_FBXMT_Save_Template
from .fbx_import import (
    OT_FBXMT_Import_FBX,
    OT_FBXMT_Import_FBX_Ask,
    OT_UT4_Import_FBX_Multi,
    FBXMT_MT_Import_Dropdown,
)
from .materials import (
    OT_FBXMT_Set_Corner_Preset,
    OT_FBXMT_Scene_Setup,
    OT_FBXMT_Add_Materials,
    OT_FBXMT_Rebuild_Materials,
    OT_FBXMT_Assign_Materials,
    OT_FBXMT_Clear_UVs,
    OT_FBXMT_Clear_Mapper_Materials,
    OT_FBXMT_Clear_All_Materials,
    OT_FBXMT_Clear_Scene_Materials,
    FBXMT_MT_Clear_Menu,
    OT_FBXMT_Check_Mesh,
    OT_FBXMT_Set_Texel_Density,
    OT_FBXMT_Set_Checker_Scale,
    OT_FBXMT_Assign_To_Faces,
    OT_FBXMT_Select_By_Material,
    OT_FBXMT_Colour_Islands,
    FBXMT_UL_AllMaterials,
    FBXMT_UL_BaseMaterials,
    FBXMT_UL_ChainMaterials,
    register_material_props,
    unregister_material_props,
)
from .uv_unwrap import OT_FBXMT_UV_Unwrap, OT_FBXMT_UV_Add, OT_FBXMT_UV_Remove, OT_FBXMT_UV_Preview, OT_FBXMT_SmartPack
from .trim_gen import OT_FBXMT_Generate_Trim
from .props import FBXMT_GlobalPrefs, FBXMT_Props
from .primitives import register_primitives, unregister_primitives
from .project_setup import (
    FBXMT_OT_ProjectSetup,
    FBXMT_OT_ProjectSetup_UpdateTile,
    FBXMT_OT_ProjectSetup_ContactSheet,
    FBXMT_OT_BakeAllModal,
    OT_FBXMT_Preset_Save,
    OT_FBXMT_Preset_Load,
    OT_FBXMT_Preset_Delete,
    OT_FBXMT_SelectTile,
    OT_FBXMT_ApplyBToAll,
    FBXMT_OT_TogglePresetLock,
    FBXMT_OT_ProjectSetup_ContactSheet_Disk,
    FBXMT_MT_ContactSheet_Dropdown,
    register as register_project_setup,
    unregister as unregister_project_setup,
)
from .panel import (
    FBXMT_AddonPreferences,
    set_addon_id,
    FBXMT_UL_UVMaps,
    FBXMT_PT_Main,
    FBXMT_PT_SceneSetup,
    FBXMT_PT_Materials,
    FBXMT_PT_Import,
    FBXMT_PT_UVUnwrap,
    FBXMT_PT_Export,
    FBXMT_PT_TrimGen,
)

# Operators, Panels, PropertyGroups, and Menus must be registered manually.
# Blender auto-registers: AddonPreferences, UIList, Header.
classes = (
    FBXMT_GlobalPrefs,
    FBXMT_Props,
    FBXMT_UL_UVMaps,           # UIList — must be registered manually
    FBXMT_UL_AllMaterials,     # UIList — unified 10-item fixed-order list
    FBXMT_UL_BaseMaterials,    # UIList — must be registered manually
    FBXMT_UL_ChainMaterials,   # UIList — must be registered manually
    FBXMT_MT_Clear_Menu,       # Menu — must be registered manually
    FBXMT_MT_Import_Dropdown,  # Menu — must be registered manually
    OT_FBXMT_UV_Add,
    OT_FBXMT_UV_Remove,
    OT_FBXMT_UV_Preview,
    OT_FBXMT_Scene_Setup,
    OT_FBXMT_Add_Materials,
    OT_FBXMT_Rebuild_Materials,
    OT_FBXMT_Clear_UVs,
    OT_FBXMT_Clear_Mapper_Materials,
    OT_FBXMT_Clear_All_Materials,
    OT_FBXMT_Clear_Scene_Materials,
    OT_FBXMT_Check_Mesh,
    OT_FBXMT_Set_Texel_Density,
    OT_FBXMT_Set_Corner_Preset,
    OT_FBXMT_Set_Checker_Scale,
    OT_FBXMT_Assign_To_Faces,
    OT_FBXMT_Select_By_Material,
    OT_FBXMT_Colour_Islands,
    OT_FBXMT_Import_FBX,
    OT_FBXMT_Import_FBX_Ask,
    OT_UT4_Import_FBX_Multi,
    OT_FBXMT_Assign_Materials,
    OT_FBXMT_UV_Unwrap,
    OT_FBXMT_SmartPack,
    OT_FBXMT_Generate_Trim,
    OT_FBXMT_Save_Template,
    OT_FBXMT_Export,
    FBXMT_PT_Main,
    FBXMT_PT_SceneSetup,
    FBXMT_PT_Materials,
    FBXMT_PT_Import,
    FBXMT_PT_UVUnwrap,
    FBXMT_PT_TrimGen,
    FBXMT_PT_Export,
)


def _draw_trim_context_menu(self, context):
    if context.tool_settings.mesh_select_mode[1]:  # edge select mode
        self.layout.separator()
        self.layout.operator('fbxmt.generate_trim', icon='MOD_EDGESPLIT')


def register():
    set_addon_id(__package__)
    FBXMT_AddonPreferences.bl_idname = __package__

    try:
        bpy.utils.register_class(FBXMT_AddonPreferences)
    except Exception:
        try:
            bpy.utils.unregister_class(FBXMT_AddonPreferences)
            bpy.utils.register_class(FBXMT_AddonPreferences)
        except Exception:
            pass

    for c in classes:
        try:
            bpy.utils.register_class(c)
        except Exception:
            try:
                bpy.utils.unregister_class(c)
                bpy.utils.register_class(c)
            except Exception:
                pass

    bpy.types.Scene.fbxmt_props = bpy.props.PointerProperty(type=FBXMT_Props)
    bpy.types.Scene.fbxmt_prefs_global = bpy.props.PointerProperty(type=FBXMT_GlobalPrefs)
    register_material_props()
    register_project_setup()
    register_handlers()
    bpy.types.VIEW3D_MT_edit_mesh_context_menu.append(_draw_trim_context_menu)
    bpy.types.VIEW3D_MT_edit_mesh.append(_draw_trim_context_menu)
    # Primitives — optional, gated on addon preference
    try:
        addon = bpy.context.preferences.addons.get(__package__)
        if addon is None or addon.preferences.enable_primitives:
            register_primitives()
    except Exception:
        register_primitives()  # safe fallback if prefs unreadable at load time


def unregister():
    bpy.types.VIEW3D_MT_edit_mesh_context_menu.remove(_draw_trim_context_menu)
    bpy.types.VIEW3D_MT_edit_mesh.remove(_draw_trim_context_menu)
    unregister_primitives()
    unregister_handlers()
    unregister_project_setup()
    unregister_material_props()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    try:
        bpy.utils.unregister_class(FBXMT_AddonPreferences)
    except Exception:
        pass
    del bpy.types.Scene.fbxmt_props
    del bpy.types.Scene.fbxmt_prefs_global
