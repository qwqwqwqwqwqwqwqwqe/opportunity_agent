# 2026-10-09 代码与安全审查记录

本次任务：更新 GitHub README、增加目录树与用户提供的架构参考图、补齐人工复核站用法，并检查公开代码中的凭据和当前缺陷。只修改文档与图片，不实施下列业务修复。

## 范围与方法

- 远程基线：`origin/main` 提交 `5d68ace`；本地与远程基线一致。
- 审查 API 鉴权与 Run 生命周期、Router、汇总与合成、Research、记忆归纳、审核站、MCP 和 Compose。
- 凭据扫描：遍历 `origin/main` 全部可达 Git blobs，而不仅是工作区。基线 2 次提交，254 个唯一 blob；工作区 253 个跟踪文件。
- 规则覆盖常见模型 / Tavily / GitHub / AWS token、JWT 字面量、私钥头和凭据赋值；另将本地配置中 3 个非占位凭据原值与远程 blobs 精确比对。值只在本地内存中使用，没有打印或上传给第三方。
- 规则命中只输出文件位置和类型。两处命中经复核为文档中的 Tavily 占位说明和 dotenv 测试字符串，不是真实凭据。
- 未发现真实凭据候选，也未发现这 3 个本地配置原值的历史匹配；`.env`、私钥文件、真实运行会话及标注 SQLite 未跟踪。
- 仓库存在明确的开发数据库口令、测试密钥和占位符；它们不属于已发现的生产凭据泄漏，但不能当作安全部署配置。

不覆盖其他远程分支、不可达提交、GitHub Actions / Issue / Release / 平台缓存，亦不能识别所有未知格式、编码或拆分后的秘密。不将结果描述为“绝对不存在 key”。本次没有主动攻击在线服务、修改业务数据或向外部模型重放用户历史。

## 发现与代码依据

### P1：Router 的上下文继承不完整

[orchestrator.py](../opportunity_agent/v2/agents/orchestrator.py) 的 `_current_research_route` 用已识别实体、字段与指代规则判断当前请求，命中后清空 Router 历史、摘要与相关偏好，并优先返回规则路由；其他请求只提供最近 6 条原文。

[conversation_context.py](../opportunity_agent/v2/services/conversation_context.py) 最多保留 14 条未归纳消息，超过阈值归纳旧消息并保留 10 条。Router 切为 6 条时，中间省略内容不一定已在摘要中。独立列出项目也不等于不依赖上轮年份、预算或申请周期。

建议：带消息来源的 Context Resolver 与活动任务状态；当前明确实体／字段不可被旧任务改写，缺失条件才继承；歧义澄清。当前系统未实现该通用组件。

### P1：公网部署前缺少防滥用与完整身份边界

- [docker-compose.yml](../docker-compose.yml)：API 使用 `8000:8000`；PostgreSQL 配置公开开发口令。数据库端口已限 loopback，不能因此推断 API 也限 loopback。
- [api/app.py](../opportunity_agent/v2/api/app.py)：注册／登录没有应用级速率限制；Run 仅有每进程最多 4 个执行任务，没有用户级队列配额与全局成本限制。多 API 进程会增加总并发，排队记录仍可增长。
- 启动时只拒绝两种已知 JWT 默认值，没有随机性或最小长度验证。Cookie 默认非 Secure；应通过 HTTPS 并显式启用 `COOKIE_SECURE`，Compose 当前未透传该变量。
- [agents/a2a.py](../opportunity_agent/v2/agents/a2a.py)：当前领域服务没有本项目配置的服务间身份验证；Compose 仅 expose、不映射宿主端口是有效的网络缩小措施，但不等于调用身份已验证。
- [mcp/server.py](../opportunity_agent/v2/mcp/server.py)：stdio MCP 私有工具接受调用方 `user_id`，工具说明要求上层落实身份。目前不是公网 HTTP 接口，也没有发现其自动暴露；不能让不受信客户端直接选择他人身份。

建议：反向代理 HTTPS、限流与配额、密钥强度检查、服务身份、受信 MCP 身份绑定、依赖扫描和公开部署配置。不是已经证明存在任意公网越权漏洞。

### P1：失败诊断和无进展修复不足

[api/app.py](../opportunity_agent/v2/api/app.py) 的 Run 异常路径保存错误并标记 failed，没有完整持久化当时的领域结果和汇总快照。事件能恢复部分路由信息，但不能保证保留每次 MCP 拒绝、字段证据与错误细节。

[orchestrator.py](../opportunity_agent/v2/agents/orchestrator.py) 有轮数／时间预算以及定向缺项修复，但没有基于新增有效证据的明确无进展停止机制，可能重复相似补查。

建议：脱敏阶段快照、机器可读失败分类、前端缺口说明；重复无新增证据时切换查询策略或提前结束。不能因缺少证据就认定官网没有政策。

### P1：偏好澄清和预算能力尚未闭环

[memory.py](../opportunity_agent/v2/services/memory.py) 使用受限偏好键及明确性规则，避免模型自报来源就直写，但自然语言漏识别与不明确偏好的通用澄清还需完善。

[contracts.py](../opportunity_agent/v2/agents/contracts.py) 的成功条件及 Checker 没有完整年度总费用核验闭环。已保存预算偏好不是费用数据完备或硬过滤已完成的证明。

建议：通用候选状态、待澄清槽位、能力声明；年度费用补齐币种、费用范围、年份、来源和未知值，再启用硬筛选。不是简单新增一个金额字段即可解决。

### P2：资源与恢复边界需要验收

- 同步模型调用放在线程中，协程超时不能强制终止已发出的读取；仍需预算、连接超时与取消后的资源回收测试。
- [api/app.py](../opportunity_agent/v2/api/app.py) SSE 使用请求级 DB session 循环轮询，游标从 0 开始，没有使用 `Last-Event-ID`；需验证长连接的连接池占用和断线去重，而不是宣称连接池已经耗尽。
- [repositories.py](../opportunity_agent/v2/repositories.py) 的 Run 租约默认 600 秒，dispatcher 回收过期租约；没有运行中心跳续租。当前 180 秒总预算低于租约，但进程崩溃恢复延迟和以后扩长任务需要测试。
- 会话历史查询没有消息分页，长会话读取与摘要构建需测内存和延迟。

### P2：合成降级后的归纳门槛与计划不一致

[orchestrator.py](../opportunity_agent/v2/agents/orchestrator.py) `_synthesize_with_fallback` 可以返回确定性文本；随后只检查 `completion.status == PASS` 就构建 `ConsolidationInput`。

[memory_consolidator.py](../opportunity_agent/v2/services/memory_consolidator.py) 只检查 PASS 与用户原文，不知道合成是否降级。原文限定和提案审批仍有效，未发现因此把生成答案写成用户偏好；但“模型合成成功才提交归纳”的计划门槛尚未落实。

建议：显式合成状态参与入队条件，覆盖 PASS + 模型失败／降级／提交失败用例。

### P2：人工复核站只适合本机

[research_annotation.py](../opportunity_agent/v2/evaluation/research_annotation.py) `serve` 拒绝非 loopback 地址，这是已有保护；但 `create_app` 的读写接口没有独立身份验证、Host / Origin 校验或请求体大小限制，上传直接读取完整 body。标注者名称是输入字段，不是已认证身份。

建议：仍限本机，不开公网隧道；远程协作前增加访问控制、来源／Host 校验、大小限制、备份和写入并发测试。图片、脚本转义已有测试，不将“没有登录”等同于已证实 XSS。

### 质量边界：不能以链路测试替代人工评测

四路检索和 A2A 测试大量使用受控网页、模型或分数；真实 RAG 需要人工 dev 校准和锁定 test。待审核事实不自动成为可信 SQL 结果是正确的证据边界，不应为了提高命中率批量改成 verified。

当前默认入学年固定为 2027；逐 token 输出、在线评估 Agent 与通用 Context Resolver 未实现。不要将参考架构图中的规划能力当成验收记录。

## 人工复核与数据保护

README 已补离线审核站启动、完整机器结果导入、等级与快捷键、人工修订、三类导出、正式 gold 门槛。没有启动用户真实审核工作区，也没有导入、修改或导出用户人工标签。

提供的架构 PNG 按原文件复制到 `docs/assets/v2-2-reference-architecture.png`，标注为设计参考；保留当前代码 Mermaid 图，两者区别已说明。

## 后续顺序

1. 上下文解析与活动任务状态。
2. 通用偏好澄清、预算能力和费用数据契约。
3. 诊断快照、无进展停止、有效补查。
4. 严格记忆归纳门槛、故障恢复与资源控制。
5. 公开部署前安全基线；若准备公网部署，本项提前作为阻断门槛。
6. 真实模型 / 浏览器 / Docker 及 40×3 输入验收。
7. 人工 gold 检索与答案质量验收。
8. 逐 token 草稿、断线恢复和最终权威答案。

## 验证记录

- 最终针对性离线回归：**114 passed, 1 skipped，19.50 秒**。覆盖 LLM 预标注导出／人工复核、基础鉴权与审批、偏好记忆、合成缓存和路由恢复；不是全量 V2 测试。
- 命令：显式设置 `DOMAIN_AGENT_TRANSPORT=local`，运行 `test_v2_research_llm_review.py`、`test_v2_research_llm_export.py`、`test_v2_foundation.py`、`test_v2_memory.py`、`test_v2_synthesis_cache.py`、`test_v2_router_recovery.py`；临时目录 `.audit-readme-20261009-c`。
- 首次受限运行停滞后中止，不记通过；常规权限首次运行是 113 passed / 1 failed / 1 skipped。失败项 `test_orchestrator_produces_proposal_not_direct_write` 未注入执行器，会读取默认 A2A 配置；显式 local 重跑后通过。该测试环境依赖已列为待修复问题，没有隐藏首轮失败。
- 跳过项需要显式配置独立 PostgreSQL 测试连接；本次未使用用户业务库代替。
- README／审查记录／V1 归档的本地链接、代码块、发布路径通过检查；Mermaid 独立源码与内嵌版本一致。未做 Mermaid 截图渲染验收。
- PNG 与用户提供的文件逐字节哈希一致，未修改原图。CLI `--help` 和 Markdown 阅读组件的 Node 语法检查通过。
- 没有执行真实外部模型质量测试、真实浏览器端到端验收或在线渗透测试；没有修改人工标签。
