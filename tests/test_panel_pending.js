'use strict';
// Execute the production panel script with a small DOM and synthetic HTTP
// responses. No browser, socket, WeChat or external service is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.join(__dirname, '..', 'wechat-weflow-bridge-ob11', 'panel_assets');
const script = fs.readFileSync(path.join(assets, 'panel.js'), 'utf8');
const markup = fs.readFileSync(path.join(assets, 'index.html'), 'utf8');
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

async function fixture(pending, preview = false) {
  const nodes = new Map(), intervalCallbacks = [], posts = [];
  let currentPending = pending, dialogCount = 0, responseCode = 200;
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
  const document = {
    hidden: false,
    getElementById: id => nodes.get(id) || null,
    querySelector: () => new Node(), querySelectorAll: () => [],
    createElement: () => new Node(), createTextNode: text => ({textContent: text}),
    addEventListener() {},
  };
  const context = {
    document, window: {addEventListener() {}},
    location: {hash: '#workspace'}, history: {replaceState() {}},
    AbortController, setTimeout: () => 1, clearTimeout() {},
    setInterval: callback => intervalCallbacks.push(callback),
    fetch: async (url, options) => {
      let body;
      if (url === '/status') body = {autopilot_pending: currentPending, autopilot_enabled: false, preview};
      else if (url === '/api/config') body = {};
      else if (url === '/api/config/schema') body = [];
      else if (url === '/api/autopilot/resolve-pending') {
        posts.push({url, options, body: JSON.parse(options.body)});
        body = responseCode === 200 ? {ok: true, autopilot_pending: null, message: '核对已保存，本次不会立即重发'} : {ok: false, error: '核对记录已变化'};
        if (responseCode === 200) currentPending = null;
        return {ok: responseCode === 200, status: responseCode, json: async () => body};
      } else throw new Error(`Unexpected request ${url}`);
      return {ok: true, status: 200, json: async () => body};
    },
  };
  vm.runInNewContext(script, context, {filename: 'panel.js'});
  await flush();
  assert.equal(nodes.get('connectionError').hidden, true, 'Production status rendering completed');
  return {
    nodes, posts, get dialogCount() { return dialogCount; },
    async poll(value) { currentPending = value; intervalCallbacks[0](); await flush(); },
    async click(id) { nodes.get(id).dispatch('click'); await flush(); },
    async confirm() { const dialog = nodes.get('confirmDialog'); dialog.returnValue = 'confirm'; dialog.dispatch('close'); await flush(); },
    failResolution() { responseCode = 409; },
  };
}

(async () => {
  const pending = {status: 'pending_review', intent_id: 'intent-1', content: '<img src=x onerror=evil()>测试正文', time: '2026-10-11 18:20:30', image_path: 'synthetic.png'};
  const screen = await fixture(pending);
  assert.equal(screen.nodes.get('autopilotPendingNotice').hidden, false);
  assert.equal(screen.nodes.get('autopilotPendingCard').hidden, false);
  assert.equal(screen.nodes.get('autopilotPendingContent').textContent, pending.content, 'Untrusted content is rendered as text');
  assert.match(screen.nodes.get('autopilotPendingMeta').textContent, /含配图/);
  assert.equal(screen.nodes.get('autopilotBadge').textContent, '待核对', 'Pending remains visible when autopilot is disabled');
  await screen.click('autopilotPublished');
  await screen.click('autopilotNotPublished');
  assert.equal(screen.dialogCount, 1, 'Only one resolution dialog can run');
  assert.equal(screen.posts.length, 0, 'Opening the dialog does not resolve or publish');
  assert.equal(screen.nodes.get('autopilotPublished').disabled, true);
  assert.equal(screen.nodes.get('autopilotNotPublished').disabled, true);
  await screen.confirm();
  assert.equal(screen.posts.length, 1);
  assert.deepEqual(screen.posts[0].body, {outcome: 'published', intent_id: 'intent-1', confirm: 'checked_in_wechat'});
  assert.equal(screen.posts[0].options.headers['X-Panel-Token'], 'synthetic-csrf');
  assert.equal(screen.nodes.get('autopilotPendingCard').hidden, true);

  const changed = await fixture(pending);
  await changed.click('autopilotNotPublished');
  await changed.poll({...pending, intent_id: 'intent-2'});
  await changed.confirm();
  assert.equal(changed.posts.length, 0, 'A replaced intent cannot be resolved by the old dialog');
  assert.equal(changed.nodes.get('autopilotPendingCard').hidden, false);
  assert.equal(changed.nodes.get('autopilotNotPublished').disabled, false);

  const rejected = await fixture(pending);
  rejected.failResolution();
  await rejected.click('autopilotNotPublished'); await rejected.confirm();
  assert.equal(rejected.posts.length, 1, 'HTTP rejection is never retried automatically');
  assert.equal(rejected.posts[0].body.outcome, 'not_published');
  assert.equal(rejected.nodes.get('autopilotPendingCard').hidden, false);

  for (const [value, preview] of [[{status: 'record_error', error: '记录损坏'}, false], [pending, true]]) {
    const blocked = await fixture(value, preview);
    assert.equal(blocked.nodes.get('autopilotPendingCard').hidden, false);
    assert.equal(blocked.nodes.get('autopilotPublished').disabled, true);
    assert.equal(blocked.nodes.get('autopilotNotPublished').disabled, true);
    await blocked.click('autopilotPublished'); await blocked.click('autopilotNotPublished');
    assert.equal(blocked.dialogCount, 0);
    assert.equal(blocked.posts.length, 0);
  }
  console.log('Panel pending behavior: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
