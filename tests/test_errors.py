"""异常契约测试 —— 退出码与结构化错误。

为什么单独一个文件：这两样东西是**对外承诺**，比内部实现更容易被无意破坏。

* 退出码：``comfyui_run.py`` 时代就已对外公开，shell 脚本、CI、systemd 单元
  都按数字判断。某次重构"顺手改成连续编号"不会让任何测试变红，
  只会让下游静默走错分支。
* 结构化错误：``as_dict()`` 同时喂给 CLI 的 ``--json`` 与 MCP 的返回体。
  它一旦退化成人类可读文本，程序化消费会在**不报错**的情况下失效。
"""

from __future__ import annotations

import json

import pytest

from om_bridge.core import errors as E
from om_bridge.core.models import Issue


# ---------------------------------------------------------------------------
# 退出码契约
# ---------------------------------------------------------------------------
def test_exit_codes_are_frozen() -> None:
    """这 8 个数字是文档、脚本、CI 共同依赖的契约。改动即破坏兼容性。"""
    from om_bridge.core.errors import (
        EXIT_BAD_WORKFLOW,
        EXIT_CONFIG,
        EXIT_OK,
        EXIT_REJECTED,
        EXIT_RENDER,
        EXIT_TIMEOUT,
        EXIT_UNREACHABLE,
        EXIT_USAGE,
    )

    assert (EXIT_OK, EXIT_USAGE, EXIT_UNREACHABLE, EXIT_BAD_WORKFLOW) == (0, 1, 2, 3)
    assert (EXIT_REJECTED, EXIT_TIMEOUT, EXIT_RENDER, EXIT_CONFIG) == (4, 5, 6, 7)


def test_every_error_class_declares_a_known_exit_code() -> None:
    """新加异常时不许"忘了"给退出码 —— 漏了会继承基类的 1（用法错误），
    把一个网络故障报成用法错误，脚本会做出完全错误的决策。"""
    from om_bridge.core.errors import (
        EXIT_BAD_WORKFLOW,
        EXIT_CONFIG,
        EXIT_REJECTED,
        EXIT_RENDER,
        EXIT_TIMEOUT,
        EXIT_UNREACHABLE,
        EXIT_USAGE,
    )

    known = {
        EXIT_USAGE, EXIT_UNREACHABLE, EXIT_BAD_WORKFLOW, EXIT_REJECTED,
        EXIT_TIMEOUT, EXIT_RENDER, EXIT_CONFIG,
    }
    for name in dir(E):
        obj = getattr(E, name)
        if isinstance(obj, type) and issubclass(obj, E.OmBridgeError) and obj is not E.OmBridgeError:
            assert obj.exit_code in known, f"{name}.exit_code={obj.exit_code} 不在契约内"
            assert obj.stage and obj.stage != "unknown", f"{name} 没有声明 stage"


def test_all_errors_share_one_base() -> None:
    """``except OmBridgeError`` 必须能兜住全部可预期故障，否则每个调用点都要列清单。"""
    for name in dir(E):
        obj = getattr(E, name)
        if isinstance(obj, type) and name.endswith("Error"):
            assert issubclass(obj, E.OmBridgeError), name


# ---------------------------------------------------------------------------
# 结构化错误
# ---------------------------------------------------------------------------
def test_error_payload_is_json_serializable_without_default() -> None:
    """不借助 ``json.dump(default=str)`` 也能序列化。

    这条是关键判据：一旦某处塞进一个模型对象，``default=str`` 会把它变成
    ``repr`` 文本而**不报错**，缺陷就这样溜到生产里。这里用严格的
    ``json.dumps`` 把它挡住。
    """
    samples = [
        E.ConfigError("配置缺失", hint="设置 OMB_COMFY_SERVER_URL"),
        E.ConfigMissingError("缺少必需配置项 global.workspace"),
        E.UnknownBackendError("未知后端 'x'", hint="已注册的后端：comfyui"),
        E.BackendUnavailableError("连接被拒", hint="检查后端是否启动"),
        E.JobRejectedError("载荷非法"),
        E.UploadError("上传失败"),
        E.ValidationError("校验不通过", issues=[
            Issue.error("宽高不是 32 的倍数", code="param.grid", location="width"),
            Issue.warning("步数与 LoRA 不匹配", code="param.steps"),
        ]),
        E.JobTimeoutError("等待超时", job_id="abc-123"),
        E.RenderFailedError("渲染失败", job_id="abc-123", details=["节点 12 报错"]),
    ]
    for exc in samples:
        payload = exc.as_dict()
        json.dumps(payload, ensure_ascii=False)   # 不加 default=str，故意的
        assert payload["ok"] is False
        assert payload["error"]
        assert payload["error_type"] == type(exc).__name__


def test_validation_issues_are_structured_dicts() -> None:
    """``issues`` 必须是字典列表。

    真实缺陷回归：``as_dict()`` 曾探测 ``issue.as_dict()``，而 ``Issue``
    定义的是 ``to_dict()`` —— ``hasattr`` 静默为 False，于是模型对象原样
    进了 payload，最终渲染成 ``Issue(severity=<Severity.ERROR: 'error'>, ...)``。
    消费端按 ``code`` 分类的能力就此消失，而没有任何地方报错。
    """
    exc = E.ValidationError("校验不通过", issues=[
        Issue.error("找不到参考图", code="media.missing_file", location="reference_image",
                    hint="检查路径"),
    ])
    payload = exc.as_dict()
    (issue,) = payload["issues"]
    assert isinstance(issue, dict)
    assert issue["severity"] == "error"
    assert issue["code"] == "media.missing_file"
    assert issue["location"] == "reference_image"
    assert issue["hint"] == "检查路径"
    assert "severity=Severity" not in json.dumps(payload, ensure_ascii=False)


def test_validation_error_without_issues_still_has_the_key() -> None:
    """键恒在（哪怕是空列表）：消费端不该为"有时有这条键"写分支。"""
    payload = E.ValidationError("不通过").as_dict()
    assert payload["issues"] == []


def test_hint_is_omitted_when_absent() -> None:
    """没有建议就不输出 ``hint``，避免下游渲染出一行空的"建议："。"""
    assert "hint" not in E.ConfigError("普通错误").as_dict()
    assert "hint" in E.ConfigError("错误", hint="这么做").as_dict()


def test_issue_to_dict_accepts_both_naming_conventions() -> None:
    """``to_dict`` 与 ``as_dict`` 都要认 —— 两层各用了一个名字。

    这条测试的作用是把"名字不匹配"这个坑显式记录下来：与其统一命名
    （要动两层），不如在这里写明两边的约定各是什么。
    """
    class WithToDict:
        def to_dict(self) -> dict:
            return {"from": "to_dict"}

    class WithAsDict:
        def as_dict(self) -> dict:
            return {"from": "as_dict"}

    assert E._issue_to_dict(WithToDict()) == {"from": "to_dict"}
    assert E._issue_to_dict(WithAsDict()) == {"from": "as_dict"}
    assert E._issue_to_dict({"already": "a dict"}) == {"already": "a dict"}


# ---------------------------------------------------------------------------
# 超时的特殊语义
# ---------------------------------------------------------------------------
def test_timeout_is_marked_recoverable_with_job_id() -> None:
    """超时**不等于失败**：作业通常还在跑。

    所以输出里必须带 ``recoverable`` 与 ``job_id`` —— 前者告诉上层"别当失败处理"，
    后者是 ``resume`` 的唯一凭据。缺了 job_id，用户只能重提，
    而重提会和原任务抢同一块 GPU。
    """
    payload = E.JobTimeoutError("等待 300s 无结果", job_id="job-42").as_dict()
    assert payload["recoverable"] is True
    assert payload["job_id"] == "job-42"
    assert payload["stage"] == "wait"


def test_timeout_exit_code_is_not_a_render_failure() -> None:
    """5 与 6 必须分开：脚本对"还在跑"和"跑失败了"的处理完全不同。"""
    assert E.JobTimeoutError("x").exit_code != E.RenderFailedError("x").exit_code


@pytest.mark.parametrize("name", ["BackendUnavailableError", "JobRejectedError", "UploadError"])
def test_backend_stage_marks_where_it_broke(name: str) -> None:
    """``stage`` 让人一眼看出断在哪一段，不必去读消息文本。"""
    payload = getattr(E, name)("x").as_dict()
    assert payload["stage"] in ("backend", "submit", "upload")
