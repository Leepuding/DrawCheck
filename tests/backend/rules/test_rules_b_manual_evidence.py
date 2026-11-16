"""A manual disposition belongs to both complete evidence snapshots."""
import json

import pytest

from backend.rules import RulesBService
from backend.rules.registry import reconcile_rule_results
from backend.store import BBox, Evidence, Source
from tests.backend.rules.test_reference_reliability import seed, unit, add, complete_catalog


def case(repo,tmp_path,status):
    vid,oid=seed(repo,tmp_path)
    units=[unit(repo,vid,oid,f'S-00{i}',i) for i in (1,2,3)]
    complete_catalog(repo,vid,oid,[u['drawing_number'] for u in units])
    facts=[add(repo,vid,u,text,category='材料规格',object_id='B1',comparison_key='规格',identity_confirmed=True)
           for u,text in zip(units,['U40X60X2.5','U40X40X2.5'])]
    issue=RulesBService(repo).run(vid).issues[0]
    old=repo.update_issue(issue['id'],review_status=status,review_note='人工核对原来的这两处标注')
    return vid,units,facts,old


def replace_fact(repo,vid,fact,changes,*,new_id=False):
    metadata=repo.get('drawing_units',fact['unit_id'])['metadata']
    annotation=metadata['fact_annotations'][fact['id']]
    current={**fact,**changes}
    if new_id:
        repo._connection.execute('DELETE FROM facts WHERE id=?',(fact['id'],))
        new=repo.add_fact(vid,Evidence(current['unit_id'],Source(current['source']),current['text'],
            BBox(*[current[k] for k in ('x0','y0','x1','y1')])))
        current=new
    else:
        # All changed columns/values are explicit valid test evidence, with the
        # same stable fact ID to exercise the old dedupe shortcut as well.
        for column,value in changes.items():
            assert column in {'unit_id','source','text','x0','y0','x1','y1'}
            repo._connection.execute(f'UPDATE facts SET {column}=? WHERE id=?',(value,fact['id']))
    destination=repo.get('drawing_units',current['unit_id'])['metadata']
    destination.setdefault('fact_annotations',{})[current['id']]=annotation
    repo._update('drawing_units',current['unit_id'],{'metadata_json':json.dumps(destination)})
    return current


def changes_for(kind,fact,units):
    return {
        'unit':{'unit_id':units[2]['id']},
        'bbox_origin':{'x0':fact['x0']+.02,'y0':fact['y0']+.002},
        'bbox_extent':{'x1':fact['x1']+.02,'y1':fact['y1']+.002},
        'source':{'source':Source.CAD_TEXT.value},
        'text':{'text':fact['text']+' 热镀锌'},
    }[kind]


@pytest.mark.parametrize('status',['已确认','排除'])
@pytest.mark.parametrize('side',[0,1],ids=['left','right'])
@pytest.mark.parametrize('kind',['unit','bbox_origin','bbox_extent','source','text'])
def test_changed_complete_evidence_never_inherits_manual_disposition(repo,tmp_path,status,side,kind):
    vid,units,facts,old=case(repo,tmp_path,status)
    replace_fact(repo,vid,facts[side],changes_for(kind,facts[side],units))
    current=RulesBService(repo).run(vid).issues
    assert len(current)==1 and current[0]['id']!=old['id']
    assert current[0]['review_status']=='疑似'
    assert current[0]['review_note']!=old['review_note']
    assert not repo.get('issues',old['id'])['rule_result'].get('evidence_changed_after_confirmation')
    # Existing reconciliation must preserve the old audit and request a human
    # recheck; the old manual record must stay outside this round's active IDs.
    result=reconcile_rule_results(repo,version_id=vid)
    manual=repo.get('issues',old['id'])
    assert result['manual_preserved']==1
    assert any(row.get('issue_id')==old['id'] for row in result['incomplete'])
    assert manual['review_status']==status
    assert manual['review_note'].startswith(old['review_note'])
    assert manual['rule_result']['evidence_changed_after_confirmation']
    assert all(manual[key]==value for key,value in old.items() if key.startswith(('left_','right_')))
    assert len(repo.list_records('issues',version_id=vid))==2


@pytest.mark.parametrize('status',['已确认','排除'])
@pytest.mark.parametrize('kind',['unit','bbox_extent','source'])
def test_new_right_fact_same_values_cannot_reuse_left_anchor_only(repo,tmp_path,status,kind):
    vid,units,facts,old=case(repo,tmp_path,status)
    replacement=replace_fact(repo,vid,facts[1],changes_for(kind,facts[1],units),new_id=True)
    result=RulesBService(repo).run(vid)
    assert replacement['text']==facts[1]['text']
    assert len(result.issues)==1 and result.issues[0]['id']!=old['id']
    assert result.issues[0]['review_status']=='疑似'
    checked=reconcile_rule_results(repo,version_id=vid)
    assert checked['manual_preserved']==1
    assert repo.get('issues',old['id'])['review_status']==status
    assert repo.get('issues',old['id'])['rule_result']['evidence_changed_after_confirmation']


@pytest.mark.parametrize('status',['已确认','排除'])
@pytest.mark.parametrize('new_ids',[False,True])
def test_identical_bilateral_evidence_retains_manual_record_without_duplicate(repo,tmp_path,status,new_ids):
    vid,units,facts,old=case(repo,tmp_path,status)
    if new_ids:
        for fact in facts:
            replace_fact(repo,vid,fact,{},new_id=True)
    for _ in range(2):
        current=RulesBService(repo).run(vid)
        assert [issue['id'] for issue in current.issues]==[old['id']]
        assert current.issues[0]['review_status']==status
    checked=reconcile_rule_results(repo,version_id=vid)
    assert checked['manual_preserved']==0
    assert not [row for row in checked['incomplete'] if row.get('issue_id')==old['id']]
    retained=repo.get('issues',old['id'])
    assert retained['review_note']==old['review_note']
    assert not retained['rule_result'].get('evidence_changed_after_confirmation')
    assert len(repo.list_records('issues',version_id=vid))==1
