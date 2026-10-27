"""模块 B · 业界动态抓取（RSS / Atom 通用解析）。

支持三类源：
1. 公众号镜像 RSS（RSSHub 路由，如 /jiqizhixin/daily、/paperweekly）；
2. 官方博客（Hugging Face、Google Research、OpenAI）；
3. 任意可订阅的 RSS —— 只要在 config/topics.yaml 的 industry.feeds 里加一行。

注意：公众号内容版权归原作者，本系统只做「线索发现 + 二次解读」，
正文改写必须由大模型重写并注明来源，不能整段搬运。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import mktime

import feedparser

from ..config import topics
from ..filters.scoring import classify_category, score_item
from ..models import Item
from .base import HttpClient, abs_url, strip_html


def _entry_image(entry: dict) -> str:
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key) or []
        if isinstance(media, list) and media:
            url = media[0].get("url")
            if url:
                return url
    for link in entry.get("links", []) or []:
        if str(link.get("type", "")).startswith("image/"):
            return link.get("href", "")
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image/"):
            return enc.get("href", "") or enc.get("url", "")
    summary = entry.get("summary", "") or ""
    if "<img" in summary:
        import re

        m = re.search(r'<img[^>]+src=["\']([^"\']+)', summary)
        if m:
            return m.group(1)
    return ""


def parse_feed(body: str, feed_name: str, weight: float = 1.0) -> list[Item]:
    parsed = feedparser.parse(body)
    items: list[Item] = []
    for entry in parsed.entries:
        title = strip_html(entry.get("title", ""), 200)
        link = entry.get("link", "")
        if not title or not link:
            continue
        raw_summary = entry.get("summary", "") or ""
        content = ""
        if entry.get("content"):
            content = entry["content"][0].get("value", "")
        summary = strip_html(content or raw_summary, 1200)

        published = None
        for key in ("published_parsed", "updated_parsed"):
            if entry.get(key):
                published = datetime.fromtimestamp(mktime(entry[key]), tz=timezone.utc)
                break

        image = _entry_image(entry)
        item = Item(
            module="industry",
            title=title,
            url=link,
            source=feed_name,
            published_at=published,
            summary=summary,
            category=classify_category(f"{title} {summary}", topics, "industry"),
            image_url=image,
            images=[image] if image else [],
            extra={"origin": "rss", "feed": feed_name, "weight": weight},
        )
        item.score, item.keywords = score_item(item, topics)
        item.score += weight  # 源权重
        items.append(item)
    return items


# 最近一次抓取的逐源体检结果，供 API / 控制台展示。
# 关键点：RSS 被反爬时 feedparser 不会抛错，只会返回 0 条 —— 必须显式记录原因，
# 否则模块会静默产出空稿，运维完全看不出问题在哪。
LAST_REPORT: list[dict] = []


async def fetch(client: HttpClient) -> list[Item]:
    feeds = topics.rss_feeds
    results: list[dict] = []

    async def one(feed: dict) -> None:
        url = feed.get("url")
        name = feed.get("name") or url
        if not url:
            return
        # enabled: false 的源直接跳过（如公共 RSSHub 实例已不可用），
        # 不参与抓取也不刷告警，避免「真正的故障」被一片 502 淹没
        if feed.get("enabled") is False:
            return
        try:
            body = await client.text(url)
        except Exception as exc:
            results.append({"feed": name, "url": url, "count": 0, "error": f"{type(exc).__name__}: {exc}"})
            return

        parsed_feed = feedparser.parse(body)
        parsed = parse_feed(body, name, float(feed.get("weight", 1.0)))
        # 单源截断：OpenAI News 一次能吐 1300+ 条，不截会把候选池淹掉，
        # 让量子位/HackCV 这类产业源永远排不进来。按发布时间取最新的 N 条。
        limit = int(topics.industry.get("feed_max_items", 60) or 0)
        if limit and len(parsed) > limit:
            parsed.sort(
                key=lambda i: i.published_at.timestamp() if i.published_at else 0,
                reverse=True,
            )
            parsed = parsed[:limit]
        entry: dict = {"feed": name, "url": url, "count": len(parsed)}
        if not parsed:
            bozo = getattr(parsed_feed, "bozo_exception", None)
            head = (body or "")[:120].replace("\n", " ")
            if bozo:
                entry["error"] = f"不是合法 XML：{bozo}"
            elif "html" in head.lower() or head.lstrip().startswith("<!"):
                entry["error"] = "返回的是 HTML（大概率被反爬/需要登录）"
            else:
                entry["error"] = "解析成功但没有条目（源可能已停更）"
            entry["head"] = head
        results.append(entry)
        if parsed:
            results[-1]["items"] = parsed

    await asyncio.gather(*(one(f) for f in feeds))

    LAST_REPORT.clear()
    LAST_REPORT.extend(
        {k: v for k, v in r.items() if k != "items"} for r in results
    )

    merged: dict[str, Item] = {}
    for result in results:
        for item in result.get("items", []):
            current = merged.get(item.fingerprint)
            if current is None or item.score > current.score:
                merged[item.fingerprint] = item
    return list(merged.values())


def feed_health() -> list[dict]:
    """返回最近一次抓取的逐源体检结果；没跑过就返回配置清单。"""
    if LAST_REPORT:
        return list(LAST_REPORT)
    return [{"name": f.get("name"), "url": f.get("url"), "count": None} for f in topics.rss_feeds]
