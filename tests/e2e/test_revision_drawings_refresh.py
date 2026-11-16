"""Revision result inventory follows the persisted version, without a page reload."""
import json
import os

from playwright.sync_api import expect
from pypdf import PdfWriter

from backend.extract.local_sheet import LocalSheetReader
from backend.models.mock import MockModelClient
from backend.models.openai_compat import CallResult
from backend.settings.service import SettingsService
from backend.store import Repository


def test_partial_revision_refreshes_carried_sheets_and_names(imported, configure, monkeypatch, tmp_path):
    page = imported
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    configure()
    old_id = page.locator('body').get_attribute('data-version-id')
    with Repository() as repo:
        old = repo.list_records('drawing_units', version_id=old_id)
        for index, sheet in enumerate(old, 1):
            repo._update('drawing_units', sheet['id'], {
                'drawing_number': f'S-00{index}', 'name': f'原图{index}',
                'metadata_json': json.dumps({**sheet['metadata'], 'extract_status': 'ok',
                                             'title_identity_uncertain': False})})
        project_id = repo.get('drawing_versions', old_id)['project_id']
        task = repo.create_task(old_id)
        repo.update_task(task['id'], status='已完成', checkpoints={}, incomplete=[], estimated_cost_micros=0)

    class ReplacementModel(MockModelClient):
        def chat_completions(self, *, model=None, **kwargs):
            return CallResult(content=json.dumps({'title_blocks': [{
                'drawing_number': 'S-001', 'name': '替换后的图纸',
                'bbox': {'x0': .6, 'y0': .7, 'x1': .9, 'y1': .9}, 'confidence': 1,
            }], 'facts': []}), input_tokens=12, output_tokens=8,
                raw_status=200, model=model or self.model, provider=self.provider,
                latency_ms=1, extras={})

    monkeypatch.setattr(SettingsService, '_make_client', lambda *args: ReplacementModel())
    monkeypatch.setattr(LocalSheetReader, 'read_unit', lambda *args, **kwargs: {})
    replacement = tmp_path / 'replacement.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=120, height=100)
    writer.write(replacement)
    page.get_by_test_id('btn-import').click()
    with page.expect_file_chooser() as chooser:
        page.get_by_role('button', name='补充或替换图纸').click()
    chooser.value.set_files(str(replacement))
    expect(page.locator('body')).to_have_attribute('data-state', 'imported', timeout=15000)
    new_id = page.locator('body').get_attribute('data-version-id')
    page.get_by_test_id('btn-review').click()
    page.get_by_test_id('btn-disclosure-ok').click()
    expect(page.locator('body')).to_have_attribute('data-state', 'results', timeout=30000)

    with Repository() as repo:
        units = repo.list_records('drawing_units', version_id=new_id)
        assert len(units) == 2
        assert {sheet['drawing_number'] for sheet in units} == {'S-001', 'S-002'}
    page.get_by_role('tab', name='图纸', exact=True).click()
    rows = page.get_by_test_id('drawing-view-row')
    expect(rows).to_have_count(2)
    expect(rows.filter(has_text='替换后的图纸')).to_have_count(1)
    carried = rows.filter(has_text='原图2')
    expect(carried).to_have_count(1)
    carried.click()
    expect(page.get_by_test_id('viewer-title')).to_have_text('原图2')
    expect(page.get_by_test_id('viewer-img')).to_be_visible()
    if os.environ.get('DRAWCHECK_QA_SCREENSHOT'):
        page.screenshot(path=os.environ['DRAWCHECK_QA_SCREENSHOT'])
    page.get_by_role('link', name='管理图纸').click()
    expect(page.locator('[data-action="remove-unit"]')).to_have_count(2)
    assert page.locator('body').get_attribute('data-version-id') == new_id
    # Repeated result loading is a replacement, not an append of carried sheets.
    page.reload(wait_until='networkidle')
    expect(page.locator('body')).to_have_attribute('data-state', 'results')
    page.get_by_role('tab', name='图纸', exact=True).click()
    expect(page.get_by_test_id('drawing-view-row')).to_have_count(2)
    # Loading historical results must not silently switch the refreshed inventory to latest.
    page.evaluate('(value) => localStorage.setItem("drawcheck-current", JSON.stringify(value))',
                  {'project_id': project_id, 'version_id': old_id})
    page.reload(wait_until='networkidle')
    expect(page.get_by_test_id('dlg-history-readonly')).to_be_visible()
    page.get_by_role('button', name='查看此版本').click()
    page.get_by_role('tab', name='图纸', exact=True).click()
    expect(page.get_by_test_id('drawing-view-row')).to_have_count(2)
    expect(page.get_by_test_id('drawing-view-row').filter(has_text='原图1')).to_have_count(1)
    expect(page.get_by_test_id('drawing-view-row').filter(has_text='替换后的图纸')).to_have_count(0)
    assert page.locator('body').get_attribute('data-version-id') == old_id
    assert not errors
