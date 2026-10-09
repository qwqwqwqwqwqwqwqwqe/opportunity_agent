# V2 Profile Extraction：路径路由与 360 条回归评测

评测日期：2026-10-03。数据集为 [profile_extraction_silver_360.jsonl](../tests/fixtures/profile_extraction_silver_360.jsonl)，包含 360 条程序化模板标注的中英文样本。

> 这不是人工 Gold Dataset。`manifest` 明确标为 `programmatic_template_annotation`、`human_review: pending`。它可用于当前版本回归、消融和路由评测；在对外宣称“Gold”或用作发布门槛前，必须逐条人工复核/修订标签。

## 路由标注与生产策略

每条样本有独立的 `expected_route`。这不是由模型回推的标签，而是数据集作者对“最低安全且成本合适路径”的标注。

| 路径 | 条数 | 含义 |
| --- | ---: | --- |
| `rule_only` | 260 | 明确分数、排名、毕业年份、明确经历和已被规则完全覆盖的偏好；不调用 LLM。 |
| `llm_only` | 50 | 自然语言申请目标、职业意图、否定/条件性备选偏好；调用结构化 LLM。 |
| `hybrid` | 20 | 规则事实和自然语义同时存在；规则候选与完整原文一起给 LLM。 |
| `reject` | 30 | 问句、纯假设、未来且尚未开始的经历；不做 Profile 抽取，也不调用 LLM。 |

`ProfileAgent` 现在用 `mode="auto"`。`route_profile_extraction()` 是确定性 gate：先过滤无事实的问句/假设和未来意图，再识别自然语义信号，最后选出最低成本的可用路径。它不会让 LLM 决定要不要调用 LLM。

## 覆盖面

| 类别 | 条数 |
| --- | ---: |
| 显式分数 | 90 |
| 多事实学术背景 | 40 |
| 排名/毕业年 | 20 |
| 自然语言目标/职业（含否定、条件、备选） | 40 |
| 实习、科研、论文、项目 | 40 |
| 明确偏好 | 30 |
| 问句、假设、非法值、不确定表达 | 40 |
| 条件性备选偏好 | 10 |
| 更正 | 30 |
| Profile + Preference + 复杂目标混合 | 20 |

事实按“字段 + 规范化值”精确计数；列表按每一项计数。偏好与 Profile facts 分开计分。模型调用异常以空预测记录，会计入 Recall 和失败数，绝不静默排除。

## 实测结果

模型为运行时 `.env` 中配置的模型。Rule Only 不调用模型；LLM Only 和强制 Hybrid 分别对全部 360 条调用模型；Auto 根据实际路由执行。模型 API 未提供可审计 token usage，因此“成本”使用模型调用次数作为透明的相对代理，不能直接换算人民币或美元。

| 模式 | Fact Precision | Fact Recall | Fact F1 | Preference F1 | 平均延迟 | 模型调用 | 相对调用成本 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Rule Only | 90.09% | 57.88% | 70.48% | 71.43% | 2.36 ms | 0/360 | 0% |
| LLM Only | 74.64% | 63.33% | 68.52% | 83.43% | 14.04 s | 360/360 | 100% |
| 强制 Hybrid | 74.46% | 78.64% | **76.49%** | 86.86% | 12.83 s | 360/360 | 100% |
| Auto Routed | 73.36% | 73.03% | 73.20% | **87.70%** | **3.92 s** | **70/360** | **19.44%** |

相对于强制 Hybrid，Auto Routed：

- 模型调用减少 **80.56%**（360 → 70）；在 token/单价相同的前提下，模型调用成本代理同等下降。
- 平均端到端延迟减少 **69.47%**（12.83 s → 3.92 s）。
- Fact F1 降低 **3.29 个百分点**（76.49% → 73.20%）。这是明确的成本/质量取舍，而不是“免费优化”。

## 自动路由正确率

Auto 路由准确率为 **100%（360/360）**。本轮的混淆矩阵：

| 预期路径 | 实际路径 |
| --- | --- |
| `rule_only` 260 | `rule_only` 260 |
| `llm_only` 50 | `llm_only` 50 |
| `hybrid` 20 | `hybrid` 20 |
| `reject` 30 | `reject` 30 |

这只说明当前确定性规则完全覆盖了这份程序化语料，不能说明未知自然语言输入上也会达到 100%。路由准确率和事实抽取 F1 是不同指标：路线选对不等于 LLM 一定正确抽取目标、学位或方向。

## 仍暴露的问题

- `硕士`、`Master's`、`master` 等学位同义值尚未统一到 `MS`。
- 英文 `software engineering` 等方向有时未被统一到既有标准方向名。
- 条件性备选偏好目前有字符串与对象两种 LLM value 形态，需要固定 schema。
- 自然语言目标和复杂混合表达仍是最低分区域；强制 Hybrid 的自然目标 Fact F1 为 53.87%，Auto 为 46.99%。
- Auto 的更正样本仍仅由规则处理，因而更正 Fact F1 为 66.67%；若希望提高这一项，应扩展纠正规则或将更复杂的纠正句明确标成 Hybrid。

这些是真实评测缺口，没有删除失败调用、重挑样本或把程序化标签说成 Gold 来掩盖。

## 重跑

先重新生成固定 fixture：

```powershell
python tools/generate_profile_extraction_dataset.py
```

完整三基线与自动路由评测需要 `.env` 中可用的 LLM 配置，并会发送 fixture 中的合成文本：

```powershell
python -m opportunity_agent.v2.evaluation.profile_benchmark `
  --modes rule_only llm_only hybrid auto `
  --workers 6 --timeout 45 `
  --output deliverables/profile_extraction_routed_eval_20261003.json
```

同一 fixture 的已完成结果会自动从 `.records.jsonl` 续跑。修改路径 gate 后，只重跑 Auto：

```powershell
python -m opportunity_agent.v2.evaluation.profile_benchmark `
  --modes auto --refresh-modes auto `
  --output deliverables/profile_extraction_routed_eval_20261003.json
```

完整机器可读报告在 [profile_extraction_routed_eval_20261003.json](../deliverables/profile_extraction_routed_eval_20261003.json)，逐条输出、预期路径和实际路径在同目录的 `.records.jsonl`。
