# GPL v3 — see https://www.gnu.org/licenses/gpl-3.0.en.html

__version__ = "0.25.1"

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
    OT_FBXMT_Auto_Detect_Wall_Islands,
    OT_FBXMT_Strip_Mesh,
    FBXMT_UL_AllMaterials,
    FBXMT_UL_BaseMaterials,
    FBXMT_UL_ChainMaterials,
    register_material_props,
    unregister_material_props,
)
from .uv_unwrap import OT_FBXMT_UV_Unwrap, OT_FBXMT_UV_Add, OT_FBXMT_UV_Remove, OT_FBXMT_UV_Preview, OT_FBXMT_SmartPack
from .trim_gen import OT_FBXMT_Generate_Trim
from .trim_gen2 import OT_FBXMT_Generate_Trim2
from .ceiling_deco import (
    OT_FBXMT_Generate_Coving,
    OT_FBXMT_Generate_Parallel,
    OT_FBXMT_Generate_Spokes,
    OT_FBXMT_Generate_Curve,
    OT_FBXMT_Generate_Beams,
)
from .beam_placement import (
    OT_FBXMT_Quick_Beam,
    OT_FBXMT_Quick_Beam,
    OT_FBXMT_Place_Parallel,
    OT_FBXMT_Preview_Parallel_Rays,
    OT_FBXMT_Clear_Parallel,
    OT_FBXMT_Place_Spokes,
    OT_FBXMT_Clear_Spokes,
    OT_FBXMT_Place_Curve,
    OT_FBXMT_Clear_Curve,
    OT_FBXMT_Clear_Beams,
)
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
from .grid import (
    register as register_grid,
    unregister as unregister_grid,
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
    FBXMT_PT_TrimMain,
    FBXMT_PT_TrimGen2,
    FBXMT_PT_CeilingDeco,
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
    OT_FBXMT_Auto_Detect_Wall_Islands,
    OT_FBXMT_Strip_Mesh,
    OT_FBXMT_Import_FBX,
    OT_FBXMT_Import_FBX_Ask,
    OT_UT4_Import_FBX_Multi,
    OT_FBXMT_Assign_Materials,
    OT_FBXMT_UV_Unwrap,
    OT_FBXMT_SmartPack,
    OT_FBXMT_Generate_Trim,
    OT_FBXMT_Generate_Trim2,
    OT_FBXMT_Generate_Coving,
    OT_FBXMT_Generate_Parallel,
    OT_FBXMT_Generate_Spokes,
    OT_FBXMT_Generate_Curve,
    OT_FBXMT_Generate_Beams,
    OT_FBXMT_Quick_Beam,
    OT_FBXMT_Place_Parallel,
    OT_FBXMT_Preview_Parallel_Rays,
    OT_FBXMT_Clear_Parallel,
    OT_FBXMT_Place_Spokes,
    OT_FBXMT_Clear_Spokes,
    OT_FBXMT_Place_Curve,
    OT_FBXMT_Clear_Curve,
    OT_FBXMT_Clear_Beams,
    OT_FBXMT_Save_Template,
    OT_FBXMT_Export,
    FBXMT_PT_Main,
    FBXMT_PT_SceneSetup,
    FBXMT_PT_Materials,
    FBXMT_PT_Import,
    FBXMT_PT_UVUnwrap,
    FBXMT_PT_Export,
)

# Trim Tools panels — registered conditionally on enable_trim_tools pref
_trim_classes = (
    FBXMT_PT_TrimMain,
    FBXMT_PT_TrimGen2,
    FBXMT_PT_CeilingDeco,
)



def register_trim_tools():
    for c in _trim_classes:
        try:
            bpy.utils.register_class(c)
        except Exception:
            try:
                bpy.utils.unregister_class(c)
                bpy.utils.register_class(c)
            except Exception:
                pass


def unregister_trim_tools():
    for c in reversed(_trim_classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


def register():
    # Purge any stale .pyc files so Blender recompiles from source
    import os, shutil
    pkg_dir = os.path.dirname(__file__)
    cache = os.path.join(pkg_dir, '__pycache__')
    if os.path.isdir(cache):
        shutil.rmtree(cache, ignore_errors=True)

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
    register_grid()
    register_handlers()
    # Primitives — optional, gated on addon preference
    try:
        addon = bpy.context.preferences.addons.get(__package__)
        if addon is None or addon.preferences.enable_primitives:
            register_primitives()
    except Exception:
        register_primitives()  # safe fallback if prefs unreadable at load time
    # Trim Tools — optional, gated on addon preference
    try:
        addon = bpy.context.preferences.addons.get(__package__)
        if addon is None or addon.preferences.enable_trim_tools:
            register_trim_tools()
    except Exception:
        register_trim_tools()  # safe fallback if prefs unreadable at load time


def unregister():
    unregister_trim_tools()
    unregister_primitives()
    unregister_handlers()
    unregister_grid()
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
