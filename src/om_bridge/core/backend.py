"""Backend —— 执行后端的契约（"在哪儿跑"）。

职责边界（这是整个架构最重要的一条分界线）
------------------------------------------
**Backend 只负责"怎么把一份作业送到某个计算服务并取回产物"，不认识任何具体模型。**

它知道：地址、认证、超时、重试、上传通道、作业状态机、产物下载。
它**不知道**：MiniMax H3 是什么、要几张参考图、步数与 LoRA 怎么配对。

反过来，:class:`~om_bridge.core.solution.Solution` 知道模型语义，但不知道网络。

于是：
* 换掉算力来源（本机 ComfyUI → 云端 API）只改 Backend，全部方案不动；
* 加一个新模型只加 Solution，Backend 不动。

为什么用 ``submit/poll/fetch`` 三段式，而不是一个阻塞的 ``generate()``
--------------------------------------------------------------------
三段式是**唯一能同时覆盖同步与异步后端**的最小形态：

* 后端异步（ComfyUI 的 ``prompt_id``、云端任务 API）→ 天然适配；
* 后端同步（一次 HTTP 调用就出结果）→ ``submit`` 内部把活干完，
  ``poll`` 立刻返回终态即可；
* 长任务超时 → 因为句柄是显式对象，``resume`` 才有东西可接
  （这是原实现的真实痛点：一旦超时，只能靠记住 prompt_id 手动续等）。

阻塞式 ``generate()`` 被保留为 :class:`~om_bridge.core.session.Session` 上的便利方法，
而不是接口本身。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any, ClassVar

from .models import Artifact, Issue, JobHandle, JobSpec, JobState, MediaAsset, ProbeReport


class Capability:
    """后端能力的标准取值。

    用普通字符串常量而不是 Enum，是刻意的：第三方后端可以自报新能力
    （例如 ``"lipsync"``），核心层不该因为枚举里没有就拒绝它。
    """

    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    MEDIA_UPLOAD = "media_upload"
    """支持把本地媒体送到后端（没有它就只能用远端已有的素材）。"""
    RAW_JOB = "raw_job"
    """支持执行调用方直接给定的作业载荷（工作流），而不只是方案构造出来的作业。"""
    RESUME = "resume"
    """支持对超时作业续等，而不是只能重提。"""


class Backend(ABC):
    """执行后端抽象基类。

    子类必须声明 :attr:`name` 与 :attr:`capabilities`，并实现
    :meth:`probe` / :meth:`submit` / :meth:`poll` / :meth:`fetch`。
    """

    name: ClassVar[str] = ""
    """后端唯一标识，小写 + 下划线。用于配置键与 CLI 参数，一经发布不应更改。"""

    display_name: ClassVar[str] = ""
    """人类可读名称，仅用于输出。"""

    capabilities: ClassVar[frozenset[str]] = frozenset()
    """能力集合，取值见 :class:`Capability`。方案靠它做兼容性自检。"""

    description: ClassVar[str] = ""

    def __init__(self, config: Any, **options: Any) -> None:
        """
        :param config: :class:`~om_bridge.core.config.Config`，后端从中读自己的配置段。
        :param options: 构造期覆盖项（优先级高于配置文件），便于测试与临时切换。
        """
        self.config = config
        self.options = options

    # -- 元信息 -------------------------------------------------------------
    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def describe(self) -> dict:
        """给 CLI / MCP 的自描述。新增后端不必手写文档，这里的输出就是文档。"""
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "description": self.description,
            "capabilities": sorted(self.capabilities),
        }

    def endpoint(self) -> str | None:
        """后端的主地址（用于日志/报告）。非网络型后端可返回 ``None``。"""
        return None

    # -- 必选：四个动作 -----------------------------------------------------
    @abstractmethod
    def probe(self) -> ProbeReport:
        """探测连通性并盘点可用能力/资产。

        实现要求：**不可达时不要抛异常**，返回 ``reachable=False`` 的报告。
        原因：探针会被 preflight、doctor、MCP 的 check 工具共用，
        它们都需要"拿到一份说明为什么不行的报告"，而不是被异常打断。
        """

    @abstractmethod
    def submit(self, job: JobSpec) -> JobHandle:
        """提交作业，返回句柄。失败抛 :class:`~om_bridge.core.errors.JobRejectedError`。"""

    @abstractmethod
    def poll(self, handle: JobHandle) -> JobState:
        """查询作业状态。实现要求：**不抛异常表示"还没好"**，只如实返回状态。"""

    @abstractmethod
    def fetch(self, artifacts: Iterable[Artifact], dest_dir: str) -> list[Artifact]:
        """把产物下载到 ``dest_dir``，返回补好 ``local_path`` 的产物列表。"""

    # -- 可选：由能力开关决定 ------------------------------------------------
    def upload(self, asset: MediaAsset) -> MediaAsset:
        """上传一份本地媒体，返回带远端标识的副本。

        未声明 ``media_upload`` 能力的后端不应被调用；默认实现直接拒绝，
        而不是静默返回原值 —— 静默会让"上传没生效"变成一个难查的渲染错误。
        """
        raise NotImplementedError(
            f"后端 {self.name!r} 未声明 {Capability.MEDIA_UPLOAD} 能力，不能上传媒体"
        )

    def preflight(self, job: JobSpec, *, probe: ProbeReport | None = None) -> list[Issue]:
        """提交前的作业校验（默认不做任何事）。

        有能力的后端应在此把"会被后端拒绝的载荷"用**零成本**的方式挡下来，
        例如 ComfyUI 可以拿真实图去对 ``/object_info`` 做静态校验。
        这样用户得到的是一条可读的错误，而不是一次白排队的 400。
        """
        return []

    def resume(self, handle: JobHandle) -> JobState:
        """对一个已提交的作业重新取状态。默认等价于 :meth:`poll`。"""
        return self.poll(handle)

    # -- 生命周期 -----------------------------------------------------------
    def close(self) -> None:  # noqa: B027  故意留空：可选钩子，不该强迫每个后端都实现
        """释放资源。默认无操作；持有连接池/子进程的后端应覆写。"""

    def __enter__(self) -> Backend:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
