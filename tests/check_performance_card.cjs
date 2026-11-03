// Headless synthetic rendering with intercepted requests; no HTTP server or live API.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const playwright = require(process.env.WECHATBOT_PLAYWRIGHT_MODULE || 'playwright');
const assets = path.join(__dirname, '..', 'wechat-weflow-bridge-ob11', 'panel_assets');
const output = process.env.WECHATBOT_PERFORMANCE_PREVIEW_DIR;
const sample = (latest, average, p95, count) => ({latest_seconds: latest, average_seconds: average, p95_seconds: p95, sample_count: count});
const status = {preview: true, running: true, paused: false, send_queue: {pending: 2, active: {elapsed_seconds: 3.4}},
  runtime_watchdog: {state: 'recovering', reason: '等待当前操作结束'},
  response_performance: {stages: {first_text: sample(8.2, 6.4, 11.5, 12), first_voice: sample(12.4, 10.5, 16.3, 5),
    voice_prepare: sample(.04, 1.1, 2.4, 8), queue_wait: sample(.2, .15, .9, 30), voice_queue: sample(.1, .3, 1.2, 5), wechat_send: sample(1.2, 1.4, 2.6, 24)}},
  tts_cache: {hits: 3, misses: 5, coalesced_waits: 1, last_synthesis_seconds: 2, synthesis_window_average_seconds: 1.8,
    synthesis_p95_seconds: 2.8, synthesis_samples: 5}};
(async () => {
  const browser = await playwright.chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(request.method(), 'GET', 'The preview never mutates anything');
      if (url.pathname === '/status') return route.fulfill({json: status});
      if (url.pathname === '/api/config') return route.fulfill({json: {}});
      if (url.pathname === '/api/config/schema') return route.fulfill({json: []});
      const file = {'/': ['index.html', 'text/html'], '/assets/panel.js': ['panel.js', 'text/javascript'], '/assets/panel.css': ['panel.css', 'text/css']}[url.pathname];
      return file ? route.fulfill({body: fs.readFileSync(path.join(assets, file[0])), contentType: file[1]}) : route.fulfill({status: 404, body: ''});
    });
    await page.goto('http://wechatbot-preview.invalid/#workspace');
    await page.waitForFunction(() => document.getElementById('responseTimingRows').children.length === 7);
    assert.equal(await page.locator('#runtimeHealthBadge').textContent(), '合成预览');
    assert.match(await page.locator('#sendProgress').textContent(), /3.40 秒.*2 条排队/);
    const card = page.locator('.response-performance');
    if (output) await card.screenshot({path: path.join(output, 'preview-v6.20-performance.png')});
    await page.setViewportSize({width: 390, height: 844});
    await card.scrollIntoViewIfNeeded();
    const layout = await page.evaluate(() => ({width: window.innerWidth, scroll: document.documentElement.scrollWidth,
      card: document.querySelector('.response-performance').getBoundingClientRect().width,
      wrap: document.querySelector('.performance-table-wrap').getBoundingClientRect().width}));
    assert.ok(layout.scroll <= layout.width, `The mobile page does not overflow: ${JSON.stringify(layout)}`);
    if (output) await card.screenshot({path: path.join(output, 'preview-v6.20-performance-mobile.png')});
    assert.deepEqual(errors, []);
    console.log('Performance card synthetic rendering: PASS');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
