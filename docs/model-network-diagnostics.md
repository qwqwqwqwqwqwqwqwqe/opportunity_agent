# 模型网络错误诊断

> 2026-10-19：申请助手导入功能已撤回。下文导入相关段落为历史诊断记录；当前仅通用模型传输日志及Luna温度兼容继续生效，不再产生workspace_import_outcome。

## Luna导入兼容（2026-10-17）

实际网关对 gpt-6-luna 的 temperature=0 返回400 unsupported_value。统一请求层对该精确模型名省略temperature（普通、结构化和流式均适用），其它模型保持原参数，不靠额外失败请求来探测能力。CMU课程页实测还出现4000个completion tokens全部为reasoning、content为空且finish_reason=length，因此导入提取在未显式设置推理档时对Luna默认low。历史失败提示在新整理时清除；本次输出为空/非法与超时单独分类，不再都显示网络错误。

模型请求失败输出 JSON 到容器标准错误，使用 `docker compose logs --since 30m worker api research` 查看。
导入阶段另记录 `workspace_import_outcome`，包含 job_id、project_id、可读来源数和模型调用次数；脱敏中文原因同时保存到项目草稿 warnings，供刷新后查看。容器日志在删除容器后不保证保留，需长期留存时由部署端集中采集，勿采集认证头和正文。

错误码：PROXY_AUTH_REQUIRED（代理407）、DNS_RESOLUTION_FAILED（域名解析）、CONNECTION_REFUSED（连接拒绝）、NETWORK_UNREACHABLE、NETWORK_TIMEOUT、TLS_CERTIFICATE_VERIFY_FAILED、TLS_UNEXPECTED_EOF、TLS_HANDSHAKE_FAILED、HTTP_状态码。

日志字段包含 destination_host、proxy_configured、proxy_host、layer、certainty、elapsed_ms、tls_profile。只记录主机，不记录URL路径、查询参数、代理密码、API密钥、模型正文或用户材料。

certainty=observed 表示观察到该故障，不意味着已找到最终责任方。特别是TLS EOF无法仅凭客户端判定服务端、代理或链路，应保持undetermined。

2026-10-17 Waterloo导入：正文6172字符读取成功，2次模型整理URLError。Worker无环境代理；无认证GET探测默认TLS、TLS1.2及显式Clash host.docker.internal:7897均出现SSL EOF。没有证据支持关闭证书验证，也未自动换服务商或模型。下一步需对照宿主机同目标连接、Clash连接日志和服务商状态进一步定位；代理存在不等于代理有故障。探测结果是当时状态，不保证持续不变。

现有TLS1.2兼容重试保留调用者的禁用选项（导入等有界流程默认禁用），重试仅限握手EOF、共享原时间预算，不放宽证书验证，不重试普通超时。历史草稿中的旧URLError记录不会自动改写；新导入/重新整理显示细分原因。
