"""OM-Bridge 的数据契约（Data Contracts）。

这个模块是**架构的地基**：它定义了核心层与插件层之间传递的全部结构。
只要这些结构稳定，后端（Backend）和方案（Solution）就可以任意增删替换，
而 CLI / MCP / SDK 三个交付面完全不需要改动。

分层原则（重要）
----------------
* 本模块**只描述数据**，不包含任何 IO、网络、日志。
  这样它可以被后端实现、被测试、被未来的 Web 服务复用，而不会拖入副作用。
* 所有结构都提供 ``to_dict()``，保证能直接序列化为 JSON ——
  CLI 的机器可读输出、MCP 的工具返回、以及日志埋点共用同一套表示。
* ``raw`` / ``metadata`` 字段是刻意留的"逃生舱"：
  后端特有的信息放进去，核心层不解释、但会原样透传，避免为了个别需求污染公共契约。
"""

from __future__ import annotations

import dataclasses
import enum
import time
from dataclasses import dataclass, field
from typing import Any


# ===========================================================================
# 问题报告：静态校验与运行期告警的统一表示
# ===========================================================================
class Severity(str, enum.Enum):
    """问题的严重级别。

    只有 ``ERROR`` 会阻止提交；``WARNING`` 一律放行。
    把严重级别建模成枚举而不是字符串，是为了让"是否阻断"这件事只有一个判断点。
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Issue:
    """一条具体的校验结论。

    刻意做成"结构化"而不是拼好的字符串：调用方可以按 ``code`` 做程序化判断、
    按 ``location`` 定位到具体节点，而不是去正则匹配一句人话。
    """

    severity: Severity
    message: str
    code: str = ""
    """稳定的机器可读标识（如 ``graph.unknown_input``），便于上层做特殊处理或归类统计。"""
    location: str = ""
    """问题所在位置，通常是 ``节点ID.输入名`` 或文件路径。"""
    hint: str = ""
    """给用户的可执行建议。"""

    @property
    def is_blocking(self) -> bool:
        return self.severity is Severity.ERROR

    def to_dict(self) -> dict:
        payload = {"severity": self.severity.value, "message": self.message}
        for key in ("code", "location", "hint"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        return payload

    @classmethod
    def error(cls, message: str, **kw: Any) -> Issue:
        return cls(Severity.ERROR, message, **kw)

    @classmethod
    def warning(cls, message: str, **kw: Any) -> Issue:
        return cls(Severity.WARNING, message, **kw)

    @classmethod
    def info(cls, message: str, **kw: Any) -> Issue:
        return cls(Severity.INFO, message, **kw)


def blocking_issues(issues: list[Issue]) -> list[Issue]:
    """过滤出会阻断执行的问题。"""
    return [issue for issue in issues if issue.is_blocking]


# ===========================================================================
# 媒体输入
# ===========================================================================
class MediaKind(str, enum.Enum):
    """媒体的物理类型。后端据此决定用哪个上传通道。"""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


@dataclass
class MediaAsset:
    """一份媒体输入。

    两种来源二选一，语义**不同**，不要混用：

    * ``path`` —— 本地文件。提交前由后端上传到远端（需要 ``upload`` 能力）。
    * ``name`` —— 该文件**已经在远端**的标识符（例如 ComfyUI 主机 ``input/`` 目录里的文件名）。
      这条路径跳过上传，适合"素材已预先同步好"的批处理场景。

    同时给出两者时以 ``name`` 优先（显式指定的远端名最可信）。
    ``role`` 由方案自己定义取值（``first_frame`` / ``last_frame`` / ``reference_image`` …），
    核心层不解释，只负责按方案声明的顺序传给后端。
    """

    role: str
    kind: MediaKind = MediaKind.IMAGE
    path: str | None = None
    name: str | None = None
    """远端已有标识符。填了就不再上传。"""
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def remote_name(self) -> str | None:
        """最终应该写进后端载荷的名字（上传前的值可能是 None）。"""
        return self.name

    @property
    def needs_upload(self) -> bool:
        return self.name is None and self.path is not None

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {"role": self.role, "kind": self.kind.value}
        if self.path:
            payload["path"] = self.path
        if self.name:
            payload["name"] = self.name
        return payload


# ===========================================================================
# 产物
# ===========================================================================
@dataclass
class Artifact:
    """后端产出的一个文件。

    ``origin`` 记录它在后端侧的位置（ComfyUI 是 ``filename`` + ``subfolder`` + ``type``），
    ``local_path`` 只在真正下载之后才有值。
    """

    filename: str
    kind: str = "unknown"
    """后端给的文件类别，例如 ComfyUI 的 ``images``。

    ⚠ 注意 ComfyUI 的陷阱：``SaveVideo`` 节点产出的 mp4 在 ``/history`` 里
    挂在 ``images`` 键下（而不是 ``video``）。因此**不要**用 ``kind`` 判断是不是视频，
    要看文件扩展名或 ``origin``。
    """
    origin: dict[str, Any] = field(default_factory=dict)
    local_path: str | None = None
    bytes_written: int | None = None

    @property
    def is_video(self) -> bool:
        return self.filename.lower().endswith((".mp4", ".mov", ".mkv", ".webm"))

    @property
    def is_audio(self) -> bool:
        return self.filename.lower().endswith((".wav", ".mp3", ".flac", ".m4a", ".aac"))

    def to_dict(self) -> dict:
        payload = {"filename": self.filename, "kind": self.kind, "origin": self.origin}
        if self.local_path:
            payload["local_path"] = self.local_path
        if self.bytes_written is not None:
            payload["bytes_written"] = self.bytes_written
        return payload


# ===========================================================================
# 作业：方案产出 → 后端消费
# ===========================================================================
@dataclass
class JobSpec:
    """方案（Solution）交给后端（Backend）执行的作业。

    ``payload`` 的**类型由后端类型决定**，核心层不解释：

    * ComfyUI 后端 → ``payload`` 是 API 格式的工作流 dict，
      ``output_selector`` 是产出成品的节点 ID（字符串，即 OpenMontage 说的 ``output_node``）。
    * 未来的异步云端后端 → ``payload`` 可能是请求体，``output_selector`` 是结果路径表达式。

    这种"核心层只做搬运、后端自己解释"的设计，是新增后端时**不必改核心**的关键。
    """

    payload: Any
    output_selector: Any = None
    kind: str = "graph"
    validate_before_submit: bool = True
    """是否在提交前跑静态校验。默认 True —— 校验是零成本的，能省下一次无效排队。"""
    metadata: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """载荷的内容指纹。

        用途有两个：① 写进 provenance，便于事后确认"这一版产物到底跑的哪份图"；
        ② 识别重复提交（相同指纹 + 相同后端 = 很可能命中整图缓存）。
        """
        import hashlib
        import json

        blob = json.dumps(self.payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()


@dataclass
class JobHandle:
    """一个已提交作业的句柄。后端自己决定 ``job_id`` 是什么（ComfyUI 用 prompt_id）。"""

    job_id: str
    backend: str
    submitted_at: float = field(default_factory=time.time)
    queue_number: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "backend": self.backend,
            "submitted_at": self.submitted_at,
            "queue_number": self.queue_number,
        }


class JobStatus(str, enum.Enum):
    """作业状态。刻意做成后端无关的最小集合。"""

    UNKNOWN = "unknown"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED)


@dataclass
class JobState:
    """轮询一次得到的作业状态快照。"""

    job_id: str
    status: JobStatus
    progress: float | None = None
    queue_running: int | None = None
    queue_pending: int | None = None
    detail: str = ""
    errors: list[Any] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def to_dict(self) -> dict:
        payload = {"job_id": self.job_id, "status": self.status.value}
        if self.progress is not None:
            payload["progress"] = self.progress
        if self.queue_running is not None:
            payload["queue_running"] = self.queue_running
            payload["queue_pending"] = self.queue_pending
        if self.detail:
            payload["detail"] = self.detail
        if self.errors:
            payload["errors"] = self.errors
        return payload


# ===========================================================================
# 后端探测
# ===========================================================================
@dataclass
class ProbeReport:
    """一次后端健康探测的结果。

    同时承担两个职责：**连通性**（能否连上）与**能力盘点**（连上之后有什么）。
    之所以合并成一次探测，是因为现实里这两件事几乎总是一起问，
    分两次意味着两次网络往返和两个可能不一致的快照。
    """

    backend: str
    reachable: bool = False
    version: str | None = None
    device: str | None = None
    vram_total_gb: float | None = None
    vram_free_gb: float | None = None
    nodes: dict[str, bool] = field(default_factory=dict)
    """**关键节点**清单：人挑出来的一小批"必须有"的节点类，及其是否存在。

    ⚠ 这里只有关键节点，**不能用它回答"某个节点类在不在"** ——
    那类问题请用 :meth:`has_node_class`（查全量清单 :attr:`node_classes`）。
    这两件事曾经被混为一谈，后果是"后端明明有这个节点，却被报成缺失"：
    一个看起来很吓人的假错误，而根因只是一次错误的字典查询。
    """
    node_classes: set[str] = field(default_factory=set)
    """后端报告的**全部**节点类名（ComfyUI 就是 ``/object_info`` 的键集合）。

    刻意不进 :meth:`to_dict`：一个节点多的实例有 1200+ 个类，
    把它们塞进 JSON 输出会让报告膨胀几十倍，而绝大多数消费者只想知道数量。
    """
    assets: dict[str, list[str]] = field(default_factory=dict)
    """按类别列出后端上可用的资产（如 ``unet`` / ``vram`` / ``lora``）。"""
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def has_node(self, class_type: str) -> bool:
        """该节点类是否可用。

        优先查全量清单；后端没提供全量清单时（某些精简后端）退回关键清单。
        """
        if self.node_classes:
            return class_type in self.node_classes
        return self.nodes.get(class_type, False)

    def has_node_class(self, class_type: str) -> bool:
        """语义明确版的 :meth:`has_node`，供资产就绪度判断使用。"""
        return self.has_node(class_type)

    def has_asset(self, category: str, name: str) -> bool:
        return name in self.assets.get(category, [])

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {
            "backend": self.backend,
            "reachable": self.reachable,
        }
        if self.version:
            payload["version"] = self.version
        if self.device:
            payload["device"] = self.device
        if self.vram_total_gb is not None:
            payload["vram_total_gb"] = self.vram_total_gb
            payload["vram_free_gb"] = self.vram_free_gb
        if self.nodes:
            payload["nodes"] = self.nodes
        if self.node_classes:
            payload["node_class_count"] = len(self.node_classes)
        if self.assets:
            payload["assets"] = self.assets
        if self.errors:
            payload["errors"] = self.errors
        if self.notes:
            payload["notes"] = self.notes
        return payload


# ===========================================================================
# 请求与结果
# ===========================================================================
@dataclass
class GenerationRequest:
    """一次生成请求 —— 三个交付面（CLI / MCP / SDK）共同的输入形态。

    ``solution`` 是唯一必填项，其余全部可省：
    参数默认值来自方案自己的声明，媒体按角色分组，时间与输出位置来自配置。
    """

    solution: str
    prompt: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    """方案参数。键名由方案声明（``width`` / ``mode`` / ``seed`` …），核心层透传不校验。"""
    media: list[MediaAsset] = field(default_factory=list)
    output_dir: str | None = None
    filename_prefix: str | None = None
    timeout_s: float | None = None
    validate: bool = True
    """是否做静态校验。关掉它意味着**明知有问题也照样提交**（``--no-validate``）。"""
    preflight: bool = True
    """是否在提交前跑后端侧的载荷校验（需要一次网络往返读节点清单）。

    与 ``validate`` 分开，是因为两者的**成本与适用场景不同**：
    ``validate`` 是纯本地计算（参数范围、媒体槽位），永远该开着；
    ``preflight`` 要读后端清单，因此"离线物化一张计算图"时应当关掉 ——
    那时代码只是想拿到 JSON，不需要连得上后端。
    """
    node_overrides: dict[str, Any] = field(default_factory=dict)
    """逃生舱：直接修改作业载荷里某个节点的输入，键为 ``节点ID.输入名``。

    存在的意义是"最后一公里"——方案还没正式支持某个新节点参数时，
    使用者不必等上游发版就能验证。但它绕过方案的类型检查，属于高级用法。
    """
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {"solution": self.solution}
        if self.prompt:
            payload["prompt"] = self.prompt
        if self.params:
            payload["params"] = self.params
        if self.media:
            payload["media"] = [asset.to_dict() for asset in self.media]
        return payload


@dataclass
class Provenance:
    """产物溯源信息。

    为什么值得单独建模：生成式流水线的产物是**不可复现**的，
    唯一能事后回答"这条视频是怎么来的"的东西就是这份记录。
    它也是"配置漂移"排查的起点 —— 出问题时先对比 provenance，比猜快得多。
    """

    backend: str = ""
    solution: str = ""
    job_id: str | None = None
    payload_fingerprint: str = ""
    payload_source: str = "generated"
    """``generated``（方案现构造）| ``user_supplied``（用户给的现成作业文件）。"""
    resolved_params: dict[str, Any] = field(default_factory=dict)
    """方案解析后的**最终**参数值（默认值已填实），不是用户传入的原始子集。"""
    assets: dict[str, Any] = field(default_factory=dict)
    """本次用到/要求的模型资产，按角色归类。"""
    media: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = 0.0
    elapsed_s: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = {
            "backend": self.backend,
            "solution": self.solution,
            "payload_fingerprint": self.payload_fingerprint,
            "payload_source": self.payload_source,
            "resolved_params": self.resolved_params,
            "elapsed_s": round(self.elapsed_s, 2),
        }
        if self.job_id:
            payload["job_id"] = self.job_id
        if self.assets:
            payload["assets"] = self.assets
        if self.media:
            payload["media"] = self.media
        if self.extra:
            payload.update(self.extra)
        return payload


@dataclass
class GenerationResult:
    """一次生成的完整结果。**同时承载成功与失败**。

    不抛异常作为主要返回路径，是因为调用方（agent、shell 脚本）几乎总要对失败做
    "读错误、决定下一步"，异常在跨进程边界（CLI stdout / MCP 返回）上表达力很差。
    异常只用于"连结果都构造不出来"的情况。
    """

    ok: bool
    backend: str = ""
    solution: str = ""
    job_id: str | None = None
    elapsed_s: float = 0.0
    artifacts: list[Artifact] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    errors: list[Any] = field(default_factory=list)
    provenance: Provenance | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def videos(self) -> list[Artifact]:
        return [a for a in self.artifacts if a.is_video]

    @property
    def local_paths(self) -> list[str]:
        return [a.local_path for a in self.artifacts if a.local_path]

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "backend": self.backend,
            "solution": self.solution,
            "elapsed_s": round(self.elapsed_s, 2),
            "artifacts": [a.to_dict() for a in self.artifacts],
        }
        if self.job_id:
            payload["job_id"] = self.job_id
        if self.issues:
            payload["issues"] = [i.to_dict() for i in self.issues]
        if self.errors:
            payload["errors"] = self.errors
        if self.provenance:
            payload["provenance"] = self.provenance.to_dict()
        if self.raw:
            payload["raw"] = self.raw
        return payload


def as_dict(obj: Any) -> Any:
    """把任意契约对象/枚举递归转成可 JSON 序列化的结构。

    CLI 和 MCP 都要输出 JSON，各自手写转换容易漏字段、也不一致。
    统一走这里，保证"输出的就是契约里定义的"。
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, enum.Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        return {k: as_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {k: as_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [as_dict(v) for v in obj]
    return str(obj)
