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
  const mobileStart = mobileStarts.at(-1) ?? -1;
  assert.notEqual(mobileStart, -1);
  const mobile = css.slice(mobileStart);
  assert.match(mobile, /\.shell\s*\{\s*display:\s*block[^}]*overflow:\s*visible/);
  assert.match(mobile, /\.sidebar\s*\{\s*display:\s*flex[^}]*width:\s*100%/);
  assert.match(mobile, /\.main\s*\{\s*display:\s*block[^}]*width:\s*100%/);
  assert.match(mobile, /\.content\s*\{\s*width:\s*calc\(100% - 24px\)/);
  assert.equal(375 - 24, 351);
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
