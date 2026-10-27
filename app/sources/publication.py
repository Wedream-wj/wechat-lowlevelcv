"""论文发表状态查证：arXiv 预印本是否已被顶级会议/期刊正式收录。

为什么需要：纯预印本未经同行评议，公众号标题打 arXiv 标签会拉低权威感；
而作者常在 arXiv comment 里写「Accepted to CVPR 2026」却不改标题——成稿前
必须检索校正。查证链（按可信度排序，全免 key）：

1. 作者自述：arXiv comment / journal_ref 字段（作者自己声明的接收信息）；
2. OpenReview 检索：标题精确匹配 + venue 字段（CVPR/ICLR/NeurIPS 等都在此）；
3. Semantic Scholar：按 arXiv ID 直查 venue 字段（匿名限流严重，只做兜底重试）。

返回约定（不抛异常，失败降级，不阻塞管线）：
- {"accepted": True,  "venue": "CVPR 2026", "source": "OpenReview"}   正式发表
- {"accepted": False, "preprint_only": True, "source": "..."}          检索确认仅预印本
- {"checked": False,  "reason": "..."}                                 全渠道失败，未知
"""

from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher

from .base import HttpClient

# 顶级 venue 白名单：只有这些才算「正式收录」（期刊会议之外的一律不算）
TOP_VENUES = {
    "CVPR", "ICCV", "ECCV", "NeurIPS", "ICLR", "ICML", "AAAI",
    "SIGGRAPH", "SIGGRAPH Asia", "TPAMI", "IJCV", "TIP", "TCSVT",
    "ACM MM", "WACV", "BMVC", "Nature", "Science",
}

# 全称 → 简称归一化（OpenReview/S2 常返回全称）
_ALIASES = {
    "ieee/cvf conference on computer vision and pattern recognition": "CVPR",
    "computer vision and pattern recognition": "CVPR",
    "ieee/cvf international conference on computer vision": "ICCV",
    "international conference on computer vision": "ICCV",
    "european conference on computer vision": "ECCV",
    "conference on neural information processing systems": "NeurIPS",
    "neural information processing systems": "NeurIPS",
    "international conference on learning representations": "ICLR",
    "international conference on machine learning": "ICML",
    "aaai conference on artificial intelligence": "AAAI",
    "association for the advancement of artificial intelligence": "AAAI",
    "ieee transactions on pattern analysis and machine intelligence": "TPAMI",
    "international journal of computer vision": "IJCV",
    "ieee transactions on image processing": "TIP",
    "transactions on image processing": "TIP",
    "ieee transactions on circuits and systems for video technology": "TCSVT",
    "acm international conference on multimedia": "ACM MM",
    "acm multimedia": "ACM MM",
    "ieee/cvf winter conference on applications of computer vision": "WACV",
    "british machine vision conference": "BMVC",
}

# 作者接收声明的高置信词
_ACCEPT_WORDS = re.compile(
    r"accept(?:ed|ance)?\s*(?:by|to|in|at)?|to appear|camera[- ]ready|published in|spotlight|oral",
    re.I,
)


def normalize_venue(text: str) -> tuple[str, str]:
    """从一段文字里提取 (白名单 venue + 年份, 原文匹配段)，如 ('CVPR 2026', 'CVPR 2026')。

    先试全称别名，再试简称 token；年份取 venue 邻近的 20XX（避免抓到别处年份）。
    """
    if not text:
        return "", ""
    low = text.lower()
    for full, short in _ALIASES.items():
        pos = low.find(full)
        if pos >= 0:
            window = text[max(0, pos - 8):pos + len(full) + 8]
            year_m = re.search(r"20\d{2}", window)
            return f"{short} {year_m.group(0)}" if year_m else short, full
    for token in sorted(TOP_VENUES, key=len, reverse=True):
        m = re.search(rf"\b{re.escape(token)}\s*(20\d{{2}})?", text, re.I)
        if m:
            year = m.group(1) or ""
            return (f"{token} {year}".strip(), m.group(0))
    return "", ""


def _from_author_declared(comment: str, journal_ref: str) -> dict | None:
    """渠道 1：作者自述。comment 带接收词 + venue，或 journal_ref 非空，即认定已发表。"""
    combined = f"{comment} {journal_ref}".strip()
    venue, _hit = normalize_venue(combined)
    if not venue:
        return None
    if journal_ref.strip() or _ACCEPT_WORDS.search(combined):
        year_m = re.search(r"20\d{2}", venue)
        return {"accepted": True, "venue": venue,
                "year": year_m.group(0) if year_m else "", "source": "arXiv 作者自述"}
    return None


def _title_similarity(a: str, b: str) -> float:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()  # noqa: E731
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def _parse_openreview(data: dict, title: str) -> dict | None:
    """OpenReview 命中判定：标题相似度 >= 0.9 的 note，venue 是白名单内的即算收录。"""
    for note in (data.get("notes") or [])[:8]:
        content = note.get("content") or {}
        n_title = content.get("title") or ""
        if isinstance(n_title, dict):  # api2 的 content 值可能是 {value: ...}
            n_title = n_title.get("value", "")
        if not n_title or _title_similarity(title, n_title) < 0.9:
            continue
        venue_raw = content.get("venue") or ""
        if isinstance(venue_raw, dict):
            venue_raw = venue_raw.get("value", "")
        venue, _ = normalize_venue(str(venue_raw))
        if venue:
            year_m = re.search(r"20\d{2}", venue)
            return {"accepted": True, "venue": venue,
                    "year": year_m.group(0) if year_m else "", "source": "OpenReview"}
    return None


def _parse_s2(data: dict) -> dict | None:
    """Semantic Scholar 命中判定：venue/publicationVenue 归一化后在白名单内。"""
    if not isinstance(data, dict) or data.get("title") is None:
        return None
    for raw in (data.get("publicationVenue", {}).get("name") if isinstance(
            data.get("publicationVenue"), dict) else None,
            data.get("venue") or ""):
        venue, _ = normalize_venue(str(raw or ""))
        if venue:
            year_m = re.search(r"20\d{2}", venue)
            return {"accepted": True, "venue": venue,
                    "year": year_m.group(0) if year_m else "", "source": "Semantic Scholar"}
    return None


async def resolve_publication(client: HttpClient, title: str, arxiv_id: str = "",
                              comment: str = "", journal_ref: str = "") -> dict:
    """查证一篇论文的发表状态。任何一步命中即返回；全渠道失败返回 checked=False。"""
    declared = _from_author_declared(comment, journal_ref)
    if declared:
        return declared

    reached = []  # 有响应的外部渠道（用于区分「查了没收录」和「没查成」）

    # OpenReview：标题检索
    if title:
        try:
            data = await client.json(
                "https://api2.openreview.net/notes/search",
                params={"term": title[:120], "limit": 8,
                        "content": "all", "group": "all", "source": "forum"},
            )
            reached.append("OpenReview")
            hit = _parse_openreview(data if isinstance(data, dict) else {}, title)
            if hit:
                return hit
        except Exception:
            pass

    # Semantic Scholar：arXiv ID 直查，匿名限流严重，重试两次
    if arxiv_id:
        for attempt in range(2):
            try:
                data = await client.json(
                    f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}",
                    params={"fields": "title,venue,year,publicationVenue"},
                )
                reached.append("Semantic Scholar")
                hit = _parse_s2(data if isinstance(data, dict) else {})
                if hit:
                    return hit
                break
            except Exception:
                if attempt == 0:
                    await asyncio.sleep(4.0)

    if reached:
        return {"accepted": False, "preprint_only": True,
                "source": "+".join(dict.fromkeys(reached))}
    return {"checked": False, "reason": "发表状态检索渠道全部失败（网络/限流）"}
