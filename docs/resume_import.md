# 简历导入：使用与维护

## 启用

在 examples/opportunity_agent 目录执行：

~~~powershell
python -m pip install -e ".[dev,resume]"
python -m opportunity_agent.web_app
~~~

基础安装仍只需要 Pydantic。没有安装 resume extra 时，CLI、聊天、手动填写资料表仍能运行。新增的 olefile 是轻量 DOC 结构校验依赖，用于识别旧 Word、密码保护和宏，不是本地 OCR。

在现有项目 .env 里设置（不要覆盖原文件）：

~~~dotenv
LLM_API_KEY=your-existing-key
LLM_API_BASE=https://yibuapi.com/v1
LLM_MODEL=gpt-5.5

# 仅增强扫描件 / 旧 DOC 时需要，与 LLM Key 不同。
MINERU_API_TOKEN=your-mineru-token
RESUME_PARSE_TIMEOUT_SECONDS=120
RESUME_LLM_TIMEOUT_SECONDS=120
RESUME_LLM_MAX_TOKENS=2600
~~~

不要把占位符当成真实 Key。修改后重启服务；PowerShell 同名环境变量优先。MinerU 未配置不影响普通 PDF/DOCX 本地读取。

## 用户流程

1. 新对话信息表点击“先导入现有简历”，或“编辑资料”旁点击“导入简历”；也可拖到导入区域，无需先填写必填项。
2. 上传每次一个、最大 10 MB 的 PDF/DOC/DOCX。进度显示读取、增强识别、AI 抽取、等待确认。
3. 普通 PDF 使用 pdfplumber，DOCX 使用 python-docx 按段落/表格顺序读取。扫描或低质量结果、带文本框 DOCX、旧 DOC 会请求单独的云端授权。没有授权不会上传原件到 MinerU。
4. 在右侧逐项核对“基础资料、申请目标、经历与项目”。展开证据可看原句和页码/段落。冲突值默认不勾选；当前值并列展示，用户明确选择后才采纳。
5. “仅保存资料”不请求规划 LLM；“确认并生成规划”先提交画像，再走现有首次时间轴/文章生成或手动重规划入口。缺少申请目标时转到预填资料表补齐。
6. 科研、项目、实习、竞赛、论文分别编辑。每条保留职责、技术、日期与成果，句内逗号不会切碎经历。导入的成果不会自动标记已有任务完成。

预算和考试安排使用普通表单输入。清空字段或取消勾选意味着不导入该字段，不是删除正式画像；已确认信息可继续通过“编辑资料”修改。

## 状态与数据边界

- data/conversations.json 仍是正式画像、聊天和进度的权威来源。
- data/resume_imports.json 保存独立导入任务、待确认草稿、编辑版本、原始结构化结果、证据和已完成的 AI 分段标记；不放入 localStorage。
- .resume_tmp/<随机ID>.<扩展名> 保存本机临时原件。解析完成后、取消或失败时删除。等待云端授权最多 30 分钟，后台每 30 秒检查，并在读取任务时检查过期。
- 本地读取在隔离子进程执行，超过解析时限可终止；AI 抽取默认预算 120 秒、默认输出上限 2600 tokens。多块或较长简历会拆成资料与经历短请求：基础资料最多 650 tokens、单个经历片段最多 800 tokens，单片最多等待 20 秒。每个成功分段会立即写入草稿并显示在右侧栏；任一分段未完成时保留已抽取内容。点击“补全未完成的 AI 提取”会跳过已完成分段和已保存的用户修改，只请求剩余分段；粘贴替换文本会按新来源重新抽取。
- 正常停止服务会清理未完成任务和待授权的原件。操作系统强制结束进程或断电时无法执行即时清理，下次启动/查询任务时再恢复并清理。
- 全局两个处理 worker，每个会话一个活动导入；同会话同上传 request_id 幂等。
- 页面轮询恢复任务，用户可切换对话；刷新、不同浏览器连接同一服务均读取同一任务。
- 确认前不修改画像、进度或文章。保存/确认检查草稿与正式会话版本；发现另一个窗口有修改时需重新核对，不能直接覆盖。
- 服务重启后，完成草稿仍可编辑；未完成任务标记“中断”。如已有解析文本，可以重试；否则重新上传。
- 确认后删除已解析的全文，只留下结构化资料、原始候选值和证据片段。删除导入记录保留最小状态墓碑用于幂等，不撤销已确认事实。
- 删除整个会话会取消其导入，清理原件和草稿，迟到结果不能恢复会话。
- 正式事实通过现有 Normalizer、ConflictResolver 与审计写入。确认后 source 为 user_explicit，证据保留简历 ID、原始抽取置信度及定位；原始 resume 候选保存在导入记录中。未调整全局来源优先级。

## 隐私与已知限制

提取文本会发送给当前配置的 LLM 服务。发送前过滤常见邮箱、手机号、证件号码；这不是完全匿名化。原件、密钥、全文不进入普通日志或浏览器持久缓存；本机服务端 JSON 仍可能含个人信息，应保存在受保护目录，不应提交 Git。应用没有账号体系，继续只绑定 127.0.0.1。

授权 MinerU 时整份文件交给第三方处理。本机删除不表示云端副本已删除，第三方留存遵循其政策；本应用不承诺云端删除时限。未配置 Token、用户拒绝或增强失败时保留能读取的文本，允许粘贴/手工补充，不报告虚假的解析成功。

PDF、MinerU 返回结果检查 20 页上限。DOCX 没有可靠的本地分页引擎，只能检查文件保存的 Pages 元数据、文本/解压大小；缺失或过时的元数据不能保证真实排版页数。无渲染器的 DOCX 块使用段落/表格位置，不伪造页码。特殊双栏、复杂文本框可能需要增强识别和人工检查。

不会推断目标国家、梦校、预算、缺失 GPA 量表或申请意愿；不会访问简历链接、执行文档指令或调用简历中指定的工具。抽取置信度表示“读取是否准确”，不代表核实了成绩或成果真实性。专业规划仍限制在计算机与电子信息类工科。

## 代码与接口

- resume_models.py：文件块、独立草稿、分类经历及导入状态。
- resume_parsers.py / resume_worker.py：格式验证、过滤联系信息、本地隔离读取。
- resume_mineru.py：签名上传 → 轮询 → 安全读取结构化 ZIP；不要求公开文件链接，不向存储域名转发 Bearer Token。当前读取 ZIP 中的 `*_content_list.json`：逐项取 `page_idx`、`text`（无 `text` 时取 `table_body`），转成本地 `ResumeBlock`，保留页码和块定位。MinerU 返回的是版面/OCR 文字块，不是“课程、科研、实习”等业务表格；随后由本地 AI 抽取器依据这些文字块及证据位置填入审核表格。
- resume_extraction.py：项目内 ResumeExtractionSkill，复用 LLMClient.generate_structured，严格证据定位、规则 fallback。
- resume_store.py / resume_service.py：JSON 原子写入、锁、后台调度、授权、草稿版本与清理。
- resume_http.py：现有 HTTP 服务的上传/轮询/编辑/确认适配。
- SessionService.confirm_resume / LifecycleAgent.on_resume_confirmation：复用画像、事实审计及现有规划入口。
- web/resume_ui.js / web/resume.css：上传、拖放、侧栏审核、证据、自动保存及表单兼容。

接口均按 session_id 隔离：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/resume/imports | multipart：file、session_id、request_id，可选 enhanced |
| GET | /api/resume/imports?session_id=… | 恢复任务列表 |
| GET | /api/resume/imports/{id}?session_id=… | 进度和草稿 |
| POST | /api/resume/imports/{id}/cloud-consent | session_id、accept |
| POST | /api/resume/imports/{id}/draft | session_id、revision、profile_revision、draft |
| POST | /api/resume/imports/{id}/confirm | session_id、revision、generate_plan |
| POST | /api/resume/imports/{id}/retry | session_id，可选 text，用现有文本重试或粘贴替代 |
| DELETE | /api/resume/imports/{id}?session_id=… | 取消/删除导入记录 |

## 验证

~~~powershell
python -B -m pytest
# Playwright 为开发验收工具，不增加应用运行时依赖：
node tests/ui_resume_smoke.cjs
node tests/ui_progress_smoke.cjs
~~~

文件测试使用合成中文/英文 PDF、双栏、空文字扫描页、DOCX 段落/表格；加密 PDF 拒绝分支和 MinerU HTTP 协议包含 mock。浏览器测试实际操作上传、审核、刷新、多浏览器、授权和小屏滚动；其中抽取与云端响应是 mock，不宣称在线验证。

真实 API 测试默认关闭。明确启用后才发送合成简历，不读取用户真实简历：

~~~powershell
# 测试默认禁用 .env 自动读取，需把用于测试的 Key 设置到当前进程环境。
$env:RUN_RESUME_LLM_INTEGRATION="1"
python -B -m pytest tests/test_resume_mineru.py::test_real_resume_llm_synthetic_only -q

$env:RUN_MINERU_INTEGRATION="1"
python -B -m pytest tests/test_resume_mineru.py::test_real_mineru_synthetic_file_only -q
~~~

测试后清除这两个 RUN_* 标记，避免下一次无意启用线上调用。本轮没有运行真实 MinerU/LLM 测试。

## 选型依据

- [pdfplumber 官方项目](https://github.com/jsvine/pdfplumber)：用于带文本层 PDF 的读取与定位。
- [python-docx 官方文档](https://python-docx.readthedocs.io/en/latest/)：DOCX 段落与表格读取。
- [MinerU 精准解析 API](https://mineru.net/apiManage/docs)：签名上传与批次结果接口，使用独立 Token。
- [Docling 官方 Agent Skills](https://docling-project.github.io/docling/usage/agent_skills/)：可以指导转换，但需要另装 Docling 运行库。本轮不安装该 Skill、Docling 或本地 OCR，项目内 ResumeExtractionSkill 是业务抽取组件，不是 Codex 插件。
