# 部署手册

> 面向把 OM-Bridge 装到真实机器上的人。装完请跑 [troubleshooting.md](troubleshooting.md) 里的
> `verify.sh` / `doctor` 确认。

## 目录

1. [两种安装模式（先读这个）](#1-两种安装模式先读这个)
2. [安装 / 卸载 / 验证](#2-安装--卸载--验证)
3. [配置分层与文件位置](#3-配置分层与文件位置)
4. [典型拓扑](#4-典型拓扑)
5. [接入外部框架](#5-接入外部框架)
6. [升级与回滚](#6-升级与回滚)
7. [安全要点](#7-安全要点)

---

## 1. 两种安装模式（先读这个）

| 模式 | 依赖从哪来 | 生成什么 | 适用 |
|---|---|---|---|
| **`path`（默认）** | **不装依赖**，用 `PYTHONPATH` 直接指向源码树 | `${PREFIX}/bin/om-bridge`、`om-bridge-mcp` 两个启动脚本 | **生产 GPU 主机**（常无 pip 镜像/无外网） |
| `venv` | 建虚拟环境 + `pip install -e .` | 规范的 console scripts | 有网络、需长期维护的机器 |

**为什么默认是 `path` 模式？** 因为运行时**零第三方依赖**（纯标准库）这件事本来就是为了这个场景设计的：
GPU 主机经常装不上 pip 包，而"装不上依赖"不该成为"用不了"的理由。
`path` 模式不做任何 `pip install`，只写两个 shell 启动器。

> 两种模式的**行为完全一致**：都走同一份源码、同一个 `Session`。
> 差别只在"包在哪被找到"。所以可以先 `path` 跑通，之后有空再换 `venv`。

**前置要求**：Python ≥ 3.10（注册表的插件发现依赖该版本的 `importlib.metadata` 行为；
低于此版本 `install.sh` 会直接停住并说明原因，而不是让它在运行时踩坑）。

---

## 2. 安装 / 卸载 / 验证

### 2.1 安装

```bash
# 零依赖装到 ~/.local（默认）
./deploy/install.sh

# 装到别处
./deploy/install.sh --prefix=/opt/om-bridge

# 常规 venv 模式
./deploy/install.sh --mode=venv
./deploy/install.sh --mode=venv --python=python3.13
```

安装脚本会：检查 Python 版本 → 生成启动器 → 因不存在而创建 `om-bridge.env`（从 `.example` 复制）
→ 打印后续三步提示。**它不会覆盖已存在的配置文件**（这一点是刻意的，见 §6）。

### 2.2 验证

```bash
./deploy/verify.sh --prefix=$HOME/.local
./deploy/verify.sh --prefix=$HOME/.local --skip-network    # 离线只验安装完整性
./deploy/verify.sh --env-file=/path/to/om-bridge.env       # 只验配置与连通性
```

`verify.sh` 会依次检查：可执行文件存在且可运行 → `list` 能列出后端与方案 →
配置能被解析 → （除非 `--skip-network`）后端可达 → **逐模式报告就绪度**（缺什么列什么）。

### 2.3 卸载

```bash
./deploy/uninstall.sh --prefix=$HOME/.local          # 预演，不删任何东西
./deploy/uninstall.sh --prefix=$HOME/.local --yes    # 真的删
./deploy/uninstall.sh --prefix=$HOME/.local --yes --keep-venv
```

**默认是预演（dry-run）**。要真删必须显式给 `--yes`。
`venv` 模式下的虚拟环境默认**不删**（`--drop-venv` 才删），因为里面可能还有别的包。

---

## 3. 配置分层与文件位置

### 3.1 优先级（高 → 低）

```
1. 构造参数 / CLI 显式选项      （调用方说了算）
2. 进程环境变量  OM_BRIDGE_*    （部署时说了算）
3. 进程环境变量  旧名 alias      （兼容存量：COMFYUI_*）
4. .env 文件     OM_BRIDGE_*
5. .env 文件     旧名 alias
6. 内置默认值
```

⚠ **旧名 alias 与规范名在同一层**，不是"alias 更低"。原因：alias 存在的理由是"存量部署在用它"，
如果让旧名盖过同文件里的新名，就没法用新名纠正旧值了。

想确认某个值**到底从哪来**：

```bash
om-bridge config explain backend.comfyui.server_url
```

### 3.2 配置文件会被从哪找到

按顺序探测（找到第一个就用）：

1. `--env-file <路径>` / `OM_BRIDGE_ENV_FILE`
2. `<workspace>/.env`
3. `<workspace>/config/om-bridge.env`
4. 安装前缀下的 `share/om-bridge/om-bridge.env`

`<workspace>` 默认是当前目录，可用 `--workspace` 或 `OM_BRIDGE_WORKSPACE` 指定。

### 3.3 配置文件模板与 profile

- `config/om-bridge.env.example` —— **权威模板**，每个变量都带来源与陷阱注释。
- `config/profiles/same-lan.env` —— 后端与调用方同网段（最常见）。
- `config/profiles/through-tunnel.env` —— 经 SSH 隧道（后端在调用方本机端口上）。

复制后改成自己的值：

```bash
cp config/om-bridge.env.example config/om-bridge.env
```

### 3.4 最小可用配置

```bash
OM_BRIDGE_COMFY_SERVER_URL=http://192.168.3.3:8188
NO_PROXY=192.168.3.3,127.0.0.1,localhost
```

其余都有合理默认值。**权重名也不用配** —— 默认值就是官方 H3 权重文件名；
除非你换了权重文件，否则不必写。

---

## 4. 典型拓扑

### 4.1 同网段（推荐）

```
  调用方 (任何机器)                         ComfyUI 主机
  ┌──────────────────┐                    ┌──────────────────┐
  │ OM-Bridge        │  HTTP 8188         │ ComfyUI          │
  │ CLI / MCP / SDK  │ ─────────────────► │ + 模型权重        │
  └──────────────────┘                    └──────────────────┘
             同一 /24 网段，直连即可
```

```bash
export OM_BRIDGE_COMFY_SERVER_URL=http://192.168.3.3:8188
export NO_PROXY=192.168.3.3,127.0.0.1,localhost
```

### 4.2 跨网段（后端在另一网段）

先确认**调用方**能否直连。很多"跨网段"其实是**同一台机器有多个网卡** ——
`ip route get <后端IP>` 会告诉你去程走哪个网卡。

确实不通时**优先用 SSH 本地转发**（不是反向隧道）：在调用方起
`ssh -N -L 8188:127.0.0.1:8188 user@gateway`，然后把地址写成 `http://127.0.0.1:8188`
（见 `config/profiles/through-tunnel.env`）。

> 为什么不推荐反向隧道/反向代理？因为**这台机器的原生客户端不支持认证头**这件事曾经是问题，
> 但 OM-Bridge 现在支持 `api_token` / Basic 认证（见 §7）。所以拓扑选择应以"最少活动部件"为准：
> 一个 SSH 本地转发，比一个需要长期维护的反向代理好。

### 4.3 多后端（不同能力指向不同机器）

```bash
OM_BRIDGE_COMFY_SERVER_URL=http://192.168.3.3:8188
OM_BRIDGE_COMFYUI_VIDEO_SERVER_URL=http://192.168.3.9:8188
```

留空的能力专用地址会**继承** `server_url`。单机部署应全部留空。

### 4.4 作为常驻 MCP 服务

```bash
# 前台（调试）
om-bridge-mcp

# 交给上层框架托管（推荐）：在框架的 MCP 配置里声明，由它拉进程
```

**不建议**自己写 systemd unit 常驻一个 stdio 服务：stdio 的生命周期应该由客户端管理
（客户端退出时进程应随之退出）。用 `supervisord`/`systemd` 托管反而会造成"僵尸 stdio 服务"，
表现为"配置改了不生效"。

---

## 5. 接入外部框架

### 5.1 通用：MCP

见 [user-guide.md §5](user-guide.md#5-mcp-用法给-agent--框架)。注意三点：
`command` 用绝对路径、`env` 里带上 `NO_PROXY`、日志只从 stderr 看。

### 5.2 OpenMontage

```bash
export PYTHONPATH=/path/to/OM-Bridge/src

# 预览将要写入什么（不落盘）
python integrations/openmontage/publish.py --dry-run

# 写入托管块（幂等）
python integrations/openmontage/publish.py \
    --target /home/vincent/MY_WS/OpenMontage/.env \
    --graph-dir /home/vincent/MY_WS/shared/workflows

# 移除托管块（原样恢复，不留残留）
python integrations/openmontage/publish.py --target <...> --remove
```

`publish.py` 做两件事：

1. 在目标 `.env` 里写入一个**带边界标记的托管块**（块外内容逐字节不动），
   含 `COMFYUI_SERVER_URL` / `COMFYUI_VIDEO_SERVER_URL` / `NO_PROXY` /
   `COMFYUI_H3_LOCAL_MODELS`，以及**现场物化好的工作流路径 + 输出节点**。
2. 有 `--prune-legacy` 时清掉块外的旧 `COMFYUI_*` 行（避免重复键互相覆盖）。

**它取代了什么**：原先两个独立脚本（一个给 OpenMontage 打猴子补丁修就绪度误判、
一个合并变量表）。合并后只有一个地方维护边界标记与幂等逻辑。

> **为什么要写 `COMFYUI_H3_LOCAL_MODELS`？** 因为 OpenMontage 的就绪度判断原本只认它内置的
> WAN 2.2 模型清单，装 H3 的机器会被判定为 `degraded`，进而把请求路由到**收费的云端 provider**。
> 这个变量让它知道"本机有 H3"。（对应 OpenMontage 侧的 `>>> host-local H3` 补丁。）

---

## 6. 升级与回滚

```bash
git -C /path/to/OM-Bridge pull
./deploy/install.sh --prefix=$HOME/.local          # 幂等，重跑即可
./deploy/verify.sh --prefix=$HOME/.local
```

- `path` 模式的"升级"就是更新源码树 —— 启动脚本只有一行 `PYTHONPATH`，不含版本信息。
- `venv` 模式需要重跑 `install.sh` 让它重装（`pip install -e .` 是 editable，代码更新会自动生效，
  但新增的 entry point 需要重装才能注册）。
- **配置文件不会被覆盖**：`install.sh` 只在文件不存在时从 `.example` 复制。
  所以要拿到新版本新增的变量说明，请**手工 diff**：

```bash
diff -u $HOME/.local/share/om-bridge/om-bridge.env config/om-bridge.env.example
```

- 回滚：`git checkout <旧提交>` + 重跑 `install.sh`。配置不需要回滚（新变量在旧代码里只是被忽略）。

---

## 7. 安全要点

| 项 | 做法 |
|---|---|
| **配置文件权限** | 含 token 的 `.env` 用 `chmod 600`。`verify.sh` 会检查并告警 |
| **别进 git** | `.gitignore` 已挡 `*.env` 与 `.env.bak.*`；提交时用 `git add <具体文件>`，**别用 `git add -A`** |
| **认证** | 后端在鉴权代理之后时用 `OM_BRIDGE_COMFYUI_API_TOKEN`（Bearer）或 `auth_user`+`auth_password`（Basic）。**原生 ComfyUI 不需要认证** |
| **对外暴露** | ComfyUI 的 API 没有鉴权，**不要**直接把 8188 暴露到公网。走 SSH 转发或只监听 LAN |
| **共享机器** | 若同机有其它用户，确认安装前缀与配置文件对其不可读（`path` 模式默认装到 `~/.local`，已是私有） |
| **密钥输出** | `config list`/`report` 对 `secret=True` 的项做**掩码**输出。别自己写脚本去 dump 原始 env |

`config report` 会把 `secret` 项显示成掩码，并单列"环境里存在但未登记"的变量 ——
后者可帮你发现"手滑写错变量名，导致密钥被写进一个没人读的变量里"。
