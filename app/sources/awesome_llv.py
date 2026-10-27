"""模块 A-3 · Awesome-Low-Level-Vision 经典论文合集（Kobaayyy 等社区维护）。

定位：arXiv 源只看「最新提交」，往年的经典/顶会论文会被时间窗口埋没——
这个仓库按会议年份整理了底层视觉全景（CVPR2020–2026 等），是「经典池」的
主数据源，解除「仅限最新论文」的限制。

数据形态（每年一个 md 文件，主 README 只是索引）：
    ### 论文标题
    - Paper: https://arxiv.org/abs/2411.13383
    - Code: https://github.com/...

抓取方式：raw.githubusercontent 在部分网络被 DNS 阻断，统一走
api.github.com base64 内容接口（配置 GITHUB_TOKEN 可提升配额）。

去重：Item.fingerprint 基于标题归一化——同一篇论文被 arXiv 源与本源同时
收录时自动合并，无需特殊处理。
"""
from __future__ import annotations

import asyncio
import base64
import re

from ..config import topics
from ..filters.scoring import score_item
from ..models import Item
from .base import HttpClient, strip_html
from .competitions import github_headers

REPO_API = ("https://api.github.com/repos/Kobaayyy/"
            "Awesome-CVPR2026-CVPR2025-CVPR2024-CVPR2021-CVPR2020-Low-Level-Vision"
            "/contents/{file}")

# 年份清单文件：文件名同时是 venue 的来源（CVPR2025.md → CVPR 2025）
DEFAULT_FILES = ("CVPR2026.md", "CVPR2025.md", "CVPR2024.md", "CVPR2021.md", "CVPR2020.md")

_ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_SECTION_RE = re.compile(r"^#{1,3}\s+(?!#)\s*(.+?)\s*$")
_TITLE_RE = re.compile(r"^###\s+(.+?)\s*$")
_PAPER_RE = re.compile(r"^-\s*Paper[:：]\s*(https?://\S+)", re.I)
_CODE_RE = re.compile(r"^-\s*Code[:：]\s*(https?://\S+)", re.I)
# 索引/导航里的假标题（仓库链接、目录锚点）不当作论文
_NAV_TITLE_RE = re.compile(r"awesome|collection|相关整理|star|pr", re.I)


def parse_year_md(text: str, venue: str) -> list[dict]:
    """解析一个年份清单文件，返回 [{title, url, code_url, section}]。

    文件按块组织：「### 标题」开头，紧跟「- Paper: 链接」「- Code: 链接」；
    小节标题（## 1.超分辨率(Super-Resolution)）给出任务方向，拼进摘要
    供关键词评分用。
    """
    entries: list[dict] = []
    section = ""
    title = ""
    paper_url = ""
    code_url = ""

    def _flush():
        nonlocal title, paper_url, code_url
        if title and paper_url:
            entries.append({"title": title, "url": paper_url,
                            "code_url": code_url, "section": section})
        title, paper_url, code_url = "", "", ""

    for raw in text.splitlines():
        line = raw.strip()
        m = _TITLE_RE.match(line)
        if m:
            _flush()
            candidate = strip_html(m.group(1), 200)
            if candidate and not _NAV_TITLE_RE.search(candidate.lower()):
                title = candidate
            continue
        m = _PAPER_RE.match(line)
        if m:
            paper_url = m.group(1).rstrip(".,);")
            continue
        m = _CODE_RE.match(line)
        if m and title:
            code_url = m.group(1).rstrip(".,);")
            continue
        m = _SECTION_RE.match(line)
        if m and not line.startswith("###"):
            _flush()
            head = strip_html(m.group(1), 80)
            # 跳过仓库大标题与导航节
            if head and not _NAV_TITLE_RE.search(head.lower()):
                section = head
    _flush()
    for e in entries:
        e["venue"] = venue
    return entries


async def fetch(client: HttpClient, max_results: int | None = None) -> list[Item]:
    cfg = topics.raw.get("academic", {}).get("awesome", {})
    if not cfg.get("enabled", True):
        return []
    files = tuple(cfg.get("files") or DEFAULT_FILES)
    per_file_cap = int(cfg.get("max_per_file", 40))

    items: dict[str, Item] = {}
    for i, file in enumerate(files):
        vm = re.match(r"([A-Za-z]+)(\d{4})", file.rsplit(".", 1)[0])
        venue = f"{vm.group(1).upper()} {vm.group(2)}" if vm else ""
        try:
            data = await client.json(REPO_API.format(file=file), headers=github_headers())
        except Exception:
            continue
        text = ""
        if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
            text = base64.b64decode(data["content"]).decode("utf-8", "ignore")
        if not text:
            continue

        count = 0
        for entry in parse_year_md(text, venue):
            if count >= per_file_cap:
                break
            title = entry["title"]
            if not title:
                continue
            url = entry["url"]
            arxiv_id = ""
            m = _ARXIV_ID_RE.search(url)
            if m:
                arxiv_id = m.group(1)
                url = f"https://arxiv.org/abs/{arxiv_id}"
            section = entry["section"]
            summary = f"{section}。发表于 {venue} 的底层视觉论文" if section else f"发表于 {venue} 的底层视觉论文"
            item = Item(
                module="paper",
                title=title,
                url=url,
                source=f"Awesome {venue}",
                published_at=None,  # 经典论文：清单只有会议年份，不设发布时间（时效过滤放行）
                summary=summary,
                authors=[],
                venue=venue,
                year=vm.group(2) if vm else "",
                category=section or "",
                code_url=entry["code_url"],
                extra={"awesome_file": file, "arxiv_id": arxiv_id},
            )
            item.score, item.keywords = score_item(item, topics)
            items.setdefault(item.fingerprint, item)
            count += 1
        if i < len(files) - 1:
            await asyncio.sleep(0.8)  # api.github.com 礼貌间隔
    out = list(items.values())
    out.sort(key=lambda x: x.score, reverse=True)
    return out[: max_results] if max_results else out
