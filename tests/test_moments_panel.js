'use strict';
// Run the real panel with synthetic HTTP and DOM, without a browser or WeChat.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.join(__dirname, '..', 'wechat-weflow-bridge-ob11', 'panel_assets');
const script = fs.readFileSync(path.join(assets, 'panel.js'), 'utf8');
const markup = fs.readFileSync(path.join(assets, 'index.html'), 'utf8');
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const ident = '0123456789abcdef0123456789abcdef';
const pending = {version: 1, status: 'pending_review', intent_id: ident, type: 'reply', content: '<img onerror=evil()>动态', text: '<script>evil()</script>回复', author_name: '合成作者', comment_author_id: 'wxid_commenter', comment_author_name: '<script>评论者</script>', comment_content: '<img>原评论', time: 1791710000};

async function fixture(changes = {}) {
  const nodes = new Map(), polls = [], posts = [];
  let current = {running: true, paused: false, moments_interactions: {enabled: true, running: false, pending: null, daily: {like: 2, comment: 1, reply: 3}, recent: []}, ...changes};
  let dialogCount = 0, resolutionCode = 200, browseCode = 202, announcementCode = 202;
  class Node {
    constructor() {
      this.listeners = new Map(); this.children = []; this.dataset = {};
      this.disabled = false; this.hidden = false; this.value = ''; this.textContent = '';
      this.classList = {toggle() {}}; this.content = 'synthetic-csrf';
    }
    set id(value) { this.identifier = value; nodes.set(value, this); }
    get id() { return this.identifier; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    addEventListener(event, callback, options) {
      const list = this.listeners.get(event) || [];
      list.push({callback, once: options?.once}); this.listeners.set(event, list);
    }
    dispatch(event) {
      const list = this.listeners.get(event) || [];
      this.listeners.set(event, list.filter(item => !item.once));
      return list.map(item => item.callback({preventDefault() {}}));
    }
    showModal() { dialogCount++; }
    querySelectorAll() { return []; }
    remove() {}
  }
  for (const match of markup.matchAll(/\bid="([^"]+)"/g)) { const node = new Node(); node.id = match[1]; }
  const context = {
    document: {hidden: false, getElementById: id => nodes.get(id), querySelector: () => new Node(), querySelectorAll: () => [], createElement: () => new Node(), createTextNode: text => ({textContent: text}), addEventListener() {}},
    window: {addEventListener() {}}, location: {hash: '#workspace'}, history: {replaceState() {}},
    AbortController, setTimeout: () => 1, clearTimeout() {}, setInterval: callback => polls.push(callback),
    fetch: async (url, options) => {
      let body, code = 200;
      if (url === '/status') body = current;
      else if (url === '/api/config') body = {moments_browse_interval_min: 15, moments_active_start_hour: 8, moments_active_end_hour: 23, moments_like_daily_limit: 12, moments_comment_daily_limit: 4, moments_reply_daily_limit: 8, moments_interaction_contacts: []};
      else if (url === '/api/config/schema') body = [];
      else if (url === '/api/autopilot/first-online') {
        posts.push({url, options, body: JSON.parse(options.body)});
        code = announcementCode;
        if (code === 202) current = {...current, autopilot_schedule: {first_online: {state: 'running', message: '正在创作'}}};
        body = code === 202 ? {ok: true, message: '已开始创作'} : {ok: false, error: '已有朋友圈发布任务正在处理。'};
      } else if (url === '/api/moments/interactions/run') {
        posts.push({url, options, body: JSON.parse(options.body)});
        code = browseCode; body = code === 202 ? {ok: true, message: '已安排浏览'} : {ok: false, error: '桥接已暂停'};
        if (code === 202) current = {...current, moments_interactions: {...current.moments_interactions, running: true}};
      } else if (url === '/api/moments/interactions/resolve') {
        posts.push({url, options, body: JSON.parse(options.body)});
        code = resolutionCode;
        if (code === 200) current = {...current, moments_interactions: {...current.moments_interactions, pending: null}};
        body = code === 200 ? {ok: true, moments_interactions: current.moments_interactions} : {ok: false, error: '核对记录已变化'};
      } else if (url === '/api/autopilot/resolve-pending') {
        posts.push({url, options, body: JSON.parse(options.body)});
        current = {...current, autopilot_pending: null}; body = {ok: true, autopilot_pending: null};
      } else throw new Error(`Unexpected request ${url}`);
      return {ok: code < 400, status: code, json: async () => body};
    },
  };
  vm.runInNewContext(script, context, {filename: 'panel.js'}); await flush();
  assert.equal(nodes.get('connectionError').hidden, true, 'The real panel completed status rendering');
  return {
    nodes, posts, get dialogCount() { return dialogCount; },
    async poll(changes) { current = {...current, ...changes}; polls[0](); await flush(); },
    async click(id) { nodes.get(id).dispatch('click'); await flush(); },
    async confirm() { const dialog = nodes.get('confirmDialog'); dialog.returnValue = 'confirm'; dialog.dispatch('close'); await flush(); },
    failResolution() { resolutionCode = 409; }, failBrowse() { browseCode = 409; },
    announcementResponse(code) { announcementCode = code; },
  };
}

(async () => {
  const screen = await fixture();
  const firstOnline = await fixture({autopilot_enabled: false, autopilot_schedule: {first_online: {state: 'ready'}}});
  assert.equal(firstOnline.posts.length, 0);
  assert.equal(firstOnline.nodes.get('firstOnlinePublish').disabled, false);
  await firstOnline.click('firstOnlinePublish');
  await firstOnline.click('firstOnlinePublish');
  assert.equal(firstOnline.posts.length, 1);
  assert.equal(firstOnline.posts[0].url, '/api/autopilot/first-online');
  assert.deepEqual(firstOnline.posts[0].body, {});
  assert.equal(firstOnline.posts[0].options.headers['X-Panel-Token'], 'synthetic-csrf');
  await firstOnline.poll({autopilot_pending: pending});
  assert.match(firstOnline.nodes.get('firstOnlineResult').textContent, /正在创作/);
  assert.equal(firstOnline.nodes.get('autopilotPendingCard').hidden, true, 'In-flight submission does not ask the user to confirm its own journal');
  await firstOnline.poll({autopilot_pending: null, autopilot_schedule: {first_online: {state: 'published', message: '此账号已发表'}}});
  assert.equal(firstOnline.nodes.get('firstOnlinePublish').disabled, true);
  assert.match(firstOnline.nodes.get('firstOnlineResult').textContent, /已发表/);
  const rejectedPost = await fixture({autopilot_schedule: {first_online: {state: 'ready'}}});
  rejectedPost.announcementResponse(409);
  await rejectedPost.click('firstOnlinePublish');
  await rejectedPost.poll({});
  assert.match(rejectedPost.nodes.get('firstOnlineResult').textContent, /已有朋友圈发布任务/);
  rejectedPost.announcementResponse(202);
  await rejectedPost.click('firstOnlinePublish');
  await rejectedPost.poll({autopilot_schedule: {first_online: {state: 'ready', message: '配图生成失败，请核对生图模型。'}}});
  assert.match(rejectedPost.nodes.get('firstOnlineResult').textContent, /配图生成失败/);
  assert.equal(rejectedPost.nodes.get('firstOnlinePublish').disabled, false);
  for (const changes of [{running: false}, {paused: true}, {preview: true}, {autopilot_pending: pending}]) {
    const blocked = await fixture({autopilot_schedule: {first_online: {state: 'ready'}}, ...changes});
    await blocked.click('firstOnlinePublish');
    assert.equal(blocked.posts.length, 0);
  }
  assert.equal(screen.nodes.get('momentsBrowseNow').disabled, false);
  assert.equal(screen.nodes.get('momentsLikeCount').textContent, '2 / 12');
  assert.equal(screen.nodes.get('momentsCommentCount').textContent, '1 / 4');
  assert.equal(screen.nodes.get('momentsReplyCount').textContent, '3 / 8');
  assert.match(screen.nodes.get('momentsInteractionPlan').textContent, /15 分钟.*08:00–23:00.*全部可见/);
  const missingAccount = await fixture({moments_interactions: {enabled: true, running: true,
    blocked_reason: 'account_identity_missing', last_check: 1791958940, last_result: '未运行：请配置当前账号的真实微信 ID'},
    moments_manual_request: {status: 'unavailable', blocked_reason: 'account_identity_missing', error: '缺少机器人账号', finished_at: 1791958941}});
  assert.equal(missingAccount.nodes.get('momentsInteractionBadge').textContent, '待配置账号');
  assert.equal(missingAccount.nodes.get('momentsAccountSetting').hidden, false);
  assert.match(missingAccount.nodes.get('momentsCheckStatus').textContent, /真实微信编号/);
  assert.match(missingAccount.nodes.get('momentsManualStatus').textContent, /缺少机器人账号/);
  const publishWait = await fixture({autopilot_enabled: true,
    autopilot_schedule: {next_run: 1791959900, last_check: 1791959000, blocked_reason: 'private_priority'}});
  assert.equal(publishWait.nodes.get('autopilotBadge').textContent, '等待条件');
  assert.match(publishWait.nodes.get('autopilotSchedule').textContent, /下次发布决策.*私聊结束/);
  const requestFailure = await fixture();
  requestFailure.failBrowse(); await requestFailure.click('momentsBrowseNow');
  assert.equal(requestFailure.nodes.get('momentsManualStatus').hidden, false);
  assert.match(requestFailure.nodes.get('momentsManualStatus').textContent, /桥接已暂停/);
  await requestFailure.poll({});
  assert.match(requestFailure.nodes.get('momentsManualStatus').textContent, /桥接已暂停/, 'Manual rejection remains visible after the toast and status polling');
  const actionError = {status: 'failed', message: '合成账号核验失败 <script>', time: 1791958940, mode: 'browse'};
  const retainedFailure = await fixture({moments_interactions: {enabled: true,
    last_check: 1791958940, last_result: '合成执行失败', last_action_error: actionError},
    moments_manual_request: {status: 'failed', error: '合成手动失败', finished_at: 1791958941}});
  await retainedFailure.poll({moments_interactions: {enabled: true,
    last_check: 1791958990, last_result: '已检查 20 条动态，暂无适合互动的新内容', last_action_error: actionError}});
  assert.equal(retainedFailure.nodes.get('momentsInteractionBadge').textContent, '最近执行失败');
  assert.match(retainedFailure.nodes.get('momentsInteractionBadge').className, /warning/);
  assert.match(retainedFailure.nodes.get('momentsInteractionResult').textContent, /^最近检查：已检查 20/);
  assert.match(retainedFailure.nodes.get('momentsCheckStatus').textContent, /最近执行错误（主动浏览）：合成账号核验失败 <script>/);
  assert.match(retainedFailure.nodes.get('momentsManualStatus').textContent, /合成手动失败/);
  await retainedFailure.poll({moments_interactions: {enabled: true,
    last_check: 1791959000, last_result: '点赞已被微信界面接受', last_action_error: {}}});
  assert.equal(retainedFailure.nodes.get('momentsInteractionBadge').textContent, '定时浏览');
  assert.doesNotMatch(retainedFailure.nodes.get('momentsCheckStatus').textContent, /最近执行错误/);
  assert.match(retainedFailure.nodes.get('momentsManualStatus').textContent, /合成手动失败/, 'Manual result remains its own history after another verified action succeeds');
  const automaticFailure = await fixture({moments_interactions: {enabled: true,
    last_result: '已检查 20 条动态，暂无适合互动的新内容',
    last_action_error: {status: 'model_failed', message: '合成自动判断失败', time: 1791958940, mode: 'reply_poll'}}});
  assert.equal(automaticFailure.nodes.get('momentsInteractionBadge').textContent, '最近执行失败');
  assert.equal(automaticFailure.nodes.get('momentsCheckStatus').hidden, false);
  assert.match(automaticFailure.nodes.get('momentsCheckStatus').textContent, /最近执行错误（评论检查）：合成自动判断失败/);
  for (const identity of ['missing', 'partial']) {
    const limited = await fixture({moments_interactions: {enabled: true, feed: {reply_identity: identity, unverified_comment_count: 2}}});
    assert.equal(limited.nodes.get('momentsReplyIdentityHint').hidden, false);
    assert.match(limited.nodes.get('momentsReplyIdentityHint').textContent, /2 条评论.*微信 ID.*跳过.*浏览、点赞与评论/);
    assert.equal(limited.nodes.get('momentsBrowseNow').disabled, false, 'Missing comment identities only block the unverified replies');
  }
  const verified = await fixture({moments_interactions: {enabled: true, feed: {reply_identity: 'available'}}});
  assert.equal(verified.nodes.get('momentsReplyIdentityHint').hidden, true);
  const skipped = await fixture({moments_interactions: {enabled: true, feed: {reply_identity: 'available', skipped_post_count: 3}}});
  assert.equal(skipped.nodes.get('momentsReplyIdentityHint').hidden, false);
  assert.match(skipped.nodes.get('momentsReplyIdentityHint').textContent, /3 条动态身份或正文不一致，已略过/);
  assert.equal(skipped.nodes.get('momentsBrowseNow').disabled, false, 'Rejected individual posts do not block other verified targets');
  const mixed = await fixture({moments_interactions: {enabled: true, feed: {reply_identity: 'partial', unverified_comment_count: 2, skipped_post_count: 1}}});
  assert.match(mixed.nodes.get('momentsReplyIdentityHint').textContent, /2 条评论.*微信 ID.*1 条动态身份或正文不一致/);
  for (const count of [0, -1, null, '<img onerror=evil()>', Infinity]) {
    const empty = await fixture({moments_interactions: {enabled: true, feed: {reply_identity: 'available', skipped_post_count: count}}});
    assert.equal(empty.nodes.get('momentsReplyIdentityHint').hidden, true, 'Absent or invalid counts never become warning text');
  }
  await screen.click('momentsBrowseNow'); await screen.click('momentsBrowseNow');
  assert.equal(screen.posts.length, 1, 'A queued run cannot be submitted twice');
  assert.deepEqual(screen.posts[0].body, {}, 'Browsing never sends enable/force flags');
  assert.equal(screen.posts[0].options.headers['X-Panel-Token'], 'synthetic-csrf');
  assert.equal(screen.nodes.get('momentsBrowseNow').disabled, true);
  assert.equal(screen.nodes.get('momentsInteractionBadge').textContent, '浏览中');
  for (const changes of [{running: false}, {paused: true}, {preview: true}, {uia_restore_result: {ok: false}},
      {moments_interactions: {enabled: false}}, {moments_interactions: {enabled: true, pending}}, {moments_interactions: {enabled: true, running: true}}]) {
    const blocked = await fixture(changes);
    assert.equal(blocked.nodes.get('momentsBrowseNow').disabled, true);
    await blocked.click('momentsBrowseNow'); assert.equal(blocked.posts.length, 0);
  }
  const review = await fixture({moments_interactions: {enabled: false, pending, daily: {}, recent: [{type: 'reply', author_name: '原动态作者', comment_author_name: '<script>', comment_content: '<img>原评论', text: '<img>回复', time: 1000, status: 'published'}]}});
  assert.equal(review.nodes.get('momentsInteractionNotice').hidden, false, 'Pending is visible even when disabled');
  assert.equal(review.nodes.get('momentsInteractionPending').hidden, false);
  assert.match(review.nodes.get('momentsInteractionPendingContent').textContent, /<img onerror=evil\(\)>动态/);
  assert.match(review.nodes.get('momentsInteractionPendingContent').textContent, /<script>evil\(\)<\/script>回复/);
  assert.match(review.nodes.get('momentsInteractionPendingContent').textContent, /wxid_commenter/);
  assert.match(review.nodes.get('momentsInteractionPendingContent').textContent, /<img>原评论/);
  assert.equal(review.nodes.get('momentsInteractionRecent').children[0].children[0].textContent, '回复评论 · <script>', 'User prose stays plain text');
  assert.equal(review.nodes.get('momentsInteractionRecent').children[0].children[1].textContent, '原评论：<img>原评论');
  await review.click('momentsInteractionCompleted'); await review.click('momentsInteractionNotCompleted');
  assert.equal(review.dialogCount, 1); assert.equal(review.posts.length, 0);
  await review.confirm();
  assert.equal(review.posts.length, 1);
  assert.deepEqual(review.posts[0].body, {outcome: 'published', intent_id: ident, confirm: 'checked_in_wechat'});
  assert.equal(review.nodes.get('momentsInteractionPending').hidden, true);

  const changed = await fixture({moments_interactions: {enabled: true, pending}});
  await changed.click('momentsInteractionNotCompleted');
  await changed.poll({moments_interactions: {enabled: true, pending: {...pending, intent_id: 'f'.repeat(32)}}});
  await changed.confirm();
  assert.equal(changed.posts.length, 0, 'Stale confirmation cannot resolve a replacement');
  assert.equal(changed.nodes.get('momentsInteractionPending').hidden, false);

  const rejected = await fixture({moments_interactions: {enabled: true, pending}});
  rejected.failResolution(); await rejected.click('momentsInteractionNotCompleted'); await rejected.confirm();
  assert.equal(rejected.posts.length, 1, 'A 409 never triggers a retry');
  assert.equal(rejected.posts[0].body.outcome, 'not_published');
  assert.equal(rejected.nodes.get('momentsInteractionPending').hidden, false);

  for (const changes of [{preview: true, moments_interactions: {pending}}, {moments_interactions: {pending: {status: 'record_error', error: '损坏'}}},
      {moments_interactions: {running: true, pending}}, {moments_interactions: {pending: {...pending, comment_author_id: ''}}}, {moments_interactions: {pending: {...pending, comment_content: ''}}},
      {moments_interactions: {pending: {...pending, intent_id: 'client-id'}}}, {moments_interactions: {pending: {...pending, type: 'send'}}}]) {
    const blocked = await fixture(changes);
    assert.equal(blocked.nodes.get('momentsInteractionCompleted').disabled, true);
    assert.equal(blocked.nodes.get('momentsInteractionNotCompleted').disabled, true);
    await blocked.click('momentsInteractionCompleted'); assert.equal(blocked.dialogCount, 0); assert.equal(blocked.posts.length, 0);
  }
  const concurrent = await fixture({autopilot_pending: {status: 'pending_review', intent_id: 'autopilot-id', content: '合成动态'}, moments_interactions: {enabled: true, pending}});
  await concurrent.click('autopilotPublished'); await concurrent.click('momentsInteractionCompleted');
  assert.equal(concurrent.dialogCount, 1, 'Two features cannot replace the same open confirmation dialog');
  await concurrent.confirm();
  assert.equal(concurrent.posts.length, 1); assert.equal(concurrent.posts[0].url, '/api/autopilot/resolve-pending');
  const diagnostics = await fixture({moments_interactions: {enabled: true,
    last_result: '朋友圈刷新失败：列表无法确认',
    last_media_error: {message: '原图读取失败 <img>', time: 1000},
    recent: [{type: 'comment', author_name: '作者', text: '合成评论', status: 'failed', error: '定位失败 <script>'}]}});
  assert.equal(diagnostics.nodes.get('momentsMediaStatus').hidden, false);
  assert.match(diagnostics.nodes.get('momentsMediaStatus').textContent, /原图读取失败 <img>/);
  assert.match(diagnostics.nodes.get('momentsInteractionRecent').children[0].children[2].textContent, /原因：定位失败 <script>/);
  await diagnostics.poll({moments_interactions: {enabled: true, last_result: '本轮略过', last_media_error: {message: '原图读取失败 <img>', time: 1000}}});
  assert.equal(diagnostics.nodes.get('momentsMediaStatus').hidden, false, 'Later refresh results cannot hide the retained media failure');
  const observedSender = await fixture({uia: {ready: true, snapshot_source: 'operation_observation', snapshot_timestamp: 1000}});
  assert.equal(observedSender.nodes.get('senderState').textContent, 'UIA · 最近操作观察');
  assert.match(observedSender.nodes.get('flowSend').title, /最近操作观察/);
  const busySender = await fixture({uia: {ready: true, busy: true, snapshot_source: 'operation_observation'}});
  assert.equal(busySender.nodes.get('senderState').textContent, 'UIA · 发送处理中');
  const checkedSender = await fixture({uia: {ready: true, snapshot_source: 'fresh_diagnostic', snapshot_timestamp: 1000}});
  assert.equal(checkedSender.nodes.get('senderState').textContent, 'UIA · 最近检查可用');
  const performance = await fixture({send_queue: {pending: 3, active: {elapsed_seconds: 4.2}},
    runtime_watchdog: {state: 'recovering', reason: '等待当前发送结束', restart_count: 1},
    response_performance: {stages: {first_text: {latest_seconds: 12, average_seconds: 9, p95_seconds: 16, sample_count: 4},
      wechat_send: {latest_seconds: .8, average_seconds: 1, p95_seconds: 1.5, sample_count: 8}}},
    tts_cache: {hits: 2, misses: 1, coalesced_waits: 1, last_synthesis_seconds: 2, synthesis_window_average_seconds: 2,
      synthesis_p95_seconds: 2, synthesis_samples: 1}});
  assert.equal(performance.nodes.get('runtimeHealthBadge').textContent, '恢复中');
  assert.match(performance.nodes.get('sendProgress').textContent, /4.20 秒.*3 条排队/);
  const timingRows = performance.nodes.get('responseTimingRows').children;
  assert.equal(timingRows[0].children[1].textContent, '12.00');
  assert.equal(timingRows[0].children[4].textContent, '4');
  assert.equal(timingRows[6].children[1].textContent, '0.80', 'UI send latency is separate from end-to-end first text');
  assert.equal(timingRows[1].children[1].textContent, '—', 'Missing voice samples are not invented');
  assert.match(performance.nodes.get('voiceCacheSummary').textContent, /命中 2 次/);
  await performance.poll({runtime_watchdog: {state: 'restarted', reason: '已恢复处理'}, send_queue: {pending: 0}});
  assert.equal(performance.nodes.get('runtimeHealthBadge').textContent, '已恢复');
  assert.equal(performance.nodes.get('sendProgress').textContent, '当前没有排队或发送任务');
  assert.equal(performance.posts.length, 0, 'Performance polling never triggers runtime recovery or sending');
  await performance.poll({tts_cache: {hits: 0, misses: 0, synthesis_samples: 0, last_synthesis_seconds: 0,
    synthesis_window_average_seconds: 0, synthesis_p95_seconds: 0}});
  assert.equal(performance.nodes.get('responseTimingRows').children[3].children[1].textContent, '—', 'No synthesis samples cannot appear as zero latency');
  console.log('Moments panel behavior: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
