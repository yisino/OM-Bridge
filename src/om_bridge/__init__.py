"""OM-Bridge —— 可扩展的 AI 生成能力桥接层。

定位
----
把"调用某个算力服务去生成内容"这件事，拆成两个可独立替换的维度：

* **Backend（后端）**：算力来源与传输方式。今天接的是本机 ComfyUI，
  明天可以是另一台 ComfyUI、云端 ComfyUI 集群、或某个异步视频 API。
* **Solution（方案）**：模型语义与参数。今天接的是 MiniMax H3 的四种形态，
  明天可以接 WAN、LTX、或者一个全新的模型。

两者通过 :mod:`om_bridge.core.models` 里的数据契约通信，彼此不知道对方的实现。

交付面
------
同一套核心，三种用法（都基于同一份配置，不重复维护）::

    # 1. 命令行（人用，也可被任何 shell 脚本调用）
    om-bridge generate --solution minimax_h3.ref2v --prompt "..." --ref-image hero.png

    # 2. MCP（agent 用；工具清单从注册表自动生成，加方案即加工具）
    om-bridge-mcp

    # 3. SDK（Python 项目内直接 import）
    from om_bridge import Session, GenerationRequest
    with Session() as s:
        result = s.run(GenerationRequest(solution="minimax_h3", prompt="..."))

零运行时依赖
------------
核心只用标准库。目标机常常是没有 pip 镜像的生产 GPU 主机，
"装不上依赖"不应该成为"用不了"的原因。第三方依赖只出现在可选扩展中，
且都必须能缺省降级。
"""

from __future__ import annotations

from .core.assets import AssetCategory, AssetRequirement
from .core.backend import Backend, Capability
from .core.config import Config, load_config
from .core.errors import (
    BackendUnavailableError,
    JobRejectedError,
    JobTimeoutError,
    OmBridgeError,
    ValidationError,
)
from .core.models import (
    Artifact,
    GenerationRequest,
    GenerationResult,
    Issue,
    JobHandle,
    JobSpec,
    JobState,
    JobStatus,
    MediaAsset,
    MediaKind,
    ProbeReport,
    Provenance,
    Severity,
)
from .core.registry import Registry, get_registry
from .core.schema import ParamSchema, ParamSpec
from .core.session import PreparedJob, Session
from .core.solution import MediaSlot, Solution

__version__ = "0.1.0"

__all__ = [
    # 编排
    "Session",
    "PreparedJob",
    # 契约基类
    "Backend",
    "Solution",
    "Capability",
    # 数据契约
    "Artifact",
    "GenerationRequest",
    "GenerationResult",
    "Issue",
    "JobHandle",
    "JobSpec",
    "JobState",
    "JobStatus",
    "MediaAsset",
    "MediaKind",
    "MediaSlot",
    "ProbeReport",
    "Provenance",
    "Severity",
    "AssetCategory",
    "AssetRequirement",
    "ParamSchema",
    "ParamSpec",
    # 配置与注册
    "Config",
    "load_config",
    "Registry",
    "get_registry",
    # 异常
    "OmBridgeError",
    "ValidationError",
    "JobTimeoutError",
    "JobRejectedError",
    "BackendUnavailableError",
    "__version__",
]
