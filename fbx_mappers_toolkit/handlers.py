import bpy
from bpy.app.handlers import persistent


@persistent
def on_load_post(filepath):
    """Ensure all chain materials exist on load — runs with a valid scene context.
    Also fires the Project Setup dialog once when opening a fresh FBXMT template.
    """
    try:
        from .materials import ensure_chain_materials
        ensure_chain_materials()
    except Exception:
        pass

    try:
        scene = bpy.context.scene
        if scene:
            # Clamp list index — a stale out-of-range index causes an access
            # violation inside blender::ui::template_list on the next draw call.
            # Safe to write here (handler context, not draw context).
            n = len(bpy.data.materials)
            if n > 0 and scene.fbxmt_base_list_index >= n:
                scene.fbxmt_base_list_index = n - 1
    except Exception:
        pass

    try:
        scene = bpy.context.scene
        if scene and getattr(scene.fbxmt_props, 'fbxmt_is_fresh_template', False):
            scene.fbxmt_props.fbxmt_is_fresh_template = False
            # Read from AddonPreferences — persists across files unlike scene props
            # Import ADDON_ID which is set at register time — __package__ is unreliable here
            try:
                from .panel import ADDON_ID
                addon_prefs = bpy.context.preferences.addons.get(ADDON_ID)
                show_setup  = addon_prefs.preferences.show_setup_on_new if addon_prefs else True
            except Exception:
                show_setup = True
            def _fire_project_setup():
                try:
                    bpy.ops.fbxmt.bake_all_modal('INVOKE_DEFAULT')
                except Exception as e:
                    print(f'[FBXMT] Initial bake failed: {e}')
                if show_setup:
                    bpy.ops.fbxmt.project_setup('INVOKE_DEFAULT')
                return None
            bpy.app.timers.register(_fire_project_setup, first_interval=0.15)
    except Exception:
        pass


def register_handlers():
    bpy.app.handlers.load_post.append(on_load_post)


def unregister_handlers():
    if on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load_post)
