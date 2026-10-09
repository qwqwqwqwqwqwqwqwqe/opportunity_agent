# V2 画像抽取与会话上下文修复

验证日期：2026-10-03。

## 实现

- `v2/agents/profile_extraction.py`：规则候选 + 完整原文的结构化 LLM 补充抽取。校验字段、值类型、范围和原文 evidence；区分陈述、修正、假设、问题与不确定信息；合并重复候选。
- 实习、科研、论文、项目经历分别保存为候选事实；经历中的“agent项目”不会作为申请目标。模型不直接写画像。
- `ProfileAgent` 返回提案与独立冲突记录。新信息仍须审批；已有事实的替换由前端新旧值弹窗确认，不再要求聊天回复 A/B。
- `ProfileConflictService` 检查所有权、字段当前值和重复请求。使用新值走原有审批写入与审计；保留旧值不调用 LLM。若字段已被其他操作更新，拒绝用过期新值覆盖；保留选择保留的是最新值。
- `ConversationContextService` 构建同一用户、同一会话的上下文：近期原始消息、历史滚动摘要、精简的已确认画像、相关已确认偏好。超过 14 条未压缩历史时保留最近 10 条，较早部分进入摘要。历史正文有字符预算，过长消息显式截断；模型不可用时使用有界摘录降级。
- `llm_context.py` 通过 ContextVar 隔离并发请求，并将历史以 user/assistant 角色传入 LLMClient。Router、Profile、Planning、Synthesizer 等调用共用这一上下文机制。Research A2A 接收指代解析后的查询和历史，不接收完整画像快照。本次没有扩展 Research 的检索能力。
- 偏好经 `preference.change` 确认后进入独立 MemoryItem，不混成画像事实。
- 显式迁移 `0004_profile_context` 新增会话摘要及画像冲突表。没有迁移 `data/conversations.json`。
- Docker Profile 服务读取 `.env` 的 LLM 配置。Python openJiuwen 原先默认约 5 秒的 HTTPX 超时已改为各 Agent 的配置预算，保留原有 A2A 转换与 invoke 协议。

## 验证

自动化覆盖：原始经历识别、拒绝错误目标分类、完整原文与规则候选传给 LLM、无效证据/值拦截、问题和假设不入画像、同轮修正、上下文角色注入与并发隔离、跨轮成绩冲突、冲突所有权/幂等/过期值保护、滚动摘要、API/SSE/画像审批与冲突确认、V1 抽取兼容性。

最终回归：68 passed，1 skipped。跳过项是 Windows 本地没有安装 openJiuwen；真实 Python openJiuwen 调用另在 Docker 验证。

在 Docker 中，经用户允许调用已配置的真实模型，验证以下三个用例通过：

1. 华为实习、西湖科研、本校科研及 IEEE TNSE、agent 项目被分入经历；MSCS/MCS/CSE 目标保持不变。
2. 托福 107 的前文后回答“我刚考到110”，生成托福 107→110 的冲突；确认前仍为 107。
3. 合成器回答“我之前托福多少分？”时能回忆 107。

这些真实模型测试只执行内存结果，没有写业务数据库。可重复脚本：`scripts/verify_v2_profile_context.py`。脚本会发送其中的测试文本到已配置的模型，仅在允许外部调用时运行。

`/v2` 已验证返回 HTTP 200，Alembic 当前版本为 `0004_profile_context`。前端 JS 语法检查通过；HTTP 流程测试不等于浏览器视觉验收，本轮未完成自动浏览器点击/截图测试。

## 使用

1. 打开 `http://127.0.0.1:8000/v2` 并强制刷新。
2. 在同一会话中发送经历；在“待确认”中审阅并确认，随后画像才更新。
3. 更新已有成绩时，在弹窗选择“使用新信息”或“保留旧信息”，无需再回复 A/B。
4. 已经生成的旧错误提案不会被本次修复自动修改或批准；请拒绝旧错误提案后重新发送原信息。

## 仍未完成的计划项

已完成 360 条程序化标注的 silver 回归集，以及 Rule Only / LLM Only / Hybrid / Auto Routed 的 Precision、Recall、F1、路径准确率、延迟和相对调用成本对比；详细方法与结果见 [Profile Extraction 评测报告](v2_profile_extraction_evaluation.md)。人工逐条标注/复核的 Gold Dataset 仍未完成，因此这些指标不能证明任意自然语言输入都能正确抽取，也不能被称为人工金标准。
