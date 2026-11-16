"""Repeated linking asks once per unresolved complete evidence anchor."""
import pytest

from backend.linking import LinkingService
from backend.linking.service import SAME_NAME_PENDING_REASON
from backend.store import BBox, Evidence, Source
from tests.backend.linking.test_linking import make_version


def setup(repo,tmp_path,*,source=Source.VISION):
    _,version,original=make_version(repo,tmp_path)
    vid=version['id']
    u=repo.create_unit(vid,original['id'],kind='pdf_page',ordinal=1,name='同名页',drawing_number='S-001')
    facts=[repo.add_fact(vid,Evidence(u['id'],source,'基础F1',BBox(x,.1,x+.1,.2))) for x in (.1,.5)]
    return vid,u,facts


def ev(fact):
    return Evidence(fact['unit_id'],Source(fact['source']),fact['text'],
                    BBox(*[fact[key] for key in ('x0','y0','x1','y1')]))


def test_repeated_linking_reuses_exact_waiting_row_without_writing(repo,tmp_path,monkeypatch):
    vid,u,facts=setup(repo,tmp_path)
    first=LinkingService(repo).link_version(vid)
    before=repo.list_records('pending_confirmations',version_id=vid)
    monkeypatch.setattr(repo,'create_pending',lambda *args,**kwargs: pytest.fail('Repeated link appended pending'))
    for _ in range(2):
        current=LinkingService(repo).link_version(vid)
        assert current.same_name_pending[0].pending_id==first.same_name_pending[0].pending_id
    assert repo.list_records('pending_confirmations',version_id=vid)==before


def test_historical_waiting_duplicates_reuse_stable_existing_row_without_deleting_audit(repo,tmp_path):
    vid,u,facts=setup(repo,tmp_path)
    older=repo.create_pending(vid,ev(facts[0]),SAME_NAME_PENDING_REASON)
    newer=repo.create_pending(vid,ev(facts[0]),SAME_NAME_PENDING_REASON)
    before=repo.list_records('pending_confirmations',version_id=vid)
    for _ in range(2):
        result=LinkingService(repo).link_version(vid)
        assert result.same_name_pending[0].pending_id==older['id']
    assert repo.list_records('pending_confirmations',version_id=vid)==before


@pytest.mark.parametrize('source',[Source.CAD_TEXT,Source.PDF_TEXT])
def test_pending_preserves_actual_fact_source_and_full_bbox(repo,tmp_path,source):
    vid,u,facts=setup(repo,tmp_path,source=source)
    first=LinkingService(repo).link_version(vid)
    pending=repo.get('pending_confirmations',first.same_name_pending[0].pending_id)
    assert all(pending[key]==value for key,value in ev(facts[0]).columns().items())
    assert LinkingService(repo).link_version(vid).same_name_pending[0].pending_id==pending['id']


@pytest.mark.parametrize('change',[{'x1':.25},{'y1':.25},{'source':Source.CAD_TEXT.value}])
def test_changed_left_evidence_does_not_reuse_previous_waiting_anchor(repo,tmp_path,change):
    vid,u,facts=setup(repo,tmp_path)
    prior=LinkingService(repo).link_version(vid).same_name_pending[0].pending_id
    for field,value in change.items():
        assert field in {'x1','y1','source'}
        repo._connection.execute(f'UPDATE facts SET {field}=? WHERE id=?',(value,facts[0]['id']))
    next_id=LinkingService(repo).link_version(vid).same_name_pending[0].pending_id
    assert next_id!=prior
    assert repo.get('pending_confirmations',prior)['status']=='待确认'
    assert LinkingService(repo).link_version(vid).same_name_pending[0].pending_id==next_id
    assert len(repo.list_records('pending_confirmations',version_id=vid))==2


def test_answered_question_is_not_a_disposition_of_changed_right_evidence(repo,tmp_path):
    vid,u,facts=setup(repo,tmp_path)
    answered_id=LinkingService(repo).link_version(vid).same_name_pending[0].pending_id
    answered=repo.confirm_pending(answered_id,status='已确认',confirmed_text='基础F1')
    repo._connection.execute('UPDATE facts SET x1=?,y1=? WHERE id=?',(.75,.25,facts[1]['id']))
    new=LinkingService(repo).link_version(vid).same_name_pending[0].pending_id
    assert new!=answered_id
    assert repo.get('pending_confirmations',answered_id)==answered
    assert repo.get('pending_confirmations',new)['status']=='待确认'
    assert LinkingService(repo).link_version(vid).same_name_pending[0].pending_id==new
    assert len(repo.list_records('pending_confirmations',version_id=vid))==2


def test_human_unable_to_determine_is_preserved_not_reopened(repo,tmp_path):
    vid,u,facts=setup(repo,tmp_path)
    pending=LinkingService(repo).link_version(vid).same_name_pending[0].pending_id
    answered=repo.confirm_pending(pending,status='无法确定')
    assert not LinkingService(repo).link_version(vid).same_name_pending
    assert repo.list_records('pending_confirmations',version_id=vid)==[answered]


def test_different_reason_pending_is_not_reused_and_readonly_link_writes_nothing(repo,tmp_path):
    vid,u,facts=setup(repo,tmp_path)
    different=repo.create_pending(vid,ev(facts[0]),'图中说明有歧义，请人工核实')
    LinkingService(repo).link_version(vid,persist=False)
    assert repo.list_records('pending_confirmations',version_id=vid)==[different]
    new=LinkingService(repo).link_version(vid).same_name_pending[0].pending_id
    assert new!=different['id']
    assert len(repo.list_records('pending_confirmations',version_id=vid))==2


def test_changed_right_side_can_reuse_only_an_open_unresolved_group(repo,tmp_path):
    vid,u,facts=setup(repo,tmp_path)
    first=LinkingService(repo).link_version(vid).same_name_pending[0].pending_id
    repo._connection.execute('UPDATE facts SET x1=?,y1=? WHERE id=?',(.75,.25,facts[1]['id']))
    result=LinkingService(repo).link_version(vid)
    assert result.same_name_pending[0].right_bbox=={'x0':.5,'y0':.1,'x1':.75,'y1':.25}
    assert result.same_name_pending[0].pending_id==first
    assert repo.get('pending_confirmations',first)['status']=='待确认'
    assert len(repo.list_records('pending_confirmations',version_id=vid))==1


@pytest.mark.parametrize('source',[Source.CAD_TEXT,Source.PDF_TEXT])
def test_legacy_vision_provenance_is_preserved_but_new_source_is_idempotent(repo,tmp_path,source):
    vid,u,facts=setup(repo,tmp_path,source=source)
    legacy=repo.create_pending(vid,Evidence(u['id'],Source.VISION,facts[0]['text'],ev(facts[0]).bbox),
                               SAME_NAME_PENDING_REASON)
    corrected=LinkingService(repo).link_version(vid).same_name_pending[0].pending_id
    assert corrected!=legacy['id']
    assert repo.get('pending_confirmations',corrected)['source']==source.value
    assert repo.get('pending_confirmations',legacy['id'])==legacy
    before=repo.list_records('pending_confirmations',version_id=vid)
    for _ in range(2):
        assert LinkingService(repo).link_version(vid).same_name_pending[0].pending_id==corrected
    assert repo.list_records('pending_confirmations',version_id=vid)==before
