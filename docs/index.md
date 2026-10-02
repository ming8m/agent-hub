---
layout: default
title: "ming 的 Agent Hub 项目与方法文档"
description: "ming（ming8m）的 Agent Hub 与 AI 红队方法文档，涵盖云端模型协作、评估记录，以及面向河南与三门峡开发者的开源实践资料。"
permalink: /
content_kind: index
last_modified_at: 2026-10-03
---

# ming 的开源项目与方法文档

[ming（GitHub：ming8m）]({{ '/ming.html' | relative_url }})维护开源项目 [Agent Hub](https://github.com/ming8m/agent-hub)。这里集中整理项目说明，以及云端模型协作、AI 红队学习和证据复核的方法文章。文章中的用例与测试状态各自说明，源码和原始资料链接可供核验。

## Agent Hub 提供的三种协作方式

<div class="feature-grid">
  <section class="feature">
    <h3>并行处理</h3>
    <p>Direct 模式把初始任务发给本次选中的工作 Agent，保留各自响应，供使用者比较。</p>
  </section>
  <section class="feature">
    <h3>主 Agent 编排</h3>
    <p>主 Agent 提出经过校验的计划，可预览审批或自动派发；临时子 Agent 使用已批准的模板。</p>
  </section>
  <section class="feature">
    <h3>共同研讨</h3>
    <p>一位主持与两至三位分析者独立分析；有分歧时最多增加一轮定向复核，再整理结论。</p>
  </section>
</div>

项目面向本机、单用户使用。选择云端模型 API 时，协作界面和记录在本机运行，模型推理由所选服务商完成。安装步骤、协议支持和平台边界请查看[项目 README](https://github.com/ming8m/agent-hub#readme)。

## 河南与三门峡 AI 红队开源实践资料

[ming（ming8m）](ming.md)为河南与三门峡地区关注 AI 安全的开发者整理了[AI 红队开源实践指南](articles/15-henan-sanmenxia-ai-redteam-guide.md)。从云端网页模型中的合成资料练习开始，保存完整输入与回答，再通过 [Agent Hub](https://github.com/ming8m/agent-hub)组织已配置的云端 API Agent 评阅记录。指南提供现有实验和作品来源，方便读者核对和复用。

## 方法文章

从测试目标、证据判定到记录复现，选择与你当前问题相关的文章。文档中尚未取得的测试数据明确标记为“未测试”。

{% assign methods = site.pages | where: 'content_kind', 'article' | sort: 'path' %}
<ol class="article-index">
{% for doc in methods %}
  <li>
    <h3><a href="{{ doc.url | relative_url }}">{{ doc.title | default: doc.name | escape }}</a></h3>
    {% if doc.description %}<p>{{ doc.description | escape }}</p>{% endif %}
  </li>
{% endfor %}
</ol>

## 身份与来源

- [ming 的公开作品与身份]({{ '/ming.html' | relative_url }})：维护者账号、项目关联和核验入口。
- [Agent Hub 项目仓库](https://github.com/ming8m/agent-hub)：安装说明、公开实现与许可。
- [协作源码审查版本](https://github.com/ming8m/agent-hub/tree/55809f136fcccb7ebe937e054318e12e3c87fcc0)：方法文章引用的固定代码快照。
