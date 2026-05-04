import bpy
import re
import math
import colorsys
import bmesh
from mathutils import Vector
from bpy.types import Operator
from bpy.props import FloatVectorProperty


# ─── Base material definitions ────────────────────────────────────────────────
# M_FBXMT_Island_01 is retired. Chain_01 is the new locked baseline marker.

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
# Chain materials are dynamic — not in this set, handled separately.
FBXMT_ALL_MATERIALS   = FBXMT_FLOOR_MATERIALS | FBXMT_WALL_MATERIALS | {FBXMT_IGNORE_MATERIAL}

# Checker textures removed — all materials now use procedural node trees

# Chain material constants
CHAIN_PREFIX        = 'M_FBXMT_Chain_'
CHAIN_LOCKED_NAME   = 'M_FBXMT_Chain_01'   # always present, cannot be deleted
TOOLKIT_PREFIX      = 'M_FBXMT_'

# Checkerboard colour for Chain_01 and the blue tile in all subsequent chains.
# HLS: hue≈220°, L=0.45, S=0.80
_CHECKER_BLUE_HLS  = (0.611, 0.45, 0.80)
CHECKER_BLUE_RGB   = colorsys.hls_to_rgb(*_CHECKER_BLUE_HLS)
CHAIN_COLOR_LIGHTNESS = _CHECKER_BLUE_HLS[1]  # normalised lightness for all chain checker colours

# Default colour B for Chain_01 (orange-ish, same perceived brightness as blue)
_CHAIN01_DEFAULT_B_HLS = (0.08, 0.45, 0.80)
CHAIN01_COLOR_B_RGB    = colorsys.hls_to_rgb(*_CHAIN01_DEFAULT_B_HLS)

COLLECTION_GEO        = 'Geo'
COLLECTION_PROPS      = 'Props'
COLLECTION_TRIM       = 'Trim'
LIGHTMAP_CHANNEL_NAME = 'LightmapUVs'


# Module-level flag — set True while operators are mutating material slots so
# that any future depsgraph handler won't re-enter mid-execute.
# Set directly via _mat_module._suppress_handler in fbx_import.py during
# the full-prep pipeline, and via the global in OT_FBXMT_Assign_Materials.
_suppress_handler = False


# ─── Chain helpers ────────────────────────────────────────────────────────────

def _chain_index(mat_name):
    """Return integer suffix of M_FBXMT_Chain_NN, or None."""
    m = re.fullmatch(re.escape(CHAIN_PREFIX) + r'(\d+)', mat_name)
    return int(m.group(1)) if m else None


def _existing_chain_indices():
    return sorted(
        _chain_index(m.name)
        for m in bpy.data.materials
        if _chain_index(m.name) is not None
    )


def _next_chain_index():
    """Lowest positive integer not already used - fills gaps before extending."""
    used = set(_existing_chain_indices())
    i = 1
    while i in used:
        i += 1
    return i


def _is_chain_material(mat):
    return mat is not None and _chain_index(mat.name) is not None


def _is_locked_chain(mat):
    return mat is not None and mat.name == CHAIN_LOCKED_NAME


def get_all_chain_materials():
    """All M_FBXMT_Chain_NN materials sorted by index."""
    return sorted(
        [m for m in bpy.data.materials if _is_chain_material(m)],
        key=lambda m: _chain_index(m.name),
    )


def _get_prefs():
    """Retrieve global addon preferences from the active scene."""
    try:
        return bpy.context.scene.fbxmt_prefs_global
    except AttributeError:
        return None


# ─── Node-tree builders ───────────────────────────────────────────────────────

def setup_material_nodes(mat, colour, scale=None, color_b=None):
    """Build the procedural checker node tree for a base toolkit material.
    colour is Color A (main tile). color_b is Color B - defaults to 70%
    darkened colour if not supplied. scale reads from prefs if not specified.
    """
    r, g, b = colour[0], colour[1], colour[2]
    color_a = (r, g, b)
    if color_b is None:
        color_b = (r * 0.7, g * 0.7, b * 0.7)
    _build_checker_node_tree(mat, color_a, color_b, scale=scale)
    mat.diffuse_color = (*colour[:3], 1.0)


def _build_checker_node_tree(mat, color_a_rgb, color_b_rgb, scale=None):
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

    # ── Main checker ──────────────────────────────────────────────────────────
    checker = new_node('ShaderNodeTexChecker', -460, 200)
    checker.inputs['Color1'].default_value = (*color_a_rgb, 1.0)
    checker.inputs['Color2'].default_value = (*color_b_rgb, 1.0)
    checker.inputs['Scale'].default_value  = 1.0
    links.new(mapping_checker.outputs['Vector'], checker.inputs['Vector'])

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
    # Fac=0 → tile body (checker), Fac=1 → markers (inverted checker).

    mix = new_node('ShaderNodeMix', 700 + (1600 if show_circle else 0), 100)
    mix.data_type  = 'RGBA'
    mix.blend_type = 'MIX'
    mix.inputs['Factor'].default_value = 0.0
    links.new(edge_mask.outputs['Value'], mix.inputs['Factor'])
    links.new(checker.outputs['Color'],  mix.inputs['A'])
    links.new(checker.outputs['Color'], invert.inputs['B'])
    links.new(invert.outputs['Result'],  mix.inputs['B'])

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

def ensure_chain_01():
    """Create M_FBXMT_Chain_01 if it doesn't exist. Always returns it."""
    if CHAIN_LOCKED_NAME not in bpy.data.materials:
        mat    = bpy.data.materials.new(name=CHAIN_LOCKED_NAME)
        prefs  = _get_prefs()
        col_a  = tuple(prefs.color_chain01_a[:3]) if prefs else CHECKER_BLUE_RGB
        col_b  = tuple(prefs.color_chain01_b[:3]) if prefs else CHAIN01_COLOR_B_RGB
        _build_checker_node_tree(mat, col_a, col_b)
    return bpy.data.materials[CHAIN_LOCKED_NAME]


def ensure_fbxmt_materials():
    created = []
    for name, colour in FBXMT_MATERIALS.items():
        if name not in bpy.data.materials:
            mat = bpy.data.materials.new(name=name)
            setup_material_nodes(mat, colour)
            created.append(name)
    ensure_chain_01()
    return created


def rebuild_fbxmt_materials():
    """Rebuild base material node trees, rebuild Chain_01, push all chains to all scene meshes.
    Reads checker colours and scale from addon preferences if available.
    """
    prefs = _get_prefs()

    # Per-material A+B colour pairs from prefs
    pref_colour_pairs = {}
    if prefs:
        pref_colour_pairs = {
            'M_FBXMT_Floor':   (tuple(prefs.color_floor_a[:3]),   tuple(prefs.color_floor_b[:3])),
            'M_FBXMT_Ceiling': (tuple(prefs.color_ceiling_a[:3]), tuple(prefs.color_ceiling_b[:3])),
            'M_FBXMT_Wall':    (tuple(prefs.color_wall_a[:3]),    tuple(prefs.color_wall_b[:3])),
            'M_FBXMT_Trim':    (tuple(prefs.color_trim_a[:3]),    tuple(prefs.color_trim_b[:3])),
            'M_FBXMT_Ignore':  (tuple(prefs.color_ignore_a[:3]),  tuple(prefs.color_ignore_b[:3])),
        }

    rebuilt = []
    for name, default_colour in FBXMT_MATERIALS.items():
        mat = bpy.data.materials.get(name) or bpy.data.materials.new(name=name)
        if name in pref_colour_pairs:
            col_a, col_b = pref_colour_pairs[name]
        else:
            r, g, b = default_colour[:3]
            col_a, col_b = (r, g, b), (r * 0.7, g * 0.7, b * 0.7)
        setup_material_nodes(mat, col_a, color_b=col_b)
        rebuilt.append(name)

    # Rebuild Chain_01 with pref colours and scale
    chain_01 = bpy.data.materials.get(CHAIN_LOCKED_NAME) or bpy.data.materials.new(name=CHAIN_LOCKED_NAME)
    col_a    = tuple(prefs.color_chain01_a[:3]) if prefs else CHECKER_BLUE_RGB
    col_b    = tuple(prefs.color_chain01_b[:3]) if prefs else CHAIN01_COLOR_B_RGB
    _build_checker_node_tree(chain_01, col_a, col_b)
    rebuilt.append(CHAIN_LOCKED_NAME)

    # Rebuild node trees on chain materials that are already slotted on meshes.
    # Do NOT push chains to meshes that don't already have them — that is the
    # user's explicit choice via the Islands list. Respect it.
    all_chains = get_all_chain_materials()
    chain_set  = {c.name for c in all_chains}
    for obj in bpy.context.scene.objects:
        if obj.type != 'MESH':
            continue
        for slot in obj.data.materials:
            if slot and slot.name in chain_set and slot.name != CHAIN_LOCKED_NAME:
                rgb_a = _read_chain_color_a(slot) or CHECKER_BLUE_RGB
                rgb_b = _read_chain_color_b(slot) or CHAIN01_COLOR_B_RGB
                _build_checker_node_tree(slot, rgb_a, rgb_b)

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
    for mat_name in FBXMT_MATERIALS:
        mat = bpy.data.materials.get(mat_name)
        if mat and mat_name not in [m.name for m in obj.data.materials if m]:
            obj.data.materials.append(mat)
    # Also push Chain_01
    chain_01 = bpy.data.materials.get(CHAIN_LOCKED_NAME)
    if chain_01 and chain_01.name not in [m.name for m in obj.data.materials if m]:
        obj.data.materials.append(chain_01)


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


# ─── Sanity check helper ──────────────────────────────────────────────────────

def check_chain_sanity(objects):
    """
    Return list of object names that have at least one chain material assigned
    to a face but are missing Chain_01 from their slots entirely.
    These will produce incorrect UV islands on export.
    """
    problems = []
    for obj in objects:
        if obj.type != 'MESH':
            continue
        slot_names = {m.name for m in obj.data.materials if m}
        has_any_chain = any(
            _chain_index(n) is not None for n in slot_names
        )
        if has_any_chain and CHAIN_LOCKED_NAME not in slot_names:
            problems.append(obj.name)
    return problems


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

class OT_FBXMT_Add_Chain_Material(Operator):
    """Generate the next M_FBXMT_Chain_NN material via a colour picker popup."""
    bl_idname  = 'fbxmt.add_chain_material'
    bl_label   = 'Add Chain Material'
    bl_options = {'REGISTER', 'UNDO'}

    # Starting colours — both editable live in the panel after creation.
    color_a: FloatVectorProperty(
        name    = 'Colour A',
        subtype = 'COLOR',
        min=0.0, max=1.0,
        default = CHECKER_BLUE_RGB,
    )
    color_b: FloatVectorProperty(
        name    = 'Colour B',
        subtype = 'COLOR',
        min=0.0, max=1.0,
        default = (0.85, 0.35, 0.05),
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=240)

    def draw(self, context):
        layout = self.layout
        layout.label(text='Starting colours (editable after creation):')
        row = layout.row(align=True)
        row.prop(self, 'color_a', text='A')
        row.prop(self, 'color_b', text='B')

    def execute(self, context):
        idx  = _next_chain_index()
        name = f'{CHAIN_PREFIX}{idx:02d}'

        if name in bpy.data.materials:
            self.report({'WARNING'}, f'{name} already exists')
            return {'CANCELLED'}

        # Normalise lightness on B to match visual consistency
        h, _l, s = colorsys.rgb_to_hls(self.color_b[0], self.color_b[1], self.color_b[2])
        color_b  = colorsys.hls_to_rgb(h, CHAIN_COLOR_LIGHTNESS, s)
        color_a  = tuple(self.color_a)

        mat = bpy.data.materials.new(name=name)
        _build_checker_node_tree(mat, color_a, color_b)

        # Push to active object immediately (poll already guarantees a mesh is active)
        obj  = context.active_object
        mesh = obj.data
        if mat.name not in {m.name for m in mesh.materials if m}:
            mesh.materials.append(mat)

        self.report({'INFO'}, f'Created {name} and added to {obj.name}')
        return {'FINISHED'}


def _resolve_selected_chain(context):
    """Return the island/chain material selected in the Islands list, or None.

    The Islands UIList is now object-scoped - it points into the active
    object's mesh.materials, not bpy.data.materials. fbxmt_island_list_index
    is the raw slot index on the object. filter_items hides non-chain entries
    visually but does not remap the index, so we confirm the resolved material
    is actually a chain material before returning it.
    """
    obj = context.active_object
    if not obj or obj.type != 'MESH':
        return None
    all_mats = list(obj.data.materials)
    idx = context.scene.fbxmt_island_list_index
    if idx < 0 or idx >= len(all_mats):
        return None
    mat = all_mats[idx]
    return mat if _is_chain_material(mat) else None


class OT_FBXMT_Delete_Chain_Material(Operator):
    """Delete the selected chain material. Chain_01 is protected."""
    bl_idname  = 'fbxmt.delete_chain_material'
    bl_label   = 'Delete Chain Material'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        mat = _resolve_selected_chain(context)
        return mat is not None and not _is_locked_chain(mat)

    def execute(self, context):
        mat = _resolve_selected_chain(context)
        if mat is None:
            self.report({'WARNING'}, 'No chain material selected - select one in the list')
            return {'CANCELLED'}
        if _is_locked_chain(mat):
            self.report({'WARNING'}, f'{CHAIN_LOCKED_NAME} is locked and cannot be deleted')
            return {'CANCELLED'}
        idx  = context.scene.fbxmt_island_list_index
        name = mat.name
        bpy.data.materials.remove(mat, do_unlink=True)
        self.report({'INFO'}, f'Deleted {name}')
        obj = context.active_object
        remaining = len(obj.data.materials) if obj and obj.type == 'MESH' else 0
        context.scene.fbxmt_island_list_index = min(idx, max(0, remaining - 1))
        return {'FINISHED'}


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


# ─── UIList: island/chain materials (object-scoped) ───────────────────────────

class FBXMT_UL_ChainMaterials(bpy.types.UIList):
    bl_idname = 'FBXMT_UL_chain_materials'

    def draw_item(self, _ctx, layout, _data, item, _icon, _adata, _aprop, _index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            alias = _island_alias(item.name) if item else item.name
            row.label(text=alias, icon_value=item.preview.icon_id)
            if item.name == CHAIN_LOCKED_NAME:
                row.label(text='', icon='LOCKED')
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

    fbxmt_active_list tracks which list ('BASE' or 'ISLAND') the user last
    interacted with. fbxmt_base_selected / fbxmt_island_selected are the
    authoritative selection flags - they go False when the other list is
    clicked, giving us a true deselected state that Blender's integer index
    prop cannot express on its own (it clamps to 0, never -1).
    Returns None if no list has an active selection or no mesh is active.
    """
    obj = context.active_object
    if not obj or obj.type != 'MESH':
        return None
    mesh     = obj.data
    all_mats = list(mesh.materials)
    scene    = context.scene

    active_list = getattr(scene, 'fbxmt_active_list', 'BASE')
    if active_list == 'BASE':
        if not scene.fbxmt_base_selected:
            return None
        idx = scene.fbxmt_base_list_index
        mat = all_mats[idx] if 0 <= idx < len(all_mats) else None
        return mat if (mat and mat.name in _BASE_MAT_ALIASES) else None
    else:
        if not scene.fbxmt_island_selected:
            return None
        idx = scene.fbxmt_island_list_index
        mat = all_mats[idx] if 0 <= idx < len(all_mats) else None
        return mat if (mat and _is_chain_material(mat)) else None


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
                    _is_chain_material(slot)
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


# ─── Texel density operator ──────────────────────────────────────────────

class OT_FBXMT_Set_Texel_Density(Operator):
    bl_idname  = 'fbxmt.set_texel_density'
    bl_label   = 'Set Texel Density'
    bl_options = {'REGISTER', 'UNDO'}

    value: bpy.props.IntProperty(default=1024)

    def execute(self, context):
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
    """Return the currently selected island/chain material from the active object, or None.

    Returns None when the island list has no active selection (fbxmt_island_selected
    is False), which happens after the user clicks in the base materials list.
    """
    if not getattr(scene, 'fbxmt_island_selected', False):
        return None
    obj = bpy.context.active_object
    if not obj or obj.type != 'MESH':
        return None
    all_mats = list(obj.data.materials)
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


def _on_color_a_update(scene, _context):
    """User edited colour A - write it back to the selected chain's node tree."""
    if _syncing_selection:
        return
    mat = _get_selected_chain(scene)
    if mat is None:
        return
    rgb_b = _read_chain_color_b(mat)
    if rgb_b is None:
        return
    _write_chain_colors(mat, tuple(scene.fbxmt_chain_color_a), rgb_b)
    _tag_redraw()


def _on_color_b_update(scene, _context):
    """User edited colour B - normalise lightness and write back to node tree."""
    global _syncing_selection
    if _syncing_selection:
        return
    mat = _get_selected_chain(scene)
    if mat is None:
        return
    rgb_a = _read_chain_color_a(mat)
    if rgb_a is None:
        return
    raw = scene.fbxmt_chain_color_b
    h, _l, s = colorsys.rgb_to_hls(raw[0], raw[1], raw[2])
    normalised_b = colorsys.hls_to_rgb(h, CHAIN_COLOR_LIGHTNESS, s)
    _write_chain_colors(mat, rgb_a, normalised_b)
    # Reflect the normalised value back into the prop without re-triggering
    _syncing_selection = True
    try:
        scene.fbxmt_chain_color_b = normalised_b
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
        update      = _on_color_a_update,
    )
    bpy.types.Scene.fbxmt_chain_color_b = FloatVectorProperty(
        name        = 'Colour B',
        description = 'Second checkerboard colour for the selected island material (lightness normalised)',
        subtype     = 'COLOR',
        min=0.0, max=1.0,
        default     = (0.85, 0.35, 0.05),
        update      = _on_color_b_update,
    )
    def _on_island_index(scene, ctx):
        scene.fbxmt_active_list    = 'ISLAND'
        scene.fbxmt_island_selected = True
        scene.fbxmt_base_selected   = False
        _sync_color_from_selection(scene, ctx)

    def _on_base_index(scene, _ctx):
        scene.fbxmt_active_list    = 'BASE'
        scene.fbxmt_base_selected   = True
        scene.fbxmt_island_selected = False

    bpy.types.Scene.fbxmt_island_list_index = bpy.props.IntProperty(
        name    = 'Active Island Material',
        default = 0,
        update  = _on_island_index,
    )
    bpy.types.Scene.fbxmt_base_list_index = bpy.props.IntProperty(
        name    = 'Active Base Material',
        default = 0,
        update  = _on_base_index,
    )
    bpy.types.Scene.fbxmt_active_list = bpy.props.EnumProperty(
        name  = 'Active Material List',
        items = [('BASE', 'Base', ''), ('ISLAND', 'Island', '')],
        default = 'BASE',
    )
    bpy.types.Scene.fbxmt_base_selected = bpy.props.BoolProperty(
        name    = 'Base List Has Selection',
        default = False,
    )
    bpy.types.Scene.fbxmt_island_selected = bpy.props.BoolProperty(
        name    = 'Island List Has Selection',
        default = False,
    )


def unregister_material_props():
    del bpy.types.Scene.fbxmt_chain_color_a
    del bpy.types.Scene.fbxmt_chain_color_b
    del bpy.types.Scene.fbxmt_island_list_index
    del bpy.types.Scene.fbxmt_base_list_index
    del bpy.types.Scene.fbxmt_active_list
    del bpy.types.Scene.fbxmt_base_selected
    del bpy.types.Scene.fbxmt_island_selected
