# OM-Bridge

**把生成式模型能力稳定地接进你的框架 —— 换引擎、换模型，上层不用改。**

OM-Bridge 是一个 AI 生成能力的**桥接层**：它把「算力从哪来」（Backend）与
「用什么模型、怎么跑」（Solution）彻底解耦，让两者可以各自独立替换。
首个实现是 **MiniMax H3 视频生成（含音视频联合生成）跑在 ComfyUI 上**。

```
   你的框架 / Agent / 脚本
            │  CLI  ·  MCP  ·  Python SDK
   ┌────────▼─────────────────────────────────┐
   │  OM-Bridge                               │
   │    ┌────────────┐   ┌─────────────────┐  │
   │    │  Backend   │ × │    Solution     │  │
   │    │  在哪跑     │   │  用什么模型/怎么跑 │  │
   │    └─────┬──────┘   └────────┬────────┘  │
   │          └───── 数据契约 ─────┘            │
   └──────────────┬───────────────────────────┘
                  │ HTTP
        ┌─────────▼─────────┐
        │ ComfyUI + H3 权重 │
        └───────────────────┘
```

## 为什么不是"再写一个 ComfyUI 封装"

| 常见的坑 | OM-Bridge 的做法 |
|---|---|
| 换个模型就要改客户端代码 | 模型是独立的 `Solution`，**新增目录即可**，不改核心 |
| GPU 主机装不上 pip 包 | **运行时零第三方依赖**（纯标准库）；安装只需写两个启动脚本 |
| 上层 `.env` 里已写死变量名 | 配置**规范名 + 兼容旧名**，存量部署零改动 |
| 自定义节点的模型被判"不可用"，Agent 转投收费云服务 | 可用性由**实现声明**，逐模式报告缺什么（见 [ADR-0008](docs/design/0008-avoid-hardcoded-readiness.md)） |
| 仓库里的工作流 JSON 与代码脱节 | 计算图**现场物化**，不预存（见 [ADR-0006](docs/design/0006-materialize-graph-on-demand.md)） |
| MCP 服务偶发"JSON 解析失败" | 日志**只走 stderr**，stdout 恒为纯净协议流（见 [ADR-0007](docs/design/0007-logs-to-stderr-only.md)） |
| 长任务超时后不知道该重试还是等 | 超时是**可恢复**状态，用 `resume` 续等，绝不重提（后者会抢同一块 GPU） |

## 快速开始

```bash
# 1) 安装（默认零依赖模式，不调用 pip）
./deploy/install.sh                      # → ~/.local
export PATH="$HOME/.local/bin:$PATH"

# 2) 配置：从模板拷贝一份，按注释逐项过一遍
#    （默认值即推荐值，通常只需改地址一行；逐项说明都在模板注释里）
cp config/om-bridge.env.example om-bridge.env
$EDITOR om-bridge.env                      # 改 OM_BRIDGE_COMFY_SERVER_URL=...
export OM_BRIDGE_ENV_FILE=$PWD/om-bridge.env

# 3) 体检
om-bridge doctor

# 4) 先看清将要提交什么（不排队）
om-bridge generate -s minimax_h3 -p "海边的灯塔，缓慢推镜，黄昏" --dry-run

# 5) 真的跑
om-bridge generate -s minimax_h3 -p "海边的灯塔，缓慢推镜，黄昏" \
    --width 864 --height 480 --length 124

# 6) 参考图生视频
om-bridge generate -s minimax_h3.ref2v \
    -p "<Picture 1> 中的角色抬起头" \
    --reference-image ./角色.png --width 608 --height 352 --length 5
```

没装成命令也能直接用：

```bash
PYTHONPATH=$PWD/src python -m om_bridge doctor
```

## 三种用法，一套内核

```bash
# ── 人：命令行
om-bridge probe --json | jq .readiness

# ── Agent / 框架：MCP over stdio
#   在框架的 MCP 配置里加：
#   {"mcpServers": {"om-bridge": {"command": "om-bridge-mcp",
#     "env": {"OM_BRIDGE_COMFY_SERVER_URL": "http://192.168.3.3:8188",
#             "NO_PROXY": "192.168.3.3,127.0.0.1,localhost"}}}}

# ── Python 程序：SDK
python - <<'PY'
from om_bridge import Session, GenerationRequest, load_config
with Session(load_config()) as s:
    r = s.run(GenerationRequest(solution="minimax_h3",
                                prompt="海边的灯塔", params={"length": 124}))
    print([a.local_path for a in r.artifacts] if r.ok else r.issues)
PY
```

MCP 提供 7 个工具：`om_bridge_check` / `validate` / `generate_video` /
`run_workflow` / `resume` / `list` / `describe`。工具的参数 schema 由注册表自动合成 ——
**新增方案后，Agent 自动看到新参数**。

## 支持的方案

| 方案 | 模式 | 说明 |
|---|---|---|
| `minimax_h3` | `t2v` / `i2v` / `flf2v` | 文生 / 首帧 / 首尾帧生视频，**含音轨** |
| `minimax_h3.ref2v` | `ref2v` | 参考图生视频（1–9 张，顺序即 `<Picture N>`） |
| `echo`（后端 `mock`） | 无 | 演示方案：离线把请求转写成 JSON 产物，用于测试与扩展参考 |

```bash
om-bridge list                  # 全部后端、方案、别名
om-bridge describe minimax_h3   # 某方案的参数、槽位、资产、就绪度
```

实测（RTX 3080 20GB，864×480 / 124 帧 ≈ 5.2s）：t2v 约 100s，ref2v 约 95s，产物 h264 + AAC 双声道。

## 项目结构

```
OM-Bridge/
├── src/om_bridge/
│   ├── core/                 # 契约层：不含任何后端代码
│   │   ├── models.py         #   数据契约（唯一跨层语言）
│   │   ├── backend.py        #   Backend 抽象（提交/轮询/取件）
│   │   ├── solution.py       #   Solution 抽象（图/参数/资产/溯源）
│   │   ├── schema.py         #   参数单一声明 → CLI/MCP/校验/文档
│   │   ├── assets.py         #   资产就绪度（声明式）
│   │   ├── config.py         #   配置分层与自省
│   │   ├── registry.py       #   自动发现（包扫描 + entry points）
│   │   ├── session.py        #   编排：解析→校验→上传→构图→提交→取件
│   │   └── errors.py         #   错误与退出码
│   ├── backends/comfyui/     # ComfyUI 后端实现
│   │   ├── client.py         #   HTTP（标准库 urllib）
│   │   ├── graph.py          #   图操作、动态槽位、产物收集
│   │   ├── inventory.py      #   能力盘点
│   │   ├── validation.py     #   载荷静态校验
│   │   ├── backend.py        #   ComfyUIBackend
│   │   └── solutions/minimax_h3/   # 一个「方案」
│   └── interfaces/           # 交付面：cli.py / mcp.py
├── config/                   # 配置模板与 deploy profiles
├── deploy/                   # install / uninstall / verify
├── scripts/                  # 运维脚本（gen-workflow / validate / mcp-smoke）
├── integrations/openmontage/ # 外部框架集成（托管块发布器）
├── docs/                     # 文档（见下）
└── tests/                    # pytest
```

**依赖方向严格单向向下**：`interfaces → core ← backends`。
`core` 里出现 `import om_bridge.backends.*` 即视为分层失效。

## 文档

| 我想…… | 读 |
|---|---|
| 先跑通一次 | 本文件 → [docs/user-guide.md](docs/user-guide.md) |
| 理解架构与分层 | [docs/architecture.md](docs/architecture.md) |
| 知道**为什么**这么设计 | [docs/design/](docs/design/)（ADR 全集） |
| 查某个模块的 API | [docs/module-reference.md](docs/module-reference.md) |
| 部署到服务器 | [docs/deployment.md](docs/deployment.md) |
| 新增后端 / 新增模型 | [docs/adding-a-backend.md](docs/adding-a-backend.md) |
| 出问题 | [docs/troubleshooting.md](docs/troubleshooting.md) |
| H3 参数细节 | [docs/solutions/minimax-h3.md](docs/solutions/minimax-h3.md) |

## 开发

```bash
make test        # 全部测试（不需要后端在线）
make lint        # ruff
make doctor      # 对当前配置做体检
make help        # 全部目标
```

测试设计原则：**核心测试不依赖网络与后端**。
`builder` 是纯函数，其产出与 5 份已验证模板做结构回归（节点数、节点 ID、键序逐项比对）——
改动构图代码后，这个测试会立刻告诉你"行为变了"。

## 环境要求

- Python **≥ 3.10**（注册表的插件发现依赖该版本的 `importlib.metadata` 行为）
- 运行时依赖：**无**
- 后端：ComfyUI（首个实现）；模型权重体积较大，需自行准备（`probe` 会告诉你缺什么）

## 许可

MIT，见 [LICENSE](LICENSE)。
