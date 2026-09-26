# Agent Hub

A local, single-user Agent Hub for direct and orchestrated runs across configured providers and command-line agents. The application uses Python's standard library. Running a command-line agent requires its CLI to be installed and configured by you.

## Quick start (Windows)

The bundled `win_dpapi.py` implementation uses Windows DPAPI for the current user. The source targets Python 3.8 or newer; the optional desktop viewer also requires Tkinter. Runtime verification has been performed on Windows with Python 3.10. Python 3.8/3.9 and non-Windows environments have not yet been runtime-tested.

```powershell
$env:AGENT_COMM_HOME = Join-Path $env:APPDATA 'AgentCommBus'
New-Item -ItemType Directory -Force $env:AGENT_COMM_HOME | Out-Null
python .\server.py
```

Open the local page, then use **Settings** to add a provider, agent, and (optionally) approved child-agent templates. Start with **Direct** mode for parallel workers. Configure an orchestrator and choose **Orchestrated** mode to delegate a plan. A provider can use any model identifier accepted by its selected protocol endpoint. No provider key is needed for local loopback services. On Windows, raw provider credentials use DPAPI storage; on other platforms, use an `${ENV:VARIABLE_NAME}` reference and set that variable in the server process environment.

`AGENT_COMM_HOME` is read by `bus.py`, `auth.py`, and the viewer. Its default is `%APPDATA%\AgentCommBus` on Windows, `$XDG_DATA_HOME/AgentCommBus` where `XDG_DATA_HOME` is set, or `~/.local/share/AgentCommBus` otherwise. Runtime files are kept outside the source tree.

## Optional CLI agent setup

Copy `agents.example.json` to `AGENT_COMM_HOME\agents.json`, or register an agent with a JSON argv array:

```powershell
Copy-Item .\agents.example.json (Join-Path $env:AGENT_COMM_HOME 'agents.json')
python .\bus.py register echo '["python","-c","print(2)"]'
python .\bus.py list
python .\bus.py run echo "hello"
```

The equivalent configuration entry is `{"echo":{"command":["python","-c","print(2)"],"desc":"Example CLI","timeout":600}}`. `command` must be a non-empty list of strings. `{prompt}` is replaced within an individual argument; without that placeholder, the task is sent as UTF-8 on stdin. `timeout` is an optional positive number of seconds (default 600). `env` is an optional mapping applied to the child process. On Windows, raw credential fields in `env` are DPAPI-protected in saved JSON and decrypted only in memory. On all platforms, `${ENV:VARIABLE_NAME}` references are supported for credential fields; raw secret values are rejected on platforms without DPAPI. Shell execution is disabled.

## Commands

```text
python bus.py list
python bus.py run echo "summarize this"
python bus.py send user echo "queued message"
python bus.py broadcast user "queued message"
python bus.py relay echo reviewer "review this"
python bus.py chat
python bus.py view
python bus.py gate <case-directory-or-blackboard.json>
```

CLI `send` and `broadcast` only append messages to inbox queues; they do not start recipients. CLI broadcast excludes the sender. WebUI chat/broadcast starts the selected agent or all configured agents, including the sender if it is configured; its API `started` field means background execution has begun, not that a durable queue accepted the task. A task message is free text: the tool does not create or validate a structured case ID, owner, absolute case path, output path, or acceptance criteria. Include those fields in your message when your workflow requires them. `run` and `relay` return nonzero when execution fails. Successful execution does not mean an external completion gate passed.

## Start and stop the Windows WebUI

From the candidate directory, in the same PowerShell session where `AGENT_COMM_HOME` is set, run:

```powershell
python .\server.py
```

The server opens the browser with a local URL whose `#token=` fragment bootstraps API access. Treat the terminal output and that link as credentials; do not forward them. The browser removes the fragment immediately and keeps the token in session storage for that browser tab. If you open the plain URL, enter the token from `python bus.py token` on Windows. The page and static assets never contain the API token. Keep this terminal open while using the WebUI; press **Ctrl+C** to stop it. On Windows, the token is stored under `AGENT_COMM_HOME` and protected at rest with the bundled Windows DPAPI implementation for the current user. On other platforms, the server uses a process-session token; use the URL printed by the running server because a separate `bus.py token` process has a different token. Runtime behavior outside Windows has not yet been verified.

## Agent Hub

The browser UI is the Agent Hub. Use its provider settings to configure agents, provider templates, and approved child-agent templates. Use **Direct** mode to select workers for a parallel run or **Orchestrated** mode to use the configured orchestrator. The mode and worker selection on the run form apply to the current run.

### 接入自定义模型服务

在 **设置 → 模型服务 → 添加模型服务** 中填写服务标识、请求格式、API 地址和可选的默认模型。这里的“请求格式”并不限制服务品牌：只有目标服务实际提供对应的 Chat Completions、Anthropic Messages 或 Ollama Chat 兼容接口时，才能选择相应格式。按服务端文档填写精确的模型 ID；默认模型只为新建 Agent 和子 Agent 模板预填，编辑时仍可分别改成其他 ID，不会覆盖已有 Agent 的模型。

保存提供商后，在 **Agent 与子 Agent** 中新增 Agent 或子 Agent 模板，选择该提供商并确认模型 ID。需要协作时，在 **协作设置** 中选主 Agent；子 Agent 模板须启用并批准。编辑提供商时，已存密钥不会回显；留空表示保留。更换服务地址时请核对密钥状态，并按需输入新服务的密钥。

例如，下列地址和模型 ID 来自服务商文档，仅供填写格式参考，未调用外部服务验证。DeepSeek：`openai-chat-completions`、`https://api.deepseek.com/chat/completions`、`deepseek-flash`（[官方调用示例](https://api-docs.deepseek.com/guides/harness)）；阿里云百炼北京地域：`openai-chat-completions`、`https://dashscope.aliyuncs.com/compatible-mode/v1`、`qwen-plus`（[官方接口地址](https://help.aliyun.com/en/model-studio/base-url)、[模型调用示例](https://help.aliyun.com/en/model-studio/model-calling-in-sub-workspace)）。密钥须与服务和地域匹配。Gemini 原生接口、OpenAI Responses 等格式目前没有对应适配器，不能仅靠填写 URL 接入。

With an orchestrator selected, **auto** dispatches a validated plan immediately. **preview** displays the plan, questions, proposed agent templates, and tasks, then waits for approval. The orchestrator may create run-scoped children only from templates that a user has approved; its output cannot introduce provider URLs, models, credentials, commands, or environment settings. Limits on tasks, child agents, message count, and depth bound fan-out; delegation depth is configurable from 1 to 4. `max_messages_per_task` is a cumulative task budget, while each protocol result block permits at most 8 requested messages. Direct runs immediately send the original task to every selected worker. For example, selecting `lead` and `peer` starts both; if `lead` then sends `peer` an `execute=true` request, that creates a third, derived task. Direct Agent-to-Agent messages can reach only workers selected for that run. To have only a lead plan and delegate the initial task, use Orchestrated mode; it may coordinate with configured registry agents and approved run-scoped children. Summary agents are configured independently and do not join a direct run as workers merely by being selected for summaries.

Providers use explicit protocol adapters: `openai-chat-completions` (OpenAI-compatible Chat Completions), `anthropic-messages` (Anthropic Messages), or `ollama-chat` (Ollama chat). These adapters use distinct request and response formats; they do not claim compatibility with arbitrary APIs. `base_url` may be an origin, a version/prefix path, or that adapter's exact endpoint (`/v1/chat/completions`, `/v1/messages`, or `/api/chat`); endpoint suffixes are added where needed. Each agent or template uses an exact, user-configured model identifier. Output limit `max_tokens` defaults to 4096 and accepts 1 through 100000; it maps to Chat Completions `max_tokens` by default, Anthropic `max_tokens`, or Ollama `options.num_predict`. For OpenAI-compatible providers, select `output_limit_field=max_completion_tokens` when the target service requires it; there is no model-name guess or automatic retry. The OpenAI-compatible adapter uses Bearer API-key auth; Anthropic uses `x-api-key` and its version header; remote Ollama uses Bearer auth, while local loopback Ollama does not send a key. HTTP is allowed only for a loopback URL when explicitly enabled, for local development/model servers. URLs must not contain credentials, query parameters, or fragments; redirects are not followed. API keys are protected at rest on Windows, and the UI only reports whether a key is configured.

CLI agents can return a final answer and requested messages/tasks through one unfenced `AGENT_HUB_RESULT` line followed by one JSON object. It may appear at the start of stdout or after ordinary prose and a blank line. Nothing may follow the JSON block. `final_answer` is the user-facing answer; preceding prose is not dispatched as a message. Multiple blocks or malformed JSON produce a protocol error. Inline mentions and fenced examples are ordinary text. The Hub delivers accepted messages after the producing request finishes; they are not live streaming messages. The event view can filter orchestrator-to-worker and worker-to-worker relationships, as well as show all events. Each task can display a concise summary, and the original full response can be opened on demand.

The run deadline is used to mark a run or task as late; it does not terminate an executing CLI process. Execution can finish after the deadline and its late response is retained and displayed with its final state.

Agent Hub run state, events, summaries, and full responses are stored under `AGENT_COMM_HOME` in the Hub runtime data. This data is separate from the source tree and is not browser session storage. The Hub's clear-log action clears only `log/chat.log`; it does not delete run history, inboxes, or saved full responses. Remove the runtime directory yourself when you intend to discard all local Hub data.

## Secure storage and platform support

On Windows, raw agent secret values and the WebUI token use this repository's standalone `win_dpapi.py` adapter; it does not depend on the local workspace's private `shared/secrets.py` package. DPAPI output is stored as `dpapi:<standard-base64-ciphertext>` using the current user's DPAPI scope; the adapter does not set `CRYPTPROTECT_LOCAL_MACHINE`. DPAPI usually binds data to the same Windows user and computer, with roaming profile exceptions. Windows ACL tightening on the token file is an additional measure.

Off Windows, the WebUI uses an in-memory token scoped to the server process; it is not saved to disk. Raw secret values cannot be persisted by CLI or provider configuration, but `${ENV:VARIABLE_NAME}` references are stored as references and resolved from the server or CLI process environment at runtime. Those platform paths are documented from code inspection and have not yet been runtime-tested.

The built-in field-name heuristic protects string values under names containing separated words such as `key`, `token`, `secret`, `password`, `passwd`, or `credential`, and common compact names such as `APIKey`, `accessToken`, `clientSecret`, `privateKey`, and `authToken`. Non-string values under recognized secret fields are rejected; place secrets directly in a named string field or use an `${ENV:NAME}` reference. The `max_tokens` field accepts an integer from 1 to 100000 as an ordinary output-limit setting. A custom field name with no secret marker is outside automatic detection; place secrets under a clearly named field such as `env.API_KEY`.

## WebUI and data

The WebUI binds only to `127.0.0.1`, requires `X-Agent-Bus-Token` on API requests, rejects disallowed Origin and cross-site fetches and OPTIONS, limits JSON POST bodies to 1 MiB, and requires boolean `confirm: true` to clear the main log. Windows stores the token with DPAPI; other platforms keep it only in server-process memory. Loopback binding and browser token handling do not protect against malicious processes already running as the same user. Do not expose the service through a proxy or public network. `clear` removes `log/chat.log`; inbox files and full responses remain.

Data is stored under `AGENT_COMM_HOME`: `agents.json`, `log/chat.log`, `log/responses/`, and `inbox/`. Treat this directory as private. Do not check it into source control.

## Optional completion gate

`AGENT_COMM_GATE_MODULE` may name a trusted importable module exporting `full_health_check(path=...)`. The argument is the absolute path to `blackboard.json`; when the CLI argument is a directory, `bus.py` appends that filename. The function returns a mapping whose values expose boolean `passed` and iterable `failures` and `warnings` attributes. Any failed check, missing provider, malformed return, or provider exception returns nonzero. A zero exit status means every returned check passed; it does not replace the provider's own policy review.

This project is licensed under the MIT License; see [`LICENSE`](LICENSE) for the full terms. The license applies to this candidate project’s original material. Third-party material remains subject to its own rights and license terms; this attribution does not claim ownership of that material.

## Attribution

Copyright (c) 2026 ming. This is the attribution for this candidate as identified by its maintainer. It does not claim ownership of third-party material or settle any third-party rights or license obligations.
