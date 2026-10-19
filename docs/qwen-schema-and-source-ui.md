# Qwen 结构输出能力与官网面板调整

2026-10-12，针对当前魔搭 API-Inference + Qwen/Qwen3.5-35B-A3B 实测，不修改生产输出模式。

- json_schema 参数下空 facts 测试返回 HTTP 200，不能据此宣称严格 Schema 支持。
- 真实抽取请求返回 HTTP 400：messages 必须出现 JSON 关键词，错误特别提到 json_object，提示当前实现可能按 JSON Object 处理。
- 约束冲突测试：Schema strict=true 且 color 枚举只有 red，提示词要求 blue；服务返回 HTTP 200、color=blue。这证实该组合未强制执行这个 Schema。生产保持 prompt + 字段专属 Pydantic + 一次格式修复 + 独立证据校验。其他 Qwen 型号/部署平台需分别测试，不能用百炼能力替代魔搭能力。

Run 6d16b7bb896c4aedbade7694b0f19da1 中两次抽取均正常返回，分别5.518/5.455秒，日期及GRE正确。How to Apply 与 Apply to MSAII 的官方标题不同；当前有来源的短名/全称绑定要求标题配对或显式本地别名，所以前者未建立模型身份对应，后者成功。这个拒绝不是证明官网页面错误，而是保守的身份绑定/模型身份表示仍有局限；本次没有放宽验证。

UI 调整：官网面板复用 run-diagnostics 视觉风格，两类诊断默认折叠；收起最大360px、展开最大520px，均不超过聊天列可用宽度，展开最高180px并内部滚动。每个页面单独折叠，阶段使用时间线和颜色，保留轮询时的展开状态。PROGRAM_MISMATCH 等改为“提取完成，但未通过事实校验”，不再混淆模型调用失败与验证失败。候选阶段改为“已发现官网候选（字段尚未验证）”。
