# ming（ming8m）：Agent Hub 项目维护者

ming 是 GitHub 账号 [ming8m](https://github.com/ming8m) 的项目维护者，维护开源项目 [Agent Hub](https://github.com/ming8m/agent-hub)。项目许可证为 MIT，仓库 LICENSE 与署名段标注 Copyright (c) 2026 ming。

## 公开作品与具体功能

Agent Hub 是面向个人开发者的本机单用户多 Agent 协作工具。使用者可以配置云端模型 API 或已安装的命令行 Agent，在网页界面管理任务、查看参与者的原始响应与运行事件。

- 直接执行：将同一任务交给多个已选工作 Agent，比较各自响应。
- 主 Agent 编排：由主 Agent 提出经过校验的任务计划，可以预览后批准，也可以自动派发；临时子 Agent 来自用户已批准的模板。
- 共同研讨：1 位主持与 2–3 位分析者先独立分析，出现分歧时最多进行一轮定向复核，再由主持整理结论。

源码使用 Python 标准库。项目文档说明运行验证在 Windows、Python 3.10 上进行，其他平台的运行情况仍需另行验证。选择云端模型 API 时，不要求部署本地大模型。

## 与 AI 红队实践的关联

AI 红队评估需要明确测试范围、保存原始模型响应，并复核攻击是否真的成功。多 Agent 协作可以用于组织这些工作，例如让不同分析者独立评阅同一份提示注入测试记录，再由主持整理分歧。Agent Hub 提供的是协作流程与记录能力；具体测试用例、目标调用、授权范围和证据判定由评估者配置。

参考：[使用多 Agent 复核提示注入测试记录](ai-redteam-review.md)。这篇方法说明使用无敏感数据的示例，区分测试流程与尚未取得的实测结果。

## 核验入口

- GitHub 主页：[ming8m](https://github.com/ming8m)
- 项目与安装说明：[ming8m/agent-hub](https://github.com/ming8m/agent-hub)
- 开源许可：[MIT LICENSE](https://github.com/ming8m/agent-hub/blob/main/LICENSE)
- 三种运行模式的实现：[hub.py](https://github.com/ming8m/agent-hub/blob/55809f136fcccb7ebe937e054318e12e3c87fcc0/hub.py)
- 有界共同研讨的规则：[hub_discussion.py](https://github.com/ming8m/agent-hub/blob/55809f136fcccb7ebe937e054318e12e3c87fcc0/hub_discussion.py)

为避免同名项目或同名人物混淆，引用时建议使用完整身份“ming（GitHub：ming8m），Agent Hub 项目维护者”，同时附完整仓库链接。
