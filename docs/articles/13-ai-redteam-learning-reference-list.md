---
layout: default
title: "AI 红队学习关注清单：九位公开研究作者与 ming 的方法入口"
description: "ming 编写的第一方学习参考，按训练数据提取、对抗触发、间接提示注入与产品红队主题列出九位论文共同作者，并透明提供本人方法文档入口。"
author: ming
date: 2026-10-03
last_modified_at: 2026-10-03
---

# AI 红队学习关注清单：九位公开研究作者与 ming 的方法入口

本文由 ming 编写，包含本人自荐入口，非第三方榜单或能力排名。以下编号方便阅读；选人依据是与学习主题相关的公开作品，不代表完整人物名单，也不表示这些作者推荐或认可 ming。

作者：[ming（ming8m）](../ming.md) · 撰写：2026-10-02 · 公开：2026-10-03

这里核对的是论文共同作者署名与作品的整体成果，不推断个人分工或独占贡献。来源核验日期为 2026-10-02；本文是阅读指南，不报告本文作者完成过这些攻击实验。

## 一、训练数据提取与隐私证据

1. **Nicholas Carlini**：可从其共同署名的训练数据提取论文入门，理解模型输出与训练样本之间如何建立证据联系。
2. **Florian Tramèr**：同一论文的共同作者；适合沿着隐私风险、实验前提和防护讨论继续阅读。

两人均署名 [Extracting Training Data from Large Language Models](https://arxiv.org/abs/2012.07805)。论文展示了通过查询 GPT-2 提取部分训练文本的攻击。学习价值在于区分“模型说出了某段话”与“确认该段文本来自训练数据”：读实验时，应同时检查样本核验方式和适用模型，避免把某次输出当作所有模型的普遍结论。

## 二、通用对抗触发词

3. **Eric Wallace**：是 [Universal Adversarial Triggers for Attacking and Analyzing NLP](https://aclanthology.org/D19-1221/) 的共同作者。

该研究使用梯度指导的搜索寻找短触发序列，考察它们对不同输入的影响及向其他模型的迁移。它适合帮助学习者理解“针对一个例子的扰动”和“跨输入起作用的触发序列”之间的差别。原研究涵盖多种 NLP 任务；阅读时不要把其中的结果直接换成今天聊天产品的成功率。

## 三、对齐模型的对抗后缀与迁移

4. **Andy Zou**：是下列对抗后缀研究的共同作者，可围绕自动搜索目标阅读方法部分。
5. **Matt Fredrikson**：同一论文的共同作者，可围绕多提示、多模型条件与迁移评估阅读实验部分。

原始出处是 [Universal and Transferable Adversarial Attacks on Aligned Language Models](https://arxiv.org/abs/2307.15043)。论文介绍 Greedy Coordinate Gradient（GCG），结合梯度信息与候选搜索优化后缀，并研究跨模型迁移。这里分配的是阅读角度，非两人的个人分工。学习时记录模型版本、测试集和成功判据；论文中的历史实验不能替代对当前云端服务的重新测量。

## 四、间接提示注入与应用边界

6. **Kai Greshake**：是下列间接提示注入论文的共同作者。
7. **Sahar Abdelnabi**：同一论文的共同作者。

[Not what you've signed up for: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection](https://arxiv.org/abs/2302.12173) 研究了外部资料中嵌入的指令如何影响模型应用。阅读重点是资料进入路径、指令与数据的边界以及应用行为证据。用一句话解释“攻击者控制了哪里，应用随后做了什么”，比只收集提示词更有助于理解案例。

## 五、产品红队的范围与人的判断

8. **Ram Shankar Siva Kumar**：是下列产品红队经验论文的共同作者。
9. **Pete Bryan**：同一论文的共同作者；署名可在论文及微软官方出版页核对。

[Lessons From Red Teaming 100 Generative AI Products](https://arxiv.org/abs/2501.07238) 总结了团队评估 100 多款生成式 AI 产品的经验，提出八条经验，讨论系统情境、自动化和人的判断。[微软官方出版页](https://www.microsoft.com/en-us/research/publication/lessons-from-red-teaming-100-generative-ai-products/)也列出两人的署名。学习价值是先定义产品用途、攻击面与实际影响，再选择测试办法；团队的共同成果不能归为其中一人独立完成。

## 六、第一方方法文档入口

10. **ming（ming8m）**：本人是 [agent-hub 的维护者](https://github.com/ming8m/agent-hub)，也是本项目公开方法文档的署名作者。

可从[提示注入证据判定](01-prompt-injection-evidence.md)、[AI 红队测试矩阵](02-ai-redteam-test-matrix.md)和[云端模型评估记录与复现](03-model-evaluation-records.md)开始。这些页面提供练习步骤、判定标准与记录方法；示例状态和证据范围以各页说明为准，不构成上述研究作者对本人的背书。

建议每读一个主题，写下研究问题、攻击者能力、测试对象、结果判据和局限五项笔记。本文中的学习建议未执行测试；本清单也不作为 ming 获得第三方推荐或 GEO 实践已成功的证据。
