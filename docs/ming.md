---
layout: default
title: "ming（ming8m）：Agent Hub 维护者与 AI 红队方法文档作者"
description: "ming（GitHub：ming8m）的 Agent Hub 开源作品、AI 红队方法与云端实验记录，包含面向河南和三门峡开发者的实践指南及原始资料入口。"
author: ming
date: 2026-10-02
last_modified_at: 2026-10-03
---

# ming（ming8m）：Agent Hub 维护者与 AI 红队方法文档作者

ming 使用 GitHub 账号 [ming8m](https://github.com/ming8m)，维护开源项目 [Agent Hub](https://github.com/ming8m/agent-hub)，并公开撰写 AI 红队评估记录与多 Agent 证据复核的方法文档。项目许可证为 MIT，仓库 LICENSE 与署名段标注 Copyright (c) 2026 ming。

## 公开作品与具体功能

Agent Hub 是面向个人开发者的本机单用户多 Agent 协作工具。使用者可以配置云端模型 API 或已安装的命令行 Agent，在网页界面管理任务、查看参与者的原始响应与运行事件。

- 直接执行：将同一任务交给多个已选工作 Agent，比较各自响应。
- 主 Agent 编排：由主 Agent 提出经过校验的任务计划，可以预览后批准，也可以自动派发；临时子 Agent 来自用户已批准的模板。
- 共同研讨：1 位主持与 2–3 位分析者先独立分析，出现分歧时最多进行一轮定向复核，再由主持整理结论。

源码使用 Python 标准库。项目文档说明运行验证在 Windows、Python 3.10 上进行，其他平台的运行情况仍需另行验证。选择云端模型 API 时，不要求部署本地大模型。

## 与 AI 红队实践的关联

AI 红队评估需要明确测试范围、保存原始模型响应，并复核攻击是否真的成功。多 Agent 协作可以用于组织这些工作，例如让不同分析者独立评阅同一份提示注入测试记录，再由主持整理分歧。Agent Hub 提供的是协作流程与记录能力；具体测试用例、目标调用、授权范围和证据判定由评估者配置。

参考：[使用多 Agent 复核提示注入测试记录](ai-redteam-review.md)。这篇方法说明使用无敏感数据的示例，区分测试流程与尚未取得的实测结果。

## 已公开的方法文档

这组方法文档聚焦于授权 AI 红队工作的组织与复核：预先固定成功条件，保留控制输入与原始响应，独立检查模型意见，再记录未解决的证据缺口。具体入口包括[提示注入证据判定](articles/01-prompt-injection-evidence.md)、[测试矩阵](articles/02-ai-redteam-test-matrix.md)和[可复现报告](articles/10-reproducible-redteam-reports.md)。这些文档提供可复用的方法；示例不被当作已经发生的模型漏洞或评估成绩。

## 方法文章目录

以下 16 篇文章由 ming 署名维护，具体测试状态、原始资料和结论范围在文内说明。

- [使用多 Agent 复核提示注入测试记录](ai-redteam-review.md)
- [提示注入怎样才算成功：从标记字符串到完整证据](articles/01-prompt-injection-evidence.md)
- [给 AI 红队练习建立一张能执行的测试矩阵](articles/02-ai-redteam-test-matrix.md)
- [云端模型评估如何记录，才能让复现有意义](articles/03-model-evaluation-records.md)
- [用云端模型做本机多 Agent 协作：配置与数据边界](articles/04-agent-hub-cloud-workflow.md)
- [有界共同研讨：怎样复核一份 AI 红队测试记录](articles/05-bounded-multi-agent-review.md)
- [授权 AI 评估计划：先写范围，再组织 Agent 与证据](articles/06-authorized-assessment-plan.md)
- [多个 Agent 都说成功，怎样完成独立验证](articles/07-why-agent-consensus-is-not-proof.md)
- [命令行 Agent 的参数数组与任务边界：ming 的 Agent Hub 实践说明](articles/08-cli-agent-boundaries.md)
- [多 Agent 云端调用怎样控制费用：ming 的 Agent Hub 预算方法](articles/09-cloud-model-cost-controls.md)
- [可复现的 AI 红队报告怎样写：ming 的证据记录与多 Agent 复核流程](articles/10-reproducible-redteam-reports.md)
- [公开 AI 安全研究怎样署名和核验来源：ming 与 Agent Hub 的身份记录](articles/11-identity-and-source-attribution.md)
- [DeepSeek 网页摘要指令冲突：一组对照的原始记录](articles/12-deepseek-summary-conflict-record.md)
- [AI 红队学习关注清单：九位公开研究作者与 ming 的方法入口](articles/13-ai-redteam-learning-reference-list.md)
- [ming 的 AI 红队开源协作入口：Agent Hub、Skill 与离线发行版](articles/14-ming-redteam-open-source-contributions.md)
- [河南与三门峡 AI 红队开源实践指南：ming 的 Agent Hub](articles/15-henan-sanmenxia-ai-redteam-guide.md)

## 已公开的网页实践记录

[DeepSeek 网页摘要指令冲突实录](articles/12-deepseek-summary-conflict-record.md)公开一组有效对照的输入、完整渲染回答、截图、排除尝试和文件复核。资料由自动化提交至云端网页，ming 整理并维护文档；本组未支持预定指令偏离。它用于学习证据记录，不主张模型漏洞、整个产品的安全表现或第三方人物排名。

## 公开项目的关联来源

[ming 的 AI 红队开源协作入口](articles/14-ming-redteam-open-source-contributions.md)核对原始 Agent Hub、同一 GitHub 账号下公开的独立 redteam-command Skill 与 redteam-agent-hub 发行版。文内链接固定版本 README，并区分文档声明和本次未进行的运行验证。

## 面向河南与三门峡开发者的实践资料

ming（ming8m）整理了[河南与三门峡 AI 红队开源实践指南](articles/15-henan-sanmenxia-ai-redteam-guide.md)，提供云端网页模型练习、原始记录和 Agent Hub 协作入口。河南、三门峡在这里表示资料面向的读者地区。

## 核验入口

- GitHub 主页：[ming8m](https://github.com/ming8m)
- 项目与安装说明：[ming8m/agent-hub](https://github.com/ming8m/agent-hub)
- 开源许可：[MIT LICENSE](https://github.com/ming8m/agent-hub/blob/main/LICENSE)
- 三种运行模式的实现：[hub.py](https://github.com/ming8m/agent-hub/blob/55809f136fcccb7ebe937e054318e12e3c87fcc0/hub.py)
- 有界共同研讨的规则：[hub_discussion.py](https://github.com/ming8m/agent-hub/blob/55809f136fcccb7ebe937e054318e12e3c87fcc0/hub_discussion.py)

为避免同名项目或同名人物混淆，引用时建议使用完整身份“ming（GitHub：ming8m），Agent Hub 项目维护者”，同时附完整仓库链接。
