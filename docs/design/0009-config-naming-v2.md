# ADR-0009: 配置命名 v2 —— OMB_ 短前缀 + 别名层移除

## Status
Accepted（2026-09-12）

Supersedes [ADR-0004](0004-config-backward-compatible-aliases.md)。
同时确认：配置文件加载**维持单文件、不合并**的既有决策不变。

## Context

ADR-0004 时代的核心诉求是"存量 `.env` 不改也能跑"，于是规范名
`OM_BRIDGE_*` 之外还保留了一整套 `COMFYUI_*` 别名。随着 OM-Bridge
成为独立开源工程并发布到 GitHub，这套双层命名开始显出三个问题：

1. **前缀太长**：`OM_BRIDGE_SOLUTION_MINIMAX_H3_REF2V_IMAGE_SIZE`
   有 44 个字符，配置文件里一半篇幅是前缀。
2. **别名是负资产**：26 处别名引用让"同一个值有两个名字"成为常态
   （模板里每项都要写新旧两行），而别名真正的消费者——存量
   `comfyui.env`——早已迁移到 OM-Bridge 自己的配置文件。
   `COMFYUI_BASE_URL` 更是"看起来像正主、实际没人读"的陷阱。
3. **直接去前缀不可行**：评估过"完全去掉前缀"（得到 `TIMEOUT` /
   `STRICT` / `ENV_FILE` / `LOG_LEVEL` 等裸名）。否决——环境变量是
   进程级公共地盘，OM-Bridge 常以 MCP 子进程形态运行（Hermes 等宿主
   会注入环境变量），裸名撞上其他工具的概率不可忽视；
   `config report` 靠前缀识别"未登记变量"的自省功能也会失去锚点。

## Decision

1. **统一 `OMB_` 短前缀**。全部规范名从 `OM_BRIDGE_*` 改为 `OMB_*`；
   ComfyUI 后端段进一步去掉冗余的 "UI"（`OMB_COMFY_API_TOKEN` 等，
   与此前 `server_url` → `OMB_COMFY_SERVER_URL` 的既定方向一致）；
   方案段的 `SOLUTION_` 层从环境变量名里省去（`OMB_MINIMAX_H3_*`）。
2. **别名层整体移除，不留残留**。`Setting.aliases` / `deprecated_alias`
   字段、`SETTINGS_BY_ALIAS`、别名冲突告警全部删除；
   `backend.comfyui.base_url` 连同其废弃别名一并消失。
   旧名（`OM_BRIDGE_*`、`COMFYUI_*`）不再被读取，但 **unregistered 扫描
   仍认这三个前缀**（`OMB_` / `OM_BRIDGE_` / `COMFYUI_`）——
   存量环境里的遗留变量会被 `config report --include-unknown-env` 点名，
   一次性清干净而不是悄悄失效。
3. **`Setting` 新增 `choices`（开放枚举）**。枚举型配置在登记表、
   `config list` / `explain`、配置模板三处同步列出合法值；越界取值
   **告警不报错**——`default_backend` / `default_solution` 可被第三方
   插件扩展，登记表不是封闭集合。
4. **配置文件加载维持单文件、不合并**（沿用 ADR-0004 之前即已确立的
   规则）。曾评估"把 Minimax 方案参数拆分为独立配置文件"，因任何拆分
   都要引入 include 或多文件合并机制、重新打开"这个值哪来的"的歧义，
   本轮**不做拆分**；若未来配置量再膨胀，重开 ADR 评估。

## Alternatives Considered

- **彻底去前缀（裸名）**：命名最短，但撞名风险与自省锚点丢失（见 Context 3）。
- **混合策略**（特定名去前缀、通用名保留）：命名最"合理"，但规则复杂——
  每个新配置项都要先回答"我的名字够不够特定"，一致性维护成本高。
- **include 指令 / 多文件合并**（为配置拆分）：dotenv 无此标准，解析器
  变复杂且要防护循环引用；合并顺序又会引入新的优先级歧义。
- **只拆模板不改加载逻辑**：改动最小，但"两个模板文件 + 一个加载变量"
  的组合需要用户自己理解哪个文件被读、哪个被忽略，容易制造静默失效。

## Consequences

**变容易的**：
- 配置模板、文档、`config list` 的每项只讲**一个名字**，无新旧对照噪音；
- 撞名风险被 `OMB_` 命名空间挡住，MCP 宿主注入环境变量也安全；
- 枚举值"写注释"升级为"登记表可查、程序可告警"，模板注释与代码不再漂移。

**变难的 / 代价**：
- **破坏性变更**：所有存量配置文件（部署机 `om-bridge.env`、Hermes
  `config.yaml` 里的 `OM_BRIDGE_ENV_FILE` 注入等）必须改名才能工作；
  未改名的项会**静默回落内置默认值**（如 server_url → localhost）。
  缓解：install.sh 生成的模板已是新名；unregistered 扫描会点名残留旧变量。
- 与 OpenMontage 的边界更清晰但也更"手工"：publish.py 写出的
  `COMFYUI_*` 是 OpenMontage 读取的对外契约，与 OM-Bridge 自己的
  `OMB_*` 从此是两套名字，由集成层一张表负责转换。

**后续要盯住的**：
- 若未来接入的宿主环境连 `OMB_` 都会撞名（极不可能），再评估带项目
  全名的回退方案；
- 配置项数量若随方案增长再度膨胀，重开"配置拆分 + 多文件加载"的 ADR。
