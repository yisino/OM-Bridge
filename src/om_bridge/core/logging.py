"""日志配置。

**硬约束：日志只能写 stderr。**
------------------------------
MCP 的 stdio 传输用 stdout 传递 JSON-RPC 消息，CLI 也用 stdout 输出机器可读的 JSON。
只要有**一行**日志跑到 stdout，协议就会被污染 —— 表现是宿主端报"invalid JSON"
而不是报日志内容。这类问题的排查成本极高，所以在这里一次性、全局地钉死。

因此本模块只往 ``StreamHandler(sys.stderr)`` 加处理器，并明确 ``propagate = False``
（否则 root logger 上任何第三方库配置的 stdout 处理器都会把它带出去）。
"""

from __future__ import annotations

import logging
import os
import sys

LOGGER_NAME = "om_bridge"
_CONFIGURED = False
_PROGRESS_ENABLED = True

_FORMAT = "%(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: str | int | None = None, *, force: bool = False) -> logging.Logger:
    """配置包级 logger 并返回它。

    幂等：重复调用不会叠加处理器（叠加会导致同一条日志打印 N 次，
    在长时间渲染里会淹没真正有用的输出）。
    """
    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)

    if _CONFIGURED and not force:
        return logger

    if level is None:
        level = os.environ.get("OMB_LOG_LEVEL") or "INFO"
    if isinstance(level, str):
        level = getattr(logging, level.strip().upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    # 关键：不向 root 传播，避免宿主环境里预置的 stdout 处理器把日志带进协议流
    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_logger(suffix: str = "") -> logging.Logger:
    """取一个带命名空间的 logger。"""
    name = f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME
    configure_logging()
    return logging.getLogger(name)


def progress(message: str) -> None:
    """面向人的进度提示，**总是**输出（不受日志级别影响）。

    和诊断日志分开，是因为两者的读者不同：
    进度（"已提交"、"等待中 30s"）是普通用户要看的运行状态；
    诊断（HTTP 细节、缓存命中）是排查时才关心的。
    把进度塞进 logging 会导致调高日志级别时用户反而看不到进度了。

    可以被 :func:`set_progress_enabled` 关掉 —— 因为存在一类调用方
    （MCP 宿主、被别的脚本解析输出的场景）不希望 stderr 里出现无关文字。
    """
    if not _PROGRESS_ENABLED:
        return
    print(message, file=sys.stderr, flush=True)


def set_progress_enabled(enabled: bool) -> None:
    """全局开关进度输出。关闭只影响进度行，不影响诊断日志。"""
    global _PROGRESS_ENABLED
    _PROGRESS_ENABLED = enabled


def diagnostic(suffix: str, message: str, *args: object) -> None:
    """受 ``OMB_LOG_LEVEL`` 控制的诊断日志。"""
    get_logger(suffix).debug(message, *args)

