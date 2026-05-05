import bpy
import math
import os
from bpy.types import Operator
from .panel import ADDON_ID
from .materials import LIGHTMAP_CHANNEL_NAME
from .uv_unwrap import ensure_lightmap_channel, unwrap_mesh
from .project_setup import cache_is_valid, copy_cache_to_textures


class OT_FBXMT_Export(Operator):
    bl_idname = "unreal.collision_exporter"   # kept for backwards compat with existing keymaps
    bl_label  = "FBXMT Export Selected"

    @classmethod
    def poll(cls, context):
        return (
            context.active_object is not None
            and context.active_object.mode == "OBJECT"
            and any(obj.type == 'MESH' for obj in context.selected_objects)
        )

    def execute(self, context):
        import bmesh

        final_dir      = context.scene.fbxmt_props.export_path

        if not final_dir:
            self.report({'ERROR'}, "No export folder set - check Addon Preferences")
            return {'CANCELLED'}

        if not final_dir.endswith(("/", "\\")):
            final_dir += "/"

        props                     = context.scene.fbxmt_props
        apply_scale_options       = props.apply_scale_options
        generate_collision        = props.ucx_generate
        force_regenerate_lightmap = props.lightmap_force_regenerate

        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        skipped      = len(context.selected_objects) - len(mesh_objects)
        exported     = 0

        # Naked face gate — only relevant when baking materials.
        # If bake is disabled, naked faces don't affect export correctness.
        from .materials import (check_naked_faces, CHAIN_NAMES,
            ISLAND_SUB_NAMES, ISLAND_MARKER_NAME, FBXMT_FLOOR_MATERIALS,
            FBXMT_WALL_MATERIALS, FBXMT_IGNORE_MATERIAL, _is_island_sub_material)
        bake_textures  = props.bake_textures
        if bake_textures:
            naked = check_naked_faces(mesh_objects)
            if naked:
                bpy.ops.object.select_all(action='DESELECT')
                for name in naked:
                    obj = bpy.data.objects.get(name)
                    if obj:
                        obj.select_set(True)
                first = bpy.data.objects.get(next(iter(naked)))
                if first:
                    context.view_layer.objects.active = first
                names  = ', '.join(list(naked.keys())[:5])
                suffix = f' (+{len(naked)-5} more)' if len(naked) > 5 else ''
                total  = sum(naked.values())
                self.report(
                    {'ERROR'},
                    f"Export halted — {total} naked face(s) on {len(naked)} object(s): {names}{suffix}. "
                    f"Assign materials to all faces before exporting."
                )
                return {'CANCELLED'}
        prefs          = bpy.context.scene.fbxmt_prefs_global
        label_grid     = bake_textures and prefs and prefs.bake_labels
        checker_scale  = prefs.checker_scale if prefs else 4
        tex_dir = os.path.join(os.path.normpath(final_dir), 'Textures')
        _used_cache = False
        if bake_textures:
            if cache_is_valid(context.scene):
                # Fast path — pre-baked cache is fresh, copy PNGs directly
                copy_cache_to_textures(context.scene, tex_dir)
                _used_cache = True
            else:
                os.makedirs(tex_dir, exist_ok=True)
        baked       = 0
        bake_failed = []
        baked_mats  = set()   # track across objects — bake each material only once

        for src_obj in mesh_objects:
            obj_name = src_obj.name

            bpy.ops.object.select_all(action='DESELECT')
            src_obj.select_set(True)
            context.view_layer.objects.active = src_obj

            # Auto-unwrap if no diffuse UV map exists yet
            if not src_obj.data.uv_layers.get("UVMap"):
                floor_dot   = math.cos(math.radians(props.uv_floor_threshold))
                unwrap_count = unwrap_mesh(src_obj.data, src_obj.matrix_world, floor_dot)
                if unwrap_count > 0:
                    self.report({'INFO'}, f"{obj_name}: auto-unwrapped {unwrap_count} face(s)")

            ensure_lightmap_channel(src_obj.data, force_regenerate_lightmap, obj=src_obj)

            # Enforce UV channel order: slot 0 = diffuse UVMap, slot 1 = LightmapUVs
            self._enforce_uv_order(src_obj.data)

            # Bake materials on this object (skip already-baked ones, skip if cache used)
            if bake_textures and not _used_cache:
                for slot in src_obj.material_slots:
                    mat = slot.material
                    if mat and mat.name not in baked_mats:
                        result = self._bake_material_emit(
                        mat, src_obj, tex_dir,
                        label_grid=label_grid,
                        checker_scale=checker_scale,
                    )
                        baked_mats.add(mat.name)
                        if result:
                            baked += 1
                        else:
                            bake_failed.append(mat.name)
                if bake_failed:
                    self.report({'WARNING'}, f"Bake failed for: {', '.join(bake_failed)}")
                    bake_failed.clear()

            # ── Pre-export: swap island sub-materials for surface-detected base mats
            # Island sub-materials are internal UV tools — they should not ship in the FBX.
            # Run normal detection on each island face and assign the appropriate base mat.
            mesh      = src_obj.data
            import bmesh as _bmesh
            from mathutils import Vector as _Vec
            _bm = _bmesh.new()
            _bm.from_mesh(mesh)
            _bm.faces.ensure_lookup_table()
            _slot_names = [m.name if m else None for m in mesh.materials]
            _island_slot_idxs = {
                i for i, n in enumerate(_slot_names)
                if n and (n.startswith('M_FBXMT_Island_') or n in set(CHAIN_NAMES))
            }
            _wm = src_obj.matrix_world
            _floor_mat  = bpy.data.materials.get('M_FBXMT_Floor')
            _ceil_mat   = bpy.data.materials.get('M_FBXMT_Ceiling')
            _wall_mat   = bpy.data.materials.get('M_FBXMT_Wall')
            # Ensure base mats are slotted
            _existing = {m.name for m in mesh.materials if m}
            for _bm_mat in [_floor_mat, _ceil_mat, _wall_mat]:
                if _bm_mat and _bm_mat.name not in _existing:
                    mesh.materials.append(_bm_mat)
                    _existing.add(_bm_mat.name)
            _slot_names = [m.name if m else None for m in mesh.materials]
            _floor_idx = _slot_names.index('M_FBXMT_Floor')   if 'M_FBXMT_Floor'   in _slot_names else None
            _ceil_idx  = _slot_names.index('M_FBXMT_Ceiling') if 'M_FBXMT_Ceiling' in _slot_names else None
            _wall_idx  = _slot_names.index('M_FBXMT_Wall')    if 'M_FBXMT_Wall'    in _slot_names else None
            _threshold = math.cos(math.radians(45.0))
            for _face in _bm.faces:
                if _face.material_index not in _island_slot_idxs:
                    continue
                _world_normal = (_wm.to_3x3() @ _face.normal).normalized()
                _dot = _world_normal.dot(_Vec(0, 0, 1))
                if _dot > _threshold and _floor_idx is not None:
                    _face.material_index = _floor_idx
                elif _dot < -_threshold and _ceil_idx is not None:
                    _face.material_index = _ceil_idx
                elif _wall_idx is not None:
                    _face.material_index = _wall_idx
            _bm.to_mesh(mesh)
            _bm.free()
            mesh.update()

            # Strip all island sub-material and legacy chain slots — now unassigned
            used_idxs = {face.material_index for face in mesh.polygons}
            strip_idxs = [
                i for i, slot in enumerate(mesh.materials)
                if slot and (
                    slot.name in set(ISLAND_SUB_NAMES) or
                    slot.name == ISLAND_MARKER_NAME or
                    slot.name in set(CHAIN_NAMES)
                ) and i not in used_idxs
            ]
            for i in reversed(strip_idxs):
                mesh.materials.pop(index=i)

            # UCX collision copy
            ucx_obj = None
            bpy.ops.object.select_all(action='DESELECT')

            if generate_collision:
                ucx_obj = src_obj.copy()
                ucx_obj.data = src_obj.data.copy()
                ucx_obj.animation_data_clear()
                ucx_obj.name = f"UCX_{obj_name}"
                context.collection.objects.link(ucx_obj)
                ucx_obj.select_set(True)

            src_obj.select_set(True)
            context.view_layer.objects.active = src_obj

            bpy.ops.export_scene.fbx(
                filepath             = final_dir + obj_name + ".fbx",
                use_selection        = True,
                apply_scale_options  = apply_scale_options,
                mesh_smooth_type     = "FACE",
                use_tspace           = True,
                add_leaf_bones       = False,
            )

            if ucx_obj is not None:
                bpy.data.objects.remove(ucx_obj, do_unlink=True)

            exported += 1

        # Restore selection
        bpy.ops.object.select_all(action='DESELECT')
        for obj in mesh_objects:
            obj.select_set(True)
        if mesh_objects:
            context.view_layer.objects.active = mesh_objects[-1]

        msg = f"Exported {exported} object(s) to {final_dir}"
        if bake_textures:
            msg += f", baked {baked} material(s)"
        if skipped:
            msg += f" - {skipped} non-mesh object(s) skipped"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


    @staticmethod
    def _draw_grid_labels(img, checker_scale):
        """Draw A1-H8 grid coordinates onto img pixel data in-place.

        Each checker square gets a coordinate label centred inside it.
        Columns = letters (A, B, C...), rows = numbers (1, 2, 3...).
        Characters are rendered from a 5x7 bitmap font, scaled to fit
        the square with 20% padding. Label colour is the inverse of the
        pixel at the square centre for guaranteed contrast.

        img.pixels is a flat RGBA array, row-major from bottom-left.
        """
        # ── 5x7 bitmap font: each glyph is 7 rows, 5 bits wide ───────────────
        # Bit 4 = leftmost column, bit 0 = rightmost.
        GLYPHS = {
            'A': [0b01110,0b10001,0b10001,0b11111,0b10001,0b10001,0b10001],
            'B': [0b11110,0b10001,0b10001,0b11110,0b10001,0b10001,0b11110],
            'C': [0b01110,0b10001,0b10000,0b10000,0b10000,0b10001,0b01110],
            'D': [0b11100,0b10010,0b10001,0b10001,0b10001,0b10010,0b11100],
            'E': [0b11111,0b10000,0b10000,0b11110,0b10000,0b10000,0b11111],
            'F': [0b11111,0b10000,0b10000,0b11110,0b10000,0b10000,0b10000],
            'G': [0b01110,0b10001,0b10000,0b10111,0b10001,0b10001,0b01111],
            'H': [0b10001,0b10001,0b10001,0b11111,0b10001,0b10001,0b10001],
            '1': [0b00100,0b01100,0b00100,0b00100,0b00100,0b00100,0b01110],
            '2': [0b01110,0b10001,0b00001,0b00110,0b01000,0b10000,0b11111],
            '3': [0b11111,0b00001,0b00010,0b00110,0b00001,0b10001,0b01110],
            '4': [0b00010,0b00110,0b01010,0b10010,0b11111,0b00010,0b00010],
            '5': [0b11111,0b10000,0b10000,0b11110,0b00001,0b00001,0b11110],
            '6': [0b01110,0b10000,0b10000,0b11110,0b10001,0b10001,0b01110],
            '7': [0b11111,0b00001,0b00010,0b00100,0b01000,0b01000,0b01000],
            '8': [0b01110,0b10001,0b10001,0b01110,0b10001,0b10001,0b01110],
        }
        LETTERS = 'ABCDEFGH'

        size        = img.size[0]           # assume square
        sq          = size // checker_scale  # pixels per checker square
        pad         = max(1, sq // 5)       # 20% padding each side
        inner       = sq - 2 * pad          # usable pixels per square

        # Scale factor: fit two chars side by side in inner width
        # Each char is 5 wide + 1 gap = 11 wide for two chars
        char_w_raw  = 5
        char_h_raw  = 7
        # Scale so 2 chars + 1 gap fit in inner width
        scale = max(1, inner // 11)
        char_w = char_w_raw * scale
        char_h = char_h_raw * scale
        gap    = scale  # 1 raw pixel gap between chars
        label_w = char_w * 2 + gap
        label_h = char_h

        # Copy pixels into a mutable list — img.pixels returns a sequence
        pixels = list(img.pixels)  # flat RGBA from bottom-left

        def get_px(x, y):
            if 0 <= x < size and 0 <= y < size:
                i = (y * size + x) * 4
                return pixels[i], pixels[i+1], pixels[i+2]
            return 0.5, 0.5, 0.5

        def set_px(x, y, r, g, b):
            if 0 <= x < size and 0 <= y < size:
                i = (y * size + x) * 4
                pixels[i]   = r
                pixels[i+1] = g
                pixels[i+2] = b
                # alpha unchanged

        def draw_char(ch, ox, oy, r, g, b):
            glyph = GLYPHS.get(ch)
            if not glyph:
                return
            for row_i, row_bits in enumerate(glyph):
                for col_i in range(5):
                    if (row_bits >> (4 - col_i)) & 1:
                        # Scale each source pixel to scale×scale block
                        for dy in range(scale):
                            for dx in range(scale):
                                px = ox + col_i * scale + dx
                                # row 0 of glyph = top; img y=0 = bottom
                                py = oy + (char_h_raw - 1 - row_i) * scale + dy
                                set_px(px, py, r, g, b)

        for col in range(checker_scale):
            for row in range(checker_scale):
                label = LETTERS[col % 26] + str(row + 1)

                # Square pixel bounds (img y=0 is bottom)
                sq_x0 = col * sq
                sq_y0 = row * sq

                # Sample centre pixel colour, invert for contrast
                cx = sq_x0 + sq // 2
                cy = sq_y0 + sq // 2
                pr, pg, pb = get_px(cx, cy)
                ir, ig, ib = 1.0 - pr, 1.0 - pg, 1.0 - pb

                # Centre the label in the square
                ox = sq_x0 + (sq - label_w) // 2
                oy = sq_y0 + (sq - label_h) // 2

                draw_char(label[0], ox, oy, ir, ig, ib)
                draw_char(label[1], ox + char_w + gap, oy, ir, ig, ib)

        img.pixels = pixels

    @staticmethod
    def _bake_material_emit(mat, _obj, tex_dir, size=1024, label_grid=False, checker_scale=4):
        """Bake the Emit output of mat to a PNG in tex_dir.

        Creates a temporary 1x1m quad, assigns the material, UV-unwraps it
        to fill 0-1 UV space, bakes EMIT to a 1024x1024 image, saves PNG,
        then deletes the quad. Using a dedicated bake object guarantees every
        pixel of the bake target is covered by a face with the material
        assigned — avoids the black-bake problem that occurs when the real
        mesh has no faces assigned to this particular material slot.

        Returns the saved filepath, or None on failure.
        """
        if not mat.use_nodes:
            return None

        scene = bpy.context.scene
        prev_engine          = scene.render.engine
        prev_samples         = scene.cycles.samples
        scene.render.engine  = 'CYCLES'
        scene.cycles.samples = max(scene.cycles.samples, 1)
        cycles_prefs = bpy.context.preferences.addons.get('cycles')
        if cycles_prefs and cycles_prefs.preferences.compute_device_type != 'NONE':
            scene.cycles.device = 'GPU'
        else:
            scene.cycles.device = 'CPU'

        # Create temporary bake quad
        bpy.ops.mesh.primitive_plane_add(size=1.0)
        bake_obj      = bpy.context.active_object
        bake_obj.name = f"__bake_plane_{mat.name}"

        # Assign material
        if bake_obj.data.materials:
            bake_obj.data.materials[0] = mat
        else:
            bake_obj.data.materials.append(mat)

        # UV-unwrap to fill 0-1 space
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.0)
        bpy.ops.object.mode_set(mode='OBJECT')

        img_name = f"__bake_{mat.name}"
        img = bpy.data.images.get(img_name)
        if img:
            bpy.data.images.remove(img)
        img = bpy.data.images.new(img_name, width=size, height=size, float_buffer=False)
        img.colorspace_settings.name = 'sRGB'

        nodes     = mat.node_tree.nodes
        bake_node = nodes.new('ShaderNodeTexImage')
        bake_node.image = img
        nodes.active    = bake_node

        try:
            bpy.ops.object.bake(type='EMIT', use_clear=True)
            if label_grid:
                OT_FBXMT_Export._draw_grid_labels(img, checker_scale)
            filepath = os.path.join(tex_dir, mat.name + '.png')
            img.filepath_raw = filepath
            img.file_format  = 'PNG'
            img.save()
        except Exception as e:
            print(f"[FBXMT] Bake failed for {mat.name}: {type(e).__name__}: {e}")
            return None
        finally:
            nodes.remove(bake_node)
            bpy.data.images.remove(img)
            bpy.data.objects.remove(bake_obj, do_unlink=True)
            scene.render.engine  = prev_engine
            scene.cycles.samples = prev_samples

        return filepath

    @staticmethod
    def _enforce_uv_order(mesh):
        """Ensure LightmapUVs sits at slot index 1.

        Reads all UV data in a single bmesh pass, rebuilds layers in the
        correct order, then writes all data back in one pass per layer.
        O(layers × faces) not O(layers² × faces).
        """
        import bmesh

        uv_names = [layer.name for layer in mesh.uv_layers]
        if LIGHTMAP_CHANNEL_NAME not in uv_names:
            return
        if uv_names.index(LIGHTMAP_CHANNEL_NAME) == 1:
            return   # already correct

        # Single read pass — collect all UV data keyed by layer name
        bm = bmesh.new()
        bm.from_mesh(mesh)
        uv_data = {}
        for layer in bm.loops.layers.uv.values():
            uvs = {}
            for face in bm.faces:
                for loop in face.loops:
                    uvs[loop.index] = loop[layer].uv.copy()
            uv_data[layer.name] = uvs
        bm.free()

        # Determine correct order: everything before LightmapUVs, then it, then rest
        ordered = [n for n in uv_names if n != LIGHTMAP_CHANNEL_NAME]
        ordered.insert(1, LIGHTMAP_CHANNEL_NAME)

        # Rebuild all layers then write all UV data in a single bmesh pass.
        while mesh.uv_layers:
            mesh.uv_layers.remove(mesh.uv_layers[0])
        for name in ordered:
            mesh.uv_layers.new(name=name)

        bm2 = bmesh.new()
        bm2.from_mesh(mesh)
        for layer in bm2.loops.layers.uv.values():
            layer_uvs = uv_data.get(layer.name, {})
            for face in bm2.faces:
                for loop in face.loops:
                    if loop.index in layer_uvs:
                        loop[layer].uv = layer_uvs[loop.index]
        bm2.to_mesh(mesh)
        bm2.free()
        mesh.update()


# bl_idname 'unreal.collision_exporter' kept for keymap backwards compatibility.
# The class is registered under this name in __init__.py.
