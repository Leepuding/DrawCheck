"""An in-range clamp result must not pretend to locate a readable line."""
import json

import pytest

from backend.pending.service import list_pending, confirm_pending_item
from backend.evidence_selection import selected_facts
from backend.store import Repository, BBox, Evidence, Source, StoreError


@pytest.fixture
def case(tmp_path):
    with Repository(tmp_path / 'data') as repo:
        project = repo.create_project('已知未定位')
        vid = repo.create_version(project['id'], 'A')['id']
        path = tmp_path / 'source.pdf'
        path.write_bytes(b'%PDF-immutable-fixture')
        original = repo.import_original(vid, path)['record']
        unit = repo.create_unit(vid, original['id'], kind='pdf_page', ordinal=1, name='支架说明')
        box = BBox(.51, .816, .96, .825)
        text = '电池组件自身强度应由组件厂家核实并提供相应数据'
        evidence = Evidence(unit['id'], Source.VISION, text, box)
        fact = repo.add_fact(vid, evidence)
        row = repo.create_pending(vid, evidence, '看不清')
        repo._update('drawing_units', unit['id'], {'metadata_json': json.dumps({
            'fact_annotations': {fact['id']: {'bbox_clamped': True, 'region_bbox_unlocated': True,
                'unclear': True, 'category': '施工要求'}}})})
        yield repo, vid, unit, fact, row


@pytest.mark.parametrize('status', ['已确认', '无法确定'])
def test_positive_clamped_box_is_coverage_gap_and_cannot_accept_a_new_decision(case, status):
    repo, vid, _, fact, row = case
    result = list_pending(repo, vid)
    assert result['items'] == [] and result['pending_count'] == 0
    assert result['location_gap_count'] == 1
    assert result['incomplete_items'][0]['pending_ids'] == [row['id']]
    assert row['text'] in result['incomplete_items'][0]['reason']
    with pytest.raises(StoreError, match='无有效识读位置'):
        confirm_pending_item(repo, row['id'], {'status': status, 'confirmed_text': '人工输入'})
    assert repo.get('facts', fact['id']) == fact
    assert repo.get('pending_confirmations', row['id']) == row
    assert not repo.list_records('model_calls', version_id=vid)


@pytest.mark.parametrize('status', ['已确认', '无法确定'])
def test_existing_human_decision_remains_in_history_for_unlocated_source(case, status):
    repo, vid, _, _, row = case
    repo.confirm_pending(row['id'], status=status,
        confirmed_text='人工现场复核' if status == '已确认' else None)
    bundle = list_pending(repo, vid)
    assert bundle['location_gap_count'] == 0
    assert len(bundle['items']) == 1 and bundle['items'][0]['status'] == status


def test_same_text_at_independent_valid_location_does_not_inherit_bad_position(case):
    repo, vid, unit, _, row = case
    valid = repo.create_pending(vid,
        Evidence(unit['id'], Source.VISION, row['text'], BBox(.51, .72, .96, .75)), '看不清')
    bundle = list_pending(repo, vid)
    assert [item['id'] for item in bundle['items']] == [valid['id']]
    assert bundle['location_gap_count'] == 1


def test_unrelated_text_at_same_box_does_not_inherit_bad_position(case):
    repo, vid, unit, _, row = case
    valid = repo.create_pending(vid,
        Evidence(unit['id'], Source.VISION, '另一条原文', BBox(row['x0'],row['y0'],row['x1'],row['y1'])), '看不清')
    assert [item['id'] for item in list_pending(repo, vid)['items']] == [valid['id']]


def test_annotation_must_belong_to_the_same_unit_as_the_flagged_fact(case):
    repo, vid, unit, fact, row = case
    other = repo.create_unit(vid, unit['original_id'], kind='pdf_page', ordinal=2, name='另一张图')
    original_metadata = repo.get('drawing_units', unit['id'])['metadata']
    repo._update('drawing_units', other['id'], {'metadata_json': json.dumps(original_metadata)})
    repo._update('drawing_units', unit['id'], {'metadata_json': '{}'})
    bundle = list_pending(repo, vid)
    assert [item['id'] for item in bundle['items']] == [row['id']]
    assert bundle['location_gap_count'] == 0


def test_unlocated_machine_literal_is_not_current_rule_evidence_even_when_text_is_clear(case):
    repo, vid, unit, fact, row = case
    metadata = repo.get('drawing_units', unit['id'])['metadata']
    metadata['fact_annotations'][fact['id']]['unclear'] = False
    repo._update('drawing_units', unit['id'], {'metadata_json': json.dumps(metadata)})
    assert selected_facts(repo, vid)[1] == []
    repo.confirm_pending(row['id'], status='已确认', confirmed_text='人工实际核对')
    active = selected_facts(repo, vid)[1]
    assert len(active) == 1 and active[0]['source'] == Source.USER.value
