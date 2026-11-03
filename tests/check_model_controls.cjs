// Synthetic preview only: refuse to mutate a live bridge.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const playwright = require(process.env.WECHATBOT_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const origin = process.env.WECHATBOT_PREVIEW_URL || 'http://127.0.0.1:8881';
  const browser = await playwright.chromium.launch({channel:'msedge',headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/#models');
    await page.waitForSelector('#cfg_main_context_compression_threshold');
    const status = await page.evaluate(async () => (await fetch('/api/status')).json());
    assert.equal(status.preview, true, 'Refuse configuration changes outside synthetic preview');
    assert.equal(await page.locator('.role-card').count(), 6);
    assert.equal(await page.locator('#cfg_main_llm_context_tokens').getAttribute('max'), '500000');
    await page.locator('#cfg_main_llm_model').fill('synthetic-deepseek-flash-' + Date.now());
    await page.locator('#cfg_main_llm_context_tokens').fill('128000');
    await page.locator('#cfg_main_llm_reasoning_effort').selectOption('low');
    await page.locator('#cfg_main_context_compression_threshold').fill('0.75');
    await page.locator('#cfg_main_streaming_enabled').uncheck();
    await page.locator('[data-page="settings"]').click();
    await page.locator('#cfg_bot_owner_wechat_ids').fill('wxid_synthetic_owner');
    await page.locator('[data-page="models"]').click();
    assert.equal(await page.locator('#cfg_main_llm_context_tokens').inputValue(), '128000');
    await page.locator('#saveConfig').click();
    await page.waitForFunction(() => document.getElementById('saveConfig').disabled);
    await page.waitForFunction(() => document.getElementById('mainWorkflowSummary').textContent.includes('128,000'));
    assert.match(await page.locator('#mainWorkflowSummary').textContent(), /128,000/);
    assert.match(await page.locator('#mainWorkflowSummary').textContent(), /75%/);
    assert.match(await page.locator('#mainWorkflowSummary').textContent(), /完整生成后发送/);
    assert.match(await page.locator('.role-budget').first().textContent(), /low/);
    const output = process.env.WECHATBOT_SCREENSHOT_DIR;
    if (output) {
      fs.mkdirSync(output, {recursive:true});
      await page.screenshot({path:path.join(output,'models-desktop.png'),fullPage:true});
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (output) await page.screenshot({path:path.join(output,'models-mobile.png'),fullPage:false});
    assert.deepEqual(errors, []);
    console.log('Model controls: PASS (synthetic-only guard, cap, draft/save, streaming/summary, mobile overflow)');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exitCode=1;});
