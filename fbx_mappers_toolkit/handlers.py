import bpy
import bmesh
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


@persistent
def on_depsgraph_update_post(scene, depsgraph):
    """Update the A/B face overlay whenever the edit-mode selection changes."""
    try:
        from .trim_overlay import build_overlay, clear_overlay

        ctx = bpy.context
        if ctx is None:
            return
        obj = ctx.active_object
        if obj is None or obj.type != 'MESH' or ctx.mode != 'EDIT_MESH':
            clear_overlay()
            # Redraw so the cleared overlay takes effect
            for area in ctx.screen.areas if ctx.screen else []:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
            return

        bm = bmesh.from_edit_mesh(obj.data)
        selected = [e for e in bm.edges if e.select]

        # Respect the per-scene overlay toggle
        overlay_on = getattr(
            getattr(ctx.scene, 'fbxmt_props', None),
            'show_trim_overlay', True
        )
        if not overlay_on:
            clear_overlay()
        elif not selected:
            clear_overlay()
        else:
            build_overlay(obj, selected)

        for area in ctx.screen.areas if ctx.screen else []:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    except Exception:
        pass


def register_handlers():
    from .trim_overlay import register_overlay
    from .par_ray_preview import register_par_preview
    register_overlay()
    register_par_preview()
    bpy.app.handlers.load_post.append(on_load_post)
    bpy.app.handlers.depsgraph_update_post.append(on_depsgraph_update_post)


def unregister_handlers():
    from .trim_overlay import unregister_overlay
    from .par_ray_preview import unregister_par_preview
    unregister_overlay()
    unregister_par_preview()
    if on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load_post)
    if on_depsgraph_update_post in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(on_depsgraph_update_post)
