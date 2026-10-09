# V2.2 业务修复与运行说明

本次修复统一 Planning 与 Research 的证据准入规则，修复 refresh token 时区和并发轮换问题，为 Run 增加数据库幂等键、原子执行领取、执行租约及事件序号。未完成或依赖旧 Research 版本的计划不再生成可批准提案，批准时再次校验。显式查询目标不能被 Router 的闲聊判断绕过；独立 Profile/Research 可并发，Planning 等待它们完成。执行时间预算覆盖目标解析到答案合成，答案中的外部链接仅保留可用证据中的来源。

## Docker 更新

在项目根目录的 `.env` 中配置私有随机 `JWT_SECRET`（至少 32 字符，不使用示例占位值）。修改密钥会使旧访问令牌失效。然后执行：

```powershell
docker compose up -d --build
docker compose logs --tail 100 api
docker compose ps
```

API 容器启动命令会执行 `python -m alembic upgrade head`，新迁移为 `0006_run_reliability`。现有数据库升级前应备份；这只是 SQL 表结构升级，不导入 `data/conversations.json`。

非 Docker 运行时，在相同数据库配置下先执行：

```powershell
python -m alembic upgrade head
```

PostgreSQL/Redis 的宿主机端口仅绑定回环地址，三个 A2A Agent 仅暴露给 Compose 内部网络。不要额外将无鉴权 A2A 服务发布到公网。

## 恢复与边界

Run 提交后写入数据库队列，由 API 调度器领取。进程意外退出时，运行中的任务在租约到期后重新排队（当前租约 10 分钟）；保留请求身份，重新执行该 Run，而不是从中间节点续跑。旧执行租约不能提交最终结果。恢复可能再次调用外部检索或模型，但不会自动批准业务写入。

本次加入的是基本来源链接检查，不是事实正确性评估 Agent。最终答案仍不是模型逐 token 流式输出。真实模型/生产数据的 Research 质量校准、GPU reranker 与部署环境验证仍需单独验收。
