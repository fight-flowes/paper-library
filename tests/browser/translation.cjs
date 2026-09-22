const { chromium } = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PAPER_TEST_BROWSER || undefined,
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    let writes = 0;
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (!['GET', 'HEAD'].includes(request.method())) writes += 1;
    });
    const base = process.env.PAPER_READER_URL || 'http://127.0.0.1:18020';
    await page.goto(base + '/');
    const home = await page.locator('body').innerText();
    assert(home.includes('Mechanism Interpretability'));
    assert(home.includes('Whitespace'));

    await page.goto(base + '/library/mechanism-interpretability/paper/mi-p0001');
    assert.equal(await page.locator('.paragraph-translation:visible').count(), 0);
    await page.getByRole('button', { name: '中英对照', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.paragraph-translation:not([hidden])').length > 200);
    assert((await page.locator('.paragraph-translation:visible').count()) > 200);
    const style = await page.locator('.paragraph-translation:visible').first().evaluate(element => {
      const css = getComputedStyle(element);
      return { border: css.borderLeftWidth, background: css.backgroundColor, padding: css.paddingTop };
    });
    assert(parseFloat(style.border) >= 3);
    assert.notEqual(style.background, 'rgba(0, 0, 0, 0)');
    assert(parseFloat(style.padding) > 0);
    await page.getByRole('button', { name: '关闭中英对照' }).click();
    assert.equal(await page.locator('.paragraph-translation:visible').count(), 0);
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.evaluate(() => {
        const paragraph = document.querySelectorAll('.paper-body > p')[20];
        paragraph.dataset.scrollTest = 'true';
        window.scrollTo({top: scrollY + paragraph.getBoundingClientRect().top - 80, behavior: 'instant'});
      });
      const position = () => page.locator('[data-scroll-test]').evaluate(el => el.getBoundingClientRect().top);
      const before = await position();
      // Programmatic activation avoids Playwright scrolling to the toolbar first.
      await page.locator('.translation-toggle').evaluate(el => el.click());
      await page.waitForFunction(() => document.querySelector('[data-translation-url]').getAttribute('aria-busy') === 'false');
      await page.waitForTimeout(200);
      assert(Math.abs(await position() - before) < 3, `show preserves paragraph position: ${before} -> ${await position()} width=${width}`);
      await page.locator('.translation-refresh').evaluate(el => el.click());
      await page.waitForFunction(() => document.querySelector('[data-translation-url]').getAttribute('aria-busy') === 'false');
      assert(Math.abs(await position() - before) < 3, 'refresh preserves paragraph position');
      await page.locator('.translation-toggle').evaluate(el => el.click());
      await page.waitForTimeout(200);
      assert(Math.abs(await position() - before) < 3, 'hide preserves paragraph position');
    }
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.equal(writes, 0, '阅读器只能读取已保存译文');
    assert.deepEqual(errors, []);
    console.log('PASS: libraries, translated paragraphs, styles, toggle, read-only requests, mobile layout');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
