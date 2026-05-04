# Contributing to FBX Mapper's Toolkit

Thanks for your interest. Here's how to help.

---

## Reporting bugs

Use the [Issues](../../issues) tab. Include:

- Blender version
- FBXMT version (shown in the N-panel header)
- What you did, what you expected, what happened
- Console output if there's a Python traceback (Window → Toggle System Console)

---

## Suggesting features

Also the Issues tab — label it `enhancement`. Be specific about the workflow problem you're solving, not just the feature you want.

---

## Architecture rules (read before touching code)

These are non-negotiable — changes that violate them won't be merged:

- **`_get_prefs()`** — single canonical version in `materials.py`, imported everywhere. Do not duplicate.
- **`_suppress_handler`** — module-level flag in `materials.py`, set via `_mat_module._suppress_handler` during slot mutations. Do not move.
- **`ADDON_ID` and `set_addon_id()`** — in `panel.py`, set at `register()` time.
- **`ensure_lightmap_channel(mesh, force_regenerate, obj=None)`** — requires `obj` to enter Edit mode for `lightmap_pack`.
- **Registration** — manual `register_class` for Operator, Panel, PropertyGroup, Menu, UIList. `AddonPreferences` is never in the classes list.
- **Property storage** — preferences go on `scene.fbxmt_prefs_global` (FBXMT_GlobalPrefs) or `scene.fbxmt_props` (FBXMT_Props). Not on AddonPreferences. See `props.py`.
- **No PNG files** — all materials are fully node-based.
- **Unwrap algorithm, chain stitching, packer algorithm** — do not change without discussion.

---

## Code style

- Self-documenting names — if you need a comment to explain what a variable is, rename it
- No multiline lambdas
- No named lambdas (if it's worth naming, it's worth `def`-ing)
- Module-level imports only — no local imports inside functions unless unavoidable
- `new_node(node_type, x, y)` for all shader node creation in `_build_checker_node_tree`
- ASCII only in user-visible strings (report messages, labels, descriptions) — Windows console encoding

---

## Submitting changes

1. Fork the repo
2. Create a branch named `fix/description` or `feature/description`
3. Make your changes — one logical change per commit
4. Open a pull request with a clear description of what changed and why
