---
name: documentation
description: 修改 Valley Agent 项目文档时使用，包括 README.md、AGENTS.md、docs/*.md、架构说明、阶段计划、开发路线、验收标准和文档边界调整。用于避免把 Agent 开发契约、协议细节、节点状态机和频繁变化的阶段内容误写进 README。
---

# 文档维护规范

## 适用场景

当任务涉及以下内容时，必须使用本 Skill：

- 修改 `README.md`、`AGENTS.md` 或 `docs/*.md`。
- 更新项目架构、行为树说明、当前阶段、开发计划或验收标准。
- 整理文档层级、迁移细节、删除过时说明。
- 判断某段内容应该放在 README、AGENTS 还是 docs。

## 工作流程

1. 先阅读 `AGENTS.md`，确认当前事实源和必须遵守的项目约束。
2. 根据修改目标阅读当前文档本身，避免只凭历史记忆改文档。
3. 若涉及文档边界或内容迁移，阅读 `references/document-boundary.md`。
4. 只迁移或改写与当前任务相关的文档内容，不顺手重排无关章节。
5. 文档更新后，检查 README 是否仍保持“项目门面”粒度。

## 核心原则

- `README.md` 面向第一次打开项目的开发者，只讲项目是什么、怎么跑、总体架构和文档导航。
- `AGENTS.md` 面向 Codex / Agent，放实现代码时必须遵守的工程契约、运行时约束和协议边界。
- `docs/current-stage.md` 放当前阶段目标、进度、验收标准和已知缺口。
- `docs/next-development-plan.md` 放后续计划、未实现方案和开发顺序。
- 频繁变化的细节不要长期堆在 README 或 AGENTS；优先放阶段文档或专题 docs。
- Agent 改代码必须遵守的规则，不要只写在 README。

## README 禁止承载的内容

README 中不要展开：

- 行为树节点内部状态机、重试、超时、黑板字段和跨节点信号。
- SMAPI / Python 协议字段、动作 JSON、返回值结构和字段大小写细节。
- 具体工具动作等待、动画收招、BUSY 处理和 state 验证细节。
- 箱子、背包、菜单等具体交互实现细节。
- 临时 mock 数据、调试日志解释、当前 bug 复现路径。

如果这些内容仍然重要，应迁移到 `AGENTS.md`、`docs/current-stage.md` 或 `docs/next-development-plan.md`。
