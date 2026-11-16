"""Independent local OCR locations and corroborated original line transcriptions."""
from __future__ import annotations

import hashlib
import math
import json
from pathlib import Path
import re
import tempfile

VERSION = "positioned-notes-v3-ppocr6-reference-bottom-band"
POSITION_VERSION = VERSION + ":literal-position-v4"
MIN_CONFIDENCE = .9
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ITEM = re.compile(r"\s*(?:[0-9０-９]{1,3}[.．、](?!\d)|[（(][一二三四五六七八九十]+[）)])")
_NOTE_ANCHOR_LENGTH = 8
_CONDITION_GLYPHS = frozenset(
    "不未无非勿禁免莫否应须需必可宜能仅只除若如或且亦均每各及与则先再才已暂待"
    "上下内外大小前后正负超低高增减等最少多强弱"
)
_NUMBER_GLYPHS = frozenset("〇零一二三四五六七八九十两百千万亿倍半壹贰叁肆伍陆柒捌玖拾佰仟")
_CONDITION_TERMS = re.compile(
    r"(?:不(?:得|可|应|允许|满足)|若|否则|除非|方可|如[果需有]|只[有要]|[应须必]当|"
    r"满足|达到|超[过出]|[大小高低]于|至[多少]|合格|[以之][前后内外])"
)
_PROSE_COMMA = re.compile(r"(?<=\w)，(?=[\u3400-\u9fff])")
_KEYS = ("x0", "y0", "x1", "y1")
_SHORT_REFERENCE = re.compile(r"见(?:图)?(?P<token>[0-9]{2,6})")
_REFERENCE_TOKEN = re.compile(r"(?:详见|参见|见(?:详图|图)?|详图索引|索引)[:：]?(?:图纸编号|图纸|图号|图)?[（(]?"
                              r"(?P<token>[A-Za-z0-9_./—~～-]+)")


_FASTENER = re.compile(r"M[1-9][0-9]*(?:[U∪]型)?螺栓")
_FASTENER_CLAUSE_BREAK = re.compile(r"[,，。；;：:!?！？]")
_FASTENER_TEST_INTRO = re.compile(r"按(?:有关|相关)(?:要求|规定)(?:做好|进行)(?:抗拔|拉拔)试验,")
_FASTENER_TEST_PHASE = re.compile(r"[\u3400-\u9fff]{2,}(?:前|后|时|期间)[\u3400-\u9fff]*抗拔试验")
_FASTENER_LOAD = re.compile(
    r"(?P<label>抗拔力|拉拔力|抗拉力|拉力|抗剪力)(?:必须|应|需|须)?"
    r"(?P<threshold>不小于|不低于|不大于|不超过|至少|大于|小于|≥|≤|>=|<=|[><=＞＜＝]|为)?"
    r"(?P<value>[0-9]+(?:\.[0-9]+)?)(?P<unit>[kKmMµμ]?N)(?![A-Za-z0-9/.²³])"
)
_FAILURE_ACTION = re.compile(
    r"[,，;；](?P<condition>(?:如果|若|如|当)?(?:试验)?(?:结果)?"
    r"(?:不满足|未满足|未达到|达不到|不符合|未通过|不合格|不达标)"
    r"(?:要求|规定|标准|限值|条件|试验)?(?:时)?)"
    r"(?P<action>[,，]?(?:则|应当|应|需|须|必须|不得|严禁|立即)[^。！？;；?？□�]+)\。\Z"
)


def reference_typography(text):
    """Only reference-token separators have the explicitly permitted equivalence."""
    def substitute(match):
        token=match["token"]
        if not re.fullmatch(r"(?:[A-Za-z0-9]|[-—](?=\d))[A-Za-z0-9_./—-]*(?:[~～][-—]?\d+)?",token):
            return match[0]
        replacement=re.sub(r"—(?=\d)","-",token)
        if re.fullmatch(r"[-—]?\d+[~～][-—]?\d+",token):
            replacement=replacement.replace("～","~")
        return match[0][:match.start("token")-match.start()]+replacement
    return _REFERENCE_TOKEN.sub(substitute,text)


def _reference_occurrences(query, observed):
    """A full prose substring must also contain exactly the full target token."""
    query_tokens=[(m.start("token"),m.end("token")) for m in _REFERENCE_TOKEN.finditer(query)]
    observed_tokens={(m.start("token"),m.end("token")) for m in _REFERENCE_TOKEN.finditer(observed)}
    if not query_tokens:
        return []
    offsets=[]
    start=0
    while (offset := observed.find(query,start)) >= 0:
        if all((offset+left,offset+right) in observed_tokens for left,right in query_tokens):
            offsets.append(offset)
        start=offset+1
    return offsets


def normalized_text(text):
    # No NFKC, case folding, punctuation substitution or fuzzy character edits.
    return "".join(str(text).split()).rstrip("。；")


def has_reference_token(text):
    """A visible action and literal token, not a label such as a detail title."""
    return bool(_REFERENCE_TOKEN.search(normalized_text(text)))


def _note_format(text, *, omit_number):
    if omit_number:
        text = _ITEM.sub("", text, count=1)
    return _PROSE_COMMA.sub(",", text)


def complete_note_match(text, observed, match_type):
    """The complete note body agrees; substring and prefix hits only locate it."""
    if not isinstance(text, str) or not isinstance(observed, str):
        return False
    query, actual = normalized_text(text), normalized_text(observed)
    if len(query) < 12 or len(re.findall(r"[\u3400-\u9fff]", query)) < 8:
        return False
    if match_type == "exact":
        return actual == query
    return (match_type == "note_layout_typography"
            and _note_format(actual, omit_number=not _ITEM.match(query))
            == _note_format(query, omit_number=False))


def _box(value):
    try:
        values = tuple(value[key] for key in _KEYS)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            return None
        a, b, c, d = values
        return values if 0 <= a < c <= 1 and 0 <= b < d <= 1 else None
    except (KeyError, TypeError):
        return None


def _inside_crop(box, crop):
    margin = min(.002, .12 * (box[3] - box[1]))
    return (box[0] - crop[0] > margin and box[1] - crop[1] > margin
            and crop[2] - box[2] > margin and crop[3] - box[3] > margin)


def _same_place(a, b, *, minimum_overlap=.8):
    # Both near-identical extent and near-identical line centre are required.
    overlap = max(0., min(a[2], b[2])-max(a[0], b[0])) * max(0., min(a[3], b[3])-max(a[1], b[1]))
    area_a, area_b = (a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1])
    return (overlap / max(area_a, area_b) >= minimum_overlap
            and abs((a[1]+a[3])-(b[1]+b[3])) <= .5*min(a[3]-a[1], b[3]-b[1]))


def _union(rows):
    boxes = [row["box"] for row in rows]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _continuation(first, second):
    a, b = first["box"], second["box"]
    height = max(a[3]-a[1], b[3]-b[1])
    return (0 <= b[1]-a[3] <= height*1.1
            and abs(a[0]-b[0]) <= height*1.2
            and min(a[2],b[2]) > max(a[0],b[0]))


def _sequences(rows):
    """Only join unambiguous adjacent lines from the same OCR crop."""
    for first in rows:
        chain = [first]
        yield chain
        if first.get("reread"):
            continue
        for _ in range(3):
            candidates = [r for r in rows if not r.get("reread") and r["crop"] == first["crop"] and _continuation(chain[-1], r)]
            if not candidates:
                break
            next_top = min(r["box"][1] for r in candidates)
            nearest = [r for r in candidates if abs(r["box"][1]-next_top) < 1e-6]
            # A numbered next line is a boundary, not a row to skip while
            # searching for a later line that happens to finish the query.
            if (len(nearest) != 1 or _ITEM.match(nearest[0]["text"])
                    or chain[-1]["text"].rstrip().endswith(("。", "；", "！", "？", "!", "?", ";"))):
                break
            chain = chain + nearest
            yield chain


def _validated_rows(observations, image_sha256):
    rows, rejected = [], 0
    for raw in observations:
        if not isinstance(raw, dict):
            rejected += 1
            continue
        confidence = raw.get("confidence")
        box = _box(raw.get("bbox"))
        crop = raw.get("crop")
        crop_box = _box(dict(zip(_KEYS, crop))) if isinstance(crop, (tuple, list)) and len(crop) == 4 else None
        if (box is None or raw.get("image_sha256") != image_sha256 or raw.get("bbox_clamped")
                or raw.get("region_bbox_unlocated") or raw.get("unclear")
                or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or not MIN_CONFIDENCE <= confidence <= 1
                or not isinstance(raw.get("text"), str) or not raw.get("observation_id")
                or crop_box is None or not (crop_box[0]-1e-6 <= box[0] < box[2] <= crop_box[2]+1e-6
                                             and crop_box[1]-1e-6 <= box[1] < box[3] <= crop_box[3]+1e-6)):
            rejected += 1
            continue
        rows.append({"text":raw["text"], "box":box, "confidence":confidence,
                     "id":raw["observation_id"], "crop":tuple(raw["crop"]),
                     "reread":raw.get("line_reread"), "raw":raw})
    return rows, rejected


def _fastener_anchor(text):
    body = _ITEM.sub("", normalized_text(text), count=1)
    return _FASTENER.search(_FASTENER_CLAUSE_BREAK.split(body, maxsplit=1)[0])


def _fastener_loads(text):
    loads = [match.groupdict() for match in _FASTENER_LOAD.finditer(text)]
    return loads if (len(loads) == 2
                     and {load["label"] == "抗剪力" for load in loads} == {False, True}) else None


def _load_identity(loads):
    return tuple((load["label"] == "抗剪力", load["value"], load["unit"]) for load in loads)


def is_fastener_test_note(text):
    """Select prose with a fastener designation and both force readings."""
    return bool(_fastener_anchor(text) and "试验" in text and _fastener_loads(normalized_text(text)))


def _same_fastener(a, b):
    # Some OCR crops render the curved U as a union glyph. This equivalence
    # applies only inside the fastener designation, not the paragraph body.
    return a.replace("∪", "U") == b.replace("∪", "U")


def _same_row(a, b):
    height = min(a[3] - a[1], b[3] - b[1])
    return abs((a[1] + a[3]) - (b[1] + b[3])) <= .9 * height


def _same_fastener_start(a, b):
    return _same_row(a, b) and abs(a[0] - b[0]) <= .5 * min(a[3] - a[1], b[3] - b[1])


def _fastener_paragraph(text):
    compact = "".join(text.split())
    anchor = _fastener_anchor(compact)
    marker = _ITEM.match(compact)
    start = marker.end() if marker else 0
    body = compact[start:]
    clause = _FAILURE_ACTION.search(body)
    before_condition = body[:clause.start()] if clause else normalized_text(body)
    loads = _fastener_loads(before_condition)
    if (not anchor or not loads or re.search(r"[?？□�]", before_condition)
            or _FASTENER.search(before_condition, anchor.end())):
        return None
    first, second = _FASTENER_LOAD.finditer(before_condition)
    if anchor.end() > first.start():
        return None
    # Preserve all text around the loads. Only the trial name and optional
    # "先" after an explicit "前" stage have narrow reading equivalents.
    tail = before_condition[second.end():].replace("，", ",")
    tail = tail.replace("拉拔试验", "抗拔试验")
    tail = re.sub(r"(前(?:应|需|须|必须))先(?=进行抗拔试验)", r"\1", tail)
    return {"item": marker[0].translate(str.maketrans("０１２３４５６７８９（）", "0123456789()"))
                    .rstrip(".．、") if marker else None,
            "designation": anchor[0], "prefix": before_condition[:anchor.start()],
            "after_designation": before_condition[anchor.end():first.start()].replace("，", ","),
            "between_loads": before_condition[first.end():second.start()].replace("，", ","),
            "test_context": tail, "loads": loads, "clause": clause,
            "condition_start": start + clause.start("condition") if clause else None}


def _complete_fastener_note(text, query):
    paragraph = _fastener_paragraph(text)
    if not paragraph or re.search(r"[?？□�]", text):
        return None
    clause = paragraph["clause"]
    if not clause or len(re.findall(r"[\u3400-\u9fff]", clause["action"])) < 3:
        return None
    loads = paragraph["loads"]
    if (not _same_fastener(paragraph["designation"], query["designation"])
            or _load_identity(loads) != _load_identity(query["loads"])
            or any(not load["threshold"] or load["label"] in {"抗拉力", "拉力"} for load in loads)):
        return None
    return paragraph


def _same_fastener_paragraph(query, paragraph):
    if query["item"] and query["item"] != paragraph["item"]:
        return False
    if any(query[key] != paragraph[key] for key in ("prefix", "between_loads", "test_context")):
        return False
    # A generic testing instruction may precede the loads in the source.
    # Any additional object, location or test-stage words remain significant.
    after_designation = paragraph["after_designation"]
    if (query["after_designation"] != after_designation
            and (query["after_designation"] or not _FASTENER_TEST_INTRO.fullmatch(after_designation))):
        return False
    condition = query["clause"]
    return not condition or condition["condition"] == paragraph["clause"]["condition"]


def _fastener_crop_notes(rows, seed, query):
    for sequence in _sequences(rows):
        first = sequence[0]
        if not _same_fastener_start(first["box"], seed):
            continue
        # A numbered item supplies a paragraph start across physical lines.
        if len(sequence) > 1 and not _ITEM.match(first["text"]):
            continue
        box = _union(sequence)
        margin = .12 * min(row["box"][3] - row["box"][1] for row in sequence)
        crop = first["crop"]
        if min(box[0] - crop[0], box[1] - crop[1], crop[2] - box[2], crop[3] - box[3]) <= margin:
            continue
        confidence = min(row["confidence"] for row in sequence)
        text = "".join(row["text"] for row in sequence)
        paragraph = _complete_fastener_note(text, query)
        if not paragraph or confidence < .95:
            continue
        if len(sequence) > 1 and paragraph["condition_start"] >= len("".join(first["text"].split())):
            # Without part of the condition on the first line, an adjacent
            # unnumbered condition can be mistaken for this item's continuation.
            continue
        clause = paragraph["clause"]
        yield {"bbox": dict(zip(_KEYS, box)), "observed_text": text,
               "confidence": confidence, "source_lines": [row["raw"] for row in sequence],
               "loads": paragraph["loads"], "condition": clause["condition"], "action": clause["action"],
               "paragraph_binding": {"item": paragraph["item"], "test_context": paragraph["test_context"],
                                     "matches_query": _same_fastener_paragraph(query, paragraph)}}


def reread_fastener_test_note(text, image_path, bundle, *, image_sha256, engine):
    """Verify a single complete paragraph on two independently detected crops."""
    from PIL import Image
    from .local_sheet import _image_words

    result = {"status": "missing", "locations": []}
    if not bundle or bundle.get("version") != VERSION or bundle.get("image_sha256") != image_sha256:
        return {**result, "reason": "local_ocr_unavailable"}
    query = _fastener_paragraph(text)
    if not query or not _FASTENER_TEST_PHASE.search(query["test_context"]):
        return {**result, "reason": "insufficient_fastener_paragraph_context"}
    designation = query["designation"]
    rows, _ = _validated_rows(bundle["observations"], image_sha256)
    seeds = [row for row in rows if (anchor := _fastener_anchor(row["text"]))
             and _same_fastener(anchor[0], designation)]
    if not seeds:
        return {**result, "reason": "fastener_start_not_detected"}
    result["anchor_observations"] = [row["raw"] for row in seeds]
    # Pairwise agreement prevents an intermediate crop bridging distinct rows.
    if any(not _same_fastener_start(a["box"], b["box"])
           for i, a in enumerate(seeds) for b in seeds[i+1:]):
        return {**result, "status": "ambiguous", "reason": "multiple_fastener_paragraphs"}
    left, top, right, bottom = max((row["box"] for row in seeds), key=lambda box: box[2] - box[0])
    seed = (left, top, right, bottom)
    height = bottom - top
    # Expand over physically overlapping OCR fragments. Text from these
    # fragments is never spliced into the paragraph or its condition.
    right = max([right, *(row["box"][2] for row in rows if _same_row(seed, row["box"])
                          and left < row["box"][2] and row["box"][0] < right)])
    crops = [(max(0., left - x * height), max(0., top - y * height),
              min(1., right + w * height), min(1., bottom + h * height))
             for x, y, w, h in ((1.8, 1.55, 4.1, 7.7), (3., 2.1, 6., 9.4))]
    path = Path(image_path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != image_sha256:
        return {**result, "reason": "source_image_changed"}
    locations = []
    competing = []
    with Image.open(path) as image:
        size = image.size
        reader = engine()
        for index, crop in enumerate(crops):
            observations = [{**word.evidence(), "crop": list(crop), "image_sha256": image_sha256,
                             "observation_id": f"fastener:{index}:{line}"}
                            for line, word in enumerate(_image_words(reader, image, crop))]
            reread, _ = _validated_rows(observations, image_sha256)
            locations.append(list(_fastener_crop_notes(reread, seed, query)))
            competing.extend(row["raw"] for row in reread if (anchor := _fastener_anchor(row["text"]))
                             and _same_fastener(anchor[0], designation)
                             and not _same_fastener_start(row["box"], seed))
    if hashlib.sha256(path.read_bytes()).hexdigest() != image_sha256:
        return {**result, "reason": "source_image_changed"}
    result["locations"] = [location for variants in locations for location in variants]
    if competing:
        return {**result, "status": "ambiguous", "reason": "multiple_fastener_paragraphs",
                "competing_observations": competing}
    if any(len(variants) != 1 for variants in locations):
        return {**result, "reason": "complete_condition_not_read_in_both_crops"}
    first, second = result["locations"]
    readings = [first["observed_text"], second["observed_text"]]
    # A U in the designator must have independent source support. Any other
    # glyph or text disagreement between the full readings remains unresolved.
    u_crops = {row["crop"] for row in seeds if "U" in _fastener_anchor(row["text"])[0]}
    if len(u_crops) >= 2 and any("∪" in _fastener_anchor(reading)[0] for reading in readings):
        readings = [re.sub(r"(M[1-9][0-9]*\s*)∪(?=\s*型\s*螺栓)", r"\1U", reading, count=1)
                    for reading in readings]
        result["designation_typography"] = "U corroborated by initial detector crops"
    if ("".join(readings[0].split()) != "".join(readings[1].split())
            or not _same_place(_box(first["bbox"]), _box(second["bbox"]), minimum_overlap=.75)
            or len({tuple(round(edge * side) for edge, side in zip(crop, size * 2))
                    for crop in crops}) != 2):
        return {**result, "status": "ambiguous", "reason": "complete_readings_disagree"}
    if not all(variant["paragraph_binding"]["matches_query"] for variant in (first, second)):
        return {**result, "locations": [], "reason": "fastener_paragraph_context_mismatch"}
    location = {**first, "bbox": dict(zip(_KEYS, _union(
                    [{"box": _box(variant["bbox"])} for variant in (first, second)]))),
                "confidence": min(first["confidence"], second["confidence"]),
                "observed_variants": [first, second]}
    return {**result, "status": "unique_reread", "locations": [location], "reread_text": readings[0]}


def locate_note(text, observation_bundle, *, image_sha256, allow_prefix=False, _reference=False):
    """Return all independently observed positions without editing the supplied text.

    A unique prefix is a location only, never proof that a truncated/unclear
    original was read completely. Callers must preserve its original state.
    """
    result = {"version": VERSION, "original_text": text, "image_sha256": image_sha256,
              "reading_status": "unchanged", "status": "invalid", "locations": []}
    if (not isinstance(text, str) or not isinstance(image_sha256, str) or not _SHA.fullmatch(image_sha256)
            or not isinstance(observation_bundle, dict) or observation_bundle.get("version") != VERSION
            or observation_bundle.get("image_sha256") != image_sha256):
        result["reason"] = "source_image_or_observation_version_mismatch"
        return result
    query = normalized_text(text)
    if len(query) < (8 if _reference else 12) or len(re.findall(r"[\u3400-\u9fff]", query)) < (3 if _reference else 8):
        result["reason"] = "insufficient_distinctive_text"
        return result
    if _reference:
        query=reference_typography(query)
    observations = observation_bundle.get("observations")
    if not isinstance(observations, list):
        result["reason"] = "invalid_observations"
        return result
    rows, rejected = _validated_rows(observations, image_sha256)
    result["rejected_observations"] = rejected
    candidates = []
    for sequence in _sequences(rows):
        observed = "".join(r["text"] for r in sequence)
        literal_normalized = normalized_text(observed)
        normalized = reference_typography(literal_normalized) if _reference else literal_normalized
        exact = normalized == query
        # For prose, match the entire observed body. Removing a visible
        # leading list marker is permitted only when the input has none.
        # Comma width is insignificant only in prose, never between numbers.
        note_format = (not _reference and not exact
                       and _note_format(normalized, omit_number=not _ITEM.match(query))
                       == _note_format(query, omit_number=False))
        occurrences=_reference_occurrences(query,normalized) if _reference else []
        contained = _reference and bool(occurrences)
        prefix = allow_prefix and len(query) >= 24 and normalized.startswith(query) and len(normalized) > len(query)
        if not (exact or prefix or contained or note_format):
            continue
        # A multiline prefix must actually reach its last included line.
        if len(sequence) > 1:
            # An independently complete matching line already supplies the
            # position. A preceding title is context, not a second occurrence.
            if _reference and any(_reference_occurrences(query,reference_typography(normalized_text(r["text"]))) for r in sequence):
                continue
            preceding = normalized_text("".join(r["text"] for r in sequence[:-1]))
            if _reference:
                preceding=reference_typography(preceding)
            if (bool(_reference_occurrences(query,preceding)) if _reference else len(preceding) >= len(query)):
                continue
        format_equivalent=_reference and not _reference_occurrences(normalized_text(text),literal_normalized)
        candidate = {"bbox":dict(zip(_KEYS, _union(sequence))), "observed_text":observed,
                     "confidence":min(r["confidence"] for r in sequence),
                     "observation_ids":[r["id"] for r in sequence],
                     "source_lines":[{"bbox":dict(zip(_KEYS, r["box"])), "text":r["text"],
                                      "observation_id":r["id"], "crop":list(r["crop"]),
                                      **({"line_reread":r["reread"]} if r["reread"] else {})} for r in sequence],
                     "match_type":"reference_separator_typography" if format_equivalent else "note_layout_typography" if note_format else "exact" if exact else "literal_substring" if contained else "prefix_position_only",
                     "literal_occurrence_count":len(occurrences) if _reference else 1 if note_format else normalized.count(query)}
        if format_equivalent:
            candidate["basis"]="引用分隔符排印等价定位；仅图号前横线及完整数字范围分隔符等价，不核验引用身份"
        if note_format:
            candidate["basis"] = "整行或连续原文；仅容许原图有额外的行首条目编号或非数值逗号宽度不同"
        candidates.append(candidate)
    # Evaluate original pairs before merging. Overlap is not transitive: a
    # middle crop must never bridge two incompatible physical positions.
    candidates.sort(key=lambda c:(_box(c["bbox"]),c["match_type"],c["observed_text"],tuple(c["observation_ids"])))
    neighbours=[set() for _ in candidates]
    for i,a in enumerate(candidates):
        for j in range(i+1,len(candidates)):
            b=candidates[j]
            same_reading=((a["match_type"]==b["match_type"] and normalized_text(a["observed_text"])==normalized_text(b["observed_text"]))
                          or (_reference and a["literal_occurrence_count"]==b["literal_occurrence_count"]==1))
            same_reading = same_reading or (not _reference and all(
                candidate["match_type"] in {"exact", "note_layout_typography"}
                for candidate in (a, b)))
            # OCR estimates slightly different ascender/descender margins
            # across crops. The same full note body still needs strong
            # reciprocal overlap; keep the existing reference threshold.
            if same_reading and _same_place(_box(a["bbox"]), _box(b["bbox"]),
                                            minimum_overlap=.8 if _reference else .75):
                neighbours[i].add(j);neighbours[j].add(i)
    grouped=[]
    visited=set()
    for i in range(len(candidates)):
        if i in visited:
            continue
        component=set();frontier=[i]
        while frontier:
            member=frontier.pop()
            if member in component:
                continue
            component.add(member);frontier.extend(neighbours[member]-component)
        visited.update(component)
        if not all(len(neighbours[member]&component)==len(component)-1 for member in component):
            grouped.extend(candidates[member] for member in sorted(component))
            continue
        members=[candidates[member] for member in sorted(component)]
        representative={**min(members, key=lambda c: (c["match_type"] not in {"exact", "literal_substring"}, _box(c["bbox"])))}
        representative["observation_ids"]=list(dict.fromkeys(oid for c in members for oid in c["observation_ids"]))
        if len(members)>1:
            representative["observed_variants"]=[{key:c[key] for key in ("observed_text","bbox","observation_ids","source_lines")} for c in members]
        grouped.append(representative)
    candidates=grouped
    result["locations"] = candidates
    result["status"] = ("missing" if not candidates else "ambiguous" if len(candidates) > 1 or candidates[0]["literal_occurrence_count"] != 1
                        else "unique_exact" if candidates[0]["match_type"] in {"exact","literal_substring"}
                        else "unique_format_equivalent" if candidates[0]["match_type"] in {"reference_separator_typography", "note_layout_typography"} else "unique_prefix")
    return result


def locate_reference(text, observation_bundle, *, image_sha256):
    """Locate the complete literal reference inside independently read line context.

    The surrounding list number or note remains in observed_text. It is never
    deleted from the source or used to infer a missing target or a tight box.
    """
    from backend.references import drawing_reference

    if not isinstance(text, str) or not re.search(r"\d", text) or not drawing_reference({"text":reference_typography(normalized_text(text))}):
        return {"version":VERSION,"original_text":text,"image_sha256":image_sha256,
                "reading_status":"unchanged","status":"invalid","locations":[],
                "reason":"no_literal_drawing_reference"}
    return locate_note(text,observation_bundle,image_sha256=image_sha256,_reference=True)



def _line_literal(text):
    return "".join(text.split())


def single_note_omission(original, observed):
    """Only an interior Han glyph in an otherwise identical plain prose line."""
    if not isinstance(original, str) or not isinstance(observed, str):
        return False
    prior, current = map(_line_literal, (original, observed))
    prior_item, current_item = _ITEM.match(prior), _ITEM.match(current)
    if prior_item and (not current_item or prior_item[0] != current_item[0]):
        return False
    prior, current = (_ITEM.sub("", text, count=1) for text in (prior, current))
    if (len(prior) < 24 or len(current) != len(prior) + 1
            or not re.fullmatch(r"[\u3400-\u9fff，、；。：,]+", current)):
        return False
    index = next((i for i, (a, b) in enumerate(zip(prior, current)) if a != b), len(prior))
    # Keep a distinctive literal on both sides. A changed qualifier or written
    # quantity is out of scope even if just one extra character would explain it.
    glyph = current[index]
    return (min(index, len(prior) - index) >= _NOTE_ANCHOR_LENGTH
            and re.fullmatch(r"[\u3400-\u9fff]", glyph) is not None
            and glyph not in _CONDITION_GLYPHS and glyph not in _NUMBER_GLYPHS
            and prior == current[:index] + current[index + 1:]
            and _CONDITION_TERMS.findall(prior) == _CONDITION_TERMS.findall(current))


def reread_note_omission(image_path, bundle, text, *, engine):
    """Require two uncut detections and complete native reads at one position."""
    from PIL import Image
    from .local_sheet import _read_detected_line

    sha = bundle["image_sha256"]
    if bundle.get("version") != VERSION:
        return None
    rows, _ = _validated_rows(bundle["observations"], sha)
    body = _ITEM.sub("", _line_literal(text), count=1)
    prefix, suffix = body[:_NOTE_ANCHOR_LENGTH], body[-_NOTE_ANCHOR_LENGTH:]
    candidates = []
    for row in rows:
        observed = _ITEM.sub("", _line_literal(row["text"]), count=1)
        if not row["reread"] and observed.startswith(prefix) and observed.endswith(suffix):
            candidates.append(row)
    if not candidates or len(candidates) > 8 or not single_note_omission(text, candidates[0]["text"]):
        return None
    literal = _line_literal(candidates[0]["text"])
    if any(_line_literal(row["text"]) != literal for row in candidates):
        return None
    if any(not _same_place(row["box"], other["box"])
           for i, row in enumerate(candidates) for other in candidates[:i]):
        return None
    selected = [row for row in candidates if _inside_crop(row["box"], row["crop"])]
    if len({row["crop"] for row in selected}) < 2:
        return None
    path = Path(image_path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        return None
    reads = []
    reader = engine()
    with Image.open(path) as image:
        for row in selected:
            samples = list(_read_detected_line(reader, image, row["box"]))
            if ({scale for _, _, scale in samples} != {1., 1.15}
                    or any(not .95 <= score <= 1 or _line_literal(value) != literal
                           for value, score, _ in samples)):
                return None
            reads.extend({**row["raw"], "text": value, "confidence": score,
                          "observation_id": f'{row["id"]}:omission:{scale}',
                          "line_reread": {"original_text": row["text"],
                                          "original_observation_id": row["id"],
                                          "original_confidence": row["confidence"],
                                          "confidence": score, "horizontal_scale": scale}}
                         for value, score, scale in samples)
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise OSError("Source image changed during local line reread")
    corrected = reads[0]["text"]
    recovered = locate_note(corrected, {**bundle, "observations": reads}, image_sha256=sha)
    proof = {**recovered, "original_text": text, "reading_status": "locally_transcribed",
             "recovery_stage": "corroborated_native_note_omission", "transcribed_text": corrected}
    return proof if corroborated_note_omission(proof, corrected) else None


def corroborated_note_omission(proof, text):
    """Validate the stored, positioned detector and native OCR readings."""
    if (proof.get("recovery_stage") != "corroborated_native_note_omission"
            or proof.get("status") != "unique_exact" or proof.get("transcribed_text") != text
            or not single_note_omission(proof.get("original_text"), text)):
        return False
    try:
        location, = proof["locations"]
        anchor = _box(location["bbox"])
        literal = _line_literal(text)
        observations = {}
        for variant in location["observed_variants"]:
            line, = variant["source_lines"]
            original = line["line_reread"]
            crop, box = tuple(line["crop"]), _box(line["bbox"])
            if (not box or not anchor or not _same_place(anchor, box) or len(crop) != 4
                    or not _box(dict(zip(_KEYS, crop))) or not _inside_crop(box, crop)
                    or _line_literal(line["text"]) != literal
                    or _line_literal(original["original_text"]) != literal
                    or not original["original_observation_id"] or not .9 <= original["original_confidence"] <= 1
                    or not .95 <= original["confidence"] <= 1):
                return False
            observations.setdefault((crop, original["original_observation_id"], box), set()).add(original["horizontal_scale"])
        complete = {(crop, oid) for (crop, oid, _), scales in observations.items() if scales == {1., 1.15}}
        return len({crop for crop, _ in complete}) >= 2 and len({oid for _, oid in complete}) >= 2
    except (KeyError, ValueError, TypeError):
        return False


def short_reference_token(text):
    match = _SHORT_REFERENCE.fullmatch(normalized_text(text))
    return match["token"] if match else None


def locate_short_reference(text, bundle, *, image_sha256, image_path, engine):
    """Recheck a standalone bare reference without the model's original box."""
    from PIL import Image
    from .local_sheet import _image_words, _read_detected_line

    result = {"status": "invalid", "locations": [], "reason": "source_image_or_observation_version_mismatch"}
    if bundle.get("version") != VERSION or bundle.get("image_sha256") != image_sha256:
        return result
    observations = bundle.get("observations")
    if not isinstance(observations, list):
        return {**result, "reason": "invalid_observations"}
    token = short_reference_token(text)
    query = normalized_text(text)
    rows, rejected = _validated_rows(observations, image_sha256)
    result["rejected_observations"] = rejected
    standalone, embedded, other_forms = [], [], []
    for row in rows:
        observed = normalized_text(row["text"])
        targets = {match["token"] for match in _REFERENCE_TOKEN.finditer(reference_typography(observed))}
        if token not in targets and f"-{token}" not in targets:
            continue
        evidence = {"text": row["text"], "bbox": dict(zip(_KEYS, row["box"])),
                    "confidence": row["confidence"], "crop": list(row["crop"]), "observation_id": row["id"]}
        if short_reference_token(observed) == token:
            standalone.append(evidence)
        elif token in targets:
            embedded.append(evidence)
        else:
            other_forms.append(evidence)
    result.update(status="missing", reason="no_complete_standalone_reference",
                  candidate_lines=standalone + embedded, different_reference_forms=other_forms)
    if not standalone:
        return result
    # Require pairwise agreement. An intervening crop cannot join two sites.
    if embedded or any(not _same_place(_box(a["bbox"]), _box(b["bbox"]))
                       or normalized_text(a["text"]) != normalized_text(b["text"])
                       for i, a in enumerate(standalone) for b in standalone[i+1:]):
        return {**result, "status": "ambiguous", "reason": "competing_short_reference_lines"}
    actual = normalized_text(standalone[0]["text"])
    if actual not in {query, f"见图{token}"}:
        return {**result, "reason": "different_short_reference_wording"}
    uncut = [line for line in standalone if _inside_crop(_box(line["bbox"]), line["crop"])]
    if not uncut:
        return {**result, "reason": "short_reference_at_crop_boundary"}
    anchor = max(uncut, key=lambda line: line["confidence"])
    box = _box(anchor["bbox"])
    x0, y0, x1, y1 = box
    height = y1 - y0
    context_crop = (max(0., x0 - 8 * height), max(0., y0 - 5 * height),
                    min(1., x1 + 8 * height), min(1., y1 + 5 * height))
    path = Path(image_path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != image_sha256:
        return {**result, "status": "invalid", "reason": "source_image_or_observation_version_mismatch"}
    reader = engine()
    with Image.open(path) as image:
        context = _image_words(reader, image, context_crop)
        native = list(_read_detected_line(reader, image, box))
    if hashlib.sha256(path.read_bytes()).hexdigest() != image_sha256:
        raise OSError("Source image changed during short reference reread")
    context = [word for word in context if MIN_CONFIDENCE <= word.score <= 1
               and _inside_crop(word.box, context_crop)]
    same_line = [word for word in context if _same_place(word.box, box)]
    competing_context = [word.evidence() for word in context if word not in same_line
                         and any(match["token"] == token
                                 for match in _REFERENCE_TOKEN.finditer(normalized_text(word.text)))]
    labels = [word.evidence() for word in context if not has_reference_token(word.text)
              and len(re.findall(r"[\u3400-\u9fff]", word.text)) >= 3
              and not _same_place(word.box, box)]
    result.update(context_crop=list(context_crop), context_reference=[word.evidence() for word in same_line],
                  context_labels=labels, native_reread=[{"text": reading, "confidence": score,
                                                        "horizontal_scale": scale, "bbox": anchor["bbox"]}
                                                       for reading, score, scale in native])
    if competing_context:
        return {**result, "status": "ambiguous", "reason": "competing_short_reference_lines",
                "competing_context": competing_context}
    if len(same_line) != 1 or normalized_text(same_line[0].text) != actual:
        return {**result, "reason": "short_reference_context_reread_disagrees"}
    if not labels:
        return {**result, "reason": "short_reference_without_nearby_labels"}
    if len(native) != 2 or {scale for _, _, scale in native} != {1., 1.15} or any(
            normalized_text(reading) != actual or not MIN_CONFIDENCE <= score <= 1
            for reading, score, _ in native):
        return {**result, "reason": "short_reference_native_reread_disagrees"}
    location = {"bbox": anchor["bbox"], "observed_text": anchor["text"], "confidence": anchor["confidence"],
                "observation_ids": [line["observation_id"] for line in uncut],
                "match_type": "short_reference_reread", "literal_occurrence_count": 1}
    result.pop("reason")
    return {**result, "status": "unique_exact", "locations": [location],
            "recovered_text": anchor["text"], "recovery_stage": "corroborated_short_reference"}


def reread_note_lines(image_path, bundle, queries, *, engine):
    """Re-read a bounded set of detected, uncut lines at native resolution.

    Query text only selects work using a literal prose prefix. Candidate
    locations come from the local detector; the model's box is never used.
    No reread is joined with another line or used to edit an original glyph.
    """
    from PIL import Image
    from .local_sheet import _read_detected_line

    path = Path(image_path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if sha != bundle.get("image_sha256") or bundle.get("version") != VERSION:
        return bundle
    anchors = {value[:min(12, max(4, len(value) // 4))]
               for text in queries if len(value := normalized_text(text)) >= 10}
    anchors = {anchor for anchor in anchors if len(re.findall(r"[\u3400-\u9fff]", anchor)) >= 3}
    if not anchors:
        return bundle
    selected = []
    rows, _ = _validated_rows(bundle["observations"], sha)
    for row in rows:
        box = row["box"]
        if not any(anchor in normalized_text(row["text"]) for anchor in anchors):
            continue
        # A clipped detector line could omit a negation or qualifier.
        if not _inside_crop(row["box"], row["crop"]):
            continue
        selected.append((row["raw"], box))
    if not selected or len(selected) > 32:
        return bundle
    reread = []
    reader = engine()
    with Image.open(path) as image:
        for raw, box in selected:
            for text, confidence, scale in _read_detected_line(reader, image, box):
                if confidence < MIN_CONFIDENCE or not _single_digit_reread(raw["text"], text):
                    continue
                reread.append({**raw, "text":text, "confidence":confidence,
                               "observation_id":f'{raw["observation_id"]}:line:{scale}',
                               "line_reread":{"original_text":raw["text"],
                                              "original_observation_id":raw["observation_id"],
                                              "horizontal_scale":scale}})
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise OSError("Source image changed during local line reread")
    return {**bundle, "observations":[*bundle["observations"], *reread]} if reread else bundle


def _single_digit_reread(original, reread):
    """Reject alternative reads changing an existing digit, wording, or unit.

    A fresh OCR result may disambiguate one Latin-shaped glyph inside a
    numeral. This predicate rejects all broader disagreement; it does not
    create or substitute a character in either reading.
    """
    prior, current = (_note_format(normalized_text(text), omit_number=False) for text in (original, reread))
    if len(prior) != len(current):
        return False
    differences = [i for i, (a, b) in enumerate(zip(prior, current)) if a != b]
    if len(differences) != 1:
        return False
    index, = differences
    return (prior[index].isascii() and prior[index].isalpha() and current[index] in "0123456789"
            and bool(re.search(r"\d[.]?$", current[:index]) or re.match(r"\d", current[index+1:])))


def independently_reread_location(result, bundle, *, reference):
    """Require corroboration before using a match found only by rereading."""
    if result["status"] not in {"unique_exact", "unique_format_equivalent"}:
        return False
    location, = result["locations"]
    variants = location.get("observed_variants") or [location]
    lines = [line for variant in variants for line in variant["source_lines"] if line.get("line_reread")]
    by_crop = {}
    for line in lines:
        by_crop.setdefault(tuple(line["crop"]), set()).add(line["line_reread"]["horizontal_scale"])
    # Multiple scale settings on a single crop are not independent positions.
    if not reference:
        return sum(scales == {1., 1.15} for scales in by_crop.values()) >= 2
    if len(by_crop) < 2:
        return False
    # A disputed reference glyph also needs the exact token from the
    # unmodified detector pass, even if that pass clipped adjacent prose.
    target = _REFERENCE_TOKEN.search(reference_typography(normalized_text(result["original_text"])))
    if not target:
        return False
    anchor = _box(location["bbox"])
    rows, _ = _validated_rows(bundle["observations"], bundle["image_sha256"])
    for row in rows:
        if row["reread"]:
            continue
        other = row["box"]
        height = min(anchor[3] - anchor[1], other[3] - other[1])
        if (abs((anchor[1] + anchor[3]) - (other[1] + other[3])) <= height * .5
                and min(anchor[2], other[2]) - max(anchor[0], other[0]) >= (other[2] - other[0]) * .85
                and any(match["token"] == target["token"] for match in
                        _REFERENCE_TOKEN.finditer(reference_typography(normalized_text(row["text"]))))):
            result["reread_token_corroboration"] = {"text":row["text"], "bbox":dict(zip(_KEYS, other)),
                                                    "observation_id":row["id"], "crop":list(row["crop"])}
            return True
    return False


def read_note_observations(image_path, *, engine=None):
    """Offline OCR on the immutable input image, including a shifted column grid."""
    from PIL import Image
    from .local_sheet import _engine, _image_words

    path = Path(image_path)
    image_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    engine = engine if engine is not None else _engine()
    observations = []
    with Image.open(path) as image:
        for x in (.02, .32, .62, .17, .47):
            for y in (.025, .40, .70):
                region = (x, y, min(.99,x+.38), min(.99,y+.475))
                for word in _image_words(engine, image, region):
                    observations.append({**word.evidence(), "observation_id":str(len(observations)),
                                         "crop":list(region), "image_sha256":image_sha})
    if hashlib.sha256(path.read_bytes()).hexdigest() != image_sha:
        raise OSError("Source image changed during local observation")
    return {"version":VERSION, "image_sha256":image_sha, "observations":observations}


def cached_note_observations(image_path, *, cache_root, engine=None, engine_factory=None, cache_only=False):
    """Cache only local observations, isolated from paid provider region caches."""
    path = Path(image_path).resolve()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    identity = {"path":str(path),"image_sha256":sha,"version":VERSION}
    encode = lambda value: json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
    folder = Path(cache_root)/"cache"/"positioned-notes"
    cache = folder/(hashlib.sha256(encode(identity)).hexdigest()+".json")
    try:
        saved = json.loads(cache.read_text())
        bundle = saved["bundle"]
        if (saved.get("identity") == identity and saved.get("bundle_sha256") == hashlib.sha256(encode(bundle)).hexdigest()
                and bundle.get("version") == VERSION and bundle.get("image_sha256") == sha
                and isinstance(bundle.get("observations"),list)
                and hashlib.sha256(path.read_bytes()).hexdigest() == sha):
            return bundle
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        pass
    if cache_only:
        return None
    folder.mkdir(parents=True,exist_ok=True)
    if engine is None and engine_factory is not None:
        engine = engine_factory()
    bundle = read_note_observations(path,engine=engine)
    if bundle.get("image_sha256") != sha:
        raise OSError("Source image changed before local observation")
    saved = {"identity":identity,"bundle":bundle,"bundle_sha256":hashlib.sha256(encode(bundle)).hexdigest()}
    with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=folder,delete=False) as handle:
        handle.write(json.dumps(saved,ensure_ascii=False))
        temporary = Path(handle.name)
    temporary.replace(cache)
    return bundle
