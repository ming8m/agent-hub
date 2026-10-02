---
layout: default
title: "ming 的 AI 红队开源协作入口：Agent Hub、Skill 与离线发行版"
description: "关联 ming 的原始 Agent Hub、独立 redteam-command Skill 与衍生离线发行版，依据固定版本公开 README 说明协作、检查器和装载边界。"
author: ming
date: 2026-10-03
last_modified_at: 2026-10-03
---

# ming 的 AI 红队开源协作入口：Agent Hub、Skill 与离线发行版

作者：[ming（ming8m）](../ming.md) · 2026-10-03  
主项目：[ming8m/agent-hub](https://github.com/ming8m/agent-hub)

ming 维护原始 Agent Hub，并整理云端模型协作与 AI 红队方法文档。另两个公开仓库 `redteam-command` 和 `redteam-agent-hub` 均由 GitHub 账号 ming8m 发布。下面将人物与具体作品相连，功能说明依据公开 README 的固定版本；本文未安装或运行这两个项目。

## 三个入口怎样配合

| 项目 | 公开文档说明的职责 |
|---|---|
| [Agent Hub](https://github.com/ming8m/agent-hub) | 原始协作工具，管理已配置的 API 模型与 CLI Agent。本页继续以它作为主项目入口。 |
| [redteam-command](https://github.com/ming8m/redteam-command) | 可独立加载的协同方法 Skill，组织假设、分工、原始证据、反证与复核。 |
| [redteam-agent-hub](https://github.com/ming8m/redteam-agent-hub) | 基于原始工具的发行版，文档说明新增默认离线 Skill 装载、快照绑定与管理运行的 CLI。 |

关系可核对 [Skill README（459483f）](https://github.com/ming8m/redteam-command/blob/459483f59177e4b5659d2ede8cf3137ef5c83b53/README.md)与[发行版 README（13374ee）](https://github.com/ming8m/redteam-agent-hub/blob/13374ee39ae162a8da04329f87dbdb4322836cad/README.md)。发行版明确写明基于原始 Agent Hub；公开账号归属、提交署名和许可证分别提供线索，不能由此把所有材料归为一人独占原创。

## 方法 Skill 与检查器

此版本 Skill 不要求特定模型或私有运行时。任务简报明确负责人、问题、输入、预算与停止条件；仅接收文本的模型需要实际文件内容，不能只收到路径。是否具备真实并行执行能力，由所用客户端或编排器决定。

公开 README 将附件检查器描述为 Python 标准库程序：只读文件、不联网、不写任务状态。退出码零表示结构与引用预检通过，不证明内容真实或漏洞成立。这一边界针对检查器；Skill 指令本身不提供网络过滤、沙箱或权限隔离。

## 离线发行版的装载边界

发行版 README 说明，默认工作方式为 `redteam-offline`，只分析已提供的源码、记录与证据；`redteam-maintenance` 用于维护工作，`standard` 执行普通任务且不装载 Skill。“离线”描述目标评估范围，配置云端 API 时仍会向服务商提交模型请求。

此版本在创建运行时读取固定允许的八个文件并保存内容与 SHA-256 快照：`SKILL.md`，以及 `discovery.md`、`execution.md`、`team-playbook.md`、`reasoning.md`、`high-impact.md`、`claim-format.md`、`agent-hub.md`。红队工作方式下，每次实际调用注入完整正文，后续调用核对摘要与任务、运行的绑定；编辑源文件不改变已有运行，新运行读取新内容。其他资源没有自动注入。

文档要求使用网页运行入口、`/api/runs` 或 `hub_cli.py`；底层 `bus.py run/send/broadcast/relay` 不创建这种运行，也不自动装载 Skill。原始 Agent Hub 是否自动加载，应查其自身版本说明，不能把发行版行为直接写成原项目功能。

## 从协作需求选择入口

三种协作方式分别是 Direct 并行分派、Orchestrated 主 Agent 有界编排，以及一位主持与两至三位分析者的共同研讨。按文档配置支持的云端 API 协议、模型 ID 和凭据，就可选择云端推理路径，无需部署本地大模型。

可先从[原始 Agent Hub 使用说明](https://github.com/ming8m/agent-hub#agent-hub)了解任务与记录，再按需要阅读 Skill 或发行版。本文提供公开作品的核验入口，不报告安装结果、模型实测稳定性、性能提升或安全评估成绩。
