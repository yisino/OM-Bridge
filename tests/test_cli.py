"""CLI 冒烟测试（全部离线，不需要后端在线）。

用真实子进程跑，而不是直接调函数 —— 因为要验证的正是**进程边界上的行为**：
退出码、stdout 与 stderr 的分工、`--json` 输出的纯净性。
直接调函数无法覆盖这些。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# 指向一个必然拒绝连接的地址：让"需要联网"的命令在离线环境下**快速**失败，
# 从而使测试结果与开发机上是否正好跑着 ComfyUI 无关。
UNREACHABLE = "http://127.0.0.1:1"


@pytest.fixture(scope="module")
def env_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("om-bridge-test") / "test.env"
    path.write_text(
        "\n".join([
            f"OM_BRIDGE_COMFY_SERVER_URL={UNREACHABLE}",
            "OM_BRIDGE_CONNECT_TIMEOUT=1",
            "OM_BRIDGE_COMFYUI_CONNECT_TIMEOUT=1",
            "OM_BRIDGE_COMFYUI_OBJECT_INFO_TIMEOUT=1",
            "NO_PROXY=127.0.0.1,localhost",
        ]),
        encoding="utf-8",
    )
    return path


def run(args: list[str], *, env_file: Path | None = None, cwd: Path | None = None,
        timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    full = [sys.executable, "-m", "om_bridge"]
    if env_file is not None:
        full += ["--env-file", str(env_file)]
    full += args
    return subprocess.run(
        full, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(cwd or ROOT), timeout=timeout,
    )


def parse_json(result: subprocess.CompletedProcess) -> object:
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# 基本可用性
# ---------------------------------------------------------------------------
def test_version() -> None:
    result = run(["--version"])
    assert result.returncode == 0
    assert "om-bridge" in (result.stdout + result.stderr).lower()


def test_help_lists_subcommands() -> None:
    result = run(["--help"])
    assert result.returncode == 0
    for command in ("generate", "validate", "graph", "run", "resume",
                    "probe", "list", "describe", "config", "doctor"):
        assert command in result.stdout, f"--help 里缺少 {command}"


def test_list_offline() -> None:
    """`list` 是纯注册表查询 —— 不该联网，也不该需要配置。"""
    result = run(["list"], cwd=Path.cwd())
    assert result.returncode == 0, result.stderr
    assert "comfyui" in result.stdout
    assert "minimax_h3" in result.stdout


def test_list_json_is_pure_json_on_stdout() -> None:
    result = run(["list", "--json"])
    assert result.returncode == 0
    payload = parse_json(result)
    assert isinstance(payload, (dict, list))


def test_verbose_logs_do_not_pollute_stdout() -> None:
    """`--json` 下 stdout 必须**只有一份 JSON**，日志一律去 stderr。

    这是脚本能 `| jq` 的前提，也是 MCP 侧同类约束的进阶层（ADR-0007）。

    第二个断言不是陪衬：如果 ``-v`` 什么也不输出，"stdout 干净"就是一句废话
    （少写就干净），测试会变成空转。所以 ``-v`` **必须真的产出** stderr 诊断 ——
    它顺带回答"程序最终读了哪个 ``.env``"，是"改了配置没生效"的第一手线索。
    """
    result = run(["-v", "list", "--json"])
    assert result.returncode == 0
    parse_json(result)          # 多余输出会让这里抛 JSONDecodeError
    assert result.stderr.strip(), "-v 应当产生 stderr 日志"
    assert "发现" in result.stderr, "诊断应说明注册表发现了什么"
    assert "配置文件" in result.stderr, "诊断应说明最终读的是哪个配置文件"


def test_verbose_diagnostics_stay_off_stdout_when_not_json() -> None:
    """非 JSON 模式下 ``-v`` 的诊断同样只走 stderr —— 免得污染人读输出。"""
    result = run(["-v", "list"])
    assert result.returncode == 0
    assert "发现" not in result.stdout
    assert "发现" in result.stderr


# ---------------------------------------------------------------------------
# 自省
# ---------------------------------------------------------------------------
def test_describe_solution_json(env_file: Path) -> None:
    result = run(["describe", "minimax_h3", "--json"], env_file=env_file)
    assert result.returncode == 0, result.stderr
    payload = parse_json(result)
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "minimax_h3" in dumped
    # 自描述必须能回答"有哪些模式""要什么资产"
    assert "ref2v" in dumped


def test_describe_all_offline(env_file: Path) -> None:
    result = run(["describe", "--json"], env_file=env_file)
    assert result.returncode == 0, result.stderr
    parse_json(result)


def test_config_explain_reports_source(env_file: Path) -> None:
    result = run(["config", "explain", "backend.comfyui.server_url", "--json"],
                 env_file=env_file)
    assert result.returncode == 0, result.stderr
    payload = parse_json(result)
    assert UNREACHABLE in json.dumps(payload)


def test_config_report_masks_secrets(tmp_path: Path) -> None:
    secret_env = tmp_path / "secret.env"
    secret_env.write_text("OM_BRIDGE_COMFYUI_API_TOKEN=this-must-not-leak\n",
                          encoding="utf-8")
    result = run(["config", "report", "--json"], env_file=secret_env)
    assert result.returncode == 0, result.stderr
    assert "this-must-not-leak" not in result.stdout
    assert "this-must-not-leak" not in result.stderr


def test_config_report_flags_unregistered_variable(tmp_path: Path) -> None:
    """`config report` 是"我改了配置没生效"的自助答案。"""
    bogus_env = tmp_path / "bogus.env"
    bogus_env.write_text("COMFYUI_MADE_UP_SETTING=1\n", encoding="utf-8")
    result = run(["config", "report", "--json"], env_file=bogus_env)
    assert result.returncode == 0, result.stderr
    assert "COMFYUI_MADE_UP_SETTING" in result.stdout


def test_doctor_skip_probe_offline(env_file: Path) -> None:
    """离线体检也要能给出结论（并明确指出"未探测"，而不是假装一切正常）。"""
    result = run(["doctor", "--skip-probe", "--json"], env_file=env_file)
    assert result.returncode == 0, result.stderr
    payload = json.dumps(parse_json(result), ensure_ascii=False)
    assert "probe" in payload


def test_probe_unreachable_is_clean_failure(env_file: Path) -> None:
    """后端不可达时必须给出**可执行的结论**，且不能抛堆栈。"""
    result = run(["probe", "--json"], env_file=env_file)
    assert "Traceback" not in result.stderr
    combined = result.stdout + result.stderr
    assert "127.0.0.1:1" in combined or "不可达" in combined or "unreachable" in combined
    if result.stdout.strip():
        payload = parse_json(result)
        assert payload.get("reachable") is False


# ---------------------------------------------------------------------------
# graph —— 离线物化（外部框架需要的"文件 + 节点 ID"）
# ---------------------------------------------------------------------------
def test_graph_writes_file_and_reports_output_node(env_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "h3.json"
    result = run(
        ["graph", "-s", "minimax_h3", "-p", "离线物化测试", "-o", str(out), "--json"],
        env_file=env_file,
    )
    assert result.returncode == 0, result.stderr
    payload = parse_json(result)
    assert payload["output_node"] == "15"
    assert Path(payload["path"]).is_file()

    graph = json.loads(out.read_text(encoding="utf-8"))
    assert graph["15"]["class_type"] == "SaveVideo"
    assert any(n["class_type"] == "MiniMaxH3ImageToVideo" for n in graph.values())


def test_graph_does_not_need_network(env_file: Path, tmp_path: Path) -> None:
    """物化的语义是"把代码里的构造结果写出来"，与后端是否可达无关。

    如果这条依赖联网，CI 与跨机部署预生成就都不可用了。
    """
    result = run(["graph", "-s", "minimax_h3", "-p", "x",
                  "-o", str(tmp_path / "g.json"), "--json"], env_file=env_file)
    assert result.returncode == 0, result.stderr


def test_graph_ref2v_output_node_shifts_with_reference_count(
    env_file: Path, tmp_path: Path
) -> None:
    """ref2v 的 ``output_node`` 随参考图**数量**变化 —— 这正是它不能预存的原因。

    参考图会插入 1..N 个 ``LoadImage`` 节点，整个下游（H3 → 采样 → SaveVideo）
    的编号都往后挪，于是"保存节点是几号"没有固定答案。预存一份图的写法
    （旧的 ``COMFYUI_MINIMAX_H3_REF2V_OUTPUT_NODE``）在这里必然出错。

    用 ``--media-remote`` 而不是本地文件：本测试只关心**图的形状**，
    引入文件系统只会多一个与结论无关的失败面（而且要假装在后端素材目录里
    已经有这张图，远端名正是那个场景的表达方式）。
    """
    one = run(["graph", "-s", "minimax_h3.ref2v",
               "--media-remote", "reference_image=one.png",
               "-o", str(tmp_path / "one.json"), "--json"], env_file=env_file)
    two = run(["graph", "-s", "minimax_h3.ref2v",
               "--media-remote", "reference_image=one.png",
               "--media-remote", "reference_image=two.png",
               "-o", str(tmp_path / "two.json"), "--json"], env_file=env_file)
    assert one.returncode == 0, one.stderr
    assert two.returncode == 0, two.stderr

    assert parse_json(one)["output_node"] == "16"
    assert parse_json(two)["output_node"] == "17"

    # 单参考：H3 接在 1 个 LoadImage 之后，落在 7 号
    single = json.loads((tmp_path / "one.json").read_text(encoding="utf-8"))
    assert single["7"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    slots = single["7"]["inputs"]
    assert slots["ref_images.ref_image_0"] == ["6", 0]
    assert "ref_images.ref_image_1" not in slots, "只给了一张图就不该有第二个槽位"

    # 双参考：多出的 LoadImage 把 H3 推到 8 号，槽位按**命令行顺序**绑定
    double = json.loads((tmp_path / "two.json").read_text(encoding="utf-8"))
    assert double["8"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert double["8"]["inputs"]["ref_images.ref_image_0"] == ["6", 0]
    assert double["8"]["inputs"]["ref_images.ref_image_1"] == ["7", 0]
    assert double["6"]["inputs"]["image"] == "one.png"
    assert double["7"]["inputs"]["image"] == "two.png"


def test_graph_rejects_missing_local_media(env_file: Path, tmp_path: Path) -> None:
    """本地文件不存在时必须**明确报错**，不能拿一个不存在的路径去构图。

    否则错误会被推后到提交阶段，表现为 ComfyUI 一句"图片读不到"，
    而那时用户已经排过一次队了。退出码 3 = 作业非法。
    """
    result = run(["graph", "-s", "minimax_h3.ref2v",
                  "--ref-image", str(tmp_path / "does-not-exist.png"),
                  "-o", str(tmp_path / "g.json"), "--json"], env_file=env_file)
    assert result.returncode == 3, result.stdout
    payload = parse_json(result)
    assert payload["ok"] is False
    assert any(issue.get("code") == "media.missing_file"
               for issue in payload.get("issues", [])), payload


def test_graph_stdout_mode_emits_single_document(env_file: Path) -> None:
    """`-o -` 独占 stdout。与 `--json` 同用会把两份 JSON 连起来，所以不允许 ——
    这里验证单独用时输出是**一份**合法 JSON。"""
    result = run(["graph", "-s", "minimax_h3", "-p", "x", "-o", "-"], env_file=env_file)
    assert result.returncode == 0, result.stderr
    graph = parse_json(result)
    assert isinstance(graph, dict) and "15" in graph


# ---------------------------------------------------------------------------
# generate --dry-run
# ---------------------------------------------------------------------------
def test_dry_run_reports_plan_without_submitting(env_file: Path) -> None:
    result = run(["generate", "-s", "minimax_h3", "-p", "干燥运行",
                  "--dry-run", "--no-preflight", "--json"], env_file=env_file)
    assert result.returncode == 0, result.stderr
    payload = parse_json(result)
    assert payload["dry_run"] is True
    assert payload["job"]["output_selector"] == "15"
    assert payload["params"]["width"] == 864      # 内置默认值


def test_dry_run_honours_explicit_params(env_file: Path) -> None:
    result = run(["generate", "-s", "minimax_h3", "-p", "x", "--dry-run",
                  "--no-preflight", "--width", "608", "--height", "352",
                  "--length", "5", "--json"], env_file=env_file)
    assert result.returncode == 0, result.stderr
    params = parse_json(result)["params"]
    assert params["width"] == 608 and params["height"] == 352 and params["length"] == 5


def test_dry_run_shows_human_summary_on_stderr(env_file: Path) -> None:
    """不带 `--json` 时，人读摘要必须走 stderr（stdout 留给结果/管道）。"""
    result = run(["generate", "-s", "minimax_h3", "-p", "x",
                  "--dry-run", "--no-preflight"], env_file=env_file)
    assert result.returncode == 0, result.stderr
    assert "minimax_h3" in result.stderr
    assert result.stdout.strip() == ""


def test_config_default_beats_builtin_in_cli(tmp_path: Path) -> None:
    """配置文件里的分辨率应当压过代码内置默认 —— 部署事实优先于代码常量。"""
    cfg = tmp_path / "custom.env"
    cfg.write_text(
        "OM_BRIDGE_SOLUTION_MINIMAX_H3_WIDTH=608\n"
        "OM_BRIDGE_SOLUTION_MINIMAX_H3_HEIGHT=352\n",
        encoding="utf-8",
    )
    result = run(["generate", "-s", "minimax_h3", "-p", "x", "--dry-run",
                  "--no-preflight", "--json"], env_file=cfg)
    assert result.returncode == 0, result.stderr
    params = parse_json(result)["params"]
    assert params["width"] == 608 and params["height"] == 352


# ---------------------------------------------------------------------------
# 错误路径 —— 退出码要有意义，且不能抛堆栈
# ---------------------------------------------------------------------------
def test_unknown_solution_fails_without_traceback(env_file: Path) -> None:
    result = run(["generate", "-s", "does_not_exist", "-p", "x", "--dry-run"],
                 env_file=env_file)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "minimax_h3" in result.stderr, "报错应当列出可选项"


def test_unknown_mode_alias_fails_cleanly(env_file: Path) -> None:
    result = run(["generate", "-s", "minimax_h3.nope", "-p", "x", "--dry-run"],
                 env_file=env_file)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def test_ref2v_without_reference_image_is_rejected(env_file: Path) -> None:
    """ref2v 缺参考图必须在本地就拦住，不要排到队里才失败。"""
    result = run(["generate", "-s", "minimax_h3.ref2v", "-p", "x",
                  "--dry-run", "--no-preflight"], env_file=env_file)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def test_validate_never_traces_back(env_file: Path) -> None:
    """`validate` 的产出是**问题清单**，不是异常。

    它一旦抛异常，用户一次只能看到一个阻断项 —— 最坏的使用体验。
    这条约束被 CLI 与 MCP 共同依赖。
    """
    result = run(["validate", "-s", "minimax_h3.ref2v", "-p", "x", "--json"],
                 env_file=env_file)
    assert "Traceback" not in result.stderr
    assert result.stdout.strip(), "应当输出问题清单"
    payload = parse_json(result)
    assert payload["ok"] is False
    assert payload["blocking"] >= 1
    assert payload["total"] >= payload["blocking"]


def test_validate_lists_multiple_problems_at_once(env_file: Path) -> None:
    """一次给全 —— 这是"能自助修好"的前提。"""
    result = run(["validate", "-s", "minimax_h3.ref2v", "-p", "x",
                  "--width", "100", "--ref-image-size", "bogus", "--json"],
                 env_file=env_file)
    assert "Traceback" not in result.stderr
    payload = parse_json(result)
    messages = json.dumps(payload, ensure_ascii=False)
    assert payload["total"] >= 2, messages


def test_missing_env_file_argument_is_usage_error() -> None:
    result = run(["--env-file", "/nonexistent/path.env", "list"])
    # 文件不存在时不应崩，只是回落到内置默认（list 也不依赖配置）
    assert "Traceback" not in result.stderr


def test_error_payload_is_machine_readable(env_file: Path, tmp_path: Path) -> None:
    """错误要能被程序**不读文档**地消费：``issues`` 是字典列表，不是 repr 文本。

    真实缺陷回归：`ValidationError.as_dict()` 探测的是 ``issue.as_dict()``，
    而 `Issue` 定义的是 ``to_dict()``。``hasattr`` 静默为 False，模型对象原样
    进了 payload，最后靠 ``json.dump(default=str)`` 渲染成一句
    ``Issue(severity=<Severity.ERROR: 'error'>, ...)``。
    全程没有任何地方报错 —— 只有消费端按 ``code`` 分流时才失效，
    排查成本极高。这条测试是它唯一的护栏。
    """
    result = run(["graph", "-s", "minimax_h3.ref2v",
                  "--ref-image", str(tmp_path / "gone.png"),
                  "-o", str(tmp_path / "g.json"), "--json"], env_file=env_file)
    assert result.returncode == 3, result.stdout
    payload = parse_json(result)
    assert payload["ok"] is False
    assert payload["issues"], "校验失败必须带上逐条问题"
    for issue in payload["issues"]:
        assert isinstance(issue, dict), f"issues 应为字典，实际 {type(issue).__name__}"
        assert issue["severity"] in ("error", "warning", "info")
        assert issue["message"]
        assert issue.get("code"), "code 是调用方做程序化判断的依据"
    assert "Issue(" not in result.stdout, "repr 文本泄漏进了机器可读输出"


def test_json_error_output_has_consistent_keys(env_file: Path) -> None:
    """所有失败路径共用一组键 —— 消费端一个分支就够，不必按错误类型分叉。

    ``validate`` 是唯一的例外（它的产出是**问题清单**，退出码恒 0 —— 这是
    有意的设计，问题清单是结果不是命令失败），所以它单独断言报告字段。
    """
    probes = [
        (["describe", "no-such-solution", "--json"], 1),
        (["graph", "-s", "no-such-solution", "-o", "-", "--json"], 1),
    ]
    for argv, expected_code in probes:
        result = run(argv, env_file=env_file)
        assert result.returncode == expected_code, (argv, result.stdout, result.stderr)
        payload = parse_json(result)
        assert payload["ok"] is False, argv
        assert payload["error_type"].endswith("Error"), argv
        assert payload["stage"], argv
        assert payload["error"], argv

    result = run(["validate", "-s", "minimax_h3.t2v", "--width", "100", "--json"],
                 env_file=env_file)
    assert result.returncode == 0
    payload = parse_json(result)
    assert payload["ok"] is False
    assert payload["blocking"] >= 1
    assert payload["total"] >= payload["blocking"]
    for issue in payload["issues"]:
        assert isinstance(issue, dict), issue


# ---------------------------------------------------------------------------
# mock 后端：CLI 交付面上的离线端到端（第三个交付面）
# ---------------------------------------------------------------------------
def test_mock_backend_end_to_end_via_cli(tmp_path: Path) -> None:
    """CLI → Session → mock 后端 → 产物落盘，全程零网络。

    这条同时证明两件事：`--backend` 选项可用；新增第二个后端+方案
    **没有**给 CLI 带来任何针对性代码 —— `--frames` 选项来自 echo 的
    schema 自动生成（参数单一声明）。
    """
    env = tmp_path / "mock.env"
    env.write_text(
        "\n".join([
            "OM_BRIDGE_DEFAULT_BACKEND=mock",
            f"OM_BRIDGE_WORKSPACE={tmp_path / 'ws'}",
        ]),
        encoding="utf-8",
    )
    result = run(["generate", "-s", "echo", "-p", "cli offline", "--frames", "8",
                  "--backend", "mock", "--json"], env_file=env)
    assert result.returncode == 0, result.stderr
    payload = parse_json(result)
    assert payload["ok"] is True
    assert payload["backend"] == "mock"
    artifact = payload["artifacts"][0]
    assert Path(artifact["local_path"]).is_file()
    transcript = json.loads(Path(artifact["local_path"]).read_text(encoding="utf-8"))
    assert transcript["payload"]["prompt"] == "cli offline"
    assert transcript["payload"]["frames"] == 8


def test_list_shows_second_backend() -> None:
    result = run(["list"])
    assert result.returncode == 0
    assert "mock" in result.stdout
    assert "echo" in result.stdout
