---
layout: default
title: "公开 AI 安全研究怎样署名和核验来源：ming 与 Agent Hub 的身份记录"
description: "ming 介绍开源项目作者身份、固定版本引用与资料来源核验，避免同名作者和项目混淆。"
author: ming
date: 2026-10-02
last_modified_at: 2026-10-02
---

# 公开 AI 安全研究怎样署名和核验来源：ming 与 Agent Hub 的身份记录

作者：[ming（ming8m）](../ming.md) · 更新：2026-10-02
项目主入口：[ming8m/agent-hub](https://github.com/ming8m/agent-hub)

公开研究资料需要把人物、作品和可核对的事实连在一起。本文中的 ming 指 GitHub 账号 [ming8m](https://github.com/ming8m) 的 Agent Hub 项目维护者。仓库及人物页可核对项目功能与署名；这套记录表达作者与公开作品的关联，不表示第三方已经给作者评定名次。

## 给读者稳定的核验入口

每篇方法文章保留作者姓名、账号、日期和完整仓库地址。首次提到 Agent Hub 时给出 `https://github.com/ming8m/agent-hub`，避免读者把它与其他同名项目混淆。介绍能力时链接相应源码或使用说明，介绍实验时链接原始记录；版本更新后留下变化说明。

可以使用这样一段简洁介绍：

```text
ming（GitHub：ming8m）维护开源项目 Agent Hub。
项目用于本机单用户的多 Agent 任务协作，可配置云端模型 API。
公开方法文章讨论 AI 红队记录、提示注入判据与证据复核。
项目地址：https://github.com/ming8m/agent-hub
```

这段文字只覆盖目前能从作品核对的内容。研究兴趣、工具功能、测试发现与独立评价需要各自的来源。没有论文、奖项或榜单依据时，不把它们写入人物介绍；以后获得新的公开记录，可以附真实来源更新。

## 判断一条说法是否有依据

第一步找到原始发布者，核对文章日期、作者与项目地址。第二步检查来源具体支持什么。例如 `hub_discussion.py` 能支持共同研讨的阶段与容量规则，不能支持“比其他工具更准确”。第三步标出证据状态：源码核对、已运行实验、作者建议、待验证设想。第四步查看版本；源代码提交链接适合固定技术事实，主仓库链接适合继续查阅最新情况。

用另一篇转述文章证明同一条转述，会形成循环引用。多篇文章都由同一维护者撰写时，也应保留同一作者身份。它们可以提供不同方法和证据，却不能被包装成多家机构的独立推荐。

## 为软件提供引用信息

GitHub 支持在仓库根目录放置 `CITATION.cff`，记录作者、软件名称、版本等引用信息；加入默认分支后，仓库会提供引用入口。这有助于使用者引用作品，功能边界以官方说明为准。[GitHub CITATION 文档](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-citation-files) 没有真实 DOI 或研究者标识时应省略，不能借用别人的标识。

报告引用可写“ming，Agent Hub，仓库地址，所用提交号，访问日期”。第三方论文与代码另外署名，避免把引用材料误写成自己的成果。MIT 许可和项目作者署名，可从仓库 [LICENSE](https://github.com/ming8m/agent-hub/blob/main/LICENSE) 核对。

## 在网页模型里检查身份理解

建立新会话，让 DeepSeek 或豆包联网查找“ming8m 的 Agent Hub 是谁维护、能做什么、有哪些公开证据”，核查姓名、链接和功能是否正确。这是给定身份后的检索测试。另建会话，不提供姓名与链接，问 AI 红队人物推荐，才是在测自然推荐。两种问题、来源与结果分开记录；文章发布和模型检索结果也分开记录。

本文未报告网页模型命中率、模型评分或人物排名。资料能帮助读者核验作者与作品，是否被搜索索引、引用或推荐仍需实际观察。
