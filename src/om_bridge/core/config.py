"""配置系统 —— 分层解析 + 旧变量名兼容 + 自描述。

三个必须解决的问题
------------------
**1. 旧变量名必须继续可用。**
现网 SER 上的 ``comfyui.env`` 用的是 ``COMFYUI_SERVER_URL`` 这一组名字，
而且 ``OpenMontage/.env`` 里也写死了同样的名字（那是 OpenMontage 自己读的）。
迁移到 OM-Bridge 时如果强制改名，等于要同时改两处且冒着打断生产线的风险。
所以这里的做法是：**规范名 + 别名表**。规范名是新名字（``OM_BRIDGE_...``），
别名是旧名字；两者等价读取，规范名优先。旧名字永远不会被删掉。

**2. 配置必须能自我解释。**
原实现的配置知识一半在 ``comfyui.env`` 的注释里、一半在部署脚本里，
想知道"这个变量到底谁在读"只能翻源码。这里把所有设置登记成 :class:`Setting`，
于是一条命令就能列出全部配置项、当前取值、取值来源、以及它属于谁。

**3. 优先级必须是确定的。**
四层，从高到低：

===================  ==========================================
显式覆盖（API/CLI）  代码里直接传的，测试与临时切换用
进程环境变量         ``export`` 出来的，一次性覆盖用
配置文件             ``.env`` / profile，日常维护的主战场
内置默认值           代码里的兜底
===================  ==========================================

同一层内，规范名优先于别名（两个都设时以规范名为准，并在 ``explain`` 里提示冲突）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError, ConfigMissingError


# ===========================================================================
# 设置登记表
# ===========================================================================
@dataclass(frozen=True)
class Setting:
    """一条配置项的完整元数据。

    有了它，``om-bridge config list`` 能打印出全部配置面，
    ``om-bridge config explain KEY`` 能说明"当前这个值是哪来的"。
    """

    key: str
    """规范键，点分命名：``<scope>.<owner>.<name>``。"""
    env: str
    """规范环境变量名。"""
    default: Any = None
    type: str = "string"
    description: str = ""
    aliases: tuple[str, ...] = ()
    """兼容用的旧环境变量名（如 ``COMFYUI_SERVER_URL``）。排在前面的优先。"""
    secret: bool = False
    """标记为敏感的项，在报告里会被打码。"""
    deprecated_alias: bool = False
    """别名本身已不推荐（例如 ``COMFYUI_BASE_URL`` 从来不被 OpenMontage 读取）。"""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "env": self.env,
            "aliases": list(self.aliases),
            "type": self.type,
            "default": None if self.secret else self.default,
            "description": self.description,
            "secret": self.secret,
        }


def _s(key: str, env: str, default: Any = None, type: str = "string", description: str = "",
       aliases: tuple[str, ...] = (), secret: bool = False, deprecated_alias: bool = False) -> Setting:
    return Setting(key, env, default, type, description, aliases, secret, deprecated_alias)


# --- 全局 ------------------------------------------------------------------
GLOBAL_SETTINGS: list[Setting] = [
    _s("global.default_backend", "OM_BRIDGE_DEFAULT_BACKEND", "comfyui", "string",
       "未显式指定后端时使用的默认后端名。"),
    _s("global.default_solution", "OM_BRIDGE_DEFAULT_SOLUTION", "minimax_h3", "string",
       "未显式指定方案时使用的默认方案键。"),
    _s("global.workspace", "OM_BRIDGE_WORKSPACE", ".", "path",
       "工作区根目录。产物、上传缓存等运行时目录默认挂在它下面。"),
    _s("global.output_dir", "OM_BRIDGE_OUTPUT_DIR", "", "path",
       "产物默认落盘目录。留空则用 <workspace>/var/output。"),
    _s("global.connect_timeout", "OM_BRIDGE_CONNECT_TIMEOUT", 15, "float",
       "连接与轻量探测的超时（秒）。"),
    _s("global.timeout", "OM_BRIDGE_TIMEOUT", 900, "float",
       "等待作业完成的总超时（秒）。超时**不等于失败**，可用 resume 续等。"),
    _s("global.poll_interval", "OM_BRIDGE_POLL_INTERVAL", 5, "float",
       "轮询间隔（秒）。后端越慢越应调大，减少无谓请求。"),
    _s("global.validate", "OM_BRIDGE_VALIDATE", True, "bool",
       "提交前是否做静态校验。建议保持 true：校验零成本，能省下一次无效排队。"),
    _s("global.strict", "OM_BRIDGE_STRICT", False, "bool",
       "严格模式：把非阻断告警也视为失败。CI 场景有用。"),
    _s("global.log_level", "OM_BRIDGE_LOG_LEVEL", "INFO", "string",
       "日志级别（DEBUG/INFO/WARNING/ERROR）。CLI/MCP 下日志只写 stderr。"
       "⚠ 只认**进程环境变量**：日志必须在读到任何 .env 之前初始化，"
       "所以写在 .env 里的同名变量不生效（这是启动顺序的固有限制，不是疏漏）。"),
    _s("global.env_file", "OM_BRIDGE_ENV_FILE", "", "path",
       "显式指定配置文件路径。留空则按 workspace/.env 与 config/om-bridge.env 依次探测。"),
]

# --- ComfyUI 后端 ----------------------------------------------------------
# 别名列即旧 comfyui.env 里的变量名。规范名与旧名同时存在于同一优先级层。
COMFYUI_SETTINGS: list[Setting] = [
    _s("backend.comfyui.server_url", "OM_BRIDGE_COMFY_SERVER_URL", "http://localhost:8188", "string",
       "ComfyUI 基地址。所有工具的公共地址，单机部署只需设这一个。"
       "旧名 OM_BRIDGE_COMFYUI_SERVER_URL / COMFYUI_SERVER_URL 仍然有效（已废弃）。",
       ("OM_BRIDGE_COMFYUI_SERVER_URL", "COMFYUI_SERVER_URL"), False, True),
    _s("backend.comfyui.video_server_url", "OM_BRIDGE_COMFYUI_VIDEO_SERVER_URL", "", "string",
       "视频能力专用地址，留空继承 server_url（单机部署应留空）。",
       ("COMFYUI_VIDEO_SERVER_URL",)),
    _s("backend.comfyui.image_server_url", "OM_BRIDGE_COMFYUI_IMAGE_SERVER_URL", "", "string",
       "图像能力专用地址，留空继承 server_url。", ("COMFYUI_IMAGE_SERVER_URL",)),
    _s("backend.comfyui.music_server_url", "OM_BRIDGE_COMFYUI_MUSIC_SERVER_URL", "", "string",
       "音频/音乐能力专用地址，留空继承 server_url。", ("COMFYUI_MUSIC_SERVER_URL",)),
    _s("backend.comfyui.base_url", "OM_BRIDGE_COMFYUI_BASE_URL", "", "string",
       "⚠ 已废弃的别名，优先级最低。OpenMontage 从不读取这个名字，请改用 server_url。",
       ("COMFYUI_BASE_URL",), deprecated_alias=True),
    _s("backend.comfyui.connect_timeout", "OM_BRIDGE_COMFYUI_CONNECT_TIMEOUT", 15, "float",
       "探测 /system_stats 等轻量请求的超时（秒）。", ("COMFYUI_CONNECT_TIMEOUT",)),
    _s("backend.comfyui.read_timeout", "OM_BRIDGE_COMFYUI_READ_TIMEOUT", 900, "float",
       "等待一次渲染完成的总超时（秒）。", ("COMFYUI_READ_TIMEOUT",)),
    _s("backend.comfyui.poll_interval", "OM_BRIDGE_COMFYUI_POLL_INTERVAL", 5, "float",
       "/history 轮询间隔（秒）。", ("COMFYUI_POLL_INTERVAL",)),
    _s("backend.comfyui.api_token", "OM_BRIDGE_COMFYUI_API_TOKEN", "", "string",
       "Bearer token。仅当 ComfyUI 位于鉴权代理之后时需要。", ("COMFYUI_API_TOKEN",), secret=True),
    _s("backend.comfyui.auth_user", "OM_BRIDGE_COMFYUI_AUTH_USER", "", "string",
       "HTTP Basic 用户名（与 api_token 二选一）。", ("COMFYUI_AUTH_USER",), secret=True),
    _s("backend.comfyui.auth_password", "OM_BRIDGE_COMFYUI_AUTH_PASSWORD", "", "string",
       "HTTP Basic 密码。", ("COMFYUI_AUTH_PASSWORD",), secret=True),
    _s("backend.comfyui.upload_timeout", "OM_BRIDGE_COMFYUI_UPLOAD_TIMEOUT", 300, "float",
       "单次媒体上传的超时（秒）。", ()),
    _s("backend.comfyui.object_info_timeout", "OM_BRIDGE_COMFYUI_OBJECT_INFO_TIMEOUT", 180, "float",
       "拉取 /object_info 的超时（秒）。节点多、插件多的实例会明显偏慢。", ()),
]

# --- MiniMax H3 方案 -------------------------------------------------------
# 别名列对应旧 comfyui.env 的 COMFYUI_MINIMAX_H3_* 一组。
H3_SETTINGS: list[Setting] = [
    _s("solution.minimax_h3.width", "OM_BRIDGE_SOLUTION_MINIMAX_H3_WIDTH", 864, "integer",
       "输出宽度，必须是 32 的倍数。", ("COMFYUI_MINIMAX_H3_WIDTH",)),
    _s("solution.minimax_h3.height", "OM_BRIDGE_SOLUTION_MINIMAX_H3_HEIGHT", 480, "integer",
       "输出高度，必须是 32 的倍数。", ("COMFYUI_MINIMAX_H3_HEIGHT",)),
    _s("solution.minimax_h3.length", "OM_BRIDGE_SOLUTION_MINIMAX_H3_LENGTH", 124, "integer",
       "帧数（24fps），会吸附到 17k+5 栅格：5, 22, 39, ... 124≈5.2s。",
       ("COMFYUI_MINIMAX_H3_LENGTH",)),
    _s("solution.minimax_h3.fps", "OM_BRIDGE_SOLUTION_MINIMAX_H3_FPS", 24, "integer",
       "封装帧率。", ("COMFYUI_MINIMAX_H3_FPS",)),
    _s("solution.minimax_h3.steps", "OM_BRIDGE_SOLUTION_MINIMAX_H3_STEPS", 0, "integer",
       "采样步数。0 = 按模式自动（ref2v 用 4，其余用 8）。必须与所选 LoRA 配对。",
       ("COMFYUI_MINIMAX_H3_STEPS",)),

    _s("solution.minimax_h3.unet", "OM_BRIDGE_SOLUTION_MINIMAX_H3_UNET",
       "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "string",
       "t2v/i2v/flf2v 用的扩散模型（fl2va 系列）。", ("COMFYUI_MINIMAX_H3_UNET",)),
    _s("solution.minimax_h3.clip", "OM_BRIDGE_SOLUTION_MINIMAX_H3_CLIP",
       "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "string",
       "Qwen3-VL 文本编码器。", ("COMFYUI_MINIMAX_H3_CLIP",)),
    _s("solution.minimax_h3.video_vae", "OM_BRIDGE_SOLUTION_MINIMAX_H3_VIDEO_VAE",
       "minimax_h3_video_vae_fp16.safetensors", "string",
       "视频 VAE。", ("COMFYUI_MINIMAX_H3_VIDEO_VAE",)),
    _s("solution.minimax_h3.audio_vae", "OM_BRIDGE_SOLUTION_MINIMAX_H3_AUDIO_VAE",
       "minimax_h3_audio_vae_fp32.safetensors", "string",
       "音频 VAE。H3 是音视频联合生成，缺它就没有音轨。", ("COMFYUI_MINIMAX_H3_AUDIO_VAE",)),
    _s("solution.minimax_h3.turbo_lora", "OM_BRIDGE_SOLUTION_MINIMAX_H3_TURBO_LORA",
       "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors", "string",
       "8 步 Turbo LoRA（配合 steps=8）。", ("COMFYUI_MINIMAX_H3_TURBO_LORA",)),

    _s("solution.minimax_h3.ref2v_unet", "OM_BRIDGE_SOLUTION_MINIMAX_H3_REF2V_UNET",
       "minimax_h3_ref2va_pruned_int8_convrot.safetensors", "string",
       "ref2v 专用扩散模型（ref2va 系列）。与 fl2va **不可互换**。",
       ("COMFYUI_MINIMAX_H3_REF2V_UNET",)),
    _s("solution.minimax_h3.ref2v_turbo_lora", "OM_BRIDGE_SOLUTION_MINIMAX_H3_REF2V_TURBO_LORA",
       "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", "string",
       "ref2v 专用 4 步 Turbo LoRA（配合 steps=4）。",
       ("COMFYUI_MINIMAX_H3_REF2V_TURBO_LORA",)),
    _s("solution.minimax_h3.ref_image_size", "OM_BRIDGE_SOLUTION_MINIMAX_H3_REF2V_IMAGE_SIZE",
       "match", "string",
       "参考图缩放模式：match=缩到生成分辨率（快）；max=保留 2048px 短边（身份更保真、慢数倍）。",
       ("COMFYUI_MINIMAX_H3_REF2V_IMAGE_SIZE",)),
    _s("solution.minimax_h3.ref_images", "OM_BRIDGE_SOLUTION_MINIMAX_H3_REF2V_IMAGES",
       [], "list",
       "默认参考图文件名（已位于后端 input 目录中的名字）。仅在 ref2v 未显式给出参考图时生效。",
       ("COMFYUI_MINIMAX_H3_REF2V_IMAGES",)),

    _s("solution.minimax_h3.preflight_models", "OM_BRIDGE_SOLUTION_MINIMAX_H3_PREFLIGHT_MODELS",
       [], "list",
       "用于向外部框架声明「本机 H3 就绪」的扩散权重要求（逗号分隔）。"
       "由 OpenMontage 集成在发布托管块时读取并写出 COMFYUI_H3_LOCAL_MODELS。",
       ("COMFYUI_H3_LOCAL_MODELS",)),
    _s("solution.minimax_h3.graph_dir", "OM_BRIDGE_SOLUTION_MINIMAX_H3_GRAPH_DIR",
       "", "path",
       "物化后的计算图落盘目录（外部框架需要「文件路径 + 节点 ID」而非内存图时使用）。"
       "留空则用 <workspace>/var/graphs。",
       ()),
]

# 说明（迁移相关）：旧 comfyui.env 里的以下四个变量**不再被读取**，因此已从登记表移除：
#   COMFYUI_MINIMAX_H3_WORKFLOW_PATH / _OUTPUT_NODE
#   COMFYUI_MINIMAX_H3_REF2V_WORKFLOW_PATH / _OUTPUT_NODE
# 它们存在的唯一原因是"OpenMontage 必须拿到一个文件路径 + 节点 ID"。
# 现在这件事由集成层负责（om-bridge publish / gen-workflow 现场生成并记录），
# 核心层不再持有"预生成文件"这个中间态 —— 少一个状态就少一类不同步的问题。

ALL_SETTINGS: list[Setting] = GLOBAL_SETTINGS + COMFYUI_SETTINGS + H3_SETTINGS
SETTINGS_BY_KEY: dict[str, Setting] = {s.key: s for s in ALL_SETTINGS}
SETTINGS_BY_ENV: dict[str, Setting] = {s.env: s for s in ALL_SETTINGS}
SETTINGS_BY_ALIAS: dict[str, Setting] = {a: s for s in ALL_SETTINGS for a in s.aliases}


# ===========================================================================
# .env 解析（零依赖）
# ===========================================================================
def parse_env_file(path: str | Path) -> dict[str, str]:
    """解析一个 dotenv 风格的文件。

    刻意**不用** python-dotenv：核心运行时零依赖是硬约束（生产 GPU 主机常常
    没有 pip 镜像）。这里实现的是 dotenv 的常用子集：

    * ``KEY=VALUE``
    * ``# 注释`` 与空行忽略
    * ``export KEY=VALUE`` 前缀容忍
    * 值两侧的成对引号剥除（``KEY="a b"``）
    * 行尾 ``#`` 注释**不**剥除（值里合法地可以含 ``#``，例如颜色或 URL 片段）

    最后一条是有意的取舍：宁可少一个便利功能，也不要静默截断用户的值。
    """
    values: dict[str, str] = {}
    path = Path(path)
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name or not (name[0].isalpha() or name[0] == "_"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[name] = value
    return values


def default_env_candidates(workspace: str | Path = ".") -> list[Path]:
    """按优先级返回默认的配置文件候选路径。

    顺序即优先级：越靠前的越"贴身"，越往后越"通用"。
    用**第一个存在的文件**，不合并多个 —— 合并会让"这个值到底哪来的"变成玄学。
    """
    ws = Path(workspace)
    return [
        ws / ".env",  # 工作区根：部署机上的实际配置
        ws / "config" / "om-bridge.env",  # 工程约定位置
        Path.home() / ".config" / "om-bridge" / "om-bridge.env",  # 用户级默认
    ]


# ===========================================================================
# 配置对象
# ===========================================================================
class Config:
    """分层配置读取器。

    典型用法::

        cfg = load_config()                       # 自动探测配置文件
        cfg.get("global.timeout")                 # 规范键
        cfg.backend("comfyui").get("server_url")  # 作用域视图
        cfg.explain("backend.comfyui.server_url") # 这个值从哪来？
    """

    def __init__(
        self,
        file_values: dict[str, str] | None = None,
        environ: dict[str, str] | None = None,
        overrides: dict[str, Any] | None = None,
        *,
        env_file: str | None = None,
    ) -> None:
        self.file_values = dict(file_values or {})
        self.environ = dict(os.environ if environ is None else environ)
        self.overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
        self.env_file = env_file
        self._warnings: list[str] = []
        self._resolve_sources()

    # -- 解析 ---------------------------------------------------------------
    def _resolve_sources(self) -> None:
        """确定每个设置项的取值与来源，并记录规范名/别名冲突。"""
        self._values: dict[str, Any] = {}
        self._sources: dict[str, str] = {}

        for setting in ALL_SETTINGS:
            # 1) 显式覆盖
            if setting.key in self.overrides:
                self._values[setting.key] = self.overrides[setting.key]
                self._sources[setting.key] = "override"
                continue

            # 2) 进程环境（规范名优先，其次别名）
            found_env = None
            for name in (setting.env, *setting.aliases):
                if name in self.environ and self.environ[name] != "":
                    found_env = name
                    break
            if found_env:
                self._values[setting.key] = self._coerce(setting, self.environ[found_env])
                self._sources[setting.key] = f"env:{found_env}"
                self._note_alias_conflict(setting)
                continue

            # 3) 配置文件（同样的规范名优先规则）
            found_file = None
            for name in (setting.env, *setting.aliases):
                if self.file_values.get(name, "") != "":
                    found_file = name
                    break
            if found_file:
                self._values[setting.key] = self._coerce(setting, self.file_values[found_file])
                self._sources[setting.key] = f"file:{found_file}"
                self._note_alias_conflict(setting, in_file=True)
                continue

            # 4) 内置默认
            if setting.default is not None:
                self._values[setting.key] = setting.default
            self._sources[setting.key] = "default"

    def _note_alias_conflict(self, setting: Setting, *, in_file: bool = False) -> None:
        """规范名与别名同时出现且值不同 -> 提示，避免"改了旧名却没生效"的困惑。"""
        pool = self.file_values if in_file else self.environ
        canonical = pool.get(setting.env)
        for alias in setting.aliases:
            alias_value = pool.get(alias)
            if not alias_value:
                continue
            if canonical and str(canonical) != str(alias_value):
                self._warnings.append(
                    f"{setting.key}: 规范名 {setting.env}={canonical!r} 与旧名 "
                    f"{alias}={alias_value!r} 冲突，已采用规范名"
                )

    @staticmethod
    def _coerce(setting: Setting, raw: Any) -> Any:
        """按声明类型还原取值。文件里一切都是字符串。"""
        if raw is None or raw == "":
            return setting.default
        if setting.type == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")
        if setting.type == "integer":
            try:
                return int(float(str(raw).strip()))
            except ValueError:
                raise ConfigError(
                    f"{setting.key} 期望整数，收到 {raw!r}",
                    hint=f"检查 {setting.env}（或旧名 {'/'.join(setting.aliases)}）",
                ) from None
        if setting.type == "float":
            try:
                return float(str(raw).strip())
            except ValueError:
                raise ConfigError(
                    f"{setting.key} 期望数字，收到 {raw!r}",
                    hint=f"检查 {setting.env}",
                ) from None
        if setting.type == "list":
            if isinstance(raw, (list, tuple)):
                return list(raw)
            return [item.strip() for item in str(raw).split(",") if item.strip()]
        return raw

    # -- 读取 ---------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """按规范键取值。未知键返回 ``default``（不抛异常）。

        未知键不报错是有意的：它让"方案自定义了一个未登记的配置项"这种扩展用法可行。
        """
        if key in self._values:
            return self._values[key]
        return default

    def require(self, key: str) -> Any:
        """取必填项，缺失即抛错。"""
        value = self.get(key)
        if value in (None, "", [], ()):
            setting = SETTINGS_BY_KEY.get(key)
            hint = f"设置 {setting.env}" if setting else ""
            raise ConfigMissingError(f"缺少必需配置项 {key}", hint=hint)
        return value

    def get_int(self, key: str, default: int = 0) -> int:
        value = self.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        value = self.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "y")

    def source(self, key: str) -> str:
        """返回取值的来源标签（``override`` / ``env:VAR`` / ``file:VAR`` / ``default``）。"""
        return self._sources.get(key, "unknown")

    def is_default(self, key: str) -> bool:
        return self.source(key) == "default"

    # -- 作用域视图 ---------------------------------------------------------
    def backend(self, name: str = "comfyui") -> ScopedConfig:
        """后端配置视图：``cfg.backend('comfyui').get('server_url')``。"""
        return ScopedConfig(self, f"backend.{name}")

    def solution(self, key: str) -> ScopedConfig:
        """方案配置视图：``cfg.solution('minimax_h3').get('unet')``。"""
        return ScopedConfig(self, f"solution.{key}")

    @property
    def workspace(self) -> Path:
        return Path(self.get("global.workspace", ".")).expanduser().resolve()

    def output_dir(self) -> Path:
        """产物落盘目录。未显式配置时落在 ``<workspace>/var/output``。"""
        configured = self.get("global.output_dir")
        path = Path(configured).expanduser() if configured else self.workspace / "var" / "output"
        return path.resolve()

    def resolve_path(self, raw: str | Path) -> Path:
        """把相对路径按 workspace 解析成绝对路径。"""
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (self.workspace / path).resolve()

    # -- 自省 ---------------------------------------------------------------
    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    def explain(self, key: str) -> dict:
        """说明单个配置项的当前状态。供 ``om-bridge config explain`` 使用。"""
        setting = SETTINGS_BY_KEY.get(key)
        if setting is None:
            return {"key": key, "known": False}
        value = self.get(key)
        return {
            "key": key,
            "known": True,
            "env": setting.env,
            "aliases": list(setting.aliases),
            "value": "***" if setting.secret and value else value,
            "source": self.source(key),
            "default": setting.default,
            "type": setting.type,
            "description": setting.description,
        }

    def report(self, *, include_unknown_env: bool = False) -> dict:
        """全量配置报告。``doctor`` 与文档生成都基于它。

        ``include_unknown_env=True`` 时额外列出"看起来是本项目的变量、但程序并不读"的键。
        这是"我改了配置却没生效"最有效的自助答案 —— 它直接把 no-op 变量指出来。

        ⚠ 必须**同时扫进程环境与已加载的配置文件**：
        用户写错变量名时，绝大多数情况是写在 ``.env`` 里（而不是 ``export`` 出来的），
        只扫 ``os.environ`` 会让最常见的情形查不出来。
        """
        entries = []
        for setting in ALL_SETTINGS:
            entry = self.explain(setting.key)
            entries.append(entry)

        payload: dict[str, Any] = {
            "env_file": self.env_file or "(未加载)",
            "entries": entries,
            "warnings": self._warnings,
        }
        if include_unknown_env:
            known = set(SETTINGS_BY_ENV) | set(SETTINGS_BY_ALIAS)
            candidates: dict[str, str] = {}
            for source, mapping in (("env", self.environ), ("file", self.file_values)):
                for name in mapping:
                    if name.startswith(("COMFYUI_", "OM_BRIDGE_")) and name not in known:
                        candidates.setdefault(name, source)
            payload["unregistered_env"] = [
                {"name": name, "origin": origin}
                for name, origin in sorted(candidates.items())
            ]
        return payload

    def items(self) -> Iterator[tuple[str, Any]]:
        for setting in ALL_SETTINGS:
            yield setting.key, self.get(setting.key)


class ScopedConfig:
    """带前缀的配置视图，让 ``cfg.backend('x').get('server_url')`` 自然可读。"""

    def __init__(self, config: Config, prefix: str) -> None:
        self._config = config
        self._prefix = prefix

    def key(self, name: str) -> str:
        return f"{self._prefix}.{name}"

    def get(self, name: str, default: Any = None) -> Any:
        return self._config.get(self.key(name), default)

    def require(self, name: str) -> Any:
        return self._config.require(self.key(name))

    def get_int(self, name: str, default: int = 0) -> int:
        return self._config.get_int(self.key(name), default)

    def get_float(self, name: str, default: float = 0.0) -> float:
        return self._config.get_float(self.key(name), default)

    def get_bool(self, name: str, default: bool = False) -> bool:
        return self._config.get_bool(self.key(name), default)

    def source(self, name: str) -> str:
        return self._config.source(self.key(name))

    def registered(self) -> list[str]:
        """该作用域下已登记的设置名（不含前缀）。"""
        prefix = self._prefix + "."
        return [s.key[len(prefix):] for s in ALL_SETTINGS if s.key.startswith(prefix)]

    def as_dict(self) -> dict[str, Any]:
        return {name: self.get(name) for name in self.registered()}


def load_config(
    env_file: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
    workspace: str | Path | None = None,
) -> Config:
    """加载配置。

    配置文件查找顺序（取**第一个存在**的，不合并）::

        1. 参数 env_file
        2. 环境变量 OM_BRIDGE_ENV_FILE
        3. <workspace>/.env
        4. <workspace>/config/om-bridge.env
        5. ~/.config/om-bridge/om-bridge.env
    """
    env = dict(os.environ if environ is None else environ)
    ws = Path(workspace or env.get("OM_BRIDGE_WORKSPACE") or ".")

    chosen: Path | None = None
    if env_file:
        candidate = Path(env_file).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"指定的配置文件不存在：{candidate}")
        chosen = candidate
    elif env.get("OM_BRIDGE_ENV_FILE"):
        chosen = Path(env["OM_BRIDGE_ENV_FILE"]).expanduser()
    else:
        for candidate in default_env_candidates(ws):
            if candidate.is_file():
                chosen = candidate
                break

    file_values = parse_env_file(chosen) if chosen else {}
    # 单次解析就完成全部层级的合并；不做"边读边合并"，避免出现读取顺序依赖
    return Config(file_values, env, overrides, env_file=str(chosen) if chosen else None)
