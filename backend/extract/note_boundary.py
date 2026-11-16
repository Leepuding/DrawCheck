"""Propose a source crop that can retain note continuations across the locator edge."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from statistics import median

import numpy as np
from PIL import Image

from .layout_boundary import (
    INK_THRESHOLD, MAX_SEARCH_HEIGHTS, MAX_SEARCH_PAGE_FRACTION, NOTES_GAP_HEIGHTS, PADDING_HEIGHTS,
    _crop, _grow_projection, _neighbor_ink, _note_bands, _notes_candidate, _pixel_box, _search_box,
)


def propose_note_crop(png, region, page_regions):
    """Use nearby anchored physical bands and expose uncertainty about ownership."""
    with Image.open(BytesIO(png)) as page:
        width, height = page.size
        ink = np.asarray(page.convert("L")) < INK_THRESHOLD
    original = tuple(region["pixel_box"])
    primary = _pixel_box(region["bbox"], width, height)
    # Start with a bounded window, then scale its reach from the physical lines.
    max_reach = max(1, round(min(width, height) * MAX_SEARCH_PAGE_FRACTION))
    anchored = _note_bands(ink, primary, original)
    scale = max(3, round(median([end - start for start, end in anchored] or [3])))
    window = _search_box(original, width, height, min(max_reach, MAX_SEARCH_HEIGHTS * scale))
    regions = [(region, primary), *((other, _pixel_box(other["bbox"], width, height))
                                   for other in page_regions if other["id"] != region["id"])]
    blocked, neighbors = _neighbor_ink(ink, regions, 0, window)
    usable = ink & ~blocked
    bands = _note_bands(usable, primary, window)
    active = np.zeros(window[3] - window[1], dtype=bool)
    for top, bottom in bands:
        active[top - window[1]:bottom - window[1]] = True
    padding = max(1, round(scale * PADDING_HEIGHTS))
    (top, bottom), vertical = _grow_projection(active, window[1], (original[1], original[3]),
                                                round(1.5 * scale), padding)
    selected_bands = [(start, end) for start, end in bands if start < bottom and end > top]
    vertical_crop = (original[0], top, original[2], bottom)
    candidate, checks = _notes_candidate(usable, selected_bands, vertical_crop, primary, window,
                                         max(1, round(scale * NOTES_GAP_HEIGHTS)), padding)
    checks.update(zip(("top", "bottom"), vertical))
    rejected = _crop(blocked, candidate).sum() > _crop(blocked, original).sum()
    additional_ink = int(_crop(usable, candidate).sum() - _crop(usable, original).sum())
    boundary = "partial" if region.get("boundary_status") == "partial" else "unknown"
    return {"region_id": region["id"], "source_image_sha256": sha256(png).hexdigest(),
            "source_pixel_box": list(original), "candidate_pixel_box": list(original if rejected else candidate),
            "rejected_candidate_pixel_box": list(candidate) if rejected else None,
            "edge_checks": checks, "neighbor_blocks": neighbors, "additional_candidate_ink_px": additional_ink,
            "boundary_status": boundary, "whole_complete": False}
