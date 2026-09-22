const { chromium } = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PAPER_TEST_BROWSER || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const base = process.env.PAPER_READER_URL || 'http://127.0.0.1:18020';
    await page.goto(base + '/library/whitespace/paper/ws-p0002');
    await page.getByLabel('更多阅读操作', {exact:true}).click();
    await page.locator('.guide-toolbar').click();
    assert(await page.locator('#reading-guide').evaluate(el => el.open));
    assert((await page.locator('#reading-guide .guide-points > li').count()) > 5);
    assert((await page.locator('.paper-body .section-reading-guide').count()) > 5);
    const section = page.locator('#reading-guide .guide-section').filter({has: page.locator('.guide-depth')}).first();
    assert(await section.count(), 'rich sections offer detailed explanations');
    await section.locator(':scope > summary').click();
    const depth = section.locator('.guide-depth');
    assert.equal(await depth.evaluate(el => el.open), false, 'details are collapsed by default');
    const detailText = depth.locator('.guide-points > li').first();
    assert.equal(await detailText.isVisible(), false);
    await depth.locator(':scope > summary').click();
    assert(await detailText.isVisible(), 'reader can opt into detail');
    await depth.locator(':scope > summary').click();
    await section.locator(':scope > summary').click();
    const missing = await page.locator('.guide-source').evaluateAll(links => links.map(a => a.hash.slice(1)).filter(id => !document.getElementById(id)));
    assert.deepEqual(missing, [], 'all source links resolve');
    await page.locator('#reading-guide .guide-evidence summary').first().click();
    const link = page.locator('#reading-guide .guide-evidence .guide-source').first();
    const target = await link.getAttribute('href');
    await link.click();
    await page.waitForTimeout(500);
    assert.equal(await page.evaluate(() => location.hash), target);
    const bounds = await page.locator(target).boundingBox();
    assert(bounds.y < 900 && bounds.y + bounds.height > 0);
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    console.log('PASS: overview, local section guides, evidence anchors, source navigation, mobile layout');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
