"""OM-Bridge 核心层。

对外只暴露**契约与编排**，不暴露任何后端实现 —— 这样 `import om_bridge.core`
永远不会因为某个后端的可选依赖缺失而失败。
"""

from __future__ import annotations

from . import assets, backend, config, errors, logging, models, registry, schema, session, solution

__all__ = [
    "assets",
    "backend",
    "config",
    "errors",
    "logging",
    "models",
    "registry",
    "schema",
    "session",
    "solution",
]
