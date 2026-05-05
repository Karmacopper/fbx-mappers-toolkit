# uv_pack.py — FBX Mapper's Toolkit v2.1.5
# UV Island Packer: MaxRects algorithm (no rotation, 0+ UV space, no overlaps)
#
# Algorithm: Maximal Rectangles (Best Short Side Fit heuristic)
# Significantly outperforms shelf packing for irregular island sets,
# adapts to elongated layouts, and respects the no-rotation constraint.
#
# This is an original Python implementation of the algorithm described in:
#   Jylänki, J. (2010). "A Thousand Ways to Pack the Bin — A Practical
#   Approach to Two-Dimensional Rectangle Bin Packing."
#   http://clb.demon.fi/files/RectangleBinPack.pdf
#
# No code from Jylänki's C++ reference implementation or any derived
# library was copied. Citation is for academic credit only.
# This file is licensed under GPL v3, same as the parent addon.

import math
from collections import namedtuple

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

MARGIN = 0.0   # UV-unit gap between islands — zero for seamless tiling


def pack_islands(bm, uv_layer, groups):
    """Pack UV islands in-place without rotation.

    Args:
        bm:        BMesh (used only for face iteration, not modified directly here)
        uv_layer:  Active UV layer from bm.loops.layers.uv
        groups:    list[list[BMFace]] — each sub-list is one UV island whose
                   faces already carry UV coordinates from the projection step.

    The function translates each island's UV coordinates so they pack
    tightly in the [0+, 0+] half-plane with no overlaps.
    Rotation is never applied (wall face Z-up orientation is preserved).
    UVs may exceed 1.0 on either axis; UE5 handles this fine.
    """
    if not groups:
        return

    # 1. Measure every island — skip degenerate zero-area islands which
    #    would all collapse to position (0,0) and cause overlaps.
    MIN_DIM = 1e-6  # UV units — anything smaller is degenerate
    islands = []
    for group in groups:
        isl = _Island(group, uv_layer)
        if isl.w > MIN_DIM and isl.h > MIN_DIM:
            islands.append(isl)

    if not islands:
        return

    # 2. Sort largest-area-first (greedy insertion works better this way).
    islands.sort(key=lambda isl: isl.area, reverse=True)

    # 3. Estimate a good initial bin width.
    total_area = sum(isl.area for isl in islands)
    # Use a slightly wider initial estimate than pure sqrt to reduce the
    # number of rows needed; tuned empirically for map-geometry aspect ratios.
    bin_w = _estimate_bin_width(islands, total_area)

    # 4. Run MaxRects.  If packing somehow fails (shouldn't with infinite
    #    height), fall back to the shelf packer so the addon never crashes.
    try:
        placements = _maxrects_pack(islands, bin_w)
    except Exception:
        placements = _shelf_pack_fallback(islands, bin_w)

    # 5. Apply translations back to the actual UV loops.
    for isl, (tx, ty) in zip(islands, placements):
        dx = tx - isl.min_u
        dy = ty - isl.min_v
        for face in isl.faces:
            for loop in face.loops:
                uv = loop[uv_layer].uv
                uv.x += dx
                uv.y += dy


# ---------------------------------------------------------------------------
# Island data class
# ---------------------------------------------------------------------------

class _Island:
    """Lightweight wrapper around a group of BMFaces that caches UV bounds."""

    __slots__ = ("faces", "min_u", "min_v", "max_u", "max_v", "w", "h", "area")

    def __init__(self, faces, uv_layer):
        self.faces = faces
        min_u = min_v = math.inf
        max_u = max_v = -math.inf
        for face in faces:
            for loop in face.loops:
                u, v = loop[uv_layer].uv
                if u < min_u: min_u = u
                if u > max_u: max_u = u
                if v < min_v: min_v = v
                if v > max_v: max_v = v
        self.min_u = min_u
        self.min_v = min_v
        self.max_u = max_u
        self.max_v = max_v
        self.w = max_u - min_u
        self.h = max_v - min_v
        self.area = self.w * self.h


# ---------------------------------------------------------------------------
# Bin-width estimator
# ---------------------------------------------------------------------------

def _estimate_bin_width(islands, total_area):
    """Heuristically choose a bin width that tends toward square output.

    We iterate a short candidate list and pick the width whose resulting
    aspect ratio (estimated from a mock shelf pass) is closest to 1:1.
    This is cheap and avoids the elongated-layout problem of a fixed sqrt.
    """
    sqrt_area = math.sqrt(total_area) if total_area > 0 else 1.0

    # Widest single island sets a hard lower bound.
    min_w = max((isl.w for isl in islands), default=1.0) + MARGIN + 1e-6

    best_w = sqrt_area * 1.2
    best_ratio_err = math.inf

    # Include wider factors and sum-of-two-widest as candidates —
    # helps when the layout has two natural clusters that fit side by side.
    top2_w = sum(sorted((isl.w for isl in islands), reverse=True)[:2]) + MARGIN
    candidates = [sqrt_area * f for f in (0.8, 0.9, 1.0, 1.1, 1.2, 1.4, 1.6, 1.8, 2.0, 2.5, 3.0)]
    candidates.append(top2_w)

    for candidate in candidates:
        candidate = max(candidate, min_w)
        total_h = _shelf_estimate(islands, candidate)
        if total_h == 0:
            continue
        aspect = candidate / total_h
        err = abs(math.log(aspect))  # 0 = perfect square
        if err < best_ratio_err:
            best_ratio_err = err
            best_w = candidate

    return max(best_w, min_w)


def _shelf_estimate(islands, bin_w):
    """Quick O(n) shelf simulation. Returns total packed height."""
    x = y = shelf_h = 0.0
    for isl in islands:
        iw = isl.w + MARGIN
        ih = isl.h + MARGIN
        if x + iw > bin_w and x > 0:
            y += shelf_h
            x = 0.0
            shelf_h = 0.0
        x += iw
        if ih > shelf_h: shelf_h = ih
    return y + shelf_h


# ---------------------------------------------------------------------------
# MaxRects packer
# ---------------------------------------------------------------------------

Rect = namedtuple("Rect", ["x", "y", "w", "h"])


def _maxrects_pack(islands, bin_w):
    """Place each island using the Maximal Rectangles / BSSF heuristic.

    Returns a list of (x, y) placements in the same order as `islands`.
    The bin has infinite height (extends downward as needed).

    BSSF = Best Short Side Fit: choose the free rectangle whose shorter
    leftover side after placing the island is minimised.  This tends to
    produce compact, square-ish layouts.
    """
    # Start with one large free rectangle covering the whole usable area.
    # We cap the initial height generously; it's extended automatically.
    INF_H = 1e9
    free_rects = [Rect(0.0, 0.0, bin_w, INF_H)]
    placements = []

    for isl in islands:
        iw = isl.w + MARGIN
        ih = isl.h + MARGIN
        best_rect, best_score = _find_best_rect(free_rects, iw, ih)

        if best_rect is None:
            # bin_w too narrow for this island — place below everything
            # and expand the bin by updating free_rects.
            max_y = max((p[1] + islands[i].h + MARGIN
                         for i, p in enumerate(placements)), default=0.0)
            placements.append((0.0, max_y))
            placed = Rect(0.0, max_y, iw, ih)
            free_rects = _update_free_rects(free_rects, placed)
            continue

        px, py = best_rect.x, best_rect.y
        placements.append((px, py))

        placed = Rect(px, py, iw, ih)
        free_rects = _update_free_rects(free_rects, placed)

    return placements


def _find_best_rect(free_rects, iw, ih):
    """BSSF: minimise the shorter leftover dimension."""
    best = None
    best_score = math.inf
    for r in free_rects:
        if r.w >= iw and r.h >= ih:
            short_side = min(r.w - iw, r.h - ih)
            if short_side < best_score:
                best_score = short_side
                best = r
    return best, best_score


def _update_free_rects(free_rects, placed):
    """Split all free rectangles that overlap `placed`, then prune contained ones."""
    new_free = []
    for r in free_rects:
        if _overlaps(r, placed):
            # Split r around placed and keep valid sub-rects.
            new_free.extend(_split_rect(r, placed))
        else:
            new_free.append(r)

    # Remove any free rect that is fully contained in another.
    new_free = _prune_contained(new_free)
    return new_free


def _overlaps(a, b):
    """True if rectangles a and b share any area."""
    return (a.x < b.x + b.w and a.x + a.w > b.x and
            a.y < b.y + b.h and a.y + a.h > b.y)


def _split_rect(r, placed):
    """Return up to 4 sub-rectangles of r that don't overlap placed."""
    result = []
    # Left slice
    if placed.x > r.x:
        result.append(Rect(r.x, r.y, placed.x - r.x, r.h))
    # Right slice
    if placed.x + placed.w < r.x + r.w:
        result.append(Rect(placed.x + placed.w, r.y,
                           r.x + r.w - (placed.x + placed.w), r.h))
    # Bottom slice (UV y increases upward, but we treat y as increasing upward too)
    if placed.y > r.y:
        result.append(Rect(r.x, r.y, r.w, placed.y - r.y))
    # Top slice
    if placed.y + placed.h < r.y + r.h:
        result.append(Rect(r.x, placed.y + placed.h,
                           r.w, r.y + r.h - (placed.y + placed.h)))
    return result


def _prune_contained(rects):
    """Remove rectangles fully contained within another in the list."""
    to_remove = set()
    n = len(rects)
    for i in range(n):
        if i in to_remove:
            continue
        for j in range(n):
            if i == j or j in to_remove:
                continue
            if _contains(rects[j], rects[i]):
                to_remove.add(i)
                break
    return [r for k, r in enumerate(rects) if k not in to_remove]


def _contains(outer, inner):
    """True if outer fully contains inner."""
    return (outer.x <= inner.x and outer.y <= inner.y and
            outer.x + outer.w >= inner.x + inner.w and
            outer.y + outer.h >= inner.y + inner.h)


# ---------------------------------------------------------------------------
# Shelf-pack fallback (kept from original, used only if MaxRects errors out)
# ---------------------------------------------------------------------------

def _shelf_pack_fallback(islands, bin_w):
    """Simple left-to-right shelf packer.  O(n), no guarantees on density."""
    placements = []
    x = y = shelf_h = 0.0
    for isl in islands:
        iw = isl.w + MARGIN
        ih = isl.h + MARGIN
        if x + iw > bin_w and x > 0:
            y += shelf_h
            x = 0.0
            shelf_h = 0.0
        placements.append((x, y))
        x += iw
        if ih > shelf_h:
            shelf_h = ih
    return placements
