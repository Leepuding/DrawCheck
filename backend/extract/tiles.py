"""Deterministic image regions and conservative whole-sheet evidence merging."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from math import isfinite

STRATEGY = "overview-tiles-v2"
MERGE_VERSION = "physical-evidence-v4"
THRESHOLD = 2560
TILE_SIZE = 2048
OVERLAP = 307


def plan_regions(width, height):
    if max(width, height) <= THRESHOLD:
        return []
    def starts(length):
        if length <= TILE_SIZE:
            return [0]
        positions = list(range(0, length - TILE_SIZE + 1, TILE_SIZE - OVERLAP))
        if positions[-1] != length - TILE_SIZE:
            positions.append(length - TILE_SIZE)
        return positions
    return [(x, y, min(x + TILE_SIZE, width), min(y + TILE_SIZE, height))
            for y in starts(height) for x in starts(width)]


def map_fact(fact, region, size, region_id):
    row = deepcopy(fact)
    x, y, right, bottom = region
    width, height = size
    box = row['bbox']
    # A whole-crop fallback looks smaller after mapping to the sheet. Preserve
    # its lack of a text anchor before transforming coordinates; this is audit
    # metadata, not a claim that the original model returned unclear=True.
    if row.get('bbox_clamped') or not _located_text_bbox(box):
        row['region_bbox_unlocated'] = True
    row['bbox'] = {"x0": (x + box['x0'] * (right-x))/width,
                   "x1": (x + box['x1'] * (right-x))/width,
                   "y0": (y + box['y0'] * (bottom-y))/height,
                   "y1": (y + box['y1'] * (bottom-y))/height}
    row['region_ids'] = [region_id]
    return row


def overlap_fraction(a, b):
    intersection = max(0, min(a['x1'], b['x1']) - max(a['x0'], b['x0'])) * max(0, min(a['y1'], b['y1']) - max(a['y0'], b['y0']))
    minimum = min((a['x1']-a['x0'])*(a['y1']-a['y0']), (b['x1']-b['x0'])*(b['y1']-b['y0']))
    return intersection / minimum if minimum > 0 else 0


def _located_text_bbox(box):
    """Whether input coordinates locate text rather than the complete input."""
    def coordinate(value):
        try:
            return (not isinstance(value, bool) and isinstance(value, (int, float)) and
                    isfinite(value) and 0 <= value <= 1)
        except OverflowError:
            return False

    if not isinstance(box, dict):
        return False
    values = [box.get(key) for key in ('x0', 'y0', 'x1', 'y1')]
    if not all(coordinate(value) for value in values):
        return False
    x0, y0, x1, y1 = values
    return x0 < x1 and y0 < y1 and not (x1-x0 >= .95 and y1-y0 >= .95)


def _same_text_location(a, b):
    """Require comparable physical extents, not containment in a broad box."""
    if any(row.get('bbox_clamped') or row.get('region_bbox_unlocated') or
           not _located_text_bbox(row.get('bbox')) for row in (a, b)):
        return False
    left, right = a['bbox'], b['bbox']
    intersection = (max(0, min(left['x1'], right['x1']) - max(left['x0'], right['x0'])) *
                    max(0, min(left['y1'], right['y1']) - max(left['y0'], right['y0'])))
    maximum = max((box['x1']-box['x0'])*(box['y1']-box['y0']) for box in (left, right))
    return intersection / maximum >= .7


def _literal_key(row):
    text = row.get('text', '')
    # Only terminal Chinese sentence separators in prose are insignificant.
    # Keep decimal points, ranges, units, negations and every internal character.
    return text.rstrip('。；') if row.get('category') == '说明' else text


def merge_facts(facts):
    def numeric(value):
        if value is None:
            return None
        try:
            parsed = Decimal(str(value))
            return parsed if parsed.is_finite() else str(value)
        except (InvalidOperation, ValueError):
            return str(value)
    identity_keys = ('category', 'object_id', 'comparison_key', 'identity_confirmed', 'unit',
                     'target_drawing_number', 'target_detail_number', 'detail_number', 'quantity_role')
    def corresponding(a, b):
        return (not set(a['region_ids']) & set(b['region_ids']) and
                all(a.get(key) == b.get(key) for key in identity_keys) and
                _same_text_location(a, b))

    def same_reading(a, b):
        left, right = numeric(a.get('numeric_value')), numeric(b.get('numeric_value'))
        return bool(_literal_key(a)) and _literal_key(a) == _literal_key(b) and (left == right or left is None or right is None)

    # Evaluate the original cross-region pairs before deduplication extends their
    # region sets. Otherwise A=B can hide a B/C conflict when C also came from A's
    # region. All flags are written to copies, never the cached input.
    rows = deepcopy(facts)
    duplicates = [set() for _ in rows]
    for index, fact in enumerate(rows):
        bound_field = (fact.get('identity_confirmed') is True and
                       str(fact.get('object_id') or '').strip() and
                       str(fact.get('comparison_key') or '').strip())
        for other_index in range(index+1, len(rows)):
            other = rows[other_index]
            if not corresponding(fact, other):
                continue
            if same_reading(fact, other):
                duplicates[index].add(other_index)
                duplicates[other_index].add(index)
            elif bound_field:
                fact['unclear'] = other['unclear'] = True
                fact['region_conflict'] = other['region_conflict'] = True

    # Geometric overlap is not transitive. Only an unambiguous complete group
    # can represent one annotation: a middle box must not bridge two distinct
    # locations, nor choose between repeated candidates from the same region.
    groups, visited = {}, set()
    for index in range(len(rows)):
        if index in visited:
            continue
        component, frontier = set(), [index]
        while frontier:
            member = frontier.pop()
            if member in component:
                continue
            component.add(member)
            frontier.extend(duplicates[member] - component)
        visited.update(component)
        if all(len(duplicates[member] & component) == len(component)-1 for member in component):
            groups[min(component)] = sorted(component)
        else:
            groups.update((member, [member]) for member in component)

    merged = []
    for index in sorted(groups):
        members = groups[index]
        duplicate = deepcopy(rows[index])
        for member in members[1:]:
            fact = rows[member]
            duplicate['region_ids'].extend(fact['region_ids'])
            duplicate['confidence'] = min(duplicate.get('confidence', 1), fact.get('confidence', 1))
            duplicate['unclear'] = bool(duplicate.get('unclear') or fact.get('unclear'))
            if fact.get('region_conflict'):
                duplicate['region_conflict'] = True
            if duplicate.get('numeric_value') is None:
                duplicate['numeric_value'] = fact.get('numeric_value')
        merged.append(duplicate)
    return merged
