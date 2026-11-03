'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const csrf = document.querySelector('meta[name="panel-csrf"]').content;
  const pageNames = {workspace: '会话工作台', models: '模型工作室', moments: '朋友圈轮询', media: '生图与参考图', voice: '声音与克隆', settings: '全部设置', memory: '共享记忆', diagnostics: '发送与设备', logs: '运行日志'};
  const configPages = ['settings', 'models', 'moments', 'media', 'voice'];
  const modelGroups = ['主模型', '共享记忆', '群消息回复判断', 'AI 自主朋友圈', '拟真与朋友圈决策模型'];
  let configurationSections = [], references = [];
  const modeNames = {mention: '仅在群聊被 @ 时回应', all: '回应每条符合规则的群消息', batch: '合并同一会话的连续群消息'};
  const features = [
    ['human_like_enabled', '自然短句', '用短气泡表达完整意思'],
    ['memory_enabled', '记住对话', '让后续回应有上下文'],
    ['sticker_enabled', '表情包', '给对话一点情绪'],
    ['voice_enabled', '语音回复', '允许合成语音发送'],
    ['tools_enabled', '联网与生图', '需要时使用内置工具'],
  ];
  let page = 'workspace', configData = {}, schema = [], dirty = false, settingsReady = false, configLoadPromise = null;
  let statusBusy = false, status = {}, memoryOffset = 0, memoryTotal = 0, lastLog = '', memoryRequest = 0, logRequest = 0;
  let pendingResolving = false, socialResolving = false, socialBrowsing = false, confirmationOpen = false;
  let socialRequestError = null;
  let announcementRequested = false, announcementError = '';
  let controlAction = '', controlRevision = 0;
  const memoryLimit = 30;
  const clone = value => JSON.parse(JSON.stringify(value));
  const valueSame = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function toast(message, type = 'success') {
    const node = element('div', `toast ${type}`, message);
    $('toastRegion').append(node);
    setTimeout(() => node.remove(), type === 'error' ? 8000 : 4200);
  }
  async function api(url, data, timeoutMs = 15000) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const options = {credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: {'Accept': 'application/json'}};
      if (data !== undefined) {
        options.method = 'POST';
        options.headers['Content-Type'] = 'application/json';
        options.headers['X-Panel-Token'] = csrf;
        options.body = JSON.stringify(data);
      }
      const response = await fetch(url, options);
      let body;
      try { body = await response.json(); } catch (_) { throw new Error('面板返回了无效的数据，请刷新后重试'); }
      if (response.status === 401) {
        showConnectionError('面板会话已过期，请刷新页面后继续。');
        $('retryConnection').textContent = '刷新页面';
        $('retryConnection').dataset.expired = 'true';
      }
      if (!response.ok) {
        const error = new Error(body.error || `请求失败（${response.status}）`);
        error.status = typeof body.status === 'string' ? body.status : '';
        throw error;
      }
      return body;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('操作等待超时，请核对服务结果后重试；创建音色请先刷新列表，避免重复创建');
      if (error instanceof TypeError) throw new Error('无法连接本机面板，请确认启动脚本仍在运行');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function showConnectionError(message) {
    if (page === 'closed') return;
    $('connectionError').hidden = false;
    $('connectionErrorText').textContent = message;
    $('connectionBadge').textContent = '连接异常';
    $('connectionBadge').className = 'badge error';
  }
  function empty(target, title, hint, retry) {
    const node = element('div', 'empty');
    node.append(element('span', 'empty-symbol', '☾'), element('strong', '', title), element('p', '', hint));
    if (retry) {
      const button = element('button', 'text-button', '重新加载');
      button.type = 'button'; button.addEventListener('click', retry); node.append(button);
    }
    target.replaceChildren(node);
  }
  async function busy(button, callback) {
    const wasDisabled = button.disabled;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    try { await callback(); } catch (error) { toast(error.message, 'error'); }
    finally { button.disabled = wasDisabled; button.removeAttribute('aria-busy'); }
  }
  function askConfirm(title, message, action = '确认') {
    if (confirmationOpen) return Promise.resolve(false);
    confirmationOpen = true;
    const dialog = $('confirmDialog');
    $('confirmTitle').textContent = title; $('confirmText').textContent = message;
    $('confirmProceed').textContent = action;
    return new Promise(resolve => {
      dialog.addEventListener('close', () => { confirmationOpen = false; resolve(dialog.returnValue === 'confirm'); }, {once: true});
      dialog.returnValue = 'cancel'; dialog.showModal();
    });
  }
  function navigate(next) {
    if (page === 'closed') return;
    if (!(next in pageNames)) next = 'workspace';
    page = next;
    document.querySelectorAll('.page').forEach(node => { const active = node.id === `page-${page}`; node.hidden = !active; node.classList.toggle('active', active); });
    document.querySelectorAll('[data-page]').forEach(node => { const active = node.dataset.page === page; node.classList.toggle('active', active); if (active) node.setAttribute('aria-current', 'page'); else node.removeAttribute('aria-current'); });
    $('pageTitle').textContent = pageNames[page];
    $('saveBar').hidden = !configPages.includes(page);
    if (location.hash !== `#${page}`) history.replaceState(null, '', `#${page}`);
    arrangeConfiguration();
    if (configPages.includes(page) && !settingsReady) loadConfig();
    if (page === 'media') loadReferences();
    if (page === 'voice' && settingsReady) $('cloneModel').value = configData.tts_model || 'cosyvoice-v2';
    if (page === 'memory') loadMemories();
    if (page === 'diagnostics') loadDiagnostics();
    if (page === 'logs') loadLogs();
  }
  function badge(node, text, style = 'neutral') { node.textContent = text; node.className = `badge ${style}`; }
  function metric(node, value, unit) { node.replaceChildren(document.createTextNode(String(value ?? '—')), element('small', '', unit)); }
  function numberValue(...values) { return values.find(value => typeof value === 'number' && Number.isFinite(value)); }
  function statusMetrics(snapshot) {
    const metrics = snapshot.metrics || {}, outbox = snapshot.outbox || {};
    const queues = snapshot.queues || snapshot.send_queue || metrics.queues || {};
    let queue = numberValue(snapshot.queue_count, queues.total, queues.pending, metrics.queue_count, metrics.pending);
    if (queue === undefined && typeof queues === 'object') {
      const parts = Object.values(queues).filter(value => typeof value === 'number');
      if (parts.length) queue = parts.reduce((a, b) => a + b, 0);
    }
    metric($('queueCount'), queue, '条');
    $('queueDetail').textContent = snapshot.paused ? '暂停期间不生成或发送回复' : queue > 0 ? '会话独立处理，按目标有序发送' : '当前没有等待的消息';
    const latency = snapshot.latency || metrics.latency || {};
    let milliseconds = numberValue(latency.median_ms, latency.average_ms, latency.avg_ms, metrics.average_latency_ms, metrics.avg_latency_ms, snapshot.avg_latency_ms);
    if (milliseconds === undefined && typeof latency === 'number') milliseconds = latency;
    metric($('latencyValue'), milliseconds === undefined ? undefined : (milliseconds / 1000).toFixed(2), '秒');
    $('latencyDetail').textContent = '中位调用耗时 · 含未确认，不含模型等待';
    const pending = numberValue(outbox.pending, outbox.queued, metrics.outbox_pending, snapshot.outbox_pending);
    metric($('deliveryCount'), pending, '条');
    $('deliveryDetail').textContent = pending > 0 ? '等待 AstrBot 连接恢复后投递' : '离线时保留，恢复后继续';
    $('messageCounters').textContent = `已接收 ${metrics.received ?? '—'} · 已转交 ${metrics.forwarded ?? '—'} · 已发送 ${metrics.sent ?? '—'} · 发送失败 ${metrics.send_failed ?? '—'}`;
  }
  function renderResponsePerformance(snapshot) {
    const timings = snapshot.response_performance?.stages || {}, cache = snapshot.tts_cache || {};
    const formatSeconds = value => typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value).toFixed(2) : '—';
    const rows = [
      ['首条文字 · 端到端', timings.first_text], ['语音 · 端到端', timings.first_voice],
      ['语音准备 · 含缓存', timings.voice_prepare],
      ['实际合成 · 未命中', {latest_seconds: cache.last_synthesis_seconds, average_seconds: cache.synthesis_window_average_seconds,
        p95_seconds: cache.synthesis_p95_seconds, sample_count: cache.synthesis_samples}],
      ['发送任务排队', timings.queue_wait], ['语音排队', timings.voice_queue], ['微信发送 · 本机确认', timings.wechat_send],
    ];
    const body = $('responseTimingRows'); body.replaceChildren();
    for (const [label, sample = {}] of rows) {
      const row = element('tr');
      const heading = element('th', '', label); heading.scope = 'row'; row.append(heading);
      for (const key of ['latest_seconds', 'average_seconds', 'p95_seconds']) row.append(element('td', '', formatSeconds(sample.sample_count > 0 ? sample[key] : undefined)));
      row.append(element('td', '', Number.isInteger(sample.sample_count) ? String(sample.sample_count) : '0'));
      body.append(row);
    }
    const queue = snapshot.send_queue || {}, active = queue.active, watchdog = snapshot.runtime_watchdog || {}, desktop = watchdog.desktop || {};
    const pending = numberValue(queue.pending) ?? 0;
    $('sendProgress').textContent = active ? `正在发送 · 已用 ${formatSeconds(active.elapsed_seconds)} 秒 · 另有 ${pending} 条排队`
      : desktop.active ? `桌面任务处理中 · 已用 ${formatSeconds(desktop.elapsed_seconds)} 秒 · ${pending} 条排队`
      : pending > 0 ? `当前 ${pending} 条排队，等待发送` : '当前没有排队或发送任务';
    const healthNames = {healthy: ['运行正常', ''], recovering: ['恢复中', 'warning'], restarted: ['已恢复', ''],
      blocked: ['需处理', 'warning'], stopped: ['已停止', 'neutral'], paused: ['已暂停', 'neutral']};
    const health = snapshot.preview ? ['合成预览', 'neutral'] : healthNames[watchdog.state] || (!snapshot.running ? ['已停止', 'neutral'] : ['等候状态', 'neutral']);
    badge($('runtimeHealthBadge'), ...health);
    $('runtimeHealthDetail').textContent = snapshot.preview ? '合成示例，不代表实机运行或恢复结果。'
      : `${watchdog.reason || (snapshot.running ? '运行恢复状态尚未上报。' : '桥接尚未运行。')}${watchdog.restart_count ? ` · 本次恢复 ${watchdog.restart_count} 次` : ''}${watchdog.last_recovery_at ? ` · 最近恢复 ${formatTime(watchdog.last_recovery_at, true)}` : ''}`;
    $('voiceCacheSummary').textContent = numberValue(cache.hits) === undefined ? '语音缓存：尚无统计。'
      : `语音缓存 · 命中 ${cache.hits} 次 · 未命中 ${cache.misses ?? 0} 次 · 合并等待 ${cache.coalesced_waits ?? 0} 次${cache.inflight ? ` · 正在合成 ${cache.inflight} 条` : ''}`;
  }
  function renderActivity(snapshot) {
    const activities = snapshot.recent_activity || snapshot.activity || (snapshot.metrics || {}).recent_activity || [];
    if (!Array.isArray(activities) || !activities.length) {
      empty($('activityList'), '等待第一条消息', snapshot.running ? '新的处理记录会出现在这里。' : '启动桥接后，消息的处理进度会出现在这里。');
      return;
    }
    const kindNames = {sent: '回复已发送', error: '发送未确认', rename: '联系人名称更新', connection: '连接状态', lifecycle: '工作台状态', receive: '收到消息', queued: '消息已暂存', tool: '使用工具'};
    const nodes = activities.slice(0, 12).map(item => {
      const row = element('div', 'activity-row');
      const failed = item.level === 'error' || item.status === 'failed' || item.kind === 'error';
      row.append(element('span', `activity-dot ${failed ? 'error' : item.status === 'sent' || item.status === 'success' || item.kind === 'sent' ? 'success' : ''}`));
      const body = element('div', 'activity-body');
      body.append(element('strong', '', item.title || item.event || kindNames[item.kind] || item.kind || '会话处理'), element('p', '', `${item.target ? `${item.target} · ` : ''}${item.detail || item.message || item.text || item.session || '状态已更新'}`));
      const timestamp = item.timestamp || item.time || item.at;
      row.append(body, element('time', 'activity-time', formatTime(timestamp)));
      return row;
    });
    $('activityList').replaceChildren(...nodes);
  }
  function formatTime(value, full = false) {
    if (!value) return '刚刚';
    const timestamp = typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : value;
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return String(value);
    return full ? date.toLocaleString('zh-CN', {hour12: false}) : date.toLocaleTimeString('zh-CN', {hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'});
  }
  function renderAutopilotPending(snapshot) {
    const pending = snapshot.autopilot_pending;
    const publishingAnnouncement = snapshot.autopilot_schedule?.first_online?.state === 'running';
    const exists = !publishingAnnouncement && pending && typeof pending === 'object' && !Array.isArray(pending);
    $('autopilotPendingNotice').hidden = !exists;
    $('autopilotPendingCard').hidden = !exists;
    const reviewable = exists && pending.status !== 'record_error' && typeof pending.intent_id === 'string' && pending.intent_id.length > 0 && typeof pending.content === 'string';
    $('autopilotPublished').disabled = !reviewable || pendingResolving || snapshot.preview === true;
    $('autopilotNotPublished').disabled = !reviewable || pendingResolving || snapshot.preview === true;
    $('autopilotPendingCard').setAttribute('aria-busy', String(pendingResolving));
    if (!exists) return;
    badge($('autopilotBadge'), '待核对', 'warning');
    $('autopilotPendingHelp').textContent = reviewable ? '请先打开微信朋友圈，按正文、时间与配图核对这条动态是否已发表，再在这里记录结果。' : '待核对记录无法读取，自动发表与生图保持暂停。请查看运行日志，保留原始记录后排查；这里不会自动清除记录。';
    $('autopilotPendingMeta').textContent = reviewable ? `尝试时间：${pending.time ? formatTime(pending.time, true) : '未记录'} · ${pending.image_path ? '含配图，请同时核对图片' : '纯文字'}` : '';
    $('autopilotPendingContent').hidden = !reviewable;
    $('autopilotPendingContent').textContent = reviewable ? pending.content : '';
    $('autopilotPendingError').hidden = reviewable;
    $('autopilotPendingError').textContent = reviewable ? '' : pending.error || '记录格式无效，无法确认需要核对的动态。';
  }
  async function resolveAutopilotPending(outcome) {
    if (pendingResolving || status.preview === true || status.autopilot_schedule?.first_online?.state === 'running') return;
    const pending = status.autopilot_pending;
    if (!pending || pending.status === 'record_error' || !pending.intent_id || typeof pending.content !== 'string') return;
    // Capture the displayed intent before opening the dialog. A status poll or
    // another tab may resolve it meanwhile; neither may change this request.
    const intentId = pending.intent_id;
    pendingResolving = true; renderAutopilotPending(status);
    try {
      const published = outcome === 'published';
      if (!await askConfirm(published ? '已在微信确认发表？' : '确认这条动态未发表？', `请先在微信朋友圈核对正文、尝试时间${pending.image_path ? '与配图' : ''}。\n\n${String(pending.content).slice(0, 240)}\n\n${published ? '记录为已发表，避免重复发表。' : '仅在确认微信中没有这条动态后，记录为未发表。'}核对结果只恢复正常周期的决策，不会立即重发。`, published ? '确认已发表' : '确认未发表')) return;
      if (status.autopilot_pending?.intent_id !== intentId) throw new Error('待核对记录已变化，请刷新面板并在微信重新核对。');
      const response = await api('/api/autopilot/resolve-pending', {outcome, intent_id: intentId, confirm: 'checked_in_wechat'});
      if (response.ok !== true) throw new Error(response.error || '核对结果未能保存，自动发表保持暂停。');
      status.autopilot_pending = response.autopilot_pending;
      toast(response.message || '核对结果已保存；本次不会立即重发。');
    } catch (error) { toast(error.message, 'error'); }
    finally { pendingResolving = false; renderAutopilotPending(status); await refreshStatus(); }
  }
  const socialKinds = {like: '点赞', comment: '评论', reply: '回复评论'};
  const socialResults = {already_liked: '此前已赞，本次跳过', published: '已完成', completed: '已完成', success: '已完成', not_published: '核对为未完成', pending_review: '待核对', failed: '未提交', cancelled: '已取消'};
  function socialPendingReviewable(pending) {
    return pending && pending.version === 1 && pending.status === 'pending_review'
      && typeof pending.intent_id === 'string' && /^[0-9a-f]{32}$/.test(pending.intent_id)
      && Object.hasOwn(socialKinds, pending.type) && typeof pending.content === 'string'
      && typeof pending.text === 'string' && (pending.type !== 'reply'
        || typeof pending.comment_author_id === 'string' && pending.comment_author_id.length > 0
          && typeof pending.comment_content === 'string' && pending.comment_content.trim().length > 0);
  }
  function canBrowseMoments(snapshot) {
    const social = snapshot.moments_interactions || {};
    return snapshot.preview !== true && snapshot.running === true && snapshot.paused !== true
      && social.enabled === true && social.running !== true && !social.pending
      && snapshot.uia_restore_result?.ok !== false && !socialBrowsing;
  }
  function renderMomentsInteractions(snapshot) {
    const social = snapshot.moments_interactions || {};
    const pending = social.pending, hasPending = pending && typeof pending === 'object' && !Array.isArray(pending);
    const reviewable = socialPendingReviewable(pending), counts = social.daily || social.counts || {};
    const stopped = snapshot.running !== true || snapshot.paused === true;
    const blocked = social.blocked_reason || '';
    const missingAccount = blocked === 'account_identity_missing';
    const executionError = social.last_action_error && typeof social.last_action_error === 'object' && !Array.isArray(social.last_action_error) ? social.last_action_error : {};
    const executionMessage = typeof executionError.message === 'string' ? executionError.message : '';
    const hasExecutionError = !!(executionMessage || typeof executionError.status === 'string' && executionError.status);
    badge($('momentsInteractionBadge'), hasPending ? '待核对' : !social.enabled ? '关闭' : stopped ? '已暂停' : missingAccount ? '待配置账号' : social.running ? '浏览中' : hasExecutionError ? '最近执行失败' : blocked ? '等待条件' : '定时浏览', hasPending || stopped && social.enabled || blocked || hasExecutionError ? 'warning' : !social.enabled ? 'neutral' : '');
    const start = String(configData.moments_active_start_hour ?? 8).padStart(2, '0');
    const end = String(configData.moments_active_end_hour ?? 23).padStart(2, '0');
    const contacts = configData.moments_interaction_contacts;
    const targetHint = Array.isArray(contacts) && contacts.length ? `仅 ${contacts.length} 个指定微信 ID` : '全部可见联系人';
    $('momentsInteractionPlan').textContent = `主动互动每 ${configData.moments_browse_interval_min ?? 240} 分钟 · 发布决策每 ${configData.autopilot_interval_min ?? 150} 分钟 · 评论每 ${configData.moments_reply_poll_seconds ?? 5} 秒检查 · 私聊空闲 ${configData.moments_private_quiet_seconds ?? 60} 秒后回应 · ${start === end ? '全天' : `${start}:00–${end}:00`} · ${targetHint}`;
    const schedule = hasPending ? '核对后恢复' : !social.enabled ? '开启互动后安排' : stopped ? '恢复桥接后安排' : null;
    const activeAt = social.next_active_run || social.next_run;
    const replyAt = social.next_reply_poll || social.next_reply_run;
    const priority = social.private_priority || {};
    const privateBusy = priority.private_inflight === true;
    const privateElapsed = numberValue(priority.seconds_since_private_activity);
    const quietRemaining = privateElapsed === undefined ? 0 : Math.max(0, Math.ceil((configData.moments_private_quiet_seconds ?? 60) - privateElapsed));
    const priorityHint = privateBusy ? ' · 私聊正在处理，朋友圈稍候' : quietRemaining > 0 ? ` · 私聊空闲还需 ${quietRemaining} 秒` : '';
    $('momentsInteractionNext').textContent = schedule || `主动浏览：${activeAt ? formatTime(activeAt, true) : '等待安排'} · 评论检查：${replyAt ? formatTime(replyAt, true) : '等待安排'}${social.running ? ' · 本次正在处理' : ''}${priorityHint}`;
    for (const [kind, id, key, limit] of [['like', 'momentsLikeCount', 'moments_like_daily_limit', 12], ['comment', 'momentsCommentCount', 'moments_comment_daily_limit', 4], ['reply', 'momentsReplyCount', 'moments_reply_daily_limit', 8]]) {
      const count = numberValue(counts[kind]) ?? 0;
      $(id).textContent = `${count} / ${configData[key] ?? limit}`;
    }
    $('momentsInteractionResult').textContent = `最近检查：${social.last_result || (social.last_run ? `上次浏览：${formatTime(social.last_run, true)}` : '尚未浏览朋友圈；模型可以选择只看、不互动。')}`;
    const checkNotes = {account_identity_missing: '请填写机器人当前账号的真实微信编号；昵称和备注不能替代。', model_configuration_missing: '请检查主模型与朋友圈决策模型配置。', private_priority: '私聊优先，空闲满设定时间后继续。', outside_hours: '当前不在互动时段。', sender_unavailable: '朋友圈界面发送器不可用。', feed_failed: '动态读取失败，请检查 WeFlow。', refresh_failed: '朋友圈界面刷新失败。', model_failed: '本条模型判断未完成。', cancelled_by_settings: '设置变化取消了本次任务。'};
    const checkText = Object.hasOwn(checkNotes, blocked) ? checkNotes[blocked] : blocked ? '本次检查未执行互动，请查看最近结果。' : '';
    const executionMode = executionError.mode === 'reply_poll' ? '评论检查' : '主动浏览';
    const executionTime = numberValue(executionError.time);
    const executionNote = hasExecutionError ? `最近执行错误（${executionMode}）：${executionMessage || '本次执行未完成'}${executionTime ? ` · ${formatTime(executionTime, true)}` : ''}` : '';
    $('momentsCheckStatus').hidden = !social.last_check && !checkText && !executionNote;
    $('momentsCheckStatus').textContent = `${social.last_check ? `最近检查：${formatTime(social.last_check, true)}` : ''}${checkText ? ` · ${checkText}` : ''}${executionNote ? `${social.last_check || checkText ? ' · ' : ''}${executionNote}` : ''}`;
    const manual = snapshot.moments_manual_request || {};
    const manualNames = {queued: '已排队，等待执行', running: '正在读取和判断', started: '正在读取和判断', accepted_by_ui: '已完成界面互动', skipped: '已浏览，模型选择不互动', cancelled: '已取消', disabled: '任务已取消或功能已关闭', account_identity_missing: '缺少当前账号编号', unavailable: '运行条件未满足', private_priority: '等待私聊结束', outside_hours: '当前不在互动时段', failed: '执行失败'};
    const useRequestError = socialRequestError && (!manual.started_at || socialRequestError.at >= manual.started_at * 1000);
    const manualText = useRequestError ? `最近手动请求：${socialRequestError.message}` : manual.status && manual.status !== 'idle' ? `最近手动请求：${Object.hasOwn(manualNames, manual.status) ? manualNames[manual.status] : '请查看执行结果'}${manual.error ? ` · ${manual.error}` : ''}${manual.finished_at ? ` · ${formatTime(manual.finished_at, true)}` : ''}` : '';
    $('momentsManualStatus').hidden = !manualText;
    $('momentsManualStatus').textContent = manualText;
    const mediaError = social.last_media_error || {};
    $('momentsMediaStatus').hidden = !mediaError.message;
    $('momentsMediaStatus').textContent = mediaError.message ? `最近媒体读取问题：${mediaError.message}${mediaError.time ? ` · ${formatTime(mediaError.time, true)}` : ''}。无法读取的动态会跳过，不凭图片猜内容。` : '';
    $('momentsAccountSetting').hidden = !(missingAccount || manual.blocked_reason === 'account_identity_missing');
    const feed = social.feed || {}, identity = feed.reply_identity;
    const missing = identity === 'missing' || identity === 'partial';
    const unobserved = identity === 'not_observed' || identity === 'unknown';
    const unknownCount = numberValue(feed.unverified_comment_count);
    const skippedCount = Math.max(0, Math.floor(numberValue(feed.skipped_post_count) ?? 0));
    const identityHint = missing ? `${unknownCount > 0 ? `${unknownCount} 条` : '部分'}评论缺少可核验的微信 ID，会跳过这些回复；浏览、点赞与评论仍按规则进行。` : unobserved ? '尚未读取到可核验的评论身份；获得真实微信 ID 后，再由模型决定是否回复。' : '';
    const feedHints = [identityHint, skippedCount > 0 ? `${skippedCount} 条动态身份或正文不一致，已略过。` : ''].filter(Boolean);
    $('momentsReplyIdentityHint').hidden = feedHints.length === 0;
    $('momentsReplyIdentityHint').className = `moments-identity-hint small ${missing || skippedCount > 0 ? 'identity-warning' : 'subtle'}`;
    $('momentsReplyIdentityHint').textContent = feedHints.join(' ');
    $('momentsBrowseNow').disabled = !canBrowseMoments(snapshot);
    $('momentsBrowseNow').setAttribute('aria-busy', String(socialBrowsing));
    $('momentsInteractionNotice').hidden = !hasPending;
    $('momentsInteractionPending').hidden = !hasPending;
    $('momentsInteractionCompleted').disabled = !reviewable || socialResolving || social.running === true || snapshot.preview === true;
    $('momentsInteractionNotCompleted').disabled = !reviewable || socialResolving || social.running === true || snapshot.preview === true;
    $('momentsInteractionPending').setAttribute('aria-busy', String(socialResolving));
    if (hasPending) {
      $('momentsInteractionPendingHelp').textContent = reviewable ? '请在微信找到对应动态，核对点赞状态或评论文字。确认结果只恢复正常周期，不会立即重复互动。' : '待核对记录无法读取，后续互动保持暂停。请查看日志并保留原始记录；这里不会自动清除记录。';
      $('momentsInteractionPendingMeta').textContent = reviewable ? `${socialKinds[pending.type]} · ${pending.author_name || '作者身份见微信'} · ${pending.time ? formatTime(pending.time, true) : '时间未记录'}` : '';
      $('momentsInteractionPendingContent').hidden = !reviewable;
      const originalComment = pending.type === 'reply' ? `原评论（${pending.comment_author_name || '评论者'} · ${pending.comment_author_id}）：${pending.comment_content}\n\n` : '';
      $('momentsInteractionPendingContent').textContent = reviewable ? `动态：${pending.content}\n\n${originalComment}${pending.type === 'like' ? '操作：点赞' : `${socialKinds[pending.type]}：${pending.text}`}` : '';
      $('momentsInteractionPendingError').hidden = reviewable && !pending.error;
      $('momentsInteractionPendingError').textContent = pending.error || (reviewable ? '' : '记录格式无效，无法确认互动对象。');
    }
    const recent = social.recent || social.history || [];
    if (Array.isArray(recent) && recent.length) {
      $('momentsInteractionRecent').replaceChildren(...recent.slice(0, 6).map(item => {
        const row = element('article', 'moments-record');
        const actorName = item.type === 'reply' ? item.comment_author_name || item.comment_author_id : item.author_name;
        row.append(element('strong', '', `${socialKinds[item.type] || '互动'}${actorName ? ` · ${actorName}` : ''}`));
        if (item.type === 'reply' && item.comment_content) row.append(element('p', '', `原评论：${item.comment_content}`));
        row.append(element('p', '', item.text || item.content || item.message || '操作已记录'));
        if (item.error) row.append(element('p', 'small identity-warning', `原因：${item.error}`));
        row.append(element('time', 'small subtle', `${formatTime(item.time || item.timestamp, true)}${item.status ? ` · ${Object.hasOwn(socialResults, item.status) ? socialResults[item.status] : '状态待检查'}` : ''}`));
        return row;
      }));
    } else $('momentsInteractionRecent').replaceChildren(element('p', 'small subtle', '尚无互动记录。'));
  }
  async function browseMomentsNow() {
    if (!canBrowseMoments(status)) return;
    socialRequestError = null; socialBrowsing = true; renderMomentsInteractions(status);
    try {
      const response = await api('/api/moments/interactions/run', {});
      if (response.ok !== true) throw new Error(response.error || '浏览任务未能排队');
      toast(response.message || '已安排浏览朋友圈，互动由模型按规则决定');
    } catch (error) {
      socialRequestError = {status: error.status || 'failed', message: error.message, at: Date.now()};
      toast(error.message, 'error');
    }
    finally { socialBrowsing = false; await refreshStatus(); renderMomentsInteractions(status); }
  }
  async function resolveMomentsInteraction(outcome) {
    if (socialResolving || status.preview === true || status.moments_interactions?.running === true) return;
    const pending = status.moments_interactions?.pending;
    if (!socialPendingReviewable(pending)) return;
    const intentId = pending.intent_id;
    socialResolving = true; renderMomentsInteractions(status);
    try {
      const complete = outcome === 'published';
      const originalComment = pending.type === 'reply' ? `\n原评论：${pending.comment_author_name || '评论者'} · ${pending.comment_author_id}\n${pending.comment_content.slice(0, 120)}` : '';
      if (!await askConfirm(complete ? '已在微信确认互动完成？' : '确认这次互动未完成？', `请先核对微信中的动态作者、正文${pending.type === 'like' ? '和点赞状态' : '及评论文字'}。\n\n${String(pending.content).slice(0, 120)}${originalComment}\n${String(pending.text).slice(0, 120)}\n\n核对后不会立即重复这次互动。`, complete ? '确认已完成' : '确认未完成')) return;
      if (status.moments_interactions?.pending?.intent_id !== intentId) throw new Error('待核对记录已变化，请刷新面板并在微信重新核对。');
      const response = await api('/api/moments/interactions/resolve', {outcome, intent_id: intentId, confirm: 'checked_in_wechat'});
      if (response.ok !== true) throw new Error(response.error || '核对结果未能保存');
      status.moments_interactions = response.moments_interactions;
      toast(response.message || '核对结果已保存，不会立即重复互动');
    } catch (error) { toast(error.message, 'error'); }
    finally { socialResolving = false; await refreshStatus(); renderMomentsInteractions(status); }
  }
  function renderRunControls(snapshot) {
    const preview = snapshot.preview === true;
    const running = !!snapshot.running, paused = !!snapshot.paused;
    const restorePending = !preview && snapshot.uia_restore_result?.ok === false;
    const service = snapshot.service_control || {};
    const serviceBusy = ['starting', 'stopping'].includes(service.state);
    const busyAction = controlAction || (serviceBusy ? service.action : '');
    const controlsBusy = !!controlAction || serviceBusy;
    const serviceFailed = service.state === 'failed';
    const failedStop = serviceFailed && service.action === 'stop';
    $('restoreNotice').hidden = !restorePending;
    $('retryRestore').disabled = controlsBusy || !restorePending;
    $('restoreNoticeText').textContent = running ? '微信临时状态还原未确认。请先停止机器人并重试还原。' : '机器人已停止，微信临时状态尚未确认还原。请重试还原后再启动。';
    $('retryRestore').textContent = running ? '停止并还原' : '重试还原';
    const stateLabel = controlsBusy ? busyAction === 'stop' ? '正在停止服务' : busyAction === 'start' ? '正在启动服务' : '正在处理'
      : serviceFailed ? failedStop ? '停止未完成' : '启动未完成'
      : restorePending ? running ? '临时状态待还原' : '已停止 · 等待还原'
      : !running ? service.state === 'stopped' ? '机器人已停止' : '机器人未启动' : paused ? '任务已暂停' : '运行中';
    badge($('bridgeState'), preview ? '预览模式' : stateLabel,
      preview || restorePending || controlsBusy ? 'warning' : serviceFailed ? 'error' : !running ? 'neutral' : paused ? 'warning' : '');
    $('btnStart').disabled = controlsBusy || running || restorePending || failedStop;
    $('btnStop').disabled = controlsBusy || (!running && !restorePending && !serviceFailed);
    $('btnExit').disabled = controlsBusy || preview;
    $('btnExit').textContent = busyAction === 'exit' ? '正在退出…' : '完全退出';
    $('btnPause').disabled = controlsBusy || !running;
    $('btnStart').textContent = busyAction === 'start' ? '启动中…' : '启动机器人';
    $('btnStop').textContent = busyAction === 'stop' ? '停止中…' : failedStop ? '重试停止' : restorePending ? running ? '停止并还原' : '重试还原' : '停止机器人';
    $('btnPause').dataset.action = paused ? 'resume' : 'pause';
    $('btnPause').textContent = paused ? '恢复任务' : '暂停任务';
    const note = $('serviceControlNote');
    note.textContent = controlsBusy
      ? busyAction === 'exit' ? '正在关闭本包的机器人、AstrBot 和面板。' : busyAction === 'stop' ? '正在停止机器人和 AstrBot，面板会保持打开。' : busyAction === 'start' ? '正在启动机器人和 AstrBot，请稍候。' : '正在更新机器人运行状态，请稍候。'
      : service.error || service.message || '停止机器人后可在面板再次启动。更换或删除本包前，请选“完全退出”。';
    note.classList.toggle('error', serviceFailed && !controlsBusy);
  }
  async function refreshStatus() {
    if (statusBusy || document.hidden || page === 'closed' || controlAction === 'exit') return;
    statusBusy = true;
    const requestedRevision = controlRevision;
    try {
      const snapshot = await api('/status');
      if (requestedRevision !== controlRevision) return;
      status = snapshot;
      $('connectionError').hidden = true; badge($('connectionBadge'), '面板已连接', '');
      const preview = status.preview === true;
      $('previewNotice').hidden = !preview;
      const running = !!status.running, paused = !!status.paused;
      renderRunControls(status);
      const share = status.bili_sharing || {};
      const tastes = status.character_interests || {};
      $('roleInterests').textContent = [['基础爱好', tastes.seeds], ['最近喜欢', tastes.likes], ['最近兴致较低', tastes.dislikes]]
        .filter(([, labels]) => labels?.length).map(([name, labels]) => name + ' · ' + labels.join('、')).join(' ｜ ') || '暂无兴趣记录。可设置基础爱好，也可以随观看逐渐形成。';
      $('biliShareState').textContent = share.enabled
        ? [!running ? '桥接已停止' : paused ? '桥接已暂停' : '主动分享已开启', share.last_result, running && !paused && share.next_run ? '下次浏览 ' + formatTime(share.next_run, true) : ''].filter(Boolean).join(' · ')
        : '主动分享已关闭';
      const weflowConnected = !!(status.weflow_connected ?? status.connected);
      $('flowWeChat').classList.toggle('connected', !preview && weflowConnected);
      $('flowBrain').classList.toggle('connected', !preview && !!status.ob_connected);
      const uia = status.uia || {};
      const operationObservation = uia.snapshot_source === 'operation_observation';
      $('flowSend').classList.toggle('connected', !preview && running && !paused && !uia.busy && !operationObservation && !!uia.ready);
      $('flowSend').title = uia.snapshot_timestamp ? `最近${operationObservation ? '操作观察' : '控件检查'}：${formatTime(uia.snapshot_timestamp, true)}` : '发送时会重新核验微信窗口和真实目标。';
      $('weflowState').textContent = preview ? '未连接微信 · 界面预览' : weflowConnected ? '已连接 · 等候新消息' : '未连接';
      $('obState').textContent = preview ? '合成数据' : status.service_control?.state === 'stopped' ? 'AstrBot 已停止 · 可再次启动' : status.ob_connected ? '已连接 AstrBot' : '未连接 · 消息暂存';
      $('senderState').textContent = preview ? '未执行发送 · 界面预览' : !running ? '等待启动' : paused ? '暂停发送' : uia.busy ? 'UIA · 发送处理中' : operationObservation ? 'UIA · 最近操作观察' : uia.ready ? 'UIA · 最近检查可用' : uia.unavailable_reason || uia.last_error || 'UIA · 待检查';
      $('modeDescription').textContent = modeNames[status.group_reply_mode] || '等待群聊模式';
      $('btnMode').disabled = false;
      $('memoryCount').textContent = `${status.memory_count ?? '—'} 条`;
      $('navMemoryCount').textContent = status.memory_count ?? '—';
      $('companionState').textContent = !running ? '安静等候下一次对话。' : paused ? '稍作停留，等你准备好。' : '听见消息，也记得分寸。';
      $('rhythmLabel').textContent = configData.human_like_enabled === false ? '完整回复' : '自然短句';
      const publishSchedule = status.autopilot_schedule || {};
      const announcement = publishSchedule.first_online || {};
      $('firstOnlinePublish').disabled = announcementRequested || announcement.state !== 'ready' || !running || paused || preview || !!status.autopilot_pending;
      $('firstOnlinePublish').setAttribute('aria-busy', String(announcementRequested || announcement.state === 'running'));
      if (announcement.state === 'running' || announcement.state === 'published') {
        announcementError = '';
        $('firstOnlineResult').textContent = announcement.message;
      } else {
        $('firstOnlineResult').textContent = preview ? '当前是界面预览。'
          : !running || paused ? '启动并继续机器人后可以发表。'
          : status.autopilot_pending ? '请先在会话工作台核对上次发表的结果。'
          : announcementRequested ? '正在提交发布请求…'
          : announcementError || announcement.message || (announcement.state === 'ready' ? '点击后立即开始创作，无需等待定时发布。' : '等待朋友圈发布服务就绪。');
      }
      const publishBlocked = publishSchedule.blocked_reason && !['waiting_interval', 'due'].includes(publishSchedule.blocked_reason);
      const publishPaused = status.autopilot_enabled && (!running || paused || publishBlocked);
      badge($('autopilotBadge'), status.autopilot_enabled ? publishPaused ? '等待条件' : '已启用' : '关闭', publishPaused ? 'warning' : status.autopilot_enabled ? '' : 'neutral');
      $('autopilotResult').textContent = status.autopilot_last || status.autopilot_last_result || '启用后，会结合近期记忆决定是否分享生活片段。';
      const publishReasons = {waiting_interval: '按设定周期等待', due: '到期，等待本次决策', private_priority: '等待私聊结束及空闲期', disabled: '自动发表已关闭', bridge_stopped: '请先启动桥接', bridge_paused: '请先继续桥接', pending_review: '请先核对上次发表', sender_unavailable: '朋友圈发送器未就绪'};
      const nextPublish = publishSchedule.next_run || status.autopilot_next_run;
      $('autopilotSchedule').textContent = !status.autopilot_enabled ? '开启自动发表后安排下一次决策。' : !running || paused ? '桥接暂停或停止，自动发表暂不执行。' : `${nextPublish ? `下次发布决策：${formatTime(nextPublish, true)}` : '等待安排发布决策'}${publishSchedule.last_check ? ` · 最近检查：${formatTime(publishSchedule.last_check, true)}` : ''}${publishSchedule.blocked_reason ? ` · ${Object.hasOwn(publishReasons, publishSchedule.blocked_reason) ? publishReasons[publishSchedule.blocked_reason] : '请查看最近结果'}` : ''}`;
      renderAutopilotPending(status);
      renderMomentsInteractions(status);
      const profileStatus = status.model_profile_status || {};
      $('modelRuntimeStatus').textContent = profileStatus.state === 'restart_required' ? profileStatus.reason || '主模型配置版本较多，请运行“重启AstrBot.bat”后继续。' : profileStatus.reason || '主模型配置用于后续请求，正在处理的请求保留自己的设置。';
      $('modelRuntimeStatus').classList.toggle('error', profileStatus.state === 'restart_required');
      const sync = status.name_sync || {};
      const syncWarning = sync.status === 'partial' || sync.status === 'offline';
      badge($('nameSyncBadge'), sync.running ? '同步中' : sync.last_error ? '同步失败' : sync.status === 'partial' ? '部分同步' : sync.status === 'offline' ? '连接离线' : sync.last_sync || sync.last_success ? '已同步' : '等待同步', sync.last_error ? 'error' : sync.running || syncWarning ? 'warning' : 'neutral');
      $('nameSyncStatus').textContent = sync.last_error || sync.message || (sync.last_sync || sync.last_success ? `上次同步：${formatTime(sync.last_sync || sync.last_success, true)}${sync.contacts !== undefined ? ` · ${sync.contacts} 个联系人、${sync.sessions || 0} 个会话` : sync.count !== undefined ? ` · ${sync.count} 个名称` : ''}${sync.changed !== undefined ? ` · 更新 ${sync.changed} 项` : ''}` : '启动桥接后会读取联系人名称；身份标识保持稳定。');
      const syncDetails = $('nameSyncDetails');
      syncDetails.replaceChildren();
      syncDetails.hidden = !sync.name_source && sync.fresh_capability === undefined && !sync.upstream_limit;
      if (!syncDetails.hidden) {
        const sources = {fresh_snapshot: '微信数据库快照', legacy_cached: 'WeFlow 缓存接口'};
        diagnosticRow(syncDetails, '名称来源', sources[sync.name_source] || '等待确认');
        diagnosticRow(syncDetails, '无缓存快照能力', sync.fresh_capability === true ? '已检测到' : sync.fresh_capability === false ? '当前上游未提供' : '尚未检测');
        if (sync.upstream_limit) diagnosticRow(syncDetails, '更新时限', sync.upstream_limit);
      }
      statusMetrics(status); renderResponsePerformance(status); renderActivity(status);
      if (preview) $('latencyDetail').textContent = '合成示例 · 不代表实机延迟';
      $('lastUpdate').textContent = `状态更新于 ${formatTime(Date.now())}`;
      if (page === 'logs') loadLogs();
    } catch (error) { if (requestedRevision === controlRevision) showConnectionError(error.message); }
    finally { statusBusy = false; }
  }
  async function performAction(button) {
    if (controlAction || ['starting', 'stopping'].includes(status.service_control?.state) || button.disabled) return;
    const action = button.dataset.action;
    if (action === 'exit' && dirty && !await askConfirm('还有未保存的设置', '完全退出会丢弃本页未保存的修改。', '丢弃修改并退出')) return;
    controlAction = action;
    controlRevision++;
    button.setAttribute('aria-busy', 'true');
    renderRunControls(status);
    try {
      const response = await api(action === 'exit' ? '/api/service/exit' : `/${action}`, {}, ['start', 'stop', 'exit'].includes(action) ? 60000 : 15000);
      status = {...status, ...response};
      if (response.ok === false) throw new Error(response.error || '操作未完成');
      if (action === 'exit') {
        if (response.ok !== true) throw new Error('退出结果未确认，请重试。');
        clearInterval(statusTimer);
        page = 'closed';
        dirty = false;
        $('main').hidden = $('sidebar').hidden = true;
        $('exitNotice').hidden = false;
        $('exitNotice').focus();
        return;
      }
      if (action === 'stop' && response.uia_restore_result?.ok === false) throw new Error('机器人已停止，但微信临时状态尚未确认还原。请点击“重试还原”。');
      const serviceMessage = ['start', 'stop'].includes(action) ? response.service_control?.message : '';
      toast(serviceMessage || response.message || {start: '机器人已启动', stop: '机器人已停止，面板保持打开', pause: '回复已暂停，当前未发送任务已取消', resume: '回复已恢复，将接收新的消息'}[action]);
    } catch (error) {
      const timedOut = ['start', 'stop', 'exit'].includes(action) && error.message.startsWith('操作等待超时');
      toast(timedOut ? '服务操作尚未确认完成，请查看运行状态后再试。' : error.message, 'error');
    }
    finally {
      controlRevision++;
      controlAction = '';
      button.removeAttribute('aria-busy');
      if (page !== 'closed') {
        renderRunControls(status);
        await refreshStatus();
      }
    }
  }
  function setDirty(value) {
    dirty = value;
    $('dirtyDot').hidden = !value;
    $('saveConfig').disabled = !value || !settingsReady;
    $('saveState').textContent = value ? '有未保存的修改' : '设置已同步';
  }
  function inputValue(input, field) {
    if (field.type === 'bool') return input.checked;
    if (field.type === 'number') return input.value === '' ? '' : Number(input.value);
    if (Array.isArray(field.default)) return input.value.split(/[,，]/).map(item => item.trim().replace(/^['"]|['"]$/g, '')).filter(Boolean);
    if (field.type === 'textarea') return input.value;
    return input.value.trim();
  }
  function configChanges() {
    const updates = {}, clear = [];
    schema.forEach(group => group.fields.forEach(field => {
      const input = $(`cfg_${field.key}`);
      if (!input) return;
      const clearBox = $(`clear_${field.key}`);
      if (field.sensitive) {
        if (clearBox && clearBox.checked) clear.push(field.key);
        else if (input.value.trim()) updates[field.key] = input.value.trim();
      } else {
        const value = inputValue(input, field);
        const original = configData[field.key] === undefined ? field.default : configData[field.key];
        const displayed = field.type === 'textarea' ? String(original ?? '').replace(/\r\n?/g, '\n') : original;
        if (!valueSame(value, displayed)) updates[field.key] = value;
      }
    }));
    if (clear.length) updates._clear_secrets = clear;
    return updates;
  }
  function renderConfig() {
    const sections = [], nav = [], configured = new Set(configData._configured_secrets || []);
    const redacted = new Set(configData._redacted_keys || []);
    // Retain legacy values on disk without presenting an unused model route.
    const activeSchema = schema.filter(group => group.title !== '生图提示词子模型').map(group => ({
      ...group, fields: group.fields.filter(field => field.key !== 'image_prompt_enabled'),
    }));
    activeSchema.forEach((group, index) => {
      const section = element('section', 'settings-group'); section.id = `settings-group-${index}`;
      section.dataset.group = group.title;
      section.dataset.search = group.title.toLowerCase();
      const title = element('h2', '', group.title); title.append(element('span', '', `${group.fields.length} 项`));
      const fields = element('div', 'settings-fields');
      group.fields.forEach(field => {
        field.sensitive = field.sensitive || redacted.has(field.key);
        const box = element('div', `setting-field ${field.type === 'textarea' ? 'full' : ''}`);
        box.dataset.search = `${group.title} ${field.label} ${field.description || ''} ${field.key}`.toLowerCase();
        const label = element('label', 'field-label', field.label); label.htmlFor = `cfg_${field.key}`;
        if (field.sensitive) label.append(element('span', 'credential-tag', '凭证'));
        box.append(label);
        let input;
        const value = configData[field.key] === undefined ? field.default : configData[field.key];
        if (field.type === 'select') {
          input = element('select');
          (field.options || []).forEach(option => { const item = element('option', '', option[1]); item.value = option[0]; input.append(item); });
          input.value = String(value ?? '');
        } else if (field.type === 'textarea') { input = element('textarea'); input.value = value ?? ''; input.rows = 4; }
        else {
          input = element('input'); input.type = field.sensitive ? 'password' : field.type === 'bool' ? 'checkbox' : field.type === 'number' ? 'number' : 'text';
          if (field.type === 'bool') input.checked = !!value;
          else input.value = field.sensitive ? '' : Array.isArray(value) ? value.join(', ') : value ?? '';
        }
        input.id = `cfg_${field.key}`; input.name = field.key;
        input.placeholder = field.sensitive && configured.has(field.key) ? '已配置，留空保持原值' : field.placeholder || '';
        if (field.sensitive) input.autocomplete = 'new-password';
        if (field.type === 'number') {
          input.step = field.step ?? 'any'; if (field.min !== undefined) input.min = field.min; if (field.max !== undefined) input.max = field.max;
        }
        if (field.type === 'bool') { const wrap = element('label', 'checkbox-label'); wrap.append(input, document.createTextNode('启用')); box.append(wrap); }
        else box.append(input);
        if (field.sensitive && configured.has(field.key)) {
          const clear = element('label', 'checkbox-label secret-clear');
          const check = element('input'); check.type = 'checkbox'; check.id = `clear_${field.key}`;
          check.addEventListener('change', () => { input.disabled = check.checked; if (check.checked) input.value = ''; });
          clear.append(check, document.createTextNode('清除已保存的凭证')); box.append(clear);
        }
        if (field.description) box.append(element('p', 'field-description', field.description));
        if (field.restart_required) box.append(element('p', 'field-description', '更改后需要重启整个项目进程。'));
        fields.append(box);
      });
      section.append(title, fields); sections.push(section);
      const button = element('button', '', group.title); button.type = 'button';
      button.append(element('span', 'setting-count', group.fields.length));
      button.addEventListener('click', () => { $('configSearch').value = ''; filterConfig(); section.scrollIntoView({behavior: 'smooth', block: 'start'}); });
      nav.push(button);
    });
    configurationSections.forEach(section => section.remove());
    configurationSections = sections;
    $('settingsForm').replaceChildren(...sections); $('settingsNav').replaceChildren(...nav);
    $('configSummary').textContent = `${activeSchema.length} 个分组 · ${activeSchema.reduce((n, group) => n + group.fields.length, 0)} 项设置`;
    settingsReady = true; setDirty(false); filterConfig(); arrangeConfiguration(); renderModelRoles();
  }
  function arrangeConfiguration() {
    if (!configurationSections.length) return;
    configurationSections.forEach(section => {
      const group = section.dataset.group;
      let target = 'settingsForm';
      if (page === 'models' && modelGroups.includes(group)) target = 'modelConfiguration';
      if (page === 'moments' && group === '朋友圈互动') target = 'momentsConfiguration';
      if (page === 'media' && ['内置工具', '媒体与生图'].includes(group)) target = 'mediaConfiguration';
      if (page === 'voice' && ['语音发送', '声音调节'].includes(group)) target = 'voiceConfiguration';
      $(target).append(section);
      if (target !== 'settingsForm') { section.hidden = false; section.querySelectorAll('.setting-field').forEach(field => { field.hidden = false; }); }
    });
    if (page === 'settings') filterConfig();
  }
  function renderModelRoles() {
    const roles = [
      ['主模型', 'main_llm', '理解媒体，创作聊天、语音台词、朋友圈正文与画面提示词'], ['共享记忆', 'memory_llm', '抽取有证据的稳定事实'],
      ['群消息回复判断', 'group_reply_judge', '只判断群聊是否参与及回复形式'], ['AI 自主朋友圈', 'autopilot_llm', '只决定发不发及是否配图'],
      ['拟真与朋友圈决策模型', 'moments_decision_llm', '只判断是否回应、互动动作及分寸；正文交给主模型'],
    ];
    $('modelRoles').replaceChildren(...roles.map(([title, prefix, hint]) => {
      const card = element('button', 'role-card'); card.type = 'button';
      card.append(element('span', 'eyebrow', title), element('strong', '', configData[`${prefix}_model`] || '尚未配置模型'), element('small', '', hint));
      const mode = configData[`${prefix}_thinking`] || 'auto';
      const effort = configData[`${prefix}_reasoning_effort`] || 'auto';
      const context = configData[`${prefix}_context_tokens`];
      const thinking = mode === 'off' ? '关闭思考' : effort !== 'auto' ? `思考 ${effort}` : mode === 'on' ? '开启思考' : '模型默认思考';
      card.append(element('span', 'role-budget', `${thinking} · ${typeof context === 'number' ? context.toLocaleString('zh-CN') : '默认'} token`));
      if (prefix === 'main_llm' && configData.main_llm_model === 'deepseek-flash' && /^https:\/\/api\.deepseek\.com(?:\/|$)/i.test(configData.main_llm_api_base || '')) {
        const p = configData.main_llm_top_p ?? -1;
        const sampling = mode === 'off'
          ? `温度 ${configData.main_llm_temperature ?? 0.6} · Top P 固定为 1.00`
          : `Top P ${p < 0 ? '模型默认（1.00）' : Math.max(0.95, p).toFixed(2)} · 思考时温度不生效`;
        card.append(element('small', '', sampling));
      }
      card.addEventListener('click', () => configurationSections.find(section => section.dataset.group === title)?.scrollIntoView({behavior: 'smooth', block: 'start'}));
      return card;
    }));
    const limit = configData.main_llm_context_tokens ?? 32768;
    const output = configData.main_llm_max_tokens ?? 4096;
    const trigger = configData.main_context_compression_threshold ?? 0.82;
    $('mainWorkflowSummary').textContent = `${configData.main_streaming_enabled === false ? '完整生成后发送' : '完整短句边生成边发送'} · ${configData.main_context_compression_enabled === false ? '关闭摘要，超长时裁剪旧轮次' : `接近估算输入预算的 ${Math.round(trigger * 100)}% 时摘要旧对话`} · 上限 ${Number(limit).toLocaleString('zh-CN')} token，预留输出 ${Number(output).toLocaleString('zh-CN')} token`;
  }
  function filterConfig() {
    const query = $('configSearch').value.trim().toLowerCase();
    let matches = 0;
    $('settingsForm').querySelectorAll('.settings-group').forEach(group => {
      let count = 0;
      group.querySelectorAll('.setting-field').forEach(field => { field.hidden = !field.dataset.search.includes(query); if (!field.hidden) count++; });
      group.hidden = count === 0; matches += count;
    });
    let noMatches = $('configNoMatches');
    if (!noMatches) { noMatches = element('div', 'empty settings-empty'); noMatches.id = 'configNoMatches'; noMatches.append(element('strong', '', '没有匹配的设置'), element('p', '', '试试其他关键词，或清空搜索查看全部配置。')); $('settingsForm').append(noMatches); }
    noMatches.hidden = matches !== 0;
  }
  async function loadConfig() {
    if (configLoadPromise) return configLoadPromise;
    configLoadPromise = (async () => {
      try {
        const [data, fields] = await Promise.all([api('/api/config'), api('/api/config/schema')]);
        if (!Array.isArray(fields)) throw new Error('配置模型无法读取，请刷新页面');
        configData = clone(data); schema = fields; renderConfig(); renderFeatures();
        const nicknames = configData.bot_nicknames;
        const firstNickname = Array.isArray(nicknames) ? nicknames.find(name => typeof name === 'string' && name.trim()) : typeof nicknames === 'string' ? nicknames.split(/[,，]/)[0] : '';
        $('companionName').textContent = firstNickname ? firstNickname.trim() : '微信bot';
        renderMomentsInteractions(status);
      } catch (error) {
        settingsReady = false;
        empty($('settingsForm'), '配置没有加载成功', error.message, loadConfig);
        empty($('featureToggles'), '对话习惯暂不可用', '重新读取配置后即可调整。', loadConfig);
        $('saveConfig').disabled = true;
      }
    })();
    try { await configLoadPromise; } finally { configLoadPromise = null; }
  }
  async function saveConfig() {
    if (!settingsReady || !dirty) return;
    const forms = ['settingsForm', 'modelConfiguration', 'momentsConfiguration', 'mediaConfiguration', 'voiceConfiguration'];
    for (const id of forms) {
      const invalid = $(id).querySelector(':invalid');
      if (invalid) { navigate(id === 'settingsForm' ? 'settings' : {modelConfiguration:'models', momentsConfiguration:'moments', mediaConfiguration:'media', voiceConfiguration:'voice'}[id]); invalid.reportValidity(); return; }
    }
    const updates = configChanges();
    await busy($('saveConfig'), async () => {
      const result = await api('/api/config', updates);
      if (result.ok === false) throw new Error(result.error || '设置保存失败');
      setDirty(false); await loadConfig(); await refreshStatus();
      const requiresRestart = result.restart_required === true || (Array.isArray(result.restart_required) && result.restart_required.length > 0);
      toast(requiresRestart ? '设置已保存，部分设置需要重启整个项目进程' : '设置已保存');
      if (Array.isArray(result.warnings) && result.warnings.length) toast(result.warnings.join('；'), 'error');
    });
    $('saveConfig').disabled = !dirty || !settingsReady;
  }
  function renderFeatures() {
    $('featureToggles').replaceChildren(...features.map(([key, title, hint]) => {
      const row = element('div', 'feature-row'), label = element('div', 'feature-label', title);
      label.append(element('small', '', hint));
      const button = element('button', 'switch'); button.type = 'button'; button.setAttribute('role', 'switch'); button.setAttribute('aria-label', title); button.setAttribute('aria-checked', String(configData[key] !== false));
      button.addEventListener('click', () => busy(button, async () => {
        if (dirty) { toast('设置页还有未保存的修改，请先保存或重新载入。', 'error'); return; }
        const next = !(configData[key] !== false); await api('/api/config', {[key]: next}); configData[key] = next;
        button.setAttribute('aria-checked', String(next)); toast(`${title}${next ? '已启用' : '已关闭'}`);
        settingsReady = false; await refreshStatus();
      }));
      row.append(label, button); return row;
    }));
  }
  async function jumpSetting(key) {
    navigate('settings');
    if (!settingsReady) await loadConfig();
    $('configSearch').value = ''; filterConfig();
    const input = $(`cfg_${key}`);
    if (input) { input.closest('.settings-group').scrollIntoView({behavior: 'smooth', block: 'start'}); setTimeout(() => input.focus({preventScroll: true}), 250); }
  }
  async function loadMemories() {
    const request = ++memoryRequest;
    try {
      const query = $('memorySearch').value.trim();
      const result = await api(`/api/memories?limit=${memoryLimit}&offset=${memoryOffset}&q=${encodeURIComponent(query)}`);
      if (request !== memoryRequest) return;
      if (result.ok === false) throw new Error(result.error || '记忆读取失败');
      memoryTotal = result.total ?? result.count ?? 0;
      if (memoryOffset >= memoryTotal && memoryOffset > 0) { memoryOffset = Math.max(0, Math.floor((memoryTotal - 1) / memoryLimit) * memoryLimit); return loadMemories(); }
      const memories = result.memories || [];
      $('memoryTotal').textContent = `${memoryTotal} 条${query ? '匹配的' : ''}记忆`;
      $('memoryRange').textContent = memories.length ? `${memoryOffset + 1}–${memoryOffset + memories.length} / ${memoryTotal}` : '0 / 0';
      $('memoryPrev').disabled = memoryOffset === 0; $('memoryNext').disabled = memoryOffset + memories.length >= memoryTotal;
      $('clearMemories').disabled = result.available === false;
      if (!memories.length) { empty($('memoryList'), result.available === false ? '记忆库尚未就绪' : query ? '没有匹配的记忆' : '还没有值得记住的事', result.available === false ? '启动桥接后即可查看共享记忆。' : query ? '换个关键词，或清空搜索查看全部。' : '启用共享记忆后，对话中有用的信息会逐渐出现在这里。'); return; }
      $('memoryList').replaceChildren(...memories.map(memory => {
        const row = element('article', 'memory-row'), content = element('div');
        content.append(element('p', 'memory-text', memory.content));
        const provenance = element('div', 'memory-meta');
        if (memory.actor_id) provenance.append(element('span', '', `微信ID：${memory.actor_id}`));
        if (memory.session_id) provenance.append(element('span', '', `会话：${memory.session_id}`));
        if (memory.provenance) provenance.append(element('span', '', ({extracted:'原话抽取 · 用户陈述', manual:'人工记忆', legacy:'旧版记录'})[memory.provenance] || '来源待核实'));
        content.append(provenance);
        if (memory.evidence) { const evidence = element('details', 'memory-evidence'); evidence.append(element('summary', 'small subtle', `查看原文证据${memory.evidence_message_id ? ' · 消息 '+memory.evidence_message_id : ''}`), element('p', 'small subtle', memory.evidence)); content.append(evidence); }
        const meta = element('div', 'memory-meta'); meta.append(element('span', '', `#${memory.id}`), element('span', '', `来源：${memory.source || '对话'}`), element('time', '', `更新：${formatTime(memory.updated_at || memory.created_at, true)}`)); content.append(meta);
        const button = element('button', 'memory-delete', '删除'); button.setAttribute('aria-label', `删除记忆 ${memory.id}`);
        button.addEventListener('click', async () => { if (await askConfirm('删除这条记忆？', String(memory.content).slice(0, 180), '删除记忆')) await busy(button, async () => { await api('/api/memories/delete', {id: Number(memory.id)}); toast('记忆已删除'); await loadMemories(); await refreshStatus(); }); });
        row.append(content, button); return row;
      }));
    } catch (error) { if (request === memoryRequest) empty($('memoryList'), '记忆没有加载成功', error.message, loadMemories); }
  }
  function diagnosticRow(target, name, value) { const row = element('div', 'diagnostic-row'); row.append(element('span', '', name), element('strong', '', typeof value === 'boolean' ? value ? '是' : '否' : value ?? '—')); target.append(row); }
  const diagnosticNames = {available:'可用',connected:'窗口已连接',window_found:'找到微信窗口',window_title:'窗口标题',window_handle:'窗口句柄',process_id:'进程编号',backend:'自动化方式',current_chat:'当前聊天',chat_title:'当前聊天',target_verified:'目标已确认',busy:'正在发送',last_error:'最近失败原因',clipboard_restore:'恢复剪贴板',safe_mode:'安全模式',input_found:'找到输入框',send_button_found:'找到发送按钮',ready:'可用状态',window:'微信主窗口',editor:'输入区可读',control_count:'控件数量',tree_classes:'控件类型',compatibility:'兼容状态',accessibility_help:'处理建议',screen_reader_flag:'屏幕阅读状态',verification:'验证方式',gate_recovery:'主窗口无障碍恢复'};
  const recoveryNames = {idle:'尚未尝试',disabled:'已关闭',closed:'已还原临时状态',active_pending_control_check:'临时状态已启用',already_active_external:'检测到已有临时状态',restore_failed:'还原未确认',unavailable:'未能恢复'};
  const buildCheckNames = {not_checked:'尚未采集',matched:'已匹配复核方案，仍需检查 UIA 控件',version_unavailable:'微信模块版本读取失败',version_mismatch:'尚未匹配已复核版本',sha256_unavailable:'模块 SHA256 读取失败',sha256_mismatch:'同版本模块 SHA256 未匹配复核方案',image_size_unavailable:'模块映像大小读取失败',image_size_mismatch:'模块映像大小不匹配',architecture_unavailable:'模块真实架构未能读取',architecture_mismatch:'模块架构不匹配',ambiguous_profile:'恢复方案不唯一，已停止'};
  Object.assign(diagnosticNames, {safety:'发送前目标确认',delivery_verification:'发送验证范围'});
  const diagnosticValues = {safety:{unique_search_and_header_and_focus:'精确搜索、聊天标题与输入焦点'},delivery_verification:{local_ui_only:'微信界面接收状态'},verification:{local_ui_accepted:'微信界面已接受',not_confirmed:'未确认',not_attempted:'尚未尝试'}};
  async function loadDiagnostics() {
    const result = await Promise.allSettled([api('/api/uia/diagnostics'), api('/api/audio/status')]);
    const uia = $('uiaDiagnostics'); uia.replaceChildren();
    if (result[0].status === 'rejected') empty(uia, '无法读取微信发送器', result[0].reason.message);
    else {
      const response = result[0].value;
      if (response.ok === false) uia.append(element('p', 'diagnostic-error', response.error || '发送器尚未就绪'));
      else {
        const data = response.diagnostics || {};
        Object.entries(data).filter(([, value]) => value === null || !['object', 'function'].includes(typeof value)).forEach(([key, value]) => diagnosticRow(uia, diagnosticNames[key] || key, Object.hasOwn(diagnosticValues, key) && Object.hasOwn(diagnosticValues[key], value) ? diagnosticValues[key][value] : value));
        const recovery = data.gate_recovery;
        if (recovery && typeof recovery === 'object') {
          diagnosticRow(uia, '主窗口无障碍恢复', recovery.enabled ? '已启用' : '关闭');
          diagnosticRow(uia, '恢复状态', recoveryNames[recovery.status] || '等待检查');
          const observed = recovery.observed_build;
          const supported = Array.isArray(recovery.supported_builds) ? recovery.supported_builds : recovery.verified_build ? [recovery.verified_build] : [];
          const selected = recovery.selected_profile || recovery.verified_build;
          let candidates = supported.filter(build => build.file_version === observed?.file_version);
          const hashMatches = candidates.filter(build => build.sha256 === observed?.sha256);
          if (hashMatches.length) candidates = hashMatches;
          // A display comparison is distinct from selecting a runtime profile.
          const expected = selected || (candidates.length === 1 ? candidates[0] : {});
          const versions = [...new Set(supported.map(build => build.file_version).filter(Boolean))].join('、');
          diagnosticRow(uia, '已复核恢复构建', `${supported.length} 项${versions ? ' · '+versions : ''}`);
          diagnosticRow(uia, '匹配恢复方案', selected?.profile_id || (selected ? selected.file_version : '未匹配，不执行恢复'));
          if (selected?.review_basis === 'static_equivalence') diagnosticRow(uia, '匹配方案验证范围', '离线静态等价复核；本机 UIA 控件与发送须另行核验');
          const check = recovery.build_check;
          if (check) diagnosticRow(uia, '构建核验', buildCheckNames[check.status] || '等待检查');
          if (observed || check?.status && check.status !== 'not_checked') {
            diagnosticRow(uia, '实际微信模块版本', observed?.file_version || '未能读取');
            diagnosticRow(uia, '支持的微信模块版本', expected.file_version || versions || '尚无已复核方案');
            diagnosticRow(uia, '实际模块 SHA256', observed?.sha256 || '未能读取');
            diagnosticRow(uia, '已复核模块 SHA256', expected.sha256 || '未选定，请查看支持构建列表');
            diagnosticRow(uia, '实际模块映像大小', Number.isInteger(observed?.size_of_image) ? `${observed.size_of_image.toLocaleString()} 字节` : '未能读取');
            diagnosticRow(uia, '已复核模块映像大小', Number.isInteger(expected.size_of_image) ? `${expected.size_of_image.toLocaleString()} 字节` : '未提供');
            diagnosticRow(uia, '实际模块架构', observed?.architecture && observed.architecture !== 'unknown' ? observed.architecture : '未采集');
            diagnosticRow(uia, '已复核模块架构', expected.architecture || '未提供');
          }
          if (recovery.busy) diagnosticRow(uia, '恢复检查', '正在处理');
          if (recovery.restore_result) diagnosticRow(uia, '临时状态还原', recovery.restore_result.ok ? recovery.restore_result.status === 'not_active' || recovery.restore_result.status === 'write_not_attempted' ? '无需还原' : '已还原' : '未能确认');
          if (recovery.last_error) uia.append(element('p', 'diagnostic-error', recovery.last_error));
          const copy = element('button', 'text-button', '复制恢复检查结果'); copy.type = 'button';
          copy.addEventListener('click', async () => {
            // Copy only bounded recovery metadata, excluding window titles,
            // chat data, module paths and all provider configuration.
            const report = Object.fromEntries(['enabled','status','busy','observed_build','supported_builds','selected_profile','verified_build','build_check','restore_result','lease'].filter(key => Object.hasOwn(recovery, key)).map(key => [key, recovery[key]]));
            try { await navigator.clipboard.writeText(JSON.stringify(report, null, 2)); toast('恢复检查结果已复制'); }
            catch { toast('复制失败，请展开完整检查结果后手动复制', 'error'); }
          });
          uia.append(copy);
        }
        const details = element('details'); details.append(element('summary', 'text-button', '查看完整检查结果'));
        details.append(element('pre', 'diagnostic-details', JSON.stringify(data, null, 2))); uia.append(details);
      }
    }
    const audio = $('audioDiagnostics'); audio.replaceChildren();
    if (result[1].status === 'rejected') empty(audio, '无法读取播放设备', result[1].reason.message);
    else {
      const data = result[1].value;
      diagnosticRow(audio, '当前默认播放设备', data.device || '未能读取'); diagnosticRow(audio, '语音发送目标', data.config_vb_cable || '未配置');
      diagnosticRow(audio, '找到语音目标设备', data.target_available ? '已找到' : '未找到');
      diagnosticRow(audio, '当前默认录音设备', (data.recording_default || {}).name || '未能读取');
      diagnosticRow(audio, 'VB-Cable 录音端点', data.recording_target_available ? '已找到' : data.recording_target_count > 1 ? '多个候选，无法确认唯一设备' : '未找到');
      if (!data.target_available || !data.recording_target_available) audio.append(element('p', 'diagnostic-error', '语音发送需要 VB-Cable 的播放和录音端点。检查虚拟声卡安装和设备名称，再启用语音发送。'));
      (data.devices || []).forEach(device => { const node = element('div', 'device-item', device.name || device.id); if (device.is_default) node.append(element('span', 'badge', '当前默认')); audio.append(node); });
      (data.recording_devices || []).forEach(device => { const node = element('div', 'device-item', `录音：${device.name || device.id}`); if (device.is_default) node.append(element('span', 'badge', '当前默认')); audio.append(node); });
      if (!(data.devices || []).length) audio.append(element('p', 'subtle small', '未读取到设备列表，请检查 pycaw 依赖或系统声音设置。'));
    }
  }
  async function loadLogs() {
    const request = ++logRequest;
    try {
      const result = await api(`/api/logs?level=${encodeURIComponent($('logLevel').value)}&q=${encodeURIComponent($('logSearch').value.trim())}`);
      if (request !== logRequest) return;
      lastLog = (result.lines || []).join('\n');
      const output = $('logOutput');
      const previousTop = output.scrollTop;
      output.textContent = lastLog || '当前没有匹配的日志。调整筛选条件，或等待新的处理记录。';
      output.scrollTop = $('logFollow').checked ? output.scrollHeight : previousTop;
      $('logCount').textContent = `${result.count || 0} 行 · 已隐藏凭证`;
    } catch (error) { if (request === logRequest) $('logOutput').textContent = error.message; }
  }
  function debounce(callback, delay = 300) { let timer; return () => { clearTimeout(timer); timer = setTimeout(callback, delay); }; }
  async function loadReferences() {
    try {
      if (!settingsReady) await loadConfig();
      const result = await api('/api/references'); references = result.images || []; renderReferences();
    } catch (error) { empty($('referenceList'), '素材库暂不可用', error.message, loadReferences); }
  }
  function referenceLimit() {
    const main = $('cfg_tools_image_model')?.value || configData.tools_image_model || '';
    const model = /^qwen-image-3\.0(?:-pro)?$/.test(main) ? main : ($('cfg_bot_image_model')?.value || configData.bot_image_model || main);
    return /^qwen-image-3\.0(?:-pro)?$/.test(model) ? 3 : model === 'wan2.6-image' ? 4 : 9;
  }
  function renderReferences() {
    const limit = referenceLimit();
    $('referenceLimitHint').textContent = `当前模型最多选择 ${limit} 张参考图。`;
    if (!references.length) { empty($('referenceList'), '还没有参考图', '上传角色或风格参考图，再勾选用于生图。'); return; }
    const selected = String($('cfg_bot_image_refs')?.value || '').split(',').map(value => value.trim());
    $('referenceList').replaceChildren(...references.map(item => {
      const card = element('article', 'reference-card'), image = element('img');
      image.src = `/api/references/image/${encodeURIComponent(item.id)}`; image.alt = item.name; image.loading = 'lazy';
      const label = element('label', 'checkbox-label'), check = element('input'); check.type = 'checkbox'; check.checked = selected.includes(item.value);
      check.addEventListener('change', () => {
        const input = $('cfg_bot_image_refs'), values = input.value.split(',').map(value => value.trim()).filter(Boolean);
        const next = values.filter(value => value !== item.value);
        if (check.checked) next.push(item.value);
        if (next.length > referenceLimit()) { check.checked = false; toast(`当前模型最多选择 ${referenceLimit()} 张参考图`, 'error'); return; }
        input.value = next.join(', '); setDirty(Object.keys(configChanges()).length > 0);
      });
      label.append(check, document.createTextNode(item.name));
      const remove = element('button', 'text-button', '删除素材'); remove.type = 'button';
      remove.addEventListener('click', async () => {
        if (item.builtin) return;
        if (dirty) { toast('请先保存或重新载入未保存的设置，再删除素材', 'error'); return; }
        if (!await askConfirm('删除这张参考图？', `“${item.name}”会从本机素材库移除，同时取消已保存的选择。`, '删除素材')) return;
        await busy(remove, async () => { await api('/api/references/remove', {id:item.id}); await loadConfig(); await loadReferences(); toast('素材已删除'); });
      });
      if (item.builtin) { remove.disabled = true; remove.textContent = '内置示例'; }
      card.append(image, label, element('p', 'small subtle', item.note || `${item.width} × ${item.height}`), remove); return card;
    }));
  }
  async function uploadReference(event) {
    event.preventDefault();
    const file = $('referenceFile').files[0]; if (!file) return;
    await busy($('uploadReference'), async () => {
      if (file.size > 20*1024*1024) throw new Error('单张参考图最多 20MB');
      const encoded = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('无法读取图片')); reader.readAsDataURL(file); });
      await api('/api/references/upload', {data_base64: encoded, name:$('referenceName').value.trim() || file.name, note:$('referenceNote').value.trim()}, 60000);
      $('referenceUpload').reset(); await loadReferences(); toast('参考图已保存，可勾选后保存设置');
    });
  }
  let voicePage = 0;
  async function loadVoices() {
    await busy($('refreshVoices'), async () => {
      const result = await api('/api/voices/list', {page_index:voicePage, page_size:20}, 60000);
      const voices = result.voices || [];
      $('voiceList').replaceChildren(...voices.map(item => {
        const row = element('article', 'voice-row'), info = element('div');
        info.append(element('strong', '', item.voice_id), element('p', 'small subtle', `${item.status} · ${item.target_model || '请核验模型'} · ${item.gmt_create || '创建时间未提供'}`));
        const select = element('button', 'button', '使用此音色'); select.type = 'button'; select.disabled = item.status !== 'OK';
        select.addEventListener('click', () => { if (!item.target_model) { toast('请先核验音色的合成模型', 'error'); return; } $('cfg_tts_voice').value = item.voice_id; $('cfg_tts_model').value = item.target_model; setDirty(Object.keys(configChanges()).length > 0); toast('音色及对应模型已选择，请保存设置'); });
        const query = element('button', 'text-button', '核验状态'); query.type = 'button';
        query.addEventListener('click', () => busy(query, async () => { const value = await api('/api/voices/query', {voice_id:item.voice_id}, 60000); item.target_model = value.target_model; info.querySelector('p').textContent = `${value.status} · ${value.target_model}`; select.disabled = value.status !== 'OK' || !value.target_model; }));
        const remove = element('button', 'text-button', '删除'); remove.type = 'button';
        remove.addEventListener('click', async () => { if (dirty) { toast('请先保存设置，避免删除正在选择的音色', 'error'); return; } if (await askConfirm('删除克隆音色？', item.voice_id, '删除音色')) await busy(remove, async () => { await api('/api/voices/delete', {voice_id:item.voice_id, confirm:'delete_voice'}, 60000); await loadVoices(); }); });
        row.append(info, select, query, remove); return row;
      }));
      if (!voices.length) $('voiceList').append(element('p', 'subtle', '这一页没有克隆音色。'));
      const navigation = element('div', 'pending-actions');
      const prev = element('button', 'button', '上一页'), next = element('button', 'button', '下一页'); prev.type = next.type = 'button';
      prev.disabled = voicePage === 0; next.disabled = voices.length < 20;
      prev.addEventListener('click', () => { voicePage--; loadVoices(); }); next.addEventListener('click', () => { voicePage++; loadVoices(); });
      navigation.append(prev, element('span', 'small subtle', `第 ${voicePage+1} 页`), next); $('voiceList').append(navigation);
    });
  }
  $('referenceUpload').addEventListener('submit', uploadReference);
  $('refreshReferences').addEventListener('click', loadReferences);
  $('refreshVoices').addEventListener('click', loadVoices);
  $('voiceClone').addEventListener('submit', async event => {
    event.preventDefault();
    if (!await askConfirm('创建克隆音色？', '供应商会读取填写的公开录音，并可能按其规则计费。请确认拥有使用该声音的授权。', '创建音色')) return;
    await busy($('createVoice'), async () => {
      const result = await api('/api/voices/create', {url:$('cloneURL').value.trim(), prefix:$('clonePrefix').value.trim(), target_model:$('cloneModel').value.trim(), confirm:'create_voice'}, 90000);
      toast(`已创建 ${result.voice_id}，请读取音色列表并核验就绪状态`); voicePage = 0; await loadVoices();
    });
  });
  $('previewVoice').addEventListener('click', () => busy($('previewVoice'), async () => {
    if (dirty) throw new Error('请先保存设置再试听');
    const result = await api('/api/voices/preview', {text:$('voicePreviewText').value.trim()}, 90000);
    $('voiceAudio').src = `/api/voices/audio/${encodeURIComponent(result.audio_id)}`; $('voiceAudio').hidden = false; await $('voiceAudio').play();
  }));
  document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => navigate(button.dataset.page)));
  document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => performAction(button)));
  document.querySelectorAll('[data-go]').forEach(button => button.addEventListener('click', () => navigate(button.dataset.go)));
  document.querySelectorAll('[data-setting]').forEach(button => button.addEventListener('click', () => jumpSetting(button.dataset.setting)));
  $('btnMode').addEventListener('click', () => busy($('btnMode'), async () => { if (dirty) throw new Error('请先保存设置页中的修改，再切换群聊模式'); await api('/mode', {}); configData.group_reply_mode = undefined; settingsReady = false; await refreshStatus(); toast('群聊回复模式已切换'); }));
  ['settingsForm', 'modelConfiguration', 'momentsConfiguration', 'mediaConfiguration', 'voiceConfiguration'].forEach(id => {
    $(id).addEventListener('input', event => { setDirty(Object.keys(configChanges()).length > 0); if (['cfg_tools_image_model', 'cfg_bot_image_model'].includes(event.target.id)) renderReferences(); });
    $(id).addEventListener('change', () => setDirty(Object.keys(configChanges()).length > 0));
    $(id).addEventListener('submit', event => { event.preventDefault(); saveConfig(); });
  });
  $('saveConfig').addEventListener('click', saveConfig);
  $('reloadConfig').addEventListener('click', async () => { if (!dirty || await askConfirm('重新载入设置？', '未保存的修改会被丢弃。', '重新载入')) await busy($('reloadConfig'), loadConfig); });
  $('configSearch').addEventListener('input', filterConfig);
  $('memorySearch').addEventListener('input', debounce(() => { memoryOffset = 0; loadMemories(); }));
  $('refreshMemories').addEventListener('click', () => busy($('refreshMemories'), loadMemories));
  $('memoryPrev').addEventListener('click', () => { memoryOffset = Math.max(0, memoryOffset - memoryLimit); loadMemories(); });
  $('memoryNext').addEventListener('click', () => { memoryOffset += memoryLimit; loadMemories(); });
  $('clearMemories').addEventListener('click', async () => { if (await askConfirm('清空全部共享记忆？', '所有记忆及本地聊天日志会永久删除，包括当前搜索没有显示的内容。', '清空全部记忆')) await busy($('clearMemories'), async () => { const response = await api('/api/memories/clear', {confirm: 'clear_all_memories'}); if (!response.ok) throw new Error('记忆清空失败'); memoryOffset = 0; toast('共享记忆与本地聊天日志已清空'); await loadMemories(); await refreshStatus(); }); });
  $('refreshDiagnostics').addEventListener('click', () => busy($('refreshDiagnostics'), loadDiagnostics));
  $('openSoundSettings').addEventListener('click', () => busy($('openSoundSettings'), async () => { const result = await api('/api/audio/open-settings', {}); if (!result.ok) throw new Error('系统声音设置未能打开，请从 Windows 设置进入'); toast('系统声音设置已打开'); }));
  $('autopilotPublished').addEventListener('click', () => resolveAutopilotPending('published'));
  $('autopilotNotPublished').addEventListener('click', () => resolveAutopilotPending('not_published'));
  $('momentsBrowseNow').addEventListener('click', browseMomentsNow);
  $('firstOnlinePublish').addEventListener('click', async () => {
    if ($('firstOnlinePublish').disabled) return;
    announcementRequested = true;
    announcementError = '';
    $('firstOnlinePublish').disabled = true;
    $('firstOnlinePublish').setAttribute('aria-busy', 'true');
    $('firstOnlineResult').textContent = '正在提交发布请求…';
    try {
      const task = await api('/api/autopilot/first-online', {});
      $('firstOnlineResult').textContent = task.message;
      toast(task.message);
    } catch (error) {
      announcementError = error.message;
      $('firstOnlineResult').textContent = announcementError;
      toast(error.message, 'error');
    } finally {
      announcementRequested = false;
      await refreshStatus();
    }
  });
  $('momentsInteractionCompleted').addEventListener('click', () => resolveMomentsInteraction('published'));
  $('momentsInteractionNotCompleted').addEventListener('click', () => resolveMomentsInteraction('not_published'));
  $('logLevel').addEventListener('change', loadLogs); $('logSearch').addEventListener('input', debounce(loadLogs));
  $('refreshLogs').addEventListener('click', () => busy($('refreshLogs'), loadLogs));
  $('exportLogs').addEventListener('click', () => { const url = URL.createObjectURL(new Blob([lastLog], {type: 'text/plain;charset=utf-8'})); const anchor = element('a'); anchor.href = url; anchor.download = `wechat-bot-log-${new Date().toISOString().slice(0, 10)}.txt`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
  $('retryConnection').addEventListener('click', () => $('retryConnection').dataset.expired ? location.reload() : refreshStatus());
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && configPages.includes(page)) { event.preventDefault(); saveConfig(); } });
  window.addEventListener('hashchange', () => navigate(location.hash.slice(1)));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshStatus(); });
  $('clock').textContent = new Date().toLocaleDateString('zh-CN', {month: 'long', day: 'numeric', weekday: 'short'});
  navigate(location.hash.slice(1) || 'workspace');
  if (page !== 'settings') loadConfig();
  refreshStatus();
  const statusTimer = setInterval(refreshStatus, 3000);
})();
