"""Session —— 编排层，把"请求"变成"产物"。

它承担的职责（也是唯一该由它承担的职责）
----------------------------------------
把一次生成的全过程串起来，并让每一步都可观测、可中断、可恢复::

    解析方案 → 解析参数 → 校验 → 上传媒体 → 构造作业 → 静态校验
        → 提交 → 轮询 → 收集产物 → 下载 → 组装溯源

**这里不包含任何后端特有逻辑。** 所有"怎么跟某个服务说话"的细节都在 Backend 里，
所有"这个模型要什么"的细节都在 Solution 里。Session 只负责顺序、重试、超时、
以及把散落的结论汇总成一份结果或一个带退出码的异常。

失败语义（调用方需要明确的契约）
--------------------------------
* **基础设施类失败**（配置错、后端不可达、载荷非法、提交被拒）→ **抛异常**，
  每个异常自带 ``exit_code``，CLI 直接拿它当进程退出码，MCP 拿它当工具错误。
* **作业跑完但产出不理想**（渲染报错、没有产物）→ **返回** ``GenerationResult(ok=False)``，
  因为它附带的信息（错误详情、原始返回、已下载的部分产物）比异常能承载的多得多。
* **等待超时** → 抛 :class:`~om_bridge.core.errors.JobTimeoutError`，
  但异常里带 ``job_id`` 且 ``recoverable=True``。这是有意设计的：
  超时是**最常见的非失败**，必须让调用方拿到"可以续等"的凭据。
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import logging as log
from .backend import Backend, Capability
from .config import Config, load_config
from .errors import JobTimeoutError, OmBridgeError, ValidationError
from .models import (
    Artifact,
    GenerationRequest,
    GenerationResult,
    Issue,
    JobHandle,
    JobSpec,
    JobState,
    JobStatus,
    MediaAsset,
    ProbeReport,
    Provenance,
    Severity,
    blocking_issues,
)
from .registry import Registry, get_registry
from .solution import Solution


@dataclass
class PreparedJob:
    """一次请求解析后的全部中间产物。

    单独建一个容器，是为了让 ``--dry-run`` / ``validate`` / ``describe`` 这类
    "只想看不想跑"的路径可以复用同一套解析逻辑，而不必复制一遍。
    """

    request: GenerationRequest
    solution: Solution
    backend: Backend
    mode: str | None
    params: dict[str, Any]
    media: list[MediaAsset]
    job: JobSpec
    issues: list[Issue] = field(default_factory=list)
    preset: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "solution": self.solution.key,
            "backend": self.backend.name,
            "mode": self.mode,
            "params": self.params,
            "media": [m.to_dict() for m in self.media],
            "job": {
                "kind": self.job.kind,
                "output_selector": self.job.output_selector,
                "fingerprint": self.job.fingerprint(),
                "payload_nodes": len(self.job.payload) if isinstance(self.job.payload, dict) else None,
            },
            "issues": [i.to_dict() for i in self.issues],
        }


class Session:
    """一次会话 —— 绑定一个配置与一个后端，负责执行请求。"""

    def __init__(
        self,
        config: Config | None = None,
        *,
        registry: Registry | None = None,
        backend: Backend | None = None,
        backend_name: str | None = None,
        solution_name: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        self.config = config or load_config(overrides=overrides)
        self.registry = registry or get_registry()
        self._default_solution: Solution | None = self._lookup_solution(solution_name)
        self.backend = backend or self.registry.create_backend(backend_name, self.config)
        self._probe: ProbeReport | None = None

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def _lookup_solution(self, name: str | None) -> Solution | None:
        """尝试解析默认方案。

        **失败不抛异常**：只装了后端没装方案是合法形态（把桥当纯执行器用），
        这时构造 Session 不该失败 —— 等到真正需要方案时再给出明确错误。
        """
        key = name or self.config.get("global.default_solution") or "minimax_h3"
        try:
            solution, _preset = self.registry.resolve(key)
        except OmBridgeError:
            return None
        return solution

    def solution_of(self, request: GenerationRequest) -> tuple[Solution, dict[str, Any]]:
        """确定本次请求用哪个方案，并返回其模式预设。"""
        name = request.solution or (self._default_solution.key if self._default_solution else "")
        if not name:
            # 兜底：只注册了唯一方案时自动选中，避免用户被迫记住键名
            available = self.registry.solutions()
            if len(available) == 1:
                name = next(iter(available))
            else:
                raise ValidationError(
                    "未指定方案，且已注册方案不止一个",
                    hint=f"可用方案：{', '.join(sorted(available)) or '(无)'}",
                )
        return self.registry.resolve(name)

    def _resolve(
        self, request: GenerationRequest
    ) -> tuple[Solution, dict[str, Any], str | None, dict[str, Any], list[MediaAsset], list[Issue]]:
        """解析方案与参数，并跑完**本地**校验（不联网、不上传、不构造载荷）。

        返回 ``(方案, 预设, 模式, 已解析参数, 媒体, 问题)``。

        单独抽出来有两个理由，都是被现实逼出来的：

        1. **让 :meth:`prepare` 与 :meth:`validate` 共用同一套解析。**
           两处各写一遍"填默认值 / 合并预设 / 补配置素材"曾经是缺陷的温床 ——
           任何一次阈值或默认值调整都只改一处，另一处悄悄漂移。
        2. **让"载荷构造失败"不等于"校验命令失败"。** 图建不出来（例如 i2v
           缺首图）本身就是一条**结论**，而 :meth:`validate` 的产出就是结论清单。
         """
        solution, preset = self.solution_of(request)

        # 预设（来自别名，如 minimax_h3.ref2v）优先级最低，只用来补默认值
        merged_params: dict[str, Any] = {**preset, **request.params}
        mode = solution.resolve_mode(merged_params)

        resolved = solution.schema.resolve(
            merged_params,
            mode,
            default_overrides=solution.config_defaults(self.config, mode),
        )
        resolved.setdefault("mode", mode)

        # 用配置补齐"部署机上固定的素材"，但只补空槽位 —— 调用方显式给的永远优先
        media = list(request.media)
        supplied_roles = {asset.role for asset in media}
        for asset in solution.default_media(self.config, mode):
            if asset.role not in supplied_roles:
                media.append(asset)
                supplied_roles.add(asset.role)

        issues: list[Issue] = []
        issues.extend(solution.check_compatible(self.backend))
        issues.extend(solution.validate_params(resolved, mode))
        issues.extend(solution.validate_media(media, mode))
        return solution, preset, mode, resolved, media, issues

    def blocking(self, issues: list[Issue]) -> list[Issue]:
        """决定哪些问题会阻断执行 —— 这是**判定规则**的唯一裁决点。

        默认只把 ERROR 视为阻断；``global.strict``（``OMB_STRICT``）
        开启后 WARNING 也一并阻断。

        ⚠ 这条配置此前只登记在配置表里、从未被任何代码读取 —— 一个
        "文档承诺了但从不生效"的 no-op 变量，正是本项目明确反对的东西
        （参见 ADR-0008 对硬编码可用性判断的教训：同一类"声明与行为脱节"）。
        放在会话层而不是 CLI，是为了让 ``validate`` 的报告与 ``generate``
        的实际放行**必然**使用同一条规则 —— 两处各写一份判定迟早会漂移。
        """
        if self.config.get_bool("global.strict", False):
            return [issue for issue in issues if issue.severity is not Severity.INFO]
        return blocking_issues(issues)

    def prepare(self, request: GenerationRequest) -> PreparedJob:
        """解析 + 校验 + 构造作业，但**不提交**。"""
        solution, preset, mode, resolved, media, issues = self._resolve(request)

        if request.validate:
            blocking = self.blocking(issues)
            if blocking:
                raise ValidationError(
                    f"请求校验未通过（{len(blocking)} 项）",
                    issues=issues,
                    hint="修正上述问题后重试；用 --no-validate 可跳过（不推荐）",
                )

        media = self._upload_media(media, solution, resolved)

        job = solution.build_job(
            request, self.backend,
            resolved_params=resolved,
            media=media,
            config=self.config,
        )
        if request.node_overrides:
            self._apply_overrides(job, request.node_overrides)

        prepared = PreparedJob(
            request=request,
            solution=solution,
            backend=self.backend,
            mode=mode,
            params=resolved,
            media=media,
            job=job,
            issues=issues,
            preset=preset,
        )

        if request.validate and request.preflight and job.validate_before_submit:
            pre_issues = solution.preflight(job, self.backend, probe=self._probe)
            prepared.issues.extend(pre_issues)
            blocking = self.blocking(pre_issues)
            if blocking:
                raise ValidationError(
                    f"作业静态校验未通过（{len(blocking)} 项）",
                    issues=pre_issues,
                    hint="这类问题若提交给后端也必然被拒；先修正再提交可省下一次无效排队",
                )
        return prepared

    def _upload_media(
        self,
        assets: list[MediaAsset],
        solution: Solution,
        resolved: dict[str, Any],
    ) -> list[MediaAsset]:
        """把需要上传的媒体送到后端，返回带远端名的副本。

        顺序**必须**保持：方案会按列表顺序把 ``<Picture 1>``、``<Picture 2>``
        映射到实际图片，乱序会让提示词指错对象。
        """
        media: list[MediaAsset] = []
        for asset in assets:
            if not asset.needs_upload:
                media.append(asset)
                continue
            path = self.config.resolve_path(asset.path or "")
            if not path.is_file():
                raise ValidationError(
                    f"媒体文件不存在：{path}", issues=[Issue.error(
                        f"{asset.role}: 找不到 {path}", code="media.missing_file", location=asset.role)]
                )
            local = MediaAsset(
                role=asset.role, kind=asset.kind, path=str(path), metadata=asset.metadata
            )
            uploaded = self.backend.upload(local)
            log.progress(f"[info] 已上传 {Path(asset.path or '').name} -> {uploaded.name}")
            media.append(uploaded)
        return media

    def _apply_overrides(self, job: JobSpec, overrides: dict[str, Any]) -> None:
        """把 ``节点ID.输入名=值`` 直接写进载荷（逃生舱，见 GenerationRequest）。"""
        if not isinstance(job.payload, dict):
            return
        for dotted, value in overrides.items():
            if "." not in dotted:
                raise ValidationError(
                    f"节点覆盖项的格式应为 <节点ID>.<输入名>，收到 {dotted!r}")
            node_id, _, input_name = dotted.partition(".")
            node = job.payload.get(node_id)
            if not isinstance(node, dict):
                raise ValidationError(
                    f"节点覆盖失败：载荷里没有节点 {node_id!r}",
                    hint=f"现有节点：{sorted(job.payload)[:20]}",
                )
            inputs = node.setdefault("inputs", {})
            if input_name not in inputs:
                raise ValidationError(
                    f"节点覆盖失败：节点 {node_id}（{node.get('class_type')}）没有输入 {input_name!r}",
                    hint=f"可用输入：{sorted(inputs)}",
                )
            inputs[input_name] = value

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------
    def probe(self, *, refresh: bool = False) -> ProbeReport:
        """探测后端（结果缓存，避免一次会话里反复打网络）。"""
        if self._probe is None or refresh:
            self._probe = self.backend.probe()
        return self._probe

    def readiness(self, solution: Solution | None = None, mode: str | None = None) -> dict:
        """方案在当前后端上的就绪度（含缺失清单）。"""
        target = solution or self._default_solution
        if target is None:
            return {}
        report = target.readiness(self.probe(), self.config)
        if mode and mode in report:
            return {mode: report[mode]}
        return report

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def submit(self, prepared: PreparedJob) -> JobHandle:
        """提交已解析的作业。"""
        handle = prepared.backend.submit(prepared.job)
        log.progress(f"[info] 已提交 job_id={handle.job_id} 到 {prepared.backend.name}"
                     + (f"（队列序号 {handle.queue_number}）" if handle.queue_number else ""))
        return handle

    def wait(
        self,
        handle: JobHandle,
        *,
        timeout: float | None = None,
        interval: float | None = None,
        on_state: Any = None,
    ) -> JobState:
        """轮询直到终态或超时。

        ``on_state`` 是可选回调，用于把进度透出给长任务的调用方
        （CLI 打进度行、未来的 Web 前端推 SSE）。
        """
        timeout = timeout if timeout is not None else self.config.get_float("global.timeout", 900)
        interval = interval if interval is not None else self._poll_interval()
        started = time.time()
        deadline = started + timeout
        last_note = ""
        state = JobState(handle.job_id, JobStatus.UNKNOWN)

        while time.time() < deadline:
            state = self.backend.poll(handle)
            if on_state is not None:
                on_state(state)
            if state.is_terminal:
                return state

            note = self._progress_note(state)
            if note and note != last_note:
                log.progress(f"[info] {note}（已等待 {int(time.time() - started)}s）")
                last_note = note
            time.sleep(interval)

        raise JobTimeoutError(
            f"等待作业超时（>{timeout:.0f}s），作业 {handle.job_id} 很可能仍在运行",
            job_id=handle.job_id,
            hint=f"不要重新提交（会产生重复渲染并争抢同一块 GPU）。"
                 f"用 resume --job-id {handle.job_id} 继续等待，或加大 --timeout。",
        )

    def _poll_interval(self) -> float:
        """轮询间隔：后端专属值优先于全局值。

        ``backend.<name>.poll_interval`` 此前
        同样只登记未读取。给它一个明确的语义 —— **按后端覆盖**：
        一个进程可能同时配置多个后端（本地 3080 出 124 帧约 100s、云端更快），
        合理的轮询间隔差着一个量级，全局单值表达不了这种差异。

        ⚠ 必须用 ``is_default()`` 判断"用户是否真的给了值"：登记过的键
        **永远**有值（未设置时是登记默认 5），单看 ``get_float(key, 0.0) > 0``
        会把内置默认当成显式配置 —— 这个坑在第一次实现时就踩了。
        """
        key = f"backend.{self.backend.name}.poll_interval"
        if not self.config.is_default(key):
            specific = self.config.get_float(key, 0.0)
            if specific > 0:
                return specific
        return self.config.get_float("global.poll_interval", 5)

    @staticmethod
    def _progress_note(state: JobState) -> str:
        if state.queue_running is None:
            return state.detail or ""
        return f"running={state.queue_running} pending={state.queue_pending or 0}"

    def collect(self, state: JobState, dest_dir: str | Path | None = None) -> list[Artifact]:
        """下载产物。``dest_dir`` 为空时用配置里的产物目录。"""
        if not state.artifacts:
            return []
        target = Path(dest_dir) if dest_dir else self.config.output_dir()
        target.mkdir(parents=True, exist_ok=True)
        return self.backend.fetch(state.artifacts, str(target))

    def run(self, request: GenerationRequest, *, prepared: PreparedJob | None = None) -> GenerationResult:
        """完整执行一次生成：解析 → 提交 → 等待 → 下载 → 组装结果。"""
        started = time.time()
        prepared = prepared or self.prepare(request)
        provenance = prepared.solution.provenance(
            request,
            resolved_params=prepared.params,
            media=prepared.media,
            job=prepared.job,
        )
        provenance.started_at = started

        handle = self.submit(prepared)
        provenance.job_id = handle.job_id
        state = self.wait(handle, timeout=request.timeout_s)

        artifacts: list[Artifact] = []
        download_error: str | None = None
        if state.status is JobStatus.SUCCEEDED and state.artifacts:
            try:
                artifacts = self.collect(state, request.output_dir)
            except Exception as exc:  # noqa: BLE001
                # 下载失败不该抹掉"渲染成功"这个事实，如实分开报告
                download_error = f"{type(exc).__name__}: {exc}"

        elapsed = time.time() - started
        provenance.elapsed_s = elapsed

        raw: dict[str, Any] = {"state": state.to_dict(), "job": prepared.job.metadata}
        if download_error:
            raw["download_error"] = download_error
        # 后端原始返回是排查问题的第一手材料，尽量带上（可能很大，故放在 raw 末尾）
        if state.raw:
            raw["backend"] = state.raw

        ok = state.status is JobStatus.SUCCEEDED and bool(artifacts) and not state.errors
        result = GenerationResult(
            ok=ok,
            backend=prepared.backend.name,
            solution=prepared.solution.key,
            job_id=handle.job_id,
            elapsed_s=elapsed,
            artifacts=artifacts,
            issues=prepared.issues,
            errors=list(state.errors),
            provenance=provenance,
            raw=raw,
        )
        if state.status is JobStatus.FAILED:
            log.progress(f"[warn] 后端报告渲染失败：{state.detail or '见 errors'}")
        return result

    def resume(
        self,
        job_id: str,
        *,
        timeout: float | None = None,
        output_dir: str | Path | None = None,
        backend_name: str | None = None,
    ) -> GenerationResult:
        """对超时作业续等。

        这是超时的**唯一正确恢复路径**。之所以强调：重新提交会排入一个重复作业，
        它和原作业争抢同一块 GPU，结果是两个都变慢 —— 在单卡机上尤其明显。
        """
        started = time.time()
        handle = JobHandle(job_id=job_id, backend=self.backend.name)
        state = self.wait(handle, timeout=timeout)
        artifacts = self.collect(state, output_dir) if state.status is JobStatus.SUCCEEDED else []
        return GenerationResult(
            ok=state.status is JobStatus.SUCCEEDED and bool(artifacts) and not state.errors,
            backend=self.backend.name,
            solution="(resume)",
            job_id=job_id,
            elapsed_s=time.time() - started,
            artifacts=artifacts,
            errors=list(state.errors),
            raw={"state": state.to_dict()},
        )

    # ------------------------------------------------------------------
    # 通用作业（不经方案）
    # ------------------------------------------------------------------
    def run_raw(
        self,
        payload: Any,
        *,
        output_selector: Any = None,
        output_dir: str | Path | None = None,
        timeout: float | None = None,
        validate: bool = True,
        label: str = "raw_job",
        metadata: dict[str, Any] | None = None,
    ) -> GenerationResult:
        """执行一份**调用方直接给出**的作业载荷，完全不经过方案。

        存在的意义：方案是"经过建模的常用路径"，但现实里总有两类需求落在它之外 ——
        ① 手上已经有一份别人给的工作流 JSON，只想把它跑起来；
        ② 想验证一个方案还没支持的新节点参数。

        这两件事都不该被迫先写一个 Solution。因此这里直接走
        ``后端校验 → 提交 → 轮询 → 下载`` 的同一条流水线，只是把
        "构造载荷"这一步让给了调用方。

        代价是**没有方案层的参数校验与媒体槽位语义** —— 载荷里引用的
        文件名、模型路径是否正确，只能靠后端的静态校验（``preflight``）。
        所以这里的 ``validate`` 默认仍为 True：不校验就提交，等于把错误
        推迟到排队之后才发现。

        ``provenance.payload_source`` 会被标成 ``user_supplied``，
        与"方案现构造"的产物区分开 —— 事后回溯时这是关键差异：
        前者不受代码版本影响，后者受影响。
        """
        if not self.backend.supports(Capability.RAW_JOB):
            raise ValidationError(
                f"后端 {self.backend.name!r} 未声明 {Capability.RAW_JOB} 能力，不能执行外部作业",
                hint="这类后端只接受由方案构造出来的作业",
            )

        started = time.time()
        job = JobSpec(
            payload=payload,
            output_selector=output_selector,
            kind="graph",
            validate_before_submit=validate,
            metadata={"label": label, **(metadata or {})},
        )

        issues: list[Issue] = []
        if validate:
            issues = self.backend.preflight(job, probe=self._probe)
            blocking = blocking_issues(issues)
            if blocking:
                raise ValidationError(
                    f"作业静态校验未通过（{len(blocking)} 项）", issues=issues,
                    hint="修正后重试；确实要强行提交可用 validate=False",
                )

        handle = self.submit(job)
        state = self.wait(handle, timeout=timeout)
        artifacts: list[Artifact] = []
        download_error: str | None = None
        if state.status is JobStatus.SUCCEEDED and state.artifacts:
            try:
                artifacts = self.collect(state, output_dir)
            except Exception as exc:  # noqa: BLE001
                download_error = f"{type(exc).__name__}: {exc}"

        elapsed = time.time() - started
        provenance = Provenance(
            backend=self.backend.name,
            solution="(raw)",
            job_id=handle.job_id,
            payload_fingerprint=job.fingerprint(),
            payload_source="user_supplied",
            resolved_params={"output_selector": output_selector},
            started_at=started,
            elapsed_s=elapsed,
        )
        raw: dict[str, Any] = {"state": state.to_dict()}
        if download_error:
            raw["download_error"] = download_error
        if state.raw:
            raw["backend"] = state.raw

        return GenerationResult(
            ok=state.status is JobStatus.SUCCEEDED and bool(artifacts) and not state.errors,
            backend=self.backend.name,
            solution="(raw)",
            job_id=handle.job_id,
            elapsed_s=elapsed,
            artifacts=artifacts,
            issues=issues,
            errors=list(state.errors),
            provenance=provenance,
            raw=raw,
        )

    # ------------------------------------------------------------------
    # 只读检查
    # ------------------------------------------------------------------
    def validate(self, request: GenerationRequest) -> list[Issue]:
        """只做校验与构造，不提交。返回全部问题（含非阻断项）。

        ⚠ **与 :meth:`prepare` 的关键差别：这个方法不抛异常。**

        校验命令的产出**就是**"问题清单"。如果它按 ``prepare`` 的语义在第一个
        阻断项上抛 :class:`~om_bridge.core.errors.ValidationError`，
        调用方拿到的是一小撮问题 + 一个异常 —— 而用户真正需要的是
        "一次看到全部问题，然后一起改"。

        因此这里刻意把 ``request.validate`` 降级后再走同一条 :meth:`prepare`
        路径（降级只是关掉"抛异常"这个行为，不关掉"收集问题"），
        并把载荷构造失败也当作一条**结论**并入清单。
        """
        probe = self.probe()
        solution, _preset, mode, _resolved, _media, issues = self._resolve(request)

        # 资产就绪度只在这一步检查：它需要后端清单，但不必进每次生成的热路径
        # （热路径里载荷合法性已由后端静态校验覆盖，包括模型文件名）
        issues.extend(solution.asset_issues(probe, mode, self.config))

        try:
            prepared = self.prepare(replace(request, validate=False))
        except ValidationError as exc:
            # 到这一步说明方案连载荷都构造不出来（例如 i2v 缺首图）。
            # 把它的结论并进来，而不是让整条命令以异常收场。
            issues.extend(exc.issues)
        else:
            # 载荷静态校验也一并跑：它被上面的降级关掉了（挂在 request.validate 上），
            # 但校验命令恰恰应该包含它 —— 这是最有价值的一类"零成本预检"。
            if request.preflight and prepared.job.validate_before_submit:
                issues.extend(
                    prepared.solution.preflight(prepared.job, self.backend, probe=probe)
                )

        return _dedupe_issues(issues)

    def probe_report(self, solution: Solution | None = None) -> dict:
        probe = self.probe(refresh=True)
        payload = probe.to_dict()
        target = solution or self._default_solution
        if target is not None:
            payload["readiness"] = target.readiness(probe, self.config)
        return payload

    def close(self) -> None:
        self.backend.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _dedupe_issues(issues: list[Issue]) -> list[Issue]:
    """按 (级别, 错误码, 位置, 文案) 去重，保持首次出现的顺序。

    校验路径会把三个来源的结论合并：本地参数/媒体校验 → 方案资产就绪度 →
    载荷静态校验。它们之间有**天然重叠**（例如"缺首帧图"会被媒体槽位检查与
    构建器各报一次）。

    去重不是为了省字节，而是为了让"共 N 项问题"这个数字可信 ——
    一个虚高的计数会让人误判严重程度，也会掩盖真正的新增问题。
    """
    seen: set[tuple] = set()
    result: list[Issue] = []
    for issue in issues:
        key = (issue.severity, issue.code, issue.location, issue.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result


def collect_artifacts(artifacts: Iterable[Artifact]) -> list[str]:
    """取产物本地路径的便利函数（给只想拿文件路径的调用方）。"""
    return [a.local_path for a in artifacts if a.local_path]


__all__ = ["Session", "PreparedJob", "collect_artifacts"]
