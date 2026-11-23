"""作者补全：清单页（Awesome CVPR / GitHub 榜单）只给标题和链接，authors 常年为空。

信息卡的「作者团队」是检索溯源的命脉：写不出人名时正确做法是**整行省略**，
但省略是最后手段——先按可信度补全。三条通道全免 key、按可靠性排序：

1. arXiv API（作者本人填写，最可信；id 直查优先，其次标题检索）；
2. OpenAlex（覆盖 CVPR/ICCV 的正式论文集，作者顺序与拼写照抄）；
3. Crossref（期刊与 IEEE/ACM 会议，给定名 + 姓，需拼装）。

共同的安全阀：命中结果的标题相似度必须 ≥ 0.88 才认，否则宁可不补——
补错作者比缺作者严重得多（张冠李戴事故的教训）。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

from .base import HttpClient

_ATOM = {"a": "http://www.w3.org/2005/Atom"}
_ARXIV_ID_IN_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", re.I)
_ARXIV_ID = re.compile(r"^[0-9]{4}\.[0-9]{4,5}$")
MIN_TITLE_MATCH = 0.88


def _norm_title(text: str) -> str:
    """标题归一：只留字母数字，用来算相似度（期刊前缀、标点差异不该影响判定）。"""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def title_match(a: str, b: str) -> float:
    """两个标题的相似度（0–1）。"""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def arxiv_id_of(item) -> str:
    """从 item 的 extra / url 里认出 arXiv id（清单页给的往往是 abs 链接）。"""
    extra = getattr(item, "extra", None) or {}
    raw = str(extra.get("arxiv_id") or "").strip()
    if _ARXIV_ID.match(raw):
        return raw
    m = _ARXIV_ID_IN_URL.search(str(getattr(item, "url", "") or ""))
    return m.group(1) if m else ""


def _person(name: str) -> str:
    """人名词序归一：Crossref 常给「姓, 名」，统一成「名 姓」。"""
    text = re.sub(r"\s+", " ", str(name or "").strip())
    m = re.match(r"^([^,]{2,40}),\s*(.{2,40})$", text)
    return f"{m.group(2).strip()} {m.group(1).strip()}" if m else text


async def _from_arxiv_id(client: HttpClient, arxiv_id: str) -> dict:
    xml_text = await client.text(
        "https://export.arxiv.org/api/query",
        params={"id_list": arxiv_id, "max_results": 1})
    root = ET.fromstring(xml_text)
    entry = root.find("a:entry", _ATOM)
    if entry is None:
        return {}
    title = (entry.findtext("a:title", "", _ATOM) or "").strip()
    authors = [(n.text or "").strip() for n in entry.findall("a:author/a:name", _ATOM)]
    authors = [_person(a) for a in authors if (a or "").strip()]
    return {"title": title, "authors": authors, "source": "arXiv"}


async def _search_arxiv(client: HttpClient, title: str) -> dict:
    phrase = re.sub(r'["\\]', " ", title).strip()
    xml_text = await client.text(
        "https://export.arxiv.org/api/query",
        params={"search_query": f'ti:"{phrase[:180]}"', "max_results": 5})
    root = ET.fromstring(xml_text)
    best, score = {}, 0.0
    for entry in root.findall("a:entry", _ATOM):
        hit_title = (entry.findtext("a:title", "", _ATOM) or "").strip()
        s = title_match(title, hit_title)
        if s > score:
            names = [(n.text or "").strip() for n in entry.findall("a:author/a:name", _ATOM)]
            names = [_person(n) for n in names if n]
            best, score = {"title": hit_title, "authors": names, "source": "arXiv"}, s
    return best if score >= MIN_TITLE_MATCH else {}


async def _from_openalex(client: HttpClient, title: str, doi: str = "") -> dict:
    params = {"per-page": 5}
    if doi:
        params["filter"] = f"doi:{doi}"
    else:
        params["search"] = title[:200]
    data = await client.json("https://api.openalex.org/works", params=params)
    best, score = {}, 0.0
    for work in (data or {}).get("results") or []:
        hit_title = str(work.get("display_name") or "")
        s = title_match(title, hit_title)
        if s <= score:
            continue
        names = [_person((a.get("author") or {}).get("display_name", ""))
                 for a in work.get("authorships") or []]
        names = [n for n in names if n]
        best, score = {"title": hit_title, "authors": names, "source": "OpenAlex"}, s
    return best if score >= MIN_TITLE_MATCH else {}


async def _from_crossref(client: HttpClient, title: str, doi: str = "") -> dict:
    params = {"rows": 5}
    if doi:
        params["filter"] = f"doi:{doi}"
    else:
        params["query.bibliographic"] = title[:200]
    data = await client.json("https://api.crossref.org/works", params=params)
    items = ((data or {}).get("message") or {}).get("items") or []
    best, score = {}, 0.0
    for item in items:
        hit_title = " ".join(item.get("title") or [])
        s = title_match(title, hit_title)
        if s <= score:
            continue
        names = []
        for author in item.get("author") or []:
            given, family = str(author.get("given") or ""), str(author.get("family") or "")
            name = f"{given} {family}".strip() or str(author.get("name") or "")
            if name:
                names.append(_person(name))
        best, score = {"title": hit_title, "authors": names, "source": "Crossref"}, s
    return best if score >= MIN_TITLE_MATCH else {}


async def resolve_authors(client: HttpClient, title: str, arxiv_id: str = "",
                          doi: str = "") -> dict:
    """按可信度补全作者；全渠道失败返回 {}（调用方据此省略作者行）。

    返回 ``{"authors": [...], "source": "arXiv|OpenAlex|Crossref", "title": 命中原题}``。
    """
    if arxiv_id:
        try:
            hit = await _from_arxiv_id(client, arxiv_id)
            if hit.get("authors"):
                return hit
        except Exception:
            pass
    for fetcher, kwargs in ((_search_arxiv, {}), (_from_openalex, {"doi": doi}),
                            (_from_crossref, {"doi": doi})):
        try:
            hit = await fetcher(client, title, **kwargs)
        except TypeError:
            hit = await fetcher(client, title)
        except Exception:
            continue
        if hit.get("authors"):
            return hit
    return {}
