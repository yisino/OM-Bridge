"""ComfyUI 后端包。

模块划分
--------
====================================  ==================================================
模块                                  职责
====================================  ==================================================
:mod:`~.client`                       传输层：HTTP 端点、认证、上传、超时、错误类型
:mod:`~.graph`                        计算图操作：格式判定、覆盖、动态槽位、产物收集
:mod:`~.inventory`                    ``/object_info`` 的归纳：有哪些节点、哪些资产
:mod:`~.validation`                   静态校验器：提交前把非法图挡下来
:mod:`~.backend`                      :class:`Backend` 实现，把上面几层拼成契约
:mod:`~.solutions`                    跑在这台 ComfyUI 上的具体模型方案
====================================  ==================================================

分层的意义：换模型只动 ``solutions/``，换服务只动 ``client``/``backend``，
而校验与图操作这两块**同时被两者复用**，不会重复实现。
"""

from __future__ import annotations

from .backend import ComfyUIBackend
from .client import ComfyUIClient
from .inventory import Inventory

__all__ = ["ComfyUIBackend", "ComfyUIClient", "Inventory"]
