// Synthetic preview only. No process memory, desktop or real clipboard access.
const assert = require('node:assert/strict');
const playwright = require(process.env.WECHATBOT_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const configured = new URL(process.env.WECHATBOT_PREVIEW_URL || 'http://127.0.0.1:8881');
  assert.equal(configured.protocol, 'http:');
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(configured.hostname));
  const origin = configured.origin;
  const expected = {profile_id:'synthetic-legacy', file_version:'4.1.13.65', sha256:'a'.repeat(64), size_of_image:198553600, architecture:'AMD64'};
  const second = {profile_id:'synthetic-second', file_version:'4.9.0.1', sha256:'c'.repeat(64), size_of_image:65536, architecture:'AMD64',review_basis:'static_equivalence'};
  let recovery = {
    enabled:true, active:false, status:'unavailable', busy:false,
    supported_builds:[expected,second], selected_profile:null, verified_build:null,
    observed_build:{file_version:'4.1.15.50', sha256:'b'.repeat(64), size_of_image:200000000, architecture:'unknown'},
    build_check:{status:'version_mismatch', reason:'微信模块版本不匹配；已停止恢复。'},
    last_error:'微信模块版本不匹配；已停止恢复。', restore_result:null, lease:null,
  };
  const browser = await playwright.chromium.launch({channel:'msedge', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'clipboard', {
        value:{writeText:async text => { window.syntheticDiagnosticCopy = text; }}, configurable:true,
      });
    });
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin || !['GET','HEAD'].includes(request.method())) {
        errors.push(`Forbidden request: ${request.method()} ${url.pathname}`);
        return route.abort();
      }
      if (url.pathname === '/api/uia/diagnostics') {
        return route.fulfill({contentType:'application/json', body:JSON.stringify({ok:true, diagnostics:{ready:false, current_chat:'CHAT_CANARY', gate_recovery:recovery}})});
      }
      return route.continue();
    });
    const rows = () => page.locator('#uiaDiagnostics').evaluate(node => Object.fromEntries(
      [...node.querySelectorAll('.diagnostic-row')].map(row => [row.children[0].textContent, row.children[1].textContent])
    ));
    await page.goto(origin + '/#diagnostics');
    const status = await page.evaluate(async () => (await fetch('/api/status')).json());
    assert.equal(status.preview, true, 'Only test a synthetic preview');
    await page.getByRole('button', {name:'复制恢复检查结果', exact:true}).waitFor();
    let observed = await rows();
    assert.equal(observed['实际微信模块版本'], '4.1.15.50');
    assert.equal(observed['支持的微信模块版本'], '4.1.13.65、4.9.0.1');
    assert.equal(observed['构建核验'], '尚未匹配已复核版本');
    assert.equal(observed['已复核恢复构建'], '2 项 · 4.1.13.65、4.9.0.1');
    assert.equal(observed['匹配恢复方案'], '未匹配，不执行恢复');
    assert.equal(observed['实际模块架构'], '未采集');
    assert.equal(observed['恢复状态'], '未能恢复');
    await page.getByRole('button', {name:'复制恢复检查结果', exact:true}).click();
    const copied = await page.evaluate(() => window.syntheticDiagnosticCopy);
    assert.ok(copied && !copied.includes('CHAT_CANARY'));
    assert.deepEqual(JSON.parse(copied).supported_builds, [expected,second]);
    assert.equal(JSON.parse(copied).selected_profile, null);
    assert.equal(JSON.parse(copied).verified_build, null);
    assert.equal(JSON.parse(copied).observed_build.file_version, '4.1.15.50');

    recovery = {...recovery, observed_build:{...recovery.observed_build,file_version:'4.1.13.65'},
      build_check:{status:'sha256_mismatch',reason:'同版本 SHA256 不匹配。'},last_error:'同版本 SHA256 不匹配。'};
    await page.locator('#refreshDiagnostics').click();
    await page.waitForFunction(() => document.getElementById('uiaDiagnostics').textContent.includes('同版本模块 SHA256 未匹配复核方案'));
    observed = await rows();
    assert.equal(observed['实际模块 SHA256'], 'b'.repeat(64));
    assert.equal(observed['已复核模块 SHA256'], 'a'.repeat(64));

    recovery = {...recovery, observed_build:second, selected_profile:second, verified_build:second,
      build_check:{status:'matched',reason:'模块身份已匹配，仍需核验控件。'},last_error:''};
    await page.locator('#refreshDiagnostics').click();
    await page.waitForFunction(() => document.getElementById('uiaDiagnostics').textContent.includes('synthetic-second'));
    observed = await rows();
    assert.equal(observed['匹配恢复方案'], 'synthetic-second');
    assert.equal(observed['支持的微信模块版本'], '4.9.0.1');
    assert.equal(observed['实际模块架构'], 'AMD64');
    assert.equal(observed['构建核验'], '已匹配复核方案，仍需检查 UIA 控件');
    assert.equal(observed['匹配方案验证范围'], '离线静态等价复核；本机 UIA 控件与发送须另行核验');

    recovery = {...recovery, selected_profile:null, verified_build:null,
      observed_build:{file_version:null,sha256:null,size_of_image:null,architecture:'unknown'},
      build_check:{status:'version_unavailable',reason:'未能读取版本。'},last_error:'未能读取版本。'};
    await page.locator('#refreshDiagnostics').click();
    await page.waitForFunction(() => document.getElementById('uiaDiagnostics').textContent.includes('微信模块版本读取失败'));
    observed = await rows();
    assert.equal(observed['实际微信模块版本'], '未能读取');
    assert.equal(observed['实际模块映像大小'], '未能读取');
    assert.deepEqual(errors, []);
    console.log('PASS: UIA multiple profiles, exact selected comparison, unverified runtime scope, unavailable metadata and safe synthetic copy');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
