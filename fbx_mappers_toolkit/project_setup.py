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
import shutil
import tempfile
import bmesh
from mathutils import Vector
from bpy.types import Operator
from bpy.props import IntProperty, BoolProperty

from .materials import _get_prefs, ensure_fbxmt_materials, rebuild_fbxmt_materials
from .uv_unwrap import unwrap_mesh


# ─── Constants ────────────────────────────────────────────────────────────────

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

# Colour property pairs on FBXMT_GlobalPrefs, indexed by ALL_DISPLAY_MATERIAL_NAMES order
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
    prev_scene.render.engine  = 'CYCLES'
    prev_scene.cycles.samples = 1
    # Use GPU if user has it configured in Cycles preferences
    cycles_prefs = bpy.context.preferences.addons.get('cycles')
    if cycles_prefs and cycles_prefs.preferences.compute_device_type != 'NONE':
        prev_scene.cycles.device = 'GPU'
    else:
        prev_scene.cycles.device = 'CPU'
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
    bl_options = {'REGISTER'}

    def execute(self, context):
        if not context.scene.fbxmt_props.export_path:
            self.report({'WARNING'}, 'No export folder set — textures not saved')
        bpy.ops.fbxmt.bake_all_modal('INVOKE_DEFAULT')
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

class FBXMT_OT_ProjectSetup_ContactSheet(Operator):
    bl_idname  = 'fbxmt.project_setup_contact_sheet'
    bl_label   = 'Build Contact Sheet'
    bl_options = {'REGISTER'}

    # Layout: 5 cols × 2 rows for all 10 materials including Ignore
    COLS      = 5
    ROWS      = 2
    CELL_SIZE = 256

    def execute(self, context):
        # Collect images — bake any missing tiles on the fly
        imgs = []
        for mat_name in ALL_DISPLAY_MATERIAL_NAMES:
            img = bpy.data.images.get(f'__cache_{mat_name}')
            if img is None:
                # Try loading from disk cache first
                cp = _cache_path(context.scene, mat_name)
                if cp and os.path.exists(cp):
                    loaded      = bpy.data.images.load(cp)
                    loaded.name = f'__cache_{mat_name}'
                    img         = loaded
            if img is None:
                # Bake on the fly at preview size — no prior bake run yet
                self.report({'INFO'}, f'Baking {mat_name}...')
                from .op import OT_FBXMT_Export
                prefs   = context.scene.fbxmt_prefs_global
                mat     = bpy.data.materials.get(mat_name)
                tmp_dir = tempfile.mkdtemp(prefix='fbxmt_cs_')
                try:
                    result = OT_FBXMT_Export._bake_material_emit(
                        mat, None, tmp_dir,
                        size          = PREVIEW_SIZE,
                        label_grid    = False,
                        checker_scale = prefs.checker_scale if prefs else 4,
                    ) if mat else None
                    if result and os.path.exists(result):
                        loaded      = bpy.data.images.load(result)
                        loaded.name = f'__cache_{mat_name}'
                        # Force pixel buffer into memory NOW — tmp_dir is deleted
                        # below and reload() will fail on a missing file.
                        _ = loaded.pixels[:]
                        img         = loaded
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
            if img:
                imgs.append((mat_name, img))
            else:
                self.report({'WARNING'}, f'Could not bake {mat_name} — skipping')

        if not imgs:
            self.report({'ERROR'}, 'No material images available')
            return {'CANCELLED'}

        cell   = self.CELL_SIZE
        cols   = self.COLS
        rows   = self.ROWS
        sheet_w = cell * cols
        sheet_h = cell * rows

        existing = bpy.data.images.get('FBXMT_ContactSheet')
        if existing:
            bpy.data.images.remove(existing)
        sheet = bpy.data.images.new('FBXMT_ContactSheet', width=sheet_w, height=sheet_h, alpha=False)

        try:
            import numpy as np
            sheet_px = np.zeros((sheet_h, sheet_w, 4), dtype=np.float32)
            for idx, (mat_name, img) in enumerate(imgs):
                col = idx % cols
                row = idx // cols
                y0  = (rows - 1 - row) * cell   # Blender pixel origin is bottom-left
                x0  = col * cell
                # Force buffer load — only reload if the source file actually exists
                if not img.has_data:
                    if img.filepath and os.path.exists(bpy.path.abspath(img.filepath)):
                        img.reload()
                    else:
                        # Memory-only image with no backing file — skip this cell
                        self.report({'WARNING'}, f'No pixel data for {mat_name} — skipping')
                        continue
                # Scale image to cell size if needed
                if img.size[0] != cell or img.size[1] != cell:
                    img.scale(cell, cell)
                arr = np.array(img.pixels[:], dtype=np.float32).reshape(cell, cell, 4)
                sheet_px[y0:y0 + cell, x0:x0 + cell] = arr
            sheet.pixels = sheet_px.ravel().tolist()
        except ImportError:
            # Pure Python fallback
            pixels = [0.0] * (sheet_w * sheet_h * 4)
            for idx, (mat_name, img) in enumerate(imgs):
                col = idx % cols
                row = idx // cols
                y0  = (rows - 1 - row) * cell
                x0  = col * cell
                if not img.has_data:
                    if img.filepath and os.path.exists(bpy.path.abspath(img.filepath)):
                        img.reload()
                    else:
                        self.report({'WARNING'}, f'No pixel data for {mat_name} — skipping')
                        continue
                src = list(img.pixels)
                for py in range(cell):
                    for px in range(cell):
                        si = (py * cell + px) * 4
                        di = ((y0 + py) * sheet_w + x0 + px) * 4
                        pixels[di:di + 4] = src[si:si + 4]
            sheet.pixels = pixels

        # Draw material name labels on each cell using a simple pixel font
        try:
            px_data = list(sheet.pixels)
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
                '_':['00000','00000','00000','00000','00000','00000','11111'],
                ' ':['00000','00000','00000','00000','00000','00000','00000'],
            }
            def _put_char(px_data, ch, cx, cy, img_w, img_h, scale=1):
                rows = _FONT_5X7.get(ch.upper(), _FONT_5X7[' '])
                for ry, row in enumerate(rows):
                    for rx, bit in enumerate(row):
                        if bit == '1':
                            for sy in range(scale):
                                for sx in range(scale):
                                    px = cx + rx * scale + sx
                                    py = cy - ry * scale - sy
                                    if 0 <= px < img_w and 0 <= py < img_h:
                                        i = (py * img_w + px) * 4
                                        px_data[i:i+3] = [1.0, 1.0, 1.0]
                                        px_data[i+3]   = 1.0

            for idx, (mat_name, _img) in enumerate(imgs):
                col  = idx % cols
                row  = idx // cols
                y0   = (rows - 1 - row) * cell
                x0   = col * cell
                # Short label: strip M_FBXMT_ prefix, replace _ with space
                label = mat_name.replace('M_FBXMT_', '').replace('_', ' ')[:12]
                scale = 1
                cx    = x0 + 4
                cy    = y0 + cell - 4  # near top of cell (Blender Y=0 is bottom)
                for ch in label:
                    _put_char(px_data, ch, cx, cy, sheet_w, sheet_h, scale)
                    cx += (5 * scale) + 1
                    if cx + 5 * scale >= x0 + cell:
                        break
            sheet.pixels = px_data
        except Exception as e:
            print(f'[FBXMT] Contact sheet label draw failed: {e}')

        # Save contact sheet to MaterialCache/ alongside the blend file
        if bpy.data.filepath:
            save_dir = os.path.join(os.path.dirname(bpy.data.filepath), CACHE_SUBDIR)
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, 'FBXMT_ContactSheet.png')
            sheet.filepath_raw = save_path
            sheet.file_format  = 'PNG'
            sheet.save()
            self.report({'INFO'}, f'Contact sheet saved to {save_path}')
        else:
            self.report({'INFO'}, 'Contact sheet generated — save the file to write it to disk')

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
        # Rebake all materials on dialog close via modal operator
        if not context.scene.fbxmt_props.export_path:
            self.report({'WARNING'}, 'No export folder set — textures not saved')
        bpy.ops.fbxmt.bake_all_modal('INVOKE_DEFAULT')

        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        props  = context.scene.fbxmt_props
        prefs  = context.scene.fbxmt_prefs_global

        if props is None or prefs is None:
            layout.label(text='Scene properties unavailable', icon='ERROR')
            return

        # Derive index from enum
        try:
            idx = int(context.scene.fbxmt_preview_mat_enum)
        except (AttributeError, ValueError):
            idx = props.fbxmt_selected_mat_index
        idx = max(0, min(idx, len(ALL_DISPLAY_MATERIAL_NAMES) - 1))

        mat_name       = ALL_DISPLAY_MATERIAL_NAMES[idx]
        prop_a, prop_b = _MAT_COLOR_PROPS[idx]
        preview_mode   = getattr(context.scene, 'fbxmt_preview_mode', 'TILE')

        # ── Preview image — full width, tight vertical ───────────────────────
        tile_img  = bpy.data.images.get(f'__tile_{mat_name}')
        model_img = bpy.data.images.get('__fbxmt_preview')

        box = layout.box()
        if preview_mode == 'SHEET':
            for row_names in (ALL_DISPLAY_MATERIAL_NAMES[:5], ALL_DISPLAY_MATERIAL_NAMES[5:]):
                row = box.row(align=True)
                for mn in row_names:
                    col = row.column()
                    img = bpy.data.images.get(f'__tile_{mn}')
                    if img:
                        img.preview_ensure()
                        col.template_icon(icon_value=img.preview.icon_id, scale=8.0)
                    else:
                        sub = col.box()
                        sub.scale_y = 3.5
                        sub.label(text='')
                    col.label(text=_MAT_DISPLAY_NAMES.get(mn, mn))
        elif preview_mode == 'MODEL' and model_img:
            model_img.preview_ensure()
            box.template_icon(icon_value=model_img.preview.icon_id, scale=18.0)
        elif tile_img:
            tile_img.preview_ensure()
            box.template_icon(icon_value=tile_img.preview.icon_id, scale=18.0)
        else:
            box.scale_y = 3.0
            box.label(text='Please stand by, compiling shaders.', icon='TIME')

        # ── Preview buttons ───────────────────────────────────────────────────
        row = layout.row(align=True)
        row.operator('fbxmt.project_setup_update_tile',   text='Update Tile',   icon='FILE_REFRESH')
        row.operator('fbxmt.project_setup_contact_sheet', text='Contact Sheet', icon='IMAGE_REFERENCE')
        if props.fbxmt_preview_stale:
            row.label(text='', icon='ERROR')

        layout.separator(factor=0.5)

        # ── Bottom: two columns ───────────────────────────────────────────────
        split     = layout.split(factor=0.5)
        col_left  = split.column()
        col_right = split.column()

        # ── LEFT: Material Settings ───────────────────────────────────────────
        col_left.label(text='Material Settings', icon='SHADING_RENDERED')
        col_left.separator(factor=0.5)
        col_left.prop(context.scene, 'fbxmt_preview_mat_enum', text='Material')
        col_left.separator(factor=0.5)
        row = col_left.row(align=True)
        row.prop(prefs, prop_a, text='A')
        row.prop(prefs, prop_b, text='B')
        col_left.separator(factor=0.5)
        col_left.label(text='Texel Density:')
        row = col_left.row(align=True)
        for val in (512, 1024, 2048, 4096, 8192):
            op = row.operator(
                'fbxmt.project_setup_set_density',
                text    = str(val),
                depress = (props.geo_texel_density == val),
            )
            op.density = val
        col_left.separator(factor=0.5)
        col_left.prop(prefs, 'checker_scale',      text='Checker Scale')
        col_left.separator(factor=0.5)
        col_left.prop(prefs, 'corner_mark_preset', text='Corner Preset')
        col_left.separator(factor=0.5)
        row = col_left.row(align=False)
        row.prop(prefs, 'corner_mark_width_px', text='Width')
        row.separator(factor=1.5)
        row.prop(prefs, 'show_corner_circle',   text='Circle')
        row.separator(factor=1.5)
        row.prop(prefs, 'bake_labels',          text='Labels')
        col_left.separator(factor=0.5)
        col_left.prop(prefs, 'corner_hue_shift', text='Line Hue Shift')

        # ── RIGHT: Project Settings ───────────────────────────────────────────
        col_right.label(text='Project Settings', icon='PROPERTIES')
        col_right.separator(factor=0.5)
        col_right.label(text='Paths:', icon='FILEBROWSER')
        col_right.separator(factor=0.5)
        col_right.prop(props, 'export_path', text='Export')
        col_right.separator(factor=0.5)
        col_right.prop(props, 'import_path', text='Import')
        col_right.separator(factor=0.5)
        col_right.label(text='Import:', icon='IMPORT')
        col_right.separator(factor=0.5)
        col_right.prop(props,  'quick_import_type', text='Type')
        col_right.separator(factor=0.5)
        col_right.prop(prefs,  'prep_on_import',      text='Full Prep on Import')
        col_right.separator(factor=0.5)
        # show_setup_on_new lives in AddonPreferences — persists across files
        addon_prefs = bpy.context.preferences.addons.get(__package__)
        if addon_prefs:
            col_right.prop(addon_prefs.preferences, 'show_setup_on_new', text='Show Setup on New Project')



# ─── Modal bake operator ─────────────────────────────────────────────────────

class FBXMT_OT_BakeAllModal(Operator):
    """Modal operator that bakes one material per timer tick so Blender can
    redraw the header progress text between each bake. Invoked by the dialog
    execute, Update Tile, Rebuild, and template load."""
    bl_idname  = 'fbxmt.bake_all_modal'
    bl_label   = 'FBXMT Bake All Materials'
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        from .op import OT_FBXMT_Export
        from .materials import rebuild_fbxmt_materials, ensure_fbxmt_materials

        scene = context.scene
        prefs = scene.fbxmt_prefs_global
        props = scene.fbxmt_props

        ensure_fbxmt_materials()

        # Sanity check — skip bake if nothing has changed since last bake
        current_hash = _compute_cache_hash(scene)
        stored_hash  = scene.fbxmt_props.fbxmt_cache_hash if scene.fbxmt_props else ''
        if current_hash == stored_hash and stored_hash:
            self.report({'INFO'}, 'FBXMT: Materials unchanged — skipping bake')
            return {'FINISHED'}

        self._checker_scale = prefs.checker_scale if prefs else 4
        self._mat_queue     = list(ALL_DISPLAY_MATERIAL_NAMES)
        self._total         = len(self._mat_queue)
        self._done          = 0
        self._tmp_dir       = tempfile.mkdtemp(prefix='fbxmt_bake_')
        self._OT_Export     = OT_FBXMT_Export

        rebuild_fbxmt_materials()
        context.workspace.status_text_set('FBXMT  |  Getting ready...  Stand by...')

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
            f'FBXMT  |  Baking {display_name}  ({self._done}/{self._total})  —  Stand by...'
        )

        mat = bpy.data.materials.get(mat_name)
        if mat:
            # Preview tile
            tile_result = self._OT_Export._bake_material_emit(
                mat, None, self._tmp_dir,
                size          = PREVIEW_SIZE,
                label_grid    = False,
                checker_scale = self._checker_scale,
            )
            if tile_result:
                img_name = f'__tile_{mat_name}'
                existing = bpy.data.images.get(img_name)
                if existing:
                    bpy.data.images.remove(existing)
                img      = bpy.data.images.load(tile_result)
                img.name = img_name
                img.pack()


        return {'RUNNING_MODAL'}

    def _finish(self, context):
        scene  = context.scene
        props  = scene.fbxmt_props

        context.window_manager.event_timer_remove(self._timer)
        context.workspace.status_text_set(None)

        from .materials import rebuild_fbxmt_materials
        rebuild_fbxmt_materials()

        shutil.rmtree(self._tmp_dir, ignore_errors=True)

        if props:
            props.fbxmt_cache_hash = _compute_cache_hash(scene)

        # Switch preview to sheet view
        context.scene.fbxmt_preview_mode = 'SHEET'

        self.report({'INFO'}, f'FBXMT bake complete — {self._total} materials processed')
        return {'FINISHED'}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)
        context.workspace.status_text_set(None)
        shutil.rmtree(self._tmp_dir, ignore_errors=True)


# ─── Registration ─────────────────────────────────────────────────────────────

CLASSES = (
    FBXMT_OT_BakeAllModal,
    FBXMT_OT_ProjectSetup_UpdateTile,
    FBXMT_OT_ProjectSetup_SetDensity,
    FBXMT_OT_ProjectSetup_Preview,
    FBXMT_OT_ProjectSetup_ContactSheet,
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


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "fbxmt_preview_mat_enum"):
        del bpy.types.Scene.fbxmt_preview_mat_enum
    if hasattr(bpy.types.Scene, "fbxmt_preview_mode"):
        del bpy.types.Scene.fbxmt_preview_mode
