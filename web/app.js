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
    runTab: null, runTabRunId: null, selectedRepresentatives: new Set(), selectedDiscussion: new Set(), disputeSignature: null, disputeOptionsSignature: null,
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
    state.selectedRepresentatives = new Set([...state.selectedRepresentatives].filter(id => registeredIds.has(id)));
    state.selectedDiscussion = new Set([...state.selectedDiscussion].filter(id => registeredIds.has(id)));
    const list = $('#agentList'), picks = $('#targetPicks');
    list.replaceChildren(); picks.replaceChildren();
    $('#agentCount').textContent = state.agents.length;
    if (!state.agents.length) {
      addText(list, 'div', 'empty-side', '尚无已注册 Agent');
      addText(picks, 'span', 'muted', '先添加自定义模型服务，再创建 Agent。');
      addSetupAction(picks); addSetupAction(list);
      renderCollaborationSelectors();
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
    renderTargetPicks(); renderOrchestratorOptions(); renderCollaborationSelectors(); updateDispatchState();
  }
  function renderTargetPicks() {
    const picks = $('#targetPicks'); picks.replaceChildren();
    if (!state.agents.length) { addText(picks, 'span', 'muted', '请先在设置中创建或连接 Agent。'); return; }
    const discussion = state.mode === 'discussion';
    const selectedIds = discussion ? state.selectedDiscussion : state.selectedTargets;
    const leadId = state.hub?.orchestrator_agent_id;
    for (const agent of state.agents) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'target-pick';
      const isLead = discussion && agent.id === leadId;
      button.dataset.agentId = agent.id; button.disabled = isLead;
      button.setAttribute('aria-pressed', String(selectedIds.has(agent.id)));
      button.classList.toggle('selected', selectedIds.has(agent.id));
      button.append(makeAvatar(agent.id, true)); addText(button, 'span', '', `${agent.name || agent.id}${isLead ? ' · 主 Agent' : ''}`);
      addText(button, 'span', 'target-check', isLead ? '主' : selectedIds.has(agent.id) ? '✓' : '+'); picks.append(button);
    }
  }
  function renderCollaborationSelectors() {
    const picks = $('#representativePicks'); picks.replaceChildren();
    if (!state.agents.length) addText(picks, 'span', 'muted', '请先创建至少两位 Agent。');
    for (const agent of state.agents) {
      const button = addText(picks, 'button', 'collab-agent-pick', agent.name || agent.id);
      button.type = 'button'; button.dataset.agentId = agent.id;
      button.setAttribute('aria-pressed', String(state.selectedRepresentatives.has(agent.id)));
      button.classList.toggle('selected', state.selectedRepresentatives.has(agent.id));
    }
    const select = $('#presetArbiter'), current = select.value;
    select.replaceChildren();
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = '由本轮合格上级处理'; select.append(empty);
    for (const agent of state.agents) {
      const option = document.createElement('option'); option.value = agent.id; option.textContent = agent.name || agent.id; select.append(option);
    }
    select.value = [...select.options].some(option => option.value === current) ? current : '';
  }
  function renderCollabFileList() {
    const list = $('#collabFileList'); list.replaceChildren();
    const files = [...$('#collabFiles').files];
    if (!files.length) { list.textContent = '尚未选择文件。'; return; }
    for (const file of files) addText(list, 'span', 'collab-file', `${file.name} · ${file.size} B`);
  }
  function renderDiscussionFileList() {
    const list = $('#discussionFileList'); list.replaceChildren();
    const files = [...$('#discussionFiles').files];
    if (!files.length) { list.textContent = '尚未选择文件。'; return; }
    for (const file of files) addText(list, 'span', 'collab-file', `${file.name} · ${file.size} B`);
  }
  function setRepresentativeMode() {
    $('#collaborationFields').classList.toggle('hidden', !$('#enableCollaboration').checked);
    updateDispatchState();
  }
  function setAdvancedMode() {
    $('#collaborationConfig').classList.toggle('hidden', !$('#enableAdvancedCollaboration').checked);
    updateDispatchState();
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
      renderCollaborationSelectors();
      if (!state.hubLoaded) { state.mode = config.mode_default === 'orchestrated' ? 'orchestrated' : 'direct'; state.hubLoaded = true; }
      if (state.mode === 'discussion') renderTargetPicks();
      renderOrchestratorOptions();
      $('#orchestratorEnabled').checked = Boolean(config.orchestrator_enabled);
      $('#summaryPolicy').value = config.summary_policy || 'auto';
      $('#defaultMode').value = config.mode_default || 'direct';
      $('#defaultDeadline').value = config.deadline_seconds ?? '';
      $('#maxTasks').value = config.max_tasks ?? 16;
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
    $('#modeDiscussion').setAttribute('aria-disabled', String(!ready || config.discussion_supported !== true));
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
    const discussion = state.mode === 'discussion';
    $$('.mode-option').forEach(button => {
      const selected = button.dataset.mode === state.mode;
      button.classList.toggle('active', selected); button.setAttribute('aria-checked', String(selected));
    });
    const ready = state.hub?.orchestrator_state === 'enabled';
    $('#orchestratorNotice').classList.toggle('hidden', !(orchestrated || discussion) || ready);
    $('#orchestratorNotice span').textContent = '请在设置的协作设置中选择并启用有效的主 Agent，然后保存。';
    $('#modeExplainer').textContent = discussion
      ? '分析者先独立回答；有分歧时最多互评一轮，再由主 Agent 裁决。'
      : orchestrated
      ? '主 Agent 可联系已注册 Agent，并仅从批准模板创建本轮子 Agent。'
      : `工作任务与相互派发仅限本轮选中的 Agent；摘要由${state.hub?.summary_agent_id ? `${agentName(state.hub.summary_agent_id)} 生成` : '配置的摘要 Agent 生成'}。`;
    $('#modePill').textContent = discussion ? `共同研讨 · 主 Agent：${ready ? agentName(state.hub.orchestrator_agent_id) : '尚未就绪'}`
      : orchestrated ? `启用主 Agent · ${ready ? agentName(state.hub.orchestrator_agent_id) : '尚未就绪'}` : '无主 Agent · 直接并行';
    $('#targetPicks').classList.toggle('targets-disabled', orchestrated);
    $('#targetLabel').innerHTML = discussion ? '研讨分析者 <span>选择 2–3 位，不含主 Agent</span>'
      : orchestrated ? '派发目标 <span>由主 Agent 计划选择</span>' : '接收 Agent <span>可多选，一次并行运行</span>';
    $('#orchestrationOptions').classList.toggle('hidden', !orchestrated);
    const collaborationSupported = state.hub?.collaboration_supported === true;
    $('#collaborationSetup').classList.toggle('hidden', !orchestrated || !collaborationSupported);
    $('#collabUnsupported').classList.toggle('hidden', !orchestrated || collaborationSupported);
    $('#discussionSetup').classList.toggle('hidden', !discussion || state.hub?.discussion_supported !== true);
    $('#discussionUnsupported').classList.toggle('hidden', !discussion || state.hub?.discussion_supported === true);
    $('#modeDiscussion').title = state.hub?.discussion_supported === true ? '' : '当前服务暂不支持共同研讨';
    updateDispatchState();
  }
  function updateDispatchState() {
    const orchestrated = state.mode === 'orchestrated';
    const discussion = state.mode === 'discussion';
    const hasTargets = state.selectedTargets.size > 0;
    const ready = state.hub?.orchestrator_state === 'enabled';
    const discussionCount = state.selectedDiscussion.size;
    const requiredTasks = discussionCount * 2 + 2;
    const discussionReady = ready && state.hub?.discussion_supported === true
      && discussionCount >= 2 && discussionCount <= 3
      && Number($('#discussionMaxCalls').value) >= requiredTasks
      && Number(state.hub?.max_tasks ?? 0) >= requiredTasks;
    $('#startRun').disabled = (discussion ? !discussionReady : orchestrated ? !ready : !hasTargets) || !$('#prompt').value.trim();
    $('#startRun').textContent = discussion ? '启动共同研讨 ↑' : orchestrated ? '启动主 Agent 计划 ↑' : '并行派发 ↑';
    $('#dispatchHint').textContent = discussion
      ? !ready ? '请先启用有效的主 Agent。'
        : state.hub?.discussion_supported !== true ? '当前服务暂不支持共同研讨。'
          : discussionCount < 2 ? '请选择 2–3 位分析者；主 Agent 不计入。'
            : Number(state.hub?.max_tasks ?? 0) < requiredTasks ? `当前任务上限不足；${discussionCount} 位分析者至少需要 ${requiredTasks} 个任务名额。`
              : Number($('#discussionMaxCalls').value) < requiredTasks ? `请把模型调用次数上限设为至少 ${requiredTasks}。`
                : `${discussionCount} 位分析者 + ${agentName(state.hub.orchestrator_agent_id)}；有分歧时最多互评一轮，主 Agent 最终裁决。`
      : orchestrated
      ? ready ? ($('#enableAdvancedCollaboration').checked && $('#enableCollaboration').checked ? `先由 ${state.selectedRepresentatives.size} 位代码代表阅读选定文件，再由主 Agent 规划。` : $('#dispatchPolicy').value === 'auto' ? '主 Agent 生成并校验计划后将自动派发任务。' : '主 Agent 生成并校验计划后，会等待你预览和批准。') : '请先在侧栏启用有效的主 Agent。'
      : hasTargets ? `将并行启动 ${state.selectedTargets.size} 个已选 Agent。` : '请选择一个或多个 Agent。';
    const representativeCount = $('#enableAdvancedCollaboration').checked && $('#enableCollaboration').checked ? state.selectedRepresentatives.size : 0;
    const maxTasks = state.hub?.max_tasks ?? 16;
    const remaining = maxTasks - representativeCount - 1;
    $('#collabTaskBudget').textContent = `当前每轮任务上限 ${maxTasks}；${representativeCount} 位代码代表和 1 个主 Agent 规划任务后，最多可派发 ${Math.max(0, remaining)} 个执行任务。${representativeCount && remaining < 2 ? '若需比较两位 Agent 的结论，请在协作设置中调高上限，至少留出 2 个执行任务名额。' : ''}模型调用次数另有限额。`;
    $('#targetPicks').setAttribute('aria-disabled', String(orchestrated));
  }
  function setMode(mode) {
    if (mode === 'discussion' && state.hub?.discussion_supported !== true) {
      toast('当前服务暂不支持共同研讨。'); return;
    }
    state.mode = ['direct', 'orchestrated', 'discussion'].includes(mode) ? mode : 'direct';
    renderTargetPicks();
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
  function boundedInteger(selector, label, min, max) {
    const value = Number($(selector).value);
    if (!Number.isInteger(value) || value < min || value > max) throw new Error(`${label}需为 ${min}–${max} 的整数。`);
    return value;
  }
  async function buildCollaborationPayload() {
    if (state.mode !== 'orchestrated' || state.hub?.collaboration_supported !== true || !$('#enableAdvancedCollaboration').checked) return null;
    const limits = {
      max_concurrent_tasks: boundedInteger('#collabMaxConcurrent', '并发模型调用上限', 1, 8),
      max_context_bytes: boundedInteger('#collabMaxContext', '上下文上限', 1024, 262144),
      max_calls: boundedInteger('#collabMaxCalls', '全轮模型调用次数上限', 1, 100)
    };
    const collaboration = { limits };
    if ($('#enableCollaboration').checked) {
      const agentIds = [...state.selectedRepresentatives];
      if (agentIds.length < 2 || agentIds.length > 3) throw new Error('请选择 2–3 位代码代表。');
      const version = $('#collabVersion').value.trim();
      if (!version || version.length > 128) throw new Error('请填写不超过 128 字的源码版本标签。');
      const files = [...$('#collabFiles').files];
      if (files.length < 1 || files.length > 32) throw new Error('请选择 1–32 个文本源码文件。');
      const seen = new Set(), sourceFiles = [];
      let totalBytes = 0;
      for (const file of files) {
        const path = file.name;
        const foldedPath = path.toLocaleLowerCase();
        if (!path || path === '.' || path === '..' || /[/\\]/.test(path) || seen.has(foldedPath)) throw new Error('源码文件名必须唯一，且不能包含路径分隔符。');
        if (file.size > 131072) throw new Error(`${path} 超过单文件 128 KiB 上限。`);
        seen.add(foldedPath); totalBytes += file.size;
        if (totalBytes > 262144) throw new Error('所选源码文件合计不得超过 256 KiB。');
        const text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
        if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(text)) throw new Error(`${path} 不是可用的文本文件。`);
        sourceFiles.push({ path, text });
      }
      if (!sourceFiles.some(file => file.text.length)) throw new Error('请选择至少一个有内容的源码文件。');
      if (totalBytes > limits.max_context_bytes) throw new Error('源码总字节数超过当前上下文上限。');
      collaboration.source = { version, files: sourceFiles };
      collaboration.representative_agent_ids = agentIds;
    }
    const arbiter = $('#presetArbiter').value;
    if (arbiter) collaboration.arbiter_agent_id = arbiter;
    return collaboration;
  }
  async function buildDiscussionPayload() {
    if (state.hub?.discussion_supported !== true || state.hub?.orchestrator_state !== 'enabled')
      throw new Error('当前服务或主 Agent 尚未支持共同研讨。');
    const ids = [...state.selectedDiscussion];
    const leadId = state.hub.orchestrator_agent_id;
    if (ids.length < 2 || ids.length > 3 || new Set(ids).size !== ids.length || ids.includes(leadId)
      || ids.some(id => !state.agents.some(agent => agent.id === id)))
      throw new Error('请选择 2–3 位不同的已注册分析者，且不能包含主 Agent。');
    const needed = 2 * ids.length + 2;
    if (Number(state.hub.max_tasks) < needed)
      throw new Error(`${ids.length} 位分析者的固定流程至少需要 ${needed} 个任务名额；请在协作设置调高每轮任务数上限。`);
    const limits = {
      max_concurrent_tasks: boundedInteger('#discussionMaxConcurrent', '并发模型调用上限', 1, 8),
      max_context_bytes: boundedInteger('#discussionMaxContext', '上下文上限', 1024, 262144),
      max_calls: boundedInteger('#discussionMaxCalls', '全轮模型调用次数上限', needed, 100)
    };
    const collaboration = { limits };
    const files = [...$('#discussionFiles').files];
    const version = $('#discussionVersion').value.trim();
    if (!files.length && version) throw new Error('未选择源码文件时，请清空版本标签。');
    if (files.length) {
      if (files.length > 32) throw new Error('最多选择 32 个文本源码文件。');
      if (!version || version.length > 128) throw new Error('附上源码时，请填写不超过 128 字的版本标签。');
      const seen = new Set(), sourceFiles = [];
      let totalBytes = 0;
      for (const file of files) {
        const path = file.name, folded = path.toLocaleLowerCase();
        if (!path || path === '.' || path === '..' || /[/\\]/.test(path) || seen.has(folded))
          throw new Error('源码文件名必须唯一，且不能包含路径分隔符。');
        if (file.size > 131072) throw new Error(`${path} 超过单文件 128 KiB 上限。`);
        seen.add(folded); totalBytes += file.size;
        if (totalBytes > 262144) throw new Error('所选源码文件合计不得超过 256 KiB。');
        const fileText = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
        if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(fileText))
          throw new Error(`${path} 不是可用的文本文件。`);
        sourceFiles.push({ path, text: fileText });
      }
      if (!sourceFiles.some(file => file.text.length)) throw new Error('请选择至少一个有内容的源码文件。');
      if (totalBytes > limits.max_context_bytes) throw new Error('源码总字节数超过当前上下文上限。');
      collaboration.source = { version, files: sourceFiles };
    }
    return { target_agent_ids: ids, collaboration };
  }
  function shortId(id) { return id ? id.slice(0, 8) : ''; }
  function countStatus(tasks, statuses) { return tasks.filter(task => statuses.includes(task.status)).length; }
  function statusLabel(task) { return TaskStatus.label(task); }
  const RUN_TABS = ['collaboration', 'outputs', 'results'];
  function setRunTab(name, { focus = false } = {}) {
    if (!RUN_TABS.includes(name)) return;
    state.runTab = name;
    for (const tab of $$('.run-tab')) {
      const selected = tab.dataset.runTab === name;
      tab.classList.toggle('active', selected);
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) tab.focus();
    }
    for (const panel of $$('.run-tab-panel')) panel.hidden = panel.id !== `runPanel${name[0].toUpperCase()}${name.slice(1)}`;
    if (name === 'results') $('#runTabResults').classList.remove('has-update');
    if (name === 'collaboration') $('#runTabCollaboration').classList.remove('needs-action');
  }
  function defaultRunTab(run) {
    if (['completed', 'failed'].includes(run.status) && ['unresolved', 'needs_human'].includes(run.review_state)) return 'results';
    if (['completed', 'failed', 'interrupted'].includes(run.status) && ['ready', 'partial'].includes(run.summary?.status) && run.summary?.content) return 'results';
    if (run.status === 'failed' && run.mode === 'orchestrated' && run.plan?.status === 'failed') return 'collaboration';
    if (run.status === 'completed') return 'outputs';
    return ['orchestrated', 'discussion'].includes(run.mode) ? 'collaboration' : 'outputs';
  }
  function renderRun(run) {
    state.currentRun = run;
    $('#currentRun').classList.remove('hidden');
    if (state.runTabRunId !== run.run_id) {
      state.runTabRunId = run.run_id;
      state.disputeSignature = null; state.disputeOptionsSignature = null;
      setRunTab(defaultRunTab(run));
    }
    $('#runShortId').textContent = shortId(run.run_id);
    $('#runPrompt').textContent = run.prompt || '(无任务描述)';
    $('#runModeLabel').textContent = run.mode === 'discussion'
      ? `共同研讨 · 主 Agent：${agentName(run.orchestrator_agent_id)}`
      : run.mode === 'orchestrated'
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
    const planningTasks = tasks.filter(task => task.role === 'orchestrator');
    const outputTasks = tasks.filter(task => !['orchestrator', 'representative', 'arbiter', 'discussion_compare', 'discussion_final'].includes(task.role));
    const workerTasks = outputTasks.filter(task => task.role === 'worker');
    const done = tasks.filter(task => TaskStatus.isTerminal(task.status)).length;
    const succeeded = countStatus(outputTasks, ['succeeded']);
    $('#runCountLabel').textContent = `${done} / ${tasks.length} 已结束 · ${succeeded} 条成功回复`;
    $('#runProgressBar').style.width = `${tasks.length ? Math.round(done / tasks.length * 100) : 0}%`;
    $('#collaborationCount').textContent = run.mode === 'discussion'
      ? ({ ready: '已裁决', needs_attention: '待处理', failed: '失败' })[run.discussion?.status] || '研讨中'
      : run.plan?.status === 'awaiting_approval' ? '待批准' : planningTasks.length ? String(planningTasks.length) : '';
    $('#outputsCount').textContent = outputTasks.length ? String(outputTasks.length) : '';
    const summaryBadges = { ready: '已生成', partial: '部分', stale: '待更新', failed: '失败' };
    const summaryReady = ['ready', 'partial'].includes(run.summary?.status);
    $('#resultsCount').textContent = ['unresolved', 'needs_human'].includes(run.review_state) ? '待裁决' : summaryBadges[run.summary?.status] || '';
    $('#runTabResults').classList.toggle('has-update', summaryReady && state.runTab !== 'results');
    $('#runTabCollaboration').classList.toggle('needs-action', run.plan?.status === 'awaiting_approval' && state.runTab !== 'collaboration');
    $('#runCollaborationIntro').textContent = run.mode === 'discussion'
      ? '查看独立观点、分歧及最多一轮定向互评；主 Agent 最终裁决。'
      : run.mode === 'orchestrated'
      ? '查看任务计划、执行动态和 Agent 间的交流。'
      : '无主模式没有主 Agent 计划；这里显示任务动态和 Agent 间的交流。';
    $('#planningRecord').hidden = run.mode !== 'orchestrated';
    $('#summaryTitle').textContent = run.mode === 'discussion' ? '主 Agent 汇总' : '本轮汇总';
    $('#requestSummary').classList.toggle('hidden', run.mode === 'discussion');
    renderTasks(outputTasks, $('#runTasks'), run);
    renderTasks(planningTasks, $('#planningTasks'), run, { planning: true });
    renderRepresentatives(run); renderDiscussion(run); renderPlan(run); renderGroups(run); renderDisputes(run);
    renderRunSummary(run, run.mode === 'discussion' ? outputTasks : workerTasks);
    renderReviewResults(run); renderDiscussionResult(run);
    const late = run.deadline_state === 'reached' && done < tasks.length;
    $('#deadlineAlert').classList.toggle('hidden', !late);
    if (late) $('#deadlineAlert').textContent = `已到截止时间，${tasks.length - done} 个任务仍在执行。截止只触发提醒；迟到回复会继续归入本轮任务。当前已有 ${succeeded}/${outputTasks.length} 条成功回复。`;
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
      addText(card, 'small', '', `Agent：${agentName(task.agent_id)}${task.group_id ? ` · 分组 ${task.group_id}` : ''}${task.depends_on?.length ? ` · 依赖 ${task.depends_on.join('、')}` : ''}`);
      addText(card, 'p', '', task.prompt || ''); list.append(card);
    }
    const approve = $('#approvePlan');
    const canApprove = plan.status === 'awaiting_approval';
    approve.classList.toggle('hidden', !canApprove);
    approve.disabled = !canApprove;
  }
  function renderRepresentatives(run) {
    const collaboration = run.collaboration || {};
    const representatives = Array.isArray(collaboration.representatives) ? collaboration.representatives : [];
    const selected = Array.isArray(collaboration.representative_agent_ids) ? collaboration.representative_agent_ids : [];
    const panel = $('#representativePanel'), list = $('#representativeResults'); list.replaceChildren();
    panel.classList.toggle('hidden', !run.collaboration || run.mode === 'discussion');
    if (!run.collaboration || run.mode === 'discussion') return;
    const source = collaboration.source || {};
    const files = Array.isArray(source.files) ? source.files : [];
    $('#collabSourceMeta').textContent = files.length
      ? `源码版本：${source.version || '未标注'} · 已提交 ${files.length} 个文本文件：${files.map(file => file.path).join('、')}`
      : '本轮未启用代码代表，也未提交源码文件。';
    const limits = collaboration.limits || {};
    $('#collabBudgetMeta').textContent = `每轮任务数上限 ${run.max_tasks ?? '—'} · 并发模型调用上限 ${limits.max_concurrent_tasks ?? '—'} · 全轮模型调用次数上限 ${limits.max_calls ?? '—'}（已使用 ${collaboration.calls_used ?? '—'}） · 上下文上限 ${limits.max_context_bytes ?? '—'} 字节`;
    for (const agentId of selected) {
      const representative = representatives.find(item => item.agent_id === agentId) || {};
      const card = document.createElement('article'); card.className = 'collab-info-card';
      addText(card, 'b', '', agentName(agentId));
      addText(card, 'small', 'collab-meta', `代码代表 · ${representative.status ? statusLabel(representative) : '等待开始'}`);
      const explanation = representative.explanation;
      if (explanation && typeof explanation === 'object') {
        if (explanation.summary) addText(card, 'p', '', explanation.summary);
        if (explanation.source_version) addText(card, 'p', 'collab-meta', `对应源码版本：${explanation.source_version}`);
        addText(card, 'p', 'collab-meta', explanation.reported_verified ? '代表自述已验证；Hub 尚未独立验证。' : '代表未声称验证通过；仍需独立核验。');
        for (const finding of Array.isArray(explanation.evidence) ? explanation.evidence : []) {
          addText(card, 'p', 'collab-finding', `${finding.path} 第 ${finding.line_start}–${finding.line_end} 行 · ${finding.note}`);
        }
        for (const unknown of Array.isArray(explanation.unknowns) ? explanation.unknowns : []) addText(card, 'p', 'collab-meta', `待确认：${unknown}`);
      } else if (typeof explanation === 'string') addText(card, 'p', '', explanation);
      else addText(card, 'p', 'muted', '代码说明尚未就绪。');
      if (representative.error?.message) addText(card, 'p', 'task-error', representative.error.message);
      const task = (run.tasks || []).find(item => item.task_id === representative.task_id);
      if (task?.response_id) {
        const button = addText(card, 'button', 'response-link', '展开代表原文'); button.type = 'button';
        button.addEventListener('click', () => showResponse(task));
      }
      list.append(card);
    }
  }
  function discussionTask(run, taskId) {
    return (run.tasks || []).find(task => task.task_id === taskId);
  }
  function addDiscussionTaskLink(card, task) {
    if (!task?.response_id) return;
    const button = addText(card, 'button', 'response-link', '展开原始全文');
    button.type = 'button';
    button.addEventListener('click', () => showResponse(task));
  }
  function renderDiscussion(run) {
    const panel = $('#discussionPanel'), list = $('#discussionStages');
    panel.classList.toggle('hidden', run.mode !== 'discussion');
    if (run.mode !== 'discussion') return;
    const discussion = run.discussion || {};
    const labels = { analyzing: '独立分析中', comparing: '提取分歧中', reviewing: '定向互评中',
      finalizing: '主 Agent 裁决中', ready: '已裁决', needs_attention: '存在未决项',
      failed: '研讨失败', interrupted: '已中断' };
    $('#discussionState').textContent = labels[discussion.status] || '准备中';
    $('#discussionState').className = `summary-state ${discussion.status || 'pending'}`;
    const limits = run.collaboration?.limits || {};
    $('#discussionBudget').textContent = `主 Agent：${agentName(run.orchestrator_agent_id)} · 分析者 ${(discussion.analyst_agent_ids || []).map(agentName).join('、') || '尚未建立'} · 模型调用 ${run.collaboration?.calls_used ?? 0}/${limits.max_calls ?? '—'} · 并发上限 ${limits.max_concurrent_tasks ?? '—'} · 上下文上限 ${limits.max_context_bytes ?? '—'} 字节。有分歧时最多互评一轮。`;
    list.replaceChildren();
    if (['needs_attention', 'failed', 'interrupted'].includes(discussion.status)) {
      const reasons = discussion.unresolved?.length ? discussion.unresolved
        : !discussion.final && run.summary?.content ? [run.summary.content] : [];
      for (const reason of reasons) addText(list, 'p', 'collab-unresolved', `${discussion.final ? '未决' : '停止原因'}：${reason}`);
    }
    const heading = (title, help) => {
      const block = document.createElement('section'); block.className = 'discussion-stage';
      addText(block, 'h4', '', title);
      if (help) addText(block, 'p', 'collab-meta', help);
      list.append(block); return block;
    };
    const analysisBlock = heading('1 · 独立分析', '每位分析者先独立回答，原文可在“Agent 输出”展开。');
    for (const id of discussion.analysis_task_ids || []) {
      const task = discussionTask(run, id), result = task?.discussion_result || {};
      const card = document.createElement('article'); card.className = 'collab-info-card';
      addText(card, 'b', '', `${agentName(task?.agent_id)} · ${task ? statusLabel(task) : '等待开始'}`);
      if (result.summary) addText(card, 'p', '', result.summary);
      if (result.proposal) addText(card, 'p', 'collab-finding', `观点：${result.proposal}`);
      for (const note of result.evidence || []) addText(card, 'p', 'collab-meta', `依据：${note}`);
      for (const risk of result.risks || []) addText(card, 'p', 'collab-meta', `风险：${risk}`);
      if (task?.error?.message) addText(card, 'p', 'task-error', task.error.message);
      addDiscussionTaskLink(card, task); analysisBlock.append(card);
    }
    if (!(discussion.analysis_task_ids || []).length) addText(analysisBlock, 'p', 'muted', '等待分析任务建立。');
    const compareTask = discussionTask(run, discussion.compare_task_id);
    const compare = discussion.compare || compareTask?.discussion_result || {};
    const compareBlock = heading('2 · 主 Agent 提取分歧', compareTask ? `状态：${statusLabel(compareTask)}` : '等待独立分析完成。');
    if (compare.summary) addText(compareBlock, 'p', '', compare.summary);
    for (const conflict of compare.conflicts || []) {
      const card = document.createElement('article'); card.className = 'collab-info-card';
      addText(card, 'b', '', `分歧 ${conflict.conflict_id || '未编号'}`);
      addText(card, 'p', '', conflict.description || '等待分歧说明。');
      const agents = (conflict.response_ids || []).map(id => discussionTask(run, (run.tasks || []).find(task => task.response_id === id)?.task_id)?.agent_id).filter(Boolean);
      if (agents.length) addText(card, 'small', 'collab-meta', `涉及：${agents.map(agentName).join('、')}`);
      compareBlock.append(card);
    }
    if (compareTask?.error?.message) addText(compareBlock, 'p', 'task-error', compareTask.error.message);
    addDiscussionTaskLink(compareBlock, compareTask);
    const reviewBlock = heading('3 · 定向互评（最多一轮）', '只围绕已识别的分歧，保留每位分析者的原始回复。');
    for (const id of discussion.review_task_ids || []) {
      const task = discussionTask(run, id), result = task?.discussion_result || {};
      const card = document.createElement('article'); card.className = 'collab-info-card';
      addText(card, 'b', '', `${agentName(task?.agent_id)} · ${task ? statusLabel(task) : '等待开始'}`);
      if (result.summary) addText(card, 'p', '', result.summary);
      for (const review of result.reviews || []) {
        addText(card, 'p', 'collab-finding', `${review.conflict_id || '分歧'}：${review.position || '待复核'}`);
        for (const note of review.evidence || []) addText(card, 'p', 'collab-meta', `依据：${note}`);
      }
      if (task?.error?.message) addText(card, 'p', 'task-error', task.error.message);
      addDiscussionTaskLink(card, task); reviewBlock.append(card);
    }
    const noConflict = compareTask?.status === 'succeeded' && Array.isArray(compare.conflicts) && compare.conflicts.length === 0;
    if (!(discussion.review_task_ids || []).length) addText(reviewBlock, 'p', 'muted',
      noConflict ? '无分歧，已跳过互评。'
        : ['needs_attention', 'failed', 'interrupted'].includes(discussion.status) ? '研讨已停止，互评未进行。'
          : '等待分歧提取；有分歧时才进行互评。');
    const finalTask = discussionTask(run, discussion.final_task_id);
    const finalBlock = heading('4 · 主 Agent 裁决', finalTask ? `状态：${statusLabel(finalTask)}`
      : noConflict ? '无分歧，跳过互评后直接裁决。'
        : ['needs_attention', 'failed', 'interrupted'].includes(discussion.status) ? '研讨已停止，未进入裁决阶段。'
          : '等待分歧核对或互评完成。');
    addText(finalBlock, 'p', 'collab-meta', '裁决依据、采纳结论与未决项显示在“最终结果”。');
    if (finalTask?.error?.message) addText(finalBlock, 'p', 'task-error', finalTask.error.message);
    addDiscussionTaskLink(finalBlock, finalTask);
  }
  function renderDiscussionResult(run) {
    const panel = $('#discussionResultPanel'), list = $('#discussionResult');
    panel.classList.toggle('hidden', run.mode !== 'discussion');
    if (run.mode !== 'discussion') return;
    list.replaceChildren();
    const discussion = run.discussion || {}, result = discussion.final || {};
    if (!discussion.final) {
      const terminal = ['needs_attention', 'failed', 'interrupted'].includes(discussion.status);
      addText(list, 'p', terminal ? 'collab-unresolved' : 'muted', terminal
        ? '研讨已停止，主 Agent 未能给出完整裁决。'
        : '主 Agent 尚未完成裁决；各分析者原文仍可在“Agent 输出”查看。');
      const reasons = discussion.unresolved?.length ? discussion.unresolved
        : terminal && run.summary?.content ? [run.summary.content] : [];
      for (const reason of reasons) addText(list, 'p', 'collab-unresolved', `停止原因：${reason}`);
      return;
    }
    if (result.summary) addText(list, 'p', '', result.summary);
    for (const decision of result.decisions || []) {
      const card = document.createElement('article'); card.className = 'collab-info-card';
      addText(card, 'b', '', `${decision.conflict_id || '分歧'} · ${decision.status === 'resolved' ? '已裁决' : '未决'}`);
      if (decision.resolution) addText(card, 'p', '', decision.resolution);
      const adopted = (decision.adopted_response_ids || []).map(id => (run.tasks || []).find(task => task.response_id === id)).filter(Boolean);
      if (adopted.length) addText(card, 'p', 'collab-resolution', `采纳：${adopted.map(task => agentName(task.agent_id)).join('、')}；原回复保留。`);
      const evidence = (decision.evidence_response_ids || []).map(id => (run.tasks || []).find(task => task.response_id === id)).filter(Boolean);
      if (evidence.length) addText(card, 'small', 'collab-meta', `参考回复：${evidence.map(task => agentName(task.agent_id)).join('、')}`);
      for (const task of adopted) addDiscussionTaskLink(card, task);
      list.append(card);
    }
    const unresolved = result.unresolved?.length ? result.unresolved : discussion.unresolved || [];
    for (const item of unresolved) addText(list, 'p', 'collab-unresolved', `未决：${item}`);
    if (!unresolved.length && !(result.decisions || []).length) addText(list, 'p', 'muted', '本轮没有需要逐项裁决的分歧。');
  }
  function renderGroups(run) {
    const groups = Array.isArray(run.groups) && run.groups.length ? run.groups : Array.isArray(run.plan?.groups) ? run.plan.groups : [];
    const panel = $('#groupPanel'), list = $('#groupList'); list.replaceChildren();
    panel.classList.toggle('hidden', !groups.length);
    for (const group of groups) {
      const card = document.createElement('article'); card.className = 'collab-info-card';
      addText(card, 'b', '', group.group_id || '未命名分组');
      addText(card, 'p', '', `组长：${agentName(group.leader_agent_id)}`);
      addText(card, 'p', 'collab-meta', `成员：${(group.member_agent_ids || []).map(agentName).join('、') || '尚无成员'}`);
      list.append(card);
    }
  }
  function sourceFilesFor(run) { return Array.isArray(run.collaboration?.source?.files) ? run.collaboration.source.files : []; }
  function fillSelect(select, rows, placeholder, selected = '') {
    select.replaceChildren();
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = placeholder; select.append(empty);
    for (const [value, label] of rows) {
      const option = document.createElement('option'); option.value = value; option.textContent = label; select.append(option);
    }
    select.value = rows.some(([value]) => value === selected) ? selected : '';
  }
  function eligibleDisputeTasks(run) {
    return (Array.isArray(run.tasks) ? run.tasks : []).filter(task => task.role === 'worker' && task.status === 'succeeded' && task.response_id);
  }
  function eligibleArbiters(run, left, right) {
    if (!left || !right || left.agent_id === right.agent_id) return [];
    const ids = new Set();
    if (run.collaboration?.arbiter_agent_id) ids.add(run.collaboration.arbiter_agent_id);
    if (run.orchestrator_agent_id) ids.add(run.orchestrator_agent_id);
    if (left.group_id && left.group_id === right.group_id) {
      const group = (run.groups || []).find(item => item.group_id === left.group_id);
      if (group?.leader_agent_id) ids.add(group.leader_agent_id);
    }
    ids.delete(left.agent_id); ids.delete(right.agent_id);
    return [...ids].map(id => [id, agentName(id)]);
  }
  function refreshDisputeArbiters() {
    const run = state.currentRun;
    if (!run) return;
    const left = eligibleDisputeTasks(run).find(task => task.task_id === $('#disputeLeft').value);
    const right = eligibleDisputeTasks(run).find(task => task.task_id === $('#disputeRight').value);
    const arbiter = $('#disputeArbiter');
    fillSelect(arbiter, eligibleArbiters(run, left, right), left && right && left.agent_id === right.agent_id ? '请选择不同 Agent 的结论' : '选择合格的上级 Agent', arbiter.value);
  }
  function renderDisputeOptions(run) {
    const eligible = eligibleDisputeTasks(run);
    const signature = JSON.stringify([run.run_id, eligible.map(task => [task.task_id, task.agent_id]), sourceFilesFor(run).map(file => file.path)]);
    if (state.disputeOptionsSignature === signature) return;
    state.disputeOptionsSignature = signature;
    const rows = eligible.map(task => [task.task_id, `${agentName(task.agent_id)} · 任务 ${task.sequence ?? shortId(task.task_id)}`]);
    for (const selector of ['#disputeLeft', '#disputeRight']) fillSelect($(selector), rows, '选择已完成任务', $(selector).value);
    const files = sourceFilesFor(run);
    fillSelect($('#disputeSourcePath'), files.map(file => [file.path, file.path]), '选择已提交的源码文件', $('#disputeSourcePath').value);
    $('#disputeSourceRow').classList.toggle('hidden', !files.length);
    $('#disputeLineStartRow').classList.toggle('hidden', !files.length);
    $('#disputeLineEndRow').classList.toggle('hidden', !files.length);
    $('#disputeCreate').classList.toggle('hidden', eligible.length < 2 || state.hub?.collaboration_supported !== true);
    refreshDisputeArbiters();
  }
  function evidenceItem(note, path, lineStart, lineEnd, files) {
    const description = String(note || '').trim();
    if (!description) throw new Error('请填写可复核的证据说明。');
    if (!files.length) return { note: description };
    if (!files.some(file => file.path === path)) throw new Error('请选择本轮已提交的证据文件。');
    const start = Number(lineStart), end = Number(lineEnd);
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start) throw new Error('请填写有效的证据起止行。');
    return { path, line_start: start, line_end: end, note: description };
  }
  function addEvidenceFields(form, files) {
    const noteLabel = addText(form, 'label', 'form-field wide', '补充证据说明');
    const note = document.createElement('textarea'); note.name = 'note'; note.required = true; note.rows = 3; noteLabel.append(note);
    if (!files.length) return;
    const pathLabel = addText(form, 'label', 'form-field', '证据文件');
    const path = document.createElement('select'); path.name = 'path'; path.required = true;
    fillSelect(path, files.map(file => [file.path, file.path]), '选择源码文件'); pathLabel.append(path);
    for (const [name, label] of [['line_start', '起始行'], ['line_end', '结束行']]) {
      const wrapper = addText(form, 'label', 'form-field', label);
      const input = document.createElement('input'); input.name = name; input.type = 'number'; input.min = '1'; input.required = true; wrapper.append(input);
    }
  }
  function addResponseAction(parent, task, label) {
    if (!task?.response_id) return;
    const button = addText(parent, 'button', 'response-link', label); button.type = 'button';
    button.addEventListener('click', () => showResponse(task));
  }
  function renderDisputes(run) {
    const panel = $('#disputePanel'), list = $('#disputeList');
    const disputes = Array.isArray(run.disputes) ? run.disputes : [];
    panel.classList.toggle('hidden', run.mode === 'discussion' || (!run.collaboration && !disputes.length));
    if (run.mode === 'discussion' || (!run.collaboration && !disputes.length)) return;
    renderDisputeOptions(run);
    const signature = JSON.stringify([run.run_id, disputes]);
    if (state.disputeSignature === signature) return;
    state.disputeSignature = signature; list.replaceChildren();
    if (!disputes.length) addText(list, 'p', 'muted', '尚无争议。分组任务完成后，可比较两位 Agent 的结论。');
    for (const dispute of disputes) {
      const card = document.createElement('article'); card.className = 'collab-info-card dispute-card';
      addText(card, 'b', '', `争议 ${shortId(dispute.dispute_id)} · ${disputeStatusLabel(dispute.status)}`);
      addText(card, 'p', '', dispute.claim || '未提供争议说明。');
      const left = (run.tasks || []).find(task => task.task_id === dispute.left_task_id);
      const right = (run.tasks || []).find(task => task.task_id === dispute.right_task_id);
      addText(card, 'p', 'collab-meta', `结论 A：${agentName(left?.agent_id)} · 结论 B：${agentName(right?.agent_id)} · 仲裁：${agentName(dispute.arbiter_agent_id)}`);
      addResponseAction(card, left, '查看结论 A 原文'); addResponseAction(card, right, '查看结论 B 原文');
      for (const item of Array.isArray(dispute.evidence) ? dispute.evidence : []) {
        addText(card, 'p', 'collab-finding', `${item.path ? `${item.path} 第 ${item.line_start}–${item.line_end} 行 · ` : ''}${item.note || ''}`);
      }
      for (const [index, round] of (Array.isArray(dispute.rounds) ? dispute.rounds : []).entries()) {
        addText(card, 'p', 'collab-meta', `第 ${index + 1} 轮：${round.verdict?.rationale || round.rationale || disputeStatusLabel(round.verdict?.status)}`);
      }
      if (dispute.status === 'open' && state.hub?.collaboration_supported === true) {
        const button = addText(card, 'button', 'secondary-button', '发起上级仲裁'); button.type = 'button';
        button.addEventListener('click', () => reviewDispute(run.run_id, dispute.dispute_id, button));
      } else if (dispute.status === 'needs_evidence' && state.hub?.collaboration_supported === true) {
        const form = document.createElement('form'); form.className = 'dispute-action-form';
        addEvidenceFields(form, sourceFilesFor(run));
        const submit = addText(form, 'button', 'secondary-button', '补证据并再审'); submit.type = 'submit';
        form.addEventListener('submit', event => { event.preventDefault(); supplementDispute(run.run_id, dispute.dispute_id, form); });
        card.append(form);
      } else if (dispute.status === 'needs_human' && state.hub?.collaboration_supported === true) {
        const form = document.createElement('form'); form.className = 'dispute-action-form';
        const label = addText(form, 'label', 'form-field', '人工采纳');
        const choice = document.createElement('select'); choice.name = 'selected_response_id'; choice.required = true;
        fillSelect(choice, [[dispute.left_response_id, '结论 A'], [dispute.right_response_id, '结论 B']], '选择一方'); label.append(choice);
        const reasonLabel = addText(form, 'label', 'form-field wide', '决定依据');
        const reason = document.createElement('textarea'); reason.name = 'rationale'; reason.required = true; reason.rows = 3; reasonLabel.append(reason);
        const submit = addText(form, 'button', 'secondary-button', '记录人工决定'); submit.type = 'submit';
        form.addEventListener('submit', event => { event.preventDefault(); decideDispute(run.run_id, dispute.dispute_id, form); });
        card.append(form);
      } else if (dispute.status === 'resolved') {
        const resolution = dispute.resolution || {};
        const selected = selectedDisputeResponse(dispute);
        addText(card, 'p', 'collab-resolution', `已采纳 ${selected === dispute.left_response_id ? '结论 A' : selected === dispute.right_response_id ? '结论 B' : '指定结论'}${resolution.rationale ? ` · ${resolution.rationale}` : ''}`);
      }
      list.append(card);
    }
  }
  function reviewStateLabel(value) {
    return ({ clear: '暂无争议', unresolved: '有未决争议', resolved: '争议已处理', needs_human: '需要人工决定' })[value] || '审阅状态待更新';
  }
  function disputeStatusLabel(value) {
    return ({ open: '待仲裁', reviewing: '仲裁中', needs_evidence: '等待补证据', needs_human: '等待人工决定', resolved: '已处理' })[value] || value || '状态未知';
  }
  function selectedDisputeResponse(dispute) {
    return dispute.selected_response_id || dispute.resolution?.selected_response_id || dispute.decision?.selected_response_id || null;
  }
  function renderReviewResults(run) {
    const disputes = Array.isArray(run.disputes) ? run.disputes : [];
    const panel = $('#resultReviewPanel'), list = $('#resultReviewList'); list.replaceChildren();
    panel.classList.toggle('hidden', run.mode === 'discussion' || (!run.collaboration && !disputes.length));
    if (panel.classList.contains('hidden')) return;
    addText(list, 'p', 'collab-review-state', reviewStateLabel(run.review_state));
    if (!disputes.length) { addText(list, 'p', 'muted', '当前没有提交的同级争议。'); return; }
    for (const dispute of disputes) {
      const card = document.createElement('article'); card.className = 'collab-info-card';
      addText(card, 'b', '', `争议 ${shortId(dispute.dispute_id)} · ${disputeStatusLabel(dispute.status)}`);
      addText(card, 'p', '', dispute.claim || '未提供争议说明。');
      const selected = selectedDisputeResponse(dispute);
      if (dispute.status === 'resolved' && selected) {
        const side = selected === dispute.left_response_id ? '结论 A' : selected === dispute.right_response_id ? '结论 B' : '指定结论';
        addText(card, 'p', 'collab-resolution', `已采纳：${side}。原始双方结论保留在协作记录中。`);
      } else if (dispute.status === 'resolved') addText(card, 'p', 'collab-resolution', '已裁决；请在协作记录查看裁决依据。');
      else addText(card, 'p', 'collab-unresolved', '此争议仍未完成裁决，不能视为最终结论。');
      list.append(card);
    }
  }
  function formatDuration(value) {
    const seconds = Math.max(0, Math.floor(value));
    const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
    return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分`;
  }
  function renderTasks(tasks, list, run, { planning = false } = {}) {
    list.replaceChildren();
    if (!tasks.length) {
      const message = planning
        ? run.mode === 'orchestrated' ? '主 Agent 规划任务尚未建立。' : '无主模式没有主 Agent 规划任务；可在本轮动态查看实际通信与状态事件。'
        : run.mode === 'orchestrated' ? '计划尚未派发执行任务。批准计划后，Agent 输出会显示在这里。' : '本轮尚未创建执行任务。';
      addText(list, 'p', 'empty-events', message); return;
    }
    for (const task of tasks) {
      const safeStatus = /^[a-z_]+$/.test(task.status || '') ? task.status : 'unknown';
      const row = document.createElement('article'); row.className = `run-task status-${safeStatus}`;
      row.append(makeAvatar(task.agent_id, true));
      const meta = document.createElement('div'); meta.className = 'run-task-meta';
      addText(meta, 'b', '', agentName(task.agent_id));
      const roleLabel = planning ? '主 Agent · 规划'
        : task.role === 'discussion_analysis' ? '共同研讨 · 独立分析'
          : task.role === 'discussion_review' ? '共同研讨 · 定向互评'
        : task.role === 'orchestrator_reply' ? '主 Agent · 执行回复'
          : run.child_agents?.[task.agent_id] ? '本轮子 Agent'
            : task.parent_task_id ? '派生任务' : 'Agent';
      addText(meta, 'small', 'run-task-role', `${roleLabel}${task.group_id ? ` · 分组 ${task.group_id}` : ''} · 任务 ${task.sequence ?? '—'} · ${task.started_at ? dateLabel(task.started_at) : '尚未启动'}`);
      row.append(meta);
      const statePill = addText(row, 'span', `task-state ${task.overdue ? 'overdue' : task.status}`, statusLabel(task));
      statePill.setAttribute('aria-label', `任务状态：${statusLabel(task)}`);
      if (task.error?.message) addText(row, 'p', 'task-error', task.error.message);
      const discussionPhase = task.role?.startsWith('discussion_');
      const summary = discussionPhase && task.discussion_result
        ? { status: 'ready', content: task.discussion_result.summary, source_agent_id: task.agent_id }
        : task.summary;
      const taskSummary = document.createElement('div'); taskSummary.className = 'task-summary';
      const summaryText = typeof summary === 'string' ? summary : summary?.content;
      const summaryStatus = typeof summary === 'object' ? summary?.status : summaryText ? 'ready' : 'idle';
      if (discussionPhase && !task.discussion_result) {
        const terminal = TaskStatus.isTerminal(task.status);
        addText(taskSummary, 'span', `task-summary-label ${terminal ? 'failed' : 'pending'}`, terminal ? '阶段输出未生成' : '阶段输出等待中');
        addText(taskSummary, 'p', 'task-summary-content muted', task.error?.message
          || (terminal ? '该阶段没有有效的结构化结论；可查看任务状态和原始回复。' : '等待该阶段完成。'));
      } else if (summaryStatus === 'ready' && summaryText) {
        addText(taskSummary, 'span', 'task-summary-label', task.role?.startsWith('discussion_') ? '阶段结论' : '模型摘要');
        addText(taskSummary, 'p', 'task-summary-content', summaryText);
        const source = task.role?.startsWith('discussion_') ? '由本轮研讨 Agent 生成'
          : summary.source_agent_id ? `由 ${agentName(summary.source_agent_id)} 生成` : '由模型生成';
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
  function renderRunSummary(run, workerTasks) {
    const summary = run.summary || { status: 'unavailable', reason: 'No model summary adapter configured' };
    if (run.mode === 'discussion') {
      const labels = { ready: '主 Agent 裁决已完成', partial: '裁决含未决项', pending: '裁决进行中', failed: '裁决失败' };
      const stoppedWithoutFinal = !run.discussion?.final && ['needs_attention', 'failed', 'interrupted'].includes(run.discussion?.status);
      $('#summaryState').textContent = stoppedWithoutFinal ? '研讨已停止，未形成裁决' : labels[summary.status] || '等待主 Agent 裁决';
      $('#summaryState').className = `summary-state ${summary.status || 'pending'}`;
      $('#summaryContent').textContent = summary.content || run.discussion?.final?.summary
        || '研讨尚未完成；独立分析和互评记录可在前两个页签查看。';
      const reviewed = (run.discussion?.review_task_ids || []).length > 0;
      $('#summaryHelp').textContent = stoppedWithoutFinal
        ? '未形成完整裁决。停止原因和未决项见下方；已有的 Agent 原始回复仍可查看。'
        : reviewed
        ? '这是主 Agent 根据独立分析与最多一轮定向互评生成的裁决汇总；原始回复仍可展开查看。'
        : '这是主 Agent 根据独立分析与分歧核对生成的裁决汇总；无分歧时会跳过互评。原始回复仍可展开查看。';
      const failed = (run.tasks || []).filter(task => task.role?.startsWith('discussion_') && TaskStatus.isTerminal(task.status) && task.status !== 'succeeded');
      const unresolved = run.discussion?.unresolved || [];
      $('#summaryExceptions').textContent = [
        failed.length ? `${failed.length} 个研讨阶段未成功。` : '',
        unresolved.length ? `仍有 ${unresolved.length} 项未决；不可视作完整结论。` : '',
        summary.error?.message ? `汇总异常：${summary.error.message}` : ''
      ].filter(Boolean).join(' ');
      $('#requestSummary').disabled = true;
      return;
    }
    const statusLabels = { ready: '完整模型摘要', partial: '部分模型摘要', stale: '摘要待更新', pending: '生成中', failed: '生成失败', unavailable: '模型摘要未就绪', blocked_review: '等待争议处理' };
    $('#summaryState').textContent = statusLabels[summary.status] || '模型摘要状态未知';
    $('#summaryState').className = `summary-state ${summary.status || 'unknown'}`;
    if (['unresolved', 'needs_human'].includes(run.review_state) || summary.status === 'blocked_review') $('#summaryContent').textContent = '本轮仍有未决争议。请先在“思考与协作”处理证据、仲裁或人工决定；此处不能视为最终结论。';
    else if (summary.content) $('#summaryContent').textContent = summary.content;
    else if (summary.status === 'stale') $('#summaryContent').textContent = '有新回复到达，当前摘要已过期。请重新生成摘要以包含最新回复。';
    else if (summary.status === 'failed') $('#summaryContent').textContent = '模型摘要生成失败；逐条 Agent 回复仍可查看和展开。';
    else if (summary.status === 'pending') $('#summaryContent').textContent = `正在汇总当前 ${summary.task_ids?.length || 0}/${workerTasks.length} 个已结束执行任务…`;
    else $('#summaryContent').textContent = summary.reason || '本轮未配置可用的摘要 Agent。逐条任务回复和原始全文仍可查看。';
    $('#summaryHelp').textContent = ['unresolved', 'needs_human'].includes(run.review_state)
      ? '双方原始结论和证据保留在“思考与协作”。解决争议后再查看更新的模型汇总。'
      : summary.source_run_revision != null
      ? `覆盖 ${summary.task_ids?.length || 0}/${workerTasks.length} 个执行任务 · 生成于 ${dateLabel(summary.generated_at)} · 新回复可能使摘要过期。`
      : '摘要由模型生成；不会把 Agent 原始回复伪装成摘要。';
    const outputTasks = (Array.isArray(run.tasks) ? run.tasks : []).filter(task => task.role !== 'orchestrator');
    const problems = outputTasks.filter(task => !['representative', 'arbiter'].includes(task.role) && TaskStatus.isTerminal(task.status) && task.status !== 'succeeded');
    const representativeProblems = outputTasks.filter(task => task.role === 'representative' && TaskStatus.isTerminal(task.status) && task.status !== 'succeeded');
    const notes = [];
    if (run.plan?.status === 'failed') notes.push(`主 Agent 计划失败${run.plan.error?.message ? `：${run.plan.error.message}` : '。'}`);
    if (problems.length) notes.push(`${problems.length} 个执行任务未成功：${problems.map(task => `${agentName(task.agent_id)}（${statusLabel(task)}）`).join('、')}。`);
    if (representativeProblems.length) notes.push(`${representativeProblems.length} 位代码代表未成功：${representativeProblems.map(task => `${agentName(task.agent_id)}（${statusLabel(task)}）`).join('、')}。`);
    if (outputTasks.some(task => task.role === 'orchestrator_reply')) notes.push('主 Agent 的执行回复不计入本轮汇总，可在 Agent 输出查看。');
    if (summary.error?.message) notes.push(`汇总异常：${summary.error.message}`);
    if (['unresolved', 'needs_human'].includes(run.review_state)) notes.push('争议未决，当前汇总不可作为最终结果。');
    $('#summaryExceptions').textContent = notes.join(' ');
    $('#requestSummary').textContent = summary.status === 'stale' ? '更新模型摘要' : summary.status === 'partial' ? '更新当前摘要' : '请求模型摘要';
    $('#requestSummary').disabled = ['unresolved', 'needs_human'].includes(run.review_state);
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
      if (event.kind === 'response' && event.task_id) {
        const task = state.currentRun?.tasks?.find(row => row.task_id === event.task_id);
        if (task) {
          const planning = task.role === 'orchestrator';
          const representative = task.role === 'representative', arbiter = task.role === 'arbiter';
          const compare = task.role === 'discussion_compare', final = task.role === 'discussion_final';
          const jump = addText(body, 'button', 'event-jump', planning ? '查看规划原始记录' : representative ? '查看代码代表' : arbiter ? '查看争议与仲裁' : compare ? '查看研讨分歧' : final ? '查看主 Agent 裁决' : '查看 Agent 输出');
          jump.type = 'button';
          jump.addEventListener('click', () => {
            setRunTab(final ? 'results' : planning || representative || arbiter || compare ? 'collaboration' : 'outputs', { focus: true });
            if (planning) $('#planningRecord').open = true;
            if (representative) $('#representativePanel').focus();
            if (arbiter) $('#disputePanel').focus();
            if (compare) $('#discussionPanel').focus();
            if (final) $('#discussionResultPanel').focus();
          });
        }
      }
      if (event.task_id) addText(body, 'small', 'event-task-id', `任务 ${shortId(event.task_id)}`);
      item.append(body); list.append(item);
    }
  }
  function eventDescription(event) {
    if (event.kind === 'status') return `任务状态：${event.state || '已更新'}${event.error_category ? ` · ${event.error_category}` : ''}`;
    if (event.kind === 'response') return `回复已${event.state === 'succeeded' ? '完成' : '更新'}。`;
    if (event.kind === 'deadline') return '本轮截止时间已到；未完成任务继续运行，迟到回复仍会归入本轮。';
    if (event.kind === 'summary') return `模型摘要状态：${event.state || '已更新'}`;
    if (event.kind === 'dispute') return `争议状态：${disputeStatusLabel(event.state)}。`;
    if (event.kind === 'review') return `仲裁状态：${disputeStatusLabel(event.state)}。`;
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
    if (['orchestrated', 'discussion'].includes(state.mode) && state.hub?.orchestrator_state !== 'enabled') { showError(new Error('请先启用有效的主 Agent。')); return; }
    let collaboration, discussionPayload;
    try {
      if (state.mode === 'discussion') discussionPayload = await buildDiscussionPayload();
      else collaboration = await buildCollaborationPayload();
    }
    catch (error) { showError(error); if (state.mode === 'orchestrated') $('#collaborationSetup').open = true; return; }
    $('#startRun').disabled = true; $('#startRun').textContent = '正在提交…';
    const idempotencyKey = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
    const body = state.mode === 'discussion'
      ? { mode: 'discussion', prompt, ...discussionPayload }
      : state.mode === 'orchestrated'
        ? { mode: 'orchestrated', dispatch_policy: $('#dispatchPolicy').value, prompt }
        : { mode: 'direct', prompt, target_agent_ids: [...state.selectedTargets] };
    if (deadlineSeconds != null) body.deadline_seconds = deadlineSeconds;
    if (collaboration) body.collaboration = collaboration;
    try {
      const accepted = await api('/api/runs', { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey }, body: JSON.stringify(body) });
      $('#prompt').value = '';
      if (collaboration) {
        $('#collabFiles').value = ''; $('#collabVersion').value = ''; $('#enableCollaboration').checked = false; $('#enableAdvancedCollaboration').checked = false;
        state.selectedRepresentatives.clear(); $('#presetArbiter').value = '';
        $('#collaborationSetup').open = false; setRepresentativeMode(); setAdvancedMode(); renderCollaborationSelectors(); renderCollabFileList();
      }
      if (discussionPayload) {
        $('#discussionFiles').value = ''; $('#discussionVersion').value = '';
        state.selectedDiscussion.clear(); renderTargetPicks();
        $('#discussionSource').open = false;
        renderDiscussionFileList();
      }
      state.currentRunId = accepted.run_id; state.events = []; state.eventCursor = 0;
      await selectRun(accepted.run_id, { silent: true });
      toast(state.mode === 'discussion' ? `共同研讨已开始 · 主 Agent：${agentName(state.hub.orchestrator_agent_id)}`
        : state.mode === 'orchestrated' ? `主 Agent 计划已开始 · ${agentName(state.hub.orchestrator_agent_id)}`
          : `本轮任务已受理 · ${accepted.accepted_tasks?.length || 0} 个 Agent`);
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
    const maxTasksValue = Number($('#maxTasks').value);
    if (!Number.isInteger(maxTasksValue) || maxTasksValue < 1 || maxTasksValue > 100) {
      showError(new Error('每轮任务数上限需为 1 至 100。')); $('#maxTasks').focus(); return;
    }
    if (deadlineSeconds != null && (!Number.isInteger(deadlineSeconds) || deadlineSeconds < 1 || deadlineSeconds > 86400)) {
      showError(new Error('默认截止时间需为 1 至 86400 秒。')); $('#defaultDeadline').focus(); return;
    }
    $('#saveConfig').disabled = true;
    try {
      const result = await api('/api/config/hub', { method: 'PUT', body: JSON.stringify({
        orchestrator_agent_id: agentId, orchestrator_enabled: $('#orchestratorEnabled').checked,
        summary_agent_id: $('#summaryAgentSelect').value || null, summary_policy: $('#summaryPolicy').value,
        mode_default: $('#defaultMode').value, deadline_seconds: deadlineSeconds, max_tasks: maxTasksValue
      }) });
      state.dirtyConfig = false; renderConfig(result.config); formatDeadlineInput(result.config.deadline_seconds);
      toast('主 Agent 设置已保存；已创建任务的模式与 Agent 快照不会改变'); clearError();
    } catch (error) { state.dirtyConfig = true; showError(error); }
    finally { $('#saveConfig').disabled = false; }
  }
  async function requestSummary() {
    if (!state.currentRunId || ['unresolved', 'needs_human'].includes(state.currentRun?.review_state)) return;
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
  async function reviewDispute(runId, disputeId, button) {
    button.disabled = true; clearError();
    try {
      await api(`/api/runs/${encodeURIComponent(runId)}/disputes/${encodeURIComponent(disputeId)}/review`, { method: 'POST', body: '{}' });
      await refreshRun(); toast('已发起仲裁，结果会自动更新。');
    } catch (error) { showError(error); }
    finally { button.disabled = false; }
  }
  async function submitDispute(event) {
    event.preventDefault(); clearError();
    const run = state.currentRun;
    if (!run?.collaboration) return;
    const tasks = eligibleDisputeTasks(run);
    const left = tasks.find(task => task.task_id === $('#disputeLeft').value);
    const right = tasks.find(task => task.task_id === $('#disputeRight').value);
    const arbiter = $('#disputeArbiter').value;
    if (!left || !right || left.task_id === right.task_id || left.agent_id === right.agent_id) { showError(new Error('请选两位不同 Agent 的成功执行任务。')); return; }
    if (!eligibleArbiters(run, left, right).some(([id]) => id === arbiter)) { showError(new Error('请选择与争议双方不同的合格上级 Agent。')); return; }
    let evidence;
    try { evidence = [evidenceItem($('#disputeEvidence').value, $('#disputeSourcePath').value, $('#disputeLineStart').value, $('#disputeLineEnd').value, sourceFilesFor(run))]; }
    catch (error) { showError(error); return; }
    const claim = $('#disputeClaim').value.trim();
    if (!claim) { showError(new Error('请填写争议说明。')); return; }
    const button = $('#disputeForm [type="submit"]'); button.disabled = true;
    try {
      await api(`/api/runs/${encodeURIComponent(run.run_id)}/disputes`, { method: 'POST', body: JSON.stringify({
        left_task_id: left.task_id, right_task_id: right.task_id, arbiter_agent_id: arbiter, claim, evidence
      }) });
      $('#disputeForm').reset(); $('#disputeCreate').open = false;
      await refreshRun(); toast('争议已记录，可发起上级仲裁。');
    } catch (error) { showError(error); }
    finally { button.disabled = false; }
  }
  async function supplementDispute(runId, disputeId, form) {
    clearError();
    const files = sourceFilesFor(state.currentRun || {});
    let evidence;
    try { evidence = [evidenceItem(form.elements.note.value, form.elements.path?.value, form.elements.line_start?.value, form.elements.line_end?.value, files)]; }
    catch (error) { showError(error); return; }
    const button = form.querySelector('[type="submit"]'); button.disabled = true;
    try {
      await api(`/api/runs/${encodeURIComponent(runId)}/disputes/${encodeURIComponent(disputeId)}/evidence`, { method: 'POST', body: JSON.stringify({ evidence }) });
      await api(`/api/runs/${encodeURIComponent(runId)}/disputes/${encodeURIComponent(disputeId)}/review`, { method: 'POST', body: '{}' });
      await refreshRun(); toast('补充证据已提交，并已发起再审。');
    } catch (error) { showError(error); await refreshRun(); }
    finally { button.disabled = false; }
  }
  async function decideDispute(runId, disputeId, form) {
    clearError();
    const dispute = state.currentRun?.disputes?.find(item => item.dispute_id === disputeId);
    const selected_response_id = form.elements.selected_response_id.value;
    const rationale = form.elements.rationale.value.trim();
    if (!dispute || ![dispute.left_response_id, dispute.right_response_id].includes(selected_response_id) || !rationale) {
      showError(new Error('请选择结论并填写人工决定依据。')); return;
    }
    const button = form.querySelector('[type="submit"]'); button.disabled = true;
    try {
      await api(`/api/runs/${encodeURIComponent(runId)}/disputes/${encodeURIComponent(disputeId)}/human-decision`, { method: 'POST', body: JSON.stringify({ selected_response_id, rationale }) });
      await refreshRun(); toast('人工决定已记录。');
    } catch (error) { showError(error); }
    finally { button.disabled = false; }
  }
  async function onResponseClick(event) {
    const button = event.target.closest('[data-agent-id]');
    if (!button) return;
    const id = button.dataset.agentId;
    if (!state.agents.some(agent => agent.id === id)) return;
    if (state.mode === 'orchestrated') return;
    const discussion = state.mode === 'discussion';
    if (discussion && id === state.hub?.orchestrator_agent_id) return;
    const selectedSet = discussion ? state.selectedDiscussion : state.selectedTargets;
    if (selectedSet.has(id)) selectedSet.delete(id);
    else if (discussion && selectedSet.size >= 3) { toast('共同研讨最多选择 3 位分析者。'); return; }
    else selectedSet.add(id);
    const selected = selectedSet.has(id);
    button.setAttribute('aria-pressed', String(selected)); button.classList.toggle('selected', selected);
    const mark = $('.target-check', button); if (mark) mark.textContent = selected ? '✓' : '+';
    updateDispatchState();
  }
  function bindRunTabs() {
    $$('.run-tab').forEach(tab => tab.addEventListener('click', () => setRunTab(tab.dataset.runTab)));
    $('.run-tabs').addEventListener('keydown', event => {
      const tab = event.target.closest('.run-tab');
      if (!tab || !$('.run-tabs').contains(tab)) return;
      const index = RUN_TABS.indexOf(tab.dataset.runTab);
      let next = index;
      if (event.key === 'ArrowRight') next = (index + 1) % RUN_TABS.length;
      else if (event.key === 'ArrowLeft') next = (index - 1 + RUN_TABS.length) % RUN_TABS.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = RUN_TABS.length - 1;
      else if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      setRunTab(RUN_TABS[next], { focus: true });
    });
  }
  function bindCollaborationEvents() {
    $('#enableAdvancedCollaboration').addEventListener('change', setAdvancedMode);
    $('#enableCollaboration').addEventListener('change', setRepresentativeMode);
    $('#collabFiles').addEventListener('change', renderCollabFileList);
    $('#discussionFiles').addEventListener('change', renderDiscussionFileList);
    $('#discussionMaxCalls').addEventListener('input', updateDispatchState);
    $('#representativePicks').addEventListener('click', event => {
      const button = event.target.closest('[data-agent-id]');
      if (!button) return;
      const id = button.dataset.agentId;
      if (state.selectedRepresentatives.has(id)) state.selectedRepresentatives.delete(id);
      else if (state.selectedRepresentatives.size < 3) state.selectedRepresentatives.add(id);
      else { toast('最多选择 3 位代码代表。'); return; }
      renderCollaborationSelectors(); updateDispatchState();
    });
    $('#disputeLeft').addEventListener('change', refreshDisputeArbiters);
    $('#disputeRight').addEventListener('change', refreshDisputeArbiters);
    $('#disputeForm').addEventListener('submit', submitDispute);
  }
  function bindEvents() {
    bindRunTabs(); bindCollaborationEvents();
    $('#agentList').addEventListener('click', onResponseClick);
    $('#targetPicks').addEventListener('click', onResponseClick);
    $('#prompt').addEventListener('input', updateDispatchState);
    $('#startRun').addEventListener('click', createRun);
    $('#saveConfig').addEventListener('click', saveConfig);
    ['#orchestratorSelect', '#summaryAgentSelect', '#orchestratorEnabled', '#summaryPolicy', '#defaultMode'].forEach(selector => {
      $(selector).addEventListener('change', () => { state.dirtyConfig = true; $('#configMessage').textContent = '有未保存的 Hub 设置。'; });
    });
    $('#defaultDeadline').addEventListener('input', () => { state.dirtyConfig = true; $('#configMessage').textContent = '有未保存的 Hub 设置。'; });
    $('#maxTasks').addEventListener('input', () => { state.dirtyConfig = true; $('#configMessage').textContent = '有未保存的 Hub 设置。'; });
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
