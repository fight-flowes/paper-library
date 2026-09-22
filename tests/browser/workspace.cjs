const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch();
 try {
  const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const base=process.env.PAPER_READER_URL||'http://127.0.0.1:18020';
  for(const width of [1440,390])for(const path of ['/','/library/whitespace','/search?q=hallucinations','/reading','/manage/status','/manage/import']){
   await page.setViewportSize({width,height:900});assert.equal((await page.goto(base+path)).status(),200);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),path+' overflow');
   assert(await page.locator('.workspace-nav a').evaluateAll(links=>links.every(a=>getComputedStyle(a).textDecorationLine==='none')));
  }
  await page.goto(base+'/search?q=hallucinations');
  const titles=await page.locator('.search-results > article > h2 a').allTextContents();
  assert.equal(new Set(titles).size,titles.length,'one result group per paper');
  assert(await page.locator('mark').count());
  const details=page.locator('.search-matches').first();await details.locator('summary').click();
  assert(await details.locator('.hit-link').first().isVisible());
  const target=await details.locator('.hit-link').first().getAttribute('href');assert(target.includes('#'));
  assert.deepEqual(errors,[]);
  console.log('PASS: all workspace pages, navigation, mobile layout, grouped search and source links');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
