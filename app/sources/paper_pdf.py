"""指定论文的全文素材提取：PDF 与开放获取 XML 双通道。

分工（同一篇论文两条路并行取，按可用性合流）：
  · JATS XML（Europe PMC，生物医学 OA 期刊如 MDPI Sensors）——表格是干净的
    <table> 结构，图注/摘要/正文全文结构化，**数字逐字可信**，优先采用；
  · PDF（OA 官方地址 / Semantic Scholar 镜像 / 出版商 citation_pdf_url）——
    表格提取不可靠（实测该论文 find_tables 把「84.09」竖排拆成「8409\n.」，
    数字直接失真，喂给模型就是编造），所以 PDF 只干一件事：
    **按图注区域整块渲染插图**——架构图/流程图常由几十个小图块拼成，
    单抠 xref 只能拿到半张脸；这里取图注上方全部图元的 bbox 并集，
    以 2.5x+ 缩放把整块区域渲染成 PNG，矢量文字一并保真。

表格完整性的铁律：喂给模型的每一张表都必须过「单元格合理性」校验——
数字单元格必须形如 0.9374 / 94.68% / 352，含换行残片的单元格直接判废，
宁可整表丢弃（正文改用数值句），也绝不把解析残渣当实验数据。
"""
from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from ..config import settings
from .base import HttpClient
from .paper_search import _s2_headers
from .pdf_figs import _fitz

# 图注正则：与 pdf_figs 保持同款容错（Figure 2. / Fig. 3: / Figure 12 |）
_CAPTION_RE = re.compile(r"^\s*(?:figure|fig\.?)\s*(\d{1,2})\s*[:：|.\-–—]\s*(.*)", re.I | re.S)
# 架构判定词：流程图（flowchart）是应用型论文最典型的「方法总览图」
_ARCH_WORDS = ("overview", "architecture", "architectur", "pipeline", "framework",
               "flowchart", "flow chart", "flow diagram", "structure of", "schematic",
               "block diagram", "workflow", "process of the", "establishment process")
_RESULT_WORDS = ("qualitative", "comparison", "compared", "visual result", "results on",
                 "detection and instance segmentation", "segmentation of the", "output of",
                 "example of", "failure", "precision", "recall curve")
_MAX_FIGURE_PAGES = 20
# 数字单元格：允许 0.9374 / 94.68% / -12.3 / 352 / 1e-4
_NUM_CELL_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:e-?\d+)?%?$", re.I)
# 文本单元格：方法名/指标名，短且无控制字符
_TEXT_CELL_RE = re.compile(r"^[A-Za-z][A-Za-z\d .+\-/()×x%≤≥~]{0,32}$")


# ---------------------------------------------------------------- 图角色
def _figure_role(caption: str, number: int) -> str:
    low = (caption or "").lower()
    if any(w in low for w in _ARCH_WORDS):
        return "arch"
    if any(w in low for w in _RESULT_WORDS):
        return "result"
    if number == 1:
        return "teaser"
    return "other"


# ---------------------------------------------------------------- PDF：按图注区域渲染
@dataclass
class RegionFigure:
    """按「图注上方整块区域」渲染出的插图（矢量文字已栅格化进图里）。"""

    number: int
    caption: str
    role: str
    png: bytes
    page: int
    width: int = 0
    height: int = 0


def _caption_regions(pdf_bytes: bytes, max_pages: int = _MAX_FIGURE_PAGES,
                     limit: int = 6) -> list[RegionFigure]:
    if not pdf_bytes:
        return []
    fitz = _fitz()
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return []
    out: list[RegionFigure] = []
    seen: set[int] = set()
    try:
        for pno in range(min(len(doc), max_pages)):
            page = doc.load_page(pno)
            for block in page.get_text("blocks"):
                if len(block) < 7 or block[6] != 0:
                    continue
                text = re.sub(r"\s+", " ", str(block[4] or "")).strip()
                m = _CAPTION_RE.match(text)
                if not m:
                    continue
                number = int(m.group(1))
                if number in seen:
                    continue
                cap_rect = fitz.Rect(block[0], block[1], block[2], block[3])
                region = _region_above(page, cap_rect)
                if region is None:
                    continue
                zoom = max(2.5, 1100.0 / max(region.width, 1.0))
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=region)
                except Exception:
                    continue
                if pix.width < 300 or pix.height < 120:
                    continue
                # 纯白图（图注误匹配到正文段落）直接丢弃
                samples = pix.samples
                if samples and len(samples) > 1024:
                    step = max(1, len(samples) // 1024)
                    bright = sum(1 for i in range(0, len(samples), step)
                                 if samples[i] > 245)
                    if bright / (len(samples) // step + 1) > 0.995:
                        continue
                seen.add(number)
                out.append(RegionFigure(
                    number=number, caption=text[:220],
                    role=_figure_role(text, number),
                    png=pix.tobytes("png"), page=pno,
                    width=pix.width, height=pix.height))
                if len(out) >= limit * 2:
                    return _dedupe(out, limit)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return _dedupe(out, limit)


def _dedupe(figs: list[RegionFigure], limit: int) -> list[RegionFigure]:
    """同图号取首张，按角色排序：arch > result > teaser > other。

    同角色内的页序方向不同：架构图集中在方法节（越早越是总览图，Figure 2 的
    Flowchart 优于 Figure 4 的局部结构），定性效果图集中在实验节（越晚越是
    整体结果，Figure 10 的分割示例优于 Figure 6 的分支输出）。
    """
    by_num: dict[int, RegionFigure] = {}
    for fig in figs:
        by_num.setdefault(fig.number, fig)
    order = {"arch": 0, "result": 1, "teaser": 2, "other": 3}
    ranked = sorted(by_num.values(),
                    key=lambda f: (order.get(f.role, 9),
                                   f.page if f.role == "arch" else -f.page))
    return ranked[:limit]


def _region_above(page, caption_rect):
    """图注上方的图元 bbox 并集；没有足够大的图元时返回 None。

    图元 = 嵌入位图 + 矢量绘图（流程图的框、箭头与连线常是纯矢量，
    get_image_info 看不见，必须补 get_drawings 这一路）。
    """
    fitz = _fitz()
    rects: list = []
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        infos = []
    for info in infos:
        rect = fitz.Rect(info["bbox"])
        if rect.width < 40 or rect.height < 24:
            continue
        gap = caption_rect.y0 - rect.y1
        if gap < -8 or gap > 160:
            continue
        if rect.x1 < caption_rect.x0 - 40 or rect.x0 > caption_rect.x1 + 40:
            continue
        rects.append(rect)
    if not rects:
        # 纯矢量图：取图注上方 160pt 内、横向有交集的绘图并集
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
        for d in drawings:
            rect = fitz.Rect(d.get("rect") or d.get("bbox") or (0, 0, 0, 0))
            if rect.is_empty or rect.width < 12 or rect.height < 8:
                continue
            gap = caption_rect.y0 - rect.y1
            if gap < -8 or gap > 160:
                continue
            if rect.x1 < caption_rect.x0 - 40 or rect.x0 > caption_rect.x1 + 40:
                continue
            rects.append(rect)
    if not rects:
        return None
    region = rects[0]
    for rect in rects[1:]:
        region |= rect
    region.y1 = max(region.y0 + 40, caption_rect.y0 - 2)
    # 收边：不要把页眉页脚和相邻栏带进来
    region.x0 = max(region.x0, 24)
    region.x1 = min(region.x1, page.rect.width - 24)
    region.y0 = max(region.y0, 30)
    if region.width < 120 or region.height < 60:
        return None
    return region


# ---------------------------------------------------------------- JATS XML（Europe PMC）
def _strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment or "")).strip()


def _cell_text(el) -> str:
    return _strip_tags(ET.tostring(el, encoding="unicode") if len(el) else (el.text or ""))


def jats_assets(xml_text: str) -> dict:
    """Europe PMC 全文 XML → 摘要 / 正文全文 / 干净表格 / 数值结论句 / 图注表。

    返回 {"abstract": str, "full_text": str, "tables": [...],
          "snippets": [...], "fig_captions": {n: caption}}；解析失败返回空 dict。
    """
    if not xml_text:
        return {}
    out: dict = {"abstract": "", "full_text": "", "tables": [],
                 "snippets": [], "fig_captions": {}}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}

    def _txt(el) -> str:
        return _strip_tags(ET.tostring(el, encoding="unicode")) if el is not None else ""

    abstract = root.find(".//abstract")
    out["abstract"] = _txt(abstract)

    # 表格：<table-wrap> → label + caption + thead/tbody（rowspan/colspan 展开成平铺网格）
    for wrap in root.iter("table-wrap"):
        label = _txt(wrap.find("label"))
        caption = _txt(wrap.find("caption"))
        table = wrap.find(".//table")
        if table is None:
            continue
        grid: list[list[str]] = []
        pending: list[tuple[int, int, str]] = []   # (row, col, text) 由 rowspan/colspan 落位
        for r, tr in enumerate(table.findall(".//tr")):
            row: list[str] = []
            c = 0
            for cell in list(tr):
                tag = cell.tag.lower()
                if tag not in ("td", "th"):
                    continue
                while any(pc[0] == r and pc[1] == c for pc in pending):
                    row.append(next(pc[2] for pc in pending if pc[0] == r and pc[1] == c))
                    c += 1
                try:
                    rowspan = max(1, int(cell.get("rowspan", "1")))
                    colspan = max(1, int(cell.get("colspan", "1")))
                except ValueError:
                    rowspan = colspan = 1
                text = _txt(cell)
                for rr in range(rowspan):
                    for cc in range(colspan):
                        if rr == 0:
                            row.append(text)
                        else:
                            pending.append((r + rr, c + cc, text))
                c += colspan
            while any(pc[0] == r and pc[1] == c for pc in pending):
                row.append(next(pc[2] for pc in pending if pc[0] == r and pc[1] == c))
                c += 1
            if any(v.strip() for v in row):
                grid.append(row)
        if not grid or len(grid) < 2:
            continue
        width = max(len(r) for r in grid)
        grid = [r + [""] * (width - len(r)) for r in grid]
        # 两行表头（「IoU 阈值 0/0.3/0.5/0.8 × R/P/F1」的 colspan 分组表）：
        # 第二行是子表头、首行是分组——合并成 R@0 / F1@0.5 这种单行列名，
        # 否则模型会把分组行当成数据行，表就直接失真了
        if _is_two_row_header(grid):
            header = [grid[0][j] if grid[0][j] == grid[1][j] or not grid[0][j]
                      else (f"{grid[1][j]}@{grid[0][j]}" if _NUM_CELL_RE.match(grid[0][j])
                            else f"{grid[1][j]}({grid[0][j]})")
                      for j in range(width)]
            out["tables"].append({"caption": f"{label} {caption}".strip(),
                                  "header": header, "rows": grid[2:]})
            continue
        out["tables"].append({"caption": f"{label} {caption}".strip(),
                              "header": grid[0], "rows": grid[1:]})

    # 图注：<fig> → label "Figure N" + caption
    for fig in root.iter("fig"):
        label = _txt(fig.find("label"))
        caption = _txt(fig.find("caption"))
        m = re.search(r"(?:figure|fig\.?)\s*(\d{1,2})", label or caption or "", re.I)
        if m and caption:
            out["fig_captions"][int(m.group(1))] = caption[:220]

    # 正文全文（含段落文字；max 32000 字，够模型引用）
    parts: list[str] = []
    body = root.find(".//body")
    for p in body.iter("p") if body is not None else []:
        text = _txt(p)
        if text and len(text) > 40:
            parts.append(text)
    out["full_text"] = "\n".join(parts)[:32000]

    # 数值结论句：正文 + 摘要里带数字与比较词的句子（给「03 实验解读」供数）
    pool = [out["abstract"], out["full_text"]]
    sentences: list[str] = []
    for chunk in pool:
        for sent in re.split(r"(?<=[.!?])\s+", chunk.replace("\n", " ")):
            if re.search(r"\d", sent) and re.search(
                    r"compared|improv|achiev|outperform|higher|lower|increase|obtain"
                    r"|precision|recall|F1|mAP|accuracy|score", sent, re.I):
                clean = sent.strip()
                key = clean.lower()
                if clean not in sentences and key not in {s.lower() for s in sentences}:
                    sentences.append(clean[:300])
            if len(sentences) >= 12:
                break
        if len(sentences) >= 12:
            break
    out["snippets"] = sentences
    return out


# ---------------------------------------------------------------- PDF 文本与表格（严格校验）
def _is_two_row_header(grid: list[list[str]]) -> bool:
    """第二行也是表头的特征：几乎全是文本、首格与首行重复、首行有分组重复值。"""
    if len(grid) < 3:
        return False
    second = [c.strip() for c in grid[1]]
    if not any(second):
        return False
    text_cells = sum(1 for c in second[1:] if c and not _NUM_CELL_RE.match(c))
    if text_cells < max(2, len(second) - 2):
        return False
    first = [c.strip() for c in grid[0]]
    if second[0] and second[0] == first[0]:
        return True
    nonempty = [c for c in first if c]
    return len(nonempty) > len(set(nonempty))   # 首行有重复分组（0/0/0/0.3/0.3/0.3…）


def pdf_text(pdf_bytes: bytes, max_chars: int = 24000) -> str:
    if not pdf_bytes:
        return ""
    try:
        doc = _fitz().open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return ""
    parts: list[str] = []
    try:
        for pno in range(min(len(doc), 16)):
            parts.append(doc.load_page(pno).get_text())
    finally:
        try:
            doc.close()
        except Exception:
            pass
    text = "\n".join(parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text[:max_chars]


def _plausible_grid(grid: list[list[str]]) -> bool:
    """表格完整性校验：数字单元必须像数字，文本单元必须像短标签。

    PDF 竖排/断行会把「84.09」拆成「8409\n.」这类残片——任何一格残片
    都意味着整表数字不可信，判废。
    """
    if not grid or len(grid) < 2:
        return False
    width = max(len(r) for r in grid)
    if width < 2:
        return False
    numeric = text_ok = bad = 0
    for row in grid[1:]:
        for cell in row:
            cell = re.sub(r"\s+", " ", str(cell or "")).strip().strip(".,;")
            if not cell:
                continue
            if _NUM_CELL_RE.match(cell):
                numeric += 1
            elif _TEXT_CELL_RE.match(cell):
                text_ok += 1
            else:
                bad += 1
    total = numeric + text_ok + bad
    return total >= 4 and bad <= max(1, int(total * 0.12)) and numeric >= max(2, int(total * 0.25))


def pdf_tables(pdf_bytes: bytes, limit: int = 3) -> list[dict]:
    """PDF find_tables 提取 + 严格校验；不达标的表直接丢弃（宁缺毋假）。"""
    if not pdf_bytes:
        return []
    try:
        doc = _fitz().open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return []
    out: list[dict] = []
    try:
        for pno in range(len(doc)):
            page = doc.load_page(pno)
            try:
                tabs = page.find_tables()
            except Exception:
                continue
            # 图注/表注定位：把最近的 "Table N." 说明挂到表上
            captions = []
            for block in page.get_text("blocks"):
                if len(block) >= 7 and block[6] == 0:
                    text = re.sub(r"\s+", " ", str(block[4] or "")).strip()
                    if re.match(r"^\s*table\s*\d{1,2}", text, re.I):
                        captions.append((abs(block[1]), text[:200]))
            for tab in tabs.tables:
                raw = tab.extract()
                if not raw:
                    continue
                grid = [[re.sub(r"\s+", " ", str(c or "")).strip() for c in row]
                        for row in raw]
                if not _plausible_grid(grid):
                    continue
                caption = min(captions, key=lambda x: abs(x[0] - tab.bbox[1]))[1] if captions else ""
                out.append({"caption": caption, "header": grid[0], "rows": grid[1:]})
                if len(out) >= limit:
                    return out
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- 下载与合流
async def semantic_scholar_pdf(client: HttpClient, doi: str, *,
                               attempts: int | None = None,
                               delay: float | None = None) -> str:
    """Semantic Scholar 按 DOI 取 OA PDF 镜像地址（pdfs.semanticscholar.org 可直下）。

    匿名限流是共享池（本机网络实测长期 429），间隔重试 4 次；配置 S2_API_KEY
    后走独立配额，基本一次即中。仍失败返回空串，由调用方降级
    （JATS 全文照常、封面走品牌底图）。
    """
    if not doi:
        return ""
    attempts, delay = 4, 5.0
    url = (f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
           "?fields=openAccessPdf")
    for i in range(attempts):
        try:
            data = await client.json(url, headers=_s2_headers())
            pdf = str(((data.get("openAccessPdf") or {}).get("url")) or "")
            if pdf:
                return pdf
            return ""
        except Exception:
            if i < attempts - 1:
                await asyncio.sleep(delay)
    return ""


async def fetch_pdf(client: HttpClient, urls: list[str], *,
                    timeout: float = 90.0) -> bytes:
    """按顺序尝试 PDF 地址，返回字节数据；全失败返回空。"""
    import httpx

    for url in urls:
        if not url:
            continue
        try:
            async with client._sem:
                resp = await asyncio.wait_for(
                    client._client.get(url, headers={"Accept": "application/pdf,*/*"},
                                       follow_redirects=True, timeout=45.0),
                    timeout=timeout)
            if resp.status_code >= 400:
                continue
            ctype = resp.headers.get("content-type", "").lower()
            if "pdf" not in ctype and not resp.content[:5].startswith(b"%PDF-"):
                continue
            return resp.content
        except Exception:
            continue
    return b""


async def fetch_jats(client: HttpClient, pmcid: str, *, timeout: float = 45.0) -> str:
    if not pmcid:
        return ""
    pmcid = pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
    try:
        async with client._sem:
            resp = await asyncio.wait_for(
                client._client.get(url, headers={"Accept": "application/xml,*/*"},
                                   timeout=timeout), timeout=timeout + 5)
        if resp.status_code != 200:
            return ""
        return resp.text
    except Exception:
        return ""


async def enrich_target(client: HttpClient, item: Item, *, log=print) -> dict:
    """给指定检索命中的论文补齐成稿素材：全文 / 表格 / 数值句 / 架构与结果图。

    素材写入 item.extra 与 item.images（架构图在前，与流水线 paper 模块的
    图注对齐约定一致：extra["figure_captions"] 与 images 等长）。
    返回摘要 dict 供日志。
    """
    extra = item.extra
    pdf_urls = [u for u in (extra.get("pdf_url"),) if u]
    doi = str(extra.get("doi") or "")
    pdf_bytes, jats = await asyncio.gather(
        fetch_pdf(client, pdf_urls),
        fetch_jats(client, str(extra.get("pmcid") or "")),
    )
    if not pdf_bytes and doi:
        # 出版商直链被反爬墙拦下（MDPI/Elsevier 常见）：换 Semantic Scholar 的 OA 镜像
        mirror = await semantic_scholar_pdf(client, doi)
        if mirror and mirror not in pdf_urls:
            pdf_urls.append(mirror)
            pdf_bytes = await fetch_pdf(client, [mirror])
    summary = {"pdf": len(pdf_bytes), "jats": bool(jats), "figures": 0,
               "tables": 0, "snippets": 0}

    assets: dict = {"tables": [], "snippets": [], "full_text": "", "fig_captions": {}}
    if jats:
        parsed = jats_assets(jats)
        assets["tables"] = parsed.get("tables") or []
        assets["snippets"] = parsed.get("snippets") or []
        assets["full_text"] = parsed.get("full_text") or ""
        assets["fig_captions"] = parsed.get("fig_captions") or {}
    if not assets["full_text"] and pdf_bytes:
        text = pdf_text(pdf_bytes)
        assets["full_text"] = text
        # PDF 兜底数值句（只在 JATS 缺席时用——同源口径要一致）
        sentences: list[str] = []
        for sent in re.split(r"(?<=[.!?])\s+", text.replace("\n", " ")):
            if re.search(r"\d", sent) and re.search(
                    r"F1|precision|recall|mAP|accuracy|compared|outperform|achiev", sent, re.I):
                if sent.strip() not in sentences:
                    sentences.append(sent.strip()[:300])
            if len(sentences) >= 10:
                break
        assets["snippets"] = assets["snippets"] or sentences
    if not assets["tables"] and pdf_bytes:
        assets["tables"] = pdf_tables(pdf_bytes)

    # 插图：优先 PDF 图注区域渲染；caption 优先用 JATS 的干净版本
    figures: list[tuple[str, str, str]] = []   # (path, caption, role)
    if pdf_bytes:
        regions = _caption_regions(pdf_bytes)
        slug = re.sub(r"[^A-Za-z\d]+", "-", item.title)[:40].strip("-").lower() or "paper"
        for fig in regions[:4]:
            path = settings.image_dir / f"paper_{slug}_fig{fig.number:02d}.png"
            path.write_bytes(fig.png)
            caption = assets["fig_captions"].get(fig.number) or fig.caption
            figures.append((str(path), caption, fig.role))
        summary["figures"] = len(figures)
    images: list[str] = []
    caps: list[str] = []
    for role in ("arch", "result", "teaser"):
        for path, caption, fig_role in figures:
            if fig_role == role and path not in images:
                images.append(path)
                caps.append(caption)
                break
    if images:
        item.images = images
        item.extra["figure_captions"] = caps
        item.extra["figure_caption"] = caps[0]
        if len(caps) > 1:
            item.extra["figure_caption_2"] = caps[1]
        item.image_url = images[0]
    if assets["tables"]:
        item.extra["tables"] = assets["tables"][:3]
        summary["tables"] = len(assets["tables"][:3])
    if assets["snippets"]:
        item.extra["result_snippets"] = assets["snippets"][:10]
        summary["snippets"] = len(assets["snippets"][:10])
    if assets["full_text"]:
        item.extra["full_text"] = assets["full_text"]
    # 发表状态：Crossref/EuropePMC 的期刊字段就是权威结论，直接写进 publication，
    # 让标题走「venue 前缀」信誉规则（不再让 OpenReview 查证把期刊论文误判成预印本）
    if item.venue:
        item.extra["publication"] = {"checked": True, "accepted": True,
                                     "venue": item.venue, "source": "Crossref/EuropePMC"}
    return summary
