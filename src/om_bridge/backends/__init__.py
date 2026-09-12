"""后端插件目录 —— 每个子包就是一个"执行后端"。

新增一个后端（例如接入云端视频 API）
------------------------------------
1. 新建目录 ``src/om_bridge/backends/<name>/``（``<name>`` 即后端标识，小写+下划线）；
2. 在其中实现 ``backend.py``，定义一个 :class:`~om_bridge.core.backend.Backend` 子类，
   声明类属性 ``name`` / ``capabilities``，并实现 ``probe`` / ``submit`` / ``poll`` / ``fetch``；
3. 如需自己的方案，建 ``<name>/solutions/<solution>/``（结构见
   ``comfyui/solutions/minimax_h3/``）；
4. 完事。**不需要改任何核心文件** —— registry 会自动发现（``pkgutil`` 扫描 + 可选 entry point）。

发布成独立 PyPI 包
------------------
在第三方包的 ``pyproject.toml`` 里声明::

    [project.entry-points."om_bridge.backends"]
    myapi = "my_pkg.backend:MyApiBackend"

装了这个包的环境会自动多出一个后端，与内置后端在运行时完全等价。

目录约定
--------
* 只有 :class:`Backend` 子类的模块会被登记为后端；
* 只有 :class:`Solution` 子类的模块会被登记为方案；
* ``solutions/`` 是**约定目录名**，registry 只在这个名字下递归找方案；
* 以 ``_`` 开头的模块/子包会被扫描跳过，可用于放私有辅助代码。
"""

from __future__ import annotations

__all__: list[str] = []
