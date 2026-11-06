"""指定论文的全文素材提取：PDF、开放获取 XML 与出版商图床三通道。

分工（同一篇论文三条路并行/递进取，按可用性合流）：
  · JATS XML（Europe PMC，生物医学 OA 期刊如 MDPI Sensors）——表格是干净的
    <table> 结构，图注/摘要/正文全文结构化，**数字逐字可信**，优先采用；
  · PDF（OA 官方地址 / arXiv / 欧洲 PMC / OpenAlex 副本 / 出版商直链）——
    **网页直采失效时的必选兜底通道**：结构图缺失、实验表为空或全文没取到时
    自动下载全文，走 pdf_tables() 把 Tables 转成 {caption, header, rows}；
    每张表都要过「单元格合理性」校验（实测 find_tables 会把「84.09」竖排拆成
    「8409\n.」，数字直接失真，喂给模型就是编造），残表整张丢弃。
    同时按图注区域整块渲染插图——架构图/流程图常由几十个小图块拼成，
    单抠 xref 只能拿到半张脸；这里取图注上方全部图元的 bbox 并集，
    以 2.5x+ 缩放把整块区域渲染成 PNG，矢量文字一并保真。
  · 出版商落地页 + 期刊图床（app/sources/publisher_page.py）——ScienceDirect
    等正文页对非订阅 IP 恒返回 403 验证码页（实测 1.2MB challenge 页里
    `Table 4`、`<table>`、`gr1_lrg` 全部零命中），所以文本/表格这条路走不通；
    但图床 ars.els-cdn.com 是静态 CDN，可直连下载
    `1-s2.0-{PII}-gr{n}_lrg.jpg` 高清大图（实测 gr1 200/470KB），
    这是「核心创新」节能配上模型结构图的关键；HTML 真能拿到时再顺带抽
    <table> 转 Markdown 表格。import 放在函数内，避免与 publisher_page
    互相 import 成环。

表格完整性的铁律：喂给模型的每一张表都必须过「单元格合理性」校验——
数字单元格必须形如 0.9374 / 94.68% / 352，含换行残片的单元格直接判废，
宁可整表丢弃（正文改用数值句），也绝不把解析残渣当实验数据。
"""
from __future__ import annotations

import asyncio
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from ..config import settings, topics
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
# 表注：IEEE 惯例是罗马数字（TABLE I / TABLE IV），其它刊物用阿拉伯数字
_TABLE_CAPTION_RE = re.compile(r"^\s*table\s+(?:\d{1,2}|[IVX]{1,5})\b", re.I)
# 表体下边界：同栏下一处表注/图注或章节标题、参考文献标题
_TABLE_STOP_RE = re.compile(
    r"^(?:table\s|figure\s|fig\.?\s|references\b|acknowledg|"
    r"[IVX]{1,4}\.\s+[A-Z]|[A-Z]\.\s+[A-Z][A-Z ]{3,})", re.I)


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

    返回 {"abstract": str, "full_text": str, "tables": [...], "snippets": [...],
          "fig_captions": {n: caption}, "figures": [{number, caption, href}]}；
    解析失败返回空 dict。
    """
    if not xml_text:
        return {}
    out: dict = {"abstract": "", "full_text": "", "tables": [],
                 "snippets": [], "fig_captions": {}, "figures": []}
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

    # 图注：<fig> → label "Figure N" + caption；顺带记下 graphic 的文件名，
    # 让 OA 全文通道能直接下载架构图（付费墙/反爬站点的 PDF 常拿不到图）
    for fig in root.iter("fig"):
        label = _txt(fig.find("label"))
        caption = _txt(fig.find("caption"))
        m = re.search(r"(?:figure|fig\.?)\s*(\d{1,2})", label or caption or "", re.I)
        number = int(m.group(1)) if m else 0
        if m and caption:
            out["fig_captions"][number] = caption[:220]
        graphic = fig.find(".//graphic")
        href = (str(graphic.get("{http://www.w3.org/1999/xlink}href") or "")
                if graphic is not None else "")
        if href and caption:
            out["figures"].append({"number": number, "caption": caption[:220], "href": href})

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


def _find_tables(page, strategy: str) -> list:
    """按策略找表：lines 认框线，text 认空白分栏（booktabs 三线表只能靠它）。"""
    try:
        return list(page.find_tables(strategy=strategy).tables)
    except TypeError:
        keywords = {"vertical_strategy": strategy, "horizontal_strategy": strategy}
        try:
            return list(page.find_tables(**keywords).tables)
        except Exception:
            return []
    except Exception:
        return []


def _table_grid(page, tab) -> list[list[str]]:
    """按单元格 bbox 以阅读顺序重取文本，并丢掉全空列。

    PDF 常把小数点画成独立的文本块，extract() 会给出「24.7」→「247 .」这类
    乱序残片；sort=True 按坐标重排即可还原。合并单元格会在 find_tables 里多出
    一整列空值，留着会让 Markdown 表格错位。
    """
    grid = []
    for row in getattr(tab, "rows", None) or []:
        cells = []
        for cell in row.cells:
            if cell is None:
                cells.append("")
                continue
            words = page.get_text("words", clip=cell, sort=True)
            cells.append(" ".join(str(w[4]) for w in words))
        grid.append(cells)
    if not grid:
        grid = [[str(c or "") for c in row] for row in (tab.extract() or [])]
    grid = [[re.sub(r"\s+", " ", c).strip() for c in row] for row in grid]
    width = max((len(r) for r in grid), default=0)
    keep = [j for j in range(width) if any(j < len(r) and r[j] for r in grid)]
    if not keep:
        return []
    return [[r[j] if j < len(r) else "" for j in keep] for r in grid]


def _caption_for(captions: list[tuple], bbox) -> str:
    """取与表同栏、纵向最近的 "Table N." 说明。

    双栏排版里两表的表注 y 几乎相同，只按 |Δy| 会张冠李戴，必须先用 x 区间
    过滤同栏候选，再比到表上下边缘的纵向距离。
    """
    if not captions:
        return ""
    same_column = [c for c in captions if min(c[2], bbox[2]) > max(c[0], bbox[0])]
    pool = same_column or captions
    return min(pool, key=lambda c: min(abs(c[1] - bbox[1]), abs(c[1] - bbox[3])))[4]


def _table_captions(page) -> list[tuple]:
    """本页表注：(x0, y0, x1, y1, 文本)，阿拉伯数字与罗马数字都认。"""
    captions = []
    for block in page.get_text("blocks"):
        if len(block) >= 7 and block[6] == 0:
            text = re.sub(r"\s+", " ", str(block[4] or "")).strip()
            if _TABLE_CAPTION_RE.match(text):
                captions.append((block[0], block[1], block[2], block[3], text[:200]))
    return captions


def _table_band(page, caption: tuple):
    """表注下方同栏的表体纵向范围：到同栏下一处表注/图注/章节标题为止。"""
    x0, x1, y1 = caption[0], caption[2], caption[3]
    stop = float(page.rect.y1)
    for block in page.get_text("blocks"):
        if len(block) < 7 or block[6] != 0:
            continue
        bx0, by0, bx1 = block[0], block[1], block[2]
        if by0 <= y1 + 2 or min(bx1, x1) <= max(bx0, x0):
            continue
        if _TABLE_STOP_RE.match(re.sub(r"\s+", " ", str(block[4] or "")).strip()):
            stop = min(stop, by0)
    if stop - y1 < 24:
        return None
    return (x0 - 4.0, y1 + 1.0, x1 + 4.0, stop - 1.0)


def _grid_from_words(page, clip) -> list[list[str]]:
    """无框线表（IEEE 常见：表体只剩填充矩形）按词坐标重建行列。

    find_tables 的 lines/text 两个策略对这类版式都返回零表，但单元格文字
    仍在文本层：按 y 中心聚成行、按 x 中心聚成列即可还原，列内按 x 排序拼字。
    """
    words = [w for w in page.get_text("words", clip=clip, sort=True)
             if str(w[4] or "").strip()]
    if len(words) < 6:
        return []
    heights = sorted(max(1.0, w[3] - w[1]) for w in words)
    row_tol = max(2.0, heights[len(heights) // 2] * 0.55)
    rows: list[list[tuple]] = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2.0, w[0])):
        cy = (w[1] + w[3]) / 2.0
        if rows:
            row_cy = sum((r[1] + r[3]) / 2.0 for r in rows[-1]) / len(rows[-1])
            if abs(cy - row_cy) <= row_tol:
                rows[-1].append(w)
                continue
        rows.append([w])
    widths = sorted(max(0.5, (w[2] - w[0]) / max(1, len(str(w[4])))) for w in words)
    col_tol = max(3.0, widths[len(widths) // 2] * 2.2)
    centers: list[float] = []
    for cx in sorted((w[0] + w[2]) / 2.0 for w in words):
        if centers and cx - centers[-1] <= col_tol:
            centers[-1] = (centers[-1] + cx) / 2.0
        else:
            centers.append(cx)
    if len(centers) < 2 or len(rows) < 2:
        return []
    grid = []
    for row in rows:
        cells: list[list[tuple]] = [[] for _ in centers]
        for w in row:
            cx = (w[0] + w[2]) / 2.0
            j = min(range(len(centers)), key=lambda k: abs(centers[k] - cx))
            cells[j].append(w)
        grid.append([" ".join(str(w[4]) for w in sorted(c, key=lambda w: w[0]))
                     for c in cells])
    return [[re.sub(r"\s+", " ", c).strip() for c in row] for row in grid]


def _table_dict(grid: list[list[str]], caption: str) -> dict | None:
    """过完整性校验后转成 {caption, header, rows}；不达标返回 None。"""
    if not _plausible_grid(grid):
        return None
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    if _is_two_row_header(grid):
        header = [grid[0][j] if grid[0][j] == grid[1][j] or not grid[0][j]
                  else (f"{grid[1][j]}@{grid[0][j]}" if _NUM_CELL_RE.match(grid[0][j])
                        else f"{grid[1][j]}({grid[0][j]})")
                  for j in range(width)]
        return {"caption": caption, "header": header, "rows": grid[2:]}
    return {"caption": caption, "header": grid[0], "rows": grid[1:]}


def pdf_tables(pdf_bytes: bytes, limit: int = 3) -> list[dict]:
    """PDF 表格提取（find_tables + 词坐标重建）+ 严格校验，不达标整表丢弃。

    只扫带表注的页：整本逐页跑两个策略在 19 页论文上要 80s+，而正文页永远
    不会有表；先看文本层有没有 "Table N." 再决定解析，耗时降到秒级。
    """
    if not pdf_bytes:
        return []
    try:
        doc = _fitz().open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return []
    out: list[dict] = []
    seen: set[tuple] = set()
    try:
        for pno in range(len(doc)):
            page = doc.load_page(pno)
            captions = _table_captions(page)
            if not captions:
                continue
            boxes: list[tuple] = []
            for strategy in ("lines", "text"):
                for tab in _find_tables(page, strategy):
                    key = (pno,) + tuple(int(v) for v in tab.bbox)
                    if key in seen:
                        continue
                    seen.add(key)
                    table = _table_dict(_table_grid(page, tab),
                                        _caption_for(captions, tab.bbox))
                    if not table:
                        continue
                    out.append(table)
                    boxes.append(tuple(tab.bbox))
                    if len(out) >= limit:
                        return out
            # 表体是填充矩形/无框线时 find_tables 零命中，按表注下方的词坐标重建
            for cap in captions:
                band = _table_band(page, cap)
                if not band:
                    continue
                if any(min(b[2], band[2]) > max(b[0], band[0]) and
                       min(b[3], band[3]) > max(b[1], band[1]) for b in boxes):
                    continue
                table = _table_dict(_grid_from_words(page, band), cap[4])
                if not table:
                    continue
                out.append(table)
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
    """按顺序尝试 PDF 地址，返回字节数据；全失败返回空。

    只保留「拿到就用」的语义；逐渠道失败原因见 acquire_pdf()（成稿链路走那里）。
    """
    for url in urls:
        if not url:
            continue
        body, _detail = await _fetch_pdf_once(client, url, timeout=min(float(timeout), 30.0))
        if body:
            return body
    return b""


# ---------------------------------------------------------------- PDF 获取链（渠道可追溯）
# 期刊 PDF 多在 1.5MB 上下；64MB 是「别把整库扫描件拖回来」的闸门
_MAX_PDF_BYTES = 64 * 1024 * 1024
_PDF_MAGIC = b"%PDF-"
# 结果表优先：Table 1 常是数据集统计，正文「实验结果」要的是对比/消融表
_RESULT_TABLE_RE = re.compile(
    r"compar|result|performance|ablation|mae|mse|rmse|psnr|ssim|f1|accuracy|top-?1|"
    r"precision|recall|miou|iou", re.I)


@dataclass
class PdfAttempt:
    """单渠道 PDF 尝试：失败原因必须能落到日志（HTTP 403 ≠ 超时 ≠ 非 PDF）。"""

    channel: str
    url: str
    detail: str = ""
    size: int = 0

    @property
    def ok(self) -> bool:
        return self.size > 0


def _slug(item: Item) -> str:
    return re.sub(r"[^A-Za-z\d]+", "-", item.title)[:40].strip("-").lower() or "paper"


def _arxiv_id_of(item: Item) -> str:
    """论文的 arXiv id：检索字段 → PDF 地址 → 落地页，三处任一命中即可。"""
    from . import arxiv as arxiv_source

    return (str(item.extra.get("arxiv_id") or "")
            or arxiv_source._arxiv_id_from_url(str(item.extra.get("pdf_url") or ""))
            or arxiv_source._arxiv_id_from_url(str(getattr(item, "url", "") or "")))


async def _fetch_pdf_once(client: HttpClient, url: str, *,
                          timeout: float = 30.0) -> tuple[bytes, str]:
    """下载单个 PDF 地址 → (字节, 失败原因)；失败原因为空串表示成功。

    验证码页/登录墙/落地页都会 200 + text/html，必须按文件头 `%PDF-` 判真；
    体积上限挡住整库扫描件。失败原因按渠道进日志，避免「静默无素材」。
    """
    if not str(url or "").startswith("http"):
        return b"", "地址无效"
    try:
        async with client._sem:
            resp = await asyncio.wait_for(
                client._client.get(url, headers={"Accept": "application/pdf,*/*"},
                                   follow_redirects=True, timeout=timeout),
                timeout=timeout + 5)
    except asyncio.TimeoutError:
        return b"", f"超时（>{int(timeout)}s）"
    except Exception as exc:
        return b"", f"请求异常 {type(exc).__name__}"
    if resp.status_code >= 400:
        return b"", f"HTTP {resp.status_code}"
    body = resp.content or b""
    if not body:
        return b"", "空响应"
    if len(body) > _MAX_PDF_BYTES:
        return b"", f"文件过大（{len(body) // 1024 // 1024}MB）"
    if not body.startswith(_PDF_MAGIC):
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        return b"", f"非 PDF 响应（{ctype or 'unknown'}）"
    return body, ""


async def europepmc_pdf_urls(client: HttpClient, doi: str, pmcid: str) -> list[str]:
    """Europe PMC 的 OA 全文 PDF；没有 PMCID 时按 DOI 查一次（多数非 OA 论文会落空）。"""
    pid = str(pmcid or "").strip()
    if pid and not pid.upper().startswith("PMC"):
        pid = f"PMC{pid}"
    if not pid and doi:
        try:
            data = await asyncio.wait_for(client.json(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params={"query": f'DOI:"{doi}"', "format": "json",
                        "resultType": "lite", "pageSize": 1}), timeout=15)
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        hits = (data.get("resultList") or {}).get("result") or []
        pid = str((hits[0] if hits else {}).get("pmcid") or "")
    if not pid:
        return []
    return [f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pid}/fullTextPDF"]


async def openalex_pdf_urls(client: HttpClient, doi: str) -> list[str]:
    """OpenAlex 的 OA 副本地址（locations[].pdf_url / oa_url，免鉴权）。

    作者自存档、机构库、预印本都会作为 location 挂在 OpenAlex 上——这是付费墙
    论文唯一可能合法拿到全文的缺口；closed access 论文这里就是空列表。
    """
    if not doi:
        return []
    try:
        data = await asyncio.wait_for(
            client.json(f"https://api.openalex.org/works/doi:{doi}"), timeout=20)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    locations = [data.get("open_access") or {}, data.get("best_oa_location") or {},
                 *(data.get("locations") or [])]
    out: list[str] = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        for key in ("pdf_url", "oa_url"):
            url = str(loc.get(key) or "")
            if url.startswith("http"):
                out.append(url)
    return out


async def unpaywall_pdf_urls(client: HttpClient, doi: str) -> list[str]:
    """Unpaywall 的 OA 副本；需要真实联系邮箱，未配置就跳过（绝不伪造）。"""
    email = (os.getenv("UNPAYWALL_EMAIL", "") or "").strip()
    if not doi or not email:
        return []
    try:
        data = await asyncio.wait_for(
            client.json(f"https://api.unpaywall.org/v2/{doi}", params={"email": email}),
            timeout=20)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    locations = [data.get("best_oa_location") or {}, *(data.get("oa_locations") or [])]
    return [str(loc.get("url_for_pdf") or "") for loc in locations
            if isinstance(loc, dict) and str(loc.get("url_for_pdf") or "").startswith("http")]


async def sciencedirect_pdf_url(client: HttpClient, item: Item) -> str:
    """ScienceDirect 全文 PDF：只有配了订阅 Cookie 才可能下到，否则恒 403 不浪费请求。"""
    if not (os.getenv("SCIENCEDIRECT_COOKIE") or os.getenv("ELSEVIER_COOKIE")):
        return ""
    from .publisher_page import elsevier_pii, resolve_pii

    landing = str(getattr(item, "url", "") or item.extra.get("landing_url") or "")
    pii = elsevier_pii(landing)
    if not pii and landing.startswith("http"):
        try:
            pii = await asyncio.wait_for(
                resolve_pii(client, landing, log=lambda *_a, **_k: None), timeout=20)
        except Exception:
            return ""
    if not pii:
        return ""
    return (f"https://www.sciencedirect.com/science/article/pii/{pii}"
            "/pdfft?isDTMRedir=true&download=true")


async def resolve_pdf_urls(client: HttpClient, item: Item, *,
                           log=print) -> list[tuple[str, str]]:
    """PDF 全文候选（渠道, URL）：按可信度排序、按 URL 去重。

    候选解析并行跑（每个源都有超时与异常兜底），真正的下载在 acquire_pdf 里
    串行试——命中即停，不为后面的渠道浪费带宽。
    """
    extra = item.extra
    doi = str(extra.get("doi") or "").strip()
    pairs: list[tuple[str, str]] = []

    def add(channel: str, url: object) -> None:
        text = str(url or "").strip()
        if text.startswith("http") and all(text != seen for _c, seen in pairs):
            pairs.append((channel, text))

    for url in (extra.get("pdf_url"), *(extra.get("pdf_urls") or [])):
        add("直链", url)
    arxiv_id = _arxiv_id_of(item)
    if arxiv_id:
        add("arXiv", f"https://arxiv.org/pdf/{arxiv_id}")

    from .paper_search import scihub_pdf_url

    tasks = [
        ("欧洲 PMC", europepmc_pdf_urls(client, doi, str(extra.get("pmcid") or ""))),
        ("OpenAlex", openalex_pdf_urls(client, doi)),
        ("Unpaywall", unpaywall_pdf_urls(client, doi)),
        ("ScienceDirect", _one(sciencedirect_pdf_url(client, item))),
        ("S2 镜像", _one(semantic_scholar_pdf(client, doi))),
        ("Sci-Hub", _one(scihub_pdf_url(client, doi))),
    ]
    found = await asyncio.gather(*(t for _c, t in tasks), return_exceptions=True)
    for (channel, _task), result in zip(tasks, found):
        if isinstance(result, Exception):
            log(f"素材兜底：PDF 候选源 {channel} 异常（{type(result).__name__}）")
            continue
        for url in result if isinstance(result, list) else [result]:
            add(channel, url)
    return pairs


async def _one(value) -> list[str]:
    """把「返回单值」的协程统一成列表，好与批量候选源一起 gather。"""
    text = await value
    return [text] if text else []


def _save_pdf(item: Item, body: bytes):
    """PDF 落盘（out/pdfs/{slug}.pdf）便于复现与人工核对；失败不影响成稿。"""
    try:
        folder = settings.out_dir / "pdfs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_slug(item)}.pdf"
        path.write_bytes(body)
        return path
    except Exception:
        return None


async def acquire_pdf(client: HttpClient, item: Item, *, log=print,
                      per_url: float = 25.0,
                      total: float = 100.0) -> tuple[bytes, list[PdfAttempt]]:
    """网页直采不足时的 PDF 兜底：解析候选 → 逐渠道下载 → 落盘。

    返回 (PDF 字节, 逐渠道尝试记录)。全失败返回 b""，并把每个渠道的失败原因
    写进 item.extra["pdf_failure"]——「无图无表」的稿子必须能定位到是哪条链路
    断了（403 验证码 / 超时 / 拿到的是 HTML），绝不静默降级。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total
    attempts: list[PdfAttempt] = []
    for channel, url in await resolve_pdf_urls(client, item, log=log):
        remain = deadline - loop.time()
        if remain <= 1:
            attempts.append(PdfAttempt(channel, url, "总预算耗尽，未尝试"))
            continue
        body, detail = await _fetch_pdf_once(
            client, url, timeout=min(per_url, max(5.0, remain)))
        attempts.append(PdfAttempt(channel, url, detail, len(body)))
        if body:
            path = _save_pdf(item, body)
            item.extra["pdf_source"] = channel
            item.extra["pdf_failure"] = ""
            log(f"素材兜底：PDF 已下载（渠道 {channel}，{len(body) // 1024}KB"
                + (f"，落盘 {path.name}" if path else "") + "）")
            return body, attempts
        log(f"素材兜底：PDF 渠道 {channel} 失败（{detail}）")
    reasons = "；".join(f"{a.channel}:{a.detail}" for a in attempts) or "无可用候选地址"
    item.extra["pdf_failure"] = reasons
    log(f"素材兜底：PDF 全渠道失败（{len(attempts)} 条）——{reasons}")
    return b"", attempts


def _rank_tables(tables: list[dict]) -> list[dict]:
    """结果/对比表排前面（稳定排序）：ensure_paper_result_table 取第一张表进正文。"""
    return sorted(tables, key=lambda t: 0 if _RESULT_TABLE_RE.search(
        str(t.get("caption") or "")) else 1)


def _result_sentences(text: str, limit: int = 10) -> list[str]:
    """全文里带数字的结论句（PDF 兜底口径：与 JATS 的 snippets 同款筛选）。"""
    sentences: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", (text or "").replace("\n", " ")):
        if re.search(r"\d", sent) and re.search(
                r"F1|precision|recall|mAP|accuracy|compared|outperform|achiev", sent, re.I):
            if sent.strip() not in sentences:
                sentences.append(sent.strip()[:300])
        if len(sentences) >= limit:
            break
    return sentences


# Markdown 表（作者仓库 README）里的强调/图片/内联 HTML：社区常用 <ins> 标最高分
_MD_DECOR_RE = re.compile(
    r"</?(?:ins|u|b|strong|em|i|sup|sub|mark|span|br|a)\b[^>]*>|\*\*|__|`|"
    r"!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)", re.I)
_MD_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")


def _md_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [_md_cell(c) for c in cells]


def _md_cell(cell: str) -> str:
    text = _MD_DECOR_RE.sub(lambda m: m.group(1) or "", str(cell or ""))
    return re.sub(r"\s+", " ", text.replace("\\|", "|")).strip()


def _md_tables(text: str, limit: int = 4) -> list[dict]:
    """README/HTML 里的 Markdown 表 → {caption, header, rows}，过同一套合理性校验。

    论文 PDF 的实验表常被整块贴成位图（文本层一个字都没有），作者仓库 README
    的 Markdown 表与论文表格同源、数值逐字可信，是「结果节必须有结构化表」的
    合规兜底。表前最近一行非空文本当表注；残表（列数不足/数字占比过低）丢弃。
    """
    lines = (text or "").splitlines()
    out: list[dict] = []
    block: list[str] = []
    caption = pending = ""

    def flush() -> bool:
        if not block:
            return False
        rows = [_md_row(ln) for ln in block]
        rows = [r for r in rows
                if not (r and all(_MD_SEP_CELL_RE.match(c) for c in r if c))]
        if len(rows) >= 2:
            table = _table_dict(rows, caption)
            if table:
                out.append(table)
        block.clear()
        return len(out) >= limit

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            if not block:
                caption = pending
            block.append(stripped)
            continue
        if flush():
            return out
        if stripped and not stripped.startswith(("#", "|", "!", "[", "<", ">")):
            pending = _md_cell(stripped)[:160]
    flush()
    return out[:limit]


_REPO_SOURCE_RE = re.compile(r"github\.com/([^/\s?#]+/[^/\s?#]+)")
_README_NAMES = ("README.md", "readme.md", "README.MD")


async def _repo_tables(client: HttpClient, item: Item) -> list[dict]:
    """官方仓库 README 里的结果表：PDF 表体栅格化时的结构化兜底。"""
    source = str(item.code_url or item.extra.get("code_url") or "")
    m = _REPO_SOURCE_RE.search(source)
    if not m:
        return []
    repo = m.group(1).removesuffix(".git").rstrip(".,);")
    for ref in ("HEAD", "main", "master"):
        for name in _README_NAMES:
            url = f"https://raw.githubusercontent.com/{repo}/{ref}/{name}"
            try:
                text = await asyncio.wait_for(
                    client.text(url, timeout=20), timeout=25)
            except Exception:
                continue
            tables = _md_tables(text, limit=4)
            if tables:
                return tables
    return []


def _persist_pdf_figures(pdf_bytes: bytes, item: Item, assets: dict,
                         figures: list[tuple[str, str, str]]) -> None:
    """图注区域整块渲染成 PNG 落盘：架构图常由几十个小图块拼成，整块才完整。"""
    if not pdf_bytes:
        return
    slug = _slug(item)
    for fig in _caption_regions(pdf_bytes)[:4]:
        path = settings.image_dir / f"paper_{slug}_fig{fig.number:02d}.png"
        path.write_bytes(fig.png)
        caption = assets.get("fig_captions", {}).get(fig.number) or fig.caption
        figures.append((str(path), caption, fig.role))


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


_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _title_match(a: str, b: str) -> float:
    """两个标题的词元重合度（0~1）：arXiv 检索结果里认出同一篇论文用。"""
    ta = set(_TITLE_TOKEN_RE.findall((a or "").lower()))
    tb = set(_TITLE_TOKEN_RE.findall((b or "").lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


async def _arxiv_assets(client: HttpClient, item: Item) -> dict:
    """架构图/表格兜底：论文在 arXiv 有版面时抓 HTML 版。

    先按 URL 里的 arXiv id 直取；没有 id 就用标题检索，词元重合度 ≥0.7 才认，
    避免把同名的另一篇论文的图当成这篇的架构图。
    """
    from . import arxiv as arxiv_source

    arxiv_id = _arxiv_id_of(item)
    if not arxiv_id:
        endpoint = (topics.raw.get("academic", {}).get("arxiv", {})
                    .get("endpoint", "http://export.arxiv.org/api/query"))
        query = re.sub(r'["“”]', " ", item.title)[:180].strip()
        if not query:
            return {}
        try:
            xml_text = await client.text(endpoint, params={
                "search_query": f'ti:"{query}"', "start": 0,
                "max_results": 5, "sortBy": "relevance"})
            for hit in arxiv_source.parse_arxiv_feed(xml_text):
                if _title_match(hit.title, item.title) >= 0.7:
                    arxiv_id = str(hit.extra.get("arxiv_id") or "")
                    if arxiv_id:
                        break
        except Exception:
            return {}
    if not arxiv_id:
        return {}
    try:
        return await arxiv_source.extract_assets(client, arxiv_id, item.title)
    except Exception:
        return {}


async def _repo_arch_figure(client: HttpClient, item: Item) -> tuple[str, str, str]:
    """官方仓库 README 里的架构图：arXiv 也没有版面时的最后一手。"""
    from ..render.cover_select import _readme_candidates

    source = str(item.code_url or item.extra.get("code_url") or "")
    m = re.search(r"github\.com/([^/\s?#]+/[^/\s?#]+)", source)
    if not m:
        return ("", "", "")
    repo = m.group(1).removesuffix(".git").rstrip(".,);")
    try:
        candidates = await _readme_candidates(client, repo)
    except Exception:
        return ("", "", "")
    for cand in candidates:
        if cand.role != "arch":
            continue
        for url in [cand.url, *cand.fallbacks]:
            try:
                path = await asyncio.wait_for(
                    client.download_image(url, referer=cand.referer or ""), timeout=15)
            except Exception:
                path = ""
            if path:
                return (path, cand.caption or item.title, "arch")
    return ("", "", "")


async def _jats_arch_figure(client: HttpClient, figs: list[dict], pmcid: str,
                            item: Item) -> tuple[str, str, str]:
    """免费全文里的架构图：出版商 PDF 被反爬墙拦下时的主力通道。

    JATS 的 <graphic xlink:href> 是期刊包内的文件名，原图要走 PMC 的 instance
    图床；只认图注命中架构词（flowchart / architecture / framework / pipeline）
    的 <fig>——拿不到就返回空三元组，交给 arXiv / README 兜底。
    """
    arch = [f for f in figs or []
            if _figure_role(str(f.get("caption") or ""), int(f.get("number") or 0)) == "arch"]
    digits = re.sub(r"\D", "", pmcid or "")
    if not arch or not digits:
        return ("", "", "")
    referer = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{digits}/"
    for fig in sorted(arch, key=lambda f: int(f.get("number") or 0))[:4]:
        name = str(fig.get("href") or "").rsplit("/", 1)[-1].strip()
        if not name:
            continue
        url = f"https://pmc.ncbi.nlm.nih.gov/articles/instance/{digits}/bin/{name}"
        try:
            path = await asyncio.wait_for(
                client.download_image(url, referer=referer), timeout=20)
        except Exception:
            path = ""
        if path:
            return (path, str(fig.get("caption") or item.title), "arch")
    return ("", "", "")


async def enrich_target(client: HttpClient, item: Item, *, pdf_bytes: bytes = b"",
                        pdf_label: str = "", log=print) -> dict:
    """给指定检索命中的论文补齐成稿素材：全文 / 表格 / 数值句 / 架构与结果图。

    素材写入 item.extra 与 item.images（架构图在前，与流水线 paper 模块的
    图注对齐约定一致：extra["figure_captions"] 与 images 等长）。
    `pdf_bytes` 非空表示用户直接给了本地 PDF：全文/表格/插图以这份文件为准，
    跳过下载链（本地原件常超过 _MAX_PDF_BYTES 的下载闸门），再由联网通道补
    高清图床大图与作者仓库的结构化结果表。
    返回摘要 dict 供日志。
    """
    extra = item.extra
    pmcid = str(extra.get("pmcid") or "")
    summary = {"pdf": 0, "jats": False, "figures": 0, "tables": 0, "snippets": 0,
               "pdf_source": "", "pdf_tried": []}
    assets: dict = {"tables": [], "snippets": [], "full_text": "",
                    "fig_captions": {}, "figures": []}
    figures: list[tuple[str, str, str]] = []   # (path, caption, role)
    arch_src = ""
    local = bool(pdf_bytes)
    label = pdf_label or "本地 PDF"
    figs_done = False

    # 阶段一（网页 / HTML 直采）：OA 全文 JATS 的表与图注最干净，优先取用
    jats = await fetch_jats(client, pmcid)
    summary["jats"] = bool(jats)
    if jats:
        parsed = jats_assets(jats)
        assets["tables"] = assets["tables"] or parsed.get("tables") or []
        assets["snippets"] = assets["snippets"] or parsed.get("snippets") or []
        assets["full_text"] = assets["full_text"] or parsed.get("full_text") or ""
        assets["fig_captions"] = parsed.get("fig_captions") or {}
        assets["figures"] = parsed.get("figures") or []

    # 阶段一之二（本地 PDF）：用户给了文件就以它为准——元数据/全文/数值句/表格
    # 直接取自本地原件，插图按图注区域整块渲染；联网通道只补它没有的东西
    if local:
        text = pdf_text(pdf_bytes) or assets["full_text"]
        assets["full_text"] = text
        assets["snippets"] = _result_sentences(text) or assets["snippets"]
        local_tables = pdf_tables(pdf_bytes, limit=8)
        if local_tables:
            assets["tables"] = local_tables
        _persist_pdf_figures(pdf_bytes, item, assets, figures)
        figs_done = True
        summary["pdf"] = len(pdf_bytes)
        summary["pdf_source"] = label
        log(f"素材兜底：{label} 解析完成（全文 {len(text)} 字，"
            f"表格 {len(local_tables)} 张，插图 {len(figures)} 张）")
        if not local_tables:
            log("素材兜底：pdf_tables() 未取到结构完整的表（表体多为位图，"
                "改由官方仓库 README / 网页 HTML 补结构化表）")
        if any(role == "arch" for _p, _c, role in figures):
            arch_src = label

    if assets["figures"]:
        path, caption, role = await _jats_arch_figure(client, assets["figures"], pmcid, item)
        if path:
            figures.append((path, caption, role))
            arch_src = "OA-XML"
            log(f"素材兜底：架构图已由 OA 全文图床补齐（{caption[:40]}）")
    # 出版商落地页 + 期刊图床：ScienceDirect 正文页对非订阅 IP 恒 403（验证码），
    # 但图床 ars.els-cdn.com 可直连，按 PII 拼 `-gr{n}_lrg.jpg` 就能拿到高清结构
    # 大图；表格只能来自拿得到的 HTML（验证码页里 Table 4 一个字都没有），拿不到
    # 就如实留空，绝不用模型凭空补表。
    need_arch = not any(role == "arch" for _p, _c, role in figures)
    landing = str(getattr(item, "url", "") or extra.get("landing_url") or "")
    if landing.startswith("http") and (need_arch or not assets["tables"]):
        try:
            from .publisher_page import publisher_assets
            pub = await asyncio.wait_for(
                publisher_assets(client, landing, captions=assets["fig_captions"], log=log),
                timeout=120)
            new_figs = pub.get("figures") or []
            arch_pub = next((f for f in new_figs if f[2] == "arch"), None)
            if need_arch and arch_pub:
                figures.insert(0, arch_pub)
                need_arch = False
                arch_src = "出版商图床"
                log(f"素材兜底：架构图已由出版商图床补齐（{arch_pub[1][:40]}）")
            have = {p for p, _c, _r in figures}
            for fig in new_figs:
                if fig[0] not in have:
                    figures.append(fig)
                    have.add(fig[0])
            if not assets["fig_captions"] and pub.get("fig_captions"):
                assets["fig_captions"] = pub["fig_captions"]
            if not assets["tables"] and pub.get("tables"):
                assets["tables"] = pub["tables"]
                log(f"素材兜底：表格已由出版商落地页补齐（{len(assets['tables'])} 张）")
        except Exception:
            pass

    # 阶段二（PDF 必选兜底）：网页端结构图缺失 / 实验表为空 / 全文没取到，就自动
    # 下载完整 PDF，走 pdf_tables() 与图注区域渲染——绝不静默产出「无图无表」的稿子
    if not local and (need_arch or not assets["tables"] or not assets["full_text"]):
        log("素材兜底：网页直采不足（结构图缺失/实验表为空/全文未取到），"
            "自动下载 PDF 走 pdf_tables()")
        pdf_bytes, attempts = await acquire_pdf(client, item, log=log)
        summary["pdf"] = len(pdf_bytes)
        summary["pdf_source"] = str(extra.get("pdf_source") or "")
        summary["pdf_tried"] = [{"channel": a.channel, "url": a.url, "ok": a.ok,
                                 "detail": a.detail} for a in attempts]
    elif not local:
        pdf_bytes = b""
    if not assets["full_text"] and pdf_bytes:
        assets["full_text"] = pdf_text(pdf_bytes)
        # PDF 兜底数值句（只在 JATS 缺席时用——同源口径要一致）
        assets["snippets"] = assets["snippets"] or _result_sentences(assets["full_text"])
    if pdf_bytes and not assets["tables"]:
        pdf_rows = pdf_tables(pdf_bytes, limit=8)
        assets["tables"] = pdf_rows
        if pdf_rows:
            log(f"素材兜底：pdf_tables() 从 PDF 提取 {len(pdf_rows)} 张表")
        else:
            log("素材兜底：pdf_tables() 未取到结构完整的表（宁缺毋假，不喂解析残渣）")

    # 插图：优先 PDF 图注区域渲染；caption 优先用 JATS 的干净版本
    # （本地 PDF 已在阶段一之二落盘，这里不再重复渲染）
    if not figs_done:
        _persist_pdf_figures(pdf_bytes, item, assets, figures)
    need_arch = not any(role == "arch" for _p, _c, role in figures)
    if not need_arch and not arch_src:
        arch_src = label if local else "PDF"

    # 阶段三（免费全文/官方仓库兜底）：arXiv 版面与作者仓库 README 里的结构图
    if need_arch or not assets["tables"]:
        try:
            fb = await _arxiv_assets(client, item)
        except Exception:
            fb = {}
        arch = fb.get("arch")
        if need_arch and arch and arch[0]:
            figures.insert(0, (arch[0], arch[1] or item.title, "arch"))
            need_arch = False
            arch_src = "arXiv"
        if not assets["tables"] and fb.get("tables"):
            assets["tables"] = fb["tables"]
        if not assets["snippets"] and fb.get("snippets"):
            assets["snippets"] = fb["snippets"]
    if need_arch:
        try:
            path, caption, role = await _repo_arch_figure(client, item)
            if path:
                figures.insert(0, (path, caption, role))
                need_arch = False
                arch_src = "README"
        except Exception:
            pass
    # 作者仓库 README 的 Markdown 结果表：PDF 表体是整块位图时唯一可信的表来源
    if not assets["tables"]:
        try:
            repo_tables = await _repo_tables(client, item)
        except Exception:
            repo_tables = []
        if repo_tables:
            assets["tables"] = repo_tables
            log(f"素材兜底：表格已由官方仓库 README 补齐（{len(repo_tables)} 张）")
    if assets["tables"]:
        assets["tables"] = _rank_tables(assets["tables"])
    summary["figures"] = len(figures)
    log(f"素材兜底：架构图 "
        f"{'已补齐（' + arch_src + '）' if not need_arch else '仍缺失'}，"
        f"表格 {len(assets['tables'])} 张")
    if need_arch:
        log("素材红线：模型结构图缺失（网页直采 + PDF + arXiv/README 全部未命中）")
    if not assets["tables"]:
        log("素材红线：实验对比表缺失（网页 <table> / pdf_tables() / 仓库 README "
            "均未取到可用表）")
    images: list[str] = []
    caps: list[str] = []
    for role in ("arch", "result", "teaser"):
        for path, caption, fig_role in figures:
            if fig_role == role and path not in images:
                images.append(path)
                caps.append(caption)
                break
    if not images and figures:
        # 图注没命中任何角色词（role 全落在 other）时也得有图：
        # 「核心创新与方法拆解」节零插图是硬伤，宁可先挂第一张真图
        images.append(figures[0][0])
        caps.append(figures[0][1])
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
