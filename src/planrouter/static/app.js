'use strict';

const state = {
  config: null,
  snapshot: null,
  tab: 'channels',
  key: sessionStorage.getItem('planrouter_key') || '',
  editing: -1,
  dirty: false,
  subs: null,
  subDirty: false,
  editingSub: -1,
  usage: null,
  usageRange: 30,
  notify: null,
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

async function api(path, opts = {}) {
  const headers = {};
  if (state.key) headers.Authorization = 'Bearer ' + state.key;
  let body;
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, { method: opts.method || 'GET', headers, body });
  if (res.status === 401 && !opts.retried) {
    const input = window.prompt('输入网关密钥（listen.api_key）');
    if (input) {
      state.key = input.trim();
      sessionStorage.setItem('planrouter_key', state.key);
      return api(path, { ...opts, retried: true });
    }
    throw new Error('未授权：需要网关密钥');
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

function markDirty() {
  state.dirty = true;
  $('btn-save').classList.add('dirty');
  $('btn-save').textContent = '保存并应用 *';
}

function clearDirty() {
  state.dirty = false;
  $('btn-save').classList.remove('dirty');
  $('btn-save').textContent = '保存并应用';
}

function normalizeConfig() {
  const c = state.config;
  c.channels = c.channels || [];
  c.routes = c.routes || [];
  c.listen = c.listen || {};
  c.defaults = c.defaults || {};
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

function channelCard(c, i) {
  const models = Object.entries(c.models || {}).map(([k, v]) => k + ' → ' + v).join(' · ');
  return `<article class="card">
    <div class="card-top">
      <div><span class="pill ${esc(c.protocol)}">${esc(c.protocol)}</span><h3>${esc(c.name)}</h3></div>
      <span class="switch ${c.enabled === false ? 'off' : ''}">${c.enabled === false ? '已停用' : '启用'}</span>
    </div>
    <dl>
      <div><dt>端点</dt><dd>${esc(c.base_url)}</dd></div>
      <div><dt>优先级</dt><dd>${esc(c.priority ?? 100)}</dd></div>
      <div><dt>额度冷却</dt><dd>${esc(c.cooldown_minutes ?? 300)} 分钟</dd></div>
      <div><dt>本地限流</dt><dd>${esc(rateText(c.rate))}</dd></div>
      <div><dt>模型映射</dt><dd class="wrap">${esc(models) || '未配置'}</dd></div>
      <div><dt>密钥</dt><dd>${keyText(c.api_key)}</dd></div>
    </dl>
    <div class="card-actions">
      <button class="btn small" data-action="edit" data-index="${i}">编辑</button>
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
  $('status-meta').textContent = `粘性会话 ${s.sticky_sessions} · 每 5 秒自动刷新 · 更新于 ${new Date().toLocaleTimeString()}`;
  $('status-body').innerHTML = s.channels.map((c) => {
    const kind = c.blocked ? 'bad' : (c.cooling ? 'warn' : 'ok');
    const label = c.blocked ? '已停用' : (c.cooling ? '冷却中' : '可用');
    const reason = c.blocked ? c.blocked_reason : c.reason;
    const total = Math.max(c.cooldown_minutes * 60, c.cooling_seconds, 1);
    const pct = Math.min(100, (c.cooling_seconds / total) * 100);
    const remain = c.cooling
      ? `${Math.ceil(c.cooling_seconds / 60)} 分钟<div class="bar"><span style="width:${pct}%"></span></div>` : '—';
    return `<tr>
      <td><span class="dot ${kind}"></span><strong>${esc(c.name)}</strong><br><span class="pill ${esc(c.protocol)}">${esc(c.protocol)}</span></td>
      <td>${label}${reason ? '<br><small>' + esc(reason) + '</small>' : ''}</td>
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

function markSubDirty() {
  state.subDirty = true;
  $('btn-save-subs').classList.add('dirty');
  $('btn-save-subs').textContent = '保存订阅 *';
}

function clearSubDirty() {
  state.subDirty = false;
  $('btn-save-subs').classList.remove('dirty');
  $('btn-save-subs').textContent = '保存订阅';
}

async function loadSubscriptions() {
  state.subs = await api('/api/subscriptions');
  renderSubscriptions();
}

function renderBanner() {
  const summary = state.snapshot && state.snapshot.subscriptions;
  const el = $('renewal-banner');
  if (!summary || !summary.alerts || !summary.alerts.length) {
    el.innerHTML = '';
    return;
  }
  const critical = summary.alerts.some((a) => a.status === 'expired');
  const items = summary.alerts.map((a) => {
    const days = a.status === 'expired' ? `已过期 ${Math.abs(a.days_left)} 天` : `剩 ${a.days_left} 天`;
    return `<span class="banner-item">${esc(a.vendor)} / ${esc(a.account || '—')} / ${esc(a.plan)} · <span class="days">${days}</span></span>`;
  }).join('');
  el.innerHTML = `<div class="banner ${critical ? 'critical' : ''}">
    <span class="banner-title">${critical ? '有套餐已过期' : '套餐即将到期'}</span>
    <span class="banner-items">${items}</span>
    <button class="btn small" id="banner-view">查看订阅</button>
  </div>`;
  $('banner-view').onclick = () => switchTab('subscriptions');
}

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + name));
  if (name === 'status') {
    loadState().catch(() => {});
    loadUsage().catch((e) => toast(e.message, 'bad'));
  }
  if (name === 'subscriptions') {
    loadNotifications().catch((e) => toast(e.message, 'bad'));
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
      <td class="sub-note">${esc((r.channels || []).join(', ')) || '—'}</td>
      <td>
        <button class="btn small" data-action="renew" data-id="${esc(r.id)}">续费</button>
        <button class="btn small ghost" data-action="edit-sub" data-id="${esc(r.id)}">编辑</button>
        <button class="btn small danger" data-action="delete-sub" data-id="${esc(r.id)}">删除</button>
      </td>
    </tr>`;
  }).join('') : '<tr><td colspan="11" class="sub-note">还没有订阅记录，点击「新增订阅」录入厂家、账号、套餐与到期时间。</td></tr>';
}

function openSubEditor(index) {
  state.editingSub = index;
  const r = index >= 0 ? state.subs.subscriptions[index] : {
    vendor: '', account: '', plan: '', tier: '', price: '', currency: 'CNY',
    billing_cycle: 'monthly', started_at: todayISO(), expires_at: '',
    auto_renew: false, remind_days: 7, channels: [], note: '',
  };
  const channels = Array.isArray(r.channels) ? r.channels.join(', ') : (r.channels || '');
  const currencies = ['CNY', 'USD', 'EUR', 'HKD'].map((c) =>
    `<option value="${c}" ${r.currency === c ? 'selected' : ''}>${c}</option>`).join('');
  const cycles = [['monthly', '月付'], ['quarterly', '季付'], ['yearly', '年付'],
    ['one_time', '一次性'], ['custom', '自定义']].map(([v, l]) =>
    `<option value="${v}" ${r.billing_cycle === v ? 'selected' : ''}>${l}</option>`).join('');
  openModal(index >= 0 ? '编辑订阅' : '新增订阅', `
    <form id="sub-form">
      <div class="form-grid">
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
        ${field('关联渠道（逗号分隔）', 'channels', channels, { wide: true, placeholder: 'volcengine-a, volcengine-b' })}
        ${field('备注', 'note', r.note, { wide: true, textarea: true })}
      </div>
      <div class="modal-foot">
        <button type="button" class="btn ghost" id="sub-cancel">取消</button>
        <button type="submit" class="btn primary">确定</button>
      </div>
    </form>`);
  $('sub-cancel').onclick = closeModal;
  $('sub-form').onsubmit = (e) => { e.preventDefault(); saveSubEditor(); };
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
      channels: val('channels').split(',').map((s) => s.trim()).filter(Boolean),
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
  } catch (err) { toast(err.message, 'bad'); }
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

function deleteSub(id) {
  const record = state.subs.subscriptions.find((x) => x.id === id);
  if (!record || !window.confirm(`删除订阅「${record.vendor} / ${record.plan}」？`)) return;
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

function renderAll() {
  renderChannels();
  renderRoutes();
  renderSummary();
  renderStatus();
  renderSubscriptions();
  renderBanner();
  renderUsage();
  renderNotifications();
}

function openModal(title, html) {
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = html;
  $('modal').hidden = false;
}

function closeModal() {
  $('modal').hidden = true;
}

const field = (label, name, value, opts = {}) => `
  <div class="field ${opts.wide ? 'wide' : ''}">
    <label for="f-${name}">${label}</label>
    ${opts.textarea
      ? `<textarea id="f-${name}" name="${name}" rows="4">${esc(value)}</textarea>`
      : `<input id="f-${name}" name="${name}" type="${opts.type || 'text'}" value="${esc(value)}" ${opts.type === 'number' ? 'min="0"' : ''} placeholder="${esc(opts.placeholder || '')}">`}
  </div>`;

function openChannelEditor(index) {
  state.editing = index;
  const c = index >= 0 ? state.config.channels[index] : {
    name: '', protocol: 'openai', base_url: '', api_key: 'env:', models: { auto: '' },
    priority: 100, cooldown_minutes: 300, enabled: true, rate: {},
  };
  const rate = c.rate || {};
  openModal(index >= 0 ? '编辑渠道' : '新增渠道', `
    <form id="editor-form">
      <div class="form-grid">
        ${field('渠道名称', 'name', c.name, { placeholder: 'my-coding-plan' })}
        <div class="field"><label for="f-protocol">协议</label>
          <select id="f-protocol" name="protocol">
            <option value="openai" ${c.protocol === 'openai' ? 'selected' : ''}>openai</option>
            <option value="anthropic" ${c.protocol === 'anthropic' ? 'selected' : ''}>anthropic</option>
          </select></div>
        ${field('上游 base_url', 'base_url', c.base_url, { wide: true, placeholder: 'http://127.0.0.1:8080' })}
        ${field('密钥（env:VAR 或留空不改）', 'api_key', c.api_key || '', { wide: true, placeholder: 'env:MY_PLAN_KEY' })}
        ${field('优先级（小者靠前）', 'priority', c.priority ?? 100, { type: 'number' })}
        ${field('额度冷却（分钟）', 'cooldown_minutes', c.cooldown_minutes ?? 300, { type: 'number' })}
        ${field('RPM', 'rpm', rate.rpm ?? '', { type: 'number' })}
        ${field('RPD', 'rpd', rate.rpd ?? '', { type: 'number' })}
        ${field('TPM', 'tpm', rate.tpm ?? '', { type: 'number' })}
        ${field('TPD', 'tpd', rate.tpd ?? '', { type: 'number' })}
        ${field('模型映射（每行 exposed=wire，*=* 表示透传）', 'models', serializeModels(c.models), { wide: true, textarea: true })}
        <div class="field wide"><label class="check"><input type="checkbox" name="enabled" ${c.enabled === false ? '' : 'checked'}> 启用该渠道</label></div>
      </div>
      <div class="modal-foot">
        <button type="button" class="btn ghost" id="editor-cancel">取消</button>
        <button type="submit" class="btn primary">确定</button>
      </div>
    </form>`);
  $('editor-cancel').onclick = closeModal;
  $('editor-form').onsubmit = (e) => { e.preventDefault(); saveChannelEditor(); };
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
        <button type="submit" class="btn primary">确定</button>
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
  } catch (err) { toast(err.message, 'bad'); }
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
    if (state.dirty && !window.confirm('有未保存的修改，确认放弃并从磁盘重载？')) return;
    try {
      await api('/api/reload', { method: 'POST' });
      await loadConfig(); await loadState(); clearDirty();
      toast('已从磁盘重载');
    } catch (err) { toast(err.message, 'bad'); }
  };
  $('btn-key').onclick = () => {
    const input = window.prompt('网关密钥（留空表示清除）', state.key);
    if (input === null) return;
    state.key = input.trim();
    if (state.key) sessionStorage.setItem('planrouter_key', state.key);
    else sessionStorage.removeItem('planrouter_key');
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
  $('modal').onclick = (e) => { if (e.target === $('modal')) closeModal(); };
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

  $('channel-list').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-action]');
    if (!b) return;
    const i = Number(b.dataset.index);
    const action = b.dataset.action;
    if (action === 'edit') openChannelEditor(i);
    if (action === 'toggle') {
      state.config.channels[i].enabled = state.config.channels[i].enabled === false;
      markDirty(); renderChannels();
    }
    if (action === 'delete') {
      const name = state.config.channels[i].name;
      if (!window.confirm(`删除渠道「${name}」？它也会从所有路由链中移除。`)) return;
      state.config.channels.splice(i, 1);
      removeFromRoutes(name);
      markDirty(); renderAll();
    }
    if (action === 'reset') resetCooldown(b.dataset.name);
  });

  $('route-list').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-action]');
    if (!b) return;
    const ri = Number(b.dataset.index);
    const pos = Number(b.dataset.pos);
    const route = state.config.routes[ri];
    const action = b.dataset.action;
    if (action === 'delete-route') {
      if (!window.confirm(`删除链「${route.name}」？`)) return;
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

async function boot() {
  bindEvents();
  try {
    await loadConfig();
    await loadSubscriptions();
    await loadState();
    await loadUsage();
    await loadNotifications();
  } catch (err) {
    toast(err.message, 'bad');
    $('conn-badge').textContent = '连接失败';
    $('conn-badge').className = 'badge bad';
  }
  setInterval(() => {
    if (state.tab === 'status') loadState().catch(() => {});
  }, 5000);
}

boot();
