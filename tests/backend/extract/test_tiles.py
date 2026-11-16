import json
from copy import deepcopy

import pytest
from PIL import Image

from backend.extract import ExtractService
from backend.extract.tiles import plan_regions, map_fact, merge_facts, STRATEGY
from backend.models.errors import ModelError
from backend.models.openai_compat import CallResult
from backend.store import Repository


def fact(text='100', object_id='F1', bbox=None):
    return {'text': text, 'numeric_value': text, 'category': '尺寸', 'unit': 'mm',
            'object_id': object_id, 'comparison_key': '长度', 'identity_confirmed': True,
            'bbox': bbox or {'x0': .4, 'y0': .1, 'x1': .5, 'y1': .2}, 'confidence': .9, 'unclear': False}


def test_plan_covers_edges_and_mapping():
    assert plan_regions(2560, 1000) == []
    regions = plan_regions(8192, 4096)
    assert regions[0][:2] == (0, 0)
    assert regions[-1][2:] == (8192, 4096)
    for y in range(0, 4096, 128):
        for x in range(0, 8192, 128):
            assert any(a <= x < c and b <= y < d for a,b,c,d in regions)
    row = map_fact(fact(bbox={'x0':0,'y0':0,'x1':1,'y1':1}), (1000,500,2000,1500), (4000,2000), 'a')
    assert row['bbox'] == {'x0':.25,'y0':.25,'x1':.5,'y1':.75}


def test_merge_only_overlapping_matching_evidence_and_keep_conflicts():
    a = fact(); a['region_ids'] = ['a']
    b = deepcopy(a); b['region_ids'] = ['b']
    assert len(merge_facts([a,b])) == 1
    b['object_id'] = 'F2'
    assert len(merge_facts([a,b])) == 2
    b['object_id'] = 'F1'; b['text'] = b['numeric_value'] = '101'
    rows = merge_facts([a,b])
    assert len(rows) == 2 and all(r['unclear'] and r['region_conflict'] for r in rows)
    b = deepcopy(a); b['region_ids'] = ['b']; b['bbox'] = {'x0':.7,'y0':.1,'x1':.8,'y1':.2}
    assert len(merge_facts([a,b])) == 2


class Client:
    def __init__(self, fail_at=None):
        self.calls = 0; self.fail_at = fail_at; self.instructions = []
    def chat_completions(self, **kwargs):
        self.calls += 1
        self.instructions.append(kwargs['messages'][0]['content'])
        if self.calls == self.fail_at:
            raise ModelError('TIMEOUT', '受控失败')
        overview = '本次是整张图纸概览' in self.instructions[-1]
        data = {'title_blocks': [{'drawing_number':'S-001' if overview else 'WRONG-TILE', 'name':'基础',
                'bbox': {'x0':.8,'y0':.8,'x1':1,'y1':1}, 'confidence':.9}],
                'facts': [fact()]}
        return CallResult(content=json.dumps(data), input_tokens=10, output_tokens=5, raw_status=200, model='test', provider='test', latency_ms=1)


@pytest.fixture
def case(tmp_path):
    with Repository(tmp_path/'data') as store:
        project = store.create_project('大图')
        version = store.create_version(project['id'], 'v1')
        original = tmp_path/'original.pdf'; original.write_bytes(b'%PDF-original-immutable')
        record = store.import_original(version['id'], original)['record']
        image = store.root/'review.png'; Image.new('RGB',(3000,1200),'white').save(image)
        unit = store.create_unit(version['id'],record['id'],kind='pdf_page',ordinal=1,name='大图',metadata={'review_relative_path':'review.png'})
        task = store.create_task(version['id'])
        yield store, version, unit, task


def test_large_image_aggregate_cache_billing_and_progress(case):
    store,version,unit,task = case
    client = Client(); updates=[]
    service = ExtractService(store,use_mock=False,model_client=client,task_id=task['id'])
    service.region_progress_callback = lambda done,total: updates.append((done,total))
    result = service.extract_unit(unit['id'])
    assert result.status == 'ok' and result.model_calls == 3
    assert result.title_blocks[0]['drawing_number'] == 'S-001'
    assert len(result.facts) == 2  # overview detail facts deliberately ignored
    assert updates == [(0,3),(1,3),(2,3),(3,3)]
    assert '当前输入图片内' in client.instructions[-1]
    assert len(store.list_records('model_calls',version_id=version['id'])) == 3
    metadata=store.get('drawing_units',unit['id'])['metadata']
    assert metadata['extract_coverage']['completed_regions'] == 3 and metadata['extract_strategy'] == STRATEGY
    again=service.extract_unit(unit['id'])
    assert again.cache_hit and again.model_calls == 0 and client.calls == 3
    assert len(store.list_records('facts',version_id=version['id'])) == 2
    assert store.verify_originals(version['id'])


def test_failed_region_has_no_partial_facts_and_resume_only_calls_missing(case):
    store,version,unit,task = case
    client=Client(fail_at=3)
    service=ExtractService(store,use_mock=False,model_client=client,task_id=task['id'],format_retries=0)
    first=service.extract_unit(unit['id'])
    assert first.status == 'incomplete' and first.model_calls == 3
    assert store.list_records('facts',version_id=version['id']) == []
    coverage=store.get('drawing_units',unit['id'])['metadata']['extract_coverage']
    assert coverage['completed_regions']==2 and coverage['incomplete_region_ids']==['region-2']
    second=service.extract_unit(unit['id'])
    assert second.status=='ok' and second.model_calls==1 and client.calls==4
    assert len(store.list_records('model_calls',version_id=version['id']))==4
    assert 'extract_incomplete_reason' not in store.get('drawing_units',unit['id'])['metadata']


def test_budget_stops_before_region_then_reuses_completed_overview(case):
    store,version,unit,task=case
    client=Client()
    service=ExtractService(store,use_mock=False,model_client=client,task_id=task['id'],allow_model_call=lambda:client.calls<1)
    first=service.extract_unit(unit['id'])
    assert first.cost_paused and first.status=='incomplete' and client.calls==1
    service.allow_model_call=lambda:True
    second=service.extract_unit(unit['id'])
    assert second.status=='ok' and second.model_calls==2 and client.calls==3


def test_invalid_image_is_incomplete_without_model_call(case):
    store,version,unit,task=case
    (store.root/'review.png').write_bytes(b'bad image')
    client=Client()
    result=ExtractService(store,use_mock=False,model_client=client).extract_unit(unit['id'])
    assert result.status=='incomplete' and client.calls==0


def test_removal_copy_reuses_aggregate_without_duplicate_facts(case):
    from backend.drawings.service import DrawingRemovalService
    store,version,unit,task=case
    other=store.create_unit(version['id'],unit['original_id'],kind='pdf_page',ordinal=2,name='误导入')
    client=Client()
    first=ExtractService(store,use_mock=False,model_client=client,task_id=task['id']).extract_unit(unit['id'])
    assert first.status=='ok'
    store.update_task(task['id'],status='已完成',checkpoints={},incomplete=[],estimated_cost_micros=0)
    adjusted=DrawingRemovalService(store).remove(version['id'],unit_ids=[other['id']])
    new_id=adjusted['version_id']; copied=store.list_records('drawing_units',version_id=new_id)[0]
    count=len(store.list_records('facts',version_id=new_id))
    result=ExtractService(store,use_mock=False,model_client=client).extract_unit(copied['id'])
    assert result.cache_hit and result.model_calls==0 and client.calls==3
    assert len(store.list_records('facts',version_id=new_id))==count==2


def test_equivalent_numeric_representations_are_not_conflicts():
    a=fact(); a['region_ids']=['a']; a['numeric_value']=100
    b=deepcopy(a); b['region_ids']=['b']; b['numeric_value']='100.0'
    result=merge_facts([a,b])
    assert len(result)==1 and not result[0]['unclear']


def test_unclear_overview_uses_readable_local_title(case):
    store,version,unit,task=case
    class LocalTitleClient(Client):
        def chat_completions(self,**kwargs):
            result=super().chat_completions(**kwargs)
            data=json.loads(result.content)
            overview='本次是整张图纸概览' in kwargs['messages'][0]['content']
            if overview:
                data['title_blocks'][0].update(drawing_number='',unclear=True,confidence=.2)
            else:
                data['title_blocks'][0]['drawing_number']='S-LOCAL'
            result.content=json.dumps(data)
            return result
    result=ExtractService(store,use_mock=False,model_client=LocalTitleClient()).extract_unit(unit['id'])
    assert result.status=='ok' and result.title_blocks[0]['drawing_number']=='S-LOCAL'


def test_blank_title_objects_do_not_count_as_identified_sheet(case):
    store,version,unit,task=case
    class BlankTitleClient(Client):
        def chat_completions(self,**kwargs):
            result=super().chat_completions(**kwargs)
            data=json.loads(result.content)
            data['title_blocks'][0].update(drawing_number='',name='',confidence=.9,unclear=False)
            result.content=json.dumps(data)
            return result
    result=ExtractService(store,use_mock=False,model_client=BlankTitleClient()).extract_unit(unit['id'])
    assert result.status=='incomplete'
    assert store.list_records('facts',version_id=version['id'])==[]
