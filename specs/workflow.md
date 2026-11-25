# 任务执行工作流

按 specs/tasks.md 逐任务推进时的标准操作：下达指令 → 自验汇报 → 核对验收 → 下一个任务。

CLAUDE.md 会自动加载三份 specs，指令可以写得很短；但把 design.md 的具体章节写显式，执行者不用大海捞针，也降低漏读验收标准的概率，因此固定使用下述模板。

## 1. 指令模板

```text
请实现 Task {N}（{任务名}）。

具体需求见 specs/tasks.md 中 Task {N} 的描述、输入输出及验收标准。
数据模型定义见 specs/design.md {§章节}（{模型名}）。      ← 采集/生成类任务适用
接口契约见 specs/design.md {§章节}。                      ← 采集/推送类任务适用
错误处理策略见 specs/design.md §6.1。                     ← 采集/推送类任务适用
前置状态：{哪些任务已完成、哪些模块已存在}。
请生成 {输出文件清单}。

完成后跑 lint → typecheck → 相关测试，全部通过后对照 Task {N} 验收标准逐条汇报自验结果。
```

要点：

- 「前置状态」必须填写——告诉执行者哪些东西已存在，避免重复造轮子或假设错依赖
- 非采集/推送类任务（T1、T2、T9、T10）删除不适用的引用行

## 2. 填写示例：Task 2

T1 完成后的下一个任务：

```text
请实现 Task 2（共享基础层）。

具体需求见 specs/tasks.md 中 Task 2 的描述及验收标准。
数据模型定义见 specs/design.md §3（CommitRecord / MessageRecord / MemberReport / DailyReport）。
错误处理策略见 specs/design.md §6.1（超时重试、降级标注、JSON lines 日志）。
前置状态：Task 1 已完成，pyproject.toml 与 config.yaml.example 已存在，依赖已通过 uv sync 安装。
请生成 shared/config.py、shared/logger.py、shared/errors.py、shared/storage.py、shared/models.py
及测试文件 tests/test_config.py、test_logger.py、test_errors.py、test_storage.py。

完成后跑 lint → typecheck → 相关测试，全部通过后对照 Task 2 验收标准逐条汇报自验结果。
```

注意：`shared/models.py` 不在 tasks.md Task 2 的原始输出清单里，是 CLAUDE.md 目录结构的约定（T3~T6 都要用这四个模型，放 T2 一次定义最省事）。

## 3. 任务速查表

| Task | design.md 引用（填进指令） | 前置状态 | 产出 |
|------|---------------------------|----------|------|
| T1 项目初始化 | §2（模块划分） | 空项目（仅 CLAUDE.md + specs/） | 目录结构、pyproject.toml、config.yaml.example |
| T2 共享基础层 | §3（四个数据模型）、§6.1 | T1 完成 | shared/ 五个模块 + 各自测试 |
| T3 GitHub 采集 | §3.1（CommitRecord）、§4.1、§6.1 | T2 完成 | collector/github.py + tests/test_github.py |
| T4 飞书任务采集 | — | — | 已裁剪（2026-11-25，未开通飞书项目） |
| T5 飞书消息采集 | §3.1（MessageRecord）、§4.1、§6.1、§6.2（敏感词过滤） | T2 完成 | collector/lark_msg.py + tests/test_lark_msg.py |
| T6 日报生成 | §3.2（MemberReport/DailyReport）、§4.2、§6.1 | T2 完成 | generator/formatter.py、template.py + tests/test_generator.py |
| T7 邮件推送 | §4.3（send 契约）、§6.1（重试 2 次） | T6 完成 | notifier/email.py + tests/test_email.py |
| T8 飞书推送 | §4.3、§6.1 | T6 完成 | notifier/lark_bot.py + tests/test_lark_bot.py |
| T9 主编排入口 | §2.5（--check / --dry-run）、§6.1 | T3~T8 全部完成 | main.py |
| T10 集成测试 | §6.1（降级场景） | T9 完成 | tests/test_integration.py |

## 4. 执行策略

1. **一条指令 = 一个 Task**，严格按 T1→T2→T3/5→T6→T7/8→T9→T10 的顺序发。
2. **收到汇报后先核对验收标准再发下一条**；不过关就要求修复，不带病前进。
3. T3/T5、T7/T8 标注可并行：赶时间可以在一条指令里一起提（"请并行实现 Task 3、5"），但逐条执行更稳——上下文连续，出错好定位。
4. **T1 指令中加一句"并执行 git init 完成首次提交"**（项目初始时不是 git 仓库），此后每完成一个 Task 提交一次（`<类型>: <摘要>`），出问题可回滚。

---

*规范版本：v1.1*

*变更记录：v1.1（2026-11-25）Task 4 裁剪（未开通飞书项目），模型清单与并行安排同步。*
