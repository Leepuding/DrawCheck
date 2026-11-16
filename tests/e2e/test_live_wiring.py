"""前端任务与结果接线：显式 HTTP 替身，独立于模型输出。"""
from playwright.sync_api import expect


def test_task_polling_never_seeds_and_failure_is_explicit(imported, configure):
    page = imported
    configure()
    seeds = []
    polls = []
    page.route('**/seed-mock', lambda route: (seeds.append(route.request.url), route.abort()))
    page.route('**/api/versions/*/tasks', lambda route: route.fulfill(json={
        'task_id': 'task-ui', 'status': '进行中', 'percent': 12, 'message': '正在审查图纸，请稍候'}))
    def poll(route):
        polls.append(True)
        route.fulfill(json={'task_id': 'task-ui', 'status': '失败', 'percent': 30,
                            'message': '审查未完成', 'incomplete_count': 1})
    page.route('**/api/tasks/task-ui', poll)
    page.get_by_test_id('btn-review').click()
    page.get_by_test_id('btn-disclosure-ok').click()
    expect(page.get_by_test_id('results-title')).to_have_text('审查未完成', timeout=10000)
    expect(page.get_by_test_id('empty-issues')).to_contain_text('尚未形成有效对照')
    expect(page.get_by_test_id('incomplete-panel').locator('summary')).to_have_text('未核验')
    assert polls and not seeds
    assert '未发现不一致' not in page.locator('body').inner_text()


def test_review_request_failure_does_not_show_empty_success(imported, configure):
    page = imported
    configure()
    page.route('**/api/versions/*/tasks', lambda route: route.fulfill(
        status=500, json={'error': {'code': 'OTHER', 'message': '启动失败'}}))
    page.get_by_test_id('btn-review').click()
    page.get_by_test_id('btn-disclosure-ok').click()
    expect(page.locator('body')).to_have_attribute('data-state', 'imported')
    expect(page.get_by_test_id('toast-block')).to_contain_text('启动失败')
    assert page.get_by_test_id('empty-issues').count() == 0


def test_export_and_import_choices(imported, configure, screenshot):
    page = imported
    configure()
    page.route('**/api/versions/*/tasks', lambda route: route.fulfill(json={
        'task_id': 'done-ui', 'status': '已完成', 'percent': 100, 'incomplete_count': 0, 'unreadable_count': 0, 'out_of_scope_count': 0}))
    page.route('**/api/issues?*', lambda route: route.fulfill(json={
        'items': [], 'incomplete_count': 0, 'unreadable_count': 0, 'out_of_scope_count': 0,
        'recognized_count': 2, 'drawing_count': 2, 'cost_paused': False, 'revised': False}))
    page.route('**/api/pending?*', lambda route: route.fulfill(json={
        'items': [], 'pending_count': 1, 'incomplete_pending_count': 0}))
    page.route('**/api/versions/*/exports', lambda route: route.fulfill(json={
        'files': [{'id': 'pdf', 'name': '审查.pdf', 'url': '/api/exports/pdf/download'},
                  {'id': 'xls', 'name': '清单.xlsx', 'url': '/api/exports/xls/download'}],
        'pending_count': 1}))
    page.get_by_test_id('btn-review').click()
    page.get_by_test_id('btn-disclosure-ok').click()
    expect(page.get_by_test_id('results-title')).to_have_text('审查完成', timeout=10000)
    page.get_by_test_id('btn-export').click()
    expect(page.get_by_test_id('dlg-export-warning')).to_contain_text('1 项待确认')
    page.get_by_role('button', name='继续导出').click()
    dialog = page.get_by_test_id('dlg-export')
    expect(dialog).to_contain_text('审查.pdf')
    expect(dialog).to_contain_text('清单.xlsx')
    screenshot(page, 'dc-live-export.png')
    page.get_by_role('button', name='关闭').click()
    page.get_by_test_id('btn-import').click()
    expect(page.get_by_test_id('dlg-import-mode')).to_contain_text('补充或替换图纸')
    expect(page.get_by_test_id('dlg-import-mode')).to_contain_text('新项目')


def test_real_backend_review_export_revision_and_restore(imported, configure, monkeypatch, tmp_path, pdf_files, screenshot):
    """真实本地 HTTP、任务、SQLite 和文件导出；仅模型使用结构化替身。"""
    import json
    from backend.models.mock import MockModelClient
    from backend.models.openai_compat import CallResult
    from backend.settings.service import SettingsService

    class StructuredModel(MockModelClient):
        def chat_completions(self, *, model=None, messages=None, **kwargs):
            return CallResult(content=json.dumps({'prompt_version': 'dc12-extract-v2',
                'title_blocks': [], 'facts': []}), input_tokens=12, output_tokens=8,
                raw_status=200, model=model or self.model, provider=self.provider,
                latency_ms=1, extras={})

    monkeypatch.setattr(SettingsService, '_make_client', lambda self, slot, secret: StructuredModel())
    monkeypatch.setenv('DRAWCHECK_EXPORT_DIR', str(tmp_path / 'exports'))
    page = imported
    configure()
    page.get_by_test_id('btn-review').click()
    page.get_by_test_id('btn-disclosure-ok').click()
    expect(page.locator('body')).to_have_attribute('data-state', 'results', timeout=30000)
    expect(page.get_by_test_id('results-title')).to_have_text('审查部分完成')
    page.get_by_test_id('btn-export').click()
    expect(page.get_by_test_id('dlg-export')).to_be_visible(timeout=30000)
    files = list((tmp_path / 'exports').rglob('*'))
    assert {f.suffix for f in files if f.is_file()} == {'.pdf', '.xlsx'}
    screenshot(page, 'dc-live-real-export.png')
    page.get_by_role('button', name='关闭').click()
    page.get_by_test_id('btn-import').click()
    with page.expect_file_chooser() as chooser:
        page.get_by_role('button', name='补充或替换图纸').click()
    chooser.value.set_files([pdf_files[0]])
    expect(page.locator('body')).to_have_attribute('data-state', 'imported', timeout=30000)
    expect(page.get_by_test_id('revision-import-hint')).to_contain_text('未替换的图纸将保留')
    revision_id = page.locator('body').get_attribute('data-version-id')
    assert revision_id
    page.get_by_test_id('btn-review').click()
    page.get_by_test_id('btn-disclosure-ok').click()
    expect(page.get_by_test_id('results-title')).to_have_text('审查部分完成', timeout=30000)
    assert '对比上一版：' in page.locator('body').inner_text()
    page.reload(wait_until='networkidle')
    expect(page.locator('body')).to_have_attribute('data-version-id', revision_id, timeout=10000)
    expect(page.get_by_test_id('results-title')).to_have_text('审查部分完成')
    page.get_by_test_id('link-history').click()
    expect(page.get_by_test_id('dlg-history').locator('li')).to_have_count(2)
    screenshot(page, 'dc-live-real-history.png')
