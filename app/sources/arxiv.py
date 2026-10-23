"""模块 A-2 · arXiv 抓取 + 论文配图与指标表自动提取。

arXiv API 是 Atom XML，无需 key，但要守规矩：两次请求间隔 >= 3s。

素材提取策略（痛点 A 的修复核心，全部来自 arXiv HTML 版 `arxiv.org/html/{id}`）：
1. <figure> 按 caption 关键词分两槽挑图：「架构图 / pipeline」一张、「结果对比 /
   定性可视化」一张，两槽互斥（同一张图不重复占位）；
2. <table> 识别实验指标对比表（表头或单元格命中 PSNR / SSIM / LPIPS / mAP 等
   指标词，或首列出现 Ours / Proposed / 本文），最多 3 张转成结构化数据
   {header, rows, caption} 喂给大模型照抄进正文 table 块；正文里带指标数字的
   结论句另抽一份 result_snippets，保证「实验结果解读」有可核对的数值；
3. PDF 首页渲染（PyMuPDF）仅作为架构图缺失时的兜底，默认关闭——历史上曾把
   流水线拖挂 23 分钟，只允许配置 arxiv.pdf_fallback: true 显式开启。
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
# 架构槽关键词里刻意不含 "method"：teaser 图注常写 "Compared with CNN-based
# methods"，曾把定性对比大图误选成架构图
ARCH_HINTS = ("architect", "overview", "pipeline", "framework", "overall", "网络结构", "框架")
# 架构槽排除词：只看图注不看文件名（名为 comparison.png 的架构图仍可入选）
ARCH_EXCLUDE = ("comparison", "compared", "qualitative", "visual comparison")
RESULT_HINTS = ("comparison", "compared", "qualitative", "visual", "results", "examples", "teaser", "对比", "可视化", "效果")

# 指标表判定：表头或单元格文本里出现这些指标词，才认为是「实验结果对比表」
_METRIC_RE = re.compile(
    r"(?<![a-z0-9])(psnr|ssim|lpips|niqe|fid|mae|rmse|mse|map|miou|accuracy|bpp)(?![a-z0-9])",
    re.I,
)
# 正文句子里没有指标词、但带「参数量/速度/开销」数字（6.065M 参数、30 fps）同样值得引用
_PERF_WORD_RE = re.compile(
    r"parameter|params?\b|flops|macs|runtime|inference|fps|speed|latency|efficien|"
    r"参数量|推理|耗时",
    re.I,
)
# 首列出现 Ours / Proposed / 本文（含「Ours+XX」这类变体）也算
_OURS_RE = re.compile(r"^(ours|proposed|本文)", re.I)
# 转置表：方法名在表头列而非首列（首列是指标名）
_OURS_COL_RE = re.compile(r"^(ours|proposed|本文)", re.I)
# LaTeXML 残留清洗：图注前缀 "Figure 3 :"、引用 "[ 43 ]"、宏 "\uparrow"
_LABEL_HEAD = re.compile(r"^(?:figure|table|fig\.?|图|表)\s*\d+\s*[:：.]\s*", re.I)
_CITE_REF = re.compile(r"\[\s*\d+(?:\s*[,，–-]\s*\d+)*\s*\]")
_LATEX_JUNK = re.compile(r"\\[a-zA-Z]+\s*")
# 从任意 arXiv 链接（abs/html/pdf）里抠论文 id——熵视条目的 data-href 用
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|html|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", re.I)


def _clean_caption(text: str) -> str:
    """去掉 LaTeXML 的 "Figure 3 :" 式前缀与多余空白，超长时截到最后一个完整句。"""
    text = _LABEL_HEAD.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= 160:
        return text
    cut = text[:160]
    for sep in ("。", ". "):
        pos = cut.rfind(sep)
        if pos > 40:
            return cut[:pos + len(sep)].strip()
    return cut


def _clean_cell(text: str) -> str:
    r"""去掉单元格里的文献引用 "[ 43 ]" 与 LaTeX 宏（如 \uparrow）。"""
    text = _CITE_REF.sub("", str(text or ""))
    text = _LATEX_JUNK.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _method_hint(soup) -> str:
    """猜「本文方法名」（小写）。取论文标题的第一个词：标题多写作
    "Freqformer: Efficient ..."，冒号前的第一个词就是方法名。

    实验对比表的表头常把方法名写在最后一列（而不是写 Ours），超宽裁列时
    必须认出这一列，否则「本文方法」整列被砍掉，模型就只剩一句「素材未提供数值」。
    """
    node = soup.select_one("h1.ltx_title_document")
    title = node.get_text(" ", strip=True) if node else ""
    if not title:
        return ""
    first = re.split(r"[\s:：]", title)[0].strip(".,;()[]")
    return first.lower() if len(first) >= 4 and re.search(r"[A-Za-z]", first) else ""


def _is_ours_header(cell: str, hint: str) -> bool:
    """表头是不是「本文方法」列：字面 Ours/Proposed，或含标题里的方法名。"""
    if _OURS_COL_RE.match(cell):
        return True
    return bool(hint) and hint in cell.lower()


def _arxiv_id_from_url(url: str) -> str:
    m = _ARXIV_URL_RE.search(url or "")
    return m.group(1) if m else ""


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


def pick_figures_from_html(html: str, base_url: str) -> tuple[str, str, str, str]:
    """挑两张图，返回 (架构图 url, 图注, 结果图 url, 图注)，缺哪个哪个为空串。

    架构槽看 ARCH_HINTS，且图注命中 ARCH_EXCLUDE（"Compared with ..." 类定性
    对比图）的直接弃；结果槽看 RESULT_HINTS——低层视觉论文的 teaser 图通常
    就是「定性对比大图」，所以在结果槽里加权而非惩罚。
    两槽互斥：结果槽跳过与架构槽同一张的候选。图注统一过 _clean_caption。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    arch_cands: list[tuple[int, str, str]] = []
    result_cands: list[tuple[int, str, str]] = []
    for fig in soup.select("figure"):
        img = fig.select_one("img")
        if not img or not img.get("src"):
            continue
        src = abs_url(base_url, img["src"])
        if src.endswith(".svg"):
            continue
        caption_node = fig.select_one("figcaption")
        caption = caption_node.get_text(" ", strip=True) if caption_node else ""
        cap_l = caption.lower()
        blob = cap_l + " " + str(img.get("src", "")).lower()
        if any(bad in blob for bad in ("logo", "icon")):
            continue
        arch_hit = [h for h in ARCH_HINTS if h in blob]
        if arch_hit and not any(bad in cap_l for bad in ARCH_EXCLUDE):
            early = any(cap_l[:80].find(h) >= 0 for h in arch_hit)
            arch_cands.append((3 + len(arch_hit) + (2 if early else 0), src, _clean_caption(caption)))
        if any(h in blob for h in RESULT_HINTS):
            w = 4 + (2 if "teaser" in blob else 0) + (1 if "qualitative" in blob else 0)
            result_cands.append((w, src, _clean_caption(caption)))
    arch_cands.sort(key=lambda x: x[0], reverse=True)
    result_cands.sort(key=lambda x: x[0], reverse=True)
    arch = arch_cands[0] if arch_cands else ("", "", "")
    res = ("", "", "")
    for cand in result_cands:
        if cand[1] != arch[1]:
            res = cand
            break
    return arch[1], arch[2], res[1], res[2]


def extract_tables(html: str, limit: int = 3, max_cols: int = 9) -> list[dict]:
    """从 HTML 版论文里抽「实验指标对比表」，返回 [{header, rows, caption}]。

    只收有指标特征（PSNR/SSIM/... 或首列 Ours/Proposed/本文）的表——
    超参配置表、数据集统计表对读者没价值，直接丢弃。行截到 12 行；
    列数超宽时优先保住首列 + Ours 列，再从右往左补齐近邻强对手——
    对比表的方法按强弱从右往左排，从左边补会把真正要对比的 SOTA 全挤掉。
    Ours 列认不出时（表头只写方法名）用标题首词兜底，仍认不出就取最右列。
    单元格与表注都过 _clean_cell / _clean_caption 清洗 LaTeXML 残留。

    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    hint = _method_hint(soup)
    out: list[dict] = []
    for tbl in soup.select("table"):
        if len(out) >= limit:
            break
        # 表注：LaTeXML 把表包在 <figure class="ltx_table"> 里配 figcaption
        wrapper = tbl.find_parent("figure")
        cap_node = wrapper.select_one("figcaption") if wrapper else tbl.find_previous("figcaption")
        caption = _clean_caption(cap_node.get_text(" ", strip=True)) if cap_node else ""

        head_cells = [_clean_cell(th.get_text(" ", strip=True)) for th in tbl.select("thead th")]
        body_trs = tbl.select("tbody tr")
        if not body_trs:  # 无 tbody 的裸表
            body_trs = [tr for tr in tbl.select("tr") if not tr.find_parent("thead")]
        body_rows: list[list[str]] = []
        for tr in body_trs:
            cells = [_clean_cell(c.get_text(" ", strip=True))
                     for c in tr.find_all(["td", "th"], recursive=False)]
            if len(cells) >= 2 and sum(1 for c in cells if c) >= 2:
                body_rows.append(cells)
        if not head_cells and body_rows:
            head_cells, body_rows = body_rows[0][:], body_rows[1:]
        if not head_cells or len(body_rows) < 2:
            continue

        blob = " ".join(head_cells) + " " + " ".join(" ".join(r) for r in body_rows)
        has_metric = bool(_METRIC_RE.search(blob))
        has_ours = any(_OURS_RE.match(r[0]) for r in body_rows if r) or any(
            _is_ours_header(h, hint) for h in head_cells
        )
        if not (has_metric or has_ours):
            continue

        # 超宽表裁列：保首列（指标名/方法名）+ Ours 列，再从右往左补到 max_cols
        width = max([len(head_cells)] + [len(r) for r in body_rows])
        if width > max_cols:
            keep = {0}
            ours = {ci for ci, h in enumerate(head_cells) if ci and _is_ours_header(h, hint)}
            if ours:
                keep |= ours
            else:
                # 表头既没写 Ours 也没出现方法名：对比表的本文方法几乎总在最右列
                keep.add(width - 1)
            for ci in list(range(1, 3)) + list(range(width - 1, 0, -1)):
                if len(keep) >= max_cols:
                    break
                keep.add(ci)
            keep = sorted(keep)
            head_cells = [head_cells[ci] if ci < len(head_cells) else "" for ci in keep]
            body_rows = [[(r[ci] if ci < len(r) else "") for ci in keep] for r in body_rows]

        out.append({
            "header": [h[:24] for h in head_cells],
            "rows": [[c[:24] for c in r] for r in body_rows[:12]],
            "caption": caption,
        })
    return out


_SENT_SPLIT = re.compile(
    r"(?<=[。!?])\s+"
    r"|(?<!e\.g\.)(?<!i\.e\.)(?<!Fig\.)(?<!et al\.)(?<=\.)\s+(?=[A-Z(])"
)


def extract_result_snippets(html: str, limit: int = 8) -> list[str]:
    """抽正文里「带指标数字的结论句」，给模型写实验解读时照抄具体数值。

    对比表可能被裁列、也可能论文只用图展示结果，这时正文里的
    「PSNR 提升 0.31 dB」「参数量仅 6.07M」是唯一可信的数字来源。
    只收同时带指标词（或参数量/速度这类开销词）与数字、长度 40–300 的句子，去重后最多 limit 条。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    snippets: list[str] = []
    seen: set[str] = set()
    for node in soup.select("p, li"):
        if node.find_parent("nav"):
            continue  # 目录/导航列表整段是「2.1 xxx 2.2 yyy」，不是结论句
        text = _clean_cell(node.get_text(" ", strip=True))
        if len(text) < 40:
            continue
        for sent in _SENT_SPLIT.split(text):
            sent = sent.strip()
            if not (40 <= len(sent) <= 300):
                continue
            if not (_METRIC_RE.search(sent) or _PERF_WORD_RE.search(sent)):
                continue
            if not re.search(r"\d", sent):
                continue
            key = sent.lower()
            if key in seen:
                continue
            seen.add(key)
            snippets.append(sent[:300])
            if len(snippets) >= limit:
                return snippets
    return snippets


async def _safe_download(client: HttpClient, url: str, referer: str,
                         timeout: float = 12.0) -> str:
    """图片下载带总时长上限。

    httpx 的 timeout 是「单次读写间隔」超时，慢链路持续吐字节时永远不触发，
    实测有图能拖到 70s 以上，所以这里再加一道整包下载的硬期限。
    """
    if not url:
        return ""
    try:
        return await asyncio.wait_for(client.download_image(url, referer=referer), timeout=timeout)
    except Exception:
        return ""


async def extract_assets(client: HttpClient, arxiv_id: str, item_title: str = "",
                         pdf_fallback: bool = False) -> dict:
    """一趟抓取 HTML 版，同时取指标表、带数字的结论句与双图。

    返回 {"arch": (本地图片路径, 图注), "result": (本地图片路径, 图注),
    "tables": [...], "snippets": [...]}，抓不到的部分为空值，渲染层会自动降级。
    PDF 首页渲染很重（要下整个 PDF），默认关闭，且只在架构图缺失时才兜底。

    表格与结论句先抽、图片后下：图片最容易卡在慢链路上，不能让它连累数据。
    """
    short = re.sub(r"v\d+$", "", arxiv_id)
    html_url = f"https://arxiv.org/html/{arxiv_id}"
    assets: dict = {"arch": ("", ""), "result": ("", ""), "tables": [], "snippets": []}
    try:
        html = await client.text(html_url)
        assets["tables"] = extract_tables(html, limit=3)
        assets["snippets"] = extract_result_snippets(html, limit=8)
        arch_url, arch_cap, res_url, res_cap = pick_figures_from_html(html, html_url)
        if arch_url:
            local = await _safe_download(client, arch_url, html_url)
            if local:
                assets["arch"] = (local, arch_cap or item_title)
        if res_url and res_url != arch_url:
            local = await _safe_download(client, res_url, html_url)
            if local:
                assets["result"] = (local, res_cap or "实验结果对比图")
    except Exception:
        pass

    if assets["arch"][0] or not pdf_fallback:
        return assets
    # 兜底：PDF 首页渲染（需要 pymupdf，没装就跳过）
    try:
        try:
            import pymupdf as fitz  # PyMuPDF ≥ 2.0 的规范名
        except ImportError:
            import fitz  # 旧版兼容

        data, _ = await client.get_bytes(f"https://arxiv.org/pdf/{short}")
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.page_count:
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            out = settings.image_dir / f"arxiv_{short.replace('/', '_')}_p1.png"
            pix.save(str(out))
            assets["arch"] = (str(out), item_title)
    except Exception:
        pass
    return assets


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
    """给已入选的论文补「架构图 + 结果图 + 指标表 + 带数字的结论句」（串行 + 硬超时，避免拖住流水线）。

    熵视条目自带列表页缩略图（item.images 非空）但缺图注与表格，同样要
    enrich——跳过条件只看 extra["figure_caption"] 是否已存在，arxiv_id 允许
    从 item.url 反抠。成功后 images 整体重排为「架构图 → 结果图」（丢列表页
    缩略图，保证序位稳定），extra["figure_captions"] 与 images 等长对齐供渲染
    层按序取注；图注另写 figure_caption / figure_caption_2，表写 extra["tables"]，
    正文数字写 extra["result_snippets"]。

    注意：必须在评分选稿之后调用——只给真正要写的那几条补，既省请求也保证
    「选中的条目一定有数据」。
    """
    cfg = topics.raw.get("academic", {}).get("arxiv", {})
    pdf_fallback = bool(cfg.get("pdf_fallback", False))
    for item in sorted(items, key=lambda x: x.score, reverse=True)[:limit]:
        arxiv_id = item.extra.get("arxiv_id") or _arxiv_id_from_url(item.url)
        if not arxiv_id or item.extra.get("figure_caption"):
            continue
        try:
            assets = await asyncio.wait_for(
                extract_assets(client, arxiv_id, item.title, pdf_fallback=pdf_fallback), timeout=60.0
            )
        except Exception:
            continue
        arch, res = assets["arch"], assets["result"]
        images: list[str] = []
        caps: list[str] = []
        if arch[0]:
            images.append(arch[0])
            caps.append(arch[1])
            item.extra["figure_caption"] = arch[1]
        if res[0]:
            images.append(res[0])
            caps.append(res[1])
            item.extra["figure_caption_2"] = res[1]
        if images:
            item.images = images
            item.extra["figure_captions"] = caps
            item.image_url = images[0]
        if assets["tables"]:
            item.extra["tables"] = assets["tables"]
        if assets["snippets"]:
            item.extra["result_snippets"] = assets["snippets"]
        await asyncio.sleep(1.0)
