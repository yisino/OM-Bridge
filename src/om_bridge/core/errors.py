"""OM-Bridge 异常层级与进程退出码。

设计要点
--------
1. 所有异常都继承 :class:`OmBridgeError`，调用方用 ``except OmBridgeError`` 即可兜住
   全部"可预期的运行时故障"，不必逐个列举。
2. 每个异常类自带 **退出码**（``exit_code``），这样 CLI / shell 调用方拿到的是**稳定契约**，
   而不是"某次重构后数字变了"。旧版 ``comfyui_run.py`` 已经对外承诺过一组退出码，
   这里刻意沿用同样的编号，避免既有脚本与文档失效。
3. 异常只承载"发生了什么"，不承载"怎么打印"。渲染交给上层（CLI 输出 JSON、MCP 输出 content），
   这样同一套异常可以同时服务人类和机器两种消费者。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 退出码契约（对外承诺，改动即破坏兼容性）
# ---------------------------------------------------------------------------
EXIT_OK = 0
"""成功。"""

EXIT_USAGE = 1
"""用法错误 / 配置缺失：参数不对，或必需的配置项没给。"""

EXIT_UNREACHABLE = 2
"""后端不可达：网络不通、服务没起、地址填错。"""

EXIT_BAD_WORKFLOW = 3
"""作业定义非法：工作流不是 API 格式、静态校验不通过、参数越界。"""

EXIT_REJECTED = 4
"""提交被后端拒绝：后端返回了校验错误（通常是工作流与后端节点版本不匹配）。"""

EXIT_TIMEOUT = 5
"""等待超时。**作业通常仍在运行** —— 正确做法是 resume，不是重提。"""

EXIT_RENDER = 6
"""渲染失败：后端执行过程中报错。"""

EXIT_CONFIG = 7
"""配置本身自相矛盾（例如同一变量同时给了两个冲突值）。"""


def _issue_to_dict(issue: object) -> object:
    """把一条校验结论转成可 JSON 序列化的结构。

    ⚠ 必须**同时**认 ``to_dict``（模型层 :class:`~om_bridge.core.models.Issue` 的命名）
    与 ``as_dict``（异常层的命名）。这不是宽容，是修一个真实缺陷：
    原先只探测 ``as_dict``，而 ``Issue`` 定义的是 ``to_dict`` ——
    ``hasattr`` 静默为 False，``Issue`` 对象被原样塞进 payload，
    最后靠 ``json.dump(..., default=str)`` 渲染成一句
    ``Issue(severity=<Severity.ERROR: 'error'>, message='...', code='...')`` 文本。
    结果是"结构化错误"在**完全不报错**的情况下退化成人类可读字符串，
    调用方按 ``code`` 分类、按 ``location`` 定位的逻辑全部失灵。
    """
    for name in ("to_dict", "as_dict"):
        method = getattr(issue, name, None)
        if callable(method):
            return method()
    return issue


class OmBridgeError(Exception):
    """所有 OM-Bridge 异常的基类。"""

    exit_code: int = EXIT_USAGE
    """该异常对应的进程退出码。"""

    stage: str = "unknown"
    """失败发生在流水线的哪个阶段，便于日志与前端提示定位。

    取值约定：``config`` | ``load`` | ``validate`` | ``upload`` | ``build`` | ``submit``
    | ``wait`` | ``collect`` | ``download`` | ``backend``。
    """

    def __init__(self, message: str, *, stage: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        if stage:
            self.stage = stage

    def as_dict(self) -> dict:
        """转成可 JSON 序列化的结构。

        让 CLI / MCP 能用**同一份**错误表示，避免两处各写一套拼装逻辑。
        """
        payload: dict = {
            "ok": False,
            "stage": self.stage,
            "error": self.message,
            "error_type": type(self).__name__,
        }
        if self.hint:
            payload["hint"] = self.hint
        return payload


# ---------------------------------------------------------------------------
# 配置层
# ---------------------------------------------------------------------------
class ConfigError(OmBridgeError):
    """配置缺失、类型不对或自相矛盾。"""

    exit_code = EXIT_CONFIG
    stage = "config"


class ConfigMissingError(ConfigError):
    """必需的配置项完全没有提供。"""


# ---------------------------------------------------------------------------
# 注册表 / 插件层
# ---------------------------------------------------------------------------
class RegistryError(OmBridgeError):
    """后端或方案的发现、解析失败。"""

    exit_code = EXIT_USAGE
    stage = "config"


class UnknownBackendError(RegistryError):
    """请求的后端名不在注册表中。"""


class UnknownSolutionError(RegistryError):
    """请求的方案名不在注册表中。"""


# ---------------------------------------------------------------------------
# 后端通信层
# ---------------------------------------------------------------------------
class BackendError(OmBridgeError):
    """后端交互失败的基类。"""

    stage = "backend"


class BackendUnavailableError(BackendError):
    """后端不可达（含 DNS / 连接被拒 / 超时 / 5xx）。"""

    exit_code = EXIT_UNREACHABLE
    stage = "backend"


class JobRejectedError(BackendError):
    """后端拒绝了这次提交。

    典型场景：工作流引用了后端上不存在的节点，或输入值的类型不对。
    这类错误**必须在提交前**用静态校验挡掉，走到这里说明校验漏了一类。
    """

    exit_code = EXIT_REJECTED
    stage = "submit"


class UploadError(BackendError):
    """媒体上传失败。"""

    exit_code = EXIT_REJECTED
    stage = "upload"


# ---------------------------------------------------------------------------
# 作业与结果层
# ---------------------------------------------------------------------------
class ValidationError(OmBridgeError):
    """作业定义/参数静态校验不通过。

    把每一条问题都放进 ``issues``，而不是只抛第一条 —— 用户希望一次看到全部问题。
    """

    exit_code = EXIT_BAD_WORKFLOW
    stage = "validate"

    def __init__(self, message: str, *, issues: list | None = None, hint: str | None = None):
        super().__init__(message, hint=hint)
        self.issues = issues or []

    def as_dict(self) -> dict:
        payload = super().as_dict()
        payload["issues"] = [_issue_to_dict(issue) for issue in self.issues]
        return payload


class JobTimeoutError(OmBridgeError):
    """等待作业完成时超时。

    ⚠ 关键语义：超时**不等于失败**。作业通常还在跑，重新提交会排进一个重复任务，
    和原任务抢同一块 GPU。正确恢复路径是 :meth:`om_bridge.core.session.Session.resume`。
    """

    exit_code = EXIT_TIMEOUT
    stage = "wait"

    def __init__(self, message: str, *, job_id: str | None = None, hint: str | None = None):
        super().__init__(message, hint=hint)
        self.job_id = job_id

    def as_dict(self) -> dict:
        payload = super().as_dict()
        payload["job_id"] = self.job_id
        payload["recoverable"] = True
        return payload


class RenderFailedError(OmBridgeError):
    """后端执行报错。"""

    exit_code = EXIT_RENDER
    stage = "collect"

    def __init__(self, message: str, *, job_id: str | None = None, details: list | None = None,
                 hint: str | None = None):
        super().__init__(message, hint=hint)
        self.job_id = job_id
        self.details = details or []

    def as_dict(self) -> dict:
        payload = super().as_dict()
        payload["job_id"] = self.job_id
        payload["details"] = self.details
        return payload
