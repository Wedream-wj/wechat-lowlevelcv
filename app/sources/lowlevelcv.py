"""模块 A-1 · 熵视 lowlevelcv.com 论文精选抓取。

页面结构（2026-10 实测，见 tests/fixtures/lowlevelcv_page1.html）：

    div.papersList
      div.lunItem
        div.inFl > span            任务方向，如「图像视频去摩尔纹」
        a.flex
          h3                       中文标题
          div.sumBox p             作者 / 时间 / 刊物
          div.imgBox img           示意图
          div.it p.sum             中文摘要
          div.m1[data-href]        论文链接（arXiv）
          div.m2[data-href]        代码链接（GitHub，可能没有）

分页走 `?current=N`（1 起），每页 9 条，共 200+ 页。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from ..config import topics
from ..models import Item
from .base import HttpClient, SourceError, abs_url
from ..filters.scoring import score_item

SITE = "https://lowlevelcv.com"


def parse_papers_html(html: str, page_url: str = SITE) -> list[Item]:
    soup = BeautifulSoup(html, "lxml")
    items: list[Item] = []
    for node in soup.select(".papersList .lunItem"):
        h3 = node.select_one("h3")
        if not h3:
            continue
        title = h3.get_text(" ", strip=True)

        meta: dict[str, str] = {}
        for p in node.select(".sumBox p"):
            text = p.get_text(" ", strip=True)
            if "：" in text:
                key, value = text.split("：", 1)
            elif ":" in text:
                key, value = text.split(":", 1)
            else:
                continue
            meta[key.strip()] = value.strip()

        authors = [a.strip() for a in meta.get("作者", "").split(",") if a.strip()]
        year = meta.get("时间", "")
        venue = meta.get("刊物", "")

        category = ""
        cat_node = node.select_one(".inFl span")
        if cat_node:
            category = cat_node.get_text(" ", strip=True)

        abstract_node = node.select_one(".it p.sum") or node.select_one("p.sum")
        abstract = abstract_node.get_text(" ", strip=True) if abstract_node else ""

        img_node = node.select_one(".imgBox img")
        image = abs_url(SITE, img_node.get("src", "")) if img_node else ""

        paper_url = ""
        m1 = node.select_one(".m1")
        if m1 and m1.get("data-href"):
            paper_url = abs_url(SITE, m1["data-href"])
        code_url = ""
        m2 = node.select_one(".m2")
        if m2 and m2.get("data-href"):
            code_url = abs_url(SITE, m2["data-href"])

        if not paper_url:
            anchor = node.select_one("a[href^='http']")
            paper_url = anchor["href"] if anchor else page_url

        item = Item(
            module="paper",
            title=title,
            url=paper_url,
            source="熵视 lowlevelcv",
            published_at=datetime.now(timezone.utc),
            summary=abstract,
            authors=authors,
            venue=venue,
            year=year,
            category=category,
            code_url=code_url,
            image_url=image,
            images=[image] if image else [],
            extra={"origin": "lowlevelcv", "title_zh": title, "page_url": page_url},
        )
        item.score, item.keywords = score_item(item, topics)
        # 熵视本身已经做过一轮人工精选，给一个加成
        item.score += 2.0
        items.append(item)
    return items


async def fetch(client: HttpClient, max_pages: int | None = None) -> list[Item]:
    cfg = topics.raw.get("academic", {}).get("lowlevelcv", {})
    if not cfg.get("enabled", True):
        return []
    base = cfg.get("base_url", f"{SITE}/papers-selection")
    pages = max_pages or int(cfg.get("max_pages", 3))
    interval = float(cfg.get("request_interval", 1.2))

    collected: dict[str, Item] = {}
    for page in range(1, pages + 1):
        url = base if page == 1 else f"{base}?current={page}"
        try:
            html = await client.text(url)
        except Exception as exc:  # 单页失败不影响其它页
            if page == 1:
                raise SourceError(f"熵视首页抓取失败：{exc}") from exc
            break
        for item in parse_papers_html(html, url):
            collected.setdefault(item.fingerprint, item)
        if page < pages:
            await asyncio.sleep(interval)
    return list(collected.values())
