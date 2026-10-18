"""Papers With Code 补充源。

注意：PWC 的公开 API 自 2025 年起稳定性很差（站点被收购后多次调整），
这里做了「静默降级」——404/超时直接返回空列表，不影响 arXiv 与熵视两条主源。
接口文档参考：https://paperswithcode.com/api/v1/docs/
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ..config import topics
from ..filters.scoring import classify_category, score_item
from ..models import Item
from .base import HttpClient, strip_html


def parse_pwc(payload: dict) -> list[Item]:
    items: list[Item] = []
    for row in payload.get("results", []) or []:
        title = row.get("title") or ""
        if not title:
            continue
        abstract = strip_html(row.get("abstract") or "", 1200)
        url = row.get("url") or (f"https://paperswithcode.com/paper/{row.get('id')}" if row.get("id") else "")
        item = Item(
            module="paper",
            title=title,
            url=url,
            source="Papers With Code",
            published_at=None,
            summary=abstract,
            authors=[a.get("name", "") for a in row.get("authors", []) if isinstance(a, dict)],
            venue=(row.get("proceeding") or "PWC")[:60],
            year=str(row.get("published", ""))[:4],
            category=classify_category(f"{title} {abstract}", topics),
            code_url=(row.get("repository") or {}).get("url", "") if isinstance(row.get("repository"), dict) else "",
            extra={"origin": "pwc", "pwc_id": row.get("id")},
        )
        item.score, item.keywords = score_item(item, topics)
        items.append(item)
    return items


async def fetch(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("academic", {}).get("paperswithcode", {})
    if not cfg.get("enabled", True):
        return []
    endpoint = cfg.get("endpoint", "https://paperswithcode.com/api/v1/papers/")
    queries = ["image restoration", "super resolution", "image denoising", "low light enhancement"]
    merged: dict[str, Item] = {}
    for query in queries:
        try:
            # PWC 的 API 时常无响应，单独压短超时，别拖累整条流水线
            data = await client.json(endpoint, params={"q": query, "items_per_page": 15}, timeout=8.0)
        except Exception:
            continue
        for item in parse_pwc(data if isinstance(data, dict) else {}):
            merged.setdefault(item.fingerprint, item)
        await asyncio.sleep(0.5)
    return list(merged.values())
