# ADR-0005: 三个交付面共享一个 Session

## Status

Accepted（2026-09-12）

## Context

这套能力要同时服务三类消费者，它们的形态差别很大：

| 消费者 | 期望的入口 | 特殊约束 |
|---|---|---|
| 人 | 命令行 | 退出码要有意义、进度要看得见、长提示词要好传 |
| Agent / 支持 MCP 的框架 | MCP over stdio | **stdout 只能是 JSON-RPC**，工具 schema 要机器可读 |
| 别人的 Python 程序 | `import` | 要能分步控制、要能拿到结构化对象 |

问题在于：这三者要调用的**业务逻辑完全一样**（解析 → 校验 → 上传 → 构图 → 预检 → 提交 → 轮询 → 取件）。
历史上这类项目的典型腐坏方式是**三份近似实现**：CLI 一份、MCP 一份、SDK 一份，
然后修 bug 只改了一处，另外两处的行为悄悄偏离——最坏的情况是"CLI 能跑、MCP 报错"。

另外还有一个直接诱因：上游 OpenMontage 需要一个"文件路径 + 节点 ID"的形态
（既不是 CLI 参数，也不是 SDK 对象），历史上是靠**两个预存的工作流 JSON 文件**
和对应的路径环境变量解决的，而它们会与代码脱节。

## Decision

**一份编排核心（`core/session.py` 的 `Session`），三个薄适配层。**

- `interfaces/cli.py`：`argparse` 子命令 → 构造 `GenerationRequest` → 调 `Session`
- `interfaces/mcp.py`：JSON-RPC 循环 → 构造 `GenerationRequest` → 调 `Session`
- Python SDK：直接 `from om_bridge import Session`

三者**共享同一个注册表与同一份 `ParamSchema`**，因此：

- CLI 的选项、MCP 的 `inputSchema`、SDK 的可用参数、参数校验规则、`describe` 的输出
  全部从**同一处声明**派生。新增一个方案参数（改 `ParamSchema`），三面同时获得，无需分别改。
- 退出码只在 CLI 层存在（`errors.py` 定义，CLI 返回），MCP 把同一异常映射为 JSON-RPC 错误码。

对外部框架的"文件 + 节点 ID"需求，用**第四个面**（`graph` 子命令）统一解决：
现场物化计算图并输出节点 ID。三个面都能调用它（CLI 直接调、MCP 有 `om_bridge_run_workflow`）。

## Alternatives Considered

**选项 B：三个面各自实现。**
短期最省事（每处只写自己需要的部分），长期必然漂移。而且参数校验这种"细节最多"的部分
最容易漂移——它有三份，就意味着三条不同的"合法输入"边界。不选。

**选项 C：只做 SDK，CLI 与 MCP 都是 SDK 的薄包装但各自重写参数表。**
比 B 好，但参数表重写就是漂移的开始：Schema 里给参数加了 `multiple_of=32`，
CLI 的 help 文本和 MCP 的 `inputSchema` 忘了同步，于是 Agent 会传不合法的值。不选。

**选项 D：把参数表做成一份 JSON/YAML，三个面都读它。**
方向对（单一来源），但把参数的表征搬到运行时数据文件会失去类型信息与文档字符串的便利，
且需要为"如何引用另一个参数"设计表达式。用 `ParamSchema` 类在代码里声明既保留了单一来源，
又保留了 IDE 补全与静态检查。不选（但这是合理的次优解）。

**选项 E：为 MCP 单独做一套"精简工具"，只暴露最必要的能力。**
曾考虑，因为 Agent 面对 10 个子命令会困惑。但最终选择"工具由注册表自动合成"，
让 Agent 通过 `om_bridge_describe` / `om_bridge_check` 自省。理由：
精简集需要对每个方案手工判断"哪些参数重要"，这本身就是一个会腐坏的知识。
不自省的工具集在新增方案后立刻过期。不选。

## Consequences

**变容易的**：
- 一处改动三面生效；bug 只可能存在于一处业务逻辑里。
- 新增方案无需碰 `interfaces/`：CLI 选项与 MCP `inputSchema` 自动出现。
- 行为一致性可验证：`om-bridge generate --dry-run` 与 MCP 的 `om_bridge_validate`
  走的是同一条解析路径，结论必然一致。

**变难的 / 代价**：
- CLI 与 MCP 的"呈现层"要写两次（怎么排版、怎么加颜色、怎么分组）。这是必要的重复，
  因为两类观众的阅读方式不同。
- stdio 的 MCP 对 stdout 有强约束，反过来限制了日志实现
  （见 [ADR-0007](0007-logs-to-stderr-only.md)）。
- `Session` 成为一个较厚的类，需要克制不把"呈现"逻辑塞进去。

**后续要盯住的**：
- 若出现第四个面（如 HTTP 服务），**必须**复用 `Session`，不允许重写编排流程。
- 若某个面出现"只有它需要"的特殊逻辑超过两三处，应重新审视契约是否缺了概念，
  而不是在那个面里打补丁。
- `Session.validate()` **保证不抛异常**这条约定（校验命令的产出就是问题清单）
  是三个面共同依赖的前提，改动它会同时破坏 CLI 与 MCP 的行为。
