# Research 身份与作用域修复报告（2026-10-12）

## 交付状态

已修改本地代码并完成定向测试。未修改模型、API URL 或密钥；未部署现有 Docker 服务，未迁移现有 PostgreSQL，未迁移 `data/conversations.json`。保留本次开始前的用户改动。

## 核心变更

- `v2/research/extraction.py`：提取/验证版本为 `semantic-admissions-6`。新增 `ProgramIdentityClaim`、身份引用与事实适用范围；项目主体、学位、授予类型、Home Unit 分开验证。不再依赖标题分隔符或缩写必须出现在标题前半段。别名必须在当前官方来源中有支持，不能仅凭域名或首字母相同接受。
- 保留正文块、标题祖先与原文偏移。模型可选择 `quote_ref` 和 `applicability_ref`，程序绑定保存的原文；引用伪造、兄弟栏目借用和交叉学位仍拒绝。MS/PhD 比较句按事实适用子句验证，不因外围提及另一学位而整批拒绝。
- Home Unit 可从同页、同项目且同学位的明确声明独立定位；不要求模型引用的 GRE 句同时包含单位。相邻声明存在不代表可以跨学位借用。
- `service.py`：按已验证作用域分组，保留合格字段和无年份日期，不因同页多项目而清空整批。身份引用不足与明确项目冲突分开处理；同页身份修正与格式纠错共享一次额外调用配额。分块筛选、多字段一次提取、版本化缓存保持有效。
- `identity.py`、Agent 契约、汇总和缺失任务：使用学校、项目主体、学位/授予类型、Home Unit、申请季作用域键；旧消息默认值兼容。不同范围不合并，同一项目多个学位不冒充多个项目。
- `fact_store.py`、`catalog.py`、`quality.py`、入库 worker：缓存/SQL 回读使用新作用域并重新验证来源。旧提取版本不能满足新的具体范围约束。SQL 事实提交与可选 RAG 入库分离，避免冷启动向量模型失败回滚已验证事实；整页相关性不等同于整页全部字段已验证。
- `tool_ledger.py`、`tool_loop.py`、失败分类和前端：预算原子记账，区分身份证据不足、范围冲突、格式错误和年份未知，保留旧错误码的展示兼容。
- 新增迁移 `0009_research_scope_identity`：扩展项目目录作用域及唯一约束；旧记录范围为空，不自动推断、删除或混并。若降级造成旧唯一键冲突，明确拒绝降级。

## 测试与固定 Fixture 指标

回归命令：

```powershell
.\.venv-research\Scripts\python.exe -m pytest tests/test_v2_identity_scope.py tests/test_v2_semantic_extraction.py tests/test_v2_research.py tests/test_v2_research_routing_repairs.py tests/test_v2_research_multi_target.py tests/test_v2_research_temporal.py tests/test_v2_research_budget_repairs.py tests/test_v2_synthesis_cache.py -q --disable-warnings --tb=short
node --test tests/ui_source_panel.test.js
```

结果：Python **216 passed, 1 skipped, 3 warnings，27.89 秒**；前端 **1 项通过**。

覆盖标题顺序、别名来源、学位比较句、Home Unit 隔离、邻接引用与兄弟栏目、伪造引用、年份未知、跨轮汇总、旧契约、临时 SQLite 迁移、缓存、共享修正预算和失败保留已有字段。

`tests/fixtures/research_identity_scope.json` 使用四个 CMU/GT 官网页面的转录摘录及明确标注的构造模型响应，不是完整网页归档，也不是历史 Run 原始模型响应重放。

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 有效候选事实接受数 | 2/8 | 8/8 |
| 被接受字段正确率 | 2/2（100%） | 8/8（100%） |
| 来源事实覆盖率 | 25% | 100% |
| 有效身份误拒绝率 | 75% | 0% |
| MSCS 错归 CSE 污染 | 0/2 | 0/2 |

接受的八条中，两条是“年份未知”的原文日期，不是已核实的 2027 Fall 截止日期；其余六条包括 CMU 两页的重复事实。这些指标仅测固定小样本上的确定性验证器，不能声称跨学校真实 LLM 准确率达到 100%。

## 真实模型抽样

沿用魔搭 `Qwen/Qwen3.5-35B-A3B` 快速提取模型；只发送公开页面摘录，关闭事实持久化与入库队列。以下是抽样，不是在线完整搜索/回答流程测试。

- CMU `how-apply`：deadline `2026-12-09`、GRE `required` 均验证成功，一次调用，8.719 秒。
- CMU `apply-msaii-program`：相同字段均验证成功，一次调用，9.125 秒。
- 最后一次 GT CSE 抽样：一次调用，15.031 秒；输入来源正文 530 字符，筛选上下文 1201 字符（含引用选项），不是整个网站。硕士 GRE `required` 验证通过；硕士 `February 1`、博士 `December 15` 保留为年份未知；两组均正确绑定 `School of CSE`。
- GT 博士 GRE：模型值输出 `optional`，但其 `gre_required` / `gre_considered` 组合与 `optional` 不一致，仍被 `inconsistent_gre_semantics` 拒绝。此时已经不是项目身份误拒绝；未通过放宽验证掩盖它。

样本少且模型输出有波动，不报告有统计意义的成功率或 P50/P95，也不把较早调试版本的 GT 成功样本算作最终版本全部通过。

## 剩余限制与后续工作

1. 对值枚举与布尔语义不一致，后续应在已有共享纠错配额内做定向修正；当前身份修复不能解决所有 `inconsistent_gre_semantics` 输出。不可直接把所有 optional 候选当作 verified。
2. 官方页面未写年份时保留日期，但不推断今年或目标申请季；回答需要明确未确认范围。跨页面申请季绑定属于后续阶段。
3. 复杂共享政策、单位简称与全称关联仍可能保守拒绝。当前样本仅两个学校，不能代表所有网页结构、PDF 和学校泛化效果。
4. 扩展测试中 `test_v2_foundation.py::test_orchestrator_produces_proposal_not_direct_write` 曾失败；尚未定位，不声称全仓测试通过。上述 216 项 Research 定向回归已通过。
5. 未执行 PostgreSQL 实库迁移、浏览器完整对话及线上模型端到端验收；部署后还需验证官方来源面板、分组回答、缓存回读和多轮补查。

## 部署前注意

代码包含新数据库列，不能只重启旧容器就认为生效。先备份数据库，使用当前实际运行所用的 Compose 文件组合重建 API、Research、worker，执行 `0009` 迁移，再一起更新三个服务。不得删除数据库卷或使用 `down -v`。

仅基础 Compose 的示例（本次未执行）：

```powershell
docker compose build api research worker
docker compose run --rm api python -m alembic upgrade head
docker compose up -d --no-deps api research worker
docker compose ps
docker compose logs --tail 100 api research worker
```

如果当前使用了 `docker-compose.research.yml`，在以上每条命令的 `docker compose` 后保留原来的 `-f docker-compose.yml -f docker-compose.research.yml` 参数；不要为本次修复临时切换 GPU/CPU 或丢失模型缓存挂载。迁移脚本已在临时 SQLite 测试，实际 PostgreSQL 迁移仍需独立确认。
