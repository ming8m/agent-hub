# Agent Hub 前端资产

本目录包含同源静态页面。它不加载第三方素材、字体或脚本；任务、Agent、状态、事件、摘要及回复均来自受令牌保护的候选 API，没有内嵌演示数据。

## 页面行为

- 一轮运行显式选择“无主 Agent”或“启用主 Agent”。无主模式支持选择一个或多个注册 Agent，点击开始即通过 `POST /api/runs` 并行派发。主模式使用设置面板中配置并启用的 Agent；主 Agent 从注册表中选择本轮目标。
- 无主模式只向当前所选 Agent 派发任务，不把任务内容转发给未选择的 Agent。主模式可联系已注册 Agent；主 Agent 创建的子 Agent 只能来自用户批准的模板，且仅在该轮运行中可见。
- 主 Agent 派发策略有 `auto` 和 `preview` 两种。`auto` 在计划通过校验后立即派发；`preview` 显示计划摘要、问题、任务、依赖及本轮主 Agent 快照，等待用户批准后再执行。批准通过 `POST /api/runs/{run_id}/approve` 派发预览计划。
- 设置面板分为协作设置、提供商与模型、Agent 与子 Agent。Hub 配置保存 `orchestrator_agent_id`、`orchestrator_enabled`、`summary_agent_id`、`summary_policy`、`mode_default` 和默认 `deadline_seconds`。Agent 可以来自既有命令行注册表，也可使用 Hub 兼容提供商；提供商支持 OpenAI Chat Completions、Anthropic Messages 和 Ollama Chat。模型名称是自由文本。无主运行使用配置的 summary Agent 生成逐项摘要和整轮摘要；启用主 Agent 的运行使用本轮主 Agent 生成摘要。`summary_policy=auto` 自动生成整轮摘要，`manual` 只关闭整轮自动汇总；逐项摘要仍独立生成。
- 提供商、静态 API Agent、子 Agent 模板和批准列表通过 `GET/PUT /api/config/agents` 管理。API key 只作为输入字段，GET 只返回 `has_api_key` 布尔值；输入值仅随保存请求发送，由服务端使用 DPAPI 加密保存。页面不会把密钥写入浏览器存储、列表、摘要或日志。动态子 Agent 不接受模型提供的 endpoint、模型、密钥或命令，只可从 `approved_template_ids` 按轮次实例化。
- Agent 间消息通过结构化 `AGENT_HUB_RESULT` 结果协议交付，生产者 CLI 退出后 Hub 才处理返回的消息或子任务，不提供实时流式交付。动态事件可按“主 ↔ 分”“分 ↔ 分”或“全部”筛选；消息正文和子任务提示分别显示。
- 截止时间只标记任务/运行的逾期状态，不会终止正在执行的 CLI 进程。CLI 即使在截止后返回，其结果仍会被保留并显示最终状态。
- 初始加载和轮询使用 `GET /api/state?compact=1`，只取 agents、busy、hub 概要，避免读取 legacy log。任务状态来自 `GET /api/runs/{run_id}`；增量动态来自 `/events`，按真实 relation 显示“主 ↔ 分 / 分 ↔ 分 / 全部”。
- 任务卡只显示服务端提供的真实模型摘要，缺失时明确标为“摘要待生成”或“模型摘要未配置”。页面不会自动读取全文或截断原文充当摘要。用户展开原始全文时才调用 `GET /api/responses/{response_id}?run_id={run_id}&task_id={task_id}`；三个 ID 用于服务端归属核对，全文通过 `textContent` 显示。整体摘要独立显示在运行汇总卡中。
- `interrupted` 显示为“服务重启中断”，计入已结束任务，避免恢复后仍误显示为运行中或逾期。
- 最近创建的 run ID 仅保存在当前浏览器标签页的 `sessionStorage` 中；不会把 prompt 或回复写进浏览器存储。
- 运行、事件、摘要及全文响应保存在 `AGENT_COMM_HOME` 下的本机 Hub 运行数据中，和浏览器的 `sessionStorage` 分开。清除主日志只删除 `log/chat.log`，不会清除 Hub 运行历史、inbox 或全文响应；删除整个 runtime 目录会丢弃这些本地数据。
- 首次打开没有预填个人 Agent、密钥或演示任务；空状态可直接打开设置添加提供商和 Agent。窄屏（≤640px）将模式选择和 Agent 列表置于主内容前；Hub 默认设置在设置对话框中。375px 视口下内容区为 351px，模式、目标、Agent 与摘要内容仍可操作。

## 静态路由与令牌注入要求

服务端需增加严格白名单静态路由：

- `/` 或 `/index.html`：只读本目录 `index.html`，将 `__BUS_TOKEN__` 替换为 `auth.get_or_create_token()` 返回的本机 64 位小写十六进制令牌，再返回 `text/html; charset=utf-8`。
- `/web/style.css`、`/web/status.js`、`/web/deadline.js`、`/web/app.js`：只返回对应固定文件，不通过用户可控路径拼接任意文件名。主页与 API 同源，保留当前 loopback Host/Origin 检查。
- `/api/config/agents`：返回安全 Agent 元数据、脱敏提供商和模型模板；PUT 请求 JSON 包含 `providers`、`provider_agents`、`templates`、`approved_template_ids`。GET 不返回 `api_key`；PUT 可省略 `api_key` 保留当前密钥，传 `null` 清除，传字符串替换。此 registry 与 legacy command Agent 配置分开。
- 不要把 token 放在 URL、浏览器存储、日志或可见页面文本中。主页只将令牌放在 `meta[name="agent-bus-token"]` 属性中；API 请求发送 `X-Agent-Bus-Token`。服务端替换前验证 `[0-9a-f]{64}` 并进行 HTML 属性安全编码。

## 验证

使用 `node --test web/tests/*.test.cjs` 运行状态映射、截止秒精度、窄屏布局契约和惰性全文读取测试。测试不启动真实 Agent，也不访问总线数据。

## 尚需服务端配合

- 全文 API 形式为 `GET /api/responses/{response_id}?run_id={run_id}&task_id={task_id}`；两个 owner ID 必填，服务端将三项 ID 传给归属校验，缺参/格式错误返回 400，归属不匹配统一返回 404。
- Agent 间动态显示服务端事件的原始 `body`；若事件含 `task_prompt`，通过独立折叠区显示子任务提示。浏览器不会自报 `from_agent_id` 来伪造发送者身份。

所有 API 使用 `X-Agent-Bus-Token`，请求体为 JSON；API 错误保持 prompt 和选择状态，文本内容使用安全 DOM API 展示。
