"""按论文名称全网检索：多源学术检索 → 标题匹配 → 记录合并 → 全文获取链路。

设计背景：用户在控制台 / CLI 输入一篇论文标题（如 PigMS R-CNN 猪只检测分割），
系统要在几十秒内定位这篇论文的元数据（作者 / 期刊 / DOI / 摘要）、全文（PDF 或
开放获取 XML）与开源代码，然后交给既有的论文精读流水线成稿。

源清单与实测结论（2026-11-03，本机网络）：
  ① OpenAlex        —— 免费无鉴权，元数据最全（作者/期刊/DOI/引用数/OA pdf 地址），主力；
  ② Crossref        —— DOI 注册机构官方，期刊/出版商字段最权威， Elsevier/IEEE/Springer 全覆盖；
  ③ Europe PMC      —— 生物医学 OA 全文（JATS XML），表格与图注干净，PMCID 直取全文；
  ④ Semantic Scholar —— 摘要质量高，openAccessPdf 常指向 pdfs.semanticscholar.org 镜像（可直下），
                        匿名限流（100 次/5 分钟），429 时静默降级；
  ⑤ PubMed          —— 经 Europe PMC 覆盖，不单独实现；
  ⑥ Google Scholar  —— 无公开 API，HTML 有反爬（本机实测代理 502），作为尽力而为的一路，
                        失败只记 trace，绝不阻塞；
  ⑦ ScienceDirect / IEEE Xplore —— 站点检索页反爬严格，走「DOI 落地页 → citation_pdf_url
                        meta 标签」的出版商通用协议（Wiley/Frontiers/Springer OA 有效），
                        检索页只在直连成功时解析；
  ⑧ Sci-Hub 镜像    —— 默认禁用（academic.paper_search.scihub_enabled: false）：
                        镜像分发的是未授权副本，批量抓取有版权合规风险。本项目只在
                        开放获取 / 合法授权范围内工作，因此默认只走 ①–⑦ 与出版商 OA；
                        确需启用由使用者自行承担合规责任并在配置里显式打开。
  ⑨ GitHub          —— 用方法名 / 标题关键词搜代码仓，命中严格（仓库名含方法名才算）。

标题匹配口径：归一化（去标点、压空白、小写）后，精确相等 → 1.0；否则
SequenceMatcher 与词级 Jaccard 各取一半。阈值 0.72——低于它宁可报「未找到」，
绝不把同领域相似标题的另一篇论文当成目标（张冠李戴比找不到更糟）。
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from urllib.parse import quote

from ..config import topics
from ..models import Item, normalize_title
from .base import HttpClient, SourceError, abs_url

# 匹配阈值：低于它视为「不是这篇论文」
MATCH_THRESHOLD = 0.72
# 同一篇论文在多源里标题写法略有差异（副标题缺失、破折号差异），合并阈值放宽
MERGE_THRESHOLD = 0.90

STOP_TOKENS = frozenset(
    "a an and based on for the with of using via in to from by automatic detection "
    "segmentation method methods approach framework network model deep learning".split()
)


@dataclass
class PaperRecord:
    """一个源返回的一条论文记录（字段残缺是常态，靠合并补齐）。"""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    venue: str = ""
    year: str = ""
    doi: str = ""
    abstract: str = ""
    pdf_url: str = ""
    landing_url: str = ""
    pmcid: str = ""
    pmid: str = ""
    citations: int = 0
    publisher: str = ""
    is_oa: bool = False
    source: str = ""
    score: float = 0.0        # 与查询标题的匹配分

    def merge(self, other: "PaperRecord") -> None:
        """把另一条记录的字段并进来：只在自身缺失时取值，绝不覆盖已有字段。"""
        self.authors = self.authors or other.authors
        self.venue = self.venue or other.venue
        self.year = self.year or other.year
        self.doi = self.doi or other.doi
        self.abstract = self.abstract or other.abstract
        self.pdf_url = self.pdf_url or other.pdf_url
        self.landing_url = self.landing_url or other.landing_url
        self.pmcid = self.pmcid or other.pmcid
        self.pmid = self.pmid or other.pmid
        self.publisher = self.publisher or other.publisher
        self.citations = max(self.citations, other.citations)
        self.is_oa = self.is_oa or other.is_oa


@dataclass
class SearchTrace:
    """每个检索源的尝试结果：成功/失败/被拦都要留痕，运营要看得到哪路挂了。"""

    source: str
    ok: bool = False
    count: int = 0
    note: str = ""
    seconds: float = 0.0

    def describe(self) -> str:
        mark = "✓" if self.ok else "×"
        detail = f"{self.count} 条" if self.ok else (self.note or "无结果")
        return f"{mark} {self.source}（{detail}，{self.seconds:.1f}s）"


@dataclass
class PaperHit:
    """检索结果：命中的论文 + 合并前的全部候选 + 各源 trace（进 errors/日志）。"""

    record: PaperRecord
    query: str
    traces: list[SearchTrace] = field(default_factory=list)
    candidates: list[PaperRecord] = field(default_factory=list)
    code_url: str = ""

    @property
    def pdf_candidates(self) -> list[str]:
        """全文获取链：OA 官方 PDF → 出版商 citation_pdf_url →（可选）Sci-Hub。"""
        urls: list[str] = []
        for u in (self.record.pdf_url,):
            if u and u not in urls:
                urls.append(u)
        return urls


# ---------------------------------------------------------------- 工具
def _s2_headers() -> dict:
    """Semantic Scholar 鉴权头。

    匿名调用走共享限流池（本机网络实测长期 429），在 .env 配 S2_API_KEY
    （https://www.semanticscholar.org/product/api 免费申请）即进入独立配额。
    """
    import os

    key = (os.getenv("S2_API_KEY", "").strip()
           or os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip())
    return {"x-API-key": key} if key else {}


def _clean(text: str, limit: int = 2000) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()[:limit]


def title_similarity(query: str, title: str) -> float:
    """归一化标题相似度：精确相等 1.0，否则 SequenceMatcher 与词 Jaccard 各半。"""
    a, b = normalize_title(query), normalize_title(title)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta = {t for t in re.findall(r"[a-z\d]+", a) if t not in STOP_TOKENS}
    tb = {t for t in re.findall(r"[a-z\d]+", b) if t not in STOP_TOKENS}
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return round(0.5 * ratio + 0.5 * jaccard, 4)


def _merge_records(records: list[PaperRecord]) -> PaperRecord:
    """按源可信度排序后逐字段合并：OpenAlex 元数据最全，Crossref 期刊名最权威。"""
    priority = {"openalex": 0, "crossref": 1, "europepmc": 2, "pubmed": 3,
                "semantic_scholar": 4, "google_scholar": 5, "sciencedirect": 6, "ieee": 7}
    ordered = sorted(records, key=lambda r: priority.get(r.source, 9))
    merged = ordered[0]
    for other in ordered[1:]:
        merged.merge(other)
    return merged


# ---------------------------------------------------------------- 各源适配器
async def _from_openalex(client: HttpClient, query: str, trace: SearchTrace) -> list[PaperRecord]:
    started = time.monotonic()
    url = "https://api.openalex.org/works"
    params = {"search": query, "per-page": 8,
              "mailto": "bot@lowlevelcv.example"}
    data = await client.json(url, params=params)
    out: list[PaperRecord] = []
    for w in data.get("results", []):
        loc = w.get("best_oa_location") or {}
        primary = w.get("primary_location") or {}
        src = (primary.get("source") or {}) or {}
        ids = w.get("ids") or {}
        inv = w.get("abstract_inverted_index") or {}
        abstract = ""
        if inv:
            pos: dict[int, str] = {}
            for word, idxs in inv.items():
                for i in idxs:
                    pos[i] = word
            abstract = " ".join(pos[i] for i in sorted(pos))[:2000]
        doi = str(w.get("doi") or "").replace("https://doi.org/", "")
        pmcid = re.search(r"(PMC\d+)", str(ids.get("pmcid") or ""), re.I)
        pmcid = pmcid.group(1).upper() if pmcid else ""
        rec = PaperRecord(
            title=_clean(w.get("title") or "", 300),
            authors=[a["author"]["display_name"] for a in w.get("authorships", [])
                     if (a.get("author") or {}).get("display_name")][:10],
            venue=_clean(src.get("display_name") or "", 120),
            year=str(w.get("publication_year") or ""),
            doi=doi,
            abstract=abstract,
            pdf_url=str(loc.get("pdf_url") or ""),
            landing_url=str(loc.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else "")),
            pmcid=pmcid,
            citations=int(w.get("cited_by_count") or 0),
            is_oa=bool(w.get("open_access", {}).get("is_oa")),
            source="openalex",
        )
        rec.score = title_similarity(query, rec.title)
        out.append(rec)
    trace.seconds = time.monotonic() - started
    trace.ok, trace.count = bool(out), len(out)
    return out


async def _from_crossref(client: HttpClient, query: str, trace: SearchTrace) -> list[PaperRecord]:
    started = time.monotonic()
    url = "https://api.crossref.org/works"
    params = {"query.bibliographic": query, "rows": 6,
              "select": "DOI,title,author,container-title,issued,abstract,link,type,publisher"}
    data = await client.json(url, params=params)
    out: list[PaperRecord] = []
    for w in data.get("message", {}).get("items", []):
        titles = w.get("title") or [""]
        issued = (w.get("issued") or {}).get("date-parts") or [[None]]
        links = [l.get("URL") for l in w.get("link", []) if l.get("URL")]
        pdf_url = next((u for u in links if "pdf" in u.lower() or u.lower().endswith(".pdf")), "")
        rec = PaperRecord(
            title=_clean(titles[0], 300),
            authors=[f"{a.get('given', '')} {a.get('family', '')}".strip()
                     for a in w.get("author", [])][:10],
            venue=_clean((w.get("container-title") or [""])[0], 120),
            year=str(issued[0][0] or "") if issued and issued[0] else "",
            doi=str(w.get("DOI") or ""),
            abstract=_clean(w.get("abstract") or ""),
            pdf_url=pdf_url,
            landing_url=f"https://doi.org/{w.get('DOI')}" if w.get("DOI") else "",
            publisher=_clean(w.get("publisher") or "", 80),
            source="crossref",
        )
        rec.score = title_similarity(query, rec.title)
        out.append(rec)
    trace.seconds = time.monotonic() - started
    trace.ok, trace.count = bool(out), len(out)
    return out


async def _from_semantic_scholar(client: HttpClient, query: str, trace: SearchTrace) -> list[PaperRecord]:
    started = time.monotonic()
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {"query": query, "limit": 6,
              "fields": "title,abstract,year,venue,externalIds,openAccessPdf,authors,citationCount"}
    data = await client.json(url, params=params, headers=_s2_headers())
    out: list[PaperRecord] = []
    for w in data.get("data", []):
        ext = w.get("externalIds") or {}
        oa = w.get("openAccessPdf") or {}
        rec = PaperRecord(
            title=_clean(w.get("title") or "", 300),
            authors=[a.get("name", "") for a in w.get("authors", [])][:10],
            venue=_clean(w.get("venue") or "", 120),
            year=str(w.get("year") or ""),
            doi=str(ext.get("DOI") or ""),
            abstract=_clean(w.get("abstract") or ""),
            pdf_url=str(oa.get("url") or ""),
            pmcid=str(ext.get("PMC") or ""),
            citations=int(w.get("citationCount") or 0),
            source="semantic_scholar",
        )
        rec.score = title_similarity(query, rec.title)
        out.append(rec)
    trace.seconds = time.monotonic() - started
    trace.ok, trace.count = bool(out), len(out)
    return out


async def _from_europepmc(client: HttpClient, query: str, trace: SearchTrace) -> list[PaperRecord]:
    started = time.monotonic()
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {"query": f'TITLE:"{query}"', "format": "json", "pageSize": 6,
              "resultType": "core"}
    data = await client.json(url, params=params)
    out: list[PaperRecord] = []
    for w in data.get("resultList", {}).get("result", []):
        if w.get("hitCount") == "0":
            continue
        rec = PaperRecord(
            title=_clean(w.get("title") or "", 300),
            authors=[a.strip() for a in (w.get("authorString") or "").split(",")][:10],
            venue=_clean(w.get("journalTitle") or "", 120),
            year=str(w.get("pubYear") or ""),
            doi=str(w.get("doi") or ""),
            abstract=_clean(w.get("abstractText") or ""),
            landing_url=f"https://doi.org/{w.get('doi')}" if w.get("doi") else "",
            pmcid=str(w.get("pmcid") or ""),
            pmid=str(w.get("pmid") or ""),
            is_oa=w.get("isOpenAccess") == "Y",
            source="europepmc",
        )
        rec.score = title_similarity(query, rec.title)
        out.append(rec)
    trace.seconds = time.monotonic() - started
    trace.ok, trace.count = bool(out), len(out)
    return out


_GS_TITLE_RE = re.compile(r'<h3 class="gs_rt"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_GS_META_RE = re.compile(r'<div class="gs_a"[^>]*>(.*?)</div>', re.S)


async def _from_google_scholar(client: HttpClient, query: str, trace: SearchTrace) -> list[PaperRecord]:
    """尽力而为的一路：无 API、反爬严（实测代理 502 / 403），失败只记 trace。"""
    started = time.monotonic()
    html = await client.text("https://scholar.google.com/scholar", params={"q": query, "hl": "en"})
    out: list[PaperRecord] = []
    titles = _GS_TITLE_RE.findall(html)
    metas = _GS_META_RE.findall(html)
    for i, (href, title) in enumerate(titles[:6]):
        meta = _clean(metas[i]) if i < len(metas) else ""
        year_m = re.search(r"(20\d{2}|\d{4})", meta)
        rec = PaperRecord(
            title=_clean(re.sub(r"<[^>]+>", "", title), 300),
            landing_url=abs_url("https://scholar.google.com/", href),
            year=year_m.group(1) if year_m else "",
            source="google_scholar",
        )
        rec.score = title_similarity(query, rec.title)
        out.append(rec)
    trace.seconds = time.monotonic() - started
    trace.ok, trace.count = bool(out), len(out)
    if not trace.ok:
        trace.note = "页面未解析出结果（可能被反爬拦截）"
    return out


async def _from_sciencedirect(client: HttpClient, query: str, trace: SearchTrace) -> list[PaperRecord]:
    """Elsevier 检索页：反爬严格（实测 403），能拿到就解析，拿不到留 trace。"""
    started = time.monotonic()
    html = await client.text("https://www.sciencedirect.com/search",
                             params={"qs": query}, headers={"Accept": "text/html"})
    titles = re.findall(r'<a[^>]+class="[^"]*result-list-title-link[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                        html, re.S)
    out: list[PaperRecord] = []
    for href, title in titles[:5]:
        rec = PaperRecord(
            title=_clean(title, 300),
            landing_url=abs_url("https://www.sciencedirect.com", href),
            publisher="Elsevier",
            source="sciencedirect",
        )
        rec.score = title_similarity(query, rec.title)
        out.append(rec)
    trace.seconds = time.monotonic() - started
    trace.ok, trace.count = bool(out), len(out)
    return out


async def _from_ieee(client: HttpClient, query: str, trace: SearchTrace) -> list[PaperRecord]:
    """IEEE Xplore：公开 REST 检索接口（无鉴权时常见 418/403），能通就解析。"""
    started = time.monotonic()
    payload = {"queryText": query, "highlight": True, "returnFacets": ["ALL"],
               "returnType": "SEARCH", "matchPubs": True, "rows": 5}
    data = await client.post_json("https://ieeexplore.ieee.org/rest/search", json=payload)
    out: list[PaperRecord] = []
    for w in data.get("records", [])[:5]:
        rec = PaperRecord(
            title=_clean(w.get("title") or w.get("html", {}).get("title", ""), 300),
            authors=[a.get("text", "") for a in w.get("authors", [])][:10],
            venue=_clean(w.get("publicationTitle") or "", 120),
            year=str(w.get("publicationYear") or ""),
            landing_url=str(w.get("articleNumber") and
                            f"https://ieeexplore.ieee.org/document/{w['articleNumber']}" or ""),
            publisher="IEEE",
            source="ieee",
        )
        rec.score = title_similarity(query, rec.title)
        out.append(rec)
    trace.seconds = time.monotonic() - started
    trace.ok, trace.count = bool(out), len(out)
    return out


async def _from_publisher_doi(client: HttpClient, doi: str, trace: SearchTrace) -> PaperRecord:
    """DOI 落地页 → citation_* meta 标签（出版商通用协议，Wiley/Springer/Frontiers OA 有效）。"""
    started = time.monotonic()
    try:
        html = await client.text(f"https://doi.org/{doi}", headers={"Accept": "text/html"})
    except SourceError as exc:
        trace.seconds = time.monotonic() - started
        trace.note = f"落地页不可达（{exc}）"
        return PaperRecord()
    meta = {k.lower(): v for k, v in re.findall(
        r'<meta[^>]+name=["\'](citation_[\w_]+)["\'][^>]+content=["\']([^"\']*)', html, re.I)
        + [(m[0], m[1]) for m in re.findall(
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\'](citation_[\w_]+)["\']', html, re.I)]}
    rec = PaperRecord(
        title=_clean(meta.get("citation_title", ""), 300),
        authors=[_clean(a) for a in re.split(r";", meta.get("citation_author", "")) if a.strip()][:10],
        venue=_clean(meta.get("citation_journal_title", ""), 120),
        pdf_url=meta.get("citation_pdf_url", ""),
        doi=doi,
        source="publisher",
    )
    trace.seconds = time.monotonic() - started
    trace.ok = bool(rec.pdf_url or rec.title)
    trace.count = 1 if trace.ok else 0
    if not trace.ok:
        trace.note = "落地页无 citation_pdf_url（可能被反爬墙拦下）"
    return rec


async def _from_github(client: HttpClient, query: str, trace: SearchTrace) -> str:
    """代码仓检索：从标题里提方法名（含 R-CNN / 大小写混合专名）作为查询词，命中要求严格。"""
    started = time.monotonic()
    from .competitions import github_headers

    headers = github_headers()
    tokens = re.findall(r"[A-Za-z][A-Za-z\d]{2,}(?:[-_][A-Za-z\d]+)*", query)
    # 方法名特征：混排大小写（PigMS）或带连字符专名（MS R-CNN → MSRCNN 不稳，取 PigMS 这类）
    candidates = [t for t in tokens if re.search(r"[a-z][A-Z]|[A-Z]{2,}", t)]
    candidates += [t for t in tokens if t.upper() in {"RCNN", "RESNET", "FPN", "YOLO"}]
    candidates = list(dict.fromkeys(candidates))[:3]
    if not candidates:
        trace.seconds = time.monotonic() - started
        trace.note = "标题里没有可检索的方法名"
        return ""
    for token in candidates:
        try:
            data = await client.json("https://api.github.com/search/repositories",
                                     params={"q": token, "sort": "stars", "per_page": 5},
                                     headers=headers)
        except Exception:
            continue
        for repo in data.get("items", [])[:3]:
            name = str(repo.get("full_name") or "").lower()
            desc = str(repo.get("description") or "")
            parts = set(re.split(r"[^a-z\d]+", name))
            # 整词命中才算：PigMS 不能靠「pigmsys」这种前缀巧合蒙混——
            # 那是别人的大数据系统，不是这篇论文的官方实现
            if (token.lower() in parts
                    or re.search(rf"\b{re.escape(token)}\b", desc)
                    or re.search(rf"\b{re.escape(token)}[A-Z]", desc)):
                trace.seconds = time.monotonic() - started
                trace.ok, trace.count = True, 1
                return str(repo.get("html_url") or "")
    trace.seconds = time.monotonic() - started
    trace.note = f"GitHub 未命中方法名 {candidates}"
    return ""


_SCIHUB_MIRRORS = ("https://www.sci-hub.ru", "https://sci-hub.se", "https://sci-hub.st")


def _scihub_enabled() -> bool:
    """Sci-Hub 是否显式启用：配置缺失 / 结构残缺一律按禁用处理（合规默认）。"""
    try:
        cfg = topics.raw["academic"]["paper_search"]
    except (KeyError, TypeError):
        return False
    return bool(cfg.get("scihub_enabled", False)) if isinstance(cfg, dict) else False


async def scihub_pdf_url(client: HttpClient, doi: str) -> str:
    """按 DOI 逐个试 Sci-Hub 镜像，返回页面里的 PDF 链接；没命中返回 ""。

    默认禁用（academic.paper_search.scihub_enabled: false）：此时与 DOI 为空
    一样直接返回，不发起任何网络请求。命中返回的链接已补全为绝对地址
    （协议相对 //... 补 https:，其余相对路径按镜像地址解析）。
    """
    if not _scihub_enabled():
        return ""
    doi = str(doi or "").strip()
    if not doi:
        return ""
    for mirror in _SCIHUB_MIRRORS:
        try:
            html = await client.text(f"{mirror}/{doi}")
        except Exception:
            continue
        m = (re.search(r'<(?:embed|iframe)[^>]+src="([^"]+\.pdf[^"]*)"', html, re.I)
             or re.search(r"location\.src\s*=\s*'([^']+\.pdf[^']*)'", html, re.I)
             or re.search(r"<button[^>]+onclick=\"location\.href='([^']+\.pdf[^']*)'", html, re.I))
        if m:
            url = m.group(1)
            if url.startswith("//"):
                url = "https:" + url
            elif not url.startswith("http"):
                url = abs_url(mirror, url)
            return url
    return ""


async def _from_sci_hub(client: HttpClient, doi: str, trace: SearchTrace) -> str:
    """Sci-Hub 镜像：**默认禁用**。镜像分发的是未授权副本，本项目默认只在
    开放获取 / 合法授权范围内工作；启用需在配置里显式打开并由使用者自担合规责任。

    解析逻辑全部委托给 scihub_pdf_url，这里只负责 trace 记账。"""
    started = time.monotonic()
    if not _scihub_enabled():
        trace.seconds = time.monotonic() - started
        trace.note = "已禁用（合规默认：仅开放获取/授权渠道；需显式打开 scihub_enabled）"
        return ""
    if not doi:
        trace.seconds = time.monotonic() - started
        trace.note = "没有 DOI，无法查询"
        return ""
    url = await scihub_pdf_url(client, doi)
    trace.seconds = time.monotonic() - started
    if url:
        trace.ok, trace.count = True, 1
    else:
        trace.note = "镜像未返回 PDF"
    return url


# ---------------------------------------------------------------- 汇总
def _rank_candidates(query: str, records: list[PaperRecord]) -> list[PaperRecord]:
    for rec in records:
        rec.score = max(rec.score, title_similarity(query, rec.title))
    return sorted(records, key=lambda r: r.score, reverse=True)


async def search_paper(client: HttpClient, query: str, *,
                       publisher_pdf: bool = True,
                       github: bool = True,
                       timeout: float | None = None) -> PaperHit:
    """并发跑全部检索源，返回标题最匹配的一篇（含各源 trace）。

    找不到匹配分 >= MATCH_THRESHOLD 的候选时抛 SourceError——宁报「未找到」，
    绝不把相似标题的另一篇论文当目标。
    """
    query = re.sub(r"\s+", " ", str(query or "")).strip().strip("\"'“”")
    if len(query) < 8:
        raise SourceError(f"论文名太短（至少 8 个字符）：{query!r}")
    cfg = topics.raw.get("academic", {}).get("paper_search", {}) or {}
    threshold = float(cfg.get("match_threshold", MATCH_THRESHOLD))
    timeout = float(cfg.get("timeout_seconds", 60.0))

    traces: list[SearchTrace] = []

    async def _guarded(name: str, fn, *args):
        trace = SearchTrace(source=name)
        traces.append(trace)
        try:
            return await asyncio.wait_for(fn(client, query, trace) if len(args) == 0
                                          else fn(client, *args, trace), timeout=timeout)
        except asyncio.TimeoutError:
            trace.note = f"超时（>{timeout:.0f}s）"
            return []
        except Exception as exc:
            trace.note = f"{type(exc).__name__}: {str(exc)[:80]}"
            return []

    results = await asyncio.gather(
        _guarded("openalex", _from_openalex),
        _guarded("crossref", _from_crossref),
        _guarded("europepmc", _from_europepmc),
        _guarded("semantic_scholar", _from_semantic_scholar),
        _guarded("google_scholar", _from_google_scholar),
        _guarded("sciencedirect", _from_sciencedirect),
        _guarded("ieee", _from_ieee),
        return_exceptions=True,
    )
    records: list[PaperRecord] = []
    for group in results:
        if isinstance(group, list):
            records.extend(group)

    ranked = _rank_candidates(query, records)
    best = ranked[0] if ranked else None
    if best is None or best.score < threshold:
        summary = "；".join(t.describe() for t in traces)
        raise SourceError(
            f"各学术源都未找到与「{query}」匹配的论文（最高相似度 "
            f"{best.score if best else 0:.2f} < {threshold}）。源情况：{summary}"
        )

    # 同一篇论文的多源记录合并（DOI 或标题相似度 >= MERGE_THRESHOLD）
    group = [r for r in ranked if r.score >= MERGE_THRESHOLD
             and (r.doi and best.doi and r.doi.lower() == best.doi.lower()
                  or title_similarity(query, r.title) >= MERGE_THRESHOLD)]
    merged = _merge_records(group or [best])

    hit = PaperHit(record=merged, query=query, traces=traces,
                   candidates=ranked[:6])

    # 全文补充：出版商 citation_pdf_url → Sci-Hub（默认关）→ GitHub 代码仓
    if publisher_pdf and merged.doi:
        pub_trace = SearchTrace(source="publisher_doi")
        traces.append(pub_trace)
        try:
            rec = await asyncio.wait_for(_from_publisher_doi(client, merged.doi, pub_trace),
                                         timeout=timeout)
        except Exception as exc:
            pub_trace.note = f"{type(exc).__name__}: {str(exc)[:80]}"
            rec = PaperRecord()
        if rec.pdf_url and not merged.pdf_url:
            merged.pdf_url = rec.pdf_url
        if rec.venue and not merged.venue:
            merged.venue = rec.venue

    if github:
        gh_trace = SearchTrace(source="github")
        traces.append(gh_trace)
        try:
            hit.code_url = await asyncio.wait_for(_from_github(client, query, gh_trace),
                                                  timeout=timeout)
        except Exception as exc:
            gh_trace.note = f"{type(exc).__name__}: {str(exc)[:80]}"

    scihub_trace = SearchTrace(source="sci_hub")
    traces.append(scihub_trace)
    if not merged.pdf_url:
        try:
            url = await asyncio.wait_for(_from_sci_hub(client, merged.doi, scihub_trace),
                                         timeout=timeout)
            if url:
                merged.pdf_url = url
        except Exception as exc:
            scihub_trace.note = f"{type(exc).__name__}: {str(exc)[:80]}"
    else:
        scihub_trace.note = "已有官方 OA 全文，跳过"
    return hit


def build_item(hit: PaperHit) -> Item:
    """命中论文 → 流水线素材 Item（extra 里带上全文获取所需的全部线索）。"""
    rec = hit.record
    landing = rec.landing_url or (f"https://doi.org/{rec.doi}" if rec.doi else "")
    year = rec.year or ""
    published = None
    if year.isdigit() and 1990 <= int(year) <= 2100:
        from datetime import datetime, timezone

        published = datetime(int(year), 1, 1, tzinfo=timezone.utc)
    return Item(
        module="paper",
        title=rec.title,
        url=landing or "https://openalex.org",
        source=f"指定检索（{rec.venue or rec.publisher or rec.source}）",
        published_at=published,
        summary=rec.abstract,
        authors=rec.authors,
        venue=rec.venue,
        year=year,
        code_url=hit.code_url,
        score=100.0,
        keywords=["指定论文"],
        extra={
            "targeted": True,
            "doi": rec.doi,
            "pmcid": rec.pmcid,
            "pmid": rec.pmid,
            "citations": rec.citations,
            "is_oa": rec.is_oa,
            "pdf_url": rec.pdf_url,
            "search_traces": [t.describe() for t in hit.traces],
            "search_query": hit.query,
        },
    )
