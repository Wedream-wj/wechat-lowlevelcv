"""微信公众号 API 封装：access_token 缓存 → 正文图片上传 → 封面上传 → 新建草稿。

几个必须知道的坑：
1. 需要在公众号后台把服务器出口 IP 加入白名单，否则一律 40164；
2. access_token 有效期 7200s，且**新 token 会顶掉旧 token**，所以必须落盘缓存、不要并发刷新；
3. 正文图片走 media/uploadimg（返回微信 CDN URL），封面必须走 material/add_material 拿 media_id；
4. 草稿接口只创建草稿，不会群发；群发需要认证号且建议人工确认。
"""
from __future__ import annotations

import json
import mimetypes
import re
import time
from pathlib import Path

import httpx

from ..config import settings

BASE = "https://api.weixin.qq.com"
TOKEN_CACHE = settings.db_path.parent / "wx_token.json"


class WeChatError(RuntimeError):
    def __init__(self, message: str, errcode: int | None = None):
        super().__init__(message)
        self.errcode = errcode


class WeChatClient:
    def __init__(self) -> None:
        self.appid = settings.wx_appid
        self.secret = settings.wx_secret
        self._client = httpx.AsyncClient(timeout=60, follow_redirects=True)

    async def __aenter__(self) -> "WeChatClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    @property
    def ready(self) -> bool:
        return settings.wechat_ready

    # ------------------------------------------------------------ token
    def _read_cache(self) -> dict:
        if TOKEN_CACHE.exists():
            try:
                data = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
                if data.get("appid") == self.appid:
                    return data
            except Exception:
                pass
        return {}

    async def token(self, force: bool = False) -> str:
        if not self.ready:
            raise WeChatError("未配置 WX_APPID / WX_SECRET")
        cache = self._read_cache()
        if not force and cache.get("token") and cache.get("expires_at", 0) > time.time() + 120:
            return cache["token"]

        resp = await self._client.get(
            f"{BASE}/cgi-bin/token",
            params={"grant_type": "client_credential", "appid": self.appid, "secret": self.secret},
        )
        data = resp.json()
        if "access_token" not in data:
            raise WeChatError(f"获取 access_token 失败：{data}", data.get("errcode"))
        payload = {
            "appid": self.appid,
            "token": data["access_token"],
            "expires_at": time.time() + int(data.get("expires_in", 7200)) - 300,
        }
        TOKEN_CACHE.write_text(json.dumps(payload), encoding="utf-8")
        return payload["token"]

    async def _post(self, path: str, *, params: dict | None = None, json_body: dict | None = None,
                    files: dict | None = None, force_token: bool = False) -> dict:
        token = await self.token(force=force_token)
        resp = await self._client.post(
            f"{BASE}{path}",
            params={"access_token": token, **(params or {})},
            json=json_body,
            files=files,
        )
        try:
            data = resp.json()
        except ValueError as exc:
            raise WeChatError(f"{path} 返回非 JSON：{resp.text[:200]}") from exc
        errcode = data.get("errcode")
        if errcode in (40001, 40014, 42001):  # token 失效，强制刷新一次
            if not force_token:
                return await self._post(path, params=params, json_body=json_body, files=files, force_token=True)
        if errcode:
            raise WeChatError(f"{path} 失败：{data.get('errmsg')}（errcode={errcode}）", errcode)
        return data

    # ------------------------------------------------------------ upload
    async def upload_content_image(self, file_path: str | Path) -> str:
        """正文图片，返回可在 content 中直接使用的 URL。"""
        path = Path(file_path)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        with path.open("rb") as fh:
            data = await self._post(
                "/cgi-bin/media/uploadimg",
                files={"media": (path.name, fh.read(), mime)},
            )
        url = data.get("url", "")
        if not url:
            raise WeChatError(f"正文图片上传未返回 url：{data}")
        return url

    async def upload_cover_material(self, file_path: str | Path) -> str:
        """封面临时/永久素材，返回 thumb_media_id。"""
        path = Path(file_path)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        with path.open("rb") as fh:
            data = await self._post(
                "/cgi-bin/material/add_material",
                params={"type": "image"},
                files={"media": (path.name, fh.read(), mime)},
            )
        media_id = data.get("media_id", "")
        if not media_id:
            raise WeChatError(f"封面上传未返回 media_id：{data}")
        return media_id

    # ------------------------------------------------------------- draft
    async def rewrite_images(self, html: str) -> str:
        """把本地图片 src 换成微信 CDN URL（同一张图只传一次）。"""
        mapping: dict[str, str] = {}
        parts: list[str] = []
        last = 0
        pattern = re.compile(r'(<img[^>]+src=)(["\'])(.*?)\2', re.I)
        for m in pattern.finditer(html):
            parts.append(html[last:m.start()])
            prefix, quote, src = m.group(1), m.group(2), m.group(3)
            if src.startswith(("http://", "https://", "data:")):
                parts.append(m.group(0))
            else:
                path = Path(src)
                if not path.is_absolute():
                    # 成稿里的图片路径统一相对 out/ 目录
                    path = settings.out_dir / src
                if not path.exists():
                    parts.append(m.group(0))
                else:
                    if src not in mapping:
                        mapping[src] = await self.upload_content_image(path)
                    parts.append(f"{prefix}{quote}{mapping[src]}{quote}")
            last = m.end()
        parts.append(html[last:])
        return "".join(parts)

    async def add_draft(self, *, title: str, author: str, digest: str, content: str,
                        thumb_media_id: str, source_url: str = "") -> str:
        article = {
            "title": title[:64],
            "author": author[:16],
            "digest": digest[:120],
            "content": content,
            "content_source_url": source_url,
            "thumb_media_id": thumb_media_id,
            "need_open_comment": settings.wx_need_open_comment,
            "only_fans_can_comment": 0,
        }
        data = await self._post("/cgi-bin/draft/add", json_body={"articles": [article]})
        media_id = data.get("media_id", "")
        if not media_id:
            raise WeChatError(f"草稿创建未返回 media_id：{data}")
        return media_id
