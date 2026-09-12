"""mock 后端 —— 确定性、离线、零网络的 Backend 参考实现。

它为什么存在（三个用途，缺一不可）
----------------------------------
1. **检验双层抽象**。契约（`core/backend.py`）是否真的"可被第二实现完整落地"，
   只有真的写第二个实现才知道 —— ComfyUI 后端写得再干净，也可能是
   "契约照着 ComfyUI 长出来的"。本模块只看契约不看 ComfyUI，是那块试金石。
2. **离线端到端**。Session → submit → poll → fetch 全路径可以在 CI、
   无 GPU 的机器上跑通，测试结果与"开发机上是否碰巧跑着 ComfyUI"无关。
3. **扩展作者的最短读物**。`docs/adding-a-backend.md` 讲规则，
   这里给一个**能跑的最小全量样例**（四个必选动作 + upload + 能力声明）。

刻意简化（命名过的取舍，不是疏漏）
----------------------------------
* **产物不是媒体文件**，而是作业转写 JSON（prompt、参数、媒体名、载荷全在里面）。
  假装产出一个 mp4 才是不诚实 —— 消费方会拿去 ffprobe。
* **作业状态只在进程内**（内存字典）。跨进程 `resume` 会得到 FAILED 而不是挂死；
  这是可感知的明确行为，不是静默丢状态。
* **job_id 由载荷指纹派生**（`mock-<sha256[:12]>`）。同载荷重复提交得到同一 id，
  恰好复现了 ComfyUI 整图缓存的语义，测试可以借此验证"防重复提交"类逻辑。

同步后端的示范
--------------
`core/backend.py` 说"同步后端 = submit 内部把活干完，poll 立刻返回终态"。
本模块就是那个形态的标准写法：submit 收理即完成，poll 恒 SUCCEEDED。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ...core.backend import Backend, Capability
from ...core.config import Config
from ...core.errors import JobRejectedError
from ...core.logging import diagnostic, get_logger
from ...core.models import (
    Artifact,
    JobHandle,
    JobSpec,
    JobState,
    JobStatus,
    MediaAsset,
    ProbeReport,
)

log = get_logger("mock.backend")


class MockBackend(Backend):
    """离线模拟后端：受理即完成，产物是作业转写 JSON。"""

    name = "mock"
    display_name = "Mock（离线模拟）"
    description = (
        "进程内模拟后端：不碰网络与 GPU，把作业原样转写成 JSON 产物。"
        "用于离线端到端测试、CI 与作为新增后端的参考实现。"
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
        super().__init__(config, **options)
        # 作业台账：job_id -> {"payload", "output_selector", "submitted_at"}
        # 进程内即终态（submit 时就"算完"了），所以台账只需保存转写所需材料。
        self._jobs: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------
    def probe(self) -> ProbeReport:
        # mock 永远"可达"：它不依赖任何外部服务。
        # 关键节点/资产清单留空 —— 空的语义是"这类没被报告"，
        # 资产就绪度评估会把它当作 declared 但无法判定，而不是误报缺失。
        return ProbeReport(
            backend=self.name,
            reachable=True,
            version="mock/1.0",
            device="in-process",
            notes=[
                "模拟后端：probe 恒可达，产物为作业转写 JSON，不做真实渲染",
            ],
        )

    # ------------------------------------------------------------------
    # 媒体上传（Capability.MEDIA_UPLOAD）
    # ------------------------------------------------------------------
    def upload(self, asset: MediaAsset) -> MediaAsset:
        """把本地文件复制进 <workspace>/var/mock-uploads/，以文件名为远端标识。

        真实后端在这里做 HTTP 上传；mock 用文件复制演示同一契约：
        返回**带 name 的新 MediaAsset**，原 asset 不动（调用方依赖这一点排序与透传）。
        """
        source = Path(asset.path or "")
        if not source.is_file():
            raise JobRejectedError(
                f"mock 上传失败：文件不存在 {source}",
                hint="Session 通常会在上传前校验文件存在；直接调用 upload 时请自查",
            )
        store = self._store_dir()
        dest = store / source.name
        shutil.copy2(source, dest)
        return MediaAsset(
            role=asset.role,
            kind=asset.kind,
            name=dest.name,
            metadata={**asset.metadata, "mock_store": str(store)},
        )

    def _store_dir(self) -> Path:
        store = self.config.resolve_path("var/mock-uploads")
        store.mkdir(parents=True, exist_ok=True)
        return store

    # ------------------------------------------------------------------
    # 作业三段式
    # ------------------------------------------------------------------
    def submit(self, job: JobSpec) -> JobHandle:
        payload = job.payload if isinstance(job.payload, dict) else {}
        if payload.get("simulate_failure"):
            # 由 echo 方案的参数显式请求：给测试与演示一条"结构化失败"路径。
            raise JobRejectedError(
                "mock：按请求模拟一次提交失败（simulate_failure=true）",
                hint="这是刻意模拟的失败，去掉 simulate_failure 参数即可恢复",
            )
        job_id = f"mock-{job.fingerprint()[:12]}"
        self._jobs[job_id] = {
            "payload": job.payload,
            "output_selector": job.output_selector,
            "kind": job.kind,
        }
        diagnostic("mock.backend", "收理作业 %s（payload %d bytes）", job_id, len(repr(job.payload)))
        return JobHandle(
            job_id=job_id,
            backend=self.name,
            queue_number=1,
            extra={"simulated": True},
        )

    def poll(self, handle: JobHandle) -> JobState:
        entry = self._jobs.get(handle.job_id)
        if entry is None:
            # 明确失败而不是挂死等待：mock 状态不跨进程，
            # "用一个进程提交、另一个进程 poll"必须立刻可见地失败。
            return JobState(
                handle.job_id,
                JobStatus.FAILED,
                detail="mock 的作业台账只在进程内：该 job_id 不属于当前进程"
                       "（mock 不支持跨进程 resume，这是刻意简化）",
            )
        artifact = Artifact(
            filename=f"{handle.job_id}.transcript.json",
            kind="mock",
            origin={
                "job_id": handle.job_id,
                "output_selector": entry["output_selector"],
            },
        )
        return JobState(
            handle.job_id,
            JobStatus.SUCCEEDED,
            progress=1.0,
            artifacts=[artifact],
        )

    def fetch(self, artifacts: Iterable[Artifact], dest_dir: str) -> list[Artifact]:
        target = Path(dest_dir)
        target.mkdir(parents=True, exist_ok=True)
        fetched: list[Artifact] = []
        for artifact in artifacts:
            entry = self._jobs.get(str(artifact.origin.get("job_id", "")))
            transcript = {
                "job_id": artifact.origin.get("job_id"),
                "output_selector": artifact.origin.get("output_selector"),
                "payload": entry["payload"] if entry else None,
                "note": None if entry else "作业台账不在当前进程内，只有句柄信息",
            }
            dest = target / artifact.filename
            blob = json.dumps(transcript, ensure_ascii=False, indent=2, default=str)
            dest.write_text(blob, encoding="utf-8")
            fetched.append(Artifact(
                filename=artifact.filename,
                kind=artifact.kind,
                origin=artifact.origin,
                local_path=str(dest),
                bytes_written=len(blob.encode("utf-8")),
            ))
        return fetched
