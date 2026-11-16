"""Optional, pixel-only candidates for a detached double-underlined view name."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
from math import ceil, floor

import numpy as np
from PIL import Image

from backend.extract.layout_boundary import _runs, propose_boundaries


VERSION = "underlined-view-caption-candidates-v1"


def _horizontal_overlap(first, second):
    return max(0, min(first[2], second[2]) - max(first[0], second[0]))


def _rules(ink, search, scale):
    left, top, right, bottom = search
    view_width = right - left
    min_length = max(4 * scale, ceil(view_width * .12))
    max_length = floor(view_width * .65)
    rules = []
    for y in range(top, bottom):
        for x0, x1 in _runs(ink[y, left:right]):
            length = x1 - x0
            if not min_length <= length <= max_length:
                continue
            row = [left + x0, y, left + x1, y + 1]
            continuation = next((rule for rule in reversed(rules) if rule[3] == y and
                                 _horizontal_overlap(rule, row) >= .9 * max(length, rule[2] - rule[0])), None)
            if continuation is None:
                rules.append(row)
            else:
                continuation[0] = min(continuation[0], row[0])
                continuation[2] = max(continuation[2], row[2])
                continuation[3] = y + 1
    return [rule for rule in rules if rule[3] - rule[1] <= max(1, round(scale * .2))]


def _text_band(ink, rule, base, scale):
    left, rule_y, right, _ = rule
    length = right - left
    y0 = max(base[3], rule_y - ceil(2.4 * scale))
    strip = ink[y0:rule_y, left:right]
    runs = _runs(strip.sum(axis=1) >= max(2, round(length * .015)))
    if not runs:
        return None
    start, end = runs[-1]
    for earlier_start, earlier_end in reversed(runs[:-1]):
        if start - earlier_end > max(1, round(scale * .1)):
            break
        start = earlier_start
    if not (.35 * scale <= end - start <= 1.6 * scale and 0 < len(strip) - end <= .8 * scale):
        return None
    band = strip[start:end]
    columns = np.flatnonzero(band.any(axis=0))
    if not columns.size or columns[-1] - columns[0] + 1 < .35 * length:
        return None
    if any(x1 - x0 >= .4 * length for row in band for x0, x1 in _runs(row)):
        return None
    return [left + int(columns[0]), y0 + start, left + int(columns[-1]) + 1, y0 + end]


def _intersects(first, second):
    return first[0] < second[2] and second[0] < first[2] and first[1] < second[3] and second[1] < first[3]


def _caption_options(ink, item, neighbors, scale, padding):
    left, top, right, bottom = item["candidate_pixel_box"]
    search = (left, bottom, right, item["search_window"][3])
    rules = _rules(ink, search, scale)
    options = []
    for index, first in enumerate(rules):
        for second in rules[index + 1:]:
            gap = second[1] - first[3]
            if gap > .7 * scale:
                break
            length = max(first[2] - first[0], second[2] - second[0])
            center = (first[0] + first[2]) / 2
            if not (gap > 0 and _horizontal_overlap(first, second) >= .9 * length and
                    abs(center - (left + right) / 2) <= (right - left) * .25):
                continue
            text = _text_band(ink, first, (left, top, right, bottom), scale)
            if text is None or abs((text[0] + text[2]) / 2 - center) > .18 * length:
                continue
            caption = [min(first[0], second[0], text[0]), text[1], max(first[2], second[2], text[2]), second[3]]
            extent = min(search[3], caption[3] + padding)
            proposed = [left, top, right, extent]
            added = [left, bottom, right, extent]
            competing_ink = ink[bottom:extent, left:right].copy()
            competing_ink[caption[1] - bottom:caption[3] - bottom,
                          max(0, caption[0] - left - padding):caption[2] - left + padding] = False
            blocks = [region_id for region_id, bounds in neighbors if _intersects(added, bounds)]
            options.append({"caption_pixel_box": caption, "text_band_pixel_box": text,
                            "underline_pixel_boxes": [first, second], "underline_gap_px": gap,
                            "white_gap_after_base_crop_px": text[1] - bottom,
                            "proposed_context_pixel_box": proposed,
                            "blocking_neighbor_ids": blocks, "other_ink_in_added_crop": bool(competing_ink.any())})
    return options


def propose_underlined_captions(png, plan, base_proposal):
    """Keep detached-caption candidates separate from the existing proposal."""
    if propose_boundaries(png, plan) != base_proposal:
        raise ValueError("Base boundary proposal cannot be reproduced")
    with Image.open(BytesIO(png)) as source:
        gray = np.asarray(source.convert("L"))
    params = base_proposal["parameters"]
    scale = params["text_scale_px"]
    padding = params["padding_px"]
    ink = gray < params["gray_ink_below"]
    regions = base_proposal["regions"]
    results = []
    for item in regions:
        if item["region_type"] != "drawing_view" or "search_window" not in item:
            continue
        neighbors = [(other["region_id"], other["model_pixel_box"]) for other in regions if other is not item]
        options = _caption_options(ink, item, neighbors, scale, padding)
        result = {"region_id": item["region_id"], "base_candidate_pixel_box": item["candidate_pixel_box"],
                  "base_edge_stops": {edge: evidence["stop"] for edge, evidence in item["edge_checks"].items()},
                  "caption_options": options, "status": "unknown", "boundary_status": "unknown",
                  "quality_status": "unassessed", "proposed_context_pixel_box": None}
        if len(options) == 1 and not options[0]["blocking_neighbor_ids"] and not options[0]["other_ink_in_added_crop"]:
            result.update(status="structural_caption_candidate", proposed_context_pixel_box=options[0]["proposed_context_pixel_box"])
        results.append(result)
    for index, first in enumerate(results):
        for second in results[index + 1:]:
            if any(_intersects(a["caption_pixel_box"], b["caption_pixel_box"])
                   for a in first["caption_options"] for b in second["caption_options"]):
                for item in (first, second):
                    item.update(status="unknown", proposed_context_pixel_box=None, caption_ownership_conflict=True)
    base_identity = json.dumps(base_proposal, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {"version": VERSION, "source_sha256": base_proposal["source_sha256"],
            "base_boundary_proposal_digest": sha256(base_identity.encode()).hexdigest(), "text_scale_px": scale,
            "regions": results, "coverage": "unknown", "whole_complete": False}
