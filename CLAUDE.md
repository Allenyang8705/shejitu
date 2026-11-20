
# 智能日报生成器（AutoDaily Generator）

Python 3.11+ / SQLite / cron 的内部工具：从 GitHub 与飞书采集当日工作数据，生成"代码提交 → 任务进展 → 协作沟通"三段式 Markdown 日报，经 SMTP 邮件和飞书机器人推送。面向 5~15 人研发团队。需求与验收见 `specs/proposal.md`，技术设计见 `specs/design.md`，任务拆解见 `specs/tasks.md`（三者经文末导入自动加载）。

## 项目规范
所有开发工作必须遵循以下规范文档：
- 需求规范：specs/proposal.md（做什么、验收清单）
- 技术设计：specs/design.md（模块职责、数据模型、接口契约、ADR、非功能约束）
- 任务清单：specs/tasks.md（10 个任务的依赖顺序、输入输出、验收标准）
- 执行工作流：specs/workflow.md（逐任务指令模板与速查表）
- 三份文档出现不一致时（如目录结构、模块命名、接口签名），先停下来向用户确认，不自行取舍

## 常用命令

- 安装依赖：`uv sync`（核心依赖：httpx / pyyaml / jinja2，见 tasks.md Task 1）
- 手动触发完整流程：`uv run python main.py`
- 采集 + 生成但不推送：`uv run python main.py --dry-run`
- 健康检查（验证连接、权限与配置）：`uv run python main.py --check`
- 新增依赖：`uv add <包名>`，并在汇报中说明引入理由
- 全量测试：`uv run pytest`；单文件：`uv run pytest tests/test_github.py`
- Lint：`uv run ruff check .`
- 格式化：`uv run ruff format .`
- 类型检查：`uv run mypy main.py collector generator notifier shared`

## 目录结构（对应 design.md §2 与 tasks.md Task 1）

- `collector/` — 数据采集，每个数据源一个模块：`github.py` / `lark_task.py` / `lark_msg.py`
- `generator/` — 日报生成：`formatter.py`（整理为 MemberReport，输出 Markdown/HTML）/ `template.py`（模板管理）
- `notifier/` — 推送通道：`email.py`（SMTP 邮件）/ `lark_bot.py`（飞书机器人 webhook）
- `shared/` — 共享基础层：`config.py`（读取并校验 config.yaml）/ `logger.py`（JSON lines 日志）/ `errors.py`（自定义异常）/ `storage.py`（SQLite 存取）/ `models.py`（跨层领域模型，定义见 design.md §3）/ `workday.py`（工作日判断：周末 + 法定节假日，proposal 3.3）
- `main.py` — 编排入口：采集 → 聚合 → 生成 → 推送，支持 `--check` / `--dry-run`
- `tests/` — 测试，文件与被测模块一一对应（test_github.py、test_lark_task.py、…、test_integration.py）
- `specs/` — 规范文档：`proposal.md` / `design.md` / `tasks.md` / `workflow.md`
- `data/` — SQLite 数据库（`data/autodaily.db`）及运行产物（gitignore，不入库）
- `config.yaml.example` — 配置模板（入库）；真实 `config.yaml` 从模板复制生成，不入库

## 配置

- `config.yaml`（参照 `config.yaml.example`）：团队名、成员映射（name / github / lark，design.md §3.3）、采集的仓库与群组列表、消息过滤关键词
- `.env`：GitHub token、飞书应用凭据、SMTP 账号密码等敏感凭据，禁止硬编码（design.md §6.2）
- config.yaml 修改后下次执行自动生效，无需重启（design.md §6.3）

## 代码规范

- 公共函数、类、方法的签名必须带类型标注；模块级 docstring 说明职责
- 命名：模块/函数/变量 snake_case；类 PascalCase；常量 UPPER_SNAKE_CASE
- 遵循 ruff 默认规则集；只改与任务相关的代码，不做顺手重构
- 注释解释"为什么"而非"是什么"
- 优先用项目已有依赖；引入新库前先说明理由
- 提交信息：`<类型>: <摘要>`，类型取 feat|fix|docs|refactor|test|chore
- 层间依赖单向：collector / generator / notifier 只依赖 shared 及 design.md §3 的数据模型，禁止反向依赖或跨层直调；层间交互以 design.md §4 接口契约为准

## 工作流规则

- 实现顺序遵循 specs/tasks.md 的依赖链：T1 → T2 → T3/T4/T5（可并行）→ T6 → T7/T8（可并行）→ T9 → T10；逐任务指令模板见 specs/workflow.md
- 每个任务完成后，对照 tasks.md 中该任务的验收标准逐条自验；功能改动另对照 proposal.md 第 3 节验收清单，并在汇报中说明结果
- 改动完成后依次跑 lint → typecheck → 相关测试，全部通过再汇报
- 新功能必须附带测试；修 bug 先写复现测试再修
- 单个数据源失败不得阻断其他数据源；失败板块必须在日报中显式标注"数据获取失败"，严禁静默跳过（proposal 3.2 / design.md §6.1）
- 所有数据源同时失败时不生成空日报，记录错误日志（design.md §6.1）
- 禁止提交：`.env`、`config.yaml`、`data/`、`__pycache__`、构建产物
- 破坏性操作（删除/覆盖文件、git push、发布）先征得确认
- 不确定时先读代码找现有模式，再问用户，不要臆测接口或数据结构

## 环境

- Python ≥ 3.11（proposal 约束；本地与 Docker 统一用 3.12 系列，避免本机 3.14 与容器版本漂移）
- 依赖管理：uv（`pyproject.toml` + `uv.lock`）；禁止手改 uv.lock
- 外部服务：GitHub API、飞书开放平台 API、公司 SMTP；凭据一律走环境变量，真实值只放本地 `.env`
- 架构约束：cron 触发的一次性进程，不引入常驻服务、消息队列、远程数据库；存储只用本地 SQLite（`data/autodaily.db`）

## 需求与设计文档

以下导入语句会在每次会话启动时自动加载文件内容：

@specs/proposal.md

@specs/design.md

@specs/tasks.md
