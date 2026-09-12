"""配置分层与自省的行为测试。

这一层的价值在于**可解释**：当有人说"我改了配置却没生效"时，
程序必须能回答"这个值从哪来"。所以测试重点不只在取值，还在 `source()` / `explain()`。
"""

from __future__ import annotations

import pytest

from om_bridge.core.config import (
    COMFYUI_SETTINGS,
    GLOBAL_SETTINGS,
    H3_SETTINGS,
    Config,
    default_env_candidates,
    parse_env_file,
)


# ---------------------------------------------------------------------------
# parse_env_file —— 自带解析器（不引 python-dotenv，见 ADR-0002）
# ---------------------------------------------------------------------------
def test_parse_env_file_basics(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "# 注释行",
                "",
                "OMB_COMFY_SERVER_URL=http://192.168.3.3:8188",
                'export OMB_MINIMAX_H3_WIDTH="608"',
                "NO_PROXY=192.168.3.3,127.0.0.1,localhost",
                "  空格两侧被裁掉  =  值  ",
            ]
        ),
        encoding="utf-8",
    )
    values = parse_env_file(path)
    assert values["OMB_COMFY_SERVER_URL"] == "http://192.168.3.3:8188"
    assert values["OMB_MINIMAX_H3_WIDTH"] == "608"   # export + 引号
    assert values["NO_PROXY"].count(",") == 2
    assert values["空格两侧被裁掉"] == "值"


def test_parse_env_file_keeps_hash_inside_value(tmp_path) -> None:
    """行尾 ``#`` 不当作注释 —— 值里合法地可以含 ``#``。

    宁可少一个便利功能，也不要静默截断用户的值（URL 片段、颜色值都会中招）。
    """
    path = tmp_path / ".env"
    path.write_text("TOKEN=abc#def\n", encoding="utf-8")
    assert parse_env_file(path)["TOKEN"] == "abc#def"


def test_parse_env_file_ignores_junk_lines(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("没有等号的一行\n=SERVER\n123BAD=x\nOK=1\n", encoding="utf-8")
    values = parse_env_file(path)
    assert values == {"OK": "1"}


def test_parse_env_file_missing_file_is_empty(tmp_path) -> None:
    """缺文件返回空字典而不是抛异常：配置探测本身就是"试几个位置"。"""
    assert parse_env_file(tmp_path / "nope.env") == {}


# ---------------------------------------------------------------------------
# 分层优先级
# ---------------------------------------------------------------------------
def test_override_beats_environment() -> None:
    cfg = Config(
        file_values={"OMB_COMFY_SERVER_URL": "http://file:8188"},
        environ={"OMB_COMFY_SERVER_URL": "http://env:8188"},
        overrides={"backend.comfyui.server_url": "http://override:8188"},
    )
    assert cfg.get("backend.comfyui.server_url") == "http://override:8188"
    assert cfg.source("backend.comfyui.server_url") == "override"


def test_environment_beats_file() -> None:
    cfg = Config(
        file_values={"OMB_COMFY_SERVER_URL": "http://file:8188"},
        environ={"OMB_COMFY_SERVER_URL": "http://env:8188"},
    )
    assert cfg.get("backend.comfyui.server_url") == "http://env:8188"


def test_file_used_when_no_environment() -> None:
    cfg = Config(
        file_values={"OMB_COMFY_SERVER_URL": "http://file:8188"},
        environ={},
    )
    assert cfg.get("backend.comfyui.server_url") == "http://file:8188"


def test_builtin_default_when_nothing_set() -> None:
    cfg = Config(environ={})
    assert cfg.get("backend.comfyui.server_url") == "http://localhost:8188"
    assert cfg.is_default("backend.comfyui.server_url")


# ---------------------------------------------------------------------------
# 命名契约（OMB_ 短前缀）—— 环境变量是公共地盘，必须有命名空间
# ---------------------------------------------------------------------------
def test_every_setting_uses_omb_prefix_and_has_description() -> None:
    """登记表的自省原则：每个设置项都必须带 OMB_ 前缀与说明。

    前缀防的是撞名：``TIMEOUT`` / ``STRICT`` / ``ENV_FILE`` 这类裸名在
    MCP 宿主注入的环境与 CI 里极易与其他工具冲突（ADR-0009）。
    """
    for setting in (*GLOBAL_SETTINGS, *COMFYUI_SETTINGS, *H3_SETTINGS):
        assert setting.env.startswith("OMB_"), setting.key
        assert setting.description, setting.key


def test_canonical_env_names_are_unique() -> None:
    seen: dict[str, str] = {}
    for setting in (*GLOBAL_SETTINGS, *COMFYUI_SETTINGS, *H3_SETTINGS):
        assert setting.env not in seen, f"{setting.env} 被 {seen.get(setting.env)} 与 {setting.key} 重复占用"
        seen[setting.env] = setting.key


def test_no_alias_layer_remains() -> None:
    """别名层已整体移除（ADR-0009 supersede ADR-0004）：登记表里不许有任何旧名残留。"""
    for setting in (*GLOBAL_SETTINGS, *COMFYUI_SETTINGS, *H3_SETTINGS):
        assert not getattr(setting, "aliases", ()), setting.key
        assert not getattr(setting, "deprecated_alias", False), setting.key


def test_legacy_comfyui_names_are_no_longer_read() -> None:
    """旧别名通道已删：COMFYUI_* 不再喂进任何配置项。

    但它们**必须**出现在 report 的 unregistered 清单里 —— 否则存量环境里
    的残留变量就成了"悄悄失效"的隐形状态，连排查线索都没有。
    """
    cfg = Config(environ={"COMFYUI_SERVER_URL": "http://legacy:8188"})
    assert cfg.get("backend.comfyui.server_url") == "http://localhost:8188"  # 默认值，不是 legacy
    assert cfg.source("backend.comfyui.server_url") == "default"
    unregistered = cfg.report(include_unknown_env=True)["unregistered_env"]
    assert any(item["name"] == "COMFYUI_SERVER_URL" for item in unregistered)


def test_old_om_bridge_prefix_is_not_read_either() -> None:
    cfg = Config(environ={"OM_BRIDGE_COMFY_SERVER_URL": "http://old-prefix:8188"})
    assert cfg.get("backend.comfyui.server_url") == "http://localhost:8188"
    unregistered = cfg.report(include_unknown_env=True)["unregistered_env"]
    assert any(item["name"] == "OM_BRIDGE_COMFY_SERVER_URL" for item in unregistered)


def test_base_url_setting_is_gone() -> None:
    """``backend.comfyui.base_url``（原 COMFYUI_BASE_URL）已随别名清理一并删除：
    OpenMontage 从不读它，登记它只会制造"设了没效果"的陷阱。"""
    keys = {s.key for s in COMFYUI_SETTINGS}
    assert "backend.comfyui.base_url" not in keys


# ---------------------------------------------------------------------------
# 枚举（choices）—— 注释里写清楚，程序里也说得清
# ---------------------------------------------------------------------------
def test_enum_settings_declare_choices() -> None:
    expected = {
        "global.default_backend": ("comfyui", "mock"),
        "global.log_level": ("DEBUG", "INFO", "WARNING", "ERROR"),
        "solution.minimax_h3.ref_image_size": ("match", "max"),
    }
    all_settings = (*GLOBAL_SETTINGS, *COMFYUI_SETTINGS, *H3_SETTINGS)
    for setting in all_settings:
        if setting.key in expected:
            assert setting.choices == expected[setting.key], setting.key


def test_valid_enum_value_passes_silently() -> None:
    cfg = Config(environ={
        "OMB_DEFAULT_BACKEND": "mock",
        "OMB_LOG_LEVEL": "WARNING",
        "OMB_MINIMAX_H3_REF2V_IMAGE_SIZE": "max",
    })
    assert cfg.warnings == []
    assert cfg.get("global.default_backend") == "mock"


def test_invalid_enum_value_warns_but_does_not_raise() -> None:
    """choices 是**开放枚举**：第三方插件可以扩展后端/方案，所以越界只告警。

    告警走 warnings（config report 会展示），绝不静默吞掉。
    """
    cfg = Config(environ={"OMB_DEFAULT_BACKEND": "my_plugin_backend"})
    assert cfg.get("global.default_backend") == "my_plugin_backend"
    assert any("my_plugin_backend" in w for w in cfg.warnings), cfg.warnings


def test_solution_enum_accepts_mode_suffix() -> None:
    """``minimax_h3.t2v`` 是合法取值：按 ``.`` 前的段匹配枚举。"""
    cfg = Config(environ={"OMB_DEFAULT_SOLUTION": "minimax_h3.t2v"})
    assert cfg.warnings == []


# ---------------------------------------------------------------------------
# 类型还原
# ---------------------------------------------------------------------------
def test_type_coercion_from_strings() -> None:
    """环境变量永远是字符串，配置层负责还原成登记表里声明的类型。

    变量名不是"."替换成"_"那么简单：``backend.comfyui.server_url`` 对应
    ``OMB_COMFY_SERVER_URL``（省略了 backend 段）。**以登记表的
    ``env`` 字段为准**，别凭键名推测 —— 写错名字不会报错，只会静默用默认值。
    """
    cfg = Config(environ={
        "OMB_MINIMAX_H3_WIDTH": "608",
        "OMB_TIMEOUT": "1200.5",
        "OMB_VALIDATE": "false",
    }, file_values={})
    assert cfg.get("solution.minimax_h3.width") == 608
    assert isinstance(cfg.get("solution.minimax_h3.width"), int)
    assert cfg.get("global.timeout") == pytest.approx(1200.5)
    assert isinstance(cfg.get("global.timeout"), float)
    assert cfg.get_bool("global.validate") is False


def test_wrong_env_name_falls_back_to_default_silently() -> None:
    """这是"我改了配置却没生效"的头号成因，把它的行为钉死。

    程序**不会**因为名字拼错而报错（否则任何前缀共存都会炸），所以必须有
    ``config report`` 的 unregistered 一栏来兜底。
    """
    cfg = Config(environ={"OMB_GLOBAL_TIMEOUT": "1200.5"}, file_values={})
    assert cfg.get("global.timeout") == 900          # 内置默认
    assert cfg.is_default("global.timeout")
    unregistered = cfg.report(include_unknown_env=True)["unregistered_env"]
    assert any(item["name"] == "OMB_GLOBAL_TIMEOUT" for item in unregistered)


def test_list_type_splits_on_comma() -> None:
    cfg = Config(environ={
        "OMB_MINIMAX_H3_REF2V_IMAGES": "a.png, b.png ,c.png"
    })
    assert cfg.get("solution.minimax_h3.ref_images") == ["a.png", "b.png", "c.png"]


def test_empty_list_value_is_empty_list() -> None:
    cfg = Config(environ={"OMB_MINIMAX_H3_REF2V_IMAGES": ""})
    assert cfg.get("solution.minimax_h3.ref_images") == []


# ---------------------------------------------------------------------------
# 作用域视图
# ---------------------------------------------------------------------------
def test_scoped_backend_view() -> None:
    cfg = Config(environ={"OMB_COMFY_VIDEO_SERVER_URL": "http://video:8188"})
    comfyui = cfg.backend("comfyui")
    assert comfyui.get("video_server_url") == "http://video:8188"
    assert comfyui.key("video_server_url") == "backend.comfyui.video_server_url"
    assert "server_url" in comfyui.registered()


def test_scoped_solution_view() -> None:
    cfg = Config(environ={"OMB_MINIMAX_H3_STEPS": "4"})
    assert cfg.solution("minimax_h3").get_int("steps") == 4


# ---------------------------------------------------------------------------
# 自省：这是"我改了配置没生效"的答案来源
# ---------------------------------------------------------------------------
def test_explain_tells_where_value_came_from() -> None:
    """来源标签必须**带变量名**，不能只写个 ``file`` ——
    否则"我的 OMB_COMFY_SERVER_URL 到底有没有被读到"没法回答。"""
    cfg = Config(file_values={"OMB_COMFY_SERVER_URL": "http://file:8188"},
                 environ={}, env_file="/tmp/x.env")
    info = cfg.explain("backend.comfyui.server_url")
    assert info["value"] == "http://file:8188"
    assert info["source"] == "file:OMB_COMFY_SERVER_URL"
    assert info["env"] == "OMB_COMFY_SERVER_URL"
    assert info["choices"] == []


def test_explain_shows_choices_for_enum_settings() -> None:
    info = Config(environ={}).explain("global.log_level")
    assert info["choices"] == ["DEBUG", "INFO", "WARNING", "ERROR"]


def test_source_labels_are_closed_set() -> None:
    """来源标签是给脚本判断的，取值必须收敛，不能随手造新词。"""
    cfg = Config(
        file_values={"OMB_COMFY_READ_TIMEOUT": "300"},
        environ={"OMB_TIMEOUT": "60"},
        overrides={"backend.comfyui.server_url": "http://o:8188"},
    )
    assert cfg.source("backend.comfyui.server_url") == "override"
    assert cfg.source("global.timeout") == "env:OMB_TIMEOUT"
    assert cfg.source("backend.comfyui.read_timeout") == "file:OMB_COMFY_READ_TIMEOUT"
    assert cfg.source("global.poll_interval") == "default"


def test_report_masks_secrets() -> None:
    """`config report` 的输出会被贴进 issue / 分享给同事，密钥必须掩码。"""
    cfg = Config(environ={"OMB_COMFY_API_TOKEN": "super-secret-token"})
    payload = cfg.report()
    assert "secret" in payload or isinstance(payload, dict)
    dumped = repr(payload)
    assert "super-secret-token" not in dumped, "密钥泄露进了报告"


def test_report_lists_unregistered_env_variables() -> None:
    """这是最有价值的一栏：它指出"你写的这个变量程序根本不读"。

    与"不造 no-op 变量"的原则配套 —— 两者一起把"改了不生效"变成可自助排查的问题。
    """
    cfg = Config(environ={"OMB_TOTALLY_MADE_UP_VAR": "x"})
    payload = cfg.report(include_unknown_env=True)
    unknown = payload.get("unknown_env") or payload.get("unregistered_env") or []
    assert any("OMB_TOTALLY_MADE_UP_VAR" in str(item) for item in unknown)


def test_removed_workflow_path_variables_are_not_registered() -> None:
    """这 4 个变量已被 ADR-0006 移除（改为现场物化计算图）。

    它们一旦被重新登记，就会重新制造"仓库图与代码脱节"的问题。
    """
    registered = {s.env for s in (*GLOBAL_SETTINGS, *COMFYUI_SETTINGS, *H3_SETTINGS)}
    forbidden = {
        "COMFYUI_MINIMAX_H3_WORKFLOW_PATH",
        "COMFYUI_MINIMAX_H3_OUTPUT_NODE",
        "COMFYUI_MINIMAX_H3_REF2V_WORKFLOW_PATH",
        "COMFYUI_MINIMAX_H3_REF2V_OUTPUT_NODE",
    }
    assert not (registered & forbidden), registered & forbidden


def test_variables_that_source_never_read_are_not_registered() -> None:
    """上游 OpenMontage 从不读这些名字 —— 登记它们等于制造 no-op 变量。"""
    registered = {s.env for s in (*GLOBAL_SETTINGS, *COMFYUI_SETTINGS, *H3_SETTINGS)}
    for name in (
        "COMFYUI_TIMEOUT_SECONDS",
        "COMFYUI_CONNECT_TIMEOUT_SECONDS",
        "COMFYUI_POLL_INTERVAL_SECONDS",
        "COMFYUI_POLL_TIMEOUT",
        "COMFYUI_VIDEO_TIMEOUT",
    ):
        assert name not in registered, f"{name} 是 no-op 变量，不该出现"


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def test_workspace_and_output_dir_defaults(tmp_path) -> None:
    cfg = Config(environ={}, overrides={"global.workspace": str(tmp_path)})
    assert cfg.workspace == tmp_path
    assert cfg.output_dir() == tmp_path / "var" / "output"


def test_relative_paths_resolve_against_workspace(tmp_path) -> None:
    cfg = Config(environ={}, overrides={"global.workspace": str(tmp_path)})
    assert cfg.resolve_path("out") == tmp_path / "out"
    absolute = tmp_path / "abs"
    assert cfg.resolve_path(absolute) == absolute


def test_env_candidates_order(tmp_path) -> None:
    """探测顺序：workspace/.env 优先于 workspace/config/om-bridge.env。

    用 ``Path`` 比较而不是拼字符串：``/ws`` 在 Windows 上会被解析成当前盘的
    根（``D:\\ws``），写死 POSIX 路径的断言只在 Linux 上成立 —— 而测试必须
    在开发机（Windows）和部署机（Linux）上给出同一个结论。
    """
    candidates = default_env_candidates(tmp_path)
    assert candidates.index(tmp_path / ".env") < candidates.index(
        tmp_path / "config" / "om-bridge.env"
    )
    assert len(candidates) == len(set(candidates)), "探测顺序里有重复项"


def test_env_candidates_are_absolute(tmp_path) -> None:
    """相对路径会让"从哪个目录启动程序"影响读到的配置 —— 必须消掉这个变量。"""
    for candidate in default_env_candidates(tmp_path):
        assert candidate.is_absolute(), candidate


def test_require_raises_for_unregistered_key() -> None:
    from om_bridge.core.errors import ConfigError

    cfg = Config(environ={})
    with pytest.raises(ConfigError):
        cfg.require("nope.not.a.key")
