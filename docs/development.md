# 开发环境搭建

> 目标：10 分钟内在开发机（Windows / Linux 均可）搭出能跑全部测试的环境。
> 注意区分两件事：**运行** OM-Bridge 不需要本文件（核心零依赖，`deploy/install.sh` 即可）；
> 只有**改代码、跑测试**才需要往下走。

## 1. 前置

- Python **≥ 3.10**（注册表插件发现依赖该版本的 `importlib.metadata` 行为；
  开发验证常用 3.13）
- git

## 2. 创建并启用虚拟环境（.venv）

### Linux / macOS

```bash
cd OM-Bridge
python3 -m venv .venv
source .venv/bin/activate        # 每次开新 shell 都要重新启用
```

### Windows（PowerShell）

```powershell
cd OM-Bridge
python -m venv .venv
.venv\Scripts\Activate.ps1       # 每次开新 shell 都要重新启用
```

> PowerShell 报"禁止运行脚本"时：`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。

### Windows（cmd）

```bat
cd OM-Bridge
python -m venv .venv
.venv\Scripts\activate.bat
```

确认启用成功：`where python`（Linux: `which python`）应指向 `.venv` 内的解释器。

## 3. 安装依赖

```bash
# 以可编辑模式安装本项目（含 om-bridge / om-bridge-mcp 两个入口点）
pip install -e .

# 开发依赖：pytest + ruff（二选一，内容一致）
pip install -r requirements-dev.txt
# pip install -e ".[dev]"
```

依赖清单（**运行时零第三方依赖**是硬约束，见 [ADR-0002](design/0002-zero-runtime-dependencies.md)）：

| 文件 | 内容 | 用途 |
|---|---|---|
| `requirements.txt` | 空（注释 + 可选 `websocket-client`） | 运行时：什么都不装也能跑 |
| `requirements-dev.txt` | `pytest>=7.4`、`ruff>=0.5` | 跑测试与静态检查 |
| `pyproject.toml` `[ws]` extra | `websocket-client>=1.6` | 可选：ComfyUI 实时进度（不装则轮询 `/history`） |

## 4. 验证环境

```bash
pytest                 # 全部测试（不需要后端在线，~30s）
ruff check .           # 静态检查
om-bridge list         # 入口点已装好；应看到 comfyui/mock 两个后端
```

有真实 ComfyUI 时可加跑（默认测试集**不含**网络测试）：

```bash
pytest -m network --backend-url http://<comfyui>:8188
```

## 5. 日常开发约定

- **改行为必须改文档**：尤其是 `config/om-bridge.env.example` 与
  [module-reference.md](module-reference.md) 的配置总表——三者不一致会被
  `om-bridge config report` 与 review 直接抓住。
- **配置命名**：新配置项必须登记进 `core/config.py` 的 `Setting` 表，
  环境变量名带 `OMB_` 短前缀；枚举值写进 `choices`。
  见 [ADR-0009](design/0009-config-naming-v2.md) 与 [adding-a-backend.md](adding-a-backend.md)。
- **测试原则**：核心测试不依赖网络与后端；构图代码改动会触发结构回归测试。
- 提交前：`ruff check . && pytest`。

## 6. 常见问题

- **`pip install -e .` 报缺少 setuptools**：`pip install -U pip setuptools` 后重试。
- **Debian/Ubuntu 提示 `ensurepip is not available`**：`apt install python3-venv` 后重建 venv。
- **不想建 venv？** 不推荐，但可以：本项目运行时零依赖，`PYTHONPATH=$PWD/src python -m om_bridge`
  直接可用；只是跑测试仍需要 `pytest`，会装进当前解释器环境。
