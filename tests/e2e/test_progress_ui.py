"""受控进度响应：验证上传/阶段展示和失败恢复，不调用真实模型。"""
from playwright.sync_api import expect


def test_upload_processing_and_failed_import_restores_view(page, pdf_files):
    page.evaluate('''() => {
      const originalFetch = window.fetch;
      window.fetch = (url, options) => String(url).includes('/api/import/job-progress')
        ? new Promise(resolve => setTimeout(() => resolve(new Response(JSON.stringify({status:'failed',phase:'导入中断',error:{message:'测试导入失败'}}), {status:200,headers:{'Content-Type':'application/json'}})), 1400))
        : originalFetch(url, options);
      window.XMLHttpRequest = class {
        constructor() { this.upload = {}; this.status = 202; this.response = {job_id:'job-progress'}; }
        open() {}
        send() {
          setTimeout(() => this.upload.onprogress({lengthComputable:true,loaded:25,total:100}), 30);
          setTimeout(() => { this.upload.onload(); this.onload(); }, 1000);
        }
      };
    }''')
    with page.expect_file_chooser() as chooser:
        page.get_by_test_id('btn-import').click()
    chooser.value.set_files([pdf_files[0]])
    expect(page.get_by_test_id('progress-title')).to_have_text('正在上传图纸到本机')
    expect(page.get_by_test_id('progress')).to_have_attribute('aria-valuenow', '25')
    expect(page.get_by_test_id('progress-percent')).to_have_text('上传 25%')
    expect(page.get_by_test_id('btn-import')).to_be_disabled()
    expect(page.get_by_test_id('btn-settings')).to_be_disabled()
    expect(page.get_by_test_id('progress-title')).to_have_text('正在准备处理图纸', timeout=5000)
    expect(page.get_by_test_id('progress-eta')).to_contain_text('正在估算')
    expect(page.locator('body')).to_have_attribute('data-state', 'idle', timeout=5000)
    expect(page.get_by_test_id('toast-block')).to_contain_text('测试导入失败')
    expect(page.get_by_test_id('btn-import')).to_be_enabled()


def test_review_displays_phase_counts_and_phase_eta(imported, configure, screenshot):
    page = imported
    configure()
    page.evaluate('''() => {
      const originalFetch = window.fetch;
      window.fetch = (url, options) => {
        if (String(url).includes('/api/versions/') && String(url).endsWith('/tasks'))
          return Promise.resolve(new Response(JSON.stringify({task_id:'ui-stage',status:'进行中',phase:'图纸识别中',phase_key:'recognizing',region_processed_count:2,region_total_count:5,region_percent:40,percent:20,phase_percent:50,processed_count:2,total_count:4,progress_unit:'张图',elapsed_seconds:65,eta_seconds:30,eta_scope:'phase',estimate_basis:'根据已处理图纸估算'}), {status:200,headers:{'Content-Type':'application/json'}}));
        if (String(url).endsWith('/api/tasks/ui-stage'))
          return new Promise(resolve => setTimeout(() => resolve(new Response(JSON.stringify({task_id:'ui-stage',status:'失败',percent:20,incomplete_count:1}), {status:200,headers:{'Content-Type':'application/json'}})), 2000));
        return originalFetch(url, options);
      };
    }''')
    page.get_by_test_id('btn-review').click()
    page.get_by_test_id('btn-disclosure-ok').click()
    expect(page.get_by_test_id('progress-title')).to_have_text('图纸识别中')
    expect(page.get_by_test_id('progress-count')).to_have_text('本阶段 2 / 4 张图')
    expect(page.get_by_test_id('progress-percent')).to_have_text('整体 20%')
    expect(page.get_by_test_id('progress-eta')).to_have_text('本阶段预计剩余 30 秒')
    assert '整体预计剩余' not in page.locator('body').inner_text()
    expect(page.get_by_test_id('progress-region')).to_have_text('当前图纸区域 2 / 5 · 40%')
    assert '本阶段 50%' in page.locator('body').inner_text()
    assert '已用时 1 分 5 秒' in page.locator('body').inner_text()
    expect(page.get_by_test_id('btn-import')).to_be_disabled()
    screenshot(page, 'dc-progress-review.png')
    expect(page.get_by_test_id('results-title')).to_have_text('审查未完成', timeout=7000)


def test_remove_page_unreadable_and_source_real_backend(imported, client, screenshot):
    page = imported
    first_version = page.locator('body').get_attribute('data-version-id')
    rows = page.get_by_test_id('sheet-row')
    expect(rows).to_have_count(2)
    rows.first.get_by_role('button', name='移除', exact=True).click()
    expect(page.get_by_test_id('dlg-remove')).to_contain_text('原图和历史记录保留')
    screenshot(page, 'dc-remove-confirm.png')
    page.get_by_role('button', name='确认移除').click()
    expect(rows).to_have_count(1, timeout=15000)
    second_version = page.locator('body').get_attribute('data-version-id')
    assert second_version != first_version
    page.get_by_test_id('btn-import').click()
    expect(page.get_by_test_id('dlg-import-mode')).to_contain_text('补充或替换图纸')
    expect(page.get_by_test_id('dlg-import-mode')).to_contain_text('新项目')
    page.get_by_role('button', name='取消').click()
    page.get_by_role('button', name='移除记录').click()
    page.get_by_role('button', name='确认移除').click()
    expect(page.get_by_test_id('unreadable-list')).to_have_count(0, timeout=15000)
    page.get_by_role('button', name='移除整个文件').click()
    page.get_by_role('button', name='确认移除').click()
    expect(rows).to_have_count(0, timeout=15000)
    expect(page.get_by_test_id('btn-review')).to_be_disabled()
    history = client.get('/api/history').json()['items']
    old = next(item for item in history if item['version_id'] == first_version)
    original = client.post('/api/history/open', json={'project_id':old['project_id'], 'version_id':first_version}).json()
    assert len(original['drawings']) == 2 and len(original['unreadable']) == 1


def test_remove_from_results_opens_manage_and_requires_rereview(imported, configure):
    page = imported
    configure()
    page.get_by_test_id('btn-review').click()
    page.get_by_test_id('btn-disclosure-ok').click()
    expect(page.locator('body')).to_have_attribute('data-state', 'results', timeout=30000)
    page.get_by_role('link', name='管理图纸').click()
    expect(page.locator('body')).to_have_attribute('data-state', 'managing')
    page.get_by_test_id('sheet-row').first.get_by_role('button', name='移除', exact=True).click()
    page.get_by_role('button', name='确认移除').click()
    expect(page.locator('body')).to_have_attribute('data-state', 'imported', timeout=15000)
    expect(page.get_by_test_id('sheet-row')).to_have_count(1)
    expect(page.get_by_test_id('btn-review')).to_be_enabled()
    expect(page.get_by_test_id('btn-export')).to_be_disabled()


def test_retained_partial_source_cannot_hide_import_warning(imported, client, monkeypatch):
    """A converted source with retained pages keeps a visible, non-dismissible risk."""
    import json
    import os
    from pathlib import Path
    page = imported
    version = page.locator('body').get_attribute('data-version-id')
    root = Path(os.environ['DRAWCHECK_DATA_DIR'])
    manifest_path = next(root.glob(f'projects/*/cache/import-{version}.json'))
    manifest = json.loads(manifest_path.read_text())
    source = next(row for row in manifest['files'] if row.get('unit_count', 0))
    source['status'] = 'partial'
    manifest['unreadable'].append({'name':source['name'], 'original_id':source['original_id'],
        'code':'PDF_INCOMPLETE', 'reason':'测试转换风险：局部文字未恢复'})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
    page.reload(wait_until='networkidle')
    warning = page.get_by_test_id('unreadable-list')
    expect(warning).to_contain_text('测试转换风险：局部文字未恢复')
    row = warning.locator('li').filter(has_text='测试转换风险')
    expect(row).to_contain_text(source['name'])
    expect(row.get_by_role('button', name='移除记录')).to_have_count(0)
    # An unrelated, unsupported file may still be removed.
    expect(warning.get_by_role('button', name='移除记录')).to_have_count(1)
    expect(page.get_by_role('button', name='移除整个文件')).to_be_visible()
