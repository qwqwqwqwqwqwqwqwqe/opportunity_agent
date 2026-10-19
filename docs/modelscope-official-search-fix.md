# 魔搭配置与真实官网搜索修复

## 原因

旧 OfficialWeb.search 在没有搜索服务结果时，把子域名和常见路径组合并打分，返回的 `official URL candidate` 不是已发现的网页。CMU MSAII 实测曾选择不存在的 `/education/graduate-admissions`，HTTP 404 又被压成 TOOL_FAILURE，浪费补查预算。

此外，本地 .env 与容器模型不一致：研究容器仍请求旧 GPT 型号；DeepSeek URL 指向平台网页而非 API。官方 DeepSeek 模型请求实测 402 余额不足。

## 修改

- 新增 `research/official_search.py`。现有 `search_official_pages` 工具调用真实 Tavily 索引，发送 include_domains 官方域名限制，程序再校验 HTTPS、域名、凭据和端口。搜索摘要仅是发现提示，不作为已核实事实。
- OfficialWeb 优先搜索结果；缺少索引结果时只保留 sitemap/实际发现链接，不返回拼接的申请路径。不合并不同 host 上的同名路径。404/410 明确记录并优先切换下一个候选，避免依赖另一次 LLM 判断。
- 不更换 Agent 框架，不放宽原始 Evidence Verifier，不新增数据库迁移。
- 本地配置选用魔搭 API-Inference `https://api-inference.modelscope.cn/v1`。全局/回答模型 `Qwen/Qwen3.5-122B-A10B`；Research 快速模型 `Qwen/Qwen3.5-35B-A3B`。型号在该账户的 /models 列表存在，但尚不能称生成验收通过。
- 新增 LLM_PROVIDER=modelscope，严格只使用 MODELSCOPE_API_KEY；不将遗留的其他平台 LLM_API_KEY 发给魔搭。Compose 给所有业务容器传递供应商和结构输出模式。默认本次配置为 prompt + Pydantic 校验，不冒充原生 strict Schema。
- 不持久化或输出真实 key，沿用本机已有 MODELSCOPE_API_KEY 环境变量。其他平台 key 未删除，但新供应商模式不会使用它。

## 验证

新官网索引实测返回 8 个 CMU 官方候选，第一个是 `https://msaii.cs.cmu.edu/apply-msaii-program`；读取成功，正文 3187 字符，包含 December 9, 2026 与 GRE is required。此检查仅证明发现和读取成功，不能代替字段级 Pydantic 提取和证据验证。

相关回归首批 131 passed / 1 failed / 2 skipped；唯一失败为已有 CSE 全称被 search_scope.py 误判为其他项目。新增官方搜索和原 LLM 配置测试最终 13 passed。新增测试含：索引返回链接、域名/凭据/端口拒绝、空搜索不编造路径、不同 host 同名路径保留、魔搭 token 隔离、404 分类、模型 403 不误认为官网读取失败。

## 当前外部阻塞

魔搭 /models 返回 200，但生成请求返回 403：账户尚未完成邮箱验证，API 明确要求访问 `https://modelscope.cn/my/settings/account` 完善邮箱。已停止继续调用生成 API；尚不能测量这些模型的抽取正确率或延迟，也不能宣称完整问题已成功回答。邮箱验证后需重新做 CMU MSAII 2027 Fall 端到端测试，保持项目身份和证据门槛。

魔搭 API-Inference 是用户要求的免费额度入口，受账户验证及平台额度限制；Tavily 搜索使用已有 key 和 basic 搜索，不承诺搜索服务无额度消耗。官方入口参考：[ModelScope API-Inference](https://www.modelscope.cn/docs/model-service/API-Inference/intro)、[Tavily SDK 官方实现](https://github.com/tavily-ai/tavily-python/blob/master/tavily/tavily.py)。
