"""Physical correspondence is required before a tile merge changes evidence."""
from copy import deepcopy
from itertools import permutations

import pytest

from backend.extract.tiles import map_fact, merge_facts, plan_regions
from tests.backend.extract.test_tiles import fact


def pair(text='安装前应去除毛刺。', **changes):
    a = fact(text=text, object_id='')
    a.update(category='说明', numeric_value=None, unit=None,
             comparison_key='', identity_confirmed=False, region_ids=['a'])
    a.update(changes)
    b = deepcopy(a)
    b['region_ids'] = ['b']
    return a, b


def test_empty_identity_and_overlapping_boxes_do_not_pair_different_notes():
    a, b = pair('无毛刺、锐边锐角均倒钝；')
    b['text'] = '横梁高度根据现场调整，但需保证支架可靠安装。'
    result = merge_facts([a, b])
    assert [r['text'] for r in result] == [a['text'], b['text']]
    assert all(not r['unclear'] and not r.get('region_conflict') for r in result)


@pytest.mark.parametrize('suffix', ['。', '。；', '；', '；。；'])
def test_same_local_note_ignores_only_terminal_sentence_separators(suffix):
    a, b = pair()
    b['text'] = a['text'].rstrip('。') + suffix
    result = merge_facts([a, b])
    assert len(result) == 1 and result[0]['text'] == a['text']
    assert result[0]['region_ids'] == ['a', 'b']
    assert not result[0]['unclear']


@pytest.mark.parametrize('left,right', [
    ('厚度4mm。', '厚度5mm。'), ('厚度4mm。', '厚度4cm。'),
    ('尺寸4～6mm。', '尺寸4～8mm。'), ('不得现场焊接。', '可以现场焊接。'),
    ('安装前应去除毛刺。', '安装后应去除毛刺。'),
    ('若不满足要求需更换螺栓。', '满足要求需更换螺栓。'),
    ('焊缝厚度4.0。', '焊缝厚度40。'),
    ('现场安装过程中可稍作调整；', '现场安装过程可稍作调整；'),
    ('A；B。', 'AB。'),
])
def test_content_differences_are_never_punctuation_duplicates(left, right):
    a, b = pair(left)
    b['text'] = right
    result = merge_facts([a, b])
    assert len(result) == 2
    assert {row['text'] for row in result} == {left, right}


def test_terminal_decimal_point_in_numeric_label_is_not_removed():
    a, b = pair('4.', category='尺寸')
    b['text'] = '4'
    assert len(merge_facts([a, b])) == 2


def test_two_real_locations_of_same_text_remain_two_annotations():
    a, b = pair()
    b['bbox'] = {'x0': .4, 'y0': .21, 'x1': .5, 'y1': .31}
    assert len(merge_facts([a, b])) == 2


def test_containing_box_does_not_establish_a_physical_text_match():
    a, b = pair()
    b['bbox'] = {'x0': .3, 'y0': .05, 'x1': .8, 'y1': .6}
    assert len(merge_facts([a, b])) == 2
    a.update(object_id='F1', comparison_key='长度', identity_confirmed=True)
    b.update(object_id='F1', comparison_key='长度', identity_confirmed=True, text='另一读数')
    assert not any(r.get('region_conflict') for r in merge_facts([a, b]))


@pytest.mark.parametrize('box', [None, {}, {'x0': 0, 'y0': 0, 'x1': 1, 'y1': 1},
    {'x0': .4, 'y0': .1, 'x1': .4, 'y1': .2},
    {'x0': .4, 'y0': .1, 'x1': float('nan'), 'y1': .2},
    {'x0': .4, 'y0': .1, 'x1': 10**1000, 'y1': .2},
    {'x0': .4, 'y0': .1, 'x1': 1.1, 'y1': .2}])
def test_missing_degenerate_or_full_page_location_cannot_be_merged(box):
    a, b = pair(bbox=box)
    assert len(merge_facts([a, b])) == 2


def test_clamped_location_and_original_unclear_are_retained():
    a, b = pair(bbox_clamped=True, unclear=True)
    assert len(merge_facts([a, b])) == 2
    assert all(r['unclear'] and r['bbox_clamped'] for r in merge_facts([a, b]))
    a, b = pair(unclear=True)
    b['unclear'] = False
    result = merge_facts([a, b])
    assert len(result) == 1 and result[0]['unclear']


def test_real_same_object_field_numeric_conflict_is_retained_without_input_mutation():
    a = fact(); a['region_ids'] = ['a']
    b = deepcopy(a); b.update(text='101', numeric_value='101', region_ids=['b'])
    incoming = [a, b]
    before = deepcopy(incoming)
    first = merge_facts(incoming)
    assert incoming == before
    assert len(first) == 2 and all(r['unclear'] and r['region_conflict'] for r in first)
    assert merge_facts(incoming) == first
    assert [r['text'] for r in first] == ['100', '101']


@pytest.mark.parametrize('field,value', [('object_id', ''), ('comparison_key', ''),
                                       ('identity_confirmed', False)])
def test_incomplete_business_identity_does_not_invent_numeric_conflict(field, value):
    a = fact(); a.update(region_ids=['a'], **{field: value})
    b = deepcopy(a); b.update(text='101', numeric_value='101', region_ids=['b'])
    result = merge_facts([a, b])
    assert len(result) == 2 and not any(r.get('region_conflict') for r in result)


def test_same_region_repetitions_are_not_deduplicated():
    a, b = pair()
    b['region_ids'] = ['a']
    assert len(merge_facts([a, b])) == 2


def test_duplicate_keeps_low_confidence_original_unclear_and_conflict_flags():
    a, b = pair()
    b.update(confidence=.4, unclear=True, region_conflict=True)
    incoming = deepcopy([a, b])
    result = merge_facts([a, b])
    assert len(result) == 1 and result[0]['confidence'] == .4
    assert result[0]['unclear'] and result[0]['region_conflict']
    assert [a, b] == incoming


@pytest.mark.parametrize('shared_region', [False, True])
def test_duplicate_does_not_hide_third_reading_conflict_in_any_order(shared_region):
    a = fact(); a['region_ids'] = ['a']
    b = deepcopy(a); b['region_ids'] = ['b']
    c = deepcopy(a); c.update(text='101', numeric_value='101',
                             region_ids=['a' if shared_region else 'c'])
    for incoming in permutations([a, b, c]):
        before = deepcopy(incoming)
        result = merge_facts(list(incoming))
        assert len(result) == 2
        assert {row['text'] for row in result} == {'100', '101'}
        assert all(row['unclear'] and row['region_conflict'] for row in result)
        assert list(incoming) == list(before)


def test_overlapping_middle_box_cannot_bridge_two_locations_in_any_order():
    rows = []
    for index, left in enumerate([.400, .425, .450]):
        a, _ = pair(bbox={'x0': left, 'y0': .1, 'x1': left+.1, 'y1': .2})
        a['region_ids'] = [str(index)]
        rows.append(a)
    for incoming in permutations(rows):
        before = deepcopy(incoming)
        result = merge_facts(list(incoming))
        # AB and BC overlap sufficiently, AC does not. No unique physical group
        # can be selected; preserve all original readings instead of a bridge.
        assert len(result) == 3
        assert [r['bbox'] for r in result] == [r['bbox'] for r in incoming]
        assert all(len(r['region_ids']) == 1 and not r['unclear'] for r in result)
        assert list(incoming) == list(before)


def test_ambiguous_same_region_candidates_are_not_arbitrarily_deduplicated():
    a, b = pair()
    c = deepcopy(a)
    for incoming in permutations([a, b, c]):
        assert len(merge_facts(list(incoming))) == 3


def test_complete_three_region_group_can_merge_in_any_order():
    a, b = pair()
    c = deepcopy(a); c['region_ids'] = ['c']
    for incoming in permutations([a, b, c]):
        result = merge_facts(list(incoming))
        assert len(result) == 1
        assert result[0]['region_ids'] == [r['region_ids'][0] for r in incoming]


@pytest.mark.parametrize('different_reading', [False, True])
def test_whole_crop_fallback_is_not_a_text_anchor_after_mapping(different_reading):
    size = (2561, 1200)
    regions = plan_regions(*size)
    assert len(regions) == 2
    original = fact(bbox={'x0': 0, 'y0': 0, 'x1': 1, 'y1': 1})
    if not different_reading:
        original.update(object_id='', comparison_key='', identity_confirmed=False)
    before = deepcopy(original)
    a, b = [map_fact(original, region, size, str(i)) for i, region in enumerate(regions)]
    if different_reading:
        b.update(text='101', numeric_value='101')
    assert all(row['region_bbox_unlocated'] and not row['unclear'] for row in (a, b))
    assert a['bbox']['x1'] < .8 and b['bbox']['x0'] > .2
    result = merge_facts([a, b])
    assert len(result) == 2
    assert not any(row.get('region_conflict') or row['unclear'] for row in result)
    assert original == before


@pytest.mark.parametrize('kind', ['fact', 'catalog', 'title'])
@pytest.mark.parametrize('clamped', [False, True])
def test_unlocated_audit_flag_does_not_invent_unclear_for_other_mapped_evidence(kind, clamped):
    row = {'bbox': {'x0': 0, 'y0': 0, 'x1': 1, 'y1': 1}, 'unclear': False}
    if kind == 'fact':
        row.update(text='说明原文', category='说明')
    elif kind == 'catalog':
        row['drawing_number'] = 'S-001'
    else:
        row.update(drawing_number='S-001', name='基础图')
    if clamped:
        row.update(bbox={'x0': .2, 'y0': .2, 'x1': .3, 'y1': .3}, bbox_clamped=True)
    before = deepcopy(row)
    result = map_fact(row, (1000, 0, 3048, 1200), (4000, 1200), 'b')
    assert result['region_bbox_unlocated'] and result['unclear'] is False
    assert row == before


def test_valid_crop_text_anchors_can_merge_after_mapping():
    size = (2561, 1200)
    regions = plan_regions(*size)
    mapped = []
    # Two precise local crops of the same actual sheet text extent.
    for index, (x, y, right, bottom) in enumerate(regions):
        box = {'x0': (1100-x)/(right-x), 'y0': .1,
               'x1': (1200-x)/(right-x), 'y1': .2}
        mapped.append(map_fact(fact(bbox=box), regions[index], size, str(index)))
    assert not any(row.get('region_bbox_unlocated') for row in mapped)
    assert len(merge_facts(mapped)) == 1


def test_clamped_marker_survives_mapping_and_blocks_both_merge_paths():
    a = fact(bbox={'x0': .4, 'y0': .1, 'x1': .5, 'y1': .2})
    a.update(bbox_clamped=True, unclear=True)
    b = deepcopy(a)
    a = map_fact(a, (0, 0, 2048, 1200), (3000, 1200), 'a')
    b = map_fact(b, (0, 0, 2048, 1200), (3000, 1200), 'b')
    assert len(merge_facts([a, b])) == 2
    b.update(text='101', numeric_value='101')
    result = merge_facts([a, b])
    assert len(result) == 2
    assert all(row['unclear'] and row['region_bbox_unlocated'] for row in result)
    assert not any(row.get('region_conflict') for row in result)
