"""pytest 公共配置。

只做一件事：把 ``src/`` 放进 ``sys.path``，让测试**不需要先安装**本项目就能跑。
这与 [ADR-0002](../../docs/design/0002-zero-runtime-dependencies.md)
的"零依赖/裸源码树可用"是同一个取向：clone 下来就能测。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

TEMPLATES = (
    SRC / "om_bridge" / "backends" / "comfyui" / "solutions" / "minimax_h3" / "templates"
)
