"""``python -m om_bridge`` 入口。

同时支持两种运行方式：

* 作为包运行：``python3 -m om_bridge``（需要 ``src`` 在 ``sys.path`` 上）；
* **作为脚本直接运行**：``python3 /path/to/src/om_bridge/__main__.py``。

第二种能力是刻意的 —— 生产 GPU 主机上经常既没有 pip 也没有 editable 安装，
`install.sh` 生成的启动器就是直接执行本文件。因此这里在包导入失败时
把 ``src`` 目录补进 ``sys.path``，让"零安装可运行"这条性质成立。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_importable() -> None:
    """在"以文件方式直接运行"时把 src 目录补进 sys.path。"""
    if __package__ not in (None, ""):
        return
    src_dir = Path(__file__).resolve().parents[1]
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))


_ensure_importable()

from om_bridge.interfaces.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
