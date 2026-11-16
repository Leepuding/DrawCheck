"""Real local evidence must repair an old anchor, without making a human decision."""
import copy
import hashlib
import json

import pytest
from PIL import Image

import backend.local_reading as recovery
from backend.local_reading import local_reading_recoveries
from backend.pending import list_pending
from backend.pending.current import current_incomplete
from backend.store import BBox, Evidence, Repository, Source
from tests.backend.rules.test_reference_reliability import seed, unit


@pytest.fixture
def case(tmp_path):
    with Repository(tmp_path / 'data') as repo:
        vid, oid = seed(repo, tmp_path)
        drawing = unit(repo, vid, oid, 'S-001', 1)
        path = repo.root / 'local.png'
        Image.new('RGB', (50, 50), 'white').save(path)
        image_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        old_box = BBox(1, .8, 1, .9)
        row = repo.create_pending(vid, Evidence(drawing['id'], Source.VISION, '施工图', old_box), '看不清')
        fact = repo.add_fact(vid, Evidence(drawing['id'], Source.VISION, '施工图', old_box))
        positioned = {'text': '施工图', 'source': Source.VISION.value, 'confidence': .999,
                      'bbox': dict(x0=.82, y0=.8, x1=.9, y1=.86), 'reader': 'local_ocr',
                      'scope': 'title_region', 'candidate_count': 1, 'field': 'drawing_type'}
        title = {'number': 'S-001', 'label': {'text': '图号', 'confidence': 1,
                 'bbox': dict(x0=.6, y0=.9, x1=.7, y1=.94)},
                 'value': {'text': 'S-001', 'confidence': 1, 'bbox': dict(x0=.72, y0=.9, x1=.9, y1=.94)}}
        meta = {'review_relative_path': 'local.png', 'local_sheet': {
            'version': 'rapidocr-test-v1', 'image_sha': image_sha, 'title': title,
            'title_readings': [positioned]}, 'fact_annotations': {fact['id']: {
                'category': '其他', 'comparison_key': 'drawing_type', 'bbox_clamped': True}}}

        def save(changes=None):
            value = copy.deepcopy(meta)
            if changes:
                changes(value)
            repo._update('drawing_units', drawing['id'], {'metadata_json': json.dumps(value)})
        save()
        yield repo, vid, drawing, row, meta, save


def test_independent_same_literal_recovers_the_real_box_and_preserves_history(case):
    repo, vid, drawing, row, meta, save = case
    before = repo.list_records('pending_confirmations', version_id=vid)
    for _ in range(2):
        recovered = local_reading_recoveries(repo, vid)
        assert list(recovered) == [row['id']]
        proof = recovered[row['id']]
        assert proof['original_bbox']['x0'] == proof['original_bbox']['x1'] == 1
        assert proof['evidence']['bbox']['x0'] < proof['evidence']['bbox']['x1']
        assert proof['original_field'] == 'drawing_type' and proof['human_confirmation'] is False
        assert current_incomplete(repo, vid, []) == []
        view = list_pending(repo, vid)
        assert view['pending_count'] == view['incomplete_pending_count'] == view['location_gap_count'] == 0
        assert view['recovered_readings'] == list(recovered.values())
    assert repo.list_records('pending_confirmations', version_id=vid) == before
    assert not repo.list_records('model_calls', version_id=vid)


@pytest.mark.parametrize('change', [
    lambda m: m['local_sheet'].update(image_sha='0' * 64),
    lambda m: m['local_sheet'].update(version='remote-model-response'),
    lambda m: m['local_sheet']['title'].update(label={}),
    lambda m: m['local_sheet']['title_readings'][0].update(text='2?00'),
    lambda m: m['local_sheet']['title_readings'][0].update(confidence=.8),
    lambda m: m['local_sheet']['title_readings'][0].update(candidate_count=2),
    lambda m: m['local_sheet']['title_readings'][0].update(candidate_count=True),
    lambda m: m['local_sheet']['title_readings'][0].update(reader='cloud_vision'),
    lambda m: m['local_sheet']['title_readings'][0].update(scope='whole_page'),
    lambda m: m['local_sheet']['title_readings'][0].update(field='project_name'),
    lambda m: m['local_sheet']['title_readings'][0].update(bbox=dict(x0=1, y0=.8, x1=1, y1=.9)),
    lambda m: m['local_sheet']['title_readings'].append(copy.deepcopy(m['local_sheet']['title_readings'][0])),
    lambda m: m.update(fact_annotations={}),
    lambda m: m.update(review_relative_path='../elsewhere.png'),
])
def test_missing_conflicting_stale_or_ambiguous_evidence_does_not_close_a_gap(case, change):
    repo, vid, drawing, row, meta, save = case
    save(change)
    assert local_reading_recoveries(repo, vid) == {}
    gaps = current_incomplete(repo, vid, [])
    assert len(gaps) == 1 and gaps[0]['pending_ids'] == [row['id']]


def test_a_processed_human_decision_at_the_anchor_blocks_automatic_recovery(case):
    repo, vid, drawing, row, meta, save = case
    other = repo.create_pending(vid, Evidence(drawing['id'], Source.VISION, '施工图', BBox(1, .8, 1, .9)), '人工历史')
    repo.confirm_pending(other['id'], status='无法确定', confirmed_text=None)
    assert local_reading_recoveries(repo, vid) == {}


def test_native_pdf_candidate_requires_the_actual_page_hash(case):
    repo, vid, drawing, row, meta, save = case
    meta['local_sheet']['title_readings'][0].update(reader='positioned_native_text', source=Source.PDF_TEXT.value)
    save()
    assert local_reading_recoveries(repo, vid) == {}
    page = repo.root / 'page.pdf'
    page.write_bytes(b'%PDF-actual-page')
    meta['page_relative_path'] = 'page.pdf'
    meta['local_sheet']['pdf_sha'] = hashlib.sha256(page.read_bytes()).hexdigest()
    save()
    assert len(local_reading_recoveries(repo, vid)) == 1
    page.write_bytes(b'%PDF-another-page')
    assert local_reading_recoveries(repo, vid) == {}


@pytest.mark.parametrize('field,allowed', [('project_name',False),('drawing_type',False),('design_institution',True),(None,True)])
def test_native_institution_reading_never_borrows_an_explicit_other_field(case,field,allowed):
    repo,vid,drawing,row,meta,save=case
    fact_id=next(iter(meta['fact_annotations']))
    repo._update('facts',fact_id,{'text':'甲设计院'})
    repo._update('pending_confirmations',row['id'],{'text':'甲设计院'})
    meta['fact_annotations'][fact_id]['comparison_key']='design_institution'
    page=repo.root/'native.pdf';page.write_bytes(b'%PDF-native-fixture')
    meta['page_relative_path']='native.pdf';meta['local_sheet']['pdf_sha']=hashlib.sha256(page.read_bytes()).hexdigest()
    evidence=meta['local_sheet']['title_readings'][0]
    evidence.update(text='甲设计院',source=Source.PDF_TEXT.value,reader='positioned_native_text')
    if field is None:evidence.pop('field',None)
    else:evidence['field']=field
    save()
    before=repo.list_records('pending_confirmations',version_id=vid)
    assert bool(recovery.local_reading_recoveries(repo,vid)) is allowed
    assert bool(current_incomplete(repo,vid,[])) is not allowed
    assert repo.list_records('pending_confirmations',version_id=vid)==before


@pytest.mark.parametrize('status',['已确认','无法确定'])
@pytest.mark.parametrize('same_field',[True,False])
def test_human_field_decision_beats_variant_literal_but_not_an_unrelated_shared_zero_box(case,status,same_field):
    repo,vid,drawing,row,meta,save=case
    text='施工阶段?' if same_field else '项目名?'
    old=Evidence(drawing['id'],Source.VISION,text,BBox(1,.8,1,.9))
    prior=repo.create_pending(vid,old,'历史人工核对')
    fact=repo.add_fact(vid,old)
    meta['fact_annotations'][fact['id']]={'comparison_key':'drawing_type' if same_field else 'project_name','bbox_clamped':True}
    repo.confirm_pending(prior['id'],status=status,confirmed_text='初步设计' if status=='已确认' else None)
    if status=='已确认':repo.add_fact(vid,Evidence(drawing['id'],Source.USER,'初步设计',BBox(1,.8,1,.9)))
    save()
    before=repo.list_records('pending_confirmations',version_id=vid)
    assert bool(recovery.local_reading_recoveries(repo,vid)) is not same_field
    assert bool(current_incomplete(repo,vid,[])) is same_field
    assert repo.list_records('pending_confirmations',version_id=vid)==before
