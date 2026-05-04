import bpy
from bpy.types import Operator
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, CollectionProperty
from .materials import (
    ensure_fbxmt_materials,
    FBXMT_MATERIALS,
    FBXMT_ALL_MATERIALS,
    FBXMT_IGNORE_MATERIAL,
    _is_chain_material,
    _get_prefs,
    add_fbxmt_slots,
    assign_trim_material,
    move_to_collection,
    COLLECTION_GEO,
    COLLECTION_PROPS,
    COLLECTION_TRIM,
)

# Module-level state for multi-file ask flow
_pending_files = []
_ask_index     = 0


def get_newly_imported(before_names, import_type):
    """
    Process newly imported mesh objects based on import_type.
    before_names: set of object names that existed before the import op ran.
    the UV unwrap automatically after slot assignment.
    Returns list of newly created mesh objects.
    """
    import math
    new_names = set(bpy.data.objects.keys()) - before_names
    new_objs  = [bpy.data.objects[n] for n in new_names
                 if bpy.data.objects[n].type == 'MESH']

    for obj in new_objs:
        if import_type == 'GEO':
            add_fbxmt_slots(obj)
            move_to_collection(obj, COLLECTION_GEO)
        elif import_type == 'TRIM':
            add_fbxmt_slots(obj)
            assign_trim_material(obj)
            move_to_collection(obj, COLLECTION_TRIM)
        elif import_type == 'PROP':
            move_to_collection(obj, COLLECTION_PROPS)
        # NONE: import as-is, no slot or collection changes

    # Post-import processing — Geo only, single full-prep pipeline
    prefs = _get_prefs()
    if prefs and prefs.prep_on_import and import_type == 'GEO' and new_objs:
        import bmesh as _bmesh
        from mathutils import Vector as _Vector
        from .uv_unwrap import unwrap_mesh, ensure_lightmap_channel
        from .materials import (
            FBXMT_MATERIALS, FBXMT_IGNORE_MATERIAL, FBXMT_ALL_MATERIALS,
            _is_chain_material, ensure_fbxmt_materials,
        )
        from . import materials as _mat_module

        scene               = bpy.context.scene
        props               = scene.fbxmt_props
        floor_threshold_dot = math.cos(math.radians(props.uv_floor_threshold))
        z_axis              = _Vector((0.0, 0.0, 1.0))

        _mat_module._suppress_handler = True
        try:
            ensure_fbxmt_materials()

            for obj in new_objs:
                mesh = obj.data

                # 1. Strip all non-FBXMT material slots
                slots_to_remove = [
                    i for i, slot in enumerate(mesh.materials)
                    if slot and slot.name not in FBXMT_ALL_MATERIALS
                    and not _is_chain_material(slot)
                ]
                for i in reversed(slots_to_remove):
                    mesh.materials.pop(index=i)
                mesh.update()

                # 2. Strip all UV maps and flush mesh state cleanly
                while mesh.uv_layers:
                    mesh.uv_layers.remove(mesh.uv_layers[0])
                mesh.update()

                # 3. Add base M_FBXMT material slots
                for mat_name in FBXMT_MATERIALS:
                    mat = bpy.data.materials.get(mat_name)
                    if mat and mat_name not in {m.name for m in mesh.materials if m}:
                        mesh.materials.append(mat)

                # 4. Auto-assign by world-space normal
                slot_index   = {m.name: i for i, m in enumerate(mesh.materials) if m}
                world_matrix = obj.matrix_world

                bm = _bmesh.new()
                bm.from_mesh(mesh)
                bm.faces.ensure_lookup_table()
                for face in bm.faces:
                    current = (
                        mesh.materials[face.material_index]
                        if face.material_index < len(mesh.materials) else None
                    )
                    if current and (current.name == FBXMT_IGNORE_MATERIAL
                                    or _is_chain_material(current)):
                        continue
                    world_normal = (world_matrix.to_3x3() @ face.normal).normalized()
                    dot_z        = abs(world_normal.dot(z_axis))
                    mn = (
                        ('M_FBXMT_Floor' if world_normal.z > 0 else 'M_FBXMT_Ceiling')
                        if dot_z >= floor_threshold_dot else 'M_FBXMT_Wall'
                    )
                    if mn in slot_index:
                        face.material_index = slot_index[mn]
                bm.to_mesh(mesh)
                bm.free()
                mesh.update()

                # 5. UV unwrap — ensure mesh is fully evaluated before unwrapping
                obj.data.update()
                bpy.context.view_layer.update()
                unwrap_mesh(obj.data, obj.matrix_world, floor_threshold_dot)

                # 6. Generate LightmapUVs
                ensure_lightmap_channel(obj.data, force_regenerate=False, obj=obj)

        finally:
            _mat_module._suppress_handler = False
    return new_objs


# ─── Import dropdown menu ─────────────────────────────────────────────────────

class FBXMT_MT_Import_Dropdown(bpy.types.Menu):
    bl_idname = "FBXMT_MT_Import_Dropdown"
    bl_label  = "Import"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Select one or multiple files", icon="INFO")
        layout.separator()
        layout.operator("fbxmt.import_fbx", text="Import",            icon="IMPORT"     ).import_type = 'NONE'
        layout.operator("fbxmt.import_fbx", text="Import as Geo",     icon="MESH_GRID"  ).import_type = 'GEO'
        layout.operator("fbxmt.import_fbx", text="Import as Trim",    icon="EDGESEL"    ).import_type = 'TRIM'
        layout.operator("fbxmt.import_fbx", text="Import as Prop",    icon="OBJECT_DATA").import_type = 'PROP'
        layout.separator()
        layout.operator("fbxmt.import_fbx", text="Import and Ask per File", icon="QUESTION").import_type = 'ASK'


# ─── Single / batch file import ───────────────────────────────────────────────

class OT_FBXMT_Import_FBX(Operator, ImportHelper):
    bl_idname      = "fbxmt.import_fbx"
    bl_label       = "Import FBX"
    bl_description = "Import FBX file(s) - select multiple files for batch import"
    bl_options     = {'REGISTER', 'UNDO'}

    filter_glob:   StringProperty(default="*.fbx", options={'HIDDEN'})
    files:         CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory:     StringProperty(subtype='DIR_PATH')
    import_type:   StringProperty(default='NONE')
    filename_ext = ".fbx"

    def execute(self, context):
        import os

        files = [os.path.join(self.directory, f.name) for f in self.files if f.name]
        if not files:
            files = [self.filepath]

        if len(files) > 1 and self.import_type == 'ASK':
            global _ask_index
            _pending_files[:] = files
            _ask_index = 0
            bpy.ops.fbxmt.import_fbx_ask('INVOKE_DEFAULT')
            return {'FINISHED'}

        total = 0
        for filepath in files:
            before   = set(bpy.data.objects.keys())
            bpy.ops.import_scene.fbx(filepath=filepath)
            new_objs = get_newly_imported(before, self.import_type)
            total   += len(new_objs)

        label = self.import_type.lower() if self.import_type != 'NONE' else 'as-is'
        self.report({'INFO'}, f"Imported {len(files)} file(s), {total} object(s) - {label}")
        return {'FINISHED'}


# ─── Per-file ask dialog ──────────────────────────────────────────────────────

class OT_FBXMT_Import_FBX_Ask(Operator):
    """Per-file import type dialog for batch imports."""
    bl_idname  = "fbxmt.import_fbx_ask"
    bl_label   = "How to import this file?"
    bl_options = {'REGISTER', 'UNDO'}

    import_choice: bpy.props.EnumProperty(
        name  = "Import as",
        items = [
            ('GEO',  "Geo - add M_FBXMT materials",       ""),
            ('TRIM', "Trim - add and assign M_FBXMT_Trim", ""),
            ('PROP', "Prop - import as-is",                ""),
            ('SKIP', "Skip this file",                     ""),
        ],
        default = 'GEO',
    )

    def invoke(self, context, event):
        import os
        filepath       = _pending_files[_ask_index] if _ask_index < len(_pending_files) else ""
        self.bl_label  = f"Import: {os.path.basename(filepath)}"
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        import os
        layout   = self.layout
        filepath = _pending_files[_ask_index] if _ask_index < len(_pending_files) else ""
        layout.label(text=os.path.basename(filepath))
        layout.prop(self, "import_choice", expand=True)

    def execute(self, context):
        global _ask_index
        filepath = _pending_files[_ask_index] if _ask_index < len(_pending_files) else ""

        if self.import_choice != 'SKIP' and filepath:
            before = set(bpy.data.objects.keys())
            bpy.ops.import_scene.fbx(filepath=filepath)
            get_newly_imported(before, self.import_choice)

        _ask_index += 1
        if _ask_index < len(_pending_files):
            bpy.ops.fbxmt.import_fbx_ask('INVOKE_DEFAULT')

        return {'FINISHED'}


# Kept for backwards compatibility — redirects old operator ID to new one.
# Remove in a future major version.
class OT_UT4_Import_FBX_Multi(Operator):
    bl_idname = "ut4.import_fbx_multi"
    bl_label  = "Multi Import (deprecated)"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        self.report({'WARNING'}, "ut4.import_fbx_multi is deprecated - use fbxmt.import_fbx")
        return {'CANCELLED'}
