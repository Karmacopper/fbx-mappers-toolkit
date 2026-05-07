import bpy
import re
import math
import colorsys
import bmesh
from mathutils import Vector
from bpy.types import Operator
from bpy.props import FloatVectorProperty


# ─── Base material definitions ────────────────────────────────────────────────

FBXMT_MATERIALS = {
    'M_FBXMT_Floor':   (0.3,  0.75, 0.3,  1.0),
    'M_FBXMT_Ceiling': (0.3,  0.55, 0.9,  1.0),
    'M_FBXMT_Wall':    (0.9,  0.65, 0.2,  1.0),
    'M_FBXMT_Trim':    (0.75, 0.3,  0.75, 1.0),
    'M_FBXMT_Ignore':  (0.25, 0.25, 0.25, 1.0),
}

FBXMT_FLOOR_MATERIALS = {'M_FBXMT_Floor', 'M_FBXMT_Ceiling'}
FBXMT_WALL_MATERIALS  = {'M_FBXMT_Wall', 'M_FBXMT_Trim'}
FBXMT_IGNORE_MATERIAL = 'M_FBXMT_Ignore'
FBXMT_ALL_MATERIALS   = FBXMT_FLOOR_MATERIALS | FBXMT_WALL_MATERIALS | {FBXMT_IGNORE_MATERIAL}

# Checker textures removed — all materials now use procedural node trees

# ── Island marker system ─────────────────────────────────────────────────────
# One marker material visible to the artist. 15 hidden sub-materials used
# internally by the graph colourer and unwrapper to distinguish islands.
# Sub-materials share Colour A with the marker but get distinct grey B values
# (0..100% in 15 steps). They are filtered from all panels and never baked.
ISLAND_MARKER_NAME = 'M_FBXMT_Island'
ISLAND_SUB_PREFIX  = 'M_FBXMT_Island_'
ISLAND_SUB_COUNT   = 15
ISLAND_SUB_NAMES   = [f'M_FBXMT_Island_{i:02d}' for i in range(1, ISLAND_SUB_COUNT + 1)]
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

# Lighter/Darker multipliers (indices 0-6, default index 3 = 1.0 = same as A)
_DARKER_MULTIPLIERS = [0.30, 0.50, 0.70, 1.00, 1.30, 1.50, 1.70]
# Greyscale values (indices 0-4: black, 25%, 50%, 75%, white)
_GREY_VALUES        = [0.0, 0.25, 0.50, 0.75, 1.0]

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
    """Return 1-based index of M_FBXMT_Island_NN, or None."""
    m = re.fullmatch(re.escape(ISLAND_SUB_PREFIX) + r'(\d+)', mat_name)
    return int(m.group(1)) if m else None


def _is_island_sub_material(mat):
    """True for hidden sub-materials M_FBXMT_Island_01..15."""
    return mat is not None and mat.name in set(ISLAND_SUB_NAMES)


def _is_island_material(mat):
    """True for the visible marker OR any hidden sub-material."""
    return mat is not None and (
        mat.name == ISLAND_MARKER_NAME or _is_island_sub_material(mat)
    )


def get_all_island_sub_materials():
    """All 15 hidden sub-materials, creating missing ones."""
    return [bpy.data.materials.get(n) for n in ISLAND_SUB_NAMES if bpy.data.materials.get(n)]


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


def _resolve_color_b(color_a_rgb, mode, color_b_rgb, darker_idx=3, grey_idx=2):
    """Return the resolved colour B as an RGB tuple.

    mode='MANUAL'    — return color_b_rgb as-is (stored vector, index 0 = manual)
    mode='DARKER'    — multiply A's lightness by _DARKER_MULTIPLIERS[darker_idx-1], clamped 0-1
    mode='GREYSCALE' — return grey from _GREY_VALUES[grey_idx-1], ignores A
    mode='INVERSE'   — rotate A's hue by 0.5, keep S and L
    """
    if mode == 'MANUAL':
        return tuple(color_b_rgb[:3])

    r, g, b = color_a_rgb[0], color_a_rgb[1], color_a_rgb[2]

    if mode == 'DARKER':
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        mult     = _DARKER_MULTIPLIERS[max(0, min(6, darker_idx - 1))]
        new_l    = max(0.0, min(1.0, l * mult))
        return colorsys.hls_to_rgb(h, new_l, s)

    if mode == 'GREYSCALE':
        v = _GREY_VALUES[max(0, min(4, grey_idx - 1))]
        return (v, v, v)

    if mode == 'INVERSE':
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        return colorsys.hls_to_rgb((h + 0.5) % 1.0, l, s)

    return tuple(color_b_rgb[:3])  # fallback


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

    return None  # unknown pattern — fall back to square


def _build_checker_node_tree(mat, color_a_rgb, color_b_rgb, scale=None, pattern='SQUARE'):
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
    Modulo 1.0 gives position within one tile [0, 1).
    BORDER_W = px/1024 (arm width as fraction of tile, tile always = 1024px).
    BORDER_L = corner_mark_length/100 (arm extent as fraction of tile).
    """
    prefs = _get_prefs()

    if scale is None:
        squares_per_tile = prefs.checker_scale if prefs else 4
        scale = float(squares_per_tile)

    # Texel tile scale: geo_texel_density/1024 maps 1 UV unit to 1 texel tile.
    # Falls back to 1024tx/m = 1m tile if scene prefs unavailable.
    try:
        geo_texel_density = bpy.context.scene.fbxmt_props.geo_texel_density
    except Exception:
        geo_texel_density = 1024.0
    tile_scale = geo_texel_density / 1024.0

    # Corner marker constants — all fractions of one texel tile (1.0 in tile UV space).
    # Preset 1-4 maps to 12.5/25/37.5/50% of tile.
    preset        = prefs.corner_mark_preset if prefs else 2
    BORDER_L      = preset * 0.125
    CIRCLE_R      = BORDER_L * 0.5          # quarter-circle radius = half arm length
    show_circle   = prefs.show_corner_circle if prefs else True
    px            = prefs.corner_mark_width_px if prefs else 4
    # Tile always = 1024px wide by definition of texel density.
    BORDER_W = px / 1024.0

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
    # Returns a Value socket (0/1) for DIAGONAL and DIAMOND.
    # Returns None for SQUARE — checker's own Fac output is used instead.
    pattern_factor = _build_pattern_nodes(
        nodes, links, new_node, mapping_checker, pattern
    )

    # For SQUARE, use the checker Fac directly.
    # For DIAGONAL/DIAMOND, we need a separate A/B mix driven by pattern_factor.
    # The checker node is still used to derive the invert colour for cross markers.
    if pattern_factor is not None:
        # Pattern mix — pure A/B driven by pattern geometry
        pat_mix = new_node('ShaderNodeMix', -100, 300)
        pat_mix.data_type  = 'RGBA'
        pat_mix.blend_type = 'MIX'
        pat_mix.inputs['A'].default_value = (*color_a_rgb, 1.0)
        pat_mix.inputs['B'].default_value = (*color_b_rgb, 1.0)
        links.new(pattern_factor, pat_mix.inputs['Factor'])
        checker_color_out = pat_mix.outputs['Result']
    else:
        checker_color_out = checker.outputs['Color']

    # ── Colour invert — applied to checker output for cross arms ────────────
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
    # Fac=0 → tile body (checker), Fac=1 → markers.
    # Hue shift node sits between invert and mix — shifts marker colour.
    # Default 180 degrees = fully inverted checker (maximum contrast).
    # +/-180 from default = lines match checker colour (invisible).

    x_mix = 700 + (1600 if show_circle else 0)

    hue_shift_deg = prefs.corner_hue_shift if prefs else 180.0
    hue_node = new_node('ShaderNodeHueSaturation', x_mix - 200, 0)
    hue_node.inputs['Saturation'].default_value = 1.0
    hue_node.inputs['Value'].default_value      = 1.0
    hue_node.inputs['Fac'].default_value        = 1.0
    hue_node.inputs['Hue'].default_value        = 0.5 + (hue_shift_deg / 360.0)

    mix = new_node('ShaderNodeMix', x_mix, 100)
    mix.data_type  = 'RGBA'
    mix.blend_type = 'MIX'
    mix.inputs['Factor'].default_value = 0.0
    links.new(edge_mask.outputs['Value'], mix.inputs['Factor'])
    links.new(checker_color_out,           mix.inputs['A'])
    links.new(checker_color_out,           invert.inputs['B'])
    links.new(invert.outputs['Result'],   hue_node.inputs['Color'])
    links.new(hue_node.outputs['Color'],  mix.inputs['B'])

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
    Colour A is always taken from color_wall_a — island faces are wall-type
    surfaces by definition and should read as such.
    """
    created = []
    prefs   = _get_prefs()
    if prefs and hasattr(prefs, 'color_wall_a'):
        col_a = tuple(prefs.color_wall_a[:3])
    else:
        col_a = tuple(FBXMT_MATERIALS['M_FBXMT_Wall'][:3])

    # Visible marker — always rebuild to pick up current wall colour
    if ISLAND_MARKER_NAME not in bpy.data.materials:
        mat = bpy.data.materials.new(name=ISLAND_MARKER_NAME)
        created.append(ISLAND_MARKER_NAME)
    else:
        mat = bpy.data.materials[ISLAND_MARKER_NAME]
    _build_checker_node_tree(mat, col_a, (0.5, 0.5, 0.5))

    # Hidden sub-materials — invert wall A, use as centre of 5 lightness steps
    inv_a      = (1.0 - col_a[0], 1.0 - col_a[1], 1.0 - col_a[2])
    h_i, l_i, s_i = colorsys.rgb_to_hls(*inv_a)
    _offsets   = [-0.5, -0.25, 0.0, 0.25, 0.5]
    _step_cols = [colorsys.hls_to_rgb(h_i, max(0.15, min(0.85, l_i + off)), s_i) for off in _offsets]

    for i, name in enumerate(ISLAND_SUB_NAMES):
        if name not in bpy.data.materials:
            mat = bpy.data.materials.new(name=name)
            created.append(name)
        else:
            mat = bpy.data.materials[name]
        col_b = _step_cols[i % len(_step_cols)]
        _build_checker_node_tree(mat, col_a, col_b)
    return created


# Legacy shim — called by load_post handler which previously ensured chain materials
def ensure_chain_materials():
    return ensure_island_materials()


def _chain_pref_color(prefs, idx, ab):
    """Legacy — no longer used for new files."""
    return None


def ensure_fbxmt_materials():
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


def rebuild_fbxmt_materials():
    """Rebuild base material node trees and all 5 chain materials.
    Reads checker colours, pattern, and colour-B mode from addon preferences.
    """
    global _materials_built
    prefs = _get_prefs()

    # Slot key → material name mapping
    _SLOT_TO_MAT = {
        'floor':   'M_FBXMT_Floor',
        'ceiling': 'M_FBXMT_Ceiling',
        'wall':    'M_FBXMT_Wall',
        'trim':    'M_FBXMT_Trim',
        'ignore':  'M_FBXMT_Ignore',
    }
    def _read_mat_settings(slot):
        """Return (color_a, color_b, pattern) for a named slot from prefs."""
        if not prefs:
            return None, None, 'SQUARE'
        col_a   = tuple(getattr(prefs, f'color_{slot}_a', (0.5,)*4)[:3])
        col_b_v = tuple(getattr(prefs, f'color_{slot}_b', (0.35,)*4)[:3])
        mode    = getattr(prefs, f'color_b_mode_{slot}', 'MANUAL')
        darker  = getattr(prefs, f'color_b_darker_{slot}', 4)
        grey    = getattr(prefs, f'color_b_grey_{slot}',   3)
        pattern = getattr(prefs, f'checker_pattern_{slot}', 'SQUARE')
        col_b   = _resolve_color_b(col_a, mode, col_b_v, darker, grey)
        return col_a, col_b, pattern

    rebuilt = []

    for slot, mat_name in _SLOT_TO_MAT.items():
        try:
            mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(name=mat_name)
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
        _, _, pattern_island = _read_mat_settings('island')
        if pattern_island is None:
            pattern_island = 'SQUARE'

        # Visible marker — B is mid-grey
        marker = bpy.data.materials.get(ISLAND_MARKER_NAME) or bpy.data.materials.new(name=ISLAND_MARKER_NAME)
        _build_checker_node_tree(marker, col_a_island, (0.5, 0.5, 0.5), pattern=pattern_island)
        rebuilt.append(ISLAND_MARKER_NAME)

        # Hidden sub-materials — invert wall A, use as centre of 5 lightness steps
        inv_a      = (1.0 - col_a_island[0], 1.0 - col_a_island[1], 1.0 - col_a_island[2])
        h_i, l_i, s_i = colorsys.rgb_to_hls(*inv_a)
        _offsets   = [-0.5, -0.25, 0.0, 0.25, 0.5]
        _step_cols = [colorsys.hls_to_rgb(h_i, max(0.15, min(0.85, l_i + off)), s_i) for off in _offsets]
        for i, name in enumerate(ISLAND_SUB_NAMES):
            mat   = bpy.data.materials.get(name) or bpy.data.materials.new(name=name)
            col_b = _step_cols[i % len(_step_cols)]
            _build_checker_node_tree(mat, col_a_island, col_b, pattern=pattern_island)
            rebuilt.append(name)
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
    target = bpy.data.collections.get(collection_name)
    if not target:
        return
    for col in list(obj.users_collection):
        col.objects.unlink(obj)
    target.objects.link(obj)


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
            floor_threshold_dot = math.cos(math.radians(props.uv_floor_threshold))
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

                    if dot_z >= floor_threshold_dot:
                        mat_name = 'M_FBXMT_Floor' if world_normal.z > 0 else 'M_FBXMT_Ceiling'
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
    'M_FBXMT_Floor':   'Floor',
    'M_FBXMT_Ceiling': 'Ceiling',
    'M_FBXMT_Wall':    'Wall',
    'M_FBXMT_Trim':    'Trim',
    'M_FBXMT_Ignore':  'Ignore',
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
    'M_FBXMT_Floor':   'Floor',
    'M_FBXMT_Ceiling': 'Ceiling',
    'M_FBXMT_Wall':    'Wall',
    'M_FBXMT_Trim':    'Trim',
    'M_FBXMT_Ignore':  'Ignore',
    'M_FBXMT_Island':  'Island Marker',
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
        self.report({'INFO'}, f'Removed {mat_count} material(s) from scene')
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

            mesh = obj.data
            bm   = bmesh.new()
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

            # ── Step 3: greedy graph colouring — only recolour fresh faces ────
            colours = list(comp_sub_colour)  # seed with existing colours
            for ci in range(n_comp):
                if not comp_is_new[ci]:
                    continue  # already coloured — don't touch
                used   = {colours[cj] for cj in adj[ci] if colours[cj] >= 0}
                colour = 0
                while colour in used:
                    colour += 1
                colours[ci] = min(colour, ISLAND_SUB_COUNT - 1)

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

            bm.to_mesh(mesh)
            bm.free()
            mesh.update()

        self.report({'INFO'}, f'Coloured {total_coloured} face(s) across {n_comp} component(s)')
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
