# 架构决策记录（ADR）

> 记录**为什么**这么设计，以及**我们放弃了什么**。改核心代码前请先读一遍。

## 为什么要写 ADR

代码能告诉你"是什么"，很难告诉你"为什么"。当有人（包括三个月后的你自己）想改一处设计时，
如果没有记录，通常会发生两件事之一：

- 因为不知道约束而**改坏**（例如把 `validate()` 改成抛异常）；
- 因为怕改坏而**不敢动**，让设计僵化。

ADR 就是为了避免这两者。每条记录包含：**背景**（当时面对什么问题）、
**决策**（选了什么）、**考虑过的其它选项**（为什么不选）、
**后果**（变容易了什么、变难了什么）。

## 什么时候要新开一份 ADR

- 引入或更换一项**技术选型**（依赖、协议、存储）
- 确立或推翻一条**架构约束**（分层规则、依赖方向）
- 做出**难以回退**的决定
- 改动了本文档里已有 ADR 所依据的前提

不写 ADR 的情况：修 bug、加参数、改文案 —— 这些属于"跟着既有决策做"。

## 状态流转

```
Proposed → Accepted → (Deprecated | Superseded by ADR-XXXX)
```

**推翻一条旧决策的正确做法**：新开一份 ADR，在"背景"里引用旧编号，
并把旧文件的 Status 改成 `Superseded by ADR-XXXX`。
**不要**直接改旧文件的"决策"段落 —— 那会抹掉历史，让下一次讨论重新踩同一个坑。

## 索引

| 编号 | 标题 | 状态 | 一句话 |
|---|---|---|---|
| [0001](0001-two-layer-backend-solution.md) | 双层扩展模型（Backend × Solution） | Accepted | 把"在哪跑"和"用什么模型"彻底分开 |
| [0002](0002-zero-runtime-dependencies.md) | 运行时零第三方依赖 | Accepted | 不用 requests/python-dotenv，换"GPU 主机免 pip" |
| [0003](0003-registry-autodiscovery.md) | 注册表自动发现，无中心清单 | Accepted | 新增实现不修改任何既有文件 |
| [0004](0004-config-backward-compatible-aliases.md) | 配置规范名 + 兼容旧变量名 | Superseded by ADR-0009 | 上层存量 `.env` 不改也能跑 |
| [0005](0005-three-delivery-surfaces.md) | 三个交付面共享一个 Session | Accepted | CLI / MCP / SDK 行为一致 |
| [0006](0006-materialize-graph-on-demand.md) | 计算图现场物化，不预存文件 | Accepted | 消灭"仓库 JSON 与代码脱节" |
| [0007](0007-logs-to-stderr-only.md) | 日志只走 stderr | Accepted | 保住 MCP 的 stdio 协议 |
| [0008](0008-avoid-hardcoded-readiness.md) | 禁止硬编码的可用性判断 | Accepted | 错误的"不可用"会把人推向收费服务 |
| [0009](0009-config-naming-v2.md) | 配置命名 v2：OMB_ 短前缀 + 别名移除 | Accepted | 命名空间防撞名，模板只讲一个名字 |

## 模板

新开 ADR 时复制下面这段：

```markdown
# ADR-XXXX: 标题

## Status
Proposed

## Context
我们面对什么问题？有哪些约束（技术、组织、时间）？为什么现在必须决定？

## Decision
选了什么。用完整句子，不要只写结论词。

## Alternatives Considered
- **选项 B**：描述。为什么不选（要具体：是成本、复杂度，还是它解决不了某个真实场景）。
- **选项 C**：同上。

## Consequences
**变容易的**：…
**变难的 / 代价**：…
**后续要盯住的**：什么情况下这条决策会失效，届时该怎么重新评估。
```
