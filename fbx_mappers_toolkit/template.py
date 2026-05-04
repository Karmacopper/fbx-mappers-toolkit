# template.py — FBX Mapper's Toolkit v2.1.5
# Startup template generation.
#
# Blender automatically discovers app_templates folders inside installed addons.
# The template lives at:
#   <addon_dir>/app_templates/FBX_Mapper_Toolkit/startup.blend
#
# IMPORTANT: We do NOT use bpy.ops.wm.read_factory_settings() — that destroys
# the live session context and causes a hard crash. Instead we save the current
# file as the template directly, after ensuring collections and materials exist.
# The user should run this from a clean/empty scene for best results.

import bpy
import os
import shutil
from bpy.types import Operator

ADDON_DIR      = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_NAME  = "FBX_Mapper_Toolkit"


def _get_template_dir():
    """Return the correct user app_templates directory for this Blender version."""
    # bpy.utils.resource_path('USER') gives e.g.
    # C:\Users\Chris\AppData\Roaming\Blender Foundation\Blender\5.1
    user_path    = bpy.utils.resource_path('USER')
    template_dir = os.path.join(user_path, "scripts", "startup",
                                "bl_app_templates_user", TEMPLATE_NAME)
    return template_dir


class OT_FBXMT_Save_Template(Operator):
    """Save the current scene as the FBX Mapper startup template.
    Run this from a clean scene with the desired setup already in place.
    The current scene (collections, materials, settings) is saved as-is.
    Restart Blender after saving — the template appears under File > New."""
    bl_idname  = "fbxmt.save_template"
    bl_label   = "Save Startup Template"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        from .materials import (
            ensure_collections,
            ensure_fbxmt_materials,
            ensure_chain_01,
        )

        template_dir  = _get_template_dir()
        template_file = os.path.join(template_dir, "startup.blend")
        os.makedirs(template_dir, exist_ok=True)

        # Ensure the scene has collections and materials without wiping anything
        ensure_collections()
        ensure_fbxmt_materials()
        ensure_chain_01()

        # Set metric units
        scene = context.scene
        scene.unit_settings.system       = 'METRIC'
        scene.unit_settings.scale_length = 1.0
        scene.unit_settings.length_unit  = 'METERS'

        # Save a copy of the current scene to the template location
        bpy.ops.wm.save_as_mainfile(filepath=template_file, copy=True)

        self.report(
            {'INFO'},
            f"Template saved to {template_dir} - restart Blender to see it under File > New"
        )
        return {'FINISHED'}
