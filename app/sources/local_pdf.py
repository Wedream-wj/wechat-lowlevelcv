"""本地 PDF 输入通道（学术论文模块「轨道 A」）。

用户直接给出一个本地 PDF 文件路径时，不走下载链：文件本身就在手边，读取后
交给 `paper_pdf.enrich_target(pdf_bytes=...)` 做高精解析（`pdf_tables()` 抽表、
`_caption_regions()` 抽架构图、`pdf_text()` 抽正文）。

这里只负责「把文件变成一条可编排的 Item」：校验文件、从元数据与首页取标题/
作者/期刊/DOI/摘要/关键词，再构造成与 `paper_search.build_item()` 同构的素材，
让后续「轨道 B 全网补全」可以按标题/DOI 检索后原地合并。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..models import Item
from .base import SourceError
from .paper_pdf import pdf_text
from .pdf_figs import _fitz

_PDF_MAGIC = b"%PDF-"
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+", re.I)
_CODE_RE = re.compile(r"github\.com\s*/\s*([A-Za-z0-9_.\-]+)\s*/\s*([A-Za-z0-9_.\-]+)", re.I)
_COPYRIGHT_YEAR_RE = re.compile(r"(?:©|\(c\)|copyright)\s*((?:19|20)\d{2})", re.I)
_DOI_YEAR_RE = re.compile(r"10\.\d{4,9}/[a-z]+\.((?:19|20)\d{2})\.", re.I)
_VENUE_LINE_RE = re.compile(r"^(?:IEEE|ACM|ELSEVIER|SPRINGER|WILEY|CVF|IET|MDPI)\b")
_NAME_RE = re.compile(r"^[A-Z][a-z]+(?: [A-Z]\.)? [A-Z][a-zA-Z\-]+$")
_ABSTRACT_RE = re.compile(
    r"abstract\s*[—\-–:]\s*(.+?)(?=\bindex terms\b|\bkeywords\b|\bI\.\s+Introduction\b)",
    re.S | re.I)
_KEYWORDS_RE = re.compile(
    r"(?:index terms|keywords)\s*[—\-–:]\s*(.+?)(?:\n\s*\n|\bI\.\s+[A-Z]|\bII\.\s+[A-Z]|$)",
    re.S | re.I)
# 机构/栏目行：作者行绝不长这样，用来把首页的「单位行」从作者候选里剔掉
_AFFIL_WORDS = ("university", "department", "school", "college", "institute", "laborator",
                "faculty", "academy", "centre", "center", "abstract", "index terms",
                "keywords", "received", "corresponding author", "digital object")
_ROLE_WORDS = ("member", "student", "fellow", "senior", "life", "ieee", "and", "associate")
_VENUE_STOPWORDS = {"on", "of", "and", "the", "for", "in", "to", "a", "an"}


@dataclass
class PdfMeta:
    """本地 PDF 首页 + 文档元数据里能可靠拿到的信息。"""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)
    venue: str = ""
    doi: str = ""
    year: str = ""
    code_url: str = ""
    pages: int = 0


def read_local_pdf(path: str) -> bytes:
    """读取用户指定的 PDF 文件；任何不合规都直接报中文原因，不静默降级。"""
    raw_path = Path(str(path).strip().strip('"').strip("'"))
    if not str(path or "").strip():
        raise SourceError("未提供本地 PDF 路径")
    if not raw_path.exists():
        raise SourceError(f"本地 PDF 不存在：{raw_path}")
    if not raw_path.is_file():
        raise SourceError(f"本地 PDF 路径不是文件：{raw_path}")
    if raw_path.suffix.lower() != ".pdf":
        raise SourceError(f"本地文件不是 .pdf：{raw_path.name}")
    try:
        data = raw_path.read_bytes()
    except OSError as exc:
        raise SourceError(f"本地 PDF 读取失败：{exc}") from exc
    if len(data) < 1024 or not data.startswith(_PDF_MAGIC):
        raise SourceError(f"本地文件不是有效的 PDF（缺少 %PDF- 文件头）：{raw_path.name}")
    return data


def _clean_line(text: str) -> str:
    return re.sub(r"[\s\u3000]+", " ", text or "").strip()


def _venue_case(line: str) -> str:
    """全大写刊名 → 常规大小写（IEEE TRANSACTIONS ON X → IEEE Transactions on X）。"""
    if not line.isupper():
        return line
    words = line.split()
    out = []
    for idx, word in enumerate(words):
        low = word.lower()
        if idx and low in _VENUE_STOPWORDS:
            out.append(low)
        elif word in {"IEEE", "ACM", "CVF", "IET"}:
            out.append(word)
        else:
            out.append(low.capitalize())
    return " ".join(out)


def _looks_like_title(line: str) -> bool:
    if len(line) < 15 or len(line) > 220:
        return False
    if _VENUE_LINE_RE.match(line) and line.isupper():
        return False
    if any(w in line.lower() for w in _AFFIL_WORDS):
        return False
    return True


def _parse_authors(metadata: dict, page_lines: list[str], title: str) -> list[str]:
    raw = _clean_line(metadata.get("author") or "")
    if raw:
        names = [n.strip() for n in re.split(r"[;,]", raw) if n.strip()]
        names = [n for n in names if _NAME_RE.match(n)]
        if names:
            return names[:12]
    for line in page_lines:
        low = line.lower()
        if title and _clean_line(title).lower() in low:
            continue
        if any(w in low for w in _AFFIL_WORDS):
            continue
        if line.count(",") < 1 or len(line) > 240:
            continue
        parts = [p.strip(" .,*†‡0123456789") for p in line.split(",")]
        names = []
        for part in parts:
            part = re.sub(r"^(?:and|&)\s+", "", part, flags=re.I).strip()
            if not part or any(w in part.lower() for w in _ROLE_WORDS):
                continue
            if part.isupper() and len(part) > 1:
                continue
            if _NAME_RE.match(part):
                names.append(part)
        if len(names) >= 2:
            return names[:12]
    return []


def _parse_keywords(text: str) -> list[str]:
    m = _KEYWORDS_RE.search(text or "")
    if not m:
        return []
    items = re.split(r"[,;]", _clean_line(m.group(1)))
    out: list[str] = []
    for item in items:
        item = item.strip(" .")
        if 1 < len(item) <= 40:
            out.append(item)
    return out[:8]


def front_matter(raw: bytes) -> PdfMeta:
    """解析文档元数据与首页：标题/作者/期刊/DOI/摘要/关键词/代码仓。"""
    fitz = _fitz()
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:  # 损坏/加密 PDF：报原因，不猜
        raise SourceError(f"本地 PDF 无法打开：{type(exc).__name__}: {str(exc)[:80]}") from exc
    try:
        meta = dict(doc.metadata or {})
        pages = int(doc.page_count or 0)
        first = doc.load_page(0).get_text("text") if pages else ""
    finally:
        doc.close()

    lines = [ln.strip() for ln in (first or "").splitlines() if ln.strip()]
    body = pdf_text(raw)  # 前 16 页正文：DOI/代码仓常出现在首页脚注或实验节
    head = "\n".join(lines[:80])

    title = _clean_line(meta.get("title") or "")
    if (not title or len(title) < 8 or "untitled" in title.lower()
            or "microsoft word" in title.lower()):
        title = next((ln for ln in lines[:12] if _looks_like_title(ln)), "")

    subject = _clean_line(meta.get("subject") or "")
    venue = ""
    for chunk in subject.split(";"):
        chunk = chunk.strip()
        if chunk and not re.match(r"^[\d.]+$", chunk) and not _DOI_RE.fullmatch(chunk):
            venue = chunk
            break
    if not venue:
        line = next((ln for ln in lines[:8] if _VENUE_LINE_RE.match(ln) and ln.isupper()), "")
        venue = _venue_case(line)

    doi = ""
    for blob in (subject, head, body[:4000]):
        m = _DOI_RE.search(blob or "")
        if m:
            doi = m.group(0).rstrip(".,;)")
            break

    year = ""
    m = _COPYRIGHT_YEAR_RE.search(head) or _COPYRIGHT_YEAR_RE.search(body)
    if m:
        year = m.group(1)
    else:
        m = _DOI_YEAR_RE.search(doi) or re.search(r"\b((?:19|20)\d{2})\b", subject)
        if m:
            year = m.group(1)

    abstract = ""
    m = _ABSTRACT_RE.search(head) or _ABSTRACT_RE.search(body[:6000])
    if m:
        abstract = _clean_line(m.group(1))[:1200]

    code = ""
    m = _CODE_RE.search(head) or _CODE_RE.search(body)
    if m:
        # 论文里的仓库链接常被排版断成两行（.../github.com/ 换行 owner/repo）
        code = f"https://github.com/{m.group(1)}/{m.group(2)}".rstrip(".,;)")

    return PdfMeta(
        title=title,
        authors=_parse_authors(meta, lines[:60], title),
        abstract=abstract,
        keywords=_parse_keywords(head) or _parse_keywords(body[:6000]),
        venue=venue,
        doi=doi,
        year=year,
        code_url=code,
        pages=pages,
    )


def build_local_item(meta: PdfMeta, path: str) -> Item:
    """本地 PDF → 流水线素材（与 paper_search.build_item 同构，直接进成稿链路）。"""
    raw_path = Path(str(path).strip().strip('"').strip("'"))
    title = meta.title or raw_path.stem.replace("_", " ")
    published = None
    if meta.year.isdigit() and 1990 <= int(meta.year) <= 2100:
        published = datetime(int(meta.year), 1, 1, tzinfo=timezone.utc)
    return Item(
        module="paper",
        title=title,
        url=f"https://doi.org/{meta.doi}" if meta.doi else "",
        source=f"本地 PDF（{raw_path.name}）",
        published_at=published,
        summary=meta.abstract,
        authors=meta.authors,
        venue=meta.venue,
        year=meta.year,
        code_url=meta.code_url,
        score=100.0,
        keywords=meta.keywords or ["本地 PDF"],
        extra={
            "targeted": True,
            "local_pdf": str(raw_path),
            "local_pages": meta.pages,
            "doi": meta.doi,
            "search_query": title,
        },
    )


def merge_online(item: Item, hit) -> Item:
    """轨道 B 结果合并进轨道 A 的 Item：本地解析出的字段为准，只补空缺。"""
    rec = getattr(hit, "record", None)
    if rec is None:
        return item
    item.venue = item.venue or rec.venue or rec.publisher
    item.year = item.year or rec.year
    if not item.authors:
        item.authors = rec.authors
    if not item.summary:
        item.summary = rec.abstract
    if not item.code_url:
        item.code_url = getattr(hit, "code_url", "") or ""
    if not item.url:
        item.url = rec.landing_url or (f"https://doi.org/{rec.doi}" if rec.doi else "")
    extra = item.extra
    extra["doi"] = extra.get("doi") or rec.doi
    extra["pmcid"] = rec.pmcid
    extra["pmid"] = rec.pmid
    extra["citations"] = rec.citations
    extra["is_oa"] = rec.is_oa
    extra["pdf_url"] = rec.pdf_url
    extra["search_traces"] = [t.describe() for t in getattr(hit, "traces", []) or []]
    extra["search_query"] = extra.get("search_query") or getattr(hit, "query", "")
    return item
