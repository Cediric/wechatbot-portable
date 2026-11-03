'use strict';
// Run actual panel handlers against a synthetic DOM and deferred local replies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.join(__dirname, '..', 'wechat-weflow-bridge-ob11', 'panel_assets');
const script = fs.readFileSync(path.join(assets, 'panel.js'), 'utf8');
const markup = fs.readFileSync(path.join(assets, 'index.html'), 'utf8');
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const stopped = () => ({running: false, service_control: {state: 'stopped', action: 'stop', message: '机器人和 AstrBot 已停止，面板保持打开。', error: ''}});

async function fixture(initial, {configFails = false} = {}) {
  const nodes = new Map(), intervals = [], posts = [], timeouts = [];
  let current = initial, actionReply, heldStatus, holdNextStatus = false, dialogs = 0, statusReads = 0, stoppedPolling = false;
  class Node {
    constructor() { this.listeners = {}; this.children = []; this.dataset = {}; this.disabled = false; this.hidden = false; this.value = ''; this.textContent = ''; this.classList = {toggle() {}}; this.content = 'test-csrf'; }
    set id(value) { this.identifier = value; nodes.set(value, this); }
    get id() { return this.identifier; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    dispatch(name) { for (const fn of this.listeners[name] || []) fn({preventDefault() {}}); }
    querySelectorAll() { return []; }
    showModal() { dialogs++; }
    focus() {}
    remove() {}
  }
  for (const match of markup.matchAll(/\bid="([^"]+)"/g)) { const node = new Node(); node.id = match[1]; }
  for (const [id, action] of [['btnStart', 'start'], ['btnStop', 'stop'], ['btnPause', 'pause'], ['btnExit', 'exit'], ['retryRestore', 'stop']]) nodes.get(id).dataset.action = action;
  const document = {hidden: false, getElementById: id => nodes.get(id), querySelector: () => new Node(),
    querySelectorAll: selector => selector === '[data-action]' ? [...nodes.values()].filter(node => node.dataset.action) : [],
    createElement: () => new Node(), createTextNode: text => ({textContent: text}), addEventListener() {}};
  const response = (body, code = 200) => ({ok: code < 400, status: code, json: async () => body});
  vm.runInNewContext(script, {document, window: {addEventListener() {}}, location: {hash: '#workspace'}, history: {replaceState() {}}, AbortController,
    setTimeout: (fn, ms) => { timeouts.push(ms); return 1; }, clearTimeout() {}, setInterval: fn => intervals.push(fn),
    clearInterval: id => { assert.equal(id, 1); stoppedPolling = true; },
    fetch: async (url, options) => {
      if (url === '/status') {
        statusReads++;
        if (holdNextStatus) { holdNextStatus = false; const captured = current; return new Promise(resolve => { heldStatus = () => resolve(response(captured)); }); }
        return response(current);
      }
      if (url === '/api/config' || url === '/api/config/schema') return response(configFails ? {error: '配置暂时不可读'} : url.endsWith('/schema') ? [] : {}, configFails ? 503 : 200);
      if (['/start', '/stop', '/pause', '/resume', '/api/service/exit'].includes(url)) {
        posts.push({url, options});
        return new Promise(resolve => { actionReply = (body, code = 200) => resolve(response(body, code)); });
      }
      throw new Error(`Unexpected request ${url}`);
    },
  }, {filename: 'panel.js'});
  await flush();
  assert.equal(nodes.get('connectionError').hidden, true);
  return {nodes, posts, timeouts, get dialogs() { return dialogs; },
    get statusReads() { return statusReads; }, get stoppedPolling() { return stoppedPolling; },
    async click(id) { nodes.get(id).dispatch('click'); await flush(); },
    async poll(value) { current = value; intervals[0](); await flush(); },
    async complete(value, code = 200) { current = value; actionReply(value, code); await flush(); },
    async holdPoll(value) { current = value; holdNextStatus = true; intervals[0](); await flush(); },
    async releasePoll() { heldStatus(); await flush(); },
    toasts() { return nodes.get('toastRegion').children.map(node => node.textContent); },
  };
}

(async () => {
  const screen = await fixture({running: true});
  assert.equal(screen.nodes.get('btnStop').textContent, '停止机器人');
  await screen.click('btnStop');
  assert.equal(screen.dialogs, 0);
  assert.equal(screen.posts.length, 1);
  assert.equal(screen.timeouts.at(-1), 60000);
  await screen.poll({running: false, service_control: {state: 'stopping', action: 'stop'}});
  for (const id of ['btnStart', 'btnStop', 'btnPause', 'retryRestore']) assert.equal(screen.nodes.get(id).disabled, true);
  await screen.poll({running: false}); // A poll without newer service state cannot unlock the local request.
  await screen.click('btnStart'); await screen.click('btnStop');
  assert.equal(screen.posts.length, 1);
  await screen.complete(stopped());
  assert.equal(screen.nodes.get('btnStart').disabled, false);
  assert.equal(screen.nodes.get('btnStop').disabled, true);
  assert.match(screen.nodes.get('obState').textContent, /AstrBot 已停止/);
  assert.equal(screen.toasts().at(-1), stopped().service_control.message);
  await screen.click('btnStart');
  assert.equal(screen.posts[1].url, '/start');
  assert.equal(screen.timeouts.at(-1), 60000);
  await screen.complete({running: true, service_control: {state: 'running', action: 'start', message: '服务已启动，等待模型连接。'}});
  assert.equal(screen.nodes.get('btnStop').disabled, false);
  assert.equal(screen.toasts().at(-1), '服务已启动，等待模型连接。');

  const failed = await fixture({running: true});
  await failed.click('btnStop');
  const failure = {ok: false, running: false, error: 'AstrBot 仍在退出', service_control: {state: 'failed', action: 'stop', error: 'AstrBot 仍在退出'}};
  await failed.complete(failure);
  assert.equal(failed.nodes.get('btnStop').disabled, false);
  assert.equal(failed.nodes.get('btnStop').textContent, '重试停止');
  assert.equal(failed.nodes.get('btnStart').disabled, true);
  assert.equal(failed.toasts().at(-1), 'AstrBot 仍在退出');
  await failed.click('btnStop'); await failed.complete(stopped());
  assert.equal(failed.posts.length, 2);

  const restore = await fixture({running: true});
  await restore.click('btnStop');
  await restore.complete({...stopped(), uia_restore_result: {ok: false}});
  assert.equal(restore.nodes.get('btnStart').disabled, true);
  assert.equal(restore.nodes.get('retryRestore').disabled, false);
  assert.match(restore.toasts().at(-1), /重试还原/);

  const stale = await fixture({running: true});
  await stale.holdPoll({running: true});
  await stale.click('btnStop');
  await stale.complete(stopped());
  await stale.releasePoll();
  assert.equal(stale.nodes.get('btnStart').disabled, false, 'Old status requests cannot replace a completed stop');

  const external = await fixture({running: false, service_control: {state: 'starting', action: 'start'}});
  for (const id of ['btnStart', 'btnStop', 'btnPause']) assert.equal(external.nodes.get(id).disabled, true);
  await external.click('btnStart'); assert.equal(external.posts.length, 0);

  const failedStart = await fixture({running: false, service_control: {state: 'failed', action: 'start', error: '桥接启动失败'}});
  assert.equal(failedStart.nodes.get('btnStop').disabled, false);
  assert.equal(failedStart.nodes.get('btnStop').textContent, '停止机器人');
  assert.equal(failedStart.nodes.get('btnStart').disabled, false);
  await failedStart.click('btnStop'); await failedStart.complete(stopped());
  assert.equal(failedStart.posts[0].url, '/stop');

  const configFailed = await fixture(stopped(), {configFails: true});
  await configFailed.click('btnStart');
  assert.equal(configFailed.posts[0].url, '/start', 'Saved backend config can start without the settings editor loading');
  await configFailed.complete({running: true});

  const paused = await fixture({running: true, service_control: {state: 'running', action: 'start', message: '旧的启动结果'}});
  await paused.click('btnPause');
  await paused.complete({running: true, paused: true, service_control: {state: 'running', action: 'start', message: '旧的启动结果'}});
  assert.match(paused.toasts().at(-1), /回复已暂停/);
  const preview = await fixture({preview: true});
  assert.equal(preview.nodes.get('bridgeState').textContent, '预览模式');
  assert.equal(preview.nodes.get('btnExit').disabled, true);

  for (const startingState of [{running: true}, stopped()]) {
    const exit = await fixture(startingState);
    assert.equal(exit.nodes.get('btnExit').disabled, false);
    await exit.holdPoll(startingState);
    await exit.click('btnExit');
    assert.equal(exit.posts[0].url, '/api/service/exit');
    assert.equal(exit.posts[0].options.body, '{}');
    assert.equal(exit.timeouts.at(-1), 60000);
    await exit.click('btnExit');
    assert.equal(exit.posts.length, 1);
    await exit.complete({ok: true});
    assert.equal(exit.stoppedPolling, true);
    await exit.releasePoll();
    const readsAtExit = exit.statusReads;
    await exit.poll({running: true});
    assert.equal(exit.statusReads, readsAtExit, 'Closed session ignores stale polling and visibility events');
    assert.equal(exit.nodes.get('main').hidden, true);
    assert.equal(exit.nodes.get('exitNotice').hidden, false);
    assert.equal(exit.nodes.get('connectionError').hidden, true);
  }
  const couldNotExit = await fixture(stopped());
  await couldNotExit.click('btnExit');
  await couldNotExit.complete({ok: false, error: '服务仍在退出'}, 409);
  assert.equal(couldNotExit.stoppedPolling, false);
  assert.equal(couldNotExit.nodes.get('main').hidden, false);
  assert.equal(couldNotExit.nodes.get('btnExit').disabled, false);
  assert.match(couldNotExit.toasts().at(-1), /服务仍在退出/);
  await couldNotExit.click('btnExit');
  await couldNotExit.complete({ok: true});
  assert.equal(couldNotExit.stoppedPolling, true);
  console.log('Panel service control: PASS (12 behavior scenarios)');
})().catch(error => { console.error(error); process.exitCode = 1; });
