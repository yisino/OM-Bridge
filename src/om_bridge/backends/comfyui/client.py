"""ComfyUI 传输层 —— 只做"跟这台 ComfyUI 说话"，不含任何模型知识。

与旧实现的区别
--------------
原来这份 HTTP 管道散在 ``comfyui_run.py`` 的模块级函数里（``_req`` / ``http_json`` /
``http_bytes`` / ``upload_image`` / ``wait_for``），靠读全局环境变量取配置。
那导致三个问题：无法同时连两台 ComfyUI、无法在测试里替换传输、超时值改不了。

这里把它收进一个类，配置由构造参数注入，超时全部可调，错误带类型。

关于代理
--------
本模块用 ``urllib``，它会自动读取 ``no_proxy`` / ``NO_PROXY`` 环境变量。
在"主机装了全局代理、而 GPU 机器在局域网"的网络里，**必须**把 GPU 主机加进
``no_proxy``，否则请求会被代理吃掉并返回 502 —— 现象看起来像"ComfyUI 挂了"，
实际是流量根本没到。这类假故障排查成本很高，所以在此显式记录。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from ...core.errors import BackendUnavailableError, JobRejectedError, UploadError
from ...core.logging import get_logger

log = get_logger("comfyui.client")

# ComfyUI 的 media type 只有 image 这一条上传通道。
# 视频/音频素材必须先落到它的 input 目录，再用文件名引用。
UPLOAD_ENDPOINT = "/upload/image"


class ComfyUIClient:
    """一个 ComfyUI 实例的裸客户端。

    刻意**不做**作业状态管理 —— 那是 :class:`ComfyUIBackend` 的职责。
    这里只保证"每个端点都有对应的、带正确错误类型的函数"。
    """

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float = 15.0,
        read_timeout: float = 900.0,
        upload_timeout: float = 300.0,
        object_info_timeout: float = 180.0,
        detail_timeout: float = 30.0,
        token: str = "",
        user: str = "",
        password: str = "",
    ) -> None:
        self.base_url = (base_url or "http://localhost:8188").rstrip("/")
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.upload_timeout = float(upload_timeout)
        self.object_info_timeout = float(object_info_timeout)
        self.detail_timeout = float(detail_timeout)
        self.token = token or ""
        self.user = user or ""
        self.password = password or ""
        self._cache: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------------
    # 低层请求
    # ------------------------------------------------------------------
    def auth_headers(self) -> dict[str, str]:
        """按配置生成认证头。

        注意：**OpenMontage 的原生客户端不支持任何认证**，因此给 ComfyUI
        前面挂鉴权代理会让 OpenMontage 直接 401，而 CLI/MCP 仍然可用。
        要限制访问，优先在网络层做（防火墙 / 隧道），而不是应用层鉴权。
        """
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        if self.user and self.password:
            import base64

            raw = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            return {"Authorization": f"Basic {raw}"}
        return {}

    def _request(
        self,
        url: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ):
        merged = dict(self.auth_headers())
        merged.update(headers or {})
        request = urllib.request.Request(url, data=data, headers=merged)
        return urllib.request.urlopen(request, timeout=timeout)

    def json_get(self, path: str, timeout: float | None = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            with self._request(url, timeout=timeout or self.detail_timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, url) from None
        except urllib.error.URLError as exc:
            raise BackendUnavailableError(
                f"无法连接 ComfyUI（{self.base_url}）：{exc.reason}",
                hint="① 服务是否在运行 ② 地址/端口是否正确 ③ 主机代理是否劫持了局域网流量"
                     "（检查 NO_PROXY）",
            ) from None

    def json_post(self, path: str, payload: dict, timeout: float = 60.0) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        try:
            with self._request(url, body, {"Content-Type": "application/json"}, timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, url) from None
        except urllib.error.URLError as exc:
            raise BackendUnavailableError(
                f"无法连接 ComfyUI（{self.base_url}）：{exc.reason}") from None

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError, url: str) -> Exception:
        """把 HTTP 错误体读出来 —— ComfyUI 的校验详情都在 body 里，不读就丢了。"""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            detail = ""
        return JobRejectedError(
            f"{url} 返回 HTTP {exc.code}：{detail[:2000]}",
            hint="这类错误通常是载荷与后端节点版本不匹配，先跑一次静态校验。",
        )

    # ------------------------------------------------------------------
    # 端点
    # ------------------------------------------------------------------
    def system_stats(self) -> dict:
        """``GET /system_stats`` —— 连通性与显存信息。"""
        return self.json_get("/system_stats", timeout=self.connect_timeout)

    def object_info(self, *, cache_seconds: float = 0.0) -> dict:
        """``GET /object_info`` —— 全部节点定义。

        这是静态校验与资产盘点的**事实来源**：节点是否存在、输入名、
        枚举取值、数值范围全在里面。

        ``cache_seconds > 0`` 时按秒缓存，因为节点多的实例拉一次可能要好几秒，
        而一次会话里往往会问好几次。
        """
        key = "object_info"
        now = time.time()
        if cache_seconds > 0:
            cached = self._cache.get(key)
            if cached and now - cached[0] < cache_seconds:
                return cached[1]
        data = self.json_get("/object_info", timeout=self.object_info_timeout)
        if cache_seconds > 0:
            self._cache[key] = (now, data)
        return data

    def queue(self) -> dict:
        """``GET /queue`` —— 队列长度，用于给用户一个"还要等多久"的直觉。"""
        return self.json_get("/queue")

    def history(self, prompt_id: str) -> dict:
        """``GET /history/<id>`` —— 单个作业的执行记录。"""
        return self.json_get(f"/history/{urllib.parse.quote(prompt_id)}")

    def submit_prompt(self, graph: dict, *, client_id: str | None = None) -> dict:
        """``POST /prompt`` —— 提交一张 API 格式的图。"""
        return self.json_post(
            "/prompt",
            {"prompt": graph, "client_id": client_id or uuid.uuid4().hex},
            timeout=60.0,
        )

    def view_bytes(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        """``GET /view`` —— 取回产物原始字节。"""
        query = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": type_}
        )
        url = f"{self.base_url}/view?{query}"
        try:
            with self._request(url, timeout=self.upload_timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, url) from None
        except urllib.error.URLError as exc:
            raise BackendUnavailableError(f"下载产物失败：{exc.reason}") from None

    # ------------------------------------------------------------------
    # 上传
    # ------------------------------------------------------------------
    def _post_multipart(self, path: str, file_path: str, name: str, subfolder: str = "") -> str:
        boundary = "----ombridge" + uuid.uuid4().hex
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with open(file_path, "rb") as handle:
            payload = handle.read()

        parts: list[bytes] = []
        if subfolder:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\n'
                f"{subfolder}\r\n".encode()
            )
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
                f'filename="{name}"\r\nContent-Type: {content_type}\r\n\r\n'
            ).encode()
            + payload
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())

        with self._request(
            f"{self.base_url}{path}",
            b"".join(parts),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=self.upload_timeout,
        ) as response:
            parsed = json.load(response)
        sub = parsed.get("subfolder") or ""
        return f"{sub}/{parsed['name']}" if sub else parsed["name"]

    def upload_image(self, file_path: str, subfolder: str = "") -> str:
        """上传一张图片，返回可以写进 ``LoadImage.image`` 的名字。

        重名处理值得说明：ComfyUI 对同名上传会返回 **HTTP 409**。
        直接复用服务端已有文件是不安全的（内容可能完全不同），
        因此这里改用**内容哈希**命名重传，保证"图里读到的字节就是我发出去的字节"。
        代价是可能留下几份同内容的副本，权衡下来正确性更重要。
        """
        base = os.path.basename(file_path)
        try:
            return self._post_multipart(UPLOAD_ENDPOINT, file_path, base, subfolder)
        except JobRejectedError as exc:
            if "409" not in exc.message:
                raise UploadError(exc.message) from None
        digest = hashlib.md5(open(file_path, "rb").read()).hexdigest()[:8]
        stem, ext = os.path.splitext(base)
        alt = f"{stem}_{digest}{ext}"
        log.warning("%s 已存在于服务端，改用内容哈希名 %s 重传", base, alt)
        try:
            return self._post_multipart(UPLOAD_ENDPOINT, file_path, alt, subfolder)
        except (JobRejectedError, BackendUnavailableError) as exc:
            raise UploadError(f"上传 {base} 失败：{exc}") from None
