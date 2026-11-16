"""Normal saved SiliconFlow reviews use the same bounded streaming transport as a retry."""
import pytest

from backend.extract import ExtractResult
from backend.jobs.plugins import ExtractPlugin, StepContext


@pytest.mark.parametrize('provider, expected', [('siliconflow', True), ('qwen', False), ('custom', False)])
def test_normal_extract_uses_provider_supported_streaming(repo, drawing, monkeypatch, provider, expected):
    options = []

    class Extract:
        def __init__(self, store, **kwargs):
            options.append(kwargs)
        def extract_unit(self, uid, **kwargs):
            return ExtractResult(unit_id=uid, version_id=drawing['version']['id'], status='ok', prompt_version='test')

    class LocalReader:
        def __init__(self, store):
            pass
        def read_unit(self, uid, **kwargs):
            return None

    monkeypatch.setattr('backend.extract.ExtractService', Extract)
    monkeypatch.setattr('backend.extract.local_sheet.LocalSheetReader', LocalReader)
    monkeypatch.setattr('backend.catalog.CatalogService.apply_extract_result', lambda *args, **kwargs: None)
    ctx = StepContext(store=repo, task_id='test', version_id=drawing['version']['id'], model_status='single',
        settings={}, checkpoints={}, incomplete=[], primary_model={'provider': provider, 'model': 'test-model'},
        review_model=None, allow_model_call=lambda: True)
    result = ExtractPlugin().run(ctx)
    assert result.state == 'done' and len(options) == 1
    assert options[0]['stream'] is expected
    assert options[0]['use_mock'] is False
    assert not repo.list_records('model_calls', version_id=drawing['version']['id'])
