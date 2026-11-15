'use strict';

// Keys live in the browser only. sessionStorage is the default (cleared when the
// tab closes); "remember" also writes localStorage so embedded previews that
// open a fresh session do not ask for the key on every reload.
const KEY_STORAGE = { gateway: 'facet_key', admin: 'facet_admin_key' };

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

const PAGE_TITLES = {
  dashboard: '仪表板', free: '免费模型',
  channels: '渠道配置', routes: '路由链', models: '模型接入',
  subscriptions: '套餐订阅', vendors: '厂家管理',
  apikeys: 'API Keys', adminkey: '管理密钥',
  status: '运行状态', maintenance: '自维护', cu: '浏览器代理', usage: '用量报表', requests: '请求日志',
  members: '成员用量',
  quickstart: '快速开始',
  integration: '接入文档',
};

const state = {
  config: null,
  snapshot: null,
  tab: 'dashboard',
  channelFilter: '',
  vendorFilter: '',
  key: readStoredKey('gateway'),
  adminKey: readStoredKey('admin'),
  requests: null,
  editing: -1,
  subs: null,
  editingSub: -1,
  providers: null,
  usage: null,
  usageFilter: '',
  usageRange: 30,
  notify: null,
  report: null,
  maintenance: null,
  freeFilter: 'all',
  freeQuery: '',
  maintlog: null,
  freealignLogs: null,
  backups: null,
  dashUsage: null,
  channelUsage: null,
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

// Compact number for vendor quota cells: 1234567.5 -> "1,234,567.5".
const fmtNum = (v) => (v === null || v === undefined || isNaN(Number(v)))
  ? '—' : Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 });

// Compact token counts for chart labels: 40298224 -> "40.3M", 411208 -> "411K".
const fmtTok = (v) => {
  const n = Number(v);
  if (!isFinite(n)) return '—';
  if (n >= 1e9) return `${parseFloat((n / 1e9).toFixed(2))}B`;
  if (n >= 1e6) return `${parseFloat((n / 1e6).toFixed(2))}M`;
  if (n >= 1e3) return `${parseFloat((n / 1e3).toFixed(1))}K`;
  return `${n}`;
};

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
      title: isApi ? '需要管理密钥' : '需要 API Key',
      message: isApi
        ? '管理接口返回 401。首次登录默认填 admin；登录后可在「管理密钥」页验证旧密钥后替换。'
        : '网关接口返回 401。填写 listen.api_key（API Key），也可在「API Keys」页复制或生成。',
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
      : '未授权：请点右上角「API Key」填入 api_key 后重试');
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

const DRAFT_SUBS = 'facet_draft_subs';
const saveTimers = { config: null, subs: null };

function dropDraft(key) {
  try { localStorage.removeItem(key); } catch (_) { /* ignore */ }
}
function readDraft(key) {
  try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { return null; }
}

// Shared by every copy button on the page.
function copyText(value) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(value).then(
      () => toast('已复制到剪贴板'),
      () => toast('复制失败，请手动选中复制', 'warn'),
    );
  } else {
    toast('当前环境不支持自动复制，请手动选中复制', 'warn');
  }
}

// The channel config persists on its own after every edit; there is no
// separate "save and apply" step. Debounced so typing in the model-policy
// box does not POST per keystroke. (Subscription edits commit immediately
// on confirm; they do not go through this scheduler.)
function scheduleSave(kind) {
  clearTimeout(saveTimers[kind]);
  saveTimers[kind] = setTimeout(() => {
    saveTimers[kind] = null;
    saveConfig();
  }, 600);
}

function markDirty() {
  scheduleSave('config');
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

// Pay-as-you-go vendors (OpenRouter, DeepSeek) report account usage to the
// same key the channel already uses. Loaded on demand by the channel page;
// the backend caches snapshots with a TTL, so re-entering the page is cheap.
async function loadChannelUsage() {
  const res = await api('/api/channels/usage');
  state.channelUsage = (res && res.usage) || {};
  renderChannels();
}

async function loadState() {
  state.snapshot = await api('/api/state');
  renderSummary();
  renderStatus();
  renderChannels();
  renderExactModels();
  renderBrandList();
  renderScores();
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

// Billing-priority badge fed by the router's billing_ranks snapshot: the
// order an EXPLICIT model name drains candidates in — monthly plans
// (soonest expiry first, fuller plan first on a same-day tie), then free
// tiers, then pay-as-you-go. Internal aliases (facet-auto/facet-free) do
// not consult this order.
function billingBadge(c) {
  const b = (state.snapshot?.channels || []).find((x) => x.name === c.name)?.billing;
  if (!b) return '';
  const tip = '显式模型请求的计费优先级：包月（快到期优先）→ 免费档 → 按量';
  if (b.group === 0) {
    let extra = '';
    if (b.expiry && b.burn > 0) {
      const days = Math.max(Math.floor((new Date(b.expiry) - new Date()) / 86400000), 0) + 1;
      const pct = Math.round(b.burn * days * 100);
      if (pct > 0) extra = ` · 剩约${pct}%`;
    }
    return `<span class="tag plan" title="${tip}；同日到期时剩余额度多者优先">包月${b.expiry ? '·' + b.expiry : ''}${extra}</span>`;
  }
  if (b.group === 1) return `<span class="tag plan" title="${tip}">包月·未登记到期</span>`;
  if (b.group === 2) return `<span class="tag free" title="${tip}">免费档</span>`;
  return `<span class="tag off" title="${tip}">按量</span>`;
}

// Per-(channel, model) stability score: served +1, non-quota failure -5.
// Quota exhaustion only cools and never scores, so this badge is stability.
function scoreBadge(score, channel, wire) {
  if (!score) return '';
  const cls = score > 0 ? 'score-pos' : 'score-neg';
  const reset = (channel && wire)
    ? `<button class="score-x" type="button" data-score-channel="${esc(channel)}" data-score-wire="${esc(wire)}" title="重置该渠道×模型的稳定性分" aria-label="重置分数">×</button>`
    : '';
  return `<span class="tag ${cls}" title="稳定性分：成功 +1，失败 -5">${score > 0 ? '+' : ''}${score}${reset}</span>`;
}

// The auto-model picker: `models.auto` is the concrete upstream model this
// channel serves whenever the router schedules internally (branded ids and
// unmatched names). Switching it here is the manual override the user asked
// for: a model that performs badly gets swapped without editing the mapping.
function autoModelOptions(c) {
  const models = c.models || {};
  const current = String(models.auto || '');
  const seen = new Set();
  const opts = [];
  Object.entries(models).forEach(([k, v]) => {
    if (!v || v === '*' || seen.has(v)) return;
    seen.add(v);
    const label = k === 'auto' ? `${v}（auto）` : (k === v ? v : `${v}（${k}）`);
    opts.push(`<option value="${esc(v)}" ${v === current ? 'selected' : ''}>${esc(label)}</option>`);
  });
  if (current && !seen.has(current)) {
    opts.unshift(`<option value="${esc(current)}" selected>${esc(current)}（当前）</option>`);
  }
  if (!opts.length) opts.push('<option value="">未配置模型</option>');
  return opts.join('');
}

function channelCard(c, i) {
  const entries = Object.entries(c.models || {});
  // Runtime status (cooling/blocked) lives in the state snapshot, not the config.
  const st = (state.snapshot?.channels || []).find((x) => x.name === c.name);
  const cooling = !!(st && st.cooling);
  // Vendor-reported usage for pay-as-you-go keys (OpenRouter spend, DeepSeek
  // balance); null until the channel page's usage fetch lands.
  const vuHtml = vendorUsageHtml(state.channelUsage?.[c.name]);
  const bit = ([k, v]) => {
    const badge = tierBadge(tierFor(c.name, c.tier, k));
    return `${esc(k)} → ${esc(v)}${badge}${scoreBadge(st?.scores?.[v], c.name, v)}`;
  };
  // Long mappings (aggregators can hold hundreds of models) collapse to the
  // first few plus an expander so the row cannot stretch the page.
  const SHOW = 8;
  const modelBits = entries.length > SHOW
    ? `${entries.slice(0, SHOW).map(bit).join(' · ')} <button type="button" class="btn small ghost" data-action="toggle-models" data-total="${entries.length}">… 共 ${entries.length} 个</button><span class="models-more" hidden>${entries.slice(SHOW).map(bit).join(' · ')}</span>`
    : entries.map(bit).join(' · ');
  const ptag = providerTag(c);
  return `<article class="card row-card${cooling ? ' cooling' : ''}">
    <div class="row-head">
      <h3 class="ch-name ${c.enabled === false ? 'off' : esc(c.tier === 'free' ? 'free' : c.tier === 'paid' ? 'paid' : '')}">${esc(c.name)}</h3>
      <span class="pill ${esc(c.protocol)}">${esc(c.protocol)}</span>
      ${channelTierBadge(c.name, c.tier)}${billingBadge(c)}${ptag ? `<small class="provider-tag">${esc(ptag)}</small>` : ''}
      <span class="switch ${c.enabled === false ? 'off' : ''}">${c.enabled === false ? '已停用' : '启用'}</span>
      ${cooling ? `<span class="tag cooling">冷却中 · 剩 ${Math.max(1, Math.ceil((st.cooling_seconds || 0) / 60))} 分钟</span>` : ''}
      <span class="row-actions">
        <button class="btn small" data-action="edit" data-index="${i}">编辑</button>
        <button class="btn small ghost" data-action="duplicate" data-name="${esc(c.name)}">复制</button>
        <button class="btn small ghost" data-action="catalog" data-index="${i}" data-name="${esc(c.name)}">拉取模型</button>
        <button class="btn small ghost" data-action="test" data-index="${i}">测试</button>
        <button class="btn small ghost" data-action="toggle" data-index="${i}">${c.enabled === false ? '启用' : '停用'}</button>
        <button class="btn small ghost" data-action="reset" data-name="${esc(c.name)}">解除冷却</button>
        <button class="btn small danger" data-action="delete" data-index="${i}">删除</button>
      </span>
    </div>
    <dl>
      <div><dt>端点</dt><dd>${esc(c.base_url)}</dd></div>
      <div><dt>优先级</dt><dd>${esc(c.priority ?? 100)}</dd></div>
      <div><dt>额度冷却</dt><dd>${esc(c.cooldown_minutes ?? 300)} 分钟</dd></div>
      <div><dt>额度计量</dt><dd>${c.per_model_quota ? '按模型（只冻结该模型）' : '按渠道'}</dd></div>
      <div><dt>本地限流</dt><dd>${esc(rateText(c.rate))}</dd></div>
      <div><dt>密钥</dt><dd>${keyText(c.api_key)}</dd></div>
      <div><dt>账号 / 业务线</dt><dd>${c.account ? esc(accountLabel(c.account)) : '—'}${c.business_line ? ' · ' + esc(c.business_line) : ''}</dd></div>
      ${vuHtml ? `<div><dt>厂家用量</dt><dd class="wrap">${vuHtml}</dd></div>` : ''}
      <div><dt>模型目录</dt><dd class="wrap">${esc(catalogSummary(c.name))}</dd></div>
    </dl>
    <dl class="models-line"><div><dt>默认模型</dt><dd>
      <select class="auto-model" data-auto-model data-index="${i}" aria-label="切换该渠道的默认（auto）模型">
        ${autoModelOptions(c)}
      </select>
      <small class="hint">切换后点「保存配置」生效，auto / facet 请求都走这里</small>
    </dd></div></dl>
    <dl class="models-line"><div><dt>模型映射</dt><dd class="wrap">${modelBits || '未配置'}</dd></div></dl>
  </article>`;
}

function channelHaystack(c) {
  return [c.name, c.protocol, c.base_url, c.tier || '', providerTag(c),
    ...Object.entries(c.models || {}).flat()].join(' ').toLowerCase();
}

function renderChannels() {
  const list = $('channel-list');
  const kw = (state.channelFilter || '').trim().toLowerCase();
  // Enabled channels first, then the config order (priority; the backend
  // sort stays untouched because it also drives $free expansion and retry
  // order). Array.prototype.sort is stable, so ties keep their order.
  const shown = state.config.channels
    .map((c, i) => [c, i])
    .filter(([c]) => !kw || channelHaystack(c).includes(kw))
    .sort((a, b) => (a[0].enabled === false ? 1 : 0) - (b[0].enabled === false ? 1 : 0));
  list.innerHTML = shown.length
    ? shown.map(([c, i]) => channelCard(c, i)).join('')
    : `<div class="card"><p class="hint">${state.config.channels.length
        ? `没有匹配「${esc(kw)}」的渠道`
        : '还没有渠道，点击「新增渠道」开始。'}</p></div>`;
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
  // With an active channel filter the top metrics reflect the filtered subset.
  const kw = (state.channelFilter || '').trim().toLowerCase();
  const rows = kw
    ? s.channels.filter((c) => {
        const cfg = (state.config?.channels || []).find((x) => x.name === c.name);
        return cfg && channelHaystack(cfg).includes(kw);
      })
    : s.channels;
  const cooling = rows.filter((c) => c.cooling).length;
  const requests = rows.reduce((n, c) => n + c.total_requests, 0);
  const tokens = rows.reduce((n, c) => n + c.total_tokens, 0);
  const listen = s.listen ? `${s.listen.host}:${s.listen.port}` : '—';
  const items = [
    ['渠道总数', kw ? `${rows.length} / ${s.channels.length}` : rows.length, ''],
    ['可用渠道', rows.length - cooling, 'ok'],
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
    const kind = !c.enabled ? 'bad' : (c.blocked ? 'bad' : (c.cooling ? 'warn' : (!c.configured ? 'muted' : 'ok')));
    const label = !c.enabled ? '已停用' : (c.blocked ? '已停用' : (c.cooling ? '冷却中' : (!c.configured ? '未配置' : '可用')));
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
    const account = a.account_id || a.account || '—';
    return `<span class="banner-item">${esc(a.vendor)} / ${esc(account)} / ${esc(a.plan)} · <span class="days">${days}</span></span>`;
  }).join('') + quotaAlerts.map((a) =>
    `<span class="banner-item">${esc(a.vendor)} / ${esc(a.account_id || a.account || '—')} / ${esc(a.plan)} · <span class="days">额度仅剩 ${esc(a.quota_remaining)} ${esc(a.quota_unit)}</span></span>`).join('');
  el.innerHTML = `<div class="banner ${critical ? 'critical' : ''}">
    <span class="banner-title">${critical ? '有套餐已过期' : '套餐与额度提醒'}</span>
    <span class="banner-items">${items}</span>
    <button class="btn small" id="banner-view">查看订阅</button>
  </div>`;
  $('banner-view').onclick = () => switchPage('subscriptions');
}

// The sidebar selection is mirrored in the URL hash ("#/vendors"), so a
// reload or a bookmark reopens the page that was open instead of falling back
// to the dashboard.
function pageFromHash() {
  const name = (location.hash || '').replace(/^#\/?/, '');
  return PAGE_TITLES[name] ? name : '';
}

function switchPage(name) {
  if (!PAGE_TITLES[name]) name = 'dashboard';
  state.tab = name;
  const hash = '#/' + name;
  if (location.hash !== hash) history.replaceState(null, '', hash);
  document.querySelectorAll('.menu-item').forEach((t) =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach((p) =>
    p.classList.toggle('active', p.id === 'panel-' + name));
  $('page-title').textContent = PAGE_TITLES[name] || name;
  // Each page loads its own data on entry, so opening the console stays cheap.
  if (name === 'dashboard') loadDashboard().catch((e) => toast(e.message, 'bad'));
  if (name === 'free') loadFreeModels().catch((e) => toast(e.message, 'bad'));
  if (name === 'channels') loadChannelUsage().catch(() => {});
  if (name === 'models') {
    renderModels();
    loadState().catch(() => {});
    refreshModelInsights().catch(() => {});
  }
  if (name === 'status') loadState().catch(() => {});
  if (name === 'maintenance') {
    loadMaintenance().catch((e) => toast(e.message, 'bad'));
    loadMaintLogs().catch(() => {});
    loadFreeAlignLogs().catch(() => {});
    loadBackups().catch(() => {});
  }
  if (name === 'cu') loadCuCredentials().catch((e) => toast(e.message, 'bad'));
  if (name === 'usage') {
    loadUsage().catch((e) => toast(e.message, 'bad'));
    loadReport($('report-month').value).catch(() => {});
  }
  if (name === 'requests') loadRequests().catch(() => {});
  if (name === 'members') loadMembers().catch((e) => toast(e.message, 'bad'));
  if (name === 'quickstart') {
    const open = $('btn-open-wizard');
    if (open) open.onclick = () => renderWizard('welcome').catch(() => {});
    renderWizard('welcome').catch(() => {});
  }
  if (name === 'apikeys') loadApiKeys().catch((e) => toast(e.message, 'bad'));
  if (name === 'subscriptions') {
    loadNotifications().catch((e) => toast(e.message, 'bad'));
    loadReport($('report-month').value).catch(() => {});
    loadSubscriptions().catch((e) => toast(e.message, 'bad'));
  }
  if (name === 'vendors') {
    loadProviders().then(renderVendors).catch((e) => toast(e.message, 'bad'));
  }
  if (name === 'integration') loadIntegrationDoc().catch((e) => toast(e.message, 'bad'));
}

// Integration guide: render docs/agent-integration.md inline (same Markdown
// renderer as the api-key manual) so downstream agents can read it in-browser.
async function loadIntegrationDoc() {
  const body = $('integration-body');
  if (!body || body.dataset.loaded === 'yes') return;
  const res = await api('/api/guide?doc=integration');
  if (!res || !res.available) {
    body.innerHTML = '<p class="hint">接入文档不可用：docs/agent-integration.md 未随镜像打包。</p>';
    return;
  }
  body.innerHTML = renderMarkdown(res.markdown);
  body.dataset.loaded = 'yes';
}

// Deep links rendered inside HTML strings (e.g. the fallback hint's jump to
// the request log) resolve here.
document.addEventListener('click', (e) => {
  const link = e.target.closest('[data-goto-tab]');
  if (link) switchPage(link.dataset.gotoTab);
});

// Click-away for the channel editor's test-model dropdown.
document.addEventListener('click', (e) => {
  const drop = $('editor-test-model-drop');
  if (drop && !drop.hidden && !e.target.closest('.tm-wrap')) drop.hidden = true;
});

// Dashboard: the landing page. Pure frontend aggregation over existing
// endpoints — /api/state carries channels, the subscription summary and the
// maintenance digest; /api/usage/7, /api/maintenance and /api/backups top up
// the trend chart, free-quota alignment and backup counts. No backend logic.
async function loadDashboard() {
  const [snap, usage, maint, backups, dsh, subsData] = await Promise.all([
    api('/api/state'),
    api('/api/usage/7').catch(() => null),
    api('/api/maintenance').catch(() => null),
    api('/api/backups').catch(() => null),
    api('/api/integrations/dsh').catch(() => null),
    // Per-account plan windows live on /api/subscriptions; the vendor calls
    // behind it are TTL-cached server-side, so polling this is cheap.
    api('/api/subscriptions').catch(() => null),
  ]);
  state.snapshot = snap;
  if (usage) state.dashUsage = usage;
  if (maint) state.maintenance = maint;
  if (backups) state.backups = backups;
  if (dsh) state.dshInfo = dsh;
  if (subsData) {
    state.subs = subsData;
    renderSubscriptions();
  }
  renderDashboard();
}

function dashRow(label, html) {
  return `<div class="dash-kv-row"><span class="k">${label}</span><span class="v">${html}</span></div>`;
}

function renderDashboard() {
  const s = state.snapshot;
  if (!s) return;
  const channels = s.channels || [];
  const cooling = channels.filter((c) => c.cooling).length;
  const blocked = channels.filter((c) => c.blocked).length;
  const ok = channels.length - cooling - blocked;
  const requests = channels.reduce((n, c) => n + c.total_requests, 0);
  const tokens = channels.reduce((n, c) => n + c.total_tokens, 0);
  const handoffs = channels.reduce((n, c) => n + c.handoffs, 0);
  let unstable = 0;
  channels.forEach((c) => Object.values(c.scores || {}).forEach((v) => { if (v < 0) unstable += 1; }));
  const subs = s.subscriptions || {};
  const counts = subs.counts || {};
  const cost = Object.entries(subs.monthly_cost || {})
    .map(([code, amount]) => `${amount} ${code}`).join(' / ') || '—';
  const quotaAlerts = (subs.quota_alerts || []).length;
  const free = (state.maintenance && state.maintenance.free_models) || {};
  const freeSummary = free.summary || {};
  const freeUsable = Math.max(0, (freeSummary.routable || 0) - (freeSummary.quota_warning || 0));

  const kpis = [
    ['可用渠道', channels.length ? `${ok} / ${channels.length}` : '0', ok ? 'ok' : ''],
    ['可用免费模型', `${freeUsable} / ${freeSummary.models || 0}`,
      freeUsable ? 'ok' : (freeSummary.models ? 'warn' : '')],
    ['冷却中', cooling, cooling ? 'warn' : ''],
    ['累计请求', requests.toLocaleString(), ''],
    ['累计 Tokens', tokens.toLocaleString(), ''],
    ['月度成本', cost, ''],
    ['即将到期', counts.expiring ?? 0, counts.expiring ? 'warn' : ''],
  ];
  $('dash-summary').innerHTML = kpis.map(([label, value, cls]) =>
    `<div class="metric"><div class="label">${label}</div><div class="value ${cls}">${esc(value)}</div></div>`).join('');

  // 免费模型速览：直接回答「现在能用哪些免费模型」。
  const freeWarn = freeSummary.quota_warning || 0;
  const freeDead = freeSummary.quota_exhausted || 0;
  $('dash-free').innerHTML = [
    dashRow('现在能用', `<span class="days-left ${freeUsable ? 'active' : 'expired'}">`
      + `${freeUsable} 个模型</span>`),
    dashRow('额度紧张', freeWarn
      ? `<span class="days-left expiring">${freeWarn} 个（≥80%）</span>` : '0 个'),
    dashRow('额度用完', freeDead
      ? `<span class="days-left expired">${freeDead} 个（已停止路由）</span>` : '0 个'),
    dashRow('厂家额度', freeSummary.quota_unsynced
      ? `<span class="days-left expiring">${freeSummary.quota_unsynced} 个渠道未同步</span>` : '已同步'),
  ].join('');

  // 运行健康（稳定）：failover 面板。
  $('dash-health').innerHTML = [
    dashRow('渠道状态', `${ok} 可用 · ${cooling} 冷却 · ${blocked} 停用`),
    dashRow('累计交接', `${handoffs} 次`),
    dashRow('不稳定模型', unstable
      ? `<span class="days-left expired">${unstable} 个（负分）</span>` : '0 个'),
  ].join('');

  // 订阅与成本（省钱）：资源面板。
  const upcoming = (subs.upcoming || []).slice(0, 3);
  $('dash-subs').innerHTML = [
    dashRow('订阅状态',
      `${counts.active ?? 0} 生效 · ${counts.expiring ?? 0} 即将到期 · ${counts.expired ?? 0} 已过期`),
    dashRow('月度等效成本', esc(cost)),
    dashRow('低额度告警', quotaAlerts
      ? `<span class="days-left expired">${quotaAlerts} 条</span>` : '0 条'),
  ].join('')
    + (upcoming.length ? upcoming.map((r) => dashRow(
        `${esc(r.vendor)}${r.account_id ? ' / ' + esc(r.account_id) : ''}`,
        `${esc(r.plan)} · 剩 ${r.days_left} 天`)).join('') : '');

  // 自维护（省心）：自主面板。
  const maint = s.maintenance || {};
  const last = maint.last;
  const rows = [
    dashRow('维护调度', maint.hours ? `每 ${maint.hours} 小时` : '未启用'),
    dashRow('上次维护', last
      ? `${new Date(last.finished_at * 1000).toLocaleString()} · ${esc(MAINT_TRIGGER[last.trigger] || last.trigger || '')}`
      : '还没有运行记录'),
  ];
  if (last) {
    rows.push(dashRow('维护结果',
      `应用 ${last.applied} · 待采纳 ${last.suggestions} · 告警 ${last.alerts}`
      + (last.llm_error ? ' · <span class="days-left expired">LLM 分析失败</span>' : '')));
  }
  const align = state.maintenance?.free_align;
  const alignLast = (align?.reports || [])[0];
  const alignAt = alignLast ? (alignLast.ts || alignLast.finished_at) : null;
  rows.push(dashRow('免费额度对齐',
    (align && align.enabled ? `每 ${align.hours} 小时` : '未启用')
    + (alignAt && alignLast.finished_at ? ` · 上次 ${new Date(alignAt * 1000).toLocaleString()}`
        : alignAt ? ` · 上次 ${esc(alignAt)}` : '')));
  rows.push(dashRow('配置备份', `${(state.backups?.backups || []).length} 份`));
  $('dash-selfcare').innerHTML = rows.join('');

  // DSH 验证：外部客户端是否装在这台宿主机、profile 指向哪、界面能否打开。
  const dsh = state.dshInfo;
  const dshRows = [];
  if (!dsh) {
    dshRows.push(dashRow('客户端', '无法读取 DSH 状态'));
  } else {
    dshRows.push(dashRow('客户端', dsh.installed
      ? `已安装${dsh.version ? ' · v' + esc(dsh.version) : ''}`
      : '<span class="days-left expired">未安装</span>'));
    dshRows.push(dashRow('profile', `${esc(dsh.profile || '')}`
      + (dsh.profile_exists ? '' : ' <span class="days-left expired">缺失</span>')));
    dshRows.push(dashRow('指向网关', dsh.base_url ? esc(dsh.base_url) : '未配置'));
    dshRows.push(dashRow('运行状态', dsh.running
      ? `运行中 · pid ${dsh.pid}`
      : '未运行'));
    if (dsh.url) {
      dshRows.push(dashRow('界面地址',
        `<a href="${esc(dshUrl(dsh.url))}" target="_blank" rel="noopener">打开</a>`));
    }
    if (dsh.note) dshRows.push(dashRow('提示', esc(dsh.note)));
  }
  $('dash-dsh').innerHTML = dshRows.join('');
  // Keep the anchor on the console's hostname (see dshUrl); the click itself is
  // handled by the popup below, but Ctrl/middle clicks use this href.
  $('btn-dsh-open').href = (dsh && dsh.running && dsh.url) ? dshUrl(dsh.url) : '#';

  // 订阅用量：账号、档位、状态、厂家额度窗口与到期，登录即见每个订阅还剩多少。
  const records = (state.subs && state.subs.subscriptions) || [];
  const subLabels = { active: '生效中', expiring: '即将到期', expired: '已过期', unknown: '未填到期日' };
  $('dash-subusage').innerHTML = records.length ? records.map((r) => {
    const days = r.days_left === null || r.days_left === undefined ? '—'
      : r.days_left < 0 ? `逾期 ${-r.days_left} 天` : `剩 ${r.days_left} 天`;
    return `<tr>
      <td><strong>${esc(r.plan)}</strong><br><span class="sub-note">${esc([r.vendor && r.vendor !== r.plan ? r.vendor : '',
        r.account_id || r.account || '未填账号'].filter(Boolean).join(' · '))}</span></td>
      <td>${esc(r.tier) || '—'}</td>
      <td><span class="tag ${esc(r.status)}">${esc(subLabels[r.status] || r.status)}</span></td>
      <td class="${r.low_quota ? 'quota-low' : 'sub-note'}">${planQuotaCell(r)}</td>
      <td>${esc(r.expires_at) || '—'}<br><span class="days-left ${esc(r.status)}">${days}</span></td>
    </tr>`;
  }).join('') : '<tr><td colspan="5" class="sub-note">还没有订阅记录，去「套餐订阅」新增后即可在这里看到账号与用量。</td></tr>';

  // 近 7 天用量趋势：与用量报表页同一套 bars/sparkline。
  const usage = state.dashUsage;
  const el = $('dash-trend');
  if (!usage || !usage.days || !usage.days.length) {
    el.innerHTML = '<div class="chart-empty">暂无用量数据，产生请求后会自动按天累计。</div>';
    return;
  }
  const req = usage.days.map((d) => d.requests);
  const tok = usage.days.map((d) => d.tokens);
  el.innerHTML = `<div class="chart-grid">
    <div class="chart-block"><h4>每日请求数（合计 ${req.reduce((a, b) => a + b, 0)}）</h4>${bars(req, '#5b9dff')}</div>
    <div class="chart-block"><h4>每日 Tokens（合计 ${tok.reduce((a, b) => a + b, 0).toLocaleString()}）</h4>${sparkline(tok, '#8a6cff')}</div>
  </div>`;
}

$('btn-refresh-dashboard').onclick = () => loadDashboard().catch((e) => toast(e.message, 'bad'));

// DSH is a separate Node client: start its web profile, then hand the tokenised
// URL to the browser. "启动" keeps the tab closed; "打开界面" opens it.
async function startDshRequest() {
  const result = await api('/api/integrations/dsh/web', { method: 'POST', body: {} });
  state.dshInfo = result;
  renderDashboard();
  return result;
}

async function startDshOnly() {
  const btn = $('btn-dsh-start');
  btn.disabled = true;
  try {
    const result = await startDshRequest();
    if (result.url) toast('DSH 已启动，点「打开 DSH 界面」进入', 'ok');
    else toast('DSH 已启动，但还没有拿到访问地址，请刷新后再试', 'warn');
  } catch (e) {
    toast(e.message, 'bad');
    loadDashboard().catch(() => {});
  } finally {
    btn.disabled = false;
  }
}

// Open DSH in its own popup window; the window is claimed synchronously inside
// the click so the navigation keeps the user gesture.
const DSH_WIN = 'facet-dsh';

// dsh always prints 127.0.0.1:3080 while the console may be served from
// localhost:8010. Those are different sites, and DSH's session cookie is
// SameSite=Strict: Chrome/Edge do store it on the token URL but then refuse to
// send it on the redirect that follows the cross-site navigation, so `GET /`
// answers 401 — plain text on a white background, which reads as a blank page.
// Same-site (localhost:8010 -> localhost:3080) keeps the cookie flowing, so
// keep DSH on the console's loopback alias. Only the loopback aliases are
// interchangeable in practice, and dsh listens on loopback only.
const LOOPBACK_HOSTS = ['localhost', '127.0.0.1', '[::1]'];

function dshUrl(url) {
  if (!url) return '';
  try {
    const u = new URL(url);
    const here = window.location.hostname;
    if (LOOPBACK_HOSTS.includes(u.hostname) && LOOPBACK_HOSTS.includes(here)) {
      u.hostname = here;
    }
    return u.href;
  } catch (e) { return url; }
}

function dshPopup(url) {
  const width = 1280;
  const height = 900;
  const left = Math.max(0, Math.round(window.screenX + (window.outerWidth - width) / 2));
  const top = Math.max(0, Math.round(window.screenY + (window.outerHeight - height) / 2));
  try {
    const win = window.open(url, DSH_WIN,
      `popup=yes,width=${width},height=${height},left=${left},top=${top}`);
    if (win) win.focus();
    return win;
  } catch (e) { return null; }
}

$('btn-dsh-start').onclick = startDshOnly;
// Modified clicks (Ctrl/Shift/Cmd/middle) fall through to the anchor's own href.
$('btn-dsh-open').onclick = async (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
  ev.preventDefault();
  const dsh = state.dshInfo;
  if (dsh && dsh.running && dsh.url) {
    if (!dshPopup(dshUrl(dsh.url))) toast('浏览器拦截了弹窗，请允许本站弹窗后重试', 'warn');
    return;
  }
  // Cold start: no URL yet, so the window can only be opened after dsh reports
  // one — the first click starts it, the second opens it.
  await startDshOnly();
};

// Vendor usage snapshot rendering, shared by the subscriptions table (plan
// rows) and the channel cards (pay-as-you-go keys). Vendor-reported windows
// (Volcengine plans, OpenCode Go) replace the hand-typed remainder when
// available; the local number stays in the editor and the banner logic, it
// just loses the display slot.
function vendorUsageHtml(vp) {
  if (!vp) return '';
  if (vp.error && !vp.windows) {
    return `<span class="sub-note">厂家余量获取失败：${esc(vp.error)}${vp.stale ? '（显示上次快照）' : ''}</span>`;
  }
  if (Array.isArray(vp.windows) && vp.windows.length) {
    const levels = { session: '会话', weekly: '周', monthly: '月',
      five_hour: '5小时', daily: '日', budget: '消费限额' };
    // Pre-formatted lines win: pay-as-you-go vendors (OpenRouter) express
    // budgets the generic math below cannot.
    const texts = vp.windows.filter((w) => w.text);
    if (texts.length) return texts.map((w) => esc(w.text)).join(' · ');
    // Coding Plan windows report Percent only; Agent Plan (AFP) reports the
    // absolute Quota/Used, which reads better so it wins when present.
    const absolute = vp.windows.some((w) => w.quota !== undefined && w.quota !== null);
    const win = absolute
      ? vp.windows.map((w) =>
          `${levels[w.level] || esc(w.level)} 已用 ${fmtNum(w.used)} / ${fmtNum(w.quota)}`)
        .join(' · ')
      : vp.windows
          .filter((w) => w.percent !== null && w.percent !== undefined)
          .map((w) => {
            // OpenCode Go reports ISO reset stamps; surfacing the monthly
            // one answers "when does the quota come back" at a glance.
            const reset = (vp.provider === 'opencode'
              && w.level === 'monthly' && w.reset_ts)
              ? `（${new Date(w.reset_ts * 1000).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })} 重置）`
              : '';
            const bad = w.status && w.status !== 'ok' ? `（${esc(w.status)}）` : '';
            return `${levels[w.level] || esc(w.level)} 剩 ${Math.max(0, 100 - w.percent).toFixed(1)}%${reset}${bad}`;
          })
          .join(' · ');
    const meta = [vp.plan
        ? `${esc(vp.plan)}${vp.plan_type ? ` ${esc(vp.plan_type)}` : ''}` : '',
      vp.end_time ? `到期 ${esc(vp.end_time.slice(0, 10))}` : '']
      .filter(Boolean).join(' · ');
    return `${esc(win)}${meta ? `<br><span class="sub-note">${meta}</span>` : ''}`;
  }
  if (vp.windows && !vp.windows.length) {
    // Pay-as-you-go balances (DeepSeek) carry no windows, only a summary.
    if (vp.summary) return esc(vp.summary);
    return '<span class="sub-note">厂家无用量窗口（可能未订阅）</span>';
  }
  return '';
}

function planQuotaCell(r) {
  const quota = (r.quota_remaining === null || r.quota_remaining === undefined)
    ? '—'
    : `${r.quota_remaining} ${r.quota_unit || ''}${r.quota_ratio !== null && r.quota_ratio !== undefined ? ` · ${Math.round(r.quota_ratio * 100)}%` : ''}`;
  const vp = r.vendor_plan;
  const html = vp ? vendorUsageHtml(vp) : '';
  return html || esc(quota);
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
    return `<tr>
      <td><strong>${esc(r.plan)}</strong><br><span class="sub-note">${esc([r.vendor && r.vendor !== r.plan ? r.vendor : '',
        r.account_id || r.account || '未填账号'].filter(Boolean).join(' · '))}</span></td>
      <td>${esc(r.tier) || '未填档位'}</td>
      <td>${esc(r.price)} ${esc(r.currency)}</td>
      <td>${esc(cycles[r.billing_cycle] || r.billing_cycle)}</td>
      <td>${esc(r.started_at) || '—'}</td>
      <td>${esc(r.expires_at) || '—'}</td>
      <td class="days-left ${esc(r.status)}">${days}</td>
      <td><span class="tag ${esc(r.status)}">${labels[r.status] || r.status}</span></td>
      <td>${r.auto_renew ? '是' : '否'}</td>
      <td class="${r.low_quota ? 'quota-low' : 'sub-note'}">${planQuotaCell(r)}</td>
      <td class="sub-note">${esc((r.channel_names || r.channels || []).join(', ')) || '—'}</td>
      <td>
        <button class="btn small" data-action="renew" data-id="${esc(r.id)}">续费</button>
        <button class="btn small ghost" data-action="edit-sub" data-id="${esc(r.id)}">编辑</button>
        <button class="btn small danger" data-action="delete-sub" data-id="${esc(r.id)}">删除</button>
      </td>
    </tr>`;
  }).join('') : '<tr><td colspan="12" class="sub-note">还没有订阅记录，点击「新增订阅」选择关联渠道、录入套餐与到期时间。</td></tr>';
}

function channelPicker(selected) {
  const saved = (Array.isArray(selected) ? selected : []).map((n) => String(n));
  const available = state.config?.channels || [];
  // Channel ids are the stable link; older rows still carry the channel name.
  const refOf = (c) => String(c.id || c.name);
  const refs = [...new Set([...available.map(refOf), ...saved])];
  if (!refs.length) {
    return `<div class="field wide"><label>关联渠道（勾选，可多选）</label>
      <div class="channel-picker empty">还没有可选渠道。请先在「渠道配置」新增渠道并点「保存」，再回来勾选。</div></div>`;
  }
  const items = refs.map((ref) => {
    const c = available.find((x) => refOf(x) === ref);
    const name = c ? String(c.name) : ref;
    const detail = c ? `${c.protocol} · ${c.base_url}` : '渠道不存在（保存时保留原值）';
    return `<label class="pick ${c ? '' : 'missing'}">
      <input type="checkbox" name="channels" value="${esc(ref)}" ${saved.includes(ref) ? 'checked' : ''}>
      <span><strong>${esc(name)}</strong><small>${esc(detail)}</small></span>
    </label>`;
  }).join('');
  return `<div class="field wide"><label>关联渠道（勾选，可多选）</label>
    <div class="channel-picker">${items}</div>
    <small class="pick-note">按渠道 ID 关联，渠道改名不影响；勾选后自动带出套餐名称与档位，仍可手动修改</small></div>`;
}

function pickedChannels(f) {
  return Array.from(f.querySelectorAll('input[name="channels"]:checked'), (n) => n.value);
}

// --- Account helpers ------------------------------------------------------
// Accounts are optional: a channel with no account is its own implicit account.
// They group one vendor's AK/SK so several business lines share a single pair.
const BUSINESS_LINES = [['', '未指定'], ['coding', 'coding'], ['agent', 'agent'],
  ['payg', '按量付费']];

const accountsFor = (provider) => (state.config?.accounts || [])
  .filter((a) => a && (!provider || a.provider === provider));

const accountLabel = (id) => {
  const a = (state.config?.accounts || []).find((x) => x && x.id === id);
  return a ? `${a.label || a.id}（${a.id}）` : id;
};

function accountOptions(provider, current) {
  const items = accountsFor(provider);
  const opts = [`<option value="" ${current ? '' : 'selected'}>不绑定账号（渠道自带凭据）</option>`];
  items.forEach((a) => {
    opts.push(`<option value="${esc(a.id)}" ${a.id === current ? 'selected' : ''}>${esc(a.label || a.id)}（${esc(a.id)}）</option>`);
  });
  if (current && !items.some((a) => a.id === current)) {
    opts.push(`<option value="${esc(current)}" selected>${esc(current)}（账号不存在）</option>`);
  }
  return opts.join('');
}

function businessLineOptions(current) {
  const cur = String(current || '');
  return BUSINESS_LINES.map(([v, l]) =>
    `<option value="${v}" ${cur === v ? 'selected' : ''}>${l}</option>`).join('');
}

function selectField(label, name, optionsHtml, hint) {
  return `<div class="field"><label for="f-${name}">${label}</label>
    <select id="f-${name}" name="${name}">${optionsHtml}</select>
    ${hint ? `<small class="hint">${esc(hint)}</small>` : ''}</div>`;
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
  const refs = pickedChannels(f);
  if (!refs.length) return;
  const c = (state.config?.channels || []).find((x) => String(x.id || x.name) === refs[0]);
  if (!c) return;
  const fill = (field, value) => {
    const el = f.elements.namedItem(field);
    if (!el || !value) return;
    if (el.value.trim() && el.dataset.autofill !== '1') return;
    el.value = value;
    el.dataset.autofill = '1';
  };
  fill('plan', c.plan || inferPlan(c));
  if (c.tier) fill('tier', c.tier);
}

function openSubEditor(index) {
  state.editingSub = index;
  const r = index >= 0 ? state.subs.subscriptions[index] : {
    account: '', account_id: '', business_line: '', plan: '', tier: '',
    price: '', currency: 'CNY',
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
        ${field('套餐名称 *', 'plan', r.plan, { placeholder: 'agentplan', hint: '这次订阅或套餐的名称；厂家由关联渠道的 provider 决定' })}
        ${field('账号备注', 'account', r.account || '', { placeholder: '账号 A（自由文本）' })}
        ${selectField('绑定账号', 'account_id',
          accountOptions('', r.account_id || ''), '选择已建档账号后，余量按账号聚合；未建档就只填左侧备注')}
        ${selectField('业务线', 'business_line',
          businessLineOptions(r.business_line || ''), 'coding / agent / payg：同账号多条业务线分别记账')}
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
  ['plan', 'tier'].forEach((n) => {
    const el = f.elements.namedItem(n);
    if (el) el.addEventListener('input', () => { el.dataset.autofill = ''; });
  });
  autofillFromChannels(f);
  f.onsubmit = (e) => { e.preventDefault(); saveSubEditor(); };
}

async function saveSubEditor() {
  const f = $('sub-form');
  const val = (n) => (f.elements.namedItem(n)?.value ?? '').trim();
  let record;
  try {
    record = {
      ...(state.editingSub >= 0 ? { id: state.subs.subscriptions[state.editingSub].id } : {}),
      account: val('account'), account_id: val('account_id'),
      business_line: val('business_line'), plan: val('plan'), tier: val('tier'),
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
    if (!record.plan) throw new Error('套餐名称不能为空');
  } catch (err) {
    toast(err.message, 'bad');
    return;
  }
  const list = state.subs.subscriptions.slice();
  if (state.editingSub >= 0) list[state.editingSub] = record;
  else list.push(record);
  // Confirm = commit: the modal POSTs immediately; on failure it stays open
  // so nothing can silently live in page memory only.
  try {
    state.subs = await api('/api/subscriptions', {
      method: 'POST', body: { subscriptions: list },
    });
  } catch (err) {
    toast('订阅未保存：' + err.message, 'bad');
    return;
  }
  closeModal();
  renderSubscriptions();
  await loadState();
  toast('订阅已保存');
}

function openRenewModal(id) {
  const r = state.subs.subscriptions.find((x) => x.id === id);
  if (!r) return;
  const months = { monthly: 1, quarterly: 3, yearly: 12 }[r.billing_cycle] || 1;
  const suggested = addMonthsISO(r.expires_at, months, !r.expires_at || r.expires_at < todayISO());
  openModal('续费 · ' + (r.vendor && r.vendor !== r.plan ? r.vendor + ' / ' : '') + r.plan, `
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
  if (!record || !(await askConfirm('删除订阅', `删除订阅「${record.plan}」？`))) return;
  // Confirm = commit: the row is removed server-side immediately.
  try {
    state.subs = await api('/api/subscriptions', {
      method: 'POST',
      body: { subscriptions: state.subs.subscriptions.filter((x) => x.id !== id) },
    });
  } catch (err) {
    toast('删除失败：' + err.message, 'bad');
    return;
  }
  renderSubscriptions();
  await loadState();
  toast('订阅已删除');
}

// ---------------------------------------------------------- vendor registry
const VENDOR_FETCH_LABEL = { docs: '文档提取', api: '接口拉取', manual: '手动' };
const VENDOR_MODE_KEYS = ['coding', 'agent', 'payg'];
const VENDOR_TIER_LABEL = { free: '免费', paid: '收费' };

function vendorFetchMethod(mode) {
  if (mode.docs_url) return 'docs';
  if (mode.models_url) return 'api';
  return 'manual';
}

function vendorHaystack(p) {
  const parts = [p.id, p.name, p.origin === 'overlay' ? '自定义' : '内置'];
  for (const [key, mode] of Object.entries(p.modes || {})) {
    parts.push(key, mode.label || '', mode.protocol || '', mode.base_url || '', mode.tier || '',
      ...(mode.models || []));
  }
  return parts.join(' ').toLowerCase();
}

function renderVendors() {
  const el = $('vendor-list');
  if (!el) return;
  const vendors = state.providers || [];
  if (!vendors.length) {
    el.innerHTML = '<p class="sub-note">注册表为空。点「新增厂家」添加，或检查 providers 配置。</p>';
    return;
  }
  const kw = (state.vendorFilter || '').trim().toLowerCase();
  const shown = kw ? vendors.filter((p) => vendorHaystack(p).includes(kw)) : vendors;
  if (!shown.length) {
    el.innerHTML = `<div class="card"><p class="hint">没有匹配「${esc(kw)}」的厂家</p></div>`;
    return;
  }
  el.innerHTML = shown.map((p) => {
    const modes = Object.entries(p.modes || {}).map(([key, mode]) => {
      const method = vendorFetchMethod(mode);
      const models = (mode.models || []).map((m) => `<code>${esc(m)}</code>`).join(' ');
      const listHtml = models
        ? `<button type="button" class="btn small ghost" data-action="toggle-vendor-models" data-total="${mode.models.length}">… 共 ${mode.models.length} 个</button>
           <div class="models-more" hidden>${models}</div>`
        : '<span class="sub-note">无模型清单</span>';
      return `<div class="vendor-mode">
        <div class="vendor-mode-head">
          <span class="tag active">${esc(mode.label || key)}</span>
          <span class="sub-note"><code>${esc(key)}</code></span>
          <span class="sub-note">${esc(mode.protocol)}</span>
          <span class="tag unknown">${esc(VENDOR_TIER_LABEL[mode.tier] || (mode.tier ? mode.tier : '档位未标'))}</span>
          <span class="tag ${method === 'manual' ? 'unknown' : 'active'}">${VENDOR_FETCH_LABEL[method]}</span>
        </div>
        <div class="vendor-mode-body">
          <span class="sub-note">${esc(mode.base_url)}</span>
          ${listHtml}
        </div>
      </div>`;
    }).join('');
    return `<div class="card vendor-card">
      <div class="vendor-head">
        <div class="vendor-title">
          <h3>${esc(p.name || p.id)}</h3>
          <span class="sub-note"><code>${esc(p.id)}</code></span>
          <span class="tag ${p.origin === 'overlay' ? 'warn' : 'unknown'}">${p.origin === 'overlay' ? '自定义' : '内置'}</span>
        </div>
        <div class="vendor-actions">
          <button type="button" class="btn small ghost" data-action="edit-vendor" data-id="${esc(p.id)}">编辑</button>
          <button type="button" class="btn small ghost" data-action="maintain-vendor" data-id="${esc(p.id)}">立即维护</button>
          ${p.origin === 'overlay'
            ? `<button type="button" class="btn small ghost" data-action="restore-vendor" data-id="${esc(p.id)}">恢复内置</button>` : ''}
        </div>
      </div>
      ${vendorAccountsHtml(p.id)}
      ${modes || '<p class="sub-note">该厂家没有配置模式。</p>'}
    </div>`;
  }).join('');
}

// --- Account management (vendor page) -------------------------------------
// One vendor's management-plane AK/SK shared by several channels. Optional:
// a vendor with a single key can keep using the vendor-level credential slot.
function ensureAccounts() {
  if (!Array.isArray(state.config.accounts)) state.config.accounts = [];
  return state.config.accounts;
}

function providerCredEnvs(providerId) {
  const p = (state.providers || []).find((x) => x.id === providerId);
  const seen = new Map();
  Object.values((p && p.modes) || {}).forEach((m) => {
    (m.credential_envs || []).forEach((e) => {
      if (e && e.env && !seen.has(e.env)) seen.set(e.env, e);
    });
  });
  return Array.from(seen.values());
}

function vendorAccountsHtml(providerId) {
  const rows = accountsFor(providerId).map((a) => {
    const credCount = Object.keys(a.credentials || {}).length;
    return `<div class="vendor-account">
      <span class="acc-main"><strong>${esc(a.label || a.id)}</strong> <code>${esc(a.id)}</code>
        <span class="tag ${credCount ? 'active' : 'unknown'}">${credCount ? '凭据已配' : '无凭据'}</span>
        ${a.note ? `<small class="sub-note">${esc(a.note)}</small>` : ''}</span>
      <span class="chain-actions">
        <button type="button" class="icon" data-action="edit-account" data-id="${esc(a.id)}" data-provider="${esc(providerId)}">编辑</button>
        <button type="button" class="icon" data-action="delete-account" data-id="${esc(a.id)}" data-provider="${esc(providerId)}">删除</button>
      </span>
    </div>`;
  }).join('');
  return `<div class="vendor-accounts">
    <div class="vendor-accounts-head"><strong>账号档案</strong>
      <button type="button" class="btn small ghost" data-action="add-account" data-provider="${esc(providerId)}">+ 新增账号</button>
    </div>
    ${rows || '<p class="sub-note">未建档。同一 AK/SK 的多条业务线可共用一个账号；单 Key 厂家可跳过。</p>'}
  </div>`;
}

function openAccountEditor(providerId, accountId) {
  const p = (state.providers || []).find((x) => x.id === providerId)
    || { id: providerId, name: providerId };
  const existing = (state.config.accounts || []).find((a) => a && a.id === accountId) || null;
  const credInputs = providerCredEnvs(providerId).map((e) => {
    const prev = (existing && existing.credentials && existing.credentials[e.env]) || '';
    const masked = prev === '***';
    return `<div class="field"><label for="f-acc-${esc(e.env)}">${esc(e.label || e.env)}</label>
      <input id="f-acc-${esc(e.env)}" data-acc-env="${esc(e.env)}" type="text"
        value="${masked ? '' : esc(prev)}" placeholder="${masked ? '已配置，留空保持不变' : esc(e.env)}"
        autocomplete="off" spellcheck="false"></div>`;
  }).join('');
  openModal(existing ? `编辑账号 · ${p.name || providerId}` : `新增账号 · ${p.name || providerId}`, `
    <form id="account-form"><div class="form-grid">
      ${field('账号 id', 'acc-id', existing ? existing.id : '', {
        placeholder: 'volc-main',
        hint: existing ? '账号 id 不可修改' : '字母开头，可含数字 / 下划线 / 横线，≤64 字符' })}
      ${field('显示名', 'acc-label', existing ? (existing.label || '') : (p.name || ''), {
        placeholder: '火山主账号' })}
      ${field('备注', 'acc-note', existing ? (existing.note || '') : '', {
        wide: true, placeholder: '可选' })}
      ${credInputs
        ? `<div class="field wide"><label>管理面凭据（AK/SK）</label>
            <div class="form-grid">${credInputs}</div>
            <small class="hint">只用于余量 / 套餐查询，不是数据面 API Key；保存后回显 ***。</small></div>`
        : '<p class="sub-note wide">该厂家不需要管理面凭据。</p>'}
    </div>
    <div class="modal-foot">
      <button type="button" class="btn ghost" data-account="cancel">取消</button>
      <button type="button" class="btn primary" data-account="save">保存账号</button>
    </div></form>`);
  const f = $('account-form');
  if (existing) f.elements.namedItem('acc-id').disabled = true;
  $('modal').querySelector('[data-account="cancel"]').onclick = closeModal;
  $('modal').querySelector('[data-account="save"]').onclick = () => saveAccount(providerId, existing);
}

function saveAccount(providerId, existing) {
  const f = $('account-form');
  const v = (n) => String(f.elements.namedItem(n)?.value ?? '').trim();
  const id = existing ? existing.id : v('acc-id');
  if (!id) { toast('账号 id 不能为空', 'bad'); return; }
  if (!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(id)) {
    toast('账号 id 需字母开头，仅含字母 / 数字 / 下划线 / 横线，≤64 字符', 'bad'); return;
  }
  const accounts = ensureAccounts();
  if (!existing && accounts.some((a) => a && a.id === id)) {
    toast(`账号 ${id} 已存在`, 'bad'); return;
  }
  const envs = providerCredEnvs(providerId);
  const prevCreds = (existing && existing.credentials) || {};
  const creds = {};
  f.querySelectorAll('[data-acc-env]').forEach((inp) => {
    const env = inp.dataset.accEnv;
    const value = inp.value.trim();
    if (value) creds[env] = value;
    else if (prevCreds[env]) creds[env] = prevCreds[env];
  });
  if (!Object.keys(creds).length && envs.length) {
    toast('至少填写一项管理面凭据（AK/SK）', 'bad'); return;
  }
  const entry = { id, provider: providerId, label: v('acc-label') || id };
  // Vendors without a management plane keep whatever credentials they had
  // (free-form escape hatch) instead of silently dropping them on edit.
  const finalCreds = envs.length ? creds : { ...prevCreds, ...creds };
  if (Object.keys(finalCreds).length) entry.credentials = finalCreds;
  const note = v('acc-note');
  if (note) entry.note = note;
  if (existing) Object.assign(existing, entry);
  else accounts.push(entry);
  markDirty();
  closeModal();
  renderVendors();
  renderChannels();
  toast(`账号 ${id} 已${existing ? '更新' : '新增'}`);
}

async function deleteAccount(accountId) {
  const refs = [];
  (state.config.channels || []).forEach((c) => {
    if (c && c.account === accountId) refs.push(`渠道「${c.name}」`);
  });
  ((state.subs && state.subs.subscriptions) || []).forEach((s) => {
    if (s && s.account_id === accountId) refs.push(`订阅「${s.plan || s.id}」`);
  });
  const msg = refs.length
    ? `以下对象正在引用该账号：${refs.join('、')}。删除后它们回退到渠道自带凭据，确认删除？`
    : '确认删除该账号？';
  if (!await askConfirm('删除账号', msg)) return;
  state.config.accounts = (state.config.accounts || []).filter((a) => a && a.id !== accountId);
  markDirty();
  renderVendors();
  renderChannels();
  toast(`账号 ${accountId} 已删除`);
}

async function maintainVendor(id) {
  try {
    const report = await api('/api/providers/maintain', {
      method: 'POST', body: id ? { id } : {},
    });
    const applied = (report.applied || []).length;
    const alerts = report.alerts || [];
    const mode = report.extraction === 'llm' ? 'LLM 提取' : '规则提取';
    let summary = applied ? `已应用 ${applied} 项` : '模型清单无变化';
    if (alerts.length) summary += `，警报 ${alerts.length} 条`;
    toast(`维护完成（${mode}）：` + summary, alerts.length ? 'warn' : 'ok');
    if (report.extraction !== 'llm') {
      toast('没有可用的 LLM 维护模型（未配置 maintenance_model 且队列空/探测失败），'
        + '本次为规则提取；可点「测试维护模型」探测免费模型', 'warn');
    }
    alerts.forEach((a) => toast(a.message, 'warn'));
    await loadProviders();
    renderVendors();
  } catch (err) { toast(err.message, 'bad'); }
}

async function restoreVendor(id) {
  const p = (state.providers || []).find((x) => x.id === id);
  if (!(await askConfirm('恢复内置', `删除厂家「${p?.name || id}」的覆盖条目，恢复为内置注册表？`))) return;
  try {
    await api('/api/providers/' + encodeURIComponent(id), { method: 'DELETE' });
    await loadProviders();
    renderVendors();
    toast(`已恢复厂家 ${id} 为内置定义`);
  } catch (err) { toast(err.message, 'bad'); }
}

function vendorModeFieldset(index, modeKey, mode) {
  const m = mode || { label: '', protocol: 'openai', base_url: '', tier: '',
    docs_url: '', models_url: '', models: [] };
  const method = vendorFetchMethod(m);
  const opt = (v, cur, text) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${text}</option>`;
  return `<fieldset class="vendor-mode-fieldset" data-index="${index}">
    <legend>模式 ${index + 1}
      <button type="button" class="btn small ghost" data-remove-mode="${index}">删除该模式</button>
    </legend>
    <div class="form-grid">
      <div class="field"><label for="f-mode-key-${index}">模式 key</label>
        <select id="f-mode-key-${index}" name="mode-key">
          ${VENDOR_MODE_KEYS.map((k) => opt(k, modeKey, k)).join('')}
        </select></div>
      ${field('显示名', `mode-label-${index}`, m.label, { placeholder: '如 coding 月度套餐' })}
      <div class="field"><label for="f-mode-protocol-${index}">协议</label>
        <select id="f-mode-protocol-${index}" name="mode-protocol">
          ${opt('openai', m.protocol, 'openai')}${opt('anthropic', m.protocol, 'anthropic')}
        </select></div>
      ${field('base_url', `mode-base-${index}`, m.base_url, { wide: true, placeholder: 'https://api.example.com/v1' })}
      <div class="field"><label for="f-mode-tier-${index}">档位</label>
        <select id="f-mode-tier-${index}" name="mode-tier">
          ${opt('', m.tier || '', '自动推断（按订阅）')}${opt('free', m.tier, '免费')}${opt('paid', m.tier, '收费')}
        </select></div>
      <div class="field"><label for="f-mode-method-${index}">获取方式</label>
        <select id="f-mode-method-${index}" name="mode-method">
          ${FETCH_METHOD_CHOICES.map(([v, text]) => opt(v, method, text)).join('')}
        </select></div>
      ${field('文档 URL（文档提取用）', `mode-docs-${index}`, m.docs_url || '', { wide: true, placeholder: 'https://docs.example.com/pricing' })}
      ${field('模型接口 URL（接口拉取用）', `mode-models-${index}`, m.models_url || '', { wide: true, placeholder: 'https://api.example.com/v1/models', hint: '同厂家已配置渠道时自动附带其 API Key；百炼类分页接口会自动翻页' })}
      ${field('模型清单（每行一个；自动维护成功后会覆盖）', `mode-model-list-${index}`, (m.models || []).join('\n'), { wide: true, textarea: true, rows: 4 })}
    </div>
  </fieldset>`;
}

const FETCH_METHOD_CHOICES = [
  ['docs', '文档提取（docs_url）'],
  ['api', '接口拉取（models_url）'],
  ['manual', '手动维护'],
];

function openVendorEditor(id) {
  const existing = id ? (state.providers || []).find((x) => x.id === id) : null;
  const draft = existing ? JSON.parse(JSON.stringify(existing)) : {
    id: '', name: '',
    modes: { payg: { label: '', protocol: 'openai', base_url: '', tier: '',
      docs_url: '', models_url: '', models: [] } },
  };
  const entries = Object.entries(draft.modes || {});
  openModal(existing ? '编辑厂家 · ' + (existing.name || id) : '新增厂家', `
    <form id="vendor-form">
      <div class="form-grid">
        ${field('厂家 id（小写字母、数字、连字符）', 'vendor-id', draft.id,
          { placeholder: 'zhipu', hint: '保存后不可修改' })}
        ${field('名称', 'vendor-name', draft.name, { placeholder: '智谱' })}
      </div>
      <div id="vendor-mode-list">${entries.map(([k, m], i) =>
        vendorModeFieldset(i, k, m)).join('')}</div>
      <div class="vendor-mode-foot">
        <button type="button" class="btn ghost" id="vendor-add-mode">+ 添加模式</button>
        <small class="hint">模式 key 限定 coding / agent / payg，每个厂家每种模式最多一个。</small>
      </div>
      <div class="modal-foot">
        <button type="button" class="btn ghost" id="vendor-cancel">取消</button>
        <button type="submit" class="btn primary">保存厂家</button>
      </div>
    </form>`);
  $('vendor-cancel').onclick = closeModal;
  if (existing) $('f-vendor-id').disabled = true;  // id is immutable once saved

  const list = $('vendor-mode-list');
  const bindRemoveButtons = () => {
    list.querySelectorAll('button[data-remove-mode]').forEach((b) => {
      b.onclick = () => b.closest('.vendor-mode-fieldset').remove();
    });
  };
  $('vendor-add-mode').onclick = () => {
    const count = list.querySelectorAll('.vendor-mode-fieldset').length;
    if (count >= VENDOR_MODE_KEYS.length) return toast('每个厂家最多 ' + VENDOR_MODE_KEYS.length + ' 个模式', 'warn');
    list.insertAdjacentHTML('beforeend', vendorModeFieldset(count, 'coding', null));
    bindRemoveButtons();
  };
  bindRemoveButtons();

  $('vendor-form').onsubmit = async (e) => {
    e.preventDefault();
    const vendorId = (existing ? existing.id : $('f-vendor-id').value.trim()).toLowerCase();
    const name = $('f-vendor-name').value.trim();
    if (!vendorId) return toast('厂家 id 必填', 'bad');
    if (!/^[a-z0-9-]+$/.test(vendorId)) return toast('厂家 id 只能包含小写字母、数字和连字符', 'bad');
    if (!name) return toast('厂家名称必填', 'bad');
    const modes = {};
    let bad = null;
    list.querySelectorAll('.vendor-mode-fieldset').forEach((fs) => {
      if (bad) return;
      const idx = fs.dataset.index;
      const q = (suffix) => (fs.querySelector(`[name="${suffix}-${idx}"]`)?.value ?? '').trim();
      const key = fs.querySelector('[name="mode-key"]').value;
      if (modes[key]) { bad = `模式 key 重复：${key}`; return; }
      const label = q('mode-label');
      const base = q('mode-base');
      if (!label) { bad = `模式 ${key} 的显示名必填`; return; }
      if (!base) { bad = `模式 ${key} 的 base_url 必填`; return; }
      const method = fs.querySelector('[name="mode-method"]').value;
      let docsUrl = q('mode-docs');
      let modelsUrl = q('mode-models');
      if (method === 'docs' && !docsUrl) { bad = `模式 ${key} 选择了文档提取但未填文档 URL`; return; }
      if (method === 'api' && !modelsUrl) { bad = `模式 ${key} 选择了接口拉取但未填模型接口 URL`; return; }
      if (method === 'manual' && (docsUrl || modelsUrl)) {
        bad = `模式 ${key} 选择了手动维护但仍填了 URL，请改选获取方式或清空 URL`;
        return;
      }
      if (method === 'manual') { docsUrl = ''; modelsUrl = ''; }
      modes[key] = {
        label,
        protocol: fs.querySelector('[name="mode-protocol"]').value,
        base_url: base,
        tier: fs.querySelector('[name="mode-tier"]').value,
        docs_url: docsUrl,
        models_url: modelsUrl,
        models: q('mode-model-list').split('\n').map((s) => s.trim()).filter(Boolean),
      };
    });
    if (bad) return toast(bad, 'bad');
    if (!Object.keys(modes).length) return toast('厂家至少需要配置一个模式', 'bad');
    try {
      const result = await api('/api/providers', {
        method: 'POST', body: { id: vendorId, name, modes },
      });
      closeModal();
      await loadProviders();
      renderVendors();
      toast(`已保存厂家 ${result.provider?.name || vendorId}`);
    } catch (err) { toast(err.message, 'bad'); }
  };
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
  const cacheRows = (usage.channels || []).filter((c) => c.input_tokens);
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
    <div class="chart-block" style="margin-top:14px">
      <h4>渠道缓存命中率（缓存输入 / 总输入，近 ${usage.range} 天）</h4>
      ${cacheRows.length ? cacheRows.slice(0, 6).map((c) => {
        const rate = (c.cached_tokens / c.input_tokens * 100).toFixed(1);
        return `<div class="dist-row cache">
          <span>${esc(c.name)}</span>
          <div class="dist-bar"><span style="width:${rate}%"></span></div>
          <span class="dist-value">${rate}%（${fmtTok(c.cached_tokens)} / ${fmtTok(c.input_tokens)} tok）</span>
        </div>`;
      }).join('') : '<div class="chart-empty">上游未上报缓存字段（prompt_tokens_details.cached_tokens / cache_read_input_tokens），暂无数据</div>'}
    </div>
    <p class="hint">${esc(usage.note || '')}</p>`;
  renderUsageModels(usage);
}

// Every (channel, model) pair seen in the usage window, free and paid alike.
// Tier comes from the backend: a manual per-model marking wins over the
// channel default, so a free channel can still host a paid model.
function renderUsageModels(usage) {
  const body = $('usage-models-body');
  const rows = (usage && usage.models) || [];
  const note = $('usage-models-note');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="7" class="sub-note">暂无模型调用记录，产生请求后自动累计。</td></tr>';
    if (note) note.textContent = '按「渠道 × 模型」统计全部模型，含免费与收费';
    return;
  }
  const free = rows.filter((r) => r.tier === 'free').length;
  const paid = rows.filter((r) => r.tier === 'paid').length;
  if (note) {
    note.textContent = `近 ${usage.range} 天共 ${rows.length} 个「渠道 × 模型」组合`
      + `（免费 ${free} · 收费 ${paid}`
      + (rows.length - free - paid > 0 ? ` · 未标注 ${rows.length - free - paid}` : '') + '）';
  }
  const query = (state.usageFilter || '').trim().toLowerCase();
  const filtered = query
    ? rows.filter((r) => (r.model + ' ' + r.channel).toLowerCase().includes(query))
    : rows;
  if (!filtered.length) {
    body.innerHTML = '<tr><td colspan="7" class="sub-note">没有匹配的模型。</td></tr>';
    return;
  }
  body.innerHTML = filtered.map((r) => {
    const cacheRate = r.input_tokens
      ? `${(r.cached_tokens / r.input_tokens * 100).toFixed(1)}%`
        + ` <span class="sub-note">(${Number(r.cached_tokens).toLocaleString()} / ${Number(r.input_tokens).toLocaleString()})</span>`
      : '<span class="sub-note">上游未上报</span>';
    const last = r.last_used
      ? new Date(r.last_used * 1000).toLocaleString() : '—';
    return `<tr>
      <td><code>${esc(r.model)}</code></td>
      <td>${esc(r.channel)}</td>
      <td>${tierBadge(r.tier) || '<span class="sub-note">未标注</span>'}</td>
      <td>${r.requests} / ${r.failed}</td>
      <td>${Number(r.tokens || 0).toLocaleString()}</td>
      <td>${cacheRate}</td>
      <td>${last}</td>
    </tr>`;
  }).join('');
}

async function loadNotifications() {
  state.notify = await api('/api/notifications');
  renderNotifications();
}

// ---------------------------------------------------------------- members
// Per-member usage only; the keys themselves live on the "API Keys" page.

async function loadMembers(range) {
  if (range) state.memberRange = range;
  const usage = await api('/api/usage/users/' + state.memberRange);
  document.querySelectorAll('[data-member-range]').forEach((b) =>
    b.classList.toggle('active', Number(b.dataset.memberRange) === state.memberRange));
  renderMemberUsage(usage);
}

async function loadApiKeys() {
  renderApiKeys(await api('/api/keys'));
}

function renderApiKeys(payload) {
  const master = (payload && payload.master) || '';
  const enabled = payload ? payload.master_enabled !== false : true;
  const masterEl = $('master-key-value');
  masterEl.textContent = master || '未设置（当前网关不校验主密钥）';
  masterEl.dataset.copy = master;
  masterEl.dataset.enabled = enabled && master ? '1' : '0';
  masterEl.onclick = () => { if (master) copyText(master); };

  const statusEl = $('master-key-status');
  const toggleEl = $('btn-toggle-master');
  if (!master) {
    statusEl.textContent = '未设置';
    statusEl.className = 'tag unknown';
    toggleEl.textContent = '暂停主密钥';
    toggleEl.disabled = true;
  } else if (enabled) {
    statusEl.textContent = '启用';
    statusEl.className = 'tag active';
    toggleEl.textContent = '暂停主密钥';
    toggleEl.disabled = false;
  } else {
    statusEl.textContent = '已暂停';
    statusEl.className = 'tag unknown';
    toggleEl.textContent = '启用主密钥';
    toggleEl.disabled = false;
  }

  const rows = (payload && payload.keys) || [];
  const body = $('api-keys-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5" class="sub-note">还没有 API Key。点右上角「生成新 Key」即可签发。</td></tr>';
    return;
  }
  body.innerHTML = rows.map((r) => {
    const when = r.created ? new Date(r.created * 1000).toLocaleString() : '—';
    const active = r.enabled !== false;
    return `<tr>
      <td>${esc(r.name)}</td>
      <td><code data-copy="${esc(r.key)}" title="点击复制">${esc(r.key)}</code></td>
      <td>${active ? '<span class="tag active">启用</span>' : '<span class="tag unknown">已暂停</span>'}</td>
      <td>${when}</td>
      <td>${active
        ? `<button class="btn small ghost" data-pause="${esc(r.key)}">暂停</button>`
        : `<button class="btn small" data-enable="${esc(r.key)}">启用</button>`}
        <button class="btn small ghost" data-delete="${esc(r.key)}">删除</button></td>
    </tr>`;
  }).join('');
  body.querySelectorAll('[data-copy]').forEach((el) => {
    el.onclick = () => copyText(el.dataset.copy);
  });
  const bindAction = (selector, datasetKey, path, doneText, confirmText) => {
    body.querySelectorAll(selector).forEach((button) => {
      button.onclick = async () => {
        if (confirmText && !(await askConfirm('确认操作', confirmText))) return;
        try {
          await api(path, { method: 'POST', body: { key: button.dataset[datasetKey] } });
          toast(doneText);
          loadApiKeys().catch(() => {});
        } catch (e) { toast(e.message, 'bad'); }
      };
    });
  };
  bindAction('[data-pause]', 'pause', '/api/keys/pause', '已暂停', '暂停后该 Key 立即失效，可随时重新启用，确定？');
  bindAction('[data-enable]', 'enable', '/api/keys/enable', '已启用');
  bindAction('[data-delete]', 'delete', '/api/keys/delete', '已删除', '删除后不可恢复，确定删除这个 Key？');
}

function renderMemberUsage(usage) {
  const body = $('member-usage-body');
  const rows = (usage && usage.users) || [];
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="4" class="sub-note">暂无按人统计。成员开始用各自的 key 调用后自动累计。</td></tr>';
    return;
  }
  body.innerHTML = rows.map((r) => {
    const rate = r.requests ? ((r.requests - r.failed) / r.requests * 100).toFixed(1) + '%' : '—';
    return `<tr>
      <td>${esc(r.name || '匿名')}</td>
      <td>${r.requests} / ${r.failed}</td>
      <td>${Number(r.tokens || 0).toLocaleString()}</td>
      <td>${rate}</td>
    </tr>`;
  }).join('');
}

// Browser-agent logins: GET returns hosts with masked flags only (the console
// never receives a password or cookie back), POST upserts by host.
async function loadCuCredentials() {
  const data = await api('/api/cu-credentials');
  renderCuCredentials(data);
}

function renderCuCredentials(data) {
  const body = $('cu-body');
  const hosts = (data && data.hosts) || [];
  if (!hosts.length) {
    body.innerHTML = '<tr><td colspan="5" class="sub-note">还没有保存任何站点凭据。在下方表单填入站点与凭据后点「保存」。</td></tr>';
    return;
  }
  body.innerHTML = hosts.map((h) => `<tr>
      <td><code>${esc(h.host)}</code></td>
      <td>${esc(h.username || '—')}</td>
      <td>${h.has_password ? '<span class="tag active">已保存</span>' : '<span class="sub-note">—</span>'}</td>
      <td>${h.has_cookie ? '<span class="tag active">已保存</span>' : '<span class="sub-note">—</span>'}</td>
      <td><button class="btn small ghost" data-cu-remove="${esc(h.host)}">删除</button></td>
    </tr>`).join('');
  body.querySelectorAll('[data-cu-remove]').forEach((el) => {
    el.onclick = () => removeCuCredential(el.dataset.cuRemove);
  });
}

async function removeCuCredential(host) {
  if (!await askConfirm('删除站点凭据', `确认删除 ${host} 的浏览器代理凭据？`)) return;
  try {
    await api('/api/cu-credentials', { method: 'POST', body: { host, remove: true } });
    toast(`已删除 ${host} 的凭据`);
    await loadCuCredentials();
  } catch (e) { toast(e.message, 'bad'); }
}

async function saveCuCredential(event) {
  event.preventDefault();
  const f = $('cu-form');
  const v = (n) => String(f.elements.namedItem(n)?.value ?? '').trim();
  const host = v('cu-host');
  if (!host) { toast('站点不能为空', 'bad'); return; }
  const payload = { host };
  const username = v('cu-username');
  const password = String(f.elements.namedItem('cu-password')?.value ?? '');
  const cookie = v('cu-cookie');
  // Empty fields are omitted: the backend keeps the stored value unchanged.
  if (username) payload.username = username;
  if (password) payload.password = password;
  if (cookie) payload.cookie = cookie;
  try {
    await api('/api/cu-credentials', { method: 'POST', body: payload });
    toast(`已保存 ${host} 的凭据`);
    f.reset();
    await loadCuCredentials();
  } catch (e) { toast(e.message, 'bad'); }
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

// ---- 模型接入：名字落点可视化、链内展开、最近请求联动 ----

const FREE_CHANNEL_MACRO = '$free';
const REQUEST_LABELS = {
  ok: '成功', upstream_error: '上游错误', network_error: '网络错误',
  no_candidate: '无可用渠道', rejected: '模型被拒绝', local: '本地估算',
};

function requestTag(status) {
  const cls = status === 'ok' || status === 'local' ? 'active'
    : (status === 'upstream_error' || status === 'network_error' || status === 'rejected')
      ? 'expired' : 'unknown';
  return `<span class="tag ${cls}">${esc(REQUEST_LABELS[status] || status || '—')}</span>`;
}

// Cooldown countdown for display: 95 -> "1分35秒".
function fmtDur(seconds) {
  const s = Math.max(1, Math.round(seconds));
  if (s < 60) return s + '秒';
  const m = Math.floor(s / 60);
  if (m < 60) return m + '分' + (s % 60 ? (s % 60) + '秒' : '');
  const h = Math.floor(m / 60);
  return h + '小时' + (m % 60 ? (m % 60) + '分' : '');
}

// Mirror of the server-side "$free" route macro: every enabled tier="free"
// channel speaking the route's protocol joins the chain unless named already.
// The console cannot re-derive tier from subscriptions, so the channel's raw
// config tier is the closest available approximation.
function expandedChain(route) {
  const raw = (route && route.channels) || [];
  const listed = raw.filter((x) => x && x !== FREE_CHANNEL_MACRO);
  const out = listed.map((name) => ({ name, viaMacro: false }));
  if (!raw.includes(FREE_CHANNEL_MACRO)) return out;
  (state.config.channels || []).forEach((c) => {
    if (c.protocol !== route.protocol) return;
    if (c.tier !== 'free' || c.enabled === false) return;
    if (listed.includes(c.name)) return;
    out.push({ name: c.name, viaMacro: true });
  });
  return out;
}

function channelEntry(name) {
  return (state.config.channels || []).find((c) => c.name === name);
}

function channelSnap(name) {
  return (state.snapshot?.channels || []).find((c) => c.name === name);
}

// The channel's auto target lives inside the models mapping — /api/config
// exposes no top-level `auto` field, so reading ch.auto always yields
// undefined and made every chain look like it had no ready landing spot.
function channelAuto(ch) {
  return (ch && (ch.models || {}).auto) || '';
}

function modelStateOf(exposed) {
  return (state.snapshot?.model_states || []).find((m) => m.model === exposed);
}

// Health of one (channel, wire model) pair. Cooldowns are tracked per wire
// model (ChannelState.model_cooldown_until), channel cooling above that.
function pairStatus(ch, snap, wire) {
  if (ch.enabled === false) return { cls: 'off', text: '已停用' };
  if (snap && snap.cooling) return { cls: 'warn', text: '渠道冷却 ' + fmtDur(snap.cooling_seconds || 0) };
  const cd = snap && snap.model_cooldowns ? snap.model_cooldowns[wire] : 0;
  if (cd > 0) return { cls: 'warn', text: '模型冷却 ' + fmtDur(cd) };
  return { cls: 'active', text: '可用' };
}

function pairReady(ch, snap, wire) {
  return !!ch && ch.enabled !== false && !!wire
    && !(snap && (snap.cooling || (snap.model_cooldowns && snap.model_cooldowns[wire] > 0)));
}

// Where the next request for this brand lands right now: first ready channel
// in the chain, dispatched to its auto model.
function currentLanding(route) {
  if (!route) return { cls: 'off', text: '链不存在' };
  const nodes = expandedChain(route);
  if (!nodes.length) return { cls: 'off', text: '链为空' };
  for (const node of nodes) {
    const ch = channelEntry(node.name);
    const snap = channelSnap(node.name);
    const auto = channelAuto(ch);
    if (pairReady(ch, snap, auto)) {
      return { cls: 'active', text: node.name + ' · ' + auto };
    }
  }
  return { cls: 'warn', text: '链内暂无可用落点' };
}

// Expandable detail under a brand row: one node per chain channel, listing
// its auto mapping and every explicit model pair with live health.
function chainDetail(route) {
  const nodes = expandedChain(route);
  if (!nodes.length) {
    return '<div class="chain-node"><div class="chain-node-head">链为空</div></div>';
  }
  return nodes.map((node, i) => {
    const ch = channelEntry(node.name);
    const snap = channelSnap(node.name);
    const dead = !ch || ch.enabled === false;
    const pairs = [];
    const auto = channelAuto(ch);
    if (auto) {
      const st = pairStatus(ch, snap, auto);
      pairs.push(`<li><span class="tag mixed">auto</span><code>${esc(auto)}</code><span class="tag ${st.cls}">${esc(st.text)}</span></li>`);
    }
    Object.entries((ch && ch.models) || {}).forEach(([exposed, wire]) => {
      if (exposed === 'auto' || exposed === '*') return;
      const st = pairStatus(ch, snap, wire);
      const ms = modelStateOf(exposed);
      const label = ms ? `<span class="tag ${STATUS_CLASS[ms.status] || ''}">${esc(ms.label)}</span>` : '';
      pairs.push(`<li><code>${esc(exposed)}</code><span class="sub-note">→</span><code>${esc(wire)}</code>${label}<span class="tag ${st.cls}">${esc(st.text)}</span></li>`);
    });
    const head = `<div class="chain-node-head"><span class="order">${i + 1}</span><strong>${esc(node.name)}</strong>`
      + (node.viaMacro ? '<span class="tag mixed">$free</span>' : '')
      + (ch ? `<span class="sub-note">${esc(ch.protocol || '')}</span>` : '<span class="tag off">未定义</span>')
      + (dead ? '<span class="tag off">已停用</span>' : '')
      + (snap && snap.cooling ? `<span class="tag warn">渠道冷却 ${esc(fmtDur(snap.cooling_seconds || 0))}</span>` : '')
      + '</div>';
    const body = pairs.length ? pairs.join('') : '<li class="sub-note">该渠道没有声明任何模型映射</li>';
    return `<div class="chain-node">${head}<ul class="chain-models">${body}</ul></div>`;
  }).join('');
}

// Expanded panels survive re-renders (typing in the policy box or a state
// refresh re-renders rows); keyed by brand name, not row index.
function brandPanelState(name) {
  if (!state.brandPanels) state.brandPanels = {};
  return state.brandPanels[name] || (state.brandPanels[name] = { chain: false, recent: false });
}

function brandRouteOf(name) {
  const chain = ((state.config.models || {}).branded || {})[name];
  return (state.config.routes || []).find((r) => r.name === chain) || null;
}

function brandRecentHtml(name) {
  const entries = (state.recentEntries || []).filter((e) => e.model === name).slice(0, 8);
  if (!entries.length) {
    return '<ul class="recent-requests"><li class="sub-note">最近 200 条请求里没有这个模型名的调用</li></ul>';
  }
  return '<ul class="recent-requests">' + entries.map((e) => {
    const at = logTime(e.ts);
    const dest = e.channel
      ? esc(e.channel) + (e.wire_model ? ' <span class="sub-note">·</span> ' + esc(e.wire_model) : '')
      : '—';
    return `<li><span class="sub-note">${esc(at.date)} ${esc(at.time)}</span>${requestTag(e.status)}`
      + `<span>${dest}</span><span class="sub-note">${esc(e.mode || '')} · ${esc(e.tokens ?? 0)} tok · ${esc(e.ms ?? 0)} ms</span></li>`;
  }).join('') + '</ul>';
}

function renderBrandList() {
  if (!state.config) return;
  const branded = (state.config.models && state.config.models.branded) || {};
  const names = Object.keys(branded);
  $('brand-list').innerHTML = names.length ? names.map((name) => {
    const st = brandPanelState(name);
    const route = brandRouteOf(name);
    const landing = currentLanding(route);
    const chainBody = st.chain ? chainDetail(route) : '';
    const recentBody = st.recent ? brandRecentHtml(name) : '';
    return `<div class="model-row brand-block">
      <div class="brand-head">
        <code>${esc(name)}</code>
        <span class="sub-note">→ ${esc(branded[name])} 链 · 内部调度</span>
        ${route ? `<span class="tag ${landing.cls}" title="按当前健康度推断的下一个落点">${esc(landing.text)}</span>`
          : '<span class="tag off">链不存在</span>'}
        <span class="brand-actions">
          <button class="btn small ghost" data-copy="${esc(name)}">复制</button>
          <button class="btn small ghost" data-chain-toggle="${esc(name)}">${st.chain ? '收起链内' : '链内模型'}</button>
          <button class="btn small ghost" data-recent-toggle="${esc(name)}">${st.recent ? '收起日志' : '最近请求'}</button>
        </span>
      </div>
      <div class="brand-panel chain" ${st.chain ? '' : 'hidden'}>${chainBody}</div>
      <div class="brand-panel recent" ${st.recent ? '' : 'hidden'}>${recentBody}</div>
    </div>`;
  }).join('') : '<div class="chart-empty">还没有自有模型名</div>';
}

function toggleChain(button) {
  const name = button.dataset.chainToggle;
  const st = brandPanelState(name);
  st.chain = !st.chain;
  const row = button.closest('.model-row');
  const panel = row && row.querySelector('.brand-panel.chain');
  if (!panel) return;
  if (st.chain) panel.innerHTML = chainDetail(brandRouteOf(name));
  panel.hidden = !st.chain;
  button.textContent = st.chain ? '收起链内' : '链内模型';
}

async function toggleRecent(button) {
  const name = button.dataset.recentToggle;
  const st = brandPanelState(name);
  st.recent = !st.recent;
  const row = button.closest('.model-row');
  const panel = row && row.querySelector('.brand-panel.recent');
  if (!panel) return;
  button.textContent = st.recent ? '收起日志' : '最近请求';
  if (!st.recent) { panel.hidden = true; return; }
  panel.hidden = false;
  if (!state.recentEntries) {
    panel.innerHTML = '<ul class="recent-requests"><li class="sub-note">加载中…</li></ul>';
    try {
      state.recentEntries = (await api('/api/requests/200')).entries || [];
    } catch (err) {
      panel.innerHTML = `<ul class="recent-requests"><li class="sub-note">加载失败：${esc(err.message)}</li></ul>`;
      return;
    }
  }
  panel.innerHTML = brandRecentHtml(name);
}

// Longer audit tail for the models page: powers both the per-brand "最近请求"
// panels and the fallback hint below the policy editor.
async function refreshModelInsights() {
  const data = await api('/api/requests/200');
  state.recentEntries = data.entries || [];
  renderBrandList();
  renderFallbackHint();
}

function renderFallbackHint() {
  const box = $('fallback-hint');
  if (!box) return;
  const fallbacks = (state.recentEntries || []).filter((e) => e.mode === 'fallback');
  if (!fallbacks.length) { box.hidden = true; box.innerHTML = ''; return; }
  const names = [...new Set(fallbacks.map((e) => e.model).filter(Boolean))];
  box.hidden = false;
  box.innerHTML = `最近 ${fallbacks.length} 次请求是未知模型名走了兜底调度（${esc(names.slice(0, 6).join('、'))}${names.length > 6 ? ' 等' : ''}）。如果是固定使用的名字，建议在上面固化为自有模型名；明细见 `
    + '<button class="btn small ghost" data-goto-tab="requests">请求日志</button>。';
}

const STATUS_CLASS = { free_ok: 'free', free_exhausted: 'warn', paid_only: 'paid', available: 'mixed', no_channel: 'off' };

function renderExactModels() {
  const filter = state.modelStatusFilter || 'all';
  const rows = (state.snapshot?.model_states || [])
    .filter((row) => filter === 'all' || row.status === filter)
    .map((row) => {
      const tag = `<span class="tag ${STATUS_CLASS[row.status] || ''}">${esc(row.label)}</span>`;
      const detail = row.channels.map((ch) => {
        const flag = ch.enabled === false ? '（已停用）' : (ch.cooling ? '（冷却中）' : '');
        return `${esc(ch.name)}${flag}${scoreBadge(ch.score)}`;
      }).join('、');
      return `<div class="model-row"><code>${esc(row.model)}</code>${tag}<span class="sub-note">${detail || '无可用渠道'}</span></div>`;
    });
  $('exact-models').innerHTML = rows.length
    ? rows.join('') : '<div class="chart-empty">还没有显式模型映射，任意名字都会内部调度</div>';
}

// Ready-to-paste per-tool snippets. The gateway base URL is the console's
// origin; the key is never echoed, so "<API Key>" stays a placeholder.
const ACCESS_TOOLS = {
  'claude-code': (model, base) => [
    '# Claude Code（settings.json 的 env 段，或系统环境变量）',
    'ANTHROPIC_BASE_URL=' + base,
    'ANTHROPIC_AUTH_TOKEN=<API Key>',
    'ANTHROPIC_MODEL=' + model,
    'ANTHROPIC_SMALL_FAST_MODEL=' + model,
  ].join('\n'),
  'codex': (model, base) => [
    '# ~/.codex/config.toml',
    'model = "' + model + '"',
    'model_provider = "facet"',
    '',
    '[model_providers.facet]',
    'name = "Facet"',
    'base_url = "' + base + '/v1"',
    'wire_api = "chat"',
    'env_key = "PLANROUTER_API_KEY"',
    '',
    '# 另在环境变量中设置：',
    '# export PLANROUTER_API_KEY=<API Key>',
  ].join('\n'),
  'cline': (model, base) => [
    'Cline / VS Code 插件设置（API Provider 选 OpenAI Compatible）：',
    'Base URL: ' + base + '/v1',
    'API Key:  <API Key>',
    'Model ID: ' + model,
  ].join('\n'),
  'dsh': (model, base) => [
    '# ~/.dsh/profiles/<profile>/cordis.patch.yml（顶层 YAML 数组）',
    '- id: agent-default-model',
    '  config:',
    '    provider: facet',
    '    model: ' + model,
    '- id: llm-pi-ai',
    '  config:',
    '    providers:',
    '      facet:',
    '        apiKeyEnv: ROUTER_API_KEY',
    '        api: openai-completions',
    '        baseURL: ' + base + '/v1',
    '        compat:',
    '          supportsDeveloperRole: false',
    '          maxTokensField: max_tokens',
    '        models:',
    '          - id: ' + model,
    '',
    '# 另在环境变量中设置 ROUTER_API_KEY=<API Key>',
    '# 建 profile：dsh --profile facet-web --from-default-profile web',
  ].join('\n'),
  'trae': (model, base) => [
    'Trae（设置 → AI/LLM → 添加提供方，类型选 OpenAI/Custom Model）：',
    'API Base: ' + base + '/v1',
    'API Key:  <API Key>',
    '模型名:   ' + model,
    '',
    '# 注意：Agent 模式依赖 tool calling，免费上游模型可能报 400，',
    '# 此时换支持函数调用的模型或走付费渠道。',
  ].join('\n'),
  'opencode': (model, base) => [
    '// opencode.json（项目根目录，或 ~/.config/opencode/opencode.json）',
    JSON.stringify({
      '$schema': 'https://opencode.ai/config.json',
      'providers': {
        'facet': {
          'name': 'Facet',
          'env': ['ROUTER_API_KEY'],
          'package': '@opencode/ai/providers/openai-compatible',
          'settings': { 'baseURL': base + '/v1' },
          'models': { [model]: { 'name': model } },
        },
      },
    }, null, 2),
    '// 另设置环境变量 ROUTER_API_KEY=<API Key>，会话内 /models 选择模型',
  ].join('\n'),
  'codebuddy': (model, base) => [
    '// ~/.codebuddy/models.json（用户级）或项目根 .codebuddy/models.json（项目级）',
    JSON.stringify({
      'models': [{
        'id': model,
        'name': model + ' (Facet)',
        'vendor': 'Custom',
        'url': base + '/v1',
        'apiKey': '<API Key>',
        'supportsToolCall': true,
      }],
      'availableModels': [model],
    }, null, 2),
    '// 启动：codebuddy --model ' + model + '，或会话内 /model 切换',
  ].join('\n'),
  'openai': (model, base) => [
    'base_url = "' + base + '/v1"',
    'api_key  = "<API Key>"',
    'model    = "' + model + '"',
  ].join('\n'),
  'anthropic': (model, base) => [
    'base_url = "' + base + '"   # Anthropic 协议用根路径（/v1/messages）',
    'api_key  = "<API Key>"      # x-api-key 请求头',
    'model    = "' + model + '"',
  ].join('\n'),
};

function renderAccessSnippet() {
  const box = $('access-snippet');
  if (!box) return;
  const tool = $('access-tool').value;
  const model = $('access-model').value || 'auto';
  const build = ACCESS_TOOLS[tool] || ACCESS_TOOLS.openai;
  box.value = build(model, location.origin);
}

function fillAccessModels() {
  const sel = $('access-model');
  if (!sel) return;
  const names = Object.keys(((state.config.models || {}).branded) || {});
  const prev = sel.value;
  sel.innerHTML = (names.length ? names : ['auto'])
    .map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  if (names.includes(prev)) sel.value = prev;
}

function renderModels() {
  const p = state.config.models || {};
  $('set-branded').value = Object.entries(p.branded || {})
    .map(([name, hint]) => name + '=' + hint).join('\n');
  $('set-fallback').checked = p.fallback_unknown !== false;
  $('set-fallback-route').value = p.fallback_route || 'default';
  renderPolicyErrors([]);
  renderBrandList();
  renderExactModels();
  fillAccessModels();
  renderAccessSnippet();
}

function renderPolicyErrors(errors) {
  const box = $('policy-errors');
  if (!box) return;
  if (!errors.length) { box.hidden = true; box.innerHTML = ''; return; }
  box.hidden = false;
  box.innerHTML = errors.map((e) => `<div>${esc(e)}</div>`).join('');
}

// Strict "name=route" parsing for the policy box: unlike the old free-form
// parser, a line without "=" is an error, not a silent "default" mapping.
function parseBrandedLines(text, errors) {
  const out = {};
  text.split('\n').forEach((line, idx) => {
    const s = line.trim();
    if (!s) return;
    const i = s.indexOf('=');
    const name = i > 0 ? s.slice(0, i).trim() : '';
    const chain = i > 0 ? s.slice(i + 1).trim() : '';
    if (!name || !chain) {
      errors.push(`第 ${idx + 1} 行「${s}」格式应为：模型名=路由链名`);
      return;
    }
    if (out[name] && out[name] !== chain) {
      errors.push(`模型名「${name}」重复定义（${out[name]} / ${chain}）`);
    }
    out[name] = chain;
  });
  return out;
}

function syncModelPolicy() {
  const errors = [];
  const branded = parseBrandedLines($('set-branded').value, errors);
  const routeNames = new Set((state.config.routes || []).map((r) => r.name));
  Object.entries(branded).forEach(([name, chain]) => {
    if (!routeNames.has(chain)) {
      errors.push(`模型名「${name}」指向的链「${chain}」不存在（现有：${[...routeNames].join('、') || '无'}）`);
    }
  });
  const fallbackOn = $('set-fallback').checked;
  const fallbackRoute = $('set-fallback-route').value.trim() || 'default';
  if (fallbackOn && !routeNames.has(fallbackRoute)) {
    errors.push(`未知名字默认走的链「${fallbackRoute}」不存在（现有：${[...routeNames].join('、') || '无'}）`);
  }
  renderPolicyErrors(errors);
  if (errors.length) { renderBrandList(); return; }
  state.config.models = { branded, fallback_unknown: fallbackOn, fallback_route: fallbackRoute };
  markDirty();
  renderBrandList();
}

$('set-branded').addEventListener('input', syncModelPolicy);
$('model-status-filter').addEventListener('click', (e) => {
  const button = e.target.closest('button[data-status]');
  if (!button) return;
  state.modelStatusFilter = button.dataset.status;
  $('model-status-filter').querySelectorAll('button')
    .forEach((b) => b.classList.toggle('active', b === button));
  renderExactModels();
});
$('set-fallback-route').addEventListener('input', syncModelPolicy);
$('set-fallback').addEventListener('change', syncModelPolicy);
$('access-tool').addEventListener('change', renderAccessSnippet);
$('access-model').addEventListener('change', renderAccessSnippet);
$('btn-copy-access').addEventListener('click', () => copyText($('access-snippet').value));
$('brand-list').addEventListener('click', (e) => {
  const chain = e.target.closest('button[data-chain-toggle]');
  if (chain) { toggleChain(chain); return; }
  const recent = e.target.closest('button[data-recent-toggle]');
  if (recent) { toggleRecent(recent); return; }
  const button = e.target.closest('button[data-copy]');
  if (button) copyText(button.dataset.copy);
});

async function loadRequests() {
  state.requests = await api('/api/requests/50');
  renderRequests();
  refreshModelInsights().catch(() => {});
}

// The audit log stores UTC ("2026-11-10T04:39:54Z"). Show it in the browser's
// local timezone; keep the date on a second line only when it is not today.
function logTime(ts) {
  const d = new Date(ts);
  if (!ts || Number.isNaN(d.getTime())) return { time: '—', date: '' };
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return {
    time: d.toLocaleTimeString('zh-CN', { hour12: false }),
    date: sameDay ? '' : `${d.getMonth() + 1}-${d.getDate()}`,
  };
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
    const at = logTime(e.ts);
    return `<tr>
      <td>${esc(at.time)}${at.date ? `<br><span class="sub-note">${esc(at.date)}</span>` : ''}</td>
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
  await loadTasks().catch(() => {});
  await loadRecommendations().catch(() => {});
  await loadChanges().catch(() => {});
}

async function loadTasks() {
  state.tasks = await api('/api/tasks');
  renderTasks();
}

async function loadRecommendations() {
  state.recommendations = await api('/api/recommend?purpose=agent&limit=20');
  renderRecommendations();
}

function renderRecommendations() {
  const body = $('recommend-body');
  const note = $('recommend-note');
  const data = state.recommendations;
  if (!data) return;
  const items = data.items || [];
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="7" class="sub-note">暂无推荐模型：请先拉取一次渠道模型目录（自维护 → 立即运行）。</td></tr>';
    note.textContent = '';
    return;
  }
  // Top row: the alias itself — this is what users copy into their tools.
  const aliasRow = `<tr style="background:#f0f7ff">
      <td><code>facet-agent</code><br><span class="sub-note">推荐工具里填这个</span></td>
      <td>所有符合条件的渠道</td>
      <td><span class="tag free">免费优先</span></td>
      <td><span class="tag active">支持</span></td>
      <td>—</td>
      <td>—</td>
      <td><button class="btn small primary" data-copy-model="facet-agent">复制 facet-agent</button></td>
    </tr>`;
  body.innerHTML = aliasRow + items.map((it) => {
    const unknown = it.matched === false;
    const toolText = it.tool_call === true
      ? '<span class="tag active">支持</span>'
      : '<span class="tag unknown">未知</span>';
    const ctx = it.context_window ? `${Number(it.context_window).toLocaleString()} tok` : '—';
    const score = typeof it.score === 'number' ? it.score : 0;
    return `<tr${unknown ? ' class="row-unknown"' : ''}>
      <td><code>${esc(it.wire_model)}</code>${unknown ? '<br><span class="sub-note">能力未知（registry 未收录）</span>' : ''}</td>
      <td>${esc(it.channel || '—')}</td>
      <td>${tierBadge(it.tier) || '<span class="sub-note">—</span>'}</td>
      <td>${toolText}</td>
      <td>${esc(ctx)}</td>
      <td><span class="tag ${score >= 0 ? 'active' : 'expired'}">${score}</span></td>
      <td><button class="btn small ghost" data-copy-model="${esc(it.wire_model)}">复制模型名</button></td>
    </tr>`;
  }).join('');
  const cov = data.coverage || {};
  note.textContent = `目录覆盖 ${cov.matched ?? 0}/${cov.total ?? items.length} 个已映射模型`
    + (data.unknown_capability ? ` · ${data.unknown_capability} 个模型能力未知（registry 未收录，排在「支持工具调用」之后）` : '')
    + ` · 已剔除不支持工具调用、冷却/失效的模型，免费优先，按稳定性排序`;
}

$('recommend-body').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-copy-model]');
  if (!btn) return;
  const name = btn.getAttribute('data-copy-model');
  try {
    await navigator.clipboard.writeText(name);
    toast(`已复制：${name}`);
  } catch (_) {
    // Fallback for non-secure contexts: select into a hidden textarea.
    const ta = document.createElement('textarea');
    ta.value = name;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast(`已复制：${name}`); }
    catch (_2) { toast(`复制失败，请手动复制：${name}`, 'bad'); }
    ta.remove();
  }
});

const TASK_STATES = {
  running: ['运行中', 'unknown'],
  idle: ['等待中', 'active'],
  ok: ['正常', 'active'],
  warn: ['需注意', 'expiring'],
  disabled: ['已关闭', 'expired'],
};

function renderTasks() {
  const body = $('tasks-body');
  const tasks = (state.tasks && state.tasks.tasks) || [];
  if (!tasks.length) {
    body.innerHTML = '<tr><td colspan="6" class="sub-note">没有后台任务</td></tr>';
    return;
  }
  body.innerHTML = tasks.map((t) => {
    const [stateText, cls] = TASK_STATES[t.state] || ['—', 'unknown'];
    const cycle = t.enabled ? (t.cycle || `每 ${t.hours} 小时`) : '未启用';
    const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '—');
    const result = t.last_error
      ? `<span class="tag expired">失败</span> ${esc(t.last_error)}`
      : esc(t.last_summary || '—');
    return `<tr>
      <td><strong>${esc(t.label)}</strong><br><span class="sub-note">${esc(t.description || '')}</span></td>
      <td><span class="tag ${cls}">${stateText}</span></td>
      <td>${esc(cycle)}</td>
      <td>${esc(when(t.last_run_at))}</td>
      <td>${esc(when(t.next_run_at))}</td>
      <td>${result}</td>
    </tr>`;
  }).join('');
}

const MAINT_TRIGGER = { manual: '手动运行', scheduled: '定时运行', first_run: '启动后首次运行' };

// ------------------------------------------------------------ source watch
const CHANGE_KINDS = {
  new_model: ['新增模型', 'active'],
  removed_model: ['模型下架', 'expired'],
  doc_updated: ['文档更新', 'unknown'],
  catalog_changed: ['目录变化', 'expiring'],
};

const CHANGE_ORIGINS = { provider: '厂家套餐', channel: '渠道目录', subscription: '套餐文档' };

async function loadChanges() {
  state.changes = await api('/api/changes');
  renderChanges();
}

function renderChanges() {
  const body = $('changes-body');
  const note = $('changes-note');
  const data = state.changes;
  if (!data || !body) return;
  const summary = data.summary || {};
  const unread = data.unread || 0;
  const kindLabel = (k) => CHANGE_KINDS[k] || [k, 'unknown'];
  const events = data.events || [];
  const mutedMap = data.muted || {};
  const modelsChip = (list, cls) => (list || []).slice(0, 8).map((m) =>
    `<span class="tag ${cls}" style="font-family:monospace">${esc(m)}</span>`).join(' ')
    + ((list || []).length > 8 ? `<span class="sub-note">…共 ${list.length} 个</span>` : '');
  body.innerHTML = events.length ? events.map((ev) => {
    const [kindText, kindCls] = kindLabel(ev.kind);
    const detail = ev.detail || {};
    const when = ev.ts ? esc(new Date(ev.ts.replace('Z', '+00:00')).toLocaleString()) : '—';
    const chips = (detail.added || []).length ? `<div>${modelsChip(detail.added, 'active')}</div>` : '';
    const chips2 = (detail.removed || []).length ? `<div>${modelsChip(detail.removed, 'expired')}</div>` : '';
    const excerpt = ev.evidence && ev.evidence.excerpt
      ? `<details><summary class="sub-note">页面摘录</summary><p class="sub-note" style="word-break:break-all">${esc(ev.evidence.excerpt)}</p></details>` : '';
    const readBtn = ev.status === 'unread'
      ? `<button class="btn small ghost" data-change-read="${esc(ev.id)}">标为已读</button>` : '';
    const srcKey = ev.source && ev.source.key ? ev.source.key : '';
    const isMuted = !!mutedMap[srcKey];
    const mutedTag = isMuted ? '<span class="tag unknown">已静默</span>' : '';
    const muteBtn = srcKey ? `<button class="btn small ghost" data-change-mute="${esc(srcKey)}"
      data-change-muted="${isMuted ? '1' : ''}">${isMuted ? '恢复上报' : '静默此源'}</button>` : '';
    const targets = ev.targets || [];
    const mapUI = ev.kind === 'new_model' && targets.length
      ? `<span class="sub-note">映射到</span><select data-change-target="${esc(ev.id)}">${
        targets.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join('')
      }</select><button class="btn small" data-change-map="${esc(ev.id)}">写入渠道</button>` : '';
    return `<div class="maint-item" style="align-items:flex-start;flex-wrap:wrap;gap:6px">
      <span class="tag ${kindCls}">${kindText}</span>
      <span style="flex:1;min-width:200px">
        <strong>${esc(ev.source && ev.source.label || '—')}</strong>
        ${mutedTag}
        <span class="sub-note">${CHANGE_ORIGINS[ev.source && ev.source.origin] || ''} · ${when}</span>
        ${chips}${chips2}${excerpt}
      </span>
      ${mapUI}${muteBtn}${readBtn}
    </div>`;
  }).join('') : '<p class="sub-note">暂无变更事件。首次扫描只建立基线，之后的模型集变化才会记为事件。</p>';
  const errSources = (data.sources || []).filter((s) => s.ok === false);
  const errText = errSources.length
    ? ` · ${errSources.length} 个信息源抓取失败（${errSources.slice(0, 3).map((s) => esc(s.label)).join('、')}${errSources.length > 3 ? '…' : ''}）` : '';
  const mutedCount = summary.muted || Object.keys(mutedMap).length;
  const notify = data.notify || {};
  note.textContent = `监视 ${summary.total || 0} 个信息源`
    + ` · 事件 ${events.length} 条 · 未读 ${unread} 条${errText}`
    + (mutedCount ? ` · 静默 ${mutedCount} 个源` : '')
    + (notify.ok ? ' · 变更通知已接入' : ' · 变更通知未配置');
  note.title = notify.ok ? '' : (notify.hint || '');
}

$('changes-body').addEventListener('click', async (e) => {
  const read = e.target.closest('button[data-change-read]');
  if (read) {
    read.disabled = true;
    try {
      await api('/api/changes/read', { method: 'POST', body: { ids: [read.dataset.changeRead] } });
      await loadChanges();
    } catch (err) {
      toast(err.message, 'bad');
      read.disabled = false;
    }
    return;
  }
  const mute = e.target.closest('button[data-change-mute]');
  if (mute) {
    mute.disabled = true;
    try {
      await api('/api/changes/mute', { method: 'POST', body: {
        keys: [mute.dataset.changeMute], muted: mute.dataset.changeMuted !== '1' } });
      await loadChanges();
    } catch (err) {
      toast(err.message, 'bad');
      mute.disabled = false;
    }
    return;
  }
  const map = e.target.closest('button[data-change-map]');
  if (!map) return;
  const select = map.closest('.maint-item') &&
    map.closest('.maint-item').querySelector(`select[data-change-target="${map.dataset.changeMap}"]`);
  map.disabled = true;
  try {
    const result = await api('/api/changes/map', { method: 'POST', body: {
      id: map.dataset.changeMap, channel: select ? select.value : '' } });
    toast(`已写入渠道 ${result.channel}：${(result.models || []).join('、')}`);
    await loadChanges();
  } catch (err) {
    toast(err.message, 'bad');
    map.disabled = false;
  }
});

function maintItem(tag, cls, html) {
  return `<div class="maint-item"><span class="tag ${cls}">${tag}</span><span>${html}</span></div>`;
}

function renderNotifyBanner(notify) {
  // Alerts that cannot leave the box look exactly like a healthy system, so
  // the self-maintenance page says it out loud instead of staying silent.
  const el = $('maintenance-notify-banner');
  if (!el) return;
  if (!notify || notify.ok) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = `<div class="banner critical">
    <span class="banner-title">运行告警无法外发</span>
    <div class="banner-items">${esc(notify.hint || '未配置通知通道，告警只留在本地记录。')}</div>
    <button class="btn small" id="banner-goto-notify">去配置</button>
  </div>`;
  const btn = $('banner-goto-notify');
  if (btn) btn.onclick = () => switchPage('subscriptions');
}

function renderMaintenance() {
  const data = state.maintenance;
  if (!data) return;
  renderNotifyBanner(data.notify);
  const queue = data.llm_queue || [];
  const queueText = queue.length
    ? `自动队列 ${queue.length} 个（${queue.slice(0, 3).map(esc).join('、')}${queue.length > 3 ? '…' : ''}）`
    : '自动（队列为空，将探测免费模型）';
  const llm = data.llm_model
    ? esc(data.llm_model) + (data.llm_available ? '' : '（暂无可用渠道承担）')
    : queueText;
  const verify = data.free_verify || {};
  $('maintenance-note').textContent = `调度：${data.enabled ? `每 ${data.hours} 小时` : '未启用'}`
    + ` · 免费模型体检：${verify.enabled ? `每 ${verify.hours} 小时` : '未启用'}`
    + ` · LLM 分析：${llm} · 由 defaults.maintenance_model 控制（留空自动探测）`;
  // Probe detail: the status payload carries every candidate's quiz outcome
  // (model, pass, extraction hits, error) — render it instead of only the
  // summary toast so users can see what was tested and why it failed.
  const probeResults = data.llm_queue_results || [];
  const probeHtml = [];
  if (probeResults.length) {
    const verifiedAt = data.llm_queue_verified_at
      ? ` · 探测于 ${new Date(data.llm_queue_verified_at * 1000).toLocaleString()}` : '';
    probeHtml.push('<p class="sub-note">维护模型探测明细 — 考题：从样例文档'
      + `提取模型清单，须与标准答案完全一致，通过者进入维护队列${verifiedAt}</p>`
      + probeResults.map((r) => maintItem(r.ok ? '通过' : '未通过', r.ok ? 'active' : 'expired',
        `<code>${esc(r.model)}</code> · ${r.channel ? `渠道 <strong>${esc(r.channel)}</strong>` : '网关路由'}`
        + ` · 提取命中 ${r.hit}/${r.expected}`
        + (r.ok ? '' : ` · ${esc(r.error || '答案不符')}`))).join(''));
  }
  const deadModels = data.dead_models || [];
  if (deadModels.length) {
    probeHtml.push(`<p class="sub-note">已验证失效的免费模型 — 路由会跳过这些映射，`
      + `标签 ${data.dead_ttl_hours || 6} 小时后自动失效，可点「全量校验免费模型」复验</p>`
      + deadModels.map((r) => maintItem('失效', 'expired',
        `<code>${esc(r.model)}</code> · 渠道 <strong>${esc(r.channel)}</strong>`
        + ` · ${esc(r.reason || '调用失败')}`
        + (r.marked_at ? ` · 标记于 ${new Date(r.marked_at * 1000).toLocaleString()}` : ''))).join(''));
  }
  $('maintenance-probe').innerHTML = probeHtml.join('');
  const reports = data.reports || [];
  const body = $('maintenance-body');
  if (!reports.length) {
    body.innerHTML = '<p class="sub-note">还没有运维报告。点「立即运行」会拉取各渠道模型目录，'
      + '把目录确证的变更（下架模型清理、auto 重指）自动应用到配置（先备份），'
      + '新模型与限流建议留给你一键采纳，密钥失效只报告。</p>';
    return;
  }
  body.innerHTML = reports.map((r) => {
    const rows = maintReportRows(r, true);
    return `<div class="maint-report">${maintReportHead(r)}`
      + (rows.length ? rows.join('') : '<p class="sub-note">本次运行未发现需要处理的变更。</p>')
      + '</div>';
  }).join('');
}

function maintReportHead(r) {
  const when = r.finished_at ? new Date(r.finished_at * 1000).toLocaleString()
    : (r.ts || '');
  return `<div class="maint-head"><strong>${esc(when)}</strong>`
    + `<span class="sub-note">${esc(MAINT_TRIGGER[r.trigger] || r.trigger || '')}`
    + ` · ${esc(r.channels_total ?? 0)} 个渠道</span></div>`;
}

function maintReportRows(r, withApply) {
  const tierTag = (name) => channelTierBadge(name);
  const rows = [];
  (r.applied || []).forEach((a) => {
    const detail = a.action === 'repoint_auto'
      ? `auto：<code>${esc(a.from)}</code> → <code>${esc(a.to)}</code>`
      : (a.models || []).map((m) => `<code>${esc(m)}</code>`).join(' ');
    rows.push(maintItem('自动应用', 'active',
      `<strong>${esc(a.channel)}</strong> ${tierTag(a.channel)} ${esc(a.reason || '')}：${detail}`));
  });
  (r.suggestions || []).forEach((s) => {
    const btn = withApply && s.kind === 'add_model' && (s.models || []).length
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
  return rows;
}

$('btn-refresh-maintenance').onclick = () => loadMaintenance().catch((e) => toast(e.message, 'bad'));

$('btn-refresh-cu').onclick = () => loadCuCredentials()
  .then(() => toast('浏览器代理凭据已刷新', 'good'))
  .catch((e) => toast(e.message, 'bad'));

$('cu-form').onsubmit = saveCuCredential;

$('btn-refresh-tasks').onclick = () => loadTasks()
  .then(() => toast('定时任务状态已刷新', 'good'))
  .catch((e) => toast(e.message, 'bad'));

$('btn-refresh-recommend').onclick = () => loadRecommendations()
  .then(() => toast('模型推荐已刷新', 'good'))
  .catch((e) => toast(e.message, 'bad'));

$('btn-verify-free').onclick = async (e) => {
  const b = e.target;
  const label = b.textContent;
  b.disabled = true;
  b.textContent = '校验中…';
  try {
    const r = await api('/api/maintenance/verify-free', { method: 'POST' });
    if (r.skipped) {
      toast('免费模型校验：已有体检在运行，本次已跳过', 'warn');
    } else {
      toast(`免费模型校验：检查 ${r.checked} 个，可用 ${r.alive}，失效 ${r.dead}，`
        + `无法判定 ${r.inconclusive}`, r.dead ? 'warn' : 'ok');
    }
    await loadMaintenance();
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    b.disabled = false;
    b.textContent = label;
  }
};

$('btn-test-maintenance-models').onclick = async (e) => {
  const b = e.target;
  const label = b.textContent;
  b.disabled = true;
  b.textContent = '探测中…';
  try {
    const r = await api('/api/maintenance/test-models', { method: 'POST' });
    const passed = (r.queue || []).length;
    const failed = (r.results || []).filter((x) => !x.ok);
    let msg = `维护模型探测完成：候选 ${r.candidates} 个，通过 ${passed} 个`;
    if (passed) msg += `：${(r.queue || []).join('、')}`;
    if (failed.length) msg += `；未通过示例：${failed[0].model}（${failed[0].error || '答案不符'}）`;
    toast(msg, passed ? 'ok' : 'warn');
    await loadMaintenance();
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    b.disabled = false;
    b.textContent = label;
  }
};

// ---- 免费模型页 -----------------------------------------------------------
// One flat table over every free channel: the previous per-channel cards hid
// quota and rate limits inside maintenance noise. Rows are sorted by "can I use
// it right now", so the answer to "哪些免费模型现在能用" is the first screen.
const FREE_STATUS = {
  ok: ['可用', 'active'], warn: ['额度紧张', 'warn'],
  quizfail: ['测验未通过', 'warn'],
  exhausted: ['额度用完', 'expired'], cooling: ['冷却中', 'expiring'],
  nokey: ['未配密钥', 'unknown'], dead: ['已失效', 'expired'],
  disabled: ['已停用', 'off'], catalog: ['仅目录未映射', 'unknown'],
  other: ['不可用', 'unknown'],
};
const FREE_RANK = { ok: 0, warn: 1, quizfail: 2, exhausted: 3, cooling: 4,
  nokey: 5, dead: 6, disabled: 7, catalog: 8, other: 9 };
const FREE_PERIOD = { total: '总计', month: '每月', day: '每日' };
const FREE_RESET = {
  total: '额度用完不重置',
  month: '每月 1 日重置',
  day: '次日 0 点重置',
};

function freeModelState(m, ch) {
  const q = m.quota || null;
  const quizFailed = !!(m.test && m.test.ok === false);
  if (m.routable) {
    // Still routable (a quiz miss is not proof the model is gone), but the
    // user just watched it fail — the status column must not say 可用.
    if (quizFailed) return 'quizfail';
    return q && q.warning ? 'warn' : 'ok';
  }
  if (q && q.exhausted) return 'exhausted';
  if (!ch.enabled) return 'disabled';
  if (ch.cooling) return 'cooling';
  if (!ch.has_key) return 'nokey';
  if (m.dead) return 'dead';
  if (quizFailed) return 'quizfail';
  if (m.source === 'catalog') return 'catalog';
  return 'other';
}

function freeRateCaps(limits) {
  if (!limits) return '';
  const bits = [];
  if (limits.rpm) bits.push(`${limits.rpm} RPM`);
  if (limits.rpd) bits.push(`${limits.rpd} 次/天`);
  if (limits.tpm) bits.push(`${Number(limits.tpm).toLocaleString()} tok/分`);
  if (limits.tpd) bits.push(`${Number(limits.tpd).toLocaleString()} tok/天`);
  return bits.join(' · ');
}

function freeRows(free) {
  const rows = [];
  (free.channels || []).forEach((ch) => {
    (ch.models || []).forEach((m) => {
      rows.push({ ch, m, status: freeModelState(m, ch) });
    });
  });
  return rows.sort((a, b) => FREE_RANK[a.status] - FREE_RANK[b.status]
    || a.ch.name.localeCompare(b.ch.name) || a.m.model.localeCompare(b.m.model));
}

function freeQuotaCell(q) {
  if (!q) return '<span class="sub-note">未设本地额度硬门禁<br>以厂家额度 / 限流为准</span>';
  const ratio = Math.min(1, Math.max(0, Number(q.ratio || 0)));
  const tone = q.exhausted ? 'bad' : q.warning ? 'warn' : '';
  const hasVendor = q.vendor_remaining != null || q.vendor_total != null;
  const lines = [];
  if (hasVendor) {
    const total = q.vendor_total == null ? null : Number(q.vendor_total);
    const remain = q.vendor_remaining == null ? null : Number(q.vendor_remaining);
    const used = q.vendor_consumed != null ? Number(q.vendor_consumed)
      : (total != null && remain != null ? total - remain : null);
    lines.push(`<span class="sub-note">厂家侧</span> `
      + `${used == null ? '?' : used.toLocaleString()} / `
      + `${total == null ? '?' : total.toLocaleString()} tok`
      + (remain == null ? '' : ` · 剩 ${remain.toLocaleString()}`));
  }
  const local = [];
  if (q.limit_tokens != null) {
    local.push(`${Number(q.used_tokens || 0).toLocaleString()} / `
      + `${Number(q.limit_tokens || 0).toLocaleString()} tok`
      + (q.remaining_tokens == null ? ''
        : ` · 剩 ${Number(q.remaining_tokens).toLocaleString()}`));
  }
  if (q.limit_requests != null) {
    local.push(`${Number(q.used_requests || 0).toLocaleString()} / `
      + `${Number(q.limit_requests).toLocaleString()} 次`
      + (q.remaining_requests == null ? ''
        : ` · 剩 ${Number(q.remaining_requests).toLocaleString()}`));
  }
  if (local.length) {
    lines.push(`<span class="sub-note">网关本地</span> ${local.join('；')}`);
  }
  const period = FREE_PERIOD[q.period] || q.period || '';
  const reset = FREE_RESET[q.period] || '';
  const badge = q.exhausted ? '<span class="tag expired">已用完</span>'
    : q.warning ? '<span class="tag expiring">临近上限</span>' : '';
  const source = hasVendor
    ? `厂家额度${q.vendor_at ? ` · 对齐 ${esc(q.vendor_at)}` : ' · 已对齐'}`
    : `网关本地计数 · ${q.source === 'provider_baseline' ? '注册表基线' : '渠道配置'}`;
  const periodNote = local.length
    ? `${esc(period)}${reset ? ` · ${reset}` : ''} · ` : '';
  return `<div class="free-quota"><div>${lines.join('<br>')}</div>`
    + `<div class="bar"><i class="${tone}" style="width:${(ratio * 100).toFixed(1)}%"></i></div>`
    + `<div class="sub-note">${periodNote}${source} ${badge}</div></div>`;
}

function freeRateCell(ch) {
  const vendor = freeRateCaps(ch.free_limits);
  const own = freeRateCaps(ch.rate);
  const note = (ch.free_limits || {}).note || '';
  const doc = ch.docs_url
    ? ` <a class="provider-link" href="${esc(ch.docs_url)}" target="_blank" rel="noopener">文档</a>` : '';
  return `<div title="${esc(note)}"><span class="sub-note">厂家</span> `
    + (vendor ? esc(vendor) : '<span class="sub-note">未收录</span>') + `</div>`
    + `<div class="sub-note">网关 ${own ? esc(own) : '未配置限流'}${doc}</div>`
    + (note ? `<div class="sub-note free-rate-note">${esc(note)}</div>` : '');
}

function renderFreePage(free) {
  const rows = freeRows(free);
  const summary = free.summary || {};
  const count = (keys) => rows.filter((r) => keys.includes(r.status)).length;
  const ok = count(['ok']);
  const warn = count(['warn']);
  const quizfail = count(['quizfail']);
  const exhausted = count(['exhausted']);
  const other = rows.length - ok - warn - quizfail - exhausted;

  const metrics = [
    ['免费模型', rows.length, ''],
    ['现在能用', ok, ok ? 'ok' : 'warn'],
    ['额度紧张', warn, warn ? 'warn' : ''],
    ['测验未通过', quizfail, quizfail ? 'warn' : ''],
    ['额度用完', exhausted, exhausted ? 'bad' : ''],
    ['其他不可用', other, ''],
    ['厂家额度未同步', summary.quota_unsynced || 0, summary.quota_unsynced ? 'warn' : ''],
  ];
  $('free-summary').innerHTML = metrics.map(([label, value, cls]) =>
    `<div class="metric"><div class="label">${label}</div><div class="value ${cls}">${esc(value)}</div></div>`).join('');
  $('btn-retest-failed').hidden = !quizfail;
  $('btn-retest-failed').textContent = `重测未通过（${quizfail}）`;

  const unsynced = (free.channels || []).filter((ch) => ch.quota_sync);
  $('free-alerts').innerHTML = unsynced.map((ch) =>
    `<div class="banner"><span class="banner-title">${esc(ch.name)} 额度未同步</span>`
    + `<span class="banner-items">${esc(ch.quota_sync)}</span>`
    + '<button class="btn small banner-align">对齐</button></div>').join('');

  const filters = [
    ['all', '全部', rows.length],
    ['ok', '可用', ok],
    ['warn', '额度紧张', warn],
    ['quizfail', '测验未通过', quizfail],
    ['exhausted', '额度用完', exhausted],
    ['other', '其他不可用', other],
  ];
  $('free-filters').innerHTML = filters.map(([key, label, n]) =>
    `<button class="btn small ${state.freeFilter === key ? 'active' : ''}" data-free-filter="${key}">`
    + `${label} ${n}</button>`).join('');

  const q = (state.freeQuery || '').trim().toLowerCase();
  const visible = rows.filter((r) => {
    if (state.freeFilter === 'ok' && r.status !== 'ok') return false;
    if (state.freeFilter === 'warn' && r.status !== 'warn') return false;
    if (state.freeFilter === 'quizfail' && r.status !== 'quizfail') return false;
    if (state.freeFilter === 'exhausted' && r.status !== 'exhausted') return false;
    if (state.freeFilter === 'other' && (r.status === 'ok' || r.status === 'warn'
      || r.status === 'quizfail' || r.status === 'exhausted')) return false;
    if (!q) return true;
    return (r.m.model + ' ' + r.ch.name + ' ' + (r.ch.provider_name || '') + ' '
      + (r.ch.provider || '')).toLowerCase().includes(q);
  });

  const box = $('free-body');
  if (!rows.length) {
    box.innerHTML = '<tr><td colspan="7" class="sub-note">还没有免费渠道。到「渠道配置」把渠道的套餐设为「免费」，'
      + '它的模型就会出现在这里。</td></tr>';
    return;
  }
  if (!visible.length) {
    box.innerHTML = '<tr><td colspan="7" class="sub-note">没有符合筛选条件的模型。</td></tr>';
    return;
  }

  box.innerHTML = visible.map(({ ch, m, status }) => {
    const [label, cls] = FREE_STATUS[status] || FREE_STATUS.other;
    const use = m.usage || {};
    const useText = use.requests
      ? `${Number(use.requests).toLocaleString()} 次 · ${Number(use.tokens || 0).toLocaleString()} tok`
        + (use.cached_tokens && use.input_tokens
          ? ` · 缓存 ${(use.cached_tokens / use.input_tokens * 100).toFixed(1)}%` : '')
      : '<span class="sub-note">未调用</span>';
    const reason = status === 'ok' ? '' : `<div class="sub-note">${esc(m.skip_reason || '')}</div>`;
    const testNote = status === 'quizfail' && m.test
      ? `<div class="sub-note">最近测验 `
        + `${new Date((m.test.tested_at || 0) * 1000).toLocaleString()}：`
        + `${esc(m.test.error || '未通过')}（仍可路由，测验失败不等于模型失效）</div>`
      : '';
    return '<tr>'
      + `<td><code>${esc(m.model)}</code> <button class="btn small ghost free-copy" `
      + `title="复制模型名" data-model="${esc(m.model)}">复制</button>`
      + (m.in_queue ? ' <span class="tag active">队列中</span>' : '') + '</td>'
      + `<td><strong>${esc(ch.provider_name || ch.provider)}</strong>`
      + `<div class="sub-note">${esc(ch.name)}${ch.mode ? ` · ${esc(ch.mode)}` : ''}</div></td>`
      + `<td><span class="tag ${cls}">${label}</span>${reason}${testNote}</td>`
      + `<td>${freeQuotaCell(m.quota)}</td>`
      + `<td>${freeRateCell(ch)}</td>`
      + `<td>${useText}</td>`
      + `<td><button class="btn small ghost free-test" data-channel="${esc(ch.name)}" `
      + `data-model="${esc(m.model)}">测试</button></td>`
      + '</tr>';
  }).join('');
}

async function loadFreeModels() {
  const data = await api('/api/maintenance');
  state.maintenance = data;
  renderFreePage((data && data.free_models) || {});
}

$('btn-refresh-free-models').onclick = () => loadFreeModels().catch((e) => toast(e.message, 'bad'));

// One click re-quizzes every model the page shows as 测验未通过, instead of
// tapping the per-row 测试 button dozens of times. Channels run in parallel,
// models within a channel serially — the free keys answer one request at a
// time, so parallel calls would 429 and look like another failure.
$('btn-retest-failed').onclick = async (e) => {
  const b = e.target;
  const label = b.textContent;
  b.disabled = true;
  b.textContent = '重测中…（免费键逐个发问，可能要几分钟）';
  try {
    const r = await api('/api/maintenance/retest-failed', { method: 'POST' });
    const failed = (r.results || []).filter((x) => !x.ok);
    let msg = `重测完成：${r.retested} 个，通过 ${r.passed}，仍失败 ${r.failed}`;
    if (r.passed) msg += `；通过：${(r.results || []).filter((x) => x.ok).map((x) => x.model).join('、')}`;
    if (failed.length) msg += `；未通过示例：${failed[0].model}（${failed[0].error || '答案不符'}）`;
    toast(msg, r.failed ? 'warn' : 'ok');
    await loadFreeModels();
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    b.disabled = false;
    b.textContent = label;
  }
};

$('free-filters').addEventListener('click', (e) => {
  const b = e.target.closest('[data-free-filter]');
  if (!b) return;
  state.freeFilter = b.dataset.freeFilter;
  renderFreePage((state.maintenance && state.maintenance.free_models) || {});
});

$('free-filter').addEventListener('input', (e) => {
  state.freeQuery = e.target.value;
  renderFreePage((state.maintenance && state.maintenance.free_models) || {});
});

$('free-alerts').addEventListener('click', (e) => {
  if (!e.target.closest('.banner-align')) return;
  $('btn-free-align').click();
});

$('btn-free-align').onclick = async (e) => {
  const b = e.target;
  b.disabled = true;
  b.textContent = '对齐中…';
  try {
    const r = await api('/api/freealign/run', { method: 'POST' });
    if (r.busy) {
      toast(r.message || '上一次自维护仍在运行，本次对齐已跳过', 'warn');
      return;
    }
    const changed = Object.keys(r.patch || {}).length;
    const alerts = (r.alerts || []).length;
    toast(`额度对齐完成：${changed} 个渠道映射更新${alerts ? ` · ${alerts} 条告警` : ''}`,
      alerts ? 'warn' : 'ok');
    await loadFreeModels();
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    b.disabled = false;
    b.textContent = '立即对齐厂家额度';
  }
};

$('free-body').addEventListener('click', async (e) => {
  const copy = e.target.closest('.free-copy');
  if (copy) {
    try {
      await navigator.clipboard.writeText(copy.dataset.model);
      toast(`已复制 ${copy.dataset.model}`, 'ok');
    } catch (_) {
      toast('复制失败，请手动选中模型名', 'warn');
    }
    return;
  }
  const b = e.target.closest('.free-test');
  if (!b) return;
  b.disabled = true;
  b.textContent = '测试中…';
  try {
    const r = await api('/api/maintenance/test-model', {
      method: 'POST',
      body: { channel: b.dataset.channel, model: b.dataset.model },
    });
    toast(r.ok ? `通过：${r.model} 提取命中 ${r.hit}/${r.expected}，已进入维护队列`
      : `未通过：${r.model} · ${r.error || '答案不符'}`, r.ok ? 'ok' : 'warn');
    await loadFreeModels();
  } catch (err) {
    toast(err.message, 'bad');
    b.disabled = false;
    b.textContent = '测试';
  }
});

$('btn-dash-free').onclick = () => switchPage('free');

$('btn-refresh-changes').onclick = () => loadChanges().catch((e) => toast(e.message, 'bad'));

$('btn-changes-readall').onclick = async (e) => {
  const b = e.target;
  b.disabled = true;
  try {
    await api('/api/changes/read', { method: 'POST', body: { all: true } });
    toast('全部变更已标为已读');
    await loadChanges();
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    b.disabled = false;
  }
};

$('btn-run-watch').onclick = async (e) => {
  const b = e.target;
  const label = b.textContent;
  b.disabled = true;
  b.textContent = '扫描中…';
  try {
    const r = await api('/api/changes/run', { method: 'POST' });
    if (r.busy) {
      toast('上一次监视扫描仍在运行，本次已跳过', 'warn');
    } else {
      toast(`扫描完成：${r.summary || '监视完成'}`, r.error_sources ? 'warn' : 'ok');
    }
    await loadChanges();
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    b.disabled = false;
    b.textContent = label;
  }
};

$('btn-run-maintenance').onclick = async (e) => {
  const b = e.target;
  const label = b.textContent;
  b.disabled = true;
  b.textContent = '运行中…';
  try {
    const r = await api('/api/maintenance/run', { method: 'POST' });
    await loadMaintenance();
    toast(r.busy ? (r.message || '上一次自维护仍在运行，本次已跳过') : '运维分析完成',
      r.busy ? 'warn' : 'ok');
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

// ------------------------------------------------------------ self-maintenance
// The 自维护 page surfaces the automation the backend already runs: scores,
// free-quota alignment history and config backups, all previously headless.

// Channel x wire stability score table, straight from the state snapshot.
// scoreBadge() embeds a per-row × reset; delegation lives in bindEvents.
function renderScores() {
  const el = $('score-body');
  if (!el) return;
  const rows = [];
  (state.snapshot?.channels || []).forEach((c) => {
    Object.entries(c.scores || {}).forEach(([wire, score]) => {
      rows.push(`<div class="model-row">
        <code>${esc(c.name)}</code>
        <code>${esc(wire)}</code>
        ${scoreBadge(score, c.name, wire)}
        <span class="sub-note">${score > 0 ? '稳定' : '不稳定'} · ${score <= -3 ? '已沉底，衰减回升后归位' : '按链顺序参与调度'}</span>
      </div>`);
    });
  });
  el.innerHTML = rows.length ? rows.join('')
    : '<div class="chart-empty">还没有稳定性评分记录。服务成功 +1、非额度类失败 −5，产生请求后按渠道×模型自动累计并按半衰期衰减。</div>';
}

$('btn-reset-all-scores').onclick = async () => {
  if (!(await askConfirm('重置全部评分',
    '确认清空所有渠道×模型的稳定性评分？分数会随后续请求重新积累。'))) return;
  try {
    const r = await api('/api/score/reset', { method: 'POST', body: {} });
    toast(`已重置 ${r.removed} 条评分`);
    await loadState();
  } catch (err) { toast(err.message, 'bad'); }
};

async function loadFreeAlignLogs() {
  state.freealignLogs = await api('/api/freealign/logs');
  renderFreeAlignLogs();
}

function renderFreeAlignLogs() {
  const data = state.freealignLogs;
  if (!data) return;
  const align = state.maintenance?.free_align;
  $('freealign-note').textContent =
    `调度：${align && align.enabled ? `每 ${align.hours} 小时` : '未启用（defaults.free_align_hours 为 0）'}`
    + ` · 历史来源：${data.source === 'file' ? 'freealign.jsonl' : '本次进程内存'}`
    + (data.has_more ? ' · 更早记录未加载' : '');
  const logs = data.logs || [];
  const box = $('freealign-body');
  if (!logs.length) {
    box.innerHTML = '<p class="sub-note">还没有对齐记录。点「立即对齐免费额度」手动触发一次，'
      + '或等定时任务运行后这里会留档每一次结果。</p>';
    return;
  }
  box.innerHTML = logs.map((r) => {
    const when = r.ts || (r.finished_at
      ? new Date(r.finished_at * 1000).toLocaleString() : '');
    const aligned = (r.aligned || []).map((a) =>
      `<strong>${esc(a.channel)}</strong>：${(a.models || []).length} 个免费模型${a.changed ? '（映射已更新）' : '（无变化）'}`);
    const head = `<div class="maint-report"><div class="maint-head"><strong>${esc(when)}</strong>`
      + `<span class="sub-note">${esc(MAINT_TRIGGER[r.trigger] || r.trigger || '')}</span></div>`;
    const rows = [];
    if (aligned.length) {
      rows.push(maintItem('对齐', 'active', aligned.join('；')));
    } else {
      rows.push(maintItem('对齐', 'unknown', '本次没有带免费额度工具的免费渠道'));
    }
    (r.alerts || []).forEach((a) => {
      rows.push(maintItem('注意', 'expired',
        `${a.channel ? `<strong>${esc(a.channel)}</strong> ` : ''}${esc(a.message)}`));
    });
    return head + rows.join('') + '</div>';
  }).join('');
}

$('btn-run-freealign').onclick = async (e) => {
  const b = e.target;
  const label = b.textContent;
  b.disabled = true;
  b.textContent = '对齐中…';
  try {
    const r = await api('/api/freealign/run', { method: 'POST' });
    if (r.busy) {
      toast(r.message || '上一次自维护仍在运行，本次对齐已跳过', 'warn');
      return;
    }
    const changed = Object.keys(r.patch || {}).length;
    toast(changed
      ? `免费额度对齐完成：${changed} 个渠道映射已更新（原配置已备份）`
      : '免费额度对齐完成：没有需要更新的映射');
    await loadMaintenance();
    await loadFreeAlignLogs();
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    b.disabled = false;
    b.textContent = label;
  }
};

$('btn-refresh-freealign').onclick = () => Promise.all([loadMaintenance(), loadFreeAlignLogs()])
  .catch((e) => toast(e.message, 'bad'));

async function loadBackups() {
  state.backups = await api('/api/backups');
  renderBackups();
}

const BACKUP_TAGS = { maintenance: '自动运维', freealign: '免费额度对齐' };

function renderBackups() {
  const data = state.backups;
  if (!data) return;
  const box = $('backup-body');
  const backups = data.backups || [];
  box.innerHTML = backups.length ? backups.map((b) => `
    <div class="model-row">
      <code>${esc(b.file)}</code>
      <span class="tag unknown">${esc(BACKUP_TAGS[b.tag] || b.tag)}</span>
      <span class="sub-note">${new Date(b.mtime * 1000).toLocaleString()} · ${(b.size / 1024).toFixed(1)} KB</span>
    </div>`).join('')
    : '<p class="sub-note">还没有备份文件。自动运维或免费额度对齐改写配置前会自动生成。</p>';
}

async function loadMaintLogs() {
  state.maintlog = await api('/api/maintenance/logs');
  renderMaintLogs();
}

const MAINT_LOG_CHIPS = [
  ['applied', '应用', 'active'], ['suggestions', '建议', 'unknown'],
  ['alerts', '注意', 'expired'], ['llm_notes', 'AI 备注', 'active'],
];

function renderMaintLogs() {
  const data = state.maintlog;
  if (!data) return;
  const logs = data.logs || [];
  const body = $('maintlog-body');
  if (!logs.length) {
    $('maintlog-note').textContent = '';
    body.innerHTML = '<p class="sub-note">还没有维护日志。每次「自动运维」运行（定时或手动）'
      + '完成后都会在这里留档。</p>';
    return;
  }
  $('maintlog-note').textContent = `显示最近 ${logs.length} 次运行`
    + (data.has_more ? ' · 更早的记录未加载' : '');
  body.innerHTML = logs.map((r) => {
    const chips = MAINT_LOG_CHIPS
      .map(([k, label, cls]) => (r[k] || []).length
        ? `<span class="tag ${cls}">${label} ${(r[k] || []).length}</span>` : '')
      .join('');
    const rows = maintReportRows(r, false);
    return `<details class="maint-log"><summary>${maintReportHead(r)}`
      + `<span class="maint-chips">${chips}</span></summary>`
      + '<div class="maint-log-body">'
      + (rows.length ? rows.join('') : '<p class="sub-note">本次运行未发现需要处理的变更。</p>')
      + '</div></details>';
  }).join('');
}

$('btn-refresh-maintlog').onclick = () =>
  loadMaintLogs().catch((e) => toast(e.message, 'bad'));

$('btn-quickstart').onclick = () => switchPage('quickstart');

$('btn-api-key-create').onclick = async () => {
  const answer = await askText({
    title: '生成新 API Key',
    message: '给这个 Key 起个名字（工具名或成员名），方便之后暂停或删除。',
    confirmText: '生成',
  });
  if (!answer || !answer.value.trim()) return;
  try {
    await api('/api/keys', { method: 'POST', body: { name: answer.value.trim() } });
    await loadApiKeys();
    toast('已生成新 Key，点击表格中的 Key 即可复制');
  } catch (e) {
    toast(e.message, 'bad');
  }
};

$('btn-copy-master').onclick = () => {
  const value = $('master-key-value').dataset.copy || '';
  if (value) copyText(value); else toast('当前没有配置主密钥', 'warn');
};

$('btn-toggle-master').onclick = async () => {
  const pass = $('master-admin-pass').value.trim();
  if (!pass) { toast('请先填写管理密码', 'bad'); return; }
  const enabled = $('master-key-value').dataset.enabled === '1';
  if (enabled && !(await askConfirm('暂停主密钥',
    '暂停后，用主密钥发起的调用会立即返回 401；已签发的成员 API Key 不受影响。确定暂停？'))) return;
  try {
    await api('/api/master-key', {
      method: 'POST', body: { action: enabled ? 'pause' : 'enable', admin_key: pass },
    });
    $('master-admin-pass').value = '';
    toast(enabled ? '主密钥已暂停' : '主密钥已启用');
    await loadApiKeys();
  } catch (e) {
    toast(e.message, 'bad');
  }
};

$('btn-rotate-master').onclick = async () => {
  const pass = $('master-admin-pass').value.trim();
  const newKey = $('master-new-key').value.trim();
  if (!pass) { toast('请先填写管理密码', 'bad'); return; }
  if (newKey && newKey.length < 8) { toast('新主密钥至少 8 个字符', 'bad'); return; }
  if (!(await askConfirm('更换主密钥', newKey
    ? '旧主密钥将立即失效，仍在用旧密钥的工具需要改用新密钥。确定更换？'
    : '留空将随机生成一个新主密钥，旧主密钥立即失效。确定更换？'))) return;
  const oldMaster = $('master-key-value').dataset.copy || '';
  try {
    const res = await api('/api/master-key', {
      method: 'POST', body: { action: 'rotate', key: newKey, admin_key: pass },
    });
    // Only follow the rotation when this browser itself signs in with the
    // master key; a member key keeps its own identity and attribution.
    if (!state.key || state.key === oldMaster) {
      state.key = res.api_key;
      storeKey('gateway', state.key, true);
    }
    $('master-admin-pass').value = '';
    $('master-new-key').value = '';
    toast('主密钥已更换');
    await loadApiKeys();
  } catch (e) {
    toast(e.message, 'bad');
  }
};

$('btn-admin-key-save').onclick = async () => {
  const oldKey = $('admin-old-key').value.trim();
  const newKey = $('admin-new-key').value.trim();
  const again = $('admin-new-key2').value.trim();
  if (!oldKey) { toast('请填写当前管理密钥', 'bad'); return; }
  if (!newKey) { toast('请填写新管理密钥', 'bad'); return; }
  if (newKey !== again) { toast('两次输入的新密钥不一致', 'bad'); return; }
  try {
    const res = await api('/api/admin/key', {
      method: 'POST', body: { key: newKey, old_key: oldKey },
    });
    state.adminKey = res.admin_key;
    storeKey('admin', res.admin_key, true);
    ['admin-old-key', 'admin-new-key', 'admin-new-key2'].forEach((id) => { $(id).value = ''; });
    toast('管理密钥已更新，本浏览器已自动切换');
    loadState().catch(() => {});
  } catch (e) {
    toast(e.message, 'bad');
  }
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
        <td class="${s.quota_ratio !== null && s.quota_ratio !== undefined && s.quota_ratio <= 0.15 ? 'quota-low' : 'sub-note'}">${s.quota_remaining === null || s.quota_remaining === undefined ? '—' : esc(s.quota_remaining + ' ' + (s.quota_unit || ''))}</td>
      </tr>`).join('')
    : '<tr><td colspan="6" class="sub-note">还没有订阅记录</td></tr>';
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
  renderVendors();
  renderBanner();
  renderUsage();
  renderNotifications();
  renderModels();
  renderRequests();
  renderReport();
  renderScores();
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
  closeGuideOverlay();
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
  // Typed vendor-management credentials (IAM AK/SK) travel with the draft so
  // a catalog pull uses them immediately, before the channel is saved.
  const provider = val('provider');
  const creds = {};
  if (provider) {
    f.querySelectorAll('#vendor-credentials [data-cred-env]').forEach((inp) => {
      const v = inp.value.trim();
      if (v) creds[inp.dataset.credEnv] = v;
    });
  }
  return {
    name: val('name'), protocol: val('protocol'), base_url: val('base_url'),
    api_key: val('api_key') || '***',
    priority: Number(val('priority') || 100),
    cooldown_minutes: Number(val('cooldown_minutes') || 300),
    rate, models: parseModels(val('models')),
    enabled: f.elements.namedItem('enabled').checked,
    provider,
    mode: val('mode'),
    ...(Object.keys(creds).length ? { credentials: { [provider]: creds } } : {}),
    ...(val('upstream_path') ? { upstream_path: val('upstream_path') } : {}),
    ...(val('proxy') ? { proxy: val('proxy') } : {}),
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
      ? '请点右上角「管理密钥」填入 listen.admin_key（首次登录默认 admin）后重试'
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

// Row-level probe: tests the saved channel (bare name resolves server-side)
// with its persisted test model; a passing probe clears a live cooldown.
async function testChannelRow(button, i) {
  const c = state.config.channels[i];
  if (!c) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = '测试中…';
  try {
    const body = { channels: [c.name] };
    if (c.test_model) body.model = c.test_model;
    const data = await api('/api/test/channels', { method: 'POST', body });
    const r = (data.results || [])[0] || {};
    if (!r.ok) throw new Error(r.error || '测试未通过');
    const st = (state.snapshot?.channels || []).find((x) => x.name === c.name);
    let extra = '';
    if (st && (st.cooling || (st.cooling_seconds || 0) > 0)) {
      await api('/api/channels/' + encodeURIComponent(c.name) + '/reset', { method: 'POST' });
      extra = '，已解除冷却';
      await loadState();
    }
    const bits = [r.model ? '模型 ' + r.model : '',
      r.latency_ms != null ? r.latency_ms + 'ms' : ''].filter(Boolean);
    toast(`${c.name} 测试通过${bits.length ? '（' + bits.join(' · ') + '）' : ''}${extra}`, 'ok');
  } catch (err) {
    toast(`${c.name} 测试失败：${err.message}`, 'bad');
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
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
    const where = result.source === 'registry' ? '（厂家套餐清单）'
      : result.source === 'ark-quota' ? '（协作奖励免费额度清单）'
      : result.source === 'free-quota' ? '（免费额度清单）' : '';
    toast(`拉取成功：${result.count} 个模型${where}`);
    if (result.hint && !(result.models || []).length) {
      // quota-driven pulls legitimately return an empty list (quota used up);
      // the vendor's own reason beats the generic "empty list" note below.
      list.hidden = false;
      list.innerHTML = `<div class="hint">${esc(result.hint)}</div>`;
    }
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
  // Subscriptions link channels by id; accept the name too for legacy rows.
  const channelId = String((state.config?.channels || [])
    .find((c) => String(c.name) === channelName)?.id || '');
  const channelRefs = new Set([channelName, channelId].filter(Boolean));
  const planModels = new Set();
  (state.subs?.subscriptions || []).forEach((rec) => {
    if ((rec.channels || []).some((r) => channelRefs.has(String(r))) ||
        (rec.channel_names || []).includes(channelName)) {
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
  const refs = pickedChannels(f);
  if (!refs.length) return toast('请先勾选关联渠道', 'bad');
  const drafts = refs.map((ref) =>
    (state.config?.channels || []).find((c) => String(c.id || c.name) === ref) || ref);
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
    refreshTestModelList(names);
    box.innerHTML = '<small class="hint">模型映射中还没有可标注的模型。</small>';
    return;
  }
  refreshTestModelList(names);
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

// Test-model dropdown: the native <datalist> filters its suggestions by the
// typed value, so with a value present the ▼ arrow looked dead. A custom
// panel mirrors the mapping textarea's exposed names instead — the arrow
// always shows every name, typing narrows it, free typing stays allowed.
let testModelNames = [];

function refreshTestModelList(names) {
  testModelNames = names;
  const drop = $('editor-test-model-drop');
  if (drop && !drop.hidden) renderTestModelDrop($('editor-test-model').value);
}

function renderTestModelDrop(filter) {
  const drop = $('editor-test-model-drop');
  if (!drop) return;
  const q = (filter || '').trim().toLowerCase();
  const hits = testModelNames.filter((m) => !q || m.toLowerCase().includes(q));
  drop.innerHTML = hits.length
    ? hits.map((m) => `<div class="tm-item" data-model="${esc(m)}">${esc(m)}</div>`).join('')
    : '<div class="tm-empty">模型映射中还没有匹配的模型</div>';
}

function toggleTestModelDrop(force) {
  const drop = $('editor-test-model-drop');
  if (!drop) return;
  const show = force === undefined ? drop.hidden : force;
  if (show) renderTestModelDrop($('editor-test-model')?.value || '');
  drop.hidden = !show;
}

function providerOptions(selected) {
  return ['<option value="">请选择厂家</option>']
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

// Registry links for the channel editor: 获取 Key / 使用文档 of the selected
// vendor+mode; when no concrete mode is picked fall back to the vendor's first
// mode that has any link. Null when custom/unknown.
function providerLinks(pid, modeKey) {
  const p = (state.providers || []).find((x) => x.id === pid);
  if (!p) return null;
  const mode = modeKey ? p.modes?.[modeKey] : null;
  const src = (mode && (mode.keys_url || mode.docs_url))
    ? mode
    : Object.values(p.modes || {}).find((m) => m.keys_url || m.docs_url);
  if (!src) return null;
  return { keys_url: src.keys_url || '', docs_url: src.docs_url || '' };
}

function renderProviderLinks() {
  const box = $('provider-links');
  if (!box) return;
  const f = $('editor-form');
  const pid = f?.elements.namedItem('provider')?.value || '';
  if (!pid) {
    box.innerHTML = '<small class="hint">选择厂家后，这里会显示注册 / 获取 Key 与使用文档按钮</small>';
    return;
  }
  const links = providerLinks(pid, f?.elements.namedItem('mode')?.value || '');
  const parts = [];
  if (links?.keys_url) {
    parts.push(`<a class="btn small ghost provider-link" href="${esc(links.keys_url)}" target="_blank" rel="noreferrer">注册 / 获取 Key ↗</a>`);
  }
  // 使用文档 opens the bundled manual section for this vendor in an overlay;
  // it intentionally does not jump to the vendor's own docs site.
  parts.push(`<button type="button" class="btn small ghost provider-link" data-guide="${esc(pid)}">使用文档</button>`);
  box.innerHTML = parts.join(' ');
  const btn = box.querySelector('[data-guide]');
  if (btn) btn.onclick = () => showGuideOverlay(btn.dataset.guide);
}

// Vendor management credentials (registry `credential_envs`, e.g. Volcengine
// IAM AK/SK for the free-quota CLI). Channel = account: the values belong to
// the edited channel's own `creds`, so a second vendor account is simply a
// second channel carrying its own pair; the vendor-level config.credentials
// slot stays as the fallback for channels without their own and is NOT
// echoed into these inputs. Round-trip is masked: saved plaintext reaches
// the browser as "***" and an empty input keeps the stored value.
// Registry `credential_help` (steps / permissions / note): the AK/SK
// acquisition walkthrough for vendors whose management keys are genuinely
// hard to obtain (Volcengine IAM + ArkFullAccess, Bailian RAM + workspace
// membership). Rendered as an expandable block so the user gets the exact
// console path right where they type the keys.
function vendorCredHelp(help) {
  if (!help || !Array.isArray(help.steps) || !help.steps.length) return '';
  const items = help.steps.map((s) => `<li>${esc(s)}</li>`).join('');
  const perm = help.permissions
    ? `<p class="cred-help-perm"><strong>权限要求：</strong>${esc(help.permissions)}</p>` : '';
  const note = help.note ? `<p class="cred-help-note">${esc(help.note)}</p>` : '';
  return `<details class="cred-help"><summary>不会获取 AK/SK？点开看详细步骤与权限要求</summary>`
    + `<ol>${items}</ol>${perm}${note}</details>`;
}

function renderVendorCredentials() {
  const box = $('vendor-credentials');
  if (!box) return;
  const f = $('editor-form');
  const pid = f?.elements.namedItem('provider')?.value || '';
  const modeKey = f?.elements.namedItem('mode')?.value || '';
  const mode = registryMode(pid, modeKey) || {};
  const envs = mode.credential_envs || [];
  if (!envs.length) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  const saved = (state.editing >= 0
    ? state.config.channels[state.editing]?.creds : null) || {};
  box.hidden = false;
  box.innerHTML = envs.map((e) => {
    const cur = saved[e.env] || '';
    const shown = cur === '***' ? '' : cur;
    const ph = cur === '***' ? '已配置，留空保持不变' : (cur ? `当前引用 ${cur}` : '未配置（留空则回退厂家级配置）');
    return `<label>${esc(e.label || e.env)}（${esc(e.env)}）</label>
      <input data-cred-env="${esc(e.env)}" type="text" value="${esc(shown)}" placeholder="${esc(ph)}">`;
  }).join('')
    + '<small class="hint">本渠道自己的管理面凭据（渠道即账号：同一厂家的第二个账号就建第二条渠道各填各的），仅用于余量与免费额度查询（火山引擎填 IAM AK/SK，阿里云百炼填 RAM 用户的 AccessKey ID/Secret，都不是数据面 Key）；提交后回显为 ***，留空保持已配值；渠道未填时查询回退到厂家级配置。</small>'
    + vendorCredHelp(mode.credential_help);
}

// --- Vendor free-tier rate presets ----------------------------------------
// Some vendors gate their free tier behind account state the user controls
// (OpenRouter: topping up $10 lifts the daily :free cap 50 -> 1000). Registry
// presets render as buttons above the rate inputs so the user picks the tier
// they actually paid for instead of hand-typing numbers from vendor docs.
function renderRatePresets() {
  const wrap = $('rate-presets');
  if (!wrap) return;
  const f = $('editor-form');
  const mode = registryMode(f.elements.namedItem('provider').value,
    f.elements.namedItem('mode').value);
  const presets = (mode && mode.free_limit_presets) || [];
  if (!presets.length) {
    wrap.hidden = true;
    wrap.innerHTML = '';
    return;
  }
  wrap.hidden = false;
  wrap.innerHTML = '<label>厂家免费档位（点击按档位填入下方限流）</label>'
    + '<div class="channel-picker">'
    + presets.map((p, i) =>
      `<button type="button" class="btn ghost small" data-preset="${i}">${esc(p.label)}</button>`).join('')
    + '</div>'
    + `<small class="hint">${esc(presets.map((p) => `${p.label}：${p.note || '无说明'}`).join('；'))}</small>`;
  wrap.querySelectorAll('[data-preset]').forEach((btn) => {
    btn.onclick = () => {
      const preset = presets[Number(btn.dataset.preset)] || {};
      const rate = preset.rate || {};
      ['rpm', 'rpd', 'tpm', 'tpd'].forEach((k) => {
        f.elements.namedItem(k).value = rate[k] ?? '';
      });
      const hint = wrap.querySelector('small.hint');
      if (hint && preset.note) hint.textContent = `${preset.label}：${preset.note}`;
    };
  });
}

// Selecting a vendor + mode auto-fills base_url / protocol / tier and keeps
// the pair in the draft; the vendor itself is mandatory (a channel without a
// vendor is rejected on save), so the registry entry buttons in the key field
// label row always resolve to a real vendor.
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

// --- Bundled usage manual (docs/api-key-guide.md) --------------------------
// The channel editor's 使用文档 renders our own guide section in an overlay
// instead of jumping to the vendor's docs site; a small link to the official
// docs stays available inside the overlay.

// Heading text in the guide that documents each builtin vendor; vendors not
// listed here fall back to the whole manual. Values must match the guide's
// ### headings exactly (extractGuideSection compares verbatim).
const GUIDE_SECTIONS = {
  pollinations: '3.1 Pollinations——免注册免 Key',
  nvidia: '3.2 NVIDIA NIM——免费层',
  openrouter: '3.3 OpenRouter——免费池',
  volcengine: '4.1 火山引擎方舟',
  zhipu: '4.2 智谱 GLM',
  bailian: '4.3 阿里云百炼',
  modelscope: '4.4 魔搭 ModelScope',
  xiaomi: '4.5 小米 MiMo',
  moonshot: '4.6 Kimi（月之暗面）',
  deepseek: '4.7 DeepSeek',
  hunyuan: '4.8 腾讯混元',
  minimax: '4.9 MiniMax',
  anthropic: '5.1 Anthropic 直连',
  openai: '5.2 OpenAI 直连',
  xai: '5.3 xAI Grok',
  zai: '5.4 Z.AI（智谱海外）',
  opencode: '6.1 OpenCode',
  commandcode: '6.2 Command Code',
};

let guidePayload = null;    // cached /api/guide response
let guideCurrentPid = '';
let guideFullMode = false;  // true while the overlay shows the whole manual

async function loadGuide() {
  if (!guidePayload) guidePayload = await api('/api/guide');
  return guidePayload;
}

// GitHub-style anchor slug so the guide's internal links (目录 / 总览表)
// scroll inside the overlay instead of navigating away.
function mdSlug(text) {
  return String(text).toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, '').trim().replace(/\s+/g, '-');
}

// Content of one guide section: from the exact heading to the next heading of
// the same or higher level. Falls back to the whole manual when not found.
function extractGuideSection(markdown, heading) {
  if (!heading) return markdown;
  const lines = String(markdown).split('\n');
  const start = lines.findIndex((l) => {
    const m = l.match(/^(#{1,6})\s+(.*?)\s*$/);
    return m && m[2] === heading;
  });
  if (start < 0) return markdown;
  const level = lines[start].match(/^#+/)[0].length;
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    const m = lines[i].match(/^(#{1,6})\s+/);
    if (m && m[1].length <= level) { end = i; break; }
  }
  return lines.slice(start, end).join('\n');
}

// Inline markdown for already-block-split text. Input is HTML-escaped first,
// so guide content can never inject markup into the console.
function mdInline(text) {
  let s = esc(text);
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, src) => {
    const href = /^https?:/.test(src) ? src : '/api/guide/img/' + src.replace(/^\.\//, '');
    return `<img data-src="${href}" alt="${alt}">`;
  });
  s = s.replace(/\[([^\]]+)\]\(#([^)\s]+)\)/g, '<a href="#$2" class="guide-anchor">$1</a>');
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  return s;
}

function mdTable(rows) {
  const cells = (row) => row.trim().replace(/^\|/, '').replace(/\|$/, '')
    .split('|').map((c) => c.trim());
  const head = cells(rows[0]);
  const body = rows.slice(2).map(cells);
  return `<div class="md-table"><table><thead><tr>${
    head.map((c) => `<th>${mdInline(c)}</th>`).join('')
  }</tr></thead><tbody>${
    body.map((r) => `<tr>${r.map((c) => `<td>${mdInline(c)}</td>`).join('')}</tr>`).join('')
  }</tbody></table></div>`;
}

// Minimal block renderer for the guide: headings, tables, fenced code, lists
// (with indented continuations such as screenshots), quotes, hr, paragraphs.
function renderMarkdown(text) {
  const lines = String(text).split('\n');
  const out = [];
  const isListItem = (l) => /^\s*([-*]|\d+[.)])\s+/.test(l);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1;
      out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`);
      continue;
    }
    const head = line.match(/^(#{1,6})\s+(.*?)\s*$/);
    if (head) {
      const lvl = head[1].length;
      out.push(`<h${lvl} id="${esc(mdSlug(head[2]))}">${mdInline(head[2])}</h${lvl}>`);
      i += 1;
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || '')) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(lines[i]); i += 1; }
      out.push(mdTable(rows));
      continue;
    }
    if (isListItem(line)) {
      const ordered = /^\s*\d/.test(line);
      const items = [];
      while (i < lines.length) {
        const l = lines[i];
        if (isListItem(l)) {
          items.push(mdInline(l.replace(/^\s*([-*]|\d+[.)])\s+/, '')));
          i += 1;
        } else if (items.length && /^\s{2,}\S/.test(l)) {
          items[items.length - 1] += '<br>' + mdInline(l.trim());
          i += 1;
        } else if (items.length && !l.trim()) {
          // Loose list: a blank line stays inside the list when the next
          // non-blank line is another item or an indented continuation
          // (e.g. a screenshot line), so numbering does not restart.
          let j = i + 1;
          while (j < lines.length && !lines[j].trim()) j += 1;
          if (j < lines.length && (isListItem(lines[j]) || /^\s{2,}\S/.test(lines[j]))) { i = j; continue; }
          break;
        } else break;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}>${items.map((t) => `<li>${t}</li>`).join('')}</${tag}>`);
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ''));
        i += 1;
      }
      out.push(`<blockquote>${buf.map(mdInline).join('<br>')}</blockquote>`);
      continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { out.push('<hr>'); i += 1; continue; }
    if (!line.trim()) { i += 1; continue; }
    const para = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() && !isListItem(lines[i])
      && !/^(#{1,6}\s|```|\s*>|\s*\|)/.test(lines[i])) {
      para.push(lines[i]);
      i += 1;
    }
    out.push(`<p>${para.map(mdInline).join('<br>')}</p>`);
  }
  return out.join('\n');
}

// <img data-src> points at the admin-gated /api/guide/img route, so a plain
// src would 401; fetch with the admin key and swap in a blob URL instead.
// Images hydrate in place — removing and re-appending them would yank every
// screenshot out of its step and pile them up at the end of the guide.
async function hydrateGuideImages() {
  const body = $('guide-body');
  for (const img of Array.from(body.querySelectorAll('img[data-src]'))) {
    const src = img.getAttribute('data-src');
    const alt = img.alt || '';
    try {
      if (!state.adminKey) throw new Error('no admin key');
      const res = await fetch(src, { headers: { 'X-Admin-Key': state.adminKey } });
      if (!res.ok) throw new Error(String(res.status));
      img.src = URL.createObjectURL(await res.blob());
      img.removeAttribute('data-src');
    } catch {
      const note = document.createElement('em');
      note.className = 'hint';
      note.textContent = `截图待补：${alt || src}`;
      img.replaceWith(note);
    }
  }
}

function renderGuideBody() {
  const md = guidePayload?.markdown || '';
  const section = guideFullMode
    ? md
    : extractGuideSection(md, GUIDE_SECTIONS[guideCurrentPid] || '');
  const body = $('guide-body');
  body.innerHTML = renderMarkdown(section);
  body.scrollTop = 0;
  hydrateGuideImages();
}

function closeGuideOverlay() {
  const wrap = $('guide-backdrop');
  if (wrap) wrap.hidden = true;
  document.body.classList.remove('guide-open');
}

// Capture phase: with the channel editor open underneath, the page-wide
// Escape handler must not close the editor while the guide overlay is up.
function guideEscape(e) {
  if (e.key === 'Escape' && !$('guide-backdrop')?.hidden) {
    e.preventDefault();
    e.stopPropagation();
    closeGuideOverlay();
  }
}

async function showGuideOverlay(pid) {
  guideCurrentPid = pid;
  guideFullMode = false;
  const vendor = (state.providers || []).find((p) => p.id === pid);
  let wrap = $('guide-backdrop');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'guide-backdrop';
    wrap.className = 'modal-backdrop guide-backdrop';
    wrap.innerHTML = `<div class="modal guide-modal" role="dialog" aria-modal="true">
      <div class="modal-head">
        <h2 id="guide-title"></h2>
        <div class="guide-head-actions">
          <button type="button" class="btn small ghost" id="guide-toggle">完整手册</button>
          <a class="btn small ghost" id="guide-official" target="_blank" rel="noreferrer" hidden>厂家官方文档 ↗</a>
          <button type="button" class="btn small ghost" id="guide-close">关闭</button>
        </div>
      </div>
      <div class="guide-body" id="guide-body"></div>
    </div>`;
    document.body.appendChild(wrap);
    $('guide-close').onclick = closeGuideOverlay;
    wrap.addEventListener('click', (e) => { if (e.target === wrap) closeGuideOverlay(); });
    $('guide-toggle').onclick = () => {
      if (!guidePayload?.available) return;
      guideFullMode = !guideFullMode;
      $('guide-toggle').textContent = guideFullMode ? '返回本节' : '完整手册';
      renderGuideBody();
    };
    $('guide-body').addEventListener('click', (e) => {
      const a = e.target.closest('a.guide-anchor');
      if (!a) return;
      e.preventDefault();
      const target = $('guide-body').querySelector(
        `[id="${CSS.escape(a.getAttribute('href').slice(1))}"]`);
      if (target) target.scrollIntoView({ block: 'start' });
    });
    document.addEventListener('keydown', guideEscape, true);
  }
  $('guide-title').textContent = `${vendor ? (vendor.name || pid) : pid} · 使用文档`;
  const docs = providerLinks(pid, '');
  if (docs?.docs_url) {
    $('guide-official').href = docs.docs_url;
    $('guide-official').hidden = false;
  } else {
    $('guide-official').hidden = true;
  }
  $('guide-toggle').textContent = '完整手册';
  wrap.hidden = false;
  document.body.classList.add('guide-open');
  $('guide-body').innerHTML = '<p class="hint">正在加载使用手册…</p>';
  try {
    const guide = await loadGuide();
    if (!guide.available) {
      $('guide-toggle').hidden = true;
      $('guide-body').innerHTML = '<p class="hint">本地使用手册不可用（docs/api-key-guide.md 不在运行环境中），可点右上角查看厂家官方文档。</p>';
      return;
    }
    $('guide-toggle').hidden = false;
    renderGuideBody();
  } catch (err) {
    $('guide-body').innerHTML = `<p class="hint">手册加载失败：${esc(err.message)}</p>`;
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
        ${selectField('所属账号（可选）', 'account',
          accountOptions(c.provider || '', c.account || ''),
          '绑定后管理面 AK/SK 取自账号；同一账号可挂多条业务线渠道')}
        ${selectField('业务线', 'business_line',
          businessLineOptions(c.business_line || c.mode || ''),
          '默认跟随计费模式；订阅余量按账号 + 业务线聚合')}
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
        ${field('出站代理（可选）', 'proxy', c.proxy || '', { wide: true, placeholder: '留空直连；国外上游填 http://127.0.0.1:7890，支持 env:HTTP_PROXY' })}
        <div class="field wide">
          <div class="label-row">
            <label for="f-api_key">密钥（直接粘贴明文 API Key）</label>
            <span id="provider-links"></span>
          </div>
          <input id="f-api_key" name="api_key" type="text" value="${esc(c.api_key || '')}" placeholder="直接粘贴 API Key；需要引用环境变量时才写 env:ARK_API_KEY">
        </div>
        <div class="field wide" id="vendor-credentials" hidden></div>
        ${field('优先级（小者靠前）', 'priority', c.priority ?? 100, { type: 'number' })}
        ${field('额度冷却（分钟）', 'cooldown_minutes', c.cooldown_minutes ?? 300, { type: 'number' })}
        <div class="field wide" id="rate-presets" hidden></div>
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
          <small class="hint">免费渠道按免费额度拉取，其余渠道按密钥请求上游 /models：火山引擎（按量付费）配置 IAM AK/SK 后，先从方舟管理面拉有免费额度的模型族名，再结合 /models 归并出可直接调用的版本名；百炼（免费）配置 AccessKey 后执行 bl usage free，只保留仍有免费额度且未过期的 Text 模型</small>
        </div>
        <div id="catalog-list" class="channel-picker" hidden></div>
        <div class="field wide"><label class="check"><input type="checkbox" name="enabled" ${c.enabled === false ? '' : 'checked'}> 启用该渠道</label></div>
        <div class="field wide"><label class="check"><input type="checkbox" name="per_model_quota" ${c.per_model_quota ? 'checked' : ''}> 额度按模型计量（某个模型额度用完只冻结该模型，不冻结整个渠道；余额/欠费错误仍冻结渠道）</label></div>
      </div>
      <div class="test-row">
        <div class="tm-wrap">
          <input id="editor-test-model" placeholder="测试模型（点 ▾ 选映射中的模型或手动填写，留空自动选择）" value="${esc(c.test_model || '')}">
          <button type="button" class="tm-toggle" id="editor-test-model-btn" tabindex="-1" title="展开模型列表">▾</button>
          <div id="editor-test-model-drop" class="tm-drop" hidden></div>
        </div>
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
  const tmInput = $('editor-test-model');
  $('editor-test-model-btn').onclick = () => toggleTestModelDrop();
  tmInput.oninput = () => {
    if (!$('editor-test-model-drop').hidden) renderTestModelDrop(tmInput.value);
  };
  // mousedown on the panel keeps input focus (no blur), so the click below fires.
  $('editor-test-model-drop').onmousedown = (e) => e.preventDefault();
  $('editor-test-model-drop').onclick = (e) => {
    const item = e.target.closest('.tm-item');
    if (!item) return;
    tmInput.value = item.dataset.model;
    toggleTestModelDrop(false);
  };
  $('editor-fetch-models').onclick = fetchChannelModels;
  $('editor-form').onsubmit = (e) => { e.preventDefault(); saveChannelEditor(); };
  $('f-models').addEventListener('input', renderModelTierRows);
  const fProvider = $('f-provider');
  const fMode = $('f-mode');
  fProvider.onchange = () => {
    fMode.innerHTML = providerModeOptions(fProvider.value, '');
    $('f-account').innerHTML = accountOptions(fProvider.value, '');
    applyProviderPreset();
    renderProviderLinks();
    renderVendorCredentials();
    renderRatePresets();
  };
  fMode.onchange = () => {
    // Business line follows the billing mode until the user overrides it.
    const bl = $('f-business_line');
    if (bl && !bl.value && BUSINESS_LINES.some(([v]) => v === fMode.value)) bl.value = fMode.value;
    applyProviderPreset(); renderProviderLinks(); renderVendorCredentials(); renderRatePresets();
  };
  renderModelTierRows();
  renderProviderLinks();
  renderVendorCredentials();
  renderRatePresets();
}

function saveChannelEditor() {
  const f = $('editor-form');
  const val = (n) => (f.elements.namedItem(n)?.value ?? '').trim();
  try {
    const name = val('name');
    const models = parseModels(val('models'));
    if (!name) throw new Error('渠道名称不能为空');
    if (!Object.keys(models).length) throw new Error('至少配置一条模型映射');
    const clash = state.config.channels.some(
      (c, i) => c.name === name && i !== state.editing);
    if (clash) throw new Error('渠道名称与已有渠道重复：' + name);
    const rate = {};
    ['rpm', 'rpd', 'tpm', 'tpd'].forEach((k) => { if (val(k)) rate[k] = Number(val(k)); });
    const tiers = {};
    $('model-tier-rows').querySelectorAll('select').forEach((s) => {
      if (s.value) tiers[s.dataset.model] = s.value;
    });
    const provider = val('provider');
    if (!provider) throw new Error('必须选择厂家：渠道对应厂家，没有厂家就没有渠道');
    // Channel-bound management credentials (channel = account): typed values
    // land on the channel's own creds; untouched fields round-trip the stored
    // masked/env value ("***" or env:REF) so the backend inherits instead of
    // clearing — typing only one of AK/SK must not lose the other.
    const credInputs = f.querySelectorAll('[data-cred-env]');
    const prevCreds = (state.editing >= 0
      ? state.config.channels[state.editing]?.creds : null) || {};
    const ownCreds = {};
    credInputs.forEach((inp) => {
      const env = inp.dataset.credEnv;
      const v = inp.value.trim();
      if (v) ownCreds[env] = v;
      else if (prevCreds[env]) ownCreds[env] = prevCreds[env];
    });
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
      account: val('account'),
      business_line: val('business_line'),
      ...(f.elements.namedItem('per_model_quota').checked ? { per_model_quota: true } : {}),
      ...(Object.keys(ownCreds).length ? { creds: ownCreds } : {}),
      ...(val('editor-test-model') ? { test_model: val('editor-test-model') } : {}),
      ...(val('upstream_path') ? { upstream_path: val('upstream_path') } : {}),
      ...(val('proxy') ? { proxy: val('proxy') } : {}),
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
    await loadConfig();
    await loadState();
    toast(`已保存并热加载：${result.channels} 个渠道，${result.routes.length} 条链`);
    return true;
  } catch (err) {
    toast('配置未保存：' + err.message + '（改动仍在页面中，保持页面会再次自动保存）', 'bad');
    return false;
  }
}

function bindEvents() {
  document.querySelectorAll('.menu-item').forEach((item) => {
    item.onclick = () => switchPage(item.dataset.tab);
  });
  // Back/forward or a hand-edited hash navigates like a menu click.
  window.addEventListener('hashchange', () => {
    const name = pageFromHash();
    if (name && name !== state.tab) switchPage(name);
  });

  $('btn-add-channel').onclick = () => openChannelEditor(-1);
  $('channel-filter').addEventListener('input', (e) => {
    state.channelFilter = e.target.value;
    renderChannels();
    renderSummary();
  });
  $('vendor-filter').addEventListener('input', (e) => {
    state.vendorFilter = e.target.value;
    renderVendors();
  });
  $('btn-add-route').onclick = openRouteModal;
  $('btn-refresh').onclick = () => loadState().catch((e) => toast(e.message, 'bad'));
  $('btn-reload').onclick = async () => {
    try {
      await api('/api/reload', { method: 'POST' });
      await loadConfig(); await loadState();
      toast('已从磁盘重载');
    } catch (err) { toast(err.message, 'bad'); }
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
    if (e.key === 'Escape') {
      const drop = $('editor-test-model-drop');
      if (drop && !drop.hidden) { drop.hidden = true; return; }
      e.preventDefault(); closeModal(); return;
    }
    if (e.key !== 'Tab') return;
    const box = $('modal').querySelector('.modal');
    const list = focusable(box);
    if (!list.length) return;
    if (!box.contains(document.activeElement)) { e.preventDefault(); list[0].focus(); return; }
    const firstEl = list[0], lastEl = list[list.length - 1];
    if (e.shiftKey && document.activeElement === firstEl) { e.preventDefault(); lastEl.focus(); }
    else if (!e.shiftKey && document.activeElement === lastEl) { e.preventDefault(); firstEl.focus(); }
  });

  $('channel-list').addEventListener('change', (e) => {
    const sel = e.target.closest('select[data-auto-model]');
    if (!sel) return;
    const i = Number(sel.dataset.index);
    const c = state.config.channels[i];
    if (!c || !sel.value) return;
    c.models = { ...(c.models || {}), auto: sel.value };
    markDirty();
    renderChannels();
    toast(`「${c.name}」默认模型已切换为 ${sel.value}`);
  });

  $('channel-list').addEventListener('click', async (e) => {
    const resetBtn = e.target.closest('[data-score-channel]');
    if (resetBtn) {
      await api('/api/score/reset', {
        method: 'POST',
        body: { channel: resetBtn.dataset.scoreChannel, wire: resetBtn.dataset.scoreWire },
      });
      await loadState();
      return;
    }
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
    if (action === 'duplicate') {
      try {
        const r = await api('/api/channels/duplicate',
          { method: 'POST', body: { name: b.dataset.name } });
        toast(`已复制为「${r.name}」：密钥与映射一并复制，改好名称/Key 后记得加入路由链`);
        await loadState();
      } catch (err) { toast(err.message, 'bad'); }
    }
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
    if (action === 'test') testChannelRow(b, i);
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

  // Stability-score resets on the 自维护 page share the badge contract with
  // the channel list (data-score-channel/-wire).
  $('score-body').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-score-channel]');
    if (!b) return;
    try {
      await api('/api/score/reset', {
        method: 'POST',
        body: { channel: b.dataset.scoreChannel, wire: b.dataset.scoreWire },
      });
      await loadState();
      toast('已重置该评分');
    } catch (err) { toast(err.message, 'bad'); }
  });

  $('btn-refresh-usage').onclick = () => loadUsage().catch((e) => toast(e.message, 'bad'));

  $('usage-model-search').oninput = (e) => {
    state.usageFilter = e.target.value;
    if (state.usage) renderUsageModels(state.usage);
  };

  document.querySelectorAll('.range-switch .btn').forEach((button) => {
    button.onclick = () => loadUsage(Number(button.dataset.range)).catch((e) => toast(e.message, 'bad'));
  });

  // Member keys page: the shared .range-switch selector above also catches
  // these buttons, so this later binding wins for them.
  document.querySelectorAll('[data-member-range]').forEach((button) => {
    button.onclick = () => loadMembers(Number(button.dataset.memberRange)).catch((e) => toast(e.message, 'bad'));
  });

  $('btn-save-settings').onclick = () => saveSettings(false);
  $('btn-test-notify').onclick = () => saveSettings(true);
  $('btn-check-notify').onclick = checkNotifications;

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

  $('btn-add-provider').onclick = () => openVendorEditor(null);
  $('btn-maintain-providers').onclick = () => maintainVendor(null);
  $('vendor-list').addEventListener('click', (e) => {
    const toggle = e.target.closest('button[data-action="toggle-vendor-models"]');
    if (toggle) {
      const more = toggle.parentElement.querySelector('.models-more');
      if (more) {
        const open = !more.hidden;
        more.hidden = open;
        toggle.textContent = open ? `… 共 ${toggle.dataset.total} 个` : '收起';
      }
      return;
    }
    const b = e.target.closest('button[data-action]');
    if (!b) return;
    const id = b.dataset.id;
    if (b.dataset.action === 'edit-vendor') openVendorEditor(id);
    if (b.dataset.action === 'maintain-vendor') maintainVendor(id);
    if (b.dataset.action === 'restore-vendor') restoreVendor(id);
    if (b.dataset.action === 'add-account') openAccountEditor(b.dataset.provider, '');
    if (b.dataset.action === 'edit-account') openAccountEditor(b.dataset.provider, id);
    if (b.dataset.action === 'delete-account') deleteAccount(id);
  });
}

async function restoreDrafts() {
  const subs = readDraft(DRAFT_SUBS);
  if (subs) {
    // Subscriptions now commit on confirm; a leftover draft means an old
    // page never managed to POST. Offer it once, then clear it either way.
    if (await askConfirm('恢复草稿', '检测到上次未保存的订阅修改，是否恢复并重新保存？')) {
      state.subs = state.subs || { subscriptions: [] };
      state.subs.subscriptions = subs;
      renderSubscriptions();
      try {
        state.subs = await api('/api/subscriptions', {
          method: 'POST', body: { subscriptions: subs },
        });
        renderSubscriptions();
        toast('订阅已保存');
      } catch (err) { toast('订阅未保存：' + err.message, 'bad'); }
    }
    dropDraft(DRAFT_SUBS);
  }
}

async function boot() {
  bindEvents();
  try {
    await loadConfig();
    await restoreDrafts();
    await loadState();
    // Paint the shell first: switchPage makes the dashboard fetch its own
    // data on entry, so the console renders immediately and the rest of the
    // boot payload lands in parallel behind it — a cold vendor snapshot
    // (subscriptions/report) never delays first paint by seconds.
    switchPage(pageFromHash() || 'dashboard');
  } catch (err) {
    toast(err.message, 'bad');
    $('conn-badge').textContent = '连接失败';
    $('conn-badge').className = 'badge bad';
  }
  const rest = await Promise.allSettled([
    loadSubscriptions(),
    loadProviders(),
    loadUsage(),
    loadNotifications(),
    loadRequests(),
    loadMaintenance().catch(() => {}),
    (async () => {
      $('report-month').value = todayISO().slice(0, 7);
      await loadReport();
    })(),
  ]);
  for (const r of rest) {
    if (r.status === 'rejected') {
      toast(r.reason.message, 'bad');
      $('conn-badge').textContent = '连接失败';
      $('conn-badge').className = 'badge bad';
      break;
    }
  }
  setInterval(() => {
    // Live pages poll on their own cadence; other pages stay static.
    if (state.tab === 'dashboard') loadDashboard().catch(() => {});
    if (state.tab === 'status') loadState().catch(() => {});
    if (state.tab === 'requests') loadRequests().catch(() => {});
  }, 5000);
}

boot();
