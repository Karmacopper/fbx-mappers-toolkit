import bpy
from bpy.app.handlers import persistent


@persistent
def on_depsgraph_update(scene, depsgraph):
    """Reserved for future use."""
    pass


def register_handlers():
    # Nothing to register — handler stub defined but not active.
    pass


def unregister_handlers():
    pass
