# GPL v3 — see https://www.gnu.org/licenses/gpl-3.0.en.html

__version__ = "2.6.7"

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
    OT_FBXMT_Add_Chain_Material,
    OT_FBXMT_Delete_Chain_Material,
    OT_FBXMT_Set_Texel_Density,
    OT_FBXMT_Set_Checker_Scale,
    OT_FBXMT_Assign_To_Faces,
    OT_FBXMT_Select_By_Material,
    FBXMT_UL_BaseMaterials,
    FBXMT_UL_ChainMaterials,
    register_material_props,
    unregister_material_props,
    ensure_chain_01,
)
from .uv_unwrap import OT_FBXMT_UV_Unwrap, OT_FBXMT_UV_Add, OT_FBXMT_UV_Remove
from .props import FBXMT_GlobalPrefs, FBXMT_Props
from .panel import (
    FBXMT_AddonPreferences,
    set_addon_id,
    FBXMT_UL_UVMaps,
    FBXMT_PT_Main,
    FBXMT_PT_SceneSetup,
    FBXMT_PT_Materials,
    FBXMT_PT_Import,
    FBXMT_PT_UVUnwrap,
    FBXMT_PT_AddonPrefs,
    FBXMT_PT_Export,
)

# Operators, Panels, PropertyGroups, and Menus must be registered manually.
# Blender auto-registers: AddonPreferences, UIList, Header.
classes = (
    FBXMT_GlobalPrefs,
    FBXMT_Props,
    FBXMT_UL_UVMaps,           # UIList — must be registered manually
    FBXMT_UL_BaseMaterials,    # UIList — must be registered manually
    FBXMT_UL_ChainMaterials,   # UIList — must be registered manually
    FBXMT_MT_Clear_Menu,       # Menu — must be registered manually
    FBXMT_MT_Import_Dropdown,  # Menu — must be registered manually
    OT_FBXMT_UV_Add,
    OT_FBXMT_UV_Remove,
    OT_FBXMT_Scene_Setup,
    OT_FBXMT_Add_Materials,
    OT_FBXMT_Rebuild_Materials,
    OT_FBXMT_Clear_UVs,
    OT_FBXMT_Clear_Mapper_Materials,
    OT_FBXMT_Clear_All_Materials,
    OT_FBXMT_Clear_Scene_Materials,
    OT_FBXMT_Add_Chain_Material,
    OT_FBXMT_Delete_Chain_Material,
    OT_FBXMT_Set_Texel_Density,
    OT_FBXMT_Set_Corner_Preset,
    OT_FBXMT_Set_Checker_Scale,
    OT_FBXMT_Assign_To_Faces,
    OT_FBXMT_Select_By_Material,
    OT_FBXMT_Import_FBX,
    OT_FBXMT_Import_FBX_Ask,
    OT_UT4_Import_FBX_Multi,
    OT_FBXMT_Assign_Materials,
    OT_FBXMT_UV_Unwrap,
    OT_FBXMT_Save_Template,
    OT_FBXMT_Export,
    FBXMT_PT_Main,
    FBXMT_PT_SceneSetup,
    FBXMT_PT_Materials,
    FBXMT_PT_Import,
    FBXMT_PT_UVUnwrap,
    FBXMT_PT_Export,
    FBXMT_PT_AddonPrefs,
)


def register():
    set_addon_id(__package__)
    FBXMT_AddonPreferences.bl_idname = __package__

    try:
        bpy.utils.register_class(FBXMT_AddonPreferences)
    except ValueError:
        bpy.utils.unregister_class(FBXMT_AddonPreferences)
        bpy.utils.register_class(FBXMT_AddonPreferences)

    for c in classes:
        try:
            bpy.utils.register_class(c)
        except ValueError:
            bpy.utils.unregister_class(c)
            bpy.utils.register_class(c)

    bpy.types.Scene.fbxmt_props = bpy.props.PointerProperty(type=FBXMT_Props)
    bpy.types.Scene.fbxmt_prefs_global = bpy.props.PointerProperty(type=FBXMT_GlobalPrefs)
    register_material_props()
    register_handlers()
    try:
        ensure_chain_01()
    except Exception:
        pass


def unregister():
    unregister_handlers()
    unregister_material_props()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    try:
        bpy.utils.unregister_class(FBXMT_AddonPreferences)
    except Exception:
        pass
    del bpy.types.Scene.fbxmt_props
    del bpy.types.Scene.fbxmt_prefs_global
