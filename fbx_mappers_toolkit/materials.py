import bpy
import re
import math
import colorsys
import bmesh
from mathutils import Vector
from bpy.types import Operator
from bpy.props import FloatVectorProperty


def _safe_float(prefs, prop, default):
    """Read a float-valued EnumProperty safely.

    EnumProperty stores values as strings. If the saved blend file has a value
    that no longer exists in the items list Blender returns '' — plain float()
    raises ValueError. This helper falls back to default and resets the prop
    to a valid string value so the UI dropdown shows correctly next redraw.
    """
    raw = getattr(prefs, prop, str(default))
    try:
        v = float(raw)
        if v == 0.0 and raw == '':
            raise ValueError
        return v
    except (ValueError, TypeError):
        # Reset prop to closest valid string so UI shows a value
        try:
            setattr(prefs, prop, str(default))
        except Exception:
            pass
        return default

# ─── Base material definitions ────────────────────────────────────────────────

FBXMT_MATERIALS = {
    'M_FBXMT_Floor':         (0.3,  0.75, 0.3,  1.0),
    'M_FBXMT_Ceiling':       (0.3,  0.55, 0.9,  1.0),
    'M_FBXMT_Wall':          (0.9,  0.65, 0.2,  1.0),
    'M_FBXMT_Trim':          (0.75, 0.3,  0.75, 1.0),
    'M_FBXMT_Ignore':        (0.25, 0.25, 0.25, 1.0),
    'M_FBXMT_Ramp_Floor':    (0.6,  0.7,  0.25, 1.0),
    'M_FBXMT_Ramp_Ceiling':  (0.45, 0.65, 0.75, 1.0),
}

FBXMT_FLOOR_MATERIALS = {'M_FBXMT_Floor', 'M_FBXMT_Ceiling'}
FBXMT_RAMP_MATERIALS  = {'M_FBXMT_Ramp_Floor', 'M_FBXMT_Ramp_Ceiling'}
FBXMT_WALL_MATERIALS  = {'M_FBXMT_Wall', 'M_FBXMT_Trim'}
FBXMT_IGNORE_MATERIAL = 'M_FBXMT_Ignore'
FBXMT_ALL_MATERIALS   = FBXMT_FLOOR_MATERIALS | FBXMT_RAMP_MATERIALS | FBXMT_WALL_MATERIALS | {FBXMT_IGNORE_MATERIAL}

# Checker textures removed — all materials now use procedural node trees

# ── Island marker system ─────────────────────────────────────────────────────
# One marker material visible to the artist. 15 hidden sub-materials used
# internally by the graph colourer and unwrapper to distinguish islands.
# Sub-materials share Colour A with the marker but get distinct grey B values
# (0..100% in 15 steps). They are filtered from all panels and never baked.
ISLAND_MARKER_NAME = 'M_FBXMT_Island'
ISLAND_SUB_PREFIX  = 'M_FBXMT_Island_'  # used by op.py startswith check

# Named by parent, interleaved Floor/Ceil/Wall for graph-colouring spread:
# index 0=Floor_01, 1=Ceil_01, 2=Wall_01, 3=Floor_02, ... 14=Wall_05
ISLAND_SUB_NAMES   = [
    name
    for i in range(1, 6)
    for name in (
        f'M_FBXMT_Island_Wall_{i:02d}',
        f'M_FBXMT_Island_Floor_{i:02d}',
        f'M_FBXMT_Island_Ceil_{i:02d}',
    )
]

RAMP_ISLAND_NAMES  = [f'M_FBXMT_Island_Ramp_{i:02d}' for i in range(1, 4)]

# Combined set for fast membership tests — covers all hidden sub-materials
ALL_ISLAND_SUB_NAMES = ISLAND_SUB_NAMES + RAMP_ISLAND_NAMES
_ALL_ISLAND_SUB_SET  = set(ALL_ISLAND_SUB_NAMES)
# B values for sub-materials: cycle through 5 darkness steps, repeated across 15 slots
# Step darknesses: 100%, 80%, 60%, 40%, 20% of parent Colour B toward black
_ISLAND_B_STEP_COUNT = 5

# Keep CHAIN_PREFIX/CHAIN_NAMES as legacy aliases so existing blend files
# that still reference old chain materials don't hard-error on import.
CHAIN_PREFIX  = 'M_FBXMT_Chain_'
CHAIN_NAMES   = [f'M_FBXMT_Chain_0{i}' for i in range(1, 6)]
TOOLKIT_PREFIX = 'M_FBXMT_'

# All visible FBXMT material names (excludes hidden sub-materials)
FBXMT_ALL_MATERIALS_WITH_CHAINS = set(FBXMT_ALL_MATERIALS) | {ISLAND_MARKER_NAME}

# Default island marker colour A — vivid cyan-blue, distinct from base materials
_ISLAND_COLOR_A = colorsys.hls_to_rgb(0.58, 0.55, 0.90)  # bright cyan
_CHAIN_DEFAULTS = []  # legacy — no longer used for new files

CHECKER_BLUE_RGB   = _ISLAND_COLOR_A

COLLECTION_GEO        = 'Geo'
COLLECTION_PROPS      = 'Props'
COLLECTION_TRIM       = 'Trim'
LIGHTMAP_CHANNEL_NAME = 'LightmapUVs'
PREVIEW_UV_NAME       = 'UVPreview'

# Island B step offsets from inverted-B centre lightness
# [-0.5, -0.25, 0, +0.25, +0.5] — clamped 0-1 at use time
ISLAND_B_STEP_OFFSETS = [-0.5, -0.25, 0.0, 0.25, 0.5]
ISLAND_B_STEPS = [0.0, 0.25, 0.50, 0.75, 1.0]  # legacy — replaced by offsets


# Module-level flag — set True while operators are mutating material slots so
# that any future depsgraph handler won't re-enter mid-execute.
# Set directly via _mat_module._suppress_handler in fbx_import.py during
# the full-prep pipeline, and via the global in OT_FBXMT_Assign_Materials.
_suppress_handler = False


# ─── Island marker helpers ───────────────────────────────────────────────────

def _island_sub_index(mat_name):
    """Return 1-based index of a named island sub-material (base or ramp), or None."""
    if mat_name in _ALL_ISLAND_SUB_SET:
        try:
            return ALL_ISLAND_SUB_NAMES.index(mat_name) + 1
        except ValueError:
            pass
    return None


def _is_island_sub_material(mat):
    """True for any hidden island sub-material (base or ramp)."""
    return mat is not None and mat.name in _ALL_ISLAND_SUB_SET


def _is_island_material(mat):
    """True for the visible marker OR any hidden sub-material."""
    return mat is not None and (
        mat.name == ISLAND_MARKER_NAME or _is_island_sub_material(mat)
    )


def get_all_island_sub_materials():
    """All hidden sub-materials (base + ramp), creating missing ones."""
    return [bpy.data.materials.get(n) for n in ALL_ISLAND_SUB_NAMES if bpy.data.materials.get(n)]


# Legacy aliases so old code that imports _chain_index / _is_chain_material
# from materials.py doesn't hard-error on existing blend files.
def _chain_index(mat_name):
    m = re.fullmatch(re.escape(CHAIN_PREFIX) + r'(\d+)', mat_name)
    return int(m.group(1)) if m else None

def _is_chain_material(mat):
    return mat is not None and mat.name in set(CHAIN_NAMES)

def get_all_chain_materials():
    return [bpy.data.materials.get(name) for name in CHAIN_NAMES if bpy.data.materials.get(name)]


def _get_prefs():
    """Retrieve global addon preferences from the active scene."""
    try:
        return bpy.context.scene.fbxmt_prefs_global
    except AttributeError:
        return None


# ─── Node-tree builders ───────────────────────────────────────────────────────

def setup_material_nodes(mat, colour, scale=None, color_b=None, pattern='SQUARE'):
    """Build the procedural checker node tree for a base toolkit material."""
    r, g, b = colour[0], colour[1], colour[2]
    color_a = (r, g, b)
    if color_b is None:
        color_b = (r * 0.7, g * 0.7, b * 0.7)
    _build_checker_node_tree(mat, color_a, color_b, scale=scale, pattern=pattern)
    mat.diffuse_color = (*colour[:3], 1.0)


def _resolve_color_b(color_a_rgb, hue_offset_deg, sat, val):
    """Return the resolved colour B as an RGB tuple.

    Derives B from A by:
      - rotating A's hue by hue_offset_deg (0-180, 30-degree steps)
      - applying independent sat and val (both 0.0-1.0)

    color_a_rgb  — (r, g, b) tuple for colour A
    hue_offset_deg — int or str, degrees to rotate hue (0, 30, 60 ... 180)
    sat          — float 0.0-1.0, saturation for B
    val          — float 0.0-1.0, value/lightness for B (HLS lightness)
    """
    r, g, b = color_a_rgb[0], color_a_rgb[1], color_a_rgb[2]
    h, _l, _s = colorsys.rgb_to_hls(r, g, b)
    offset = int(hue_offset_deg) / 360.0
    h_new  = (h + offset) % 1.0
    return colorsys.hls_to_rgb(h_new, float(val), float(sat))


def _build_pattern_nodes(nodes, links, new_node, mapping_checker, pattern):
    """Build the pattern sub-graph between the checker mapping and the A/B Mix.

    Returns a Value socket (0.0 or 1.0 per pixel) used as the Mix factor:
      0.0 → colour A,  1.0 → colour B

    SQUARE   — uses ShaderNodeTexChecker directly (existing path), returns None
                so the caller falls back to the checker node's Fac output.
    DIAGONAL — fract(U) + fract(V) < 1.0
    DIAMOND  — abs(fract(U) - 0.5) + abs(fract(V) - 0.5) < 0.5
    """
    if pattern == 'SQUARE':
        return None  # caller uses checker.outputs['Color'] as before

    # Separate U and V from the checker-mapped vector
    sep = new_node('ShaderNodeSeparateXYZ', -460, 400)
    links.new(mapping_checker.outputs['Vector'], sep.inputs['Vector'])

    frac_u = new_node('ShaderNodeMath', -280, 460)
    frac_u.operation = 'FRACT'
    links.new(sep.outputs['X'], frac_u.inputs[0])

    frac_v = new_node('ShaderNodeMath', -280, 360)
    frac_v.operation = 'FRACT'
    links.new(sep.outputs['Y'], frac_v.inputs[0])

    if pattern == 'DIAGONAL':
        # fract(U) + fract(V) < 1.0
        add = new_node('ShaderNodeMath', -100, 410)
        add.operation = 'ADD'
        links.new(frac_u.outputs['Value'], add.inputs[0])
        links.new(frac_v.outputs['Value'], add.inputs[1])

        lt = new_node('ShaderNodeMath', 80, 410)
        lt.operation = 'LESS_THAN'
        lt.inputs[1].default_value = 1.0
        links.new(add.outputs['Value'], lt.inputs[0])
        return lt.outputs['Value']

    if pattern == 'DIAMOND':
        # abs(fract(U) - 0.5) + abs(fract(V) - 0.5) < 0.5
        sub_u = new_node('ShaderNodeMath', -100, 460)
        sub_u.operation = 'SUBTRACT'
        sub_u.inputs[1].default_value = 0.5
        links.new(frac_u.outputs['Value'], sub_u.inputs[0])

        sub_v = new_node('ShaderNodeMath', -100, 360)
        sub_v.operation = 'SUBTRACT'
        sub_v.inputs[1].default_value = 0.5
        links.new(frac_v.outputs['Value'], sub_v.inputs[0])

        abs_u = new_node('ShaderNodeMath', 80, 460)
        abs_u.operation = 'ABSOLUTE'
        links.new(sub_u.outputs['Value'], abs_u.inputs[0])

        abs_v = new_node('ShaderNodeMath', 80, 360)
        abs_v.operation = 'ABSOLUTE'
        links.new(sub_v.outputs['Value'], abs_v.inputs[0])

        add = new_node('ShaderNodeMath', 260, 410)
        add.operation = 'ADD'
        links.new(abs_u.outputs['Value'], add.inputs[0])
        links.new(abs_v.outputs['Value'], add.inputs[1])

        lt = new_node('ShaderNodeMath', 440, 410)
        lt.operation = 'LESS_THAN'
        lt.inputs[1].default_value = 0.5
        links.new(add.outputs['Value'], lt.inputs[0])
        return lt.outputs['Value']

    if pattern == 'CIRCLE':
        # sqrt((fract(U)-0.5)² + (fract(V)-0.5)²) < sqrt(1/(2π))
        # Radius chosen so circle area = 50% of checker square area.
        # πr² = 0.5  →  r = sqrt(0.5/π) ≈ 0.3989
        import math
        EQUAL_AREA_R = math.sqrt(0.5 / math.pi)  # ≈ 0.3989
        sub_u = new_node('ShaderNodeMath', -100, 460)
        sub_u.operation = 'SUBTRACT'
        sub_u.inputs[1].default_value = 0.5
        links.new(frac_u.outputs['Value'], sub_u.inputs[0])

        sub_v = new_node('ShaderNodeMath', -100, 360)
        sub_v.operation = 'SUBTRACT'
        sub_v.inputs[1].default_value = 0.5
        links.new(frac_v.outputs['Value'], sub_v.inputs[0])

        sq_u = new_node('ShaderNodeMath', 80, 460)
        sq_u.operation = 'MULTIPLY'
        links.new(sub_u.outputs['Value'], sq_u.inputs[0])
        links.new(sub_u.outputs['Value'], sq_u.inputs[1])

        sq_v = new_node('ShaderNodeMath', 80, 360)
        sq_v.operation = 'MULTIPLY'
        links.new(sub_v.outputs['Value'], sq_v.inputs[0])
        links.new(sub_v.outputs['Value'], sq_v.inputs[1])

        add = new_node('ShaderNodeMath', 260, 410)
        add.operation = 'ADD'
        links.new(sq_u.outputs['Value'], add.inputs[0])
        links.new(sq_v.outputs['Value'], add.inputs[1])

        sqrt = new_node('ShaderNodeMath', 440, 410)
        sqrt.operation = 'SQRT'
        links.new(add.outputs['Value'], sqrt.inputs[0])

        lt = new_node('ShaderNodeMath', 620, 410)
        lt.operation = 'GREATER_THAN'  # circles = colour A (inside), background = colour B (outside)
        lt.inputs[1].default_value = EQUAL_AREA_R
        links.new(sqrt.outputs['Value'], lt.inputs[0])
        return lt.outputs['Value']

    return None  # unknown pattern — fall back to square


def _build_checker_node_tree(mat, color_a_rgb, color_b_rgb, scale=None, pattern='SQUARE', checker_invert=False, no_corner_marks=False, geo_texel_density=None):
    """Procedural checkerboard with texel-tile corner cross markers via Emission.

    Two independent mapping paths:

    1. CHECKER path - mapping_checker at scale=squares_per_tile.
       Drives both checker nodes. Controls how many checker squares appear
       per texel tile. Purely visual subdivision.

    2. MARKER path - mapping_tile at scale=geo_texel_density/1024.
       Drives SeparateXYZ for corner cross detection only.
       One cross per texel tile, at texel tile corners - not at checker
       square corners. Each tile corner is the meeting point of 4 L-shapes
       from the 4 surrounding tiles, forming a + cross.

    In mapping_tile space: 1.0 UV unit = 1 texel tile (by construction).
    BORDER_W = px/1024 (arm width as fraction of tile, tile always = 1024px).
    BORDER_L = corner_mark_length/100 (arm extent as fraction of tile).

    geo_texel_density: pass explicitly when bpy.context.scene may not be the
    user scene (e.g. during modal renders). Falls back to bpy.context.scene
    then 1024 if not supplied.
    """
    prefs = _get_prefs()

    if scale is None:
        squares_per_tile = prefs.checker_scale if prefs else 4
        scale = float(squares_per_tile)

    # Texel tile scale: geo_texel_density/1024 maps 1 UV unit to 1 texel tile.
    if geo_texel_density is None:
        try:
            geo_texel_density = bpy.context.scene.fbxmt_props.geo_texel_density
        except Exception:
            geo_texel_density = 1024.0
    tile_scale = float(geo_texel_density) / 1024.0

    # Corner marker constants — all fractions of one texel tile (1.0 in tile UV space).
    show_circle = True  # circle always on
    show_lines  = getattr(prefs, 'show_corner_lines', False) if prefs else False
    CIRCLE_PRESET = 2                              # circle always preset 2
    LINE_PRESET   = 4 if show_lines else 2         # lines: extended or short
    BORDER_L    = LINE_PRESET * 0.125              # arm length as fraction of tile
    CIRCLE_R    = CIRCLE_PRESET * 0.125 * 0.5     # circle radius = half of preset 2
    BORDER_W    = 8.0 / 1024.0                    # fixed 8px at 1024tx/m

    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    def new_node(node_type, x, y):
        nd = nodes.new(node_type)
        nd.location = (x, y)
        return nd

    # ── Inputs ───────────────────────────────────────────────────────────────
    tc = new_node('ShaderNodeTexCoord', -900, 0)

    # ── Checker mapping — squares_per_tile subdivisions per texel tile ────────
    mapping_checker = new_node('ShaderNodeMapping', -700, 150)
    mapping_checker.inputs['Scale'].default_value = (scale, scale, scale)
    links.new(tc.outputs['UV'], mapping_checker.inputs['Vector'])

    # ── Tile mapping — one UV unit per texel tile, drives corner markers ──────
    mapping_tile = new_node('ShaderNodeMapping', -700, -150)
    mapping_tile.inputs['Scale'].default_value = (tile_scale, tile_scale, tile_scale)
    links.new(tc.outputs['UV'], mapping_tile.inputs['Vector'])

    # ── Main checker (always built — provides colour source and SQUARE factor) ──
    checker = new_node('ShaderNodeTexChecker', -460, 200)
    checker.inputs['Color1'].default_value = (*color_a_rgb, 1.0)
    checker.inputs['Color2'].default_value = (*color_b_rgb, 1.0)
    checker.inputs['Scale'].default_value  = 1.0
    links.new(mapping_checker.outputs['Vector'], checker.inputs['Vector'])

    # ── Pattern sub-graph ─────────────────────────────────────────────────────
    pattern_factor = _build_pattern_nodes(
        nodes, links, new_node, mapping_checker, pattern
    )

    # For SQUARE, use the checker Fac directly.
    # For DIAGONAL/DIAMOND/CIRCLE, mix A/B by pattern_factor.
    # CIRCLE always forces XOR — circles must alternate with checker squares
    # to produce the checkers-board-with-pieces look.
    # checker_invert=True: XOR pattern with checker phase — alternating squares invert.
    force_xor = (pattern == 'CIRCLE')
    if pattern_factor is not None:
        if checker_invert or force_xor:
            # XOR: add pattern_factor + checker.Fac, modulo 2, then > 0.5
            xor_add = new_node('ShaderNodeMath', -200, 350)
            xor_add.operation = 'ADD'
            links.new(pattern_factor,               xor_add.inputs[0])
            links.new(checker.outputs['Fac'],       xor_add.inputs[1])

            xor_mod = new_node('ShaderNodeMath', 0, 350)
            xor_mod.operation = 'MODULO'
            xor_mod.inputs[1].default_value = 2.0
            links.new(xor_add.outputs['Value'],     xor_mod.inputs[0])

            xor_gt = new_node('ShaderNodeMath', 200, 350)
            xor_gt.operation = 'GREATER_THAN'
            xor_gt.inputs[1].default_value = 0.5
            links.new(xor_mod.outputs['Value'],     xor_gt.inputs[0])

            mix_factor = xor_gt.outputs['Value']
        else:
            mix_factor = pattern_factor

        pat_mix = new_node('ShaderNodeMix', -100, 300)
        pat_mix.data_type  = 'RGBA'
        pat_mix.blend_type = 'MIX'
        pat_mix.inputs['A'].default_value = (*color_a_rgb, 1.0)
        pat_mix.inputs['B'].default_value = (*color_b_rgb, 1.0)
        links.new(mix_factor, pat_mix.inputs['Factor'])
        checker_color_out = pat_mix.outputs['Result']
    else:
        checker_color_out = checker.outputs['Color']

    # ── Corner marks (skipped for preview renders — composited in numpy instead) ──
    if no_corner_marks:
        # Wire checker directly to output — no corner marks
        emission = new_node('ShaderNodeEmission',       400, 100)
        output   = new_node('ShaderNodeOutputMaterial', 600, 100)
        links.new(checker_color_out,            emission.inputs['Color'])
        links.new(emission.outputs['Emission'], output.inputs['Surface'])
        return

    # Corner marker constants — all fractions of one texel tile (1.0 in tile UV space).
    # 1 - checker per channel via DIFFERENCE blend against white.
    # ShaderNodeInvert/InvertColor removed in Blender 5.1.
    invert = new_node('ShaderNodeMix', -200, 0)
    invert.data_type  = 'RGBA'
    invert.blend_type = 'DIFFERENCE'
    invert.inputs['Factor'].default_value    = 1.0
    invert.inputs['A'].default_value         = (1.0, 1.0, 1.0, 1.0)  # white

    # ── Separate XYZ from tile mapping — one period per texel tile ────────────
    sep = new_node('ShaderNodeSeparateXYZ', -500, -150)
    links.new(mapping_tile.outputs['Vector'], sep.inputs['Vector'])

    # ── Cross marker at each texel tile corner ────────────────────────────────
    # Each tile contributes an L-shape in its corner; adjacent tiles' L-shapes
    # meet to form a full + cross. Modulo 1.0 gives position within one tile.
    #
    # Vertical arm:   u within BORDER_W of tile edge, v within BORDER_L of corner
    # Horizontal arm: v within BORDER_W of tile edge, u within BORDER_L of corner

    mod_u = new_node('ShaderNodeMath', -300, -100)
    mod_u.operation = 'MODULO'
    mod_u.inputs[1].default_value = 1.0
    links.new(sep.outputs['X'], mod_u.inputs[0])

    mod_v = new_node('ShaderNodeMath', -300, -280)
    mod_v.operation = 'MODULO'
    mod_v.inputs[1].default_value = 1.0
    links.new(sep.outputs['Y'], mod_v.inputs[0])

    # U near tile edge (vertical arm width)
    lt_u = new_node('ShaderNodeMath', -100, -50)
    lt_u.operation = 'LESS_THAN'
    lt_u.inputs[1].default_value = BORDER_W
    links.new(mod_u.outputs['Value'], lt_u.inputs[0])

    gt_u = new_node('ShaderNodeMath', -100, -150)
    gt_u.operation = 'GREATER_THAN'
    gt_u.inputs[1].default_value = 1.0 - BORDER_W
    links.new(mod_u.outputs['Value'], gt_u.inputs[0])

    u_edge = new_node('ShaderNodeMath', 100, -100)
    u_edge.operation = 'MAXIMUM'
    links.new(lt_u.outputs['Value'], u_edge.inputs[0])
    links.new(gt_u.outputs['Value'], u_edge.inputs[1])

    # V near tile edge (horizontal arm width)
    lt_v = new_node('ShaderNodeMath', -100, -230)
    lt_v.operation = 'LESS_THAN'
    lt_v.inputs[1].default_value = BORDER_W
    links.new(mod_v.outputs['Value'], lt_v.inputs[0])

    gt_v = new_node('ShaderNodeMath', -100, -330)
    gt_v.operation = 'GREATER_THAN'
    gt_v.inputs[1].default_value = 1.0 - BORDER_W
    links.new(mod_v.outputs['Value'], gt_v.inputs[0])

    v_edge = new_node('ShaderNodeMath', 100, -280)
    v_edge.operation = 'MAXIMUM'
    links.new(lt_v.outputs['Value'], v_edge.inputs[0])
    links.new(gt_v.outputs['Value'], v_edge.inputs[1])

    # V within arm length of corner (vertical arm extent)
    lt_vl = new_node('ShaderNodeMath', -100, -430)
    lt_vl.operation = 'LESS_THAN'
    lt_vl.inputs[1].default_value = BORDER_L
    links.new(mod_v.outputs['Value'], lt_vl.inputs[0])

    gt_vl = new_node('ShaderNodeMath', -100, -530)
    gt_vl.operation = 'GREATER_THAN'
    gt_vl.inputs[1].default_value = 1.0 - BORDER_L
    links.new(mod_v.outputs['Value'], gt_vl.inputs[0])

    v_len = new_node('ShaderNodeMath', 100, -480)
    v_len.operation = 'MAXIMUM'
    links.new(lt_vl.outputs['Value'], v_len.inputs[0])
    links.new(gt_vl.outputs['Value'], v_len.inputs[1])

    # U within arm length of corner (horizontal arm extent)
    lt_ul = new_node('ShaderNodeMath', -100, -630)
    lt_ul.operation = 'LESS_THAN'
    lt_ul.inputs[1].default_value = BORDER_L
    links.new(mod_u.outputs['Value'], lt_ul.inputs[0])

    gt_ul = new_node('ShaderNodeMath', -100, -730)
    gt_ul.operation = 'GREATER_THAN'
    gt_ul.inputs[1].default_value = 1.0 - BORDER_L
    links.new(mod_u.outputs['Value'], gt_ul.inputs[0])

    u_len = new_node('ShaderNodeMath', 100, -680)
    u_len.operation = 'MAXIMUM'
    links.new(lt_ul.outputs['Value'], u_len.inputs[0])
    links.new(gt_ul.outputs['Value'], u_len.inputs[1])

    # Vertical arm:   u near edge AND v within arm length
    v_arm = new_node('ShaderNodeMath', 300, -280)
    v_arm.operation = 'MINIMUM'
    links.new(u_edge.outputs['Value'], v_arm.inputs[0])
    links.new(v_len.outputs['Value'],  v_arm.inputs[1])

    # Horizontal arm: v near edge AND u within arm length
    h_arm = new_node('ShaderNodeMath', 300, -480)
    h_arm.operation = 'MINIMUM'
    links.new(v_edge.outputs['Value'], h_arm.inputs[0])
    links.new(u_len.outputs['Value'],  h_arm.inputs[1])

    # Cross = vertical arm OR horizontal arm
    cross_mask = new_node('ShaderNodeMath', 500, -380)
    cross_mask.operation = 'MAXIMUM'
    links.new(v_arm.outputs['Value'], cross_mask.inputs[0])
    links.new(h_arm.outputs['Value'], cross_mask.inputs[1])

    if show_circle:
        # ── Quarter-circle SDF at each tile corner ────────────────────────────
        # For each corner (cu, cv) in {0,1}² compute distance from that corner
        # in tile UV space (modulo 1.0). A ring fires where:
        #   abs(distance - CIRCLE_R) < BORDER_W * 0.5
        # OR all four corners together to form a full circle at the corner.
        #
        # Node layout: x=600..900 to the right of the cross nodes.
        circle_masks = []
        corner_coords = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
        x_base = 600
        for ci, (cu, cv) in enumerate(corner_coords):
            yo = -200 * ci  # vertical offset per corner group

            # du = mod_u - cu
            du = new_node('ShaderNodeMath', x_base, yo)
            du.operation = 'SUBTRACT'
            du.inputs[1].default_value = cu
            links.new(mod_u.outputs['Value'], du.inputs[0])

            # dv = mod_v - cv
            dv = new_node('ShaderNodeMath', x_base, yo - 80)
            dv.operation = 'SUBTRACT'
            dv.inputs[1].default_value = cv
            links.new(mod_v.outputs['Value'], dv.inputs[0])

            # du² and dv²
            du2 = new_node('ShaderNodeMath', x_base + 160, yo)
            du2.operation = 'MULTIPLY'
            links.new(du.outputs['Value'], du2.inputs[0])
            links.new(du.outputs['Value'], du2.inputs[1])

            dv2 = new_node('ShaderNodeMath', x_base + 160, yo - 80)
            dv2.operation = 'MULTIPLY'
            links.new(dv.outputs['Value'], dv2.inputs[0])
            links.new(dv.outputs['Value'], dv2.inputs[1])

            # d² = du² + dv²
            d2 = new_node('ShaderNodeMath', x_base + 320, yo - 40)
            d2.operation = 'ADD'
            links.new(du2.outputs['Value'], d2.inputs[0])
            links.new(dv2.outputs['Value'], d2.inputs[1])

            # d = sqrt(d²)
            d = new_node('ShaderNodeMath', x_base + 480, yo - 40)
            d.operation = 'SQRT'
            links.new(d2.outputs['Value'], d.inputs[0])

            # abs(d - CIRCLE_R) via SUBTRACT then ABSOLUTE
            d_sub_r = new_node('ShaderNodeMath', x_base + 640, yo - 40)
            d_sub_r.operation = 'SUBTRACT'
            d_sub_r.inputs[1].default_value = CIRCLE_R
            links.new(d.outputs['Value'], d_sub_r.inputs[0])

            d_abs = new_node('ShaderNodeMath', x_base + 800, yo - 40)
            d_abs.operation = 'ABSOLUTE'
            links.new(d_sub_r.outputs['Value'], d_abs.inputs[0])

            # ring fires where abs(d - r) < half line width
            ring = new_node('ShaderNodeMath', x_base + 960, yo - 40)
            ring.operation = 'LESS_THAN'
            ring.inputs[1].default_value = BORDER_W * 0.5
            links.new(d_abs.outputs['Value'], ring.inputs[0])

            circle_masks.append(ring)

        # OR all four corner rings together
        def or_pair(a, b, x, y):
            n = new_node('ShaderNodeMath', x, y)
            n.operation = 'MAXIMUM'
            links.new(a.outputs['Value'], n.inputs[0])
            links.new(b.outputs['Value'], n.inputs[1])
            return n

        c01  = or_pair(circle_masks[0], circle_masks[1], x_base + 1120, -100)
        c23  = or_pair(circle_masks[2], circle_masks[3], x_base + 1120, -400)
        circ = or_pair(c01, c23,                          x_base + 1280, -250)

        # Final mask = cross OR circle
        edge_mask = new_node('ShaderNodeMath', x_base + 1440, -380)
        edge_mask.operation = 'MAXIMUM'
        links.new(cross_mask.outputs['Value'], edge_mask.inputs[0])
        links.new(circ.outputs['Value'],       edge_mask.inputs[1])
    else:
        edge_mask = cross_mask

    # ── Wire invert from checker, mix against combined mask ─────────────────
    # Gamma-correct invert: x^0.4545 (linear→sRGB) → DIFFERENCE → x^2.2 (sRGB→linear).
    # Matches Paint.net invert perceptually — punchy, saturated marks.
    x_mix = 700 + (1600 if show_circle else 0)

    # Step 1: linearise to sRGB before invert
    gamma_in = new_node('ShaderNodeGamma', x_mix - 400, 0)
    gamma_in.inputs['Gamma'].default_value = 0.4545
    links.new(checker_color_out, gamma_in.inputs['Color'])

    # Step 2: invert in sRGB space
    links.new(gamma_in.outputs['Color'], invert.inputs['B'])

    # Step 3: re-apply gamma to bring back to linear
    gamma_out = new_node('ShaderNodeGamma', x_mix - 200, 0)
    gamma_out.inputs['Gamma'].default_value = 2.2
    links.new(invert.outputs['Result'], gamma_out.inputs['Color'])

    mix = new_node('ShaderNodeMix', x_mix, 100)
    mix.data_type  = 'RGBA'
    mix.blend_type = 'MIX'
    mix.inputs['Factor'].default_value = 0.0
    links.new(edge_mask.outputs['Value'],  mix.inputs['Factor'])
    links.new(checker_color_out,           mix.inputs['A'])
    links.new(gamma_out.outputs['Color'],  mix.inputs['B'])

    # ── Output ────────────────────────────────────────────────────────────────
    # Position emission and output to the right of mix, wherever it landed
    mix_x    = mix.location.x
    emission = new_node('ShaderNodeEmission',       mix_x + 200, 100)
    output   = new_node('ShaderNodeOutputMaterial', mix_x + 400, 100)
    links.new(mix.outputs['Result'],        emission.inputs['Color'])
    links.new(emission.outputs['Emission'], output.inputs['Surface'])


def _get_chain_checker(mat):
    """Return the TEX_CHECKER node from a chain material, or None."""
    if not mat or not mat.use_nodes:
        return None
    return next((n for n in mat.node_tree.nodes if n.type == 'TEX_CHECKER'), None)

def _read_chain_color_a(mat):
    """Read colour A (Color1) from a chain material's checker node. Returns RGB tuple or None."""
    checker = _get_chain_checker(mat)
    if not checker:
        return None
    rgba = checker.inputs['Color1'].default_value
    return (rgba[0], rgba[1], rgba[2])

def _read_chain_color_b(mat):
    """Read colour B (Color2) from a chain material's checker node. Returns RGB tuple or None."""
    checker = _get_chain_checker(mat)
    if not checker:
        return None
    rgba = checker.inputs['Color2'].default_value
    return (rgba[0], rgba[1], rgba[2])

def _write_chain_colors(mat, color_a, color_b):
    """Write both colours to a chain material's checker node in place."""
    checker = _get_chain_checker(mat)
    if not checker:
        return
    checker.inputs['Color1'].default_value = (*color_a, 1.0)
    checker.inputs['Color2'].default_value = (*color_b, 1.0)


# ─── Base material management ─────────────────────────────────────────────────

def ensure_island_materials():
    """Ensure M_FBXMT_Island marker and all 15 hidden sub-materials exist.
    Only creates missing materials — does NOT rebuild existing node trees.
    Node tree updates happen in rebuild_fbxmt_materials().
    """
    created = []
    prefs   = _get_prefs()
    if prefs and hasattr(prefs, 'color_wall_a'):
        col_a = tuple(prefs.color_wall_a[:3])
    else:
        col_a = tuple(FBXMT_MATERIALS['M_FBXMT_Wall'][:3])

    # Visible marker — only create if missing
    if ISLAND_MARKER_NAME not in bpy.data.materials:
        mat = bpy.data.materials.new(name=ISLAND_MARKER_NAME)
        _build_checker_node_tree(mat, col_a, (0.5, 0.5, 0.5))
        created.append(ISLAND_MARKER_NAME)

    # Hidden sub-materials — only create missing ones
    def _get_col(prop):
        if prefs and hasattr(prefs, prop):
            return tuple(getattr(prefs, prop)[:3])
        return col_a

    _group_cols = [
        _get_col('color_floor_a'),
        _get_col('color_ceiling_a'),
        col_a,
    ]
    _offsets = [-0.4, -0.2, 0.0, 0.2, 0.4]

    for i, name in enumerate(ISLAND_SUB_NAMES):
        if name not in bpy.data.materials:
            mat      = bpy.data.materials.new(name=name)
            group    = i % 3
            slot     = i // 3
            parent_a = _group_cols[group]
            h, l, s  = colorsys.rgb_to_hls(*parent_a)
            hue_b    = (h + 0.5) % 1.0
            off      = _offsets[slot]
            sub_b    = colorsys.hls_to_rgb(hue_b, max(0.15, min(0.85, 0.5 + off)), max(0.6, s))
            _build_checker_node_tree(mat, parent_a, sub_b, checker_invert=True)
            created.append(name)

    # Ramp island sub-materials — 3 slots, parent is Ramp Floor A
    ramp_a = _get_col('color_ramp_floor_a')
    if ramp_a == col_a:  # fallback if prop missing
        ramp_a = tuple(FBXMT_MATERIALS['M_FBXMT_Ramp_Floor'][:3])
    ramp_offsets = [-0.25, 0.0, 0.25]
    for i, name in enumerate(RAMP_ISLAND_NAMES):
        if name not in bpy.data.materials:
            mat   = bpy.data.materials.new(name=name)
            h, l, s = colorsys.rgb_to_hls(*ramp_a)
            hue_b = (h + 0.5) % 1.0
            sub_b = colorsys.hls_to_rgb(hue_b, max(0.15, min(0.85, 0.5 + ramp_offsets[i])), max(0.6, s))
            _build_checker_node_tree(mat, ramp_a, sub_b, checker_invert=True)
            created.append(name)
    return created


# Legacy shim — called by load_post handler which previously ensured chain materials
def ensure_chain_materials():
    return ensure_island_materials()


def _derive_colours_from_anchor(prefs):
    """Populate all colour props on prefs from anchor_hue, anchor_saturation, anchor_value.

    Derivation:
      Wall A    = HSL(H,        S, V)
      Floor A   = HSL(H+120,    S, V)
      Ceiling A = HSL(H+240,    S, V)
      Trim A    = HSL(H+270,    S, V)
      Ignore A  = 25% grey  (hue-independent)
      Ignore B  = 75% grey  (hue-independent)
    B colours derived via color_b_hue_offset / color_b_saturation / color_b_value.
    """
    if not prefs:
        print('[FBXMT] _derive_colours_from_anchor: prefs is None — aborting')
        return

    h_base    = float(prefs.anchor_hue) % 1.0
    s_a       = _safe_float(prefs, 'anchor_saturation', 0.6)
    v_a       = _safe_float(prefs, 'anchor_value', 0.50)
    b_offset  = getattr(prefs, 'color_b_hue_offset',  '0')
    b_sat     = _safe_float(prefs, 'color_b_saturation', 0.6)
    b_val     = _safe_float(prefs, 'color_b_value', 0.35)

    def _hue_col(offset_norm):
        h = (h_base + offset_norm) % 1.0
        r, g, b = colorsys.hls_to_rgb(h, v_a, s_a)
        return (r, g, b, 1.0)

    def _derive_b(col_a_rgb):
        return (*_resolve_color_b(col_a_rgb, b_offset, b_sat, b_val), 1.0)

    # A colours — offsets as fractions of the colour wheel
    wall_a    = _hue_col(0.0)
    floor_a   = _hue_col(120.0 / 360.0)
    ceiling_a = _hue_col(240.0 / 360.0)
    trim_a    = _hue_col(270.0 / 360.0)

    prefs.color_wall_a    = wall_a
    prefs.color_floor_a   = floor_a
    prefs.color_ceiling_a = ceiling_a
    prefs.color_trim_a    = trim_a
    prefs.color_ignore_a  = (0.25, 0.25, 0.25, 1.0)
    # B colours are no longer stored — always derived fresh via _resolve_color_b


def ensure_fbxmt_materials():
    # Clean up any orphaned preview materials from interrupted bake sessions
    for mat in list(bpy.data.materials):
        if mat.name.startswith('__fbxmt_preview_'):
            bpy.data.materials.remove(mat)
    created = []
    for name, colour in FBXMT_MATERIALS.items():
        if name not in bpy.data.materials:
            mat = bpy.data.materials.new(name=name)
            setup_material_nodes(mat, colour)
            created.append(name)
    created += ensure_island_materials()
    return created


# Set True after the first successful rebuild this session — gates the
# automatic rebuild on import so it only runs once. Reset to False by any
# deliberate user-triggered rebuild so the next import refreshes correctly.
_materials_built = False


def _read_mat_settings(slot):
    """Return (color_a, color_b, pattern) for a named slot from scene prefs."""
    prefs = _get_prefs()
    if not prefs:
        return None, None, 'SQUARE'
    col_a    = tuple(getattr(prefs, f'color_{slot}_a', (0.5,)*4)[:3])
    b_offset = getattr(prefs, 'color_b_hue_offset',  '0')
    b_sat    = _safe_float(prefs, 'color_b_saturation', 0.6)
    b_val    = _safe_float(prefs, 'color_b_value', 0.35)
    pattern  = getattr(prefs, f'checker_pattern_{slot}', 'SQUARE')
    col_b    = _resolve_color_b(col_a, b_offset, b_sat, b_val)
    return col_a, col_b, pattern


def rebuild_fbxmt_materials():
    """Rebuild base material node trees and all 5 chain materials.
    Reads checker colours, pattern, and colour-B mode from addon preferences.
    """
    global _materials_built
    prefs = _get_prefs()

    # Always re-derive colour_*_a props from anchor notches before reading them.
    # This ensures rebuild is consistent after file reload regardless of what
    # was cached in color_*_a at save time.
    _derive_colours_from_anchor(prefs)

    # Clear island sub-material node trees to prevent stale cached colours
    for name in ISLAND_SUB_NAMES:
        mat = bpy.data.materials.get(name)
        if mat and mat.use_nodes:
            mat.node_tree.nodes.clear()

    # Slot key → material name mapping
    _SLOT_TO_MAT = {
        'floor':        'M_FBXMT_Floor',
        'ceiling':      'M_FBXMT_Ceiling',
        'wall':         'M_FBXMT_Wall',
        'trim':         'M_FBXMT_Trim',
        'ignore':       'M_FBXMT_Ignore',
        'ramp_floor':   'M_FBXMT_Ramp_Floor',
        'ramp_ceiling': 'M_FBXMT_Ramp_Ceiling',
    }
    rebuilt = []

    for slot, mat_name in _SLOT_TO_MAT.items():
        try:
            mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(name=mat_name)
            if slot == 'ignore':
                # Ignore is always hue-independent grey — never follows the anchor
                col_a   = (0.25, 0.25, 0.25)
                col_b   = (0.10, 0.10, 0.10)
                pattern = getattr(prefs, 'checker_pattern_ignore', 'SQUARE') if prefs else 'SQUARE'
            else:
                col_a, col_b, pattern = _read_mat_settings(slot)
                if col_a is None:
                    default = FBXMT_MATERIALS[mat_name]
                    r, g, b = default[:3]
                    col_a, col_b, pattern = (r,g,b), (r*.7, g*.7, b*.7), 'SQUARE'
            setup_material_nodes(mat, col_a, color_b=col_b, pattern=pattern)
            rebuilt.append(mat_name)
        except Exception:
            pass

    # Rebuild island marker and all 15 hidden sub-materials.
    # Sub-materials share Colour A from prefs with the marker,
    # but keep their evenly-spaced grey B values unchanged.
    try:
        # Island Colour A always tracks Wall Colour A — island faces are
        # wall-type surfaces and should read identically in the viewport.
        col_a_island, _, _ = _read_mat_settings('wall')
        if col_a_island is None:
            col_a_island = tuple(FBXMT_MATERIALS['M_FBXMT_Wall'][:3])

        # Override sat/val with island-specific controls while keeping anchor hue
        import colorsys
        h, _l, _s = colorsys.rgb_to_hls(*col_a_island[:3])
        isl_sat = _safe_float(prefs, 'island_marker_saturation', 0.6)
        isl_val = _safe_float(prefs, 'island_marker_value', 0.50)
        col_a_island = colorsys.hls_to_rgb(h, isl_val, isl_sat)
        _, _, pattern_island = _read_mat_settings('island')
        if pattern_island is None:
            pattern_island = 'SQUARE'

        # Visible marker — B derived from island-specific b controls
        isl_b_offset = getattr(prefs, 'island_marker_b_hue_offset', '0')
        isl_b_sat    = _safe_float(prefs, 'island_marker_b_saturation', 0.6)
        isl_b_val    = _safe_float(prefs, 'island_marker_b_value', 0.35)
        col_b_island = _resolve_color_b(col_a_island, isl_b_offset, isl_b_sat, isl_b_val)
        marker = bpy.data.materials.get(ISLAND_MARKER_NAME) or bpy.data.materials.new(name=ISLAND_MARKER_NAME)
        _build_checker_node_tree(marker, col_a_island, col_b_island, pattern=pattern_island)
        rebuilt.append(ISLAND_MARKER_NAME)

        # Hidden sub-materials — Wall_01-05, Floor_01-05, Ceil_01-05
        # Island A = parent's colour B hue, with island sat/val controls
        # Island B = derived from Island A using island B modifiers
        # Read parent B colours using global B modifiers — most authoritative source
        b_offset = getattr(prefs, 'color_b_hue_offset',  '0')
        b_sat    = _safe_float(prefs, 'color_b_saturation', 0.6)
        b_val    = _safe_float(prefs, 'color_b_value', 0.35)

        def _get_parent_b(slot):
            col_a = tuple(getattr(prefs, f'color_{slot}_a', (0.5,)*4)[:3])
            return _resolve_color_b(col_a, b_offset, b_sat, b_val)

        _parent_b_cols = [
            _get_parent_b('wall'),    # group 0: Wall_xx  (i%3==0)
            _get_parent_b('floor'),   # group 1: Floor_xx (i%3==1)
            _get_parent_b('ceiling'), # group 2: Ceil_xx  (i%3==2)
        ]

        # Value steps spread across the 5 slots within each group
        # Slot 0 = darkest, slot 4 = lightest — evenly spaced around isl_val
        _step_offsets = [-0.20, -0.10, 0.0, +0.10, +0.20]

        for i, name in enumerate(ISLAND_SUB_NAMES):
            mat      = bpy.data.materials.get(name) or bpy.data.materials.new(name=name)
            group    = i % 3       # Wall(0), Floor(1), Ceil(2)
            slot     = i // 3      # 0-4 within each group
            parent_b = _parent_b_cols[group]

            # Island A = parent B hue with island sat/val + per-slot value step
            h, _l, _s = colorsys.rgb_to_hls(*parent_b)
            stepped_val = max(0.05, min(0.95, isl_val + _step_offsets[slot]))
            island_a = colorsys.hls_to_rgb(h, stepped_val, isl_sat)

            # Island B = derived from Island A via island B modifiers
            island_b = _resolve_color_b(island_a, isl_b_offset, isl_b_sat, isl_b_val)

            _build_checker_node_tree(mat, island_a, island_b, pattern=pattern_island, checker_invert=True)
            rebuilt.append(name)

        # Ramp island sub-materials — 3 slots, parent is Ramp Floor A
        col_a_ramp, _, _ = _read_mat_settings('ramp_floor')
        if col_a_ramp is None:
            col_a_ramp = tuple(FBXMT_MATERIALS['M_FBXMT_Ramp_Floor'][:3])
        for i, name in enumerate(RAMP_ISLAND_NAMES):
            mat      = bpy.data.materials.get(name) or bpy.data.materials.new(name=name)
            h, l, s  = colorsys.rgb_to_hls(*col_a_ramp)
            island_a = colorsys.hls_to_rgb(h, isl_val, isl_sat)
            island_b = _resolve_color_b(island_a, isl_b_offset, isl_b_sat, isl_b_val)
            _build_checker_node_tree(mat, island_a, island_b, pattern=pattern_island, checker_invert=True)
            rebuilt.append(name)
    except Exception as e:
        print(f'[FBXMT] Island rebuild failed: {type(e).__name__}: {e}')

    # Force redraw of all UI regions so tile images update in the N-panel list
    # without needing to open the setup window
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                for region in area.regions:
                    region.tag_redraw()
    except Exception:
        pass

    _materials_built = True
    return rebuilt


def ensure_collections():
    scene = bpy.context.scene
    cols = {}
    for name in (COLLECTION_GEO, COLLECTION_PROPS, COLLECTION_TRIM):
        if name not in bpy.data.collections:
            col = bpy.data.collections.new(name)
            scene.collection.children.link(col)
        cols[name] = bpy.data.collections[name]
    return cols


def move_to_collection(obj, collection_name):
    """Move obj to the named collection, creating and linking it to the scene if needed."""
    target = bpy.data.collections.get(collection_name)
    if not target:
        target = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(target)
    # Ensure the collection is actually in the scene (may exist but be unlinked)
    def _is_in_scene(col, scene_col):
        if col.name == scene_col.name:
            return True
        for child in scene_col.children_recursive if hasattr(scene_col, 'children_recursive') else []:
            if child.name == col.name:
                return True
        # Simple check via all scene collections
        return col.name in [c.name for c in bpy.context.scene.collection.children_recursive]
    if not _is_in_scene(target, bpy.context.scene.collection):
        bpy.context.scene.collection.children.link(target)
    for col in list(obj.users_collection):
        col.objects.unlink(obj)
    target.objects.link(obj)


def get_trim_room_names():
    """Return sorted list of room names that are direct children of the Trim collection."""
    trim = bpy.data.collections.get(COLLECTION_TRIM)
    if not trim:
        return []
    return sorted(c.name for c in trim.children)


def get_room_collection(room_name, categorised=False, category='Coving'):
    """Find or create Trim/room_name and optionally Trim/room_name/category.

    Returns the target collection the object should be placed in.
    """
    scene = bpy.context.scene

    # Ensure Trim root exists
    trim = bpy.data.collections.get(COLLECTION_TRIM)
    if not trim:
        trim = bpy.data.collections.new(COLLECTION_TRIM)
        scene.collection.children.link(trim)

    # Ensure room sub-collection
    room_col = bpy.data.collections.get(room_name)
    if room_col is None or room_col not in list(trim.children):
        if room_col is None:
            room_col = bpy.data.collections.new(room_name)
        if room_col.name not in [c.name for c in trim.children]:
            trim.children.link(room_col)

    if not categorised:
        return room_col

    # Ensure category sub-collection under room
    cat_name   = f'{room_name}.{category}'
    cat_col    = bpy.data.collections.get(cat_name)
    if cat_col is None or cat_col not in list(room_col.children):
        if cat_col is None:
            cat_col = bpy.data.collections.new(cat_name)
        if cat_name not in [c.name for c in room_col.children]:
            room_col.children.link(cat_col)
    return cat_col


def add_fbxmt_slots(obj):
    ensure_fbxmt_materials()
    existing = {m.name for m in obj.data.materials if m}
    # Push base materials + visible island marker only.
    # Hidden sub-materials are added on demand by the graph colourer.
    for mat_name in list(FBXMT_MATERIALS) + [ISLAND_MARKER_NAME]:
        mat = bpy.data.materials.get(mat_name)
        if mat and mat.name not in existing:
            obj.data.materials.append(mat)
            existing.add(mat.name)


def assign_trim_material(obj):
    ensure_fbxmt_materials()
    mat = bpy.data.materials.get('M_FBXMT_Trim')
    if not mat:
        return
    if 'M_FBXMT_Trim' not in [m.name for m in obj.data.materials if m]:
        obj.data.materials.append(mat)
    slot_index = next(
        (i for i, m in enumerate(obj.data.materials) if m and m.name == 'M_FBXMT_Trim'),
        None,
    )
    if slot_index is None:
        return
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    for face in bm.faces:
        face.material_index = slot_index
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


# ─── Sanity check helpers ─────────────────────────────────────────────────────

def check_naked_faces(objects):
    """Return dict of {obj_name: naked_face_count} for mesh objects that have
    faces with no material assigned (material_index out of range or slot is None).
    Only reports objects where ALL faces have materials — if any face is naked,
    the whole object is flagged.
    """
    problems = {}
    for obj in objects:
        if obj.type != 'MESH':
            continue
        mesh = obj.data
        slot_count = len(mesh.materials)
        naked = sum(
            1 for face in mesh.polygons
            if face.material_index >= slot_count
            or mesh.materials[face.material_index] is None
        )
        if naked:
            problems[obj.name] = naked
    return problems


class OT_FBXMT_Check_Mesh(Operator):
    """Check selected mesh objects for faces with no material assigned."""
    bl_idname  = 'fbxmt.check_mesh'
    bl_label   = 'Check Mesh'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == 'OBJECT'
            and any(obj.type == 'MESH' for obj in context.selected_objects)
        )

    def execute(self, context):
        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        problems     = check_naked_faces(mesh_objects)

        if not problems:
            self.report({'INFO'}, f"All {len(mesh_objects)} selected mesh(es) clean — no naked faces")
            return {'FINISHED'}

        # Select all offending objects, stay in Object mode
        bpy.ops.object.select_all(action='DESELECT')
        for name in problems:
            obj = bpy.data.objects.get(name)
            if obj:
                obj.select_set(True)
        if problems:
            first = bpy.data.objects.get(next(iter(problems)))
            if first:
                context.view_layer.objects.active = first

        names  = ', '.join(list(problems.keys())[:5])
        suffix = f' (+{len(problems)-5} more)' if len(problems) > 5 else ''
        total  = sum(problems.values())
        self.report(
            {'WARNING'},
            f"Naked faces found: {total} face(s) across {len(problems)} object(s): {names}{suffix}"
        )
        return {'FINISHED'}


# ─── Operators: scene setup ───────────────────────────────────────────────────

class OT_FBXMT_Scene_Setup(Operator):
    bl_idname = 'fbxmt.scene_setup'
    bl_label = 'Setup Scene'
    bl_description = 'Create Geo, Props and Trim collections and add M_FBXMT materials to the scene'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ensure_collections()
        created = ensure_fbxmt_materials()
        # geo_texel_density uses its own property default — no seed needed
        msg = 'Scene ready - Geo, Props, Trim collections created'
        if created:
            msg += f' - {len(created)} material(s) added'
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ─── Operators: base materials ────────────────────────────────────────────────

class OT_FBXMT_Add_Materials(Operator):
    bl_idname = 'fbxmt.add_materials'
    bl_label = 'Add Materials'
    bl_description = 'Add M_FBXMT materials to this blend file. Existing materials are left untouched.'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        created = ensure_fbxmt_materials()
        if created:
            self.report({'INFO'}, f'Created: {", ".join(created)}')
        else:
            self.report({'INFO'}, 'All M_FBXMT materials already present')
        return {'FINISHED'}


class OT_FBXMT_Rebuild_Materials(Operator):
    bl_idname = 'fbxmt.rebuild_materials'
    bl_label = 'Rebuild Materials'
    bl_description = (
        'Rebuild node trees on all M_FBXMT materials. '
        'Pushes all chain materials to every mesh object in the scene.'
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        global _materials_built
        _materials_built = False
        rebuilt = rebuild_fbxmt_materials()
        self.report({'INFO'}, f'Rebuilt: {", ".join(rebuilt)}')
        # Regenerate __tile_* preview images so the N-panel UIList stays current
        try:
            with context.temp_override(window=context.window, scene=context.scene):
                bpy.ops.fbxmt.bake_all_modal('INVOKE_DEFAULT')
        except Exception as e:
            print(f'[FBXMT] tile rebuild after Rebuild failed: {e}')
        return {'FINISHED'}


class OT_FBXMT_Assign_Materials(Operator):
    bl_idname = 'fbxmt.assign_materials'
    bl_label = 'Auto-Assign Materials'
    bl_description = (
        'Assign M_FBXMT materials to faces based on world-space normal. '
        'Skips faces already assigned M_FBXMT_Ignore or any chain material. '
        'Object mode: all faces. Edit mode: selected faces only.'
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode == 'OBJECT':
            return any(obj.type == 'MESH' for obj in context.selected_objects)
        if context.mode == 'EDIT_MESH':
            return context.active_object and context.active_object.type == 'MESH'
        return False

    def execute(self, context):
        global _suppress_handler
        _suppress_handler = True
        try:
            ensure_fbxmt_materials()

            props               = context.scene.fbxmt_props
            floor_threshold_dot = math.cos(math.radians(props.ramp_wall_threshold))
            floor_ramp_threshold_dot  = math.cos(math.radians(props.floor_ramp_threshold))
            z_axis              = Vector((0.0, 0.0, 1.0))
            edit_mode           = context.mode == 'EDIT_MESH'

            if edit_mode:
                bpy.ops.object.mode_set(mode='OBJECT')
                objects = [context.active_object]
            else:
                objects = [obj for obj in context.selected_objects if obj.type == 'MESH']

            total = 0

            for obj in objects:
                mesh         = obj.data
                world_matrix = obj.matrix_world

                # Append any missing base material slots BEFORE loading the bmesh.
                # slot_index is built after appending so indices are stable and
                # consistent with what bmesh.from_mesh will see.
                for mat_name in FBXMT_MATERIALS:
                    mat = bpy.data.materials.get(mat_name)
                    if mat and mat_name not in {m.name for m in mesh.materials if m}:
                        mesh.materials.append(mat)

                # slot_index must be built AFTER all appends — bmesh inherits the
                # same indices as mesh.materials at the time from_mesh is called.
                slot_index = {m.name: i for i, m in enumerate(mesh.materials) if m}

                bm = bmesh.new()
                bm.from_mesh(mesh)
                bm.faces.ensure_lookup_table()

                for face in bm.faces:
                    if edit_mode and not face.select:
                        continue
                    current_mat = (
                        mesh.materials[face.material_index]
                        if face.material_index < len(mesh.materials) else None
                    )
                    # Skip faces already marked as ignore or any chain material
                    if current_mat:
                        if current_mat.name == FBXMT_IGNORE_MATERIAL:
                            continue
                        if _is_chain_material(current_mat):
                            continue

                    world_normal = (world_matrix.to_3x3() @ face.normal).normalized()
                    dot_z        = abs(world_normal.dot(z_axis))

                    if dot_z >= floor_ramp_threshold_dot:
                        mat_name = 'M_FBXMT_Floor' if world_normal.z > 0 else 'M_FBXMT_Ceiling'
                    elif dot_z >= floor_threshold_dot:
                        mat_name = 'M_FBXMT_Ramp_Floor' if world_normal.z > 0 else 'M_FBXMT_Ramp_Ceiling'
                    else:
                        mat_name = 'M_FBXMT_Wall'

                    if mat_name in slot_index:
                        face.material_index = slot_index[mat_name]
                        total += 1

                bm.to_mesh(mesh)
                bm.free()
                mesh.update()

            if edit_mode:
                bpy.ops.object.mode_set(mode='EDIT')

            self.report({'INFO'}, f'Assigned M_FBXMT materials to {total} face(s)')
            return {'FINISHED'}
        finally:
            _suppress_handler = False


# ─── Operators: chain materials ───────────────────────────────────────────────

# ─── Material display helpers ─────────────────────────────────────────────────

# Human-readable aliases for base material internal names.
_BASE_MAT_ALIASES = {
    'M_FBXMT_Floor':        'Floor',
    'M_FBXMT_Ceiling':      'Ceiling',
    'M_FBXMT_Wall':         'Wall',
    'M_FBXMT_Trim':         'Trim',
    'M_FBXMT_Ignore':       'Ignore',
    'M_FBXMT_Ramp_Floor':   'Ramp Floor',
    'M_FBXMT_Ramp_Ceiling': 'Ramp Ceiling',
}

def _island_alias(mat_name):
    """Return 'Island NN' for M_FBXMT_Chain_NN, else the raw name."""
    idx = _chain_index(mat_name)
    return f'Island {idx:02d}' if idx is not None else mat_name


# ─── UIList: base materials (object-scoped) ───────────────────────────────────

class FBXMT_UL_BaseMaterials(bpy.types.UIList):
    bl_idname = 'FBXMT_UL_base_materials'

    def draw_item(self, _ctx, layout, _data, item, _icon, _adata, _aprop, _index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            alias = _BASE_MAT_ALIASES.get(item.name, item.name)
            row.label(text=alias, icon_value=item.preview.icon_id)
        else:
            layout.alignment = 'CENTER'
            layout.label(text='', icon_value=item.preview.icon_id)

    def filter_items(self, _ctx, data, prop_name):
        all_mats = getattr(data, prop_name)
        flags = [
            self.bitflag_filter_item if (m and m.name in _BASE_MAT_ALIASES) else 0
            for m in all_mats
        ]
        return flags, list(range(len(all_mats)))


# ─── UIList: all FBXMT materials — fixed canonical order ─────────────────────

# Canonical display order and aliases for the unified list
_ALL_MAT_DISPLAY = {
    'M_FBXMT_Floor':        'Floor',
    'M_FBXMT_Ceiling':      'Ceiling',
    'M_FBXMT_Wall':         'Wall',
    'M_FBXMT_Trim':         'Trim',
    'M_FBXMT_Ignore':       'Ignore',
    'M_FBXMT_Island':       'Island Marker',
    'M_FBXMT_Ramp_Floor':   'Ramp Floor',
    'M_FBXMT_Ramp_Ceiling': 'Ramp Ceiling',
}
# Hidden sub-materials are intentionally absent from _ALL_MAT_DISPLAY
# so they never appear in the panel UIList.
_ALL_MAT_ORDER = list(_ALL_MAT_DISPLAY.keys())


class FBXMT_UL_AllMaterials(bpy.types.UIList):
    """Unified material list showing all 10 FBXMT materials in fixed canonical order."""
    bl_idname = 'FBXMT_UL_all_materials'

    def draw_item(self, _ctx, layout, _data, item, icon, _adata, _aprop, _index):
        alias    = _ALL_MAT_DISPLAY.get(item.name, item.name)
        tile_img = bpy.data.images.get(f'__tile_{item.name}')
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            if tile_img:
                tile_img.preview_ensure()
                layout.label(text=alias, icon_value=tile_img.preview.icon_id)
            else:
                layout.label(text=alias, icon='MATERIAL')
        else:
            layout.alignment = 'CENTER'
            if tile_img:
                tile_img.preview_ensure()
                layout.label(text='', icon_value=tile_img.preview.icon_id)
            else:
                layout.label(text='', icon='MATERIAL')

    def filter_items(self, _ctx, data, prop_name):
        all_mats = getattr(data, prop_name)
        # Show only FBXMT materials, sorted in canonical order
        order_map = {name: i for i, name in enumerate(_ALL_MAT_ORDER)}
        flags = []
        order = []
        for i, m in enumerate(all_mats):
            if m and m.name in order_map:
                flags.append(self.bitflag_filter_item)
                order.append(order_map[m.name])
            else:
                flags.append(0)
                order.append(i)
        return flags, order


# ─── UIList: island/chain materials (object-scoped) ───────────────────────────

class FBXMT_UL_ChainMaterials(bpy.types.UIList):
    bl_idname = 'FBXMT_UL_chain_materials'

    def draw_item(self, _ctx, layout, _data, item, _icon, _adata, _aprop, _index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            alias = _island_alias(item.name) if item else item.name
            row.label(text=alias, icon_value=item.preview.icon_id)
        else:
            layout.alignment = 'CENTER'
            layout.label(text='', icon_value=item.preview.icon_id)

    def filter_items(self, _ctx, data, prop_name):
        all_mats = getattr(data, prop_name)
        flags = [
            self.bitflag_filter_item if (m and _is_chain_material(m)) else 0
            for m in all_mats
        ]
        return flags, list(range(len(all_mats)))


# ─── Shared face operators ────────────────────────────────────────────────────

def _resolve_active_material(context):
    """Return the material currently highlighted in whichever list is active.

    Both lists are now sourced from bpy.data.materials (global pool) so the
    index refers to that pool, not the active object's material slots.

    fbxmt_active_list tracks which list ('BASE' or 'ISLAND') the user last
    interacted with. fbxmt_base_selected / fbxmt_island_selected are the
    authoritative selection flags - they go False when the other list is
    clicked, giving us a true deselected state that Blender's integer index
    prop cannot express on its own (it clamps to 0, never -1).
    Returns None if no list has an active selection.
    """
    scene    = context.scene
    all_mats = list(bpy.data.materials)

    if not scene.fbxmt_base_selected:
        return None
    idx = scene.fbxmt_base_list_index
    mat = all_mats[idx] if 0 <= idx < len(all_mats) else None
    if mat and (mat.name in _BASE_MAT_ALIASES or _is_chain_material(mat) or _is_island_material(mat)):
        return mat
    return None


class OT_FBXMT_Assign_To_Faces(Operator):
    """Assign the selected material to all selected faces (Edit mode only)."""
    bl_idname  = 'fbxmt.assign_to_faces'
    bl_label   = 'Assign to Selected Faces'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode != 'EDIT_MESH':
            return False
        return _resolve_active_material(context) is not None

    def execute(self, context):
        mat = _resolve_active_material(context)
        if mat is None:
            self.report({'WARNING'}, 'No material selected in list')
            return {'CANCELLED'}

        obj  = context.active_object
        mesh = obj.data

        # Ensure the material has a slot on this object
        slot_names = [m.name for m in mesh.materials if m]
        if mat.name not in slot_names:
            mesh.materials.append(mat)
        slot_index = next(i for i, m in enumerate(mesh.materials) if m and m.name == mat.name)

        # Must switch to Object mode to edit mesh data, then back
        bpy.ops.object.mode_set(mode='OBJECT')
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()
        assigned = 0
        for face in bm.faces:
            if face.select:
                face.material_index = slot_index
                assigned += 1
        bm.to_mesh(mesh)
        bm.free()
        mesh.update()
        bpy.ops.object.mode_set(mode='EDIT')

        alias = _BASE_MAT_ALIASES.get(mat.name) or _island_alias(mat.name)
        self.report({'INFO'}, f'Assigned {alias} to {assigned} face(s)')

        # Auto-colour islands when the Island Marker is assigned
        # Deferred via timer — ensures mode switch completes before operator fires
        if mat.name == ISLAND_MARKER_NAME and assigned > 0:
            mesh.update()
            bpy.ops.object.mode_set(mode='OBJECT')
            def _deferred_colour():
                try:
                    bpy.ops.fbxmt.colour_islands('EXEC_DEFAULT')
                except Exception as e:
                    print(f'[FBXMT] Deferred colour_islands failed: {e}')
                return None
            bpy.app.timers.register(_deferred_colour, first_interval=0.05)
            bpy.ops.object.mode_set(mode='EDIT')

        return {'FINISHED'}


class OT_FBXMT_Select_By_Material(Operator):
    """Select all faces assigned the active material (Edit mode only)."""
    bl_idname  = 'fbxmt.select_by_material'
    bl_label   = 'Select Faces with Material'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode != 'EDIT_MESH':
            return False
        return _resolve_active_material(context) is not None

    def execute(self, context):
        mat = _resolve_active_material(context)
        if mat is None:
            self.report({'WARNING'}, 'No material selected in list')
            return {'CANCELLED'}

        obj  = context.active_object
        mesh = obj.data
        slot_names = [m.name if m else None for m in mesh.materials]
        if mat.name not in slot_names:
            self.report({'INFO'}, 'No faces assigned this material')
            return {'FINISHED'}
        slot_index = slot_names.index(mat.name)

        bpy.ops.object.mode_set(mode='OBJECT')
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()
        selected = 0
        for face in bm.faces:
            if face.material_index == slot_index:
                face.select = True
                selected += 1
        bm.to_mesh(mesh)
        bm.free()
        mesh.update()
        bpy.ops.object.mode_set(mode='EDIT')

        alias = _BASE_MAT_ALIASES.get(mat.name) or _island_alias(mat.name)
        self.report({'INFO'}, f'Selected {selected} face(s) with {alias}')
        return {'FINISHED'}


# ─── Operators: clear ────────────────────────────────────────────────────────


class OT_FBXMT_Clear_UVs(Operator):
    bl_idname = 'fbxmt.clear_uvs'
    bl_label = 'Clear UV Maps'
    bl_description = 'Remove all UV maps from selected objects'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        cleared = 0
        for obj in mesh_objects:
            mesh = obj.data
            while mesh.uv_layers:
                mesh.uv_layers.remove(mesh.uv_layers[0])
                cleared += 1
            mesh.update()
        self.report({'INFO'}, f'Cleared {cleared} UV map(s) from {len(mesh_objects)} object(s)')
        return {'FINISHED'}


class OT_FBXMT_Clear_Mapper_Materials(Operator):
    bl_idname = 'fbxmt.clear_mapper_materials'
    bl_label = 'Clear Mapper Materials'
    bl_description = 'Remove M_FBXMT_* material slots from selected objects'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        edit_mode    = context.mode == 'EDIT_MESH'
        if edit_mode:
            bpy.ops.object.mode_set(mode='OBJECT')
        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        cleared = 0
        for obj in mesh_objects:
            mesh = obj.data
            bm = bmesh.new()
            bm.from_mesh(mesh)
            bm.faces.ensure_lookup_table()
            for face in bm.faces:
                face.material_index = 0
            bm.to_mesh(mesh)
            bm.free()
            slots_to_remove = [
                i for i, slot in enumerate(mesh.materials)
                if slot and (
                    slot.name in FBXMT_ALL_MATERIALS or
                    _is_island_material(slot) or
                    _is_chain_material(slot)  # legacy
                )
            ]
            for i in reversed(slots_to_remove):
                mesh.materials.pop(index=i)
                cleared += 1
            mesh.update()
        if edit_mode:
            bpy.ops.object.mode_set(mode='EDIT')
        self.report({'INFO'}, f'Removed {cleared} M_FBXMT slot(s) from {len(mesh_objects)} object(s)')
        return {'FINISHED'}


class OT_FBXMT_Clear_All_Materials(Operator):
    bl_idname = 'fbxmt.clear_all_materials'
    bl_label = 'Clear All Materials'
    bl_description = 'Remove ALL material slots from selected objects'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        edit_mode    = context.mode == 'EDIT_MESH'
        if edit_mode:
            bpy.ops.object.mode_set(mode='OBJECT')
        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        for obj in mesh_objects:
            mesh = obj.data
            bm = bmesh.new()
            bm.from_mesh(mesh)
            bm.faces.ensure_lookup_table()
            for face in bm.faces:
                face.material_index = 0
            bm.to_mesh(mesh)
            bm.free()
            mesh.materials.clear()
            mesh.update()
        if edit_mode:
            bpy.ops.object.mode_set(mode='EDIT')
        self.report({'INFO'}, f'Cleared all materials from {len(mesh_objects)} object(s)')
        return {'FINISHED'}


class OT_FBXMT_Clear_Scene_Materials(Operator):
    bl_idname = 'fbxmt.clear_scene_materials'
    bl_label = 'Clear Scene Materials'
    bl_description = 'Remove ALL materials from the entire scene - nuclear option'
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        edit_mode = context.mode == 'EDIT_MESH'
        if edit_mode:
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in context.scene.objects:
            if obj.type == 'MESH':
                bm = bmesh.new()
                bm.from_mesh(obj.data)
                bm.faces.ensure_lookup_table()
                for face in bm.faces:
                    face.material_index = 0
                bm.to_mesh(obj.data)
                bm.free()
                obj.data.materials.clear()
                obj.data.update()
        mat_count = len(bpy.data.materials)
        for mat in list(bpy.data.materials):
            bpy.data.materials.remove(mat)

        # Purge all FBXMT cached images — tile previews, swatches, preview copies
        img_prefixes = ('__tile_', '__fbxmt_tile_', '__fbxmt_swatch_', '__fbxmt_preview_')
        img_count = 0
        for img in list(bpy.data.images):
            if any(img.name.startswith(p) for p in img_prefixes):
                bpy.data.images.remove(img)
                img_count += 1

        self.report({'INFO'}, f'Removed {mat_count} material(s) and {img_count} cached image(s) from scene')
        return {'FINISHED'}



class OT_FBXMT_Strip_Mesh(Operator):
    """Strip all UV maps and all material slots from selected objects.

    Use to reset imported master meshes to a clean state before running
    full prep. Does not affect mesh geometry.
    """
    bl_idname  = 'fbxmt.strip_mesh'
    bl_label   = 'Strip UVs & Materials'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        stripped = 0
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            mesh = obj.data
            # Strip UV maps
            while mesh.uv_layers:
                mesh.uv_layers.remove(mesh.uv_layers[0])
            # Strip all material slots
            mesh.materials.clear()
            mesh.update()
            stripped += 1
        self.report({'INFO'}, f'Stripped UVs & materials from {stripped} object(s)')
        return {'FINISHED'}


class FBXMT_MT_Clear_Menu(bpy.types.Menu):
    bl_idname = 'FBXMT_MT_Clear_Menu'
    bl_label  = 'Clear'

    def draw(self, context):
        layout = self.layout
        layout.operator('fbxmt.clear_uvs',              text='Clear UV Maps',          icon='UV')
        layout.separator()
        layout.label(text='Selected Objects:')
        layout.operator('fbxmt.clear_mapper_materials', text='Clear Mapper Materials',  icon='MATERIAL')
        layout.operator('fbxmt.clear_all_materials',    text='Clear All Materials',     icon='X')
        layout.operator('fbxmt.strip_mesh',             text='Strip UVs & Materials',   icon='BRUSH_DATA')
        layout.separator()
        layout.operator('fbxmt.clear_scene_materials',  text='Clear Scene Materials',   icon='TRASH')


# ─── Island graph colouring operator ────────────────────────────────────────

class OT_FBXMT_Colour_Islands(Operator):
    """Auto-assign hidden island sub-materials by adjacency graph colouring.

    Finds all faces marked M_FBXMT_Island on selected objects, groups them
    into connected components (islands), builds an adjacency graph between
    components, and assigns M_FBXMT_Island_01..15 such that no two adjacent
    islands share the same sub-material. Floor/ceiling/ignore faces are
    ignored for adjacency — only lateral island-to-island edges matter.
    """
    bl_idname  = 'fbxmt.colour_islands'
    bl_label   = 'Auto-Colour Islands'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        ensure_island_materials()

        total_coloured = 0
        n_comp         = 0

        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue

            mesh      = obj.data
            in_edit   = (context.mode == 'EDIT_MESH' and obj == context.edit_object)

            if in_edit:
                bm = bmesh.from_edit_mesh(mesh)
            else:
                bm = bmesh.new()
                bm.from_mesh(mesh)
            bm.faces.ensure_lookup_table()

            slot_names = [m.name if m else None for m in mesh.materials]

            # All island faces = marker + any already-coloured sub-material faces
            marker_slots = {
                i for i, n in enumerate(slot_names)
                if n == ISLAND_MARKER_NAME or (n and n.startswith(ISLAND_SUB_PREFIX))
            }
            island_faces = [f for f in bm.faces if f.material_index in marker_slots]

            if not island_faces:
                bm.free()
                continue

            # ── Step 1: connected components ──────────────────────────────────
            # Hard rule: two faces sharing an edge are only in the same component
            # if they have the SAME slot name. This means:
            #   - Fresh marker faces group by connectivity as normal
            #   - Already-coloured sub-material faces only group with same-sub neighbours
            #   - Island boundaries from a previous run are fully respected
            face_set = set(f.index for f in island_faces)
            visited  = set()
            components       = []   # list of sets of face indices
            comp_is_new      = []   # True = uncoloured marker, False = already coloured sub
            comp_sub_colour  = []   # existing sub-material index if already coloured, else -1

            for start in island_faces:
                if start.index in visited:
                    continue
                comp       = set()
                start_name = slot_names[start.material_index]
                queue      = [start]
                while queue:
                    face = queue.pop()
                    if face.index in visited:
                        continue
                    visited.add(face.index)
                    comp.add(face.index)
                    face_name = slot_names[face.material_index]
                    for edge in face.edges:
                        for nb in edge.link_faces:
                            if nb.index not in face_set or nb.index in visited:
                                continue
                            nb_name = slot_names[nb.material_index]
                            # Only merge if both faces have the same slot name
                            if nb_name == face_name:
                                queue.append(nb)
                components.append(comp)
                # Determine if this component is a fresh marker or already coloured
                if start_name == ISLAND_MARKER_NAME:
                    comp_is_new.append(True)
                    comp_sub_colour.append(-1)
                else:
                    comp_is_new.append(False)
                    try:
                        existing_idx = ISLAND_SUB_NAMES.index(start_name)
                    except ValueError:
                        existing_idx = 0
                    comp_sub_colour.append(existing_idx)

            # ── Step 2: adjacency graph ───────────────────────────────────────
            n_comp       = len(components)
            face_to_comp = {}
            for ci, comp in enumerate(components):
                for fi in comp:
                    face_to_comp[fi] = ci

            adj = [set() for _ in range(n_comp)]
            for face in island_faces:
                ci = face_to_comp[face.index]
                for edge in face.edges:
                    for nb in edge.link_faces:
                        if nb.index in face_to_comp:
                            cj = face_to_comp[nb.index]
                            if cj != ci:
                                adj[ci].add(cj)
                                adj[cj].add(ci)

            # ── Step 2b: determine group (Floor/Ceil/Wall) per component from normals ──
            props      = context.scene.fbxmt_props if hasattr(context.scene, 'fbxmt_props') else None
            thresh_deg = props.ramp_wall_threshold if props else 45.0
            thresh_dot = math.cos(math.radians(thresh_deg))
            world_mat  = obj.matrix_world
            z_axis     = Vector((0.0, 0.0, 1.0))

            # Group index: 0=Floor, 1=Ceil, 2=Wall
            # ISLAND_SUB_NAMES is Wall/Floor/Ceil interleaved:
            # Wall=0,3,6,9,12  Floor=1,4,7,10,13  Ceil=2,5,8,11,14
            _GROUP_INDICES = {
                0: list(range(1, 15, 3)),   # Floor: 1,4,7,10,13
                1: list(range(2, 15, 3)),   # Ceil:  2,5,8,11,14
                2: list(range(0, 15, 3)),   # Wall:  0,3,6,9,12
            }

            comp_group = []
            for ci, comp in enumerate(components):
                # Average world-space normal of component faces
                avg_normal = Vector((0.0, 0.0, 0.0))
                for fi in comp:
                    avg_normal += world_mat.to_3x3() @ bm.faces[fi].normal
                if avg_normal.length > 0:
                    avg_normal.normalize()
                dot_z = avg_normal.dot(z_axis)
                if abs(dot_z) >= thresh_dot:
                    group = 0 if dot_z > 0 else 1   # Floor or Ceiling
                else:
                    group = 2                         # Wall
                comp_group.append(group)

            # ── Step 3: greedy graph colouring per group ──────────────────────
            # colour = index into ISLAND_SUB_NAMES (not sequential — per-group slice)
            colours = list(comp_sub_colour)
            for ci in range(n_comp):
                if not comp_is_new[ci]:
                    continue
                group       = comp_group[ci]
                group_idxs  = _GROUP_INDICES[group]
                used_global = {colours[cj] for cj in adj[ci] if colours[cj] >= 0}
                # Pick lowest available index from this group's slice
                chosen = group_idxs[0]
                for idx in group_idxs:
                    if idx not in used_global:
                        chosen = idx
                        break
                colours[ci] = chosen

            # ── Step 4: ensure needed sub-material slots exist ────────────────
            needed_indices = {colours[ci] for ci in range(n_comp) if comp_is_new[ci]}
            existing_names = {m.name for m in mesh.materials if m}
            for idx in needed_indices:
                sub_name = ISLAND_SUB_NAMES[idx]
                if sub_name not in existing_names:
                    sub_mat = bpy.data.materials.get(sub_name)
                    if sub_mat:
                        mesh.materials.append(sub_mat)
                        existing_names.add(sub_name)

            slot_names = [m.name if m else None for m in mesh.materials]

            # ── Step 5: assign sub-material to new faces only ─────────────────
            bm.faces.ensure_lookup_table()
            for ci, comp in enumerate(components):
                if not comp_is_new[ci]:
                    continue
                sub_name = ISLAND_SUB_NAMES[colours[ci]]
                slot_idx = slot_names.index(sub_name) if sub_name in slot_names else None
                if slot_idx is None:
                    continue
                for fi in comp:
                    bm.faces[fi].material_index = slot_idx
                total_coloured += len(comp)

            if in_edit:
                bmesh.update_edit_mesh(mesh)
            else:
                bm.to_mesh(mesh)
                bm.free()
            mesh.update()

        # Rebuild so island sub-materials immediately reflect current prefs
        # (pattern, colours, corner marks) without requiring a manual Rebuild.
        rebuild_fbxmt_materials()

        self.report({'INFO'}, f'Coloured {total_coloured} face(s) across {n_comp} component(s)')
        return {'FINISHED'}



# ─── Auto-detect wall island runs ────────────────────────────────────────────

class OT_FBXMT_Auto_Detect_Wall_Islands(Operator):
    """Detect same-size connected wall face runs and mark them as islands.

    Walks wall-classified faces on the active object. Groups connected faces
    whose area stays within tolerance and whose normals stay within the break
    angle. Each qualifying group (2+ faces) is assigned M_FBXMT_Island and
    the auto-colourer is fired. Singleton or non-matching wall faces are
    assigned M_FBXMT_Trim as a visual flag and reported.

    Run in Edit mode to see results immediately.
    """
    bl_idname  = 'fbxmt.auto_detect_wall_islands'
    bl_label   = 'Auto-Detect Wall Islands'
    bl_options = {'REGISTER', 'UNDO'}

    break_angle_deg: bpy.props.FloatProperty(
        name        = 'Break Angle',
        description = 'Normal deviation between adjacent wall faces that starts a new island',
        default     = 45.0,
        min         = 5.0,
        max         = 170.0,
        step        = 5,
        precision   = 1,
    )

    area_tolerance: bpy.props.FloatProperty(
        name        = 'Area Tolerance',
        description = 'Maximum fractional area difference allowed within a run (0.002 = 0.2%%)',
        default     = 0.002,
        min         = 0.0,
        max         = 0.1,
        precision   = 4,
    )

    @classmethod
    def poll(cls, context):
        return (
            context.mode == 'EDIT_MESH'
            and context.active_object is not None
            and context.active_object.type == 'MESH'
        )

    def execute(self, context):
        import math as _math
        from mathutils import Vector as _Vector

        obj  = context.active_object
        mesh = obj.data

        # Ensure required materials exist
        ensure_fbxmt_materials()
        ensure_island_materials()

        bm = bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()

        world_mat   = obj.matrix_world
        z_axis      = _Vector((0.0, 0.0, 1.0))

        props      = context.scene.fbxmt_props if hasattr(context.scene, 'fbxmt_props') else None
        thresh_deg = props.ramp_wall_threshold if props else 45.0
        thresh_dot = _math.cos(_math.radians(thresh_deg))

        break_cos   = _math.cos(_math.radians(self.break_angle_deg))
        area_tol    = self.area_tolerance

        slot_names  = [m.name if m else None for m in mesh.materials]

        # ── Collect wall faces ────────────────────────────────────────────────
        wall_mat_names = {
            'M_FBXMT_Wall', 'M_FBXMT_Island',
            *[n for n in ISLAND_SUB_NAMES if 'Wall' in n],
        }

        def _is_wall(face):
            world_normal = (world_mat.to_3x3() @ face.normal).normalized()
            dot_z = abs(world_normal.dot(z_axis))
            return dot_z < thresh_dot

        wall_faces = [f for f in bm.faces if _is_wall(f)]

        if not wall_faces:
            self.report({'WARNING'}, 'No wall faces found on active object')
            return {'CANCELLED'}

        # ── Flood-fill into runs ──────────────────────────────────────────────
        # Break conditions:
        #   - Normal deviation from seed face > break_angle_deg
        #   - Area deviation from seed face area > area_tol
        wall_set = set(f.index for f in wall_faces)
        visited  = set()
        groups   = []   # list of lists of bmesh faces

        for seed in wall_faces:
            if seed.index in visited:
                continue

            seed_normal = (world_mat.to_3x3() @ seed.normal).normalized()
            seed_area   = seed.calc_area()

            group   = []
            queue   = [seed]

            while queue:
                face = queue.pop()
                if face.index in visited:
                    continue
                visited.add(face.index)
                group.append(face)

                face_normal = (world_mat.to_3x3() @ face.normal).normalized()
                face_area   = face.calc_area()

                for edge in face.edges:
                    for nb in edge.link_faces:
                        if nb.index not in wall_set or nb.index in visited:
                            continue
                        nb_normal = (world_mat.to_3x3() @ nb.normal).normalized()
                        nb_area   = nb.calc_area()

                        # Normal break — neighbour vs immediate face (local continuity)
                        # not vs seed, so curves accumulate correctly around a loop
                        if nb_normal.dot(face_normal) < break_cos:
                            continue
                        # Area break — still vs seed face (drift = mesh error signal)
                        if seed_area > 0:
                            area_diff = abs(nb_area - seed_area) / seed_area
                            if area_diff > area_tol:
                                continue

                        queue.append(nb)

            groups.append(group)

        # ── Ensure marker and trim slots exist ────────────────────────────────
        existing_names = {m.name for m in mesh.materials if m}

        def _ensure_slot(mat_name):
            if mat_name not in existing_names:
                mat = bpy.data.materials.get(mat_name)
                if mat:
                    mesh.materials.append(mat)
                    existing_names.add(mat_name)
            # Refresh slot_names after possible append
            return [m.name if m else None for m in mesh.materials]

        # ── Assign materials ──────────────────────────────────────────────────
        island_groups  = []
        flagged_faces  = []
        slot_names     = list(slot_names)

        for group in groups:
            if len(group) >= 2:
                # Multi-face run → island marker
                slot_names = _ensure_slot(ISLAND_MARKER_NAME)
                slot_idx   = slot_names.index(ISLAND_MARKER_NAME)
                for face in group:
                    face.material_index = slot_idx
                island_groups.append(group)
            else:
                # Singleton — only flag as Trim if truly isolated from all other
                # wall faces (i.e. no wall neighbours at all). A singleton that
                # sits at the boundary of a straight wall section is legitimate
                # wall geometry, not a mesh error.
                face       = group[0]
                has_wall_nb = any(
                    nb.index in wall_set
                    for edge in face.edges
                    for nb in edge.link_faces
                    if nb.index != face.index
                )
                if not has_wall_nb:
                    slot_names = _ensure_slot('M_FBXMT_Trim')
                    if 'M_FBXMT_Trim' in slot_names:
                        slot_idx = slot_names.index('M_FBXMT_Trim')
                        face.material_index = slot_idx
                    flagged_faces.append(face)
                # else: leave as M_FBXMT_Wall — boundary face of a straight section

        bmesh.update_edit_mesh(mesh)
        mesh.update()

        n_islands = len(island_groups)
        n_flagged = len(flagged_faces)

        # ── Fire auto-colourer ────────────────────────────────────────────────
        if n_islands > 0:
            bpy.ops.fbxmt.colour_islands('EXEC_DEFAULT')

        msg = f'Auto-detected {n_islands} wall island group(s)'
        if n_flagged:
            msg += f', {n_flagged} unmatched face(s) flagged as Trim — check mesh'
            print(f'[FBXMT] Auto-detect: {n_flagged} unmatched wall face(s) on "{obj.name}" flagged as Trim')

        self.report({'INFO'}, msg)
        return {'FINISHED'}

# ─── Texel density operator ──────────────────────────────────────────────

class OT_FBXMT_Set_Texel_Density(Operator):
    bl_idname  = 'fbxmt.set_texel_density'
    bl_label   = 'Set Texel Density'
    bl_options = {'REGISTER', 'UNDO'}

    value: bpy.props.IntProperty(default=1024)

    def execute(self, context):
        global _materials_built
        _materials_built = False
        context.scene.fbxmt_props.geo_texel_density = self.value
        rebuild_fbxmt_materials()
        return {'FINISHED'}


# ─── Corner mark preset operator ──────────────────────────────────────────────

class OT_FBXMT_Set_Corner_Preset(Operator):
    bl_idname  = 'fbxmt.set_corner_preset'
    bl_label   = 'Set Corner Mark Preset'
    bl_options = {'REGISTER', 'UNDO'}

    value: bpy.props.IntProperty(default=2)

    def execute(self, context):
        global _materials_built
        _materials_built = False
        prefs = _get_prefs()
        if prefs:
            prefs.corner_mark_preset = self.value
        rebuild_fbxmt_materials()
        return {'FINISHED'}


# ─── Checker scale operator ──────────────────────────────────────────────────

class OT_FBXMT_Set_Checker_Scale(Operator):
    """Set checker squares per tile and rebuild materials immediately."""
    bl_idname  = 'fbxmt.set_checker_scale'
    bl_label   = 'Set Checker Scale'
    bl_options = {'REGISTER', 'UNDO'}

    value: bpy.props.IntProperty(default=4)

    def execute(self, context):
        global _materials_built
        _materials_built = False
        prefs = _get_prefs()
        if prefs:
            prefs.checker_scale = self.value
        rebuild_fbxmt_materials()
        return {'FINISHED'}


# ─── Scene property lifecycle ─────────────────────────────────────────────────

# Module flag — prevents update callbacks from triggering each other
# when the selection sync writes both colour props at once.
_syncing_selection = False


def _tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _get_selected_chain(scene):
    """Return the currently selected island/chain material from the global pool, or None.

    Returns None when the island list has no active selection (fbxmt_island_selected
    is False), which happens after the user clicks in the base materials list.
    List is now sourced from bpy.data.materials so no active object is required.
    """
    if not getattr(scene, 'fbxmt_island_selected', False):
        return None
    all_mats = list(bpy.data.materials)
    idx      = scene.fbxmt_island_list_index
    if idx < 0 or idx >= len(all_mats):
        return None
    mat = all_mats[idx]
    return mat if _is_chain_material(mat) else None


def _sync_color_from_selection(scene, _context):
    """On list selection change - read both colours from the material and
    populate the two picker props. Sets _syncing_selection to prevent the
    picker update callbacks from writing back while we're populating them."""
    global _syncing_selection
    mat = _get_selected_chain(scene)
    if mat is None:
        return
    rgb_a = _read_chain_color_a(mat)
    rgb_b = _read_chain_color_b(mat)
    if rgb_a is None or rgb_b is None:
        return
    _syncing_selection = True
    try:
        scene.fbxmt_chain_color_a = rgb_a
        scene.fbxmt_chain_color_b = rgb_b
    finally:
        _syncing_selection = False
    _tag_redraw()


def register_material_props():
    bpy.types.Scene.fbxmt_chain_color_a = FloatVectorProperty(
        name        = 'Colour A',
        description = 'First checkerboard colour for the selected island material',
        subtype     = 'COLOR',
        min=0.0, max=1.0,
        default     = CHECKER_BLUE_RGB,
    )
    bpy.types.Scene.fbxmt_chain_color_b = FloatVectorProperty(
        name        = 'Colour B',
        description = 'Second checkerboard colour (manual mode only)',
        subtype     = 'COLOR',
        min=0.0, max=1.0,
        default     = (0.85, 0.35, 0.05),
    )
    def _on_mat_index(scene, _ctx):
        scene.fbxmt_base_selected = True

    bpy.types.Scene.fbxmt_base_list_index = bpy.props.IntProperty(
        name    = 'Active Material',
        default = 0,
        update  = _on_mat_index,
    )
    bpy.types.Scene.fbxmt_base_selected = bpy.props.BoolProperty(
        name    = 'Material List Has Selection',
        default = False,
    )
    # Kept for compatibility — no longer drives a second list
    bpy.types.Scene.fbxmt_island_list_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.fbxmt_island_selected   = bpy.props.BoolProperty(default=False)
    bpy.types.Scene.fbxmt_active_list       = bpy.props.EnumProperty(
        name  = 'Active List',
        items = [('BASE', 'Base', ''), ('ISLAND', 'Island', '')],
        default = 'BASE',
    )


def unregister_material_props():
    del bpy.types.Scene.fbxmt_chain_color_a
    del bpy.types.Scene.fbxmt_chain_color_b
    del bpy.types.Scene.fbxmt_island_list_index
    del bpy.types.Scene.fbxmt_base_list_index
    del bpy.types.Scene.fbxmt_active_list
    del bpy.types.Scene.fbxmt_base_selected
    del bpy.types.Scene.fbxmt_island_selected
