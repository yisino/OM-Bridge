"""参数声明的行为测试。

这层之所以值得测，是因为它是**四处消费者**（CLI 选项、MCP inputSchema、
调用前校验、describe 输出）的唯一来源。它一旦出错，错误会以四种不同的面貌出现。
"""

from __future__ import annotations

import pytest

from om_bridge.core.schema import (
    TYPE_BOOL,
    TYPE_ENUM,
    TYPE_FLOAT,
    TYPE_INT,
    TYPE_STR,
    ParamSchema,
    ParamSpec,
    coerce,
)


def make_schema() -> ParamSchema:
    return ParamSchema(
        [
            ParamSpec("width", TYPE_INT, 864, "宽度", minimum=32, maximum=1344,
                      multiple_of=32),
            ParamSpec("length", TYPE_INT, 124, "帧数", grid=(17, 5)),
            ParamSpec("seed", TYPE_INT, 0, "随机种子"),
            ParamSpec("filename_prefix", TYPE_STR, "video/H3", "文件名前缀"),
            ParamSpec("turbo", TYPE_BOOL, True, "挂 Turbo LoRA"),
            ParamSpec("sampler", TYPE_ENUM, "res_multistep", "采样器",
                      choices=["res_multistep", "euler"]),
            # 只在 ref2v 有意义的参数 —— 用来验证"按模式过滤"
            ParamSpec("ref_image_size", TYPE_STR, "match", "参考图缩放", applies_to=["ref2v"]),
        ]
    )


# ---------------------------------------------------------------------------
# coerce
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,type_name,expected",
    [
        ("864", TYPE_INT, 864),
        ("3.5", TYPE_FLOAT, 3.5),
        ("true", TYPE_BOOL, True),
        ("false", TYPE_BOOL, False),
        ("yes", TYPE_BOOL, True),
        ("0", TYPE_BOOL, False),
        ("hello", TYPE_STR, "hello"),
        # 已经是正确类型时不折腾（CLI 传字符串，SDK 传原生值）
        (8, TYPE_INT, 8),
        (True, TYPE_BOOL, True),
    ],
)
def test_coerce(raw: object, type_name: str, expected: object) -> None:
    assert coerce(raw, type_name) == expected


def test_coerce_bool_does_not_treat_nonempty_string_as_true() -> None:
    """``bool("false") is True`` 是经典陷阱 —— 必须按字面值判断。"""
    assert coerce("false", TYPE_BOOL) is False
    assert coerce("no", TYPE_BOOL) is False
    assert coerce("off", TYPE_BOOL) is False


# ---------------------------------------------------------------------------
# resolve：默认值优先级与模式过滤
# ---------------------------------------------------------------------------
def test_config_defaults_beat_builtin_defaults() -> None:
    """**这是本项目最重要的一条优先级规则。**

    "这台机器该用多大的分辨率/哪个权重"是部署事实，不是代码常量。
    如果内置默认值赢，用户改了配置却不生效，就只能去改代码。
    """
    schema = make_schema()
    resolved = schema.resolve({}, "t2v", default_overrides={"width": 608, "length": 5})
    assert resolved["width"] == 608          # 来自配置
    assert resolved["length"] == 5           # 来自配置
    assert resolved["fps" if "fps" in resolved else "seed"] == 0   # 其余仍用内置


def test_explicit_values_beat_config_defaults() -> None:
    """用户显式传参 > 配置文件 —— 临时实验不该被部署配置压住。"""
    schema = make_schema()
    resolved = schema.resolve({"width": 480}, "t2v", default_overrides={"width": 608})
    assert resolved["width"] == 480


def test_empty_config_default_falls_back_to_builtin() -> None:
    """配置里留空（``""``）不该把默认值覆盖成空字符串。"""
    schema = make_schema()
    resolved = schema.resolve({}, "t2v", default_overrides={"filename_prefix": ""})
    assert resolved["filename_prefix"] == "video/H3"


def test_mode_irrelevant_params_are_dropped() -> None:
    """把 ``ref_image_size`` 带进 t2v 的载荷会被 ComfyUI 拒绝，所以这里必须丢掉。"""
    schema = make_schema()
    t2v = schema.resolve({"ref_image_size": "max"}, "t2v")
    assert "ref_image_size" not in t2v

    ref2v = schema.resolve({"ref_image_size": "max"}, "ref2v")
    assert ref2v["ref_image_size"] == "max"


def test_keys_not_declared_are_passed_through() -> None:
    """核心层不做白名单清洗 —— 方案可能认得自己的扩展参数。"""
    schema = make_schema()
    resolved = schema.resolve({"my_custom_knob": 7}, "t2v")
    assert resolved["my_custom_knob"] == 7


def test_resolve_coerces_string_inputs() -> None:
    """CLI 传进来的一律是字符串。"""
    schema = make_schema()
    resolved = schema.resolve({"width": "608", "turbo": "false"}, "t2v")
    assert resolved["width"] == 608 and isinstance(resolved["width"], int)
    assert resolved["turbo"] is False


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def _codes(issues: list) -> set[str]:
    return {issue.code for issue in issues}


def test_validate_flags_non_multiple_of() -> None:
    schema = make_schema()
    codes = _codes(schema.validate({"width": 100}, "t2v"))
    assert any("multiple" in c or "width" in c for c in codes), codes


def test_validate_accepts_aligned_value() -> None:
    schema = make_schema()
    assert schema.validate({"width": 608}, "t2v") == []


def test_validate_flags_out_of_range() -> None:
    schema = make_schema()
    assert schema.validate({"width": 4096}, "t2v") != []


def test_validate_enum_rejects_unknown_choice() -> None:
    schema = make_schema()
    assert schema.validate({"sampler": "nope"}, "t2v") != []


def test_validate_grid_mismatch_is_warning_not_blocking() -> None:
    """栅格不符只告警：后端一般会自动吸附到最近合法值。

    如果把它判成阻断，用户会因为"120 不是 17k+5"而无法提交 —— 而实际上
    后端能自己处理。这类"过度严格"会让人绕开校验（用 ``--no-validate``），
    反而失去保护。
    """
    from om_bridge.core.models import blocking_issues

    schema = make_schema()
    issues = schema.validate({"length": 20}, "t2v")
    assert issues, "应当有告警"
    assert not blocking_issues(issues), "栅格不符不应阻断提交"


def test_validate_reports_unknown_parameter() -> None:
    """拼错的参数名必须被指出来。

    静默丢弃是最坏的处理方式：用户会以为参数生效了，然后困惑于"为什么没变化"。
    """
    schema = make_schema()
    issues = schema.validate({"widht": 608}, "t2v")   # 故意拼错
    assert issues and any("widht" in (i.location or "") + i.message for i in issues)


# ---------------------------------------------------------------------------
# 面向消费者的三种投影必须自洽
# ---------------------------------------------------------------------------
def test_json_schema_carries_constraints() -> None:
    spec = ParamSpec("width", TYPE_INT, 864, "宽度", minimum=32, maximum=1344,
                     multiple_of=32)
    js = spec.to_json_schema()
    assert js["type"] == "integer"
    assert js["minimum"] == 32 and js["maximum"] == 1344 and js["multipleOf"] == 32


def test_json_schema_list_type_gets_items() -> None:
    spec = ParamSpec("ref_images", "list", [], "参考图")
    assert spec.to_json_schema()["items"] == {"type": "string"}


def test_cli_help_mentions_constraints() -> None:
    """帮助文本要与校验规则一致 —— 否则用户按 help 传值仍会被拒。"""
    spec = ParamSpec("width", TYPE_INT, 864, "宽度", minimum=32, maximum=1344,
                     multiple_of=32)
    help_text = spec.to_cli_help()
    assert "宽度" in help_text
    assert "32" in help_text and "1344" in help_text and "倍数" in help_text


def test_cli_help_shows_grid_rule() -> None:
    spec = ParamSpec("length", TYPE_INT, 124, "帧数", grid=(17, 5))
    assert "17" in spec.to_cli_help() and "5" in spec.to_cli_help()


def test_schema_lookup_and_defaults() -> None:
    schema = make_schema()
    assert "width" in schema
    assert schema.get("width").default == 864
    assert schema.get("nope") is None
    defaults = schema.defaults("t2v")
    assert "ref_image_size" not in defaults   # 不属于该模式
    assert defaults["width"] == 864
