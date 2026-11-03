// Real browser smoke of synthetic preview; never calls a model or WeChat.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const playwright = require(process.env.WECHATBOT_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const configuredUrl = new URL(process.env.WECHATBOT_PREVIEW_URL || 'http://127.0.0.1:8881');
  assert.equal(configuredUrl.protocol, 'http:', 'Synthetic preview must use local HTTP');
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(configuredUrl.hostname), 'Synthetic preview must be local');
  const origin = configuredUrl.origin;
  const browser = await playwright.chromium.launch({channel:'msedge', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440, height:1000}});
    const errors = [];
    let verifiedPreview = false;
    page.on('pageerror', error => errors.push(error.message));
    // Before the status guard passes, every network mutation is forbidden.
    // The smoke only needs configuration POSTs; never permit send/clear/API tests.
    await page.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin !== origin || (!['GET', 'HEAD'].includes(request.method())
          && (!verifiedPreview || request.method() !== 'POST' || url.pathname !== '/api/config'))) {
        errors.push(`Forbidden smoke request: ${request.method()} ${url.pathname}`);
        return route.abort('blockedbyclient');
      }
      return route.continue();
    });
    await page.goto(origin + '/#models');
    const status = await page.evaluate(async () => (await fetch('/api/status')).json());
    assert.equal(status.preview, true, 'Refuse UI/configuration changes outside synthetic preview');
    verifiedPreview = true;
    const initial = await page.evaluate(async () => {
      const [schema, config] = await Promise.all([
        fetch('/api/config/schema').then(response => response.json()),
        fetch('/api/config').then(response => response.json()),
      ]);
      return {schema, model:config.main_llm_model};
    });
    assert.ok(Array.isArray(initial.schema) && initial.schema.length > 0);
    const keys = initial.schema.flatMap(group => group.fields.map(field => field.key));
    assert.equal(new Set(keys).size, keys.length, 'Schema keys must be unique');
    await page.waitForFunction(({count, model}) => {
      const summary = document.getElementById('configSummary');
      const input = document.getElementById('cfg_main_llm_model');
      return summary?.textContent.includes(`${count} 项设置`) && input?.value === model;
    }, {count:keys.length, model:initial.model});
    assert.equal(await page.locator('.role-card').count(), 6);
    assert.equal(await page.locator('[id^="cfg_"]').count(), keys.length);
    for (const key of keys) assert.equal(await page.locator('#cfg_' + key).count(), 1, `Missing field: ${key}`);
    for (const title of await page.locator('.role-card .eyebrow').allTextContents()) {
      const reachable = await page.evaluate(title => [...document.querySelectorAll('#modelConfiguration .settings-group')]
        .some(section => section.dataset.group === title), title);
      assert.equal(reachable, true, `Model card has no visible configuration group: ${title}`);
    }

    const syntheticModel = 'synthetic-vision-model-' + Date.now();
    await page.locator('#cfg_main_llm_model').fill(syntheticModel);
    await page.locator('[data-page="moments"]').click();
    await page.locator('#cfg_moments_browse_interval_min').fill('27');
    await page.locator('[data-page="voice"]').click();
    await page.locator('#cfg_tts_speech_rate').fill('1.2');
    await page.locator('[data-page="models"]').click();
    assert.equal(await page.locator('#cfg_main_llm_model').inputValue(), syntheticModel);
    await page.waitForFunction(() => !document.getElementById('saveConfig').disabled);
    const savedResponse = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/config');
    await page.locator('#saveConfig').click();
    const saved = await savedResponse;
    assert.equal(saved.ok(), true);
    assert.notEqual((await saved.json()).ok, false);
    await page.waitForFunction(model => {
      const button = document.getElementById('saveConfig');
      return button?.disabled && !button.hasAttribute('aria-busy')
        && document.getElementById('dirtyDot')?.hidden
        && document.getElementById('cfg_main_llm_model')?.value === model;
    }, syntheticModel);
    const persisted = await page.evaluate(async () => (await fetch('/api/config')).json());
    assert.equal(persisted.main_llm_model, syntheticModel);
    assert.equal(persisted.moments_browse_interval_min, 27);
    assert.equal(persisted.tts_speech_rate, 1.2);

    await page.locator('[data-page="media"]').click();
    await page.waitForFunction(() => {
      const image = document.querySelector('.reference-card img');
      return image?.complete && image.naturalWidth > 0;
    });
    assert.equal(await page.locator('.reference-card button').first().isDisabled(), true);
    await page.locator('.reference-card input[type="checkbox"]').first().check();
    await page.locator('[data-page="settings"]').click();
    assert.match(await page.locator('#cfg_bot_image_refs').inputValue(), /^ref:/);
    assert.equal(await page.locator('#cfg_main_llm_model').inputValue(), syntheticModel);

    await page.locator('[data-page="memory"]').click();
    await page.locator('#memorySearch').fill('预览会话 1：');
    await page.waitForFunction(() => document.querySelector('.memory-evidence summary')?.textContent.includes('preview-message-0001'));
    assert.match((await page.locator('.memory-meta').allTextContents()).join(' '), /wxid_demo_actor/);
    assert.match(await page.locator('.memory-row time').textContent(), /^更新：/);
    await page.locator('#clearMemories').click();
    await page.waitForSelector('#confirmDialog[open]');
    assert.match(await page.locator('#confirmText').textContent(), /记忆及本地聊天日志会永久删除/);
    await page.locator('#confirmDialog button[value="cancel"]').click();
    await page.waitForSelector('#confirmDialog[open]', {state:'hidden'});
    await page.locator('#memorySearch').fill('');
    await page.waitForFunction(() => document.querySelectorAll('.memory-row').length === 30);

    const output = process.env.WECHATBOT_SCREENSHOT_DIR || path.resolve(__dirname, '../../panel-screenshots');
    fs.mkdirSync(output, {recursive:true});
    const pages = ['workspace', 'models', 'moments', 'media', 'voice', 'settings', 'memory', 'diagnostics', 'logs'];
    for (const name of pages) {
      await page.locator(`[data-page="${name}"]`).click();
      await page.waitForSelector('#page-' + name + ':not([hidden])');
      await page.screenshot({path:path.join(output, `${name}-desktop.png`), fullPage:true});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${name} desktop overflow`);
    }
    await page.setViewportSize({width:390, height:844});
    for (const name of pages) {
      await page.evaluate(name => {location.hash = '#' + name;}, name);
      await page.waitForSelector('#page-' + name + ':not([hidden])');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${name} mobile overflow`);
      await page.screenshot({path:path.join(output, `${name}-mobile.png`), fullPage:false});
    }
    assert.deepEqual(errors, []);
    console.log(`Actual browser panel: PASS (synthetic-only guard, ${pages.length} pages, ${keys.length} fields, draft/save, references, memory confirmation, desktop/mobile)`);
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
