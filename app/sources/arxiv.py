"""模块 A-2 · arXiv 抓取 + 论文插图自动提取。

arXiv API 是 Atom XML，无需 key，但要守规矩：两次请求间隔 >= 3s。

插图提取策略（优先级从高到低）：
1. arXiv HTML 版（`arxiv.org/html/{id}`）里的 <figure>，按 caption 关键词挑「架构图 / pipeline」；
2. PDF 首页用 PyMuPDF 渲染成 PNG（多数论文首页含 teaser/架构图）；
3. 都没有就返回空，渲染层会自动降级为纯文字卡片。
"""
from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from ..config import settings, topics
from ..models import Item
from ..filters.scoring import classify_category, score_item
from .base import HttpClient, abs_url, strip_html

NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ARCH_HINTS = ("architect", "overview", "pipeline", "framework", "overall", "method", "网络结构", "框架")


def _text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    found = node.find(path, NS)
    return (found.text or "").strip() if found is not None else ""


def parse_arxiv_feed(xml_text: str) -> list[Item]:
    root = ET.fromstring(xml_text)
    items: list[Item] = []
    for entry in root.findall("atom:entry", NS):
        raw_id = _text(entry, "atom:id")
        arxiv_id = re.sub(r"^https?://arxiv.org/abs/", "", raw_id)
        title = re.sub(r"\s+", " ", _text(entry, "atom:title"))
        summary = re.sub(r"\s+", " ", _text(entry, "atom:summary"))
        if not title:
            continue
        authors = [
            (a.find("atom:name", NS).text or "").strip()
            for a in entry.findall("atom:author", NS)
            if a.find("atom:name", NS) is not None
        ]
        published_raw = _text(entry, "atom:published")
        try:
            published = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
        except ValueError:
            published = None

        comment = _text(entry, "arxiv:comment")
        journal = _text(entry, "arxiv:journal_ref")
        primary = entry.find("arxiv:primary_category", NS)
        category_code = primary.get("term", "") if primary is not None else ""

        venue = "arXiv"
        for token in ("CVPR", "ICCV", "ECCV", "NeurIPS", "ICLR", "ICML", "AAAI", "SIGGRAPH", "TPAMI", "IJCV", "TIP"):
            if token.lower() in (comment + journal).lower():
                venue = token
                break
        if journal and venue == "arXiv":
            venue = journal[:60]

        code_match = re.search(r"https?://github\.com/[\w\-./]+", summary + " " + comment)
        code_url = code_match.group(0).rstrip(".,);") if code_match else ""

        year_match = re.search(r"(20\d{2})", published_raw)
        item = Item(
            module="paper",
            title=title,
            url=f"https://arxiv.org/abs/{arxiv_id}",
            source="arXiv",
            published_at=published,
            summary=summary,
            authors=authors,
            venue=venue,
            year=year_match.group(1) if year_match else "",
            category=classify_category(f"{title} {summary}", topics) or category_code,
            code_url=code_url,
            extra={"arxiv_id": arxiv_id, "comment": comment, "journal_ref": journal},
        )
        item.score, item.keywords = score_item(item, topics)
        items.append(item)
    return items


def pick_figure_from_html(html: str, base_url: str) -> tuple[str, str]:
    """从 arXiv HTML 版里挑最像架构图的一张，返回 (url, caption)。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    candidates: list[tuple[int, str, str]] = []
    for idx, fig in enumerate(soup.select("figure")):
        img = fig.select_one("img")
        if not img or not img.get("src"):
            continue
        caption_node = fig.select_one("figcaption")
        caption = caption_node.get_text(" ", strip=True) if caption_node else ""
        src = abs_url(base_url, img["src"])
        if src.endswith(".svg"):
            continue
        lowered = (caption + " " + img.get("src", "")).lower()
        weight = 0
        if any(h in lowered for h in ARCH_HINTS):
            weight += 5
        if idx == 0:
            weight += 2
        if any(bad in lowered for bad in ("logo", "teaser", "icon")):
            weight -= 1
        candidates.append((weight, src, caption[:160]))
    if not candidates:
        return "", ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][2]


async def extract_figure(client: HttpClient, arxiv_id: str, item_title: str = "",
                         pdf_fallback: bool = False) -> tuple[str, str]:
    """返回 (本地图片路径, 图注)。失败返回 ("", "")。

    PDF 首页渲染很重（要下整个 PDF），默认关闭；配置 arxiv.pdf_fallback: true 才启用。
    """
    short = re.sub(r"v\d+$", "", arxiv_id)
    html_url = f"https://arxiv.org/html/{arxiv_id}"
    try:
        html = await client.text(html_url)
        url, caption = pick_figure_from_html(html, html_url)
        if url:
            local = await client.download_image(url, referer=html_url)
            if local:
                return local, caption or item_title
    except Exception:
        pass

    if not pdf_fallback:
        return "", ""
    # 兜底：PDF 首页渲染（需要 pymupdf，没装就跳过）
    try:
        import fitz  # type: ignore

        data, _ = await client.get_bytes(f"https://arxiv.org/pdf/{short}")
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.page_count:
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            out = settings.image_dir / f"arxiv_{short.replace('/', '_')}_p1.png"
            pix.save(str(out))
            return str(out), item_title
    except Exception:
        pass
    return "", ""


async def fetch(client: HttpClient, max_results: int | None = None) -> list[Item]:
    cfg = topics.raw.get("academic", {}).get("arxiv", {})
    if not cfg.get("enabled", True):
        return []
    endpoint = cfg.get("endpoint", "http://export.arxiv.org/api/query")
    limit = max_results or int(cfg.get("max_results_per_query", 25))
    queries = cfg.get("queries") or topics.arxiv_queries

    collected: dict[str, Item] = {}
    for i, query in enumerate(queries):
        params = {
            "search_query": query,
            "start": 0,
            "max_results": limit,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        try:
            xml_text = await client.text(endpoint, params=params)
        except Exception:
            continue
        for item in parse_arxiv_feed(xml_text):
            collected.setdefault(item.fingerprint, item)
        if i < len(queries) - 1:
            await asyncio.sleep(3.0)  # arXiv 官方建议
    return list(collected.values())


async def enrich_figures(client: HttpClient, items: list[Item], limit: int = 3) -> None:
    """给评分最高的几篇补架构图（串行 + 硬超时，避免把整条流水线拖住）。"""
    cfg = topics.raw.get("academic", {}).get("arxiv", {})
    pdf_fallback = bool(cfg.get("pdf_fallback", False))
    for item in sorted(items, key=lambda x: x.score, reverse=True)[:limit]:
        arxiv_id = item.extra.get("arxiv_id")
        if not arxiv_id or item.images:
            continue
        try:
            local, caption = await asyncio.wait_for(
                extract_figure(client, arxiv_id, item.title, pdf_fallback=pdf_fallback), timeout=25.0
            )
        except Exception:
            continue
        if local:
            item.image_url = local
            item.images = [local]
            item.extra["figure_caption"] = caption
        await asyncio.sleep(1.0)
