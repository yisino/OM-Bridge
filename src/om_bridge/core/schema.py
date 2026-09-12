"""参数声明（ParamSchema）—— 让方案用声明而不是 if 链来描述自己的入参。

为什么要它
----------
一个方案的可调参数，需要同时被四个地方用到：

1. CLI 的 ``--width`` / ``--steps`` 等选项；
2. MCP 工具的 ``inputSchema``（JSON Schema，给 agent 看）；
3. 提交前的参数校验（越界、类型、栅格对齐）；
4. 文档／帮助文本。

原实现把这四件事各写一遍（``argparse`` 一份、MCP 的 schema 一份、
``build_h3_workflow`` 里又一份 ValueError 判断），任何新增参数都要改三处、
且必然逐渐漂移。这里用一次声明同时喂四者。

约束的表达方式
--------------
除了常规的 min/max/choices，还额外支持两类**生成模型特有的约束**：

* :attr:`ParamSpec.multiple_of` —— 分辨率必须是某数的倍数（H3 是 32）；
* :attr:`ParamSpec.grid` —— 取值必须落在 ``step * k + offset`` 的栅格上
  （H3 的帧数是 ``17k + 5``）。

这两类约束的**违约后果不同**：越界会被后端拒绝（阻断），而栅格不对齐
后端会**悄悄吸附**到最近合法值（只是告警）。把它们区分开，是为了不让
"后端会自动修正"的情况阻断用户的正常提交。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import Issue

# 支持的参数类型标记。刻意保持很小的一组，且以 Python 类型名为准，
# 便于与 JSON Schema 双向对照（见 ParamSpec.to_json_schema）。
TYPE_INT = "integer"
TYPE_FLOAT = "number"
TYPE_STR = "string"
TYPE_BOOL = "boolean"
TYPE_PATH = "path"
TYPE_PATH_LIST = "path_list"
TYPE_ENUM = "enum"
TYPE_LIST = "list"


def coerce(raw: Any, type_name: str) -> Any:
    """把外部输入（CLI 字符串 / JSON 值）转成声明所期望的类型。

    CLI 传进来的永远是字符串，而工作流里 ``width`` 必须是整数、
    ``denoise`` 必须是浮点 —— 类型错了后端会直接拒绝。
    因此统一在这里做一次"按声明类型还原"，而不是让每个方案自己 int()。

    转换失败时**原样返回**而不是抛异常：让校验层去报一条带位置信息的错误，
    比在这里抛一个没有上下文的 ValueError 有用得多。
    """
    if raw is None:
        return None

    if type_name == TYPE_INT:
        if isinstance(raw, bool):
            return int(raw)
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return raw

    if type_name == TYPE_FLOAT:
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return raw

    if type_name == TYPE_BOOL:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on", "y"):
            return True
        if text in ("0", "false", "no", "off", "n"):
            return False
        return raw

    if type_name == TYPE_LIST or type_name == TYPE_PATH_LIST:
        if isinstance(raw, (list, tuple)):
            return list(raw)
        if isinstance(raw, str):
            # 逗号分隔是命令行与 .env 里最自然的列表写法
            return [item.strip() for item in raw.split(",") if item.strip()]
        return raw

    # string / path / enum 都按字符串处理
    return raw if isinstance(raw, str) else str(raw)


@dataclass
class ParamSpec:
    """单个参数的声明。"""

    name: str
    type: str = TYPE_STR
    default: Any = None
    description: str = ""
    required: bool = False

    minimum: float | None = None
    maximum: float | None = None
    choices: list[Any] | None = None
    multiple_of: int | None = None
    """取值必须为该数的倍数（不满足=阻断）。典型用途：分辨率对齐。"""
    grid: tuple[int, int] | None = None
    """``(step, offset)``，取值须落在 ``step*k + offset`` 上。
    不满足时**只告警**，因为后端通常会自动吸附到最近合法值。"""
    applies_to: list[str] = field(default_factory=list)
    """限定该参数只在某些模式下有意义（对应 Solution 的 ``modes``）。空=全部模式。"""

    def applies_to_mode(self, mode: str | None) -> bool:
        if not self.applies_to:
            return True
        if mode is None:
            return True
        return mode in self.applies_to

    # -- 面向四种消费者 -----------------------------------------------------
    def to_json_schema(self) -> dict:
        """转成 JSON Schema 片段，直接喂给 MCP 工具的 ``inputSchema``。"""
        type_map = {
            TYPE_INT: "integer",
            TYPE_FLOAT: "number",
            TYPE_STR: "string",
            TYPE_PATH: "string",
            TYPE_BOOL: "boolean",
            TYPE_ENUM: "string",
            TYPE_LIST: "array",
            TYPE_PATH_LIST: "array",
        }
        schema: dict[str, Any] = {"type": type_map.get(self.type, "string")}
        if self.description:
            schema["description"] = self.description
        if self.default is not None:
            schema["default"] = self.default
        if self.choices:
            schema["enum"] = list(self.choices)
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.multiple_of:
            schema["multipleOf"] = self.multiple_of
        if self.type in (TYPE_LIST, TYPE_PATH_LIST):
            schema["items"] = {"type": "string"}
        return schema

    def to_cli_help(self) -> str:
        """给 ``argparse`` 的 help 文本，把约束一并说清。"""
        suffix: list[str] = []
        if self.choices:
            suffix.append("/".join(str(c) for c in self.choices))
        if self.minimum is not None or self.maximum is not None:
            suffix.append(f"[{self.minimum if self.minimum is not None else '-inf'}.."
                          f"{self.maximum if self.maximum is not None else 'inf'}]")
        if self.multiple_of:
            suffix.append(f"{self.multiple_of} 的倍数")
        if self.grid:
            suffix.append(f"栅格 {self.grid[0]}k+{self.grid[1]}")
        tail = f"  ({', '.join(suffix)})" if suffix else ""
        return f"{self.description}{tail}".strip()

    def validate(self, value: Any, *, location: str = "") -> list[Issue]:
        """校验单个取值，返回问题列表（可能为空）。"""
        issues: list[Issue] = []
        loc = location or self.name

        if value is None:
            if self.required:
                issues.append(Issue.error(f"缺少必填参数 {self.name}", code="param.required",
                                          location=loc))
            return issues

        expected_python = {
            TYPE_INT: int,
            TYPE_FLOAT: (int, float),
            TYPE_BOOL: bool,
            TYPE_STR: str,
            TYPE_PATH: str,
            TYPE_ENUM: str,
            TYPE_LIST: list,
            TYPE_PATH_LIST: list,
        }.get(self.type, object)

        if self.type in (TYPE_INT, TYPE_FLOAT) and isinstance(value, bool):
            issues.append(Issue.error(
                f"{self.name} 期望 {self.type}，收到布尔值", code="param.type", location=loc))
            return issues

        if not isinstance(value, expected_python):
            issues.append(Issue.error(
                f"{self.name} 期望 {self.type}，收到 {type(value).__name__}（值：{value!r}）",
                code="param.type", location=loc))
            return issues

        if self.choices is not None and value not in self.choices:
            issues.append(Issue.error(
                f"{self.name} = {value!r} 不是合法取值，可选：{self.choices}",
                code="param.choice", location=loc))
            return issues

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                issues.append(Issue.error(f"{self.name} = {value} 小于下限 {self.minimum}",
                                          code="param.range", location=loc))
            if self.maximum is not None and value > self.maximum:
                issues.append(Issue.error(f"{self.name} = {value} 超过上限 {self.maximum}",
                                          code="param.range", location=loc))
            if self.multiple_of and value % self.multiple_of != 0:
                issues.append(Issue.error(
                    f"{self.name} = {value} 不是 {self.multiple_of} 的倍数",
                    code="param.multiple_of", location=loc))
            if self.grid:
                step, offset = self.grid
                if step and (value - offset) % step != 0:
                    # 只告警：后端会吸附到最近合法值，提交本身是安全的
                    nearest = offset + round((value - offset) / step) * step
                    issues.append(Issue.warning(
                        f"{self.name} = {value} 不落在 {step}k+{offset} 栅格上，"
                        f"后端会吸附为 {nearest}",
                        code="param.grid", location=loc))
        return issues


@dataclass
class ParamSchema:
    """一组参数声明，外加"整组"级别的解析与校验。"""

    specs: list[ParamSpec] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_name = {spec.name: spec for spec in self.specs}
        if len(self._by_name) != len(self.specs):
            # 重复声明是配置错误，早失败好过让"后者覆盖前者"的隐性行为坑人
            names = [spec.name for spec in self.specs]
            dupes = {n for n in names if names.count(n) > 1}
            raise ValueError(f"ParamSchema 中存在重复参数名: {sorted(dupes)}")

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def __iter__(self) -> Iterable[ParamSpec]:
        return iter(self.specs)

    def get(self, name: str) -> ParamSpec | None:
        return self._by_name.get(name)

    def defaults(self, mode: str | None = None) -> dict[str, Any]:
        """取出适用于某模式的默认值。"""
        return {
            spec.name: spec.default
            for spec in self.specs
            if spec.default is not None and spec.applies_to_mode(mode)
        }

    def resolve(
        self,
        values: dict[str, Any] | None,
        mode: str | None = None,
        *,
        default_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把用户传入的**原始**参数解析成完整可用的参数集。

        四件事：
        1. 按声明类型做类型还原（CLI 字符串 -> int/float/bool/list）；
        2. 填上默认值 —— 优先用 ``default_overrides``（来自配置文件/部署档），
           没有才用声明里的内置默认；
        3. 丢弃不属于当前模式的参数（避免把 ``ref_image_size`` 塞进 t2v 的载荷里）；
        4. 保留声明里没有的键，让方案自定义扩展参数能透传。

        第 2 点的顺序很重要：**用户显式传参 > 配置文件 > 内置默认**。
        配置文件代表"这台机器上应该怎么跑"，内置默认只代表"代码作者认为的通用值"。
        """
        supplied = dict(values or {})
        overrides = default_overrides or {}
        resolved: dict[str, Any] = {}

        for spec in self.specs:
            if not spec.applies_to_mode(mode):
                # ★ 这里必须**显式丢弃**。
                # 只 `continue` 是不够的：末尾的 ``resolved.update(supplied)``
                # 会把没被 pop 掉的键原样搬回来，于是"丢弃模式无关参数"这条承诺
                # 静默失效 —— 例如给 t2v 传的 ``ref_image_size`` 会一路进到载荷里，
                # 被 ComfyUI 以 400 拒绝（报错还看不出是哪个参数导致的）。
                # 注意区分：这里丢的是"**声明过**但不适用于本模式"的键；
                # 完全未声明的键仍然透传（那是方案扩展参数的通道）。
                supplied.pop(spec.name, None)
                continue
            if spec.name in supplied:
                resolved[spec.name] = coerce(supplied.pop(spec.name), spec.type)
                continue
            if spec.name in overrides and overrides[spec.name] not in (None, ""):
                resolved[spec.name] = coerce(overrides[spec.name], spec.type)
                continue
            if spec.default is not None:
                resolved[spec.name] = spec.default

        # 声明里没有的键原样保留：它们可能是方案实现自己认识的扩展项，
        # 或者是 node_overrides 之类的旁路输入。核心层不做"白名单清洗"。
        resolved.update(supplied)
        return resolved

    def validate(self, values: dict[str, Any], mode: str | None = None) -> list[Issue]:
        """整组校验：先按模式过滤，再逐个校验，最后报告"不认识但传了"的参数。"""
        issues: list[Issue] = []
        for spec in self.specs:
            if not spec.applies_to_mode(mode):
                if spec.name in values:
                    issues.append(Issue.warning(
                        f"参数 {spec.name} 在模式 {mode} 下无效，已忽略",
                        code="param.not_applicable", location=spec.name))
                continue
            issues.extend(spec.validate(values.get(spec.name), location=spec.name))

        unknown = [k for k in values if k not in self._by_name]
        for name in unknown:
            issues.append(Issue.warning(
                f"参数 {name} 未在方案声明中登记，将原样透传",
                code="param.undeclared", location=name))
        return issues
