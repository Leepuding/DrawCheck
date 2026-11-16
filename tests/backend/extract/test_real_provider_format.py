import json
import pytest
from backend.extract.schema import parse_model_json
from backend.extract.errors import ExtractError


def test_glm_final_answer_exact_box_envelope():
    data={'title_blocks':[], 'facts':[{'text':'<|end_of_box|> literal evidence'}]}
    assert parse_model_json('\n<|begin_of_box|>'+json.dumps(data)+'<|end_of_box|>\n')==data


@pytest.mark.parametrize('value',[
 '<|begin_of_box|>{"facts":[]}',
 'model explanation <|begin_of_box|>{"facts":[]}<|end_of_box|>',
 '<think>{"facts":[]}</think>',
 '<|begin_of_box|>{"facts":[]}<|end_of_box|>trailing explanation',
])
def test_do_not_salvage_unbalanced_or_embedded_json(value):
    with pytest.raises(ExtractError):parse_model_json(value)


def test_complete_json_with_length_finish_is_not_accepted_or_retried(tmp_path):
    from PIL import Image
    from backend.store import Repository
    from backend.extract import ExtractService
    from backend.models.openai_compat import CallResult
    with Repository(tmp_path/'data') as repo:
        project=repo.create_project('截断');version=repo.create_version(project['id'],'v1')
        source=tmp_path/'source.pdf';source.write_bytes(b'%PDF synthetic')
        original=repo.import_original(version['id'],source)['record']
        Image.new('RGB',(100,100),'white').save(repo.root/'image.png')
        unit=repo.create_unit(version['id'],original['id'],kind='pdf_page',ordinal=1,name='截断图',metadata={'review_relative_path':'image.png'})
        task=repo.create_task(version['id'])
        class Client:
            calls=0
            def chat_completions(self,**kwargs):
                self.calls+=1
                return CallResult(content='{"title_blocks":[],"facts":[]}',input_tokens=10,output_tokens=8192,
                    raw_status=200,model='test',provider='test',latency_ms=1,finish_reason='length')
        client=Client()
        result=ExtractService(repo,use_mock=False,model_client=client,task_id=task['id']).extract_unit(unit['id'])
        assert result.status=='incomplete' and client.calls==1
        assert '长度上限' in result.incomplete_reason
        assert repo.list_records('facts',version_id=version['id'])==[]
        calls=repo.list_records('model_calls',version_id=version['id'])
        assert len(calls)==1 and calls[0]['status']=='truncated' and calls[0]['output_tokens']==8192
