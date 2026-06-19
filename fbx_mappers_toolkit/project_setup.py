# project_setup.py — FBX Mapper's Toolkit
#
# Project Setup / Material Preview window.
# Operator dialog accessible via the Preferences panel button and
# optionally auto-fired on fresh FBXMT template load.
#
# Architecture notes:
#   - _get_prefs() imported from materials.py — single source of truth
#   - All colour properties live on FBXMT_GlobalPrefs (scene.fbxmt_prefs_global)
#   - Operational state (index, stale flag, hash) lives on FBXMT_Props (scene.fbxmt_props)
#   - _bake_material_emit reused from op.py — no duplication of bake logic
#   - ShaderNodeMix(data_type='RGBA') only — no ShaderNodeMixRGB, no ShaderNodeInvert

import bpy
import os
import hashlib
import tempfile
import bmesh
import numpy as np
import colorsys
from mathutils import Vector
from bpy.types import Operator
from bpy.props import IntProperty, BoolProperty

from .materials import _get_prefs, ensure_fbxmt_materials, rebuild_fbxmt_materials, _safe_float
from .panel import _draw_preset_lock_ticker


# ─── Constants ────────────────────────────────────────────────────────────────

# (split tile preview removed — tiles show full material only)

# 8 visible materials in display order
ALL_DISPLAY_MATERIAL_NAMES = [
    'M_FBXMT_Wall',
    'M_FBXMT_Ramp_Floor',
    'M_FBXMT_Floor',
    'M_FBXMT_Ramp_Ceiling',
    'M_FBXMT_Ceiling',
    'M_FBXMT_Trim',
    'M_FBXMT_Ignore',
    'M_FBXMT_Island',
]

# Materials baked to MaterialCache/ — Ignore excluded (faces have no UVs)
BAKE_MATERIAL_NAMES = [n for n in ALL_DISPLAY_MATERIAL_NAMES if n != 'M_FBXMT_Ignore']

PREVIEW_SIZE  = 128
CACHE_SUBDIR  = 'MaterialCache'

# 5×7 pixel font for contact sheet labels
_FONT_5X7 = {
    'A':['01110','10001','10001','11111','10001','10001','10001'],
    'B':['11110','10001','10001','11110','10001','10001','11110'],
    'C':['01110','10001','10000','10000','10000','10001','01110'],
    'D':['11110','10001','10001','10001','10001','10001','11110'],
    'E':['11111','10000','10000','11110','10000','10000','11111'],
    'F':['11111','10000','10000','11110','10000','10000','10000'],
    'G':['01110','10001','10000','10111','10001','10001','01111'],
    'H':['10001','10001','10001','11111','10001','10001','10001'],
    'I':['01110','00100','00100','00100','00100','00100','01110'],
    'J':['00111','00010','00010','00010','00010','10010','01100'],
    'K':['10001','10010','10100','11000','10100','10010','10001'],
    'L':['10000','10000','10000','10000','10000','10000','11111'],
    'M':['10001','11011','10101','10001','10001','10001','10001'],
    'N':['10001','11001','10101','10011','10001','10001','10001'],
    'O':['01110','10001','10001','10001','10001','10001','01110'],
    'P':['11110','10001','10001','11110','10000','10000','10000'],
    'Q':['01110','10001','10001','10001','10101','10010','01101'],
    'R':['11110','10001','10001','11110','10100','10010','10001'],
    'S':['01111','10000','10000','01110','00001','00001','11110'],
    'T':['11111','00100','00100','00100','00100','00100','00100'],
    'U':['10001','10001','10001','10001','10001','10001','01110'],
    'V':['10001','10001','10001','10001','10001','01010','00100'],
    'W':['10001','10001','10001','10101','10101','11011','10001'],
    'X':['10001','10001','01010','00100','01010','10001','10001'],
    'Y':['10001','10001','01010','00100','00100','00100','00100'],
    'Z':['11111','00001','00010','00100','01000','10000','11111'],
    '0':['01110','10001','10011','10101','11001','10001','01110'],
    '1':['00100','01100','00100','00100','00100','00100','01110'],
    '2':['01110','10001','00001','00110','01000','10000','11111'],
    '3':['11110','00001','00001','01110','00001','00001','11110'],
    '4':['00010','00110','01010','10010','11111','00010','00010'],
    '5':['11111','10000','10000','11110','00001','00001','11110'],
    '6':['01110','10000','10000','11110','10001','10001','01110'],
    '7':['11111','00001','00010','00100','01000','01000','01000'],
    '8':['01110','10001','10001','01110','10001','10001','01110'],
    '9':['01110','10001','10001','01111','00001','00001','01110'],
    '_':['00000','00000','00000','00000','00000','00000','11111'],
    ' ':['00000','00000','00000','00000','00000','00000','00000'],
}


def _snap_px(frac, size):
    """Round a tile-fraction up to the nearest power-of-2 pixel count."""
    px = max(1, frac * size)
    p2 = 1
    while p2 < px:
        p2 <<= 1
    return p2 / size


# ─── Colour property maps ─────────────────────────────────────────────────────
# A-only — B is always derived, never stored
_MAT_COLOR_PROPS_A = [
    'color_floor_a',
    'color_ceiling_a',
    'color_wall_a',
    'color_trim_a',
    'color_ignore_a',
    'color_ramp_floor_a',
    'color_ramp_ceiling_a',
]

_MAT_DISPLAY_NAMES = {
    'M_FBXMT_Floor':          'Floor',
    'M_FBXMT_Ceiling':        'Ceiling',
    'M_FBXMT_Wall':           'Wall',
    'M_FBXMT_Trim':           'Trim',
    'M_FBXMT_Ignore':         'Ignore',
    'M_FBXMT_Island':         'Island Marker',
    'M_FBXMT_Ramp_Floor':     'Ramp Floor',
    'M_FBXMT_Ramp_Ceiling':   'Ramp Ceiling',
}


# ─── Preview mesh data ────────────────────────────────────────────────────────
# Hardcoded geometry for the two preview models.
# FBXMT_Preview_Geo_Trim: architectural piece showing Floor/Ceiling/Wall/Trim/Ignore
# FBXMT_Preview_Island_Chains: Q3DM6-inspired stacked curves showing all 5 chains
# Exported from Blender, processed through the toolkit's own unwrap pipeline.



# ─── Helpers ──────────────────────────────────────────────────────────────────

def _compute_cache_hash(scene) -> str:
    """Hash of all settings that affect baked material output.
    If this matches the stored hash, rebake can be skipped.
    """
    props = scene.fbxmt_props
    prefs = scene.fbxmt_prefs_global
    parts = [
        str(props.geo_texel_density),
        str(prefs.checker_scale),
        str(prefs.corner_mark_preset),
        str(prefs.corner_mark_width_px),
        str(prefs.show_corner_circle),
        str(round(prefs.corner_hue_shift, 2)),
        str(prefs.bake_labels),
    ]
    # All A colour values — B is always derived, so only A affects the hash
    for prop_a in _MAT_COLOR_PROPS_A:
        ca = getattr(prefs, prop_a, None)
        if ca:
            parts.append(','.join(f'{v:.4f}' for v in ca))
    # B derivation settings also affect output
    parts.append(getattr(prefs, 'color_b_hue_offset', '0'))
    parts.append(getattr(prefs, 'color_b_saturation', '0.6'))
    parts.append(getattr(prefs, 'color_b_value', '0.35'))
    raw = '|'.join(parts)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _cache_dir(scene):
    if not bpy.data.filepath:
        return None
    return os.path.join(os.path.dirname(bpy.data.filepath), CACHE_SUBDIR)


def _cache_path(scene, mat_name: str):
    d = _cache_dir(scene)
    return os.path.join(d, mat_name + '.png') if d else None


def cache_is_valid(scene) -> bool:
    """Public — called by OT_FBXMT_Export to check for valid pre-baked cache."""
    if _compute_cache_hash(scene) != scene.fbxmt_props.fbxmt_cache_hash:
        return False
    for name in BAKE_MATERIAL_NAMES:
        p = _cache_path(scene, name)
        if not p or not os.path.exists(p):
            return False
    return True


def copy_cache_to_textures(scene, tex_dir: str):
    """Copy pre-baked PNGs from MaterialCache/ to Textures/. Called by export fast path."""
    os.makedirs(tex_dir, exist_ok=True)
    for mat_name in BAKE_MATERIAL_NAMES:
        src = _cache_path(scene, mat_name)
        if src and os.path.exists(src):
            shutil.copy2(src, os.path.join(tex_dir, mat_name + '.png'))


def _get_tex_dir(scene):
    """Returns normalised export_folder/Textures path, or None if export folder not set."""
    export_path = scene.fbxmt_props.export_path if scene.fbxmt_props else ''
    if not export_path:
        return None
    return os.path.normpath(os.path.join(export_path, 'Textures'))




# ─── New properties — added to FBXMT_Props in props.py ───────────────────────
# Defined here for reference; actual registration happens in props.py.
#
#   fbxmt_selected_mat_index : IntProperty  — which row is highlighted (0–9)
#   fbxmt_preview_stale      : BoolProperty — True when colours/density changed
#   fbxmt_cache_hash         : StringProperty — hash at last Pre-Bake All
#   fbxmt_is_fresh_template  : BoolProperty — set by Save Template, cleared on load


# ─── Operator: update single tile ────────────────────────────────────────────

class FBXMT_OT_ProjectSetup_UpdateTile(Operator):
    bl_idname  = 'fbxmt.project_setup_update_tile'
    bl_label   = 'Update Tile'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        if not context.scene.fbxmt_props.export_path:
            self.report({'WARNING'}, 'No export folder set — textures not saved')
        _render_dialog_tiles_sync(context)
        return {'FINISHED'}



# ─── Operator: set texel density ─────────────────────────────────────────────

class FBXMT_OT_ProjectSetup_SetDensity(Operator):
    bl_idname  = 'fbxmt.project_setup_set_density'
    bl_label   = 'Set Texel Density'
    bl_options = {'INTERNAL'}

    density: IntProperty()

    def execute(self, context):
        props = context.scene.fbxmt_props
        props.geo_texel_density   = self.density
        props.fbxmt_preview_stale = True
        props.fbxmt_cache_hash    = ''
        _render_dialog_tiles_sync(context)
        return {'FINISHED'}


class FBXMT_OT_ProjectSetup_SetCheckerScale(Operator):
    """Set checker scale from inside Project Setup — marks stale, no viewport rebuild.
    The N-panel button (fbxmt.set_checker_scale) still rebuilds immediately."""
    bl_idname  = 'fbxmt.project_setup_set_checker_scale'
    bl_label   = 'Set Checker Scale (Dialog)'
    bl_options = {'INTERNAL'}

    value: IntProperty(default=4)

    def execute(self, context):
        prefs = context.scene.fbxmt_prefs_global
        if prefs:
            prefs.checker_scale = self.value
        context.scene.fbxmt_props.fbxmt_preview_stale = True
        _render_dialog_tiles_sync(context)
        return {'FINISHED'}


# ─── Operator: render preview ─────────────────────────────────────────────────

class FBXMT_OT_ProjectSetup_TilingTest(Operator):
    """Render a 3×3 tiling test sheet of the Ignore material — no labels.
    Shows how the tile pattern tiles across 9 adjacent squares so edge
    alignment lines can be verified visually."""
    bl_idname  = 'fbxmt.project_setup_tiling_test'
    bl_label   = 'Tiling Test'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        import tempfile, os
        props = context.scene.fbxmt_props
        cell  = props.contact_sheet_size
        cols  = 3
        rows  = 3
        sheet_w = cell * cols
        sheet_h = cell * rows

        # Build temp no_corner_marks materials for both Ignore and Wall
        def _make_tmp(slot, tmp_name):
            from .materials import _read_mat_settings, _build_checker_node_tree
            existing = bpy.data.materials.get(tmp_name)
            if existing:
                bpy.data.materials.remove(existing)
            tmp = bpy.data.materials.new(tmp_name)
            tmp.use_nodes = True
            col_a, col_b, pattern = _read_mat_settings(slot)
            if col_a:
                _build_checker_node_tree(tmp, col_a, col_b, pattern=pattern, geo_texel_density=props.geo_texel_density)
            return tmp_name

        _make_tmp('ignore', '__fbxmt_tiling_test_ignore')
        _make_tmp('wall',   '__fbxmt_tiling_test_wall')

        def _render_slot(tmp_name):
            img = _render_tile(tmp_name, context, size=cell, split=False, no_apex_lines=True)
            if img is None:
                return None
            arr = np.array(img.pixels[:], dtype=np.float32).reshape(cell, cell, 4)
            interior = arr[1:-1, 1:-1, :]
            padded   = np.pad(interior, ((1, 1), (1, 1), (0, 0)), mode='edge')
            bpy.data.images.remove(img)
            return padded

        arr_ignore = _render_slot('__fbxmt_tiling_test_ignore')
        arr_wall   = _render_slot('__fbxmt_tiling_test_wall')

        bpy.data.materials.remove(bpy.data.materials['__fbxmt_tiling_test_ignore'])
        bpy.data.materials.remove(bpy.data.materials['__fbxmt_tiling_test_wall'])

        if arr_ignore is None or arr_wall is None:
            self.report({'ERROR'}, 'Failed to render tiles')
            return {'CANCELLED'}

        # Cross layout: Ignore in centre + 4 edges, Wall in 4 corners
        # Grid positions (col, row) 0-indexed, row 0 = bottom
        _IGNORE = arr_ignore
        _WALL   = arr_wall
        layout = [
            [_WALL,   _IGNORE, _WALL  ],  # top row    (row 2)
            [_IGNORE, _IGNORE, _IGNORE],  # middle row (row 1)
            [_WALL,   _IGNORE, _WALL  ],  # bottom row (row 0)
        ]

        try:
            sheet_px = np.zeros((sheet_h, sheet_w, 4), dtype=np.float32)
            for row in range(rows):
                for col in range(cols):
                    y0  = (rows - 1 - row) * cell
                    x0  = col * cell
                    arr = layout[rows - 1 - row][col]
                    sheet_px[y0:y0 + cell, x0:x0 + cell] = arr

            sheet_name = 'FBXMT_TilingTest'
            existing = bpy.data.images.get(sheet_name)
            if existing:
                bpy.data.images.remove(existing)
            sheet = bpy.data.images.new(sheet_name, width=sheet_w, height=sheet_h, alpha=False)
            sheet.pixels = sheet_px.ravel().tolist()
            sheet.update()

            # Save alongside contact sheet
            export_path = props.export_path.strip()
            if export_path:
                cache_dir  = os.path.join(export_path, 'MaterialCache')
                os.makedirs(cache_dir, exist_ok=True)
                save_path  = os.path.join(cache_dir, 'FBXMT_TilingTest.png')
                sheet.filepath_raw = save_path
                sheet.file_format  = 'PNG'
                sheet.save()
                self.report({'INFO'}, f'Tiling test saved to {save_path}')
            else:
                self.report({'INFO'}, 'Tiling test built — set export path to save')

        except Exception as e:
            self.report({'ERROR'}, f'Tiling test failed: {e}')
            return {'CANCELLED'}

        return {'FINISHED'}


class FBXMT_OT_ProjectSetup_SetContactSheetSize(Operator):
    """Set the contact sheet render size."""
    bl_idname  = 'fbxmt.set_contact_sheet_size'
    bl_label   = 'Set Contact Sheet Size'
    bl_options = {'INTERNAL'}

    size: IntProperty()

    def execute(self, context):
        context.scene.fbxmt_props.contact_sheet_size = self.size
        return {'FINISHED'}


class FBXMT_OT_ProjectSetup_ContactSheet(Operator):
    bl_idname  = 'fbxmt.project_setup_contact_sheet'
    bl_label   = 'Build Contact Sheet'
    bl_options = {'INTERNAL'}

    COLS = 3

    def execute(self, context):
        props = context.scene.fbxmt_props
        prefs = context.scene.fbxmt_prefs_global
        cell  = props.contact_sheet_size
        full  = props.contact_sheet_full

        # Hard cap — above 2048px the RAM path goes not-responding and
        # accumulates gigabytes in bpy.data.images. Use To Disk instead.
        if cell > 2048:
            self.report({'ERROR'},
                f'To RAM is capped at 2048px per tile (current: {cell}px). '
                f'Use "To Disk" from the dropdown for larger sizes.')
            return {'CANCELLED'}

        # Build material list — standard 6 or full 21
        if full:
            mat_names = list(ALL_DISPLAY_MATERIAL_NAMES)
            # ISLAND_SUB_NAMES interleaves Wall/Floor/Ceil per row:
            # Wall_01, Floor_01, Ceil_01, Wall_02, Floor_02, Ceil_02 ...
            # Extract each column's tiles for correct 3-col grid placement
            from .materials import ISLAND_SUB_NAMES as _ISN, RAMP_ISLAND_NAMES as _RIN
            wall_islands  = [_ISN[i] for i in range(0,  15, 3)]   # indices 0,3,6,9,12
            floor_islands = [_ISN[i] for i in range(1,  15, 3)]   # indices 1,4,7,10,13
            ceil_islands  = [_ISN[i] for i in range(2,  15, 3)]   # indices 2,5,8,11,14
            for w, f, c in zip(wall_islands, floor_islands, ceil_islands):
                mat_names.extend([w, f, c])
            # Ramp islands — 3 slots, pad to full row of 3
            mat_names.extend(_RIN + [''] * (3 - len(_RIN) % 3) if len(_RIN) % 3 else _RIN)
        else:
            mat_names = list(ALL_DISPLAY_MATERIAL_NAMES)

        rows = (len(mat_names) + self.COLS - 1) // self.COLS

        # Build temp materials from current prefs — always at 1024tx/m since
        # texel density is irrelevant to the material pattern
        temp_map = _build_preview_materials(prefs, geo_texel_density=1024)

        # Render all tiles at contact_sheet_size — timed per tile
        import time
        imgs        = []
        tile_times  = []
        t_total_start = time.perf_counter()
        try:
            for mat_name in mat_names:
                render_name = temp_map.get(mat_name, mat_name)
                t0  = time.perf_counter()
                img = _render_tile(render_name, context, size=cell, no_apex_lines=True)
                t1  = time.perf_counter()
                tile_times.append((mat_name, t1 - t0))
                if img:
                    img.name = f'__fbxmt_cs_{mat_name}'
                    imgs.append((mat_name, img))
                else:
                    self.report({'WARNING'}, f'Could not render {mat_name} — skipping')
        finally:
            _cleanup_preview_materials()
        t_render_done = time.perf_counter()

        if not imgs:
            self.report({'ERROR'}, 'No material images available')
            return {'CANCELLED'}

        cols    = self.COLS
        sheet_w = cell * cols
        sheet_h = cell * rows

        existing = bpy.data.images.get('FBXMT_ContactSheet')
        if existing:
            bpy.data.images.remove(existing)
        sheet = bpy.data.images.new('FBXMT_ContactSheet', width=sheet_w, height=sheet_h, alpha=False)

        try:
            sheet_px = np.zeros((sheet_h, sheet_w, 4), dtype=np.float32)
            for idx, (mat_name, img) in enumerate(imgs):
                col = idx % cols
                row = idx // cols
                y0  = (rows - 1 - row) * cell
                x0  = col * cell
                if img.size[0] != cell or img.size[1] != cell:
                    img.scale(cell, cell)
                try:
                    # foreach_get into pre-allocated buffer — avoids intermediate Python list
                    buf = np.empty(cell * cell * 4, dtype=np.float32)
                    img.pixels.foreach_get(buf)
                    arr = buf.reshape(cell, cell, 4)
                    # 1px crop on all edges to eliminate EEVEE render border bleed
                    interior = arr[1:-1, 1:-1, :]
                    padded   = np.pad(interior, ((1, 1), (1, 1), (0, 0)), mode='edge')
                    sheet_px[y0:y0 + cell, x0:x0 + cell] = padded
                except Exception as e:
                    self.report({'WARNING'}, f'Could not read pixels for {mat_name}: {e}')
            # foreach_set from raw numpy buffer — avoids .tolist() Python overhead
            sheet.pixels.foreach_set(sheet_px.ravel())
        except Exception as e:
            self.report({'WARNING'}, f'Contact sheet pixel assembly failed: {e}')

        # Draw material name labels
        try:
            # Label drawing — work directly on the numpy array already in memory
            label_px = sheet_px  # already (sheet_h, sheet_w, 4) float32

            def _put_char_np(arr, ch, cx, cy, img_w, img_h, scale=1):
                rows_f = _FONT_5X7.get(ch.upper(), _FONT_5X7[' '])
                for ry, row_f in enumerate(rows_f):
                    for rx, bit in enumerate(row_f):
                        if bit == '1':
                            for sy in range(scale):
                                for sx in range(scale):
                                    px = cx + rx * scale + sx
                                    py = cy - ry * scale - sy
                                    if 0 <= px < img_w and 0 <= py < img_h:
                                        arr[py, px, :] = 1.0

            _COL_LABELS = {0: 'WALL', 1: 'FLOOR', 2: 'CEIL', 3: 'RAMP'}
            _ISLAND_START_IDX = len(ALL_DISPLAY_MATERIAL_NAMES)

            for idx, (mat_name, _img) in enumerate(imgs):
                col  = idx % cols
                row  = idx // cols
                y0   = (rows - 1 - row) * cell
                x0   = col * cell
                if idx >= _ISLAND_START_IDX:
                    island_row = row - 1
                    prefix = _COL_LABELS.get(col, 'ISL')
                    label  = f'{prefix}{format(island_row, "X")}'
                else:
                    label = _MAT_DISPLAY_NAMES.get(mat_name, mat_name.replace('M_FBXMT_', '').replace('_', ' '))[:12]
                scale = max(1, cell // 128)
                cx    = x0 + 4
                cy    = y0 + cell - 4
                for ch in label:
                    _put_char_np(label_px, ch, cx, cy, sheet_w, sheet_h, scale)
                    cx += (5 * scale) + 1
                    if cx + 5 * scale >= x0 + cell:
                        break

            # Write final numpy array back to Blender image — one fast bulk operation
            sheet.pixels.foreach_set(label_px.ravel())
        except Exception as e:
            print(f'[FBXMT] Contact sheet label draw failed: {e}')

        # Save to MaterialCache/
        t_assemble = time.perf_counter() - t_render_done
        t_total    = time.perf_counter() - t_total_start

        # Print benchmark to system console
        px_total = cell * cell * len(mat_names)
        print(f'\n[FBXMT] Contact Sheet Benchmark — {cell}×{cell}px, {len(mat_names)} tiles')
        print(f'{"Material":<40} {"Time (s)":>10}')
        print('-' * 52)
        for name, t in tile_times:
            label = name.replace('M_FBXMT_', '').replace('_', ' ')
            print(f'{label:<40} {t:>10.3f}s')
        print('-' * 52)
        slowest = max(tile_times, key=lambda x: x[1])
        fastest = min(tile_times, key=lambda x: x[1])
        avg     = sum(t for _, t in tile_times) / len(tile_times)
        print(f'{"Fastest:":<40} {fastest[1]:>10.3f}s  ({fastest[0].replace("M_FBXMT_","").replace("_"," ")})')
        print(f'{"Slowest:":<40} {slowest[1]:>10.3f}s  ({slowest[0].replace("M_FBXMT_","").replace("_"," ")})')
        print(f'{"Average per tile:":<40} {avg:>10.3f}s')
        print(f'{"Render total:":<40} {t_render_done - t_total_start:>10.3f}s')
        print(f'{"Assembly + labels:":<40} {t_assemble:>10.3f}s')
        print(f'{"Grand total:":<40} {t_total:>10.3f}s')
        print(f'{"Total pixels:":<40} {px_total:>10,}')
        print(f'{"Est. VRAM/RAM (f32):":<40} {px_total * 16 / 1024**3:>10.2f} GB\n')

        if bpy.data.filepath:
            save_dir  = os.path.join(os.path.dirname(bpy.data.filepath), CACHE_SUBDIR)
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, 'FBXMT_ContactSheet.png')
            sheet.filepath_raw = save_path
            sheet.file_format  = 'PNG'
            sheet.save()
            self.report({'INFO'}, f'Contact sheet saved — {len(mat_names)} tiles, {t_total:.1f}s total ({avg:.2f}s/tile avg)')
        else:
            self.report({'INFO'}, f'Contact sheet generated — {len(mat_names)} tiles, {t_total:.1f}s total ({avg:.2f}s/tile avg)')

        return {'FINISHED'}


class FBXMT_OT_ProjectSetup_ContactSheet_Disk(Operator):
    """Build contact sheet and save directly to disk — low memory path.
    Uses numpy + zlib (stdlib only, no PIL needed). Never loads the full sheet
    into Blender's image system. Suitable for large sizes and network publishing."""
    bl_idname  = 'fbxmt.project_setup_contact_sheet_disk'
    bl_label   = 'Contact Sheet → Disk'
    bl_options = {'INTERNAL'}

    COLS = 3

    @staticmethod
    def _write_png_rgb(path, arr_uint8):
        """Write (H, W, 3) uint8 numpy array as PNG using only numpy + stdlib."""
        import zlib, struct
        h, w = arr_uint8.shape[:2]

        def chunk(name, data):
            c = name + data
            return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

        sig  = b'\x89PNG\r\n\x1a\n'
        ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
        rows = [b'\x00' + row.tobytes() for row in arr_uint8]
        compressed = zlib.compress(b''.join(rows), level=1)  # level 1 = fastest

        with open(path, 'wb') as f:
            f.write(sig)
            f.write(chunk(b'IHDR', ihdr))
            f.write(chunk(b'IDAT', compressed))
            f.write(chunk(b'IEND', b''))

    def execute(self, context):
        props = context.scene.fbxmt_props
        prefs = context.scene.fbxmt_prefs_global
        cell  = props.contact_sheet_size
        full  = props.contact_sheet_full

        # Resolve output path
        if prefs.contact_sheet_output_path.strip():
            out_dir = bpy.path.abspath(prefs.contact_sheet_output_path)
        elif bpy.data.filepath:
            out_dir = os.path.join(os.path.dirname(bpy.data.filepath), CACHE_SUBDIR)
        else:
            self.report({'ERROR'}, 'No output path set and blend file not saved')
            return {'CANCELLED'}

        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, 'FBXMT_ContactSheet.png')

        # Build material list
        if full:
            from .materials import ISLAND_SUB_NAMES as _ISN, RAMP_ISLAND_NAMES as _RIN
            mat_names = list(ALL_DISPLAY_MATERIAL_NAMES)
            wall_islands  = [_ISN[i] for i in range(0,  15, 3)]
            floor_islands = [_ISN[i] for i in range(1,  15, 3)]
            ceil_islands  = [_ISN[i] for i in range(2,  15, 3)]
            for w, f, c in zip(wall_islands, floor_islands, ceil_islands):
                mat_names.extend([w, f, c])
            mat_names.extend(_RIN + [''] * (3 - len(_RIN) % 3) if len(_RIN) % 3 else _RIN)
        else:
            mat_names = list(ALL_DISPLAY_MATERIAL_NAMES)

        cols    = self.COLS
        rows    = (len(mat_names) + cols - 1) // cols
        sheet_w = cell * cols
        sheet_h = cell * rows

        # Build temp materials from current prefs
        temp_map = _build_preview_materials(prefs, geo_texel_density=1024)

        import time
        tile_times    = []
        t_total_start = time.perf_counter()

        # Allocate full sheet as uint8 RGB — much smaller than float32 RGBA
        sheet_u8 = np.zeros((sheet_h, sheet_w, 3), dtype=np.uint8)

        try:
            for idx, mat_name in enumerate(mat_names):
                render_name = temp_map.get(mat_name, mat_name)
                t0  = time.perf_counter()
                img = _render_tile(render_name, context, size=cell, no_apex_lines=True)
                t1  = time.perf_counter()
                tile_times.append((mat_name, t1 - t0))

                if img is None:
                    self.report({'WARNING'}, f'Could not render {mat_name} — skipping')
                    continue

                # Read pixels — foreach_get into pre-allocated buffer
                buf = np.empty(cell * cell * 4, dtype=np.float32)
                img.pixels.foreach_get(buf)
                arr = buf.reshape(cell, cell, 4)

                # 1px crop + edge-pad, drop alpha, convert float→uint8, flip Y
                interior = arr[1:-1, 1:-1, :3]
                padded   = np.pad(interior, ((1, 1), (1, 1), (0, 0)), mode='edge')
                tile_u8  = (np.clip(padded, 0, 1) * 255).astype(np.uint8)
                tile_u8  = np.flipud(tile_u8)

                col = idx % cols
                row = idx // cols
                x0  = col * cell
                y0  = row * cell  # sheet_u8 is top-down
                sheet_u8[y0:y0 + cell, x0:x0 + cell] = tile_u8

                # Free this tile's Blender image immediately — keep memory flat
                bpy.data.images.remove(img)

        finally:
            _cleanup_preview_materials()

        t_render_done = time.perf_counter()

        # Write PNG directly — no bpy.data.images involved for the sheet
        self._write_png_rgb(out_path, sheet_u8)
        t_total = time.perf_counter() - t_total_start

        # Benchmark
        px_total = cell * cell * len(mat_names)
        avg      = sum(t for _, t in tile_times) / max(len(tile_times), 1)
        t_write  = t_total - (t_render_done - t_total_start)
        print(f'\n[FBXMT] Contact Sheet (Disk) Benchmark — {cell}×{cell}px, {len(mat_names)} tiles')
        print(f'{"Render total:":<40} {t_render_done - t_total_start:>10.3f}s')
        print(f'{"Assemble + PNG write:":<40} {t_write:>10.3f}s')
        print(f'{"Grand total:":<40} {t_total:>10.3f}s')
        print(f'{"Average per tile:":<40} {avg:>10.3f}s')
        print(f'{"Total pixels:":<40} {px_total:>10,}')
        print(f'{"Output:":<40} {out_path}\n')

        self.report({'INFO'}, f'Contact sheet saved — {len(mat_names)} tiles, {t_total:.1f}s → {out_path}')
        return {'FINISHED'}


class FBXMT_MT_ContactSheet_Dropdown(bpy.types.Menu):
    """Contact sheet render mode dropdown."""
    bl_idname = 'FBXMT_MT_ContactSheet_Dropdown'
    bl_label  = 'Contact Sheet Options'

    def draw(self, context):
        layout = self.layout
        layout.operator('fbxmt.project_setup_contact_sheet',      text='To RAM',  icon='MEMORY')
        layout.operator('fbxmt.project_setup_contact_sheet_disk', text='To Disk', icon='DISK_DRIVE')
        layout.separator()
        layout.operator('fbxmt.contact_sheet_benchmark',          text='Benchmark', icon='SORTTIME')


# ─── Preset system helpers ────────────────────────────────────────────────────

# Derivation props — stored in every preset, used by Simple load

# ─── Hardcoded default preset ─────────────────────────────────────────────────
# Shipped with the addon, not a file. Applied on first run and via Load Default.
# Values supplied by the author — do not modify without regenerating the preset.

_DEFAULT_PRESET = {
    "format": "full",
    "derivation": {
        "anchor_hue":                    "0.0833",
        "anchor_saturation":             "0.6",
        "anchor_value":                  "0.50",
        "color_b_hue_offset":            "0",
        "color_b_saturation":            "0.6",
        "color_b_value":                 "0.35",
        "island_marker_saturation":      "0.6",
        "island_marker_value":           "0.50",
        "island_marker_b_hue_offset":    "0",
        "island_marker_b_saturation":    "0.6",
        "island_marker_b_value":         "0.35",
        "checker_scale":            8,
        "corner_mark_preset":       2,
        "show_corner_circle":       False,
        "show_corner_lines":        False,
        "bake_labels":              True,
        "checker_pattern_floor":    "SQUARE",
        "checker_pattern_ceiling":  "SQUARE",
        "checker_pattern_wall":     "SQUARE",
        "checker_pattern_trim":     "SQUARE",
        "checker_pattern_ignore":   "SQUARE",
        "checker_pattern_island":   "CIRCLE",
        "apex_line_seed":           42,
    },
    "colours": {
        "color_floor_a":        [0.25,   0.75,   0.4999, 1.0],
        "color_ceiling_a":      [0.4999, 0.25,   0.75,   1.0],
        "color_wall_a":         [0.75,   0.4999, 0.25,   1.0],
        "color_trim_a":         [0.7499, 0.25,   0.75,   1.0],
        "color_ignore_a":       [0.25,   0.25,   0.25,   1.0],
        "color_ramp_floor_a":   [0.6,    0.7,    0.25,   1.0],
        "color_ramp_ceiling_a": [0.45,   0.65,   0.75,   1.0],
    },
    "__name__": "FBXMT_default",
}

_PRESET_DERIVATION_PROPS = [
    'anchor_hue',
    'anchor_saturation',
    'anchor_value',
    'color_b_hue_offset',
    'color_b_saturation',
    'color_b_value',
    'island_marker_saturation',
    'island_marker_value',
    'island_marker_b_hue_offset',
    'island_marker_b_saturation',
    'island_marker_b_value',
    'checker_scale',
    'corner_mark_preset',
    'show_corner_circle',
    'show_corner_lines',
    'bake_labels',
    *[f'checker_pattern_{s}' for s in ('floor', 'ceiling', 'wall', 'trim', 'ignore', 'island', 'ramp_floor', 'ramp_ceiling')],
    'apex_line_seed',
]

# Full colour stack — stored in addition to derivation props in every preset
# Island colour excluded — always derived from wall A at build time
_PRESET_COLOUR_PROPS = [
    *[f'color_{s}_a' for s in ('floor', 'ceiling', 'wall', 'trim', 'ignore', 'ramp_floor', 'ramp_ceiling')],
    # color_{s}_b removed — B is always derived, never stored
]

# Swatch material order and labels.
# B is derived at draw time via _resolve_color_b — prop_b column removed.
_SWATCH_MATS = [
    ('wall',         'color_wall_a',         'Wall'),
    ('ramp_floor',   'color_ramp_floor_a',   'Ramp Fl'),
    ('floor',        'color_floor_a',        'Floor'),
    ('ramp_ceiling', 'color_ramp_ceiling_a', 'Ramp Cl'),
    ('ceiling',      'color_ceiling_a',      'Ceiling'),
    ('trim',         'color_trim_a',         'Trim'),
    ('ignore',       'color_ignore_a',       'Ignore'),
    ('island',       'color_wall_a',         'Island*'),
]

_SWATCH_SQ   = 16   # pixels per colour square
_SWATCH_PAD  = 2    # gap between A and B squares
_SWATCH_ROW  = 12   # row height (squares are 8px, 2px padding above/below)
_SWATCH_LABEL_W = 40  # pixels reserved for label
_SWATCH_W    = _SWATCH_LABEL_W + _SWATCH_SQ * 2 + _SWATCH_PAD
_SWATCH_H    = _SWATCH_ROW * len(_SWATCH_MATS)


def _build_swatch_image(prefs, name):
    """Build a bpy.data.images swatch: 6 rows × (label + A square | B square).
    Returns the image. Caller owns it and must remove when done.
    """
    w, h = _SWATCH_W, _SWATCH_H
    existing = bpy.data.images.get(name)
    if existing:
        bpy.data.images.remove(existing)
    img = bpy.data.images.new(name, width=w, height=h, alpha=False, float_buffer=False)
    px  = np.zeros((h, w, 4), dtype=np.float32)
    px[:, :, 3] = 1.0  # fully opaque background (dark grey)
    px[:, :, :3] = 0.15

    from .materials import _resolve_color_b as _rcb
    _b_off = getattr(prefs, 'color_b_hue_offset', '0')
    _b_sat = _safe_float(prefs, 'color_b_saturation', 0.6)
    _b_val = _safe_float(prefs, 'color_b_value', 0.35)

    import colorsys as _cs
    for row_idx, (slot, prop_a, label) in enumerate(_SWATCH_MATS):
        y0 = h - (row_idx + 1) * _SWATCH_ROW   # top-down order
        y1 = y0 + _SWATCH_ROW

        col_a_raw = tuple(getattr(prefs, prop_a, (0.5, 0.5, 0.5, 1.0))[:3])

        if slot == 'island':
            # Island A: wall hue with island-specific sat/val
            h_i, _l, _s = _cs.rgb_to_hls(*col_a_raw)
            isl_sat = _safe_float(prefs, 'island_marker_saturation', 0.6)
            isl_val = _safe_float(prefs, 'island_marker_value', 0.50)
            col_a = _cs.hls_to_rgb(h_i, isl_val, isl_sat)
            # Island B: island-specific B params
            isl_b_off = getattr(prefs, 'island_marker_b_hue_offset', '0')
            isl_b_sat = _safe_float(prefs, 'island_marker_b_saturation', 0.6)
            isl_b_val = _safe_float(prefs, 'island_marker_b_value', 0.35)
            col_b = _rcb(col_a, isl_b_off, isl_b_sat, isl_b_val)
        else:
            col_a = col_a_raw
            col_b = _rcb(col_a, _b_off, _b_sat, _b_val)

        # A square
        ax0 = _SWATCH_LABEL_W
        ax1 = ax0 + _SWATCH_SQ
        sq_y0 = y0 + (_SWATCH_ROW - _SWATCH_SQ) // 2
        sq_y1 = sq_y0 + _SWATCH_SQ
        px[sq_y0:sq_y1, ax0:ax1, :3] = col_a

        # B square
        bx0 = ax1 + _SWATCH_PAD
        bx1 = bx0 + _SWATCH_SQ
        px[sq_y0:sq_y1, bx0:bx1, :3] = col_b

        # Label — draw using _FONT_5X7, white text
        cx = 2
        cy = y0 + (_SWATCH_ROW + 5) // 2  # vertically centre 5px-tall glyph
        for ch in label.upper():
            rows_f = _FONT_5X7.get(ch, _FONT_5X7[' '])
            for ry, row_f in enumerate(rows_f):
                for rx, bit in enumerate(row_f):
                    if bit == '1':
                        px_y = cy - ry
                        px_x = cx + rx
                        if 0 <= px_y < h and 0 <= px_x < _SWATCH_LABEL_W:
                            px[px_y, px_x, :3] = (1.0, 1.0, 1.0)
            cx += 6
            if cx + 5 >= _SWATCH_LABEL_W:
                break

    img.pixels.foreach_set(px.ravel())
    img.update()
    return img


def _save_swatch_png(prefs, json_path):
    """Save a companion swatch PNG alongside json_path. Returns the PNG path."""
    png_path = os.path.splitext(json_path)[0] + '.png'
    img_name = '__fbxmt_swatch_save_tmp'
    img = _build_swatch_image(prefs, img_name)
    try:
        img.filepath_raw = png_path
        img.file_format  = 'PNG'
        img.save()
    except Exception as e:
        print(f'[FBXMT] Swatch save failed: {e}')
        png_path = None
    finally:
        bpy.data.images.remove(img)
    return png_path


def _load_swatch_into_blender(png_path, img_name):
    """Load a swatch PNG into bpy.data.images. Returns image or None."""
    if not png_path or not os.path.exists(png_path):
        return None
    existing = bpy.data.images.get(img_name)
    if existing:
        bpy.data.images.remove(existing)
    try:
        img = bpy.data.images.load(png_path)
        img.name = img_name
        return img
    except Exception:
        return None


def _get_presets_dir(context):
    """Return the presets directory path, or None if not configured.
    Path lives on FBXMT_GlobalPrefs (scene-stored) so it persists with
    the blend file and startup template without requiring a manual prefs save.
    """
    try:
        path = context.scene.fbxmt_prefs_global.presets_path.strip()
        return path if path else None
    except Exception:
        return None


def _list_presets(context):
    """Return sorted list of (name, filepath) tuples for all .json presets."""
    import glob
    d = _get_presets_dir(context)
    if not d or not os.path.isdir(d):
        return []
    files = sorted(glob.glob(os.path.join(d, '*.json')))
    return [(os.path.splitext(os.path.basename(f))[0], f) for f in files]


def _prop_to_json(prefs, prop):
    val = getattr(prefs, prop, None)
    if val is None:
        return None
    if hasattr(val, '__iter__') and not isinstance(val, str):
        return [round(v, 4) if isinstance(v, float) else v for v in val]
    if isinstance(val, float):
        return round(val, 4)
    return val


def _prefs_to_dict(prefs):
    """Serialise preset to new-format dict with derivation and colours sections."""
    derivation = {}
    for prop in _PRESET_DERIVATION_PROPS:
        v = _prop_to_json(prefs, prop)
        if v is not None:
            derivation[prop] = v
    colours = {}
    for prop in _PRESET_COLOUR_PROPS:
        v = _prop_to_json(prefs, prop)
        if v is not None:
            colours[prop] = v
    return {'format': 'full', 'derivation': derivation, 'colours': colours}


def _dict_to_prefs_full(prefs, data):
    """Apply a full preset dict — sets derivation props AND colour stack."""
    # Handle legacy flat-dict presets (no 'format' key)
    if 'format' not in data:
        for prop, val in data.items():
            if prop.startswith('__') or not hasattr(prefs, prop):
                continue
            try:
                setattr(prefs, prop, val)
            except Exception:
                pass
        return
    for section in ('derivation', 'colours'):
        for prop, val in data.get(section, {}).items():
            if not hasattr(prefs, prop):
                continue
            try:
                setattr(prefs, prop, val)
            except Exception:
                pass


def _dict_to_prefs_simple(prefs, data):
    """Apply a simple load — derivation props only, then re-derive colours."""
    from .materials import _derive_colours_from_anchor
    for prop, val in data.get('derivation', data).items():
        if prop.startswith('__') or prop not in _PRESET_DERIVATION_PROPS:
            continue
        if not hasattr(prefs, prop):
            continue
        try:
            setattr(prefs, prop, val)
        except Exception:
            pass
    _derive_colours_from_anchor(prefs)



class FBXMT_OT_Preset_LoadDefault(Operator):
    """Load the built-in FBXMT default preset. Cannot be deleted or modified."""
    bl_idname  = 'fbxmt.preset_load_default'
    bl_label   = 'Load Default'
    bl_options = {'INTERNAL', 'UNDO'}

    def execute(self, context):
        prefs = context.scene.fbxmt_prefs_global
        _dict_to_prefs_full(prefs, _DEFAULT_PRESET)
        from .materials import rebuild_fbxmt_materials
        rebuild_fbxmt_materials()
        _render_dialog_tiles_sync(context)
        FBXMT_OT_ProjectSetup._active_snapshot = _snapshot_prefs(prefs)
        self.report({'INFO'}, 'Default preset loaded')
        return {'FINISHED'}


class OT_FBXMT_Preset_Save(Operator):
    """Save current material settings as a named preset (always Full format)."""
    bl_idname  = 'fbxmt.preset_save'
    bl_label   = 'Save Preset'
    bl_options = {'INTERNAL'}

    name:      bpy.props.StringProperty(name="Preset Name", default="My Preset")
    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        self.layout.prop(self, "name", text="Name")

    def execute(self, context):
        import json
        name = self.name.strip()
        if not name:
            self.report({'ERROR'}, 'Please enter a preset name')
            return {'CANCELLED'}

        if self.directory:
            context.scene.fbxmt_prefs_global.presets_path = self.directory
            d = self.directory
        else:
            d = _get_presets_dir(context)

        if not d:
            self.report({'WARNING'}, "No presets folder set — pick one now.")
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}

        os.makedirs(d, exist_ok=True)
        safe     = "".join(c if c.isalnum() or c in ' _-' else '_' for c in name)
        filepath = os.path.join(d, safe + '.json')
        prefs    = context.scene.fbxmt_prefs_global
        data     = _prefs_to_dict(prefs)
        data['__name__'] = name
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

        _save_swatch_png(prefs, filepath)

        self.report({'INFO'}, f'Preset saved: {name}')
        return {'FINISHED'}


class OT_FBXMT_Preset_Load(Operator):
    """Load a material preset — asks Simple (derive from hue) or Full (apply all values)."""
    bl_idname  = 'fbxmt.preset_load'
    bl_label   = 'Load Preset'
    bl_options = {'INTERNAL', 'UNDO'}

    filepath: bpy.props.StringProperty()
    load_mode: bpy.props.EnumProperty(
        name='Load Mode',
        items=[
            ('SIMPLE', 'Simple', 'Apply anchor hue and derive all colours fresh'),
            ('FULL',   'Full',   'Apply all stored colour values verbatim'),
        ],
        default='FULL',
    )

    def invoke(self, context, event):
        # Load swatch preview for the dialog
        png_path = os.path.splitext(self.filepath)[0] + '.png'
        _load_swatch_into_blender(png_path, '__fbxmt_swatch_load_preview')
        return context.window_manager.invoke_props_dialog(self, width=280)

    def draw(self, context):
        layout = self.layout
        import json
        name = os.path.splitext(os.path.basename(self.filepath))[0]
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            name = data.get('__name__', name)
        except Exception:
            pass
        layout.label(text=name, icon='PRESET')
        layout.separator(factor=0.5)

        # Swatch preview
        swatch = bpy.data.images.get('__fbxmt_swatch_load_preview')
        if swatch:
            swatch.preview_ensure()
            layout.template_icon(icon_value=swatch.preview.icon_id, scale=4.0)
        else:
            layout.label(text='(no preview)', icon='INFO')

        layout.separator(factor=0.5)
        layout.prop(self, 'load_mode', expand=True)

    def execute(self, context):
        import json
        # Clean up preview swatch
        swatch = bpy.data.images.get('__fbxmt_swatch_load_preview')
        if swatch:
            bpy.data.images.remove(swatch)

        if not os.path.exists(self.filepath):
            self.report({'ERROR'}, f'Preset file not found: {self.filepath}')
            return {'CANCELLED'}
        with open(self.filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        prefs = context.scene.fbxmt_prefs_global
        if self.load_mode == 'SIMPLE':
            _dict_to_prefs_simple(prefs, data)
        else:
            _dict_to_prefs_full(prefs, data)
        from .materials import rebuild_fbxmt_materials
        rebuild_fbxmt_materials()
        _render_dialog_tiles_sync(context)
        # Update snapshot so Cancel restores to this preset state
        FBXMT_OT_ProjectSetup._active_snapshot = _snapshot_prefs(prefs)
        name = data.get('__name__', os.path.basename(self.filepath))
        mode = 'simple' if self.load_mode == 'SIMPLE' else 'full'
        # Full load — lock controls and record preset name
        if self.load_mode == 'FULL':
            prefs.preset_locked      = True
            prefs.active_preset_name = name
        self.report({'INFO'}, f'Preset loaded ({mode}): {name}')
        return {'FINISHED'}


class OT_FBXMT_Preset_Delete(Operator):
    """Delete a material preset file."""
    bl_idname  = 'fbxmt.preset_delete'
    bl_label   = 'Delete Preset'
    bl_options = {'INTERNAL'}

    filepath: bpy.props.StringProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        if os.path.exists(self.filepath):
            os.remove(self.filepath)
            self.report({'INFO'}, 'Preset deleted')
        return {'FINISHED'}


class OT_FBXMT_SelectTile(Operator):
    """Select a material tile by index — sets active material for editing."""
    bl_idname  = 'fbxmt.select_tile'
    bl_label   = 'Select Tile'
    bl_options = {'INTERNAL'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        context.scene.fbxmt_props.fbxmt_selected_mat_index = self.index
        try:
            context.scene.fbxmt_preview_mat_enum = str(self.index)
        except Exception:
            pass
        return {'FINISHED'}


class OT_FBXMT_ApplyBToAll(Operator):
    """Copy the current material's Colour B mode and value to all 6 materials.
    Preview tiles update; 3D viewport rebuild happens only on dialog close."""
    bl_idname  = 'fbxmt.apply_b_to_all'
    bl_label   = 'Apply B to All'
    bl_options = {'UNDO', 'INTERNAL'}

    source_slot: bpy.props.StringProperty(options={'HIDDEN'})

    def execute(self, context):
        # B is now globally derived — no per-slot spreading needed.
        # Operator retained to avoid unregistered bl_idname errors from saved blend files.
        return {'FINISHED'}


class FBXMT_OT_ApplyAnchor(Operator):
    """Derive all material colours from the anchor hue and refresh preview tiles.
    The 3D viewport rebuild happens only when the Project Setup dialog is closed."""
    bl_idname  = 'fbxmt.apply_anchor'
    bl_label   = 'Apply Anchor'
    bl_options = {'INTERNAL', 'UNDO'}

    def execute(self, context):
        prefs = context.scene.fbxmt_prefs_global
        from .materials import _derive_colours_from_anchor
        _derive_colours_from_anchor(prefs)
        context.scene.fbxmt_props.fbxmt_preview_stale = True
        # Synchronous render — bake_all_modal can't tick inside invoke_props_dialog
        _render_dialog_tiles_sync(context)
        return {'FINISHED'}


class FBXMT_OT_ResetAnchor(Operator):
    """Reset all material settings to the state when the setup window was opened."""
    bl_idname  = 'fbxmt.reset_anchor'
    bl_label   = 'Reset'
    bl_options = {'INTERNAL', 'UNDO'}

    def execute(self, context):
        prefs    = context.scene.fbxmt_prefs_global
        snapshot = getattr(bpy.types.FBXMT_OT_ProjectSetup, '_active_snapshot', None)
        if not snapshot:
            self.report({'WARNING'}, 'No snapshot available — open Setup window first')
            return {'CANCELLED'}
        _restore_prefs(prefs, snapshot)
        from .materials import _derive_colours_from_anchor
        _derive_colours_from_anchor(prefs)
        context.scene.fbxmt_props.fbxmt_preview_stale = True
        _render_dialog_tiles_sync(context)
        return {'FINISHED'}


# ─── Operator: main project setup dialog ─────────────────────────────────────

# ── Material settings snapshot ────────────────────────────────────────────────
# All left-side props that the dialog can change. Right-side (paths, import
# settings) are persistent and never snapshotted.

_MATERIAL_SNAPSHOT_PROPS = [
    'anchor_hue',
    'anchor_saturation',
    'anchor_value',
    'color_b_hue_offset',
    'color_b_saturation',
    'color_b_value',
    'island_marker_saturation',
    'island_marker_value',
    'island_marker_b_hue_offset',
    'island_marker_b_saturation',
    'island_marker_b_value',
    'checker_scale',
    'corner_mark_preset',
    'corner_mark_width_px',
    'show_corner_circle',
    'show_corner_lines',
    'apex_line_seed',
    'preset_locked',
    'active_preset_name',
    *[f'color_{s}_a'         for s in ('floor', 'ceiling', 'wall', 'trim', 'ignore', 'ramp_floor', 'ramp_ceiling')],
    *[f'color_{s}_b'         for s in ('floor', 'ceiling', 'wall', 'trim', 'ignore', 'ramp_floor', 'ramp_ceiling')],
    *[f'checker_pattern_{s}' for s in ('floor', 'ceiling', 'wall', 'trim', 'ignore', 'island', 'ramp_floor', 'ramp_ceiling')],
]


def _snapshot_prefs(prefs):
    """Return a dict copy of all material settings from prefs."""
    snap = {}
    for prop in _MATERIAL_SNAPSHOT_PROPS:
        val = getattr(prefs, prop, None)
        if val is None:
            continue
        # FloatVectorProperty returns a bpy_prop_array — copy to plain tuple
        if hasattr(val, '__len__') and not isinstance(val, str):
            snap[prop] = tuple(val)
        else:
            snap[prop] = val
    return snap


def _restore_prefs(prefs, snapshot):
    """Write a snapshot dict back onto prefs."""
    for prop, val in snapshot.items():
        try:
            setattr(prefs, prop, val)
        except Exception as e:
            print(f'[FBXMT] _restore_prefs: could not restore {prop}: {e}')


class FBXMT_OT_ProjectSetup(Operator):
    bl_idname  = 'fbxmt.project_setup'
    bl_label   = 'FBXMT Project Setup'
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        ensure_fbxmt_materials()

        # Snapshot current material settings — restored on cancel/click-outside
        prefs = context.scene.fbxmt_prefs_global
        self._snapshot = _snapshot_prefs(prefs)
        FBXMT_OT_ProjectSetup._active_snapshot = self._snapshot

        # Centre the dialog on the active area (viewport), falling back to window centre
        win  = context.window
        area = context.area
        if area:
            x = area.x + area.width  // 2
            y = area.y + area.height // 2
        else:
            x = win.width  // 2
            y = win.height // 2
        context.window.cursor_warp(x, y)

        # Render preview tiles from current material state on open — synchronous
        # so tiles are ready before the dialog first draws
        _render_dialog_tiles_sync(context)

        return context.window_manager.invoke_props_dialog(self, width=720)

    def execute(self, context):
        # OK pressed — snapshot is accepted, discard it
        self._snapshot = {}

        # Purge dialog-local swatch images
        for img in list(bpy.data.images):
            if img.name.startswith('__fbxmt_swatch_'):
                bpy.data.images.remove(img)

        # Rebuild viewport materials from the accepted settings
        rebuild_fbxmt_materials()
        with bpy.context.temp_override(window=context.window, scene=context.scene):
            bpy.ops.fbxmt.bake_all_modal('INVOKE_DEFAULT')
        context.scene.fbxmt_props.fbxmt_preview_stale = False
        return {'FINISHED'}

    def cancel(self, context):
        # Cancel / click-outside — restore original settings, no viewport rebuild
        prefs = context.scene.fbxmt_prefs_global
        if hasattr(self, '_snapshot') and self._snapshot:
            _restore_prefs(prefs, self._snapshot)
            self._snapshot = {}

        # Purge dialog-local swatch images
        for img in list(bpy.data.images):
            if img.name.startswith('__fbxmt_swatch_'):
                bpy.data.images.remove(img)

    def draw(self, context):
        layout = self.layout
        props  = context.scene.fbxmt_props
        prefs  = context.scene.fbxmt_prefs_global

        if props is None or prefs is None:
            layout.label(text='Scene properties unavailable', icon='ERROR')
            return

        # ── Tile grid — 4 columns, 2 rows ────────────────────────────────────
        _PANEL_TILES = [
            'M_FBXMT_Wall',         'M_FBXMT_Floor',      'M_FBXMT_Ceiling',     'M_FBXMT_Ignore',
            'M_FBXMT_Island',       'M_FBXMT_Ramp_Floor', 'M_FBXMT_Ramp_Ceiling','M_FBXMT_Trim',
        ]
        box = layout.box()
        for row_start in range(0, len(_PANEL_TILES), 4):
            row_names = _PANEL_TILES[row_start:row_start + 4]
            tile_row = box.row(align=True)
            for mn in row_names:
                col = tile_row.column()
                img = bpy.data.images.get(f'__tile_{mn}')
                if img:
                    img.preview_ensure()
                    col.template_icon(icon_value=img.preview.icon_id, scale=5.0)
                else:
                    sub = col.box()
                    sub.scale_y = 3.5
                    sub.label(text='')
                lbl_row = col.row()
                lbl_row.split(factor=0.5)
                lbl_row.alignment = 'CENTER'
                lbl_row.label(text=_MAT_DISPLAY_NAMES.get(mn, mn))

        # ── Preview buttons ───────────────────────────────────────────────────
        body = layout.box()
        row = body.row(align=True)
        row.operator('fbxmt.project_setup_update_tile',   text='Update Tile',   icon='FILE_REFRESH')
        # Contact sheet — split button: main action left, dropdown right
        cs_row = row.row(align=True)
        cs_row.operator('fbxmt.project_setup_contact_sheet', text='Contact Sheet', icon='IMAGE_REFERENCE')
        cs_row.menu('FBXMT_MT_ContactSheet_Dropdown', text='', icon='DOWNARROW_HLT')
        row.operator('fbxmt.project_setup_tiling_test',   text='Tiling Test',   icon='TEXTURE')
        row.prop(props, 'contact_sheet_full', text='Full', toggle=True)
        if props.fbxmt_preview_stale:
            row.label(text='', icon='ERROR')

        # Contact sheet size — fixed options independent of texel density
        # Sizes above 2048px are only safe with To Disk (dropdown)
        row = body.row(align=True)
        row.label(text='Sheet Size:')
        for sz in (256, 512, 1024, 2048, 4096):
            sub = row.row(align=True)
            if sz > 2048:
                sub.alert = (props.contact_sheet_size == sz)
            op = sub.operator(
                'fbxmt.set_contact_sheet_size',
                text=str(sz),
                depress=(props.contact_sheet_size == sz),
            )
            op.size = sz
        if props.contact_sheet_size > 2048:
            row = body.row()
            row.alert = False
            row.label(text='Use "To Disk" for sizes above 2048px', icon='INFO')

        body.separator(factor=0.5)

        # ── Tab bar — inline horizontal ───────────────────────────────────────
        tab_row = body.row(align=True)
        tab_row.prop_enum(props, 'setup_tab', 'MATERIALS')
        tab_row.prop_enum(props, 'setup_tab', 'PROJECT')
        body.separator(factor=0.5)

        # ══════════════════════════════════════════════════════════════════════
        # MATERIALS TAB
        # ══════════════════════════════════════════════════════════════════════
        if props.setup_tab == 'MATERIALS':

            split    = body.split(factor=0.5)
            col_left  = split.box()
            col_right = split.column()
            checker_box = col_right.box()

            # ── LEFT: Anchor + Colour B + Island Marker ───────────────────────
            col_left.separator(factor=0.5)
            col_left.label(text='Material Settings', icon='SHADING_RENDERED')
            col_left.separator(factor=0.5)

            locked = prefs.preset_locked

            # Anchor Colour A
            col_left.label(text='Anchor Colour — A:')
            a_row = col_left.row(align=False)
            a_row.enabled = not locked
            a_row.separator()
            a_row.prop(prefs, 'anchor_hue',        text="H")
            a_row.separator(factor=0.5)
            a_row.prop(prefs, 'anchor_saturation', text="S")
            a_row.separator(factor=0.5)
            a_row.prop(prefs, 'anchor_value',      text="V")
            a_row.separator()
            col_left.separator(factor=0.5)

            # Colour B
            col_left.label(text='Anchor Colour — B:')
            b_row = col_left.row(align=False)
            b_row.enabled = not locked
            b_row.separator()
            b_row.prop(prefs, 'color_b_hue_offset', text="H+")
            b_row.separator(factor=0.5)
            b_row.prop(prefs, 'color_b_saturation', text="S")
            b_row.separator(factor=0.5)
            b_row.prop(prefs, 'color_b_value',      text="V")
            b_row.separator()
            col_left.separator(factor=0.5)

            # Swatch row 1 — Wall / Floor / Ceiling (A + B each)
            swatch_row = col_left.row(align=True)
            for mat_label, a_img_name in (
                ('W', '__fbxmt_swatch_wall'),
                ('F', '__fbxmt_swatch_floor'),
                ('C', '__fbxmt_swatch_ceiling'),
            ):
                b_img_name = a_img_name.replace('_swatch_', '_swatch_b_')
                img_a = bpy.data.images.get(a_img_name)
                img_b = bpy.data.images.get(b_img_name)
                if img_a:
                    img_a.preview_ensure()
                    swatch_row.template_icon(icon_value=img_a.preview.icon_id, scale=1.875)
                else:
                    swatch_row.label(text=mat_label + 'A')
                if img_b:
                    img_b.preview_ensure()
                    swatch_row.template_icon(icon_value=img_b.preview.icon_id, scale=1.875)
                else:
                    swatch_row.label(text=mat_label + 'B')

            # Swatch row 2 — Island / Ramp Floor / Ramp Ceiling (A + B each)
            # Island replaces the old transparent spacers — tracks Wall so colours match
            ramp_swatch_row = col_left.row(align=True)
            for mat_label, a_img_name in (
                ('I',  '__fbxmt_swatch_island'),
                ('RF', '__fbxmt_swatch_ramp_floor'),
                ('RC', '__fbxmt_swatch_ramp_ceiling'),
            ):
                b_img_name = a_img_name.replace('_swatch_', '_swatch_b_')
                img_a = bpy.data.images.get(a_img_name)
                img_b = bpy.data.images.get(b_img_name)
                if img_a:
                    img_a.preview_ensure()
                    ramp_swatch_row.template_icon(icon_value=img_a.preview.icon_id, scale=1.875)
                else:
                    ramp_swatch_row.label(text=mat_label + 'A')
                if img_b:
                    img_b.preview_ensure()
                    ramp_swatch_row.template_icon(icon_value=img_b.preview.icon_id, scale=1.875)
                else:
                    ramp_swatch_row.label(text=mat_label + 'B')
            col_left.separator(factor=0.5)

            # Island Colour
            col_left.label(text='Island Colour — A:')
            isl_a_row = col_left.row(align=False)
            isl_a_row.enabled = not locked
            isl_a_row.separator()
            isl_a_row.prop(prefs, 'island_marker_saturation', text="S")
            isl_a_row.separator(factor=0.5)
            isl_a_row.prop(prefs, 'island_marker_value',      text="V")
            isl_a_row.separator()
            col_left.separator(factor=0.5)

            col_left.label(text='Island Colour — B:')
            isl_b_row = col_left.row(align=False)
            isl_b_row.enabled = not locked
            isl_b_row.separator()
            isl_b_row.prop(prefs, 'island_marker_b_hue_offset', text="H+")
            isl_b_row.separator(factor=0.5)
            isl_b_row.prop(prefs, 'island_marker_b_saturation', text="S")
            isl_b_row.separator(factor=0.5)
            isl_b_row.prop(prefs, 'island_marker_b_value',      text="V")
            isl_b_row.separator()
            col_left.separator(factor=0.5)

            # Apply + Reset at bottom of box
            apply_row = col_left.row(align=False)
            apply_row.enabled = not locked
            apply_row.separator()
            apply_row.operator('fbxmt.apply_anchor', text='Apply', icon='FILE_REFRESH')
            apply_row.separator(factor=0.5)
            apply_row.operator('fbxmt.reset_anchor', text='Reset', icon='LOOP_BACK')
            apply_row.separator()
            col_left.separator(factor=0.5)

            # ── RIGHT: Checker Style + Density + Scale + Lines + Lock ─────────
            locked = prefs.preset_locked

            checker_box.separator(factor=0.5)
            checker_box.label(text='Checker Style:', icon='TEXTURE')
            checker_box.separator(factor=0.5)
            style_col = checker_box.column()
            style_col.enabled = not locked
            for i, ((slot_l, label_l), (slot_r, label_r)) in enumerate((
                (('wall',         'Wall'),       ('island',       'Island')),
                (('floor',        'Floor'),      ('ramp_floor',   'Ramp Fl')),
                (('ceiling',      'Ceiling'),    ('ramp_ceiling', 'Ramp Cl')),
                (('ignore',       'Ignore'),     ('trim',         'Trim')),
            )):
                if i > 0:
                    style_col.separator(factor=0.4)
                row = style_col.row(align=False)
                row.separator()
                left = row.split(factor=0.5, align=True)
                ls = left.split(factor=0.35, align=True)
                ls.label(text=label_l)
                ls.prop(prefs, f'checker_pattern_{slot_l}', text='')
                rs = left.split(factor=0.35, align=True)
                rs.label(text=label_r)
                rs.prop(prefs, f'checker_pattern_{slot_r}', text='')
                row.separator()
            checker_box.separator(factor=0.5)

            checker_box.label(text='Texel Density:')
            checker_box.separator(factor=0.5)
            row = checker_box.row(align=True)
            row.enabled = not locked
            row.separator()
            for val in (1024, 2048, 4096, 8192):
                op = row.operator(
                    'fbxmt.project_setup_set_density',
                    text    = str(val),
                    depress = (props.geo_texel_density == val),
                )
                op.density = val
            row.separator()
            checker_box.separator(factor=0.5)

            checker_box.label(text='Checker Scale:')
            checker_box.separator(factor=0.5)
            row = checker_box.row(align=True)
            row.enabled = not locked
            row.separator()
            for val in (1, 2, 4, 8):
                op = row.operator(
                    'fbxmt.project_setup_set_checker_scale',
                    text    = str(val),
                    depress = (prefs.checker_scale == val),
                )
                op.value = val
            row.separator()
            checker_box.separator(factor=0.5)

            lines_row = checker_box.row(align=True)
            lines_row.enabled = not locked
            lines_row.separator()
            lines_row.prop(prefs, 'show_corner_lines', text='Lines', toggle=True)
            lines_row.separator()
            lines_row.label(text='Seed:')
            lines_row.prop(prefs, 'apex_line_seed', text='')
            lines_row.separator()
            checker_box.separator(factor=0.5)

            # Lock settings — own box below checker box, double space above
            col_right.separator(factor=2.0)
            lock_box = col_right.box()
            _draw_preset_lock_ticker(lock_box, prefs)
            col_right.separator(factor=0.5)

        # ══════════════════════════════════════════════════════════════════════
        # PROJECT TAB
        # ══════════════════════════════════════════════════════════════════════
        elif props.setup_tab == 'PROJECT':

            split     = body.split(factor=0.5)
            col_left  = split.column()
            col_right = split.column()

            # ── LEFT: Paths + Import ──────────────────────────────────────────
            paths_box = col_left.box()
            paths_box.label(text='Paths:', icon='FILEBROWSER')
            paths_box.prop(props, 'export_path',  text='Export')
            paths_box.prop(props, 'import_path',  text='Import')
            paths_box.prop(prefs, 'contact_sheet_output_path', text='Contact Sheet')
            col_left.separator(factor=0.5)

            import_box = col_left.box()
            import_box.label(text='Import:', icon='IMPORT')
            import_box.prop(props,  'quick_import_type', text='Type')
            import_box.prop(prefs,  'prep_on_import',    text='Full Prep on Import')

            col_left.separator(factor=0.5)
            classify_box = col_left.box()
            classify_box.label(text='Classification:', icon='SORTSIZE')
            classify_box.prop(props, 'ramp_wall_threshold', text='Floor Angle')
            classify_box.prop(props, 'floor_ramp_threshold',     text='Ramp Angle')

            # ── RIGHT: Presets ────────────────────────────────────────────────
            preset_box_outer = col_right.box()
            preset_box_outer.label(text='Material Presets', icon='PRESET')
            preset_box_outer.prop(prefs, 'presets_path', text='Folder')
            preset_box_outer.separator(factor=0.5)
            presets = _list_presets(context)
            preset_box_outer.operator('fbxmt.preset_load_default', text='Load Default', icon='LOOP_BACK')
            preset_box_outer.separator(factor=0.5)
            if presets:
                sel_idx = getattr(context.scene, 'fbxmt_selected_preset_index', 0)
                sel_idx = max(0, min(sel_idx, len(presets) - 1))
                drop_row = preset_box_outer.row(align=True)
                sel_name, sel_path = presets[sel_idx]
                drop_row.menu('FBXMT_MT_PresetPicker', text=sel_name, icon='PRESET')
                del_op = drop_row.operator('fbxmt.preset_delete', text='', icon='X')
                del_op.filepath = sel_path
                preset_box_outer.separator(factor=0.5)
                png_path   = os.path.splitext(sel_path)[0] + '.png'
                swatch_key = f'__fbxmt_swatch_{sel_name}'
                swatch     = bpy.data.images.get(swatch_key) or _load_swatch_into_blender(png_path, swatch_key)
                if swatch:
                    swatch.preview_ensure()
                    preset_box_outer.template_icon(icon_value=swatch.preview.icon_id, scale=3.5)
                else:
                    preset_box_outer.label(text='(no preview)', icon='INFO')
                preset_box_outer.separator(factor=0.5)
                load_op = preset_box_outer.operator('fbxmt.preset_load', text='Load...', icon='IMPORT')
                load_op.filepath = sel_path
            else:
                preset_box_outer.label(text='(no presets found)', icon='INFO')

            preset_box_outer.operator('fbxmt.preset_save', text='Save Current...', icon='FILE_TICK')


# ─── Modal bake operator ─────────────────────────────────────────────────────


def _composite_apex_lines(img, prefs, checker_scale, size):
    """Draw two opposite half-lines at every checker square apex.

    One angle per apex, two half-lines: angle and angle+180.
    Excluded only where a corner marker actually occupies the apex
    (kx AND ky both at tile corners). Per-pixel invert of checker colour.
    Edge non-corner apexes wrap their outward half to the opposite side.
    """
    try:
        pixels = np.empty(size * size * 4, dtype=np.float32)
        img.pixels.foreach_get(pixels)
        pixels = pixels.reshape(size, size, 4)

        sq     = size / checker_scale
        half_l = sq * 0.5
        lw     = max(2.0, size / 128.0)

        corner = {0, checker_scale}
        total  = checker_scale * checker_scale  # canonical index space: interior apexes only

        import random as _random
        _seed   = getattr(prefs, 'apex_line_seed', 42) if prefs else 42
        _rng    = _random.Random(_seed)
        _angles = list(range(total))
        _rng.shuffle(_angles)

        px_x = np.arange(size, dtype=np.float32)
        px_y = np.arange(size, dtype=np.float32)
        gx, gy = np.meshgrid(px_x, px_y)

        def _draw_half(apex_x, apex_y, angle_deg, torus=False):
            """Draw one half-line. Interior apexes use torus arithmetic.
            Edge apexes use linear arithmetic — no wrapping."""
            angle_rad = np.radians(angle_deg)
            dx =  np.cos(angle_rad)
            dy =  np.sin(angle_rad)
            nx = -np.sin(angle_rad)
            ny =  np.cos(angle_rad)

            if torus:
                half_size = size * 0.5
                ox = ((gx - apex_x + half_size) % size) - half_size
                oy = ((gy - apex_y + half_size) % size) - half_size
            else:
                ox = gx - apex_x
                oy = gy - apex_y

            along = ox * dx + oy * dy
            perp  = ox * nx + oy * ny
            mask  = (along >= 0) & (along <= half_l) & (np.abs(perp) <= lw * 0.5)
            if not np.any(mask):
                return

            under    = pixels[mask, :3].copy()
            r, g, b  = under[:, 0], under[:, 1], under[:, 2]
            chroma   = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
            lum      = 0.299 * r + 0.587 * g + 0.114 * b
            inverted = 1.0 - under
            bw       = np.where(lum[:, np.newaxis] > 0.5,
                                np.zeros_like(under),
                                np.ones_like(under))
            pixels[mask, :3] = np.where(chroma[:, np.newaxis] > 0.05, inverted, bw)
            pixels[mask, 3]  = 1.0

        for ky in range(checker_scale + 1):
            for kx in range(checker_scale + 1):
                # Exclude only the 4 tile corners — where a marker actually sits
                if kx in corner and ky in corner:
                    continue

                ax  = kx * sq
                ay  = ky * sq
                # Use modular kx/ky for the angle index so shared edge apexes
                # produce the same angle regardless of which tile renders them.
                canonical_kx = kx % checker_scale if checker_scale > 0 else 0
                canonical_ky = ky % checker_scale if checker_scale > 0 else 0
                idx = canonical_ky * checker_scale + canonical_kx
                angle_deg = 45.0 + (_angles[idx] / total) * 360.0

                on_x_edge = kx in corner
                on_y_edge = ky in corner
                on_edge   = on_x_edge or on_y_edge

                if not on_edge:
                    # Interior apex — torus, both halves
                    _draw_half(ax, ay, angle_deg,       torus=True)
                    _draw_half(ax, ay, angle_deg + 180, torus=True)
                else:
                    # Edge apex — linear, inward half only
                    for a_deg in (angle_deg, angle_deg + 180.0):
                        a_rad = np.radians(a_deg)
                        cdx   = np.cos(a_rad)
                        cdy   = np.sin(a_rad)
                        if on_x_edge and ax == 0    and cdx <= 0: continue
                        if on_x_edge and ax >= size and cdx >= 0: continue
                        if on_y_edge and ay == 0    and cdy <= 0: continue
                        if on_y_edge and ay >= size and cdy >= 0: continue
                        _draw_half(ax, ay, a_deg, torus=False)

        img.pixels.foreach_set(pixels.ravel())
        img.update()

    except Exception as e:
        print(f'[FBXMT] Apex lines failed: {e}')


def _composite_corner_marks(img, prefs, size, checker_scale):
    """Reapply corner reticle marks on top of an already-composited image.
    Always drawn last so marks are on top of the checker.
    """
    try:

        if prefs:
            preset = getattr(prefs, 'corner_mark_preset', 2)
        else:
            preset = 2
        show_c = True  # circle always on

        sq_edge       = 1.0 / checker_scale
        raw_l         = preset * 0.5 * sq_edge
        snapped_l     = _snap_px(raw_l, size)
        BORDER_L_TILE = max(2.0 / size, snapped_l)
        CIRCLE_R      = BORDER_L_TILE * 0.5
        BORDER_W      = max(1.0 / size, _snap_px(1.0 / 64.0, size))

        # Tile-space grids
        ut = np.tile((np.arange(size) + 0.5) / size, (size, 1))
        vt = np.tile((np.arange(size) + 0.5) / size, (size, 1)).T

        # Checker-square-space grids (for circle only)
        u  = ut * checker_scale
        v  = vt * checker_scale
        fu = u - np.floor(u)
        fv = v - np.floor(v)

        u_edge = (ut < BORDER_W)       | (ut > 1.0 - BORDER_W)
        v_edge = (vt < BORDER_W)       | (vt > 1.0 - BORDER_W)
        u_arm  = (fv < BORDER_L_TILE)  | (fv > 1.0 - BORDER_L_TILE)
        v_arm  = (fu < BORDER_L_TILE)  | (fu > 1.0 - BORDER_L_TILE)
        cross  = (u_edge & u_arm) | (v_edge & v_arm)

        if show_c:
            cu   = np.minimum(fu, 1.0 - fu)
            cv   = np.minimum(fv, 1.0 - fv)
            dist = np.sqrt(cu**2 + cv**2)
            arc  = np.abs(dist - CIRCLE_R) < BORDER_W
            cross = cross | arc

        pixels = np.empty(size * size * 4, dtype=np.float32)
        img.pixels.foreach_get(pixels)
        pixels = pixels.reshape(size, size, 4)

        under   = pixels[cross, :3]
        r, g, b = under[:, 0], under[:, 1], under[:, 2]
        cmax    = np.maximum(np.maximum(r, g), b)
        cmin    = np.minimum(np.minimum(r, g), b)
        chroma  = cmax - cmin
        lum     = 0.299 * r + 0.587 * g + 0.114 * b

        inverted = 1.0 - under
        bw       = np.where(lum[:, np.newaxis] > 0.5,
                            np.zeros_like(under),
                            np.ones_like(under))
        mark_rgb = np.where(chroma[:, np.newaxis] > 0.05, inverted, bw)

        pixels[cross, :3] = mark_rgb
        pixels[cross, 3]  = 1.0
        img.pixels.foreach_set(pixels.ravel())
        img.update()
    except Exception as e:
        print(f'[FBXMT] Corner mark composite failed: {e}')


_TILE_SCENE_NAME  = '__fbxmt_tile_scene'
_TILE_OBJ_NAME   = '__fbxmt_tile_quad'
_TILE_CAM_NAME   = '__fbxmt_tile_cam'
_tile_scene_size  = None   # cached render size
_tile_scene_scale = None   # cached tile_scale


def _ensure_tile_scene(size, tile_scale=1.0):
    """Create (or reuse) the persistent flat-quad tile render scene.

    The quad UVs span 1/tile_scale units so that one UV unit = one texel tile
    in the material's mapping_checker node. This ensures the checker and reticle
    render at exactly the right scale regardless of geo_texel_density.

    Returns (scene, obj).
    """
    global _tile_scene_size, _tile_scene_scale
    scene = bpy.data.scenes.get(_TILE_SCENE_NAME)
    obj   = bpy.data.objects.get(_TILE_OBJ_NAME)
    if (scene and obj
            and _tile_scene_size  == size
            and _tile_scene_scale == tile_scale):
        return scene, obj

    # Tear down stale scene
    if scene:
        for o in list(scene.collection.objects):
            d = o.data
            scene.collection.objects.unlink(o)
            bpy.data.objects.remove(o, do_unlink=True)
            if d and d.users == 0:
                if isinstance(d, bpy.types.Mesh):   bpy.data.meshes.remove(d)
                if isinstance(d, bpy.types.Camera):  bpy.data.cameras.remove(d)
        bpy.data.scenes.remove(scene, do_unlink=True)

    scene = bpy.data.scenes.new(_TILE_SCENE_NAME)

    # ── Render settings ───────────────────────────────────────────────────────
    scene.render.engine = 'BLENDER_EEVEE'
    scene.render.resolution_x          = size
    scene.render.resolution_y          = size
    scene.render.resolution_percentage = 100
    scene.render.film_transparent       = False
    # Blender 5.0+ requires media_type before file_format on ImageFormatSettings
    try:
        scene.render.image_settings.media_type = 'IMAGE'
    except Exception:
        pass
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode  = 'RGB'
    # Full-resolution GPU tiles where supported
    try:
        scene.render.tile_x = size
        scene.render.tile_y = size
    except Exception:
        pass
    if hasattr(scene, 'eevee'):
        eevee = scene.eevee
        # 1 sample — flat procedural shaders need no more
        if hasattr(eevee, 'taa_render_samples'):   eevee.taa_render_samples   = 1
        if hasattr(eevee, 'taa_viewport_samples'): eevee.taa_viewport_samples = 1
        # Disable everything we don't need — use hasattr throughout since
        # many of these were removed or moved in Blender 5.0/5.1
        for attr, val in (
            ('use_bloom',              False),
            ('use_ssr',                False),
            ('use_motion_blur',        False),
            ('use_overscan',           False),
            ('use_volumetric_lights',  False),
            ('use_volumetric_shadows', False),
        ):
            if hasattr(eevee, attr):
                try:
                    setattr(eevee, attr, val)
                except Exception:
                    pass
    scene.view_settings.view_transform = 'Standard'
    try:
        scene.view_settings.look = 'None'
    except Exception:
        try:
            scene.view_settings.look = ''
        except Exception:
            pass
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma    = 1.0

    # ── Flat quad — UVs span 1/tile_scale to cover exactly one texel tile ────
    # mapping_checker in the node tree scales UV by checker_scale (squares/tile)
    # and mapping_tile scales by tile_scale (geo_texel_density/1024).
    # By making the quad's UV span 1/tile_scale units, one quad = one texel tile.
    uv_extent = 1.0 / tile_scale

    me = bpy.data.meshes.new(_TILE_OBJ_NAME)
    bm = bmesh.new()
    verts = [
        bm.verts.new((-0.5,  0.5, 0.0)),
        bm.verts.new(( 0.5,  0.5, 0.0)),
        bm.verts.new(( 0.5, -0.5, 0.0)),
        bm.verts.new((-0.5, -0.5, 0.0)),
    ]
    bm.verts.ensure_lookup_table()
    face = bm.faces.new(verts)
    uv_layer = bm.loops.layers.uv.new('UVMap')
    uvs = [
        (0.0,       uv_extent),
        (uv_extent, uv_extent),
        (uv_extent, 0.0),
        (0.0,       0.0),
    ]
    for loop, uv in zip(face.loops, uvs):
        loop[uv_layer].uv = uv
    bm.to_mesh(me)
    bm.free()
    me.uv_layers.active_index = 0

    obj = bpy.data.objects.new(_TILE_OBJ_NAME, me)
    scene.collection.objects.link(obj)

    # ── Orthographic camera — frames the quad exactly ─────────────────────────
    cam_data             = bpy.data.cameras.new(_TILE_CAM_NAME)
    cam_data.type        = 'ORTHO'
    cam_data.ortho_scale = 1.0
    cam_data.clip_start  = 0.01
    cam_data.clip_end    = 10.0
    cam_obj              = bpy.data.objects.new(_TILE_CAM_NAME, cam_data)
    cam_obj.location     = (0.0, 0.0, 1.0)
    cam_obj.rotation_euler = (0.0, 0.0, 0.0)
    scene.collection.objects.link(cam_obj)
    scene.camera = cam_obj
    scene.world  = None

    _tile_scene_size  = size
    _tile_scene_scale = tile_scale

    return scene, obj


def _render_tile(mat_name, context, size=None, split=False, no_apex_lines=False):
    """Render one preview tile by swapping the material on the persistent quad
    and running EEVEE. Returns a packed bpy.data.Image or None on failure.

    This is the single source of truth for tile rendering — replaces the old
    numpy analytical renderer. Output is guaranteed to match the viewport
    because it uses the same EEVEE + node tree path.
    """
    try:
        mat = bpy.data.materials.get(mat_name)
        if not mat:
            return None

        prefs = context.scene.fbxmt_prefs_global
        render_size = size if size is not None else PREVIEW_SIZE

        # Always use tile_scale=1.0 for the preview quad — the quad spans [0,1] UV
        # which is exactly one tile. The node tree's mapping_tile handles geo density
        # internally for corner mark placement. Passing geo_texel_density here caused
        # the quad to show only 1/tile_scale of a tile at higher densities.
        scene, obj = _ensure_tile_scene(render_size, tile_scale=1.0)

        # Assign material to quad
        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)

        # Render — provide full window context for EEVEE
        tmp_path = os.path.join(tempfile.gettempdir(), f'__fbxmt_tile_{mat_name}.png')
        scene.render.filepath = tmp_path
        win = context.window if hasattr(context, 'window') and context.window else bpy.context.window
        with bpy.context.temp_override(window=win, scene=scene):
            bpy.ops.render.render(write_still=True, scene=scene.name)

        if not os.path.exists(tmp_path):
            print(f'[FBXMT] Tile render produced no output for {mat_name}')
            return None

        # Load into bpy.data.images — keep source file until after pack
        img_name = f'__tile_{mat_name}'
        existing = bpy.data.images.get(img_name)
        if existing:
            bpy.data.images.remove(existing)
        img = bpy.data.images.load(tmp_path)
        img.name = img_name
        img.update()

        # Apex position lines — drawn LAST so they're always on top
        if not no_apex_lines:
            _composite_apex_lines(img, prefs, checker_scale, render_size)

        # Pack into .blend memory, then delete temp file
        img.pack()
        try:
            os.remove(tmp_path)
        except Exception:
            pass

        # Force Blender's preview cache to regenerate — without this, template_icon
        # shows the stale thumbnail even after the image data has been replaced.
        try:
            img.preview.reload()
        except Exception:
            pass

        return img

    except Exception as e:
        import traceback
        print(f'[FBXMT] Tile render failed for {mat_name}: {e}')
        traceback.print_exc()
        return None


def _build_colour_swatches(prefs):
    """Build tiny 32x32 solid-colour images for the W/F/C anchor swatches.
    Builds both A and B swatches for each of Wall, Floor, Ceiling.
    Called after Apply so the images reflect the latest derived colours.
    Uses the same preview.icon_id path as the tile grid.
    """
    from .materials import _resolve_color_b

    b_offset = getattr(prefs, 'color_b_hue_offset',  '0')
    b_sat    = _safe_float(prefs, 'color_b_saturation', 0.6)
    b_val    = _safe_float(prefs, 'color_b_value', 0.35)

    entries = (
        ('__fbxmt_swatch_wall',         'color_wall_a'),
        ('__fbxmt_swatch_floor',        'color_floor_a'),
        ('__fbxmt_swatch_ceiling',      'color_ceiling_a'),
        ('__fbxmt_swatch_ramp_floor',   'color_ramp_floor_a'),
        ('__fbxmt_swatch_ramp_ceiling', 'color_ramp_ceiling_a'),
    )

    # Transparent spacer swatches for alignment
    for spacer_name in ('__fbxmt_swatch_spacer_a', '__fbxmt_swatch_spacer_b'):
        img = bpy.data.images.get(spacer_name)
        if img is None:
            img = bpy.data.images.new(spacer_name, width=32, height=32, alpha=True)
        px = np.zeros((32, 32, 4), dtype=np.float32)
        img.pixels.foreach_set(px.ravel())
        try:
            img.preview.reload()
        except Exception:
            pass

    # Island swatch — wall hue, island sat/val, island B params
    import colorsys as _cs
    _wall_a_raw = tuple(getattr(prefs, 'color_wall_a', (0.5, 0.5, 0.5, 1.0))[:3])
    _h_isl, _l_isl, _s_isl = _cs.rgb_to_hls(*_wall_a_raw)
    _isl_sat = _safe_float(prefs, 'island_marker_saturation', 0.6)
    _isl_val = _safe_float(prefs, 'island_marker_value', 0.50)
    _col_a_island = _cs.hls_to_rgb(_h_isl, _isl_val, _isl_sat)
    _isl_b_off = getattr(prefs, 'island_marker_b_hue_offset', '0')
    _isl_b_sat = _safe_float(prefs, 'island_marker_b_saturation', 0.6)
    _isl_b_val = _safe_float(prefs, 'island_marker_b_value', 0.35)
    _col_b_island = _resolve_color_b(_col_a_island, _isl_b_off, _isl_b_sat, _isl_b_val)

    for img_name, rgb, glyph in (
        ('__fbxmt_swatch_island',   _col_a_island, 'A'),
        ('__fbxmt_swatch_b_island', _col_b_island, 'B'),
    ):
        r, g, b = rgb[0], rgb[1], rgb[2]
        img = bpy.data.images.get(img_name)
        if img and (img.size[0] != 32 or img.size[1] != 32):
            bpy.data.images.remove(img)
            img = None
        if img is None:
            img = bpy.data.images.new(img_name, width=32, height=32, alpha=False)
        rl = min(1.0, max(0.0, r)) ** (1/2.2)
        gl = min(1.0, max(0.0, g)) ** (1/2.2)
        bl_v = min(1.0, max(0.0, b)) ** (1/2.2)
        px = np.full((32, 32, 4), 1.0, dtype=np.float32)
        px[:, :, 0] = rl
        px[:, :, 1] = gl
        px[:, :, 2] = bl_v
        # Stamp glyph
        glyph_col = (1.0 - rl, 1.0 - gl, 1.0 - bl_v)
        glyph_rows = _FONT_5X7.get(glyph, _FONT_5X7[' '])
        ox, oy = 2, 32 - 2 - 7
        for row_idx, row_bits in enumerate(glyph_rows):
            py = oy + (6 - row_idx)
            for col_idx, bit in enumerate(row_bits):
                if bit == '1':
                    px_x = ox + col_idx
                    if 0 <= py < 32 and 0 <= px_x < 32:
                        px[py, px_x, :3] = glyph_col
        img.pixels.foreach_set(px.ravel())
        img.update()
        img.preview_ensure()
        try:
            img.preview.reload()
        except Exception:
            pass

    for a_img_name, prop_a in entries:
        b_img_name = a_img_name.replace('_swatch_', '_swatch_b_')

        col_a = tuple(getattr(prefs, prop_a, (0.5, 0.5, 0.5, 1.0)))
        col_b = _resolve_color_b(col_a[:3], b_offset, b_sat, b_val)

        for img_name, rgb, glyph in (
            (a_img_name, col_a[:3], 'A'),
            (b_img_name, col_b,     'B'),
        ):
            r, g, b = rgb[0], rgb[1], rgb[2]
            img = bpy.data.images.get(img_name)
            if img and (img.size[0] != 32 or img.size[1] != 32):
                bpy.data.images.remove(img)
                img = None
            if img is None:
                img = bpy.data.images.new(img_name, width=32, height=32, alpha=False)

            # Fill solid colour — encode linear→sRGB to match COLOR_GAMMA prop display
            rl = min(1.0, max(0.0, r)) ** (1/2.2)
            gl = min(1.0, max(0.0, g)) ** (1/2.2)
            bl = min(1.0, max(0.0, b)) ** (1/2.2)
            px = np.full((32, 32, 4), 1.0, dtype=np.float32)
            px[:, :, 0] = rl
            px[:, :, 1] = gl
            px[:, :, 2] = bl

            # Stamp glyph
            glyph_col = (1.0 - rl, 1.0 - gl, 1.0 - bl)
            glyph_rows = _FONT_5X7.get(glyph, _FONT_5X7[' '])
            ox, oy = 2, 32 - 2 - 7
            for row_idx, row_bits in enumerate(glyph_rows):
                py = oy + (6 - row_idx)
                for col_idx, bit in enumerate(row_bits):
                    if bit == '1':
                        px_x = ox + col_idx
                        if 0 <= py < 32 and 0 <= px_x < 32:
                            px[py, px_x, :3] = glyph_col

            img.pixels.foreach_set(px.ravel())
            img.update()
            img.preview_ensure()
            try:
                img.preview.reload()
            except Exception:
                pass


def _build_preview_materials(prefs, geo_texel_density=None):
    """Build temporary copies of all display materials with current prefs applied.
    Returns a dict {real_name: temp_name}. Caller must call _cleanup_preview_materials().

    prefs: FBXMT_GlobalPrefs from the operator context — read directly, never
    via _get_prefs() which uses bpy.context.scene and can return the wrong scene.
    geo_texel_density: pass from operator context for the same reason.
    """
    from .materials import (
        _build_checker_node_tree, _resolve_color_b,
        ISLAND_SUB_NAMES, ISLAND_MARKER_NAME,
    )
    import colorsys

    if prefs is None:
        return {}

    # Read global B derivation settings directly from prefs
    b_offset = getattr(prefs, 'color_b_hue_offset',  '0')
    b_sat    = _safe_float(prefs, 'color_b_saturation', 0.6)
    b_val    = _safe_float(prefs, 'color_b_value', 0.35)

    def _slot_colours(slot):
        """Return (col_a, col_b, pattern) for a slot, reading directly from prefs."""
        col_a   = tuple(getattr(prefs, f'color_{slot}_a', (0.5,)*4)[:3])
        pattern = getattr(prefs, f'checker_pattern_{slot}', 'SQUARE')
        col_b   = _resolve_color_b(col_a, b_offset, b_sat, b_val)
        return col_a, col_b, pattern

    _SLOT_TO_MAT = {
        'floor':        'M_FBXMT_Floor',
        'ceiling':      'M_FBXMT_Ceiling',
        'wall':         'M_FBXMT_Wall',
        'trim':         'M_FBXMT_Trim',
        'ignore':       'M_FBXMT_Ignore',
        'ramp_floor':   'M_FBXMT_Ramp_Floor',
        'ramp_ceiling': 'M_FBXMT_Ramp_Ceiling',
    }
    temp_map = {}

    for slot, mat_name in _SLOT_TO_MAT.items():
        if slot == 'ignore':
            col_a   = (0.25, 0.25, 0.25)
            col_b   = (0.10, 0.10, 0.10)
            pattern = getattr(prefs, 'checker_pattern_ignore', 'SQUARE')
        else:
            col_a, col_b, pattern = _slot_colours(slot)
        tmp_name = f'__fbxmt_preview_{mat_name}'
        tmp = bpy.data.materials.get(tmp_name) or bpy.data.materials.new(tmp_name)
        tmp.use_nodes = True
        _build_checker_node_tree(tmp, col_a, col_b, pattern=pattern, geo_texel_density=geo_texel_density)
        temp_map[mat_name] = tmp_name

    # Island marker — hue from wall, sat/val from island marker props
    col_a_wall, _, _ = _slot_colours('wall')
    pattern_island   = getattr(prefs, 'checker_pattern_island', 'CIRCLE')
    h_isl, _, _ = colorsys.rgb_to_hls(*col_a_wall)
    isl_sat = _safe_float(prefs, 'island_marker_saturation', 0.6)
    isl_val = _safe_float(prefs, 'island_marker_value', 0.50)
    col_a_island  = colorsys.hls_to_rgb(h_isl, isl_val, isl_sat)
    isl_b_offset  = getattr(prefs, 'island_marker_b_hue_offset', '0')
    isl_b_sat     = _safe_float(prefs, 'island_marker_b_saturation', 0.6)
    isl_b_val     = _safe_float(prefs, 'island_marker_b_value', 0.35)
    col_b_island  = _resolve_color_b(col_a_island, isl_b_offset, isl_b_sat, isl_b_val)
    tmp_name = f'__fbxmt_preview_{ISLAND_MARKER_NAME}'
    tmp = bpy.data.materials.get(tmp_name) or bpy.data.materials.new(tmp_name)
    tmp.use_nodes = True
    _build_checker_node_tree(tmp, col_a_island, col_b_island, pattern=pattern_island, geo_texel_density=geo_texel_density)
    temp_map[ISLAND_MARKER_NAME] = tmp_name

    # Island sub-materials — A = parent B hue with island sat/val, B = island b modifiers
    def _get_preview_parent_b(slot):
        col_a = tuple(getattr(prefs, f'color_{slot}_a', (0.5,)*4)[:3])
        return _resolve_color_b(col_a, b_offset, b_sat, b_val)

    _preview_parent_b = [
        _get_preview_parent_b('wall'),    # group 0: Wall_xx  (i%3==0)
        _get_preview_parent_b('floor'),   # group 1: Floor_xx (i%3==1)
        _get_preview_parent_b('ceiling'), # group 2: Ceil_xx  (i%3==2)
    ]
    for i, name in enumerate(ISLAND_SUB_NAMES):
        group    = i % 3
        parent_b = _preview_parent_b[group]
        h, l, s  = colorsys.rgb_to_hls(*parent_b)
        island_a = colorsys.hls_to_rgb(h, isl_val, isl_sat)
        island_b = _resolve_color_b(island_a, isl_b_offset, isl_b_sat, isl_b_val)
        tmp_name = f'__fbxmt_preview_{name}'
        tmp = bpy.data.materials.get(tmp_name) or bpy.data.materials.new(tmp_name)
        tmp.use_nodes = True
        _build_checker_node_tree(tmp, island_a, island_b, pattern=pattern_island, checker_invert=True, geo_texel_density=geo_texel_density)
        temp_map[name] = tmp_name

    # Ramp island preview materials
    from .materials import RAMP_ISLAND_NAMES as _RIN
    ramp_a_raw    = tuple(getattr(prefs, 'color_ramp_floor_a', (0.6, 0.7, 0.25, 1.0))[:3])
    h_r, l_r, s_r = colorsys.rgb_to_hls(*ramp_a_raw)
    ramp_island_a = colorsys.hls_to_rgb(h_r, isl_val, isl_sat)
    ramp_island_b = _resolve_color_b(ramp_island_a, isl_b_offset, isl_b_sat, isl_b_val)
    for name in _RIN:
        tmp_name = f'__fbxmt_preview_{name}'
        tmp = bpy.data.materials.get(tmp_name) or bpy.data.materials.new(tmp_name)
        tmp.use_nodes = True
        _build_checker_node_tree(tmp, ramp_island_a, ramp_island_b, pattern=pattern_island, checker_invert=True, geo_texel_density=geo_texel_density)
        temp_map[name] = tmp_name

    return temp_map


def _cleanup_preview_materials():
    """Remove all temporary preview materials."""
    to_remove = [m for m in bpy.data.materials if m.name.startswith('__fbxmt_preview_')]
    for m in to_remove:
        bpy.data.materials.remove(m)


def _render_dialog_tiles_sync(context):
    """Synchronously render all 6 preview tiles for the Project Setup dialog.

    Always renders at PREVIEW_SIZE with geo_texel_density=1024 — the preview
    shows the material pattern, not world-space scale. Texel density is irrelevant
    to how the tile looks.
    """
    scene = context.scene
    prefs = scene.fbxmt_prefs_global
    if prefs is None:
        return

    # Always preview at 1024tx/m — density is a world-space concept, not a material one
    temp_map = _build_preview_materials(prefs, geo_texel_density=1024)
    _build_colour_swatches(prefs)

    try:
        render_list = list(ALL_DISPLAY_MATERIAL_NAMES)
        for mat_name in render_list:
            render_name = temp_map.get(mat_name, mat_name)
            img = _render_tile(render_name, context, split=True, no_apex_lines=True)
            if img:
                existing = bpy.data.images.get(f'__tile_{mat_name}')
                if existing:
                    bpy.data.images.remove(existing)
                img.name = f'__tile_{mat_name}'
                try:
                    img.preview.reload()
                except Exception:
                    pass
    finally:
        _cleanup_preview_materials()

    # Force full window redraw so dialog picks up new thumbnails
    if context.window:
        for area in context.window.screen.areas:
            area.tag_redraw()


class FBXMT_OT_BakeAllModal(Operator):
    """Modal operator that bakes one material per timer tick so Blender can
    redraw the header progress text between each bake. Invoked by the dialog
    execute, Update Tile, Rebuild, and template load."""
    bl_idname  = 'fbxmt.bake_all_modal'
    bl_label   = 'FBXMT Build Preview Tiles'
    bl_options = {'INTERNAL'}

    skip_rebuild: bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        from .materials import rebuild_fbxmt_materials, ensure_fbxmt_materials

        scene = context.scene
        prefs = scene.fbxmt_prefs_global
        props = scene.fbxmt_props

        ensure_fbxmt_materials()

        self._mat_queue = list(ALL_DISPLAY_MATERIAL_NAMES) + ['M_FBXMT_Ramp_Floor', 'M_FBXMT_Ramp_Ceiling']
        self._total     = len(self._mat_queue)
        self._done      = 0
        self._temp_map  = {}

        if self.skip_rebuild:
            # Build temp copies of materials with current prefs — viewport untouched.
            geo_td = props.geo_texel_density if props else 1024
            try:
                self._temp_map = _build_preview_materials(prefs, geo_texel_density=geo_td)
            except Exception as e:
                import traceback
                print(f'[FBXMT] ERROR in _build_preview_materials: {e}')
                traceback.print_exc()
                self._temp_map = {}
        else:
            rebuild_fbxmt_materials()

        context.workspace.status_text_set('FBXMT  |  Building preview tiles...')

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'RUNNING_MODAL'}

        if not self._mat_queue:
            return self._finish(context)

        mat_name     = self._mat_queue.pop(0)
        display_name = _MAT_DISPLAY_NAMES.get(mat_name, mat_name)
        self._done  += 1

        context.workspace.status_text_set(
            f'FBXMT  |  Building tile: {display_name}  ({self._done}/{self._total})'
        )

        # Use temp preview material name if available, fall back to real name
        render_name = self._temp_map.get(mat_name, mat_name)
        using_temp  = render_name != mat_name
        mat = bpy.data.materials.get(render_name)
        if mat:
            img = _render_tile(render_name, context, split=True, no_apex_lines=True)
            if img:
                # Rename image to match real material name for display
                img.name = f'__tile_{mat_name}'
                img.preview_ensure()
                try:
                    img.preview.reload()
                except Exception:
                    pass
                # Tag all areas for redraw — dialog is a separate region from context.area
                for window in context.window_manager.windows:
                    for area in window.screen.areas:
                        area.tag_redraw()
                        for region in area.regions:
                            region.tag_redraw()
        else:
            print(f'[FBXMT] material not found: {render_name}')

        return {'RUNNING_MODAL'}

    def _finish(self, context):
        scene  = context.scene
        props  = scene.fbxmt_props

        context.window_manager.event_timer_remove(self._timer)
        context.workspace.status_text_set(None)

        # Clean up temp preview materials if they were used
        if getattr(self, '_temp_map', {}):
            _cleanup_preview_materials()
            self._temp_map = {}

        if props:
            props.fbxmt_cache_hash = _compute_cache_hash(scene)

        # Switch preview to sheet view and force redraw of all areas including dialog
        context.scene.fbxmt_preview_mode = 'SHEET'
        try:
            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()
                    for region in area.regions:
                        region.tag_redraw()
        except Exception:
            pass

        self.report({'INFO'}, f'FBXMT preview tiles built — {self._total} materials')
        return {'FINISHED'}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)
        context.workspace.status_text_set(None)
        if getattr(self, '_temp_map', {}):
            _cleanup_preview_materials()
            self._temp_map = {}


class FBXMT_OT_SelectPreset(Operator):
    """Select a preset by index for the dropdown picker."""
    bl_idname  = 'fbxmt.select_preset'
    bl_label   = 'Select Preset'
    bl_options = {'INTERNAL'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        context.scene.fbxmt_selected_preset_index = self.index
        return {'FINISHED'}


class FBXMT_MT_PresetPicker(bpy.types.Menu):
    bl_idname = 'FBXMT_MT_PresetPicker'
    bl_label  = 'Select Preset'

    def draw(self, context):
        layout  = self.layout
        presets = _list_presets(context)
        if not presets:
            layout.label(text='No presets found', icon='INFO')
            return
        for i, (name, _filepath) in enumerate(presets):
            op = layout.operator('fbxmt.select_preset', text=name)
            op.index = i


# ─── Registration ─────────────────────────────────────────────────────────────

class FBXMT_OT_TogglePresetLock(Operator):
    """Lock or unlock all material controls.
    Locked: anchor hue, colour B mode, patterns, scale and seed controls are all disabled.
    Set automatically on Full preset load. Toggle at any time to protect or free settings."""
    bl_idname  = 'fbxmt.toggle_preset_lock'
    bl_label   = 'Toggle Settings Lock'
    bl_options = {'INTERNAL', 'UNDO'}

    def execute(self, context):
        prefs = context.scene.fbxmt_prefs_global
        prefs.preset_locked = not prefs.preset_locked
        if not prefs.preset_locked:
            prefs.active_preset_name = ''
        return {'FINISHED'}


class FBXMT_OT_ContactSheet_Benchmark(Operator):
    """Benchmark contact sheet tile rendering at all standard texel densities.
    Runs both RAM and Disk paths at native tile resolution (density px = tile px).
    Densities: 512, 1024, 2048, 4096 tx/m.
    4096 RAM path skipped unless system has >= 64 GB RAM.
    Results written as CSV to the presets folder (prompts to set one if not configured).
    """
    bl_idname  = 'fbxmt.contact_sheet_benchmark'
    bl_label   = 'Render Benchmark'
    bl_options = {'REGISTER'}

    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    _DENSITIES = (512, 1024, 2048, 4096, 8192)

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def invoke(self, context, event):
        d = _get_presets_dir(context)
        if not d:
            self.report({'WARNING'}, 'No presets folder set — pick one now')
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        return self.execute(context)

    def execute(self, context):
        import csv
        import time as _time

        if self.directory:
            context.scene.fbxmt_prefs_global.presets_path = self.directory

        out_dir = _get_presets_dir(context)
        if not out_dir:
            self.report({'ERROR'}, 'No presets folder available — cannot write CSV')
            return {'CANCELLED'}
        os.makedirs(out_dir, exist_ok=True)

        prefs   = _get_prefs()

        from .materials import ALL_ISLAND_SUB_NAMES as _AISN
        base_mat_names = list(BAKE_MATERIAL_NAMES)
        full_mat_names = base_mat_names + list(_AISN)

        tile_sets = (
            ('Base', base_mat_names),
            ('Full', full_mat_names),
        )

        try:
            ram_gb = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 1024 ** 3
        except Exception:
            ram_gb = 0.0
        ram_ok_for_4096 = ram_gb >= 64.0

        csv_path = os.path.join(out_dir, 'FBXMT_Benchmark.csv')

        print('\n[FBXMT] Contact Sheet Benchmark')
        print(f'  System RAM:  {ram_gb:.1f} GB')
        print(f'  4096 RAM:    {"ENABLED" if ram_ok_for_4096 else "SKIPPED (< 64 GB)"}')
        print(f'  Base tiles:  {len(base_mat_names)}')
        print(f'  Full tiles:  {len(full_mat_names)}')
        print(f'  Output:      {csv_path}\n')
        print(f'  {"Set":>5}  {"Density":>8}  {"Path":>5}  {"Tile px":>7}  {"Tiles":>5}  {"Total (s)":>10}  {"Per tile (s)":>12}  {"Est GB f32":>10}')
        print('  ' + '-' * 80)

        rows        = []
        grand_start = _time.perf_counter()

        for set_label, mat_names in tile_sets:
            n_mats = len(mat_names)
            print(f'\n  -- {set_label} ({n_mats} tiles) --')

            for density in self._DENSITIES:
                tile_px  = density
                px_total = tile_px * tile_px * 4 * n_mats

                temp_map = _build_preview_materials(prefs, geo_texel_density=density)

                for path_label, use_ram in (('RAM', True), ('Disk', False)):
                    if density == 4096 and use_ram and not ram_ok_for_4096:
                        print(f'  {set_label:>5}  {density:>8}  {path_label:>5}  {tile_px:>7}  {n_mats:>5}  {"SKIPPED (< 64 GB RAM)":>10}')
                        rows.append({'set': set_label, 'density_tx_m': density, 'path': path_label,
                                     'tile_px': tile_px, 'n_tiles': n_mats, 'total_s': 'SKIPPED',
                                     'per_tile_s': 'SKIPPED', 'est_gb_f32': f'{px_total * 16 / 1024**3:.2f}',
                                     'note': 'RAM < 64 GB'})
                        continue

                    if density == 8192 and use_ram:
                        print(f'  {set_label:>5}  {density:>8}  {path_label:>5}  {tile_px:>7}  {n_mats:>5}  {"SKIPPED (Disk only)":>10}')
                        rows.append({'set': set_label, 'density_tx_m': density, 'path': path_label,
                                     'tile_px': tile_px, 'n_tiles': n_mats, 'total_s': 'SKIPPED',
                                     'per_tile_s': 'SKIPPED', 'est_gb_f32': f'{px_total * 16 / 1024**3:.2f}',
                                     'note': 'Disk only at 8192'})
                        continue

                    t_start = _time.perf_counter()

                    if use_ram:
                        for mat_name in mat_names:
                            tmp_name = temp_map.get(mat_name)
                            if tmp_name:
                                _render_tile(tmp_name, context, size=tile_px, no_apex_lines=True)
                    else:
                        import tempfile, shutil
                        tmp_dir = tempfile.mkdtemp(prefix='fbxmt_bench_')
                        try:
                            for mat_name in mat_names:
                                tmp_name = temp_map.get(mat_name)
                                if not tmp_name:
                                    continue
                                img = _render_tile(tmp_name, context, size=tile_px, no_apex_lines=True)
                                if img:
                                    out_f = os.path.join(tmp_dir, mat_name + '.png')
                                    img.filepath_raw = out_f
                                    img.file_format  = 'PNG'
                                    img.save()
                                    bpy.data.images.remove(img)
                        finally:
                            shutil.rmtree(tmp_dir, ignore_errors=True)

                    t_total  = _time.perf_counter() - t_start
                    per_tile = t_total / n_mats if n_mats else 0.0
                    est_gb   = px_total * 16 / 1024 ** 3

                    print(f'  {set_label:>5}  {density:>8}  {path_label:>5}  {tile_px:>7}  {n_mats:>5}  {t_total:>10.3f}s  {per_tile:>12.3f}s  {est_gb:>10.2f}')
                    rows.append({'set': set_label, 'density_tx_m': density, 'path': path_label,
                                 'tile_px': tile_px, 'n_tiles': n_mats, 'total_s': f'{t_total:.3f}',
                                 'per_tile_s': f'{per_tile:.3f}', 'est_gb_f32': f'{est_gb:.2f}', 'note': ''})

                _cleanup_preview_materials()

        grand_total = _time.perf_counter() - grand_start
        print(f'\n  Grand total: {grand_total:.1f}s')
        print(f'  CSV: {csv_path}\n')

        fieldnames = ['set', 'density_tx_m', 'path', 'tile_px', 'n_tiles', 'total_s', 'per_tile_s', 'est_gb_f32', 'note']
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        self.report({'INFO'}, f'Benchmark complete — {grand_total:.1f}s total. CSV → {csv_path}')
        return {'FINISHED'}



CLASSES = (
    FBXMT_OT_BakeAllModal,
    FBXMT_OT_ProjectSetup_UpdateTile,
    FBXMT_OT_ProjectSetup_SetDensity,
    FBXMT_OT_ProjectSetup_SetCheckerScale,
    FBXMT_OT_ProjectSetup_TilingTest,
    FBXMT_OT_ProjectSetup_SetContactSheetSize,
    FBXMT_OT_ApplyAnchor,
    FBXMT_OT_ResetAnchor,
    FBXMT_OT_ProjectSetup_ContactSheet,
    OT_FBXMT_Preset_Save,
    FBXMT_OT_Preset_LoadDefault,
    OT_FBXMT_Preset_Load,
    OT_FBXMT_Preset_Delete,
    OT_FBXMT_SelectTile,
    OT_FBXMT_ApplyBToAll,
    FBXMT_OT_SelectPreset,
    FBXMT_MT_PresetPicker,
    FBXMT_OT_TogglePresetLock,
    FBXMT_OT_ProjectSetup,
    FBXMT_OT_ProjectSetup_ContactSheet_Disk,
    FBXMT_MT_ContactSheet_Dropdown,
    FBXMT_OT_ContactSheet_Benchmark,
)


def _on_mat_dropdown_update(scene, _context):
    """Sync dropdown selection to fbxmt_selected_mat_index and switch to tile view."""
    scene.fbxmt_props.fbxmt_selected_mat_index = int(scene.fbxmt_preview_mat_enum)
    scene.fbxmt_preview_mode = 'TILE'  # switching material always shows tile


def register():
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            try:
                bpy.utils.unregister_class(cls)
                bpy.utils.register_class(cls)
            except Exception:
                pass
    bpy.types.Scene.fbxmt_preview_mat_enum = bpy.props.EnumProperty(
        name   = "Material",
        items  = [(str(i), name, "") for i, name in enumerate(_MAT_DISPLAY_NAMES.values())],
        default = "0",
        update  = _on_mat_dropdown_update,
    )
    bpy.types.Scene.fbxmt_preview_mode = bpy.props.EnumProperty(
        name  = "Preview Mode",
        items = [("TILE", "Tile", ""), ("SHEET", "Contact Sheet", "")],
        default = "TILE",
    )
    bpy.types.Scene.fbxmt_selected_preset_index = bpy.props.IntProperty(
        name    = "Selected Preset",
        default = 0,
        min     = 0,
    )


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "fbxmt_preview_mat_enum"):
        del bpy.types.Scene.fbxmt_preview_mat_enum
    if hasattr(bpy.types.Scene, "fbxmt_preview_mode"):
        del bpy.types.Scene.fbxmt_preview_mode
    if hasattr(bpy.types.Scene, "fbxmt_selected_preset_index"):
        del bpy.types.Scene.fbxmt_selected_preset_index
