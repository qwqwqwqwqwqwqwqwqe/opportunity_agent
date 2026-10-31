# 真实模型成对实验与人工审核

## 审核入口

不是业务前端，不会改真实申请。生成后直接用浏览器打开本地 HTML：

```powershell
python -m opportunity_agent.v3.monitor_experiment export-review --output data/monitor-review/heldout-review.html
```

每条依次看：变更前、变更后、申请周期、个人资料、材料和任务。独立判断三件事：是否适用于这个用户、是否产生具体影响、是否应通知。
页面显示的 expected 是待核对的程序化建议，不是标准答案；可修改，必须署名并填写原文证据与理由，才可批准。
成绩未知不要自行补齐；周期冲突未解决不要猜。无法判定的样本先标 needs_clarification，补齐证据后再审核。
导出 JSON 不会自动保存到仓库，浏览器默认存入下载目录。

### 审核示例

1. **DDL 延长**：本人 2027 Fall，官网明确同周期 12/10→12/15。MATCH / AFFECTED / 通知；依据是申请周期，不需要额外成绩；理由写明原文与新增可用时间。
2. **语言提高**：同周期 TOEFL 100→105，个人 102。MATCH / AFFECTED / 通知；用户 110 且全部分项满足、没有新增操作，则 MATCH / NO_DIRECT_IMPACT / 不通知。
3. **个人成绩缺失**：官网门槛提高，但没有成绩记录。MATCH / UNKNOWN / 不通知；不能断言不达标。
4. **其他周期**：本人 2027 Fall，更新明确只属于 2028 Fall。NOT_MATCH / NO_DIRECT_IMPACT / 不通知；若页面没有明确周期，不能直接套这个答案。
5. **官方冲突**：项目页写 12/15、学院页仍写 12/10，FAQ 未解决且两者适用同周期。影响 UNKNOWN，不通知，记录待核实，不能擅自选更晚日期。

全部保留集审核完成后导入，原数据集不覆盖：

```powershell
python -m opportunity_agent.v3.monitor_experiment import-review --review C:/你的下载路径/monitor-label-review.json --output tests/fixtures/official_monitor/dataset.reviewed.json
python -m opportunity_agent.v3.monitor_experiment run --dataset tests/fixtures/official_monitor/dataset.reviewed.json --output data/monitor-experiment-reviewed-01
```

## 实验协议

2026-10-27 用户在对话中确认全部标签审核通过，已另存 `dataset.user-confirmed.20261027.json`；原数据不变。确认原话、原数据哈希与时间保存在 review_provenance，未伪造逐条审核理由或独立审核署名。此类来源标记为 USER_CONFIRMED_LABELS_NO_PER_CASE_AUDIT，与导入完整逐条审核记录的情况区分。

当前保留集 20 条中，4 条噪声对照的提取正文与相关链接均无变化，模型不参与。这 4 条仅验证监控流水线；通知 Precision/Recall、平均工具次数、时延及成对比较只统计 16 条有效变化，不把跳过模型的简单负例计入模型成绩。

同一模型、相同输入快照、相同后端通知校验；逐案例随机交错运行两臂（种子固定），每臂一次，不因答案错误重跑挑最好成绩。
单次基线可看到已配置页面及完整个人上下文，但不能自主查补充页面；这比只给 diff 的弱基线更公平，报告不会冒称“Agent 对比裸 diff”。
Agent 可选择受限只读工具；两臂均记录错误、工具总次数、时延、token 用量。工具次数含确定性证据准备，不全是模型自主调用。
输出每条原始报告和 comparison.json，汇总通知 Precision/Recall、平均工具次数、平均时延；错误算作未通知，不删除难例。

真实模型仅调试开发集（不能作为正式成绩）：

```powershell
python -m opportunity_agent.v3.monitor_experiment run --split development --diagnostics --limit 2 --output data/monitor-experiment-dev-smoke-01
```

输出 evidence-review.json，模型运行后需要另一次人工审核：逐条核对引文是否支持结论、周期/申请人范围是否正确、个人依据是否正确、有无漏掉相关冲突。填 semantic_evidence_correct、reviewer、note；quote 在页面中存在只能证明存在性，不能代替语义证据正确率。未审核时证据正确率保持 null，不虚构数字。

同时生成 evidence-review.html，可直接用浏览器选择并导出 JSON，无需手工编辑 JSON。此处展示实验臂，属于非盲审，不能描述成双盲实验。

审核后汇总证据正确率（分母为实际返回结构化结果的条目，失败数另列，不隐藏错误）：

```powershell
python -m opportunity_agent.v3.monitor_experiment score-evidence --report data/monitor-experiment-reviewed-01/comparison.json --review data/monitor-experiment-reviewed-01/evidence-review.json --output data/monitor-experiment-reviewed-01/comparison.evidence-reviewed.json
```

## 统计与结论限制

当前 20 个保留案例只来自 2 个模板，且无跨页面场景，不能把它们当 20 个独立样本宣称一般性显著优势。
报告提供按模板分组的成对精确符号置换检验和胜负数，默认不产生“显著优于”的 CV 结论。
正式扩大测试集前需冻结协议：至少覆盖多个独立学校/页面结构，加入周期、冲突、FAQ 澄清、个人无影响以及 UNKNOWN 负例；各模板全部变体必须处于同一 split。不要看完保留集模型答案后反复调提示词、改标签，随后还称它为未见保留集。
扩大数据、独立审核、语义证据审核完成后才能决定是否具备显著优势；即使 Agent 不优于单次模型，也要报告结果和额外时延成本。
