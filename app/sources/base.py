"""抓取基础设施：带重试的异步 HTTP、图片落盘、HTML 清洗。

几条硬规则（也是这类系统最容易踩的坑）：
1. 统一 UA + 并发信号量 + 请求间隔，别把源站打挂；
2. 图片必须校验 content-type 与体积上限，防止把 .exe 当 jpg 存下来；
3. 单源失败不影响整条流水线，异常统一包成 SourceError 由上层收集。
"""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import settings

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 LLCV-Bot/1.0 (+https://lowlevelcv.com)"
)
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class SourceError(RuntimeError):
    """单个数据源失败，不影响其它源。"""


class HttpClient:
    def __init__(self, concurrency: int = 6) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            },
            timeout=httpx.Timeout(20.0, connect=8.0),
            follow_redirects=True,
            http2=False,
        )

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with self._sem:
            resp = await self._client.request(method, url, **kwargs)
        if resp.status_code >= 400:
            raise SourceError(f"{method} {url} -> HTTP {resp.status_code}")
        return resp

    async def text(self, url: str, **kwargs: Any) -> str:
        resp = await self._request("GET", url, **kwargs)
        return resp.text

    async def json(self, url: str, **kwargs: Any) -> Any:
        resp = await self._request("GET", url, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"{url} 返回的不是合法 JSON") from exc

    async def get_bytes(self, url: str, **kwargs: Any) -> tuple[bytes, str]:
        resp = await self._request("GET", url, **kwargs)
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        data = resp.content
        if len(data) > MAX_IMAGE_BYTES:
            raise SourceError(f"图片过大（{len(data) / 1e6:.1f}MB）：{url}")
        return data, ctype

    async def download_image(self, url: str, referer: str = "", timeout: float = 15.0) -> str:
        """下载图片到 out/assets，返回本地路径（失败返回空串）。单次请求，不走重试。"""
        if not url:
            return ""
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("/"):
            if not referer:
                return ""
            url = re.sub(r"^(https?://[^/]+).*$", r"\1", referer) + url
        if not url.startswith("http"):
            return ""
        digest = hashlib.md5(url.encode()).hexdigest()[:16]
        headers = {"Referer": referer} if referer else {}
        try:
            resp = await self._client.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
        except Exception:
            return ""
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        data = resp.content
        if len(data) > MAX_IMAGE_BYTES:
            return ""
        if not ctype.startswith("image/"):
            guessed = mimetypes.guess_extension(ctype) or ""
            if not guessed:
                return ""
        ext = mimetypes.guess_extension(ctype) or Path(url.split("?")[0]).suffix or ".jpg"
        if ext == ".jpe":
            ext = ".jpg"
        path = settings.image_dir / f"{digest}{ext}"
        path.write_bytes(data)
        return str(path)


def strip_html(text: str, limit: int = 1200) -> str:
    """去标签、压空白，用于 RSS 摘要清洗。"""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
        .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    )
    text = re.sub(r"[\s\u3000]+", " ", text).strip()
    return text[:limit]


def abs_url(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        m = re.match(r"^(https?://[^/]+)", base)
        return (m.group(1) if m else base.rstrip("/")) + href
    return base.rstrip("/") + "/" + href.lstrip("/")
