"""echo 方案 —— 把请求原样转写成作业的演示 Solution。

为什么它存在
------------
`minimax_h3` 绑定 ComfyUI 后端，是无法验证"方案 ↔ 另一个后端"组合的。
echo 绑定 mock 后端，把 Session 全路径（参数解析 → 媒体上传 → 作业构造 →
提交 → 轮询 → 取件）在**零网络**条件下走通，同时给扩展作者一个
"最小合法 Solution"的样子。

刻意不做的仪式（命名过的取舍）
------------------------------
H3 方案按 manifest / builder / solution 三分，因为它的约束与图构造复杂到
值得分开测试。echo 总共不到百行，三分只会让读者在三份文件间跳来跳去 ——
**分层的价值随规模出现**，最小实现就一个文件。`docs/adding-a-backend.md`
里的三分结构是给"真实模型方案"的建议，不是硬性要求。

与 H3 的关键差异点
------------------
* 无模式（``modes=[]``）：演示"单一形态"方案的注册路径（H3 只演示了带模式的）。
* 无资产需求：mock 后端没有模型文件，就绪度恒 ready —— 正好覆盖
  "无资产声明"分支，与 H3 的逐模式资产判断互补。
"""

from __future__ import annotations

from typing import Any

from .....core.errors import ValidationError
from .....core.models import GenerationRequest, JobSpec, MediaKind
from .....core.schema import ParamSchema, ParamSpec
from .....core.solution import MediaSlot, Solution


def _require_prompt(request: GenerationRequest) -> str:
    prompt = (request.prompt or "").strip()
    if not prompt:
        raise ValidationError(
            "echo 需要非空提示词（-p/--prompt）",
            hint="echo 的产物就是提示词与参数的转写，没有提示词就没有内容可转写",
        )
    return prompt


class EchoSolution(Solution):
    """演示方案：请求 → mock 后端 → 转写 JSON。"""

    key = "echo"
    backend_name = "mock"
    kind = "video"
    """产物大类仍标 video：echo 模拟的是一条"视频生成方案"的形状。
    注意 mock 的实际产物是转写 JSON（见 mock/backend.py 的取舍说明）。"""
    modes: list[str] = []
    description = "演示方案：把提示词、参数与媒体名转写成 JSON 产物（离线，无 GPU）。"
    docs = "docs/solutions/echo.md"

    schema = ParamSchema([
        ParamSpec(
            name="frames", type="integer", default=24, minimum=1, maximum=10000,
            description="转写进产物的帧数（纯演示参数：验证 int 类型与范围校验）",
        ),
        ParamSpec(
            name="case", type="string", default="plain", choices=["plain", "upper"],
            description="提示词写法：plain 原样；upper 转大写（验证枚举校验）",
        ),
        ParamSpec(
            name="simulate_failure", type="boolean", default=False,
            description="true 时提交阶段按请求失败（用于测试结构化错误路径）",
        ),
    ])
    media_slots = [
        MediaSlot(
            role="reference", kind=MediaKind.IMAGE,
            required=False, multiple=True, max_count=3,
            description="可选参考图：上传后其远端名会写进转写产物",
        ),
    ]

    def build_job(
        self,
        request: GenerationRequest,
        backend: Any,
        *,
        resolved_params: dict[str, Any],
        media: list,
        config: Any = None,
    ) -> JobSpec:
        prompt = _require_prompt(request)
        if resolved_params["case"] == "upper":
            prompt = prompt.upper()
        payload = {
            "prompt": prompt,
            "frames": resolved_params["frames"],
            "case": resolved_params["case"],
            # simulate_failure 放进载荷而不是在方案里拦截：
            # 失败发生在 submit（后端），这才是"后端拒绝"路径的真实演练。
            "simulate_failure": bool(resolved_params["simulate_failure"]),
            "media": [m.remote_name for m in media if m.remote_name],
        }
        return JobSpec(
            payload=payload,
            output_selector="transcript",
            kind="echo-transcript",
            metadata={"solution": self.key},
        )
