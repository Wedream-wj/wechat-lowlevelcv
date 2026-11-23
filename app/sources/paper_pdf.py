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
import math
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
# 数字词锚点：词坐标重建时认「这格是数值」（允许千分位逗号，比单元格更宽松）
_NUM_ANCHOR_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?(?:e-?\d+)?%?$")
# 文本单元格：方法名/指标名，短且无控制字符（方括号引用、逗号都是论文表格常态）
_TEXT_CELL_RE = re.compile(r"^[A-Za-z\[][A-Za-z\d .+\-/()×x%≤≥~\[\],]{0,40}$")
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
    """表注下方同栏的表体纵向范围：到同栏下一处表注/图注/章节标题为止。

    表注常被 PDF 拆成多块（"TABLE III" 一块、续行 "ABLATION STUDY OF …"
    另一块）：紧邻的、全大写、不含数值的短块并入表注区，否则续行会被
    词坐标重建吸进表头，表头就带上 "STAGES." 这类碎屑。
    """
    x0, x1, y1 = caption[0], caption[2], caption[3]
    blocks = [b for b in page.get_text("blocks") if len(b) >= 7 and b[6] == 0]
    top = y1
    changed = True
    while changed:
        changed = False
        for b in blocks:
            bx0, by0, bx1, by1 = b[0], b[1], b[2], b[3]
            if by0 <= top + 1 or by0 - top > 9:
                continue  # 必须紧贴当前表注区下缘
            if min(bx1, x1) <= max(bx0, x0):
                continue  # 必须同栏
            text = re.sub(r"\s+", " ", str(b[4] or "")).strip()
            letters = [ch for ch in text if ch.isalpha()]
            if not letters or len(text) > 120:
                continue
            if sum(1 for ch in letters if ch.isupper()) / len(letters) < 0.7:
                continue  # 表头行是大小写混排的，只有表注续行才是整句全大写
            if _NUM_ANCHOR_RE.match(text) or re.search(
                    r"\d+\.\d+", text):
                continue  # 含数值的是表体不是表注
            top = by1
            changed = True
    stop = float(page.rect.y1)
    for block in blocks:
        if len(block) < 7 or block[6] != 0:
            continue
        bx0, by0, bx1 = block[0], block[1], block[2]
        if by0 <= top + 2 or min(bx1, x1) <= max(bx0, x0):
            continue
        if _TABLE_STOP_RE.match(re.sub(r"\s+", " ", str(block[4] or "")).strip()):
            stop = min(stop, by0)
    if stop - top < 24:
        return None
    return (x0 - 4.0, top + 1.0, x1 + 4.0, stop - 1.0)


def _band_words(page, band, pad: float = 12.0) -> list[tuple]:
    """表注辖域内的完整词（绝不裁字）：x 与展宽后的辖域重叠过半、y 落在辖域内。

    居中表注的文本块比表体窄，直接拿它当 clip 会把表体左缘的 "Metric" 裁成
    "etric"、"MAE" 裁成 "AE"——数据从这里开始错位。所以先取全页词，
    再按几何过滤；向两侧展宽 pad 兼顾表体略宽于表注的版式。
    """
    x0, y0, x1, y1 = band
    out = []
    for w in page.get_text("words", sort=True):
        if (w[1] + w[3]) / 2.0 <= y0 or w[1] > y1 + 1.0:
            continue  # 词心在辖域上缘之上 = 表注末行（bbox 下缘常与上缘齐平）
        overlap = min(w[2], x1 + pad) - max(w[0], x0 - pad)
        if overlap >= 0.5 * max(1.0, w[2] - w[0]):
            out.append(w)
    return out


def _words_rows(words: list[tuple]) -> list[list[tuple]]:
    """词按 y 中心聚成行，行内按 x 排序（词 = (x0, y0, x1, y1, text, …)）。"""
    words = [w for w in words if str(w[4] or "").strip()]
    if len(words) < 4:
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
    for r in rows:
        r.sort(key=lambda w: w[0])
    return rows


def _numeric_words(row: list[tuple]) -> list[tuple]:
    """一行里的数值词（尾随逗号的枚举 token 如 "1," 不算数值）。"""
    return [w for w in row if _NUM_ANCHOR_RE.match(str(w[4]).rstrip(","))]


def _data_frames(rows: list[list[tuple]]) -> list[tuple[float, float]]:
    """由「满行数据行」的数值词做种子聚出数据列，返回每列 (x0, x1) 区间。

    种子只取数值格最多的行（真数据行的数值列彼此对齐，列间距远大于列内抖动），
    其余数值词就近归入种子列——图表轴刻度以同 x 纵向堆叠、散点横跨全栏，
    链式聚类会被它们桥接成一整条，种子法天然免疫；单字符枚举 token（表头
    "Stage 1, 2" 里的 1/2/3）不当种子。成员数不足峰值 60% 的列丢弃。
    """
    counts = [len(_numeric_words(r)) for r in rows]
    peak = max(counts, default=0)
    if peak < 2:
        return []
    seed_rows = [r for r, n in zip(rows, counts) if n >= max(2, int(0.8 * peak))][:6]
    strong = [w for r in seed_rows for w in _numeric_words(r) if len(str(w[4]).rstrip(",")) >= 2]
    seed_words = strong or [w for r in seed_rows for w in _numeric_words(r)]
    if not seed_words:
        return []
    seeds = sorted((w[0] + w[2]) / 2.0 for w in seed_words)
    # 种子按列聚拢（同列种子中心几乎重合）
    widths = sorted(w[2] - w[0] for w in seed_words)
    tol = max(6.0, 0.5 * widths[len(widths) // 2])
    cols: list[list[float]] = [[seeds[0]]]
    for c in seeds[1:]:
        if c - cols[-1][-1] <= tol:
            cols[-1].append(c)
        else:
            cols.append([c])
    centers = [sum(g) / len(g) for g in cols]
    gaps = sorted(centers[i + 1] - centers[i] for i in range(len(centers) - 1))
    assign_tol = max(8.0, 0.35 * gaps[len(gaps) // 2]) if gaps else 8.0
    members: list[list] = [[] for _ in centers]
    for row in rows:
        for w in _numeric_words(row):
            cx = (w[0] + w[2]) / 2.0
            k = min(range(len(centers)), key=lambda i: abs(centers[i] - cx))
            if abs(centers[k] - cx) <= assign_tol:
                members[k].append(w)
    frames = []
    for mem in members:
        if mem:
            frames.append([min(w[0] for w in mem), max(w[2] for w in mem), len(mem)])
    peak_m = max((f[2] for f in frames), default=0)
    return [(f[0], f[1]) for f in frames if f[2] >= max(2, 0.6 * peak_m)]


def _assign_col(w, frames) -> int:
    """词 → 列号；-1 = 首列左侧的标签列（方法名/Dataset 等）。"""
    cx = (w[0] + w[2]) / 2.0
    widths = sorted(f[1] - f[0] for f in frames)
    bias = max(8.0, 0.25 * widths[len(widths) // 2])
    if cx < frames[0][0] - bias:
        return -1
    return min(range(len(frames)),
               key=lambda k: abs((frames[k][0] + frames[k][1]) / 2.0 - cx))


def _phrase_row(row: list[tuple], frames) -> list[list]:
    """表头行的词 → 短语 [text, x0, x1, cols]；cols ≥ 2 即跨列分组表头。

    相邻词间距小于列距阈值时拼成短语（"DH-SHT" + "PartA" → "DH-SHT PartA"），
    被拆开的同列短语再回拼（"Stage 1," + "2" → "Stage 1, 2"）。
    """
    gap_tol = 3.0
    if len(frames) >= 2:
        gaps = sorted(frames[i + 1][0] - frames[i][1] for i in range(len(frames) - 1))
        gap_tol = max(3.0, 0.35 * gaps[len(gaps) // 2])
    phrases: list[list] = []
    for w in row:
        if phrases and w[0] - phrases[-1][2] <= gap_tol:
            phrases[-1][0] += " " + str(w[4])
            phrases[-1][2] = w[2]
        else:
            phrases.append([str(w[4]), w[0], w[2]])
    out = []
    for text, x0, x1 in phrases:
        cols = [j for j, (f0, f1) in enumerate(frames)
                if min(x1, f1) - max(x0, f0) >= 0.3 * max(1.0, x1 - x0)]
        if not cols:
            cols = [_assign_col((x0, 0.0, x1, 0.0, text), frames)]
        out.append([text, x0, x1, cols])
    merged = [out[0]]
    for p in out[1:]:
        if p[3] == merged[-1][3] and p[3][0] >= 0:
            merged[-1][0] += " " + p[0]
            merged[-1][2] = p[2]
        else:
            merged.append(p)
    return merged


def _cell_numeric(cell: list[str]) -> bool:
    text = " ".join(str(x) for x in cell).strip().strip(".,;")
    return bool(text) and bool(_NUM_CELL_RE.match(text))


def _band_grid(page, band) -> list[list[str]] | None:
    """表注辖域内的词坐标重建——数据对齐契约的主通道。

    数字词锚定数据列 → 每行按列框回格 → 多行分组表头折叠成单行 →
    按「行签名」截掉图表残渣（轴刻度/图例）。返回 [header, *rows] 网格，
    表头、方法名、数值 100% 对应；对齐不了的稀疏行一律丢弃，宁缺毋假。
    """
    rows = _words_rows(_band_words(page, band))
    if len(rows) < 2:
        return None
    rough = [row for row in rows if len(_numeric_words(row)) >= 2]
    if len(rough) < 2:
        return None
    frames = _data_frames(rough)
    if len(frames) < 2:
        return None

    def _place(rs, frs):
        out = []
        for row in rs:
            cells: list[list[str]] = [[] for _ in frs]
            label: list[str] = []
            for w in row:
                j = _assign_col(w, frs)
                (label if j < 0 else cells[j]).append(str(w[4]))
            out.append((label, cells))
        return out

    # 行分类：数值格 ≥ 2 且数值占比 ≥ 0.5 的才是数据行（表头/图例行都是文本格）
    def _is_data(cells) -> bool:
        filled = [c for c in cells if c]
        nums = sum(1 for c in filled if _cell_numeric(c))
        return len(filled) >= 2 and nums >= 2 and nums * 2 >= len(filled)

    placed = _place(rows, frames)
    # 两遍帧计算：首遍帧可能被表头枚举 token / 图表数字污染，用干净数据行重算
    data_rows = [row for row, (_l, c) in zip(rows, placed) if _is_data(c)]
    if len(data_rows) >= 2:
        refined = _data_frames(data_rows)
        if len(refined) >= 2:
            frames = refined
            placed = _place(rows, frames)

    first = next((i for i, (_l, c) in enumerate(placed) if _is_data(c)), -1)
    if first < 0:
        return None
    width = len(frames) + 1
    # 表头：数据行之前最多 3 行；整句大写的表注续行（>80 字符）不是表头，跳过；
    # 跨列分组短语下放给被覆盖的每一列后折叠成单行
    covers: list[list[str]] = [[] for _ in range(width)]
    for row in rows[max(0, first - 3):first]:
        if len(" ".join(str(w[4]) for w in row)) > 80:
            continue
        for text, _x0, _x1, cols in _phrase_row(row, frames):
            for j in cols:
                covers[j + 1].append(text)
    header = []
    for parts in covers:
        dedup: list[str] = []
        for p in parts:
            if not dedup or dedup[-1] != p:
                dedup.append(p)
        header.append(" ".join(dedup).strip())

    def _full_row(cells) -> bool:
        filled = sum(1 for c in cells if c)
        nums = sum(1 for c in cells if _cell_numeric(c))
        return nums >= 2 and filled >= math.ceil(0.7 * width)

    last = max((i for i in range(first, len(rows)) if _full_row(placed[i][1])),
               default=-1)
    if last < first:
        return None
    grid = [header]
    pending: list[str] = []

    def _clean_label(words: list[str]) -> str:
        # 辖域展宽吸进来的孤立脚标（单个字母）不许混进方法名
        while len(words) > 1 and len(words[0].strip()) == 1 and words[0].isalpha():
            words = words[1:]
        return " ".join(words).strip()

    for i in range(first, last + 1):
        label, cells = placed[i]
        if _full_row(cells):
            grid.append([_clean_label(pending + label)]
                        + [" ".join(c).strip() for c in cells])
            pending = []
        elif not _numeric_words(rows[i]):
            pending.extend(label)      # 折行的方法名：并给下一个数据行
        # 行内含数字但不满格的行 = 图表残渣，丢弃（对齐不了的数据宁可不呈现）
    return grid if len(grid) >= 2 else None


def _trim_grid(grid: list[list[str]]) -> list[list[str]]:
    """网格修缮：规整单元格、丢全空列、截掉图表残渣行。

    图表轴刻度/图例被误收进表体时，表现为「数值格远少于峰值的稀疏行」；
    最后一个数值格达半数峰值的行之后的一切整段截断，尾部孤立数字行继续截。
    对齐不了的残行一律丢弃：对齐不了的数据宁可不给，也绝不许错位。
    """
    if not grid:
        return []
    grid = [[re.sub(r"\s+", " ", str(c or "")).strip() for c in row] for row in grid]
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    keep = [j for j in range(width) if any(r[j] for r in grid)]
    if not keep:
        return []
    grid = [[r[j] for j in keep] for r in grid]
    counts = [sum(1 for c in row[1:] if _cell_numeric([c])) for row in grid]
    peak = max(counts, default=0)
    last = -1
    for i, n in enumerate(counts):
        if n >= max(1, 0.5 * peak):
            last = i
    grid = grid[:last + 1]
    while len(grid) > 1 and sum(1 for c in grid[-1] if c) <= 1:
        grid.pop()
    return grid


def _table_dict(grid: list[list[str]], caption: str,
                *, folded: bool = False) -> dict | None:
    """过完整性校验后转成 {caption, header, rows}；不达标返回 None。

    folded=True 表示网格已经过词坐标重建（表头是折叠后的单行），跳过再折叠。
    """
    grid = _trim_grid(grid)
    if not _plausible_grid(grid):
        return None
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    if not folded and _is_two_row_header(grid):
        header = [grid[0][j] if grid[0][j] == grid[1][j] or not grid[0][j]
                  else (f"{grid[1][j]}@{grid[0][j]}" if _NUM_CELL_RE.match(grid[0][j])
                        else f"{grid[1][j]}({grid[0][j]})")
                  for j in range(width)]
        return {"caption": caption, "header": header, "rows": grid[2:]}
    return {"caption": caption, "header": grid[0], "rows": grid[1:]}


# ---------------------------------------------------------------- 候选审计与数字回源
_TABLE_NUM_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")


def _table_numbers(table: dict) -> list[str]:
    """表里出现过的全部数字 token（去重保序）。"""
    cells = list(table.get("header") or [])
    for row in table.get("rows") or []:
        cells.extend(row or [])
    toks: list[str] = []
    for cell in cells:
        for tok in _TABLE_NUM_TOKEN_RE.findall(str(cell)):
            tok = tok.strip(",").strip()
            if tok and tok not in toks:
                toks.append(tok)
    return toks


def _band_text(page, band) -> str:
    """表注辖域内的原始词序列（空格连接），作数字回源校验的基准。"""
    return " ".join(str(w[4]) for w in _band_words(page, band))


def _unverified_numbers(table: dict, band_text: str) -> list[str]:
    """数字回源校验：表里每个数字 token 必须能在辖域原始词序列中逐字找回。

    返回找不回的 token 列表——非空即代表这张表存在「来历不明的数字」，
    按数据对齐契约整张判废（宁缺毋假）。无空格拼接串作降级比对，
    兼容小数点被 PDF 拆成独立文本块的版式。
    """
    toks = _table_numbers(table)
    if not band_text:
        return toks
    words = set(re.findall(r"[^\s]+", band_text))
    despaced = re.sub(r"\s+", "", band_text)
    bad: list[str] = []
    for tok in toks:
        if tok in words or tok in despaced:
            continue
        if tok not in bad:
            bad.append(tok)
    return bad


def _grid_score(header: list[str], rows: list[list[str]]) -> float:
    """网格质量分：表头完整度 + 满格率 + 列形态一致性；宽表重罚防列爆。"""
    width = max(1, len(header))
    score = 30.0 * sum(1 for h in header if str(h).strip()) / width
    if rows:
        filled = sum(sum(1 for c in r if str(c).strip()) for r in rows)
        score += 40.0 * filled / (width * len(rows))
    consistent = 0
    for j in range(width):
        vals = [str(r[j]).strip() for r in rows if j < len(r) and str(r[j]).strip()]
        if not vals:
            continue
        nums = sum(1 for v in vals if _NUM_CELL_RE.match(v.strip().strip(".,;")))
        ratio = nums / len(vals)
        if ratio >= 0.75 or ratio <= 0.25:
            consistent += 1
    score += 20.0 * consistent / width
    if width > 9:
        score -= (width - 9) * 6.0
    if any(re.search(r"\btable\b", str(h), re.I) for h in header):
        score -= 40.0  # 表注文字被吸进表头 = 解析已失真
    score -= 15.0 * sum(1 for h in header if len(str(h).strip()) > 28)
    score -= 15.0 * sum(1 for r in rows for c in r if len(str(c).strip()) > 40)
    return score


def _best_table(page, band, caption: str) -> dict | None:
    """同一表注辖域内的多通道候选择优：find_tables 两策略 + 词坐标重建。

    全部候选先去重，再按网格质量分排序；逐个做数字回源校验，第一个
    「零来历不明数字」的候选入选；全部有缺失时退而取缺失率 ≤ 2% 的最高分者，
    否则放弃（宁缺毋假）。
    """
    band_text = _band_text(page, band)
    candidates: list[dict] = []
    for strategy in ("lines", "text"):
        for tab in _find_tables(page, strategy):
            b = tuple(tab.bbox)
            if not (min(b[2], band[2]) > max(b[0], band[0])
                    and min(b[3], band[3]) > max(b[1], band[1])):
                continue  # 表体不在该表注辖域内，不属于这张表
            d = _table_dict(_table_grid(page, tab), caption)
            if d:
                candidates.append(d)
    rebuilt = _band_grid(page, band)
    if rebuilt:
        d = _table_dict(rebuilt, caption, folded=True)
        if d:
            candidates.append(d)
    uniq: dict[tuple, list] = {}
    for d in candidates:
        key = (tuple(d["header"]), tuple(tuple(r) for r in d["rows"]))
        if key not in uniq:
            uniq[key] = [d, 0.0]
    # 词坐标重建是数据对齐契约的主通道（几何对齐 + 数字回源双校验）：
    # 与 find_tables 候选分差 ≤ 15 时一律优先——find_tables 的 text 策略
    # 常把相邻窄列并成一列（如 Methods/Year），满格率并不吃亏但表头已失真
    rebuilt_key = None
    if rebuilt:
        rebuilt_key = (tuple(rebuilt[0]), tuple(tuple(r) for r in rebuilt[1:]))
    for pair in uniq.values():
        key = (tuple(pair[0]["header"]), tuple(tuple(r) for r in pair[0]["rows"]))
        if rebuilt_key and key == rebuilt_key:
            pair[1] = 15.0
    ranked = sorted(uniq.values(),
                    key=lambda p: _grid_score(p[0]["header"], p[0]["rows"]) + p[1],
                    reverse=True)
    fallback = None
    for pair in ranked:
        d = pair[0]
        bad = _unverified_numbers(d, band_text)
        if not bad:
            return d
        total = max(1, len(_table_numbers(d)))
        if len(bad) / total <= 0.02 and fallback is None:
            fallback = d
    return fallback


def pdf_tables(pdf_bytes: bytes, limit: int = 3) -> list[dict]:
    """PDF 表格提取：表注辖域中心制 + 双通道候选 + 审计择优 + 数字回源校验。

    数据对齐契约（铁律）：
    1. 行列精准映射——词坐标重建以数字词锚定数据列、跨列分组表头折叠成单行，
       表头/方法名（Baseline vs. SOTA vs. Ours）/数值 100% 对应；图表轴刻度
       与图例按行签名整段截断，绝不混入表体。
    2. 降级兜底校验——每张入选表的全部数字 token 必须能在该表辖域的原始
       词序列里逐字找回，找不回即判废；多通道候选按网格质量分择优，宁缺毋假。

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
    seen: set[str] = set()
    try:
        for pno in range(len(doc)):
            page = doc.load_page(pno)
            captions = _table_captions(page)
            if not captions:
                continue
            for cap in captions:
                if cap[4] in seen:
                    continue
                band = _table_band(page, cap)
                if not band:
                    continue
                table = _best_table(page, band, cap[4])
                if not table:
                    continue
                seen.add(cap[4])
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


def _dedupe_tables(tables: list[dict]) -> list[dict]:
    """按表头 + 行内容去重，保留先到的（PDF 通道优先于 HTML 二次解析）。"""
    out: list[dict] = []
    seen: set[tuple] = set()
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        key = (tuple(str(h) for h in t.get("header") or []),
               tuple(tuple(str(c) for c in r) for r in t.get("rows") or []))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


async def _ensure_ours_tables(client: HttpClient, item: Item, assets: dict,
                             pdf_bytes: bytes, log) -> None:
    """素材侧前置断言：至少一张表里「本文方法」有数值。

    实测事故：HTML 表格的 rowspan/colspan 没展开时，表头还留着「+Ours」，
    右侧整列数值却飞了——模型照抄就写出「+Ours | — | — | —」。这里在把表交给
    模型之前先验一遍：缺 Ours 数值就二级降级（PDF 词坐标重建 → arXiv HTML →
    官方仓库 README），穷尽仍无则标记素材缺口，成稿侧改文字说明，绝不留白。
    """
    from ..tables_guard import has_ours_data, usable_tables

    tables = [t for t in (assets.get("tables") or []) if isinstance(t, dict)]
    if has_ours_data(usable_tables(tables, item), item):
        return
    log("表格断言：素材表里没有本文方法（Ours）的数值，触发二级降级深度解析")
    extra: list[dict] = []
    if pdf_bytes:
        try:
            extra.extend(pdf_tables(pdf_bytes, limit=8))
        except Exception:
            pass
    if not has_ours_data(usable_tables(extra, item), item):
        try:
            fb = await _arxiv_assets(client, item)
            extra.extend(fb.get("tables") or [])
        except Exception:
            pass
    if not extra:
        try:
            extra.extend(await _repo_tables(client, item))
        except Exception:
            pass
    merged = _dedupe_tables(tables + extra)
    assets["tables"] = merged
    if has_ours_data(usable_tables(merged, item), item):
        log(f"表格断言：二级解析已补齐本文方法数值（候选 {len(merged)} 张）")
    else:
        item.extra["ours_table_missing"] = True
        log("素材红线：多源解析后仍无本文方法数值 → 成稿撤表改文字，绝不留白")


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
    # 前置断言（Ours 零留白）：表可以有残缺的次要列，但本文方法那一行/列必须有数；
    # 缺了就二级降级重解析，仍缺则标记素材缺口，由成稿侧撤表改文字。
    await _ensure_ours_tables(client, item, assets, pdf_bytes, log)
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
        # arXiv / SSRN 这类托管方不是「正式收录」：若照抄 accepted=True，
        # 标题就会顶着「arXiv (Cornell University) 2026 |」的假 venue 前缀发出，
        # 既难看也违反「预印本不得挂 venue 标签」的信誉铁律
        from ..spec_tools import is_preprint_venue

        preprint = is_preprint_venue(item.venue)
        item.extra["publication"] = {"checked": True, "accepted": not preprint,
                                     "venue": item.venue,
                                     "source": "Crossref/EuropePMC"
                                     + ("（托管方为预印本服务器）" if preprint else "")}
    return summary
