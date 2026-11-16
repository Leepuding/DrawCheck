"""Positioned default length units, scoped to a verified volume and measurement kind."""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import re
import unicodedata

from backend.evidence_selection import reliable_fact, selected_facts
from backend.references import sheet_volume, verified_sheet_number
from backend.store import Source, StoreError
from backend.store.paths import safe_path

LENGTH_SCALE_MM = {"mm": Decimal(1), "cm": Decimal(10), "m": Decimal(1000)}
_UNITS = {"毫米": "mm", "厘米": "cm", "米": "m", **{unit: unit for unit in LENGTH_SCALE_MM}}
_UNIT = r"毫米|厘米|米|mm|cm|m"
_DIMENSIONS = {"尺寸", "标高"}
_BOX_KEYS = ("x0", "y0", "x1", "y1")
_POSITION_VERSION = 2
_NOTE_INDEX = r"(?:(?:[0-9]{1,3}[.、]|\([0-9一二三四五六七八九十]+\))(?:其他)?)?"
_NOTE_HEADER = re.compile(rf"^{_NOTE_INDEX}(?:说明|注):")
_HEAD = (_NOTE_INDEX +
         r"(?:(?:除(?:本卷册|本图|图中)?(?:另有|另行|特别|特殊)?(?:标注|注明|说明)外),?)?"
         r"(?:本卷册|本册|图中)?(?:所注|所标)?")
_DECLARATION = re.compile(
    rf"{_HEAD}(?P<kind>平面尺寸|立面尺寸|尺寸|标高|高程)(?:均|统一|皆)?"
    rf"(?:以(?P<unit>{_UNIT})(?:\((?P<alias>{_UNIT})\))?为单位"
    rf"|(?:的)?单位(?:为|是|:)(?P<short>{_UNIT})(?:\((?P<short_alias>{_UNIT})\))?)",
    re.I,
)
_NUMBER = r"[+\-±]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
_VALUE = rf"[ΦØR]?(?:{_NUMBER})(?:[×xX]{_NUMBER}){{0,2}}"
_MEASUREMENT = re.compile(
    rf"(?:(?:[^:;。?!]{{0,36}}?(?:尺寸|净高|高度|长度|宽度|厚度|间距|跨度|直径|埋深|深度|标高|高程)|±0(?:\.0+)?)"
    rf"(?:为|[:=])?)?(?P<value>{_VALUE})(?P<unit>{_UNIT})?",
    re.I,
)


def _compact(text):
    return "".join(unicodedata.normalize("NFKC", text).split())


def declaration_units(text):
    """Read complete, unqualified clauses; keep the actual note in evidence."""
    raw = _compact(text)
    if "?" in raw or re.search(r"是否|假设|暂按|不作为", raw):
        return []
    # First match the whole text after any exception phrase, then individual
    # clauses. A restricted field such as 平面尺寸 is never a volume default.
    raw = re.sub(r"(除[^,。;]{0,18}?(?:注明|标注|说明)外),", r"\1", raw)
    found = []
    for sentence in re.split(r"[。;]", raw):
        clauses = _NOTE_HEADER.sub("", sentence, count=1).split(",")
        matches = [_DECLARATION.fullmatch(clause) for clause in clauses]
        # An unknown qualifier can restrict every declaration in the sentence.
        # A named field such as 平面尺寸 restricts only its own clause.
        if not all(matches):
            continue
        for match in matches:
            if match["kind"] in {"平面尺寸", "立面尺寸"}:
                continue
            unit = _UNITS[(match["unit"] or match["short"]).lower()]
            alias = match["alias"] or match["short_alias"]
            if alias and _UNITS[alias.lower()] != unit:
                continue
            found.append(("尺寸" if match["kind"] == "尺寸" else "标高", unit))
    return found


def valid_positioned_statement(fact):
    if (not reliable_fact(fact) or fact.get("bbox_clamped") or fact.get("region_bbox_unlocated")
            or fact.get("region_conflict") or fact.get("unit_invalid")
            or not isinstance(fact.get("text"), str) or not fact["text"].strip()):
        return False
    try:
        x0, y0, x1, y1 = (fact[k] for k in _BOX_KEYS)
        return (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
                and (x1-x0)*(y1-y0) < .35 and float(fact.get("confidence", 1)) >= .85)
    except (KeyError, TypeError, ValueError):
        return False


def literal_measurement(text):
    """Read a single visible measurement or vector, never a range or model guess."""
    text = _compact(text or "").rstrip("。;,")
    match = _MEASUREMENT.fullmatch(text)
    if not match:
        return None, None
    return match["value"], _UNITS.get((match["unit"] or "").lower())


def positioned_length_unit(text, value):
    """Read an explicit suffix for a value already located by geometry/text rules."""
    raw = _compact(text or "")
    value = _compact(value)
    matches = set(re.findall(
        rf"(?<![A-Za-z0-9.+\-±]){re.escape(value)}({_UNIT})(?![A-Za-z0-9²³^/%])", raw, re.I))
    normalized = {_UNITS[unit.lower()] for unit in matches}
    return next(iter(normalized)) if len(normalized) == 1 else None


def local_declaration_evidence(words):
    # The general engineering OCR tiles can cut off a qualifier on the left.
    # Optical notes need the separately verified declaration location below.
    return [word.evidence() for word in words if word.source != Source.VISION.value
            and word.score >= .85 and declaration_units(word.text)]


def _verified_note_location(fact):
    if fact.get("source") != Source.VISION.value or fact.get("_local_position"):
        return True
    recovery = fact.get("position_recovery") or {}
    positions = recovery.get("locations") or []
    return (recovery.get("status") == "unique_exact" and recovery.get("original_text") == fact["text"]
            and len(positions) == 1 and positions[0].get("bbox") == {key: fact[key] for key in _BOX_KEYS})


def _legacy_hints(units, facts):
    volumes = {uid for uid, unit in units.items() if sheet_volume(verified_sheet_number(unit))[0]}
    hints = defaultdict(list)
    for fact in facts:
        if (fact["unit_id"] in volumes and fact.get("source") == Source.VISION.value
                and valid_positioned_statement(fact) and not _verified_note_location(fact)
                and declaration_units(fact["text"])):
            hints[fact["unit_id"]].append(fact)
    return hints


def _image_snapshot(store, unit, hints):
    meta = unit.get("metadata") or {}
    relative = meta.get("review_relative_path") or meta.get("preview_relative_path")
    if not relative or not hasattr(store, "root"):
        return None, None
    try:
        image_path = safe_path(store.root, relative)
        stat = image_path.stat()
    except (OSError, StoreError):
        return None, None
    literals = sorted({(row["text"], *(row[key] for key in _BOX_KEYS)) for row in hints})
    signature = hashlib.sha256(json.dumps(literals, ensure_ascii=False).encode()).hexdigest()
    return image_path, {"version": _POSITION_VERSION, "image": relative, "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns, "hints": signature}


def _locate_in_original(image_path, hints, get_engine, *, cache_root=None):
    """Read a complete optical line with margins around the old approximate box."""
    from PIL import Image
    from backend.extract.local_sheet import _image_words
    from backend.extract.positioned_notes import cached_note_observations

    evidence = {}

    def add(reading, region, facts):
        box = tuple(reading["bbox"][key] for key in _BOX_KEYS)
        if reading["confidence"] < .9 or any(
                not region[i] + .003 < box[i] < box[i+2] < region[i+2] - .003 for i in (0, 1)):
            return set()  # A crop boundary may conceal a qualifier.
        observed = _compact(reading["text"]).rstrip("。;")
        actual = set(declaration_units(reading["text"]))
        # Require the original clause, not just its two unit tokens. A cropped
        # or garbled line that happens to contain those tokens is insufficient.
        if actual and any(actual <= set(declaration_units(fact["text"]))
                          and (observed in (hint := _compact(fact["text"]).rstrip("。;")) or hint in observed)
                          for fact in facts):
            evidence[reading["text"], box] = {key: reading[key] for key in ("text", "bbox", "source", "confidence")}
            return actual
        return set()

    remaining = {entry for fact in hints for entry in declaration_units(fact["text"])}
    if cache_root and (cached := cached_note_observations(image_path, cache_root=cache_root, cache_only=True)):
        for observation in cached["observations"]:
            remaining -= add(observation, observation["crop"], hints)
        if not remaining:
            return list(evidence.values())

    with Image.open(image_path) as image:
        if min(image.size) < 450:
            return []
        crops = defaultdict(list)
        for fact in hints:
            x0, y0, x1, y1 = (fact[key] for key in _BOX_KEYS)
            region = (max(0, x0-.10), max(0, y0-.05), min(1, x1+.10), min(1, y1+.05))
            crops[region].append(fact)
        for region, facts in crops.items():
            if not any(remaining.intersection(declaration_units(fact["text"])) for fact in facts):
                continue
            for word in _image_words(get_engine(), image, region):
                remaining -= add(word.evidence(), region, facts)
            if any(remaining.intersection(declaration_units(fact["text"])) for fact in facts):
                # A second window uses the established overlapping note grid;
                # it can read the complete line when the narrow crop cannot.
                x = min((.02, .17, .32, .47, .62), key=lambda n: abs(n-region[0]))
                y = min((.025, .40, .70), key=lambda n: abs(n-region[1]+.25))
                shifted = (x, y, min(.99, x+.38), min(.99, y+.475))
                for word in _image_words(get_engine(), image, shifted):
                    remaining -= add(word.evidence(), shifted, facts)
    return list(evidence.values())


def refresh_declaration_locations(store, units, facts):
    """Verify saved model hints locally during a rule run; keep the source facts unchanged."""
    engine = None
    def get_engine():
        nonlocal engine
        if engine is None:
            from backend.extract.local_sheet import _engine
            engine = _engine()
        return engine

    for uid, hints in _legacy_hints(units, facts).items():
        unit = units[uid]
        image, identity = _image_snapshot(store, unit, hints)
        meta = unit.get("metadata") or {}
        if not identity or (meta.get("unit_declaration_recovery") or {}).get("identity") == identity:
            continue
        try:
            evidence = _locate_in_original(image, hints, get_engine, cache_root=store.root)
        except (ImportError, OSError, RuntimeError, ValueError):
            continue
        if _image_snapshot(store, unit, hints)[1] != identity:
            continue
        metadata = {**meta, "unit_declaration_recovery": {"identity": identity, "evidence": evidence}}
        units[uid] = store._update("drawing_units", uid, {"metadata_json": json.dumps(metadata, ensure_ascii=False)})


@dataclass(frozen=True)
class UnitUse:
    scale: Decimal | None = None
    declarations: tuple = ()
    conflict: bool = False


def unit_conflict_reason(category):
    return f"同卷册{category}单位声明冲突，请核对哪条声明适用"


class VolumeUnits:
    def __init__(self, units, facts, decisions=(), recoveries=None):
        self.volumes = {unit["id"]: sheet_volume(verified_sheet_number(unit))[0] for unit in units.values()}
        self.by_scope = defaultdict(dict)
        blocked = defaultdict(list)
        for row in decisions:
            if row.get("status") in {"已确认", "无法确定"}:
                blocked[row["unit_id"]].append(row)
        # Keep at most one evidence location per sheet/category/unit. Stored
        # current facts take precedence over repeat readings from a local cache.
        candidates = list(facts)
        for unit_id, unit in units.items():
            meta = unit.get("metadata") or {}
            engineering = ((meta.get("local_sheet") or {}).get("engineering") or {})
            native = [reading for reading in engineering.get("unit_declarations", [])
                      if reading.get("source") != Source.VISION.value]
            for reading in native + (recoveries or {}).get(unit_id, []):
                box = reading["bbox"]
                if any(all(box[a] < row[b] and row[a] < box[b] for a, b in (("x0", "x1"), ("y0", "y1")))
                       for row in blocked[unit_id]):
                    continue
                candidates.append({**reading, **box, "unit_id": unit_id, "_local_position": True})
        for fact in candidates:
            uid = fact.get("unit_id")
            volume = self.volumes.get(uid)
            if not volume or not valid_positioned_statement(fact) or not _verified_note_location(fact):
                continue
            for category, unit in declaration_units(fact["text"]):
                self.by_scope[volume, category].setdefault((uid, unit), {
                    "category": category, "volume": volume, "unit": unit, "unit_id": uid,
                    "drawing_number": units[uid]["drawing_number"], "ordinal": units[uid].get("ordinal"),
                    "source": fact["source"], "text": fact["text"], "fact_id": fact.get("id"),
                    "bbox": {key: fact[key] for key in _BOX_KEYS},
                })

    def for_fact(self, fact, category, *, positioned_value=None):
        if positioned_value is None:
            value, explicit = literal_measurement(fact.get("text"))
        else:
            value = positioned_value
            explicit = positioned_length_unit(fact.get("text"), positioned_value)
            raw = _compact(fact.get("text") or "")
            # A geometry reader has selected a number, but an unknown suffix
            # (e.g. m², kg, or a code) does not make that number unitless.
            if not explicit and not re.search(
                    rf"(?<![A-Za-z0-9.+\-±]){re.escape(_compact(value))}(?=$|[。;,()])", raw):
                return UnitUse()
        if explicit:
            return UnitUse(LENGTH_SCALE_MM[explicit])
        if not value or (fact.get("unit") and str(fact["unit"]).strip().lower() not in _UNITS):
            return UnitUse()
        evidence = tuple(self.by_scope.get((self.volumes.get(fact["unit_id"]), category), {}).values())
        units = {entry["unit"] for entry in evidence}
        if len(units) == 1:
            # A single located witness is enough to explain this default.
            return UnitUse(LENGTH_SCALE_MM[next(iter(units))], evidence[:1])
        return UnitUse(declarations=evidence, conflict=len(units) > 1)

    def conflicts(self):
        for (_, category), evidence in self.by_scope.items():
            if len({entry["unit"] for entry in evidence.values()}) > 1:
                rows = sorted(evidence.values(), key=lambda e: (
                    e["drawing_number"], e["ordinal"] or 0, e["bbox"]["y0"], e["bbox"]["x0"], e["unit"]))
                yield unit_conflict_reason(category), rows

    def evidence(self):
        for readings in self.by_scope.values():
            by_unit = {}
            for entry in readings.values():
                by_unit.setdefault(entry["unit"], entry)
            yield from by_unit.values()


def volume_units(store, version_id, *, units=None, facts=None, refresh=False):
    if not hasattr(store, "list_records"):
        return VolumeUnits({u["id"]: u for u in (units or [])}, [])
    if facts is None:
        selected_units, facts = selected_facts(store, version_id)
        units = selected_units.values() if units is None else units
    elif units is None:
        units = store.list_records("drawing_units", version_id=version_id)
    units = {u["id"]: u for u in units}
    if refresh:
        refresh_declaration_locations(store, units, facts)
    recovered = {}
    for uid, hints in _legacy_hints(units, facts).items():
        _, identity = _image_snapshot(store, units[uid], hints)
        snapshot = (units[uid].get("metadata") or {}).get("unit_declaration_recovery") or {}
        if identity and snapshot.get("identity") == identity:
            recovered[uid] = snapshot["evidence"]
    decisions = store.list_records("pending_confirmations", version_id=version_id)
    return VolumeUnits(units, facts, decisions, recovered)


def comparison_declarations(left, right):
    return list({(e["unit_id"], e["category"], e["unit"]): e
                 for e in (*left.declarations, *right.declarations)}.values())


def unverified_units_reason(category, left, right):
    if left.conflict or right.conflict:
        return f"同卷册{category}单位声明有冲突，未换算"
    return f"{category}单位未核实：至少一处无明示单位且无可用的同卷册声明"
