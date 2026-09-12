"""MiniMax H3 方案包。

对外只暴露 :class:`MinimaxH3Solution`；其余模块是它的实现细节。
但 ``manifest`` 与 ``builder`` 也被其它组件直接使用
（集成层物化计算图、测试做回归比对），因此一并导出。
"""

from __future__ import annotations

from . import builder, manifest
from .solution import MinimaxH3Solution

__all__ = ["MinimaxH3Solution", "builder", "manifest"]
