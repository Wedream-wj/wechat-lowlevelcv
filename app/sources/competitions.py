"""模块 C · 赛事聚合。

四个来源，按可靠性排序：
1. Kaggle 官方 API（结构化最好，需要 KAGGLE_USERNAME / KAGGLE_KEY，免费）；
2. 天池（接口非公开，做了宽松解析 + 失败降级，不会阻塞流水线）；
3. 搜索补全（Serper / Tavily，需 key），用来覆盖 CVPR/ECCV workshop、CCF 赛事；
4. 人工策展页（NTIRE / MIPI），作为稳定兜底。

统一产出 Item，结构化字段放 extra：platform / prize / deadline / start / entry_url / track。
最终由大模型在 `contest` prompt 里补齐并校验，缺失字段一律写「待官方公布」。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from ..config import topics
from ..filters.scoring import classify_category, score_item
from ..models import Item
from .base import HttpClient, abs_url, strip_html

KAGGLE_API = "https://www.kaggle.com/api/v1/competitions/list"


# ------------------------------------------------------------------ Kaggle
def parse_kaggle(payload: list[dict]) -> list[Item]:
    items: list[Item] = []
    for row in payload or []:
        title = row.get("title") or row.get("ref") or ""
        if not title:
            continue
        ref = row.get("ref", "")
        url = f"https://www.kaggle.com/competitions/{ref}" if ref else row.get("url", "")
        deadline = None
        if row.get("deadline"):
            try:
                deadline = datetime.fromisoformat(str(row["deadline"]).replace("Z", "+00:00"))
            except ValueError:
                deadline = None
        desc = strip_html(row.get("description", "") or "", 600)
        item = Item(
            module="contest",
            title=title,
            url=url,
            source="Kaggle",
            published_at=row.get("enabledDate") and _parse_dt(row["enabledDate"]) or datetime.now(timezone.utc),
            summary=desc,
            category=classify_category(f"{title} {desc}", topics) or "竞赛",
            image_url=row.get("thumbnailImageUrl", "") or "",
            extra={
                "origin": "kaggle",
                "platform": "Kaggle",
                "prize": row.get("reward", "") or "以官方页面为准",
                "deadline": deadline.isoformat() if deadline else "",
                "entry_url": url,
                "tags": row.get("tags", []),
                "team_count": row.get("teamCount"),
            },
        )
        item.score, item.keywords = score_item(item, topics)
        item.score += 3.0 if deadline and deadline > datetime.now(timezone.utc) else 0.0
        items.append(item)
    return items


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def fetch_kaggle(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("competitions", {}).get("kaggle", {})
    if not cfg.get("enabled", True):
        return []
    user = os.getenv("KAGGLE_USERNAME", "")
    key = os.getenv("KAGGLE_KEY", "")
    if not (user and key):
        return []  # 没配凭证就跳过，不报错
    out: dict[str, Item] = {}
    for term in cfg.get("search_terms", [])[:6]:
        try:
            data = await client.json(
                KAGGLE_API,
                params={"search": term, "page": 1, "pageSize": 20},
                auth=(user, key),
            )
        except Exception:
            continue
        for item in parse_kaggle(data if isinstance(data, list) else []):
            out.setdefault(item.fingerprint, item)
        await asyncio.sleep(0.8)
    return list(out.values())


# ------------------------------------------------------------------ 天池
def _extract_records(payload: Any, depth: int = 0) -> list[dict]:
    """在天池各种嵌套 JSON 里找「像比赛列表」的数组。"""
    if depth > 4:
        return []
    if isinstance(payload, list):
        dicts = [x for x in payload if isinstance(x, dict)]
        if dicts and any(("name" in d or "title" in d) for d in dicts):
            return dicts
        for x in payload:
            found = _extract_records(x, depth + 1)
            if found:
                return found
    elif isinstance(payload, dict):
        for value in payload.values():
            found = _extract_records(value, depth + 1)
            if found:
                return found
    return []


async def fetch_tianchi(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("competitions", {}).get("tianchi", {})
    if not cfg.get("enabled", True):
        return []
    api = cfg.get("api_url", "https://tianchi.aliyun.com/api/competition/list")
    payloads = [
        {"pageNum": 1, "pageSize": 20, "status": 1},
        {"page": 1, "size": 20, "state": "active"},
        {"pageNum": 1, "pageSize": 20},
    ]
    records: list[dict] = []
    for body in payloads:
        try:
            data = await client.json(api, method="POST", json=body)
        except Exception:
            continue
        records = _extract_records(data)
        if records:
            break
    if not records:
        return []

    items: list[Item] = []
    for row in records:
        title = row.get("name") or row.get("title") or ""
        if not title:
            continue
        cid = row.get("id") or row.get("competitionId") or row.get("raceId")
        url = f"https://tianchi.aliyun.com/competition/entrance/{cid}/information" if cid else cfg.get("list_url", "")
        desc = strip_html(str(row.get("description") or row.get("brief") or ""), 600)
        item = Item(
            module="contest",
            title=strip_html(title, 160),
            url=url,
            source="天池",
            published_at=datetime.now(timezone.utc),
            summary=desc,
            category=classify_category(f"{title} {desc}", topics) or "竞赛",
            extra={
                "origin": "tianchi",
                "platform": "天池",
                "prize": str(row.get("reward") or row.get("bonus") or "以官方页面为准"),
                "deadline": str(row.get("endTime") or row.get("deadline") or ""),
                "entry_url": url,
            },
        )
        item.score, item.keywords = score_item(item, topics)
        items.append(item)
    return items


# --------------------------------------------------------------- 搜索补全
async def fetch_web_search(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("competitions", {}).get("web_search", {})
    if not cfg.get("enabled", True):
        return []
    serper = os.getenv("SERPER_API_KEY", "")
    tavily = os.getenv("TAVILY_API_KEY", "")
    if not (serper or tavily):
        return []
    queries = cfg.get("queries", [])
    items: dict[str, Item] = {}

    for query in queries:
        results: list[dict] = []
        try:
            if serper:
                data = await client.json(
                    "https://google.serper.dev/search",
                    method="POST",
                    headers={"X-API-KEY": serper, "Content-Type": "application/json"},
                    json={"q": query, "num": 8, "gl": "cn", "hl": "zh-cn"},
                )
                results = data.get("organic", []) if isinstance(data, dict) else []
            elif tavily:
                data = await client.json(
                    "https://api.tavily.com/search",
                    method="POST",
                    json={"api_key": tavily, "query": query, "max_results": 8, "search_depth": "basic"},
                )
                results = data.get("results", []) if isinstance(data, dict) else []
        except Exception:
            continue
        for row in results:
            title = strip_html(row.get("title", ""), 180)
            link = row.get("link") or row.get("url") or ""
            if not title or not link:
                continue
            snippet = strip_html(row.get("snippet") or row.get("content") or "", 500)
            item = Item(
                module="contest",
                title=title,
                url=link,
                source="Web 检索",
                published_at=datetime.now(timezone.utc),
                summary=snippet,
                category=classify_category(f"{title} {snippet}", topics) or "竞赛",
                extra={
                    "origin": "search",
                    "platform": _guess_platform(link),
                    "prize": "待官方公布",
                    "deadline": "",
                    "entry_url": link,
                    "query": query,
                },
            )
            item.score, item.keywords = score_item(item, topics)
            item.score += 1.5 if item.category != "竞赛" else 0.0
            items.setdefault(item.fingerprint, item)
        await asyncio.sleep(0.5)
    return list(items.values())


def _guess_platform(url: str) -> str:
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
    if "kaggle" in host:
        return "Kaggle"
    if "tianchi" in host:
        return "天池"
    if "codalab" in host:
        return "CodaLab"
    if "github" in host:
        return "GitHub"
    return host or "官网"


# --------------------------------------------------------------- 策展页
async def fetch_curated(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("competitions", {}).get("curated", [])
    items: list[Item] = []
    for entry in cfg:
        url = entry.get("url", "")
        if not url:
            continue
        try:
            html = await client.text(url)
        except Exception:
            continue
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        h1 = soup.find(["h1", "h2"])
        page_title = strip_html(h1.get_text(" ", strip=True), 120) if h1 else entry.get("name", url)
        name = entry.get("name", "")
        # 避免出现「NTIRE 2026 · NTIRE 2026」这种重复标题
        title = name if (not page_title or name in page_title) else f"{name} · {page_title}"
        desc_node = soup.find("meta", attrs={"name": "description"})
        desc = strip_html(desc_node.get("content", "") if desc_node else "", 400)
        if not desc or len(desc) < 30:
            desc = entry.get("desc", "") or strip_html(soup.get_text(" ", strip=True), 600)

        item = Item(
            module="contest",
            title=title,
            url=url,
            source="官方赛事页",
            published_at=datetime.now(timezone.utc),
            summary=desc,
            category="竞赛",
            extra={
                "origin": "curated",
                "platform": entry.get("name", ""),
                "prize": "待官方公布",
                "deadline": "",
                "entry_url": url,
            },
        )
        item.score, item.keywords = score_item(item, topics)
        item.score += 2.0
        items.append(item)
    return items


async def fetch(client: HttpClient) -> list[Item]:
    groups = await asyncio.gather(
        fetch_kaggle(client), fetch_tianchi(client), fetch_web_search(client), fetch_curated(client),
        return_exceptions=True,
    )
    merged: dict[str, Item] = {}
    for group in groups:
        if isinstance(group, Exception):
            continue
        for item in group:
            merged.setdefault(item.fingerprint, item)
    return list(merged.values())
