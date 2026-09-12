"""mock 后端 + echo 方案 —— 双层抽象的"第二实现"检验（全部离线、确定性）。

这里测的不是 mock 本身的功能，而是它作为**契约试金石**的三件事：

1. 契约可被第二实现完整落地（四个必选动作 + upload + 能力声明），
   且新增实现**没有改核心的任何一行** —— 注册表自动发现就是证明；
2. Session 全路径（参数解析 → 媒体上传 → 构造 → 提交 → 轮询 → 取件）
   在非 ComfyUI 后端上原样可用，三个交付面共享的编排层与后端无关；
3. 同步后端形态（submit 内完成、poll 立即终态）与"不可达要返回报告"
   等契约要求的边界行为都有可执行的样例。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from om_bridge.backends.mock import MockBackend
from om_bridge.backends.mock.solutions.echo import EchoSolution
from om_bridge.core.config import Config
from om_bridge.core.errors import JobRejectedError, ValidationError
from om_bridge.core.models import GenerationRequest, JobHandle, MediaAsset, MediaKind
from om_bridge.core.registry import get_registry
from om_bridge.core.session import Session


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\nfake-bytes")
    return tmp_path


@pytest.fixture()
def session(workspace: Path) -> Session:
    cfg = Config(environ={
        "OMB_DEFAULT_BACKEND": "mock",
        "OMB_WORKSPACE": str(workspace),
    })
    return Session(cfg)


def _echo_request(workspace: Path, **kw) -> GenerationRequest:
    defaults = dict(solution="echo", prompt="hello bridge", params={"frames": 8})
    defaults.update(kw)
    return GenerationRequest(**defaults)


# ---------------------------------------------------------------------------
# 发现：新增实现 = 新增包，核心零改动
# ---------------------------------------------------------------------------
def test_second_implementation_discovered() -> None:
    registry = get_registry()
    assert "mock" in registry.backends()
    assert issubclass(registry.backend("mock"), MockBackend)
    assert set(registry.solutions(backend="mock")) == {"echo"}
    # echo 无模式声明：验证"单一形态"方案的注册路径（H3 只覆盖了带模式的）
    assert registry.solution("echo") is EchoSolution


def test_mock_show_up_in_describe() -> None:
    payload = get_registry().describe()
    backend_names = {b["name"] for b in payload["backends"]}
    assert "mock" in backend_names
    solution_keys = {s["key"] for s in payload["solutions"]}
    assert "echo" in solution_keys


def test_probe_always_reachable_and_readiness_ready(session: Session) -> None:
    report = session.backend.probe()
    assert report.reachable is True
    # mock 无资产声明 -> 就绪度恒 ready（覆盖"无资产需求"分支，与 H3 互补）
    solution = get_registry().instance("echo")
    readiness = solution.readiness(report, session.config)
    assert readiness["default"]["ready"] is True


# ---------------------------------------------------------------------------
# Session 全路径端到端
# ---------------------------------------------------------------------------
def test_echo_end_to_end_transcript(session: Session, workspace: Path) -> None:
    request = _echo_request(
        workspace,
        media=[MediaAsset(role="reference", path=str(workspace / "ref.png"))],
    )
    result = session.run(request)

    assert result.ok is True
    assert result.backend == "mock"
    artifact = result.artifacts[0]
    assert artifact.local_path is not None and Path(artifact.local_path).is_file()

    transcript = json.loads(Path(artifact.local_path).read_text(encoding="utf-8"))
    payload = transcript["payload"]
    assert payload["prompt"] == "hello bridge"
    assert payload["frames"] == 8 and isinstance(payload["frames"], int)
    # 媒体上传确实发生了：转写里是远端名（mock 用文件复制模拟上传）
    assert payload["media"] == ["ref.png"]
    assert (workspace / "var" / "mock-uploads" / "ref.png").is_file()


def test_same_payload_yields_same_job_id(session: Session, workspace: Path) -> None:
    """同载荷 → 同 job_id（复现 ComfyUI 整图缓存的语义，供防重逻辑测试）。"""
    first = session.run(_echo_request(workspace))
    second = session.run(_echo_request(workspace))
    assert first.job_id == second.job_id
    # 两次都各自落盘产物，互不覆盖路径冲突（同目录同文件名是允许的幂等写）
    assert Path(first.artifacts[0].local_path).is_file()
    assert Path(second.artifacts[0].local_path).is_file()


def test_case_upper_rewrites_prompt(session: Session) -> None:
    result = session.run(_echo_request(Path("."), params={"frames": 8, "case": "upper"}))
    assert result.ok
    transcript = json.loads(Path(result.artifacts[0].local_path).read_text(encoding="utf-8"))
    assert transcript["payload"]["prompt"] == "HELLO BRIDGE"


def test_empty_prompt_is_rejected(session: Session) -> None:
    with pytest.raises(ValidationError):
        session.run(_echo_request(Path("."), prompt="   "))


def test_simulate_failure_raises_job_rejected(session: Session) -> None:
    request = _echo_request(Path("."), params={"frames": 8, "simulate_failure": True})
    with pytest.raises(JobRejectedError):
        session.run(request)


# ---------------------------------------------------------------------------
# 契约边界行为
# ---------------------------------------------------------------------------
def test_poll_unknown_job_fails_fast_instead_of_hanging(session: Session) -> None:
    """mock 状态不跨进程：未知 job_id 必须立刻可见地失败，而不是让 wait 挂到超时。"""
    state = session.backend.poll(JobHandle(job_id="mock-notexist", backend="mock"))
    assert state.status.value == "failed"
    assert "进程" in state.detail


def test_upload_copies_file_into_store(session: Session, workspace: Path) -> None:
    asset = MediaAsset(
        role="reference", kind=MediaKind.IMAGE, path=str(workspace / "ref.png"))
    uploaded = session.backend.upload(asset)
    assert uploaded.name == "ref.png"
    assert uploaded.path is None          # 已上传：远端名取代本地路径
    assert (workspace / "var" / "mock-uploads" / "ref.png").is_file()
