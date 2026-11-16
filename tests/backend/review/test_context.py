import base64,copy,io,json,math
import pytest
from PIL import Image
from backend.review import ReviewService
from backend.review.service import PROMPT_VERSION
from tests.backend.rules.conftest import data_dir,repo,_block_network
from tests.backend.review.test_review import Client,drawing,svc


def anchor(text,x0=.55,y0=.22,x1=.75,y1=.26,**extra):
    return {'text':text,'source':'PDF文字','bbox':dict(x0=x0,y0=y0,x1=x1,y1=y1),**extra}


@pytest.fixture
def pair(repo,drawing):
    version,left,old,task=drawing
    Image.new('RGB',(800,600),'white').save(repo.root/'cache/right.png')
    right=repo.create_unit(version['id'],left['original_id'],kind='pdf_page',ordinal=2,name='右图',metadata={'review_relative_path':'cache/right.png'})
    rr={'category':'目录与图签不符','left_value':'AB-D0101-09','right_value':'AB-D0101-10',
        'basis':'同一源文件中唯一图名配对，图号逐字不同','object_label':'监控材料表',
        'match_basis':'unique_literal_name_in_source','catalog_name_evidence':anchor('监控材料表'),
        'title_name_evidence':anchor('监控材料表',.65,.6,.85,.64)}
    repo._update('issues',old['id'],{'right_unit_id':right['id'],'rule_result_json':json.dumps(rr)})
    return version,left,right,repo.get('issues',old['id']),task


def service(repo,pair,client):
    return ReviewService(repo,task_id=pair[4]['id'],primary_model={'provider':'custom','model':'one'},review_model={'provider':'custom','model':'two'},client=client)


def capture_crops(monkeypatch):
    boxes=[];original=Image.Image.crop
    def crop(image,box):boxes.append({'size':image.size,'box':box});return original(image,box)
    monkeypatch.setattr(Image.Image,'crop',crop)
    return boxes


def test_paired_name_is_fully_included_on_its_side_without_borrowing_other_side(repo,pair,monkeypatch):
    boxes=capture_crops(monkeypatch);client=Client('disagree');result=service(repo,pair,client).run(pair[0]['id'])
    assert result.reviewed==1 and len(client.calls)==1
    left,right=boxes
    assert left['box'][2]>=math.ceil(.75*800) and left['box'][3]>=math.ceil(.26*600)
    assert left['box'][3]<.6*600  # Right-side title is not added to the left crop.
    assert (left['box'][2]-left['box'][0])*(left['box'][3]-left['box'][1])<800*600
    payload=json.loads(client.calls[0]['messages'][1]['content'][0]['text'])
    assert payload['association_context']['left'][0]['text']=='监控材料表'
    assert payload['association_context']['left'][0]['unit_id']==pair[1]['id']
    assert payload['association_context']['right'][0]['unit_id']==pair[2]['id']
    assert payload['rule']['object_label']=='监控材料表'
    prompt=client.calls[0]['messages'][0]['content']
    assert '不表示两图数值相同' in prompt and '差异事实' in prompt
    saved=repo.get('issues',pair[3]['id'])
    assert saved['review_opinion']['verdict']=='disagree' and saved['review_status']=='疑似'
    assert len(repo.list_records('model_calls',version_id=pair[0]['id']))==1


@pytest.mark.parametrize('fault',['missing','zero','outside','nan','no_text','no_source','other_unit','other_side','other_version'])
def test_required_paired_name_missing_invalid_or_cross_side_is_incomplete_with_zero_requests(repo,pair,fault):
    rr=copy.deepcopy(pair[3]['rule_result']);value=rr['catalog_name_evidence']
    if fault=='missing':rr.pop('catalog_name_evidence')
    elif fault=='zero':value['bbox']['x1']=value['bbox']['x0']
    elif fault=='outside':value['bbox']['x1']=1.1
    elif fault=='nan':value['bbox']['y0']=float('nan')
    elif fault=='no_text':value['text']=''
    elif fault=='no_source':value.pop('source')
    elif fault=='other_unit':value['unit_id']=pair[2]['id']
    elif fault=='other_side':value['side']='right'
    elif fault=='other_version':value['version_id']='different-version'
    if fault=='nan':
        client=Client()
        with pytest.raises(ValueError,match='有效原图位置'):
            service(repo,pair,client)._review_context({**pair[3],'rule_result':rr})
        assert not client.calls
        return
    repo._update('issues',pair[3]['id'],{'rule_result_json':json.dumps(rr)})
    client=Client();result=service(repo,pair,client).run(pair[0]['id'])
    assert result.incomplete and result.model_calls==0 and client.calls==[]
    assert repo.get('issues',pair[3]['id'])['review_opinion']['status']=='incomplete'
    assert not repo.list_records('model_calls',version_id=pair[0]['id'])


def test_generic_opt_in_context_requires_explicit_same_unit_and_does_not_invent_claim_evidence(repo,drawing):
    client=Client();normal=svc(repo,drawing,client);normal.run(drawing[0]['id'])
    payload=json.loads(client.calls[0]['messages'][1]['content'][0]['text'])
    assert payload['association_context']=={'left':[],'right':[]}
    issue=repo.get('issues',drawing[2]['id']);rr=issue['rule_result']
    rr['review_context']={'left':[anchor('provided but unbound')]}
    repo._update('issues',issue['id'],{'rule_result_json':json.dumps(rr)})
    assert normal.run(drawing[0]['id']).incomplete and len(client.calls)==1


@pytest.mark.parametrize('fault',[None,'missing_left','other_unit'])
def test_cable_range_uses_provided_left_right_cabinet_and_headers(repo,pair,monkeypatch,fault):
    rr={'category':'接线','match_basis':'cad_cable_range_field','object_label':'柜01',
        'left_value':'A01-A16','right_value':'A01-A18','basis':'同柜字段范围原文字面不同',
        'endpoint_evidence':{'left':[anchor('柜01',.1,.55,.16,.75)],
                             'right':[anchor('柜01',.65,.1,.72,.14),anchor('开关柜编号',.1,.1,.25,.14),anchor('电缆编号',.1,.2,.25,.24)]}}
    if fault=='missing_left':rr['endpoint_evidence']['left']=[]
    elif fault=='other_unit':rr['endpoint_evidence']['left'][0]['unit_id']=pair[2]['id']
    repo._update('issues',pair[3]['id'],{'rule_result_json':json.dumps(rr)})
    boxes=capture_crops(monkeypatch);client=Client();result=service(repo,pair,client).run(pair[0]['id'])
    if fault:
        assert result.incomplete and not client.calls
    else:
        assert result.reviewed==1
        assert boxes[0]['box'][3]>=.75*600
        payload=json.loads(client.calls[0]['messages'][1]['content'][0]['text'])
        assert [item['text'] for item in payload['association_context']['right']]==['柜01','开关柜编号','电缆编号']
        assert all(item['unit_id']==pair[2]['id'] for item in payload['association_context']['right'])


def test_changed_context_text_position_and_prompt_each_invalidate_review_cache(repo,pair,monkeypatch):
    import sys
    client=Client();review=service(repo,pair,client)
    assert review.run(pair[0]['id']).model_calls==1
    assert review.run(pair[0]['id']).cache_hits==1
    rr=copy.deepcopy(pair[3]['rule_result']);rr['catalog_name_evidence']['text']='监控材料清单'
    repo._update('issues',pair[3]['id'],{'rule_result_json':json.dumps(rr)})
    assert review.run(pair[0]['id']).model_calls==1
    rr['catalog_name_evidence']['bbox']['x1']=.8
    repo._update('issues',pair[3]['id'],{'rule_result_json':json.dumps(rr)})
    assert review.run(pair[0]['id']).model_calls==1
    module=sys.modules[review.__class__.__module__]
    monkeypatch.setattr(module,'PROMPT_VERSION','different-prompt-version')
    assert review.run(pair[0]['id']).model_calls==1
    assert len(client.calls)==4 and len(repo.list_records('model_calls',version_id=pair[0]['id']))==4
