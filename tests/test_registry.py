"""注册表发现与解析的行为测试。

[ADR-0003](../../docs/design/0003-registry-autodiscovery.md) 的护栏：
**新增实现不修改任何既有文件**。所以这里既验证"内置实现自动被发现"，
也验证"没有中心清单"这一事实（见 `test_no_central_manifest_exists`）。
"""

from __future__ import annotations

import pytest

from om_bridge.core.backend import Backend
from om_bridge.core.errors import OmBridgeError, UnknownBackendError, UnknownSolutionError
from om_bridge.core.registry import ENTRY_POINT_GROUP, Registry, get_registry
from om_bridge.core.solution import Solution


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = get_registry()
    reg.discover()
    return reg


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------
def test_comfyui_backend_is_discovered(registry: Registry) -> None:
    assert "comfyui" in registry.backends()


def test_minimax_h3_solution_is_discovered(registry: Registry) -> None:
    assert "minimax_h3" in registry.solutions()


def test_solution_declares_its_backend(registry: Registry) -> None:
    """方案通过 `backend_name` 声明归属 —— 这是两层的唯一静态联系。"""
    assert registry.solution("minimax_h3").backend_name == "comfyui"


def test_solutions_can_be_filtered_by_backend(registry: Registry) -> None:
    assert "minimax_h3" in registry.solutions(backend="comfyui")
    assert registry.solutions(backend="nonexistent-backend") == {}


def test_discover_is_idempotent(registry: Registry) -> None:
    before = set(registry.backends()), set(registry.solutions())
    registry.discover()
    registry.discover()
    assert (set(registry.backends()), set(registry.solutions())) == before


def test_discovery_needs_no_central_manifest() -> None:
    """`registry.py` 里不该出现任何具体实现的 import。

    一旦出现，扩展性就退化成"每加一个实现都要改这个文件"（ADR-0003）。

    检查的是 **import 语句**而不是全文提到名字：文档字符串里举例说明
    "``minimax_h3`` 是它的一个预设入口"是有价值的，不该被判违规。
    用"出现名字"当判据会把文档写得越清楚越容易挂 —— 那是坏判据。
    """
    import ast
    from pathlib import Path

    import om_bridge.core.registry as registry_module

    source = Path(registry_module.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.append(node.module)
            imported.extend(alias.name for alias in node.names)

    offenders = [
        name
        for name in imported
        if name.startswith("om_bridge.backends.") or "minimax_h3" in name
    ]
    assert not offenders, f"registry.py 不应静态 import 具体实现：{offenders}"

    # 具体实现只能通过**包名常量 + 字符串拼接**被间接引用。
    assert "BUILTIN_PACKAGE" in source, "内置实现包名应来自常量而非字面量路径"


def test_core_layer_does_not_import_concrete_backends() -> None:
    """分层规则：`core/` 不知道任何具体后端的存在。

    违反这条，双层抽象（ADR-0001）当场失效。
    """
    from pathlib import Path

    import om_bridge.core as core_package

    core_dir = Path(core_package.__file__).parent
    offenders: list[str] = []
    for path in sorted(core_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "om_bridge.backends" in text and "pkgutil" not in text and "BUILTIN_PACKAGE" not in text:
            offenders.append(path.name)
    assert not offenders, f"core 层出现了对具体后端的依赖：{offenders}"


def test_builtin_and_entry_point_groups_share_one_name() -> None:
    """内置包路径与 entry point 组名一致，便于文档与排查统一表述。"""
    from om_bridge.core.registry import BUILTIN_PACKAGE

    assert BUILTIN_PACKAGE == ENTRY_POINT_GROUP == "om_bridge.backends"


# ---------------------------------------------------------------------------
# 解析（别名）
# ---------------------------------------------------------------------------
def test_plain_key_resolves_with_empty_preset(registry: Registry) -> None:
    solution, preset = registry.resolve("minimax_h3")
    assert solution.key == "minimax_h3"
    assert preset == {}


@pytest.mark.parametrize("alias,mode", [
    ("minimax_h3.t2v", "t2v"),
    ("minimax_h3.i2v", "i2v"),
    ("minimax_h3.flf2v", "flf2v"),
    ("minimax_h3.ref2v", "ref2v"),
])
def test_mode_aliases_are_auto_generated(registry: Registry, alias: str, mode: str) -> None:
    """别名由 `key` + `modes` 自动派生，无需登记。

    这样新增一个模式时不会有人忘记"再去注册表加一行"。
    """
    solution, preset = registry.resolve(alias)
    assert solution.key == "minimax_h3"
    assert preset == {"mode": mode}


def test_alias_preset_does_not_leak_between_calls(registry: Registry) -> None:
    """`resolve()` 返回的是预设字典的**副本**，调用方改它不该污染注册表。"""
    _, preset = registry.resolve("minimax_h3.ref2v")
    preset["mode"] = "t2v"
    _, again = registry.resolve("minimax_h3.ref2v")
    assert again == {"mode": "ref2v"}


def test_unknown_solution_error_lists_available_ones(registry: Registry) -> None:
    """报错要可自助 —— 直接列出可选项，而不是只说"找不到"。"""
    with pytest.raises(UnknownSolutionError) as excinfo:
        registry.resolve("nope")
    message = str(excinfo.value) + str(getattr(excinfo.value, "hint", ""))
    assert "minimax_h3" in message


def test_unknown_backend_error_lists_available_ones(registry: Registry) -> None:
    """报错要可自助 —— 直接列出可选项，而不是只说"找不到"。

    可选项走 ``hint`` 字段（CLI 渲染成 ``建议：...``，JSON 模式下是
    ``"hint"`` 键），与 ``UnknownSolutionError`` 保持同一形状。
    """
    with pytest.raises(UnknownBackendError) as excinfo:
        registry.backend("nope")
    assert "comfyui" in str(excinfo.value) + str(excinfo.value.hint)


def test_both_unknown_errors_share_the_same_shape(registry: Registry) -> None:
    """两个"找不到"错误的字段布局必须一致 —— 否则消费端要写两套分支。"""
    for call, names in (
        (lambda: registry.backend("nope"), ("comfyui",)),
        (lambda: registry.resolve("nope"), ("minimax_h3",)),
    ):
        with pytest.raises(OmBridgeError) as excinfo:
            call()
        payload = excinfo.value.as_dict()
        assert payload["ok"] is False
        assert payload["error_type"].startswith("Unknown")
        assert payload["hint"], "hint 不能为空，否则用户不知道该填什么"
        assert all(name in payload["hint"] for name in names)


def test_solution_lookup_does_not_accept_aliases(registry: Registry) -> None:
    """`solution()` 只认键，别名必须走 `resolve()` —— 语义分开才不会误用。"""
    with pytest.raises(UnknownSolutionError):
        registry.solution("minimax_h3.ref2v")


# ---------------------------------------------------------------------------
# 实例化
# ---------------------------------------------------------------------------
def test_instance_is_shared_singleton(registry: Registry) -> None:
    """方案实例必须无状态，因此可以共享（避免每次调用重新构造）。"""
    assert registry.instance("minimax_h3") is registry.instance("minimax_h3")


def test_instances_satisfy_abstract_contract(registry: Registry) -> None:
    for key, cls in registry.solutions().items():
        assert issubclass(cls, Solution), key
        assert isinstance(registry.instance(key), Solution), key
        assert cls.key and cls.kind, key
        # modes 允许为空（单一形态方案，如 echo）；声明了模式就必须有合法默认
        if cls.modes:
            assert cls.default_mode in cls.modes, key


def test_backends_satisfy_abstract_contract(registry: Registry) -> None:
    for name, cls in registry.backends().items():
        assert issubclass(cls, Backend), name
        assert cls.name and cls.display_name and cls.description, name
        assert cls.capabilities, f"{name} 没有声明任何 Capability"


def test_describe_covers_all_registered_implementations(registry: Registry) -> None:
    payload = registry.describe()
    assert isinstance(payload, dict)
    dumped = repr(payload)
    assert "comfyui" in dumped and "minimax_h3" in dumped


def test_capability_negotiation_passes_for_h3_on_comfyui(registry: Registry) -> None:
    """方案声明的能力必须被后端满足 —— 否则会在提交后才失败。"""
    from om_bridge.core.models import blocking_issues

    backend_cls = registry.backend("comfyui")
    from om_bridge.core.config import Config

    backend = backend_cls(Config(environ={}))
    solution = registry.instance("minimax_h3")
    assert blocking_issues(solution.check_compatible(backend)) == []
