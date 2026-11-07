'use strict';

// Keys live in the browser only. sessionStorage is the default (cleared when the
// tab closes); "remember" also writes localStorage so embedded previews that
// open a fresh session do not ask for the key on every reload.
const KEY_STORAGE = { gateway: 'planrouter_key', admin: 'planrouter_admin_key' };

function readStoredKey(kind) {
  try {
    return sessionStorage.getItem(KEY_STORAGE[kind])
      || localStorage.getItem(KEY_STORAGE[kind]) || '';
  } catch (_) { return ''; }
}

function storeKey(kind, value, remember) {
  try {
    if (value) sessionStorage.setItem(KEY_STORAGE[kind], value);
    else sessionStorage.removeItem(KEY_STORAGE[kind]);
    if (remember && value) localStorage.setItem(KEY_STORAGE[kind], value);
    else localStorage.removeItem(KEY_STORAGE[kind]);
  } catch (_) { /* private mode */ }
}

const state = {
  config: null,
  snapshot: null,
  tab: 'channels',
  key: readStoredKey('gateway'),
  adminKey: readStoredKey('admin'),
  requests: null,
  editing: -1,
  dirty: false,
  subs: null,
  subDirty: false,
  editingSub: -1,
  providers: null,
  usage: null,
  usageRange: 30,
  notify: null,
  report: null,
  maintenance: null,
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

// Embedded/IDE webviews have no native dialogs (calling one throws
// "prompt() is not supported."). Use an in-page overlay instead, and never
// reuse #modal so an open channel/subscription editor keeps its unsaved draft.
function askText({ title, message, value = '', password = false, remember = false,
  withInput = true, confirmText = '确定' }) {
  return new Promise((resolve) => {
    const wrap = document.createElement('div');
    wrap.className = 'modal-backdrop ask-backdrop';
    wrap.innerHTML = `<div class="modal ask-modal" role="dialog" aria-modal="true">
      <div class="modal-head"><h2>${esc(title)}</h2></div>
      <p class="hint ask-message">${esc(message)}</p>
      ${withInput ? `<input class="ask-input" type="${password ? 'password' : 'text'}" value="${esc(value)}" autocomplete="off" spellcheck="false">` : ''}
      ${remember ? '<label class="check ask-remember"><input type="checkbox"> 在本机记住（下次打开免填）</label>' : ''}
      <div class="modal-foot">
        <button type="button" class="btn ghost" data-ask="cancel">取消</button>
        <button type="button" class="btn primary" data-ask="ok">${esc(confirmText)}</button>
      </div>
    </div>`;
    document.body.appendChild(wrap);
    const input = wrap.querySelector('.ask-input');
    const rememberBox = wrap.querySelector('.ask-remember input');
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey, true);
      wrap.remove();
      resolve(ok ? {
        value: input ? input.value.trim() : '',
        remember: !!(rememberBox && rememberBox.checked),
      } : null);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault(); e.stopPropagation(); finish(false);
      } else if (e.key === 'Enter' && input && e.target === input) {
        e.preventDefault(); e.stopPropagation(); finish(true);
      }
    };
    // Capture phase: stop the page-wide Escape handler from closing an open editor.
    document.addEventListener('keydown', onKey, true);
    wrap.querySelector('[data-ask="cancel"]').onclick = () => finish(false);
    wrap.querySelector('[data-ask="ok"]').onclick = () => finish(true);
    if (input) { input.focus(); input.select(); }
    else wrap.querySelector('[data-ask="ok"]').focus();
  });
}

async function askConfirm(title, message) {
  return (await askText({ title, message, withInput: false, confirmText: '确认' })) !== null;
}

// Concurrent 401s during boot must share one dialog instead of stacking overlays.
const authPrompts = { api: null, gateway: null };

function promptForAuth(isApi) {
  const kind = isApi ? 'api' : 'gateway';
  if (!authPrompts[kind]) {
    authPrompts[kind] = askText({
      title: isApi ? '需要管理密钥' : '需要网关密钥',
      message: isApi
        ? '管理接口返回 401。填写 listen.admin_key（未单独设置时即网关密钥），也可点右上角「管理密钥」。'
        : '网关接口返回 401。填写 listen.api_key，也可点右上角「网关密钥」。',
      password: true,
      remember: true,
    }).finally(() => { authPrompts[kind] = null; });
  }
  return authPrompts[kind];
}

async function api(path, opts = {}) {
  const headers = {};
  if (state.key) headers.Authorization = 'Bearer ' + state.key;
  if (path.startsWith('/api/') && state.adminKey) headers['X-Admin-Key'] = state.adminKey;
  let body;
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, { method: opts.method || 'GET', headers, body });
  if (res.status === 401 && !opts.retried) {
    const isApi = path.startsWith('/api/');
    const answer = await promptForAuth(isApi);
    if (answer && answer.value) {
      if (isApi) {
        state.adminKey = answer.value;
        storeKey('admin', state.adminKey, answer.remember);
      } else {
        state.key = answer.value;
        storeKey('gateway', state.key, answer.remember);
      }
      return api(path, { ...opts, retried: true });
    }
    throw new Error(isApi
      ? '未授权：请点右上角「管理密钥」填入 admin_key 后重试'
      : '未授权：请点右上角「网关密钥」填入 api_key 后重试');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data.error && data.error.message) || ('HTTP ' + res.status));
  return data;
}

function toast(message, kind = 'ok') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = message;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

const DRAFT_CONFIG = 'planrouter_draft_config';
const DRAFT_SUBS = 'planrouter_draft_subs';
const saveTimers = { config: null, subs: null };

function stashDraft(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* quota */ }
}
function dropDraft(key) {
  try { localStorage.removeItem(key); } catch (_) { /* ignore */ }
}
function readDraft(key) {
  try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { return null; }
}

// Every edit is persisted on its own; there is no separate "save and apply"
// step. Debounced so typing in the model-policy box does not POST per keystroke.
function scheduleSave(kind) {
  clearTimeout(saveTimers[kind]);
  saveTimers[kind] = setTimeout(() => {
    saveTimers[kind] = null;
    if (kind === 'subs') saveSubscriptions(); else saveConfig();
  }, 600);
}

function holdDirty() {
  state.dirty = true;
  $('btn-save').classList.add('dirty');
  $('btn-save').textContent = '立即保存 *';
  stashDraft(DRAFT_CONFIG, state.config);
}

function markDirty() {
  holdDirty();
  scheduleSave('config');
}

function clearDirty() {
  state.dirty = false;
  $('btn-save').classList.remove('dirty');
  $('btn-save').textContent = '立即保存';
  dropDraft(DRAFT_CONFIG);
}

function normalizeConfig() {
  const c = state.config;
  c.channels = c.channels || [];
  c.routes = c.routes || [];
  c.listen = c.listen || {};
  c.defaults = c.defaults || {};
  c.models = c.models || {};
  c.models.branded = c.models.branded || {};
  if (c.models.fallback_unknown === undefined) c.models.fallback_unknown = true;
  c.models.fallback_route = c.models.fallback_route || 'default';
}

async function loadConfig() {
  state.config = await api('/api/config');
  normalizeConfig();
  renderAll();
}

async function loadState() {
  state.snapshot = await api('/api/state');
  renderSummary();
  renderStatus();
  renderChannels();
  const badge = $('conn-badge');
  badge.textContent = '已连接';
  badge.className = 'badge ok';
  $('footer-config').textContent = state.snapshot.config_path
    ? '配置文件：' + state.snapshot.config_path : '';
  renderBanner();
}

function rateText(rate) {
  if (!rate) return '未设置';
  const parts = ['rpm', 'rpd', 'tpm', 'tpd'].filter((k) => rate[k]).map((k) => k + '=' + rate[k]);
  return parts.length ? parts.join(' · ') : '未设置';
}

function keyText(key) {
  if (!key) return '未设置';
  if (String(key).startsWith('env:')) return esc(key);
  return '已保存（不显示）';
}

function serializeModels(models) {
  return Object.entries(models || {}).map(([k, v]) => k + '=' + v).join('\n');
}

function parseModels(text) {
  const out = {};
  text.split('\n').forEach((line) => {
    const s = line.trim();
    if (!s) return;
    const i = s.indexOf('=');
    if (i > 0) out[s.slice(0, i).trim()] = s.slice(i + 1).trim();
    else out[s] = s;
  });
  return out;
}

function tierBadge(tier) {
  if (tier === 'free') return '<span class="tag free">免费</span>';
  if (tier === 'paid') return '<span class="tag paid">收费</span>';
  return '';
}

// Tier resolution for a model on a channel: a manual per-model marking wins
// ("tiers" map), then the channel default (explicit field or inferred from
// subscriptions in the snapshot); falls back to the raw config before loading.
function tierFor(name, fallback, model) {
  const snap = (state.snapshot?.channels || []).find((x) => x.name === name);
  if (model) {
    if (snap?.tiers?.[model]) return snap.tiers[model];
    const cfg = (state.config?.channels || []).find((x) => x.name === name);
    if (cfg?.tiers?.[model]) return cfg.tiers[model];
  }
  return snap ? snap.tier : (fallback || '');
}

// Channel badge summarising its per-model markings: mixed when the manual
// markings disagree, otherwise the marked tier, otherwise the channel default.
function channelTierBadge(name, fallback) {
  const cfg = (state.config?.channels || []).find((x) => x.name === name);
  const snap = (state.snapshot?.channels || []).find((x) => x.name === name);
  const marks = Object.values(cfg?.tiers || {}).length
    ? Object.values(cfg.tiers) : Object.values(snap?.tiers || {});
  if (marks.length) {
    const free = marks.includes('free');
    const paid = marks.includes('paid');
    if (free && paid) return '<span class="tag mixed">混合</span>';
    return tierBadge(free ? 'free' : 'paid');
  }
  return tierBadge(tierFor(name, fallback));
}

function catalogSummary(name) {
  const snap = (state.snapshot?.channels || []).find((c) => c.name === name);
  const cat = snap && snap.catalog;
  if (!cat || !cat.fetched_at) return '未拉取';
  const when = new Date(cat.fetched_at * 1000).toLocaleString();
  const changes = cat.changes || {};
  const diff = (changes.added?.length || changes.removed?.length)
    ? `（较上次 新增 ${changes.added?.length || 0} / 移除 ${changes.removed?.length || 0}）` : '';
  return `${cat.count} 个模型 · 检查于 ${when}${diff}`;
}

function channelCard(c, i) {
  const entries = Object.entries(c.models || {});
  const bit = ([k, v]) => {
    const badge = tierBadge(tierFor(c.name, c.tier, k));
    return `${esc(k)} → ${esc(v)}${badge ? ' ' + badge : ''}`;
  };
  // Long mappings (aggregators can hold hundreds of models) collapse to the
  // first few plus an expander so the card cannot stretch the page.
  const SHOW = 8;
  const modelBits = entries.length > SHOW
    ? `${entries.slice(0, SHOW).map(bit).join(' · ')} <button type="button" class="btn small ghost" data-action="toggle-models" data-total="${entries.length}">… 共 ${entries.length} 个</button><span class="models-more" hidden>${entries.slice(SHOW).map(bit).join(' · ')}</span>`
    : entries.map(bit).join(' · ');
  const ptag = providerTag(c);
  return `<article class="card">
    <div class="card-top">
      <div><span class="pill ${esc(c.protocol)}">${esc(c.protocol)}</span><h3>${esc(c.name)}</h3>${channelTierBadge(c.name, c.tier)}${ptag ? `<small class="provider-tag">${esc(ptag)}</small>` : ''}</div>
      <span class="switch ${c.enabled === false ? 'off' : ''}">${c.enabled === false ? '已停用' : '启用'}</span>
    </div>
    <dl>
      <div><dt>端点</dt><dd>${esc(c.base_url)}</dd></div>
      <div><dt>优先级</dt><dd>${esc(c.priority ?? 100)}</dd></div>
      <div><dt>额度冷却</dt><dd>${esc(c.cooldown_minutes ?? 300)} 分钟</dd></div>
      <div><dt>本地限流</dt><dd>${esc(rateText(c.rate))}</dd></div>
      <div><dt>模型映射</dt><dd class="wrap">${modelBits || '未配置'}</dd></div>
      <div><dt>模型目录</dt><dd class="wrap">${esc(catalogSummary(c.name))}</dd></div>
      <div><dt>密钥</dt><dd>${keyText(c.api_key)}</dd></div>
    </dl>
    <div class="card-actions">
      <button class="btn small" data-action="edit" data-index="${i}">编辑</button>
      <button class="btn small ghost" data-action="catalog" data-index="${i}" data-name="${esc(c.name)}">拉取模型</button>
      <button class="btn small ghost" data-action="toggle" data-index="${i}">${c.enabled === false ? '启用' : '停用'}</button>
      <button class="btn small ghost" data-action="reset" data-name="${esc(c.name)}">解除冷却</button>
      <button class="btn small danger" data-action="delete" data-index="${i}">删除</button>
    </div>
  </article>`;
}

function renderChannels() {
  const list = $('channel-list');
  list.innerHTML = state.config.channels.length
    ? state.config.channels.map(channelCard).join('')
    : '<div class="card"><p class="hint">还没有渠道，点击「新增渠道」开始。</p></div>';
}

function routeCard(r, ri) {
  const nodes = r.channels || [];
  const options = state.config.channels.filter((c) => c.protocol === r.protocol && !nodes.includes(c.name));
  return `<article class="card">
    <div class="card-top">
      <div><span class="pill ${esc(r.protocol)}">${esc(r.protocol)}</span><h3>${esc(r.name)}</h3></div>
      <button class="btn small danger" data-action="delete-route" data-index="${ri}">删除链</button>
    </div>
    <ol class="chain">
      ${nodes.length ? nodes.map((n, ci) => `<li>
        <span class="order">${ci + 1}</span><span>${esc(n)}</span>
        <span class="chain-actions">
          <button class="icon" data-action="move-up" data-index="${ri}" data-pos="${ci}" ${ci === 0 ? 'disabled' : ''}>上移</button>
          <button class="icon" data-action="move-down" data-index="${ri}" data-pos="${ci}" ${ci === nodes.length - 1 ? 'disabled' : ''}>下移</button>
          <button class="icon" data-action="remove-node" data-index="${ri}" data-pos="${ci}">移除</button>
        </span></li>`).join('') : '<li class="empty">链内暂无渠道</li>'}
    </ol>
    <div class="add-node">
      <select id="add-select-${ri}" aria-label="选择渠道">
        ${options.length ? options.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('') : '<option value="">无同协议可用渠道</option>'}
      </select>
      <button class="btn small" data-action="add-node" data-index="${ri}">添加渠道</button>
    </div>
  </article>`;
}

function renderRoutes() {
  $('route-list').innerHTML = state.config.routes.length
    ? state.config.routes.map(routeCard).join('')
    : '<div class="card"><p class="hint">还没有路由链。</p></div>';
}

function renderSummary() {
  const s = state.snapshot;
  if (!s) return;
  const cooling = s.channels.filter((c) => c.cooling).length;
  const requests = s.channels.reduce((n, c) => n + c.total_requests, 0);
  const tokens = s.channels.reduce((n, c) => n + c.total_tokens, 0);
  const listen = s.listen ? `${s.listen.host}:${s.listen.port}` : '—';
  const items = [
    ['渠道总数', s.channels.length, ''],
    ['可用渠道', s.channels.length - cooling, 'ok'],
    ['冷却中', cooling, cooling ? 'warn' : ''],
    ['累计请求', requests, ''],
    ['累计 Tokens', tokens.toLocaleString(), ''],
    ['监听地址', listen, ''],
  ];
  $('summary').innerHTML = items.map(([label, value, cls]) =>
    `<div class="metric"><div class="label">${label}</div><div class="value ${cls}">${esc(value)}</div></div>`).join('');
}

function renderStatus() {
  const s = state.snapshot;
  if (!s) return;
  const catalogText = s.catalog_refresh_hours
    ? `模型目录每 ${s.catalog_refresh_hours} 小时自动检查` : '模型目录自动检查已停用';
  $('status-meta').textContent = `粘性会话 ${s.sticky_sessions} · ${catalogText} · 每 5 秒自动刷新 · 更新于 ${new Date().toLocaleTimeString()}`;
  $('status-body').innerHTML = s.channels.map((c) => {
    const kind = c.blocked ? 'bad' : (c.cooling ? 'warn' : 'ok');
    const label = c.blocked ? '已停用' : (c.cooling ? '冷却中' : '可用');
    const reason = c.blocked ? c.blocked_reason : c.reason;
    const q = c.quota || {};
    const quotaText = q.known
      ? (q.ratio !== null && q.ratio !== undefined
          ? `额度 ${Math.round(q.ratio * 100)}%`
          : `余量 ${q.requests_remaining ?? q.tokens_remaining ?? '—'}`)
      : '';
    const quotaLow = q.exhausted || (q.ratio !== null && q.ratio !== undefined && q.ratio <= 0.15);
    const total = Math.max(c.cooldown_minutes * 60, c.cooling_seconds, 1);
    const pct = Math.min(100, (c.cooling_seconds / total) * 100);
    const remain = c.cooling
      ? `${Math.ceil(c.cooling_seconds / 60)} 分钟<div class="bar"><span style="width:${pct}%"></span></div>` : '—';
    return `<tr>
      <td><span class="dot ${kind}"></span><strong>${esc(c.name)}</strong> ${channelTierBadge(c.name, c.tier)}<br><span class="pill ${esc(c.protocol)}">${esc(c.protocol)}</span></td>
      <td>${label}${reason ? '<br><small>' + esc(reason) + '</small>' : ''}${quotaText ? `<br><span class="sub-note ${quotaLow ? 'quota-low' : ''}">${esc(quotaText)}</span>` : ''}</td>
      <td>${remain}</td>
      <td>${c.total_requests} / ${c.served_requests} / ${c.failed_requests}</td>
      <td>${c.total_tokens.toLocaleString()}</td>
      <td>${c.handoffs}</td>
      <td><span class="err" title="${esc(c.last_error)}">${esc(c.last_error) || '—'}</span></td>
      <td><button class="btn small ghost" data-action="reset" data-name="${esc(c.name)}">解除冷却</button></td>
    </tr>`;
  }).join('');
}

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function addMonthsISO(iso, months, fromToday) {
  const base = (fromToday || !iso) ? todayISO() : iso;
  const [y, m, d] = base.split('-').map(Number);
  const first = new Date(y, m - 1 + months, 1);
  const lastDay = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  return `${first.getFullYear()}-${String(first.getMonth() + 1).padStart(2, '0')}-${String(Math.min(d, lastDay)).padStart(2, '0')}`;
}

function holdSubDirty() {
  state.subDirty = true;
  $('btn-save-subs').classList.add('dirty');
  $('btn-save-subs').textContent = '立即保存订阅 *';
  stashDraft(DRAFT_SUBS, state.subs ? state.subs.subscriptions : []);
}

function markSubDirty() {
  holdSubDirty();
  scheduleSave('subs');
}

function clearSubDirty() {
  state.subDirty = false;
  $('btn-save-subs').classList.remove('dirty');
  $('btn-save-subs').textContent = '立即保存订阅';
  dropDraft(DRAFT_SUBS);
}

async function loadSubscriptions() {
  state.subs = await api('/api/subscriptions');
  renderSubscriptions();
}

// Vendor registry for the channel editor: load once, tolerate absence so
// a console served by an older backend still works.
async function loadProviders() {
  try {
    state.providers = (await api('/api/providers')).providers || [];
  } catch (_) {
    state.providers = [];
  }
}

function renderBanner() {
  const summary = state.snapshot && state.snapshot.subscriptions;
  const el = $('renewal-banner');
  const alerts = (summary && summary.alerts) || [];
  const quotaAlerts = (summary && summary.quota_alerts) || [];
  if (!alerts.length && !quotaAlerts.length) {
    el.innerHTML = '';
    return;
  }
  const critical = alerts.some((a) => a.status === 'expired');
  const items = alerts.map((a) => {
    const days = a.status === 'expired' ? `已过期 ${Math.abs(a.days_left)} 天` : `剩 ${a.days_left} 天`;
    return `<span class="banner-item">${esc(a.vendor)} / ${esc(a.account || '—')} / ${esc(a.plan)} · <span class="days">${days}</span></span>`;
  }).join('') + quotaAlerts.map((a) =>
    `<span class="banner-item">${esc(a.vendor)} / ${esc(a.account || '—')} / ${esc(a.plan)} · <span class="days">额度仅剩 ${esc(a.quota_remaining)} ${esc(a.quota_unit)}</span></span>`).join('');
  el.innerHTML = `<div class="banner ${critical ? 'critical' : ''}">
    <span class="banner-title">${critical ? '有套餐已过期' : '套餐与额度提醒'}</span>
    <span class="banner-items">${items}</span>
    <button class="btn small" id="banner-view">查看订阅</button>
  </div>`;
  $('banner-view').onclick = () => switchTab('subscriptions');
}

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + name));
  if (name === 'models') renderModels();
  if (name === 'status') {
    loadState().catch(() => {});
    loadUsage().catch((e) => toast(e.message, 'bad'));
    loadRequests().catch(() => {});
    loadMaintenance().catch(() => {});
  }
  if (name === 'subscriptions') {
    loadNotifications().catch((e) => toast(e.message, 'bad'));
    loadReport($('report-month').value).catch(() => {});
    if (!state.subDirty) loadSubscriptions().catch((e) => toast(e.message, 'bad'));
  }
}

function renderSubscriptions() {
  if (!state.subs) return;
  const records = state.subs.subscriptions || [];
  const summary = state.subs.summary || {};
  const counts = summary.counts || {};
  const cost = Object.entries(summary.monthly_cost || {})
    .map(([code, amount]) => `${amount} ${code}`).join(' / ') || '—';
  const metrics = [
    ['订阅总数', summary.total ?? 0, ''],
    ['生效中', counts.active ?? 0, 'ok'],
    ['即将到期', counts.expiring ?? 0, counts.expiring ? 'warn' : ''],
    ['已过期', counts.expired ?? 0, counts.expired ? 'warn' : ''],
    ['每月等效成本', cost, ''],
  ];
  $('sub-summary').innerHTML = metrics.map(([label, value, cls]) =>
    `<div class="metric"><div class="label">${label}</div><div class="value ${cls}">${esc(value)}</div></div>`).join('');

  const labels = { active: '生效中', expiring: '即将到期', expired: '已过期', unknown: '未填到期日' };
  const cycles = { monthly: '月付', quarterly: '季付', yearly: '年付', one_time: '一次性', custom: '自定义' };
  $('sub-body').innerHTML = records.length ? records.map((r) => {
    const days = r.days_left === null ? '—'
      : r.days_left < 0 ? `逾期 ${-r.days_left} 天` : `${r.days_left} 天`;
    const quota = (r.quota_remaining === null || r.quota_remaining === undefined)
      ? '—'
      : `${r.quota_remaining} ${r.quota_unit || ''}${r.quota_ratio !== null && r.quota_ratio !== undefined ? ` · ${Math.round(r.quota_ratio * 100)}%` : ''}`;
    return `<tr>
      <td><strong>${esc(r.vendor)}</strong><br><span class="sub-note">${esc(r.account) || '未填账号'}</span></td>
      <td>${esc(r.plan)}<br><span class="sub-note">${esc(r.tier) || '未填档位'}</span></td>
      <td>${esc(r.price)} ${esc(r.currency)}</td>
      <td>${esc(cycles[r.billing_cycle] || r.billing_cycle)}</td>
      <td>${esc(r.started_at) || '—'}</td>
      <td>${esc(r.expires_at) || '—'}</td>
      <td class="days-left ${esc(r.status)}">${days}</td>
      <td><span class="tag ${esc(r.status)}">${labels[r.status] || r.status}</span></td>
      <td>${r.auto_renew ? '是' : '否'}</td>
      <td class="${r.low_quota ? 'quota-low' : 'sub-note'}">${esc(quota)}</td>
      <td class="sub-note">${esc((r.channels || []).join(', ')) || '—'}</td>
      <td>
        <button class="btn small" data-action="renew" data-id="${esc(r.id)}">续费</button>
        <button class="btn small ghost" data-action="edit-sub" data-id="${esc(r.id)}">编辑</button>
        <button class="btn small danger" data-action="delete-sub" data-id="${esc(r.id)}">删除</button>
      </td>
    </tr>`;
  }).join('') : '<tr><td colspan="12" class="sub-note">还没有订阅记录，点击「新增订阅」录入厂家、账号、套餐与到期时间。</td></tr>';
}

function channelPicker(selected) {
  const saved = (Array.isArray(selected) ? selected : []).map((n) => String(n));
  const available = state.config?.channels || [];
  const names = [...new Set([...available.map((c) => String(c.name)), ...saved])];
  if (!names.length) {
    return `<div class="field wide"><label>关联渠道（勾选，可多选）</label>
      <div class="channel-picker empty">还没有可选渠道。请先在「渠道配置」新增渠道并点「保存」，再回来勾选。</div></div>`;
  }
  const items = names.map((name) => {
    const c = available.find((x) => String(x.name) === name);
    const detail = c ? `${c.protocol} · ${c.base_url}` : '渠道不存在（保存时保留原值）';
    return `<label class="pick ${c ? '' : 'missing'}">
      <input type="checkbox" name="channels" value="${esc(name)}" ${saved.includes(name) ? 'checked' : ''}>
      <span><strong>${esc(name)}</strong><small>${esc(detail)}</small></span>
    </label>`;
  }).join('');
  return `<div class="field wide"><label>关联渠道（勾选，可多选）</label>
    <div class="channel-picker">${items}</div>
    <small class="pick-note">勾选后自动带出厂家与套餐名称，仍可手动修改</small></div>`;
}

function pickedChannels(f) {
  return Array.from(f.querySelectorAll('input[name="channels"]:checked'), (n) => n.value);
}

const VENDOR_RULES = [
  [/volces|volcengine|火山/i, '火山引擎'],
  [/deepseek/i, 'DeepSeek'],
  [/dashscope|aliyun|qwen|通义|阿里/i, '阿里云百炼'],
  [/qianfan|baidu|文心|百度/i, '百度千帆'],
  [/bigmodel|zhipuai|智谱|glm/i, '智谱'],
  [/moonshot|kimi/i, 'Moonshot'],
  [/siliconflow|硅基/i, '硅基流动'],
  [/hunyuan|tencent|腾讯/i, '腾讯云'],
  [/anthropic|claude/i, 'Anthropic'],
  [/openai/i, 'OpenAI'],
];

function inferVendor(c) {
  const text = `${c.name || ''} ${c.base_url || ''}`;
  for (const [pattern, vendor] of VENDOR_RULES) {
    if (pattern.test(text)) return vendor;
  }
  try {
    return new URL(c.base_url).hostname.split('.')[0];
  } catch (_) {
    return String(c.name || '');
  }
}

function inferPlan(c) {
  const name = String(c.name || '');
  const vendor = inferVendor(c);
  const rest = name.includes(vendor)
    ? name.replace(vendor, '').replace(/^[-_\s/]+/, '').trim() : name;
  return rest || name;
}

function autofillFromChannels(f) {
  const names = pickedChannels(f);
  if (!names.length) return;
  const c = (state.config?.channels || []).find((x) => x.name === names[0]);
  if (!c) return;
  const fill = (field, value) => {
    const el = f.elements.namedItem(field);
    if (!el || !value) return;
    if (el.value.trim() && el.dataset.autofill !== '1') return;
    el.value = value;
    el.dataset.autofill = '1';
  };
  fill('vendor', c.vendor || inferVendor(c));
  fill('plan', c.plan || inferPlan(c));
  if (c.tier) fill('tier', c.tier);
}

function openSubEditor(index) {
  state.editingSub = index;
  const r = index >= 0 ? state.subs.subscriptions[index] : {
    vendor: '', account: '', plan: '', tier: '', price: '', currency: 'CNY',
    billing_cycle: 'monthly', started_at: todayISO(), expires_at: '',
    auto_renew: false, remind_days: 7, channels: [], note: '',
    quota_limit: '', quota_used: '', quota_unit: '', quota_updated_at: '',
    models_url: '', models: [],
  };
  const modelsText = Array.isArray(r.models) ? r.models.join('\n') : String(r.models || '');
  const currencies = ['CNY', 'USD', 'EUR', 'HKD'].map((c) =>
    `<option value="${c}" ${r.currency === c ? 'selected' : ''}>${c}</option>`).join('');
  const cycles = [['monthly', '月付'], ['quarterly', '季付'], ['yearly', '年付'],
    ['one_time', '一次性'], ['custom', '自定义']].map(([v, l]) =>
    `<option value="${v}" ${r.billing_cycle === v ? 'selected' : ''}>${l}</option>`).join('');
  openModal(index >= 0 ? '编辑订阅' : '新增订阅', `
    <form id="sub-form">
      <div class="form-grid">
        ${channelPicker(r.channels)}
        ${field('厂家 *', 'vendor', r.vendor, { placeholder: '火山引擎' })}
        ${field('账号 / 备注', 'account', r.account, { placeholder: '账号 A' })}
        ${field('套餐名称 *', 'plan', r.plan, { placeholder: 'coding plan' })}
        ${field('档位', 'tier', r.tier, { placeholder: 'Pro / 基础版' })}
        ${field('价格 *', 'price', r.price, { type: 'number' })}
        <div class="field"><label for="f-currency">币种</label><select id="f-currency" name="currency">${currencies}</select></div>
        <div class="field"><label for="f-billing_cycle">计费周期</label><select id="f-billing_cycle" name="billing_cycle">${cycles}</select></div>
        ${field('提前提醒（天）', 'remind_days', r.remind_days ?? 7, { type: 'number' })}
        ${field('开始日期', 'started_at', r.started_at, { type: 'date' })}
        ${field('到期日期', 'expires_at', r.expires_at, { type: 'date' })}
        <div class="field"><label class="check"><input type="checkbox" name="auto_renew" ${r.auto_renew ? 'checked' : ''}> 已开启自动续费</label></div>
        ${field('本周期总额度', 'quota_limit', r.quota_limit ?? '', { type: 'number' })}
        ${field('已用额度', 'quota_used', r.quota_used ?? '', { type: 'number' })}
        <div class="field"><label for="f-quota_unit">额度单位</label><select id="f-quota_unit" name="quota_unit">
          ${[['', '未指定'], ['requests', '次请求'], ['tokens', 'tokens'], ['hours', '小时']]
            .map(([v, l]) => `<option value="${v}" ${(r.quota_unit || '') === v ? 'selected' : ''}>${l}</option>`).join('')}
        </select></div>
        ${field('备注', 'note', r.note, { wide: true, textarea: true })}
        ${field('套餐文档 URL', 'models_url', r.models_url || '', { wide: true, placeholder: 'https://…', hint: '填写官方说明页，自动维护会抓取并更新下方清单' })}
        ${field('套餐内模型', 'models', modelsText, { wide: true, textarea: true, rows: 6, hint: '每行一个，自动维护会定期更新' })}
      </div>
      <div class="test-row">
        <span class="test-hint">测试勾选的渠道是否可用（会各发一次最小请求）</span>
        <button type="button" class="btn ghost" id="sub-test">测试关联渠道</button>
      </div>
      <div id="test-result" class="test-result" hidden></div>
      <div class="modal-foot">
        <button type="button" class="btn ghost" id="sub-cancel">取消</button>
        <button type="submit" class="btn primary">保存</button>
      </div>
    </form>`);
  $('sub-cancel').onclick = closeModal;
  $('sub-test').onclick = testSubscriptionEditor;
  const f = $('sub-form');
  f.querySelectorAll('input[name="channels"]').forEach((box) => {
    box.addEventListener('change', () => autofillFromChannels(f));
  });
  ['vendor', 'plan', 'tier'].forEach((n) => {
    const el = f.elements.namedItem(n);
    if (el) el.addEventListener('input', () => { el.dataset.autofill = ''; });
  });
  autofillFromChannels(f);
  f.onsubmit = (e) => { e.preventDefault(); saveSubEditor(); };
}

function saveSubEditor() {
  const f = $('sub-form');
  const val = (n) => (f.elements.namedItem(n)?.value ?? '').trim();
  try {
    const record = {
      ...(state.editingSub >= 0 ? { id: state.subs.subscriptions[state.editingSub].id } : {}),
      vendor: val('vendor'), account: val('account'), plan: val('plan'), tier: val('tier'),
      price: Number(val('price') || 0), currency: val('currency'),
      billing_cycle: val('billing_cycle'), started_at: val('started_at'),
      expires_at: val('expires_at'), remind_days: Number(val('remind_days') || 7),
      auto_renew: f.elements.namedItem('auto_renew').checked,
      channels: pickedChannels(f),
      quota_limit: val('quota_limit') === '' ? null : Number(val('quota_limit')),
      quota_used: val('quota_used') === '' ? null : Number(val('quota_used')),
      quota_unit: val('quota_unit'),
      quota_updated_at: state.editingSub >= 0
        ? (state.subs.subscriptions[state.editingSub].quota_updated_at || '') : '',
      models_url: val('models_url'),
      models: f.elements.namedItem('models')?.value ?? '',
      note: val('note'),
    };
    if (!record.vendor || !record.plan) throw new Error('厂家和套餐名称不能为空');
    if (state.editingSub >= 0) state.subs.subscriptions[state.editingSub] = record;
    else state.subs.subscriptions.push(record);
    closeModal();
    markSubDirty();
    renderSubscriptions();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

async function saveSubscriptions() {
  try {
    state.subs = await api('/api/subscriptions', {
      method: 'POST', body: { subscriptions: state.subs.subscriptions },
    });
    clearSubDirty();
    renderSubscriptions();
    await loadState();
    toast('订阅已保存');
    return true;
  } catch (err) {
    holdSubDirty();
    toast('订阅未保存：' + err.message + '（改动已暂存，刷新后可恢复）', 'bad');
    return false;
  }
}

function openRenewModal(id) {
  const r = state.subs.subscriptions.find((x) => x.id === id);
  if (!r) return;
  const months = { monthly: 1, quarterly: 3, yearly: 12 }[r.billing_cycle] || 1;
  const suggested = addMonthsISO(r.expires_at, months, !r.expires_at || r.expires_at < todayISO());
  openModal('续费 · ' + r.vendor + ' / ' + r.plan, `
    <form id="renew-form">
      <div class="form-grid">
        ${field('新开始日期', 'started_at', todayISO(), { type: 'date' })}
        ${field('新到期日期', 'expires_at', suggested, { type: 'date' })}
        ${field('新价格', 'price', r.price, { type: 'number' })}
        ${field('档位', 'tier', r.tier)}
      </div>
      <p class="hint">当前到期：${esc(r.expires_at || '未填')} · 原价格 ${esc(r.price)} ${esc(r.currency)} / ${esc(r.billing_cycle)}。确认后会写回订阅文件并清除提醒。</p>
      <div class="modal-foot">
        <button type="button" class="btn ghost" id="renew-cancel">取消</button>
        <button type="submit" class="btn primary">确认续费</button>
      </div>
    </form>`);
  $('renew-cancel').onclick = closeModal;
  $('renew-form').onsubmit = async (e) => {
    e.preventDefault();
    const f = $('renew-form');
    const val = (n) => (f.elements.namedItem(n)?.value ?? '').trim();
    try {
      await api('/api/subscriptions/' + encodeURIComponent(id) + '/renew', {
        method: 'POST',
        body: {
          started_at: val('started_at'), expires_at: val('expires_at'),
          price: Number(val('price') || 0), tier: val('tier'),
        },
      });
      closeModal();
      await loadSubscriptions();
      await loadState();
      toast('已续费，到期日已更新');
    } catch (err) { toast(err.message, 'bad'); }
  };
}

async function deleteSub(id) {
  const record = state.subs.subscriptions.find((x) => x.id === id);
  if (!record || !(await askConfirm('删除订阅', `删除订阅「${record.vendor} / ${record.plan}」？`))) return;
  state.subs.subscriptions = state.subs.subscriptions.filter((x) => x.id !== id);
  markSubDirty();
  renderSubscriptions();
}

function bars(values, color) {
  const max = Math.max(...values, 1);
  const w = 560, h = 100, pad = 6;
  const width = (w - pad * 2) / Math.max(values.length, 1);
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <line x1="0" y1="${h - pad}" x2="${w}" y2="${h - pad}" stroke="rgba(255,255,255,.1)" />
    ${values.map((v, i) => {
      const barHeight = (v / max) * (h - pad * 2);
      return `<rect x="${(pad + i * width + 1).toFixed(1)}" y="${(h - pad - barHeight).toFixed(1)}" width="${Math.max(width - 2, 1).toFixed(1)}" height="${barHeight.toFixed(1)}" rx="2" fill="${color}" opacity="0.75" />`;
    }).join('')}
  </svg>`;
}

function sparkline(values, color) {
  const max = Math.max(...values, 1);
  const w = 560, h = 100, pad = 6;
  const step = values.length > 1 ? (w - pad * 2) / (values.length - 1) : 0;
  const points = values.map((v, i) =>
    `${(pad + i * step).toFixed(1)},${(h - pad - (v / max) * (h - pad * 2)).toFixed(1)}`).join(' ');
  const area = points
    ? `M ${pad},${h - pad} L ${points.replace(/ /g, ' L ')} L ${(pad + (values.length - 1) * step).toFixed(1)},${h - pad} Z`
    : '';
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <line x1="0" y1="${h - pad}" x2="${w}" y2="${h - pad}" stroke="rgba(255,255,255,.1)" />
    ${area ? `<path d="${area}" fill="${color}" opacity="0.12" />` : ''}
    ${points ? `<polyline points="${points}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" />` : ''}
  </svg>`;
}

async function loadUsage(range) {
  if (range) state.usageRange = range;
  state.usage = await api('/api/usage/' + state.usageRange);
  document.querySelectorAll('.range-switch .btn').forEach((b) =>
    b.classList.toggle('active', Number(b.dataset.range) === state.usageRange));
  renderUsage();
}

function renderUsage() {
  const usage = state.usage;
  const el = $('chart-body');
  if (!usage || !usage.days || !usage.days.length) {
    el.innerHTML = '<div class="chart-empty">暂无用量数据，产生请求后会自动按天累计。</div>';
    return;
  }
  const requests = usage.days.map((d) => d.requests);
  const tokens = usage.days.map((d) => d.tokens);
  const totalRequests = requests.reduce((a, b) => a + b, 0);
  const totalTokens = tokens.reduce((a, b) => a + b, 0);
  const distribution = (usage.channels || []).slice(0, 6);
  const largest = Math.max(...distribution.map((c) => c.requests), 1);
  el.innerHTML = `
    <div class="chart-grid">
      <div class="chart-block"><h4>每日请求数（合计 ${totalRequests}）</h4>${bars(requests, '#5b9dff')}</div>
      <div class="chart-block"><h4>每日 Tokens（合计 ${totalTokens.toLocaleString()}）</h4>${sparkline(tokens, '#8a6cff')}</div>
    </div>
    <div class="chart-block" style="margin-top:14px">
      <h4>渠道用量分布（近 ${usage.range} 天）</h4>
      ${distribution.length ? distribution.map((c) => `<div class="dist-row">
        <span>${esc(c.name)}</span>
        <div class="dist-bar"><span style="width:${(c.requests / largest * 100).toFixed(1)}%"></span></div>
        <span class="dist-value">${c.requests} 次</span>
      </div>`).join('') : '<div class="chart-empty">暂无渠道用量</div>'}
    </div>
    <p class="hint">${esc(usage.note || '')}</p>`;
}

async function loadNotifications() {
  state.notify = await api('/api/notifications');
  renderNotifications();
}

function renderNotifications() {
  const data = state.notify;
  if (!data) return;
  const settings = data.settings || {};
  $('set-block').checked = settings.block_expired_channels !== false;
  $('set-notify').checked = !!settings.notify_enabled;
  $('set-webhook').value = settings.webhook_url || '';
  $('set-interval').value = settings.interval_minutes || 60;
  const tag = $('notify-state');
  tag.textContent = data.enabled ? '已启用' : '未启用';
  tag.className = 'tag ' + (data.enabled ? 'active' : 'unknown');
  const history = data.history || [];
  let text = history.length
    ? '最近推送：<br>' + history.slice(0, 5).map((item) =>
      `${esc(item.at)} · ${esc(item.title)} · ${item.ok ? '成功' : '失败 ' + esc(item.error)}`).join('<br>')
    : (data.last_error ? '上次错误：' + esc(data.last_error) : '尚无推送记录');
  if (data.pending && data.pending.length) text += `<br>当前待提醒 ${data.pending.length} 条`;
  $('notify-history').innerHTML = text;
}

function currentSettings() {
  return {
    block_expired_channels: $('set-block').checked,
    notify_enabled: $('set-notify').checked,
    webhook_url: $('set-webhook').value.trim(),
    interval_minutes: Number($('set-interval').value || 60),
  };
}

async function saveSettings(withTest) {
  try {
    await api('/api/notifications', {
      method: 'POST', body: { settings: currentSettings(), test: !!withTest },
    });
    await loadNotifications();
    await loadState();
    await loadSubscriptions();
    toast(withTest ? '设置已保存，测试消息已发送' : '设置已保存');
  } catch (err) { toast(err.message, 'bad'); }
}

async function checkNotifications() {
  try {
    const result = await api('/api/notifications/check', { method: 'POST' });
    await loadNotifications();
    toast(`检查完成，新推送 ${result.sent || 0} 条`);
  } catch (err) { toast(err.message, 'bad'); }
}

function renderBrandList() {
  const p = state.config.models || {};
  const rows = Object.entries(p.branded || {});
  $('brand-list').innerHTML = rows.length ? rows.map(([name, hint]) => `
    <div class="model-row">
      <code>${esc(name)}</code>
      <span class="sub-note">→ ${esc(hint)} 链 · 内部调度</span>
      <button class="btn small ghost" data-copy="${esc(name)}">复制</button>
    </div>`).join('') : '<div class="chart-empty">还没有自有模型名</div>';
}

function renderExactModels() {
  const rows = (state.config.channels || []).filter((c) => c.enabled !== false).flatMap((c) =>
    Object.keys(c.models || {}).filter((k) => k !== 'auto' && k !== '*')
      .map((k) => `<div class="model-row"><code>${esc(k)}</code><span class="sub-note">${esc(c.name)} → ${esc(c.models[k])}</span></div>`));
  $('exact-models').innerHTML = rows.length
    ? rows.join('') : '<div class="chart-empty">还没有显式模型映射，任意名字都会内部调度</div>';
}

function renderModels() {
  const p = state.config.models || {};
  $('set-branded').value = Object.entries(p.branded || {})
    .map(([name, hint]) => name + '=' + hint).join('\n');
  $('set-fallback').checked = p.fallback_unknown !== false;
  $('set-fallback-route').value = p.fallback_route || 'default';
  renderBrandList();
  renderExactModels();
}

function syncModelPolicy() {
  const branded = {};
  $('set-branded').value.split('\n').forEach((line) => {
    const s = line.trim();
    if (!s) return;
    const i = s.indexOf('=');
    const name = (i > 0 ? s.slice(0, i) : s).trim();
    if (name) branded[name] = i > 0 ? s.slice(i + 1).trim() : 'default';
  });
  state.config.models = {
    branded,
    fallback_unknown: $('set-fallback').checked,
    fallback_route: $('set-fallback-route').value.trim() || 'default',
  };
  markDirty();
  renderBrandList();
}

$('set-branded').addEventListener('input', syncModelPolicy);
$('set-fallback-route').addEventListener('input', syncModelPolicy);
$('set-fallback').addEventListener('change', syncModelPolicy);
$('brand-list').addEventListener('click', (e) => {
  const button = e.target.closest('button[data-copy]');
  if (!button) return;
  const value = button.dataset.copy;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(value).then(
      () => toast('已复制：' + value),
      () => toast('复制失败，请手动选中：' + value, 'warn'),
    );
  } else {
    toast('请手动复制：' + value, 'warn');
  }
});

async function loadRequests() {
  state.requests = await api('/api/requests/50');
  renderRequests();
}

function renderRequests() {
  const data = state.requests;
  const tbody = $('requests-body');
  if (!data || !data.entries || !data.entries.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="sub-note">还没有请求记录，发起一次调用后会显示每次转发的结果。</td></tr>';
    $('requests-note').textContent = (data && data.note) || '请求审计未启用';
    return;
  }
  $('requests-note').textContent = `${data.note} · 最近 ${data.entries.length} 条 · ${(data.size / 1024).toFixed(1)} KB`;
  const labels = {
    ok: '成功', upstream_error: '上游错误', network_error: '网络错误',
    no_candidate: '无可用渠道', rejected: '模型被拒绝', local: '本地估算',
  };
  tbody.innerHTML = data.entries.map((e) => {
    const cls = e.status === 'ok' || e.status === 'local' ? 'active'
      : (e.status === 'upstream_error' || e.status === 'network_error' || e.status === 'rejected') ? 'expired' : 'unknown';
    return `<tr>
      <td>${esc((e.ts || '').slice(11, 19))}</td>
      <td><code>${esc(e.model || '—')}</code></td>
      <td>${esc(e.mode || '—')}<br><span class="sub-note">${esc(e.api || '')} ${esc(e.reason || '')}</span></td>
      <td>${esc(e.channel || '—')}<br><span class="sub-note">${esc(e.wire_model || '')}</span></td>
      <td><span class="tag ${cls}">${esc(labels[e.status] || e.status || '—')}</span><br><span class="sub-note">${esc(e.http_status ?? '')}</span></td>
      <td>${esc(e.attempt ?? 0)}</td>
      <td>${esc(e.ms ?? 0)} ms</td>
      <td>${esc(e.tokens ?? 0)}</td>
    </tr>`;
  }).join('');
}

$('btn-refresh-requests').onclick = () => loadRequests().catch((e) => toast(e.message, 'bad'));

async function loadMaintenance() {
  state.maintenance = await api('/api/maintenance');
  renderMaintenance();
}

const MAINT_TRIGGER = { manual: '手动运行', scheduled: '定时运行', first_run: '启动后首次运行' };

function maintItem(tag, cls, html) {
  return `<div class="maint-item"><span class="tag ${cls}">${tag}</span><span>${html}</span></div>`;
}

function renderMaintenance() {
  const data = state.maintenance;
  if (!data) return;
  const llm = data.llm_model
    ? esc(data.llm_model) + (data.llm_available ? '' : '（暂无可用渠道承担）')
    : '未配置';
  $('maintenance-note').textContent = `调度：${data.enabled ? `每 ${data.hours} 小时` : '未启用'}`
    + ` · LLM 分析：${llm} · 由 defaults.maintenance_hours / maintenance_model 控制`;
  const reports = data.reports || [];
  const tierTag = (name) => channelTierBadge(name);
  const body = $('maintenance-body');
  if (!reports.length) {
    body.innerHTML = '<p class="sub-note">还没有运维报告。点「立即运行」会拉取各渠道模型目录，'
      + '把目录确证的变更（下架模型清理、auto 重指）自动应用到配置（先备份），'
      + '新模型与限流建议留给你一键采纳，密钥失效只报告。</p>';
    return;
  }
  body.innerHTML = reports.map((r) => {
    const when = r.finished_at ? new Date(r.finished_at * 1000).toLocaleString() : '';
    const head = `<div class="maint-head"><strong>${esc(when)}</strong>`
      + `<span class="sub-note">${esc(MAINT_TRIGGER[r.trigger] || r.trigger || '')}`
      + ` · ${esc(r.channels_total ?? 0)} 个渠道</span></div>`;
    const rows = [];
    (r.applied || []).forEach((a) => {
      const detail = a.action === 'repoint_auto'
        ? `auto：<code>${esc(a.from)}</code> → <code>${esc(a.to)}</code>`
        : (a.models || []).map((m) => `<code>${esc(m)}</code>`).join(' ');
      rows.push(maintItem('自动应用', 'active',
        `<strong>${esc(a.channel)}</strong> ${tierTag(a.channel)} ${esc(a.reason || '')}：${detail}`));
    });
    (r.suggestions || []).forEach((s) => {
      const btn = s.kind === 'add_model' && (s.models || []).length
        ? ` <button class="btn small" data-apply="${esc(JSON.stringify(s.models))}"`
          + ` data-channel="${esc(s.channel)}">一键采纳</button>` : '';
      rows.push(maintItem(s.source === 'llm' ? 'AI 建议' : '建议', 'unknown',
        `${s.channel ? `<strong>${esc(s.channel)}</strong>${tierTag(s.channel)} ` : ''}${esc(s.message)}${btn}`));
    });
    (r.alerts || []).forEach((a) => {
      rows.push(maintItem('注意', 'expired',
        `${a.channel ? `<strong>${esc(a.channel)}</strong>${tierTag(a.channel)} ` : ''}${esc(a.message)}`));
    });
    (r.llm_notes || []).forEach((n) => {
      rows.push(maintItem('AI', 'active', `<span class="sub-note">${esc(n)}</span>`));
    });
    if (r.llm_error) {
      rows.push(maintItem('AI', 'expired', `LLM 分析失败：${esc(r.llm_error)}`));
    }
    return `<div class="maint-report">${head}`
      + (rows.length ? rows.join('') : '<p class="sub-note">本次运行未发现需要处理的变更。</p>')
      + '</div>';
  }).join('');
}

$('btn-refresh-maintenance').onclick = () => loadMaintenance().catch((e) => toast(e.message, 'bad'));

$('btn-run-maintenance').onclick = async (e) => {
  const b = e.target;
  const label = b.textContent;
  b.disabled = true;
  b.textContent = '运行中…';
  try {
    await api('/api/maintenance/run', { method: 'POST' });
    await loadMaintenance();
    toast('运维分析完成');
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    b.disabled = false;
    b.textContent = label;
  }
};

$('maintenance-body').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-apply]');
  if (!b) return;
  b.disabled = true;
  try {
    const models = JSON.parse(b.dataset.apply);
    const r = await api('/api/maintenance/apply',
      { method: 'POST', body: { channel: b.dataset.channel, models } });
    toast(`已为渠道 ${r.channel} 添加 ${r.models.length} 个模型映射`);
    await loadConfig();
    await loadState();
    await loadMaintenance();
  } catch (err) {
    toast(err.message, 'bad');
    b.disabled = false;
  }
});

$('btn-admin-key').onclick = async () => {
  const answer = await askText({
    title: '管理密钥',
    message: 'listen.admin_key，留空表示清除。密钥只保存在本机浏览器。',
    value: state.adminKey, password: true, remember: true,
  });
  if (!answer) return;
  state.adminKey = answer.value;
  storeKey('admin', state.adminKey, answer.remember);
  loadState().then(() => toast('管理密钥已更新')).catch((e) => toast(e.message, 'bad'));
};

async function loadReport(month) {
  state.report = await api(month ? `/api/report/${encodeURIComponent(month)}` : '/api/report');
  renderReport();
}

function renderReport() {
  const r = state.report;
  if (!r) return;
  const cost = Object.entries(r.currency_totals || {})
    .map(([code, amount]) => `${amount} ${code}`).join(' / ') || '—';
  $('report-note').textContent = `${r.month} · ${r.active_days} 天有用量 · ${r.note}`;
  $('report-metrics').innerHTML = [
    ['月等效成本', cost, ''],
    ['请求 / 成功 / 失败', `${r.total.requests} / ${r.total.success} / ${r.total.failed}`, ''],
    ['Tokens', r.total.tokens.toLocaleString(), ''],
    ['渠道数', r.channels.length, ''],
  ].map(([label, value, cls]) =>
    `<div class="metric"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div></div>`).join('');
  $('report-body').innerHTML = r.subscriptions.length
    ? r.subscriptions.map((s) => `<tr>
        <td><strong>${esc(s.vendor)}</strong><br><span class="sub-note">${esc(s.account) || '—'}</span></td>
        <td>${esc(s.plan)}<br><span class="sub-note">${esc(s.tier) || '未填档位'}</span></td>
        <td>${esc(s.monthly_equivalent?.amount ?? '—')} ${esc(s.currency)}</td>
        <td>${s.usage.requests} / ${s.usage.failed}</td>
        <td>${s.usage.tokens.toLocaleString()}</td>
        <td>${s.cost_per_1k_requests === null || s.cost_per_1k_requests === undefined
              ? '—' : s.cost_per_1k_requests + ' ' + esc(s.currency)}</td>
        <td class="${s.quota_ratio !== null && s.quota_ratio !== undefined && s.quota_ratio <= 0.15 ? 'quota-low' : 'sub-note'}">${s.quota_remaining === null || s.quota_remaining === undefined ? '—' : esc(s.quota_remaining + ' ' + (s.quota_unit || ''))}</td>
      </tr>`).join('')
    : '<tr><td colspan="7" class="sub-note">还没有订阅记录</td></tr>';
  $('report-channels').textContent = '渠道用量：' + (r.channels.map((c) =>
    `${c.name} ${c.requests} 次 / ${c.tokens} tokens${c.linked ? '' : '（未关联订阅）'}`).join(' · ') || '暂无');
}

$('btn-load-report').onclick = () => loadReport($('report-month').value).catch((e) => toast(e.message, 'bad'));
$('report-month').onchange = () => loadReport($('report-month').value).catch((e) => toast(e.message, 'bad'));

function renderAll() {
  renderChannels();
  renderRoutes();
  renderSummary();
  renderStatus();
  renderSubscriptions();
  renderBanner();
  renderUsage();
  renderNotifications();
  renderModels();
  renderRequests();
  renderReport();
}

let modalReturnFocus = null;

function openModal(title, html) {
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = html;
  $('modal').hidden = false;
  modalReturnFocus = document.activeElement;
  const target = $('modal').querySelector('.modal input, .modal select, .modal textarea')
    || $('modal').querySelector('.modal button');
  if (target) target.focus();
}

function closeModal() {
  $('modal').hidden = true;
  if (modalReturnFocus && modalReturnFocus.focus) modalReturnFocus.focus();
  modalReturnFocus = null;
}

const field = (label, name, value, opts = {}) => `
  <div class="field ${opts.wide ? 'wide' : ''}">
    <label for="f-${name}">${label}</label>
    ${opts.textarea
      ? `<textarea id="f-${name}" name="${name}" rows="${opts.rows || 4}">${esc(value)}</textarea>`
      : `<input id="f-${name}" name="${name}" type="${opts.type || 'text'}" value="${esc(value)}" ${opts.type === 'number' ? 'min="0"' : ''} placeholder="${esc(opts.placeholder || '')}">`}
    ${opts.hint ? `<small class="hint">${esc(opts.hint)}</small>` : ''}
  </div>`;

function draftChannelFromEditor() {
  const f = $('editor-form');
  const val = (n) => (f.elements.namedItem(n)?.value ?? '').trim();
  const rate = {};
  ['rpm', 'rpd', 'tpm', 'tpd'].forEach((k) => { if (val(k)) rate[k] = Number(val(k)); });
  return {
    name: val('name'), protocol: val('protocol'), base_url: val('base_url'),
    api_key: val('api_key') || '***',
    priority: Number(val('priority') || 100),
    cooldown_minutes: Number(val('cooldown_minutes') || 300),
    rate, models: parseModels(val('models')),
    enabled: f.elements.namedItem('enabled').checked,
    ...(val('upstream_path') ? { upstream_path: val('upstream_path') } : {}),
  };
}

function showTestResult(results, note) {
  const box = $('test-result');
  if (!box) return;
  box.hidden = false;
  if (!results) {
    box.className = 'test-result';
    box.textContent = note || '';
    return;
  }
  box.innerHTML = results.map((r) => {
    const ok = !!r.ok;
    const head = [r.name || '渠道', r.http_status ? 'HTTP ' + r.http_status : '',
      r.latency_ms != null ? r.latency_ms + 'ms' : '', r.model ? '模型 ' + r.model : '']
      .filter(Boolean).join(' · ');
    const detail = ok
      ? (r.reply ? '回复：' + r.reply : '上游返回成功')
      : ((r.error || '未知错误') + (r.hint ? ' — ' + r.hint : ''));
    const meta = [r.url && 'URL ' + r.url,
      r.auth && (r.auth === 'none' ? '未携带密钥' : '已携带密钥（' + r.auth + '）')]
      .filter(Boolean).join(' · ');
    const keyNote = r.key && r.key.note
      ? `<div class="test-key">密钥解析：${esc(r.key.note)}</div>` : '';
    const debug = r.debug
      ? `<details class="test-debug"><summary>查看请求 / 响应明细</summary><pre>${esc(JSON.stringify({ key: r.key, ...r.debug }, null, 2))}</pre></details>`
      : '';
    return `<div class="test-line"><div class="test-head ${ok ? 'ok' : 'bad'}">${ok ? '✓' : '✗'} ${esc(head)}</div><div class="test-detail">${esc(detail)}</div>${meta ? `<div class="test-meta">${esc(meta)}</div>` : ''}${keyNote}${debug}</div>`;
  }).join('') + (note ? `<div class="test-note">${esc(note)}</div>` : '');
  box.className = 'test-result';
}

async function runProbe(button, channels, model) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = '测试中…';
  showTestResult(null, '正在发起最小请求，请稍候…');
  try {
    const body = { channels };
    if (model) body.model = model;
    const data = await api('/api/test/channels', { method: 'POST', body });
    showTestResult(data.results || [], data.note);
    toast(data.ok ? '测试通过' : '测试未通过，查看详情', data.ok ? 'ok' : 'bad');
  } catch (err) {
    const hint = /未授权|401/.test(err.message)
      ? '请点右上角「管理密钥」填入 listen.admin_key（默认值见 compose.yaml 或 .env）后重试'
      : '';
    showTestResult([{ ok: false, name: '请求失败', error: err.message, hint }], null);
    toast(err.message, 'bad');
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function testChannelEditor() {
  const model = ($('editor-test-model')?.value || '').trim();
  return runProbe($('editor-test'), [draftChannelFromEditor()], model);
}

async function fetchChannelModels() {
  const button = $('editor-fetch-models');
  const list = $('catalog-list');
  if (!button || !list) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = '拉取中…';
  list.hidden = true;
  list.innerHTML = '';
  try {
    const result = await api('/api/probe/models',
      { method: 'POST', body: { channel: draftChannelFromEditor() } });
    if (!result.ok) {
      throw new Error((result.error || '拉取失败') + (result.hint ? ' — ' + result.hint : ''));
    }
    renderCatalogList(result.models || []);
    toast(`拉取成功：${result.count} 个模型`);
  } catch (err) {
    list.hidden = false;
    list.innerHTML = `<div class="hint">${esc(err.message)}</div>`;
    toast(err.message, 'bad');
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function renderCatalogList(models) {
  const list = $('catalog-list');
  if (!list) return;
  if (!models.length) {
    list.hidden = false;
    list.innerHTML = '<div class="hint">上游返回了空清单，请确认该密钥有模型权限</div>';
    return;
  }
  // No manual one-by-one picking: pre-check what is already mapped, take
  // everything on small catalogs (plan bridges), and on big aggregators
  // default to the free ids. The filter and bulk buttons cover the rest.
  // When a linked subscription lists plan models, those win: pre-check the
  // plan intersection instead of the ≤30 / :free heuristic, and tag hits.
  const current = parseModels($('editor-form').elements.namedItem('models').value);
  const mapped = new Set(Object.keys(current).filter((k) => k !== 'auto' && k !== '*'));
  const FREE_ID = /(:free|-free)$/i;
  const channelName = ($('editor-form').elements.namedItem('name')?.value || '').trim();
  const planModels = new Set();
  (state.subs?.subscriptions || []).forEach((rec) => {
    if ((rec.channels || []).includes(channelName)) {
      (Array.isArray(rec.models) ? rec.models : []).forEach((m) => planModels.add(String(m)));
    }
  });
  // Registry models for the channel's vendor + billing mode count as plan
  // models too: same 套餐 tag, same pre-check behaviour.
  const regMode = registryMode(
    $('editor-form').elements.namedItem('provider')?.value || '',
    $('editor-form').elements.namedItem('mode')?.value || '');
  (regMode?.models || []).forEach((m) => planModels.add(String(m)));
  const inPlan = planModels.size > 0;
  const precheck = (m) => inPlan
    ? (mapped.has(m) || planModels.has(m))
    : (mapped.has(m) || models.length <= 30 || FREE_ID.test(m));
  list.hidden = false;
  list.innerHTML = `
    <div class="test-row" style="margin-top:8px">
      <input id="catalog-filter" type="text" placeholder="输入关键字筛选，如 free / qwen" autocomplete="off">
      <button type="button" class="btn small" data-cat="all">全选</button>
      <button type="button" class="btn small" data-cat="none">全不选</button>
      <button type="button" class="btn small" data-cat="free">只选免费</button>
      ${inPlan ? '<button type="button" class="btn small" data-cat="plan">只选套餐内</button>' : ''}
    </div>
    <div id="catalog-items">${models.map((m) =>
      `<label class="pick"><input type="checkbox" value="${esc(m)}" ${precheck(m) ? 'checked' : ''}><span><strong>${esc(m)}</strong>${planModels.has(m) ? ' <span class="tag plan">套餐</span>' : ''}</span></label>`
    ).join('')}</div>
    <div class="test-row"><button type="button" class="btn small" id="catalog-apply">把勾选写入模型映射</button><span class="hint" id="catalog-count"></span></div>`;
  const boxes = () => Array.from(list.querySelectorAll('#catalog-items input[type="checkbox"]'));
  const visible = () => boxes().filter((b) => b.parentElement.style.display !== 'none');
  const count = () => {
    $('catalog-count').textContent =
      `已勾选 ${boxes().filter((b) => b.checked).length} / ${models.length} 个`;
  };
  $('catalog-filter').addEventListener('input', () => {
    const kw = $('catalog-filter').value.trim().toLowerCase();
    boxes().forEach((b) => {
      b.parentElement.style.display = (!kw || b.value.toLowerCase().includes(kw)) ? '' : 'none';
    });
  });
  list.querySelectorAll('[data-cat]').forEach((b) => {
    b.onclick = () => {
      if (b.dataset.cat === 'none') boxes().forEach((x) => { x.checked = false; });
      else if (b.dataset.cat === 'free') visible().forEach((x) => { x.checked = FREE_ID.test(x.value); });
      else if (b.dataset.cat === 'plan') visible().forEach((x) => { x.checked = planModels.has(x.value); });
      else visible().forEach((x) => { x.checked = true; });
      count();
    };
  });
  count();
  $('catalog-apply').onclick = applyCatalogSelection;
}

function applyCatalogSelection() {
  const f = $('editor-form');
  const modelsField = f.elements.namedItem('models');
  const current = parseModels(modelsField.value);
  const keep = Object.entries(current).filter(([k, v]) =>
    k === 'auto' || k === '*' || k !== v);
  const picked = Array.from(
    $('catalog-list').querySelectorAll('input[type="checkbox"]:checked'),
    (node) => node.value);
  const merged = Object.fromEntries(keep);
  picked.forEach((m) => { merged[m] = m; });
  modelsField.value = serializeModels(merged);
  renderModelTierRows();
  toast(`已写入 ${picked.length} 个模型，记得保存渠道`);
}

function testSubscriptionEditor() {
  const f = $('sub-form');
  const names = pickedChannels(f);
  if (!names.length) return toast('请先勾选关联渠道', 'bad');
  const drafts = names.map((name) =>
    (state.config?.channels || []).find((c) => c.name === name) || name);
  return runProbe($('sub-test'), drafts, '');
}

// Per-model tier marks, kept in sync with the models textarea. auto/* are
// scheduling directives rather than real models and never get a mark.
function renderModelTierRows() {
  const box = $('model-tier-rows');
  if (!box) return;
  const prev = Object.fromEntries(
    Array.from(box.querySelectorAll('select')).map((s) => [s.dataset.model, s.value]));
  const saved = state.editing >= 0
    ? (state.config.channels[state.editing].tiers || {}) : {};
  const names = [];
  const seen = new Set();
  ($('f-models').value || '').split('\n').forEach((line) => {
    const s = line.trim();
    const i = s.indexOf('=');
    if (i < 1) return;
    const k = s.slice(0, i).trim();
    if (!k || k === 'auto' || k === '*' || seen.has(k)) return;
    seen.add(k);
    names.push(k);
  });
  if (!names.length) {
    box.innerHTML = '<small class="hint">模型映射中还没有可标注的模型。</small>';
    return;
  }
  box.innerHTML = names.map((m) => {
    const cur = prev[m] ?? saved[m] ?? '';
    return `<div class="tier-row"><code>${esc(m)}</code>
      <select data-model="${esc(m)}">
        <option value="" ${!cur ? 'selected' : ''}>跟随默认</option>
        <option value="free" ${cur === 'free' ? 'selected' : ''}>免费</option>
        <option value="paid" ${cur === 'paid' ? 'selected' : ''}>收费</option>
      </select></div>`;
  }).join('');
}

function providerOptions(selected) {
  return ['<option value="">自定义 / 不选</option>']
    .concat((state.providers || []).map((p) =>
      `<option value="${esc(p.id)}" ${p.id === selected ? 'selected' : ''}>${esc(p.name || p.id)}</option>`))
    .join('');
}

function providerModeOptions(pid, selected) {
  const p = (state.providers || []).find((x) => x.id === pid);
  return ['<option value="">自定义</option>']
    .concat(Object.entries(p?.modes || {}).map(([key, mode]) =>
      `<option value="${esc(key)}" ${key === selected ? 'selected' : ''}>${esc(mode.label || key)}</option>`))
    .join('');
}

// Registry lookup for a provider id + mode key; null when custom/unknown.
function registryMode(pid, modeKey) {
  const p = (state.providers || []).find((x) => x.id === pid);
  if (!p || !modeKey) return null;
  return p.modes?.[modeKey] || null;
}

// 「厂家 · 模式label」 text for the channel card; '' when unlinked.
function providerTag(c) {
  if (!c.provider) return '';
  const p = (state.providers || []).find((x) => x.id === c.provider);
  const name = p ? (p.name || p.id) : c.provider;
  const label = p && c.mode ? (p.modes?.[c.mode]?.label || '') : '';
  return label ? `${name} · ${label}` : name;
}

// Selecting a vendor + mode auto-fills base_url / protocol / tier and keeps
// the pair in the draft; switching back to 自定义 clears the pair but keeps
// whatever base_url the user has already entered.
function applyProviderPreset() {
  const f = $('editor-form');
  if (!f) return;
  const pid = f.elements.namedItem('provider').value;
  const modeKey = f.elements.namedItem('mode').value;
  const mode = registryMode(pid, modeKey);
  if (!mode) return;
  if (mode.protocol === 'openai' || mode.protocol === 'anthropic') {
    f.elements.namedItem('protocol').value = mode.protocol;
  }
  if (mode.base_url) f.elements.namedItem('base_url').value = mode.base_url;
  if (mode.tier) {
    const tierSel = f.elements.namedItem('tier');
    if (tierSel) tierSel.value = mode.tier;
  }
}

function openChannelEditor(index) {
  state.editing = index;
  const c = index >= 0 ? state.config.channels[index] : {
    name: '', protocol: 'openai', base_url: '', api_key: '', models: { auto: '' },
    priority: 100, cooldown_minutes: 300, enabled: true, rate: {}, tier: '', tiers: {},
  };
  const rate = c.rate || {};
  openModal(index >= 0 ? '编辑渠道' : '新增渠道', `
    <form id="editor-form">
      <div class="form-grid">
        ${field('渠道名称', 'name', c.name, { placeholder: 'my-coding-plan' })}
        <div class="field"><label for="f-provider">厂家</label>
          <select id="f-provider" name="provider">${providerOptions(c.provider || '')}</select></div>
        <div class="field"><label for="f-mode">计费模式</label>
          <select id="f-mode" name="mode">${providerModeOptions(c.provider || '', c.mode || '')}</select></div>
        <div class="field"><label for="f-protocol">协议</label>
          <select id="f-protocol" name="protocol">
            <option value="openai" ${c.protocol === 'openai' ? 'selected' : ''}>openai</option>
            <option value="anthropic" ${c.protocol === 'anthropic' ? 'selected' : ''}>anthropic</option>
          </select></div>
        <div class="field"><label for="f-tier">默认计费（未单独标注的模型）</label>
          <select id="f-tier" name="tier">
            <option value="" ${!c.tier ? 'selected' : ''}>自动推断（按订阅）</option>
            <option value="free" ${c.tier === 'free' ? 'selected' : ''}>免费</option>
            <option value="paid" ${c.tier === 'paid' ? 'selected' : ''}>收费</option>
          </select></div>
        ${field('上游 base_url', 'base_url', c.base_url, { wide: true, placeholder: 'http://127.0.0.1:8080' })}
        ${field('上游路径（留空自动选择）', 'upstream_path', c.upstream_path || '', { wide: true, placeholder: 'base_url 已含 /v3 时留空即可；需要覆盖时填 /chat/completions' })}
        ${field('密钥（直接粘贴明文 API Key）', 'api_key', c.api_key || '', { wide: true, placeholder: '直接粘贴 API Key；需要引用环境变量时才写 env:ARK_API_KEY' })}
        ${field('优先级（小者靠前）', 'priority', c.priority ?? 100, { type: 'number' })}
        ${field('额度冷却（分钟）', 'cooldown_minutes', c.cooldown_minutes ?? 300, { type: 'number' })}
        ${field('RPM', 'rpm', rate.rpm ?? '', { type: 'number' })}
        ${field('RPD', 'rpd', rate.rpd ?? '', { type: 'number' })}
        ${field('TPM', 'tpm', rate.tpm ?? '', { type: 'number' })}
        ${field('TPD', 'tpd', rate.tpd ?? '', { type: 'number' })}
        ${field('模型映射（每行 exposed=wire，*=* 表示透传）', 'models', serializeModels(c.models), { wide: true, textarea: true })}
        <div class="field wide"><label>模型计费标注（免费 / 收费，可随时手动切换）</label>
          <div id="model-tier-rows" class="channel-picker"></div>
          <small class="hint">同一渠道可以既有免费额度模型也有收费模型（如百炼免费额度转收费）；未标注的模型跟随上面的默认计费，auto 与 * 不需要标注。</small>
        </div>
        <div class="field wide catalog-row">
          <button type="button" class="btn ghost" id="editor-fetch-models">从上游拉取模型清单</button>
          <small class="hint">按密钥请求上游 /models；勾选要暴露的模型后写入模型映射</small>
        </div>
        <div id="catalog-list" class="channel-picker" hidden></div>
        <div class="field wide"><label class="check"><input type="checkbox" name="enabled" ${c.enabled === false ? '' : 'checked'}> 启用该渠道</label></div>
      </div>
      <div class="test-row">
        <input id="editor-test-model" placeholder="测试模型（留空自动选择）">
        <button type="button" class="btn ghost" id="editor-test">测试连接</button>
      </div>
      <div id="test-result" class="test-result" hidden></div>
      <div class="modal-foot">
        <button type="button" class="btn ghost" id="editor-cancel">取消</button>
        <button type="submit" class="btn primary">保存</button>
      </div>
    </form>`);
  $('editor-cancel').onclick = closeModal;
  $('editor-test').onclick = testChannelEditor;
  $('editor-fetch-models').onclick = fetchChannelModels;
  $('editor-form').onsubmit = (e) => { e.preventDefault(); saveChannelEditor(); };
  $('f-models').addEventListener('input', renderModelTierRows);
  const fProvider = $('f-provider');
  const fMode = $('f-mode');
  fProvider.onchange = () => {
    fMode.innerHTML = providerModeOptions(fProvider.value, '');
    applyProviderPreset();
  };
  fMode.onchange = applyProviderPreset;
  renderModelTierRows();
}

function saveChannelEditor() {
  const f = $('editor-form');
  const val = (n) => (f.elements.namedItem(n)?.value ?? '').trim();
  try {
    const name = val('name');
    const models = parseModels(val('models'));
    if (!name) throw new Error('渠道名称不能为空');
    if (!Object.keys(models).length) throw new Error('至少配置一条模型映射');
    const rate = {};
    ['rpm', 'rpd', 'tpm', 'tpd'].forEach((k) => { if (val(k)) rate[k] = Number(val(k)); });
    const tiers = {};
    $('model-tier-rows').querySelectorAll('select').forEach((s) => {
      if (s.value) tiers[s.dataset.model] = s.value;
    });
    const provider = val('provider');
    const channel = {
      name,
      protocol: val('protocol'),
      base_url: val('base_url'),
      api_key: val('api_key') || '***',
      priority: Number(val('priority') || 100),
      cooldown_minutes: Number(val('cooldown_minutes') || 300),
      rate,
      models,
      enabled: f.elements.namedItem('enabled').checked,
      tier: val('tier'),
      tiers,
      provider,
      mode: provider ? val('mode') : '',
      ...(val('upstream_path') ? { upstream_path: val('upstream_path') } : {}),
    };
    if (state.editing >= 0) {
      const oldName = state.config.channels[state.editing].name;
      state.config.channels[state.editing] = channel;
      if (oldName !== name) {
        state.config.routes.forEach((r) => {
          r.channels = (r.channels || []).map((n) => n === oldName ? name : n);
        });
      }
    } else {
      state.config.channels.push(channel);
    }
    closeModal();
    markDirty();
    renderAll();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

function openRouteModal() {
  openModal('新增路由链', `
    <form id="route-form">
      <div class="form-grid">
        ${field('链名称（请求头 X-Route 使用）', 'rname', '', { placeholder: 'cheap' })}
        <div class="field"><label for="r-protocol">协议</label>
          <select id="r-protocol" name="rprotocol">
            <option value="openai">openai</option><option value="anthropic">anthropic</option>
          </select></div>
        <div class="field wide"><label for="r-channel">首个渠道</label>
          <select id="r-channel" name="rchannel">
            ${state.config.channels.map((c) => `<option value="${esc(c.name)}" data-proto="${esc(c.protocol)}">${esc(c.name)} (${esc(c.protocol)})</option>`).join('')}
          </select></div>
      </div>
      <div class="modal-foot">
        <button type="button" class="btn ghost" id="route-cancel">取消</button>
        <button type="submit" class="btn primary">保存</button>
      </div>
    </form>`);
  $('route-cancel').onclick = closeModal;
  $('route-form').onsubmit = (e) => {
    e.preventDefault();
    const f = $('route-form');
    const name = f.elements.namedItem('rname').value.trim();
    const protocol = f.elements.namedItem('rprotocol').value;
    const channel = f.elements.namedItem('rchannel').value;
    const picked = state.config.channels.find((c) => c.name === channel);
    if (!name) return toast('链名称不能为空', 'bad');
    if (!picked || picked.protocol !== protocol) return toast('渠道协议与链协议不一致', 'bad');
    if (state.config.routes.some((r) => r.name === name && r.protocol === protocol)) {
      return toast('同名同协议的链已存在', 'bad');
    }
    state.config.routes.push({ name, protocol, channels: [channel] });
    closeModal();
    markDirty();
    renderAll();
  };
}

function removeFromRoutes(name) {
  state.config.routes.forEach((r) => { r.channels = (r.channels || []).filter((n) => n !== name); });
}

async function resetCooldown(name) {
  try {
    await api('/api/channels/' + encodeURIComponent(name) + '/reset', { method: 'POST' });
    await loadState();
    toast('已解除冷却：' + name);
  } catch (err) { toast(err.message, 'bad'); }
}

async function saveConfig() {
  try {
    const result = await api('/api/config', { method: 'POST', body: state.config });
    clearDirty();
    await loadConfig();
    await loadState();
    toast(`已保存并热加载：${result.channels} 个渠道，${result.routes.length} 条链`);
    return true;
  } catch (err) {
    holdDirty();
    toast('配置未保存：' + err.message + '（改动已暂存，刷新后可恢复）', 'bad');
    return false;
  }
}

function bindEvents() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.onclick = () => switchTab(tab.dataset.tab);
  });

  $('btn-save').onclick = saveConfig;
  $('btn-add-channel').onclick = () => openChannelEditor(-1);
  $('btn-add-route').onclick = openRouteModal;
  $('btn-refresh').onclick = () => loadState().catch((e) => toast(e.message, 'bad'));
  $('btn-reload').onclick = async () => {
    if (state.dirty && !(await askConfirm('放弃修改', '有未保存的修改，确认放弃并从磁盘重载？'))) return;
    try {
      await api('/api/reload', { method: 'POST' });
      await loadConfig(); await loadState(); clearDirty();
      toast('已从磁盘重载');
    } catch (err) { toast(err.message, 'bad'); }
  };
  $('btn-key').onclick = async () => {
    const answer = await askText({
      title: '网关密钥',
      message: 'listen.api_key，留空表示清除。密钥只保存在本机浏览器。',
      value: state.key, password: true, remember: true,
    });
    if (!answer) return;
    state.key = answer.value;
    storeKey('gateway', state.key, answer.remember);
    loadState().then(() => toast('密钥已更新')).catch((e) => toast(e.message, 'bad'));
  };
  $('btn-export').onclick = () => {
    const blob = new Blob([JSON.stringify(state.config, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'router.json'; a.click();
    URL.revokeObjectURL(url);
    toast('已导出（不含密钥明文）', 'warn');
  };
  $('modal-close').onclick = closeModal;
  // Truly modal: clicking the backdrop or the page behind does nothing and
  // Tab cycles inside the dialog, so a stray click cannot discard a draft.
  // Only the close button, a save/cancel action or Escape dismisses it.
  const focusable = (box) => Array.from(box.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )).filter((el) => el.offsetParent !== null);
  document.addEventListener('keydown', (e) => {
    if ($('modal').hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); closeModal(); return; }
    if (e.key !== 'Tab') return;
    const box = $('modal').querySelector('.modal');
    const list = focusable(box);
    if (!list.length) return;
    if (!box.contains(document.activeElement)) { e.preventDefault(); list[0].focus(); return; }
    const firstEl = list[0], lastEl = list[list.length - 1];
    if (e.shiftKey && document.activeElement === firstEl) { e.preventDefault(); lastEl.focus(); }
    else if (!e.shiftKey && document.activeElement === lastEl) { e.preventDefault(); firstEl.focus(); }
  });

  $('channel-list').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-action]');
    if (!b) return;
    const i = Number(b.dataset.index);
    const action = b.dataset.action;
    if (action === 'toggle-models') {
      const more = b.parentElement.querySelector('.models-more');
      if (more) {
        const open = !more.hidden;
        more.hidden = open;
        b.textContent = open ? `… 共 ${b.dataset.total} 个` : '收起';
      }
      return;
    }
    if (action === 'edit') openChannelEditor(i);
    if (action === 'catalog') {
      const name = b.dataset.name;
      const label = b.textContent;
      b.disabled = true; b.textContent = '拉取中…';
      try {
        const r = await api('/api/probe/models',
          { method: 'POST', body: { channel: { name } } });
        if (!r.ok) throw new Error((r.error || '拉取失败') + (r.hint ? ' — ' + r.hint : ''));
        const ch = r.changes || {};
        const diff = (ch.added?.length || ch.removed?.length)
          ? `（新增 ${ch.added?.length || 0} / 移除 ${ch.removed?.length || 0}）` : '';
        toast(`已拉取 ${r.count} 个模型${diff}`);
        await loadState();
      } catch (err) {
        toast(err.message, 'bad');
        b.disabled = false; b.textContent = label;
      }
    }
    if (action === 'toggle') {
      state.config.channels[i].enabled = state.config.channels[i].enabled === false;
      markDirty(); renderChannels();
    }
    if (action === 'delete') {
      const name = state.config.channels[i].name;
      if (!(await askConfirm('删除渠道', `删除渠道「${name}」？它也会从所有路由链中移除。`))) return;
      state.config.channels.splice(i, 1);
      removeFromRoutes(name);
      markDirty(); renderAll();
    }
    if (action === 'reset') resetCooldown(b.dataset.name);
  });

  $('route-list').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-action]');
    if (!b) return;
    const ri = Number(b.dataset.index);
    const pos = Number(b.dataset.pos);
    const route = state.config.routes[ri];
    const action = b.dataset.action;
    if (action === 'delete-route') {
      if (!(await askConfirm('删除链', `删除链「${route.name}」？`))) return;
      state.config.routes.splice(ri, 1);
    } else if (action === 'move-up' && pos > 0) {
      [route.channels[pos - 1], route.channels[pos]] = [route.channels[pos], route.channels[pos - 1]];
    } else if (action === 'move-down' && pos < route.channels.length - 1) {
      [route.channels[pos + 1], route.channels[pos]] = [route.channels[pos], route.channels[pos + 1]];
    } else if (action === 'remove-node') {
      route.channels.splice(pos, 1);
    } else if (action === 'add-node') {
      const value = $('add-select-' + ri).value;
      const picked = state.config.channels.find((c) => c.name === value);
      if (!picked || picked.protocol !== route.protocol) return toast('渠道协议与链协议不一致', 'bad');
      if (!value) return toast('请先添加同协议的渠道', 'bad');
      route.channels.push(value);
    }
    markDirty();
    renderRoutes();
  });

  $('status-body').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-action="reset"]');
    if (b) resetCooldown(b.dataset.name);
  });

  document.querySelectorAll('.range-switch .btn').forEach((button) => {
    button.onclick = () => loadUsage(Number(button.dataset.range)).catch((e) => toast(e.message, 'bad'));
  });
  $('btn-save-settings').onclick = () => saveSettings(false);
  $('btn-test-notify').onclick = () => saveSettings(true);
  $('btn-check-notify').onclick = checkNotifications;

  $('btn-save-subs').onclick = saveSubscriptions;
  $('btn-add-sub').onclick = () => openSubEditor(-1);
  $('btn-export-subs').onclick = () => {
    const blob = new Blob([JSON.stringify({ subscriptions: state.subs.subscriptions }, null, 2)],
      { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'subscriptions.json'; a.click();
    URL.revokeObjectURL(url);
    toast('订阅已导出');
  };
  $('sub-body').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-action]');
    if (!b) return;
    const id = b.dataset.id;
    if (b.dataset.action === 'renew') openRenewModal(id);
    if (b.dataset.action === 'delete-sub') deleteSub(id);
    if (b.dataset.action === 'edit-sub') {
      const index = state.subs.subscriptions.findIndex((x) => x.id === id);
      if (index >= 0) openSubEditor(index);
    }
  });
}

async function restoreDrafts() {
  const config = readDraft(DRAFT_CONFIG);
  if (config && await askConfirm('恢复草稿', '检测到上次未保存的渠道配置，是否恢复并重新保存？')) {
    state.config = config;
    normalizeConfig();
    holdDirty();
    renderAll();
    saveConfig();
  } else if (config) {
    dropDraft(DRAFT_CONFIG);
  }
  const subs = readDraft(DRAFT_SUBS);
  if (subs && await askConfirm('恢复草稿', '检测到上次未保存的订阅修改，是否恢复并重新保存？')) {
    state.subs = state.subs || { subscriptions: [] };
    state.subs.subscriptions = subs;
    holdSubDirty();
    renderSubscriptions();
    saveSubscriptions();
  } else if (subs) {
    dropDraft(DRAFT_SUBS);
  }
}

async function boot() {
  bindEvents();
  try {
    await loadConfig();
    await loadSubscriptions();
    await loadProviders();
    await restoreDrafts();
    await loadState();
    await loadUsage();
    await loadNotifications();
    await loadRequests();
    await loadMaintenance().catch(() => {});
    $('report-month').value = todayISO().slice(0, 7);
    await loadReport();
  } catch (err) {
    toast(err.message, 'bad');
    $('conn-badge').textContent = '连接失败';
    $('conn-badge').className = 'badge bad';
  }
  setInterval(() => {
    if (state.tab === 'status') {
      loadState().catch(() => {});
      loadRequests().catch(() => {});
    }
  }, 5000);
}

boot();
