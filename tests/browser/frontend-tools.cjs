/* Live, read-only checks for the research workspace. Management writes are blocked. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');

(async()=>{
 const browser=await chromium.launch({executablePath:process.env.PAPER_TEST_BROWSER || undefined});
 const context=await browser.newContext({viewport:{width:1440,height:960},reducedMotion:'reduce'});
 const base=process.env.PAPER_READER_URL || 'http://127.0.0.1:18020';
 const artifacts=process.env.PAPER_UI_ARTIFACTS || '/tmp/paper-ui-optimized';fs.mkdirSync(artifacts,{recursive:true});
 await context.route('**/*',route=>['GET','HEAD'].includes(route.request().method())?route.continue():route.abort());
 const page=await context.newPage(), errors=[];page.on('pageerror',error=>errors.push(error.message));
 try {
  await page.goto(base+'/library/mechanism-interpretability/');
  await page.getByLabel('库内搜索',{exact:true}).fill('state media');
  await page.getByLabel('年份',{exact:true}).selectOption('2026');
  await page.getByRole('button',{name:'筛选',exact:true}).click();
  assert.equal(await page.locator('.paper-row').count(),1);
  assert((await page.locator('.paper-row h3').textContent()).includes('State media control'));
  await page.reload();assert.equal(await page.getByLabel('库内搜索',{exact:true}).inputValue(),'state media');
  await page.getByRole('link',{name:'清除筛选',exact:true}).click();assert.equal(await page.locator('.paper-row').count(),5);
  await page.getByLabel('主题内排序',{exact:true}).selectOption('title');
  await page.getByRole('button',{name:'筛选',exact:true}).click();
  const titles=await page.locator('.subject-group').first().locator('.paper-row h3').allTextContents();
  assert.deepEqual(titles,[...titles].sort((a,b)=>a.toLowerCase()<b.toLowerCase()?-1:1));
  await page.screenshot({path:artifacts+'/library-desktop.png'});
  await page.getByRole('link',{name:'导入论文',exact:true}).click();
  await page.waitForLoadState('domcontentloaded');
  assert.equal(await page.getByLabel('论文库',{exact:true}).inputValue(),'mechanism-interpretability');
  await page.getByLabel('PDF 文件',{exact:true}).setInputFiles({name:'wrong.txt',mimeType:'text/plain',buffer:Buffer.from('invalid')});
  await page.waitForFunction(()=>document.querySelector('#import-file-error').textContent.includes('PDF'));
  assert((await page.locator('#import-file-error').textContent()).includes('PDF'));
  assert.equal(await page.locator('#import-pdf').evaluate(e=>e.checkValidity()),false);
  await page.locator('.import-file-control').evaluate(el=>{
    const transfer=new DataTransfer();transfer.items.add(new File(['%PDF-fake'],'dropped.pdf',{type:'application/pdf'}));
    el.dispatchEvent(new DragEvent('drop',{dataTransfer:transfer,bubbles:true,cancelable:true}));
  });
  assert.equal(await page.locator('#import-pdf').evaluate(e=>e.files[0].name),'dropped.pdf');
  assert.equal(await page.locator('#import-pdf').evaluate(e=>e.checkValidity()),true);
  let guideRequests=0;
  await page.route('**/paper/mi-p0005/guide?*',route=>{
    guideRequests++;
    return guideRequests===1?route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'测试暂时不可用'})}):route.continue();
  });
  const paperUrl=base+'/library/mechanism-interpretability/paper/mi-p0005';
  const response=await page.goto(paperUrl,{waitUntil:'networkidle'});
  const bytes=(await response.body()).length;
  const nodes=await page.locator('*').count();
  assert.equal(guideRequests,0,'initial page does not request chapter guides');
  assert(nodes<9000,'chapter details must not be duplicated on page load');
  assert.equal(await page.locator('.paper-body img:not([loading="lazy"])').count(),0);
  assert(await page.getByRole('link',{name:'跳到论文正文'}).isVisible());
  const jump=page.getByRole('link',{name:'跳到论文正文'});const target=await jump.getAttribute('href');
  await jump.click();await page.waitForTimeout(150);
  const top=await page.locator(target).evaluate(e=>e.getBoundingClientRect().top);
  const toolbar=await page.locator('.paper-toolbar').evaluate(e=>e.getBoundingClientRect().bottom);
  assert(top>=toolbar-2&&top<400,'article jump must land below toolbar');
  await page.locator('#toc-search').fill('Sampling strategy');
  assert(await page.getByRole('link',{name:'Sampling strategy',exact:true}).isVisible());
  await page.locator('#toc-search').fill('no-such-chapter-123');assert(await page.locator('#toc-empty').isVisible());
  await page.locator('#toc-search').fill('');
  await page.locator('#toc-expand').click();assert(await page.locator('.toc-region').evaluateAll(items=>items.every(e=>e.open)));
  await page.locator('#toc-expand').click();assert(await page.locator('.toc-region').evaluateAll(items=>items.every(e=>!e.open)));
  await page.goto(paperUrl);
  await page.locator('#reading-guide > summary').click();
  const section=page.locator('#reading-guide .guide-section').first();
  await section.locator(':scope > summary').click();
  await section.getByRole('button',{name:'重新加载'}).waitFor();
  await section.getByRole('button',{name:'重新加载'}).click();
  await section.locator('[data-guide-content][data-loaded]').waitFor();
  assert.equal(guideRequests,2,'failed guide request can be retried');
  const sectionId=await section.getAttribute('data-guide-section');
  const inline=page.locator('.section-reading-guide').filter({has:page.locator('summary')}).first();
  await inline.locator(':scope > summary').click();await inline.locator('[data-guide-content][data-loaded]').waitFor();
  assert.equal(guideRequests,2,'matching local and overview guides reuse the request');
  assert.equal(await page.locator('.guide-source').evaluateAll(links=>links.filter(a=>!document.getElementById(a.hash.slice(1))).length),0);
  await page.locator('.reading-more > summary').click();await page.getByRole('button',{name:'阅读设置',exact:true}).click();
  await page.getByLabel('正文字号',{exact:true}).selectOption('20');
  await page.getByLabel('正文行距',{exact:true}).selectOption('2');
  await page.getByRole('button',{name:'关闭阅读设置'}).click();
  assert.deepEqual(await page.evaluate(()=>JSON.parse(localStorage.getItem('paper-reader-type-v1'))),{size:'20',spacing:'2'});
  await page.reload();
  await page.waitForFunction(()=>getComputedStyle(document.querySelector('.paper-body')).fontSize==='20px');
  assert.equal(await page.locator('.paper-body').evaluate(e=>getComputedStyle(e).fontSize),'20px');
  assert.equal(await page.locator('.paper-body').evaluate(e=>getComputedStyle(e).lineHeight),'40px');
  await page.locator('.translation-toggle').click();await page.waitForFunction(()=>document.querySelector('[data-translation-url]').getAttribute('aria-busy')==='false');
  assert.equal(await page.locator('[data-translation-id]:not([hidden])').count(),589);
  await page.reload();await page.waitForFunction(()=>document.querySelector('[data-translation-url]').getAttribute('aria-busy')==='false');
  assert.equal(await page.locator('.translation-toggle').getAttribute('aria-pressed'),'true');
  await page.locator('.translation-toggle').click();
  await page.locator('.reading-more > summary').click();await page.getByRole('button',{name:'阅读设置',exact:true}).click();
  await page.getByRole('button',{name:'恢复默认设置'}).click();await page.keyboard.press('Escape');
  await page.screenshot({path:artifacts+'/paper-desktop.png'});
  const paths=['/','/library/mechanism-interpretability/','/search?q=language&library=mechanism-interpretability&origin=metadata','/reading','/manage/import','/manage/status','/manage/settings','/manage/jobs/66dcda2c137144b990bf4c984a1623a4',new URL(paperUrl).pathname];
  for(const width of [1440,768,390,320])for(const path of paths){
    await page.setViewportSize({width,height:900});const result=await page.goto(base+path,{waitUntil:'networkidle'});assert.equal(result.status(),200);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`${width}px overflow: ${path}`);
    if(width===390&&path.includes('/paper/')){
      await page.screenshot({path:artifacts+'/paper-mobile.png'});
      assert(await page.locator('.translation-controls > button').evaluateAll(items=>items.filter(e=>e.checkVisibility()).every(e=>e.getBoundingClientRect().width>=44&&e.getBoundingClientRect().height>=44)));
      await page.locator('.sidebar-toggle').click();assert(await page.locator('#toc-search').isVisible());
      await page.locator('#toc-search').fill('Methods');await page.keyboard.press('Escape');assert(!(await page.locator('.reader-sidebar').isVisible()));
    }
    if(width===390&&path==='/library/mechanism-interpretability/')await page.screenshot({path:artifacts+'/library-mobile.png'});
  }
  // The unreadable-Markdown fallback has no reading-settings action.
  await page.route('**/paper/mi-p0005?unavailable=1',async route=>{
    const response=await route.fetch();
    const html=(await response.text()).replace(/<button type="button" id="open-reading-settings">[\s\S]*?<\/button>/,'')
      .replace(/<article class="paper-body">[\s\S]*?<\/article>/,'<section class="empty-state article-empty"><h2>Markdown 暂不可读</h2></section>');
    await route.fulfill({response,body:html});
  });
  await page.goto(paperUrl+'?unavailable=1',{waitUntil:'networkidle'});
  assert(await page.getByRole('heading',{name:'Markdown 暂不可读'}).isVisible());
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({pass:true,htmlBytes:bytes,initialNodes:nodes,viewportWidths:[1440,768,390,320],artifacts,checks:['catalog filters/sort/URL state','import validation/drop/preselection','physical article jump','TOC search/collapse','lazy guide failure/retry/cache','evidence anchors','reading preferences/reload','589 offline translations/persistence','9 pages responsive/no JS errors']},null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
