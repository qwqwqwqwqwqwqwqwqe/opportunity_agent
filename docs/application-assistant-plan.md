# 申请小助手：用户项目库、匹配推荐、日程与申请 To-do

## 目标与数据流

用户填写背景和全部经历 → 按项目分组提交网址／粘贴正文 → 后台生成结构化草稿 → 用户审阅整表并单独确认关键要求 → 本人项目库与检索索引 → 本地问答／推荐 → 用户加入申请 → Planning 生成待确认清单 → 任务打勾、日历和提醒。

默认最多每批10个项目、每项目5个来源，导入并发2；不自动搜索学校、不爬站、不承诺录取概率。首版复用 FastAPI、PostgreSQL/pgvector、Worker、现有 A2A、SSE 和前端，不增加调度服务或前端框架。

## 项目导入和确认

- 项目表包含学校、项目、申请季、学位、授予类型、校区、Home Unit、简介、培养目标、适合背景、学制、学分、Tracks、课程、毕业要求、语言考试及版本、GRE、先修课、GPA、材料及字数单位、deadline和费用。
- 来源按 overview/curriculum/requirements 分类。Tavily Extract basic 获取 Markdown，失败最多再 advanced 一次；无 Tavily 配置走安全直接读取。检查 results/failed_results，不把HTTP200当作完整成功。
- URL只允许公共HTTPS、443，无认证信息，重复校验DNS、重定向和返回来源；不因用户输入而放宽SSRF规则，不跟踪正文链接。
- 正文快照保存来源、获取时间、哈希、方法和失败类型。读取失败可以替换粘贴正文，也可以跳过提取直接手填。
- LLM只生成草稿；培养与申请要求两个字段组，每次最多20,000字符，最多2次正常调用和1次结构修正，单次30秒，总项目处理300秒。模型调用数持久化，Worker恢复不重置；Worker租约恢复最多3次。
- 多页面按份额分配上下文，标注截断；引用未定位、身份差异和作用域问题保留候选与警告，不走旧PROGRAM_SCOPE_CONFLICT循环。未知年份留原文，不推断成目标年份。
- 草稿编辑和模型回写检查版本；异步导入不能覆盖用户新编辑。发布后编辑形成新草稿，不自动覆盖已发布数据。
- 整表审阅后发布，deadline、timezone、language、gre、materials分别确认。未知或未确认项不能产生确定要求或自动闹钟；Tracks和课程不默认完整。
- 字段区分模型草稿、用户确认提取、手工填写；用户确认不等于官方核验。用户修正值优先于旧原文。

## 数据、API与权限

- 新增独立的user_projects、project_sources、project_import_jobs、user_project_chunks、project_tracking、workspace_task_details、calendar_events、event_reminders、workspace_notifications及workspace_receipts。
- 关系库保存字段、版本、确认、任务和提醒；向量保存语义片段。相同URL／正文不共享用户访问权限；先限定所有者、项目、有效发布版本，再进行召回和排名。
- 复用旧Application/ApplicationTask/TaskProgress，通过Tracking关联项目版本与确认要求快照。旧官方资料不自动进入新库。
- 索引失败不撤销已发布项目，可文本降级并重建索引。申请保留选中时的要求快照，项目更新仅提示同步，不重置已完成任务。
- 所有写接口带request_id；修改带expected_version。冲突409；越权404。用户显式表单和勾选直接写入，聊天提出的业务更改继续审批。

主要接口（均以/api/v1开头）：

| 接口 | 用途 |
| --- | --- |
| GET/POST /project-library | 本人项目列表、手动创建 |
| GET/PATCH /project-library/{id} | 草稿与来源快照、版本化编辑 |
| POST /project-library/{id}/confirm | 发布与关键项确认 |
| PUT /project-library/{id}/sources/{source_id} | 失败来源粘贴替换 |
| POST /project-library/{id}/{archive,reextract,reindex,select} | 归档、重新整理、索引、加入申请 |
| POST/GET /project-imports；GET /project-imports/{id} | 批量导入及持久化进度 |
| GET/PUT /workspace/background | 显式背景表单与重复经历 |
| POST /recommendations；POST /project-library/query | 完整画像匹配／本地资料查询 |
| POST /applications | 带project_id时显式选择项目；原格式继续审批 |
| GET /workspace/applications | 申请快照、清单和已有进度 |
| GET/PUT /applications/{id}/checklist | 清单草稿与编辑 |
| POST /applications/{id}/checklist/{generate,confirm} | Planning清单与用户确认 |
| POST /applications/{id}/sync-project | 明确同步要求快照，不改既有进度 |
| POST /tasks/{id}/progress | 准备／提交状态和推荐信数量 |
| GET/POST /calendar-events；PATCH /calendar-events/{id} | 日程及明确提醒 |
| POST /calendar-events/{id}/cancel | 撤销未发送提醒 |
| GET /notifications；POST /notifications/{id}/read | 站内及邮件投递状态 |

## Agent与推荐

- Profile保留表单确认背景；结构化经历与旧经历列表兼容，包含角色、时间、工作、技能和成果。
- Research新增library_qa/project_match，在已确认的本人项目池中执行。课程方向走语义检索；精确字段使用发布快照，未确认要求保持未知。
- 完整画像按偏好、每条新旧经历分拆查询；长经历分块，记录经历ID，不只使用最后一条消息。词法／向量RRF按项目聚合，以单项目最佳片段限制长文档优势；可选cross-encoder对候选重排，失败明确降级。
- 国家、学位筛选走结构化字段；安全可比的成绩做确定性比较，复杂单项／条件要求保持待核对。默认前5个，不足返回实际数量；结果附画像版本、项目版本、经历证据和要求缺口，非录取概率。
- Planning按已确认要求生成可编辑清单，未知要求生成核对任务。打勾属于后端明确写操作，不让Agent猜进度。
- 准备完成和已提交分开；推荐信保留份数、已邀请和已收到。普通完成事项不修改Profile；成绩和经历由表单或聊天确认后更新。
- Checker对工作台结果不再要求旧GRE／deadline查询合同；Router不生成答案，Synthesizer仍负责最终语言组织与现有流式事件。

## 页面与提醒

- 项目库：导入、逐项目进度、完整字段编辑、引用与警告、整表发布和关键项确认。
- 日历：月视图＋节点列表；考试、签证、自定义事项、已确认deadline、用户安排的任务日期。时区需用户确认，默认Asia/Shanghai。
- To-do：按申请项目分卡，清单草稿确认、要求、完成数、准备／提交状态、推荐信份数和时间安排。
- 聊天：原SSE／会话保留，标注“我的项目库”，附项目卡和加入申请操作。
- 一次性站内＋邮件提醒；Worker每30秒检查，以数据库租约、通知唯一键和投递记录恢复。邮件只发账户邮箱、主动勾选，最多3次尝试，失败后1分钟、5分钟重试。
- SMTP未配置时显示不可用，站内仍工作；停机错过的提醒标注延迟。取消事件、更新提醒或任务完成撤销未发送提醒。
- 具体时间使用IANA时区并转换UTC；全天保存日期；缺年日期不生成事件，缺时间不推断23:59。邮件不能保证严格exactly-once。

## 实施与验收

顺序：模型和手动项目 → 清单／日历闭环 → 异步导入与确认 → 索引匹配和聊天 → 提醒 → 文档、迁移与测试。

验收：背景表单 → 三个导入项目（含一个读取失败后手填）→ 用户确认 → 推荐和比较 → 加入申请 → 清单审阅与完成 → 设置提醒。覆盖所有权、旧记录保留、坏网址、跨学位警告、未知年份、版本冲突、导入时用户编辑、索引降级、取消和SMTP失败。

自动化使用可控模型、网页、邮件桩和临时数据库；真实官网、真实模型、实际邮件及浏览器端到端另行验收。旧全量失败不能通过改弱断言掩盖。

部署先备份数据库，在维护窗口停止旧API和Worker、构建镜像、单独迁移到0010，再启动全部服务。保留实际使用的GPU／CPU Compose组合与卷；不得down -v。旧Run保留，旧自主官网查询作为实验能力；APPLICATION_WORKSPACE_ENABLED默认1，设0只恢复旧聊天模式，不自动启用数据迁移。

## 本轮代码验证记录（2026-10-15）

- 新工作台专项：17 passed，含三个项目导入（一个读取失败后手填）、确认、完整经历匹配、追踪、清单、任务完成及设置提醒的后端闭环。
- 相关定向回归：126 passed、2 deselected；明确排除旧历史接口和依赖运行事件的两项测试，不声称全仓通过。
- 新工作台DOM轻量检查、原聊天恢复检查、来源面板检查均通过；Python编译与Compose配置检查通过。
- 临时SQLite验证全新安装、0010降级及已有0009库再次升级；未迁移实际PostgreSQL。
- 未调用真实模型／Tavily／SMTP，没有重新构建或启动用户容器，没有提交Git。完整浏览器交互、GPU吞吐及真实服务集成仍需部署后验收。
- 首版保持单Worker部署，内部两个导入消费者；匹配对已限定所有者的私有项目池做向量／词法排序，适合小规模个人项目库，未宣称大规模ANN性能。
