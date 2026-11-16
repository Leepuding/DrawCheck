"""Keep view text occurrences separate from proposed dimension relationships."""
from __future__ import annotations

from collections import Counter
import re

from .layout_context import LayoutContextError


_PUNCTUATION = str.maketrans("，", ",")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def freeze_view_transcription(raw, *, page, region_id, image_sha256, crop_sha256, response_sha256,
                              pixel_box, image_size, boundary_status):
    """Assign stable IDs within one verified response. IDs carry no spatial order."""
    if (not isinstance(raw, dict) or raw.keys() != {"texts"} or not isinstance(raw["texts"], list) or
            any(text is not None and (not isinstance(text, str) or not text.strip()) for text in raw["texts"])):
        raise LayoutContextError("INVALID_VIEW_TRANSCRIPTION", "texts")
    if (any(not isinstance(value, str) or not value.strip() for value in (page, region_id)) or
            any(not isinstance(value, str) or not _SHA.fullmatch(value)
                for value in (image_sha256, crop_sha256, response_sha256)) or
            boundary_status not in ("partial", "unknown")):
        raise LayoutContextError("INVALID_VIEW_SOURCE", "source")
    if (not isinstance(image_size, (list, tuple)) or not isinstance(pixel_box, (list, tuple)) or
            len(image_size) != 2 or len(pixel_box) != 4 or
            any(type(value) is not int for value in (*pixel_box, *image_size)) or
            not (0 <= pixel_box[0] < pixel_box[2] <= image_size[0] and
                 0 <= pixel_box[1] < pixel_box[3] <= image_size[1])):
        raise LayoutContextError("INVALID_VIEW_SOURCE_BOX", "source.pixel_box")
    source = {"page": page, "region_id": region_id, "image_sha256": image_sha256, "crop_sha256": crop_sha256,
              "response_sha256": response_sha256, "pixel_box": list(pixel_box), "image_size": list(image_size),
              "boundary_status": boundary_status, "precision": "region"}
    return {"source": source, "whole_complete": False,
            "occurrences": [{"id": f"v{index}", "text": text, "pixel_box": None}
                            for index, text in enumerate(raw["texts"], 1)]}


def compare_view_transcriptions(first, second):
    """Report disagreement on the same pixels without picking a winning reading."""
    geometry = ("page", "region_id", "image_sha256", "crop_sha256", "pixel_box", "image_size")
    if any(first["source"][key] != second["source"][key] for key in geometry):
        raise LayoutContextError("VIEW_TRANSCRIPTIONS_DIFFERENT_SOURCES", "source")
    left, right = (Counter(item["text"] for item in result["occurrences"]) for result in (first, second))
    return {"status": "conflicting" if left != right else "same_multiset",
            "first_only": list((left - right).elements()), "second_only": list((right - left).elements())}


def check_view_relations(transcription, view):
    """Check a validated view candidate against whole observed literals.

    Candidate order, claimed placement and matching text do not prove geometry.
    A compound observation can support its full literal only. No substring can
    independently support a dimension or an object in this view-level result.
    """
    occurrences = {}
    for item in transcription["occurrences"]:
        if item["text"] is not None:
            occurrences.setdefault(item["text"].translate(_PUNCTUATION), []).append(item["id"])
    available = Counter({text: len(ids) for text, ids in occurrences.items()})
    proposed = []
    if view["view_title"] is not None:
        proposed.append(("title", None, None, view["view_title"]))
    proposed.extend(("annotation", item["id"], None, item["text"]) for item in view["annotations"])
    proposed.extend(("segment", chain["id"], index, text) for chain in view["chains"]
                    for index, text in enumerate(chain["segments"]))
    demand = Counter(text.translate(_PUNCTUATION) for _, _, _, text in proposed if text is not None)
    literals = []
    for kind, candidate_id, index, text in proposed:
        key = text.translate(_PUNCTUATION) if text is not None else None
        observed = available[key]
        matches = occurrences.get(key, [])
        status = ("unread" if text is None else "missing_whole_literal" if observed == 0 else
                  "insufficient_occurrences" if observed < demand[key] else "observed")
        literals.append({"kind": kind, "candidate_id": candidate_id, "segment_index": index, "literal": text,
                         "literal_status": status, "possible_occurrence_ids": matches})
    return {"source": transcription["source"], "candidate": view, "literal_checks": literals,
            "unassigned_literals": dict(available - demand),
            "relations": [{"chain_id": chain["id"], "position": None, "object": None, "unit": None,
                           "relation_status": "unresolved"} for chain in view["chains"]],
            "whole_complete": False}
