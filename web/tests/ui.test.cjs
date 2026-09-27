const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const status = require('../status.js');
const deadline = require('../deadline.js');

const web = path.resolve(__dirname, '..');

test('fragment bootstrap removes URL token and keeps it out of page markup', () => {
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const token = 'a'.repeat(64);
  assert.doesNotMatch(html, /__BUS_TOKEN__|agent-bus-token/);
  const prefix = app.slice(0, app.indexOf('  const AGENT_COLORS'));
  const storage = new Map();
  const elements = new Map();
  const context = {
    window: { AgentHubTaskStatus: {}, AgentHubDeadline: {} },
    document: { querySelector: selector => {
      if (!elements.has(selector)) elements.set(selector, { value: '', textContent: '', style: {}, focus() {} });
      return elements.get(selector);
    } },
    location: { hash: `#token=${token}`, pathname: '/', search: '' },
    history: { replaceState(_state, _title, url) { this.cleanedUrl = url; } },
    sessionStorage: { setItem(key, value) { storage.set(key, value); }, getItem(key) { return storage.get(key); }, removeItem(key) { storage.delete(key); } },
    clearInterval() {}, URLSearchParams
  };
  vm.runInNewContext(`${prefix} const state={timer:null}; globalThis.bootstrapOk=bootstrapToken(); globalThis.apiToken=HEADERS['X-Agent-Bus-Token']; })();`, context);
  assert.equal(context.bootstrapOk, true);
  assert.equal(context.history.cleanedUrl, '/');
  assert.equal(context.apiToken, token);
  assert.equal(storage.get('agentHubToken'), token);
});

test('provider payload sends output limit field only for OpenAI-compatible protocol', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const start = app.indexOf('  function serializeProvider(provider)');
  const end = app.indexOf('  async function saveRegistry', start);
  assert.ok(start >= 0 && end > start);
  const context = {};
  vm.runInNewContext(`${app.slice(start, end)} globalThis.serializeProvider = serializeProvider;`, context);
  const common = { id: 'p', base_url: 'https://example.invalid', output_limit_field: 'max_completion_tokens' };
  const openai = context.serializeProvider({ ...common, protocol: 'openai-chat-completions' });
  const anthropic = context.serializeProvider({ ...common, protocol: 'anthropic-messages' });
  const ollama = context.serializeProvider({ ...common, protocol: 'ollama-chat' });
  assert.equal(openai.output_limit_field, 'max_completion_tokens');
  assert.equal(Object.hasOwn(anthropic, 'output_limit_field'), false);
  assert.equal(Object.hasOwn(ollama, 'output_limit_field'), false);
});

test('registry normalization and save payload retain agent and template output limits', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
  const start = app.indexOf('  function normalizedRegistry(value = {})');
  const end = app.indexOf('  function updateAgentCatalog', start);
  assert.ok(start >= 0 && end > start);
  const context = {};
  vm.runInNewContext(`${app.slice(start, end)} globalThis.normalizedRegistry = normalizedRegistry;`, context);
  const normalized = context.normalizedRegistry({ provider_agents: [{ id: 'a', max_tokens: 8192 }], templates: [{ id: 't', max_tokens: 2048 }] });
  assert.equal(normalized.provider_agents[0].max_tokens, 8192);
  assert.equal(normalized.templates[0].max_tokens, 2048);
  assert.match(app, /max_tokens: item\.max_tokens \?\? 4096/g);
  assert.match(html, /id="agentEditor"[\s\S]*name="max_tokens"/);
  assert.match(html, /id="templateEditor"[\s\S]*name="max_tokens"/);
});

test('default model round trips through registry save without exposing secrets', async () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
  const normalizeStart = app.indexOf('  function normalizedRegistry(value = {})');
  const normalizeEnd = app.indexOf('  function updateAgentCatalog', normalizeStart);
  const saveStart = app.indexOf('  function serializeProvider(provider)');
  const saveEnd = app.indexOf('  function setSettingsTab', saveStart);
  const requests = [];
  const context = {
    registryPath: '/api/config/agents',
    state: { registry: {}, agents: [] },
    api: async (_path, options) => {
      if (options) { requests.push(JSON.parse(options.body)); return {}; }
      return { providers: [{ id: 'custom', protocol: 'openai-chat-completions', base_url: 'https://example.invalid/prefix/v1', default_model: 'team/model:2026', has_api_key: true }] };
    },
    renderRegistry() {}, updateAgentCatalog() {}, refreshState: async () => {}
  };
  vm.runInNewContext(`${app.slice(normalizeStart, normalizeEnd)} ${app.slice(saveStart, saveEnd)} globalThis.saveRegistry = saveRegistry;`, context);
  await context.saveRegistry({ providers: [{ id: 'custom', protocol: 'openai-chat-completions', base_url: 'https://example.invalid/prefix/v1', default_model: 'team/model:2026', has_api_key: true }] });
  assert.equal(requests[0].providers[0].default_model, 'team/model:2026');
  assert.equal(Object.hasOwn(requests[0].providers[0], 'api_key'), false);
  assert.equal(context.state.registry.providers[0].default_model, 'team/model:2026');
  assert.match(html, /id="providerEditor"[\s\S]*name="default_model" maxlength="500"/);
});

test('provider model prefill follows new forms but preserves explicit and existing models', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const start = app.indexOf('  function prefillModelFromProvider(form)');
  const end = app.indexOf('  function showEditor', start);
  const context = { state: { registry: { providers: [
    { id: 'one', default_model: 'team/one:v1' }, { id: 'two', default_model: 'team/two:v2' }
  ] } } };
  vm.runInNewContext(`${app.slice(start, end)} globalThis.prefill = prefillModelFromProvider;`, context);
  const form = { dataset: { modelManual: 'false' }, elements: { provider_id: { value: 'one' }, model: { value: '' } } };
  context.prefill(form);
  assert.equal(form.elements.model.value, 'team/one:v1');
  form.elements.provider_id.value = 'two';
  context.prefill(form);
  assert.equal(form.elements.model.value, 'team/two:v2');
  form.elements.model.value = 'other/custom:latest';
  form.dataset.modelManual = 'true';
  form.elements.provider_id.value = 'one';
  context.prefill(form);
  assert.equal(form.elements.model.value, 'other/custom:latest');
  form.elements.model.value = 'existing/model';
  context.prefill(form);
  assert.equal(form.elements.model.value, 'existing/model');
});

test('service restart interruption is terminal and clearly labeled', () => {
  assert.equal(status.isTerminal('interrupted'), true);
  assert.equal(status.label({ status: 'interrupted' }), '服务重启中断');
  assert.equal(status.isTerminal('running'), false);
  assert.equal(status.label({ status: 'running', overdue: true }), '已逾期 · 执行仍在继续');
  assert.equal(status.isTerminal('succeeded'), true);
});

test('deadline defaults retain seconds and round upward instead of truncating', () => {
  const now = 1_000_250;
  const value = deadline.formatLocalInput(now, 30);
  assert.match(value, /:\d{2}$/);
  const parsed = new Date(value).getTime();
  assert.ok(parsed >= now + 30_000);
  assert.ok(parsed - now < 31_000);
  assert.equal(deadline.relativeSeconds(value, now), Math.ceil((parsed - now) / 1000));
  assert.equal(deadline.formatLocalInput(now, 0), '');
  assert.equal(deadline.relativeSeconds('invalid', now), null);
});

test('375px layout stacks controls and leaves 351px for page content', () => {
  const css = fs.readFileSync(path.join(web, 'style.css'), 'utf8');
  const mobileStarts = [...css.matchAll(/@media\s*\(\s*max-width\s*:\s*640px\s*\)/g)].map(match => match.index);
  const mobile = mobileStarts.map((start, index) => css.slice(start, mobileStarts[index + 1] ?? css.length))
    .find(block => /\.shell\s*\{\s*display:\s*block[^}]*overflow:\s*visible/.test(block));
  assert.ok(mobile);
  assert.match(mobile, /\.shell\s*\{\s*display:\s*block[^}]*overflow:\s*visible/);
  assert.match(mobile, /\.sidebar\s*\{\s*display:\s*flex[^}]*width:\s*100%/);
  assert.match(mobile, /\.main\s*\{\s*display:\s*block[^}]*width:\s*100%/);
  assert.match(mobile, /\.content\s*\{\s*width:\s*calc\(100% - 24px\)/);
  assert.equal(375 - 24, 351);
});

test('run detail tabs expose distinct panels and preserve a chosen tab until run changes', () => {
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  for (const [tab, panel] of [['Collaboration', 'Collaboration'], ['Outputs', 'Outputs'], ['Results', 'Results']]) {
    assert.match(html, new RegExp(`id="runTab${tab}"[^>]*role="tab"[^>]*aria-controls="runPanel${panel}"`));
    assert.match(html, new RegExp(`id="runPanel${panel}"[^>]*role="tabpanel"[^>]*aria-labelledby="runTab${tab}"[^>]*hidden`));
  }
  const start = app.indexOf("  const RUN_TABS = ['collaboration', 'outputs', 'results'];");
  const end = app.indexOf('  function renderRun(run)', start);
  assert.ok(start >= 0 && end > start);
  const tabs = ['collaboration', 'outputs', 'results'].map(name => ({
    dataset: { runTab: name }, classList: { toggle() {}, remove() {} }, setAttribute(key, value) { this[key] = value; }, focus() { this.focused = true; }
  }));
  const panels = ['Collaboration', 'Outputs', 'Results'].map(name => ({ id: `runPanel${name}`, hidden: true }));
  const state = { runTab: null };
  const context = {
    state, $$: selector => selector === '.run-tab' ? tabs : panels,
    $: selector => tabs.find(tab => `#runTab${tab.dataset.runTab[0].toUpperCase()}${tab.dataset.runTab.slice(1)}` === selector)
  };
  vm.runInNewContext(`${app.slice(start, end)} globalThis.setRunTab = setRunTab; globalThis.defaultRunTab = defaultRunTab;`, context);
  assert.equal(context.defaultRunTab({ mode: 'orchestrated', status: 'running' }), 'collaboration');
  assert.equal(context.defaultRunTab({ mode: 'direct', status: 'running' }), 'outputs');
  assert.equal(context.defaultRunTab({ mode: 'direct', status: 'completed', summary: { status: 'ready', content: 'done' } }), 'results');
  assert.equal(context.defaultRunTab({ mode: 'direct', status: 'completed', summary: { status: 'unavailable' } }), 'outputs');
  context.setRunTab('outputs', { focus: true });
  assert.equal(state.runTab, 'outputs');
  assert.equal(tabs[1]['aria-selected'], 'true');
  assert.equal(tabs[1].focused, true);
  assert.deepEqual(panels.map(panel => panel.hidden), [true, false, true]);
  assert.match(app, /if \(state\.runTabRunId !== run\.run_id\) \{[\s\S]*?setRunTab\(defaultRunTab\(run\)\)/);
  assert.match(app, /const planningTasks = tasks\.filter\(task => task\.role === 'orchestrator'\)/);
  assert.ok(app.includes("const outputTasks = tasks.filter(task => !['orchestrator', 'representative', 'arbiter'].includes(task.role))"));
});

test('run rendering and keyboard navigation keep approval and replies in their intended panels', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const runStart = app.indexOf('  function shortId(id)');
  const runEnd = app.indexOf('  function renderPlan(run)', runStart);
  const planEnd = app.indexOf('  function formatDuration(value)', runEnd);
  const keysStart = app.indexOf('  function bindRunTabs()');
  const keysEnd = app.indexOf('  function bindEvents()', keysStart);
  assert.ok(runStart >= 0 && runEnd > runStart && planEnd > runEnd && keysStart > planEnd && keysEnd > keysStart);
  const elements = new Map();
  const makeNode = selector => ({
    selector, textContent: '', style: {}, hidden: false, disabled: false, listeners: {},
    classList: { values: new Set(), toggle(name, active) { if (active) this.values.add(name); else this.values.delete(name); }, remove(name) { this.values.delete(name); }, contains(name) { return this.values.has(name); } },
    setAttribute(name, value) { this[name] = value; },
    addEventListener(name, handler) { this.listeners[name] = handler; },
    replaceChildren() {}, append() {}, focus() { this.focused = true; },
    contains(node) { return tabs.includes(node); }
  });
  const $ = selector => { if (!elements.has(selector)) elements.set(selector, makeNode(selector)); return elements.get(selector); };
  const tabs = ['collaboration', 'outputs', 'results'].map(name => Object.assign(makeNode(`tab-${name}`), {
    dataset: { runTab: name }, closest(selector) { return selector === '.run-tab' ? this : null; }
  }));
  const panels = ['Collaboration', 'Outputs', 'Results'].map(name => Object.assign(makeNode(`panel-${name}`), { id: `runPanel${name}` }));
  const state = { agents: [], runTab: null, runTabRunId: null };
  const rendered = [];
  const context = {
    state, $, $$: selector => selector === '.run-tab' ? tabs : panels,
    TaskStatus: { isTerminal: status => ['succeeded', 'failed'].includes(status) },
    agentName: id => id, dateLabel: () => 'now', formatDuration: () => '1 分',
    renderTasks: (tasks, list, _run, options) => rendered.push({ list: list.selector, roles: tasks.map(task => task.role), planning: options?.planning || false }),
    renderRunSummary() {}, addToHistory() {}, addText: (parent, _tag, _className, value) => { parent.textContent += value; },
    document: { createElement: () => makeNode('created') }
  };
  vm.runInNewContext(`${app.slice(runStart, runEnd)} ${app.slice(runEnd, planEnd)} ${app.slice(keysStart, keysEnd)} globalThis.renderRun = renderRun; globalThis.bindRunTabs = bindRunTabs; globalThis.setRunTab = setRunTab; globalThis.renderPlan = renderPlan;`, context);
  const tasks = [
    { task_id: 'plan', agent_id: 'lead', role: 'orchestrator', status: 'succeeded' },
    { task_id: 'worker', agent_id: 'lead', role: 'worker', status: 'succeeded' },
    { task_id: 'reply', agent_id: 'lead', role: 'orchestrator_reply', status: 'succeeded' }
  ];
  const run = { run_id: 'a'.repeat(32), mode: 'orchestrated', status: 'awaiting_approval', orchestrator_agent_id: 'lead',
    dispatch_policy: 'preview', created_at: 'now', tasks, plan: { status: 'awaiting_approval', summary: 'plan', tasks: [] },
    summary: { status: 'idle' } };
  context.bindRunTabs();
  context.renderRun(run);
  assert.equal(state.runTab, 'collaboration');
  assert.equal($('#runCountLabel').textContent, '3 / 3 已结束 · 2 条成功回复');
  assert.equal($('#collaborationCount').textContent, '待批准');
  assert.equal($('#approvePlan').hidden, false);
  assert.equal($('#approvePlan').classList.values.has('hidden'), false);
  assert.equal($('#approvePlan').disabled, false);
  assert.deepEqual(rendered.at(-2).roles, ['worker', 'orchestrator_reply']);
  assert.equal(rendered.at(-2).list, '#runTasks');
  assert.deepEqual(rendered.at(-1).roles, ['orchestrator']);
  assert.equal(rendered.at(-1).list, '#planningTasks');
  const keydown = $('.run-tabs').listeners.keydown;
  const press = (tab, key) => keydown({ target: tab, key, preventDefault() { this.prevented = true; } });
  press(tabs[0], 'ArrowRight');
  assert.equal(state.runTab, 'outputs');
  assert.equal(panels[1].hidden, false);
  press(tabs[1], 'End');
  assert.equal(state.runTab, 'results');
  press(tabs[2], 'Home');
  assert.equal(state.runTab, 'collaboration');
  press(tabs[0], ' ');
  assert.equal(state.runTab, 'collaboration');
  tabs[1].listeners.click();
  assert.equal(state.runTab, 'outputs');
  context.renderRun({ ...run, status: 'completed', summary: { status: 'ready', content: 'done' } });
  assert.equal(state.runTab, 'outputs', 'same-run polling must not steal the chosen tab');
  context.renderRun({ ...run, run_id: 'b'.repeat(32), status: 'completed', summary: { status: 'ready', content: 'done' } });
  assert.equal(state.runTab, 'results', 'a new completed run with a summary starts on results');
});

test('polling uses compact state and raw responses are fetched only on explicit expansion', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  assert.equal((app.match(/\/api\/state\?compact=1/g) || []).length, 2);
  assert.doesNotMatch(app, /\/api\/state['"`]/);
  assert.doesNotMatch(app, /loadResponsePreview|responseCache|responseLoading/);
  assert.match(app, /response\.addEventListener\('click', \(\) => showResponse\(task\)\)/);
  assert.match(app, /\/api\/responses\/\$\{encodeURIComponent\(task\.response_id\)\}\?run_id=/);
});

test('master mode submits the real plan policy and exposes approval for validated previews', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
  assert.match(app, /\{ mode: 'orchestrated', dispatch_policy: \$\('#dispatchPolicy'\)\.value, prompt \}/);
  assert.match(app, /\/api\/runs\/\$\{encodeURIComponent\(state\.currentRunId\)\}\/approve/);
  assert.match(app, /plan\.status === 'awaiting_approval'/);
  assert.match(html, /id="planPanel"/);
  assert.match(html, /id="approvePlan"/);
});

test('master mode and unavailable summaries explain the current setup accurately', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
  assert.match(html, /启用主 Agent<\/b><small>由主 Agent 规划并协调<\/small>/);
  assert.match(app, /主 Agent 未启用或不可用。请在设置中选择并启用已注册 Agent。/);
  assert.match(app, /本轮未配置可用的摘要 Agent。/);
  assert.doesNotMatch(app, /计划与审批派发尚未实现|摘要适配器尚未配置|模型摘要功能尚未接入/);
  assert.doesNotMatch(html, /规划与协调能力未就绪|当前服务未配置模型摘要适配器/);
});

test('summary configuration and message task prompts are shown as separate real fields', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
  assert.match(html, /id="summaryAgentSelect"/);
  assert.match(html, /id="summaryPolicy"/);
  assert.match(app, /summary_agent_id: \$\('#summaryAgentSelect'\)\.value \|\| null/);
  assert.match(app, /event\.kind === 'message' \? event\.body/);
  assert.match(app, /event\.task_prompt && event\.task_prompt !== event\.body/);
});

test('per-task model summary statuses are distinguished and ready summaries show provenance', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  assert.match(app, /const labels = \{ idle: '摘要待生成', pending: '摘要生成中', failed: '摘要生成失败', unavailable: '模型摘要不可用', stale: '摘要已过期' \}/);
  assert.match(app, /summary\?\.error\?\.message/);
  assert.match(app, /summary\.source_agent_id/);
  assert.match(app, /summary\.generated_at/);
  assert.match(app, /summaryStatus === 'ready' && summaryText/);
});

test('collaboration upload uses explicit UTF-8 files and stays off on older servers', async () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const start = app.indexOf('  function boundedInteger(selector');
  const end = app.indexOf('  function shortId(id)', start);
  assert.ok(start >= 0 && end > start);
  const values = new Map([
    ['#collaborationSetup', { open: true }], ['#enableCollaboration', { checked: true }],
    ['#enableAdvancedCollaboration', { checked: true }],
    ['#collabMaxConcurrent', { value: '3' }], ['#collabMaxContext', { value: '65536' }], ['#collabMaxCalls', { value: '24' }],
    ['#collabVersion', { value: 'snapshot-a' }], ['#presetArbiter', { value: 'judge' }],
    ['#collabFiles', { files: [{ name: 'src.js', size: 15, arrayBuffer: async () => Uint8Array.from(Buffer.from('const answer=1;')).buffer }] }]
  ]);
  const state = { mode: 'orchestrated', hub: { collaboration_supported: false, orchestrator_agent_id: 'lead' }, selectedRepresentatives: new Set(['a', 'b']) };
  const context = { state, $: selector => values.get(selector), TextDecoder };
  vm.runInNewContext(`${app.slice(start, end)} globalThis.buildCollaborationPayload = buildCollaborationPayload;`, context);
  assert.equal(await context.buildCollaborationPayload(), null);
  state.hub.collaboration_supported = true;
  const payload = await context.buildCollaborationPayload();
  assert.equal(payload.source.version, 'snapshot-a');
  assert.equal(payload.source.files[0].path, 'src.js');
  assert.equal(payload.source.files[0].text, 'const answer=1;');
  assert.deepEqual([...payload.representative_agent_ids], ['a', 'b']);
  assert.equal(payload.arbiter_agent_id, 'judge');
  assert.equal(payload.limits.max_calls, 24);
  values.get('#collaborationSetup').open = false;
  assert.ok(await context.buildCollaborationPayload(), 'closing details must not drop enabled collaboration');
  values.get('#collabFiles').files = [{ name: 'bad.js', size: 2, arrayBuffer: async () => Uint8Array.from([0xff, 0xfe]).buffer }];
  await assert.rejects(context.buildCollaborationPayload(), /encoded|valid|字符|encoding/i);
  values.get('#enableCollaboration').checked = false;
  values.get('#presetArbiter').value = 'lead';
  const arbitrationOnly = await context.buildCollaborationPayload();
  assert.equal(Object.hasOwn(arbitrationOnly, 'source'), false);
  assert.equal(arbitrationOnly.arbiter_agent_id, 'lead');
  values.get('#enableAdvancedCollaboration').checked = false;
  assert.equal(await context.buildCollaborationPayload(), null);
});

test('collaboration entry is gated by the server capability flag', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const start = app.indexOf('  function updateModePresentation()');
  const end = app.indexOf('  function updateDispatchState()', start);
  assert.ok(start >= 0 && end > start);
  const nodes = new Map();
  const $ = selector => {
    if (!nodes.has(selector)) nodes.set(selector, { classList: { hidden: false, toggle(name, on) { if (name === 'hidden') this.hidden = on; } }, textContent: '', innerHTML: '' });
    return nodes.get(selector);
  };
  const state = { mode: 'orchestrated', hub: { orchestrator_state: 'enabled', orchestrator_agent_id: 'lead' } };
  const context = { state, $, $$: () => [], agentName: id => id, updateDispatchState() {} };
  vm.runInNewContext(`${app.slice(start, end)} globalThis.updateModePresentation = updateModePresentation;`, context);
  context.updateModePresentation();
  assert.equal($('#collaborationSetup').classList.hidden, true);
  assert.equal($('#collabUnsupported').classList.hidden, false);
  state.hub.collaboration_supported = true;
  context.updateModePresentation();
  assert.equal($('#collaborationSetup').classList.hidden, false);
  assert.equal($('#collabUnsupported').classList.hidden, true);
});

test('representative picker keeps at most three distinct agents', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const start = app.indexOf('  function bindCollaborationEvents()');
  const end = app.indexOf('  function bindEvents()', start);
  assert.ok(start >= 0 && end > start);
  const nodes = new Map();
  const $ = selector => {
    if (!nodes.has(selector)) nodes.set(selector, { listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; } });
    return nodes.get(selector);
  };
  const state = { selectedRepresentatives: new Set() };
  let rendered = 0;
  const context = { state, $, setAdvancedMode() {}, setRepresentativeMode() {}, renderCollabFileList() {}, refreshDisputeArbiters() {}, submitDispute() {},
    renderCollaborationSelectors() { rendered++; }, updateDispatchState() {}, toast() {} };
  vm.runInNewContext(`${app.slice(start, end)} globalThis.bindCollaborationEvents = bindCollaborationEvents;`, context);
  context.bindCollaborationEvents();
  const choose = id => $('#representativePicks').listeners.click({ target: { closest: () => ({ dataset: { agentId: id } }) } });
  choose('a'); choose('b'); choose('c'); choose('d');
  assert.deepEqual([...state.selectedRepresentatives], ['a', 'b', 'c']);
  assert.equal(rendered, 3);
  choose('b');
  assert.deepEqual([...state.selectedRepresentatives], ['a', 'c']);
});

test('hub settings save includes the editable total task limit', async () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const start = app.indexOf('  async function saveConfig()');
  const end = app.indexOf('  async function requestSummary()', start);
  assert.ok(start >= 0 && end > start);
  const values = new Map([
    ['#orchestratorSelect', { value: 'lead' }], ['#defaultDeadline', { value: '', focus() {} }],
    ['#maxTasks', { value: '20', focus() {} }], ['#orchestratorEnabled', { checked: true }],
    ['#summaryAgentSelect', { value: '' }], ['#summaryPolicy', { value: 'manual' }],
    ['#defaultMode', { value: 'orchestrated' }], ['#saveConfig', { disabled: false }]
  ]);
  let sent;
  const context = { $: selector => values.get(selector), state: { dirtyConfig: false },
    clearError() {}, renderConfig() {}, formatDeadlineInput() {}, toast() {}, showError(error) { throw error; },
    api: async (_path, options) => { sent = JSON.parse(options.body); return { config: { deadline_seconds: null } }; } };
  vm.runInNewContext(`${app.slice(start, end)} globalThis.saveConfig = saveConfig;`, context);
  await context.saveConfig();
  assert.equal(sent.max_tasks, 20);
  assert.equal(sent.mode_default, 'orchestrated');
});

test('dispute choices require distinct completed workers and a valid superior', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const start = app.indexOf('  function sourceFilesFor(run)');
  const end = app.indexOf('  function refreshDisputeArbiters()', start);
  const evidenceStart = app.indexOf('  function evidenceItem(');
  const evidenceEnd = app.indexOf('  function addEvidenceFields(', evidenceStart);
  assert.ok(start >= 0 && end > start && evidenceStart > end && evidenceEnd > evidenceStart);
  const context = { agentName: id => id, document: {}, state: {} };
  vm.runInNewContext(`${app.slice(start, end)} ${app.slice(evidenceStart, evidenceEnd)} globalThis.tasks = eligibleDisputeTasks; globalThis.arbiters = eligibleArbiters; globalThis.evidence = evidenceItem;`, context);
  const a = { task_id: 'a1', agent_id: 'a', role: 'worker', status: 'succeeded', response_id: 'ra', group_id: 'g' };
  const b = { task_id: 'b1', agent_id: 'b', role: 'worker', status: 'succeeded', response_id: 'rb', group_id: 'g' };
  const pending = { task_id: 'c1', agent_id: 'c', role: 'worker', status: 'running' };
  const representative = { task_id: 'rep', agent_id: 'd', role: 'representative', status: 'succeeded', response_id: 'rr' };
  const run = { tasks: [a, b, pending, representative], orchestrator_agent_id: 'lead', collaboration: { arbiter_agent_id: 'judge' }, groups: [{ group_id: 'g', leader_agent_id: 'captain', member_agent_ids: ['a', 'b', 'captain'] }] };
  assert.deepEqual([...context.tasks(run)].map(task => task.task_id), ['a1', 'b1']);
  assert.deepEqual([...context.arbiters(run, a, b)].map(row => row[0]), ['judge', 'lead', 'captain']);
  assert.deepEqual([...context.arbiters(run, a, { ...b, agent_id: 'a' })], []);
  assert.deepEqual([...context.arbiters(run, a, { ...b, group_id: 'other' })].map(row => row[0]), ['judge', 'lead']);
  assert.equal(context.evidence('note', 'src.js', '2', '4', [{ path: 'src.js' }]).line_end, 4);
  assert.throws(() => context.evidence('note', 'other.js', '2', '4', [{ path: 'src.js' }]), /证据文件/);
});

test('preview groups remain visible before worker tasks are materialized', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const start = app.indexOf('  function renderGroups(run)');
  const end = app.indexOf('  function sourceFilesFor(run)', start);
  assert.ok(start >= 0 && end > start);
  const list = { children: [], replaceChildren() { this.children = []; }, append(child) { this.children.push(child); } };
  const panel = { hidden: null, classList: { toggle(_name, hidden) { panel.hidden = hidden; } } };
  const context = { $: selector => selector === '#groupPanel' ? panel : list,
    document: { createElement: () => ({ children: [], append(child) { this.children.push(child); } }) },
    addText: (parent, _tag, _className, value) => { const child = { textContent: value }; parent.append(child); return child; },
    agentName: id => id };
  vm.runInNewContext(`${app.slice(start, end)} globalThis.renderGroups = renderGroups;`, context);
  context.renderGroups({ groups: [], plan: { groups: [{ group_id: 'interface', leader_agent_id: 'a', member_agent_ids: ['a', 'b'] }] } });
  assert.equal(panel.hidden, false);
  assert.equal(list.children.length, 1);
  assert.equal(list.children[0].children[0].textContent, 'interface');
});

test('unchanged dispute polling keeps a partially written evidence form', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const start = app.indexOf('  function renderDisputes(run)');
  const end = app.indexOf('  function reviewStateLabel(value)', start);
  assert.ok(start >= 0 && end > start);
  const node = () => ({ children: [], listeners: {}, classList: { toggle() {} },
    append(child) { this.children.push(child); }, replaceChildren() { this.children = []; this.replacements = (this.replacements || 0) + 1; },
    addEventListener(name, callback) { this.listeners[name] = callback; } });
  const panel = node(), list = node(), state = { hub: { collaboration_supported: true }, disputeSignature: null };
  const context = {
    state, $: selector => selector === '#disputePanel' ? panel : list,
    document: { createElement: node }, renderDisputeOptions() {}, sourceFilesFor: () => [],
    disputeStatusLabel: status => status, shortId: id => id, agentName: id => id || '?',
    addText(parent, _tag, _className, value) { const child = node(); child.textContent = value; parent.append(child); return child; },
    addResponseAction() {}, addEvidenceFields() {}, selectedDisputeResponse() {}
  };
  vm.runInNewContext(`${app.slice(start, end)} globalThis.renderDisputes = renderDisputes;`, context);
  const run = { run_id: 'a'.repeat(32), collaboration: {}, tasks: [], disputes: [{ dispute_id: 'd1', status: 'needs_evidence', claim: 'different', rounds: [], evidence: [] }] };
  context.renderDisputes(run);
  assert.equal(list.replacements, 1);
  const card = list.children[0]; card.draft = 'partially typed evidence';
  context.renderDisputes(run);
  assert.equal(list.replacements, 1);
  assert.equal(list.children[0].draft, 'partially typed evidence');
  run.disputes = [{ ...run.disputes[0], status: 'open' }];
  context.renderDisputes(run);
  assert.equal(list.replacements, 2);
});

test('representative response events lead to the collaboration evidence panel', () => {
  const app = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
  const start = app.indexOf('  function renderEvents()');
  const end = app.indexOf('  function eventDescription(event)', start);
  assert.ok(start >= 0 && end > start);
  const makeNode = () => ({ children: [], listeners: {}, append(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
    addEventListener(name, callback) { this.listeners[name] = callback; }, focus() { this.focused = true; } });
  const list = makeNode(), representativePanel = makeNode();
  const state = { events: [{ event_id: 1, kind: 'response', task_id: 'rep', relation: 'system', state: 'succeeded' }], relation: 'all',
    currentRun: { tasks: [{ task_id: 'rep', role: 'representative', agent_id: 'arch' }] } };
  let selected;
  const context = { state, document: { createElement: makeNode }, $: selector => selector === '#eventList' ? list : representativePanel,
    $$: () => [], agentName: id => id, makeAvatar: makeNode, dateLabel: () => 'now', eventDescription: () => 'done', shortId: id => id,
    setRunTab: name => { selected = name; }, addText(parent, _tag, className, value) { const child = makeNode(); child.className = className; child.textContent = value; parent.append(child); return child; } };
  vm.runInNewContext(`${app.slice(start, end)} globalThis.renderEvents = renderEvents;`, context);
  context.renderEvents();
  const descend = node => [node, ...node.children.flatMap(descend)];
  const jump = descend(list).find(node => node.className === 'event-jump');
  assert.equal(jump.textContent, '查看代码代表');
  jump.listeners.click();
  assert.equal(selected, 'collaboration');
  assert.equal(representativePanel.focused, true);
});
