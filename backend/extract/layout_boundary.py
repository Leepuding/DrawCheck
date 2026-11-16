"""Source-pixel boundary candidates for an opt-in v2 locator experiment."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from math import ceil, floor
from statistics import median

import numpy as np
from PIL import Image

from backend.extract.layout_views import VERSION as LOCATOR_VERSION


VERSION = "layout-boundary-candidate-v1"
INK_THRESHOLD = 180
MAX_SEARCH_HEIGHTS = 8
MAX_SEARCH_PAGE_FRACTION = .20
NOTES_GAP_HEIGHTS = .75
VIEW_GAP_HEIGHTS = 2.5
PADDING_HEIGHTS = .25
VIEW_PASSES = 3
SIDE_NAMES = (("left", "right"), ("top", "bottom"))


def _runs(active):
    boundaries = np.flatnonzero(np.diff(np.r_[False, active, False]))
    return [(int(start), int(end)) for start, end in boundaries.reshape(-1, 2)]


def _pixel_box(bbox, width, height):
    return (floor(bbox["x0"] * width), floor(bbox["y0"] * height),
            ceil(bbox["x1"] * width), ceil(bbox["y1"] * height))


def _crop(mask, box):
    left, top, right, bottom = box
    return mask[top:bottom, left:right]


def _note_bands(ink, primary, context):
    left, _, right, _ = primary
    _, top, _, bottom = context
    strip = ink[top:bottom, left:right]
    runs = _runs(strip.sum(axis=1) >= max(3, round(strip.shape[1] * .0075)))
    bands = []
    for start, end in runs:
        if bands and top + start - bands[-1][1] <= 1:
            bands[-1] = (bands[-1][0], top + end)
        else:
            bands.append((top + start, top + end))
    return [(start, end) for start, end in bands if end - start >= 3]


def _grow_projection(active, offset, seed, gap_limit, padding):
    """Join anchored ink runs, leaving an auditable stop on each side."""
    runs = [(start + offset, end + offset) for start, end in _runs(active)]
    anchors = [index for index, (start, end) in enumerate(runs) if start < seed[1] and end > seed[0]]
    if not anchors:
        return seed, [{"stop": "no_anchor_ink"}, {"stop": "no_anchor_ink"}]
    first, last = anchors[0], anchors[-1]
    edges = []
    for step, index, edge in ((-1, first, offset), (1, last, offset + len(active))):
        joins = []
        neighbor = index + step
        while 0 <= neighbor < len(runs):
            lower, upper = sorted((index, neighbor))
            gap = runs[upper][0] - runs[lower][1]
            if gap > gap_limit:
                break
            joins.append({"white_gap_px": gap, "joined_ink": list(runs[neighbor])})
            index = neighbor
            neighbor += step
        ink_edge = runs[index][0 if step < 0 else 1]
        if 0 <= neighbor < len(runs):
            stop = "separating_whitespace"
            blank = gap
        else:
            blank = abs(edge - ink_edge)
            stop = "no_ink_within_search" if blank > gap_limit else "search_limit_near_ink"
        edges.append({"stop": stop, "white_gap_px": blank, "joined": joins, "last_ink": ink_edge})
    low = max(offset, edges[0]["last_ink"] - padding)
    high = min(offset + len(active), edges[1]["last_ink"] + padding)
    return (min(low, seed[0]), max(high, seed[1])), edges


def _search_box(original, width, height, reach):
    left, top, right, bottom = original
    return (max(0, left - reach), max(0, top - reach), min(width, right + reach), min(height, bottom + reach))


def _notes_candidate(ink, bands, original, primary, window, gap, padding):
    left, top, right, bottom = original
    x0, _, x1, _ = window
    checks = {side: {"stop": "no_anchored_line_band", "bands": []} for side in SIDE_NAMES[0]}
    for y0, y1 in bands:
        active = ink[y0:y1, x0:x1].any(axis=0)
        (start, end), edges = _grow_projection(active, x0, (primary[0], primary[2]), gap, padding)
        left, right = min(left, start), max(right, end)
        for side, evidence in zip(SIDE_NAMES[0], edges):
            checks[side]["stop"] = "see_line_band_stops"
            checks[side]["bands"].append({"line_band_y": [y0, y1], **evidence})
    for side in SIDE_NAMES[1]:
        checks[side] = {"stop": "vertical_notes_boundary_unassessed"}
    return (left, top, right, bottom), checks


def _view_candidate(ink, original, window, gap, padding):
    candidate = list(original)
    checks = {}
    for _ in range(VIEW_PASSES):
        previous = candidate.copy()
        for axis, sides in enumerate(SIDE_NAMES):
            strip = candidate.copy()
            strip[axis], strip[axis + 2] = window[axis], window[axis + 2]
            active = _crop(ink, strip).sum(axis=axis) >= 2
            pair, edges = _grow_projection(active, window[axis], (original[axis], original[axis + 2]), gap, padding)
            candidate[axis], candidate[axis + 2] = pair
            checks.update(zip(sides, edges))
        if candidate == previous:
            break
    else:
        return tuple(candidate), checks, True
    return tuple(candidate), checks, False


def _neighbor_ink(ink, regions, index, window):
    blocked = np.zeros_like(ink)
    neighbors = []
    left, top, right, bottom = window
    for other_index, (other, bounds) in enumerate(regions):
        if other_index == index:
            continue
        x0, y0, x1, y1 = bounds
        basis = "model_subject_bbox"
        if other["region_type"] == "notes":
            # The notes heading may be in the supplied context above its subject box.
            # Keep its anchor columns so nearby unrelated view marks remain visible.
            _, y0, _, y1 = other["pixel_box"]
            basis = "note_anchor_columns_in_context_rows"
        overlap = (max(left, x0), max(top, y0), min(right, x1), min(bottom, y1))
        if overlap[0] >= overlap[2] or overlap[1] >= overlap[3] or not _crop(ink, overlap).any():
            continue
        _crop(blocked, overlap)[:] = _crop(ink, overlap)
        neighbors.append({"region_id": other["id"], "region_type": other["region_type"],
                          "basis": basis, "blocked_search_overlap": list(overlap)})
    return blocked, neighbors


def propose_boundaries(png: bytes, plan: dict) -> dict:
    """Return monotonic crop candidates and unknown quality, without rewriting a locator plan."""
    with Image.open(BytesIO(png)) as source:
        gray = np.asarray(source.convert("L"))
    height, width = gray.shape
    if plan.get("version") != LOCATOR_VERSION or plan.get("image_size") != [width, height]:
        raise ValueError("Expected a v2 locator plan for this source size")
    ink = gray < INK_THRESHOLD
    # In the detection mask only, remove near-edge rules spanning most of the page.
    y_border = (ink.sum(axis=1) >= width * .78) & (np.minimum(np.arange(height), np.arange(height)[::-1]) <= height * .07)
    x_border = (ink.sum(axis=0) >= height * .78) & (np.minimum(np.arange(width), np.arange(width)[::-1]) <= width * .07)
    ink[y_border, :] = False
    ink[:, x_border] = False

    regions = [(region, _pixel_box(region["bbox"], width, height)) for region in plan["regions"]]
    note_bands = {region["id"]: _note_bands(ink, bounds, region["pixel_box"])
                  for region, bounds in regions if region["region_type"] == "notes"
                  and region["classification_status"] == "supported"}
    band_heights = [end - start for bands in note_bands.values() for start, end in bands]
    scale = max(3, round(median(band_heights) if band_heights else min(width, height) / 60))
    reach = min(MAX_SEARCH_HEIGHTS * scale, max(1, round(min(width, height) * MAX_SEARCH_PAGE_FRACTION)))
    notes_gap, view_gap, padding = (max(1, round(scale * factor))
                                     for factor in (NOTES_GAP_HEIGHTS, VIEW_GAP_HEIGHTS, PADDING_HEIGHTS))
    results = []
    for index, (region, primary) in enumerate(regions):
        original = tuple(region["pixel_box"])
        kind = region["region_type"]
        item = {"region_id": region["id"], "region_type": kind, "model_context_bbox": dict(region["context_bbox"]),
                "model_pixel_box": list(original), "candidate_pixel_box": list(original),
                "model_boundary_status": region["boundary_status"], "boundary_status": "unknown",
                "quality_status": "unassessed", "neighbor_blocks": [], "uncertainty_reasons": []}
        if kind not in ("notes", "drawing_view") or region["classification_status"] != "supported":
            item["edge_checks"] = {side: {"stop": "region_unassessed"} for pair in SIDE_NAMES for side in pair}
            item["uncertainty_reasons"].append("unsupported_type_or_classification_uncertain")
            results.append(item)
            continue
        window = _search_box(original, width, height, reach)
        blocked, neighbors = _neighbor_ink(ink, regions, index, window)
        usable = ink & ~blocked
        if kind == "notes":
            candidate, checks = _notes_candidate(usable, note_bands[region["id"]], original, primary, window,
                                                 notes_gap, padding)
            item["uncertainty_reasons"].append("notes_top_bottom_unassessed")
        else:
            candidate, checks, hit_pass_limit = _view_candidate(usable, original, window, view_gap, padding)
            item["uncertainty_reasons"].append("view_association_unverified")
            if hit_pass_limit:
                item["uncertainty_reasons"].append("view_iteration_limit")
        if neighbors:
            item["uncertainty_reasons"].append("neighbor_ink_suppressed_in_search")
        if _crop(blocked, candidate).sum() > _crop(blocked, original).sum():
            item["rejected_candidate_pixel_box"] = list(candidate)
            item["uncertainty_reasons"].append("would_include_neighbor_ink")
            candidate = original
        item.update(candidate_pixel_box=list(candidate), edge_checks=checks, neighbor_blocks=neighbors,
                    search_window=list(window))
        for axis, sides in enumerate(SIDE_NAMES):
            for side, edge in zip(sides, (axis, axis + 2)):
                checks[side].update(model_edge=original[edge], candidate_edge=candidate[edge])
        results.append(item)
    return {"version": VERSION, "source_sha256": sha256(png).hexdigest(), "image_size": [width, height],
            "locator_version": plan["version"], "locator_unlocated": [dict(item) for item in plan["unlocated"]],
            "coverage": "unknown", "whole_complete": False,
            "parameters": {"gray_ink_below": INK_THRESHOLD, "text_scale_px": scale,
                           "text_scale_source": "median_note_band_height" if band_heights else "page_scale_fallback",
                           "search_reach_px": reach, "notes_join_gap_px": notes_gap, "view_join_gap_px": view_gap,
                           "padding_px": padding, "view_max_passes": VIEW_PASSES,
                           "frame_rows_suppressed": int(y_border.sum()), "frame_columns_suppressed": int(x_border.sum())},
            "regions": results}
