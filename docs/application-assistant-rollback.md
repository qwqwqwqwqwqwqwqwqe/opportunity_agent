# 撤回申请助手改造（2026-10-19）

完整申请助手代码已保存到 GitHub `申请助手版`，提交 `15504e1`。
当前 `codex/revert-application-assistant` 在此基础上做选择性撤回，不是回到旧main的全部代码。

已撤回：私有项目库、网址导入、课程整理、项目匹配、日程、To-do、邮件/站内提醒、背景表单、workspace聊天切换及其专用测试和文档。恢复原官网查询聊天流程。

保留：项目身份/适用范围修复、官网搜索、来源面板、A2A及Checker范围隔离、Qwen/ModelScope配置兼容、Luna温度参数兼容、模型连接脱敏诊断，以及Redis宿主机16379端口（避免与EchoMind冲突）。本地.env不改动，现有模型和密钥继续生效。

数据库不降级，不删除数据。0010_application_workspace保留为既有数据库的迁移历史，不注册工作台ORM、不启动工作台接口和Worker。历史导入表/任务留存但不再处理；对话数据不迁移。切回申请助手版可继续访问这些数据。

注意：代码撤回不等于运行中的旧镜像已更新。需要部署时，保留实际Compose组合重建API、Research、Profile、Planning、Worker。不要删除数据库卷，不要执行alembic downgrade。
