"""模块 B 补充源 · GitHub 开源项目动态。

为什么需要它：公众号 RSS（RSSHub 公共实例）经常被限流或反爬，一旦全挂，
业界模块就会静默返回 0 条。而「开源工具 / 算法更新」本身就是模块 B 的
核心选题之一，GitHub 搜索 API 稳定、结构化、可校验，适合当兜底源。

只取满足以下条件的仓库：
- 近 N 天有 push（说明在维护，不是僵尸项目）
- star 数达标（默认 80，可配）
- 描述/主题命中底层视觉词表
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from ..config import topics
from ..filters.scoring import classify_category, score_item
from ..models import Item
from .base import HttpClient, SourceError, strip_html
from .competitions import github_headers

API = "https://api.github.com/search/repositories"


def _cfg() -> dict:
    return (topics.raw.get("industry") or {}).get("github") or {}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _to_item(repo: dict, weight: float) -> Item | None:
    name = repo.get("full_name") or repo.get("name")
    url = repo.get("html_url")
    if not name or not url:
        return None
    desc = strip_html(repo.get("description") or "", 400)
    stars = int(repo.get("stargazers_count") or 0)
    topics_list = repo.get("topics") or []
    pushed = _parse_time(repo.get("pushed_at")) or _parse_time(repo.get("updated_at"))

    title = f"{name}：{desc[:60]}" if desc else name
    summary = (
        f"{desc}｜⭐ {stars}　主力语言：{repo.get('language') or '未知'}　"
        f"最近更新：{pushed.strftime('%Y-%m-%d') if pushed else '未知'}"
    )
    # GitHub 为每个仓库生成稳定的 OpenGraph 预览图（1200×630），
    # 既是合规可引用的官方素材，也省掉「给开源项目配图」的麻烦
    og_image = f"https://opengraph.githubassets.com/1/{name}"
    item = Item(
        module="industry",
        title=title,
        url=url,
        source="GitHub 开源",
        published_at=pushed,
        summary=summary,
        category=classify_category(f"{name} {desc} {' '.join(topics_list)}", topics),
        image_url=og_image,
        images=[og_image],
        extra={
            "origin": "github",
            "repo": name,
            "stars": stars,
            "topics": topics_list,
            "language": repo.get("language") or "",
            "homepage": repo.get("homepage") or "",
            "weight": weight,
        },
    )
    item.score, item.keywords = score_item(item, topics)
    # 星标量是开源项目的「关注度」代理指标：1 万星 ≈ +6 分
    item.score += min(6.0, stars / 1667.0)
    item.score += weight
    return item


async def fetch(client: HttpClient) -> list[Item]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return []

    queries: list[str] = cfg.get("queries") or ["image restoration", "super resolution"]
    min_stars = int(cfg.get("min_stars", 80))
    days = int(cfg.get("active_days", 180))
    per_page = int(cfg.get("per_query", 15))
    weight = float(cfg.get("weight", 0.8))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    # token 来源：topics.yaml industry.github.token，或环境变量 GITHUB_TOKEN / GH_TOKEN
    token = cfg.get("token") or os.getenv("GITHUB_TOKEN", "").strip() or os.getenv("GH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    merged: dict[str, Item] = {}
    for index, query in enumerate(queries):
        params = {
            "q": f'{query} in:name,description,topics stars:>={min_stars} pushed:>{since}',
            "sort": "stars",
            "order": "desc",
            "per_page": per_page,
        }
        try:
            data = await client.json(API, params=params, headers=headers)
        except SourceError:
            continue
        for repo in (data or {}).get("items", []) or []:
            item = _to_item(repo, weight)
            if item is None:
                continue
            current = merged.get(item.fingerprint)
            if current is None or item.score > current.score:
                merged[item.fingerprint] = item
        # 未鉴权时搜索接口限流 10 次/分钟，慢一点更稳
        if index + 1 < len(queries):
            await asyncio.sleep(1.2 if token else 6.5)

    return list(merged.values())
