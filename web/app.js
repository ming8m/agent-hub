(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  let TOKEN = '';
  const TaskStatus = window.AgentHubTaskStatus;
  const Deadline = window.AgentHubDeadline;
  const HEADERS = {};
  const TOKEN_STORE_KEY = 'agentHubToken';
  function setToken(value) {
    if (!/^[0-9a-f]{64}$/.test(value)) return false;
    TOKEN = value;
    HEADERS['X-Agent-Bus-Token'] = value;
    try { sessionStorage.setItem(TOKEN_STORE_KEY, value); } catch { /* memory remains usable */ }
    $('#tokenInput').value = '';
    $('#tokenError').textContent = '';
    $('#tokenGate').style.display = 'none';
    $('.shell').inert = false;
    return true;
  }
  function requireToken(message = '') {
    TOKEN = '';
    delete HEADERS['X-Agent-Bus-Token'];
    try { sessionStorage.removeItem(TOKEN_STORE_KEY); } catch { /* unavailable storage */ }
    clearInterval(state.timer);
    $$('dialog[open]').forEach(dialog => dialog.close());
    $('.shell').inert = true;
    $('#tokenError').textContent = message;
    $('#tokenGate').style.display = 'flex';
    $('#tokenInput').focus();
  }
  function bootstrapToken() {
    const fragment = location.hash;
    if (fragment) history.replaceState(null, '', location.pathname + location.search);
    const candidate = new URLSearchParams(fragment.replace(/^#/, '')).get('token');
    if (candidate && setToken(candidate)) return true;
    try { if (setToken(sessionStorage.getItem(TOKEN_STORE_KEY) || '')) return true; }
    catch { /* manual entry remains available */ }
    requireToken();
    return false;
  }
  const AGENT_COLORS = ['violet', 'blue', 'green', 'orange', 'slate'];
  const RELATIONS = ['all', 'orchestrator_worker', 'worker_worker'];
  const STORE_KEY = 'agentHubRunsV1';
  const state = {
    agents: [], hub: null, selectedTargets: new Set(), mode: 'direct', currentRunId: null,
    currentRun: null, events: [], eventCursor: 0, relation: 'all', history: [], timer: null,
    dirtyConfig: false, loadingRun: false, agentSignature: null, hubSignature: null, hubLoaded: false,
    registry: { providers: [], provider_agents: [], templates: [], approved_template_ids: [] }, registryLoaded: false
  };

  function toast(message, kind = 'info') {
    const node = $('#toast'); node.textContent = message; node.dataset.kind = kind;
    node.classList.remove('hidden'); clearTimeout(toast.timer);
    toast.timer = setTimeout(() => node.classList.add('hidden'), 3200);
  }
  function setConnection(healthy, detail = '') {
    $('#connectionState').textContent = healthy ? '已连接' : '连接异常';
    $('#healthIcon').textContent = healthy ? '✓' : '!';
    $('#healthTitle').textContent = healthy ? 'Hub 服务正常' : '服务不可用';
    $('#healthDetail').textContent = detail || (healthy ? 'Agent Hub 已连接' : '检查服务器与令牌');
    $('#syncStatus').innerHTML = healthy ? '<i></i>实时同步' : '<i></i>同步暂停';
    $('#syncStatus').classList.toggle('sync-error', !healthy);
  }
  async function api(path, options = {}) {
    const headers = { ...HEADERS, ...(options.headers || {}) };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    let response, payload;
    try {
      response = await fetch(path, { ...options, headers, cache: 'no-store' });
      payload = await response.json();
    } catch (error) {
      const issue = new Error('无法连接 Agent Hub。输入内容已保留。'); issue.cause = error; throw issue;
    }
    if (response.status === 401) requireToken('令牌已失效，请重新输入。');
    if (!response.ok || payload?.ok === false) {
      const messages = {
        unknown_agent: '目标 Agent 已不在注册表中，请刷新后重新选择。',
        orchestrator_disabled: '主 Agent 尚未启用。请检查主 Agent 设置。',
        orchestrator_unavailable: '主 Agent 未启用或不可用。请在设置中选择并启用已注册 Agent。',
        summary_unavailable: '本轮未配置可用的摘要 Agent。原始 Agent 回复仍可查看。',
        task_limit: '超过当前配置允许的并行任务数。',
        invalid_request: safeBackendMessage(payload.message) || '请求格式无效。',
        storage_error: '任务存储暂时不可用。',
        not_found: '目标记录不存在或已被清理。',
        conflict: '请求状态已变化，请刷新后重试。',
        provider_in_use: '此模型服务仍被 Agent 或模板引用，请先移除相关引用。',
        registry_invalid: safeBackendMessage(payload.message) || '模型服务或 Agent 设置无效。',
        secure_secret_required: '此平台不接受直接保存密钥。请先设置环境变量，再填写 ${ENV:VARIABLE_NAME} 引用并重试。'
      };
      const issue = new Error(messages[payload?.error] || safeBackendMessage(payload?.message) || `请求失败（HTTP ${response.status}）。`);
      issue.status = response.status; issue.code = payload?.error || 'http_error'; throw issue;
    }
    return payload;
  }
  function safeBackendMessage(value) {
    if (typeof value !== 'string') return '';
    return value.replace(/(?:api[_-]?key|token|secret|authorization)\s*[:=]\s*[^\s,;]+/gi, '凭据=[已隐藏]')
      .replace(/Bearer\s+[^\s,;]+/gi, 'Bearer [已隐藏]').slice(0, 240);
  }
  function dateLabel(value) {
    if (!value) return '—';
    const date = typeof value === 'number' ? new Date(value < 100_000_000_000 ? value * 1000 : value) : new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value) : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
  }
  function updateToday() {
    const now = new Date();
    $('#todayDate').textContent = new Intl.DateTimeFormat('en-US', { weekday: 'long', month: 'short', day: 'numeric' }).format(now).toUpperCase();
  }
  function colorFor(id) {
    const index = Math.max(0, state.agents.findIndex(agent => agent.id === id));
    return AGENT_COLORS[index % AGENT_COLORS.length];
  }
  function makeAvatar(agentId, small = false) {
    const agent = state.agents.find(item => item.id === agentId);
    const avatar = document.createElement('span');
    avatar.className = `avatar ${colorFor(agentId)}${small ? ' tiny' : ''}`;
    avatar.textContent = (agent?.name || agentId || '?').slice(0, 1).toLocaleUpperCase();
    return avatar;
  }
  function addText(parent, tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text ?? '';
    parent.append(element);
    return element;
  }
  function addSetupAction(parent) {
    const button = addText(parent, 'button', 'empty-setup-action', '添加连接或 Agent');
    button.type = 'button';
    button.addEventListener('click', () => {
      const dialog = $('#settingsDialog'); if (!dialog.open) dialog.showModal();
      setSettingsTab(state.registry.providers.length ? 'agents' : 'providers');
    });
    return button;
  }
  function renderAgentList() {
    const signature = JSON.stringify(state.agents.map(agent => [agent.id, agent.name, agent.desc, agent.tag]));
    if (signature === state.agentSignature) { refreshAgentBusy(); return; }
    state.agentSignature = signature;
    const registeredIds = new Set(state.agents.map(agent => agent.id));
    state.selectedTargets = new Set([...state.selectedTargets].filter(id => registeredIds.has(id)));
    const list = $('#agentList'), picks = $('#targetPicks');
    list.replaceChildren(); picks.replaceChildren();
    $('#agentCount').textContent = state.agents.length;
    if (!state.agents.length) {
      addText(list, 'div', 'empty-side', '尚无已注册 Agent');
      addText(picks, 'span', 'muted', '先添加自定义模型服务，再创建 Agent。');
      addSetupAction(picks); addSetupAction(list);
      return;
    }
    for (const [index, agent] of state.agents.entries()) {
      const side = document.createElement('div'); side.className = 'agent';
      const avatar = document.createElement('span'); avatar.className = `avatar ${AGENT_COLORS[index % AGENT_COLORS.length]}`;
      avatar.textContent = (agent.name || agent.id || '?').slice(0, 1).toLocaleUpperCase(); side.append(avatar);
      const meta = document.createElement('span'); meta.className = 'meta';
      addText(meta, 'span', 'agent-name', agent.name || agent.id);
      addText(meta, 'small', 'agent-desc', agent.desc || agent.tag || '已注册'); side.append(meta);
      const indicator = document.createElement('span'); indicator.className = `status ${state.busy?.[agent.id] ? 'busy' : 'online'}`;
      indicator.setAttribute('aria-label', state.busy?.[agent.id] ? '执行中' : '空闲'); side.append(indicator); list.append(side);

    }
    renderTargetPicks(); renderOrchestratorOptions(); updateDispatchState();
  }
  function renderTargetPicks() {
    const picks = $('#targetPicks'); picks.replaceChildren();
    if (!state.agents.length) { addText(picks, 'span', 'muted', '请先在设置中创建或连接 Agent。'); return; }
    for (const agent of state.agents) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'target-pick';
      button.dataset.agentId = agent.id; button.setAttribute('aria-pressed', String(state.selectedTargets.has(agent.id)));
      button.classList.toggle('selected', state.selectedTargets.has(agent.id));
      button.append(makeAvatar(agent.id, true)); addText(button, 'span', '', agent.name || agent.id);
      addText(button, 'span', 'target-check', state.selectedTargets.has(agent.id) ? '✓' : '+'); picks.append(button);
    }
  }
  function refreshAgentBusy() {
    $$('#agentList .status').forEach((node, index) => {
      const agent = state.agents[index]; if (!agent) return;
      node.className = `status ${state.busy?.[agent.id] ? 'busy' : 'online'}`;
      node.setAttribute('aria-label', state.busy?.[agent.id] ? '执行中' : '空闲');
    });
  }
  function renderOrchestratorOptions() {
    for (const [selector, emptyLabel, configured] of [
      ['#orchestratorSelect', '选择已注册 Agent', state.hub?.orchestrator_agent_id],
      ['#summaryAgentSelect', '不配置摘要 Agent', state.hub?.summary_agent_id]
    ]) {
      const select = $(selector), selected = select.value;
      select.replaceChildren();
      const empty = document.createElement('option'); empty.value = ''; empty.textContent = emptyLabel; select.append(empty);
      for (const agent of state.agents) {
        const option = document.createElement('option'); option.value = agent.id; option.textContent = agent.name || agent.id; select.append(option);
      }
      const preferred = state.dirtyConfig ? selected : configured || selected || '';
      select.value = [...select.options].some(option => option.value === preferred) ? preferred : '';
    }
  }
  function renderConfig(config) {
    if (!config) return;
    const signature = JSON.stringify(config);
    state.hub = config;
    if (signature !== state.hubSignature && !state.dirtyConfig) {
      state.hubSignature = signature;
      if (!state.hubLoaded) { state.mode = config.mode_default === 'orchestrated' ? 'orchestrated' : 'direct'; state.hubLoaded = true; }
      renderOrchestratorOptions();
      $('#orchestratorEnabled').checked = Boolean(config.orchestrator_enabled);
      $('#summaryPolicy').value = config.summary_policy || 'auto';
      $('#defaultMode').value = config.mode_default || 'direct';
      $('#defaultDeadline').value = config.deadline_seconds ?? '';
    }
    const stateNode = $('#orchestratorState');
    const words = { enabled: '已启用', disabled: '未启用', invalid: '配置失效' };
    const actual = config.orchestrator_state || 'disabled';
    stateNode.innerHTML = '';
    const dot = document.createElement('i'); stateNode.append(dot, document.createTextNode(words[actual] || '状态未知'));
    stateNode.className = `state-pill ${actual}`;
    const ready = actual === 'enabled';
    $('#configMessage').textContent = actual === 'invalid'
      ? '当前主 Agent 已失效。请选择有效 Agent 并保存后再启用。'
      : ready ? '设置已生效。主 Agent 将先生成并校验计划，再按本轮选择自动派发或等待审批。'
        : '保存有效主 Agent 并启用后，可在本轮选择主 Agent 模式。';
    const summaryState = config.summary_agent_state || 'unavailable';
    $('#summaryAgentState').textContent = summaryState === 'enabled'
      ? `摘要 Agent：${agentName(config.summary_agent_id)}。完成后按${config.summary_policy === 'manual' ? '手动请求' : '自动生成'}摘要。`
      : summaryState === 'invalid' ? '已配置的摘要 Agent 当前不可用，请重新选择并保存。'
        : '未配置模型摘要 Agent。逐项摘要和本轮汇总将不可用。';
    $('#modeOrchestrated').setAttribute('aria-disabled', String(!ready));
    updateModePresentation();
  }
  const registryPath = '/api/config/agents';
  function normalizedRegistry(value = {}) {
    return {
      secret_storage: value.secret_storage === 'env-reference-only' ? 'env-reference-only' : 'dpapi-or-env',
      agents: Array.isArray(value.agents) ? value.agents.map(item => ({ id: item.id, desc: item.desc, kind: item.kind })) : [],
      providers: Array.isArray(value.providers) ? value.providers.map(item => ({ id: item.id, protocol: item.protocol, base_url: item.base_url, default_model: item.default_model || '', output_limit_field: item.output_limit_field || 'max_tokens', allow_insecure_loopback: Boolean(item.allow_insecure_loopback), has_api_key: item.has_api_key === true })) : [],
      provider_agents: Array.isArray(value.provider_agents) ? value.provider_agents.map(item => ({ id: item.id, desc: item.desc, provider_id: item.provider_id, model: item.model, timeout: item.timeout, max_tokens: item.max_tokens ?? 4096 })) : [],
      templates: Array.isArray(value.templates) ? value.templates.map(item => ({ id: item.id, provider_id: item.provider_id, model: item.model, desc: item.desc, enabled: item.enabled !== false, max_tokens: item.max_tokens ?? 4096 })) : [],
      approved_template_ids: Array.isArray(value.approved_template_ids) ? value.approved_template_ids : []
    };
  }
  function updateAgentCatalog(baseAgents = []) {
    const byId = new Map();
    for (const agent of baseAgents) if (agent?.id) byId.set(agent.id, { ...agent, name: agent.name || agent.desc || agent.id });
    for (const agent of state.registry.agents) if (agent?.id) byId.set(agent.id, { ...byId.get(agent.id), ...agent, name: agent.desc || agent.name || agent.id });
    for (const agent of state.registry.provider_agents) if (agent?.id) byId.set(agent.id, { ...byId.get(agent.id), ...agent, name: agent.desc || agent.name || agent.id, kind: 'provider' });
    state.agents = [...byId.values()];
    renderAgentList(); renderConfig(state.hub);
  }
  function providersForSelect(select, selected = '') {
    select.replaceChildren();
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = state.registry.providers.length ? '选择模型服务' : '先添加模型服务'; select.append(empty);
    for (const provider of state.registry.providers) {
      const option = document.createElement('option'); option.value = provider.id; option.textContent = provider.id; select.append(option);
    }
    select.value = selected || '';
  }
  function rowAction(label, action, id, disabled = false) {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
    button.dataset.registryAction = action; button.dataset.registryId = id; button.disabled = disabled; return button;
  }
  function renderRegistry() {
    const providers = state.registry.providers, agents = state.registry.provider_agents, templates = state.registry.templates;
    const providerList = $('#providerList'); providerList.replaceChildren();
    if (!providers.length) addText(providerList, 'div', 'settings-empty', '尚未配置自定义模型服务。填写兼容 API 的地址和请求格式后，即可创建 Agent。');
    for (const provider of providers) {
      const row = document.createElement('article'); row.className = 'config-row';
      const main = document.createElement('div'); main.className = 'config-row-main';
      addText(main, 'div', 'config-row-title', provider.id || '未命名模型服务');
      addText(main, 'div', 'config-row-meta', `${provider.protocol || '协议未配置'} · ${provider.base_url || 'API 地址未配置'} · 默认模型：${provider.default_model || '未设置'} · ${provider.has_api_key ? '密钥已保存' : '尚未配置密钥'}${provider.allow_insecure_loopback ? ' · 本机 HTTP' : ''}`);
      const actions = document.createElement('div'); actions.className = 'config-row-actions';
      actions.append(rowAction('编辑', 'edit-provider', provider.id));
      const referenced = agents.some(agent => agent.provider_id === provider.id) || templates.some(template => template.provider_id === provider.id);
      actions.append(rowAction('移除', 'remove-provider', provider.id, referenced));
      if (referenced) { const help = addText(actions, 'span', 'field-help', '仍被引用'); help.title = '先移除引用此模型服务的 Agent 和模板'; }
      row.append(main, actions); providerList.append(row);
    }
    const agentList = $('#agentConfigList'); agentList.replaceChildren();
    const cliAgents = state.agents.filter(agent => agent.kind === 'cli');
    if (!agents.length && !cliAgents.length) addText(agentList, 'div', 'settings-empty', '尚未创建 Agent。添加模型服务后，可创建普通 Agent，并在协作设置中选择主 Agent。');
    for (const agent of cliAgents) {
      const row = document.createElement('article'); row.className = 'config-row';
      const main = document.createElement('div'); main.className = 'config-row-main';
      addText(main, 'div', 'config-row-title', agent.desc || agent.name || agent.id);
      addText(main, 'div', 'config-row-meta', `命令行 Agent · ${agent.id} · 由 Agent 注册表管理`);
      const badge = addText(row, 'span', 'config-row-status enabled', '可用'); row.append(main, badge); agentList.append(row);
    }
    for (const agent of agents) {
      const row = document.createElement('article'); row.className = 'config-row';
      const main = document.createElement('div'); main.className = 'config-row-main';
      addText(main, 'div', 'config-row-title', agent.desc || agent.id);
      addText(main, 'div', 'config-row-meta', `${agent.id} · ${agent.provider_id || '未关联模型服务'} · ${agent.model || '未指定模型'} · 超时 ${agent.timeout ?? 600} 秒`);
      const badge = addText(row, 'span', 'config-row-status enabled', '可用');
      const actions = document.createElement('div'); actions.className = 'config-row-actions';
      actions.append(rowAction('编辑', 'edit-agent', agent.id));
      const referencedByHub = state.hub?.orchestrator_agent_id === agent.id || state.hub?.summary_agent_id === agent.id;
      actions.append(rowAction('移除', 'remove-agent', agent.id, referencedByHub));
      if (referencedByHub) addText(actions, 'span', 'field-help', '先在协作设置中解除引用');
      row.append(main, badge, actions); agentList.append(row);
    }
    const templateList = $('#templateList'); templateList.replaceChildren();
    if (!templates.length) addText(templateList, 'div', 'settings-empty', '尚未添加子 Agent 模板。模板需由你批准后，主 Agent 才能在任务轮次中创建。');
    const approved = new Set(state.registry.approved_template_ids);
    for (const template of templates) {
      const row = document.createElement('div'); row.className = 'template-row';
      const label = document.createElement('label'); label.className = 'template-choice';
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = approved.has(template.id) && template.enabled !== false;
      checkbox.dataset.templateApproval = template.id; checkbox.disabled = template.enabled === false;
      const name = addText(label, 'span', 'template-name', template.desc || template.id);
      const meta = addText(label, 'span', 'template-meta', `${template.provider_id || '无模型服务'} · ${template.model || '无模型'}`);
      label.prepend(checkbox); row.append(label);
      addText(row, 'span', 'config-row-status', template.enabled === false ? '已停用' : checkbox.checked ? '已批准' : '待批准');
      const actions = document.createElement('div'); actions.className = 'config-row-actions';
      actions.append(rowAction('编辑', 'edit-template', template.id), rowAction('移除', 'remove-template', template.id));
      row.append(actions); templateList.append(row);
    }
    providersForSelect($('#agentEditor [name="provider_id"]'), $('#agentEditor [name="provider_id"]').value);
    providersForSelect($('#templateEditor [name="provider_id"]'), $('#templateEditor [name="provider_id"]').value);
  }
  async function loadRegistry() {
    const result = await api(registryPath);
    state.registry = normalizedRegistry(result);
    state.registryLoaded = true;
    renderRegistry();
    updateAgentCatalog(state.agents);
    return state.registry;
  }
  function serializeProvider(provider) {
    const value = { id: provider.id, protocol: provider.protocol, base_url: provider.base_url, default_model: provider.default_model || '',
      allow_insecure_loopback: Boolean(provider.allow_insecure_loopback) };
    if (provider.protocol === 'openai-chat-completions')
      value.output_limit_field = provider.output_limit_field || 'max_tokens';
    return value;
  }
  async function saveRegistry(next, providerSecret = null) {
    const clean = normalizedRegistry(next);
    const body = { providers: clean.providers.map(serializeProvider),
      provider_agents: clean.provider_agents.map(item => ({ id: item.id, desc: item.desc, provider_id: item.provider_id, model: item.model, timeout: item.timeout, max_tokens: item.max_tokens ?? 4096 })),
      templates: clean.templates.map(item => ({ id: item.id, provider_id: item.provider_id, model: item.model, desc: item.desc, enabled: item.enabled !== false, max_tokens: item.max_tokens ?? 4096 })),
      approved_template_ids: clean.approved_template_ids };
    if (providerSecret && providerSecret.value) {
      const provider = body.providers.find(item => item.id === providerSecret.id);
      if (provider) provider.api_key = providerSecret.value;
    } else if (providerSecret?.clear === true) {
      const provider = body.providers.find(item => item.id === providerSecret.id);
      if (provider) provider.api_key = null;
    }
    await api(registryPath, { method: 'PUT', body: JSON.stringify(body) });
    const refreshed = await api(registryPath);
    state.registry = normalizedRegistry(refreshed); state.registryLoaded = true; renderRegistry();
    updateAgentCatalog(state.agents);
    await refreshState();
  }
  function setSettingsTab(name) {
    $$('.settings-tab').forEach(button => { const selected = button.dataset.settingsTab === name; button.classList.toggle('active', selected); button.setAttribute('aria-selected', String(selected)); });
    $$('.settings-pane').forEach(pane => { const selected = pane.id === `settings${name[0].toUpperCase()}${name.slice(1)}`; pane.hidden = !selected; pane.classList.toggle('active', selected); });
  }
  function prefillModelFromProvider(form) {
    if (form.dataset.modelManual === 'true') return;
    const provider = state.registry.providers.find(item => item.id === form.elements.provider_id.value);
    form.elements.model.value = provider?.default_model || '';
  }
  function showEditor(id, title, item = null) {
    const form = $(id); form.reset(); form.classList.remove('hidden');
    const values = item || {};
    for (const [key, value] of Object.entries(values)) if (form.elements[key] && key !== 'api_key') {
      const input = form.elements[key]; if (input.type === 'checkbox') input.checked = value !== false; else input.value = value ?? '';
    }
    if (form.elements.enabled && values.enabled == null) form.elements.enabled.checked = true;
    form.elements.id.readOnly = Boolean(item);
    if (id === '#providerEditor') {
      form.elements.output_limit_field.value = values.output_limit_field || 'max_tokens';
      $('#outputTokenFieldRow').hidden = form.elements.protocol.value !== 'openai-chat-completions';
      $('#providerEditorTitle').textContent = title;
      const secretState = form.querySelector('[data-secret-state]');
      const envOnly = state.registry.secret_storage === 'env-reference-only';
      secretState.textContent = envOnly
        ? item?.has_api_key ? '此提供商已有引用。留空保留；新值请使用 ${ENV:VARIABLE_NAME}，直接密钥会被拒绝。' : '当前平台只接受 ${ENV:VARIABLE_NAME} 引用。先设置服务进程环境变量；不要输入明文密钥。'
        : item?.has_api_key ? '此提供商已有密钥或引用。不会回显；留空保留，输入新值可替换。' : '密钥仅随本次保存请求发送；本机受保护存储可用时由服务端安全保存。也可使用 ${ENV:VARIABLE_NAME}。';
      form.elements.api_key.value = '';
      form.elements.clear_api_key.checked = false;
      form.elements.clear_api_key.disabled = !item?.has_api_key;
    } else if (id === '#agentEditor') $('#agentEditorTitle').textContent = title;
    else $('#templateEditorTitle').textContent = title;
    const providerSelect = form.elements.provider_id;
    if (providerSelect) {
      providersForSelect(providerSelect, values.provider_id || '');
      form.dataset.modelManual = item ? 'true' : 'false';
      if (!item) prefillModelFromProvider(form);
    }
    form.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    const focusable = form.querySelector('input:not([type=checkbox]),select,textarea'); focusable?.focus({ preventScroll: true });
  }
  function closeEditor(form) { form.reset(); form.classList.add('hidden'); }
  async function registryAction(event) {
    const button = event.target.closest('[data-registry-action]'); if (!button) return;
    const { registryAction: action, registryId: id } = button.dataset;
    if (action === 'edit-provider') { const item = state.registry.providers.find(value => value.id === id); if (item) showEditor('#providerEditor', '编辑模型服务', item); return; }
    if (action === 'edit-agent') { const item = state.registry.provider_agents.find(value => value.id === id); if (item) showEditor('#agentEditor', '编辑 Agent', item); return; }
    if (action === 'edit-template') { const item = state.registry.templates.find(value => value.id === id); if (item) showEditor('#templateEditor', '编辑子 Agent 模板', item); return; }
    const next = normalizedRegistry(state.registry);
    if (action === 'remove-provider') next.providers = next.providers.filter(value => value.id !== id);
    if (action === 'remove-agent') next.provider_agents = next.provider_agents.filter(value => value.id !== id);
    if (action === 'remove-template') { next.templates = next.templates.filter(value => value.id !== id); next.approved_template_ids = next.approved_template_ids.filter(value => value !== id); }
    try { await saveRegistry(next); toast('设置已保存。'); }
    catch (error) { showError(error); }
  }
  function initSettings() {
    const hubSettings = $('#hubSettings'); $('#hubSettingsMount').append(hubSettings);
    $('#openSettings').addEventListener('click', async () => {
      $('#settingsDialog').showModal();
      if (!state.registryLoaded) { try { await loadRegistry(); } catch (error) { $('#providerList').replaceChildren(); $('#agentConfigList').replaceChildren(); $('#templateList').replaceChildren(); showError(error); } }
    });
    $('#closeSettings').addEventListener('click', () => $('#settingsDialog').close());
    $('#settingsDialog').addEventListener('click', event => { if (event.target === $('#settingsDialog')) $('#settingsDialog').close(); });
    $$('.settings-tab').forEach(button => button.addEventListener('click', () => setSettingsTab(button.dataset.settingsTab)));
    $('#newProvider').addEventListener('click', () => showEditor('#providerEditor', '添加模型服务'));
    $('#providerEditor [name="protocol"]').addEventListener('change', event => {
      $('#outputTokenFieldRow').hidden = event.target.value !== 'openai-chat-completions';
    });
    $('#newAgent').addEventListener('click', () => { if (!state.registry.providers.length) { setSettingsTab('providers'); toast('请先添加一个模型服务。'); return; } showEditor('#agentEditor', '添加 Agent'); });
    $('#newTemplate').addEventListener('click', () => { if (!state.registry.providers.length) { setSettingsTab('providers'); toast('请先添加一个模型服务。'); return; } showEditor('#templateEditor', '添加子 Agent 模板'); });
    for (const form of [$('#agentEditor'), $('#templateEditor')]) {
      form.elements.provider_id.addEventListener('change', () => prefillModelFromProvider(form));
      form.elements.model.addEventListener('input', () => { form.dataset.modelManual = 'true'; });
    }
    $$('.settings-list').forEach(list => list.addEventListener('click', registryAction));
    $('#templateList').addEventListener('click', registryAction);
    $('#templateList').addEventListener('change', async event => {
      const input = event.target.closest('[data-template-approval]'); if (!input) return;
      const approved = new Set(state.registry.approved_template_ids);
      if (input.checked) approved.add(input.dataset.templateApproval); else approved.delete(input.dataset.templateApproval);
      try { await saveRegistry({ ...state.registry, approved_template_ids: [...approved] }); toast('子 Agent 批准列表已更新。'); }
      catch (error) { input.checked = !input.checked; showError(error); }
    });
    $$('[data-cancel-provider]').forEach(button => button.addEventListener('click', () => closeEditor($('#providerEditor'))));
    $$('[data-cancel-agent]').forEach(button => button.addEventListener('click', () => closeEditor($('#agentEditor'))));
    $$('[data-cancel-template]').forEach(button => button.addEventListener('click', () => closeEditor($('#templateEditor'))));
    $$('.reveal-secret').forEach(button => button.addEventListener('click', () => {
      const input = button.closest('.secret-input').querySelector('input'); input.type = input.type === 'password' ? 'text' : 'password';
      button.textContent = input.type === 'password' ? '显示' : '隐藏'; button.setAttribute('aria-pressed', String(input.type === 'text'));
    }));
    $('#providerEditor').addEventListener('submit', async event => {
      event.preventDefault(); const form = event.currentTarget, data = new FormData(form), id = String(data.get('id')).trim();
    const value = { id, protocol: String(data.get('protocol')), base_url: String(data.get('base_url')).trim(), default_model: String(data.get('default_model') || '').trim(), output_limit_field: String(data.get('output_limit_field') || 'max_tokens'), allow_insecure_loopback: form.elements.allow_insecure_loopback.checked };
      const providers = [...state.registry.providers.filter(item => item.id !== id), { ...state.registry.providers.find(item => item.id === id), ...value }];
      const apiKey = String(data.get('api_key') || ''); const clearKey = form.elements.clear_api_key.checked; const submit = form.querySelector('[type=submit]'); submit.disabled = true;
      try { await saveRegistry({ ...state.registry, providers }, apiKey ? { id, value: apiKey } : clearKey ? { id, clear: true } : null); closeEditor(form); toast('模型服务设置已保存。'); }
      catch (error) { showError(error); }
      finally { submit.disabled = false; }
    });
    $('#agentEditor').addEventListener('submit', async event => {
      event.preventDefault(); const form = event.currentTarget, data = new FormData(form), id = String(data.get('id')).trim();
      const value = { id, desc: String(data.get('desc')).trim(), provider_id: String(data.get('provider_id')), model: String(data.get('model')).trim(), timeout: Number(data.get('timeout')), max_tokens: Number(data.get('max_tokens')) };
      const provider_agents = [...state.registry.provider_agents.filter(item => item.id !== id), value]; const submit = form.querySelector('[type=submit]'); submit.disabled = true;
      try { await saveRegistry({ ...state.registry, provider_agents }); closeEditor(form); toast('Agent 设置已保存。'); }
      catch (error) { showError(error); }
      finally { submit.disabled = false; }
    });
    $('#templateEditor').addEventListener('submit', async event => {
      event.preventDefault(); const form = event.currentTarget, data = new FormData(form), id = String(data.get('id')).trim();
      const value = { id, desc: String(data.get('desc')).trim(), provider_id: String(data.get('provider_id')), model: String(data.get('model')).trim(), enabled: form.elements.enabled.checked, max_tokens: Number(data.get('max_tokens')) };
      const templates = [...state.registry.templates.filter(item => item.id !== id), value];
      const approved = new Set(state.registry.approved_template_ids); if (value.enabled) approved.add(id); else approved.delete(id);
      const submit = form.querySelector('[type=submit]'); submit.disabled = true;
      try { await saveRegistry({ ...state.registry, templates, approved_template_ids: [...approved] }); closeEditor(form); toast('子 Agent 模板已保存。'); }
      catch (error) { showError(error); }
      finally { submit.disabled = false; }
    });
  }
  function updateModePresentation() {
    const orchestrated = state.mode === 'orchestrated';
    $$('.mode-option').forEach(button => {
      const selected = button.dataset.mode === state.mode;
      button.classList.toggle('active', selected); button.setAttribute('aria-checked', String(selected));
    });
    const ready = state.hub?.orchestrator_state === 'enabled';
    $('#orchestratorNotice').classList.toggle('hidden', !orchestrated || ready);
    $('#orchestratorNotice span').textContent = '请在设置的协作设置中选择并启用有效的主 Agent，然后保存。';
    $('#modeExplainer').textContent = orchestrated
      ? '主 Agent 可联系已注册 Agent，并仅从批准模板创建本轮子 Agent。'
      : `工作任务与相互派发仅限本轮选中的 Agent；摘要由${state.hub?.summary_agent_id ? `${agentName(state.hub.summary_agent_id)} 生成` : '配置的摘要 Agent 生成'}。`;
    $('#modePill').textContent = orchestrated ? `启用主 Agent · ${ready ? agentName(state.hub.orchestrator_agent_id) : '尚未就绪'}` : '无主 Agent · 直接并行';
    $('#targetPicks').classList.toggle('targets-disabled', orchestrated);
    $('#targetLabel').innerHTML = orchestrated ? '派发目标 <span>由主 Agent 计划选择</span>' : '接收 Agent <span>可多选，一次并行运行</span>';
    $('#orchestrationOptions').classList.toggle('hidden', !orchestrated);
    updateDispatchState();
  }
  function updateDispatchState() {
    const orchestrated = state.mode === 'orchestrated';
    const hasTargets = state.selectedTargets.size > 0;
    const ready = state.hub?.orchestrator_state === 'enabled';
    $('#startRun').disabled = (orchestrated ? !ready : !hasTargets) || !$('#prompt').value.trim();
    $('#startRun').textContent = orchestrated ? '启动主 Agent 计划 ↑' : '并行派发 ↑';
    $('#dispatchHint').textContent = orchestrated
      ? ready ? ($('#dispatchPolicy').value === 'auto' ? '主 Agent 生成并校验计划后将自动派发任务。' : '主 Agent 生成并校验计划后，会等待你预览和批准。') : '请先在侧栏启用有效的主 Agent。'
      : hasTargets ? `将并行启动 ${state.selectedTargets.size} 个已选 Agent。` : '请选择一个或多个 Agent。';
    $('#targetPicks').setAttribute('aria-disabled', String(orchestrated));
  }
  function setMode(mode) {
    state.mode = mode === 'orchestrated' ? 'orchestrated' : 'direct';
    updateModePresentation();
  }
  function formatDeadlineInput(defaultSeconds) {
    $('#runDeadline').value = Deadline.formatLocalInput(Date.now(), defaultSeconds);
  }
  function getDeadlineSeconds() {
    const value = $('#runDeadline').value;
    if (!value) return null;
    const seconds = Deadline.relativeSeconds(value, Date.now());
    if (seconds === null || seconds < 1 || seconds > 86400) throw new Error('截止时间需晚于现在且不超过 24 小时。');
    return seconds;
  }
  function shortId(id) { return id ? id.slice(0, 8) : ''; }
  function countStatus(tasks, statuses) { return tasks.filter(task => statuses.includes(task.status)).length; }
  function statusLabel(task) { return TaskStatus.label(task); }
  function renderRun(run) {
    state.currentRun = run;
    $('#currentRun').classList.remove('hidden');
    $('#runShortId').textContent = shortId(run.run_id);
    $('#runPrompt').textContent = run.prompt || '(无任务描述)';
    $('#runModeLabel').textContent = run.mode === 'orchestrated'
      ? `主 Agent：${agentName(run.orchestrator_agent_id)} · ${run.dispatch_policy === 'auto' ? '自动派发' : '计划审批'}`
      : '无主 Agent · 并行';
    $('#runCreatedAt').textContent = `创建于 ${dateLabel(run.created_at)}`;
    const deadline = $('#runDeadlineState');
    if (run.deadline_at) {
      const remaining = run.deadline_at - Date.now() / 1000;
      deadline.textContent = run.deadline_state === 'reached' || remaining <= 0
        ? `已到截止时间 · ${dateLabel(run.deadline_at)}`
        : `截止 ${dateLabel(run.deadline_at)} · 剩余 ${formatDuration(remaining)}`;
      deadline.classList.toggle('overdue', run.deadline_state === 'reached' || remaining <= 0);
    } else { deadline.textContent = '未设置截止时间'; deadline.classList.remove('overdue'); }
    const tasks = Array.isArray(run.tasks) ? run.tasks : [];
    const done = tasks.filter(task => TaskStatus.isTerminal(task.status)).length;
    const succeeded = countStatus(tasks, ['succeeded']);
    $('#runCountLabel').textContent = `${done} / ${tasks.length} 已结束 · ${succeeded} 条成功回复`;
    $('#runProgressBar').style.width = `${tasks.length ? Math.round(done / tasks.length * 100) : 0}%`;
    renderTasks(tasks); renderPlan(run); renderRunSummary(run, succeeded, tasks.length);
    const late = run.deadline_state === 'reached' && done < tasks.length;
    $('#deadlineAlert').classList.toggle('hidden', !late);
    if (late) $('#deadlineAlert').textContent = `已到截止时间，${tasks.length - done} 个任务仍在执行。截止只触发提醒；迟到回复会继续归入本轮任务。当前已有 ${succeeded}/${tasks.length} 条成功回复。`;
    addToHistory(run.run_id, run.prompt || '未命名任务');
  }
  function renderPlan(run) {
    const panel = $('#planPanel');
    const isMaster = run.mode === 'orchestrated';
    panel.classList.toggle('hidden', !isMaster);
    if (!isMaster) return;
    const plan = run.plan || {};
    const labels = { pending: '等待主 Agent 计划', ready: '计划已就绪', awaiting_approval: '等待批准', dispatched: '已派发', completed: '已完成', failed: '计划失败', dispatching: '正在派发' };
    $('#planState').textContent = labels[plan.status] || '计划状态未知';
    $('#planState').className = `summary-state ${plan.status || 'unknown'}`;
    $('#planAgent').textContent = `本轮主 Agent：${agentName(run.orchestrator_agent_id)} · ${run.dispatch_policy === 'auto' ? '自动派发模式' : '审批模式'}`;
    const content = $('#planContent'); content.replaceChildren();
    if (plan.summary) addText(content, 'p', '', plan.summary);
    else if (plan.status === 'pending') addText(content, 'p', 'muted', '主 Agent 正在分析任务并生成计划…');
    else if (plan.error?.message) addText(content, 'p', 'task-error', plan.error.message);
    const questions = $('#planQuestions'); questions.replaceChildren();
    for (const question of Array.isArray(plan.questions) ? plan.questions : []) addText(questions, 'p', 'plan-question', `待确认：${question}`);
    const list = $('#planTasks'); list.replaceChildren();
    for (const [index, task] of (Array.isArray(plan.tasks) ? plan.tasks : []).entries()) {
      const card = document.createElement('article'); card.className = 'plan-task';
      addText(card, 'b', '', `${index + 1}. ${task.title || task.agent_id || '计划任务'}`);
      addText(card, 'small', '', `Agent：${agentName(task.agent_id)}${task.depends_on?.length ? ` · 依赖 ${task.depends_on.join('、')}` : ''}`);
      addText(card, 'p', '', task.prompt || ''); list.append(card);
    }
    const approve = $('#approvePlan');
    const canApprove = plan.status === 'awaiting_approval';
    approve.classList.toggle('hidden', !canApprove);
    approve.disabled = !canApprove;
  }
  function formatDuration(value) {
    const seconds = Math.max(0, Math.floor(value));
    const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
    return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分`;
  }
  function renderTasks(tasks) {
    const list = $('#runTasks'); list.replaceChildren();
    if (!tasks.length) { addText(list, 'p', 'empty-events', '本轮尚未创建任务。'); return; }
    for (const task of tasks) {
      const safeStatus = /^[a-z_]+$/.test(task.status || '') ? task.status : 'unknown';
      const row = document.createElement('article'); row.className = `run-task status-${safeStatus}`;
      row.append(makeAvatar(task.agent_id, true));
      const meta = document.createElement('div'); meta.className = 'run-task-meta';
      addText(meta, 'b', '', agentName(task.agent_id));
      const roleLabel = task.parent_task_id ? '本轮子 Agent' : task.role === 'orchestrator' ? '主 Agent' : task.role === 'orchestrator_reply' ? '主 Agent 回复' : 'Agent';
      addText(meta, 'small', 'run-task-role', `${roleLabel} · 任务 ${task.sequence ?? '—'} · ${task.started_at ? dateLabel(task.started_at) : '尚未启动'}`);
      row.append(meta);
      const statePill = addText(row, 'span', `task-state ${task.overdue ? 'overdue' : task.status}`, statusLabel(task));
      statePill.setAttribute('aria-label', `任务状态：${statusLabel(task)}`);
      if (task.error?.message) addText(row, 'p', 'task-error', task.error.message);
      const summary = task.summary;
      const taskSummary = document.createElement('div'); taskSummary.className = 'task-summary';
      const summaryText = typeof summary === 'string' ? summary : summary?.content;
      const summaryStatus = typeof summary === 'object' ? summary?.status : summaryText ? 'ready' : 'idle';
      if (summaryStatus === 'ready' && summaryText) {
        addText(taskSummary, 'span', 'task-summary-label', '模型摘要');
        addText(taskSummary, 'p', 'task-summary-content', summaryText);
        const source = summary.source_agent_id ? `由 ${agentName(summary.source_agent_id)} 生成` : '由模型生成';
        addText(taskSummary, 'small', 'task-summary-meta', `${source}${summary.generated_at ? ` · ${dateLabel(summary.generated_at)}` : ''}`);
      } else {
        const labels = { idle: '摘要待生成', pending: '摘要生成中', failed: '摘要生成失败', unavailable: '模型摘要不可用', stale: '摘要已过期' };
        const fallbacks = {
          idle: '任务完成后会按配置生成模型摘要。',
          pending: '正在为此任务生成简短摘要。',
          failed: summary?.error?.message || '摘要 Agent 未能生成有效摘要；可以查看原始回复。',
          unavailable: summary?.reason || '未配置可用的摘要 Agent；可以查看原始回复。',
          stale: '原始回复发生变化，当前摘要已过期。',
        };
        const summaryAgentMissing = !summary && state.currentRun?.summary_agent_id == null;
        const label = summaryAgentMissing ? '模型摘要未配置' : labels[summaryStatus] || '摘要状态未知';
        const description = summaryAgentMissing ? '本轮未配置摘要 Agent；可以查看原始回复。' : fallbacks[summaryStatus] || '可以查看 Agent 原始回复。';
        addText(taskSummary, 'span', `task-summary-label ${summaryStatus === 'failed' || summaryStatus === 'stale' ? summaryStatus : 'pending'}`, label);
        addText(taskSummary, 'p', 'task-summary-content muted', description);
      }
      row.append(taskSummary);
      if (task.response_id) {
        const response = document.createElement('button'); response.type = 'button'; response.className = 'response-link';
        response.textContent = '展开原始全文'; response.setAttribute('aria-label', `展开 ${agentName(task.agent_id)} 的原始全文`);
        response.addEventListener('click', () => showResponse(task)); row.append(response);
      } else if (task.status === 'succeeded') addText(row, 'span', 'response-pending', '回复已完成，原文引用暂不可用');
      list.append(row);
    }
  }
  function agentName(id) { return state.agents.find(agent => agent.id === id)?.name || id || '未知 Agent'; }
  function renderRunSummary(run, succeeded, total) {
    const summary = run.summary || { status: 'unavailable', reason: 'No model summary adapter configured' };
    const statusLabels = { ready: '完整模型摘要', partial: '部分模型摘要', stale: '摘要待更新', pending: '生成中', failed: '生成失败', unavailable: '模型摘要未就绪' };
    $('#summaryState').textContent = statusLabels[summary.status] || '模型摘要状态未知';
    $('#summaryState').className = `summary-state ${summary.status || 'unknown'}`;
    if (summary.content) $('#summaryContent').textContent = summary.content;
    else if (summary.status === 'stale') $('#summaryContent').textContent = '有新回复到达，当前摘要已过期。请重新生成摘要以包含最新回复。';
    else if (summary.status === 'failed') $('#summaryContent').textContent = '模型摘要生成失败；逐条 Agent 回复仍可查看和展开。';
    else if (summary.status === 'pending') $('#summaryContent').textContent = `正在汇总当前 ${succeeded}/${total} 条成功回复…`;
    else $('#summaryContent').textContent = summary.reason || '本轮未配置可用的摘要 Agent。逐条任务回复和原始全文仍可查看。';
    $('#summaryHelp').textContent = summary.source_run_revision != null
      ? `覆盖 ${summary.task_ids?.length || 0} 个任务 · 生成于 ${dateLabel(summary.generated_at)} · 新回复可能使摘要过期。`
      : '摘要由模型生成；不会把 Agent 原始回复伪装成摘要。';
    $('#requestSummary').textContent = summary.status === 'stale' ? '更新模型摘要' : summary.status === 'partial' ? '更新当前摘要' : '请求模型摘要';
  }
  function renderEvents() {
    const list = $('#eventList'); list.replaceChildren();
    const filtered = state.events.filter(event => state.relation === 'all' || event.relation === state.relation);
    for (const button of $$('.filter')) {
      const relation = button.dataset.relation;
      button.querySelector('.filter-count').textContent = relation === 'all' ? state.events.length : state.events.filter(item => item.relation === relation).length;
    }
    if (!filtered.length) {
      const message = state.relation === 'all'
        ? '本轮还没有事件。任务状态和回复到达后会显示在这里。'
        : '当前后端尚未提供 Agent 间通信事件；收到匹配关系的真实事件后会显示在这里。';
      addText(list, 'div', 'empty-events', message); return;
    }
    for (const event of filtered) {
      const item = document.createElement('article'); item.className = 'event-item';
      const relationLabel = ({ orchestrator_worker: '主 ↔ 分', worker_worker: '分 ↔ 分', user_agent: '用户 ↔ Agent', system: '系统' })[event.relation] || event.relation || '事件';
      const from = event.from_agent_id ? agentName(event.from_agent_id) : '';
      const to = event.to_agent_id ? agentName(event.to_agent_id) : '';
      item.append(makeAvatar(event.from_agent_id || event.to_agent_id || '', true));
      const body = document.createElement('div'); body.className = 'event-body';
      const top = document.createElement('div'); top.className = 'event-meta';
      addText(top, 'b', '', event.kind === 'message' ? `${from} → ${to}` : event.kind === 'response' ? (to || from || 'Agent') : relationLabel);
      addText(top, 'span', 'role-tag', relationLabel);
      addText(top, 'time', '', dateLabel(event.created_at));
      body.append(top);
      const message = event.kind === 'message' ? event.body : eventDescription(event);
      if (message) addText(body, 'p', 'event-text', message);
      if (event.kind === 'message' && event.task_prompt && event.task_prompt !== event.body) {
        const details = document.createElement('details'); details.className = 'event-task-prompt';
        addText(details, 'summary', '', event.execute ? '查看派发的子任务提示' : '查看相关子任务提示');
        addText(details, 'p', 'event-text', event.task_prompt); body.append(details);
      }
      if (event.task_id) addText(body, 'small', 'event-task-id', `任务 ${shortId(event.task_id)}`);
      item.append(body); list.append(item);
    }
  }
  function eventDescription(event) {
    if (event.kind === 'status') return `任务状态：${event.state || '已更新'}${event.error_category ? ` · ${event.error_category}` : ''}`;
    if (event.kind === 'response') return `回复已${event.state === 'succeeded' ? '完成' : '更新'}。可在上方任务列表打开原始全文。`;
    if (event.kind === 'deadline') return '本轮截止时间已到；未完成任务继续运行，迟到回复仍会归入本轮。';
    if (event.kind === 'summary') return `模型摘要状态：${event.state || '已更新'}`;
    if (event.kind === 'rejected') return `协作请求被拒绝：${event.reason || '未通过策略校验'}`;
    if (event.kind === 'dispatch') return `任务已派发给 ${event.to_agent_id ? agentName(event.to_agent_id) : '目标 Agent'}。`;
    return '本轮收到一条状态事件。';
  }
  async function showResponse(task) {
    try {
      const runId = encodeURIComponent(task.run_id || state.currentRunId || '');
      const taskId = encodeURIComponent(task.task_id || '');
      const result = await api(`/api/responses/${encodeURIComponent(task.response_id)}?run_id=${runId}&task_id=${taskId}`);
      $('#responseTitle').textContent = `${agentName(task.agent_id)} · 原始全文`;
      $('#responseText').textContent = result.text ?? '';
      $('#responseDialog').showModal();
    } catch (error) { showError(error); }
  }
  function showError(error) {
    $('#connectionError').textContent = error.message || '操作失败。';
    $('#connectionError').classList.remove('hidden');
    toast(error.message || '操作失败。', 'error');
    if (error.status === 401 || error.status === 403) setConnection(false, '令牌校验失败');
  }
  function clearError() { $('#connectionError').classList.add('hidden'); }
  async function loadInitialData() {
    try {
      const [stateResult, configResult] = await Promise.all([api('/api/state?compact=1'), api('/api/config/hub')]);
      state.busy = stateResult.busy || {};
      state.hub = configResult.config || stateResult.hub || null;
      state.agents = Array.isArray(stateResult.agents) ? stateResult.agents : [];
      renderAgentList(); renderConfig(state.hub); setConnection(true);
      if (state.hub?.deadline_seconds) formatDeadlineInput(state.hub.deadline_seconds);
      try { await loadRegistry(); }
      catch (registryError) { $('#providerList').replaceChildren(); $('#agentConfigList').replaceChildren(); $('#templateList').replaceChildren(); $('#settingsDialog').dataset.registryError = 'true'; }
      await restoreHistory();
      clearError();
    } catch (error) { setConnection(false, error.message); showError(error); }
  }
  function readHistory() {
    try { const parsed = JSON.parse(sessionStorage.getItem(STORE_KEY) || '[]'); return Array.isArray(parsed) ? parsed.filter(item => typeof item === 'string' && /^[0-9a-f]{32}$/.test(item)).slice(0, 20) : []; }
    catch { return []; }
  }
  function addToHistory(runId, prompt) {
    if (!runId) return;
    state.history = [runId, ...state.history.filter(id => id !== runId)].slice(0, 20);
    try { sessionStorage.setItem(STORE_KEY, JSON.stringify(state.history)); } catch { /* session history is optional */ }
    renderHistory();
  }
  function renderHistory() {
    const section = $('#sessionRuns'), list = $('#runHistory'); list.replaceChildren();
    section.classList.toggle('hidden', state.history.length === 0);
    for (const id of state.history) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'history-run';
      button.textContent = `任务 ${shortId(id)}`; button.addEventListener('click', () => selectRun(id)); list.append(button);
    }
  }
  async function restoreHistory() {
    state.history = readHistory(); renderHistory();
    if (state.history.length) await selectRun(state.history[0], { silent: true });
  }
  async function createRun() {
    clearError();
    const prompt = $('#prompt').value;
    let deadlineSeconds;
    try { deadlineSeconds = getDeadlineSeconds(); }
    catch (error) { showError(error); $('#runDeadline').focus(); return; }
    if (state.mode === 'direct' && !state.selectedTargets.size) { showError(new Error('请至少选择一个已注册 Agent。')); return; }
    if (state.mode === 'orchestrated' && state.hub?.orchestrator_state !== 'enabled') { showError(new Error('请先启用有效的主 Agent。')); return; }
    $('#startRun').disabled = true; $('#startRun').textContent = '正在提交…';
    const idempotencyKey = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
    const body = state.mode === 'orchestrated'
      ? { mode: 'orchestrated', dispatch_policy: $('#dispatchPolicy').value, prompt }
      : { mode: 'direct', prompt, target_agent_ids: [...state.selectedTargets] };
    if (deadlineSeconds != null) body.deadline_seconds = deadlineSeconds;
    try {
      const accepted = await api('/api/runs', { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey }, body: JSON.stringify(body) });
      $('#prompt').value = '';
      state.currentRunId = accepted.run_id; state.events = []; state.eventCursor = 0;
      await selectRun(accepted.run_id, { silent: true });
      toast(state.mode === 'orchestrated' ? `主 Agent 计划已开始 · ${agentName(state.hub.orchestrator_agent_id)}` : `本轮任务已受理 · ${accepted.accepted_tasks?.length || 0} 个 Agent`);
      clearError();
    } catch (error) { showError(error); }
    finally { $('#startRun').textContent = '并行派发 ↑'; updateDispatchState(); }
  }
  async function approvePlan() {
    if (!state.currentRunId) return;
    $('#approvePlan').disabled = true; clearError();
    try {
      await api(`/api/runs/${encodeURIComponent(state.currentRunId)}/approve`, { method: 'POST', body: '{}' });
      await refreshRun(); toast('计划已批准并派发。');
    } catch (error) { showError(error); }
    finally { if ($('#planState').textContent === '等待批准') $('#approvePlan').disabled = false; }
  }
  async function selectRun(runId, { silent = false } = {}) {
    if (state.loadingRun) return;
    if (state.currentRunId !== runId) { state.events = []; state.eventCursor = 0; }
    state.loadingRun = true;
    try {
      state.currentRunId = runId;
      const result = await api(`/api/runs/${encodeURIComponent(runId)}`);
      renderRun(result.run);
      await pollEvents();
      clearError();
    } catch (error) { if (!silent) showError(error); }
    finally { state.loadingRun = false; }
  }
  async function pollEvents() {
    if (!state.currentRunId) return;
    const path = `/api/runs/${encodeURIComponent(state.currentRunId)}/events?after=${state.eventCursor}&relation=all&limit=500`;
    const result = await api(path);
    const incoming = Array.isArray(result.events) ? result.events : [];
    const ids = new Set(state.events.map(event => event.event_id));
    for (const event of incoming) if (!ids.has(event.event_id)) state.events.push(event);
    state.events.sort((a, b) => (a.event_id || 0) - (b.event_id || 0));
    state.eventCursor = state.events.reduce((max, event) => Math.max(max, Number(event.event_id) || 0), state.eventCursor);
    renderEvents();
  }
  async function refreshRun() {
    if (!state.currentRunId || state.loadingRun) return;
    try {
      const result = await api(`/api/runs/${encodeURIComponent(state.currentRunId)}`);
      renderRun(result.run); await pollEvents();
    } catch (error) { showError(error); }
  }
  async function refreshState() {
    try {
      const result = await api('/api/state?compact=1'); state.agents = Array.isArray(result.agents) ? result.agents : [];
      state.busy = result.busy || {}; renderAgentList();
      if (state.registryLoaded) updateAgentCatalog(state.agents);
      if (!state.dirtyConfig && result.hub) renderConfig(result.hub);
      setConnection(true); clearError();
    } catch (error) { setConnection(false, error.message); showError(error); }
  }
  async function saveConfig() {
    clearError(); const agentId = $('#orchestratorSelect').value || null;
    const deadlineValue = $('#defaultDeadline').value.trim();
    const deadlineSeconds = deadlineValue ? Number(deadlineValue) : null;
    if (deadlineSeconds != null && (!Number.isInteger(deadlineSeconds) || deadlineSeconds < 1 || deadlineSeconds > 86400)) {
      showError(new Error('默认截止时间需为 1 至 86400 秒。')); $('#defaultDeadline').focus(); return;
    }
    $('#saveConfig').disabled = true;
    try {
      const result = await api('/api/config/hub', { method: 'PUT', body: JSON.stringify({
        orchestrator_agent_id: agentId, orchestrator_enabled: $('#orchestratorEnabled').checked,
        summary_agent_id: $('#summaryAgentSelect').value || null, summary_policy: $('#summaryPolicy').value,
        mode_default: $('#defaultMode').value, deadline_seconds: deadlineSeconds
      }) });
      state.dirtyConfig = false; renderConfig(result.config); formatDeadlineInput(result.config.deadline_seconds);
      toast('主 Agent 设置已保存；已创建任务的模式与 Agent 快照不会改变'); clearError();
    } catch (error) { state.dirtyConfig = true; showError(error); }
    finally { $('#saveConfig').disabled = false; }
  }
  async function requestSummary() {
    if (!state.currentRunId) return;
    $('#requestSummary').disabled = true; clearError();
    try {
      await api(`/api/runs/${encodeURIComponent(state.currentRunId)}/summary`, { method: 'POST', body: '{}' });
      await refreshRun();
    } catch (error) {
      if (error.code === 'summary_unavailable') {
        $('#summaryState').textContent = '模型摘要未就绪'; $('#summaryState').className = 'summary-state unavailable';
        $('#summaryContent').textContent = '本轮未配置可用的摘要 Agent。逐条回复与原始全文仍可查看。';
        $('#summaryHelp').textContent = '请在主 Agent 设置中配置可调用的 summary Agent 后重试。';
        toast('模型摘要未就绪，原始 Agent 回复仍可查看。');
      } else showError(error);
    } finally { $('#requestSummary').disabled = false; }
  }
  async function onResponseClick(event) {
    const button = event.target.closest('[data-agent-id]');
    if (!button) return;
    const id = button.dataset.agentId;
    if (!state.agents.some(agent => agent.id === id)) return;
    if (state.selectedTargets.has(id)) state.selectedTargets.delete(id); else state.selectedTargets.add(id);
    const selected = state.selectedTargets.has(id);
    button.setAttribute('aria-pressed', String(selected)); button.classList.toggle('selected', selected);
    const mark = $('.target-check', button); if (mark) mark.textContent = selected ? '✓' : '+';
    updateDispatchState();
  }
  function bindEvents() {
    $('#agentList').addEventListener('click', onResponseClick);
    $('#targetPicks').addEventListener('click', onResponseClick);
    $('#prompt').addEventListener('input', updateDispatchState);
    $('#startRun').addEventListener('click', createRun);
    $('#saveConfig').addEventListener('click', saveConfig);
    ['#orchestratorSelect', '#summaryAgentSelect', '#orchestratorEnabled', '#summaryPolicy', '#defaultMode'].forEach(selector => {
      $(selector).addEventListener('change', () => { state.dirtyConfig = true; $('#configMessage').textContent = '有未保存的 Hub 设置。'; });
    });
    $('#defaultDeadline').addEventListener('input', () => { state.dirtyConfig = true; $('#configMessage').textContent = '有未保存的 Hub 设置。'; });
    $$('.mode-option').forEach(button => button.addEventListener('click', () => setMode(button.dataset.mode)));
    $$('.filter').forEach(button => button.addEventListener('click', () => {
      state.relation = button.dataset.relation;
      $$('.filter').forEach(item => { const active = item === button; item.classList.toggle('active', active); item.setAttribute('aria-selected', String(active)); });
      renderEvents();
    }));
    $('#requestSummary').addEventListener('click', requestSummary);
    $('#dispatchPolicy').addEventListener('change', updateDispatchState);
    $('#approvePlan').addEventListener('click', approvePlan);
    $('#refreshButton').addEventListener('click', async () => { await refreshState(); await refreshRun(); });
    $('#closeRun').addEventListener('click', () => $('#currentRun').classList.add('hidden'));
    $('#responseDialog').addEventListener('close', () => { $('#responseText').textContent = ''; });
  }
  function startPolling() {
    clearInterval(state.timer);
    if (!TOKEN) return;
    state.timer = setInterval(async () => { await refreshState(); await refreshRun(); }, 2500);
  }

  updateToday(); initSettings(); bindEvents();
  $('#tokenForm').addEventListener('submit', event => {
    event.preventDefault();
    if (setToken($('#tokenInput').value.trim())) loadInitialData().then(startPolling);
    else $('#tokenError').textContent = '请输入有效的 64 位令牌。';
  });
  if (bootstrapToken()) loadInitialData().then(startPolling);
})();
