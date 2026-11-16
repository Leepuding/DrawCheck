"""Literal drawing references and conservative evidence for absence conclusions.

Matching proves sheet identity only, never the suitability of a referenced detail.
Only a verified home sheet may supply the prefix for a same-volume shorthand.
No filenames, ordinals, expected prefixes or character-confusion substitutions.
"""
from __future__ import annotations

import re

from backend.evidence_selection import reliable_fact, usable_title_identity
from backend.store import Source
from backend.title_identity import manual_number_mode, project_code

# At least one separator and both letters and digits are required for a sheet
# identity. A bare number such as 04 cannot establish a unique sheet.
_TOKEN = re.compile(r"(?<![A-Za-z0-9_./-])[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)+(?![A-Za-z0-9_./-])")
_RELATION = re.compile(
    r"(?:详见|参见|见(?:详图|图)?|详图索引|索引)\s*[:：]?\s*[（(]?\s*"
    r"(?:(?:图纸(?:编号)?|图号|图)\s*[:：]?\s*)?[（(]?\s*"
    r"(-?[A-Za-z0-9][A-Za-z0-9_./-]*)")
_PLAIN_DETAIL_RELATION = re.compile(r"详图\s*[:：]?\s*(?:图\s*)?(-?[A-Za-z0-9][A-Za-z0-9_./-]*)")
_VOLUME = re.compile(r"(?:详见|参见|见)\s*[:：]?\s*[（(]?\s*(?:卷册\s*(?P<leading>[A-Z]+\d{3,6})|(?P<trailing>[A-Z]+\d{3,6})\s*(?:卷册|图册))", re.I)
AMBIGUOUS_REFERENCE_REASON = "局部数字简写或引用图号不明确，不能确认目标图身份"
STRUCTURED_TARGET_CONFLICT_REASON = "引用原文与结构化目标不一致，需确认"
_REFERENCE_ACTION = re.compile(r"详见|参见|见\s*(?:详图|图|节点|索引)")
_INDEX_LABEL = re.compile(r"(?:详图|节点)\s*索引|(?:^|[，,。；;\s])索引\s*[:：]")
SAME_VOLUME_SHORTHAND = "same_volume_shorthand"
SAME_VOLUME_RANGE = "same_volume_range"
BARE_VOLUME = "bare_volume"
CROSS_PROJECT_VOLUME_REASON = "引用候选图纸所属卷册与来源图纸的项目代码不同，待确认"
UNLISTED_REFERENCE_REASON = "引用候选图纸未在本批实到图或目录中找到，待确认"
TITLE_CONFLICT_REASON = "引用候选图纸的目录号与实到图签不符，待确认"
_VOLUME_CODE = re.compile(r"[A-Z]+[0-9]{3,6}")
_PARTIAL_SHEET = re.compile(r"[A-Z]+[0-9]{3,6}-[0-9]{2,}[A-Z]?")
_RANGE_END = re.compile(r"\s*[~～]\s*-(?P<end>[0-9]{2,6})\s*(?:[）)]\s*)?图(?=$|\s*[。；;，,）)])")
_SUFFIX = re.compile(r"(?P<stem>.+)-(?P<sequence>[0-9]+)")
_SHORTHAND = re.compile(r"-([0-9]{2,})")
_ANOTHER_TARGET = re.compile(
    r"\s*(?:图\s*)?[）)]?\s*(?:图\s*)?"
    r"(?:[~～〜…—–-]|等|(?:、|[,，/／&＋+]|或者|或|以及|和|及|与|至|到)"
    r"\s*(?:图纸(?:编号)?|图号|图)?\s*[:：]?[（(]?\s*-?(?:[A-Za-z]*[0-9]|[A-Za-z]+(?=-[0-9]))|[（(]\s*-?[A-Za-z]*[0-9])"
)


def sheet_number(value):
    return (isinstance(value, str) and bool(_TOKEN.fullmatch(value))
            and any(c.isalpha() for c in value) and any(c.isdigit() for c in value))


def verified_sheet_number(unit):
    """Strict gate for new inferences; legacy full-code matching keeps its own gate."""
    if not unit:
        return None
    meta = unit.get("metadata") or {}
    number = (unit.get("drawing_number") or "").strip()
    if meta.get("out_of_scope") or meta.get("is_catalog_sheet") or not sheet_number(number):
        return None
    if manual_number_mode(unit) == "explicit" or (meta.get("extract_status") == "ok" and usable_title_identity(unit)):
        return number
    local = (meta.get("local_sheet") or {}).get("title") or {}
    resolution = meta.get("title_number_resolution") or {}
    if (not meta.get("title_identity_uncertain") and local.get("number") == number
            and local.get("label") and local.get("value") and resolution.get("status") == "local_selected"
            and local["value"] == resolution.get("selected_evidence")
            and local["label"] == resolution.get("label_evidence")):
        return number
    return None


def sheet_volume(number):
    """Return the full volume stem and sheet sequence, with no suffix guessing."""
    match = _SUFFIX.fullmatch(number) if sheet_number(number) else None
    return (match["stem"], match["sequence"]) if match else (None, None)


def volume_reference(text):
    match = _VOLUME.search(text or "")
    return (match["leading"] or match["trailing"]) if match else None


def short_reference_answer(text):
    """Read an explicit sheet code for a confirmed, positioned short reference."""
    text = (text or "").strip()
    if sheet_number(text):
        text = f"见图{text}"
    relation = _RELATION.match(text)
    if not relation or not re.fullmatch(r"\s*[）)]?\s*图?\s*[。.;；]?", text[relation.end():]):
        return None
    reference = drawing_reference({"text": text})
    if reference and reference.get("target") and not reference.get("reason"):
        return text
    return None


def visible_reference_hint(fact):
    """Literal reference action/index marker, even when its target is unreadable.

    A detail title or a note mentioning a node is not a reference. Neither model
    category nor a reported target supplies a missing visible relationship.
    """
    text = (fact.get("text") or "").strip()
    if drawing_reference(fact):
        return True
    for pattern in (_REFERENCE_ACTION, _INDEX_LABEL):
        for marker in pattern.finditer(text):
            tail = re.split(r"[，,。；;]", text[marker.end():], maxsplit=1)[0].strip(" \t\r\n:：")
            tail = tail.strip("（）() \t\r\n")
            tail = re.sub(r"^(?:图纸(?:编号)?|图号|图)\s*[:：]?\s*", "", tail).strip("（）() \t\r\n")
            # A readable named table/document is not a missing target. This
            # fallback handles only a genuinely absent/unreadable target, never
            # invents a sheet-number relationship for other visible Chinese text.
            if re.fullmatch(r"(?:图(?:号)?)?\s*[?？□…⋯]*", tail):
                return True
    return False


def drawing_reference(fact, home_number=None):
    """Return the visible reference with its provenance, or None for non-references.

    The original text must contain a reference action or an actual index label.
    A bare sheet code may have graphical index context, but model category/target
    fields alone do not prove that context or establish a reference.
    A model-split sheet/detail suffix is not evidence of an internal detail label.
    The optional home number is the caller's verified title-block identity.
    """
    text = (fact.get("text") or "").strip()
    volumes = list(_VOLUME.finditer(text))
    # A caption followed by a material grade does not establish a reference.
    # Keep literal sheet codes and numeric detail indices, as well as action
    # forms such as “详见” and explicit index labels.
    matches = sorted(
        (match for pattern in (_RELATION, _PLAIN_DETAIL_RELATION) for match in pattern.finditer(text)
         if pattern is _RELATION or sheet_number(token := match[1].rstrip(".,;，。；:："))
         or re.fullmatch(r"-?[0-9]+", token)),
        key=lambda match: match.start(),
    )
    relation = next((match for match in matches
                     if not any(volume.start() <= match.start() < volume.end() for volume in volumes)), None)
    if volumes and not relation:
        return None
    if not relation:
        return None
    literal = relation[1]
    prefix = text[relation.start():relation.start(1)]
    opening = next((index for index, char in enumerate(prefix) if char in '（('), None)
    if opening is not None:
        # Do not turn a parenthesized list into an arbitrarily chosen first
        # target. Preserve its visible candidates for the ambiguity diagnostic.
        start = relation.start() + opening + 1
        closing = re.search(r'[）)]', text[relation.end():])
        if closing:
            end = relation.end() + closing.start()
            enclosed = text[start:end]
            if len([m for m in _TOKEN.finditer(enclosed) if sheet_number(m[0])]) > 1:
                literal = enclosed
    literal = literal.rstrip(".,;，。；:：")
    tokens = [m[0] for m in _TOKEN.finditer(literal) if sheet_number(m[0])]
    target = tokens[0] if len(tokens) == 1 else None
    reported = fact.get("target_drawing_number")
    detail = fact.get("target_detail_number")
    result = {"fact_id": fact.get("id"), "source": fact.get("source"), "text": text,
              "bbox": {key:fact[key] for key in ("x0", "y0", "x1", "y1")} if all(key in fact for key in ("x0", "y0", "x1", "y1")) else None,
              "literal_target": literal, "target": target, "reported_target": reported,
              "reported_detail": detail, "detail": detail, "identity_only": True,
              "reason": None}
    if fact.get("confirmed_short_reference"):
        result["confirmed_short_reference"] = True
    if target is None:
        short = _SHORTHAND.fullmatch(literal)
        stem, sequence = sheet_volume(home_number)
        tail = text[relation.end(1):]
        simple = relation.re is _RELATION and len(matches) == 1 and not volumes
        end = _RANGE_END.match(tail)
        if (short and stem and simple and end and len(sequence) <= 6
                and len(short[1]) == len(end["end"]) == len(sequence)
                and not _ANOTHER_TARGET.match(tail[end.end():])):
            low, high = int(short[1]), int(end["end"])
            if 0 < low < high <= low + 20 and not low <= int(sequence) <= high:
                result.update(literal_target=text[relation.start(1):relation.end(1) + end.end("end")],
                              targets=[f"{stem}-{n:0{len(sequence)}d}" for n in range(low, high + 1)],
                              target_basis=SAME_VOLUME_RANGE, detail=None, home_number=home_number)
                return result
        if (stem and simple and _VOLUME_CODE.fullmatch(literal)
                and re.fullmatch(r"\s*[）)]?\s*[。；;.]?", tail)
                and project_code(home_number)):
            result.update(target_basis=BARE_VOLUME, detail=None, home_number=home_number,
                          expected_volume=f"{stem.rsplit('-', 1)[0]}-{literal}")
            return result
        if (short and stem and len(short[1]) == len(sequence) and short[1] != sequence
                and relation.re is _RELATION and len(matches) == 1 and not volumes
                and not _ANOTHER_TARGET.match(text[relation.end(1):])):
            # The model's split target/detail fields are audit information here.
            # Only the printed shorthand and verified home number supply identity.
            result.update(target=f"{stem}{literal}", detail=None, home_number=home_number,
                          target_basis=SAME_VOLUME_SHORTHAND)
            return result
        result["reason"] = AMBIGUOUS_REFERENCE_REASON
        return result
    if (sum(sheet_number(part) for part in re.split(r"[/&+]", literal)) > 1
            or _ANOTHER_TARGET.match(text[relation.end(1):]) or len({
            m.start(1) for m in matches
            if not any(volume.start() <= m.start() < volume.end() for volume in volumes)
            and (sheet_number(raw := m[1].rstrip(".,;，。；:："))
                 or _SHORTHAND.fullmatch(raw) or _VOLUME_CODE.fullmatch(raw))}) > 1):
        result.update(target=None, reason=AMBIGUOUS_REFERENCE_REASON)
        return result
    if _PARTIAL_SHEET.fullmatch(target) and project_code(home_number):
        result["home_number"] = home_number
    # Ignore an incorrectly split suffix only when the complete code is visibly
    # present. Never fabricate it by concatenating model fields.
    if reported and reported != target:
        if detail and target == f"{reported}-{detail}":
            result["detail"] = None
            result["structured_split_sheet_suffix"] = True
        else:
            result["reason"] = STRUCTURED_TARGET_CONFLICT_REASON
    elif detail and target.endswith("-" + str(detail)) and not re.search(
            rf"(?:详图|节点)\s*[:：]?\s*{re.escape(str(detail))}(?![A-Za-z0-9])", text):
        result["detail"] = None
        result["structured_split_sheet_suffix"] = True
    return result


def _conflicting_catalog_title(target, units):
    """A literal row and a uniquely named actual title disagree within one PDF."""
    from backend.extract.local_sheet import compact

    matches = []
    for cat in units.values():
        meta = cat.get("metadata") or {}
        if (not cat.get("original_id") or not meta.get("is_catalog_sheet")
                or meta.get("out_of_scope") or meta.get("extract_status") == "incomplete"):
            continue
        rows = ((meta.get("local_sheet") or {}).get("catalog") or {}).get("rows", [])
        for row in rows:
            if row.get("number") != target or not row.get("name"):
                continue
            name = compact(row["name"]["text"])
            if sum(bool(r.get("name")) and compact(r["name"]["text"]) == name for r in rows) != 1:
                continue
            actual = []
            for unit in units.values():
                if unit.get("original_id") != cat["original_id"]:
                    continue
                title = ((unit.get("metadata") or {}).get("local_sheet") or {}).get("title") or {}
                printed = verified_sheet_number(unit)
                if (printed and printed == title.get("number") and title.get("value")
                        and title.get("name") and compact(title["name"]["text"]) == name):
                    actual.append((unit, title))
            if len(actual) == 1:
                unit, title = actual[0]
                if title["number"] != target:
                    matches.append({"catalog_unit_id": cat["id"], "catalog_number": target,
                                    "catalog_evidence": row["value"], "unit_id": unit["id"],
                                    "title_number": title["number"], "title_evidence": title["value"]})
    return matches


def resolve_reference(reference, units):
    """Exact or delimiter-boundary suffix match, unique across all actual sheets."""
    result = {**reference, "status": "unverified", "unit_ids": [], "match_basis": None}
    if reference.get("reason"):
        return result
    if reference.get("target_basis") == SAME_VOLUME_RANGE:
        members = [resolve_reference({**reference, "target": number, "target_basis": SAME_VOLUME_SHORTHAND}, units)
                   for number in reference["targets"]]
        result["unverified_targets"] = [m["target"] for m in members if m["status"] != "resolved"]
        if not result["unverified_targets"]:
            result.update(status="resolved", unit_ids=[m["unit_ids"][0] for m in members],
                          resolved_numbers=reference["targets"], match_basis=SAME_VOLUME_RANGE)
        else:
            result["reason"] = AMBIGUOUS_REFERENCE_REASON
        return result
    if reference.get("target_basis") == BARE_VOLUME:
        candidates, competing_sheets = [], []
        for unit in units.values():
            meta = unit.get("metadata") or {}
            header = meta.get("catalog_volume_evidence") or {}
            number = (unit.get("drawing_number") or "").strip()
            if (not meta.get("is_catalog_sheet") and not meta.get("out_of_scope")
                    and (number == reference["literal_target"] or number.endswith("-" + reference["literal_target"]))):
                competing_sheets.append(unit["id"])
            if (meta.get("is_catalog_sheet") and not meta.get("out_of_scope")
                    and meta.get("extract_status") != "incomplete" and header.get("number")
                    and (not manual_number_mode(unit) or number == header["number"])
                    and header["number"].endswith("-" + reference["literal_target"])):
                candidates.append((unit["id"], header["number"]))
        result["catalog_candidate_numbers"] = sorted({number for _, number in candidates})
        if len(candidates) == 1 and not competing_sheets and candidates[0][1] == reference["expected_volume"]:
            result.update(status="resolved", unit_ids=[candidates[0][0]], resolved_number=candidates[0][1],
                          match_basis=BARE_VOLUME)
        elif len(candidates) == 1 and not competing_sheets and project_code(candidates[0][1]) not in (
                None, project_code(reference["home_number"])):
            result["reason"] = CROSS_PROJECT_VOLUME_REASON
        else:
            result["reason"] = AMBIGUOUS_REFERENCE_REASON
        return result
    if not reference.get("target"):
        return result
    target = reference["target"]
    shorthand = reference.get("target_basis") == SAME_VOLUME_SHORTHAND

    def matches(number):
        return number == target or (not shorthand and not reference.get("confirmed_short_reference")
                                    and any(number.endswith(sep + target) for sep in "-_./"))

    candidates = []
    for unit in units.values():
        meta = unit.get("metadata") or {}
        if meta.get("is_catalog_sheet") or (meta.get("out_of_scope") and not shorthand):
            continue
        number = (unit.get("drawing_number") or "").strip()
        if matches(number):
            candidates.append(unit)
    # A short code may also identify a catalogued sheet not yet delivered.
    # Its absence must not make an otherwise ambiguous suffix look unique.
    catalog_numbers = set()
    for unit in units.values():
        meta = unit.get("metadata") or {}
        raw = meta.get("catalog_listed_numbers")
        if (meta.get("is_catalog_sheet") and not meta.get("out_of_scope")
                and meta.get("extract_status") != "incomplete" and isinstance(raw, list)):
            catalog_numbers.update(n.strip() for n in raw if isinstance(n, str) and matches(n.strip()))
    possible_numbers = catalog_numbers | {unit["drawing_number"].strip() for unit in candidates}
    home_project = project_code(reference.get("home_number")) if not shorthand else None
    incompatible = home_project and any(project_code(number) != home_project for number in possible_numbers)
    result["catalog_candidate_numbers"] = sorted(catalog_numbers)
    result["unit_ids"] = [unit["id"] for unit in candidates]
    if len(candidates) > 1 or len(possible_numbers) > 1:
        result.update(status="ambiguous", reason="引用对应多个图纸身份，需确认")
    elif incompatible:
        result.update(reason="引用候选图纸身份尚未核实，需确认")
    elif not candidates:
        conflicts = _conflicting_catalog_title(next(iter(catalog_numbers)), units) if len(catalog_numbers) == 1 else []
        if conflicts:
            result.update(reason=TITLE_CONFLICT_REASON, catalog_title_conflicts=conflicts)
        elif home_project and not catalog_numbers:
            result.update(reason=UNLISTED_REFERENCE_REASON)
        else:
            result.update(status="missing", reason="当前未找到引用图号，尚需确认识别与目录范围完整")
    elif not (verified_sheet_number(candidates[0]) if shorthand else usable_title_identity(candidates[0])):
        result.update(reason="引用候选图纸身份尚未核实，需确认")
    else:
        number = candidates[0]["drawing_number"].strip()
        match_basis = SAME_VOLUME_SHORTHAND if shorthand else "exact" if number == target else "delimiter_suffix"
        result.update(status="resolved", resolved_number=number, match_basis=match_basis, reason=None)
    return result


def _independent_title(unit, facts):
    if manual_number_mode(unit) == "explicit":
        return bool((unit.get("drawing_number") or "").strip())
    resolution = (unit.get("metadata") or {}).get("title_number_resolution") or {}
    selected = resolution.get("selected_evidence") or {}
    if resolution.get("status") == "local_selected":
        local = ((unit.get("metadata") or {}).get("local_sheet") or {}).get("title") or {}
        return (local.get("number") == unit.get("drawing_number")
                and bool(selected) and selected == local.get("value")
                and resolution.get("label_evidence") == local.get("label"))
    if (resolution.get("status") != "cad_selected" or resolution.get("selected_source") != Source.CAD_TEXT.value
            or resolution.get("selected_drawing_number") != unit.get("drawing_number")):
        return False
    anchored = any(fact.get("id") == selected.get("fact_id") and fact.get("unit_id") == unit["id"]
                   and fact.get("version_id") == unit.get("version_id") and fact.get("source") == Source.CAD_TEXT.value
                   and reliable_fact(fact) and fact.get("text") == selected.get("text")
                   and all(fact.get(key) == (selected.get("bbox") or {}).get(key) for key in ("x0", "y0", "x1", "y1"))
                   for fact in facts)
    if not anchored:
        return False
    # Recheck the label/position/uniqueness too, rather than trusting a stale
    # cached "cad_selected" status after its supporting label has disappeared.
    from backend.catalog.title_resolver import resolve_title_number
    metadata = {**(unit.get("metadata") or {}), "manual_drawing_number_confirmed":False}
    current = resolve_title_number({**unit, "metadata":metadata},
        {"drawing_number":unit.get("drawing_number"), "bbox":metadata.get("title_bbox")},
        [fact for fact in facts if reliable_fact(fact)])
    return (current["status"] == "cad_selected" and current["selected_drawing_number"] == unit.get("drawing_number")
            and (current.get("selected_evidence") or {}).get("fact_id") == selected.get("fact_id"))


def import_scope_diagnostics(store, version_id):
    # Source-wide parser failures may have no drawing_unit at all. This reader
    # preserves invalid-manifest diagnostics; it performs no writes/migrations.
    from backend.revisions.service import RevisionService
    return RevisionService(store)._manifest(version_id).get("unreadable", [])


def _scope_label(unit):
    # Display useful drawing identity; internal UUIDs stay in structured records.
    label = " · ".join(str(value) for value in (unit.get("drawing_number"), unit.get("name")) if value)
    ordinal = unit.get("ordinal")
    return (label or "未命名图纸") + (f"（第{ordinal}页/图框）" if ordinal is not None else "")


def absence_scope(units, facts, *, import_diagnostics=()):
    """A complete list may exceed delivered sheets; unidentified arrivals block absence."""
    catalogs = [u for u in units.values() if (u.get("metadata") or {}).get("is_catalog_sheet")
                and not (u.get("metadata") or {}).get("out_of_scope")]
    reasons, listed = [], set()
    if import_diagnostics:
        reasons.append("源文件导入或转换仍有未完成范围，不能用现存图纸单元证明缺失")
    if not catalogs:
        reasons.append("没有完整可读目录，引用缺失范围未闭合")
    excluded = {u.get("drawing_number") for u in catalogs}
    for cat in catalogs:
        meta = cat.get("metadata") or {}
        raw = meta.get("catalog_listed_numbers")
        if (meta.get("extract_status") != "ok" or meta.get("catalog_complete") is not True
                or meta.get("catalog_volume_numbers_uncertain")
                or not isinstance(raw, list) or not raw or any(not isinstance(n, str) or not n.strip() for n in raw)):
            reasons.append(f"目录 {_scope_label(cat)} 未完整核实")
            continue
        excluded.update(n for n in meta.get("catalog_volume_numbers", []) if isinstance(n, str))
        listed.update(n.strip() for n in raw)
    listed -= excluded
    seen = set()
    for unit in units.values():
        meta = unit.get("metadata") or {}
        if meta.get("out_of_scope") or meta.get("is_catalog_sheet"):
            continue
        number = (unit.get("drawing_number") or "").strip()
        if (not usable_title_identity(unit) or not number or number not in listed) and not _independent_title(unit, facts):
            reasons.append(f"实到图纸 {_scope_label(unit)} 的身份未在完整目录或独立原文中核实")
        if meta.get("extract_status") != "ok":
            reasons.append(f"实到图纸 {_scope_label(unit)} 的视觉识别尚未完成")
        if number in seen:
            reasons.append(f"实到图号 {number} 不唯一")
        seen.add(number)
    if not listed:
        reasons.append("没有可用于引用缺失核对的完整图号清单")
    return {"closed": not reasons, "reasons": reasons, "listed_numbers": sorted(listed)}
