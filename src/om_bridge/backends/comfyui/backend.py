"""ComfyUI 后端实现。

它把 ComfyUI 的 HTTP 接口映射成 :class:`~om_bridge.core.backend.Backend` 契约。
**这里没有一行代码认识 MiniMax H3** —— 模型语义全部在
:mod:`om_bridge.backends.comfyui.solutions` 里。

地址解析（优先级从高到低）
--------------------------
::

    构造参数 base_url
      > backend.comfyui.<capability>_server_url      （规范名 OMB_COMFY_VIDEO_SERVER_URL 等）
      > backend.comfyui.server_url                   （规范名 OMB_COMFY_SERVER_URL）
      > http://localhost:8188

默认能力是 ``video``，因此不额外配置时的行为与旧实现完全相同
（旧实现也是 VIDEO_SERVER_URL 优先）。

⚠ 两个容易踩的坑，实现在这里但值得先知道
----------------------------------------
**1. 整图缓存。** ComfyUI 0.35+ 会缓存整张图的执行结果：如果提交的图与上一次
**逐字节相同**，它会秒回并复用原文件名。表现是"明明改了参数却出了旧视频"，
或"渲染只用了 0.4 秒"。要拿到真实计算必须改变图中某个值（通常是 ``noise_seed``）。
本模块会在检测到"完成得过快"时于结果里附加提示，避免用户被这个假象误导。

**2. 产物键名。**
``SaveVideo`` 的产物在 ``/history`` 里挂在 ``images`` 键下（不是 ``video``）。
详见 :func:`om_bridge.backends.comfyui.graph.collect_outputs`。
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ...core.backend import Backend, Capability
from ...core.config import Config
from ...core.errors import JobRejectedError
from ...core.logging import get_logger
from ...core.models import (
    Artifact,
    Issue,
    JobHandle,
    JobSpec,
    JobState,
    JobStatus,
    MediaAsset,
    ProbeReport,
)
from .client import ComfyUIClient
from .graph import collect_outputs, extract_errors, is_completed
from .inventory import Inventory
from .validation import validate_graph, validate_output_selector

log = get_logger("comfyui.backend")

# 完成得比这还快，几乎一定是命中了整图缓存（真实渲染最小也在数秒量级）
CACHE_SUSPECT_SECONDS = 2.0

# 探测报告里要检查的关键节点类。方案可以自己再声明更多。
CORE_NODES = (
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "LoraLoaderModelOnly",
    "KSamplerSelect",
    "BasicScheduler",
    "BasicGuider",
    "SamplerCustomAdvanced",
    "VAEDecode",
    "VAEDecodeAudio",
    "CreateVideo",
    "SaveVideo",
    "LoadImage",
)


class ComfyUIBackend(Backend):
    """本机/远端 ComfyUI 执行后端。"""

    name = "comfyui"
    display_name = "ComfyUI"
    description = (
        "通过 HTTP API 驱动一个 ComfyUI 实例。以 API 格式的计算图为作业载体，"
        "支持媒体上传、作业续等、以及提交前的静态校验。"
    )
    capabilities = frozenset(
        {
            Capability.VIDEO,
            Capability.IMAGE,
            Capability.AUDIO,
            Capability.MEDIA_UPLOAD,
            Capability.RAW_JOB,
            Capability.RESUME,
        }
    )

    def __init__(self, config: Config, **options: Any) -> None:
        # 先把后端专有选项摘出来，再交给基类保存剩余项，
        # 避免同一份配置同时存在于两处（那会导致 options 里有一份"看起来生效但其实没读"的副本）
        self.capability: str = options.pop("capability", "video")
        self._explicit_url: str | None = options.pop("base_url", None) or options.pop(
            "server_url", None
        )
        super().__init__(config, **options)
        self._clients: dict[str, ComfyUIClient] = {}
        self._inventory: Inventory | None = None

    # ------------------------------------------------------------------
    # 地址与客户端
    # ------------------------------------------------------------------
    def _resolve_url(self, capability: str | None) -> str:
        """按能力解析地址（顺序见模块文档）。"""
        if self._explicit_url:
            return str(self._explicit_url)
        section = self.config.backend("comfyui")
        candidates: list[Any] = []
        if capability:
            candidates.append(section.get(f"{capability}_server_url"))
        candidates.append(section.get("server_url"))
        for candidate in candidates:
            if candidate:
                return str(candidate).rstrip("/")
        return "http://localhost:8188"

    def client(self, capability: str | None = None) -> ComfyUIClient:
        """取（并缓存）某个能力的客户端。"""
        key = capability or self.capability
        if key not in self._clients:
            section = self.config.backend("comfyui")
            self._clients[key] = ComfyUIClient(
                self._resolve_url(capability),
                connect_timeout=section.get_float("connect_timeout", 15),
                read_timeout=section.get_float("read_timeout", 900),
                upload_timeout=section.get_float("upload_timeout", 300),
                object_info_timeout=section.get_float("object_info_timeout", 180),
                token=section.get("api_token", "") or "",
                user=section.get("auth_user", "") or "",
                password=section.get("auth_password", "") or "",
            )
        return self._clients[key]

    def endpoint(self) -> str:
        return self._resolve_url(self.capability)

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------
    def probe(self) -> ProbeReport:
        """连通性 + 节点/资产盘点。**不可达时返回报告而不是抛异常**。"""
        report = ProbeReport(backend=self.name)
        client = self.client()

        try:
            stats = client.system_stats()
        except Exception as exc:  # noqa: BLE001
            report.reachable = False
            report.errors.append(f"{type(exc).__name__}: {exc}")
            report.notes.append(f"目标地址：{client.base_url}")
            if hasattr(exc, "hint") and exc.hint:
                report.notes.append(f"排查建议：{exc.hint}")
            return report

        report.reachable = True
        system = stats.get("system") or {}
        devices = stats.get("devices") or [{}]
        device = devices[0] if devices else {}
        report.version = system.get("comfyui_version")
        report.device = device.get("name")
        if device.get("vram_total"):
            report.vram_total_gb = round(device["vram_total"] / 2**30, 2)
            report.vram_free_gb = round(device.get("vram_free", 0) / 2**30, 2)
            # 模型常驻会导致 free 很低，这是预期而非异常，提前说明避免误判
            if report.vram_free_gb is not None and report.vram_free_gb < 1.5:
                report.notes.append(
                    f"显存剩余仅 {report.vram_free_gb} GB —— 大模型常驻时属预期，"
                    "不代表异常"
                )

        try:
            inventory = self.inventory(refresh=True)
            report.nodes = {cls: inventory.has_node(cls) for cls in CORE_NODES}
            # 全量节点类清单必须一起带上：方案声明的是**任意**节点类
            # （如 MiniMaxH3ReferenceToVideo），用上面那份"关键节点"字典
            # 去回答"某类在不在"必然误报缺失。
            report.node_classes = set(inventory.node_classes)
            report.assets = dict(inventory.assets)
            report.raw["inventory"] = inventory.summary()
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"读取节点清单失败：{type(exc).__name__}: {exc}")
            report.notes.append("连通性正常但无法读取 /object_info，静态校验将不可用。")

        return report

    def inventory(self, *, refresh: bool = False) -> Inventory:
        """取（并缓存）实例能力快照。默认缓存 5 分钟，避免一次会话里重复拉取。"""
        if self._inventory is not None and not refresh:
            return self._inventory
        object_info = self.client().object_info(cache_seconds=0 if refresh else 300)
        self._inventory = Inventory.from_object_info(object_info)
        return self._inventory

    # ------------------------------------------------------------------
    # 作业执行
    # ------------------------------------------------------------------
    def submit(self, job: JobSpec) -> JobHandle:
        if not isinstance(job.payload, dict) or not job.payload:
            raise JobRejectedError("作业载荷为空或不是 API 格式的图")
        response = self.client().submit_prompt(job.payload)
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            raise JobRejectedError(
                f"后端接受了请求但没有返回 prompt_id：{response}",
                hint="这通常意味着 ComfyUI 版本与预期不符。",
            )
        return JobHandle(
            job_id=str(prompt_id),
            backend=self.name,
            queue_number=response.get("number"),
            extra={"client_id": response.get("client_id")},
        )

    def poll(self, handle: JobHandle) -> JobState:
        """查询作业状态。**不把"还没好"当成异常**。"""
        client = self.client()
        try:
            history = client.history(handle.job_id)
        except JobRejectedError as exc:
            # 作业被中断时 /history 可能查不到，如实上报失败而不是谎报运行中
            return JobState(handle.job_id, JobStatus.FAILED, detail=exc.message,
                            raw={"error": exc.as_dict()})

        entry = history.get(handle.job_id)
        if not entry:
            running, pending = self._queue_snapshot(client)
            return JobState(
                handle.job_id,
                JobStatus.RUNNING if running else JobStatus.PENDING,
                queue_running=running,
                queue_pending=pending,
                detail="已排队，等待执行",
            )

        status = entry.get("status") or {}
        if not is_completed(entry):
            running, pending = self._queue_snapshot(client)
            return JobState(
                handle.job_id, JobStatus.RUNNING,
                queue_running=running, queue_pending=pending, detail="执行中",
            )

        errors = extract_errors(status)
        artifacts = collect_outputs(entry)
        raw: dict[str, Any] = {"history_entry": entry}

        if errors:
            first = errors[0]
            detail = first.get("exception_message") if isinstance(first, dict) else str(first)
            return JobState(
                handle.job_id, JobStatus.FAILED,
                detail=f"执行失败：{detail or errors}",
                errors=errors, artifacts=artifacts, raw=raw,
            )

        if not artifacts:
            return JobState(
                handle.job_id, JobStatus.FAILED,
                detail="后端报告成功，但历史记录里没有任何产物文件",
                errors=["no_artifacts"], raw=raw,
            )

        elapsed = time.time() - handle.submitted_at
        if elapsed < CACHE_SUSPECT_SECONDS:
            # 见模块文档第 1 条：这类"秒回"是整图缓存命中，不是真的算了一遍
            raw["cache_hint"] = (
                f"作业在 {elapsed:.1f}s 内完成，极可能命中了 ComfyUI 的整图缓存"
                "（提交的图与上一次逐字节相同）。若要真的重新计算，请改动 noise_seed。"
            )
            log.warning("作业 %s 仅耗时 %.1fs，疑似整图缓存命中", handle.job_id, elapsed)

        return JobState(
            handle.job_id, JobStatus.SUCCEEDED,
            artifacts=artifacts, raw=raw,
        )

    @staticmethod
    def _queue_snapshot(client: ComfyUIClient) -> tuple[int | None, int | None]:
        """取队列长度。取不到不致命，返回 ``(None, None)``。"""
        try:
            queue = client.queue()
            return len(queue.get("queue_running", [])), len(queue.get("queue_pending", []))
        except Exception:  # noqa: BLE001
            return None, None

    def fetch(self, artifacts: Iterable[Artifact], dest_dir: str) -> list[Artifact]:
        """下载产物。**单个失败不中断其余** —— 部分成功比全盘失败有用。"""
        target = Path(dest_dir)
        target.mkdir(parents=True, exist_ok=True)
        client = self.client()
        downloaded: list[Artifact] = []

        for artifact in artifacts:
            origin = artifact.origin or {}
            try:
                blob = client.view_bytes(
                    artifact.filename,
                    subfolder=origin.get("subfolder", ""),
                    type_=origin.get("type", "output"),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("下载 %s 失败：%s", artifact.filename, exc)
                continue
            path = target / artifact.filename
            path.write_bytes(blob)
            artifact.local_path = str(path)
            artifact.bytes_written = len(blob)
            downloaded.append(artifact)
        return downloaded

    def upload(self, asset: MediaAsset) -> MediaAsset:
        """上传本地媒体，返回带远端名的副本。

        当前只支持图片：ComfyUI 的上传端点只有 ``/upload/image`` 这一条通道。
        视频/音频需要由调用方预先放到后端的 input 目录，然后用远端名引用。
        """
        if not asset.path:
            return asset
        name = self.client().upload_image(asset.path)
        return MediaAsset(
            role=asset.role,
            kind=asset.kind,
            path=asset.path,
            name=name,
            metadata=dict(asset.metadata),
        )

    # ------------------------------------------------------------------
    # 提交前校验
    # ------------------------------------------------------------------
    def preflight(self, job: JobSpec, *, probe: ProbeReport | None = None) -> list[Issue]:
        """把整张图对着真实实例静态校验一遍。

        这一步的价值在于是**零成本**的：只读 ``/object_info``（有缓存），
        就能把"节点没装、输入名拼错、文件名不存在、槽位越界"全部报出来，
        省下一次必然失败的排队。
        """
        if job.kind != "graph" or not isinstance(job.payload, dict):
            return []

        issues: list[Issue] = []
        label = job.metadata.get("label") or "workflow"
        try:
            inventory = self.inventory()
        except Exception as exc:  # noqa: BLE001
            return [Issue.warning(
                f"无法读取后端节点清单，跳过静态校验：{type(exc).__name__}: {exc}",
                code="preflight.skipped", location=label,
            )]

        issues.extend(validate_graph(job.payload, inventory, label))
        issues.extend(validate_output_selector(job.payload, job.output_selector, label))
        return issues

    # ------------------------------------------------------------------
    def close(self) -> None:
        # urllib 每次请求独立，没有连接池要关；保留钩子以便未来接入 WS 会话
        self._clients.clear()

    # ------------------------------------------------------------------
    def describe(self) -> dict:
        payload = super().describe()
        payload.update({
            "endpoint": self.endpoint(),
            "capability": self.capability,
            "capability_urls": {
                capability: self._resolve_url(capability)
                for capability in ("video", "image", "music")
            },
        })
        return payload
