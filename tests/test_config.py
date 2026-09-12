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
                "OM_BRIDGE_COMFYUI_SERVER_URL=http://192.168.3.3:8188",
                'export OM_BRIDGE_SOLUTION_MINIMAX_H3_WIDTH="608"',
                "NO_PROXY=192.168.3.3,127.0.0.1,localhost",
                "  空格两侧被裁掉  =  值  ",
            ]
        ),
        encoding="utf-8",
    )
    values = parse_env_file(path)
    assert values["OM_BRIDGE_COMFYUI_SERVER_URL"] == "http://192.168.3.3:8188"
    assert values["OM_BRIDGE_SOLUTION_MINIMAX_H3_WIDTH"] == "608"   # export + 引号
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
        file_values={"OM_BRIDGE_COMFYUI_SERVER_URL": "http://file:8188"},
        environ={"OM_BRIDGE_COMFYUI_SERVER_URL": "http://env:8188"},
        overrides={"backend.comfyui.server_url": "http://override:8188"},
    )
    assert cfg.get("backend.comfyui.server_url") == "http://override:8188"
    assert cfg.source("backend.comfyui.server_url") == "override"


def test_environment_beats_file() -> None:
    cfg = Config(
        file_values={"OM_BRIDGE_COMFYUI_SERVER_URL": "http://file:8188"},
        environ={"OM_BRIDGE_COMFYUI_SERVER_URL": "http://env:8188"},
    )
    assert cfg.get("backend.comfyui.server_url") == "http://env:8188"


def test_file_used_when_no_environment() -> None:
    cfg = Config(
        file_values={"OM_BRIDGE_COMFYUI_SERVER_URL": "http://file:8188"},
        environ={},
    )
    assert cfg.get("backend.comfyui.server_url") == "http://file:8188"


def test_builtin_default_when_nothing_set() -> None:
    cfg = Config(environ={})
    assert cfg.get("backend.comfyui.server_url") == "http://localhost:8188"
    assert cfg.is_default("backend.comfyui.server_url")


# ---------------------------------------------------------------------------
# 兼容旧变量名（ADR-0004）—— 这是"升级不破坏存量部署"的关键
# ---------------------------------------------------------------------------
def test_legacy_alias_is_honoured() -> None:
    """存量 `.env` 里写的是 `COMFYUI_SERVER_URL`，必须照常生效。"""
    cfg = Config(environ={"COMFYUI_SERVER_URL": "http://legacy:8188"})
    assert cfg.get("backend.comfyui.server_url") == "http://legacy:8188"


def test_legacy_alias_works_from_file_too() -> None:
    cfg = Config(file_values={"COMFYUI_SERVER_URL": "http://legacy-file:8188"},
                 environ={})
    assert cfg.get("backend.comfyui.server_url") == "http://legacy-file:8188"


def test_canonical_name_beats_alias_in_same_layer() -> None:
    """规范名与旧名同层时，规范名优先 —— 否则用户无法用新名纠正旧值。"""
    cfg = Config(environ={
        "OM_BRIDGE_COMFYUI_SERVER_URL": "http://canonical:8188",
        "COMFYUI_SERVER_URL": "http://legacy:8188",
    })
    assert cfg.get("backend.comfyui.server_url") == "http://canonical:8188"


def test_every_canonical_setting_has_env_name_and_description() -> None:
    """登记表的自省原则：每个设置项都必须有环境变量名与说明。

    没有 env 名的项在部署侧无法配置；没有说明的项在 `config list` 里是噪音。
    """
    for setting in (*GLOBAL_SETTINGS, *COMFYUI_SETTINGS, *H3_SETTINGS):
        assert setting.env.startswith("OM_BRIDGE_"), setting.key
        assert setting.description, setting.key


def test_canonical_env_names_are_unique() -> None:
    seen: dict[str, str] = {}
    for setting in (*GLOBAL_SETTINGS, *COMFYUI_SETTINGS, *H3_SETTINGS):
        assert setting.env not in seen, f"{setting.env} 被 {seen.get(setting.env)} 与 {setting.key} 重复占用"
        seen[setting.env] = setting.key


def test_aliases_do_not_collide_with_canonical_names() -> None:
    """别名撞上别人的规范名会造成"改 A 影响 B"的灵异现象。"""
    canonical = {s.env for s in (*GLOBAL_SETTINGS, *COMFYUI_SETTINGS, *H3_SETTINGS)}
    for setting in (*GLOBAL_SETTINGS, *COMFYUI_SETTINGS, *H3_SETTINGS):
        for alias in setting.aliases:
            assert alias not in canonical, f"{alias} 既是别名又是规范名"


def test_deprecated_base_url_alias_is_lowest_priority() -> None:
    """`COMFYUI_BASE_URL` 是废弃别名（上游从不读它），不该压过 server_url。"""
    cfg = Config(environ={
        "COMFYUI_BASE_URL": "http://deprecated:8188",
        "OM_BRIDGE_COMFYUI_SERVER_URL": "http://canonical:8188",
    })
    assert cfg.get("backend.comfyui.server_url") == "http://canonical:8188"


# ---------------------------------------------------------------------------
# 类型还原
# ---------------------------------------------------------------------------
def test_type_coercion_from_strings() -> None:
    """环境变量永远是字符串，配置层负责还原成登记表里声明的类型。

    变量名不是"."替换成"_"那么简单：``backend.comfyui.server_url`` 对应
    ``OM_BRIDGE_COMFYUI_SERVER_URL``（省略了 backend 段）。**以登记表的
    ``env`` 字段为准**，别凭键名推测 —— 写错名字不会报错，只会静默用默认值。
    """
    cfg = Config(environ={
        "OM_BRIDGE_SOLUTION_MINIMAX_H3_WIDTH": "608",
        "OM_BRIDGE_TIMEOUT": "1200.5",
        "OM_BRIDGE_VALIDATE": "false",
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
    cfg = Config(environ={"OM_BRIDGE_GLOBAL_TIMEOUT": "1200.5"}, file_values={})
    assert cfg.get("global.timeout") == 900          # 内置默认
    assert cfg.is_default("global.timeout")
    unregistered = cfg.report(include_unknown_env=True)["unregistered_env"]
    assert any(item["name"] == "OM_BRIDGE_GLOBAL_TIMEOUT" for item in unregistered)


def test_list_type_splits_on_comma() -> None:
    cfg = Config(environ={
        "OM_BRIDGE_SOLUTION_MINIMAX_H3_REF2V_IMAGES": "a.png, b.png ,c.png"
    })
    assert cfg.get("solution.minimax_h3.ref_images") == ["a.png", "b.png", "c.png"]


def test_empty_list_value_is_empty_list() -> None:
    cfg = Config(environ={"OM_BRIDGE_SOLUTION_MINIMAX_H3_REF2V_IMAGES": ""})
    assert cfg.get("solution.minimax_h3.ref_images") == []


# ---------------------------------------------------------------------------
# 作用域视图
# ---------------------------------------------------------------------------
def test_scoped_backend_view() -> None:
    cfg = Config(environ={"OM_BRIDGE_COMFYUI_VIDEO_SERVER_URL": "http://video:8188"})
    comfyui = cfg.backend("comfyui")
    assert comfyui.get("video_server_url") == "http://video:8188"
    assert comfyui.key("video_server_url") == "backend.comfyui.video_server_url"
    assert "server_url" in comfyui.registered()


def test_scoped_solution_view() -> None:
    cfg = Config(environ={"OM_BRIDGE_SOLUTION_MINIMAX_H3_STEPS": "4"})
    assert cfg.solution("minimax_h3").get_int("steps") == 4


# ---------------------------------------------------------------------------
# 自省：这是"我改了配置没生效"的答案来源
# ---------------------------------------------------------------------------
def test_explain_tells_where_value_came_from() -> None:
    """来源标签必须**带变量名**，不能只写个 ``file``。

    因为一个配置项有规范名 + 若干旧名别名。只说"来自文件"不足以回答
    "那我的 ``OM_BRIDGE_COMFYUI_SERVER_URL`` 到底有没有被读到" ——
    用户在文件里同时留着新旧两个名字时，正是最需要看这一栏的时候。
    """
    cfg = Config(file_values={"OM_BRIDGE_COMFYUI_SERVER_URL": "http://file:8188"},
                 environ={}, env_file="/tmp/x.env")
    info = cfg.explain("backend.comfyui.server_url")
    assert info["value"] == "http://file:8188"
    assert info["source"] == "file:OM_BRIDGE_COMFYUI_SERVER_URL"
    assert info["env"] == "OM_BRIDGE_COMFYUI_SERVER_URL"
    assert "COMFYUI_SERVER_URL" in info["aliases"]


def test_explain_reports_which_alias_was_honoured() -> None:
    """旧名生效时来源里要写清是**旧名**给的 —— 否则用户会以为新名也生效了。"""
    cfg = Config(environ={"COMFYUI_SERVER_URL": "http://legacy:8188"}, file_values={})
    info = cfg.explain("backend.comfyui.server_url")
    assert info["value"] == "http://legacy:8188"
    assert info["source"] == "env:COMFYUI_SERVER_URL"


def test_source_labels_are_closed_set() -> None:
    """来源标签是给脚本判断的，取值必须收敛，不能随手造新词。"""
    cfg = Config(
        file_values={"OM_BRIDGE_COMFYUI_READ_TIMEOUT": "300"},
        environ={"OM_BRIDGE_TIMEOUT": "60"},
        overrides={"backend.comfyui.server_url": "http://o:8188"},
    )
    assert cfg.source("backend.comfyui.server_url") == "override"
    assert cfg.source("global.timeout") == "env:OM_BRIDGE_TIMEOUT"
    assert cfg.source("backend.comfyui.read_timeout") == "file:OM_BRIDGE_COMFYUI_READ_TIMEOUT"
    assert cfg.source("global.poll_interval") == "default"


def test_report_masks_secrets() -> None:
    """`config report` 的输出会被贴进 issue / 分享给同事，密钥必须掩码。"""
    cfg = Config(environ={"OM_BRIDGE_COMFYUI_API_TOKEN": "super-secret-token"})
    payload = cfg.report()
    assert "secret" in payload or isinstance(payload, dict)
    dumped = repr(payload)
    assert "super-secret-token" not in dumped, "密钥泄露进了报告"


def test_report_lists_unregistered_env_variables() -> None:
    """这是最有价值的一栏：它指出"你写的这个变量程序根本不读"。

    与"不造 no-op 变量"的原则配套 —— 两者一起把"改了不生效"变成可自助排查的问题。
    """
    cfg = Config(environ={"COMFYUI_TOTALLY_MADE_UP_VAR": "x"})
    payload = cfg.report(include_unknown_env=True)
    unknown = payload.get("unknown_env") or payload.get("unregistered_env") or []
    assert any("COMFYUI_TOTALLY_MADE_UP_VAR" in str(item) for item in unknown)


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
