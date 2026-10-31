const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const assert = require('node:assert/strict');

const base = process.env.PAPER_MANAGEMENT_URL || 'http://127.0.0.1:18021';
let server = null;

async function waitForServer() {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(base + '/health/live');
      if (response.ok) return;
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('management test server did not start');
}

(async () => {
  if (!process.env.PAPER_MANAGEMENT_URL) {
    server = process.env.PAPER_TEST_PYTHON
      ? spawn(process.env.PAPER_TEST_PYTHON, ['tests/browser/management_server.py'], {cwd:process.cwd(),stdio:['ignore','pipe','pipe']})
      : spawn('conda', ['run', '-n', 'paper', 'python', 'tests/browser/management_server.py'], {
      cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'],
    });
    await waitForServer();
  }
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PAPER_TEST_BROWSER || undefined,
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    const requests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => requests.push(request.url()));
    await page.goto(base + '/');
    await page.getByRole('link', { name: '导入论文' }).click();
    assert.equal(await page.getByRole('link', { name: '关闭导入界面' }).count(), 0);
    const controlHeights = await page.evaluate(() => {
      const file = document.querySelector('input[type="file"]');
      const select = document.querySelector('select[name="library_id"]');
      return [file.getBoundingClientRect().height, select.getBoundingClientRect().height];
    });
    assert(Math.abs(controlHeights[0] - controlHeights[1]) < 2);
    for (const label of ['新增论文库', '新增主题']) {
      const button = page.getByRole('button', {name:label, exact:true});
      assert.equal(await button.locator('svg').count(), 1);
      assert.equal((await button.innerText()).trim(), '');
    }
    await page.getByRole('button', {name:'新增论文库', exact:true}).click();
    await page.getByRole('button', {name:'创建并选中', exact:true}).click();
    assert((await page.locator('#library-error').innerText()).includes('请输入'));
    await page.getByLabel('新论文库名称').fill('强化学习');
    await page.getByLabel('新论文库名称').press('Enter');
    await page.waitForFunction(() => document.querySelector('#import-library').selectedOptions[0]?.textContent === '强化学习');
    const newLibrary = await page.locator('#import-library').inputValue();
    assert.equal(await page.locator('#import-subject option').count(), 1);
    await page.getByRole('button', {name:'新增主题', exact:true}).click();
    await page.getByLabel('新主题名称').fill('策略优化');
    await page.getByRole('button', {name:'保存并选中', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#import-subject').value === '策略优化');
    await page.reload();
    await page.getByLabel('论文库', {exact:true}).selectOption(newLibrary);
    assert.equal(await page.locator('#import-subject option').last().textContent(), '策略优化');
    await page.getByRole('button', {name:'新增论文库', exact:true}).click();
    await page.getByLabel('新论文库名称').fill('强化学习');
    await page.getByRole('button', {name:'创建并选中', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#library-error').textContent.includes('同名'));
    await page.getByLabel('新论文库名称').press('Escape');
    assert.equal(await page.locator('#new-library').isVisible(), false);
    await page.getByLabel('PDF 文件').setInputFiles({
      name: 'browser-paper.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-browser'),
    });
    await page.getByLabel('论文库', {exact:true}).selectOption('whitespace');
    await page.getByRole('button', { name: '新增主题', exact: true }).click();
    await page.getByLabel('新主题名称').fill('related-work');
    await page.getByRole('button', { name: '保存并选中' }).click();
    await page.waitForFunction(() => document.querySelector('#import-subject').value === 'related-work');
    await page.getByRole('button', { name: '开始导入' }).click();
    assert.equal(await page.locator('.job-progress').count(), 1);
    await page.waitForFunction(() => document.querySelector('.job-state')?.textContent.includes('PDF 解析'));
    assert.equal(await page.locator('progress').getAttribute('value'), null);
    assert(!(await page.locator('[data-job-progress-label]').textContent()).includes('0 / 0'));
    await page.waitForFunction(() => document.querySelector('.job-state')?.textContent.includes('段落修复'));
    assert((await page.locator('[data-job-progress-label]').textContent()).includes('1 / 3'));
    await page.waitForFunction(() => document.querySelector('.job-state')?.textContent.includes('信息检索'));
    assert.equal(await page.locator('progress').getAttribute('value'), null);
    await page.waitForFunction(() => document.querySelector('.job-state')?.textContent.includes('结构校对'));
    assert((await page.locator('[data-job-progress-label]').textContent()).includes('1 / 2 块'));
    await page.waitForFunction(() => document.querySelector('.job-state')?.textContent.includes('翻译'));
    await page.getByRole('link', { name: '打开论文' }).waitFor();
    await page.waitForFunction(() => document.querySelector('.job-state')?.textContent.includes('已完成'));
    const apiCalls = requests.filter(url => url.includes('/api/jobs/')).length;
    await page.waitForTimeout(1800);
    assert.equal(requests.filter(url => url.includes('/api/jobs/')).length, apiCalls);
    assert.equal(await page.locator('.job-progress').count(), 1);
    const jobUrl = page.url();
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert(!requests.some(url => /api[_-]?key|authorization/i.test(url)));
    assert.deepEqual(errors, []);
    await page.getByRole('link', { name: '打开论文' }).click();
    await page.waitForURL('**/library/*/paper/*');
    await page.locator('.structure-abstract').waitFor();
    assert((await page.locator('body').innerText()).includes('English paragraph'), `${page.url()}\n${await page.locator('body').innerText()}`);
    assert.equal(await page.locator('.structure-abstract').count(), 1);
    assert.equal(await page.locator('.structure-abstract').evaluate(e => getComputedStyle(e).textAlign), 'left');
    // Exercise both real clipboard output and the denied-permission fallback.
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.locator('.reading-more summary').click();
    await page.getByRole('button', {name:'复制普通引用', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#citation-status').textContent === '已复制');
    const plainCitation = await page.evaluate(() => navigator.clipboard.readText());
    assert(plainCitation.includes('Browser paper'));
    await page.getByRole('button', {name:'复制 BibTeX', exact:true}).click();
    await page.waitForFunction(async () => (await navigator.clipboard.readText()).startsWith('@'));
    assert((await page.evaluate(() => navigator.clipboard.readText())).includes('title = {{Browser paper}}'));
    await page.evaluate(() => { navigator.clipboard.writeText = async () => { throw new Error('denied'); }; });
    await page.getByRole('button', {name:'复制普通引用', exact:true}).click();
    await page.getByRole('dialog').waitFor();
    assert.equal(await page.getByLabel('引用文本').inputValue(), plainCitation);
    await page.getByRole('button', {name:'关闭复制窗口'}).click();
    await page.locator('.reading-more summary').click();
    // Personal annotations stay separate from generated source content.
    await page.setViewportSize({width:1280,height:900});
    await page.locator('#source-3').evaluate(el => {
      const range=document.createRange();range.selectNodeContents(el);
      const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
    });
    await page.getByRole('button',{name:'高亮选文',exact:true}).click();
    await page.getByRole('button',{name:'标注',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('#notes-list article').length===1);
    assert.equal(await page.locator('#highlight-status').textContent(),'');
    assert.equal(await page.locator('#notes-status').textContent(),'');
    assert.equal(await page.locator('#notes-list article small').count(),0);
    assert(await page.locator('#notes-list article').evaluate(item => {
      const add=item.querySelector('summary').getBoundingClientRect();
      const remove=item.querySelector('.note-card-actions > button').getBoundingClientRect();
      return Math.abs(add.top-remove.top)<3;
    }));
    assert.equal(await page.locator('#note-text').count(),0);
    assert.equal(await page.locator('#notes-panel #add-highlight').count(),0);
    assert.equal(await page.locator('#selected-quote').count(),0);
    assert.equal(await page.getByLabel('编辑备注').isVisible(),false);
    await page.getByText('添加备注',{exact:true}).click();
    await page.getByLabel('编辑备注').fill('我的阅读理解 <script>unsafe()</script>');
    await page.getByRole('button',{name:'保存备注',exact:true}).click();
    await page.getByText('编辑备注',{exact:true}).waitFor();
    assert.equal(await page.locator('#notes-list article').count(),1);
    assert.equal(await page.locator('#notes-list script').count(),0);
    await page.getByText('编辑备注',{exact:true}).click();
    await page.getByLabel('编辑备注').fill('');
    await page.getByRole('button',{name:'保存备注',exact:true}).click();
    await page.getByText('添加备注',{exact:true}).waitFor();
    assert.equal(await page.locator('#notes-list article').count(),1);
    assert(await page.evaluate(()=>CSS.highlights.get('reader-notes').size===1));
    await page.getByRole('button',{name:'目录',exact:true}).click();
    await page.mouse.wheel(0,1800);
    await page.waitForTimeout(1600);
    await page.reload();
    await page.getByRole('button',{name:'继续上次阅读'}).waitFor();
    await page.getByRole('button',{name:'继续上次阅读'}).click();
    await page.waitForFunction(()=>window.scrollY>500);
    // A stale second tab must not overwrite the first tab's latest position.
    const second=await browser.newPage({viewport:{width:1280,height:900}});
    await second.goto(page.url());
    await second.getByRole('button',{name:'继续上次阅读'}).waitFor();
    await page.waitForTimeout(850);
    const saved=page.waitForResponse(r=>r.url().includes('/api/reading/')&&r.request().method()==='POST'&&r.ok());
    await page.mouse.wheel(0,1000);await saved;
    await second.mouse.wheel(0,800);
    await second.getByRole('button',{name:'恢复其他页面的最新位置'}).waitFor();
    await second.getByRole('button',{name:'恢复其他页面的最新位置'}).click();
    await second.waitForFunction(()=>window.scrollY>500);
    await second.close();
    await page.goto(base+'/reading');
    assert((await page.locator('body').innerText()).includes('English paragraph'));
    assert(!(await page.locator('body').innerText()).includes('我的阅读理解'));
    await page.goto(base+'/search?q=English');
    assert((await page.locator('.search-results').innerText()).includes('English paragraph'));
    // A lost status connection is visible and recovery still resumes polling.
    let disconnected = true;
    await page.route('**/api/jobs/*', async route => {
      if (disconnected) await route.abort(); else await route.continue();
    });
    await page.goto(jobUrl);
    await page.locator('[data-job-connection]:visible').waitFor();
    assert((await page.locator('[data-job-connection]').textContent()).includes('状态更新中断'));
    disconnected = false;
    await page.waitForFunction(() => document.querySelector('[data-job-connection]').hidden, {timeout: 10000});
    assert((await page.locator('[data-job-state]').textContent()).includes('已完成'));
    const apiPath = await page.locator('[data-job-url]').getAttribute('data-job-url');
    const warningPayload = await (await fetch(base + apiPath)).json();
    warningPayload.state = 'completed_with_warnings';
    warningPayload.outcomes = {
      metadata: {status: 'warning', message: '找到候选，但证据不足', reason: 'ambiguous', candidates: [{title: 'Official candidate', url: 'https://arxiv.org/abs/2510.10271', domain: 'arxiv.org'}, {title: 'Unsafe', url: 'javascript:alert(1)', domain: 'arxiv.org'}]},
      translation: {status: 'warning', message: '阅读提示 2 段'},
      guide: {status: 'warning', message: '导读未完成，可重试'}
    };
    await page.route('**/api/jobs/*', route => route.fulfill({json: warningPayload}));
    await page.goto(jobUrl);
    await page.waitForFunction(() => document.querySelector('[data-job-state]').textContent.includes('需核对'));
    assert.equal(await page.locator('[data-job-outcomes] [data-result="warning"]').count(), 3);
    assert((await page.locator('[data-job-outcomes]').textContent()).includes('导读未完成'));
    assert.equal(await page.locator('[data-job-outcomes] a[href="https://arxiv.org/abs/2510.10271"]').count(), 1);
    assert.equal(await page.locator('[data-job-outcomes] a[href^="javascript:"]').count(), 0);
    assert(await page.getByRole('button', {name: '重试', exact: true}).isVisible());
    await page.route(jobUrl, async route => {
      const response = await route.fetch();
      await route.fulfill({response, body: (await response.text()).replace('data-auto-translate="true"', 'data-auto-translate="false"')});
    });
    await page.goto(jobUrl);
    await page.getByRole('button', {name: '开始翻译', exact: true}).waitFor({state: 'visible'});
    console.log('PASS: upload, live stages, one progress bar, terminal polling, paper link, mobile layout, cross-tab reading position');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => {
  if (server) server.kill('SIGTERM');
});
