# OM-Bridge 文档

> 面向"把生成式模型能力接进外部框架/流水线"这件事，提供一层**稳定、可扩展、零依赖**的桥接层。

## 该从哪读起

| 你的角色 / 目的 | 先读 | 再读 |
|---|---|---|
| 我想先跑通一次 | [../README.md](../README.md) → [user-guide.md](user-guide.md) | [deployment.md](deployment.md) |
| 我要把它接到我的框架 | [user-guide.md](user-guide.md) §接口 | [module-reference.md](module-reference.md) §interfaces |
| 我要新增一个后端（不一定是 ComfyUI） | [architecture.md](architecture.md) | [adding-a-backend.md](adding-a-backend.md) |
| 我要新增一个模型方案（如 LTX-2） | [architecture.md](architecture.md) §双层模型 | [adding-a-backend.md](adding-a-backend.md) §新增方案 |
| 我负责部署/运维 | [deployment.md](deployment.md) | [troubleshooting.md](troubleshooting.md) |
| 我要改核心代码 | [architecture.md](architecture.md) + [design/](design/) 全部 ADR | [module-reference.md](module-reference.md) |
| 出问题了 | [troubleshooting.md](troubleshooting.md) | — |

## 文档清单

### 设计与决策

- [`design/`](design/) —— **架构决策记录（ADR）**。回答"为什么这么设计"，以及"我们放弃了什么"。
  改核心代码前请先读一遍；要推翻某条决策，请新开一份 ADR 把旧的标记为 Superseded，而不是改旧的。
- [`architecture.md`](architecture.md) —— 架构总览：分层、数据流、扩展点、依赖方向。**代码说明的入口**。
- [`module-reference.md`](module-reference.md) —— 逐模块/逐类 API 参考：职责、关键方法、契约、易错点。

### 使用与运维

- [`user-guide.md`](user-guide.md) —— 使用手册：三个交付面（CLI / MCP / Python SDK）的完整用法与示例。
- [`deployment.md`](deployment.md) —— 部署手册：安装模式、配置分层、多机拓扑、升级与回滚。
- [`troubleshooting.md`](troubleshooting.md) —— 排障手册：**按症状索引**，含本项目历史上踩过的真实坑。

### 扩展

- [`adding-a-backend.md`](adding-a-backend.md) —— 新增后端 / 新增方案的完整步骤与检查清单。

### 具体实现说明

- [`solutions/minimax-h3.md`](solutions/minimax-h3.md) —— MiniMax H3 方案的实现说明与参数详解。

## 文档约定

- **术语统一**：`Backend`（后端，在哪跑）、`Solution`（方案，用什么模型怎么跑）、`Job`（作业，一次提交的载荷）、
  `Artifact`（产物）、`Probe`（探测报告）。中文正文里首次出现时附英文。
- **命令示例可直接复制**：默认用 `om-bridge`（已安装）。未安装时等价于
  `PYTHONPATH=<repo>/src python -m om_bridge`。
- **"为什么"优先于"是什么"**：代码里能读出来的结构不重复；文档重点写**约束、取舍和踩过的坑**。
- 文档与代码同仓同分支维护。**改了行为就必须改对应文档**——尤其是 `config/om-bridge.env.example`
  与 [module-reference.md](module-reference.md) 的配置表。
