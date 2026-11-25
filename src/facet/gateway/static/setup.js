/**
 * 新手配置向导 — modal multi-step wizard for first-time setup.
 *
 * 线性流程（「设置管理密码」这一步在已设置密码时自动跳过）：
 *   欢迎 → [设置管理密码] → 免费/付费偏好 → 选择厂家 → 填写厂家 Key → 测试并保存渠道
 *   → 生成网关 Key → 完成
 *
 * 工程约定（此前版本的教训）：
 * - 渲染始终按 stepId；不带参数的 renderWizard() 重绘当前步，绝不静默跳回第一步。
 * - 页脚按钮（取消/上一步/下一步/完成）按存在与否条件渲染、判空绑定。
 * - 「完成」只有一个入口（页脚），不存在重复 id。
 * - 测试连接走 /api/test/channels（真实发起最小请求），通过后自动把渠道写入配置。
 * - 完成时 wizard_done 双写：localStorage + 服务端 router.json（POST /api/setup/wizard）。
 *
 * 首启流程：seed 不带管理密码，app.js 在控制台打开时强制弹窗设置管理密码，
 * 设置成功后调用 renderWizard('welcome') 进入本向导。控制台「快速开始」永远可手动打开。
 */

'use strict';

// 厂家卡片上的模式优先级：新手向导面向“拿 API Key 按量调用”，payg 最合适。
const WIZARD_MODE_ORDER = ['payg', 'agent', 'coding'];

const wizardState = {
  stepId: 'welcome',
  tier: null,       // 'free' | 'paid' | null（null = 不筛选，全部展示）
  billing: null,    // 付费形态：'payg' | 'token_plan' | 'coding_plan' | 'agent_plan' | null
  provider: null,   // { id, name, modeKey, mode }
  providerKey: '',
  providerCreds: {}, // 管理面 AK/SK（credential_envs），供额度对齐/余量查询
  testResult: null, // { ok, latency, error, saved }
  generatedKey: '',
  // 服务端 keys.json 已落盘的调用密钥（本次向导未新建时从服务端读取真值）。
  // 密钥只存服务端，绝不写入 localStorage/sessionStorage 前端缓存。
  serverKey: '',
};

// 从服务端读当前已落盘的调用密钥：优先启用的 quickstart，其次任一启用 Key。
// 返回密钥明文或 ''（服务端还没有任何启用 Key）。
async function fetchServerGatewayKey() {
  try {
    const payload = await api('/api/keys');
    const rows = Array.isArray(payload && payload.keys) ? payload.keys : [];
    const enabled = rows.filter((r) => r && r.enabled && r.key);
    const pick = enabled.find((r) => r.name === 'quickstart') || enabled[0];
    return pick ? String(pick.key) : '';
  } catch (_) {
    return '';
  }
}

const WIZARD_STEPS = [
  {
    id: 'welcome',
    title: '欢迎使用 Facet',
    description: '几分钟完成首次配置，马上开始多模型调度。',
    async render() {
      return `
        <div class="wizard-step-content">
          <p>向导会带您做三件事：</p>
          <p>1. 管理密码已在首次登录时设置（可随时在「管理密钥」页更换）；</p>
          <p>2. 选择服务形态与厂家，填入 API Key（还没有也没关系，可以跳过）；</p>
          <p>3. 自动测试连通性，并生成一把属于您的调用密钥。</p>
          <p class="hint">随时可以点「取消」退出，之后通过右上角「快速开始」再次打开。</p>
        </div>
      `;
    },
  },
  {
    id: 'admin-key',
    title: '设置管理密码',
    description: '管理密码用于验证敏感操作（修改配置、更换密钥等），建议设置。',
    async render() {
      const has = !!readStoredKey('admin');
      return `
        <div class="wizard-step-content">
          <p>当前管理密码：<span id="wizard-admin-status">检查中…</span></p>
          <div class="actions">
            <button class="btn primary" id="wizard-set-admin">${has ? '更换管理密码' : '设置管理密码'}</button>
          </div>
          <p class="hint">不需要就点「下一步」跳过。</p>
        </div>
      `;
    },
    async onEnter() {
      // 服务端实况探测：本地存的状态可能过期（换机器/服务端已改密码）。
      // 用裸 fetch 而非 api()，避免 401 触发全局「管理密钥」弹框。
      const probe = async (key) => {
        try {
          const r = await fetch('/api/keys', { headers: { 'X-Admin-Key': key } });
          return r.ok;
        } catch (_) { return null; }
      };
      const el = $('wizard-admin-status');
      if (el) {
        const stored = readStoredKey('admin');
        const storedOk = stored ? await probe(stored) : false;
        let text;
        if (storedOk === null) text = '无法连接服务端，请稍后重试';
        else if (storedOk) text = '已设置（本机已保存，可直接更换）';
        else {
          // 无凭据的回环控制台保持开放（探测带空管理密钥即可 200）；
          // 其余 401 一律视为服务端已设密钥或管理面已锁定，没有默认密码。
          const openOk = await probe('');
          if (openOk === null) text = '无法连接服务端，请稍后重试';
          else if (openOk) text = '管理控制台未设密钥（本机开放），建议现在设置';
          else text = '服务端需要管理密钥（已设置或管理面已锁定），本机未保存';
        }
        el.textContent = text;
      }
      const btn = $('wizard-set-admin');
      if (!btn) return;
      btn.onclick = async () => {
        const { value } = await askText({
          title: '设置管理密码',
          message: '输入新的管理密码，设置后请妥善保管。',
          password: true,
          confirmText: '确定',
        });
        if (!value) return;
        try {
          await api('/api/admin/key', {
            method: 'POST',
            body: { key: value, old_key: readStoredKey('admin') || '' },
          });
          state.adminKey = value;
          storeKey('admin', value, true);
          toast('管理密码已更新');
          renderWizard();
        } catch (e) {
          toast(e.message, 'bad');
        }
      };
    },
  },
  {
    id: 'pick-tier',
    title: '想要哪种服务形态？',
    description: '厂家提供的服务形态都已列出：有免费额度的可直接白嫖，付费的按形态选择。',
    async render() {
      return `
        <div class="wizard-step-content">
          <div class="wizard-provider-list" id="wizard-tier-list">
            <p class="hint">正在加载厂家服务形态…</p>
          </div>
        </div>
      `;
    },
    async onEnter() {
      const list = $('wizard-tier-list');
      if (!list) return;
      let providers = [];
      try {
        const payload = await api('/api/providers');
        providers = Array.isArray(payload) ? payload : (payload.providers || []);
      } catch (e) {
        list.innerHTML = `<p class="hint">厂家列表加载失败：${esc(e.message)}</p>`;
        return;
      }
      const freeNames = [];
      const shapes = {}; // billing -> [厂家名]
      providers.forEach((p) => {
        Object.entries(p.modes || {}).forEach(([k, m]) => {
          if (wizardHasFree(m) && !freeNames.includes(p.name || p.id)) freeNames.push(p.name || p.id);
          // 免费额度的按量模式（如火山 payg）同样按形态归类，
          // 这样「按量付费」筛选也能看到带免费额度的厂家。
          shapes[wizardBilling(k, m)] = (shapes[wizardBilling(k, m)] || [])
            .concat(p.name || p.id);
        });
      });
      const shapeHint = (names) => names.length
        ? `${names.slice(0, 3).join('、')}${names.length > 3 ? ` 等 ${names.length} 家` : `（${names.length} 家）`}`
        : '暂无厂家';
      const options = [
        `<div class="wizard-provider-card" data-tier="free" title="只展示有免费额度的厂家">
          <strong>有免费额度优先</strong>
          <div><span class="tag active">免费额度</span><span class="hint">${esc(shapeHint(freeNames))}，先不花钱跑通流程</span></div>
        </div>`,
        ...['payg', 'token_plan', 'coding_plan', 'agent_plan']
          .filter((b) => (shapes[b] || []).length)
          .map((b) => `
            <div class="wizard-provider-card" data-tier="paid" data-billing="${b}"
                 title="只展示提供${WIZARD_BILLING_LABELS[b]}的厂家">
              <strong>${WIZARD_BILLING_LABELS[b]}</strong>
              <div><span class="tag">付费</span><span class="hint">${esc(shapeHint(shapes[b]))}</span></div>
            </div>`),
      ].join('');
      list.innerHTML = options;
      list.querySelectorAll('.wizard-provider-card').forEach((card) => {
        card.onclick = () => {
          wizardState.tier = card.dataset.tier;
          wizardState.billing = card.dataset.billing || null;
          renderWizard('pick-provider');
        };
      });
    },
    async onNext() {
      if (!wizardState.tier) {
        toast('请先点选一种服务形态');
        return false;
      }
      return true;
    },
  },
  {
    id: 'pick-provider',
    title: '选择模型厂家',
    description: '已按您选择的服务形态筛选；有免费额度的卡片会标明具体额度。点击卡片选择一家。',
    async render() {
      return `
        <div class="wizard-step-content">
          <div class="wizard-provider-list" id="wizard-provider-list">
            <p class="hint">正在加载厂家列表…</p>
          </div>
        </div>
      `;
    },
    async onEnter() {
      const list = $('wizard-provider-list');
      if (!list) return;
      let providers = [];
      try {
        const payload = await api('/api/providers');
        providers = Array.isArray(payload) ? payload : (payload.providers || []);
      } catch (e) {
        list.innerHTML = `<p class="hint">厂家列表加载失败：${esc(e.message)}</p>`;
        return;
      }
      const tier = wizardState.tier;
      const entries = providers
        .map((p) => ({ p, pick: wizardPickMode(p, tier, wizardState.billing) }))
        .filter((e) => e.pick);
      if (!entries.length) {
        list.innerHTML = tier
          ? '<p class="hint">当前偏好下暂无可选厂家。可点「上一步」更换偏好，'
            + '或<a href="javascript:void(0)" id="wizard-show-all-providers">查看全部厂家</a>。</p>'
          : '<p class="hint">暂无可选厂家。</p>';
        const showAll = $('wizard-show-all-providers');
        if (showAll) {
          showAll.onclick = () => {
            wizardState.tier = null;
            wizardState.billing = null;
            renderWizard('pick-provider');
          };
        }
        return;
      }
      const cards = entries.map(({ p, pick }) => {
        const { modeKey, mode } = pick;
        const tierTag = wizardHasFree(mode) ? '<span class="tag active">有免费额度</span>'
          : '<span class="tag">付费</span>';
        const summary = wizardFreeSummary(mode);
        const quotaLine = summary ? `<div class="wizard-quota">${esc(summary)}</div>` : '';
        return `
          <div class="wizard-provider-card" data-provider-id="${esc(p.id)}"
               data-mode-key="${esc(modeKey)}" title="${esc(mode.label || '')}">
            <strong>${esc(p.name || p.id)}</strong>
            <div>${tierTag}<span class="hint">${esc(mode.label || modeKey)}</span></div>
            ${quotaLine}
          </div>
        `;
      }).join('');
      list.innerHTML = cards;
      list.querySelectorAll('.wizard-provider-card').forEach((card) => {
        card.onclick = () => {
          const id = card.dataset.providerId;
          const modeKey = card.dataset.modeKey;
          const entry = providers.find((p) => p.id === id);
          if (!entry) return;
          wizardState.provider = {
            id,
            name: entry.name || id,
            modeKey,
            mode: entry.modes[modeKey],
          };
          renderWizard('provider-key');
        };
      });
    },
  },
  {
    id: 'provider-key',
    title: '填写厂家 API Key',
    description: 'Key 只会保存在您本机的配置文件里，不会上传到任何第三方。',
    async render() {
      const p = wizardState.provider;
      if (!p) return '<div class="wizard-step-content"><p>尚未选择厂家，请点「上一步」返回选择。</p></div>';
      const mode = p.mode || {};
      const manual = `<a href="#" data-wizard-guide="${esc(p.id)}">查看使用手册</a>`;
      const docs = mode.keys_url
        ? `<p class="hint">还没有 Key？<a href="${esc(mode.keys_url)}" target="_blank" rel="noopener">前往官方控制台获取 ↗</a>　·　${manual}</p>`
        : `<p class="hint">填写前建议先读 ${manual}（含免费额度说明与凭据获取步骤）。</p>`;
      // 管理面凭据（如火山 IAM AK/SK、百炼 RAM AK/SK）：免费额度对齐与
      // 套餐余量查询用它签名调厂家管理 API，缺了就拉不到真实额度。
      // 分类口径来自后端 mode.meta.cred_kind（mode_meta.py 单点），
      // meta 缺失时回退到 credential_envs 是否声明（两者当前等价）。
      const envs = mode.credential_envs || [];
      const credKind = (mode.meta && mode.meta.cred_kind)
        || (envs.length ? 'aksk' : 'key');
      const credFields = credKind === 'aksk' && envs.length ? `
          <div class="wizard-cred-block">
            <p class="hint">该厂家的免费额度/余量查询还需要管理面凭据（与上方 API Key 不同），建议一并填写：</p>
            <div class="form-grid">
              ${envs.map((e) => `
              <div class="field">
                <label>${esc(e.label || e.env)}（${esc(e.env)}）</label>
                <input type="text" data-wizard-cred="${esc(e.env)}" autocomplete="off" spellcheck="false"
                       placeholder="${esc(e.env)}，可稍后在「渠道配置」补填">
              </div>`).join('')}
            </div>
            ${typeof vendorCredHelp === 'function' ? vendorCredHelp(mode.credential_help) : ''}
          </div>` : '';
      // 「免费」偏好落到渠道档位前先说清楚后果：赠送额度型厂家烧完额度
      // 会按量扣费，透明告知比静默标注更安全。
      const tierNote = wizardState.tier === 'free' && wizardHasFree(mode) && mode.tier !== 'free'
        ? '<p class="hint">按您选择的「免费」偏好，保存后该渠道会标为免费档：进免费池与 $free 宏、参与免费额度对齐。注意：赠送额度用尽后厂家会按量扣费，请留意用量报表；也可在「渠道配置」编辑渠道改回收费档。</p>'
        : '';
      return `
        <div class="wizard-step-content">
          <p>厂家：<strong>${esc(p.name)}</strong>（${esc(mode.label || p.modeKey)}）</p>
          ${docs}
          ${tierNote}
          <div class="form-grid">
            <div class="field">
              <label>API Key</label>
              <input type="text" id="wizard-provider-key" autocomplete="off" spellcheck="false"
                     placeholder="粘贴厂家的 API Key">
            </div>
          </div>
          ${credFields}
          <p class="hint">暂时没有 Key？留空点「下一步」可以跳过，稍后在「渠道配置」里补填。</p>
        </div>
      `;
    },
    async onNext() {
      const input = $('wizard-provider-key');
      wizardState.providerKey = input ? input.value.trim() : '';
      wizardState.providerCreds = {};
      document.querySelectorAll('[data-wizard-cred]').forEach((inp) => {
        const v = inp.value.trim();
        if (v) wizardState.providerCreds[inp.dataset.wizardCred] = v;
      });
      return true;
    },
  },
  {
    id: 'test-save',
    title: '测试并保存渠道',
    description: '向导会拉取厂家模型目录，自动逐个试到打通为止（优先该厂家自有模型），全程显示每个模型的测试结果。',
    async render() {
      return `
        <div class="wizard-step-content">
          <div id="wizard-test-status" class="hint">正在准备测试…</div>
          <div id="wizard-test-log" class="wizard-test-log"></div>
        </div>
      `;
    },
    async onEnter() {
      const status = $('wizard-test-status');
      if (!status) return;
      const p = wizardState.provider;
      if (!p || !wizardState.providerKey) {
        wizardState.testResult = { ok: false, skipped: true };
        status.innerHTML = '已跳过测试（未选择厂家或未填写 Key）。'
          + '稍后可在「渠道配置」页手动添加渠道。';
        return;
      }
      const mode = p.mode || {};
      const modelList = (mode.models || []).slice(0, 20);
      const draft = {
        name: wizardChannelName(p.id),
        protocol: mode.protocol,
        provider: p.id,
        mode: p.modeKey,
        base_url: mode.base_url,
        api_key: wizardState.providerKey,
        creds: wizardState.providerCreds,
        models: {},
        priority: 100,
        // 渠道档位：向导偏好「免费」且所选模式带免费额度时按偏好标 free
        // （进免费池与 $free 宏、cheap/free 链，免费额度对齐/目录拉取/
        // 额度包账本都按免费档生效）；其余透传注册表 mode.tier，向导建的
        // 渠道不能是「未标注」状态。赠送额度型厂家额度烧完按量扣费，
        // 渠道编辑器里可随时改档。
        tier: (wizardState.tier === 'free' && wizardHasFree(mode))
          ? 'free' : (mode.tier || ''),
        enabled: true,
      };
      const logBox = $('wizard-test-log');
      const rowFor = (m) => {
        const row = document.createElement('div');
        row.className = 'wizard-test-row';
        row.innerHTML = `<span class="wt-model">${esc(m)}</span>`
          + '<span class="wt-result pending">测试中…</span>';
        logBox.appendChild(row);
        return row;
      };
      const settle = (row, ok, text) => {
        const el = row.querySelector('.wt-result');
        el.textContent = text;
        el.className = 'wt-result ' + (ok ? 'ok' : 'bad');
      };
      try {
        // 注册表里写死的模型清单会过时（上游下线后一测就 404）：一律现拉
        // 一次真实目录（渠道配置页「拉取模型清单」同款接口），以目录为
        // 候选池；目录拉不出来再回退注册表清单。
        let catalog = [];
        status.innerHTML = '正在拉取该厂家的模型目录…';
        try {
          const cat = await api('/api/probe/models',
            { method: 'POST', body: { channel: draft } });
          if (cat && cat.ok && Array.isArray(cat.models)) {
            catalog = cat.models.slice();
          }
        } catch (_) { /* 目录失败不致命：回退注册表清单 */ }

        // 候选排序：厂家自有模型（如 nvidia/…）优先，免费偏好下 :free 再
        // 优先；明显非对话模型（embed/asr/tts/rerank…）不参与对话测试。
        // 目录是字母序、第三方 id 常排最前（acme/… < nvidia/…），所以不采
        // 信服务端 suggested（它指向字母序头部），由这里重新排序。
        const NON_CHAT = /(embed|asr|tts|rerank|whisper|ocr|guard|moderation|reward|stable-?diffusion|sdxl)/i;
        const chat = catalog.filter((m) => !NON_CHAT.test(m));
        const ids = [];
        const push = (m) => { if (ids.length < 20 && !ids.includes(m)) ids.push(m); };
        if (draft.tier === 'free') {
          // 免费档只测免费模型，复用全局 isFreeModelId（与渠道编辑器「只选免费」、
          // 后端 model_tier 同口径）：OpenRouter 只有 :free/-free 后缀算免费，
          // 其 openrouter/auto 等不带后缀的是付费路由，未充值账号调它们会被
          // 上游拒绝。厂家自有免费模型排前，其余按目录序。若该厂家免费模型
          // 不带后缀（如 nvidia 整层免费），过滤为空时回退全量候选，保证
          // 自动选型不卡死。
          const freeChat = chat.filter((m) => isFreeModelId(m));
          const own = freeChat.filter((m) => m.startsWith(p.id + '/'));
          const others = freeChat.filter((m) => !own.includes(m));
          own.forEach(push);
          others.forEach(push);
          if (!ids.length) {
            chat.filter((m) => m.startsWith(p.id + '/')).forEach(push);
            chat.filter((m) => !m.startsWith(p.id + '/')).forEach(push);
          }
        } else {
          const own = chat.filter((m) => m.startsWith(p.id + '/'));
          const others = chat.filter((m) => !own.includes(m));
          own.forEach(push);
          others.forEach(push);
        }
        if (!ids.length) modelList.forEach(push);
        if (!ids.length) {
          throw new Error('上游未返回模型清单，无法自动选型；'
            + '可在「渠道配置」页手动添加渠道并填写模型映射。');
        }

        // 逐个试到打通为止：404/429/5xx 这类模型级失败换下一个模型继续；
        // 401/403 是 Key/账号问题，换模型救不了，立即停。单模型 10 秒
        // 超时就跳过（服务端 clamp 3..60）：轮询给了重试机会，不值得为
        // 一个卡住的候选干等；控制台的单次手动测试仍是默认 60 秒。
        const tries = ids.slice(0, 12);
        let winner = null;
        let hardFail = '';
        for (let i = 0; i < tries.length; i += 1) {
          const m = tries[i];
          status.innerHTML = `正在测试模型（${i + 1}/${tries.length}）：${esc(m)}…`;
          const row = rowFor(m);
          try {
            const data = await api('/api/test/channels', {
              method: 'POST',
              body: { timeout: 10, channels: [{ ...draft, models: { auto: m, [m]: m } }] },
            });
            const r = (data.results || [])[0] || {};
            if (r.ok) {
              winner = { model: r.model || m, latency: r.latency_ms };
              settle(row, true, '✓ ' + (r.latency_ms != null ? r.latency_ms + 'ms' : '通过'));
              break;
            }
            settle(row, false, '✗ ' + (r.error || '失败'));
            if (/HTTP 40[13]\b/.test(r.error || '')) hardFail = r.error;
          } catch (e) {
            settle(row, false, '✗ ' + e.message);
          }
        }

        if (winner) {
          // 最终映射按测试顺序填满坑位，auto 指向实测打通的那个模型——
          // 不再相信目录字母序头部（那正是过时 404 的来源）。
          const ordered = [winner.model];
          ids.forEach((m) => {
            if (ordered.length < 20 && !ordered.includes(m)) ordered.push(m);
          });
          ordered.forEach((m) => { draft.models[m] = m; });
          draft.models.auto = winner.model;
          await wizardSaveChannel(draft);
          wizardState.testResult = {
            ok: true, latency: winner.latency, model: winner.model, saved: true,
          };
          const bits = ['模型 ' + esc(winner.model),
            winner.latency != null ? winner.latency + 'ms' : '',
            tries.length > 1 ? `第 ${tries.indexOf(winner.model) + 1} 次尝试打通` : '',
          ].filter(Boolean).join(' · ');
          status.innerHTML = `测试通过，渠道「${esc(draft.name)}」已保存并生效。<br>(${bits})`;
          status.className = 'wizard-test-result ok';
        } else if (hardFail) {
          wizardState.testResult = { ok: false, error: hardFail, saved: false };
          status.innerHTML = `测试未通过：${esc(hardFail)}<br>`
            + '这是 Key 或账号问题（换哪个模型都一样）。请点「上一步」检查 Key 后重试。';
          status.className = 'wizard-test-result bad';
        } else {
          wizardState.testResult = {
            ok: false, error: `已尝试 ${tries.length} 个模型均未打通`, saved: false,
          };
          status.innerHTML = `已依次尝试 ${tries.length} 个模型（明细如上），均未打通。<br>`
            + '渠道暂未保存。可点「上一步」检查 Key 或代理设置，或继续下一步稍后在「渠道配置」页手动配置。';
          status.className = 'wizard-test-result bad';
        }
      } catch (e) {
        wizardState.testResult = { ok: false, error: e.message, saved: false };
        status.innerHTML = `未能完成测试：${esc(e.message)}<br>`
          + '渠道暂未保存。可点「上一步」检查 Key 后重试，或继续下一步稍后在「渠道配置」页手动配置。';
        status.className = 'wizard-test-result bad';
      }
    },
  },
  {
    id: 'gateway-key',
    title: '生成调用密钥',
    description: '这把 Key 就是您的程序调用 Facet 时使用的凭证。',
    async render() {
      // 密钥真值以服务端落盘为准：本次生成过用本次的，否则读服务端已有 Key。
      if (!wizardState.generatedKey && !wizardState.serverKey) {
        wizardState.serverKey = await fetchServerGatewayKey();
      }
      const existing = wizardState.generatedKey || wizardState.serverKey;
      const shown = existing
        ? `<div class="form-grid"><div class="field">
             <label>调用密钥</label>
             <input type="text" id="wizard-generated-key" readonly value="${esc(existing)}">
           </div></div>
           <div class="actions"><button class="btn ghost" id="wizard-copy-key">复制</button>
           <button class="btn ghost" id="wizard-regen-key">重新生成</button></div>`
        : '<div class="actions"><button class="btn primary" id="wizard-gen-key">生成调用密钥</button></div>';
      return `
        <div class="wizard-step-content">
          ${shown}
          <p class="hint">${existing ? '妥善保管，泄露后可在「API Keys」页吊销重发。' : '点击按钮即可生成。'}</p>
        </div>
      `;
    },
    async onEnter() {
      const gen = $('wizard-gen-key');
      if (gen) {
        gen.onclick = async () => {
          try {
            const record = await api('/api/keys', { method: 'POST', body: { name: 'quickstart' } });
            wizardState.generatedKey = record.key || '';
            state.key = wizardState.generatedKey;
            toast('调用密钥已生成（已落盘服务端配置目录）');
            renderWizard();
          } catch (e) {
            toast(e.message, 'bad');
          }
        };
      }
      const regen = $('wizard-regen-key');
      if (regen) {
        regen.onclick = async () => {
          try {
            // 重新生成前先吊销旧密钥（服务端真值），避免 quickstart Key 越积越多。
            const current = wizardState.generatedKey || wizardState.serverKey;
            if (current) {
              await api('/api/keys/delete', { method: 'POST', body: { key: current } });
            }
            const record = await api('/api/keys', { method: 'POST', body: { name: 'quickstart' } });
            wizardState.generatedKey = record.key || '';
            wizardState.serverKey = '';
            state.key = wizardState.generatedKey;
            toast('已生成新密钥（旧密钥已吊销）');
            renderWizard();
          } catch (e) {
            toast(e.message, 'bad');
          }
        };
      }
      const copy = $('wizard-copy-key');
      if (copy) copy.onclick = () => copyText($('wizard-generated-key')?.value || '');
    },
  },
  {
    id: 'verify',
    title: '端到端验收',
    description: '用刚生成的调用密钥走真实路由链发起一次最小调用——单渠道测试通过不代表整条链可用，这一步才算数。',
    async render() {
      return `
        <div class="wizard-step-content">
          <div id="wizard-verify" class="hint">正在通过路由链发起真实调用，请稍候…</div>
        </div>
      `;
    },
    async onEnter() {
      wizardVerify();
    },
    async onNext() {
      // 验收失败不阻塞完成：失败面板已给出修复入口，用户可选择先跳过。
      return true;
    },
  },
  {
    id: 'finish',
    title: '一切就绪',
    description: '配置完成！复制下面的信息到您的工具里即可开始使用。',
    async render() {
      const key = wizardState.generatedKey || wizardState.serverKey;
      const url = location.origin;
      return `
        <div class="wizard-step-content">
          <div class="wizard-config-summary">
            <div class="form-grid">
              <div class="field">
                <label>网关地址（OpenAI 兼容）</label>
                <input type="text" id="wizard-gateway-url" readonly value="${esc(url)}">
              </div>
              <div class="field">
                <label>调用密钥</label>
                <input type="text" id="wizard-gateway-key" readonly value="${esc(key || '沿用现有 Key')}">
              </div>
            </div>
          </div>
          <div class="actions"><button class="btn ghost" id="wizard-copy-config">复制配置</button></div>
          <div class="wizard-curl">curl ${esc(url)}/v1/chat/completions \\
  -H "Authorization: Bearer ${esc(key || 'YOUR-KEY')}" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}]}'</div>
          <p class="hint">模型名填 auto 会按路由链自动调度；渠道可在「渠道配置」页调整，免费额度可在「免费模型」页查看。</p>
        </div>
      `;
    },
    async onEnter() {
      const copy = $('wizard-copy-config');
      if (!copy) return;
      copy.onclick = () => {
        const url = $('wizard-gateway-url')?.value || '';
        const key = $('wizard-gateway-key')?.value || '';
        copyText(`${url}\n${key}`);
      };
    },
  },
];

// 付费形态归类：显式 billing 字段优先（注册表可标注特殊套餐），否则按 mode key 推断。
const WIZARD_BILLING_LABELS = {
  payg: '按量付费',
  token_plan: 'Token Plan 包月',
  coding_plan: 'Coding Plan 包月',
  agent_plan: 'Agent Plan 包月',
};

// 是否带免费额度：后端注入的归一化分类（mode.meta.free_kind，来自
// mode_meta.py 单一实现点）优先；meta 缺失时回退到旧信号推断，口径一致。
// 免费形态四类：one_time（一次性赠额）/ daily（每日循环）/ forever（长期
// 免费模型）/ voucher（注册代金券），详见 docs/免费与接入模式统一建模设计.md。
function wizardHasFree(mode) {
  if (mode && mode.meta && mode.meta.free_kind) {
    return mode.meta.free_kind !== 'none';
  }
  return mode.tier === 'free' || !!mode.free_quota || !!mode.free_limits
    || !!(mode.free_models || []).length;
}
function wizardBilling(modeKey, mode) {
  if (mode.billing && WIZARD_BILLING_LABELS[mode.billing]) return mode.billing;
  if (modeKey === 'coding') return 'coding_plan';
  if (modeKey === 'agent') return 'agent_plan';
  return 'payg';
}

// 按预算偏好（'free' | 'paid' | null）与付费形态（'payg' | 'token_plan' | ... | null）
// 挑选该厂家最合适的接入模式：先筛出匹配 tier/形态的模式，再按 payg 优先排序。
function wizardPickMode(provider, tier, billing) {
  const modes = provider.modes || {};
  const keys = Object.keys(modes);
  if (!keys.length) return null;
  const wanted = tier
    ? keys.filter((k) => (tier === 'free'
      ? wizardHasFree(modes[k])
      : !billing || wizardBilling(k, modes[k]) === billing))
    : keys;
  const ordered = [
    ...WIZARD_MODE_ORDER.filter((k) => wanted.includes(k)),
    ...wanted.filter((k) => !WIZARD_MODE_ORDER.includes(k)),
  ];
  if (!ordered.length) return null;
  return { modeKey: ordered[0], mode: modes[ordered[0]] };
}

// 免费额度摘要：优先用注册表 free_limits.note（与渠道页口径一致），
// 否则从 free_quota（tokens/requests + period）格式化出简短额度文本。
function wizardFreeSummary(mode) {
  const note = (mode.free_limits || {}).note || '';
  if (note) return note;
  const quota = mode.free_quota || {};
  if (!quota.tokens && !quota.requests) return '';
  const period = quota.period === 'month' ? '/月'
    : quota.period === 'day' ? '/天' : ' 总额度';
  const parts = [];
  if (quota.tokens) {
    const n = quota.tokens;
    const amount = n >= 10000
      ? `${Math.round(n / 10000).toLocaleString()} 万`
      : n.toLocaleString();
    parts.push(`${amount} tokens${period}`);
  }
  if (quota.requests) parts.push(`${quota.requests.toLocaleString()} 次${period}`);
  return parts.join(' · ');
}

// 生成不与现有渠道重名的渠道名：openai → openai, openai → openai-2 …
function wizardChannelName(base) {
  const channels = (state.config && state.config.channels) || [];
  const taken = new Set(channels.map((c) => c.name));
  if (!taken.has(base)) return base;
  for (let i = 2; ; i += 1) {
    const name = `${base}-${i}`;
    if (!taken.has(name)) return name;
  }
}

// 向导渠道落链：不加进任何路由链的渠道永远不会被排程到——facet-auto 测得
// 再通也用不上。anthropic 渠道进 anthropic:default；openai 渠道进 default
// 尾部，tier=free 的免费层渠道同时进 cheap / free 链（「免费」偏好下赠送
// 额度型厂家也按偏好标 free，与渠道页「免费池」口径一致）。
function wizardRouteAppend(draft) {
  const routes = state.config.routes || (state.config.routes = []);
  const ensure = (protocol, name) => {
    let route = routes.find((r) => r.protocol === protocol && r.name === name);
    if (!route) {
      route = { name, protocol, channels: [] };
      routes.push(route);
    }
    route.channels = route.channels || [];
    return route;
  };
  if (draft.protocol === 'anthropic') {
    ensure('anthropic', 'default').channels.push(draft.name);
    return;
  }
  ensure('openai', 'default').channels.push(draft.name);
  if (draft.tier === 'free') {
    ensure('openai', 'cheap').channels.push(draft.name);
    ensure('openai', 'free').channels.push(draft.name);
  }
}

// 把向导构造的渠道追加进配置并保存（沿用控制台的保存路径，带热加载）。
// 每次提交前都现拉最新配置再追加：向导打开期间设管理密码等步骤会改
// router.json，拿页面打开时的缓存一提交就会撞版本校验；提交瞬间仍可能
// 被并发改动抢先，撞了就重拉重追加再试，最多三轮。
async function wizardSaveChannel(draft) {
  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    state.config = await api('/api/config');
    if (state.config && typeof state.config === 'object') {
      state.config.channels = state.config.channels || [];
      state.config.routes = state.config.routes || [];
    }
    state.config.channels.push(draft);
    wizardRouteAppend(draft);
    try {
      await api('/api/config', { method: 'POST', body: state.config });
      if (typeof loadConfig === 'function') await loadConfig();
      if (typeof loadState === 'function') await loadState();
      return;
    } catch (e) {
      lastError = e;
      if (!/version mismatch/.test(e.message || '')) break;
    }
  }
  if (typeof loadConfig === 'function') await loadConfig();
  throw new Error('渠道已测试通过，但保存失败：'
    + (lastError ? lastError.message : '未知原因'));
}

// 端到端验收：用刚生成的网关 Key 走真实路由链发起一次最小调用。
// 成功展示命中的厂家/模型与 attempt 轨迹；失败展示错误译文、
// 修复建议与修复入口（渠道配置 / 路由链 / 原地重试）。
function wizardBindVerifyRetry() {
  const retry = $('wizard-verify-retry');
  if (retry) retry.onclick = () => wizardVerify();
}

async function wizardVerify() {
  const box = $('wizard-verify');
  if (!box) return;
  // 密钥真值以服务端落盘为准；一把 Key 都没有时提示回上一步生成。
  if (!wizardState.generatedKey && !wizardState.serverKey) {
    wizardState.serverKey = await fetchServerGatewayKey();
  }
  const key = wizardState.generatedKey || wizardState.serverKey;
  if (!key) {
    box.className = 'wizard-test-result bad';
    box.innerHTML = '尚未生成调用密钥，请回到上一步生成后再验收。'
      + '<div class="actions"><button class="btn ghost" id="wizard-verify-retry">重新验收</button></div>';
    wizardBindVerifyRetry();
    return;
  }
  box.className = 'hint';
  box.innerHTML = '正在通过路由链发起真实调用，请稍候…（可能产生极少量上游费用）';
  api('/api/setup/verify-call', {
    method: 'POST',
    body: { key },
  }).then((r) => wizardRenderVerify(r)).catch((e) => {
    box.className = 'wizard-test-result bad';
    box.innerHTML = `验收请求失败：${esc(e.message)}`
      + '<div class="actions"><button class="btn ghost" id="wizard-verify-retry">重新验收</button></div>';
    wizardBindVerifyRetry();
  });
}

function wizardRenderVerify(r) {
  const box = $('wizard-verify');
  if (!box) return;
  const retryBtn = '<div class="actions"><button class="btn ghost" id="wizard-verify-retry">重新验收</button></div>';
  const attempts = Array.isArray(r.attempts) ? r.attempts : [];
  if (r.ok) {
    const hit = r.hit || {};
    const hitText = [hit.channel, hit.wire_model].filter(Boolean).join(' / ');
    const rows = attempts.map((a) => {
      const parts = [a.channel, a.wire_model, WIZARD_ATTEMPT_LABELS[a.status] || a.status]
        .filter(Boolean).join(' · ');
      return `<div class="hint">· ${esc(parts || '（无轨迹）')}${a.ms != null ? `（${a.ms}ms）` : ''}</div>`;
    }).join('');
    box.className = 'wizard-test-result ok';
    box.innerHTML = `<p>端到端验收通过${hitText ? `：命中 <strong>${esc(hitText)}</strong>` : ''}`
      + `${r.latency_ms != null ? `（耗时 ${r.latency_ms}ms）` : ''}。您的调用密钥已可正常使用。</p>`
      + (rows ? `<p class="hint">本次调用轨迹（真实路由链）：</p>${rows}` : '')
      + retryBtn;
  } else {
    const err = r.error || {};
    const status = r.channel_status && typeof r.channel_status === 'object'
      ? Object.entries(r.channel_status).map(([name, st]) =>
        `<div class="hint">· ${esc(name)}：${esc(WIZARD_ATTEMPT_LABELS[st] || st)}</div>`).join('')
      : '';
    const hints = (r.hints || []).map((t) => `<p class="hint">建议：${esc(t)}</p>`).join('');
    box.className = 'wizard-test-result bad';
    box.innerHTML = `<p>验收未通过：${esc(err.message || r.note || '未知原因')}`
      + `${err.http_status ? `（HTTP ${esc(err.http_status)}）` : ''}</p>`
      + `${status}${hints}`
      + `<div class="actions">
           <button class="btn ghost" id="wizard-verify-retry">重新验收</button>
           <button class="btn ghost" id="wizard-verify-channels">去渠道配置</button>
           <button class="btn ghost" id="wizard-verify-routes">查看路由链</button>
         </div>`;
    const go = (id, tab) => {
      const btn = $(id);
      if (btn) {
        btn.onclick = () => {
          closeWizard();
          if (typeof switchPage === 'function') switchPage(tab);
        };
      }
    };
    go('wizard-verify-channels', 'channels');
    go('wizard-verify-routes', 'routes');
  }
  wizardBindVerifyRetry();
}

// 端到端验收结果里出现的状态词 → 中文（未收录的原样展示）。
const WIZARD_ATTEMPT_LABELS = {
  ok: '成功', error: '失败', no_candidate: '无可用渠道', rejected: '被拒',
  cooldown: '冷却中', disabled: '未启用', missing: '不存在', blocked: '套餐过期',
};

// 关闭向导弹窗：移除所有向导层（含重绘前的旧层）。
function closeWizard() {
  document.querySelectorAll('[data-wizard-modal]').forEach((el) => el.remove());
}

// 「完成」：双写 wizard_done（localStorage + 服务端），回仪表板。
async function finishWizard() {
  try {
    localStorage.setItem('setup_done', 'true');
  } catch (_) { /* ignore */ }
  try { sessionStorage.removeItem(WIZARD_STEP_KEY); } catch (_) { /* private mode */ }
  try {
    await api('/api/setup/wizard', { method: 'POST', body: { done: true } });
  } catch (_) { /* localStorage 已兜底 */ }
  toast('配置完成！之后可在右上角「快速开始」重新打开向导。');
  closeWizard();
  if (typeof switchPage === 'function') switchPage('dashboard');
}

// 已设置管理密码时，向导的「设置管理密码」步骤自动跳过：首次登录已强制设过
// 密码，不再重复。服务端快照或本机保存的密钥任一为真即视为已设置。
function wizardAdminKeySet() {
  if (readStoredKey('admin')) return true;
  const listen = (typeof state !== 'undefined' && state.snapshot && state.snapshot.listen) || {};
  return listen.admin_key_set === true;
}

function wizardSteps() {
  return WIZARD_STEPS.filter((s) => s.id !== 'admin-key' || !wizardAdminKeySet());
}

// 断点续传：进行中的步骤记在 sessionStorage——标签页刷新/意外关闭后，下次
// 打开控制台自动回到中断的那一步；主动取消或完成即清除，不留打扰。
const WIZARD_STEP_KEY = 'facet_wizard_step';

function recallWizardStep() {
  try { return sessionStorage.getItem(WIZARD_STEP_KEY); } catch (_) { return null; }
}

// 渲染向导弹窗。stepId 缺省 = 重绘当前步。
async function renderWizard(stepId) {
  if (stepId) wizardState.stepId = stepId;
  closeWizard();

  const steps = wizardSteps();
  let idx = steps.findIndex((s) => s.id === wizardState.stepId);
  if (idx < 0) idx = 0;
  const step = steps[idx];
  if (step.id !== wizardState.stepId) wizardState.stepId = step.id;
  try { sessionStorage.setItem(WIZARD_STEP_KEY, step.id); } catch (_) { /* private mode */ }
  const isLast = idx === steps.length - 1;

  const content = await step.render();
  const wrap = document.createElement('div');
  wrap.className = 'modal-backdrop ask-backdrop';
  wrap.dataset.wizardModal = '1';
  wrap.innerHTML = `
    <div class="modal ask-modal" role="dialog" aria-modal="true" style="max-width: 640px;">
      <div class="modal-head">
        <h2>新手配置向导</h2>
        <span class="hint">第 ${idx + 1} 步 / 共 ${steps.length} 步</span>
      </div>
      <p class="hint ask-message">${esc(step.description || '')}</p>
      ${content}
      <div class="modal-foot">
        <button type="button" class="btn ghost" id="wizard-dismiss">取消</button>
        ${idx > 0 ? '<button type="button" class="btn ghost" id="wizard-prev">上一步</button>' : ''}
        ${isLast
          ? '<button type="button" class="btn primary" id="wizard-finish">完成</button>'
          : '<button type="button" class="btn primary" id="wizard-next">下一步</button>'}
      </div>
    </div>
  `;
  document.body.appendChild(wrap);

  // 判空绑定：按钮只在需要的步骤渲染。
  const dismiss = $('wizard-dismiss');
  if (dismiss) {
    dismiss.onclick = () => {
      try { localStorage.setItem('setup_dismissed', 'true'); } catch (_) { /* ignore */ }
      // 主动取消 = 明确放弃：清掉断点记录，刷新后不再自动弹出。
      try { sessionStorage.removeItem(WIZARD_STEP_KEY); } catch (_) { /* private mode */ }
      closeWizard();
    };
  }
  const prev = $('wizard-prev');
  if (prev) prev.onclick = () => renderWizard(steps[idx - 1].id);
  const next = $('wizard-next');
  if (next) {
    next.onclick = async () => {
      try {
        if (typeof step.onNext === 'function' && !(await step.onNext())) return;
        renderWizard(steps[idx + 1].id);
      } catch (e) {
        toast(e.message, 'bad');
      }
    };
  }
  const finish = $('wizard-finish');
  if (finish) finish.onclick = () => finishWizard().catch(() => {});

  // 「查看使用手册」复用渠道编辑的使用文档弹层（app.js 的 showGuideOverlay）。
  const guideLink = wrap.querySelector('[data-wizard-guide]');
  if (guideLink) {
    guideLink.onclick = (e) => {
      e.preventDefault();
      if (typeof showGuideOverlay === 'function') showGuideOverlay(guideLink.dataset.wizardGuide);
    };
  }

  try {
    await step.onEnter?.();
  } catch (e) {
    toast(e.message, 'bad');
  }
}

// 首启自动弹出已由 app.js 承担：控制台打开时先强制设置管理密码，设置成功后
// 调用 renderWizard('welcome') 进入向导。这里不再自行探测/自动弹出，避免与
// 强制设密码弹窗叠加。保留 window.initWizard 仅供手动触发兼容。
async function initWizard() {
  renderWizard('welcome').catch(() => {});
}

// 导出给 index.html / app.js 调用（快速开始 → renderWizard('welcome')）。
if (typeof window !== 'undefined') {
  window.renderWizard = renderWizard;
  window.initWizard = initWizard;
  window.closeWizard = closeWizard;
  window.recallWizardStep = recallWizardStep;
}
