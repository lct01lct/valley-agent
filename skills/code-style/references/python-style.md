# Python 编码规范

## 导入与文件组织

- 按“标准库、第三方库、项目内模块”分组，每组之间保留一个空行。
- 删除未使用或误导性的导入；不要为了保持旧文件外观而保留无用依赖。
- 优先复用现有模块边界：行为树放在 `agent/behavior_tree/`，游戏动作放在 `agent/action/`，SMAPI 客户端放在 `server/`。
- 避免循环导入。只在确有启动顺序或可选依赖需求时使用函数内导入。

## 命名与类型

- 类名使用 `PascalCase`，函数、方法和变量使用 `snake_case`，常量使用 `UPPER_SNAKE_CASE`。
- 新名称遵循 Python 规范；不要继续扩散已有的非标准名称。修改既有公共名称前先评估兼容性。
- 外部协议字段名不是 Python 本地变量名。读取 C# 传入的数据时，键名必须逐字保持协议原样，包括大小写；赋值后的 Python 变量再使用 `snake_case`：

  ```python
  stones = obj["Stone"]
  player_health = obj["PlayerHealth"]
  ```

- 不要为了符合 Python 命名规范，把协议访问擅自写成 `obj["stone"]` 或 `obj["player_health"]`。只有协议生产端已经使用这些字段名时才能这样访问。
- 对 state 数据，先核对 C# 实际读取的 SMAPI／Stardew Valley API 成员。若生产端直接传输原生属性 `Stamina`，Python 应读取 `obj["Stamina"]`，再赋给 `stamina`；不要在接收端另造 `player_stamina` 等线上字段名。
- 为新增公共函数、异步函数、构造参数和返回值添加类型标注。
- 使用 `X | None` 和内置泛型，例如 `list[BaseTask]`；如果待修改文件仍统一使用 `List`、`Optional`，局部保持一致即可，不为格式统一扩大改动。
- 枚举类型需要补充可读语义：无论使用 `type Xxx = Literal[...]` 还是 `class Xxx(Enum)`，只要枚举值不是像 `KeyType` 这种能从字面值直接猜到含义的简单集合，就应为每个枚举值添加简洁中文注释。注释说明业务含义、触发条件或动作效果，不重复翻译变量名。
- 外部输入、Planner 输出和命令载荷使用结构化模型，不使用依赖字符串拼接的隐式协议。

使用 Pydantic 映射协议字段时，通过别名同时保留线上的 C# 字段名和 Python 属性命名：

```python
from pydantic import BaseModel, Field


class GameState(BaseModel):
    stones: list[str] = Field(alias="Stone")
    player_health: int = Field(alias="PlayerHealth")
    stamina: float = Field(alias="Stamina")
```

本地变量名应根据值的真实语义选择单复数。如果 `Stone` 表示单个对象，优先使用 `stone = obj["Stone"]`；只有它表示集合时才使用 `stones`。

## 函数与控制流

- 保持函数职责单一；优先使用提前返回减少深层嵌套。
- 对 `None`、索引越界、连接断开、超时和不支持的任务类型显式处理。
- 不使用裸 `except:`。捕获能够处理的具体异常；重新抛出时保留原始异常链。
- 不静默忽略异常。至少记录操作、关键标识和失败原因；只有清理阶段的最佳努力操作可以谨慎降级。
- 不把正常游戏状态当作异常，例如商店打烊、当前节点不适用或尚未收到新状态。

## 异步与实时循环

- 行为树 `run(...)` 保持异步且非阻塞。
- 等待使用 `await asyncio.sleep(...)`，不要在异步路径中调用 `time.sleep(...)`。
- 避免在每帧循环中执行 LLM 请求、长时间 socket 阻塞、磁盘重写或昂贵的重复初始化。
- 跨帧状态保存在节点实例或黑板中，并在成功、失败和超时后重置。
- 后台线程只用于确实无法异步化或与现有渲染链路兼容的工作；共享状态要明确同步方式。

## 日志与注释

- 日志应说明组件、动作和结果，例如 `[RouteNode] 无法找到目标传送点`。
- 高频帧日志要节制；进度日志可使用覆盖式输出，状态变化和失败应单独换行记录。
- 注释解释“为什么”以及游戏机制限制，不逐行翻译代码。
- 删除大段失效注释代码；历史方案应交给版本控制保存。

## 数据与成功判定

- 使用 `context.state` 作为游戏事实来源，使用命令客户端执行动作。
- Planner 只产生宏观、类型化计划；底层节点负责确定性执行。
- 任务完成必须尽量由位置、背包、金钱、菜单或动作结果等状态变化验证。
- 扩展动作枚举前，先确认 C# Executor 能处理该动作；否则把缺口显式列为未实现。
