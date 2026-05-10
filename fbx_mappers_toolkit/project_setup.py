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
#   - No PNG files shipped — all preview geometry is hardcoded procedural bmesh
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

from .materials import _get_prefs, ensure_fbxmt_materials, rebuild_fbxmt_materials
from .panel import ADDON_ID
from .uv_unwrap import unwrap_mesh


# ─── Constants ────────────────────────────────────────────────────────────────

# Surface materials that get the split tile (top=parent, bottom=island steps) in dialog preview
_SPLIT_TILE_MATS = {'M_FBXMT_Floor', 'M_FBXMT_Ceiling', 'M_FBXMT_Wall'}

# 6 visible materials in display order
ALL_DISPLAY_MATERIAL_NAMES = [
    'M_FBXMT_Floor',
    'M_FBXMT_Ceiling',
    'M_FBXMT_Wall',
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
_MAT_COLOR_PROPS = [
    ('color_floor_a',   'color_floor_b'),
    ('color_ceiling_a', 'color_ceiling_b'),
    ('color_wall_a',    'color_wall_b'),
    ('color_trim_a',    'color_trim_b'),
    ('color_ignore_a',  'color_ignore_b'),
    ('color_island_a',  'color_island_b'),
]

_MAT_DISPLAY_NAMES = {
    'M_FBXMT_Floor':   'Floor',
    'M_FBXMT_Ceiling': 'Ceiling',
    'M_FBXMT_Wall':    'Wall',
    'M_FBXMT_Trim':    'Trim',
    'M_FBXMT_Ignore':  'Ignore',
    'M_FBXMT_Island':  'Island Marker',
}


# ─── Preview mesh data ────────────────────────────────────────────────────────
# Hardcoded geometry for the two preview models.
# FBXMT_Preview_Geo_Trim: architectural piece showing Floor/Ceiling/Wall/Trim/Ignore
# FBXMT_Preview_Island_Chains: Q3DM6-inspired stacked curves showing all 5 chains
# Exported from Blender, processed through the toolkit's own unwrap pipeline.

_PREVIEW_GEO_TRIM_VERTS = [
    (2.0000, -0.3446, -0.0682),
    (2.0000, 0.6554, -0.5682),
    (2.0000, 0.6554, -0.4182),
    (2.0000, -0.2446, 0.0318),
    (-2.0000, -0.3446, -0.0682),
    (-2.0000, 0.6554, -0.5682),
    (-2.0000, 0.6554, -0.4182),
    (-2.0000, -0.2446, 0.0318),
    (-1.0000, -0.3446, 0.5318),
    (1.0000, -0.3446, 0.5318),
    (1.0000, -0.2446, 0.5318),
    (-1.0000, -0.2446, 0.5318),
    (0.0696, -0.3446, 0.9039),
    (-0.0696, -0.3446, 0.9039),
    (0.0492, -0.3446, 0.9102),
    (0.0255, -0.3446, 0.9141),
    (0.0000, -0.3446, 0.9154),
    (-0.0255, -0.3446, 0.9141),
    (-0.0492, -0.3446, 0.9102),
    (-0.0696, -0.2446, 0.9039),
    (0.0696, -0.2446, 0.9039),
    (-0.0492, -0.2446, 0.9102),
    (-0.0255, -0.2446, 0.9141),
    (0.0000, -0.2446, 0.9154),
    (0.0255, -0.2446, 0.9141),
    (0.0492, -0.2446, 0.9102),
    (2.0000, -0.3446, 0.8568),
    (1.9304, -0.3446, 0.9039),
    (1.9976, -0.3446, 0.8752),
    (1.9907, -0.3446, 0.8905),
    (1.9796, -0.3446, 0.9016),
    (1.9652, -0.3446, 0.9078),
    (1.9484, -0.3446, 0.9086),
    (1.9304, -0.2446, 0.9039),
    (2.0000, -0.2446, 0.8568),
    (1.9484, -0.2446, 0.9086),
    (1.9652, -0.2446, 0.9078),
    (1.9796, -0.2446, 0.9016),
    (1.9907, -0.2446, 0.8905),
    (1.9976, -0.2446, 0.8752),
    (-2.0000, -0.2446, 0.8568),
    (-1.9304, -0.2446, 0.9039),
    (-1.9976, -0.2446, 0.8752),
    (-1.9907, -0.2446, 0.8905),
    (-1.9796, -0.2446, 0.9016),
    (-1.9652, -0.2446, 0.9078),
    (-1.9484, -0.2446, 0.9086),
    (-1.9304, -0.3446, 0.9039),
    (-2.0000, -0.3446, 0.8568),
    (-1.9484, -0.3446, 0.9086),
    (-1.9652, -0.3446, 0.9078),
    (-1.9796, -0.3446, 0.9016),
    (-1.9907, -0.3446, 0.8905),
    (-1.9976, -0.3446, 0.8752),
    (-2.0000, -0.3508, -2.0500),
    (0.0000, -0.3508, -2.0500),
    (-2.0000, -0.3508, 3.9500),
    (0.0000, -0.3508, 3.9500),
    (-2.0000, -0.3508, 1.9500),
    (-2.0000, -0.3508, -0.0500),
    (0.0000, -0.3508, -0.0500),
    (0.0000, -0.3508, 1.9500),
    (-2.0000, 1.6492, -1.0500),
    (-2.0000, 1.6492, -2.0500),
    (0.0000, 1.6492, -2.0500),
    (0.0000, 1.6492, 2.9500),
    (0.0000, 1.6492, 3.9500),
    (-2.0000, 1.6492, 3.9500),
    (-2.0000, 1.6492, 2.9500),
    (0.0000, 1.6492, -1.0500),
    (-2.0000, -1.3508, -0.0500),
    (-2.0000, -1.3508, -2.0500),
    (0.0000, -1.3508, -2.0500),
    (0.0000, -1.3508, 1.9500),
    (0.0000, -1.3508, 3.9500),
    (-2.0000, -1.3508, 3.9500),
    (-2.0000, -1.3508, 1.9500),
    (0.0000, -1.3508, -0.0500),
    (2.0000, -0.3508, -0.0500),
    (2.0000, -0.3508, 1.9500),
    (2.0000, 1.6492, 2.9500),
    (2.0000, 1.6492, 3.9500),
    (2.0000, 1.6492, -2.0500),
    (2.0000, 1.6492, -1.0500),
    (2.0000, -0.3508, 3.9500),
    (2.0000, -0.3508, -2.0500),
    (2.0000, -1.3508, 1.9500),
    (2.0000, -1.3508, 3.9500),
    (2.0000, -1.3508, -2.0500),
    (2.0000, -1.3508, -0.0500),
]

_PREVIEW_GEO_TRIM_FACES = [
    ((1, 5, 6, 2), 'M_FBXMT_Trim'),
    ((2, 6, 7, 3), 'M_FBXMT_Trim'),
    ((0, 4, 5, 1), 'M_FBXMT_Trim'),
    ((19, 13, 18, 21), 'M_FBXMT_Trim'),
    ((21, 18, 17, 22), 'M_FBXMT_Trim'),
    ((22, 17, 16, 23), 'M_FBXMT_Trim'),
    ((23, 16, 15, 24), 'M_FBXMT_Trim'),
    ((24, 15, 14, 25), 'M_FBXMT_Trim'),
    ((25, 14, 12, 20), 'M_FBXMT_Trim'),
    ((19, 11, 8, 13), 'M_FBXMT_Trim'),
    ((10, 20, 12, 9), 'M_FBXMT_Trim'),
    ((47, 41, 46, 49), 'M_FBXMT_Trim'),
    ((49, 46, 45, 50), 'M_FBXMT_Trim'),
    ((50, 45, 44, 51), 'M_FBXMT_Trim'),
    ((51, 44, 43, 52), 'M_FBXMT_Trim'),
    ((52, 43, 42, 53), 'M_FBXMT_Trim'),
    ((53, 42, 40, 48), 'M_FBXMT_Trim'),
    ((26, 34, 39, 28), 'M_FBXMT_Trim'),
    ((28, 39, 38, 29), 'M_FBXMT_Trim'),
    ((29, 38, 37, 30), 'M_FBXMT_Trim'),
    ((30, 37, 36, 31), 'M_FBXMT_Trim'),
    ((31, 36, 35, 32), 'M_FBXMT_Trim'),
    ((32, 35, 33, 27), 'M_FBXMT_Trim'),
    ((26, 28, 29, 30, 31, 32, 27, 9, 12, 14, 15, 16, 17, 18, 13, 8, 47, 49, 50, 51, 52, 53, 48, 4, 0), 'M_FBXMT_Trim'),
    ((0, 1, 2, 3, 34, 26), 'M_FBXMT_Trim'),
    ((33, 10, 9, 27), 'M_FBXMT_Trim'),
    ((3, 7, 40, 42, 43, 44, 45, 46, 41, 11, 19, 21, 22, 23, 24, 25, 20, 10, 33, 35, 36, 37, 38, 39, 34), 'M_FBXMT_Trim'),
    ((4, 48, 40, 7, 6, 5), 'M_FBXMT_Trim'),
    ((11, 41, 47, 8), 'M_FBXMT_Trim'),
    ((68, 67, 66, 65), 'M_FBXMT_Wall'),
    ((63, 62, 69, 64), 'M_FBXMT_Wall'),
    ((58, 68, 65, 61), 'M_FBXMT_Ceiling'),
    ((54, 63, 64, 55, 72, 71), 'M_FBXMT_Ceiling'),
    ((58, 61, 60, 59), 'M_FBXMT_Wall'),
    ((60, 69, 62, 59), 'M_FBXMT_Floor'),
    ((59, 62, 63, 54, 71, 70, 76, 75, 56, 67, 68, 58), 'M_FBXMT_Wall'),
    ((57, 66, 67, 56, 75, 74), 'M_FBXMT_Floor'),
    ((77, 73, 74, 75, 76, 70, 71, 72), 'M_FBXMT_Wall'),
    ((66, 57, 84, 81), 'M_FBXMT_Floor'),
    ((79, 80, 81, 84, 87, 86, 89, 88, 85, 82, 83, 78), 'M_FBXMT_Ignore'),
    ((69, 60, 78, 83), 'M_FBXMT_Floor'),
    ((77, 72, 88, 89), 'M_FBXMT_Wall'),
    ((64, 69, 83, 82), 'M_FBXMT_Wall'),
    ((65, 66, 81, 80), 'M_FBXMT_Wall'),
    ((61, 65, 80, 79), 'M_FBXMT_Ceiling'),
    ((73, 77, 89, 86), 'M_FBXMT_Wall'),
    ((72, 55, 85, 88), 'M_FBXMT_Ceiling'),
    ((57, 74, 87, 84), 'M_FBXMT_Floor'),
    ((60, 61, 79, 78), 'M_FBXMT_Wall'),
    ((55, 64, 82, 85), 'M_FBXMT_Ceiling'),
    ((74, 73, 86, 87), 'M_FBXMT_Wall'),
]

_PREVIEW_ISLAND_CHAINS_VERTS = [
    (0.2758, -0.2867, 0.5357),
    (0.2758, -0.2867, 0.7500),
    (0.1661, -0.2759, 0.7500),
    (0.1661, -0.2759, 0.5357),
    (0.1441, -0.3862, 0.5357),
    (0.2758, -0.3992, 0.5357),
    (0.2758, 0.2758, 0.7500),
    (0.2758, -0.3992, 0.3214),
    (0.2758, -0.2867, 0.3214),
    (0.2758, -0.2867, 0.1071),
    (0.2758, -0.3992, 0.1071),
    (0.2758, -0.3992, -0.1071),
    (0.2758, -0.2867, -0.1071),
    (0.2758, -0.2867, -0.3214),
    (0.2758, -0.3992, -0.3214),
    (0.2758, -0.3992, -0.5357),
    (0.2758, -0.2867, -0.5357),
    (0.2758, -0.2867, -0.7500),
    (0.2758, 0.2758, -0.7500),
    (-0.2867, 0.2758, 0.7500),
    (-0.2759, 0.1661, 0.7500),
    (-0.2439, 0.0605, 0.7500),
    (-0.1919, -0.0367, 0.7500),
    (-0.1219, -0.1219, 0.7500),
    (-0.0367, -0.1919, 0.7500),
    (0.0606, -0.2439, 0.7500),
    (0.0606, -0.2439, 0.5357),
    (0.0175, -0.3478, 0.5357),
    (0.1441, -0.3862, 0.3214),
    (0.1661, -0.2759, 0.3214),
    (0.1661, -0.2759, 0.1071),
    (0.1441, -0.3862, 0.1071),
    (0.1441, -0.3862, -0.1071),
    (0.1661, -0.2759, -0.1071),
    (0.1661, -0.2759, -0.3214),
    (0.1441, -0.3862, -0.3214),
    (0.1441, -0.3862, -0.5357),
    (0.1661, -0.2759, -0.5357),
    (0.1661, -0.2759, -0.7500),
    (0.0606, -0.2439, -0.7500),
    (-0.0367, -0.1919, -0.7500),
    (-0.1219, -0.1219, -0.7500),
    (-0.1919, -0.0367, -0.7500),
    (-0.2439, 0.0605, -0.7500),
    (-0.2759, 0.1661, -0.7500),
    (-0.2867, 0.2758, -0.7500),
    (-0.2759, 0.1661, 0.5357),
    (-0.2867, 0.2758, 0.5357),
    (-0.2439, 0.0605, 0.5357),
    (-0.1919, -0.0367, 0.5357),
    (-0.1219, -0.1219, 0.5357),
    (-0.0367, -0.1919, 0.5357),
    (-0.0992, -0.2854, 0.5357),
    (0.0175, -0.3478, 0.3214),
    (0.0606, -0.2439, 0.3214),
    (0.0606, -0.2439, 0.1071),
    (0.0175, -0.3478, 0.1071),
    (0.0175, -0.3478, -0.1071),
    (0.0606, -0.2439, -0.1071),
    (0.0606, -0.2439, -0.3214),
    (0.0175, -0.3478, -0.3214),
    (0.0175, -0.3478, -0.5357),
    (0.0606, -0.2439, -0.5357),
    (-0.0367, -0.1919, -0.5357),
    (-0.1219, -0.1219, -0.5357),
    (-0.1919, -0.0367, -0.5357),
    (-0.2439, 0.0605, -0.5357),
    (-0.2759, 0.1661, -0.5357),
    (-0.2867, 0.2758, -0.5357),
    (-0.3992, 0.2758, 0.5357),
    (-0.3862, 0.1441, 0.5357),
    (-0.3478, 0.0175, 0.5357),
    (-0.2854, -0.0992, 0.5357),
    (-0.2015, -0.2015, 0.5357),
    (-0.0992, -0.2854, 0.3214),
    (-0.0367, -0.1919, 0.3214),
    (-0.0367, -0.1919, 0.1071),
    (-0.0992, -0.2854, 0.1071),
    (-0.0992, -0.2854, -0.1071),
    (-0.0367, -0.1919, -0.1071),
    (-0.0367, -0.1919, -0.3214),
    (-0.0992, -0.2854, -0.3214),
    (-0.0992, -0.2854, -0.5357),
    (-0.2015, -0.2015, -0.5357),
    (-0.2854, -0.0992, -0.5357),
    (-0.3478, 0.0175, -0.5357),
    (-0.3862, 0.1441, -0.5357),
    (-0.3992, 0.2758, -0.5357),
    (-0.3862, 0.1441, 0.3214),
    (-0.3992, 0.2758, 0.3214),
    (-0.3478, 0.0175, 0.3214),
    (-0.2854, -0.0992, 0.3214),
    (-0.2015, -0.2015, 0.3214),
    (-0.1219, -0.1219, 0.3214),
    (-0.1219, -0.1219, 0.1071),
    (-0.2015, -0.2015, 0.1071),
    (-0.2015, -0.2015, -0.1071),
    (-0.1219, -0.1219, -0.1071),
    (-0.1219, -0.1219, -0.3214),
    (-0.2015, -0.2015, -0.3214),
    (-0.2854, -0.0992, -0.3214),
    (-0.3478, 0.0175, -0.3214),
    (-0.3862, 0.1441, -0.3214),
    (-0.3992, 0.2758, -0.3214),
    (-0.2867, 0.2758, 0.3214),
    (-0.2759, 0.1661, 0.3214),
    (-0.2439, 0.0605, 0.3214),
    (-0.1919, -0.0367, 0.3214),
    (-0.1919, -0.0367, 0.1071),
    (-0.2854, -0.0992, 0.1071),
    (-0.2854, -0.0992, -0.1071),
    (-0.1919, -0.0367, -0.1071),
    (-0.1919, -0.0367, -0.3214),
    (-0.2439, 0.0605, -0.3214),
    (-0.2759, 0.1661, -0.3214),
    (-0.2867, 0.2758, -0.3214),
    (-0.2759, 0.1661, 0.1071),
    (-0.2867, 0.2758, 0.1071),
    (-0.2439, 0.0605, 0.1071),
    (-0.3478, 0.0175, 0.1071),
    (-0.3478, 0.0175, -0.1071),
    (-0.2439, 0.0605, -0.1071),
    (-0.2759, 0.1661, -0.1071),
    (-0.2867, 0.2758, -0.1071),
    (-0.3992, 0.2758, 0.1071),
    (-0.3862, 0.1441, 0.1071),
    (-0.3862, 0.1441, -0.1071),
    (-0.3992, 0.2758, -0.1071),
]

_PREVIEW_ISLAND_CHAINS_FACES = [
    ((0, 1, 2, 3), 'M_FBXMT_Wall'),
    ((0, 3, 4, 5), 'M_FBXMT_Floor'),
    ((6, 1, 0, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18), 'M_FBXMT_Wall'),
    ((19, 20, 21, 22, 23, 24, 25, 2, 1, 6), 'M_FBXMT_Floor'),
    ((3, 2, 25, 26), 'M_FBXMT_Wall'),
    ((3, 26, 27, 4), 'M_FBXMT_Floor'),
    ((7, 5, 4, 28), 'M_FBXMT_Chain_01'),
    ((29, 8, 7, 28), 'M_FBXMT_Ceiling'),
    ((9, 8, 29, 30), 'M_FBXMT_Chain_02'),
    ((9, 30, 31, 10), 'M_FBXMT_Floor'),
    ((11, 10, 31, 32), 'M_FBXMT_Chain_03'),
    ((33, 12, 11, 32), 'M_FBXMT_Ceiling'),
    ((13, 12, 33, 34), 'M_FBXMT_Chain_04'),
    ((13, 34, 35, 14), 'M_FBXMT_Floor'),
    ((15, 14, 35, 36), 'M_FBXMT_Chain_05'),
    ((37, 16, 15, 36), 'M_FBXMT_Ceiling'),
    ((17, 16, 37, 38), 'M_FBXMT_Wall'),
    ((18, 17, 38, 39, 40, 41, 42, 43, 44, 45), 'M_FBXMT_Ceiling'),
    ((46, 20, 19, 47), 'M_FBXMT_Wall'),
    ((48, 21, 20, 46), 'M_FBXMT_Wall'),
    ((49, 22, 21, 48), 'M_FBXMT_Wall'),
    ((50, 23, 22, 49), 'M_FBXMT_Wall'),
    ((51, 24, 23, 50), 'M_FBXMT_Wall'),
    ((26, 25, 24, 51), 'M_FBXMT_Wall'),
    ((26, 51, 52, 27), 'M_FBXMT_Floor'),
    ((28, 4, 27, 53), 'M_FBXMT_Chain_01'),
    ((54, 29, 28, 53), 'M_FBXMT_Ceiling'),
    ((30, 29, 54, 55), 'M_FBXMT_Chain_02'),
    ((30, 55, 56, 31), 'M_FBXMT_Floor'),
    ((32, 31, 56, 57), 'M_FBXMT_Chain_03'),
    ((58, 33, 32, 57), 'M_FBXMT_Ceiling'),
    ((34, 33, 58, 59), 'M_FBXMT_Chain_04'),
    ((34, 59, 60, 35), 'M_FBXMT_Floor'),
    ((36, 35, 60, 61), 'M_FBXMT_Chain_05'),
    ((62, 37, 36, 61), 'M_FBXMT_Ceiling'),
    ((38, 37, 62, 39), 'M_FBXMT_Wall'),
    ((39, 62, 63, 40), 'M_FBXMT_Wall'),
    ((40, 63, 64, 41), 'M_FBXMT_Wall'),
    ((41, 64, 65, 42), 'M_FBXMT_Wall'),
    ((42, 65, 66, 43), 'M_FBXMT_Wall'),
    ((43, 66, 67, 44), 'M_FBXMT_Wall'),
    ((44, 67, 68, 45), 'M_FBXMT_Wall'),
    ((46, 47, 69, 70), 'M_FBXMT_Floor'),
    ((48, 46, 70, 71), 'M_FBXMT_Floor'),
    ((49, 48, 71, 72), 'M_FBXMT_Floor'),
    ((50, 49, 72, 73), 'M_FBXMT_Floor'),
    ((51, 50, 73, 52), 'M_FBXMT_Floor'),
    ((53, 27, 52, 74), 'M_FBXMT_Chain_01'),
    ((75, 54, 53, 74), 'M_FBXMT_Ceiling'),
    ((55, 54, 75, 76), 'M_FBXMT_Chain_02'),
    ((55, 76, 77, 56), 'M_FBXMT_Floor'),
    ((57, 56, 77, 78), 'M_FBXMT_Chain_03'),
    ((79, 58, 57, 78), 'M_FBXMT_Ceiling'),
    ((59, 58, 79, 80), 'M_FBXMT_Chain_04'),
    ((59, 80, 81, 60), 'M_FBXMT_Floor'),
    ((61, 60, 81, 82), 'M_FBXMT_Chain_05'),
    ((63, 62, 61, 82), 'M_FBXMT_Ceiling'),
    ((64, 63, 82, 83), 'M_FBXMT_Ceiling'),
    ((65, 64, 83, 84), 'M_FBXMT_Ceiling'),
    ((66, 65, 84, 85), 'M_FBXMT_Ceiling'),
    ((67, 66, 85, 86), 'M_FBXMT_Ceiling'),
    ((68, 67, 86, 87), 'M_FBXMT_Ceiling'),
    ((88, 70, 69, 89), 'M_FBXMT_Chain_01'),
    ((90, 71, 70, 88), 'M_FBXMT_Chain_01'),
    ((91, 72, 71, 90), 'M_FBXMT_Chain_01'),
    ((92, 73, 72, 91), 'M_FBXMT_Chain_01'),
    ((74, 52, 73, 92), 'M_FBXMT_Chain_01'),
    ((93, 75, 74, 92), 'M_FBXMT_Ceiling'),
    ((76, 75, 93, 94), 'M_FBXMT_Chain_02'),
    ((76, 94, 95, 77), 'M_FBXMT_Floor'),
    ((78, 77, 95, 96), 'M_FBXMT_Chain_03'),
    ((97, 79, 78, 96), 'M_FBXMT_Ceiling'),
    ((80, 79, 97, 98), 'M_FBXMT_Chain_04'),
    ((80, 98, 99, 81), 'M_FBXMT_Floor'),
    ((82, 81, 99, 83), 'M_FBXMT_Chain_05'),
    ((83, 99, 100, 84), 'M_FBXMT_Chain_05'),
    ((84, 100, 101, 85), 'M_FBXMT_Chain_05'),
    ((85, 101, 102, 86), 'M_FBXMT_Chain_05'),
    ((86, 102, 103, 87), 'M_FBXMT_Chain_05'),
    ((104, 105, 88, 89), 'M_FBXMT_Ceiling'),
    ((105, 106, 90, 88), 'M_FBXMT_Ceiling'),
    ((106, 107, 91, 90), 'M_FBXMT_Ceiling'),
    ((107, 93, 92, 91), 'M_FBXMT_Ceiling'),
    ((94, 93, 107, 108), 'M_FBXMT_Chain_02'),
    ((94, 108, 109, 95), 'M_FBXMT_Floor'),
    ((96, 95, 109, 110), 'M_FBXMT_Chain_03'),
    ((111, 97, 96, 110), 'M_FBXMT_Ceiling'),
    ((98, 97, 111, 112), 'M_FBXMT_Chain_04'),
    ((98, 112, 100, 99), 'M_FBXMT_Floor'),
    ((112, 113, 101, 100), 'M_FBXMT_Floor'),
    ((113, 114, 102, 101), 'M_FBXMT_Floor'),
    ((114, 115, 103, 102), 'M_FBXMT_Floor'),
    ((116, 105, 104, 117), 'M_FBXMT_Chain_02'),
    ((118, 106, 105, 116), 'M_FBXMT_Chain_02'),
    ((108, 107, 106, 118), 'M_FBXMT_Chain_02'),
    ((108, 118, 119, 109), 'M_FBXMT_Floor'),
    ((110, 109, 119, 120), 'M_FBXMT_Chain_03'),
    ((121, 111, 110, 120), 'M_FBXMT_Ceiling'),
    ((112, 111, 121, 113), 'M_FBXMT_Chain_04'),
    ((113, 121, 122, 114), 'M_FBXMT_Chain_04'),
    ((114, 122, 123, 115), 'M_FBXMT_Chain_04'),
    ((116, 117, 124, 125), 'M_FBXMT_Floor'),
    ((118, 116, 125, 119), 'M_FBXMT_Floor'),
    ((120, 119, 125, 126), 'M_FBXMT_Chain_03'),
    ((122, 121, 120, 126), 'M_FBXMT_Ceiling'),
    ((123, 122, 126, 127), 'M_FBXMT_Ceiling'),
    ((126, 125, 124, 127), 'M_FBXMT_Chain_03'),
    ((18, 45, 68, 87, 103, 115, 123, 127, 124, 117, 104, 89, 69, 47, 19, 6), 'M_FBXMT_Wall'),
]

# Camera data: (location_xyz, rotation_euler_xyz) from staged blend.
# Mesh data: (location_xyz, scale_uniform) matching staged scene.
# Island Chains camera/mesh X+50 offset removed — both placed at origin in preview scene.
_PREVIEW_GEO_TRIM_CAMERA        = ((0.0,  25.0, 6.0),  None)   # None = compute look-at to mesh centre
_PREVIEW_GEO_TRIM_MESH          = ((0.0,  0.2,  1.0),   2.0, 0.7854)
_PREVIEW_ISLAND_CHAINS_CAMERA   = ((0.0, -25.0, 7.0),  (1.5770, -0.00935, 0.01335))
_PREVIEW_ISLAND_CHAINS_MESH     = ((0.0,  0.0,  7.5),  10.0, 0.7854)


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
    # All 20 colour values
    for prop_a, prop_b in _MAT_COLOR_PROPS:
        ca = getattr(prefs, prop_a, None)
        cb = getattr(prefs, prop_b, None)
        if ca:
            parts.append(','.join(f'{v:.4f}' for v in ca))
        if cb:
            parts.append(','.join(f'{v:.4f}' for v in cb))
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


def _build_preview_mesh(verts_data, faces_data, scene_name: str):
    """Build a preview mesh object in a dedicated scene from hardcoded data.
    Assigns FBXMT materials to faces by name. Returns (preview_scene, obj).
    Caller is responsible for removing the scene when done.
    """
    # Remove any leftover preview scene
    prev_scene = bpy.data.scenes.get(scene_name)
    if prev_scene:
        bpy.data.scenes.remove(prev_scene, do_unlink=True)
    prev_scene = bpy.data.scenes.new(scene_name)

    # Build material slot list from ALL_DISPLAY_MATERIAL_NAMES so slot indices
    # are stable and predictable — same order every time regardless of face order.
    # Only include materials that actually appear in this mesh's faces.
    used_mat_names = set(mat_name for _, mat_name in faces_data)
    # Keep canonical order from ALL_DISPLAY_MATERIAL_NAMES
    mat_names_ordered = [n for n in ALL_DISPLAY_MATERIAL_NAMES if n in used_mat_names]
    mat_index_map = {n: i for i, n in enumerate(mat_names_ordered)}

    # Build mesh via bmesh
    me = bpy.data.meshes.new(scene_name + '_mesh')
    bm = bmesh.new()

    bm_verts = [bm.verts.new(v) for v in verts_data]
    bm.verts.ensure_lookup_table()

    for vert_indices, mat_name in faces_data:
        try:
            face_verts = [bm_verts[i] for i in vert_indices]
            f = bm.faces.new(face_verts)
            f.material_index = mat_index_map[mat_name]
        except Exception:
            pass  # skip degenerate faces

    bm.to_mesh(me)
    bm.free()

    # Append materials in the same canonical order used for mat_index_map
    for i, mat_name in enumerate(mat_names_ordered):
        mat = bpy.data.materials.get(mat_name)
        if mat:
            me.materials.append(mat)
        else:
            placeholder = bpy.data.materials.new(name='__fbxmt_placeholder_' + mat_name)
            me.materials.append(placeholder)

    # Create object and link to preview scene
    obj = bpy.data.objects.new(scene_name + '_obj', me)
    prev_scene.collection.objects.link(obj)

    # Unwrap using the toolkit's own unwrapper so UVs match the checker scale.
    # Temporarily remap Ignore faces to Wall so they get valid UVs in the preview
    # (unwrap_mesh zeroes Ignore faces, which renders black).
    import math
    ignore_slot = next((i for i, m in enumerate(me.materials) if m and m.name == 'M_FBXMT_Ignore'), None)
    wall_slot   = next((i for i, m in enumerate(me.materials) if m and m.name == 'M_FBXMT_Wall'), None)
    if ignore_slot is not None and wall_slot is not None:
        bm_tmp = bmesh.new()
        bm_tmp.from_mesh(me)
        for face in bm_tmp.faces:
            if face.material_index == ignore_slot:
                face.material_index = wall_slot
        bm_tmp.to_mesh(me)
        bm_tmp.free()

    unwrap_mesh(me, obj.matrix_world, math.cos(math.radians(45.0)))

    # Restore Ignore material slot assignments after unwrap
    if ignore_slot is not None and wall_slot is not None:
        bm_tmp = bmesh.new()
        bm_tmp.from_mesh(me)
        # Identify which faces were originally Ignore by checking the face data
        # from the original faces_data — restore by position
        ignore_face_indices = {
            i for i, (_, mat_name) in enumerate(faces_data)
            if mat_name == 'M_FBXMT_Ignore'
        }
        for i, face in enumerate(bm_tmp.faces):
            if i in ignore_face_indices:
                face.material_index = ignore_slot
        bm_tmp.to_mesh(me)
        bm_tmp.free()

    return prev_scene, obj


def _setup_preview_camera(prev_scene, cam_loc, cam_rot_euler, look_at=None):
    """Add a camera. If cam_rot_euler is None, compute rotation from look_at point."""
    cam_data = bpy.data.cameras.new('__fbxmt_preview_cam')
    cam_data.type = 'PERSP'
    cam_data.lens = 50.0
    cam_obj = bpy.data.objects.new('__fbxmt_preview_cam', cam_data)
    prev_scene.collection.objects.link(cam_obj)

    cam_obj.location = Vector(cam_loc)

    if cam_rot_euler is None and look_at is not None:
        direction = Vector(look_at) - Vector(cam_loc)
        cam_obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    else:
        cam_obj.rotation_euler = cam_rot_euler

    prev_scene.camera = cam_obj
    return cam_obj


def _render_preview(prev_scene, img_name: str, size: int):
    """Render prev_scene to a bpy.data.images entry. Returns the image or None."""
    prev_scene.render.engine            = 'BLENDER_EEVEE_NEXT'
    prev_scene.render.resolution_x     = size
    prev_scene.render.resolution_y     = size
    prev_scene.render.resolution_percentage = 100
    prev_scene.render.film_transparent = True
    prev_scene.render.image_settings.file_format = 'PNG'

    # World — plain grey so materials stand out
    if not prev_scene.world:
        prev_scene.world = bpy.data.worlds.new('__fbxmt_preview_world')
    prev_scene.world.use_nodes = True
    bg = prev_scene.world.node_tree.nodes.get('Background')
    if bg:
        bg.inputs['Color'].default_value    = (0.15, 0.15, 0.15, 1.0)
        bg.inputs['Strength'].default_value = 1.0

    tmp_path = os.path.join(tempfile.gettempdir(), f'{img_name}.png')
    prev_scene.render.filepath = tmp_path

    # Render into the temp file
    override = {'scene': prev_scene}
    try:
        with bpy.context.temp_override(**override):
            bpy.ops.render.render(write_still=True, scene=prev_scene.name)
    except Exception as e:
        print(f'[FBXMT] Preview render failed: {e}')
        return None

    if not os.path.exists(tmp_path):
        return None

    # Load result into bpy.data.images
    existing = bpy.data.images.get(img_name)
    if existing:
        bpy.data.images.remove(existing)
    img = bpy.data.images.load(tmp_path)
    img.name = img_name
    img.pack()
    try:
        os.remove(tmp_path)
    except Exception:
        pass

    return img


# ─── Material bake ───────────────────────────────────────────────────────────

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
        bpy.ops.fbxmt.bake_all_modal('INVOKE_DEFAULT', skip_rebuild=True)
        return {'FINISHED'}



# ─── Operator: set texel density ─────────────────────────────────────────────

class FBXMT_OT_ProjectSetup_SetDensity(Operator):
    bl_idname  = 'fbxmt.project_setup_set_density'
    bl_label   = 'Set Texel Density'
    bl_options = {'REGISTER'}

    density: IntProperty()

    def execute(self, context):
        props = context.scene.fbxmt_props
        props.geo_texel_density   = self.density
        props.fbxmt_preview_stale = True
        props.fbxmt_cache_hash    = ''
        # No rebuild here — viewport updates on dialog close
        bpy.ops.fbxmt.bake_all_modal('INVOKE_DEFAULT', skip_rebuild=True)
        return {'FINISHED'}


class FBXMT_OT_ProjectSetup_SetCheckerScale(Operator):
    """Set checker scale from inside Project Setup — marks stale, no viewport rebuild.
    The N-panel button (fbxmt.set_checker_scale) still rebuilds immediately."""
    bl_idname  = 'fbxmt.project_setup_set_checker_scale'
    bl_label   = 'Set Checker Scale (Dialog)'
    bl_options = {'REGISTER', 'INTERNAL'}

    value: IntProperty(default=4)

    def execute(self, context):
        prefs = context.scene.fbxmt_prefs_global
        if prefs:
            prefs.checker_scale = self.value
        context.scene.fbxmt_props.fbxmt_preview_stale = True
        bpy.ops.fbxmt.bake_all_modal('INVOKE_DEFAULT', skip_rebuild=True)
        return {'FINISHED'}


# ─── Operator: render preview ─────────────────────────────────────────────────

class FBXMT_OT_ProjectSetup_Preview(Operator):
    bl_idname  = 'fbxmt.project_setup_preview'
    bl_label   = 'Render Material Preview'
    bl_options = {'REGISTER'}

    def execute(self, context):
        props    = context.scene.fbxmt_props
        idx      = props.fbxmt_selected_mat_index
        mat_name = ALL_DISPLAY_MATERIAL_NAMES[idx]

        ensure_fbxmt_materials()

        rebuild_fbxmt_materials()

        # Choose mesh based on material type
        is_chain = mat_name.startswith('M_FBXMT_Chain_')
        if is_chain:
            verts_data  = _PREVIEW_ISLAND_CHAINS_VERTS
            faces_data  = _PREVIEW_ISLAND_CHAINS_FACES
            cam_data    = _PREVIEW_ISLAND_CHAINS_CAMERA
            mesh_data   = _PREVIEW_ISLAND_CHAINS_MESH
        else:
            verts_data  = _PREVIEW_GEO_TRIM_VERTS
            faces_data  = _PREVIEW_GEO_TRIM_FACES
            cam_data    = _PREVIEW_GEO_TRIM_CAMERA
            mesh_data   = _PREVIEW_GEO_TRIM_MESH

        scene_name = '__fbxmt_preview_scene'
        img_name   = '__fbxmt_preview'

        try:
            prev_scene, _obj = _build_preview_mesh(verts_data, faces_data, scene_name)
            # Apply scale, location offset and Z rotation matching the staged preview scene
            _obj.location        = mesh_data[0]
            s                    = mesh_data[1]
            _obj.scale           = (s, s, s)
            _obj.rotation_euler  = (0.0, 0.0, mesh_data[2])
            mesh_centre = (_obj.location[0], _obj.location[1], _obj.location[2])
            _setup_preview_camera(prev_scene, cam_data[0], cam_data[1], look_at=mesh_centre)
            img = _render_preview(prev_scene, img_name, PREVIEW_SIZE)
        finally:
            # Always clean up the temporary scene
            ps = bpy.data.scenes.get(scene_name)
            if ps:
                # Remove orphaned camera/lamp datablocks
                for obj in list(ps.collection.objects):
                    data = obj.data
                    ps.collection.objects.unlink(obj)
                    bpy.data.objects.remove(obj, do_unlink=True)
                    if data and data.users == 0:
                        if isinstance(data, bpy.types.Mesh):
                            bpy.data.meshes.remove(data)
                        elif isinstance(data, bpy.types.Camera):
                            bpy.data.cameras.remove(data)
                        elif isinstance(data, bpy.types.Light):
                            pass  # no lights in preview scene
                bpy.data.scenes.remove(ps, do_unlink=True)

        if img is None:
            self.report({'WARNING'}, 'Preview render failed — check system console')
            return {'CANCELLED'}

        props.fbxmt_preview_stale = False
        context.scene.fbxmt_preview_mode = 'MODEL'  # switch display to model render
        return {'FINISHED'}



# ─── Operator: contact sheet ──────────────────────────────────────────────────

class FBXMT_OT_ProjectSetup_TilingTest(Operator):
    """Render a 3×3 tiling test sheet of the Ignore material — no labels.
    Shows how the tile pattern tiles across 9 adjacent squares so edge
    alignment lines can be verified visually."""
    bl_idname  = 'fbxmt.project_setup_tiling_test'
    bl_label   = 'Tiling Test'
    bl_options = {'REGISTER'}

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
                _build_checker_node_tree(tmp, col_a, col_b, pattern=pattern, no_corner_marks=True)
            return tmp_name

        _make_tmp('ignore', '__fbxmt_tiling_test_ignore')
        _make_tmp('wall',   '__fbxmt_tiling_test_wall')

        def _render_slot(tmp_name):
            img = _render_tile(tmp_name, context, size=cell, split=False)
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
    bl_options = {'REGISTER', 'INTERNAL'}

    size: IntProperty()

    def execute(self, context):
        context.scene.fbxmt_props.contact_sheet_size = self.size
        return {'FINISHED'}


class FBXMT_OT_ProjectSetup_ContactSheet(Operator):
    bl_idname  = 'fbxmt.project_setup_contact_sheet'
    bl_label   = 'Build Contact Sheet'
    bl_options = {'REGISTER'}

    COLS = 3

    def execute(self, context):
        props = context.scene.fbxmt_props
        prefs = context.scene.fbxmt_prefs_global
        cell  = props.contact_sheet_size
        full  = props.contact_sheet_full

        # Build material list — standard 6 or full 21
        if full:
            mat_names = list(ALL_DISPLAY_MATERIAL_NAMES)
            # Islands fill column-first: Floor(01-05) col0, Ceil(06-10) col1, Wall(11-15) col2
            # Row-major order for a 3-col grid means interleaving the 3 groups:
            # row3: Island_01, Island_06, Island_11
            # row4: Island_02, Island_07, Island_12  etc.
            # Names are interleaved: Floor_01, Ceil_01, Wall_01, Floor_02, ...
            # Extract each column's tiles in row order for correct grid placement
            from .materials import ISLAND_SUB_NAMES as _ISN
            floor_islands = [_ISN[i] for i in range(0,  15, 3)]   # indices 0,3,6,9,12
            ceil_islands  = [_ISN[i] for i in range(1,  15, 3)]   # indices 1,4,7,10,13
            wall_islands  = [_ISN[i] for i in range(2,  15, 3)]   # indices 2,5,8,11,14
            for f, c, w in zip(floor_islands, ceil_islands, wall_islands):
                mat_names.extend([f, c, w])
        else:
            mat_names = list(ALL_DISPLAY_MATERIAL_NAMES)

        rows = (len(mat_names) + self.COLS - 1) // self.COLS

        # Render all tiles at contact_sheet_size
        imgs = []
        for mat_name in mat_names:
            img = _render_tile(mat_name, context, size=cell)
            if img:
                imgs.append((mat_name, img))
            else:
                self.report({'WARNING'}, f'Could not render {mat_name} — skipping')

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
                    arr = np.array(img.pixels[:], dtype=np.float32).reshape(cell, cell, 4)
                    # 1px crop on all edges to eliminate EEVEE render border bleed.
                    # Expand cropped interior back to cell size with edge-clamped repeat
                    # so the tile fills its slot without a gap seam.
                    interior = arr[1:-1, 1:-1, :]  # (cell-2, cell-2, 4)
                    padded   = np.pad(
                        interior,
                        ((1, 1), (1, 1), (0, 0)),
                        mode='edge',
                    )
                    sheet_px[y0:y0 + cell, x0:x0 + cell] = padded
                except Exception as e:
                    self.report({'WARNING'}, f'Could not read pixels for {mat_name}: {e}')
            sheet.pixels = sheet_px.ravel().tolist()
        except Exception as e:
            self.report({'WARNING'}, f'Contact sheet pixel assembly failed: {e}')

        # Draw material name labels
        try:
            px_data = list(sheet.pixels)
            def _put_char(px_data, ch, cx, cy, img_w, img_h, scale=1):
                rows_f = _FONT_5X7.get(ch.upper(), _FONT_5X7[' '])
                for ry, row_f in enumerate(rows_f):
                    for rx, bit in enumerate(row_f):
                        if bit == '1':
                            for sy in range(scale):
                                for sx in range(scale):
                                    px = cx + rx * scale + sx
                                    py = cy - ry * scale - sy
                                    if 0 <= px < img_w and 0 <= py < img_h:
                                        i = (py * img_w + px) * 4
                                        px_data[i:i+3] = [1.0, 1.0, 1.0]
                                        px_data[i+3]   = 1.0

            _COL_LABELS = {0: 'FLOOR', 1: 'CEIL', 2: 'WALL'}
            _ISLAND_START_IDX = 6  # first island sub-material index in imgs list

            for idx, (mat_name, _img) in enumerate(imgs):
                col  = idx % cols
                row  = idx // cols
                y0   = (rows - 1 - row) * cell
                x0   = col * cell
                # Island sub-material label: FLOOR01-05 / CEIL01-05 / WALL01-05
                if idx >= _ISLAND_START_IDX:
                    island_row = row - 1  # 1-based within island section
                    prefix = _COL_LABELS.get(col, 'ISL')
                    # Hex suffix for row numbers beyond 9 (F = row 15 max)
                    label  = f'{prefix}{format(island_row, "X")}'
                else:
                    label = mat_name.replace('M_FBXMT_', '').replace('_', ' ')[:12]
                scale = max(1, cell // 128)
                cx    = x0 + 4
                cy    = y0 + cell - 4
                for ch in label:
                    _put_char(px_data, ch, cx, cy, sheet_w, sheet_h, scale)
                    cx += (5 * scale) + 1
                    if cx + 5 * scale >= x0 + cell:
                        break
            sheet.pixels = px_data
        except Exception as e:
            print(f'[FBXMT] Contact sheet label draw failed: {e}')

        # Save to MaterialCache/
        if bpy.data.filepath:
            save_dir  = os.path.join(os.path.dirname(bpy.data.filepath), CACHE_SUBDIR)
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, 'FBXMT_ContactSheet.png')
            sheet.filepath_raw = save_path
            sheet.file_format  = 'PNG'
            sheet.save()
            self.report({'INFO'}, f'Contact sheet saved to {save_path}')
        else:
            self.report({'INFO'}, 'Contact sheet generated — save the file to write it to disk')

        return {'FINISHED'}


# ─── Preset system helpers ────────────────────────────────────────────────────

# Derivation props — stored in every preset, used by Simple load
_PRESET_DERIVATION_PROPS = [
    'anchor_hue',
    'color_b_mode',
    'color_b_notch',
    'checker_scale',
    'corner_mark_preset',
    'show_corner_circle',
    'show_corner_lines',
    'bake_labels',
    *[f'checker_pattern_{s}' for s in ('floor', 'ceiling', 'wall', 'trim', 'ignore', 'island')],
    'island_swap_ab',
    'apex_line_seed',
]

# Full colour stack — stored in addition to derivation props in every preset
_PRESET_COLOUR_PROPS = [
    *[f'color_{s}_a' for s in ('floor', 'ceiling', 'wall', 'trim', 'ignore', 'island')],
    *[f'color_{s}_b' for s in ('floor', 'ceiling', 'wall', 'trim', 'ignore', 'island')],
]

# Swatch material order and labels — matches tile grid top-left → bottom-right
_SWATCH_MATS = [
    ('wall',    'color_wall_a',    'color_wall_b',    'Wall'),
    ('floor',   'color_floor_a',   'color_floor_b',   'Floor'),
    ('ceiling', 'color_ceiling_a', 'color_ceiling_b', 'Ceiling'),
    ('trim',    'color_trim_a',    'color_trim_b',    'Trim'),
    ('ignore',  'color_ignore_a',  'color_ignore_b',  'Ignore'),
    ('island',  'color_island_a',  'color_island_b',  'Island'),
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

    for row_idx, (_, prop_a, prop_b, label) in enumerate(_SWATCH_MATS):
        y0 = h - (row_idx + 1) * _SWATCH_ROW   # top-down order
        y1 = y0 + _SWATCH_ROW

        col_a = tuple(getattr(prefs, prop_a, (0.5, 0.5, 0.5, 1.0))[:3])
        col_b = tuple(getattr(prefs, prop_b, (0.3, 0.3, 0.3, 1.0))[:3])

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
    """Return the presets directory path, or None if not configured."""
    addon_prefs = context.preferences.addons.get(ADDON_ID)
    if not addon_prefs:
        return None
    path = addon_prefs.preferences.presets_path.strip()
    return path if path else None


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
        return list(val)
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


class OT_FBXMT_Preset_Save(Operator):
    """Save current material settings as a named preset (always Full format)."""
    bl_idname  = 'fbxmt.preset_save'
    bl_label   = 'Save Preset'
    bl_options = {'REGISTER'}

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
            addon_prefs = bpy.context.preferences.addons.get(ADDON_ID)
            if addon_prefs:
                addon_prefs.preferences.presets_path = self.directory
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
    bl_options = {'REGISTER', 'UNDO'}

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
        name = data.get('__name__', os.path.basename(self.filepath))
        mode = 'simple' if self.load_mode == 'SIMPLE' else 'full'
        self.report({'INFO'}, f'Preset loaded ({mode}): {name}')
        return {'FINISHED'}


class OT_FBXMT_Preset_Delete(Operator):
    """Delete a material preset file."""
    bl_idname  = 'fbxmt.preset_delete'
    bl_label   = 'Delete Preset'
    bl_options = {'REGISTER'}

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
        prefs = context.scene.fbxmt_prefs_global
        slots = ['floor', 'ceiling', 'wall', 'trim', 'ignore', 'island']
        src   = self.source_slot
        if src not in slots:
            return {'CANCELLED'}
        mode   = getattr(prefs, f'color_b_mode_{src}')
        darker = getattr(prefs, f'color_b_darker_{src}')
        grey   = getattr(prefs, f'color_b_grey_{src}')
        col_b  = tuple(getattr(prefs, f'color_{src}_b'))
        for slot in slots:
            if slot == src:
                continue
            setattr(prefs, f'color_b_mode_{slot}',   mode)
            setattr(prefs, f'color_b_darker_{slot}',  darker)
            setattr(prefs, f'color_b_grey_{slot}',    grey)
            try:
                setattr(prefs, f'color_{slot}_b', col_b)
            except Exception:
                pass
        context.scene.fbxmt_props.fbxmt_preview_stale = True
        bpy.ops.fbxmt.bake_all_modal('INVOKE_DEFAULT', skip_rebuild=True)
        self.report({'INFO'}, 'Colour B applied to all materials')
        return {'FINISHED'}


class FBXMT_OT_ApplyAnchor(Operator):
    """Derive all material colours from the anchor hue and refresh preview tiles.
    The 3D viewport rebuild happens only when the Project Setup dialog is closed."""
    bl_idname  = 'fbxmt.apply_anchor'
    bl_label   = 'Apply Anchor'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prefs = context.scene.fbxmt_prefs_global
        from .materials import _derive_colours_from_anchor
        _derive_colours_from_anchor(prefs)
        context.scene.fbxmt_props.fbxmt_preview_stale = True
        # Re-render preview tiles so the dialog reflects the new colours.
        # Does NOT call rebuild_fbxmt_materials — viewport updates on OK only.
        bpy.ops.fbxmt.bake_all_modal('INVOKE_DEFAULT', skip_rebuild=True)
        return {'FINISHED'}


# ─── Operator: main project setup dialog ─────────────────────────────────────

class FBXMT_OT_ProjectSetup(Operator):
    bl_idname  = 'fbxmt.project_setup'
    bl_label   = 'FBXMT Project Setup'
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        ensure_fbxmt_materials()
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
        return context.window_manager.invoke_props_dialog(self, width=720)

    def execute(self, context):
        # Purge cached swatch images — they're dialog-local previews only
        for img in list(bpy.data.images):
            if img.name.startswith('__fbxmt_swatch_'):
                bpy.data.images.remove(img)
        # OK pressed — rebuild all materials so the 3D viewport reflects
        # any settings changes made inside the dialog.
        rebuild_fbxmt_materials()
        bpy.ops.fbxmt.bake_all_modal('INVOKE_DEFAULT')
        context.scene.fbxmt_props.fbxmt_preview_stale = False
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        props  = context.scene.fbxmt_props
        prefs  = context.scene.fbxmt_prefs_global

        if props is None or prefs is None:
            layout.label(text='Scene properties unavailable', icon='ERROR')
            return

        # ── Tile grid — labels replace buttons ────────────────────────────────
        box = layout.box()
        for row_names in (
            ALL_DISPLAY_MATERIAL_NAMES[:3],
            ALL_DISPLAY_MATERIAL_NAMES[3:],
        ):
            tile_row = box.row(align=True)
            for mn in row_names:
                col = tile_row.column()
                img = bpy.data.images.get(f'__tile_{mn}')
                if img:
                    img.preview_ensure()
                    col.template_icon(icon_value=img.preview.icon_id, scale=8.0)
                else:
                    sub = col.box()
                    sub.scale_y = 3.5
                    sub.label(text='')
                # Plain label — no click interaction
                col.label(text=_MAT_DISPLAY_NAMES.get(mn, mn))

        # ── Preview buttons ───────────────────────────────────────────────────
        row = layout.row(align=True)
        row.operator('fbxmt.project_setup_update_tile',   text='Update Tile',   icon='FILE_REFRESH')
        row.operator('fbxmt.project_setup_contact_sheet', text='Contact Sheet', icon='IMAGE_REFERENCE')
        row.operator('fbxmt.project_setup_tiling_test',   text='Tiling Test',   icon='TEXTURE')
        row.prop(props, 'contact_sheet_full', text='Full', toggle=True)
        if props.fbxmt_preview_stale:
            row.label(text='', icon='ERROR')

        # Contact sheet size — binary steps from 256 to geo_texel_density
        row = layout.row(align=True)
        row.label(text='Sheet Size:')
        sz = 256
        while sz <= props.geo_texel_density:
            op = row.operator(
                'fbxmt.set_contact_sheet_size',
                text=str(sz),
                depress=(props.contact_sheet_size == sz),
            )
            op.size = sz
            sz *= 2

        layout.separator(factor=0.5)

        # ── Bottom: two columns ───────────────────────────────────────────────
        split     = layout.split(factor=0.5)
        col_left  = split.column()
        col_right = split.column()

        # ── LEFT: Material Settings — new wave ────────────────────────────────
        col_left.label(text='Material Settings', icon='SHADING_RENDERED')
        col_left.separator(factor=0.5)

        # Anchor hue slider
        col_left.label(text='Anchor Hue (0–1):')
        col_left.prop(prefs, 'anchor_hue', text='', slider=True)
        col_left.operator('fbxmt.apply_anchor', text='Apply', icon='FILE_REFRESH')
        col_left.separator(factor=0.5)

        # Global B mode
        col_left.prop(prefs, 'color_b_mode', text='Colour B')
        if prefs.color_b_mode in ('DARKER', 'LIGHTER', 'GREYSCALE'):
            col_left.prop(prefs, 'color_b_notch', text='Amount', slider=True)
        col_left.separator(factor=0.5)

        # Per-material checker patterns — uniform paired rows
        col_left.label(text='Checker Style:')
        for (slot_l, label_l), (slot_r, label_r) in (
            (('wall',    'Wall'),    ('trim',    'Trim')),
            (('floor',   'Floor'),   ('ignore',  'Ignore')),
            (('ceiling', 'Ceiling'), ('island',  'Island')),
        ):
            row = col_left.row(align=False)
            # Left pair — fixed 50% of row
            left = row.split(factor=0.5, align=True)
            ls = left.split(factor=0.35, align=True)
            ls.label(text=label_l)
            ls.prop(prefs, f'checker_pattern_{slot_l}', text='')
            # Right pair — remaining 50%
            rs = left.split(factor=0.35, align=True)
            rs.label(text=label_r)
            if slot_r == 'island':
                sub = rs.row(align=True)
                sub.prop(prefs, f'checker_pattern_{slot_r}', text='')
                sub.prop(prefs, 'island_swap_ab', text='', icon='ARROW_LEFTRIGHT', toggle=True)
            else:
                rs.prop(prefs, f'checker_pattern_{slot_r}', text='')
        col_left.separator(factor=0.5)

        # Texel density
        col_left.label(text='Texel Density:')
        row = col_left.row(align=True)
        for val in (1024, 2048, 4096, 8192):
            op = row.operator(
                'fbxmt.project_setup_set_density',
                text    = str(val),
                depress = (props.geo_texel_density == val),
            )
            op.density = val
        col_left.separator(factor=0.5)

        # Checker scale
        col_left.label(text='Checker Scale:')
        row = col_left.row(align=True)
        for val in (1, 2, 4, 8):
            op = row.operator(
                'fbxmt.project_setup_set_checker_scale',
                text    = str(val),
                depress = (prefs.checker_scale == val),
            )
            op.value = val
        col_left.separator(factor=0.5)

        # Corner marks
        row = col_left.row(align=False)
        row.prop(prefs, 'show_corner_lines',  text='Lines',  toggle=True)
        col_left.separator(factor=0.5)

        # Apex line seed
        row = col_left.row(align=True)
        row.label(text='Line Seed:')
        row.prop(prefs, 'apex_line_seed', text='')

        # ── RIGHT: Project Settings + Presets ─────────────────────────────────
        col_right.label(text='Project Settings', icon='PROPERTIES')
        col_right.separator(factor=0.5)

        # Paths box
        paths_box = col_right.box()
        paths_box.label(text='Paths:', icon='FILEBROWSER')
        paths_box.prop(props, 'export_path', text='Export')
        paths_box.prop(props, 'import_path', text='Import')

        col_right.separator(factor=0.5)

        # Import box
        import_box = col_right.box()
        import_box.label(text='Import:', icon='IMPORT')
        import_box.prop(props,  'quick_import_type', text='Type')
        import_box.prop(prefs,  'prep_on_import',    text='Full Prep on Import')

        col_right.separator(factor=0.5)

        addon_prefs = bpy.context.preferences.addons.get(ADDON_ID)

        col_right.separator(factor=1.0)

        # Material Presets box
        preset_box_outer = col_right.box()
        preset_box_outer.label(text='Material Presets', icon='PRESET')
        if addon_prefs:
            preset_box_outer.prop(addon_prefs.preferences, 'presets_path', text='Folder')
        preset_box_outer.separator(factor=0.5)

        presets = _list_presets(context)
        if presets:
            # Dropdown — selected preset drives swatch preview and action buttons
            sel_idx = getattr(context.scene, 'fbxmt_selected_preset_index', 0)
            sel_idx = max(0, min(sel_idx, len(presets) - 1))

            # Draw dropdown as a menu button row
            drop_row = preset_box_outer.row(align=True)
            sel_name, sel_path = presets[sel_idx]
            drop_row.menu('FBXMT_MT_PresetPicker', text=sel_name, icon='PRESET')
            del_op = drop_row.operator('fbxmt.preset_delete', text='', icon='X')
            del_op.filepath = sel_path

            preset_box_outer.separator(factor=0.5)

            # Swatch preview — load companion PNG on the fly
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
            preset_box_outer.label(text='No presets found', icon='INFO')

        col_right.separator(factor=0.5)
        col_right.operator('fbxmt.preset_save', text='Save Current...', icon='FILE_TICK')



# ─── Modal bake operator ─────────────────────────────────────────────────────

def _composite_island_steps(img, prefs, checker_scale, mat_name):
    """Overwrite the bottom half of a tile with the middle island sub-material.

    Renders M_FBXMT_Island_{Floor|Ceil|Wall}_03 (the centre lightness step)
    directly so the preview exactly matches what's in the viewport.
    """
    _MAT_TO_ISLAND_MID = {
        'M_FBXMT_Floor':   'M_FBXMT_Island_Floor_03',
        'M_FBXMT_Ceiling': 'M_FBXMT_Island_Ceil_03',
        'M_FBXMT_Wall':    'M_FBXMT_Island_Wall_03',
    }
    island_mat_name = _MAT_TO_ISLAND_MID.get(mat_name)
    if not island_mat_name:
        return

    try:
        size = img.size[0]
        half = size // 2

        # Render the island sub-material at the same size
        island_img = _render_tile(island_mat_name, bpy.context, size=size)
        if island_img is None:
            return

        # Read both pixel arrays
        base_px   = np.empty(size * size * 4, dtype=np.float32)
        island_px = np.empty(size * size * 4, dtype=np.float32)
        img.pixels.foreach_get(base_px)
        island_img.pixels.foreach_get(island_px)

        base_px   = base_px.reshape(size, size, 4)
        island_px = island_px.reshape(size, size, 4)

        # Overwrite bottom half of base with island render
        base_px[:half] = island_px[:half]

        img.pixels.foreach_set(base_px.ravel())
        img.update()

    except Exception as e:
        print(f'[FBXMT] _composite_island_steps failed for {mat_name}: {e}')
    finally:
        if island_img:
            try:
                bpy.data.images.remove(island_img)
            except Exception:
                pass


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
    Called after _composite_island_steps so marks are always last in draw order.
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
    scene.render.resolution_x               = size
    scene.render.resolution_y               = size
    scene.render.resolution_percentage      = 100
    scene.render.film_transparent           = False
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode  = 'RGB'
    if hasattr(scene, 'eevee'):
        eevee = scene.eevee
        if hasattr(eevee, 'taa_render_samples'): eevee.taa_render_samples = 1
        if hasattr(eevee, 'use_bloom'):          eevee.use_bloom = False
        if hasattr(eevee, 'use_ssr'):            eevee.use_ssr   = False
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
        try:
            geo_texel_density = context.scene.fbxmt_props.geo_texel_density
        except Exception:
            geo_texel_density = 1024
        tile_scale  = geo_texel_density / 1024.0
        render_size = size if size is not None else PREVIEW_SIZE

        scene, obj = _ensure_tile_scene(render_size, tile_scale)

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

        # Composite island B steps for split preview only.
        # Corner marks are rendered directly by the node tree via EEVEE — no numpy composite needed.
        checker_scale = prefs.checker_scale if prefs else 4
        if split and mat_name in _SPLIT_TILE_MATS:
            _composite_island_steps(img, prefs, checker_scale, mat_name)

        # Corner marks — numpy compositor
        if (mat_name.startswith('__fbxmt_preview_') or
                mat_name.startswith('__fbxmt_tiling_test_')):
            _composite_corner_marks(img, prefs, render_size, checker_scale)

        # Apex position lines — drawn LAST so they're always on top
        if not no_apex_lines:
            _composite_apex_lines(img, prefs, checker_scale, render_size)

        # Pack into .blend memory, then delete temp file
        img.pack()
        try:
            os.remove(tmp_path)
        except Exception:
            pass

        return img

    except Exception as e:
        import traceback
        print(f'[FBXMT] Tile render failed for {mat_name}: {e}')
        traceback.print_exc()
        return None


def _build_preview_materials(prefs):
    """Build temporary copies of all display materials with current prefs applied.
    Returns a dict {real_name: temp_name}. Caller must call _cleanup_preview_materials().
    """
    from .materials import (
        _read_mat_settings, _build_checker_node_tree,
        ISLAND_SUB_NAMES, ISLAND_MARKER_NAME,
        rebuild_fbxmt_materials
    )
    import colorsys

    _SLOT_TO_MAT = {
        'floor':   'M_FBXMT_Floor',
        'ceiling': 'M_FBXMT_Ceiling',
        'wall':    'M_FBXMT_Wall',
        'trim':    'M_FBXMT_Trim',
        'ignore':  'M_FBXMT_Ignore',
    }
    temp_map = {}

    for slot, mat_name in _SLOT_TO_MAT.items():
        col_a, col_b, pattern = _read_mat_settings(slot)
        if col_a is None:
            continue
        tmp_name = f'__fbxmt_preview_{mat_name}'
        tmp = bpy.data.materials.get(tmp_name) or bpy.data.materials.new(tmp_name)
        tmp.use_nodes = True
        _build_checker_node_tree(tmp, col_a, col_b, pattern=pattern, no_corner_marks=True)
        temp_map[mat_name] = tmp_name

    # Island marker
    col_a_wall, _, _ = _read_mat_settings('wall')
    _, _, pattern_island = _read_mat_settings('island')
    if col_a_wall:
        tmp_name = f'__fbxmt_preview_{ISLAND_MARKER_NAME}'
        tmp = bpy.data.materials.get(tmp_name) or bpy.data.materials.new(tmp_name)
        tmp.use_nodes = True
        _build_checker_node_tree(tmp, col_a_wall, (0.5, 0.5, 0.5), pattern=pattern_island or 'SQUARE', no_corner_marks=True)
        temp_map[ISLAND_MARKER_NAME] = tmp_name

    # Middle island sub-materials (_03 series) for split tile preview
    _group_settings = [
        _read_mat_settings('floor'),
        _read_mat_settings('ceiling'),
        _read_mat_settings('wall'),
    ]
    _offsets = [-0.4, -0.2, 0.0, 0.2, 0.4]
    swap_ab  = getattr(prefs, 'island_swap_ab', False)
    for i, name in enumerate(ISLAND_SUB_NAMES):
        group    = i % 3
        slot_idx = i // 3
        parent_a, _, _ = _group_settings[group]
        if parent_a is None:
            continue
        h, l, s = colorsys.rgb_to_hls(*parent_a)
        hue_b   = (h + 0.5) % 1.0
        off     = _offsets[slot_idx]
        col_b   = colorsys.hls_to_rgb(hue_b, max(0.15, min(0.85, 0.5 + off)), max(0.6, s))
        a, b    = (col_b, parent_a) if swap_ab else (parent_a, col_b)
        tmp_name = f'__fbxmt_preview_{name}'
        tmp = bpy.data.materials.get(tmp_name) or bpy.data.materials.new(tmp_name)
        tmp.use_nodes = True
        _build_checker_node_tree(tmp, a, b, pattern=pattern_island or 'SQUARE', checker_invert=True, no_corner_marks=True)
        temp_map[name] = tmp_name

    return temp_map


def _cleanup_preview_materials():
    """Remove all temporary preview materials."""
    to_remove = [m for m in bpy.data.materials if m.name.startswith('__fbxmt_preview_')]
    for m in to_remove:
        bpy.data.materials.remove(m)


class FBXMT_OT_BakeAllModal(Operator):
    """Modal operator that bakes one material per timer tick so Blender can
    redraw the header progress text between each bake. Invoked by the dialog
    execute, Update Tile, Rebuild, and template load."""
    bl_idname  = 'fbxmt.bake_all_modal'
    bl_label   = 'FBXMT Build Preview Tiles'
    bl_options = {'REGISTER'}

    skip_rebuild: bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        from .materials import rebuild_fbxmt_materials, ensure_fbxmt_materials

        scene = context.scene
        prefs = scene.fbxmt_prefs_global
        props = scene.fbxmt_props

        ensure_fbxmt_materials()

        self._mat_queue = list(ALL_DISPLAY_MATERIAL_NAMES)
        self._total     = len(self._mat_queue)
        self._done      = 0
        self._temp_map  = {}

        if self.skip_rebuild:
            # Build temp copies of materials with current prefs — viewport untouched
            self._temp_map = _build_preview_materials(prefs)
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
        mat = bpy.data.materials.get(render_name)
        if mat:
            img = _render_tile(render_name, context, split=True)
            if img:
                # Rename image to match real material name for display
                img.name = f'__tile_{mat_name}'
                if context.area:
                    context.area.tag_redraw()

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

        # Switch preview to sheet view and force redraw
        context.scene.fbxmt_preview_mode = 'SHEET'
        if context.area:
            context.area.tag_redraw()

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

CLASSES = (
    FBXMT_OT_BakeAllModal,
    FBXMT_OT_ProjectSetup_UpdateTile,
    FBXMT_OT_ProjectSetup_SetDensity,
    FBXMT_OT_ProjectSetup_SetCheckerScale,
    FBXMT_OT_ProjectSetup_TilingTest,
    FBXMT_OT_ProjectSetup_SetContactSheetSize,
    FBXMT_OT_ApplyAnchor,
    FBXMT_OT_ProjectSetup_Preview,
    FBXMT_OT_ProjectSetup_ContactSheet,
    OT_FBXMT_Preset_Save,
    OT_FBXMT_Preset_Load,
    OT_FBXMT_Preset_Delete,
    OT_FBXMT_SelectTile,
    OT_FBXMT_ApplyBToAll,
    FBXMT_OT_SelectPreset,
    FBXMT_MT_PresetPicker,
    FBXMT_OT_ProjectSetup,
)


def _on_mat_dropdown_update(scene, _context):
    """Sync dropdown selection to fbxmt_selected_mat_index and switch to tile view."""
    scene.fbxmt_props.fbxmt_selected_mat_index = int(scene.fbxmt_preview_mat_enum)
    scene.fbxmt_preview_mode = 'TILE'  # switching material always shows tile


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.fbxmt_preview_mat_enum = bpy.props.EnumProperty(
        name   = "Material",
        items  = [(str(i), name, "") for i, name in enumerate(_MAT_DISPLAY_NAMES.values())],
        default = "0",
        update  = _on_mat_dropdown_update,
    )
    bpy.types.Scene.fbxmt_preview_mode = bpy.props.EnumProperty(
        name  = "Preview Mode",
        items = [("TILE", "Tile", ""), ("MODEL", "Model", ""), ("SHEET", "Contact Sheet", "")],
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
