"""One nonblocking notice when a batch has multiple proven project codes."""
from collections import Counter

from backend.extract.local_sheet import LocalSheetReader
from backend.title_identity import project_code

_OCR_PAGE_BUDGET = 8


def project_code_notice(store, version_id, drawings):
    if len(drawings) < 2:
        return None
    imported = {row["unit_id"]: row for row in drawings}
    source_sizes = Counter(row.get("original_id") or row["unit_id"] for row in drawings)
    reader = LocalSheetReader(store)
    groups = {}
    units = [unit for unit in store.list_records("drawing_units", version_id=version_id) if unit["id"] in imported]
    budget = _OCR_PAGE_BUDGET
    for unit in sorted(units, key=lambda row: row["ordinal"]):
        source = imported[unit["id"]]
        source_size = source_sizes[source.get("original_id") or unit["id"]]
        # Read the early pages of each source first. Batch imports of loose
        # sheets still use native text; reserve the bounded OCR for smaller
        # batches and multi-page volumes.
        scan = (unit["kind"] == "pdf_page" and unit["ordinal"] <= 2 and budget > 0
                and (source_size > 1 or len(drawings) <= _OCR_PAGE_BUDGET))
        budget -= int(scan)
        title = reader.read_import_title(unit, scan=scan)
        if not title or not (code := project_code(title["number"])):
            continue
        groups.setdefault(code, {"project_code": code, "unit_id": unit["id"], "drawing_number": title["number"],
                                 "ordinal": unit["ordinal"], "source": source["source_original"],
                                 "text": title["value"]["text"], "bbox": title["value"]["bbox"]})
        if len(groups) == 2:
            break  # Two verified codes already support the single batch notice.
    if len(groups) < 2:
        return None
    return {"code": "MIXED_PROJECT_CODES", "reason": "本批已核实的图签含不同项目代码，请核对是否混入其他项目图纸。",
            "evidence": [groups[code] for code in sorted(groups)]}
