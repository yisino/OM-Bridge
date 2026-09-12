# ADR-0004: 配置规范名 + 兼容旧变量名

## Status

Accepted（2026-09-12）

## Context

本项目不是从零开始的：它要**取代**已经在 SER 上运行的两个目录（`shared/` 与 `ser_comfyui/`），
而这两个目录的产出已经被上游框架消费：

- OpenMontage 的 `.env` 里有一个**托管块**，写入了 `COMFYUI_SERVER_URL`、
  `COMFYUI_VIDEO_SERVER_URL`、`NO_PROXY`，并且 `.env` 第 136 行注明
  "Source of truth: `~/MY_WS/shared/comfyui.env`"；
- 共享配置 `comfyui.env`（权限 600）里有 `COMFYUI_MINIMAX_H3_*` 一整套变量；
- 部署脚本、runbook、以及与用户的历史沟通**全部**使用 `COMFYUI_*` 命名。

同时，新工程需要一个**统一的命名空间**（否则接第二个引擎时会出现 `COMFYUI_*` 与
`OTHERENGINE_*` 混在一起的局面，且"这个变量属于哪个后端"要靠猜）。

约束：**不能让上游必须同步改动才不坏**。一次要求用户"把 `.env` 里的变量全改一遍并同步改
OpenMontage 的托管块"，就是一次"升级即故障"。

## Decision

采用**规范名 + 同名别名**的双名制：

- 规范名：`OM_BRIDGE_<命名空间>_<键>`，例如 `OM_BRIDGE_COMFYUI_SERVER_URL`、
  `OM_BRIDGE_SOLUTION_MINIMAX_H3_WIDTH`。程序内用**点号键**引用（`backend.comfyui.server_url`）。
- 旧名作为 `aliases` 登记在同一 `Setting` 上：`COMFYUI_SERVER_URL`、
  `COMFYUI_MINIMAX_H3_WIDTH` 等。

关键细节：**别名与规范名在同一个优先级层**，而不是"别名优先级更低"。解析顺序为

```
构造参数 > 进程环境变量(规范名|旧名) > .env(规范名|旧名) > 内置默认
```

并额外把 `COMFYUI_BASE_URL` 标记为 `deprecated_alias=True`（优先级最低 + 给出告警），
因为上游从来不读这个名字，留着只会误导。

`om-bridge config explain <键>` 会显示最终取值**来自哪个文件/哪一层**；
`config report` 会列出"环境里存在但未登记"的变量。

## Alternatives Considered

**选项 B：一次性改名，要求上游同步。**
最干净，但代价是"升级 OM-Bridge 必须同时改 OpenMontage"——两个仓库的变更被耦合在一起，
且**顺序错一步就线上坏**。对一个要替代在跑系统的重构来说，这个代价不可接受。不选。

**选项 C：永远只用 `COMFYUI_*`，不引入规范名。**
零迁移成本，但接第二个引擎时命名空间就乱了：`COMFYUI_MINIMAX_H3_WIDTH`
这个名字同时含引擎名与模型名，逻辑上属于"方案"而非"后端"。
而且 `COMFYUI_*` 前缀会让第三方实现看起来像在蹭别人的命名空间。不选。

**选项 D：别名优先级低于规范名（即规范名总能覆盖别名）。**
看起来更"正确"，但有个反直觉的坏处：如果同一个 `.env` 里既有旧名又有新名，
**规范名会赢**——这听起来对，实际上会让人无法用旧名"顺手覆盖"新配置，
而存量部署里旧名才是常态。更重要的是它制造了"层内还有子层"的复杂度，
解释成本高。既然别名只是历史名字，**同层覆盖（谁后出现谁生效）** 更简单也更好理解。不选。

## Consequences

**变容易的**：
- 升级对上游**零改动**：存量 `.env` 与 OpenMontage 托管块原样继续工作。
- 迁移可以**逐步进行**：今天用旧名、明天换新名，两者可以长期共存。
- 命名空间清晰：`backend.comfyui.*` / `solution.minimax_h3.*` / `global.*`，
  接新引擎时不会撞名。
- 排查有据：`config explain` 直接回答"这个值哪来的"，不用猜。

**变难的 / 代价**：
- 每个配置项要写两遍名字（规范名 + 别名），且**必须同步维护**
  （漏在 `config/om-bridge.env.example` 里说明会导致旧名用户不知道它还能用）。
- 文档要解释双名制，否则用户会疑惑"到底该用哪个"。
- 一旦某天要**真正废弃**旧名，需要一套迁移路径（告警期 → 关闭期），现在还没做。

**后续要盯住的**：
- 当 OpenMontage 的托管块已经稳定使用规范名一段时间后，可以给旧名加**弃用告警**
  （不是现在就加——现在加等于骚扰存量用户）。
- 新增任何配置项时，**必须同时更新 `config/om-bridge.env.example`**。
  本项目的原则是"不造 no-op 变量"：代码不读的变量不该出现在配置里（含 example）。
- 注意别名与规范名的**冲突检测**：若同一层里两者都设且值不同，
  `config report` 应能指出来（当前依赖"后出现者生效"的语义，属于已知的粗糙点）。
