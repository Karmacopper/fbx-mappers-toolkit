# props.py — FBX Mapper's Toolkit
#
# All PropertyGroup definitions live here.
#
# WHY NOT AddonPreferences?
#   Blender 5.x extensions load under a prefixed package name at runtime
#   (e.g. bl_ext.user_default.fbx_mappers_toolkit). AddonPreferences requires
#   bl_idname to match the package name exactly, and that name changes between
#   a local source install and an extension install. Storing preferences on
#   the Scene as PointerProperties sidesteps this entirely — the data lives
#   with the blend file, persists through saves, and is unaffected by the
#   package prefix.
#
# WHERE ARE PREFERENCES STORED?
#   context.scene.fbxmt_prefs_global  ->  FBXMT_GlobalPrefs
#     Addon-wide settings: checker appearance, material colours, workflow
#     defaults. Saved with the blend file. Set once per project (or per
#     startup template) and left alone.
#
#   context.scene.fbxmt_props         ->  FBXMT_Props
#     Per-scene operational settings: export path, texel density, FBX scale
#     options, lightmap behaviour. These are the values that vary between
#     projects and are intentionally per-blend-file.
#
# REGISTRATION
#   Both groups are registered as PointerProperties on bpy.types.Scene in
#   __init__.py after register_class() has run for each group.

import bpy
from bpy.types import PropertyGroup


_COLOR_B_MODE_ITEMS = [
    ('DARKER',    'Darker',    'Derive B by darkening A'),
    ('LIGHTER',   'Lighter',   'Derive B by lightening A'),
    ('GREYSCALE', 'Greyscale', 'B is a fixed grey value'),
    ('INVERSE',   'Inverse',   'B is the complementary hue of A'),
]

PATTERN_ITEMS = [
    ('SQUARE',   'Square',   'Standard checkerboard squares'),
    ('DIAGONAL', 'Diagonal', 'Each square split diagonally into two triangles'),
    ('DIAMOND',  'Diamond',  'Each square split into four triangles forming diamonds'),
    ('CIRCLE',   'Circle',   'Circle inscribed in each square'),
]




class FBXMT_GlobalPrefs(PropertyGroup):
    """Addon-wide preferences. Stored on Scene.fbxmt_prefs_global.
    Persists with the blend file and in the startup template.
    See props.py for the full explanation of why this is not AddonPreferences.
    """

    prep_on_import: bpy.props.BoolProperty(
        name="Full Prep on Import (Geo)",
        description=(
            "On importing as Geo: strip foreign materials, clear all UV maps, "
            "auto-assign M_FBXMT materials by normal direction, unwrap, "
            "and generate a LightmapUVs channel. One click, ready to chain-mark."
        ),
        default=False,
    )
    checker_scale: bpy.props.IntProperty(
        name="Checker Squares/Tile",
        description="Number of checker squares per UV tile — powers of 2. Applies on Rebuild.",
        default=4, min=1, max=64,
    )
    corner_mark_preset: bpy.props.IntProperty(
        name="Corner Mark Length",
        description="Cross arm length preset: 1=12.5%, 2=25%, 3=37.5%, 4=50% of texel tile. Applies on Rebuild.",
        default=2, min=1, max=4,
    )
    corner_mark_width_px: bpy.props.IntProperty(
        name="Corner Mark Width (px)",
        description="Width of each corner cross arm in pixels at 1024tx/m. Applies on Rebuild.",
        default=4, min=2, max=8,
    )
    show_corner_circle: bpy.props.BoolProperty(
        name="Show Corner Circle",
        description="Draw quarter-circle arcs at tile corners (radius = half arm length). Applies on Rebuild.",
        default=True,
    )
    show_corner_lines: bpy.props.BoolProperty(
        name="Lines",
        description="Extend lines along all tile edges (preset 4). Off = short reticle arms (preset 2). Circle always uses preset 2.",
        default=False,
    )

    # Base material checker colours — A and B for each surface type.
    # Applied on Rebuild. B defaults are 70% darkened versions of A.
    color_floor_a:   bpy.props.FloatVectorProperty(name="Floor A",   subtype='COLOR', min=0, max=1, default=(0.3,  0.75, 0.3,  1.0), size=4)
    color_floor_b:   bpy.props.FloatVectorProperty(name="Floor B",   subtype='COLOR', min=0, max=1, default=(0.2,  0.52, 0.2,  1.0), size=4)
    color_ceiling_a: bpy.props.FloatVectorProperty(name="Ceiling A", subtype='COLOR', min=0, max=1, default=(0.3,  0.55, 0.9,  1.0), size=4)
    color_ceiling_b: bpy.props.FloatVectorProperty(name="Ceiling B", subtype='COLOR', min=0, max=1, default=(0.2,  0.38, 0.63, 1.0), size=4)
    color_wall_a:    bpy.props.FloatVectorProperty(name="Wall A",    subtype='COLOR', min=0, max=1, default=(0.9,  0.65, 0.2,  1.0), size=4)
    color_wall_b:    bpy.props.FloatVectorProperty(name="Wall B",    subtype='COLOR', min=0, max=1, default=(0.63, 0.45, 0.14, 1.0), size=4)
    color_trim_a:    bpy.props.FloatVectorProperty(name="Trim A",    subtype='COLOR', min=0, max=1, default=(0.75, 0.3,  0.75, 1.0), size=4)
    color_trim_b:    bpy.props.FloatVectorProperty(name="Trim B",    subtype='COLOR', min=0, max=1, default=(0.52, 0.2,  0.52, 1.0), size=4)
    color_ignore_a:  bpy.props.FloatVectorProperty(name="Ignore A",  subtype='COLOR', min=0, max=1, default=(0.25, 0.25, 0.25, 1.0), size=4)
    color_ignore_b:  bpy.props.FloatVectorProperty(name="Ignore B",  subtype='COLOR', min=0, max=1, default=(0.15, 0.15, 0.15, 1.0), size=4)
    color_island_a:  bpy.props.FloatVectorProperty(name="Island A",  subtype='COLOR', min=0, max=1, default=(0.08, 0.55, 0.90, 1.0), size=4)
    color_island_b:  bpy.props.FloatVectorProperty(name="Island B",  subtype='COLOR', min=0, max=1, default=(0.50, 0.50, 0.50, 1.0), size=4)

    corner_hue_shift: bpy.props.FloatProperty(
        name="Corner Line Hue Shift",
        description=(
            "Shifts the hue of the corner mark lines away from their default position. "
            "Default (180) = fully inverted checker colour, maximum contrast. "
            "Reduce toward 0 or increase toward +/-180 to tint lines to any hue. "
            "At 0 the lines are the same hue as the checker (low contrast). "
            "Applies on Rebuild."
        ),
        default=180.0,
        min=-180.0,
        max=180.0,
        step=100,
        precision=1,
    )
    bake_labels: bpy.props.BoolProperty(
        name="Label Grid Squares",
        description="Overlay A1-H8 grid coordinate labels on baked material PNGs",
        default=True,
    )

    # ── New wave — Setup V2 ────────────────────────────────────────────────
    # Single anchor hue drives all material A colours via fixed derivation.
    # S=1.0, L=0.5 fixed. B colours derived by one global mode.
    anchor_hue: bpy.props.FloatProperty(
        name="Anchor Hue",
        description="Base hue from which all material colours are derived. "
                    "0=red, 0.333=green, 0.667=blue. "
                    "Wall=H, Floor=H+120°, Ceiling=H+240°, Trim=H+270°.",
        default=0.0, min=0.0, max=1.0, step=1, precision=3,
        subtype='NONE',
    )
    color_b_mode: bpy.props.EnumProperty(
        name="Colour B Mode",
        description="Global B colour derivation mode — applies to all materials simultaneously",
        items=_COLOR_B_MODE_ITEMS,
        default='DARKER',
    )
    color_b_notch: bpy.props.IntProperty(
        name="B Notch",
        description="Lightness shift amount: 1=25%  2=50%  3=75%",
        default=1, min=1, max=3,
    )

    # Per-material checker patterns (6 materials + island group)
    checker_pattern_wall: bpy.props.EnumProperty(
        name="Wall Pattern", items=PATTERN_ITEMS, default='SQUARE')
    checker_pattern_floor: bpy.props.EnumProperty(
        name="Floor Pattern", items=PATTERN_ITEMS, default='SQUARE')
    checker_pattern_ceiling: bpy.props.EnumProperty(
        name="Ceiling Pattern", items=PATTERN_ITEMS, default='SQUARE')
    checker_pattern_trim: bpy.props.EnumProperty(
        name="Trim Pattern", items=PATTERN_ITEMS, default='SQUARE')
    checker_pattern_ignore: bpy.props.EnumProperty(
        name="Ignore Pattern", items=PATTERN_ITEMS, default='SQUARE')
    checker_pattern_island: bpy.props.EnumProperty(
        name="Island Pattern", description="Pattern for Island Marker and all island sub-materials",
        items=PATTERN_ITEMS, default='CIRCLE')
    island_swap_ab: bpy.props.BoolProperty(
        name="Swap A/B",
        description="Swap colour A and B for all island sub-materials",
        default=False,
    )

    apex_line_seed: bpy.props.IntProperty(
        name="Apex Line Seed",
        description="Random seed controlling the angle assigned to each apex position line. "
                    "Different seeds produce different line patterns — part of the preset identity.",
        default=42,
        min=0,
        max=9999,
    )

class FBXMT_Props(PropertyGroup):
    """Per-scene operational settings. Stored on Scene.fbxmt_props.
    These vary between projects and are intentionally per-blend-file.
    See props.py for the full explanation of the property storage model.
    """

    export_path: bpy.props.StringProperty(
        name="Export Folder",
        description="Export destination - saved with the blend file",
        maxlen=1024,
        subtype="DIR_PATH",
    )
    geo_texel_density: bpy.props.IntProperty(
        name="Texel Density",
        description="Texel density for Geo collection objects (texels/m). 1024tx/m = 1m tile.",
        default=1024, min=512, max=8192,
    )
    contact_sheet_size: bpy.props.IntProperty(
        name="Contact Sheet Size",
        description="Render resolution per tile for the contact sheet (px)",
        default=256, min=256, max=8192,
    )
    contact_sheet_full: bpy.props.BoolProperty(
        name="Full Sheet",
        description="Include all 21 tiles (6 materials + 15 island sub-materials) in a 3×7 grid",
        default=False,
    )
    apply_scale_options: bpy.props.EnumProperty(
        name="Apply Scale",
        description="FBX scale option",
        items=[
            ('FBX_SCALE_NONE',   "None",   ""),
            ('FBX_SCALE_UNITS',  "Units",  ""),
            ('FBX_SCALE_CUSTOM', "Custom", ""),
            ('FBX_SCALE_ALL',    "All",    ""),
        ],
    )
    apply_scale: bpy.props.BoolProperty(
        name="Apply Unit Scale",
        description="Apply the selected FBX scale option",
    )
    ucx_generate: bpy.props.BoolProperty(
        name="Generate UCX Collision",
        description="Add a UCX_ prefixed collision hull to the exported FBX",
    )
    lightmap_force_regenerate: bpy.props.BoolProperty(
        name="Force Regenerate Lightmap",
        description=(
            "Off: guarantee a LightmapUVs channel exists - create if missing, leave if present.\n"
            "On: always regenerate LightmapUVs fresh, overwriting whatever was there"
        ),
        default=False,
    )
    uv_floor_threshold: bpy.props.FloatProperty(
        name="Floor Angle",
        description="Faces within this angle of horizontal are treated as floors/ceilings",
        default=45.0,
        min=0.0, max=89.0, step=5, precision=1,
    )
    bake_textures: bpy.props.BoolProperty(
        name="Bake Material Textures",
        description="Bake each material to a PNG in the Textures/ subfolder on export",
        default=True,
    )
    import_path: bpy.props.StringProperty(
        name="Import Folder",
        description="Default folder for Quick Import file browser",
        subtype='DIR_PATH',
        default="",
    )
    quick_import_type: bpy.props.EnumProperty(
        name="Quick Import Type",
        description="Import type used by the Quick Import button",
        items=[
            ('GEO',  "Import as Geo",  "Import FBX and prep as Geo collection mesh"),
            ('TRIM', "Import as Trim", "Import FBX and assign Trim material to all faces"),
            ('PROP', "Import as Prop", "Import FBX as-is into Props collection"),
        ],
        default='GEO',
    )

    # ── Project Setup window state ──────────────────────────────────────────
    fbxmt_selected_mat_index: bpy.props.IntProperty(
        name='Selected Material',
        description='Index into ALL_DISPLAY_MATERIAL_NAMES for the Project Setup list',
        default=0, min=0, max=5,
    )
    fbxmt_preview_stale: bpy.props.BoolProperty(
        name='Preview Stale',
        description='True when colours or texel density changed since last preview render',
        default=True,
    )
    fbxmt_cache_hash: bpy.props.StringProperty(
        name='Cache Hash',
        default='',
        description='MD5 of geo_texel_density|checker_scale at last Pre-Bake All',
    )
    fbxmt_is_fresh_template: bpy.props.BoolProperty(
        name='Fresh Template',
        default=False,
        description='Set by Save Template operator; cleared by load_post after firing Project Setup',
    )
