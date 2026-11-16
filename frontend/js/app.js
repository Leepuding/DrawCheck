(() => {
  'use strict';

  const main = document.getElementById('main');
  const overlay = document.getElementById('overlay-root');
  const test = name => document.querySelector(`[data-testid="${name}"]`);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  let state = 'idle';
  let returnState = 'idle';
  let config = null;
  let project = null;
  let busy = false;
  let settingsBusy = false;
  let reviewPending = false;
  let progressValue = 0;
  let reviewTask = null;
  let importProgress = null;
  let importMode = 'new';
  let resultBundle = null; // {items, pending_count, ...}
  let projectionRequest = 0;
  let activeTab = 'issues';
  let selectedIssueId = null;
  let focusMark = false;
  let selectedDrawingId = null;
  let comparing = false;
  let auxiliaryProof = null;
  let zoom = 1;
  let viewer = null;
  const MIN_ZOOM = 0.75;
  const MAX_ZOOM = 1000;
  const REGION_ZOOM = 2;
  let confirmBundle = null; // {items, pending_count, incomplete_pending_count}
  let selectedConfirmId = null;
  let editingConfirmId = null; // W7-Q1=A：点已处理行展开表单，计数不加回


  let toastTimer = null;
  const cleared = new Set();
  const failures = {
    CREDENTIAL_REQUIRED: '请先填写模型密钥', LOCAL_URL_REQUIRED: '请先填写本地模型地址',
    CONNECTION_OK: '连接正常', AUTH_INVALID: '密钥无效或没有权限，请检查后重新填写',
    BALANCE_INSUFFICIENT: '服务商账户余额不足，请到服务商处充值',
    RATE_LIMITED: '服务商暂时限制了调用次数，请稍后再试', TIMEOUT: '连接超时，请检查网络后再试',
    CERTIFICATE_ERROR: '无法建立安全连接，请检查网络或代理设置',
    VISION_UNSUPPORTED: '这个模型不能看图，请换一个支持看图的模型',
    LOCAL_UNREACHABLE: '连不上本地模型地址，请确认地址正确且本地模型已启动',
    PRIMARY_REQUIRED: '请先填主模型；只用一个模型时填在主模型里',
    MONTHLY_LIMIT_REQUIRED: '请填写每月费用上限（元）', LIMIT_REQUIRED: '请填写每月费用上限（元）',
    SAME_MODEL: '主模型和复核模型是同一个模型，请换一个复核模型或清空复核',
    NO_MODEL: '先配置模型才能审查',
    COST_EXCEEDED: '本月费用估算已到上限，审查已暂停。可在设置里调高上限后点「开始审查」继续。'
  };
  function message(result) {
    const code = result.code || 'OTHER';
    return failures[code] || (code === 'COST_WARNING' ? result.message : `连接失败（错误码：${/^[A-Za-z_0-9]+$/.test(code) ? code : 'OTHER'}），请稍后再试`);
  }
  async function api(path, options = {}) {
    const response = await fetch(path, {credentials: 'same-origin', ...options});
    const result = await response.json();
    if (!response.ok) throw result.error || {code: 'OTHER'};
    return result;
  }
  const json = (method, body) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  function toast(text, blocked = false) {
    clearTimeout(toastTimer);
    document.getElementById('toast-root').innerHTML = `<div class="toast${blocked ? ' is-block' : ''}" role="status" ${blocked ? 'data-testid="toast-block"' : ''}>${esc(text)}</div>`;
    toastTimer = setTimeout(() => { document.getElementById('toast-root').textContent = ''; }, blocked ? 6500 : 4000);
  }
  function syncHeader() {
    document.body.dataset.versionId = project?.version_id || '';
    test('btn-import').disabled = busy || !config || ['settings','reviewing'].includes(state);
    test('btn-review').disabled = busy || reviewPending || project?.can_continue === false || !project?.drawings.length || !['imported','results'].includes(state);
    test('btn-export').disabled = busy || state !== 'results';
    test('link-history').setAttribute('aria-disabled', String(busy || state === 'reviewing'));
    test('btn-settings').disabled = busy || reviewPending;
    test('btn-settings').setAttribute('aria-pressed', String(state === 'settings'));
    test('btn-settings').classList.toggle('is-active', state === 'settings');
    ['import','review','export'].forEach(action => test(`btn-${action}`).classList.toggle('is-primary',
      !['settings','reviewing'].includes(state) && action === (state === 'idle' ? 'import' : state === 'results' ? 'export' : 'review')));
  }
  function render() {
    if (state !== 'results') disposeViewer();
    document.body.dataset.state = state;
    syncHeader();
    if (state === 'idle') {
      main.innerHTML = `<section class="idle-page" aria-label="导入图纸"><div class="empty-zone" id="drop-zone">
        <div class="empty-art" aria-hidden="true"><div class="empty-art-inner">${'<i></i>'.repeat(8)}</div><span class="symbol">arrow_upward</span></div>
        <h1>把图纸拖进来，或点导入</h1><p>支持 PDF、DWG · 双击安装即可用，数据只存本机</p></div>
        <p class="setup-hint">${config?.model_status === 'none' ? '还没配置模型 · <a href="#" data-action="settings">去设置</a>' : ''}</p>
        <a href="#" class="folder-link" data-action="folder" data-testid="link-import-folder">改选文件夹</a></section>`;
    } else if (state === 'settings') renderSettings();
    else if (state === 'reviewing' || state === 'importing') renderProgress(); else if (state === 'results') renderResults();
    else renderDirectory(false);
  }
  function duration(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds)) || Number(seconds) < 0) return null;
    const n = Math.round(Number(seconds));
    if (n < 60) return `${n} 秒`;
    return `${Math.floor(n / 60)} 分${n % 60 ? ` ${n % 60} 秒` : ''}`;
  }
  function renderProgress() {
    const importing = state === 'importing';
    const info = importing ? (importProgress || {}) : (reviewTask || {});
    const uploading = importing && info.stage === 'upload';
    const percent = info.percent;
    const known = percent != null && Number.isFinite(Number(percent));
    const amount = known ? Math.max(0, Math.min(100, Math.round(Number(percent)))) : null;
    const title = uploading ? '正在上传图纸到本机' : importing ? (info.phase || '正在处理图纸') : (info.phase || '正在审查图纸，请稍候');
    const count = info.total_count > 0 ? `本阶段 ${info.processed_count || 0} / ${info.total_count} ${info.progress_unit || (importing ? '个文件' : '项')}` : '';
    const phasePercent = info.phase_percent == null ? null : Math.max(0, Math.min(100, Math.round(Number(info.phase_percent))));
    const elapsed = duration(info.elapsed_seconds);
    const stageEta = info.eta_scope === 'phase' ? duration(info.eta_seconds) : null;
    const overallEta = info.eta_scope === 'overall' ? duration(info.eta_seconds) : null;
    main.innerHTML = `<section class="progress-page"><div class="progress-content" aria-live="polite" aria-atomic="true">
      <p data-testid="progress-title">${esc(title)}</p>
      <div class="progress-track${known ? '' : ' is-indeterminate'}" data-testid="progress" role="progressbar" aria-label="${importing ? '导入进度' : '审查进度'}" aria-valuemin="0" aria-valuemax="100"${known ? ` aria-valuenow="${amount}"` : ''}><div class="progress-bar" style="width:${known ? amount : 35}%"></div></div>
      <div class="progress-meta"><span data-testid="progress-count">${esc(count)}</span><span data-testid="progress-percent">${known ? `${uploading ? '上传' : '整体'} ${amount}%` : '整体进度暂不可估算'}</span></div>
      <div class="progress-times"><span>${phasePercent != null && !uploading ? `本阶段 ${phasePercent}%` : ''}${elapsed ? `${phasePercent != null && !uploading ? ' · ' : ''}已用时 ${esc(elapsed)}` : ''}</span><span data-testid="progress-eta">${stageEta ? `本阶段预计剩余 ${esc(stageEta)}` : '本阶段剩余时间正在估算，可能不确定'}${overallEta ? ` · 整体预计剩余 ${esc(overallEta)}` : ''}</span></div>
      ${info.phase_key === 'recognizing' && info.region_total_count > 0 ? `<p class="progress-note" data-testid="progress-region">当前图纸区域 ${esc(info.region_processed_count || 0)} / ${esc(info.region_total_count)}${info.region_percent == null ? '' : ` · ${esc(info.region_percent)}%`}</p>` : ''}
      ${info.estimate_basis ? `<p class="progress-note">${esc(info.estimate_basis)}</p>` : ''}
      ${uploading ? '<p class="progress-note">文件正在传送到本机程序。</p>' : ''}
    </div></section>`;
  }
  function uploadImport(url, form) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url);
      xhr.responseType = 'json';
      xhr.upload.onprogress = event => {
        importProgress = {stage:'upload', percent: event.lengthComputable && event.total ? event.loaded / event.total * 100 : null};
        if (state === 'importing') render();
      };
      xhr.upload.onload = () => { importProgress = {stage:'processing', phase:'本机正在接收图纸', percent:null}; if (state === 'importing') render(); };
      xhr.onload = () => xhr.status >= 200 && xhr.status < 300 && xhr.response ? resolve(xhr.response) : reject(new Error('上传未完成'));
      xhr.onerror = () => reject(new Error('上传未完成'));
      xhr.onabort = () => reject(new Error('上传已中断'));
      xhr.send(form);
    });
  }
  function diagnosticText(item, fallback) {
    const {notice, reason} = item;
    if (!notice) return `<span>${esc(fallback)}</span>`;
    return `<div class="source-notice">
      <p>${esc(notice.summary)}</p>
      ${(notice.notes || []).map(note => `<p>${esc(note)}</p>`).join('')}
      <p>${esc(notice.action)}</p>
      ${reason ? `<details class="technical-details"><summary>查看技术详情</summary><p>${esc(String(reason).split('；').join('；\n'))}</p></details>` : ''}
    </div>`;
  }
  function renderDirectory(results) {
    const retainedSource = item => item.original_id && project.files?.some(file =>
      file.original_id === item.original_id && Number(file.unit_count) > 0);
    main.innerHTML = `<section class="imported-page"><div class="project-heading"><h1>${results ? '审查完成' : '导入图纸'}</h1>
      <p data-testid="import-summary">${esc(project.summary)}</p>${state === 'managing' ? '<a href="#" data-action="back-results">返回审查结果</a>' : ''}</div>
      ${project.project_code_notice ? `<aside class="unreadable" data-testid="project-code-notice" role="status"><div class="unreadable-title">${esc(project.project_code_notice.reason)}</div>
        <ul>${project.project_code_notice.evidence.map(row => `<li><strong>${esc(row.project_code)}</strong><span>${esc(row.source)} · 第 ${esc(row.ordinal)} 页/图框 · ${esc(row.drawing_number)}</span></li>`).join('')}</ul></aside>` : ''}
      ${results ? '' : ''}
      <div class="sheet-directory" aria-label="图纸目录"><div class="directory-caption"><span>图纸目录</span><span>点击图名可修改</span></div>
      ${project.drawings.map(sheet => `<div class="sheet-row" data-testid="sheet-row" data-unit="${esc(sheet.unit_id)}"><span class="symbol" aria-hidden="true">description</span>
        <span class="sheet-code">${esc(sheet.drawing_number || sheet.ordinal)}</span><span class="sheet-name" data-testid="sheet-rename" data-unit="${esc(sheet.unit_id)}" data-action="rename" role="button" tabindex="0" aria-label="修改图名：${esc(sheet.name)}"><span>${esc(sheet.name)}</span><span class="rename-hint">改名</span></span>${project.can_continue === false ? '' : `<button type="button" class="remove-link" data-action="remove-unit" data-id="${esc(sheet.unit_id)}" data-name="${esc(sheet.name)}">移除</button>`}</div>`).join('')}</div>
      ${project.unreadable.length ? `<aside class="unreadable" data-testid="unreadable-list"><div class="unreadable-title">导入提示（${project.unreadable.length} 条）</div>
        <ul>${project.unreadable.map((item, index) => `<li><strong>${esc(item.name)}${item.page ? ` · 第 ${esc(item.page)} 页` : ''}</strong>${diagnosticText(item, item.reason)}${project.can_continue === false ? '' : retainedSource(item) ? '' : `<button type="button" class="remove-link" data-action="remove-unreadable" data-index="${index}" data-name="${esc(item.name)}">移除记录</button>`}</li>`).join('')}</ul></aside>` : ''}
      ${project.files?.some(file => file.original_id) && project.can_continue !== false ? `<div class="source-files"><h2>源文件</h2>${project.files.filter(file => file.original_id).map(file => `<div class="source-file"><span>${esc(file.name)}</span><button type="button" class="remove-link" data-action="remove-original" data-id="${esc(file.original_id)}" data-name="${esc(file.name)}">移除整个文件</button></div>`).join('')}</div>` : ''}
      ${project.files?.some(file => file.status === 'pending_parse') ? '<p class="import-hint">部分图纸待解析，尚未计入审查。</p>' : ''}
      <p class="import-hint">${esc(project.duplicate_message || '')}</p>
      ${project.revised && state === 'imported' ? '<p class="import-hint" data-testid="revision-import-hint">当前显示本次导入的图纸；开始审查后会识别并与上一版匹配，未替换的图纸将保留。</p>' : ''}
      ${!results && project.drawings.length ? '<p class="import-hint">图纸已就绪，点顶部「开始审查」即可。</p>' : ''}
      <p class="import-hint"><a href="#" data-action="folder" data-testid="link-import-folder">改选文件夹</a></p></section>`;
  }

  function statusLabel(status) {
    return status || '疑似';
  }
  function buttonActive(status, button) {
    const map = {confirm: '已确认', exclude: '排除', needinfo: '需补充资料'};
    return statusLabel(status) === map[button];
  }
  function currentIssue() {
    const items = resultBundle?.items || [];
    return items.find(item => item.id === selectedIssueId) || items.find(isActiveDifference) || null;
  }
  function bboxPercent(box) {
    if (!box || ![box.x0, box.y0, box.x1, box.y1].every(Number.isFinite)) return {x:0,y:0,w:0,h:0,bad:true};
    const x = box.x0 * 100, y = box.y0 * 100, w = (box.x1 - box.x0) * 100, h = (box.y1 - box.y0) * 100;
    const bad = [box.x0, box.y0, box.x1, box.y1].some(v => v < 0 || v > 1) || box.x0 >= box.x1 || box.y0 >= box.y1;
    return {x, y, w, h, bad};
  }
  function issueRow(issue) {
    const selected = issue.id === (selectedIssueId || currentIssue()?.id);
    const excluded = issue.review_status === '排除' || issue.diff === '已解决';
    return `<article class="issue-row${selected ? ' is-selected' : ''}${excluded ? ' is-excluded' : ''}" data-testid="issue-row" data-id="${esc(issue.id)}" data-status="${esc(issue.review_status)}" data-diff="${esc(issue.diff || '')}" data-action="select-issue" tabindex="0" aria-label="查看${esc(issue.title)}" aria-current="${selected}">
      <div class="row-meta"><span class="row-number">问题</span><span class="status-label" data-status="${esc(issue.review_status)}">${esc(statusLabel(issue.review_status))}</span>${issue.diff ? `<span class="diff-label">${esc(issue.diff)}</span>` : ''}</div>
      <h3>${esc(issue.title)}</h3><p class="row-sheets">${(issue.sheets || []).map(esc).join(' · ')}</p>
      ${selected ? `<div class="issue-detail">${issue.finding ? '' : `<p>${esc(issue.description)}</p>`}<div class="review-actions" aria-label="复核问题">
        <button type="button" data-testid="btn-confirm" data-action="review-status" data-status="已确认" class="${buttonActive(issue.review_status,'confirm') ? 'is-active' : ''}" aria-pressed="${buttonActive(issue.review_status,'confirm')}">确认</button>
        <button type="button" data-testid="btn-exclude" data-action="review-status" data-status="排除" class="${buttonActive(issue.review_status,'exclude') ? 'is-active' : ''}" aria-pressed="${buttonActive(issue.review_status,'exclude')}">排除</button>
        <button type="button" data-testid="btn-needinfo" data-action="review-status" data-status="需补充资料" class="${buttonActive(issue.review_status,'needinfo') ? 'is-active' : ''}" aria-pressed="${buttonActive(issue.review_status,'needinfo')}">需补充资料</button>
      </div></div>` : ''}
    </article>`;
  }
  function isActiveDifference(item) {
    return item.review_status !== '排除' && item.diff !== '已解决';
  }
  function resultsSubtitle(bundle) {
    const lines = [];
    const count = (bundle.items || []).filter(isActiveDifference).length;
    if (count) lines.push(`发现 ${count} 条差异`);
    if (bundle.verification?.scope_note) lines.push(bundle.verification.scope_note);
    if (bundle.out_of_scope_count) lines.push(`另有 ${bundle.out_of_scope_count} 张图不在审查范围，未检查`);
    return lines.map(line => `<p class="results-note">${esc(line)}</p>`).join('');
  }

  function proofCard(side, proof, selected) {
    const label = side === 'left' ? '图纸一' : '图纸二';
    const value = proof?.value ?? proof?.text;
    const canLocate = proof?.image_url && !proof.bbox_out_of_range && !bboxPercent(proof.bbox).bad;
    return `<div class="proof-card${selected ? ' is-active' : ''}">
      <span class="proof-side">${label}</span>
      ${proof?.drawing_number || proof?.drawing_name
        ? `<strong><span class="proof-key">图号</span> ${esc(proof.drawing_number || '未提供')} <span class="proof-key">图名</span> ${esc(proof.drawing_name || '未提供')}</strong>`
        : `<strong>${esc(proof?.sheet || '无对应图纸')}</strong>`}
      <span><span class="proof-key">原文</span> ${esc(proof?.text || '未提供')}</span>
      <span><span class="proof-key">对照值</span> <strong class="proof-value">${esc(value === '' || value == null ? '未读出' : value)}</strong></span>
      ${!proof?.image_url ? '<span class="proof-warning">无可用原图</span>' : canLocate ? '' : '<span class="proof-warning">未找到原图标注位置</span>'}
      ${proof?.image_url ? `<button type="button" class="proof-jump" data-testid="proof-${side}" data-action="proof-side" data-side="${side}"
          aria-pressed="${selected}">${canLocate ? '放大并圈出标注' : '查看原图'}</button>` : ''}
    </div>`;
  }

  function proofPanel(data, issue) {
    if (!data) return '';
    const basis = data.basis || (data.right?.image_url ? '未记录配对依据' : '未找到可显示的第二张原图');
    return `<section class="proof-panel" data-testid="proof-panel" aria-label="差异证据">
      <h3>差异证据 · ${esc(data.label || issue.category || issue.title)}</h3>
      <div class="proof-grid">${proofCard('left', data.left, !comparing)}${proofCard('right', data.right, comparing)}</div>
      <div class="proof-notes"><p><strong>对照依据</strong> ${esc(basis)}</p>
      <p><strong>差异原因</strong> ${esc(data.reason || issue.description || '未记录差异原因')}</p></div>
      ${unitDeclarationList(data.unit_declarations)}
    </section>`;
  }

  function evidenceJump(proof, label, marker = '声明位置') {
    if (!proof?.image_url || bboxPercent(proof.bbox).bad) return '';
    return `<button type="button" class="proof-jump" data-action="auxiliary-proof" data-unit="${esc(proof.unit_id)}"
      data-box="${esc(JSON.stringify(proof.bbox))}" data-marker="${esc(marker)}"
      data-sheet="${esc(proof.sheet)}" data-text="${esc(proof.text)}">${esc(label)}</button>`;
  }

  function unitDeclarationList(notes) {
    if (!notes?.length) return '';
    return `<ul class="unit-declarations" data-testid="unit-declarations">${notes.map(note => `<li>
      <strong>${esc(note.category)} · ${esc(note.unit)} · ${esc(note.drawing_number)} · 第 ${esc(note.ordinal)} 页/图框</strong>
      <span>${esc(note.text)}</span>${evidenceJump(note, '圈出原图声明') || '<span>暂无原图预览</span>'}</li>`).join('')}</ul>`;
  }

  function unverifiedPanel(bundle, confirms, opened) {
    const unresolved = (confirms.items || []).filter(item => ['待确认', '无法确定'].includes(item.status));
    const flow = (bundle.incomplete_items || []).filter(item => item.category !== 'out_of_scope');
    const extraUnreadable = flow.some(item => item.category === 'unreadable') ? [] : (project?.unreadable || []);
    const pairs = (bundle.comparison_details || []).filter(row => ['unverified', 'linked'].includes(row.outcome));
    const noPair = Array.isArray(bundle.comparison_summary)
      ? bundle.comparison_summary.filter(row => !row.verified && !row.difference && !row.unverified).map(row => row.category) : [];
    const stillRunning = bundle.failed || bundle.cost_paused || bundle.flow_incomplete_count > 0;
    const hasUnreadable = bundle.unreadable_count > 0;
    const hasHuman = unresolved.length || bundle.pending_count > 0 || bundle.unsure_count > 0;
    const blocked = !bundle.verification?.has_effective_comparison && !(bundle.items || []).some(isActiveDifference);
    if (!flow.length && !extraUnreadable.length && !pairs.length && !noPair.length && !stillRunning && !hasUnreadable && !hasHuman && !blocked) return '';
    return `<details class="incomplete-panel" data-testid="incomplete-panel"${opened ? ' open' : ''}>
      <summary>未核验</summary>
      <p>以下内容尚无核验结论。</p>
      ${flow.length || extraUnreadable.length || stillRunning || hasUnreadable ? `<h3>识别或读取未完成</h3>
        ${flow.length || extraUnreadable.length ? `<ul>${flow.map(item => `<li><strong>${esc([item.drawing_number, item.drawing_name].filter(Boolean).join(' · ') || '整体审查')}</strong>
          ${item.title_verified ? '<span>图号已核实</span>' : ''}
          ${diagnosticText(item, `${item.phase || '审查'} · ${item.reason || '未完成'}`)}
          ${item.retryable && project?.can_continue !== false ? `<button type="button" class="small-button" data-action="retry-page"
            data-id="${esc(item.unit_id)}" data-name="${esc(item.drawing_number || item.drawing_name)}">仅续审此页</button>` : ''}</li>`).join('')}${extraUnreadable.map(item => `<li><strong>${esc(item.name)}${item.page ? ` · 第 ${esc(item.page)} 页` : ''}</strong>${diagnosticText(item, item.reason || '未读出')}</li>`).join('')}</ul>` : '<p>暂无可显示的明细，可查看导入记录。</p>'}` : ''}
      ${pairs.length || noPair.length || blocked ? `<h3>对照依据不足</h3>
        ${noPair.length ? `<p>以下类别缺少对照依据（${esc(noPair.join('、'))}）。</p>` : blocked && !pairs.length ? '<p>尚未形成有效对照。</p>' : ''}
        ${pairs.length ? `<ul>${pairs.map(item => `<li><strong>${esc([item.category, item.label].filter(Boolean).join(' · '))}</strong>
          <span>${esc([item.left?.sheet, item.right?.sheet].filter(Boolean).join(' ↔ '))}</span>
          <span>${esc(item.reason || item.basis || '无法确定两张图的标注是否对应。')}</span>
          ${unitDeclarationList(item.unit_declarations)}
          ${item.category === '设计参数' ? [item.left, item.right].map((side, index) => `<span>${esc(side?.sheet)}：${esc(side?.text)} ${evidenceJump(side, `圈出声明 ${index + 1}`)}</span>`).join('') : ''}
          ${['尺寸','标高'].includes(item.category) ? [item.left, item.right].map((side, index) => evidenceJump(side, index ? '圈出图纸二标注' : '圈出图纸一标注', '标注位置')).join('') : ''}
          ${item.category === '目录与图签' && !item.right?.unit_id ? evidenceJump(item.left, '圈出目录原文', '目录位置') : ''}
          </li>`).join('')}</ul>` : ''}` : ''}
      ${hasHuman ? `<h3>人工待确认</h3>
        ${unresolved.length ? `<ul>${unresolved.map(item => `<li><button type="button" class="unverified-jump" data-action="unverified-confirm" data-id="${esc(item.id)}">${esc(item.title || '查看确认项')}</button>
          <span>${esc([item.main_label, item.status].filter(Boolean).join(' · '))}</span></li>`).join('')}</ul>` : '<p>可在「待你确认」中查看。</p>'}` : ''}
      ${(stillRunning || flow.length || extraUnreadable.length || hasUnreadable) && project?.can_continue !== false ? '<button type="button" class="small-button" data-action="review">重新审查</button>' : ''}
    </details>`;
  }

  function currentConfirmation() {
    const items = confirmBundle?.items || [];
    return items.find(item => item.id === selectedConfirmId) || items[0] || null;
  }
  function confirmationRow(item) {
    const selected = item.id === (selectedConfirmId || currentConfirmation()?.id);
    const processed = item.status !== '待确认';
    const statusText = processed ? '已处理' : '待确认';
    let note = '';
    if (processed) {
      if (item.status === '无法确定') note = '<p class="processed-note unsure">无法确定 · 计入未完成</p>';
      else note = `<p class="processed-note">已保存：${esc(item.confirmed_text || '')}</p>`;
    }
    return `<article class="confirm-row${selected ? ' is-selected' : ''}" data-testid="confirm-row" data-id="${esc(item.id)}" data-status="${esc(item.status)}" data-processed="${processed}" data-action="select-confirm" tabindex="0" aria-label="查看${esc(item.title)}" aria-current="${selected}">
      <div class="row-meta"><span class="row-number">确认</span><span class="status-label"${processed ? ' data-status="确认"' : ''}>${statusText}</span></div>
      <h3>${esc(item.title)}</h3><p class="row-sheets">${esc(item.main_label || '')}</p>${note}
    </article>`;
  }
  function confirmationForm(item) {
    if (!item) return '';
    const reedit = editingConfirmId === item.id;
    const open = item.status === '待确认' || reedit;
    if (!open) {
      if (item.status === '无法确定') {
        return `<div class="confirmation-form"><p class="handled unsure">已处理：无法确定，导出中将标为未完成。</p></div>`;
      }
      return `<div class="confirmation-form"><p class="handled">已处理 · 正确内容：${esc(item.confirmed_text || '')}</p></div>`;
    }
    const prefill = item.status === '已确认' ? (item.confirmed_text || '') : '';
    return `<form class="confirmation-form" id="confirmation-form"><p>${esc(item.location || '')} · 请核对红圈中的文字或数字。</p><label for="confirm-value">正确内容</label><div class="confirmation-input-row"><input id="confirm-value" data-testid="in-confirm-value" placeholder="输入看清的内容" autocomplete="off" maxlength="80" value="${esc(prefill)}" required><button type="button" class="small-button" data-testid="btn-confirm-unsure" data-action="confirm-unsure">无法确定</button><button type="submit" class="small-button accent" data-testid="btn-confirm-save">保存</button></div></form>`;
  }
  async function processConfirmation(unsure) {
    if (project?.can_continue === false) { toast('请打开该项目最新版本后确认', true); return; }
    const item = currentConfirmation();
    if (!item) return;
    const bundle = resultBundle;
    const input = test('in-confirm-value');
    const value = (input?.value || '').trim();
    if (!unsure && !value) { input?.focus(); toast('请填写正确内容，或选择「无法确定」'); return; }
    const body = unsure ? {status: '无法确定'} : {status: '已确认', confirmed_text: value};
    try {
      const result = await api(`/api/pending/${encodeURIComponent(item.id)}`, json('PATCH', body));
      if (bundle !== resultBundle) return;
      const updated = result.item;
      const idx = (confirmBundle.items || []).findIndex(row => row.id === updated.id);
      if (Array.isArray(result.items)) {
        const current = new Map(result.items.map(row => [row.id, row]));
        const order = new Set([...confirmBundle.items.map(row => row.id), ...current.keys()]);
        confirmBundle.items = [...order].map(id => current.get(id)).filter(Boolean);
      } else if (idx >= 0) confirmBundle.items[idx] = updated;
      else confirmBundle.items = [...(confirmBundle.items || []), updated];
      confirmBundle.pending_count = result.pending_count;
      confirmBundle.incomplete_pending_count = result.incomplete_pending_count;
      if (resultBundle) {
        resultBundle.pending_count = result.pending_count;
        resultBundle.unsure_count = Math.max(0, result.incomplete_pending_count - result.pending_count);
      }
      editingConfirmId = null;
      renderResults();
      toast(unsure ? '已标记无法确定，计入未完成' : '正确内容已保存');
      refreshResultProjection(bundle, true);
    } catch (_) {
      toast('确认未保存，请重试', true);
    }
  }

  function issueProof(issue) {
    if (!issue) return null;
    const legacySide = (prefix) => ({
      unit_id: issue[`${prefix}_unit_id`], sheet: issue[`${prefix}_label`],
      image_url: issue[`${prefix}_image_url`], bbox: issue[`${prefix}_bbox`],
      bbox_out_of_range: issue[`${prefix}_bbox_out_of_range`],
    });
    return issue.finding || {left: legacySide('main'), right: legacySide('related'), reason: issue.description};
  }

  function renderResults() {
    const bundle = resultBundle || {items:[]};
    const confirms = confirmBundle || {items:[]};
    const confirmation = activeTab === 'confirm';
    const drawingsTab = activeTab === 'drawings';
    const onIssue = !drawingsTab && !confirmation;
    const issue = currentIssue();
    const pendingItem = currentConfirmation();
    const drawing = (project?.drawings || []).find(item => item.unit_id === selectedDrawingId) || project?.drawings?.[0] || null;
    const proof = onIssue ? issueProof(issue) : null;
    const side = auxiliaryProof || (comparing ? proof?.right : proof?.left);
    const otherSide = auxiliaryProof ? null : comparing ? proof?.left : proof?.right;
    const imageUrl = auxiliaryProof?.image_url || (drawingsTab ? (drawing ? `/api/units/${encodeURIComponent(drawing.unit_id)}/image` : '') : confirmation
      ? (pendingItem?.main_image_url || '') : side?.image_url || '');
    const title = auxiliaryProof?.sheet || (drawingsTab ? (drawing?.name || '') : confirmation ? (pendingItem?.main_label || '待你确认') : side?.sheet || '');
    const unitId = (auxiliaryProof?.unit_id || (drawingsTab ? drawing?.unit_id : confirmation ? pendingItem?.unit_id : side?.unit_id))
      || imageUrl.match(/^\/api\/units\/([^/]+)\/image$/)?.[1];
    const rawBox = auxiliaryProof?.bbox || (confirmation ? pendingItem?.bbox : side?.bbox);
    const box = bboxPercent(rawBox);
    const outOfRange = !!rawBox && box.bad || (!auxiliaryProof && (confirmation ? pendingItem?.bbox_out_of_range : side?.bbox_out_of_range));
    const mark = !drawingsTab && !box.bad && !outOfRange ? rawBox : null;
    const staticHint = outOfRange ? (confirmation ? '待确认位置坐标超出图纸范围，无法高亮' : '问题位置坐标超出图纸范围，无法高亮')
      : imageUrl && !mark && (confirmation || onIssue) ? '未能定位到具体原图标注' : '';
    const pendingCount = typeof confirms.pending_count === 'number' ? confirms.pending_count : (bundle.pending_count || 0);
    const activeIssues = (bundle.items || []).filter(isActiveDifference);
    const closedIssues = (bundle.items || []).filter(item => !isActiveDifference(item));
    const previousRows = document.querySelector('.result-rows');
    const retainRows = previousRows?.dataset.tab === activeTab;
    const scrollTop = retainRows ? previousRows.scrollTop : 0;
    const unverifiedOpen = retainRows && test('incomplete-panel')?.open;
    const closedOpen = (retainRows && test('closed-issues')?.open) || closedIssues.some(item => item.id === issue?.id);
    const identity = [project?.version_id, activeTab, issue?.id, pendingItem?.id, comparing, imageUrl, auxiliaryProof?.key].join('|');
    const oldCenter = viewer?.identity === identity && !focusMark ? viewer.getCenter() : null;
    disposeViewer();
    const banner = bundle.cost_paused
      ? '<div class="cost-banner" data-testid="banner-cost-paused" role="status">本月费用估算已到上限，审查已暂停。可在设置里调高上限后点「开始审查」继续。</div>' : '';

    let rows;
    if (drawingsTab) {
      rows = (project?.drawings || []).length ? project.drawings.map(sheet =>
        `<button type="button" class="drawing-list-row${drawing?.unit_id === sheet.unit_id ? ' is-selected' : ''}" data-testid="drawing-view-row" data-action="select-drawing" data-id="${esc(sheet.unit_id)}">${esc(sheet.drawing_number || sheet.ordinal || '')} ${esc(sheet.name)}</button>`
      ).join('') : '<div class="empty-issues"><p>暂无可查看的图纸</p></div>';
    } else if (confirmation) {
      rows = (confirms.items || []).length
        ? confirms.items.map(confirmationRow).join('') + '<p class="confirm-list-note">请核对红圈中的内容。无法确定的项会在导出中标为未完成。</p>'
        : '<div class="confirm-empty" data-testid="confirm-empty"><p>没有需要确认的内容</p></div>';
    } else {
      const effective = bundle.verification?.has_effective_comparison === true;
      const zeroMessage = bundle.refreshing_verification ? '正在更新核验范围…'
        : effective ? '已核验范围内未发现差异' : '尚未形成有效对照';
      rows = (activeIssues.length ? activeIssues.map(issueRow).join('') : `<div class="empty-issues" data-testid="empty-issues"><p>${zeroMessage}</p></div>`)
        + (bundle.unit_declarations?.length ? `<details class="incomplete-panel" data-testid="verified-unit-declarations"${retainRows && test('verified-unit-declarations')?.open ? ' open' : ''}>
          <summary>卷册单位声明</summary>${unitDeclarationList(bundle.unit_declarations)}</details>` : '')
        + unverifiedPanel(bundle, confirms, unverifiedOpen)
        + (closedIssues.length ? `<details class="closed-issues" data-testid="closed-issues"${closedOpen ? ' open' : ''}><summary>已排除或已解决（${closedIssues.length}）</summary>${closedIssues.map(issueRow).join('')}</details>` : '');
    }

    const placeholder = drawingsTab ? '选择左侧图纸查看' : confirmation ? '选择左侧条目以查看图纸'
      : issue ? '这一侧没有可用原图' : '可切换至「图纸」查看原图';
    main.innerHTML = `${banner}<section class="results-page">
      <div class="results-heading"><h1 data-testid="results-title">${bundle.none_recognized || ((bundle.failed || bundle.cost_paused) && !(bundle.recognized_count > 0)) ? '审查未完成' : bundle.partial || bundle.failed || bundle.cost_paused ? '审查部分完成' : bundle.revised ? '修订版审查完成' : '审查完成'}</h1>${bundle.revision_summary ? `<p class="results-note">${esc(bundle.revision_summary)}</p>` : ''}${resultsSubtitle(bundle)}${project?.can_continue === false ? '' : '<a href="#" class="manage-link" data-action="manage-drawings">管理图纸</a>'}</div>
      <div class="results-workspace">
        <section class="review-pane" aria-label="审查结果">
          <div class="result-tabs" role="tablist" aria-label="结果分类">
            <a href="#" class="result-tab${onIssue ? ' is-active' : ''}" data-testid="tab-issues" data-action="tab-issues" role="tab" aria-selected="${onIssue}">哪里对不上 <span class="tab-count" aria-label="${activeIssues.length} 条差异">${activeIssues.length}</span></a>
            <a href="#" class="result-tab${confirmation ? ' is-active' : ''}" data-testid="tab-confirm" data-action="tab-confirm" role="tab" aria-selected="${confirmation}">待你确认 <span class="tab-count" data-testid="tab-confirm-count">${pendingCount}</span></a>
            <a href="#" class="result-tab${drawingsTab ? ' is-active' : ''}" data-testid="tab-drawings" data-action="tab-drawings" role="tab" aria-selected="${drawingsTab}">图纸</a>
          </div>
          <div class="result-rows" data-tab="${activeTab}">${rows}</div>
        </section>
        <section class="viewer" data-testid="viewer" aria-label="图纸看图器">
          <div class="viewer-toolbar"><div class="viewer-document">
            <h2 class="viewer-title" data-testid="viewer-title">${esc(title || '原图')}</h2>
            ${auxiliaryProof ? '<button type="button" class="proof-jump" data-action="return-to-mark">返回当前标注</button>' : ''}
            ${otherSide?.image_url && otherSide.image_url !== imageUrl ? `<a href="#" class="viewer-compare" data-testid="viewer-compare" data-action="compare">切换到 ${esc(otherSide.sheet || '另一张图纸')}</a>` : '<span class="viewer-compare" data-testid="viewer-compare" hidden></span>'}
          </div>
            ${imageUrl ? `<div class="zoom-controls" aria-label="缩放">
              <button type="button" data-testid="btn-zoom-out" data-action="zoom-out" aria-label="缩小">−</button>
              <span class="zoom-value" id="zoom-value">${Math.round(zoom * 100)}%</span>
              <button type="button" data-testid="btn-zoom-in" data-action="zoom-in" aria-label="放大">+</button>
              <button type="button" class="zoom-shortcut" data-testid="btn-zoom-ten" data-action="zoom-ten" aria-label="放大到适配大小的十倍" title="放大到适配大小的十倍">1000%</button>
              <button type="button" class="zoom-shortcut" data-testid="btn-zoom-fit" data-action="zoom-fit" title="恢复整图适配">适配</button>
            </div>` : ''}
          </div>
          ${auxiliaryProof ? `<section class="proof-panel" data-testid="auxiliary-evidence"><h3>${esc(auxiliaryProof.marker)}</h3><p>${esc(auxiliaryProof.text)}</p></section>`
            : proofPanel(proof, issue)}
          <div class="viewer-stage"${imageUrl ? ' tabindex="0" aria-label="原图，滚轮缩放，拖动平移，也可用方向键平移"' : ''}>
            ${!imageUrl ? `<div class="viewer-placeholder" data-testid="viewer-placeholder">${placeholder}</div>`
              : `<div class="drawing-sheet" id="drawing-sheet">
                <img data-testid="viewer-img" src="${esc(imageUrl)}" alt="${esc(title)}" draggable="false">
                <img class="viewer-region" data-testid="viewer-region" alt="" draggable="false" hidden>
                <div class="viewer-hl${confirmation && !auxiliaryProof ? ' is-round' : ''}" data-testid="viewer-hl" aria-label="${esc(auxiliaryProof?.marker || (confirmation ? '待确认位置' : '差异位置'))}" style="left:${box.x}%;top:${box.y}%;width:${box.w}%;height:${box.h}%;${mark ? '' : 'display:none'}"></div>
              </div><div class="viewer-stage-message" aria-hidden="true" hidden></div>`}
          </div>
          ${staticHint || imageUrl ? `<p class="viewer-hint" data-testid="viewer-hint" role="status"${staticHint ? '' : ' hidden'}>${esc(staticHint)}</p>` : ''}
          ${confirmation && pendingItem?.unit_declarations?.length ? `<div class="proof-panel">${unitDeclarationList(pendingItem.unit_declarations)}</div>` : ''}
          ${confirmation && !auxiliaryProof ? confirmationForm(pendingItem) : ''}
        </section>
      </div>
    </section>`;
    document.querySelector('.result-rows').scrollTop = scrollTop;
    if (imageUrl) {
      const center = focusMark && mark ? {x: (mark.x0 + mark.x1) / 2, y: (mark.y0 + mark.y1) / 2} : oldCenter;
      setupViewer({identity, unitId, center, staticHint, focusBox: focusMark ? mark : null});
    }
    focusMark = false;
    if (confirmation && pendingItem && (pendingItem.status === '待确认' || editingConfirmId === pendingItem.id)) {
      document.getElementById('confirmation-form')?.addEventListener('submit', event => {
        event.preventDefault();
        processConfirmation(false);
      });
    }
  }

  function disposeViewer() {
    viewer?.destroy();
    viewer = null;
  }
  function resetViewer(nextZoom = 1, locate = false) {
    disposeViewer();
    auxiliaryProof = null;
    zoom = nextZoom;
    focusMark = locate;
  }
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  function setupViewer({identity, unitId, center, staticHint, focusBox}) {
    const stage = document.querySelector('.viewer-stage');
    const sheet = document.getElementById('drawing-sheet');
    const img = test('viewer-img');
    const region = test('viewer-region');
    const hint = test('viewer-hint');
    const status = stage.querySelector('.viewer-stage-message');
    const regionApi = unitId ? `/api/units/${encodeURIComponent(unitId)}/image-region` : null;
    const infoController = new AbortController();
    let metadata, natural, geometry, placed, pending, requestTimer, drag;
    let desiredCenter = center;
    let loadError = false;
    let destroyed = false;
    const supportsRegion = () => ['pdf_vector', 'dwg_vector', 'raster'].includes(metadata?.kind);
    const needsRegion = () => zoom >= REGION_ZOOM;
    const current = () => !destroyed && viewer === session;
    const bounds = (axis, size, frame) => zoom <= 1 ? (frame - size) / 2
      : clamp(axis, Math.min((frame - size) / 2, frame / 2 - size), Math.max((frame - size) / 2, frame / 2));

    function report(text = '') {
      if (status.textContent !== text) status.textContent = text;
      status.hidden = !text;
    }
    function showHint() {
      const failure = loadError && !(needsRegion() && supportsRegion()) ? '图纸预览加载失败，请检查导入结果或稍后重试' : '';
      const sourceNotice = metadata?.kind === 'unavailable'
        ? ['仅可查看现有预览，清晰度受限', metadata.message].filter(Boolean).join(' · ')
        : metadata?.message || (metadata?.kind === 'raster' ? '扫描图纸的清晰度受原始像素限制' : '');
      const text = [staticHint, failure, sourceNotice].filter(Boolean).join(' · ');
      if (hint.textContent !== text) hint.textContent = text;
      hint.hidden = !hint.textContent;
    }
    function getCenter() {
      if (!geometry) return desiredCenter;
      const {vw, vh, x, y, w, h} = geometry;
      return {x: clamp((vw / 2 - x) / w, 0, 1), y: clamp((vh / 2 - y) / h, 0, 1)};
    }
    function layout(at = getCenter(), pointer) {
      const source = natural || (supportsRegion() ? metadata : null);
      if (!(source?.width > 0 && source?.height > 0)) { syncZoomControls(); return; }
      const vw = stage.clientWidth, vh = stage.clientHeight;
      const fit = Math.max(0.000001, Math.min((vw - 54) / source.width, (vh - 54) / source.height));
      if (focusBox) {
        const boxWidth = (focusBox.x1 - focusBox.x0) * source.width * fit;
        const boxHeight = (focusBox.y1 - focusBox.y0) * source.height * fit;
        const comfortableZoom = clamp(Math.min(vw * 0.58 / boxWidth, vh * 0.52 / boxHeight), REGION_ZOOM, 20);
        const wholeMarkZoom = Math.min((vw - 12) / boxWidth, (vh - 12) / boxHeight);
        zoom = Math.max(1, Math.min(comfortableZoom, wholeMarkZoom));
        focusBox = null;
      }
      const w = source.width * fit * zoom, h = source.height * fit * zoom;
      const x = (pointer?.x ?? vw / 2) - (at?.x ?? 0.5) * w;
      const y = (pointer?.y ?? vh / 2) - (at?.y ?? 0.5) * h;
      geometry = {vw, vh, w, h, x: bounds(x, w, vw), y: bounds(y, h, vh)};
      desiredCenter = null;
      draw();
    }
    function syncZoomControls() {
      document.getElementById('zoom-value').textContent = `${Math.round(zoom * 100)}%`;
      test('btn-zoom-out').disabled = zoom <= MIN_ZOOM;
      test('btn-zoom-in').disabled = zoom >= MAX_ZOOM;
      test('btn-zoom-fit').disabled = zoom === 1;
      test('btn-zoom-ten').disabled = zoom === 10;
      stage.classList.toggle('can-pan', zoom > 1);
    }
    function cancelPending() {
      clearTimeout(requestTimer);
      requestTimer = null;
      if (pending) {
        pending.controller.abort();
        pending = null;
      }
    }
    function clearPlaced() {
      if (!placed) return;
      region.hidden = true;
      region.removeAttribute('src');
      URL.revokeObjectURL(placed.url);
      placed = null;
    }
    function windowBox(padding = 0) {
      const {vw, vh, x, y, w, h} = geometry;
      return {
        x0: clamp((-x - padding * vw) / w, 0, 1),
        y0: clamp((-y - padding * vh) / h, 0, 1),
        x1: clamp((vw - x + padding * vw) / w, 0, 1),
        y1: clamp((vh - y + padding * vh) / h, 0, 1),
      };
    }
    function sameScale(spec) {
      return Math.abs(spec.scale / geometry.w - 1) < 0.0001;
    }
    function covers(spec, area) {
      const eps = 1e-8;
      return spec.box.x0 <= area.x0 + eps && spec.box.y0 <= area.y0 + eps
        && spec.box.x1 >= area.x1 - eps && spec.box.y1 >= area.y1 - eps;
    }
    function positionRegion(spec) {
      const {x0, y0, x1, y1} = spec.box;
      const {w, h} = geometry;
      Object.assign(region.style, {left: `${x0 * w}px`, top: `${y0 * h}px`,
        width: `${(x1 - x0) * w}px`, height: `${(y1 - y0) * h}px`});
    }
    async function fetchViewerResource(url, signal) {
      const retryUntil = performance.now() + 120_000;
      let retryDelay = 1000;
      while (!signal.aborted) {
        const response = await fetch(url, {credentials: 'same-origin', signal});
        if (response.ok) return response;
        const body = await response.json().catch(() => null);
        if (signal.aborted) return null;
        if (body?.error?.code !== 'VIEWER_BUSY' || performance.now() + retryDelay >= retryUntil) {
          throw new Error(body?.error?.message || '此区域暂时无法从原图加载');
        }
        if (needsRegion()) report('正在准备原图细节，请稍候…');
        await new Promise(resolve => {
          const finish = () => {
            clearTimeout(timer);
            signal.removeEventListener('abort', finish);
            resolve();
          };
          const timer = setTimeout(finish, retryDelay);
          signal.addEventListener('abort', finish, {once: true});
        });
        retryDelay = Math.min(retryDelay + 1000, 3000);
      }
      return null;
    }
    async function requestRegion() {
      requestTimer = null;
      if (!current() || !needsRegion() || !supportsRegion() || !geometry) return;
      const box = windowBox(0.15);
      const w = geometry.w * (box.x1 - box.x0), h = geometry.h * (box.y1 - box.y0);
      if (!(w > 0 && h > 0)) return;
      const limit = metadata.max_dimension || 2048;
      const pixels = metadata.max_pixels || limit * limit;
      const density = Math.min(window.devicePixelRatio || 1, limit / w, limit / h, Math.sqrt(pixels / (w * h)));
      const params = new URLSearchParams({
        ...box, width: String(Math.max(1, Math.floor(w * density))), height: String(Math.max(1, Math.floor(h * density))),
      });
      const task = {box, scale: geometry.w, controller: new AbortController()};
      pending = task;
      let url;
      try {
        const response = await fetchViewerResource(`${regionApi}?${params}`, task.controller.signal);
        if (!response || !current() || pending !== task) return;
        url = URL.createObjectURL(await response.blob());
        const decoded = new Image();
        decoded.src = url;
        await decoded.decode();
        if (!current() || pending !== task || !needsRegion() || !sameScale(task)) return;
        clearPlaced();
        placed = {...task, url};
        url = null;
        positionRegion(placed);
        region.src = placed.url;
        region.hidden = false;
        report('');
      } catch (error) {
        if (current() && pending === task && error.name !== 'AbortError') {
          report(`原图细节加载失败 · ${error.message || '可移动视图后重试，或恢复适配'}`);
        }
      } finally {
        if (url) URL.revokeObjectURL(url);
        if (pending === task) pending = null;
      }
    }
    function updateRegion() {
      const high = needsRegion();
      img.hidden = loadError || (high && (!metadata || supportsRegion()));
      if (!high) {
        cancelPending();
        clearPlaced();
        report('');
        return;
      }
      if (!supportsRegion()) {
        report(metadata ? '' : '正在读取原图…');
        return;
      }
      if (placed && !sameScale(placed)) clearPlaced();
      const area = windowBox();
      if (placed && covers(placed, area)) {
        cancelPending();
        report('');
        return;
      }
      if (pending && sameScale(pending) && covers(pending, area)) return;
      cancelPending();
      report('正在加载原图细节…');
      requestTimer = setTimeout(requestRegion, 70);
    }
    function draw() {
      const {w, h, x, y} = geometry;
      Object.assign(sheet.style, {width: `${w}px`, height: `${h}px`, left: `${x}px`, top: `${y}px`});
      syncZoomControls();
      showHint();
      updateRegion();
    }
    function zoomAt(value, pointer, reset = false) {
      const next = clamp(value, MIN_ZOOM, MAX_ZOOM);
      if (next === zoom && !reset) return;
      focusBox = null;
      const g = geometry;
      const at = reset ? {x: 0.5, y: 0.5} : g && pointer
        ? {x: (pointer.x - g.x) / g.w, y: (pointer.y - g.y) / g.h} : getCenter();
      zoom = next;
      layout(at, pointer);
    }
    function pan(dx, dy) {
      if (!geometry || zoom <= 1) return;
      const {x, y, w, h, vw, vh} = geometry;
      geometry.x = bounds(x + dx, w, vw);
      geometry.y = bounds(y + dy, h, vh);
      draw();
    }
    const observer = new ResizeObserver(() => { if (current()) layout(); });
    const session = {
      identity, getCenter, zoomAt,
      destroy() {
        destroyed = true;
        observer.disconnect();
        infoController.abort();
        cancelPending();
        clearPlaced();
      },
    };
    viewer = session;

    stage.addEventListener('wheel', event => {
      event.preventDefault();
      if (!event.deltaY) return;
      const rect = stage.getBoundingClientRect();
      const point = {x: event.clientX - rect.left, y: event.clientY - rect.top};
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? stage.clientHeight : 1;
      zoomAt(zoom * Math.exp(clamp(-event.deltaY * unit * 0.0025, -1.5, 1.5)), point);
    }, {passive: false});
    stage.addEventListener('pointerdown', event => {
      if (event.button !== 0 || zoom <= 1 || !geometry || drag) return;
      drag = {id: event.pointerId, x: event.clientX, y: event.clientY};
      stage.setPointerCapture(event.pointerId);
      stage.classList.add('is-panning');
    });
    stage.addEventListener('pointermove', event => {
      if (drag?.id !== event.pointerId) return;
      pan(event.clientX - drag.x, event.clientY - drag.y);
      drag.x = event.clientX; drag.y = event.clientY;
    });
    const endPan = event => {
      if (drag?.id !== event.pointerId) return;
      drag = null;
      stage.classList.remove('is-panning');
    };
    stage.addEventListener('pointerup', endPan);
    stage.addEventListener('pointercancel', endPan);
    stage.addEventListener('lostpointercapture', endPan);
    stage.addEventListener('keydown', event => {
      const moves = {ArrowLeft: [70, 0], ArrowRight: [-70, 0], ArrowUp: [0, 70], ArrowDown: [0, -70]};
      if (!moves[event.key]) return;
      event.preventDefault();
      pan(...moves[event.key]);
    });

    const imageLoaded = () => {
      if (!current()) return;
      natural = {width: img.naturalWidth, height: img.naturalHeight};
      loadError = false;
      layout();
    };
    const imageFailed = () => {
      if (!current()) return;
      loadError = true;
      img.hidden = true;
      showHint();
    };
    img.addEventListener('load', imageLoaded);
    img.addEventListener('error', imageFailed);
    if (img.complete) (img.naturalWidth ? imageLoaded : imageFailed)();
    observer.observe(stage);
    syncZoomControls();
    if (!regionApi) { metadata = {kind: 'unavailable'}; layout(); }
    else fetchViewerResource(`${regionApi}/info`, infoController.signal).then(response => response?.json()).then(info => {
      if (!current()) return;
      metadata = info;
      layout();
    }).catch(() => {
      if (!current()) return;
      metadata = {kind: 'unavailable', message: '暂时无法读取原图细节'};
      layout();
    });
  }

  function showProofSide(side, locate) {
    const proof = issueProof(currentIssue())?.[side];
    if (!proof?.image_url) return;
    comparing = side === 'right';
    const anchored = locate && !bboxPercent(proof.bbox).bad && !proof.bbox_out_of_range;
    resetViewer(anchored ? REGION_ZOOM : 1, anchored);
    renderResults();
  }

  async function loadResults(options = {}) {
    if (!project?.version_id) throw new Error('没有当前版本');
    const params = new URLSearchParams({
      version_id: project.version_id,
      unreadable_count: String(options.unreadable_count ?? project.unreadable?.length ?? 0),
      incomplete_count: String(options.incomplete_count || 0),
      out_of_scope_count: String(options.out_of_scope_count || 0),
      cost_paused: options.cost_paused ? 'true' : 'false',
      revised: options.revised || project.revised ? 'true' : 'false',
    });
    const bundle = await api(`/api/issues?${params}`);
    const pending = await api(`/api/pending?version_id=${encodeURIComponent(project.version_id)}`);
    // Recognition can rename sheets and revision matching can carry old sheets forward.
    // Read this exact version, including when viewing a historical result.
    const refreshedProject = await api('/api/history/open', json('POST', {
      project_id: project.project_id, version_id: project.version_id
    }));
    bundle.failed = options.status === '失败';
    bundle.partial = options.outcome === 'partial' || bundle.outcome === 'partial' || !!bundle.incomplete_count || !!bundle.unreadable_count || !!bundle.out_of_scope_count;
    bundle.none_recognized = bundle.partial && Number(bundle.drawing_count) > 0 && bundle.recognized_count === 0;
    if (options.revised || project.revised) {
      const comparison = await api(`/api/versions/${encodeURIComponent(project.version_id)}/revision`);
      bundle.revision_summary = comparison.summary;
    }
    bundle.items.sort ( (a, b) => Number(a.diff === '已解决' || a.review_status === '排除') - Number(b.diff === '已解决' || b.review_status === '排除'));
    bundle.pending_count = pending.pending_count || 0;
    bundle.unsure_count = Math.max(0, (pending.incomplete_pending_count || 0) - bundle.pending_count);
    bundle.flow_incomplete_count = Array.isArray(bundle.incomplete_items) ? bundle.incomplete_items.filter(item => !['unreadable','out_of_scope'].includes(item.category)).length : Math.max(0, (bundle.incomplete_count || 0) - (pending.incomplete_pending_count || 0));
    project = {...project, ...refreshedProject, revised: !!refreshedProject.is_revision};
    resultBundle = bundle;
    confirmBundle = pending;
    if (!selectedIssueId || !bundle.items.some(item => item.id === selectedIssueId)) {
      selectedIssueId = bundle.items.find(isActiveDifference)?.id || null;
    }
    if (!selectedConfirmId || !(pending.items || []).some(item => item.id === selectedConfirmId)) {
      selectedConfirmId = pending.items?.[0]?.id || null;
    }
    editingConfirmId = null;
    comparing = false;
    resetViewer();
  }
  async function refreshResultProjection(bundle, updateIssues = false) {
    const request = ++projectionRequest;
    const versionId = project?.version_id;
    const params = new URLSearchParams({version_id: versionId});
    const isCurrent = () => resultBundle === bundle && project?.version_id === versionId && projectionRequest === request;
    try {
      const fresh = await api(`/api/issues?${params}`);
      if (!isCurrent()) return;
      bundle.verification = fresh.verification;
      bundle.comparison_summary = fresh.comparison_summary;
      bundle.comparison_details = fresh.comparison_details;
      bundle.unit_declarations = fresh.unit_declarations;
      if (updateIssues) {
        bundle.items = fresh.items;
        bundle.incomplete_items = fresh.incomplete_items;
        bundle.flow_incomplete_count = (fresh.incomplete_items || []).filter(item => !['unreadable', 'out_of_scope'].includes(item.category)).length;
      }
    } catch (_) {
      if (isCurrent()) toast('无法更新核验范围，重新打开结果可刷新', true);
    } finally {
      if (isCurrent()) {
        bundle.refreshing_verification = false;
        if (state === 'results') renderResults();
      }
    }
  }
  async function setReviewStatus(status) {
    if (project?.can_continue === false) { toast('请打开该项目最新版本后复核', true); return; }
    const issue = currentIssue();
    if (!issue) return;
    const toggle = issue.review_status === status;
    const bundle = resultBundle;
    const updated = await api(`/api/issues/${encodeURIComponent(issue.id)}`, json('PATCH', {review_status: status, review_note: '', toggle}));
    if (resultBundle !== bundle) return;
    const idx = bundle.items.findIndex(item => item.id === updated.id);
    if (activeTab === 'issues' && currentIssue()?.id === updated.id) selectedIssueId = updated.id;
    if (idx >= 0) resultBundle.items[idx] = {...resultBundle.items[idx], ...updated};
    resultBundle.items = [...resultBundle.items].sort((a, b) => {
      const ae = a.review_status === '排除' || a.diff === '已解决' ? 1 : 0;
      const be = b.review_status === '排除' || b.diff === '已解决' ? 1 : 0;
      if (ae !== be) return ae - be;
      return String(a.main_label).localeCompare(String(b.main_label), 'zh') || String(a.id).localeCompare(String(b.id));
    });
    const affectsScope = isActiveDifference(issue) !== isActiveDifference(updated);
    if (affectsScope && !bundle.items.some(isActiveDifference)) {
      bundle.verification = null;
      bundle.refreshing_verification = true;
    }
    if (state === 'results') renderResults();
    if (affectsScope) refreshResultProjection(bundle);
  }

  async function openSettings(focusModel = false) {
    closeDialog();
    try {
      config = await api('/api/settings');
      if (state !== 'settings') returnState = state;
      cleared.clear();
      state = 'settings';
      render();
      if (focusModel) test('in-model-provider').focus();
    } catch (error) { toast(message(error), true); }
  }
  function preset(id) { return config.presets.find(item => item.id === id); }
  function providerInput(role) { return test(role === 'primary' ? 'in-model-provider' : 'in-model-provider-review'); }
  function renderSettings() {
    main.innerHTML = `<section class="settings-page" data-testid="page-settings"><div class="settings-card"><div class="settings-header">
      <button type="button" class="back-button" data-action="settings-back" data-testid="btn-settings-back"><span class="symbol" aria-hidden="true">arrow_back</span>返回</button><h1>设置</h1></div>
      <form id="settings-form" novalidate><fieldset class="settings-section"><legend>模型</legend><div class="model-slots">
      ${['primary','review'].map(role => {
        const slot = config[`${role}_model`];
        return `<div class="model-slot"><h2>${role === 'primary' ? '主模型' : '复核模型（可选）'}</h2><label for="provider-${role}">服务商</label>
          <select id="provider-${role}" data-testid="${role === 'primary' ? 'in-model-provider' : 'in-model-provider-review'}">${config.presets.map(item => `<option value="${esc(item.id)}" ${item.id === (slot?.provider || config.presets[0].id) ? 'selected' : ''}>${esc(item.display_name)}</option>`).join('')}</select>
          <div class="field-group"><label for="key-${role}">模型密钥</label><div class="key-row"><input id="key-${role}" data-testid="in-model-key-${role}" type="password" autocomplete="off" placeholder="${slot?.credential_ref ? '已保存；留空保留密钥' : '请输入模型密钥'}">
          <button type="button" class="small-button" data-action="test-${role}" data-testid="btn-test-model-${role}">测试连接</button></div></div>
          <div class="field-group address-field" id="address-field-${role}"><label for="url-${role}">本地模型地址</label><input id="url-${role}" data-testid="in-model-base-url-${role}" type="password" autocomplete="off" value="${esc(slot?.base_url || '')}" placeholder="请输入模型地址"></div>
          <div class="field-group" id="custom-field-${role}"><label for="custom-${role}">模型型号</label><input id="custom-${role}" data-testid="in-model-name-${role}" value="${esc(slot?.model || '')}"></div>
          <p class="connection-message" data-testid="test-result-${role}" role="status"></p>
          ${slot ? `<a class="clear-slot" href="#" data-action="clear-${role}">清空${role === 'primary' ? '主模型' : '复核模型'}</a>` : ''}</div>`;
      }).join('')}</div><p class="settings-help" data-testid="model-display">${esc(config.model_display || '主模型：未配置／复核模型：未配置')}</p>
      <p class="settings-help">测试连接前会保存当前设置，请先填写费用上限。</p></fieldset>
      <fieldset class="settings-section"><legend>费用上限</legend><label for="fee-cap">每月上限（元）</label><input id="fee-cap" data-testid="in-fee-cap" type="number" min="0.01" step="0.01" value="${esc(config.monthly_limit_yuan)}" required><p class="settings-help">接近上限时提醒，超出后暂停审查。</p></fieldset>
      <fieldset class="settings-section"><legend>容差</legend><div class="tolerance-fields"><div><label for="dimension">尺寸允许偏差（毫米）</label><input id="dimension" data-testid="in-tolerance" type="number" min="0" step="any" value="${esc(config.dimension_tolerance_mm)}" required></div>
      <div><label for="elevation">标高允许偏差（米）</label><input id="elevation" data-testid="in-tolerance-elevation" type="number" min="0" step="any" value="${esc(config.elevation_tolerance_m)}" required></div></div></fieldset>
      <p class="form-message" data-testid="settings-message" role="alert"></p><div class="settings-actions"><button class="small-button accent" type="submit" data-testid="btn-settings-save">保存</button></div></form></div></section>`;
    ['primary','review'].forEach(role => {
      updateSlotFields(role);
      providerInput(role).addEventListener('change', () => {
        // 更换服务商不能把原服务商已存密钥静默发往新地址。
        test(`in-model-key-${role}`).value = '';
        test(`in-model-base-url-${role}`).value = '';
        cleared.add(role);
        updateSlotFields(role);
        test(`test-result-${role}`).textContent = '';
      });
      for (const input of document.querySelectorAll(`.model-slot ${role === 'primary' ? '#key-primary, #url-primary, #custom-primary' : '#key-review, #url-review, #custom-review'}`)) {
        input.addEventListener('input', () => { test(`test-result-${role}`).textContent = ''; });
      }
    });
    document.getElementById('settings-form').addEventListener('submit', event => { event.preventDefault(); saveSettings(); });
  }
  function updateSlotFields(role) {
    const id = providerInput(role).value;
    document.getElementById(`address-field-${role}`).hidden = !preset(id).requires_base_url;
    document.getElementById(`custom-field-${role}`).hidden = id !== 'custom';
  }
  function settingsPayload() {
    const fee = test('in-fee-cap');
    if (!fee.value.trim()) { fee.focus(); throw {code:'MONTHLY_LIMIT_REQUIRED'}; }
    if (!document.getElementById('settings-form').reportValidity()) return null;
    const payload = {monthly_limit_yuan:Number(fee.value), dimension_tolerance_mm:Number(test('in-tolerance').value), elevation_tolerance_m:Number(test('in-tolerance-elevation').value)};
    for (const role of ['primary','review']) {
      const provider = providerInput(role).value;
      const selected = preset(provider);
      const old = config[`${role}_model`];
      const key = test(`in-model-key-${role}`).value.trim();
      const base = test(`in-model-base-url-${role}`).value.trim();
      const keep = !cleared.has(role) && old?.provider === provider;
      if (key || (keep && old) || (selected.requires_base_url && base)) {
        if (selected.requires_base_url && !base) throw {code:'LOCAL_URL_REQUIRED'};
        payload[`${role}_model`] = {provider, model: provider === 'custom' ? test(`in-model-name-${role}`).value.trim() : (keep && old?.model ? old.model : selected[`default_${role}_model`]),
          base_url: selected.requires_base_url ? base : selected.base_url, credential_ref: keep ? (old?.credential_ref || '') : ''};
        if (key) payload[`${role}_api_key`] = key;
      } else payload[`${role}_model`] = null;
    }
    if (payload.review_model && !payload.primary_model) throw {code:'PRIMARY_REQUIRED'};
    return payload;
  }
  function lockSettings(locked) {
    settingsBusy = locked;
    document.querySelectorAll('#settings-form input, #settings-form select, #settings-form button, [data-testid="btn-settings-back"]').forEach(element => { element.disabled = locked; });
    test('btn-settings').disabled = locked;
  }
  async function persistForm() {
    test('settings-message').textContent = '';
    const payload = settingsPayload();
    if (!payload) return false;
    lockSettings(true);
    try {
      config = await api('/api/settings', json('PATCH', payload));
      cleared.clear();
      ['primary','review'].forEach(role => {
        test(`in-model-key-${role}`).value = '';
        test(`in-model-key-${role}`).placeholder = config[`${role}_model`]?.credential_ref ? '已保存；留空保留密钥' : '请输入模型密钥';
      });
      test('model-display').textContent = config.model_display || '主模型：未配置／复核模型：未配置';
      return true;
    } finally { lockSettings(false); }
  }
  async function saveSettings() {
    if (settingsBusy) return;
    try {
      if (!await persistForm()) return;
      state = returnState;
      render();
      toast('设置已保存');
    } catch (error) { if (test('settings-message')) test('settings-message').textContent = message(error); }
  }
  async function testModel(role) {
    if (settingsBusy) return;
    const result = test(`test-result-${role}`);
    result.classList.add('is-error');
    const key = test(`in-model-key-${role}`);
    const stored = !cleared.has(role) && config[`${role}_model`]?.provider === providerInput(role).value && config[`${role}_model`]?.credential_ref;
    if (!key.value.trim() && !stored) { result.textContent = failures.CREDENTIAL_REQUIRED; key.focus(); return; }
    if (preset(providerInput(role).value).requires_base_url && !test(`in-model-base-url-${role}`).value.trim()) {
      result.textContent = failures.LOCAL_URL_REQUIRED; test(`in-model-base-url-${role}`).focus(); return;
    }
    try {
      if (!await persistForm()) return;
      lockSettings(true);
      result.textContent = '正在测试连接';
      const response = await api('/api/settings/test-connection', json('POST', {role, use_image:true}));
      result.textContent = message(response);
      result.classList.toggle('is-error', response.code !== 'CONNECTION_OK');
    } catch (error) { result.textContent = message(error); }
    finally { lockSettings(false); }
  }
  async function chooseFiles(folder = false) {
    if (busy || ['settings','reviewing'].includes(state)) return;
    if (window.pywebview?.api?.choose_import) {
      try {
        const selection = await window.pywebview.api.choose_import(folder);
        if (selection?.selection_id) await importFiles(null, selection);
        else if (selection?.error) toast('暂时无法选择文件，请重试', true);
      } catch (_) { toast('暂时无法选择文件，请重试', true); }
    } else document.getElementById(folder ? 'folder-input' : 'file-input').click();
  }
  async function importFiles(files, selection = null) {
    if (busy || !config || ['settings','reviewing'].includes(state) || (!selection && !files?.length)) return;
    const previousState = state;
    busy = true; state = 'importing';
    importProgress = {stage: selection ? 'processing' : 'upload', percent: null};
    render();
    try {
      let revision = null;
      if (importMode === 'revision' && project?.version_id) revision = await api(`/api/versions/${encodeURIComponent(project.version_id)}/revisions`, json('POST', {}));
      const url = '/api/import' + (revision ? `?version_id=${encodeURIComponent(revision.version_id)}` : '');
      let submitted;
      if (selection) submitted = await api(url, json('POST', selection));
      else {
        const form = new FormData();
        for (const file of files) form.append('files', file, file.webkitRelativePath || file.name);
        submitted = await uploadImport(url, form);
      }
      importProgress = {stage:'processing', phase:'正在准备处理图纸', percent:null}; render();
      let job;
      do {
        await new Promise(resolve => setTimeout(resolve, 250));
        job = await api(`/api/import/${encodeURIComponent(submitted.job_id)}`);
        importProgress = {...job, stage:'processing'}; render();
      } while (['queued','running'].includes(job.status));
      if (job.status !== 'completed' || !job.result?.version_id) throw new Error(job.error?.message || '导入未完成，请检查文件后重试');
      if (job.result?.version_id) {
        if (revision && job.status === 'completed') await api(`/api/versions/${encodeURIComponent(revision.version_id)}/revision/prepare`, json('POST', {}));
        if (revision && job.status !== 'completed') throw new Error('修订导入未完成');
        project = {...job.result, revised: !!revision};
        localStorage.setItem('drawcheck-current', JSON.stringify({project_id: project.project_id, version_id: project.version_id}));
        resultBundle = null; confirmBundle = null;
        state = 'imported';
        render();
      }
      if (job.error) toast(job.error.message, true);
      else toast('导入已结束');
    } catch (error) { state = previousState; render(); toast(error.message || '导入未完成，请检查文件后重试', true); }
    finally { busy = false; importMode = 'new'; syncHeader(); }
  }
  function rename(element) {
    if (element.querySelector('input')) return;
    const sheet = project.drawings.find(item => item.unit_id === element.dataset.unit);
    element.innerHTML = `<input class="rename-input" data-testid="in-sheet-name" aria-label="图名" maxlength="500" value="${esc(sheet.name)}">`;
    const input = element.firstElementChild;
    input.focus(); input.select();
    input.addEventListener('keydown', async event => {
      if (event.key === 'Escape') { render(); return; }
      if (event.key !== 'Enter' || event.isComposing || input.disabled) return;
      event.preventDefault();
      if (!input.value.trim()) { toast('请填写图名', true); return; }
      input.disabled = true;
      try {
        const updated = await api(`/api/units/${encodeURIComponent(sheet.unit_id)}`, json('PATCH', {name:input.value.trim()}));
        sheet.name = updated.name;
        render();
        toast('图名已保存');
      } catch (_) { input.disabled = false; toast('图名未保存，请重试', true); }
    });
  }
  function closeDialog() {
    overlay.querySelector('dialog')?.close();
    overlay.textContent = '';
  }
  async function precheck(retryUnitId) {
    // 生成告知前只检查；用户确认后才创建或续跑任务。
    const result = await api('/api/settings/precheck', json('POST', {
      disclosure_version_id: project.version_id, retry_unit_id: retryUnitId
    }));
    if (!result.ok) {
      if (['NO_MODEL','LIMIT_REQUIRED','COST_EXCEEDED'].includes(result.code)) await openSettings(true);
      toast(message(result), true);
      return null;
    }
    if (result.warning_message) toast(result.warning_message, true);
    return result;
  }
  async function requestReview(retryUnitId, drawingLabel) {
    if (reviewPending || !project?.drawings.length || !['imported','results'].includes(state)) return;
    reviewPending = true; syncHeader();
    try {
      const checked = await precheck(retryUnitId);
      if (!checked) return;
      const providers = checked.providers.join('、');
      const copy = retryUnitId ? `<p class="disclosure-copy">将把「${esc(drawingLabel)}」这一张图发送给主模型（${esc(providers)}）重新识别。</p>
        <p class="disclosure-note">大幅图纸可能分区域发送。识别后会在本机更新相关对照。</p>`
        : `<p class="disclosure-copy">整张图会发给你配置的模型（${esc(providers)}），用于找出图纸之间的不一致。</p>
        <p class="disclosure-note">${checked.model_status === 'dual' ? '范围、相关图和双模型复核会自动完成' : '范围、相关图会自动完成'}</p>
        <p class="disclosure-note">大幅图纸会分区域识别，可能增加用时和模型费用。</p>
        <p class="disclosure-note">符合对照条件的图纸文字和位置也会发给主模型做语义分析，单独计入模型费用。</p>
        ${checked.model_status === 'single' ? '<p class="disclosure-note">只配了一个模型，本次结果未经第二个模型复核。</p>' : ''}`;
      overlay.innerHTML = `<dialog data-testid="dlg-disclosure" aria-label="审查前告知">${copy}
        <div class="dialog-actions"><button type="button" class="small-button" data-testid="btn-disclosure-cancel" data-action="cancel">取消</button><button type="button" class="small-button solid" data-testid="btn-disclosure-ok" data-action="begin">开始</button></div></dialog>`;
      const dialog = overlay.querySelector('dialog');
      dialog.dataset.disclosureId = checked.disclosure_id;
      if (retryUnitId) dialog.dataset.retryUnitId = retryUnitId;
      dialog.showModal();
    } catch (error) { toast(message(error), true); }
    finally { reviewPending = false; syncHeader(); }
  }
  async function beginReview() {
    if (reviewPending) return;
    reviewPending = true;
    const previousState = state;
    const {disclosureId, retryUnitId} = overlay.querySelector('dialog').dataset;
    test('btn-disclosure-ok').disabled = true;
    try {
      closeDialog();
      state = 'reviewing'; progressValue = 0; reviewTask = null; render();
      const submitted = await api(`/api/versions/${encodeURIComponent(project.version_id)}/tasks`, json('POST', {
        disclosure_id: disclosureId, retry_unit_id: retryUnitId
      }));
      if (!submitted.task_id) throw new Error('任务未创建');
      let task = submitted;
      do {
        reviewTask = task;
        progressValue = Math.max(0, Math.min(100, Number(task.percent) || 0));
        render();
        if (!['待开始', '进行中', 'queued', 'running'].includes(task.status)) break;
        await new Promise(resolve => setTimeout(resolve, 500));
        task = await api(`/api/tasks/${encodeURIComponent(submitted.task_id)}`);
      } while (true);
      if (!['已完成', '暂停', '失败'].includes(task.status)) throw new Error('审查状态未知');
      await loadResults({...task, revised: project.revised});
      state = 'results'; render();
      if (task.status !== '已完成') toast(task.message || '审查未完成，请检查后重试', true);
    } catch (error) {
      state = previousState; render();
      if (['NO_MODEL','LIMIT_REQUIRED','COST_EXCEEDED'].includes(error.code)) await openSettings(true);
      toast(error.message || '审查未完成，请检查后重试', true);
    } finally { reviewPending = false; syncHeader(); }
  }
  function requestRemove(element) {
    const kind = element.dataset.action;
    const body = kind === 'remove-unit' ? {unit_ids:[element.dataset.id]} : kind === 'remove-original' ? {original_ids:[element.dataset.id]} : {unreadable_indices:[Number(element.dataset.index)]};
    const target = kind === 'remove-unit' ? '这张图纸' : kind === 'remove-original' ? '整个文件及其中所有图纸和相关记录' : '这条读不了记录';
    overlay.innerHTML = `<dialog data-testid="dlg-remove" aria-label="确认移除"><h2 class="dialog-title">移除${esc(target)}？</h2><p>${esc(element.dataset.name)}</p><p>将生成调整后的新版本，需要重新审查。原图和历史记录保留。</p><div class="dialog-actions"><button type="button" class="small-button" data-action="cancel">取消</button><button type="button" class="small-button solid" data-action="remove-confirm">确认移除</button></div></dialog>`;
    overlay.querySelector('dialog').showModal();
    overlay.querySelector('[data-action="remove-confirm"]').addEventListener('click', async event => {
      event.stopPropagation();
      if (busy) return;
      busy = true; event.currentTarget.disabled = true; syncHeader();
      try {
        const updated = await api(`/api/versions/${encodeURIComponent(project.version_id)}/remove-drawings`, json('POST', body));
        project = {...updated, revised:false};
        project.drawings ||= []; project.unreadable ||= []; project.files ||= [];
        localStorage.setItem('drawcheck-current', JSON.stringify({project_id: project.project_id, version_id: project.version_id}));
        resultBundle = null; confirmBundle = null; selectedIssueId = null; selectedConfirmId = null;
        state = 'imported'; closeDialog(); render(); toast('已移除，请重新审查');
      } catch (error) { closeDialog(); toast(error.message || '移除未完成，请重试', true); }
      finally { busy = false; syncHeader(); }
    }, {once:true});
  }
  function requestImport() {
    if (project?.can_continue === false) { toast('请先打开该项目最新版本', true); return; }
    if (!project?.version_id || !['imported', 'results'].includes(state)) { importMode = 'new'; chooseFiles(); return; }
    overlay.innerHTML = `<dialog data-testid="dlg-import-mode" aria-label="导入图纸"><h2 class="dialog-title">导入图纸</h2><p>选择如何导入这批图纸。</p><div class="dialog-actions"><button type="button" class="small-button" data-action="cancel">取消</button><button type="button" class="small-button" data-action="import-new">新项目</button><button type="button" class="small-button solid" data-action="import-revision">补充或替换图纸</button></div></dialog>`;
    overlay.querySelector('dialog').showModal();
  }
  async function openHistory() {
    try {
      const history = await api('/api/history');
      overlay.innerHTML = `<dialog class="history-drawer" data-testid="dlg-history" aria-label="历史记录"><div class="dialog-top"><h2 class="dialog-title">历史记录</h2><button type="button" class="small-button" data-action="cancel">关闭</button></div>${history.items.length ? `<ul class="history-records">${history.items.map(item => `<li><div><time>${esc(item.created_at || '')}</time><p>${esc(item.project_name)} · ${esc(item.version_name || '')}${item.is_revision ? ' · 修订版' : ''}</p></div><div><span class="history-count">${item.issue_count == null ? '问题数未记录' : `${esc(item.issue_count)} 个问题`}</span><br><button class="small-button" type="button" data-action="history-open" data-project="${esc(item.project_id)}" data-version="${esc(item.version_id)}">打开</button></div></li>`).join('')}</ul>` : '<p class="history-empty">暂无历史记录</p>'}</dialog>`;
      overlay.querySelector('dialog').showModal();
    } catch (_) { toast('历史记录读取失败，请重试', true); }
  }
  async function openProject(projectId, versionId, continueLatest = false) {
    const body = continueLatest ? {project_id: projectId, source_version_id: versionId} : {project_id: projectId, version_id: versionId};
    const opened = await api('/api/history/open', json('POST', body));
    project = {...opened, revised: !!opened.is_revision};
    project.unreadable ||= []; project.drawings ||= []; project.files ||= [];
    localStorage.setItem('drawcheck-current', JSON.stringify({project_id: opened.project_id, version_id: opened.version_id}));
    selectedIssueId = null; selectedConfirmId = null; activeTab = 'issues';
    if (opened.task_id) {
      const task = await api(`/api/tasks/${encodeURIComponent(opened.task_id)}`);
      if (['已完成', '暂停', '失败'].includes(task.status)) {
        await loadResults({...task, revised: project.revised});
        state = 'results';
      } else { state = 'imported'; toast('上次审查未完成，点击开始审查继续', true); }
    } else state = 'imported';
    render();
    if (opened.can_continue === false) {
      overlay.innerHTML = `<dialog data-testid="dlg-history-readonly" aria-label="历史版本"><h2 class="dialog-title">已打开历史版本</h2><p>历史版本可查看；继续审查或复核请打开该项目的最新版本。</p><div class="dialog-actions"><button class="small-button" data-action="cancel">查看此版本</button><button class="small-button solid" data-action="history-latest" data-project="${esc(projectId)}">打开最新版本</button></div></dialog>`;
      overlay.querySelector('dialog').showModal();
    }
  }
  function requestExport() {
    const count = confirmBundle?.pending_count || 0;
    if (!count) { exportResults(); return; }
    overlay.innerHTML = `<dialog data-testid="dlg-export-warning" aria-label="导出提醒"><h2 class="dialog-title">还有 ${esc(count)} 项待确认</h2><p>这些内容将在导出中标为未完成。</p><div class="dialog-actions"><button class="small-button" data-action="cancel">返回确认</button><button class="small-button solid" data-action="export-run">继续导出</button></div></dialog>`;
    overlay.querySelector('dialog').showModal();
  }
  async function exportResults() {
    if (!project?.version_id || busy) return;
    busy = true; syncHeader();
    try {
      const result = await api(`/api/versions/${encodeURIComponent(project.version_id)}/exports`, json('POST', {}));
      overlay.innerHTML = `<dialog data-testid="dlg-export" aria-label="导出完成"><h2 class="dialog-title">导出完成</h2>${result.pending_count ? `<p>还有 ${esc(result.pending_count)} 项待确认，导出中已标明。</p>` : ''}<ul class="history-records">${(result.files || []).map(file => `<li><a href="${esc(file.url)}" download>${esc(file.name)}</a><button type="button" class="small-button" data-action="reveal-export" data-id="${esc(file.id)}">打开所在文件夹</button></li>`).join('')}</ul><div class="dialog-actions"><button type="button" class="small-button" data-action="cancel">关闭</button></div></dialog>`;
      overlay.querySelector('dialog').showModal();
    } catch (error) { toast(error.message || '导出未完成，请重试', true); }
    finally { busy = false; syncHeader(); }
  }
  document.addEventListener('click', event => {
    const element = event.target.closest('[data-action]');
    if (!element) return;
    event.preventDefault();
    const action = element.dataset.action;
    if (settingsBusy || busy || state === 'importing' || state === 'reviewing') return;
    if (action === 'settings') { if (state !== 'settings') openSettings(); }
    else if (action === 'settings-back') { state = returnState; render(); }
    else if (action === 'import') requestImport();
    else if (action === 'import-new' || action === 'import-revision') { importMode = action === 'import-revision' ? 'revision' : 'new'; closeDialog(); chooseFiles(); }
    else if (action === 'folder') chooseFiles(true);
    else if (action === 'review') requestReview();
    else if (action === 'retry-page') requestReview(element.dataset.id, element.dataset.name);
    else if (action === 'rename') rename(element);
    else if (action === 'manage-drawings') { state = 'managing'; render(); }
    else if (action === 'back-results') { state = 'results'; render(); }
    else if (['remove-unit','remove-original','remove-unreadable'].includes(action)) requestRemove(element);
    else if (action === 'cancel') closeDialog();
    else if (action === 'begin') beginReview();
    else if (action === 'history') openHistory();
    else if (action === 'history-latest') { const projectId = element.dataset.project; closeDialog(); openProject(projectId, undefined).catch(() => toast('最新版本打开失败，请重试', true)); }
    else if (action === 'history-open') { const {project: projectId, version: versionId} = element.dataset; closeDialog(); openProject(projectId, versionId, true).catch(() => toast('历史记录打开失败，请重试', true)); }
    else if (action === 'export') requestExport();
    else if (action === 'export-run') { closeDialog(); exportResults(); }
    else if (action === 'reveal-export') api(`/api/exports/${encodeURIComponent(element.dataset.id)}/reveal`, json('POST', {})).catch(error => toast(error.message || '无法打开所在文件夹', true));
    else if (action === 'tab-drawings') { activeTab = 'drawings'; comparing = false; resetViewer(); renderResults(); }
    else if (action === 'select-drawing') { selectedDrawingId = element.dataset.id; resetViewer(); renderResults(); }
    else if (action === 'tab-issues') { activeTab = 'issues'; comparing = false; resetViewer(); editingConfirmId = null; renderResults(); }
    else if (action === 'proof-side') { showProofSide(element.dataset.side, true); }
    else if (action === 'auxiliary-proof') {
      const {unit, box, sheet, marker, text} = element.dataset;
      const bbox = JSON.parse(box);
      if (!unit || bboxPercent(bbox).bad) return;
      resetViewer(REGION_ZOOM, true);
      auxiliaryProof = {unit_id: unit, image_url: `/api/units/${encodeURIComponent(unit)}/image`, bbox, sheet, marker, text, key: box};
      renderResults();
    }
    else if (action === 'return-to-mark') { resetViewer(); renderResults(); }
    else if (action === 'tab-confirm') { activeTab = 'confirm'; comparing = false; resetViewer(); renderResults(); }
    else if (action === 'select-issue') { selectedIssueId = element.dataset.id; comparing = false; resetViewer(); renderResults(); }
    else if (action === 'select-confirm' || action === 'unverified-confirm') {
      activeTab = 'confirm';
      comparing = false;
      selectedConfirmId = element.dataset.id;
      const row = (confirmBundle?.items || []).find(item => item.id === selectedConfirmId);
      // 点已处理行重新展开填写；页签计数不加回。
      editingConfirmId = row && row.status !== '待确认' ? selectedConfirmId : null;
      resetViewer(); renderResults();
    }
    else if (action === 'confirm-unsure') processConfirmation(true);
    else if (action === 'compare') showProofSide(comparing ? 'left' : 'right', false);
    else if (action === 'zoom-in') viewer?.zoomAt(zoom * 1.25);
    else if (action === 'zoom-out') viewer?.zoomAt(zoom / 1.25);
    else if (action === 'zoom-ten') viewer?.zoomAt(10);
    else if (action === 'zoom-fit') viewer?.zoomAt(1, null, true);
    else if (action === 'review-status') setReviewStatus(element.dataset.status).catch(() => toast('复核未保存，请重试', true));
    else if (action.startsWith('test-')) testModel(action.slice(5));
    else if (action.startsWith('clear-')) {
      const role = action.slice(6);
      cleared.add(role);
      test(`in-model-key-${role}`).value = '';
      test(`in-model-key-${role}`).placeholder = '请输入模型密钥';
      test(`in-model-base-url-${role}`).value = '';
      test(`test-result-${role}`).textContent = '保存后清空此模型';
    }
  });
  document.addEventListener('keydown', event => {
    const element = event.target.closest('[data-action="rename"]');
    if (element && event.target === element && ['Enter',' '].includes(event.key)) { event.preventDefault(); rename(element); }
  });
  for (const id of ['file-input','folder-input']) document.getElementById(id).addEventListener('change', event => {
    const files = Array.from(event.target.files); event.target.value = ''; importFiles(files);
  });
  // 浏览器拖放以文件内容上传；支持目录逐层读取，不依赖宿主绝对路径。
  async function entryFiles(entry, prefix = '') {
    if (entry.isFile) return [await new Promise((resolve, reject) => entry.file(file => {
      Object.defineProperty(file, 'webkitRelativePath', {value:prefix + file.name}); resolve(file);
    }, reject))];
    const reader = entry.createReader();
    const files = [];
    while (true) {
      const entries = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (!entries.length) break;
      for (const child of entries) files.push(...await entryFiles(child, prefix + entry.name + '/'));
    }
    return files;
  }
  document.addEventListener('dragover', event => { event.preventDefault(); document.getElementById('drop-zone')?.classList.add('is-dragover'); });
  document.addEventListener('dragleave', () => document.getElementById('drop-zone')?.classList.remove('is-dragover'));
  document.addEventListener('drop', async event => {
    event.preventDefault();
    const entries = Array.from(event.dataTransfer.items || []).map(item => item.webkitGetAsEntry?.()).filter(Boolean);
    const fallback = Array.from(event.dataTransfer.files);
    document.getElementById('drop-zone')?.classList.remove('is-dragover');
    try { await importFiles(entries.length ? (await Promise.all(entries.map(entry => entryFiles(entry)))).flat() : fallback); }
    catch (_) { toast('暂时无法读取拖入的文件，请点导入重试', true); }
  });
  render();
  api('/api/settings').then(async value => { config = value; render(); const saved = localStorage.getItem('drawcheck-current'); if (saved) { try { const entry = JSON.parse(saved); await openProject(entry.project_id, entry.version_id); } catch (_) { toast('上次项目未能恢复，请从历史记录打开', true); } } }).catch(() => toast('暂时无法读取设置，请重新打开应用', true));
})();
