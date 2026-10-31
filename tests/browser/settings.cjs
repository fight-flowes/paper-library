/* All settings mutations run against an isolated fixture, never the live reader. */
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const base = 'http://127.0.0.1:18022';
  const server = spawn(process.env.PAPER_TEST_PYTHON || 'python', ['tests/browser/settings_server.py'],
    {cwd:process.cwd(), stdio:['ignore','pipe','pipe']});
  let output = ''; server.stderr.on('data', chunk => { output = (output + chunk).slice(-8000); });
  const artifacts = '/tmp/paper-settings-ui'; fs.mkdirSync(artifacts, {recursive:true});
  let browser;
  try {
    let ready = false;
    for (let i=0; i<120; i++) {
      if (server.exitCode !== null) throw new Error(output);
      try { if ((await fetch(base + '/health/live')).ok) { ready=true; break; } } catch (_) {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert(ready, 'isolated settings fixture did not start');
    browser = await chromium.launch({executablePath:process.env.PAPER_TEST_BROWSER || undefined});
    const context = await browser.newContext({viewport:{width:1440,height:960}, reducedMotion:'reduce'});
    const page = await context.newPage(), errors=[];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.goto(base + '/');
    await page.getByRole('link', {name:'设置', exact:true}).click();
    assert.equal(await page.getByRole('link', {name:'设置', exact:true}).getAttribute('aria-current'), 'page');
    assert.equal(await page.locator('input[type=password]').evaluateAll(inputs => inputs.some(input => input.value)), false);
    assert(!(await page.content()).includes('fixture-secret'));
    assert(!(await page.content()).includes('fixture-ssh-secret'));
    const model = page.locator('#model form'), remote = page.locator('#remote form'), imports = page.locator('#import form');
    await page.getByLabel('模型名称', {exact:true}).fill('candidate-model');
    assert.equal(await page.locator('[data-settings-state]').innerText(), '有修改待保存');
    await model.getByRole('button', {name:'测试连接', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#model .setting-feedback').textContent.includes('尚未保存'));
    await page.reload();
    assert.equal(await page.getByLabel('模型名称', {exact:true}).inputValue(), 'fixture-model');
    await page.getByLabel('模型名称', {exact:true}).fill('unavailable');
    await model.getByRole('button', {name:'保存模型设置', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#model .setting-feedback').textContent.includes('HTTP 503'));
    await page.reload();
    assert.equal(await page.getByLabel('模型名称', {exact:true}).inputValue(), 'fixture-model');
    await page.getByLabel('模型名称', {exact:true}).fill('selected-model');
    await model.locator('details > summary').click();
    await page.getByLabel('API 密钥', {exact:true}).fill('replacement-secret');
    await model.getByRole('button', {name:'保存模型设置', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#model .setting-feedback').textContent.includes('已保存并生效'));
    assert.equal(await page.locator('[data-settings-state]').innerText(), '当前配置已生效');
    assert.equal(await page.getByLabel('API 密钥', {exact:true}).inputValue(), '');
    await page.reload();
    assert.equal(await page.getByLabel('模型名称', {exact:true}).inputValue(), 'selected-model');
    assert(!(await page.content()).includes('replacement-secret'));
    await model.locator('details > summary').click();
    await page.getByLabel('清除已保存的密钥', {exact:true}).check();
    await model.getByRole('button', {name:'保存模型设置', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#model .setting-feedback').textContent.includes('认证失败'));
    await page.getByLabel('清除已保存的密钥', {exact:true}).uncheck();
    await remote.locator('details > summary').click();
    await page.getByLabel('SSH 主机', {exact:true}).fill('broken');
    await remote.getByRole('button', {name:'测试连接', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#remote .setting-feedback').textContent.includes('连接失败'));
    await page.getByLabel('SSH 主机', {exact:true}).fill('127.0.0.1');
    await remote.getByRole('button', {name:'保存解析设置', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#remote .setting-feedback').textContent.includes('已保存并生效'));
    const other = await context.newPage(); await other.goto(base + '/manage/settings');
    await page.getByLabel('默认论文库', {exact:true}).selectOption('example');
    await page.getByLabel('默认文档语言', {exact:true}).selectOption('ch');
    await page.getByLabel('书目信息核对', {exact:true}).selectOption('0');
    await page.getByLabel('解析后自动翻译', {exact:true}).selectOption('0');
    await imports.getByRole('button', {name:'保存导入默认值', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#import .setting-feedback').textContent.includes('已保存并生效'));
    await other.locator('#import form').getByRole('button', {name:'保存导入默认值', exact:true}).click();
    await other.waitForFunction(() => document.querySelector('#import .setting-feedback').textContent.includes('其他窗口'));
    await other.close();
    await page.goto(base + '/manage/import');
    assert.equal(await page.getByLabel('论文库', {exact:true}).inputValue(), 'example');
    assert.equal(await page.getByLabel('文档语言', {exact:true}).inputValue(), 'ch');
    assert.equal(await page.getByLabel('自动检索并核对书目信息', {exact:true}).isChecked(), false);
    assert.equal(await page.getByLabel('解析后自动翻译', {exact:true}).isChecked(), false);
    await page.goto(base + '/manage/settings');
    for (const width of [1440,768,390,320]) {
      await page.setViewportSize({width,height:960});
      await page.locator('.setting-advanced').evaluateAll(items => items.forEach(item => {item.open=true;}));
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px settings overflow`);
      assert(await page.locator('.setting-actions button').evaluateAll(items => items.every(item => item.getBoundingClientRect().height >= 44)));
      if (width===1440 || width===390) await page.screenshot({path:`${artifacts}/settings-${width}.png`, fullPage:true});
    }
    await model.getByRole('button', {name:'测试连接', exact:true}).focus();
    assert.equal(await model.getByRole('button', {name:'测试连接', exact:true}).evaluate(item => document.activeElement === item), true);
    assert.equal(await page.locator('input[type=password]').evaluateAll(inputs => inputs.some(input => input.value)), false);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({pass:true,isolated:true,viewportWidths:[1440,768,390,320],artifacts,
      checks:['navigation and current state','secrets never echoed','unsaved connection test','failed model configuration retained',
              'model save and reload','credential replacement and clear failure','remote test and save',
              'import defaults applied','stale window conflict','connection disclosure and responsive layout','keyboard focus/no JS errors']},null,2));
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
  }
})().catch(error => {console.error(error);process.exitCode=1;});
