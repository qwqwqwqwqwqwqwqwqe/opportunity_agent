# Personal Opportunity Awareness Agent — V1

这是一个独立 Python Demo，覆盖“渐进画像 → 个性化路线图 → 信息更新 → 路线图重排 → 求职岗位推荐”。默认可离线运行；配置 OpenAI 兼容服务后使用模型补充自然语言画像并优化规划文章。

## 运行

### 导入现有简历

新对话可直接导入 PDF/DOC/DOCX，无需先填写资料表；右侧核对和编辑抽取草稿后，选择“仅保存资料”或“确认并生成规划”。未确认内容不会改变正式画像或任务进度。

```powershell
python -m pip install -e ".[dev,resume]"
python -m opportunity_agent.opportunity_a2a
```

再打开另一个 PowerShell 启动 Web 与 openJiuwen ReAct 聊天 Agent：

```powershell
python -m opportunity_agent.web_app
```

普通 PDF/DOCX 本地读取，画像抽取复用现有 `LLM_API_KEY`。扫描件与旧 DOC 需用户单独授权，并在项目 `.env` 配置独立的 `MINERU_API_TOKEN`；未配置仍可使用本地文本或手工填写。原件解析后本机删除，等待云端授权最长保留 30 分钟；本机删除不代表第三方副本已删除。

配置、接口、存储边界和测试说明见 [简历导入文档](docs/resume_import.md)。以下原有 CLI/手工填表方式无需安装 resume 可选依赖。

```powershell
python -m pip install -e ".[dev]"
python -m opportunity_agent.main
python -m opportunity_agent.main --demo
python -m pytest
```

启动 Web 界面：

```powershell
$env:LLM_API_KEY = "<your-sk-token>"
$env:LLM_API_BASE = "https://yibuapi.com/v1"
$env:LLM_MODEL = "gpt-5.5"
# Optional: background article-generation limits. Defaults are 210 seconds / 2400 tokens.
$env:ROADMAP_LLM_TIMEOUT_SECONDS = "210"
$env:ROADMAP_ARTICLE_MAX_TOKENS = "2400"
python -m opportunity_agent.web_app
```

也可以只配置一次项目根目录的 `.env`。先复制示例文件，再编辑 `.env` 中的 `LLM_API_KEY`；不要修改或提交 `.env.example` 中的占位符：

```powershell
Copy-Item .env.example .env
notepad .env
python -m opportunity_agent.web_app
```

`.env` 会在每次启动服务时读取，PowerShell 中的同名变量优先于 `.env`。主变量 `LLM_API_KEY` 优先于旧兼容变量 `MODELSCOPE_API_KEY`，避免历史配置覆盖新 Key。修改 `.env` 后需停止并重启服务。打开 <http://127.0.0.1:8766>。新对话先填写工科申请信息表，提交后会生成可点击的自适应时间轴；侧栏“编辑资料”会更新画像和确定性时间轴，已有文章保留，点击“重新规划”才重新生成文章。不要把真实 Token 写进 README、`.env.example` 或提交到 Git。

Web 界面的会话主数据保存在服务端的 `data/conversations.json`：同一台电脑上、连接同一个 `http://127.0.0.1:8766` 服务的不同浏览器，会在打开、刷新或重新聚焦页面时看到同一组对话。浏览器 `localStorage` 仅保留离线缓存，并会在首次打开新版页面时迁移旧数据；左侧对话条目悬浮后可删除，删除会在其他浏览器刷新或重新聚焦后同步。该文件包含画像和聊天内容且没有登录鉴权，请只绑定本机地址，不要把服务暴露到局域网或公网。

### 官网查询 Tool Calling（可选）

为 GPT 规划或咨询增加学校官网证据时，在 `.env` 配置 Tavily；不配置时原有离线规划仍可运行，不会发出搜索请求：

```env
TAVILY_API_KEY=tvly-your-key-here
OFFICIAL_SEARCH_ENABLED=1
OFFICIAL_CACHE_TTL_HOURS=168
OFFICIAL_TOOL_MAX_CALLS=6
OFFICIAL_RESEARCH_TIMEOUT_SECONDS=60
```

模型只能从本地已验证学校域名表中选择域名，再通过 Tavily 在该域名内搜索并读取公开页面。页面内容、搜索摘要和社区内容都不能直接修改画像或时间轴。路线图下方会显示引用卡片；“重新查询官网”只更新证据，随后由用户决定是否“重新规划”。未找到或没有明确写出的要求仍显示待核验，不会被模型猜成“不要求”。

常见画像信息会走确定性规则快速路径；规则无法识别的自由表达由模型补充。表单提交先立即生成确定性时间轴，页面解除锁定后再异步执行一次规划文章优化；AI 超时不会挡住对话切换。路线图文章规则保存在标准 `opportunity_agent/skills/roadmap_article/SKILL.md` 中，仅在首次生成或手动重规划时渐进加载；模型按六节结构化 JSON 生成，局部章节不完整时只修复相关章节。课程、语言、暑研、实习、网申和签证仍由独立 Planning Skill 生成，日期边界不能被模型改动。已有语言成绩时先核验目标项目官网要求，不默认重考。

详细规划只支持计算机、AI、电子信息/通信、自动化/控制及紧密相关方向。其他专业的画像仍会保存，但界面会明确提示当前版本不支持，并且不会让模型强行生成跨学科时间轴。

当画像进入 `internship_search` 或 `full_time_search` 后，Agent 读取 `data/jobs.json`，按技能、方向、地点和阶段排序，返回来源、匹配原因与置信度。岗位数据为本地模拟数据，不代表实时招聘信息。

## 进展感知与时间轴状态

- “我开始做 LLM 科研”“项目完成了”可更新唯一关联的任务；无法确定目标时先确认。
- 任务卡提供开始、完成、延期、取消、重置。日期位置、执行进度和到期风险分开显示；过期不代表完成。
- “托福 105 分够吗？”只咨询；“我托福考了 105，接下来怎么办？”先记录成绩，再回答问题。成绩不等于满足项目要求，也不会取消下一场考试。
- 普通进展不调用规划模型；首次表单仍可后台生成文章，之后由用户手动重新规划。文章生成前会传入当前执行状态，生成后按稳定任务身份恢复进度。
- 消息在模型调用前保存。请求去重、版本校验和删除标记防止重复更新、迟到结果覆盖和旧缓存恢复已删除会话。

已有会话首次写入新版格式前自动备份为 `data/conversations.v1.bak`；旧消息没有时间戳时保持未知。进展按对话隔离，仅支持单进程、本机单服务，不支持多个服务进程同时写同一个文件。

架构、接口、数据迁移和验证方法见 [进展感知说明](docs/progress_tracking.md)。

## Rust Runtime 环境检查

```powershell
python scripts/check_environment.py
```

安装 binding：

```powershell
python -m pip install maturin
cd ..\..\crates\openjiuwen-py
maturin develop --release
cd ..\..\..\examples\opportunity_agent
```

聊天框默认由 openJiuwen `ReActAgent` 处理：普通咨询直接回答；明确的用户资料或任务进展通过 `A2aClient` 调用独立的 Opportunity Agent。Opportunity Agent 监听 8770，只校验并更新画像、状态和时间轴，不回答普通问题；`ConversationStore` 仍只由 8766 Web 进程写入。消息下方和右侧会显示本轮是直接回答还是经过 A2A。模型或 A2A 暂不可用时，原始消息会保留并提供“重新处理”。

`LifecycleTools` 仍可通过 `register_rust_lifecycle_tools()` 注册到 openJiuwen，供旧 CLI 和兼容测试使用。现有 Job Agent 的可选 ReAct 模式使用独立的 `OPPORTUNITY_AGENT_*` 环境变量；只有 API key、base URL 和 model 三项都配置时才会调用。

## V1 边界

V1 不包含真实招聘抓取或自动投递。聊天理解由 openJiuwen ReAct Agent 完成，画像和时间轴更新通过本机 A2A Opportunity Agent 执行；岗位匹配分数和通知结论仍由确定性规则决定。

真实 ModelScope 测试默认跳过；只有显式启用时才访问网络：

```powershell
$env:RUN_MODELSCOPE_INTEGRATION = "1"
python -m pytest tests/test_modelscope_live_integration.py -q
```
