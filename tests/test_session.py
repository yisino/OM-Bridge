"""Session 判定规则测试 —— 阻断裁决与轮询间隔。

这两个方法是从**真实的 no-op 变量**里救回来的（见各自测试的说明）：
配置表里登记了、文档里描述了、但没有任何代码读它。静态扫描能发现它们
（"登记了却从未被引用"），但只有行为测试能证明修完之后语义是对的。
"""

from __future__ import annotations

from om_bridge.core.config import Config
from om_bridge.core.models import Issue
from om_bridge.core.session import Session


def make_session(**environ: str) -> Session:
    base = {
        "OMB_COMFY_SERVER_URL": "http://127.0.0.1:1",  # 不可达但**不会立刻连接**
        "OMB_CONNECT_TIMEOUT": "0.1",
    }
    base.update(environ)
    return Session(Config(environ=base))


SAMPLE = [
    Issue.error("错误"),
    Issue.warning("告警"),
    Issue.info("提示"),
]


# ---------------------------------------------------------------------------
# global.strict —— 此前是 no-op：登记了、文档承诺了"CI 场景有用"，没人读它
# ---------------------------------------------------------------------------
def test_blocking_ignores_warnings_by_default() -> None:
    assert [issue.message for issue in make_session().blocking(SAMPLE)] == ["错误"]


def test_strict_mode_counts_warnings_as_blocking() -> None:
    session = make_session(**{"OMB_STRICT": "true"})
    messages = [issue.message for issue in session.blocking(SAMPLE)]
    assert messages == ["错误", "告警"], "严格模式下 WARNING 也应阻断"


def test_strict_mode_never_blocks_info() -> None:
    """INFO 是给 `describe` / 人看的内容提示，任何模式下都不该阻断。"""
    only_info = [Issue.info("提示")]
    assert make_session(**{"OMB_STRICT": "true"}).blocking(only_info) == []


def test_strict_values_accepted_from_strings() -> None:
    """环境变量里永远是字符串，常见写法都必须生效。"""
    messages = lambda issues: [issue.message for issue in issues]  # noqa: E731
    for raw in ("true", "1", "yes", "on"):
        session = make_session(**{"OMB_STRICT": raw})
        assert messages(session.blocking(SAMPLE)) == ["错误", "告警"], raw
    for raw in ("false", "0", "", "off"):
        session = make_session(**{"OMB_STRICT": raw})
        assert messages(session.blocking(SAMPLE)) == ["错误"], raw


def test_validate_and_generate_share_the_same_rule() -> None:
    """CLI 的 validate 报告与 generate 的放行必须同源。

    结构上这条已经由"两处都调 Session.blocking"保证；这里把调用链钉住：
    换掉 Session.blocking 的实现（或绕开它）时，这条测试会提醒去看 CLI。
    """
    import inspect

    from om_bridge.interfaces import cli

    validate_src = inspect.getsource(cli._cmd_validate)
    assert "session.blocking(" in validate_src


# ---------------------------------------------------------------------------
# backend.<name>.poll_interval —— 此前也是 no-op（实际轮询只读 global 值）
# ---------------------------------------------------------------------------
def test_poll_interval_defaults_to_five() -> None:
    assert make_session()._poll_interval() == 5.0


def test_poll_interval_global_override() -> None:
    session = make_session(**{"OMB_POLL_INTERVAL": "9"})
    assert session._poll_interval() == 9.0


def test_poll_interval_backend_value_overrides_global() -> None:
    """覆盖的**理由**：同一进程可能配多个后端，快慢差一个量级。"""
    session = make_session(**{
        "OMB_POLL_INTERVAL": "9",
        "OMB_COMFY_POLL_INTERVAL": "2.5",
    })
    assert session._poll_interval() == 2.5


def test_poll_interval_registered_default_never_counts_as_configured() -> None:
    """回归：登记过的键**永远**有值（未设置时是登记默认 5）。

    第一次实现用 ``get_float(key, 0.0) > 0`` 判断"用户给没给值"，
    结果内置默认 5 恒大于 0，全局覆盖永远不生效 —— 且不报任何错。
    "用户给没给值"必须问 ``is_default()``，不能用自带默认值去试探。
    """
    session = make_session(**{"OMB_POLL_INTERVAL": "9"})
    assert session._poll_interval() == 9.0, "后端键未设置时应回落到全局值"

    # 显式把后端键置 0：视为"未配置"，同样回落全局
    session = make_session(**{
        "OMB_POLL_INTERVAL": "9",
        "OMB_COMFY_POLL_INTERVAL": "0",
    })
    assert session._poll_interval() == 9.0


def test_poll_interval_legacy_alias_is_not_read() -> None:
    """旧名通道已随别名清理移除：``COMFYUI_POLL_INTERVAL`` 不再生效，
    遗留变量只会作为 unregistered 出现在 `config report` 里（ADR-0009）。"""
    session = make_session(**{"COMFYUI_POLL_INTERVAL": "1.5"})
    assert session._poll_interval() == 5.0
