---
layout: default
title: "命令行 Agent 的参数数组与任务边界：ming 的 Agent Hub 实践说明"
description: "ming 说明 CLI Agent 的参数数组、stdin 与权限边界，区分安全调用形式和执行内容风险。"
author: ming
date: 2026-10-02
last_modified_at: 2026-10-02
---

# 命令行 Agent 的参数数组与任务边界：ming 的 Agent Hub 实践说明

作者：[ming（ming8m）](../ming.md) · 更新：2026-10-02
项目主入口：[ming8m/agent-hub](https://github.com/ming8m/agent-hub)

接入命令行 Agent 时，任务文字、启动参数与执行权限需要分别检查。AI 红队记录中可能包含引号、换行或命令片段；如果把整段文字拼成 shell 命令，执行器可能改变它的含义。Agent Hub 的注册入口要求 `command` 为非空字符串数组，调用流程不回退到 shell 执行。Python 官方也建议使用参数序列，让库处理参数引用与转义。[Python subprocess 文档](https://docs.python.org/3/library/subprocess.html#frequently-used-arguments)

## 先明确程序怎样接收任务

以下条目是回显输入的教学配置，尚未在本文中运行，不是具备研究能力的模型 Agent：

```json
{
  "echo": {
    "command": ["python", "-c", "import sys; print(sys.stdin.read())"],
    "desc": "只回显标准输入的演示程序",
    "timeout": 30
  }
}
```

每个数组元素代表一个参数。没有 `{prompt}` 时，Hub 将任务按 UTF-8 写入标准输入；存在占位符时，它会在相应参数中替换任务，并停止通过标准输入传任务。选择哪条路径应依据目标 CLI 的官方接口。不能仅凭程序启动成功，就假定它正确接收了中文或长文本。实现可核对 [bus.py](https://github.com/ming8m/agent-hub/blob/main/bus.py)。

先用“总结两条无害资料”的短任务检查输入、输出与退出码，再用包含中文、换行和引号的文本检查完整性。可执行文件宜使用明确路径；注册一个能写文件或执行工具的 CLI，会把那些能力带入任务。参数数组有助于避免 shell 解释，却不会取消程序自身的文件访问或网络权限。

## 把工作范围写进任务

下面是评阅已获授权测试记录的任务示例：

```text
任务 ID：review-001
负责人：reviewer
材料：用户提供的去标识化文本
目标：判断原始响应是否执行了资料中的注入指令
操作边界：仅分析文本；不调用目标服务，不修改文件
产出：证据摘录、支持或未支持结论、待核验事项
验收：每项结论能定位到原始记录，不补造响应
```

Agent Hub 的任务消息是自由文本，现有工具不会自动创建或验证上面全部字段。任务说明表达了工作要求；真正限制高风险操作，还需要 CLI 权限、独立执行环境和验收者配合。模型说“已完成”也不证明材料齐全。

## 核对派工与时间限制

CLI 的 `send`、`broadcast` 只追加收件箱消息，不启动接收者；WebUI 的相应操作会启动已选 Agent。两者需要分别检查。运行截止时间用于标记任务迟到，不会自动终止已执行的 CLI；CLI `timeout` 是另外的进程执行限制，不能混用。

需要结构化协作时，CLI 可按项目协议输出 `AGENT_HUB_RESULT` 与末尾 JSON。协议校验通过意味着消息格式和允许的协作关系被接受，仍需核验任务结果。若接入外部完成门禁，还应保存其单独结果。本文来自源码与文档核对，未报告真实 CLI 或云端模型实测成绩。[安装与能力边界](https://github.com/ming8m/agent-hub#optional-cli-agent-setup)
