"""Visible identifier literals are separate from their explanatory legends."""
import pytest

from backend.rules import RulesBService
from backend.rules.rules_b import _visible_number
from backend.rules.registry import reconcile_rule_results
from backend.store import BBox, Evidence, Source
from tests.backend.rules.test_reference_reliability import seed, unit, add, complete_catalog


def pair(repo,tmp_path,left,right,*,identity=True):
    vid,oid=seed(repo,tmp_path)
    a=unit(repo,vid,oid,'S-001',1); b=unit(repo,vid,oid,'S-002',2)
    complete_catalog(repo,vid,oid,['S-001','S-002'])
    facts=[add(repo,vid,u,text,category='编号写法',object_id='具体位置F1',comparison_key='编号',
               identity_confirmed=identity) for u,text in [(a,left),(b,right)]]
    return vid,facts


@pytest.mark.parametrize('code,description',[
    ('HL05','汇流箱及编号'),('NB09','逆变器及编号'),('1WBT06','光伏升压变及编号')])
@pytest.mark.parametrize('reverse',[False,True])
def test_identical_code_with_legend_description_is_not_writing_conflict(repo,tmp_path,code,description,reverse):
    texts=[f'{code} {description}',code]
    if reverse:
        texts.reverse()
    vid,facts=pair(repo,tmp_path,*texts)
    before=repo.list_records('facts',version_id=vid)
    result=RulesBService(repo).run(vid)
    assert not result.issues and not result.pendings
    readings=result.detail['number_readings']
    assert [r['literal'] for r in readings]==[code,code]
    assert [r['raw_text'] for r in readings]==texts
    assert repo.list_records('facts',version_id=vid)==before


def test_different_explanations_of_same_literal_do_not_prove_object_identity(repo,tmp_path):
    vid,facts=pair(repo,tmp_path,'NB09 图例说明','NB09 图内标注',identity=False)
    assert not RulesBService(repo).run(vid).issues


@pytest.mark.parametrize('left,right',[
    ('XB-03 编号说明','XB-3 实际编号'),
    ('HL05 汇流箱编号','HL06 汇流箱编号'),
    ('1WBT06 光伏升压变编号','1WBT07 光伏升压变编号'),
    ('ABC-S05','ABC-505'),
    ('NC09811-X07','NZ16181-X07'),
    ('HL05','Hl05'),
])
def test_real_literal_difference_retains_raw_bilateral_evidence(repo,tmp_path,left,right):
    vid,facts=pair(repo,tmp_path,left,right)
    result=RulesBService(repo).run(vid)
    assert len(result.issues)==1
    issue=result.issues[0]
    expected=[_visible_number({'text':text})['literal'] for text in (left,right)]
    assert all(expected) and expected[0]!=expected[1]
    assert issue['rule_result']['compared_number_literals']==dict(zip(('left','right'),expected))
    assert issue['rule_result']['left_value']==issue['left_text']==left
    assert issue['rule_result']['right_value']==issue['right_text']==right
    assert issue['left_source']==issue['right_source']==Source.VISION.value
    for side,fact in zip(('left','right'),facts):
        assert issue[f'{side}_unit_id']==fact['unit_id']
        assert all(issue[f'{side}_{key}']==fact[key] for key in ('x0','y0','x1','y1'))


@pytest.mark.parametrize('text',[
    'HL05 / HL06 汇流箱编号','HL05、NB09','HL05~HL09','HL05～09','HL05-09','HL05至HL09',
    'HL05、06','编号1-9','HL 05','仅有中文图例说明','YJV 电缆说明','YJV-3×95',
    'U40X40X2.5','Q235B','DN100','HRB400','HL05 材质Q235B','HL05 规格 40×40',
])
def test_mixed_range_or_material_text_is_pending_not_first_token(repo,tmp_path,text):
    vid,facts=pair(repo,tmp_path,text,'HL06')
    result=RulesBService(repo).run(vid)
    assert not result.issues
    assert any(p['text']==text and '唯一明确编号' in p['reason'] for p in result.pendings)
    reading=next(r for r in result.detail['number_readings'] if r['raw_text']==text)
    assert reading['literal'] is None and reading['reason']


def test_clear_code_with_measurement_unit_cannot_be_used_as_identifier():
    assert _visible_number({'text':'AB12','unit':'mm'})['literal'] is None


def test_reconciliation_removes_old_description_conflict_preserves_manual_audit(repo,tmp_path):
    vid,facts=pair(repo,tmp_path,'HL05 汇流箱及编号','HL05')
    evidences=[Evidence(f['unit_id'],Source(f['source']),f['text'],BBox(*[f[k] for k in ('x0','y0','x1','y1')])) for f in facts]
    old=[]
    for _ in range(2):
        old.append(repo.create_issue(vid,'旧说明后缀误报',*evidences,
            rule_result={'category':'编号写法','left_value':facts[0]['text'],'right_value':facts[1]['text']},
            primary_opinion={'engine':'rules_b'}))
    repo.update_issue(old[1]['id'],review_status='排除',review_note='人工核实编号一致')
    result=reconcile_rule_results(repo,version_id=vid)
    assert result['removed_suspected']==1 and result['manual_preserved']==1
    remaining=repo.list_records('issues',version_id=vid)
    assert len(remaining)==1 and remaining[0]['id']==old[1]['id']
    assert remaining[0]['review_status']=='排除' and remaining[0]['review_note'].startswith('人工核实编号一致')
    assert remaining[0]['left_text']==facts[0]['text'] and remaining[0]['right_text']==facts[1]['text']
    assert len(repo.list_records('facts',version_id=vid))==2
