# ADR-0002: 运行时零第三方依赖

## Status

Accepted（2026-09-12）

## Context

目标部署环境是**生产 GPU 主机**。这类机器有几个稳定的特征：

- 可能要装几百 GB 的模型权重与 CUDA/torch 栈，磁盘与网络都紧张；
- 常常没有外网，或 pip 镜像不稳定；
- 机器上的 Python 环境往往被 CUDA 相关工具链"占住"了，随便装包有冲突风险；
- 现场排障时间有限，多一个依赖就多一个"装不上"的失败点。

同时，本项目**真正需要网络的部分很少**：就是发几个 HTTP 请求（`GET /system_stats`、
`GET /object_info`、`POST /prompt`、`GET /history`、`GET /view`、`POST /upload/image`），
以及读一个 `.env` 文件。

历史证据：此前的实现里，SER 上的项目**连 `python-dotenv` 都没装**，导致整个链路跑不起来；
后来不得不走"纯标准库 urllib 的 CLI + JSON 契约"这个形态来绕开。

## Decision

**运行时零第三方依赖。** 具体：

- HTTP 用标准库 `urllib.request`（不用 `requests`）
- `.env` 解析自写（`core/config.py` 的 `parse_env_file`，不用 `python-dotenv`）
- 数据结构用 `dataclasses`，不用 pydantic
- CLI 用 `argparse`，不用 click/typer
- 唯一的可选依赖是 `websocket-client`（无它则退化为轮询，功能不缺）

`pyproject.toml` 里 `dependencies = []`。

## Alternatives Considered

**选项 B：`requests` + `python-dotenv` + `pydantic`。**
开发体验明显更好（重试、超时、类型校验都省事）。但这意味着 GPU 主机上必须先能
`pip install` 成功 —— 而这正是历史上失败的那一步。用"开发体验"换"可能装不上"，
对一个**要被部署到别人机器上**的桥接层来说，交换方向是错的。不选。

**选项 C：零依赖，但把 `requests` 作为 extra（`pip install om-bridge[http]`）。**
看起来两全，实际会变成"两套代码路径"：有 requests 走一条、没有走另一条，
而**部署现场跑的是哪条往往没人记得**，于是 bug 只在一条路径上被修。
如果真需要，应该是**替换**实现而不是**并存**。不选。

**选项 D：提供一个自带依赖的 Docker 镜像。**
能解决依赖问题，但引入新的约束：GPU 直通、模型目录挂载、镜像体积（CUDA 基础镜像就有数 GB）、
以及"宿主机已经在跑一个 ComfyUI 了，为什么还要再进容器"的运维复杂度。
对一个**客户端**来说，容器是过重的交付形态。不选。

## Consequences

**变容易的**：
- `pip install` 不再是前置条件；`deploy/install.sh --mode=path` 能做到"零安装"（只写两个启动脚本）。
- 部署脚本不必处理 pip 镜像、虚拟环境、编译依赖。
- 依赖冲突风险归零 —— 不会出现"桥接层升级后 torch 崩了"这种事故。
- 排障时只需考虑 ComfyUI 侧的问题，不用怀疑桥接层的依赖。

**变难的 / 代价**：
- HTTP 层要自己处理超时、重试、multipart 上传、错误分类。这部分代码（`client.py`）比用
  `requests` 长一些，且有真实踩坑点（例如 HTTP 409 重名要改用内容哈希名重传）。
- 没有 pydantic 的自动校验，类型边界要靠 `ParamSchema` 手写（但这也带来了"单一声明驱动
  CLI/MCP/校验/文档"这个额外好处，见 [ADR-0005](0005-three-delivery-surfaces.md)）。
- `.env` 解析器要自己覆盖引号、行内注释、`export` 前缀、空值等边界。

**后续要盯住的**：
- 如果将来需要**流式**能力（如 WebSocket 实时进度），`websocket-client` 会成为事实必需项。
  届时要么保持"可选 + 轮询兜底"，要么正式把它列为依赖并**同步更新本文档**。
- 若发现自己在 `client.py` 里反复实现通用 HTTP 语义（重试策略、连接池），
  应当重新评估这条决策 —— 但**先确认部署约束是否真的变了**，别为了方便而放开。
