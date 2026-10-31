# PostgreSQL 多进程并发与故障验证

## 使用方法

在项目目录、Docker Desktop 已启动的 PowerShell 中执行。宿主机只需 Python 3.12，执行器只用标准库；依赖在 Docker 镜像内安装。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_concurrency.ps1 -Action Start
powershell -ExecutionPolicy Bypass -File .\scripts\run_concurrency.ps1 -Action Test
```

默认连续 10 轮，每轮 11 个场景；第一次失败立即停止，不自动重试。`-Rounds 1` 仅用于冒烟检查，不算十轮验收。
首次构建需要下载镜像与依赖。18081、18082 端口必须空闲。

专用项目名 `applyops-concurrency`，数据库 `applyops_concurrency`，不读取 `.env` 中的业务 DATABASE_URL 或模型密钥。
Compose 仅使用自己的数据库卷，无宿主机数据库端口；测试 API 只绑定本机。迁移独立运行一次，成功后启动两个 API 与两个 Worker。
固定测试密码与 JWT 仅供隔离测试，不能用于公开部署。

测试启动入口挂载在 `/harness`，不打进生产镜像；通过依赖注入使用固定网页和模型替身，生产抓取安全规则不变。
Worker 默认空闲，由测试控制文件启用；测试分析通过真实工具、证据校验和提交逻辑，不代表真实模型效果。

## 场景与断言

* 双 API：20 个相同请求、不同内容重用幂等键、相同版本竞争修改、活动检查复用、跨用户读取与关联拒绝。
* 聊天：A 创建运行、B 查询数据库持久化 SSE，A 按 Last-Event-ID 恢复；仅一个回答和一个审批。
* Worker：领取后 kill、分析中 kill、提交前 pause 后接管、提交前 kill、分析提交后 kill；不会出现重复通知。
* 输入失效：暂停监控、改变周期拒绝旧分析；修改个人成绩后，旧分析不能提交，重试使用当前资料。
* 调度与重试：双调度器将停机遗漏合并一次；持续获取失败三次终止，临时失败两次后恢复。

故障注入使用真实 Docker SIGKILL / pause / unpause，不以异常模拟进程崩溃。测试租约 8 秒、续约间隔 2 秒；生产 240/30 秒不变。
控制屏障位于事务外，避免测试用长事务阻塞接管。测试专用 SQL 清除手动冷却时间，不新增调试 HTTP 接口。
测试执行器有界等待检查状态，并直接查询回执、版本、任务、分析与通知，不只检查返回码。

## 报告与排错

输出 `data/concurrency-reports/<时间>/`：report.json、容器状态、服务日志和任务状态。报告记录代码版本、脏工作区标识、PostgreSQL 版本、每个场景耗时与首次失败。
只包含人工测试用户，不要用这个执行器连接生产数据库。真实模型质量与高 QPS 不属于本次验收。
如失败时有 Worker 仍暂停，可执行：

```powershell
docker compose -p applyops-concurrency -f compose.concurrency.yml unpause worker1
docker compose -p applyops-concurrency -f compose.concurrency.yml logs --tail 200
```

停止并移除本测试项目容器及网络，但保留其数据库，方便排查：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_concurrency.ps1 -Action Stop
```

不要对业务 Compose 执行 `down -v`。本工具不自动删除任何数据库卷；测试数据每轮使用新用户，重复运行会累积数据。

## 当前验证状态

2026-10-27 已找到用户目录安装的 Docker CLI 并完成真实验收：报告 `data/concurrency-reports/20261027-131843/report.json`，10 轮、110 项、全部 passed，error=null。环境为 Docker Engine 29.8.1 / PostgreSQL 16.15。测试容器与网络已停止移除，专用数据库卷和报告保留，未触及业务服务。
首轮尝试在第 3 轮因 Windows 控制文件替换及 GBK 日志解码错误停止，原始失败报告保留于 `20261027-131438/`；只对控制文件传输做有界重试并修复 UTF-8 解码后，重新从第一轮执行完整 10 轮，没有自动重跑失败断言。
三项独立 PostgreSQL 并发测试也已实际执行，3 passed。修复了旧测试 search_path 对 public 已有表的误发现，改用 schema_translate_map 显式隔离；仅在专用测试库内进行排查。
本地 SQLite 回归已验证聊天令牌隔离、官网监控和画像助手：第一组 58 passed；第二组 72 passed / 3 skipped（其中两项令牌测试重复覆盖，不能直接相加作为独立用例数）。Python 编译、YAML 解析与 git diff --check 通过。未运行 Docker Compose 配置验证和真实多进程测试。

已修复：流式事件写入校验执行令牌与租约；最终业务副作用提交前锁定运行行并校验所有权，防止接管后的旧执行者继续写入。
测试注入接口为内部可选 Python checkpoint 参数，默认关闭，不增加 HTTP API 或数据迁移。
