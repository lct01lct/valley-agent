# 跨语言协议与验证规范

## 修改协议前

先定位同一能力的完整链路：

1. Python 的动作枚举或状态模型。
2. Python TCP 客户端的序列化、接收与超时逻辑。
3. C# Executor/Observer 的解析、执行与序列化。
4. 行为树节点对结果的消费和成功判定。

## 协议约束

- 动作名、字段名、大小写和空值语义必须在 Python 与 C# 两侧一致。
- 把传输字段名视为不可随语言风格自动改写的契约。C# 发送 `Stone` 时，Python 接收处必须使用 `obj["Stone"]`；进入 Python 业务逻辑后再赋给符合本地规范的变量，例如 `stones = obj["Stone"]`。
- 数据模型需要语言风格转换时使用显式别名，例如 Python 属性 `player_health` 映射协议字段 `PlayerHealth`。不要在解析器中加入不透明的全局大小写猜测。
- 新增 state 字段时，若数据直接来自 SMAPI／Stardew Valley API 属性，线上名称优先与该原生属性完全一致。例如源成员为 `Stamina`，C# 发送 `Stamina`，Python 使用 `stamina = obj["Stamina"]`。
- 派生 state 字段应明确标识自己的业务语义，不能为了看起来像原生字段而使用容易误导的名称。
- 新增字段时先决定并记录唯一的线上名称；之后任何大小写或拼写变化都按协议变更处理。
- 区分“命令已接收”“动作已执行”“状态已验证”，不要都映射为一个模糊的成功值。
- 响应至少能表达成功、失败、超时和不支持；需要恢复时携带机器可读原因。
- 为阻塞读取设置合理超时，并在超时后让 Agent 能安全停止或重新规划。
- 协议升级优先保持向后兼容；无法兼容时明确版本或同步修改所有生产者与消费者。

## 验证强度

根据改动范围选择最强的可用检查：

### Skill 或文档

```bash
python /Users/evils_you/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/<skill-name>
```

### Python

```bash
python -m compileall agent server
```

对纯函数、任务模型、状态解析和寻路逻辑补充小型确定性测试。不要用启动整个游戏代替可以离线完成的验证。

### C#/SMAPI

在本机依赖和目标框架可用时执行：

```bash
dotnet build StardewMemoryExporter/StardewMemoryExporter.csproj
```

如果因本机游戏路径或 SMAPI 依赖无法构建，要明确报告原因，不能声称已验证。

### 跨语言与游戏内行为

- 用固定 JSON 样例验证 Python 解析与 C# 输出字段。
- 用最小命令样例验证分帧、响应和超时。
- 最后进入游戏检查日志、实际动作和最终状态变化。

## 交付说明

汇报时列出：

- 修改了哪些契约或行为。
- 运行了哪些检查及结果。
- 哪些内容仍需要 SMAPI 或游戏内实测。
- 是否存在 Python 已声明但 C# 尚未实现的动作。
