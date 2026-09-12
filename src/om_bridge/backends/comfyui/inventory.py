"""后端资产盘点 —— 把 ``/object_info`` 归纳成"有什么节点、有什么文件"。

``/object_info`` 是 ComfyUI 的一份庞大 JSON（节点多的实例有 1200+ 个类，
响应几 MB）。直接在里面翻找既难读也易错，所以这里一次性归纳成一个
:class:`Inventory`，让校验器和探测报告都基于同一份归纳结果。

同时它也解决了"资产名从哪来"这个问题：模型文件名**不是**文件系统目录，
而是各加载节点下拉框里的枚举值。磁盘上有文件但没被扫描到，
在 ComfyUI 看来就等于没有 —— 所以判断"权重在不在"必须查这里，不能查磁盘。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 加载节点 → (资产类别, 该节点上承载文件名的输入名)。
# 新增一类资产（例如 upscaler 模型）时只需在这里加一行。
ASSET_LOADERS: dict[str, tuple[str, str]] = {
    "UNETLoader": ("unet", "unet_name"),
    "CheckpointLoaderSimple": ("checkpoint", "ckpt_name"),
    "CLIPLoader": ("clip", "clip_name"),
    "DualCLIPLoader": ("clip", "clip_name1"),
    "VAELoader": ("vae", "vae_name"),
    "LoraLoader": ("lora", "lora_name"),
    "LoraLoaderModelOnly": ("lora", "lora_name"),
    "UpscaleModelLoader": ("upscaler", "model_name"),
    "StyleModelLoader": ("style_model", "style_model_name"),
    "ControlNetLoader": ("controlnet", "control_net_name"),
    "DiffusionModelLoader": ("diffusion_model", "model_name"),
}


@dataclass
class Inventory:
    """一个 ComfyUI 实例的能力快照。"""

    node_classes: set[str] = field(default_factory=set)
    assets: dict[str, list[str]] = field(default_factory=dict)
    object_info: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_object_info(cls, object_info: dict[str, Any]) -> Inventory:
        inventory = cls(
            node_classes=set(object_info.keys()),
            object_info=object_info,
        )
        for class_type, (category, input_name) in ASSET_LOADERS.items():
            if class_type not in object_info:
                continue
            values = cls.combo_values(object_info, class_type, input_name)
            if values:
                # 同一类别的多个加载节点取并集（例如 LoraLoader 与 LoraLoaderModelOnly
                # 读的是同一个目录，两边都要算进去）
                merged = set(inventory.assets.get(category, []))
                merged.update(values)
                inventory.assets[category] = sorted(merged)
        return inventory

    # ------------------------------------------------------------------
    @staticmethod
    def combo_values(object_info: dict, class_type: str, input_name: str) -> list:
        """取出某个节点某个输入的可选值列表。

        ComfyUI 把自己的 schema 塞在一个位置不固定的结构里，这里有三种形态需要兼容：

        * ``[["a", "b"], {...}]`` —— 枚举直接就是第一个元素（最常见）；
        * ``["COMBO", {"options": [...]}]`` —— 新版本的 COMBO 形态；
        * ``["COMFY_DYNAMICCOMBO_V3", {"options": [{"key": "..."}, ...]}]`` —— 动态组合，
          选项是字典列表，真正的取值在 ``key`` 里。
        """
        spec = ((object_info.get(class_type, {}).get("input") or {}).get("required") or {}).get(
            input_name
        )
        if not spec:
            spec = ((object_info.get(class_type, {}).get("input") or {}).get("optional") or {}).get(
                input_name
            )
        if not spec:
            return []
        head = spec[0] if isinstance(spec, list) and spec else spec
        if isinstance(head, list):
            return list(head)
        options = spec[1] if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict) else {}
        if isinstance(options.get("options"), list):
            raw = options["options"]
            if raw and isinstance(raw[0], dict) and "key" in raw[0]:
                return [entry["key"] for entry in raw]
            return list(raw)
        return []

    @staticmethod
    def input_spec(object_info: dict, class_type: str, input_name: str) -> tuple[Any, dict] | None:
        """取某个输入的 ``(类型标记, 选项字典)``，required 优先于 optional。"""
        node_def = object_info.get(class_type) or {}
        for kind in ("required", "optional"):
            spec = (node_def.get("input") or {}).get(kind) or {}
            if input_name in spec:
                entry = spec[input_name]
                type_token = entry[0] if isinstance(entry, list) and entry else entry
                options = (
                    entry[1]
                    if isinstance(entry, list) and len(entry) > 1 and isinstance(entry[1], dict)
                    else {}
                )
                return type_token, options
        return None

    # ------------------------------------------------------------------
    def has_node(self, class_type: str) -> bool:
        return class_type in self.node_classes

    def has_asset(self, category: str, name: str) -> bool:
        if not name:
            return False
        return name in self.assets.get(category, [])

    def matching_assets(self, category: str, needle: str) -> list[str]:
        """按子串筛资产（例如列出所有含 ``minimax_h3`` 的扩散模型）。"""
        return [name for name in self.assets.get(category, []) if needle.lower() in name.lower()]

    def summary(self) -> dict:
        return {
            "node_classes": len(self.node_classes),
            "assets": {category: len(names) for category, names in sorted(self.assets.items())},
        }
