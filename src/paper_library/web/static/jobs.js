document.addEventListener("DOMContentLoaded", () => {
  const subjectData = document.getElementById("library-subjects");
  const librarySelect = document.getElementById("import-library");
  const subjectList = document.getElementById("import-subject");
  if (subjectData && librarySelect && subjectList) {
    let subjects = {};
    try {
      subjects = JSON.parse(subjectData.textContent || "{}");
    } catch (_) {
      subjects = {};
    }
    const refreshSubjects = () => {
      subjectList.replaceChildren(new Option("请选择研究主题", ""));
      for (const subject of subjects[librarySelect.value] || []) {
        const option = document.createElement("option");
        option.value = subject;
        option.textContent = subject;
        subjectList.append(option);
      }
    };
    librarySelect.addEventListener("change", refreshSubjects);
    refreshSubjects();
    const editor = document.getElementById("new-subject");
    const toggle = document.getElementById("add-subject");
    const name = document.getElementById("new-subject-name");
    const error = document.getElementById("subject-error");
    const save = document.getElementById("save-subject");
    const close = () => { editor.hidden = true; toggle.setAttribute("aria-expanded", "false"); error.textContent = ""; };
    toggle.addEventListener("click", () => {
      editor.hidden = false; toggle.setAttribute("aria-expanded", "true"); name.focus();
    });
    document.getElementById("cancel-subject").addEventListener("click", close);
    librarySelect.addEventListener("change", close);
    save.addEventListener("click", async () => {
      const library = librarySelect.value;
      if (!name.value.trim()) { error.textContent = "请输入主题名称"; name.focus(); return; }
      save.disabled = true;
      error.textContent = "";
      try {
        const body = new URLSearchParams({library_id: library, subject: name.value.trim(),
          management_token: document.querySelector('[name="management_token"]').value});
        const response = await fetch(editor.dataset.url, {method: "POST", body});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "保存失败，请重试");
        subjects[library] = [...new Set([...(subjects[library] || []), result.subject])].sort();
        if (librarySelect.value === library) {
          refreshSubjects(); subjectList.value = result.subject; name.value = ""; close(); subjectList.focus();
        }
      } catch (failure) { error.textContent = failure.message || "保存失败，请重试"; }
      finally { save.disabled = false; }
    });
    name.addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); if (!save.disabled) save.click(); }
      if (event.key === "Escape") { event.preventDefault(); close(); toggle.focus(); }
    });

    const libraryEditor = document.getElementById("new-library");
    const addLibrary = document.getElementById("add-library");
    const libraryName = document.getElementById("new-library-name");
    const libraryError = document.getElementById("library-error");
    const saveLibrary = document.getElementById("save-library");
    const closeLibrary = () => {
      libraryEditor.hidden = true;
      addLibrary.setAttribute("aria-expanded", "false");
      libraryError.textContent = "";
    };
    addLibrary.addEventListener("click", () => {
      if (!libraryEditor.hidden) { closeLibrary(); return; }
      close();
      libraryEditor.hidden = false;
      addLibrary.setAttribute("aria-expanded", "true");
      libraryName.focus();
    });
    toggle.addEventListener("click", closeLibrary);
    document.getElementById("cancel-library").addEventListener("click", () => {
      closeLibrary(); addLibrary.focus();
    });
    saveLibrary.addEventListener("click", async () => {
      if (saveLibrary.disabled) return;
      if (!libraryName.value.trim()) {
        libraryError.textContent = "请输入论文库名称"; libraryName.focus(); return;
      }
      saveLibrary.disabled = true;
      libraryError.textContent = "";
      try {
        const body = new URLSearchParams({name: libraryName.value.trim(),
          management_token: document.querySelector('[name="management_token"]').value});
        const response = await fetch(libraryEditor.dataset.url, {method: "POST", body});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "创建失败，请重试");
        const library = result.library;
        librarySelect.add(new Option(library.name, library.id));
        subjects[library.id] = [];
        librarySelect.value = library.id;
        librarySelect.dispatchEvent(new Event("change"));
        libraryName.value = "";
        closeLibrary(); librarySelect.focus();
      } catch (failure) {
        libraryError.textContent = failure.message || "创建失败，请重试";
      } finally { saveLibrary.disabled = false; }
    });
    libraryName.addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); if (!saveLibrary.disabled) saveLibrary.click(); }
      if (event.key === "Escape") { event.preventDefault(); closeLibrary(); addLibrary.focus(); }
    });
  }

  const card = document.querySelector(".job-card[data-job-url]");
  if (!card) return;

  const stateNode = card.querySelector("[data-job-state]");
  const progress = card.querySelector("[data-job-progress]");
  const progressLabel = card.querySelector("[data-job-progress-label]");
  const error = card.querySelector("[data-job-error]");
  const paperLink = card.querySelector("[data-paper-link]");
  const terminal = new Set(["completed", "completed_with_warnings", "failed", "cancelled"]);
  let timer = null;
  let failures = 0;
  let currentPayload = null;
  let suspended = false;
  const detailNode = card.querySelector('[data-job-detail]');
  const timingNode = card.querySelector('[data-job-timing]');
  const connectionNode = card.querySelector('[data-job-connection]');
  const stageNames = {queued: '排队', parsing: 'PDF 解析', repairing: '段落修复',
    enriching: '信息检索与修正', publishing: '发布入库', published: '已入库', translating: '翻译',
    structuring: '结构校对', guiding: '生成导读', completed: '完成', failed: '失败', cancelled: '已取消'};
  const stateNames = {queued: '等待处理', running: '处理中', completed: '已完成',
    completed_with_warnings: '已完成，需核对', failed: '任务失败', cancelled: '已取消'};
  const stages = ['queued', 'parsing', 'repairing',
    ...(card.dataset.enrich === 'true' ? ['enriching'] : []), 'publishing', 'structuring',
    ...(card.dataset.autoTranslate === 'true' ? ['translating'] : []), 'guiding', 'completed'];
  const stageList = card.querySelector('[data-job-stages]');
  const stageNodes = stages.map(stage => {
    const item = document.createElement('li'); item.textContent = stageNames[stage];
    stageList.append(item); return item;
  });
  const age = (stamp, now) => {
    const date = Date.parse(stamp || '');
    if (!Number.isFinite(date)) return null;
    const seconds = Math.max(0, Math.floor((now - date) / 1000));
    return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  };
  const renderTiming = () => {
    if (!currentPayload) return;
    const payload = currentPayload;
    const ended = terminal.has(payload.state);
    const now = ended ? Date.parse(payload.updated_at) || Date.now() : Date.now();
    const activity = payload.activity || {};
    const parts = [];
    const duration = age(payload.created_at, now);
    if (duration) parts.push(`${ended ? '总用时' : '已用时'} ${duration}`);
    if (!ended) {
      const stageAge = age(activity.stage_started_at, now);
      if (stageAge) parts.push(`本阶段 ${stageAge}`);
      const progressAge = age(activity.progressed_at, now);
      parts.push(progressAge ? `最近进展 ${progressAge}前` : '尚无处理进展记录');
      if (payload.state === 'running') {
        const heartbeat = Date.parse(payload.updated_at || '');
        parts.push(Number.isFinite(heartbeat) && now - heartbeat < 45000 ? '后台心跳正常' : '后台心跳暂未更新');
      }
    }
    timingNode.textContent = parts.join(' · ');
  };
  const clockTimer = window.setInterval(renderTiming, 1000);

  const toggleActions = (payload) => {
    for (const form of card.querySelectorAll("[data-job-action]")) {
      const action = form.dataset.jobAction;
      let visible = false;
      if (action === "cancel") visible = ["queued", "running"].includes(payload.state);
      if (action === "retry") visible = ["failed", "completed_with_warnings"].includes(payload.state);
      if (action === "translate") {
        visible = ["completed", "completed_with_warnings"].includes(payload.state) && Boolean(payload.paper_url) && card.dataset.autoTranslate === "false";
      }
      form.hidden = !visible;
    }
  };

  const render = (payload) => {
    currentPayload = payload;
    const title = card.querySelector('[data-job-title]');
    if (title && payload.title) title.textContent = payload.title;
    const filename = card.querySelector('[data-job-filename]');
    if (filename) { filename.textContent = payload.filename || '';filename.hidden = !payload.title || payload.title === payload.filename; }
    const library = card.querySelector('[data-job-library]');
    if (library && payload.library_name) library.textContent = payload.library_name;
    const outcomesNode = card.querySelector('[data-job-outcomes]');
    outcomesNode.replaceChildren();
    const resultNames = {parsing: '解析与入库', repairing: '段落修复', metadata: '论文信息核对', structure: '结构校对', translation: '翻译', guide: '导读'};
    const resultStates = {completed: '已完成', warning: '需核对', skipped: '未启用', running: '处理中', not_run: '未执行'};
    for (const [key, label] of Object.entries(resultNames)) {
      const outcome = payload.outcomes?.[key];
      const item = document.createElement('li');
      item.dataset.result = outcome?.status || 'not_run';
      const heading = document.createElement('span'), badge = document.createElement('span');
      heading.className = 'outcome-label';heading.textContent = label;
      badge.className = 'outcome-state';badge.textContent = outcome ? resultStates[outcome.status] || outcome.status : terminal.has(payload.state) ? '暂无核验记录' : '等待处理';
      item.append(heading, badge);
      if (outcome?.message) {
        const details = document.createElement('details'), summary = document.createElement('summary'), message = document.createElement('p');
        details.className = 'outcome-detail';summary.textContent = '处理详情';message.textContent = outcome.message;
        details.append(summary, message);item.append(details);
      }
      if (key === 'metadata' && Array.isArray(outcome?.candidates)) {
        const candidates = document.createElement('ul');
        candidates.className = 'metadata-candidates';
        for (const candidate of outcome.candidates.slice(0, 5)) {
          if (!candidate || typeof candidate.url !== 'string' || typeof candidate.title !== 'string') continue;
          let url;
          try { url = new URL(candidate.url); } catch (_) { continue; }
          const trusted = ['aclanthology.org', 'openreview.net', 'arxiv.org', 'api.crossref.org',
            'doi.org', 'nature.com', 'proceedings.iclr.cc'];
          if (url.protocol !== 'https:' || !trusted.includes(url.hostname) || url.username || url.password || url.port) continue;
          const row = document.createElement('li');
          const anchor = document.createElement('a');
          anchor.href = url.href;
          anchor.target = '_blank';
          anchor.rel = 'noopener noreferrer';
          anchor.textContent = candidate.title.slice(0, 240);
          row.append(anchor);
          candidates.append(row);
        }
        if (candidates.childElementCount) item.append(candidates);
      }
      outcomesNode.append(item);
    }
    const phase = ['failed', 'cancelled'].includes(payload.state) ? payload.activity?.phase || payload.stage : payload.stage;
    const warnings = Object.entries(payload.outcomes || {}).filter(([,value]) => value?.status === 'warning');
    const summary = card.querySelector('[data-job-summary]');
    if (summary) summary.textContent = payload.state === 'completed' ? '论文已就绪，可打开阅读；各阶段结果见下方。' :
      payload.state === 'completed_with_warnings' ? `${warnings.length || '部分'} 项处理结果需要核对，展开处理详情查看原因；修正后可重试。` : '';
    stateNode.textContent = terminal.has(payload.state) ? stateNames[payload.state] || payload.state : `${stateNames[payload.state] || payload.state} · ${stageNames[phase] || phase}`;
    const active = stages.indexOf(phase === 'published' ? 'publishing' : phase);
    stageNodes.forEach((node, index) => {
      node.classList.toggle('is-current', index === active);
      const outcomeKey = {parsing:'parsing', repairing:'repairing', enriching:'metadata', structuring:'structure', translating:'translation', guiding:'guide'}[stages[index]];
      node.dataset.result = outcomeKey ? payload.outcomes?.[outcomeKey]?.status || '' : '';
      // Stage position does not imply that optional steps actually ran.
      if (index === active) node.setAttribute('aria-current', 'step'); else node.removeAttribute('aria-current');
    });
    const activity = payload.activity || {};
    detailNode.textContent = terminal.has(payload.state) ? '' : activity.message ||
      ({queued: '等待后台执行', parsing: '正在解析 PDF，当前没有可靠的完成比例',
        repairing: '正在检查段落连续性', enriching: '正在检索并核对论文信息',
        structuring: '正在校对文本结构', publishing: '正在保存论文', guiding: '正在生成导读', translating: '正在准备或生成段落译文'}[payload.stage] || '');
    card.dataset.state = payload.state;
    const counts = payload.progress || {};
    const translation = payload.stage === 'translating' || terminal.has(payload.state) && Number(counts.total) > 0;
    const total = Math.max(0, Number(translation ? counts.total : activity.total) || 0);
    const done = translation ? (Number(counts.cached) || 0) + (Number(counts.translated) || 0) + (Number(counts.failed) || 0) : Number(activity.done) || 0;
    const completed = ['completed', 'completed_with_warnings'].includes(payload.state);
    progress.hidden = ['failed', 'cancelled'].includes(payload.state) && !total;
    if (total > 0) {
      progress.max = total;
      progress.value = Math.max(0, Math.min(done, total));
      progressLabel.textContent = `${completed ? '翻译处理' : '已处理'} ${done} / ${total} ${translation ? '段' : activity.unit || '项'}` +
        (translation && counts.failed ? ` · 失败 ${counts.failed} 段` : '');
    } else if (completed) {
      progress.max = 1; progress.value = 1;
      progressLabel.textContent = '任务已完成';
    } else {
      progress.removeAttribute('value');
      progressLabel.textContent = terminal.has(payload.state) ? stateNames[payload.state] :
        payload.state === 'queued' ? '等待处理' : '处理中，暂无法估算比例';
    }
    renderTiming();
    error.textContent = payload.error || "";
    error.hidden = !payload.error;
    if (payload.paper_url) {
      paperLink.href = payload.paper_url;
      paperLink.hidden = false;
    } else {
      paperLink.hidden = true;
    }
    toggleActions(payload);
  };

  const poll = async () => {
    if (suspended) return;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(card.dataset.jobUrl, { signal: controller.signal, cache: "no-store", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`status ${response.status}`);
      const payload = await response.json();
      failures = 0;
      connectionNode.hidden = true;
      render(payload);
      if (terminal.has(payload.state)) return;
    } catch (_) {
      failures += 1;
      connectionNode.textContent = '状态更新中断，正在重试；当前显示的是上次收到的状态。';
      connectionNode.hidden = false;
    } finally {
      window.clearTimeout(timeout);
    }
    if (suspended) return;
    timer = window.setTimeout(poll, failures ? 5000 : 1000);
  };

  poll();
  window.addEventListener("pagehide", () => {
    suspended = true;
    if (timer !== null) window.clearTimeout(timer);
    window.clearInterval(clockTimer);
  }, { once: true });
  window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
});
document.addEventListener("DOMContentLoaded", () => {
  const input = document.getElementById("import-pdf");
  const name = document.getElementById("import-file-name");
  if (!input || !name) return;
  const control = input.closest('.import-file-control');
  const error = document.getElementById('import-file-error');
  const update = () => {
    const file = input.files[0];
    const problem = file && !/\.pdf$/i.test(file.name) ? '请选择 PDF 文件。' :
      file && file.size > Number(control.dataset.maxBytes) ? '文件超过上传大小限制，请选择较小的 PDF。' : '';
    input.setCustomValidity(problem);error.textContent = problem;
    name.textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB` : "未选择文件";
    name.title = name.textContent;
  };
  input.addEventListener("change", update);
  for (const eventName of ['dragenter', 'dragover']) control.addEventListener(eventName, event => {
    event.preventDefault();control.classList.add('is-dragging');
  });
  for (const eventName of ['dragleave', 'drop']) control.addEventListener(eventName, event => {
    event.preventDefault();control.classList.remove('is-dragging');
  });
  control.addEventListener('drop', event => {
    if (event.dataTransfer.files.length !== 1) { error.textContent = '每次请选择一个 PDF 文件。';return; }
    input.files = event.dataTransfer.files;update();
  });
  input.form.addEventListener('submit', () => {
    const button = input.form.querySelector('[type="submit"]');
    button.disabled = true;button.textContent = '正在上传…';
  });
  input.form.addEventListener("reset", () => setTimeout(update, 0));
  update();
});
